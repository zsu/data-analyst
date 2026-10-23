"""Find and load source packs (../../sources/<id>/). Standard library only.

A pack is a folder with policy.json (required), source.md (required), and either
  fetch.py      a deep pack's own command line; scripts/fetch.py hands
                it the arguments unchanged, or
  endpoints.json and/or adapter.py   a standard pack run by core (generic_rest + routes + package).
"""
from __future__ import annotations

import importlib.util
import os
import math
import re
import sys
from dataclasses import dataclass

from core import packfiles
from core import policy as pol
from core.env import SKILL_DIR

SOURCES_DIR = os.path.join(SKILL_DIR, "sources")


@dataclass
class Pack:
    id: str
    dir: str
    policy: dict
    endpoints: dict | None
    adapter: object | None

    @property
    def deep_cli(self) -> str | None:
        p = os.path.join(self.dir, "fetch.py")
        return p if os.path.exists(p) else None

    @property
    def source_md(self) -> str:
        return os.path.join(self.dir, "source.md")

    def has(self, fn: str) -> bool:
        return self.adapter is not None and callable(getattr(self.adapter, fn, None))


def pack_ids() -> list[str]:
    if not os.path.isdir(SOURCES_DIR):
        return []
    return sorted(d for d in os.listdir(SOURCES_DIR)
                  if packfiles.is_pack(os.path.join(SOURCES_DIR, d)))


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load(pid: str) -> Pack:
    d = os.path.join(SOURCES_DIR, pid)
    if not packfiles.is_pack(d):
        known = ", ".join(pack_ids())
        raise SystemExit(f"unknown source {pid!r}. Known sources: {known}")
    p = pol.load(d)
    eps = packfiles.read_json(d, "endpoints.json")
    adapter = None
    ad_path = os.path.join(d, "adapter.py")
    if os.path.exists(ad_path):
        if d not in sys.path:
            sys.path.insert(0, d)
        safe = re.sub(r"\W", "_", pid)
        adapter = _load_module(f"pack_{safe}_adapter", ad_path)
    return Pack(pid, d, p, eps, adapter)


def all_packs() -> list[Pack]:
    return [load(i) for i in pack_ids()]


# ---------------------------------------------------------------------- routing a question to packs
_STOP = set("a an the of in on for to and or by with from what how much many is are was were did does do "
            "since between over last year years data show chart compare trend latest".split())


def _tokens(s: str) -> list[str]:
    """Lower-case words without stop words and a plural s. Hyphenated or dotted terms stay whole as well
    ("u-6", "pm2.5", "16-24"), so short parts that are dropped alone ("u", "6") can still match."""
    out = []
    for term in re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", s.lower()):
        parts = re.findall(r"[a-z0-9]+", term)
        for w in ([term] if len(parts) > 1 else []) + parts:
            if w in _STOP or len(w) < 2:
                continue
            out.append(w[:-1] if len(w) > 3 and w.endswith("s") and w.isalpha() else w)
    return out


_FIELDS = (("keywords", 3), ("questions", 2), ("domains", 2), ("title", 2), ("agency", 1))


def _vocab(P: dict) -> dict[str, list[tuple[str, set[str]]]]:
    """{field: [(text, its tokens)]} for the routing fields of a policy."""
    out = {}
    for field, _w in _FIELDS:
        vals = P.get(field) or []
        out[field] = [(v, set(_tokens(v))) for v in ([vals] if isinstance(vals, str) else vals)]
    return out


def route(question: str, limit: int = 6) -> list[dict]:
    """Rank packs for a question by keyword overlap with each policy's title, agency, domains, keywords and
    questions. A hint, not a decision: SKILL.md tells the model to check references/routing.md and to
    split the question into measures first.
    Each question word counts once per field, weighted by how rare it is across packs (a word every pack uses,
    like "rate", counts little), and a keyword whose words all appear in the question adds a bonus. So a pack
    with ten "... rate" keywords does not outscore the pack the question is about."""
    q = set(_tokens(question))
    packs = all_packs()
    vocab = {p.id: _vocab(p.policy) for p in packs}
    df: dict[str, int] = {}
    for v in vocab.values():
        for w in {t for entries in v.values() for _, toks in entries for t in toks}:
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log((len(packs) + 1) / (n + 0.5)) for w, n in df.items()}
    ranked = []
    for p in packs:
        P, V = p.policy, vocab[p.id]
        score, why = 0.0, []
        for field, w in _FIELDS:
            hit = q & {t for _, toks in V[field] for t in toks}
            if not hit:
                continue
            # a word of a phrase the question does not contain ("motor" of "motor vehicle crashes") counts half;
            # a whole keyword ("cpi") or a whole phrase counts in full
            whole = {t for _, toks in V[field] if len(toks) == 1 or toks <= q for t in toks}
            score += w * sum(idf.get(t, 0.0) * (1 if t in whole else 0.5) for t in hit)
            for v, toks in V[field]:
                if len(toks) > 1 and toks <= q:                      # a whole multi-word keyword or phrase
                    score += w * sum(idf.get(t, 0.0) for t in toks) / 2
                    why.insert(0, f"{field}: {v}")
                elif toks & q:                                      # only some words: say which
                    why.append(f"{field}: {v}" if len(toks) == 1 else
                               f"{field}: {' '.join(sorted(toks & q))} (one word of {v!r})")
        if p.id in q or p.id.replace("-", " ") in question.lower():
            score += 5
        if score:
            ranked.append({"source": p.id, "title": P["title"], "score": round(score, 1), "depth": P["depth"],
                           "auth": P["auth"]["required"], "matched": why[:4]})
    return sorted(ranked, key=lambda r: -r["score"])[:limit]

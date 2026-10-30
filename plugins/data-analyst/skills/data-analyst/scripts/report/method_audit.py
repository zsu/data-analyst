#!/usr/bin/env python
"""Checks shared by audit_report.py (reports, papers) and audit_answer.py (quick answers).

<out>/decisions.json is the method record of every product: the critical questions and how each was
answered, the literature searched, the references opened and the assumptions with their basis. report.json
may carry "references" and "assumptions" too; merged() joins both (report.json wins on the same id or
parameter).
{
  "questions": [{"id": "dollar_basis", "question": "...", "blocking": false, "asked": true,
                 "options": ["Real, CPI-U (recommended)", "Nominal", "Don't know — decide for me"],
                 "answer": "dont_know", "how": "dont_know | skipped | not_asked",
                 "reason_code": "(not asked only) no_user | settled_by_request", "request": "(settled_by_request: "
                 "the user's own words)", "resolution": "real, CPI-U", "assumption": "dollar_basis",
                 "rationale": "(blocking only)", "followed_up": true}],
  "literature_search": [{"query": "...", "date": "2026-10-20", "tool": "WebSearch", "found": ["cpi_hb"],
                         "note": "..."}],          # or [{"unavailable": true, "reason_code": "no_search_tool"
                                                   #      | "network_blocked", "reason": "(detail)"}]
  "disclosures": {"<question id>": "...", "literature": "..."},   # one line the answer carries per escape
  "references": [{"id": "cpi_hb", "citation": "...", "url": "https://...", "accessed": "2026-10-20",
                  "opened": true, "used_for": "deflator", "quote": "(a sentence of the page)"}],
  "assumptions": [{"parameter": "dollar_basis", "value": "...", "basis": "precedent", "evidence": "... [@cpi_hb]",
                   "why_not_empirical": "(judgment only)", "sensitivity": "(judgment only: a parameter in "
                   "exhibits/sensitivity.csv)"}],
  "data_needs": [{"id": "state_cpi", "data": "State price indexes, 2015-2025", "improves": ["dollar_basis"],
                  "effect": "an empirical deflator in place of a judgment call", "how_to_get": "BLS regional CPI",
                  "from_user": false}],      # or [] with "data_needs_note": why no data would help
}
Rules (all FAIL-level):
  questions    an asked question offers exactly one "Don't know" option and no "Skip" option; a question not
               asked has reason_code no_user (nobody can answer: a headless or scheduled run) or
               settled_by_request (with "request", the user's quoted words, which then count as the answer); a
               skipped, unanswered or no_user question counts as "Don't know" and needs a resolution tied to an
               assumption that is not "user"; a blocking question (what the user means) decided without the
               user needs a rationale (the literal reading), and a skipped one needs one follow-up first
  literature   at least one search entry with its query, date and what it found (an empty "found" with a note
               is valid); a search the environment cannot run is unavailable with reason_code no_search_tool
               or network_blocked
  disclosures  no_user questions and unavailable searches are claims no script can check: each needs a line in
               "disclosures" ({question id or "literature": text}) that the answer or report carries
  references   citation, URL or DOI, accessed date and opened: true, backed by a snapshot that
               `fetch.py --cite <url> --id <id> --out <out>` saved (references/opened.json: same URL, file
               unchanged since); a "quote" must be in that snapshot; every [@id] resolves
  assumptions  basis is one of empirical > literature > precedent > user > judgment (strongest first);
               empirical, literature and precedent need evidence: empirical names a registered {{fact}} or an
               existing CSV whose file name, a column or a row names the parameter; literature and precedent
               cite an opened [@ref] whose quote was found in its snapshot, and a numeric value from them
               (an elasticity, a factor, a threshold) states why_not_empirical: the data should estimate it
               with the literature's model when they can; user points to a question
               the user answered or quotes the request ("request"); judgment states why_not_empirical and
               names its row in exhibits/sensitivity.csv
  sensitivity  each exhibits/sensitivity.csv row was run, not typed: headline_base and headline_alt are
               registered facts (or fact_base / fact_alt name them), so --rerun reproduces them; conclusion_changes
               starts with yes or no and agrees with them (a sign flip, or opposite sides of an optional
               threshold column, is a change)
  data_needs   every judgment assumption and every benchmark row "not checked" is named in some data_needs
               entry's "improves" (the data that would settle it, with its effect and how to get it); an empty
               list needs a data_needs_note
  benchmark    exhibits/benchmark.csv exists with rows for the headline measures ("not checked" is a valid value;
               every fact registered with headline=True has a row, by name, label or value, and at least one
               fact is a headline);
               numeric computed and published agree within the published rounding, or a note column explains
               the difference; no row says it matches when it does not (computed values that are not in
               facts.json are counted as cross-checks, not failed)
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata

sys.dont_write_bytecode = True

csv.field_size_limit(2**31 - 1)   # agency CSV cells can exceed the 128 KiB default (see core/__init__.py)

BASES = ("empirical", "literature", "precedent", "user", "judgment")      # strongest first
NO_ANSWER = ("dont_know", "skipped", "not_asked")
NOT_ASKED_CODES = {"no_user": "nobody could answer (a headless or scheduled run)",
                   "settled_by_request": "the request already settles it"}
UNAVAILABLE_CODES = {"no_search_tool": "no web search or fetch tool exists in this environment, not even one "
                                       "that could be loaded",
                     "network_blocked": "the environment's network blocks every search and fetch",
                     "tool_unavailable": "the search and fetch tools exist but failed for the whole run (a usage "
                                         "limit or an outage): say when they were tried"}
DONT_KNOW_RE = re.compile(r"(?i)^\W*(?:i\s+)?don['’]?t\s+know")
SKIP_RE = re.compile(r"(?i)^\W*skip\b")
CITE_RE = re.compile(r"\[@[^\]]*\]")
SENS_COLS = ("parameter", "base", "alternative", "headline_base", "headline_alt", "conclusion_changes")
BENCH_COLS = ("measure", "period", "computed", "published", "source")


def load(base: str) -> dict:
    p = os.path.join(base, "decisions.json")
    if not os.path.exists(p):
        return {}
    raw = open(p, "rb").read()
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:                 # saved in a Windows code page: read it, and say so
        print("note: decisions.json is not UTF-8; read as cp1252. Save it as UTF-8.")
        return json.loads(raw.decode("cp1252", errors="replace"))


def merged(base: str, R: dict | None = None) -> dict:
    """R (report.json, or {}) with decisions.json's questions, literature, references and assumptions joined in."""
    R = dict(R or {})
    D = load(base)
    refs = {r.get("id"): r for r in D.get("references", [])}
    refs.update({r.get("id"): r for r in R.get("references", [])})
    asm = {a.get("parameter"): a for a in D.get("assumptions", [])}
    asm.update({a.get("parameter"): a for a in R.get("assumptions", [])})
    for k, v in (("references", list(refs.values())), ("assumptions", list(asm.values()))):
        if v:
            R[k] = v
    for k in ("questions", "literature_search", "data_needs"):
        if D.get(k) or R.get(k):
            R[k] = list(D.get(k, [])) + [x for x in R.get(k, []) if x not in D.get(k, [])]
    if D.get("disclosures") or R.get("disclosures"):
        R["disclosures"] = {**(D.get("disclosures") or {}), **(R.get("disclosures") or {})}
    if "data_needs" in D or "data_needs" in R:
        R.setdefault("data_needs", D.get("data_needs", []))
    if D.get("data_needs_note") and not R.get("data_needs_note"):
        R["data_needs_note"] = D["data_needs_note"]
    for k in ("audit_allow", "rounding_ties_ok"):
        if D.get(k):
            have = list(R.get(k, []))
            R[k] = have + [x for x in D[k] if x not in have]
    return R


def cited_refs(text: str) -> set[str]:
    return {i for g in CITE_RE.findall(text) for i in re.findall(r"@([A-Za-z0-9_-]+)", g)}


def _settled(q: dict) -> bool:
    """Not asked because the user's own words settle it (they are quoted in "request")."""
    return q.get("asked") is False and q.get("reason_code") == "settled_by_request" \
        and bool(str(q.get("request") or "").strip())


def _no_answer(q: dict) -> bool:
    if _settled(q):
        return False
    return q.get("answer") in (None, "", *NO_ANSWER) or q.get("asked") is False and not q.get("answer")


def escapes(R: dict) -> list[tuple[str, str]]:
    """(disclosure key, what was claimed) for every claim no script can check: questions not asked because
    nobody could answer, and literature searches recorded as unavailable."""
    out = []
    for q in R.get("questions", []):
        if q.get("asked") is False and q.get("reason_code") == "no_user":
            out.append((q.get("id") or q.get("question", "?")[:40], f"question not asked: {NOT_ASKED_CODES['no_user']}"))
    if any(s.get("unavailable") for s in R.get("literature_search") or []):
        codes = sorted({s.get("reason_code", "") for s in R["literature_search"] if s.get("unavailable")})
        out.append(("literature", "literature search not run: " + "; ".join(
            UNAVAILABLE_CODES.get(c, f"reason_code {c!r} is not valid" if c else "no valid reason_code") for c in codes)))
    return out


def _read_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        return list(rd.fieldnames or []), list(rd)


# ---- references opened: the snapshot fetch.py --cite saved (scripts/core/cite.py)
def _opened_log(base: str) -> dict:
    p = os.path.join(base, "references", "opened.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _url_key(u) -> str:
    u = str(u or "").strip().split("#")[0]
    return re.sub(r"(?i)^https?://", "", u).rstrip("/").lower()


def _words(s: str) -> str:
    """Text reduced to lowercase words, so a quote from a page's Markdown rendering matches its HTML."""
    return " ".join(re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", html.unescape(s)).lower()).split())


def _snapshot_text(path: str) -> str | None:
    """The words of a saved page; None for a PDF when pypdf is not installed (the quote is then not checked)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return None
        try:
            return " ".join(p.extract_text() or "" for p in PdfReader(path).pages)
        except Exception:                        # a damaged or encrypted PDF: no text to search
            return None
    text = open(path, "rb").read().decode("utf-8", "replace")
    if ext in (".html", ".xml"):
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def _check_opened(rid: str, r: dict, opened: dict, base: str) -> tuple[bool, str, str | None]:
    """(ok, message, quote): quote is "found", "uncheckable" (a PDF without pypdf) or None (no quote given)."""
    url = r.get("url") or r.get("doi") or "<url>"
    e = opened.get(rid)
    if not e:
        return False, (f"reference {rid}: \"opened\": true, but references/opened.json has no snapshot of it: open "
                       f"it with `fetch.py --cite {url} --id {rid} --out <out>` (or add --from-file <path> with the "
                       "text your own fetch tool returned); never cite what you have not opened"), None
    if r.get("url") and _url_key(e.get("url")) != _url_key(r["url"]):
        return False, f"reference {rid}: the snapshot is of {e.get('url')}, not {r['url']}: cite the page you opened", None
    path = os.path.join(base, str(e.get("file") or ""))
    if not os.path.isfile(path) or _h(path) != e.get("sha256"):
        return False, (f"reference {rid}: {e.get('file')} is missing or changed since it was opened: run "
                       "fetch.py --cite again"), None
    msg = f"reference {rid}: opened {str(e.get('opened', ''))[:10]} ({e.get('via')})"
    q = str(r.get("quote") or "").strip()
    if not q:
        return True, msg, None
    if len(_words(q)) < 20:
        return False, f"reference {rid}: quote {q!r} is too short to identify a passage: give a full sentence", None
    text = _snapshot_text(path)
    if text is None:
        print(f"note: reference {rid}: the quote was not checked ({e.get('file')} is a PDF and pypdf is not installed)")
        return True, msg + "; quote not checkable", "uncheckable"
    if _words(q) not in _words(text):
        return False, (f"reference {rid}: the quote is not in {e.get('file')}: quote the page's own words "
                       "(an exact sentence, not a paraphrase)"), None
    return True, msg + "; quote found", "found"


def _key(s) -> str:
    return re.sub(r"[\W_]+", "_", str(s or "").lower()).strip("_")


def _csv_names(path: str, param: str, max_rows: int = 5000) -> bool:
    """The CSV's file name, a column or a cell names the parameter (an empirical estimate's own table)."""
    k = _key(param)
    if not k:
        return False
    if k in _key(os.path.splitext(os.path.basename(path))[0]):
        return True
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for n, row in enumerate(csv.reader(fh)):
            if n > max_rows:
                break
            if any(_key(c) == k or (n == 0 and k in _key(c)) for c in row):
                return True
    return False


def _sens_number(s) -> tuple[float, float] | None:
    """A sensitivity cell as (value, half a unit of its last digit as written): "1.10" -> (1.1, 0.005)."""
    m = _NUM.match(str(s or "").strip())
    if not m:
        return None
    txt = m.group(1).replace(",", "")
    return float(txt), 0.5 * 10 ** -len(txt.split(".")[1]) if "." in txt else 0.5


def _check_sensitivity(i: int, row: dict, facts: dict) -> tuple[bool, str]:
    """A sensitivity row is an experiment that was run: both headlines are registered facts (so --rerun
    reproduces them), and conclusion_changes agrees with them."""
    label = f"sensitivity.csv line {i} ({row.get('parameter', '')})"
    values, nums = _fact_values(facts), {}
    for side in ("base", "alt"):
        name, cell = str(row.get(f"fact_{side}") or "").strip(), row.get(f"headline_{side}")
        x = _sens_number(cell)
        if name:
            f = facts.get(name)
            try:
                v = float((f or {}).get("value"))
            except (TypeError, ValueError):
                return False, f"{label}: fact_{side} {name!r} is not a numeric fact in facts.json"
            if x and not _is_fact(x[0], x[1] + abs(x[0]) * 1e-9, [v]):
                return False, f"{label}: headline_{side} {cell} is not fact {name} ({v:g})"
            nums[side] = x[0] if x else v
            continue
        if x is None:
            return False, (f"{label}: headline_{side} {cell!r} is not one number: register the headline under each "
                           f"alternative as a fact in analysis.py and write it here (or name it in fact_{side})")
        if not _is_fact(x[0], x[1] + abs(x[0]) * 1e-9, values):
            return False, (f"{label}: headline_{side} {cell} is not a registered fact: run the "
                           f"{'alternative' if side == 'alt' else 'base case'} in analysis.py and register its "
                           f"headline (fact_{side} names it), so the rerun reproduces it")
        nums[side] = x[0]
    cc = str(row.get("conclusion_changes") or "").strip().lower()
    says = "yes" if re.match(r"(yes|true)\b", cc) else "no" if re.match(r"(no|false)\b", cc) else None
    if says is None:
        return False, f"{label}: conclusion_changes {cc!r}: start it with yes or no"
    b, a = nums["base"], nums["alt"]
    thr = _sens_number(row.get("threshold"))
    if thr:
        # a sign flip is a change too (a fall that becomes a rise), whichever side of the threshold both are on
        crosses, flips = (b - thr[0]) * (a - thr[0]) < 0, b * a < 0
        if (says == "yes") != (crosses or flips):
            why = (f"are on opposite sides of the threshold {thr[0]:g}" if crosses else
                   f"differ in sign" if flips else f"are on the same side of the threshold {thr[0]:g} and of zero")
            return False, (f"{label}: base {b:g} and alternative {a:g} {why}, so conclusion_changes must be "
                           f"{'yes' if crosses or flips else 'no'}")
    elif says == "no" and b * a < 0:
        return False, (f"{label}: the headline changes sign ({b:g} to {a:g}), so the conclusion changes: write yes, "
                       "or give the threshold the conclusion turns on in a threshold column")
    return True, f"{label}: {b:g} vs {a:g}, conclusion changes: {says}"


def check_method(R: dict, base: str, facts: dict) -> list[tuple]:
    """(check, ok, message) for the method record in R (already merged with decisions.json)."""
    out: list[tuple] = []

    def res(check, ok, msg):
        out.append((check, bool(ok), msg))

    refs = {r.get("id"): r for r in R.get("references", [])}
    asm = {a.get("parameter"): a for a in R.get("assumptions", [])}
    questions = R.get("questions", [])

    # ---- questions
    for q in questions:
        qid = q.get("id") or q.get("question", "?")[:40]
        if q.get("asked", True):
            opts = [str(o) for o in q.get("options", [])]
            dk, skip = [o for o in opts if DONT_KNOW_RE.match(o)], [o for o in opts if SKIP_RE.match(o)]
            res("questions", len(dk) == 1 and not skip, f"question {qid}: options ok" if len(dk) == 1 and not skip
                else f"question {qid}: offer exactly one \"Don't know — decide for me\" option and no \"Skip\" "
                     f"(found {len(dk)} Don't know, {len(skip)} Skip)")
        else:
            code = q.get("reason_code")
            if code not in NOT_ASKED_CODES:
                res("questions", False, f"question {qid}: not asked; set \"reason_code\": \"no_user\" only when nobody "
                    "can answer (a headless or scheduled run; with a question tool, ask), or \"settled_by_request\" "
                    "with \"request\": the user's own words")
                continue
            if code == "settled_by_request":
                res("questions", _settled(q), f"question {qid}: settled by the request ({q.get('request')!r})"
                    if _settled(q) else f"question {qid}: settled_by_request needs \"request\": the user's own words")
                continue
        if not _no_answer(q):
            continue
        how = q.get("how") or q.get("answer") or "not_asked"
        a = asm.get(q.get("assumption"))
        ok = bool(q.get("resolution")) and a is not None and (a.get("basis") or "").lower() != "user"
        res("questions", ok, f"question {qid}: {how}, decided as {q.get('resolution')!r} ({a.get('basis')})" if ok
            else f"question {qid}: {how} counts as \"Don't know\"; record the \"resolution\" and the \"assumption\" "
                 "(a parameter in the assumptions, basis other than user) that decides it")
        if q.get("blocking"):
            ok = bool(q.get("rationale")) and (how != "skipped" or not q.get("asked", True) or q.get("followed_up"))
            res("questions", ok, f"question {qid}: blocking, decided without the user by {q.get('rationale')!r}; "
                                 "state it in the opening paragraph of the answer, its disclosures line word for word" if ok else
                f"question {qid}: blocking (what the user means): ask one follow-up after a skip "
                "(\"followed_up\": true), then record the literal reading as \"rationale\"")

    # ---- literature search
    lit = R.get("literature_search") or []
    if not lit:
        res("literature", False, "no literature_search: search the agency methodology and research behind the "
                                 "definitions and parameters, and record each query, date and what it found")
    for i, s in enumerate(lit, start=1):
        if s.get("unavailable"):
            ok = s.get("reason_code") in UNAVAILABLE_CODES
            res("literature", ok, f"search {i}: unavailable ({s['reason_code']})" if ok else
                f"search {i}: unavailable needs \"reason_code\": \"no_search_tool\" (no search or fetch tool, not even "
                "a deferred one that could be loaded), \"network_blocked\" or \"tool_unavailable\" (the tools failed "
                "for the whole run); otherwise run the search")
            continue
        ok = bool(s.get("query")) and bool(s.get("date")) and ("found" in s or bool(s.get("note")))
        res("literature", ok, f"search {i}: {s.get('query')!r}" if ok else
            f"search {i}: needs query, date and found (ids of the references opened, or [] with a note)")
        for rid in s.get("found") or []:
            if rid not in refs:
                res("literature", False, f"search {i}: found {rid!r}, which is not in references")

    # ---- disclosures: what the gate cannot check, the reader must be told
    disc = R.get("disclosures") or {}
    for key, what in escapes(R):
        text = str(disc.get(key) or "").strip()
        res("disclosures", bool(text), f"{key}: disclose in the answer: {text}" if text else
            f"{key}: {what}. No script can check that claim: add \"disclosures\": {{\"{key}\": \"<one line>\"}} to "
            "decisions.json and carry that line in the answer or report")

    # ---- references
    opened, verified = _opened_log(base), {}             # verified: reference id -> quote status
    for rid, r in refs.items():
        miss = [k for k, v in (("citation", r.get("citation")), ("url or doi", r.get("url") or r.get("doi")),
                               ("accessed", r.get("accessed")), ("opened: true", r.get("opened") is True)) if not v]
        if miss:
            res("references", False, f"reference {rid}: needs {', '.join(miss)} (never cite what you have not opened)")
            continue
        ok, msg, quote = _check_opened(rid, r, opened, base)
        res("references", ok, msg)
        if ok:
            verified[rid] = quote
    for rid in sorted(cited_refs(json.dumps(R, ensure_ascii=False)) - set(refs)):
        res("references", False, f"[@{rid}] is cited but not in references")
    if not refs:
        res("references", False, "no references: cite the publications and research that the definitions, "
                                 "parameters and methods rely on")

    # ---- assumptions
    sens_path = os.path.join(base, "exhibits", "sensitivity.csv")
    sens_rows: dict[str, dict] | None = None
    sens_list: list[dict] = []
    if os.path.exists(sens_path):
        cols, sens_list = _read_csv(sens_path)
        miss = [c for c in SENS_COLS if c not in cols]
        res("assumptions", not miss, "exhibits/sensitivity.csv: columns ok" if not miss else
            f"exhibits/sensitivity.csv lacks columns {miss}")
        sens_rows = {r.get("parameter"): r for r in sens_list}
    bases: dict[str, int] = {}
    for p, a in asm.items():
        b = (a.get("basis") or "").lower()
        bases[b] = bases.get(b, 0) + 1
        ev = str(a.get("evidence") or "")
        if b not in BASES:
            res("assumptions", False, f"assumption {p!r}: basis {b or 'missing'!r} is not one of {' > '.join(BASES)}")
            continue
        if b in ("empirical", "literature", "precedent") and not ev.strip():
            res("assumptions", False, f"assumption {p!r} ({b}): needs \"evidence\"")
            continue
        if b == "empirical":
            paths = [x for x in re.findall(r"(?:exhibits|data|published)/[\w./-]+\.csv", ev)
                     if os.path.exists(os.path.join(base, x))]
            fs = [x for x in re.findall(r"\{\{\s*([A-Za-z_]\w*)", ev) if x in facts]
            ok = bool(fs) or any(_csv_names(os.path.join(base, x), p) for x in paths)
            why = ("name the {{fact}} that holds the estimate, or a data, exhibit or published CSV whose file name, "
                   f"a column or a row names the parameter {p!r}: an estimate the evidence cannot point to was not made")
        elif b in ("literature", "precedent"):
            ids = cited_refs(ev)
            ok = any(i in verified and verified[i] in ("found", "uncheckable") for i in ids)
            why = ("cite the opened reference it comes from as [@id], with a \"quote\" in that reference: the "
                   "sentence of the page that supports it (the audit finds it in the saved snapshot)")
            if ok and _NUM.match(str(a.get("value") or "").strip()) and not str(a.get("why_not_empirical") or "").strip():
                ok, why = False, ("a number taken from the literature or an agency is a parameter the data may be able "
                                  "to estimate: fit the model the literature uses in analysis.py (basis empirical, the "
                                  "published value as a sensitivity row), or state why_not_empirical (the data cannot "
                                  "identify it, or it is an official definition such as a threshold)")
        elif b == "user":
            q = next((q for q in questions if q.get("assumption") == p or q.get("id") == a.get("question")), None)
            quoted = bool(str(a.get("request") or "").strip())
            ok = quoted or (q is not None and not _no_answer(q))
            why = ("point to the question the user answered (\"question\": id), or quote the user's own words "
                   "(\"request\") when the request settles it")
        else:
            row = (sens_rows or {}).get(a.get("sensitivity") or p)
            ok = bool(str(a.get("why_not_empirical") or "").strip()) and row is not None \
                and bool(str(row.get("conclusion_changes") or "").strip())
            why = ("judgment is the last resort: estimate it from the data or adopt a published estimate; otherwise "
                   "state why_not_empirical and add its row to exhibits/sensitivity.csv (" + ", ".join(SENS_COLS) + ")")
        res("assumptions", ok, f"assumption {p!r}: {b} ok" if ok else f"assumption {p!r} ({b}): {why}")
    for i, row in enumerate(sens_list, start=2):
        res("sensitivity", *_check_sensitivity(i, row, facts))
    if not asm:
        res("assumptions", False, "no assumptions: list each key definition and parameter with its basis and evidence")
    # ---- data that would improve the estimates
    gaps = [p for p, a in asm.items() if (a.get("basis") or "").lower() == "judgment"]
    bench = os.path.join(base, "exhibits", "benchmark.csv")
    if os.path.exists(bench):
        gaps += [r.get("measure") for r in _read_csv(bench)[1]
                 if "not checked" in str(r.get("published", "")).lower() and r.get("measure")]
    needs = R.get("data_needs")
    covered = set()
    for i, d in enumerate(needs or [], start=1):
        miss = [k for k in ("data", "improves", "effect", "how_to_get") if not d.get(k)]
        res("data_needs", not miss, f"data need {d.get('id', i)}: ok" if not miss else
            f"data need {d.get('id', i)}: needs {', '.join(miss)}")
        imp = d.get("improves") or []
        covered |= {re.sub(r"\s+", " ", str(x)).strip().lower() for x in ([imp] if isinstance(imp, str) else imp)}
    for g in dict.fromkeys(gaps):
        ok = re.sub(r"\s+", " ", str(g)).strip().lower() in covered
        res("data_needs", ok, f"{g}: data that would improve it are listed" if ok else
            f"{g} rests on judgment or an unchecked benchmark: add a data_needs entry whose \"improves\" names it "
            f"(the assumption's parameter or the benchmark.csv measure, \"{g}\"), with the data, its effect, how to "
            "get it and from_user")
    if needs is None:
        res("data_needs", False, "no data_needs: list the data that would make the estimates more accurate, or "
                                 "[] with a data_needs_note saying why none would")
    elif not needs and not R.get("data_needs_note"):
        res("data_needs", False, "data_needs is empty: add a data_needs_note saying why no data would help")

    if bases:
        n = sum(bases.values())
        print("assumption bases: " + ", ".join(f"{k} {bases[k]}" for k in BASES if k in bases)
              + f" (judgment {100 * bases.get('judgment', 0) / n:.0f}%)")
    return out


_NUM = re.compile(r"^[^\d\-+.]*([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(%|percent|pct)?[^\d]*$", re.I)
_SAYS = ("match", "matches", "matched")      # a benchmark.csv column that states the verdict


_BOUND = re.compile(r"(?i)^\s*(>=|<=|>|<|≥|≤|over|more than|above|at least|under|below|less than|at most)\s*(.+)$")


def bench_number(s) -> tuple[float, float] | None:
    """A benchmark cell as (value, rounding tolerance): "$72,380" -> (72380, 5), "0.3" -> (0.3, 0.05),
    "1992473.0" -> (1992473, 0.5). None when the cell is not one number ("219 D / 24 R", "not checked").
    Zero decimals are dropped: pandas writes a whole published figure as "101.0", so "8.0" reads as 8."""
    m = _NUM.match(str(s or "").strip())
    if not m:
        return None
    txt = m.group(1).replace(",", "")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".") if set(txt.split(".")[1]) <= {"0"} else txt
    if "." in txt:
        tol = 0.5 * 10 ** -len(txt.split(".")[1])
    else:
        digits = txt.lstrip("+-")
        zeros = len(digits) - len(digits.rstrip("0")) if digits.strip("0") else 0
        # one or two trailing zeros are usually exact (570 laws, 931,960 million): rounding to tens shows in a note;
        # three or more (140,000) read as rounding, at most 0.1 percent
        tol = 0.5 if zeros < 3 else max(0.5, min(0.5 * 10 ** zeros, abs(float(txt)) * 0.001))
    return float(txt), tol


def _fact_values(facts: dict) -> list[float]:
    out = []
    for f in (facts or {}).values():
        try:
            out.append(float((f or {}).get("value")))
        except (TypeError, ValueError):
            pass
    return out


def _is_fact(x: float, tol: float, values: list[float]) -> bool:
    """x (as written in benchmark.csv, maybe rounded and in percent or another scale) equals a registered fact."""
    for v in values:
        for k in (1, 100, 0.01, 1e3, 1e-3, 1e6, 1e-6, 1e9, 1e-9, 1e12, 1e-12):
            if abs(abs(v) * k - abs(x)) <= tol * 1.0001 + 1e-12:     # a fall may be written without its sign
                return True
    return False


def bench_agree(c: tuple, pb: tuple) -> bool:
    """Computed and published agree within the published rounding, or the computed value's own (5.66 vs 5.65891)."""
    return abs(c[0] - pb[0]) <= max(pb[1] * 1.0001, abs(pb[0]) * 1e-6,
                                    min(c[1] * 1.0001, abs(pb[0]) * 0.005)) + 1e-12


def rounding_gap(c: tuple, pb: tuple, pub, note) -> bool:
    """A note says the publisher rounds, and the gap is one its figure can show: ten in its last digit, or its
    trailing zeros ($72,400: $50)."""
    digits = re.sub(r"\D", "", str(pub).split(".")[0])
    zeros = len(digits) - len(digits.rstrip("0")) if digits.strip("0") and "." not in str(pub) else 0
    return bool(re.search(r"(?i)\bround", str(note or ""))) and \
        abs(c[0] - pb[0]) <= max(10 * pb[1], 0.5 * 10 ** zeros if zeros else 0)


def _check_headlines(facts: dict | None, rows: list[dict]) -> list[tuple]:
    """Every fact registered with headline=True has a benchmark.csv row: its name or label as the measure, or
    its value as the computed number. A row that says "not checked" counts (data_needs then names it)."""
    if not facts:
        return []
    heads = {n: f for n, f in facts.items() if (f or {}).get("headline")}
    if not heads:
        return [("benchmark", False, "no headline facts: register the numbers the answer leads with using "
                                     "facts.add(..., headline=True); each needs a benchmark.csv row")]
    out = []
    for n, f in heads.items():
        keys = {_key(n), _key(f.get("label"))} - {""}
        try:
            v = [float(f.get("value"))]
        except (TypeError, ValueError):
            v = []
        hit = False
        for r in rows:
            c = bench_number(r.get("computed"))
            if _key(r.get("measure")) in keys or (c and v and _is_fact(c[0], max(c[1], abs(c[0]) * 1e-9), v)):
                hit = True
                break
        out.append(("benchmark", hit, f"headline {n}: benchmarked" if hit else
                    f"headline {n}: no benchmark.csv row: compare it with the agency's published figure (measure = "
                    f"{n} or its label, computed = its value), or add a row with published \"not checked\" and a "
                    "data_needs entry for it"))
    return out


def check_benchmark(base: str, facts: dict | None = None) -> list[tuple]:
    """benchmark.csv exists with rows; each numeric computed/published pair agrees within the published
    figure's rounding (or 0.0001 percent, for levels summed from rounded parts) unless a note explains the
    difference; a row never says it matches when it does not. Computed values that are not numbers in
    facts.json are counted (cross-checks are fine; the headline numbers the answer cites should be there)."""
    p = os.path.join(base, "exhibits", "benchmark.csv")
    if not os.path.exists(p):
        return [("benchmark", False, "exhibits/benchmark.csv missing: compare each headline number with the agency's "
                                     "published figure (" + ", ".join(BENCH_COLS) + ", url, accessed); "
                                     "\"not checked\" when none exists")]
    cols, rows = _read_csv(p)
    miss = [c for c in BENCH_COLS if c not in cols]
    if miss or not rows:
        return [("benchmark", False, f"exhibits/benchmark.csv: {'lacks columns ' + str(miss) if miss else 'no rows'}")]
    out, values = [], _fact_values(facts)
    unchecked = compared = uncited = 0
    for i, r in enumerate(rows, start=2):
        label = f"benchmark.csv line {i} ({r.get('measure', '')}, {r.get('period', '')})"
        if None in r or None in r.values():          # an unquoted comma shifts every column after it
            out.append(("benchmark", False, f"{label}: {len(cols) + len(r.get(None) or []) if None in r else 'fewer'} "
                                            f"fields against the header's {len(cols)}: quote fields that contain commas "
                                            "(write the file with csv or pandas)"))
            continue
        pub = str(r.get("published", ""))
        if "not checked" in pub.lower():
            unchecked += 1
            continue
        if not pub.strip():
            out.append(("benchmark", False, f"{label}: the published column is empty: give the published figure, "
                                            "or write \"not checked\" in it (and say why in the note)"))
            continue
        bound = _BOUND.match(pub)               # "over 45", ">= 45", "at most 2": a published bound, not a figure
        if bound and bench_number(r.get("computed")) and bench_number(bound.group(2)):
            c0, b0 = bench_number(r.get("computed"))[0], bench_number(bound.group(2))[0]
            if c0 < 0 and b0 < 0:                 # a fall "over 45 percent" written -45: compare sizes
                c0, b0 = -c0, -b0
            up = bound.group(1).lower() in (">", ">=", "≥", "over", "more than", "above", "at least")
            if (c0 >= b0) if up else (c0 <= b0):
                compared += 1
            else:
                out.append(("benchmark", False, f"{label}: computed {r.get('computed')} is outside the published "
                                                f"bound {pub}: find the cause or explain it in the note"))
            continue
        c, pb = bench_number(r.get("computed")), bench_number(pub)
        note = str(r.get("note") or r.get("notes") or "").strip()
        says = next((str(r[k]).strip().lower() for k in _SAYS if k in r and r[k]), "")
        if c and values and not _is_fact(c[0], max(c[1], abs(c[0]) * 0.005), values):
            uncited += 1
        if c is None or pb is None:
            flat = lambda x: re.sub(r"\s+", " ", str(x)).strip().lower()      # noqa: E731
            same = flat(r.get("computed", "")) == flat(pub)
            if not same and not note and not says:
                out.append(("benchmark", False, f"{label}: computed and published are not single numbers and differ; "
                                                "add a match column (yes/no) and a note saying how they compare"))
            continue
        compared += 1
        agree = bench_agree(c, pb)
        rounding = rounding_gap(c, pb, pub, note)
        if not agree and says.startswith("yes") and not rounding:   # only a gap the size of a rounding is a match
            out.append(("benchmark", False, f"{label}: says it matches, but computed {r.get('computed')} differs from "
                                            f"published {pub} by more than the published rounding (±{pb[1]:g}); "
                                            "when the publisher rounds (to 4 significant digits, to $10), say so "
                                            "in the note; when the gap is sampling error, write match \"within "
                                            "MOE\" (not yes) and say so in the note; "
                                            "in the note"))
        elif not agree and not note:
            out.append(("benchmark", False, f"{label}: computed {r.get('computed')} vs published {pub} differ by more "
                                            f"than rounding (±{pb[1]:g}); find the cause and fix it, or add a note "
                                            "column explaining the difference (definition, vintage, universe) and say "
                                            "so in the answer"))
    out += _check_headlines(facts, rows)
    if uncited and not any(not ok for _, ok, _ in out):
        print(f"note: benchmark.csv has {uncited} computed value(s) not in facts.json: fine for cross-checks; the "
              "headline numbers the answer cites should be registered facts")
    if not any(not ok for _, ok, _ in out):
        out.append(("benchmark", True, f"{len(rows)} benchmark rows: {compared} compared within rounding or explained, "
                                       f"{unchecked} not checked" + (f", {uncited} computed values not in facts.json "
                                                                     "(cross-checks)" if uncited else "")))
    return out


def check_fact(name: str, f: dict, base: str) -> tuple[bool, str]:
    """A fact read from a CSV cell must match that cell; one without a source needs a derivation."""
    if f.get("source"):
        sp = os.path.join(base, f["source"])
        if not os.path.exists(sp):
            return False, f"{name}: source {f['source']} does not exist"
        with open(sp, encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if all(str(r.get(k)) == str(v) for k, v in f.get("where", {}).items())]
        if len(rows) != 1:
            return False, f"{name}: {len(rows)} rows in {f['source']} match {f.get('where')}"
        try:
            cell = float(rows[0][f["column"]])
        except (KeyError, ValueError, TypeError):
            return False, f"{name}: column {f.get('column')} missing or not numeric"
        ok = abs(cell - float(f["value"])) <= 1e-9 * max(1.0, abs(cell))
        return ok, f"{name}: facts.json {f['value']} vs CSV {cell}" if not ok else f"{name}: matches {f['source']}"
    if not f.get("derivation"):
        return False, f"{name}: no source cell and no derivation"
    return True, f"{name}: derived ({f['derivation'][:60]})"


FACT_FIELDS = ("source", "where", "column", "fmt", "pack", "preliminary", "vintage", "projection")


def fact_diff(orig: dict, new: dict) -> list[str]:
    """Facts a rerun did not reproduce: names added or removed, a number that moved beyond float noise or became
    NaN, and any other value that changed."""
    out = set(orig) ^ set(new)
    for k in set(orig) & set(new):
        if any((orig[k] or {}).get(f) != (new[k] or {}).get(f) for f in FACT_FIELDS):
            out.add(k)                           # same number, but cited from another cell or printed differently
            continue
        a, b = (orig[k] or {}).get("value"), (new[k] or {}).get("value")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if not (math.isfinite(a) and math.isfinite(b)):
                out.add(k)
            elif abs(a - b) > 1e-9 * max(1, abs(a)):
                out.add(k)
        elif a != b:
            out.add(k)
    return sorted(out)


def _h(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def rerun(base: str, fpath: str, tracked: list[str], redact=lambda s: s) -> list[tuple]:
    """Run analysis.py again (through lib/run_analysis.py when the package has it); facts.json and the tracked
    CSVs must come out the same. The originals and their timestamps are restored."""
    out: list[tuple] = []
    if not os.path.exists(fpath):
        return [("replication", False, "--rerun needs facts.json; run analysis.py first")]
    orig = json.load(open(fpath, encoding="utf-8"))
    fst = os.stat(fpath)
    shutil.copy(fpath, fpath + ".audit-backup")
    tracked = [t for t in dict.fromkeys(tracked) if os.path.exists(t)]
    before = {t: (_h(t), os.stat(t).st_atime, os.stat(t).st_mtime) for t in tracked}
    for t in tracked:                            # the audit must not leave the outputs changed
        shutil.copy2(t, t + ".audit-backup")
    runner = os.path.join(base, "lib", "run_analysis.py")
    cmd = [sys.executable, "-B", runner if os.path.exists(runner) else os.path.join(base, "analysis.py")]
    try:
        r_ = subprocess.run(cmd, cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=3600, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r_.returncode != 0:
            out.append(("replication", False, "analysis.py failed on rerun: " + redact(r_.stderr[-400:])))
        else:
            new = json.load(open(fpath, encoding="utf-8"))
            diff = fact_diff(orig, new)
            tag = lambda k: k + (" (new: facts.json predates analysis.py)" if k not in orig else      # noqa: E731
                                 " (gone on rerun)" if k not in new else "")
            out.append(("replication", not diff, "rerun reproduces every fact exactly" if not diff
                        else f"rerun changed facts: {[tag(k) for k in diff[:8]]}"))
    finally:
        shutil.move(fpath + ".audit-backup", fpath)
        os.utime(fpath, (fst.st_atime, fst.st_mtime))
        for t, (h0, at, mt) in before.items():
            if not os.path.exists(t) or _h(t) != h0:
                out.append(("replication", False, f"rerun {'removed' if not os.path.exists(t) else 'changed'} "
                            f"{os.path.relpath(t, base)} (the original is restored)"))
            shutil.move(t + ".audit-backup", t)
            os.utime(t, (at, mt))
    return out

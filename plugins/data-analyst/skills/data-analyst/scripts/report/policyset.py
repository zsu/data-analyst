"""The source policies a report is bound by, and the package rules they imply (standard library only).
Shared by build_report.py and audit_report.py, so the builder and the release gate apply one rule set.

Sources used = report.json "sources" (ids, or {"source": id} / {"id": id} objects), else every data/<id>/
folder under the analysis folder. Each one's policy is SOURCES_DIR/<id>/policy.json, or its entry in the upload's sources/packs.json
(core.policy.load).

Package rule for a file (decide):
  data/<id>/...   allowed          -> in (temp files *.part / *.meta.json stay out)
                  aggregates_only  -> in unless it matches redistribution.microdata_globs (row-level data)
                  definition_only  -> only redistribution.package_include globs, never microdata
                  any file above MICRO_MAX bytes stays out with a note (download it again with fetch.py)
                  redistribution.withhold: rows of shared CSVs whose key is withheld are dropped, and raw
                  files named after a withheld key stay out
  data/<file>     (not in a source folder) follows the strictest rule of the sources used
  anything else   exhibits/, code, facts: in (exhibits are aggregates) unless an audit hook
                  forbid_in_package matches, or it is microdata of a definition_only source outside exhibits/
"""
from __future__ import annotations

import csv
import fnmatch
import io
import json
import os
import re

from core import packfiles
from core import policy as pol
from core.packs import SOURCES_DIR

MICRO_MAX = 50_000_000          # data files larger than this stay out of the ZIP (they can be downloaded again)
PROV_NAMES = ("request.json", "fetch_spec.json", "manifest.json", "extract_spec.json")
TEMP_SUFFIXES = (".part", ".meta.json", ".tmp")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


class SourceError(ValueError):
    pass


def sources_dir() -> str:
    return SOURCES_DIR


def _ids(x) -> list[str]:
    out = []
    for s in x or []:
        sid = s if isinstance(s, str) else (s.get("source") or s.get("id")) if isinstance(s, dict) else None
        if sid:
            out.append(str(sid))
    return list(dict.fromkeys(out))


def data_folders(base: str) -> list[str]:
    d = os.path.join(base, "data")
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))


def used_sources(base: str, R: dict) -> list[str]:
    return _ids(R.get("sources")) or data_folders(base)


def load_policies(ids: list[str]) -> dict[str, dict]:
    out, bad = {}, []
    for sid in ids:
        d = os.path.join(sources_dir(), sid)
        if not _ID.fullmatch(sid) or not packfiles.is_pack(d):
            bad.append(sid)
            continue
        out[sid] = pol.load(d)
    if bad:
        raise SourceError(f"not a source pack: {bad} (folders under data/ must be source ids; or list the sources "
                          "in report.json \"sources\")")
    return out


def pack_lib(sid: str) -> str:
    return os.path.join(sources_dir(), sid, "lib")


def match(globs, rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(os.path.basename(rel), g) for g in globs or [])


# ---------------------------------------------------------------------- provenance
def provenance(base: str, sid: str, R: dict | None = None) -> tuple[str | None, dict]:
    """(path relative to base, content) of data/<id>/'s provenance file: report.json "provenance" (a path,
    or {id: path}), else the first of request.json, fetch_spec.json, manifest.json, extract_spec.json."""
    want = (R or {}).get("provenance")
    cands = []
    if isinstance(want, dict) and want.get(sid):
        cands.append(want[sid])
    elif isinstance(want, str) and want.replace("\\", "/").startswith(f"data/{sid}/"):
        cands.append(want)
    cands += [f"data/{sid}/{n}" for n in PROV_NAMES]
    for rel in cands:
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return rel.replace("\\", "/"), json.load(f)
            except (OSError, ValueError):
                return rel.replace("\\", "/"), {}
    return None, {}


VINTAGE_KEYS = ("vintage", "latest", "latest_period", "api_version", "as_of", "vintage_dates")


def vintage_fields(P: dict) -> dict:
    """The vintage information a provenance file carries (BLS, FRED style), plus series-level vintages."""
    got = {k: P[k] for k in VINTAGE_KEYS if P.get(k) not in (None, "", [], {})}
    if got.get("vintage") == "latest" and len(got) == 1:
        got = {}
    vint = sorted({(x.get("vintage") or {}).get("kind", "") if isinstance(x.get("vintage"), dict) else str(x.get("vintage"))
                   for x in ((P.get("spec") or {}).get("series") or []) if isinstance(x, dict) and x.get("vintage")} - {""})
    if vint and vint != ["latest"]:
        got["series_vintages"] = vint
    return got


# ---------------------------------------------------------------------- withholding (FRED copyright)
def clearances(R: dict) -> dict:
    """{(source or "", KEY): entry} from report.json "clearance" ({source, key, basis, redistribute}) and the
    FRED-style "copyright_clearance" ({series_id, basis, redistribute})."""
    out = {}
    for c in R.get("clearance") or []:
        out[(str(c.get("source") or ""), str(c.get("key", "")).upper())] = c
    for c in R.get("copyright_clearance") or []:
        out[("", str(c.get("series_id", "")).upper())] = c
    return out


def withhold_rows(base: str, sid: str, P: dict) -> tuple[dict | None, list[dict]]:
    """(the withhold rule, rows of its meta table whose column holds a withheld value)."""
    w = (P.get("redistribution") or {}).get("withhold")
    if not w:
        return None, []
    meta = os.path.join(base, "data", sid, w.get("meta", "series.csv"))
    if not os.path.exists(meta):
        return w, []
    with open(meta, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    col = w.get("column", "")
    if w.get("shareable"):                       # whitelist: anything not listed is withheld (FRED)
        ok = {str(v) for v in w["shareable"]}
        return w, [r for r in rows if str(r.get(col, "")) not in ok]
    vals = {str(v) for v in w.get("values") or []}
    return w, [r for r in rows if str(r.get(col, "")) in vals]


def withheld_keys(base: str, sid: str, P: dict, R: dict) -> set[str]:
    """Keys whose rows may not leave the analysis folder (a clearance with "redistribute": true lifts one)."""
    w, rows = withhold_rows(base, sid, P)
    if not w:
        return set()
    clear = clearances(R)
    keys = set()
    for r in rows:
        k = str(r.get(w.get("key", ""), ""))
        c = clear.get((sid, k.upper())) or clear.get(("", k.upper()))
        if k and not (c and c.get("redistribute") is True):
            keys.add(k)
    return keys


def filtered_csv(path: str, keycol: str, withheld: set[str]) -> tuple[str, int]:
    """(CSV text without the withheld rows, rows dropped)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=rd.fieldnames or [], lineterminator="\n")
        w.writeheader()
        dropped = 0
        for r in rd:
            if str(r.get(keycol, "")) in withheld:
                dropped += 1
                continue
            w.writerow(r)
    return buf.getvalue(), dropped


def csv_header(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return next(csv.reader(f), [])
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


# ---------------------------------------------------------------------- the package rule for one file
def decide(rel: str, base: str, policies: dict[str, dict], R: dict, withheld: dict[str, set] | None = None) -> dict:
    """{"ok": bool, "reason": str, "filter": (keycol, keys) | None, "policy": bool} for a file (path relative to
    base). "policy" is True when the file stays out because of a source's terms (not because of its size)."""
    rel = rel.replace("\\", "/")
    full = os.path.join(base, rel)
    withheld = withheld if withheld is not None else {}
    repl = R.get("replication") or {}
    res = {"ok": True, "reason": "", "filter": None, "policy": False}

    def no(reason, policy=True):
        return {"ok": False, "reason": reason, "filter": None, "policy": policy}

    for sid, P in policies.items():                                   # audit hooks: forbid_in_package
        for h in P.get("audit_hooks") or []:
            if h.get("kind") == "forbid_in_package" and any(fnmatch.fnmatch(rel, g) for g in h.get("globs", [])):
                return no(f"{sid} forbids it in a package ({h.get('id')}: {h.get('why', '')})")
    m = re.match(r"data/([^/]+)/(.+)$", rel)
    if m and m.group(1) in policies:
        sid, sub = m.group(1), m.group(2)
        P = policies[sid]
        rd = P["redistribution"]
        mode = rd["mode"]
        if sub.endswith(TEMP_SUFFIXES):
            return no("temporary download file", policy=False)
        if mode == "definition_only":
            if pol.is_microdata(P, sub) or pol.is_microdata(P, rel):
                return no(f"microdata of {sid}: its terms forbid redistribution (definition_only)")
            if not match(rd.get("package_include") or [], sub):
                return no(f"{sid} is definition_only: only its request definition leaves the analysis folder")
        elif mode == "aggregates_only" and match(rd.get("microdata_globs") or [], sub):
            return no(f"row-level data of {sid}: only tables of estimates may be shared (aggregates_only)")
        is_data = sub.lower().endswith((".csv", ".csv.gz", ".parquet", ".dat", ".dat.gz", ".xlsx", ".zip"))
        if repl.get("include_data") is False and is_data and os.path.basename(sub) != "data_dictionary.csv":
            return no("left out by report.json replication.include_data = false", policy=False)
        keys = withheld.get(sid) or set()
        w = rd.get("withhold") or {}
        if keys and sub.startswith("raw/"):
            name = os.path.basename(sub)
            if any(name.startswith(k + "_") or name.startswith(k + ".") for k in keys):
                return no(f"raw response of a withheld {w.get('key', 'key')} ({sid}: redistribution.withhold)")
        if os.path.isfile(full) and os.path.getsize(full) > MICRO_MAX:
            return no(f"larger than {MICRO_MAX // 1_000_000} MB; download it again with fetch.py", policy=False)
        if keys and sub.lower().endswith(".csv") and sub != w.get("meta") and w.get("key") in csv_header(full):
            res["filter"] = (w["key"], keys)
        return res
    if m:                                                            # a data folder of a source not used
        return no(f"data/{m.group(1)}/ is not a source of this report", policy=False)
    if rel.startswith("data/"):                                       # a loose file: strictest rule wins
        strict = pol.strictest(list(policies.values()))
        if strict != "allowed":
            return no(f"not in a source folder, so the strictest rule of the sources used ({strict}) applies")
        if os.path.isfile(full) and os.path.getsize(full) > MICRO_MAX:
            return no(f"larger than {MICRO_MAX // 1_000_000} MB", policy=False)
        return res
    if not rel.startswith(("exhibits/", "lib/")):
        for sid, P in policies.items():
            if P["redistribution"]["mode"] == "definition_only" and pol.is_microdata(P, rel):
                return no(f"looks like microdata of {sid} (its microdata_globs); its terms forbid redistribution")
    return res

"""Write a source's data package: <out>/data/<source>/ (standard library; pandas optional).

    package.write(out, pack, tables, spec, route, acquired="api")

Layout (the same for every standard pack; deep packs keep their own file names but the same folder):
  <name>.csv          one file per request, values exactly as published (missing values left empty)
  data_dictionary.csv table, column, inferred type, non-missing count, example
  raw/                the responses as received (credential-free), when an adapter passes them (raw=)
  request.json        spec, fingerprint, route, retrieval time, every request (from core.http.SOURCES);
                      "earlier_fetches" keeps the record of an earlier spec fetched into the same folder
                      whose tables are still there (a second, dependent fetch: laws first, then each law)
  README.md           what is here, the citation, notices, terms, redistribution and retention rules

Reuse: request.json carries a fingerprint of the spec; existing() tells routes.py whether the folder
already holds exactly this request (route reuse_existing).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from core import policy as pol

LABEL_ONLY = ("description", "label", "labels", "notes", "source_note")


SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def copy_pack_lib(out: str, source: str) -> str | None:
    """Copy sources/<source>/lib/ into <out>/lib/<source>/ and the shared analysis helpers into <out>/lib/, so
    analysis.py (which puts <out>/lib first on sys.path, SKILL.md section 4) reruns without the skill folder."""
    import shutil
    src = os.path.join(SKILL_DIR, "sources", source, "lib")
    if not os.path.isdir(src):
        return None
    lib = os.path.join(os.path.abspath(out), "lib", source)
    os.makedirs(lib, exist_ok=True)
    for f in os.listdir(src):
        if os.path.isfile(os.path.join(src, f)) and not f.endswith(".pyc"):
            shutil.copy2(os.path.join(src, f), os.path.join(lib, f))
    shared = os.path.join(SKILL_DIR, "scripts", "analysis")
    for f in os.listdir(shared):
        if f.endswith(".py"):
            shutil.copy2(os.path.join(shared, f), os.path.join(os.path.dirname(lib), f))
    return lib


def data_dir(out: str, source: str) -> str:
    return os.path.join(os.path.abspath(out), "data", source)


def fingerprint(spec: dict) -> str:
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in sorted(x.items()) if k not in LABEL_ONLY}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return hashlib.sha256(json.dumps(strip(spec), sort_keys=True, default=str).encode()).hexdigest()[:16]


def existing(out: str, source: str, spec: dict) -> dict | None:
    rq = os.path.join(data_dir(out, source), "request.json")
    if not os.path.exists(rq):
        return None
    try:
        with open(rq, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    files = [os.path.join(data_dir(out, source), t["file"]) for t in saved.get("tables", [])]
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(saved.get("retrieved", ""))).days
    except ValueError:
        age = None
    return {"path": data_dir(out, source), "matches": saved.get("fingerprint") == fingerprint(spec),
            "complete": bool(files) and all(os.path.exists(f) for f in files), "retrieved": saved.get("retrieved", ""),
            "age_days": age, "route": saved.get("route"), "problems": saved.get("problems") or []}


def _infer(vals: list[str]) -> str:
    vals = [v for v in vals if v not in ("", None)]
    if not vals:
        return "empty"
    def isnum(v):
        try:
            float(str(v).replace(",", ""))
            return True
        except ValueError:
            return False
    if all(isnum(v) for v in vals):
        return "number"
    if all(re.match(r"(?:1[6-9]|20|21)\d\d-(?:0[1-9]|1[0-2])(?:-\d\d)?(?:[T ]|$)", str(v)) for v in vals):  # not ISSNs
        return "date"
    return "text"


def _rows(table) -> tuple[list[dict], list[str]]:
    if hasattr(table, "to_dict") and hasattr(table, "columns"):          # a pandas DataFrame
        cols = [str(c) for c in table.columns]
        return [{str(k): ("" if v is None or v != v else v) for k, v in r.items()}
                for r in table.to_dict("records")], cols
    rows = list(table)
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return rows, cols


def write_csv(path: str, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def write(out: str, pack, tables: dict, spec: dict, route: str, acquired: str = "api",
          requests: list[dict] | None = None, raw: dict[str, bytes] | None = None, notes: list[str] | None = None,
          problems: list[str] | None = None, vintage: str = "", extra_readme: str = "") -> dict:
    P = pack.policy
    d = data_dir(out, pack.id)
    os.makedirs(d, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta_tables, dictionary = [], []
    endpoint = {r.get("name"): r.get("endpoint") for r in (spec or {}).get("requests") or [] if isinstance(r, dict)}
    shape = {}
    for name, table in tables.items():
        rows, cols = _rows(table)
        if cols:
            shape.setdefault(endpoint.get(name), cols)
    for name, table in tables.items():
        rows, cols = _rows(table)
        if not cols:            # an allowed empty result: same header as its endpoint's other tables, so it reads
            cols = shape.get(endpoint.get(name)) or ["no_rows"]
        fname = f"{name}.csv"
        write_csv(os.path.join(d, fname), rows, cols)
        for c in cols:
            vals = [r.get(c) for r in rows]
            nonmiss = [v for v in vals if v not in ("", None)]
            dictionary.append({"table": name, "column": c, "type": _infer(nonmiss[:500]),
                               "non_missing": len(nonmiss), "example": str(nonmiss[0])[:80] if nonmiss else ""})
        meta_tables.append({"name": name, "file": fname, "rows": len(rows), "columns": cols,
                            "sha256": sha256(os.path.join(d, fname))})
    earlier = earlier_fetches(d, {t["file"] for t in meta_tables})
    kept = {t["name"] for e in earlier for t in e["tables"]}
    if kept and os.path.exists(os.path.join(d, "data_dictionary.csv")):     # the earlier tables keep their entries
        with open(os.path.join(d, "data_dictionary.csv"), encoding="utf-8", newline="") as f:
            dictionary += [r for r in csv.DictReader(f) if r.get("table") in kept]
    if dictionary:
        write_csv(os.path.join(d, "data_dictionary.csv"), dictionary,
                  ["table", "column", "type", "non_missing", "example"])
    if raw:
        os.makedirs(os.path.join(d, "raw"), exist_ok=True)
        for rel, data in raw.items():
            with open(os.path.join(d, "raw", rel), "wb") as f:
                f.write(data)
    req = {"source": pack.id, "spec": spec, "fingerprint": fingerprint(spec), "route": route, "acquired": acquired,
           "retrieved": retrieved, "vintage": vintage, "requests": requests or [], "tables": meta_tables,
           "problems": problems or [], "notes": notes or [], "earlier_fetches": earlier,
           "policy": {"redistribution": P["redistribution"]["mode"], "cache": P["cache"]["mode"]}}
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2, default=str)
    # the citation names the user's own calendar day (an evening in the U.S. is already tomorrow in UTC);
    # request.json and the README keep the exact UTC timestamp
    local_day = datetime.now().astimezone().date().isoformat()
    cite = pol.citation(P, retrieved=local_day, vintage=vintage, dataset=spec.get("description", ""))
    if earlier:
        listed = [f"- {pol.local_stamp(e['retrieved'])} ({e['route']}): " + ", ".join(f"`{t['file']}`" for t in e["tables"])
                  + "; spec and requests in request.json `earlier_fetches`"
                  + "".join(f"\n  - **Problem**: {p}" for p in e.get("problems") or []) for e in earlier]
        extra_readme = "\n\n".join(x for x in (extra_readme, "## Earlier fetches into this folder\n\n"
                                               + "\n".join(listed)) if x)
    write_readme(d, pack, meta_tables, cite, retrieved, acquired, notes or [], problems or [], extra_readme)
    return {"data_dir": d, "tables": meta_tables, "retrieved": retrieved, "citation": cite}


def earlier_fetches(d: str, new_files: set[str]) -> list[dict]:
    """The provenance of earlier fetches whose tables are still in the folder and not replaced now: without it a
    second spec fetched into the same folder would leave those tables undocumented."""
    try:
        with open(os.path.join(d, "request.json"), encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in (old.get("earlier_fetches") or []) + [old]:
        keep = [t for t in e.get("tables") or [] if t.get("file") not in new_files
                and os.path.exists(os.path.join(d, t.get("file", "")))]
        if keep:
            # a kept table keeps its caveats: a table cut at max_rows stays partial after a later fetch
            mine = tuple(f"{t.get('name')}:" for t in keep if t.get("name"))
            out.append({k: e.get(k) for k in ("spec", "fingerprint", "route", "acquired", "retrieved", "vintage",
                                              "requests")} | {"tables": keep}
                       | {k: [str(n) for n in e.get(k) or [] if str(n).startswith(mine)] for k in ("problems", "notes")})
            new_files = new_files | {t["file"] for t in keep}
    return out


def write_readme(d, pack, tables, cite, retrieved, acquired, notes, problems, extra):
    P = pack.policy
    rd = P["redistribution"]
    lines = [f"# {P['title']}: downloaded data", "",
             f"Retrieved {pol.local_stamp(retrieved)} by the `{acquired}` route. Values are exactly as published; missing values are "
             "empty cells. Column types and examples are in `data_dictionary.csv`; every request is in "
             "`request.json` (URLs without keys).", "", "## Files", ""]
    lines += [f"- `{t['file']}`: {t['rows']:,} rows; columns {', '.join(t['columns'][:12])}"
              + (" …" if len(t["columns"]) > 12 else "") for t in tables]
    lines += ["", "## Citation", "", cite, ""]
    if P.get("notices"):
        lines += ["## Required notices", ""] + [f"- {n}" for n in P["notices"]] + [""]
    lines += ["## Use and redistribution", "", f"Rule: **{rd['mode']}**. {rd.get('why', '')}"]
    if rd["mode"] == "definition_only":
        lines += ["", "These files must not be shared, attached to a report or put in a replication package. Share "
                  "the request definition and the reproduce scripts instead; anyone rerunning the work downloads "
                  "the data under their own account."]
    if P.get("retention"):
        lines += ["", f"Retention: {P['retention'].get('note', '')}"]
    if P.get("terms_url"):
        lines += ["", f"Terms: {P['terms_url']}"]
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    if problems:
        lines += ["", "## Problems", ""] + [f"- {p}" for p in problems]
    if extra:
        lines += ["", extra]
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

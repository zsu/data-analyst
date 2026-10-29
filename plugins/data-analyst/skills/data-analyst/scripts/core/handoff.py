"""The data hand-off: what each source delivered, read back from its provenance files (standard library).

    python fetch.py --handoff --out <out>

For every <out>/data/<id>/ (request.json; Treasury's fetch_spec.json) and <out>/published/*.csv it lists the
files with row counts, the route and retrieval date, the fetch notes and problems, and a short preview of each
table, and writes the same as ready-to-send Markdown ("text"). A data-only reply is this text plus the caveats;
quick answers and reports take each source's route and retrieval date from it. Everything comes from the files
written at fetch time, so the hand-off cannot drift from what was downloaded.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re

from core import policy as pol

PREVIEW_ROWS = 4
PREVIEW_COLS = 8
CELL = 40
MAX_TABLES = 6
PROVENANCE = ("request.json", "fetch_spec.json")
MAX_NOTES = 6            # other notes shown per source; caveat notes are always shown
CAVEAT = re.compile(r"(?i)missing|may be|upper bound|withheld|suppress|fewer|stopped at|partial|incomplete|not in|"
                    r"no rows|twice|restored|skipped|without --check|not fetched|not reported|dropped|lag")


def _cell(v) -> str:
    s = " ".join(str(v if v is not None else "").split()).replace("|", "/")
    return s if len(s) <= CELL else s[:CELL - 3] + "..."


def preview(path: str, n: int = PREVIEW_ROWS, sample: int = 200) -> dict:
    """The first n rows of a CSV, showing the columns that vary (at most PREVIEW_COLS; a column with one value in
    the first `sample` rows is listed once under "same", so values are not crowded out by labels)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        head = next(r, [])
        rows, total = [], 0
        for row in r:
            total += 1
            if len(rows) < sample:
                rows.append(row + [""] * (len(head) - len(row)))
    vary = [i for i in range(len(head)) if len({row[i] for row in rows}) > 1]
    show = (vary or list(range(len(head))))[:PREVIEW_COLS]
    same = [(head[i], rows[0][i]) for i in range(len(head)) if i not in vary and rows and rows[0][i] != ""]
    return {"header": [head[i] for i in show], "rows": [[row[i] for i in show] for row in rows[:n]],
            "same": same[:6], "hidden": len(head) - len(show) - len(same[:6]), "total": total, "columns": len(head)}


def _table_md(pv: dict) -> list[str]:
    if not pv["header"]:
        return ["(empty file)"]
    out = ["| " + " | ".join(_cell(h) for h in pv["header"]) + " |", "|" + "---|" * len(pv["header"])]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in pv["rows"]]
    if pv["same"]:
        out.append(f"Same in the first {min(pv['total'], 200):,} rows: " +
                   "; ".join(f"{k} = {_cell(v)}" for k, v in pv["same"]))
    if pv["hidden"] > 0:
        out.append(f"({pv['hidden']} more columns: see the data dictionary)")
    return out


def _record(d: str) -> tuple[dict, str]:
    """(the source's provenance record, the file it came from): the hand-off points readers at that file."""
    for name in PROVENANCE:
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f), name
            except (OSError, ValueError):
                return {}, name
    return {}, ""


def _redistribution(source: str) -> str:
    from core import packfiles
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sources", source)
    try:
        return (packfiles.read_json(d, "policy.json") or {}).get("redistribution", {}).get("mode") or "allowed"
    except (OSError, ValueError, AttributeError):      # a folder that is no pack: treat as shareable data
        return "allowed"


def source_summary(out: str, d: str) -> dict:
    sid = os.path.basename(d)
    rec, rec_file = _record(d)
    tables = [t for t in rec.get("tables") or [] if isinstance(t, dict) and t.get("file")]
    if not tables:                                  # no list in the record: every CSV in the folder
        tables = [{"file": os.path.basename(p)} for p in sorted(glob.glob(os.path.join(d, "*.csv")))
                  if os.path.basename(p) != "data_dictionary.csv"]
    micro = _redistribution(sid) in ("definition_only", "aggregates_only")
    files = []
    for t in tables:
        p = os.path.join(d, t["file"])
        if not os.path.exists(p) or not p.lower().endswith(".csv"):
            files.append({"file": os.path.relpath(p, out).replace(os.sep, "/"), "rows": t.get("rows")})
            continue
        pv = preview(p)
        files.append({"file": os.path.relpath(p, out).replace(os.sep, "/"), "rows": pv["total"],
                      "columns": pv["columns"], "preview": None if micro else pv})
    retrieved = rec.get("retrieved") or rec.get("downloaded") or ""
    notes = [str(n) for n in (rec.get("notes") or []) if n]
    problems = [str(p) for p in (rec.get("problems") or []) if p]
    problems += [f"(earlier fetch, table still in the folder) {p}" for e in rec.get("earlier_fetches") or []
                 if isinstance(e, dict) for p in e.get("problems") or [] if p]
    return {"source": sid, "route": rec.get("route") or rec.get("acquired") or "unknown",
            "retrieved": pol.local_day(retrieved) if retrieved else "unknown",
            "files": files, "notes": notes, "problems": problems,
            "readme": os.path.relpath(os.path.join(d, "README.md"), out).replace(os.sep, "/")
            if os.path.exists(os.path.join(d, "README.md")) else None,
            "microdata": micro, "provenance": bool(rec), "provenance_file": rec_file}


def published_summary(out: str) -> dict | None:
    paths = sorted(glob.glob(os.path.join(out, "published", "*.csv")))
    if not paths:
        return None
    files = []
    for p in paths:
        pv = preview(p)
        urls, dates = set(), set()
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                urls.add(r.get("source_url") or "")
                dates.add(r.get("accessed") or "")
        files.append({"file": os.path.relpath(p, out).replace(os.sep, "/"), "rows": pv["total"],
                      "source_url": sorted(u for u in urls if u), "accessed": sorted(x for x in dates if x),
                      "preview": pv})
    return {"source": "published", "route": "read from the agency's page", "files": files}


def _notes_md(s: dict) -> list[str]:
    """Every caveat note (rows missing, upper bounds, withheld or collapsed rows, partial tables) and the first
    MAX_NOTES others; the rest are counted with where to read them, never dropped silently."""
    shown, rest, others = [], [], 0
    for n in s["notes"]:
        caveat = bool(CAVEAT.search(n))
        (shown if caveat or others < MAX_NOTES else rest).append(n)
        others += not caveat
    md = [f"- Note: {n}" for n in shown]
    if rest:
        md.append(f"- (+{len(rest)} more notes in data/{s['source']}/{s.get('provenance_file') or 'request.json'})")
    return md


def summarize(out: str) -> dict:
    out = os.path.abspath(out)
    dirs = sorted(d for d in glob.glob(os.path.join(out, "data", "*")) if os.path.isdir(d))
    sources = [source_summary(out, d) for d in dirs]
    pub = published_summary(out)
    md = []
    for s in sources:
        md += [f"### {s['source']}", f"- Route: {s['route']}; retrieved {s['retrieved']}"
               + ("" if s["provenance"] else " (no request.json: not packaged by fetch.py; re-fetch or --ingest it)")]
        for f in s["files"]:
            md.append(f"- `{f['file']}`: {f['rows']:,} rows" if isinstance(f.get("rows"), int) else f"- `{f['file']}`")
        md += _notes_md(s) + [f"- **Problem**: {p}" for p in s["problems"]]
        pv = [f for f in s["files"] if f.get("preview")]
        for f in pv[:MAX_TABLES]:
            md += ["", f"`{f['file']}` (first {len(f['preview']['rows'])} rows):", ""]
            md += _table_md(f["preview"])
        if s["microdata"]:
            md.append("- Microdata: no preview (the terms keep it in the analysis folder); see the data dictionary")
        if len(pv) > MAX_TABLES:
            md.append(f"- ({len(pv) - MAX_TABLES} more tables: listed above, no preview here)")
        md.append("")
    if pub:
        md += ["### Figures read from agency pages (`published/`)"]
        for f in pub["files"]:
            md.append(f"- `{f['file']}`: {f['rows']:,} rows; source {', '.join(f['source_url']) or 'MISSING source_url'}; "
                      f"accessed {', '.join(f['accessed']) or 'MISSING accessed'}")
        md.append("")
    missing = [s["source"] for s in sources if not s["provenance"]]
    return {"status": "handoff", "out": out, "sources": sources + ([pub] if pub else []),
            "unpackaged": missing, "text": "\n".join(md).strip(),
            "action": "Send `text` as the per-source part of the hand-off (it is read from the files fetched), then "
                      "add each source's caveats from its README and any data that could not be fetched, with "
                      "the route to get it." + (f" Folders without provenance: {', '.join(missing)}." if missing else "")}

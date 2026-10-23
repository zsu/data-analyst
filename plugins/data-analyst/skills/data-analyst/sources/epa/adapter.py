"""EPA adapter: Envirofacts DMAP (data.epa.gov/dmapservice) and ECHO (echodata.epa.gov), two hosts.

- Envirofacts tables are queried by path: /<schema.table>/<column>/<operator>/<value>/.../<first>:<last>/JSON.
  The pack builds the path from "filters" and pages with row ranges of 5,000.
- ECHO searches take two steps: <program>_rest_services.get_facilities (or get_systems, get_case_info)
  returns a QueryID and counts; <program>_rest_services.get_qid returns the rows, page by page
  (pageno; the page size is the responseset of the first call). The first call's summary (counts,
  total penalties) goes into the fetch notes. When ECHO echoes the parameters it understood
  (QueryParameters), a spec parameter it did not understand stops the fetch instead of silently
  returning nationwide rows (p_st on case searches does exactly that). Facility searches do not echo them,
  so check_spec refuses the parameters known to be ignored (IGNORED).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

DMAP = "https://data.epa.gov/dmapservice"
PAGE = 5000
ECHO_PAGE = 1000
OPS = ("equals", "notEquals", "lessThan", "lessThanEqual", "greaterThan", "greaterThanEqual", "beginsWith",
       "endsWith", "contains")
ROW_KEYS = ("Facilities", "WaterSystems", "Cases")
# parameters ECHO accepts without filtering (verified 2026-10): the search would silently return every row
IGNORED = {
    "echo_cases": {
        "p_st": "is ignored by case searches (every state comes back): use p_state",
        "p_fy": "is ignored by ECHO: use p_milestone_fy (one fiscal year)",
        "p_crim": "is ignored by ECHO: use p_civil_criminal_indicator (CI civil, CR criminal, ALL)",
        "p_name": "is accepted by ECHO but does not filter: fetch by state, law or year and filter CaseName",
    },
    "echo_facilities": {
        "p_naics": "is ignored by ECHO facility searches (every facility comes back): use p_ncs (NAICS, 2-6 "
                   "digits) or p_sic",
    },
}


def check_error(status, body: bytes):
    if body.lstrip()[:1] != b"{":
        return None
    try:
        d = json.loads(body.decode("utf-8-sig"))
    except ValueError:
        return None
    if isinstance(d, dict):
        err = (d.get("Results") or {}).get("Error") if isinstance(d.get("Results"), dict) else None
        if err:
            return "ECHO: " + str(err.get("ErrorMessage") if isinstance(err, dict) else err)[:400]
        if d.get("error"):
            return "Envirofacts: " + str(d["error"])[:400]
    return None


def _filters(f) -> list[tuple[str, str, str]]:
    if not f:
        return []
    if isinstance(f, dict):
        return [(k, "equals", v) for k, v in f.items()]
    out = []
    for item in f:
        if len(item) == 2:
            out.append((item[0], "equals", item[1]))
        else:
            out.append((item[0], item[1], item[2]))
    return out


def dmap_path(table: str, filters) -> str:
    segs = [table]
    for col, op, val in _filters(filters):
        segs += [urllib.parse.quote(str(col), safe=""), op, urllib.parse.quote(str(val), safe="")]
    return "/" + "/".join(segs)


def check_spec(spec):
    probs = []
    eps = {}
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        if r.get("endpoint") == "envirofacts":
            f = p.get("filters")
            if f is not None and not isinstance(f, (dict, list)):
                probs.append(f"request {r.get('name')!r}: filters must be an object or a list of [column, operator, value]")
                continue
            for item in ([] if isinstance(f, dict) or not f else f):
                if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                    probs.append(f"request {r.get('name')!r}: each filter is [column, value] or [column, operator, value]")
                elif len(item) == 3 and item[1] not in OPS:
                    probs.append(f"request {r.get('name')!r}: operator {item[1]!r} is not one of {', '.join(OPS)}")
        for k, why in IGNORED.get(r.get("endpoint"), {}).items():
            if k in p:
                probs.append(f"request {r.get('name')!r}: {k} {why}")
        if r.get("endpoint") == "echo_facilities" and p.get("program") == "air" and "p_co" in p:
            probs.append(f"request {r.get('name')!r}: p_co returns no rows on air searches (verified 2026-10); "
                         "use p_fips (5-digit state+county FIPS) instead")
        eps[r.get("name")] = r
    return probs


def _dmap(c, ep, params, max_rows):
    limit = int(max_rows or ep.get("max_rows") or 200000)
    base = dmap_path(params["table"], params.get("filters"))
    rows, first = [], 1
    while len(rows) < limit:
        n = min(PAGE, limit - len(rows))
        got = c.get_json(f"{DMAP}{base}/{first}:{first + n - 1}/JSON")
        got = got if isinstance(got, list) else []
        rows += got
        if len(got) < n:
            return gr._clean(rows, ep), False
        first += n
    return gr._clean(rows[:limit], ep), True


def _echo_paths(ep, params):
    path, _, _ = gr.split(ep, {k: v for k, v in params.items() if k == "program"})
    if params.get("program") == "sdw":
        path = path.replace(".get_facilities", ".get_systems")
    qid = path.rsplit(".", 1)[0] + ".get_qid"
    return path, qid


def _echo(c, ep, params, max_rows, name, notes):
    limit = int(max_rows or ep.get("max_rows") or 100000)
    q = {k: v for k, v in params.items() if k not in ("program", "output", "responseset")}
    first, qid_path = _echo_paths(ep, params)
    res = c.get_json(first, {"output": "JSON", **q, "responseset": ECHO_PAGE}).get("Results") or {}
    understood = {p.get("QP") for p in res.get("QueryParameters") or [] if isinstance(p, dict)}
    if understood:
        ignored = [k for k in q if k not in understood]
        if ignored:
            raise DataError(f"ECHO did not understand {', '.join(ignored)} (it understood: {', '.join(sorted(understood))}); "
                            "see source.md for the parameter names", kind="bad_request")
    total = int(str(res.get("QueryRows") or "0").replace(",", "") or 0)
    summary = {k: v for k, v in res.items() if k.endswith("Rows") or k == "TotalPenalties"}
    notes.append(f"{name}: ECHO matched {total:,} rows; summary {json.dumps(summary)}")
    if total == 0 or not res.get("QueryID"):
        return [], False
    rows, page = [], 1
    want = min(total, limit)
    while len(rows) < want:
        data = c.get_json(qid_path, {"output": "JSON", "qid": res["QueryID"], "pageno": page}).get("Results") or {}
        got = next((data[k] for k in ROW_KEYS if isinstance(data.get(k), list)), [])
        if not got:
            break
        rows += got
        page += 1
    return gr._clean(rows[:want], ep), total > want


def _num(v):
    t = str(v or "").strip().replace("$", "").replace(",", "")
    try:
        float(t)
        return t
    except ValueError:
        return ""


def _mdy(v):
    parts = str(v or "").strip().split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
        return f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"
    return ""


def normalize(req, ep, rows):
    """ECHO publishes money as "$40,000.00" and dates as MM/DD/YYYY: add *_usd and *_iso columns."""
    if ep.get("kind") != "echo":
        return rows
    for r in rows:
        for k in list(r):
            v = r[k]
            if isinstance(v, str) and v.startswith("$"):
                r[k + "_usd"] = _num(v)
            elif isinstance(v, str) and (k.startswith("Date") or k.endswith("Date")) and _mdy(v):
                r[k + "_iso"] = _mdy(v)
    return rows


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        kind = ep.get("kind")
        if kind == "dmap":
            rows, more = _dmap(c, ep, params, r.get("max_rows"))
        elif kind == "echo":
            rows, more = _echo(c, ep, params, r.get("max_rows"), r["name"], notes)
            rows = normalize(r, ep, rows)
        else:
            rows, meta = gr.fetch_request(c, ep, params, r.get("max_rows"))
            more = meta["truncated"]
        if more:
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); the source has more.")
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check table, column names and values "
                            "(see source.md)", kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        kind = ep.get("kind")
        if kind == "dmap":
            limit = int(r.get("max_rows") or PAGE)
            base = dmap_path(params["table"], params.get("filters"))
            pages = max(1, min(25, -(-limit // PAGE)))
            for i in range(pages):
                a = i * PAGE + 1
                items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"],
                              "url": f"{DMAP}{base}/{a}:{a + PAGE - 1}/CSV",
                              "filename": f"{r['name']}.csv" if pages == 1 else f"{r['name']}_{i + 1}.csv",
                              "needs_key": False, "method": "GET", "body": None,
                              "note": "" if pages == 1 else f"rows {a}-{a + PAGE - 1}; stop when a file is empty"})
        elif kind == "echo":
            first, qid = _echo_paths(ep, params)
            q = {k: v for k, v in params.items() if k not in ("program", "output", "responseset")}
            url = first + "?" + urllib.parse.urlencode({"output": "JSON", **q, "responseset": ECHO_PAGE})
            items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url, "filename": f"{r['name']}.json",
                          "needs_key": False, "method": "GET", "body": None,
                          "note": (f"Two steps: this link returns a QueryID; then open {qid}?output=JSON&qid=<QueryID>&pageno=1 "
                                   f"(pageno=2, 3 ... for more) and save those pages as {r['name']}.json, {r['name']}_2.json. "
                                   "Or use https://echo.epa.gov/facilities/facility-search (Download Data).")})
        else:
            path, query, _ = gr.split(ep, params)
            url = path if path.startswith("http") else DMAP + path
            if query:
                url += "?" + urllib.parse.urlencode(query)
            items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url, "filename": f"{r['name']}.json",
                          "needs_key": False, "method": "GET", "body": None, "note": ""})
    return items


def parse_upload(pack, req, paths):
    ep, _, _ = gr.resolve(pack, req)
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            data = f.read().decode("utf-8-sig", errors="replace")
        if p.lower().endswith(".csv"):
            rows += list(csv.DictReader(io.StringIO(data)))
            continue
        d = json.loads(data)
        if isinstance(d, list):
            rows += d
        elif isinstance(d, dict):
            res = d.get("Results") if isinstance(d.get("Results"), dict) else d
            got = next((res[k] for k in ROW_KEYS if isinstance(res.get(k), list)), None)
            rows += got if got is not None else [res]
    return normalize(req, ep, gr._clean(rows, ep))

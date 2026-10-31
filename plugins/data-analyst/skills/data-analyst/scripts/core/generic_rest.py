"""Declarative adapter for JSON/CSV REST APIs (Socrata, DKAN, CKAN, plain REST). Standard library only.

A standard pack describes its API in endpoints.json; this module turns a spec into requests, pages through
the results, flattens records into rows and builds manual-download items. A pack's adapter.py can override
or extend any step (see docs/pack_contract.md in the data-analyst repo).

endpoints.json:
{
  "base_url": "https://data.cdc.gov/resource",
  "timeout": 30,                             # seconds per attempt (slow APIs such as CMS need more)
  "retry_html_400": 3,                       # optional: retry an HTML-page HTTP 400 (a flaky front server) n times
  "endpoints": {
    "<name>": {
      "path": "/{dataset}.json",            # {x} placeholders come from params with "in": "path"
      "method": "GET",                       # or POST (params with "in": "body" go in the JSON body)
      "description": "...",
      "params": {"dataset": {"in": "path", "required": true, "desc": "...", "catalog": true},
                 # "in": "query" (default for GET) | "path" | "body" (default for POST) | "spec" (never sent)
                 # "flag": true: a query flag with no value (?flagName), sent only when the spec gives true
                 "$where": {"desc": "SoQL filter"}, "state": {"desc": "...", "enum": [...]}},
      "defaults": {"$order": ":id"},        # sent unless the spec overrides them
      "body": {...},                         # POST body template (spec params with in=body merge into it)
      "records": "results",                  # dotted path to the list of records; "" = body is the list;
                                             # "$table" = list of lists whose first row is the header (Census);
                                             # "$csv" = CSV text; "$xlsx" = an .xlsx workbook (one sheet);
                                             # "$zip:<glob>" = a zip whose members matching glob (CSV or
                                             # .xlsx) are read in name order and stacked
      "sheet": "Table 1",                    # $xlsx / zipped .xlsx: sheet name, or 1-based number (default 1)
      "header_row": 1,                       # $xlsx / zipped .xlsx: 1-based row holding the column names;
                                             # rows above it are titles, blank rows below it are dropped
      "total": "meta.count",                 # optional dotted path to the total row count
      "paging": {"type": "offset", "limit_param": "$limit", "offset_param": "$offset", "size": 1000,
                 "manual_size": 50000}       # manual_size: rows per browser download (default size)
                # or {"type": "page", "page_param": "page", "size_param": "per_page", "size": 100, "start": 1,
                #     "min_size": 5}   (optional: smallest page the API accepts)
                # or {"type": "cursor", "cursor_param": "cursor", "next": "pagination.next_cursor", "size_param": "limit", "size": 100}
                # or {"type": "link", "next": "links.next"}   or {"type": "none"}
      "max_rows": 100000,                    # hard stop; the spec may set a lower one
      "unique_key": "appl_id",               # optional: a KEY column (the source has one row per value); after
                                             # paging, rows repeated across pages are dropped and a note says
                                             # rows may be missing (offset paging over an unstable order repeats
                                             # some rows and skips as many: page by a unique sort). Never a
                                             # column whose values legitimately repeat: those rows would go.
      "truncated_note": {"when_default": "sort_field", "text": "..."},   # optional: added to the max_rows note
                                             # only when the request SENT that param (endpoint default or adapter)
                                             # and the spec did not set it: says which rows the pack's own order
                                             # keeps; a request sent without it (API order) gets no such note
      "missing": ["", "(NA)", "."],          # published codes for "no value": written as empty cells
      "flatten": true                        # nested objects become dotted columns, lists become JSON text
    }
  },
  "catalog": {"<alias>": {"id": "bi63-dtpu", "name": "...", "description": "...", "endpoint": "<name>"}}
}

spec.json for a standard pack:
{"description": "...", "requests": [{"name": "deaths", "endpoint": "query",
  "params": {"dataset": "leading_death", "$where": "year = '2017'"}, "max_rows": 5000}]}
"""
from __future__ import annotations

import csv
import fnmatch
import io
import json
import math
import os
import re
import urllib.parse
import zipfile

from core.http import Client, DataError

# A note containing this marks its table incomplete (status partial, listed under problems), like a max_rows cut:
# f"{name}{INCOMPLETE} <what is missing and why>"
INCOMPLETE = ": incomplete:"


def dig(obj, path: str):
    if path in ("", None):
        return obj
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def flatten(rec, prefix: str = "") -> dict:
    out = {}
    if not isinstance(rec, dict):
        return {prefix or "value": rec}
    for k, v in rec.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, key))
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False) if v and not all(isinstance(x, (str, int, float)) for x in v) \
                else "; ".join(str(x) for x in v)
        else:
            out[key] = v
    return out


def client_for(pack, eps: dict | None = None, **kw) -> Client:
    P = pack.policy
    eps = eps if eps is not None else (pack.endpoints or {})
    L = P["limits"]
    check = getattr(pack.adapter, "check_error", None) if pack.adapter else None
    return Client(eps.get("base_url") or P["routes"].get("api", {}).get("base", ""), name=pack.id, auth=P["auth"],
                  rps=L.get("rps") or 5, burst=L.get("burst") or 10, check_error=check,
                  errors_per_min=L.get("errors_per_min"), bytes_per_min=L.get("bytes_per_min"),
                  headers=eps.get("headers"),
                  **{"timeout": eps.get("timeout", 30), "retry_html_400": eps.get("retry_html_400", 0), **kw})


# ---------------------------------------------------------------------- spec → requests
def resolve(pack, req: dict) -> tuple[dict, dict, list[str]]:
    """(endpoint definition, merged params, problems) for one spec request."""
    eps = (pack.endpoints or {}).get("endpoints", {})
    probs = []
    name = req.get("endpoint")
    cat = (pack.endpoints or {}).get("catalog", {})
    if not name and req.get("dataset") in cat:
        name = cat[req["dataset"]].get("endpoint")
    ep = eps.get(name or "")
    if ep is None:
        return {}, {}, [f"request {req.get('name')!r}: unknown endpoint {name!r} (known: {', '.join(sorted(eps))})"]
    given = req.get("params") or {}
    defaults = dict(ep.get("defaults") or {})
    if "$group" in given and "$order" not in given:
        # SoQL: an aggregate can only be ordered by a grouped column, so a default row-id order
        # (":id", kept for stable paging) turns every grouped query into HTTP 400 (verified 2026-10)
        defaults.pop("$order", None)
    params = {**defaults, **given}
    if req.get("dataset") and not any(d.get("catalog") for d in (ep.get("params") or {}).values()):
        pass
    for pname, d in (ep.get("params") or {}).items():
        if d.get("catalog"):
            v = params.get(pname, req.get("dataset"))
            if isinstance(v, str) and v in cat:          # a list (several codes) is passed as given
                v = cat[v]["id"]
            if v is not None:
                params[pname] = v
        if d.get("required") and params.get(pname) in (None, ""):
            probs.append(f"request {req.get('name')!r}: parameter {pname!r} is required ({d.get('desc', '')})")
        if d.get("enum") and params.get(pname) not in (None, "") and str(params[pname]) not in map(str, d["enum"]):
            probs.append(f"request {req.get('name')!r}: {pname}={params[pname]!r} is not one of {d['enum']}")
    if not req.get("name") or not re.fullmatch(r"[A-Za-z0-9_\-]+", str(req.get("name"))):
        probs.append("every request needs a name of letters, digits, _ or - (it names the CSV file)")
    return ep, params, probs


def split(ep: dict, params: dict) -> tuple[str, dict, dict | None]:
    """Endpoint path with {x} filled, query params, JSON body (POST)."""
    pdefs = ep.get("params") or {}
    path = ep["path"]
    query, body = {}, (json.loads(json.dumps(ep["body"])) if ep.get("body") is not None else None)
    flags = []
    for k, v in params.items():
        where = (pdefs.get(k) or {}).get("in", "body" if ep.get("method") == "POST" else "query")
        if where == "spec":                          # read by the adapter, never sent
            continue
        if (pdefs.get(k) or {}).get("flag"):         # a bare query flag (?flagName): sent when true, never with a value
            if v is True or str(v).strip().lower() in ("true", "1", "yes"):
                flags.append(urllib.parse.quote(k))
            continue
        if where == "path" or "{" + k + "}" in path:
            raw = (pdefs.get(k) or {}).get("raw")          # "raw": true keeps slashes (Census dataset paths)
            path = path.replace("{" + k + "}", urllib.parse.quote(str(v), safe="/" if raw else ""))
        elif where == "body":
            body = body if body is not None else {}
            body[k] = v
        else:
            query[k] = v
    if flags:
        path += ("&" if "?" in path else "?") + "&".join(flags)
    return path, query, body


def check_spec(pack, spec: dict) -> list[str]:
    probs = []
    reqs = spec.get("requests") or []
    if not reqs:
        probs.append("spec.json needs a non-empty \"requests\" list")
    names = [r.get("name") for r in reqs]
    if len(set(names)) != len(names):
        probs.append("request names must be unique")
    for r in reqs:
        probs += resolve(pack, r)[2]
    if pack.has("check_spec"):
        probs += call_check_spec(pack, spec)
    return probs


def call_check_spec(pack, spec: dict) -> list[str]:
    """adapter.check_spec(spec) or check_spec(spec, pack)."""
    import inspect
    fn = pack.adapter.check_spec
    n = len(inspect.signature(fn).parameters)
    return list((fn(spec, pack) if n >= 2 else fn(spec)) or [])


# ---------------------------------------------------------------------- one request per row of a table
FOR_EACH_MAX = 2000


def expand_for_each(spec: dict, base: str) -> dict:
    """A request with "for_each": {"csv": "data/govinfo/cfr.csv", "column": "packageId", "param": "packageId"}
    becomes one request per distinct value of that column (the CSV of an earlier fetch; a relative path is read
    from the spec's folder). Several parameters at once: "map": {"billType": "type", "billNumber": "number"}
    (param: column), one request per distinct combination; "lower": true lowercases the values ("HR" -> "hr");
    "strip": "PN" drops that prefix from each value ("PN1255-17" -> "1255-17").
    Their rows come back as one table under the request's name (merge_for_each)."""
    reqs = []
    for r in spec.get("requests") or []:
        fe = r.get("for_each")
        if not fe:
            reqs.append(r)
            continue
        path = fe.get("csv", "")
        path = path if os.path.isabs(path) else os.path.join(base, path)
        if not os.path.exists(path):
            raise DataError(f"request {r.get('name')!r}: for_each csv {fe.get('csv')!r} not found (fetch that table "
                            "first; the path is relative to the spec's folder)", kind="bad_request")
        pairs = dict(fe.get("map") or {}) or {fe.get("param") or fe.get("column"): fe.get("column")}
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            miss = [c for c in pairs.values() if c not in (rd.fieldnames or [])]
            if miss:
                raise DataError(f"request {r.get('name')!r}: for_each column(s) {miss} not in {fe.get('csv')} "
                                f"(columns: {rd.fieldnames})", kind="bad_request")
            low, pre = bool(fe.get("lower")), str(fe.get("strip") or "")
            combos = []
            for x in rd:
                vals = tuple((str(x[c]).strip().lower() if low else str(x[c]).strip()) for c in pairs.values())
                vals = tuple(v[len(pre):] if pre and v.startswith(pre) else v for v in vals)
                if all(vals):
                    combos.append(vals)
            combos = list(dict.fromkeys(combos))
        if len(combos) > int(fe.get("max", FOR_EACH_MAX)):
            raise DataError(f"request {r.get('name')!r}: for_each gives {len(combos)} requests (max "
                            f"{fe.get('max', FOR_EACH_MAX)}): filter the table first, or set \"max\"", kind="bad_request")
        names = list(pairs)
        for i, vals in enumerate(combos, start=1):
            q = {k: x for k, x in r.items() if k != "for_each"}
            q.update(name=f"{r['name']}__{i}", params={**(r.get("params") or {}), **dict(zip(names, vals))},
                     _group=r["name"], _key=tuple(zip(names, vals)), _orig=r)
            reqs.append(q)
    return {**spec, "requests": reqs}


def collapse_for_each(spec: dict) -> dict:
    """The spec as written (each for_each request once), for request.json and the reuse check."""
    reqs, seen = [], set()
    for r in spec.get("requests") or []:
        g = r.get("_group")
        if g is None:
            reqs.append(r)
        elif g not in seen:
            seen.add(g)
            reqs.append(r["_orig"])
    return {**spec, "requests": reqs}


def merge_for_each(spec: dict, tables: dict) -> dict:
    """The expanded requests' tables as one table per for_each request; each row keeps the value it was asked for."""
    out = {}
    for r in spec.get("requests") or []:
        rows = tables.get(r["name"], [])
        g = r.get("_group")
        if g is None:
            out[r["name"]] = rows
            continue
        asked = dict(r["_key"])
        out.setdefault(g, [])
        out[g] += [{**{k: v for k, v in asked.items() if k not in row}, **row} for row in rows]
    return out


# ---------------------------------------------------------------------- fetching
def xlsx_sheets(src, only: set[str] | None = None) -> dict[str, list[list]]:
    """Read sheets of an .xlsx (a path, bytes or a binary file) into lists of row values, in workbook order
    (standard library only). Values are text as stored: numbers are not formatted, dates are serial numbers."""
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    try:
        z = zipfile.ZipFile(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
    except zipfile.BadZipFile:
        raise DataError("not an .xlsx workbook (an old .xls, or an HTML page in its place)", kind="api")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(x.text or "" for x in si.iter(f"{{{ns['m']}}}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    target = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in wb.find("m:sheets", ns):
        name = s.get("name")
        if only is not None and name not in only:
            continue
        tgt = target[s.get(f"{{{ns['r']}}}id")].lstrip("/")
        tgt = tgt if tgt.startswith("xl/") else "xl/" + tgt
        rows = []
        for row in ET.fromstring(z.read(tgt)).iter(f"{{{ns['m']}}}row"):
            vals = {}
            for c in row.findall("m:c", ns):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r", "A")).group(0):
                    col = col * 26 + ord(ch) - 64
                v, typ = c.find("m:v", ns), c.get("t")
                if typ == "s" and v is not None:
                    val = shared[int(v.text)]
                elif typ == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
                else:
                    val = v.text if v is not None else None
                vals[col - 1] = val
            rows.append([vals.get(i) for i in range(max(vals) + 1)] if vals else [])
        out[name] = rows
    return out


def sheet_records(ep: dict, src) -> list[dict]:
    """Rows of one sheet as dicts: ep["sheet"] (name or 1-based number, default 1) and ep["header_row"]
    (1-based, default 1). Blank and repeated column names get colN names; blank rows are dropped."""
    sheets = xlsx_sheets(src)
    want = ep.get("sheet", 1)
    if isinstance(want, int) or str(want).isdigit():
        names = list(sheets)
        if not 1 <= int(want) <= len(names):
            raise DataError(f"the workbook has {len(names)} sheet(s), not sheet {want}", kind="api")
        grid = sheets[names[int(want) - 1]]
    elif want in sheets:
        grid = sheets[want]
    else:
        raise DataError(f"no sheet {want!r} in the workbook (sheets: {', '.join(sheets)})", kind="api")
    hr = int(ep.get("header_row", 1)) - 1
    if len(grid) <= hr:
        return []
    head, seen = [], set()
    for i, h in enumerate(grid[hr]):
        name = " ".join(str(h).split()) if h not in (None, "") else f"col{i + 1}"
        name = name if name not in seen else f"{name}_col{i + 1}"
        seen.add(name)
        head.append(name)
    out = []
    for r in grid[hr + 1:]:
        if not any(v not in (None, "") for v in r):
            continue
        out.append({(head[i] if i < len(head) else f"col{i + 1}"): v for i, v in enumerate(r)})
    return out


def _csv_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _records(ep: dict, resp) -> list:
    path = ep.get("records", "")
    if path == "$csv":
        return list(csv.DictReader(io.StringIO(resp.text())))
    if path == "$xlsx":
        return sheet_records(ep, resp.body)
    if path.startswith("$zip:"):
        try:
            z = zipfile.ZipFile(io.BytesIO(resp.body))
        except zipfile.BadZipFile:
            raise DataError("expected a .zip file, got something else (an HTML page?)", kind="api")
        glob = path[5:].lower() or "*"
        members = sorted(n for n in z.namelist() if fnmatch.fnmatch(n.lower(), glob) and not n.endswith("/"))
        if not members:
            raise DataError(f"no member matching {path[5:]!r} in the zip ({', '.join(z.namelist()[:10])})",
                            kind="api")
        rows = []
        for n in members:
            if n.lower().endswith(".xlsx"):
                rows += sheet_records(ep, z.read(n))
            else:
                rows += list(csv.DictReader(io.StringIO(_csv_text(z.read(n)))))
        return rows
    data = resp.json()
    if path == "$table":
        if not data:
            return []
        head, *rows = data
        return [dict(zip(head, r)) for r in rows]
    recs = dig(data, path)
    if recs is None:
        return []
    return recs if isinstance(recs, list) else [recs]


def _clean(rows: list, ep: dict) -> list[dict]:
    missing = set(ep.get("missing", [""]))
    out = []
    for r in rows:
        r = flatten(r) if ep.get("flatten", True) else (r if isinstance(r, dict) else {"value": r})
        out.append({k: ("" if (v is None or (isinstance(v, str) and v.strip() in missing)) else v) for k, v in r.items()})
    return out


def fetch_request(client: Client, ep: dict, params: dict, max_rows: int | None = None) -> tuple[list[dict], dict]:
    """All pages of one request. Returns (rows, meta {pages, total, truncated, urls})."""
    pg = ep.get("paging") or {"type": "none"}
    kind = pg.get("type", "none")
    limit = min(int(max_rows or ep.get("max_rows") or 100000), int(ep.get("max_rows") or 10 ** 9))
    size = int(pg.get("size") or 1000)
    floor = int(pg.get("min_size") or 1)       # some APIs refuse small pages (regulations.gov: 5); rows are cut to limit
    path, query, body = split(ep, params)
    method = ep.get("method", "GET")
    rows: list = []
    meta = {"pages": 0, "total": None, "truncated": False}
    page = int(pg.get("start", 1))
    offset = int(query.get(pg.get("offset_param", ""), 0) or 0) if kind == "offset" else 0
    cursor = None
    next_url = None
    prev = None

    def put(target: dict | None, k, v):
        if k:
            (target if target is not None else query)[k] = v

    while True:
        q, b = dict(query), (dict(body) if body is not None else None)
        tgt = b if (method == "POST" and pg.get("in", "body") == "body") else q
        asked = min(size, limit - len(rows)) if kind == "offset" else min(size, limit)   # rows this page may hold
        if kind == "offset":
            put(tgt, pg.get("limit_param"), asked)
            put(tgt, pg.get("offset_param"), offset)
        elif kind == "page":
            put(tgt, pg.get("page_param"), page)
            put(tgt, pg.get("size_param"), max(min(size, limit), floor))
        elif kind == "cursor":
            put(tgt, pg.get("size_param"), max(min(size, limit), floor))
            if cursor:
                put(tgt, pg.get("cursor_param"), cursor)
        if next_url:
            resp = client.request(method, next_url, None, json_body=b)
        else:
            resp = client.request(method, path, q, json_body=b)
        meta["pages"] += 1
        got = _records(ep, resp)
        if got and kind in ("offset", "page") and got == prev:   # the API ignored the page/offset: stop, do not loop
            meta["truncated"] = meta["repeated_page"] = True
            break
        prev = got
        rows += got
        if ep.get("total") and resp.body[:1] in (b"{", b"["):
            try:
                t = dig(resp.json(), ep["total"])
                meta["total"] = int(t) if t is not None else meta["total"]
            except (ValueError, TypeError):
                pass
        if len(rows) >= limit:
            # more rows exist when the API says so, or when the page came back as full as we asked (for paging
            # types that cap each page; a "none" endpoint returns everything at once, so a full limit is the end)
            meta["truncated"] = ((meta["total"] or 0) > limit or
                                 (kind != "none" and len(got) >= asked) or (kind == "none" and len(got) > limit))
            rows = rows[:limit]
            break
        if kind == "none" or not got:
            break
        if kind == "offset":
            if len(got) < min(size, limit):
                break
            offset += len(got)
        elif kind == "page":
            if len(got) < min(size, limit) or (meta["total"] is not None and len(rows) >= meta["total"]):
                break
            page += 1
        elif kind == "cursor":
            cursor = dig(resp.json(), pg["next"])
            if not cursor:
                break
        elif kind == "link":
            next_url = dig(resp.json(), pg["next"])
            if not next_url:
                break
        if meta["pages"] >= int(pg.get("max_pages", 1000)):
            meta["truncated"] = True
            break
    return _clean(rows, ep), meta


def dedupe_pages(rows: list[dict], ep: dict, meta: dict, name: str) -> tuple[list[dict], str | None]:
    """Offset or page paging over an order the API does not keep stable repeats rows at page boundaries and
    skips as many (the result has the right count but not the right rows). With the endpoint's `unique_key`
    (a key: one row per value at the source), repeated rows are dropped and the note says how many may be
    missing."""
    col = ep.get("unique_key")
    if not col or meta.get("pages", 0) < 2 or not rows or any(col not in r for r in rows):
        return rows, None
    seen, out = set(), []
    for r in rows:
        if r[col] not in seen:
            seen.add(r[col])
            out.append(r)
    if len(out) == len(rows):
        return rows, None
    gap = len(rows) - len(out)
    return out, (f"{name}: {gap:,} row(s) came back twice across page boundaries (same {col}), so the source's "
                 f"order shifted while paging and about as many rows may be missing; the repeats were dropped "
                 f"({len(out):,} rows kept" + (f" of {meta['total']:,}" if meta.get("total") else "") +
                 "). Sort by a unique field (or narrow the request to one page) and fetch again (--force).")


def _fetch_patiently(c: Client, ep: dict, params: dict, max_rows, waits=(20, 60)):
    """fetch_request, waiting out a server that stays busy past the client's own retries (Congress.gov answered
    HTTP 503 for a minute at a time in a 1,244-call for_each, Oct 2026): one bad minute must not lose the run."""
    for wait in (*waits, None):
        try:
            return fetch_request(c, ep, params, max_rows)
        except DataError as e:
            if wait is None or e.kind != "http" or e.status not in (500, 502, 503, 504) \
                    or os.environ.get("DATA_ANALYST_FIXTURES"):
                raise
            import sys
            import time
            print(f"[{c.name}] HTTP {e.status}: waiting {wait} s, then trying again", file=sys.stderr, flush=True)
            time.sleep(wait)


class Misses:
    """One missing item must not hide the rest: every request runs, and the ones the source does not have (404) or
    that came back empty are named together at the end, so one fix covers them all. Packs with their own fetch
    loop use it too:
        miss = gr.Misses()
        for r in spec["requests"]:
            try: rows = ...
            except DataError as e:
                if e.kind != "not_found": raise
                miss.not_found(r, tables, notes); continue
            miss.check_empty(r, rows); tables[r["name"]] = rows
        miss.raise_if_any(missing_hint=..., empty_hint=...)"""

    MISSING_HINT = "check the IDs"
    EMPTY_HINT = "check the filter values (see source.md)"

    def __init__(self):
        self.missing, self.empty = [], []

    def not_found(self, r: dict, tables: dict, notes: list) -> None:
        if r.get("allow_empty"):
            notes.append(f"{r['name']}: not found at the source (404); kept as an empty table (allow_empty).")
            tables[r["name"]] = []
        else:
            self.missing.append(r["name"])

    def check_empty(self, r: dict, rows: list) -> None:
        if not rows and not r.get("allow_empty"):
            key = r.get("_key")                 # a for_each row: name it by its values (billNumber=16), not __2
            self.empty.append(r["name"] + (" (" + ", ".join(f"{k}={v}" for k, v in key) + ")" if key else ""))

    def raise_if_any(self, missing_hint: str = "", empty_hint: str = "") -> None:
        msgs = []
        if self.missing:
            msgs.append(f"request(s) {', '.join(map(repr, self.missing))} were not found at the source (404): "
                        f"{missing_hint or self.MISSING_HINT}. If the item really does not exist yet (e.g. a volume "
                        "not yet published), add \"allow_empty\": true to that request in spec.json and run again; the "
                        "answer must say it is missing.")
        if self.empty:
            msgs.append(f"request(s) {', '.join(map(repr, self.empty))} returned no rows: {empty_hint or self.EMPTY_HINT}. "
                        "If none is a correct answer (e.g. no private laws in that Congress), add \"allow_empty\": "
                        "true to that request in spec.json and run again.")
        if msgs:
            raise DataError(" ".join(msgs), kind="not_found" if self.missing else "bad_request")


def fetch(pack, spec: dict, client: Client | None = None) -> tuple[dict, list[str]]:
    """Every request in the spec. Returns ({name: rows}, notes)."""
    c = client or client_for(pack)
    tables, notes, miss = {}, [], Misses()
    for r in spec["requests"]:
        ep, params, probs = resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        extra = [k for k in (r.get("params") or {})          # a note, not an error: some adapters take extra params
                 if k not in (ep.get("params") or {}) and k not in (ep.get("defaults") or {})]
        if extra:
            notes.append(f"{r['name']}: parameter(s) {', '.join(extra)} not declared for endpoint "
                         f"{r.get('endpoint')!r} (endpoints.json); the API may ignore them: check the rows")
        if pack.has("before_request"):
            params = pack.adapter.before_request(r, ep, params) or params
        try:
            rows, meta = _fetch_patiently(c, ep, params, r.get("max_rows"))
        except DataError as e:                  # say which request failed (a spec can hold hundreds)
            if e.kind == "not_found":           # no such item: run the rest, then name every missing one
                miss.not_found(r, tables, notes)
                continue
            raise DataError(f"request {r['name']!r} ({r.get('endpoint')}): {e}", kind=e.kind,
                            status=getattr(e, "status", None)) from None
        rows, dup = dedupe_pages(rows, ep, meta, r["name"])
        if dup:
            notes.append(dup)
        if pack.has("request_note"):        # how the pack read the request (e.g. phrase vs every word)
            n = pack.adapter.request_note(r, ep, params)
            if n:
                notes.append(n)
        if pack.has("normalize"):
            rows = pack.adapter.normalize(r, ep, rows)
        if meta.get("repeated_page"):
            notes.append(f"{r['name']}{INCOMPLETE} stopped at {len(rows):,} rows: page {meta['pages']} repeated the page "
                         "before it (the API ignored the paging parameter), so later rows may be missing. Narrow the "
                         "request so it fits one page.")
        elif meta["truncated"]:
            tn = ep.get("truncated_note") or {}
            k = tn.get("when_default")      # the note describes the pack's own order: only when it was sent
            own_order = bool(tn.get("text") and k and params.get(k) not in (None, "")
                             and (r.get("params") or {}).get(k) in (None, ""))
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); the source has more"
                         + (f" ({meta['total']:,} in total)" if meta["total"] else "") + ". Narrow the filter or raise max_rows."
                         + (" " + tn["text"] if own_order else ""))
        miss.check_empty(r, rows)
        tables[r["name"]] = rows
    miss.raise_if_any()                     # every request ran: name every missing and empty one at once
    return tables, notes


# ---------------------------------------------------------------------- manual download
def manual_items(pack, spec: dict) -> list[dict]:
    """One link per page the request would need (up to 25), with the key as {API_KEY}."""
    eps = pack.endpoints or {}
    base = (eps.get("base_url") or pack.policy["routes"].get("api", {}).get("base", "")).rstrip("/")
    a = pack.policy["auth"]
    needs_key = a["required"] == "required"
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = resolve(pack, r)
        if probs:
            continue
        if pack.has("before_request"):              # links must match what the fetch sends
            params = pack.adapter.before_request(r, ep, params) or params
        path, query, body = split(ep, params)
        pg = ep.get("paging") or {"type": "none"}
        # a browser download can take a much larger page than the script uses (manual_size)
        size = int(pg.get("manual_size") or pg.get("size") or 1000)
        limit = int(r.get("max_rows") or size)
        pages = 1 if pg.get("type") in ("none", "cursor", "link") else min(25, max(1, math.ceil(limit / size)))
        rec = ep.get("records") or ""
        ext = {"$csv": "csv", "$xlsx": "xlsx"}.get(rec, "zip" if rec.startswith("$zip:") else "json")
        for i in range(pages):
            q = dict(query)
            b = dict(body) if body is not None else None
            tgt = b if (ep.get("method") == "POST" and pg.get("in", "body") == "body" and b is not None) else q
            if pg.get("type") == "offset":
                tgt[pg["limit_param"]], tgt[pg["offset_param"]] = size, i * size
            elif pg.get("type") == "page":
                tgt[pg["page_param"]], tgt[pg["size_param"]] = int(pg.get("start", 1)) + i, size
            url = path if path.startswith(("http://", "https://")) else f"{base}/{path.lstrip('/')}"
            if q:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q, doseq=True)
            if needs_key and a.get("placement") == "query":
                url += ("&" if "?" in url else "?") + f"{a['param']}={{API_KEY}}"
            elif a["required"] == "optional" and a.get("demo_key") and a.get("placement") == "query":
                url += ("&" if "?" in url else "?") + f"{a['param']}={a['demo_key']}"
            fname = f"{r['name']}.{ext}" if pages == 1 else f"{r['name']}_{i + 1}.{ext}"
            note = "" if pages == 1 else f"page {i + 1} of up to {pages}; only needed if the previous page held {size:,} rows"
            if pages == 1 and pg.get("type") in ("offset", "page"):
                note = f"holds up to {size:,} rows; if it holds exactly that many, more rows exist: narrow the filter"
            if pg.get("type") in ("cursor", "link"):
                note = "first page only: this API pages with a cursor, so the script route is needed for more rows"
            # parse_upload below also reads a CSV saved for a JSON link (an export from the dataset's page)
            items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url, "filename": fname,
                          "also_ext": [".csv"] if ext == "json" else [],
                          "needs_key": needs_key, "method": ep.get("method", "GET"), "body": b, "note": note,
                          "optional": i > 0})
    return items


def parse_upload(pack, req: dict, paths: list[str]) -> list[dict]:
    """Rows from uploaded files for one request (its pages in order)."""
    ep, _, _ = resolve(pack, req)
    rows = []
    for p in sorted(paths, key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]):
        with open(p, "rb") as f:
            data = f.read()

        class R:
            body = data
            def json(self):
                return json.loads(data.decode("utf-8-sig"))
            def text(self):
                return data.decode("utf-8-sig", errors="replace")
        if p.lower().endswith(".csv") and ep.get("records") != "$csv":
            rows += list(csv.DictReader(io.StringIO(R().text())))
        else:
            rows += _records(ep, R())
    rows = _clean(rows, ep)
    if pack.has("normalize"):
        rows = pack.adapter.normalize(req, ep, rows)
    return rows


def describe_endpoints(pack) -> list[dict]:
    eps = pack.endpoints or {}
    out = [{"endpoint": n, "method": e.get("method", "GET"), "path": e["path"], "description": e.get("description", ""),
            "params": {k: v.get("desc", "") + (" (required)" if v.get("required") else "")
                       for k, v in (e.get("params") or {}).items()}}
           for n, e in sorted((eps.get("endpoints") or {}).items())]
    return out


def search_catalog(pack, query: str, limit: int = 20) -> list[dict]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    hits = []
    for alias, c in ((pack.endpoints or {}).get("catalog") or {}).items():
        text = f"{alias} {c.get('id', '')} {c.get('name', '')} {c.get('description', '')}".lower()
        s = sum(text.count(w) for w in words)
        if s:
            hits.append((s, {"alias": alias, **c}))
    for n, e in ((pack.endpoints or {}).get("endpoints") or {}).items():
        text = f"{n} {e.get('description', '')}".lower()
        s = sum(text.count(w) for w in words)
        if s:
            hits.append((s, {"endpoint": n, "description": e.get("description", "")}))
    return [h for _, h in sorted(hits, key=lambda x: -x[0])[:limit]]

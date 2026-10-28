"""Declarative adapter for JSON/CSV REST APIs (Socrata, DKAN, CKAN, plain REST). Standard library only.

A standard pack describes its API in endpoints.json; this module turns a spec into requests, pages through
the results, flattens records into rows and builds manual-download items. A pack's adapter.py can override
or extend any step (see docs/pack_contract.md in the data-analyst repo).

endpoints.json:
{
  "base_url": "https://data.cdc.gov/resource",
  "timeout": 30,                             # seconds per attempt (slow APIs such as CMS need more)
  "endpoints": {
    "<name>": {
      "path": "/{dataset}.json",            # {x} placeholders come from params with "in": "path"
      "method": "GET",                       # or POST (params with "in": "body" go in the JSON body)
      "description": "...",
      "params": {"dataset": {"in": "path", "required": true, "desc": "...", "catalog": true},
                 # "in": "query" (default for GET) | "path" | "body" (default for POST) | "spec" (never sent)
                 "$where": {"desc": "SoQL filter"}, "state": {"desc": "...", "enum": [...]}},
      "defaults": {"$order": ":id"},        # sent unless the spec overrides them
      "body": {...},                         # POST body template (spec params with in=body merge into it)
      "records": "results",                  # dotted path to the list of records; "" = body is the list;
                                             # "$table" = list of lists whose first row is the header (Census);
                                             # "$csv" = CSV text
      "total": "meta.count",                 # optional dotted path to the total row count
      "paging": {"type": "offset", "limit_param": "$limit", "offset_param": "$offset", "size": 1000,
                 "manual_size": 50000}       # manual_size: rows per browser download (default size)
                # or {"type": "page", "page_param": "page", "size_param": "per_page", "size": 100, "start": 1,
                #     "min_size": 5}   (optional: smallest page the API accepts)
                # or {"type": "cursor", "cursor_param": "cursor", "next": "pagination.next_cursor", "size_param": "limit", "size": 100}
                # or {"type": "link", "next": "links.next"}   or {"type": "none"}
      "max_rows": 100000,                    # hard stop; the spec may set a lower one
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
import io
import json
import math
import os
import re
import urllib.parse

from core.http import Client, DataError


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
                  headers=eps.get("headers"), **{"timeout": eps.get("timeout", 30), **kw})


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
    for k, v in params.items():
        where = (pdefs.get(k) or {}).get("in", "body" if ep.get("method") == "POST" else "query")
        if where == "spec":                          # read by the adapter, never sent
            continue
        if where == "path" or "{" + k + "}" in path:
            raw = (pdefs.get(k) or {}).get("raw")          # "raw": true keeps slashes (Census dataset paths)
            path = path.replace("{" + k + "}", urllib.parse.quote(str(v), safe="/" if raw else ""))
        elif where == "body":
            body = body if body is not None else {}
            body[k] = v
        else:
            query[k] = v
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
def _records(ep: dict, resp) -> list:
    path = ep.get("records", "")
    if path == "$csv":
        return list(csv.DictReader(io.StringIO(resp.text())))
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


def fetch(pack, spec: dict, client: Client | None = None) -> tuple[dict, list[str]]:
    """Every request in the spec. Returns ({name: rows}, notes)."""
    c = client or client_for(pack)
    tables, notes, empty, missing = {}, [], [], []
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
                if r.get("allow_empty"):
                    notes.append(f"{r['name']}: not found at the source (404); kept as an empty table (allow_empty).")
                    tables[r["name"]] = []
                else:
                    missing.append(r["name"])
                continue
            raise DataError(f"request {r['name']!r} ({r.get('endpoint')}): {e}", kind=e.kind,
                            status=getattr(e, "status", None)) from None
        if pack.has("normalize"):
            rows = pack.adapter.normalize(r, ep, rows)
        if meta["truncated"]:
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); the source has more"
                         + (f" ({meta['total']:,} in total)" if meta["total"] else "") + ". Narrow the filter or raise max_rows.")
        if not rows and not r.get("allow_empty"):
            key = r.get("_key")                 # a for_each row: name it by its values (billNumber=16), not __2
            empty.append(r["name"] + (" (" + ", ".join(f"{k}={v}" for k, v in key) + ")" if key else ""))
        tables[r["name"]] = rows
    msgs = []                               # every request ran: name every missing and empty one at once
    if missing:
        msgs.append(f"request(s) {', '.join(map(repr, missing))} were not found at the source (404): check the "
                    "IDs. If the item really does not exist yet (e.g. a volume not yet published), add "
                    "\"allow_empty\": true to that request in spec.json and run again; the answer must say it "
                    "is missing.")
    if empty:
        msgs.append(f"request(s) {', '.join(map(repr, empty))} returned no rows: check the filter values (see "
                    "source.md). If none is a correct answer (e.g. no private laws in that Congress), add "
                    "\"allow_empty\": true to that request in spec.json and run again.")
    if msgs:
        raise DataError(" ".join(msgs), kind="not_found" if missing else "bad_request")
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
        ext = "csv" if ep.get("records") == "$csv" else "json"
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

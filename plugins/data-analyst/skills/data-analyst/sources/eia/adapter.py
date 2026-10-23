"""EIA API v2 adapter: route paths, bracket parameters, state facets, withheld-value flags.

- Routes are paths in the v2 tree (electricity/retail-sales, seds, petroleum/pri/spt) or catalog aliases;
  the pack fills /{route}/data/ itself because routes contain slashes.
- data params: `data` (list of columns, default ["value"] or the catalog's), `facets` ({facet: [values]}),
  `frequency`, `start`, `end`, `sort` ([{"column", "direction"}] or "period:desc"; default period desc)
  become EIA's bracket parameters data[0]=, facets[stateid][]=, sort[0][column]=. Raw bracket params
  pass through unchanged. `state` fills the route's state facet (stateid, stateId or location, from the
  catalog) with the USPS code; `msn` fills the route's MSN facet (seriesId in SEDS, msn in total-energy);
  a bare year as start/end of a sub-annual series is widened to the whole year.
- normalize: values stay as published; non-numeric markers in data columns ("W" withheld, "NA", "--",
  "NM") become empty cells with a `<column>_flag` column holding the marker.
- check_error: {"error": ...} bodies (bad facet, bad route, missing key) become errors.
- Without EIA_API_KEY the client sends api.data.gov's DEMO_KEY (EIA's API runs on api.data.gov; any
  api.data.gov key works as EIA_API_KEY).
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
MARKERS = {"W", "NA", "--", "NM", "(s)", "*", "-", "w", "na"}


def _eps() -> dict:
    from core import packfiles
    return packfiles.read_json(HERE, "endpoints.json")


def _usps(x) -> str:
    s = str(x).strip()
    if s.upper() == "US":
        return "US"
    p = os.path.join(HERE, "..", "..", "scripts", "analysis")
    if p not in sys.path:
        sys.path.insert(0, p)
    from combine import state
    return state(s, "usps")


def norm_route(r) -> str:
    r = str(r or "").strip().strip("/")
    return re.sub(r"/data$", "", r)


def _entry(route: str) -> dict | None:
    for c in _eps()["catalog"].values():
        if c.get("kind") == "route" and c["id"] == route:
            return c
    return None


def check_error(status, body: bytes):
    head = body[:400].lstrip()
    if not head.startswith(b"{") or b'"error"' not in head:
        return None
    try:
        e = json.loads(body.decode("utf-8-sig")).get("error")
    except ValueError:
        return None
    if isinstance(e, dict):
        e = f"{e.get('code', '')}: {e.get('message', '')}"
    return f"EIA API error: {e}" if e else None


def check_spec(spec) -> list[str]:
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        if (r.get("endpoint") or "data") == "data":
            cat = _eps()["catalog"]
            raw = p.get("route") or r.get("dataset")
            route = norm_route(cat[raw]["id"] if raw in cat else raw)
            e = _entry(route)
            if not route:
                probs.append(f"request {r.get('name')!r}: give a route (electricity/retail-sales, seds, ...)")
            if p.get("state") is not None and not (e or {}).get("state_facet"):
                probs.append(f"request {r.get('name')!r}: 'state' is only known for routes with a state facet "
                             "(seds, electricity/retail-sales, ...); use facets with the route's facet id")
            if p.get("state") is not None:
                try:
                    for s in (p["state"] if isinstance(p["state"], list) else [p["state"]]):
                        _usps(s)
                except ValueError as ex:
                    probs.append(f"request {r.get('name')!r}: {ex}")
            if e and p.get("frequency") and e.get("frequency") and p["frequency"] not in e["frequency"]:
                probs.append(f"request {r.get('name')!r}: frequency {p['frequency']!r} is not offered by {route} "
                             f"({', '.join(e['frequency'])})")
    return probs


# A bare year as start/end of a sub-annual series is read by the API as that year's first period, so
# end=2023 on monthly data stops at 2023-01 without a warning (verified 2026-10). Widen it to the whole year.
_YEAR_BOUNDS = {"monthly": ("-01", "-12"), "quarterly": ("-Q1", "-Q4"), "weekly": ("-01-01", "-12-31"),
                "daily": ("-01-01", "-12-31"), "hourly": ("-01-01T00", "-12-31T23"),
                "local-hourly": ("-01-01T00", "-12-31T23")}


def whole_year(value, frequency, key: str):
    s = str(value).strip() if value not in (None, "") else None
    if s and len(s) == 4 and s.isdigit() and frequency in _YEAR_BOUNDS:
        return s + _YEAR_BOUNDS[frequency][0 if key == "start" else 1]
    return s if s is None else value


def prepare(req, ep, params) -> tuple[dict, dict]:
    params = copy.deepcopy(params)
    kind = req.get("endpoint") or "data"
    route = norm_route(params.pop("route", ""))
    e = _entry(route) or {}
    if kind == "facet_values":
        facet = params.pop("facet")
        return {**ep, "path": f"/{route}/facet/{urllib.parse.quote(str(facet))}/", "params": {}}, params
    if kind in ("routes", "facets", "metadata"):
        return {**ep, "path": f"/{route}/" if route else "/", "params": {}}, params
    q = {}
    data = params.pop("data", None) or e.get("data_default") or ["value"]
    for i, d in enumerate([data] if isinstance(data, str) else data):
        q[f"data[{i}]"] = d
    facets = dict(params.pop("facets", None) or {})
    st = params.pop("state", None)
    if st is not None:
        facets[e["state_facet"]] = [_usps(s) for s in (st if isinstance(st, list) else [st])]
    msn = params.pop("msn", None)
    if msn is not None:
        # the MSN facet is named per route: seriesId in SEDS, msn in total-energy (verified 2026-10)
        name = next((f for f in ("seriesId", "msn") if f in (e.get("facets") or [])), "seriesId")
        facets[name] = [str(m).upper() for m in (msn if isinstance(msn, list) else [msn])]
    for f, vals in facets.items():
        q[f"facets[{f}][]"] = vals if isinstance(vals, list) else [vals]
    sort = params.pop("sort", None) or [{"column": "period", "direction": "desc"}]
    if isinstance(sort, str):
        sort = [{"column": s.split(":")[0], "direction": (s.split(":") + ["desc"])[1]} for s in sort.split(",")]
    for i, s in enumerate(sort):
        q[f"sort[{i}][column]"], q[f"sort[{i}][direction]"] = s["column"], s.get("direction", "desc")
    if e.get("frequency") and not params.get("frequency"):
        params["frequency"] = e.get("default_frequency") or e["frequency"][0]
    for key in ("start", "end"):
        params[key] = whole_year(params.get(key), params.get("frequency"), key)
        if params[key] is None:
            params.pop(key)
    q.update(params)
    return {**ep, "path": f"/{route}/data/", "params": {}}, q


def fetch(spec, pack, route):
    from core import generic_rest as gr
    from core.http import DataError
    client = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        try:
            ep2, q = prepare(r, ep, params)
        except (KeyError, ValueError) as e:
            raise DataError(f"request {r['name']!r}: {e}", kind="bad_request")
        rows, meta = gr.fetch_request(client, ep2, q, r.get("max_rows"))
        rows = normalize(r, ep2, rows)
        if meta["truncated"]:
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows)"
                         + (f" of {meta['total']:,}" if meta["total"] else "") + "; narrow the facets or dates")
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check the facet values (facet_values "
                            "endpoint), the frequency and the start/end format (YYYY, YYYY-MM, YYYY-MM-DD)",
                            kind="bad_request")
        if (r.get("endpoint") or "data") == "data":
            notes.append(f"{r['name']}: route {ep2['path']} frequency {q.get('frequency', '(default)')}")
        tables[r["name"]] = rows
    return tables, notes


def normalize(req, ep, rows):
    if (req.get("endpoint") or "data") != "data" or not rows:
        return rows
    p = req.get("params") or {}
    data = p.get("data") or ["value"]
    cols = [data] if isinstance(data, str) else list(data)
    for r in rows:
        for c in cols:
            v = r.get(c)
            if isinstance(v, str) and v.strip() in MARKERS:
                r[f"{c}_flag"] = v.strip()
                r[c] = ""
    flagged = {k for r in rows for k in r if k.endswith("_flag")}
    for r in rows:
        for k in flagged:
            r.setdefault(k, "")
    return rows


def ping(client):
    from core.http import DataError
    try:
        client.get("/")
        return {"auth_ok": True, "kind": "", "detail": "route list answered with the key"}
    except DataError as e:
        return {"auth_ok": e.kind not in ("auth", "key_missing"), "kind": e.kind, "detail": str(e)}


def manual_items(spec, pack):
    from core import generic_rest as gr
    base = pack.endpoints["base_url"].rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        try:
            ep2, q = prepare(r, ep, params)
        except (KeyError, ValueError):
            continue
        pg = ep2.get("paging") or {"type": "none"}
        size = int(pg.get("size") or 5000)
        pages = 1
        if pg.get("type") == "offset":
            pages = min(25, max(1, -(-int(r.get("max_rows") or size) // size)))
        for i in range(pages):
            qq = dict(q)
            if pg.get("type") == "offset":
                qq[pg["limit_param"]], qq[pg["offset_param"]] = size, i * size
            qq["api_key"] = "DEMO_KEY"
            url = f"{base}{ep2['path']}?" + urllib.parse.urlencode(qq, doseq=True)
            items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url,
                          "filename": f"{r['name']}.json" if pages == 1 else f"{r['name']}_{i + 1}.json",
                          "needs_key": False, "method": "GET", "body": None,
                          "note": ("DEMO_KEY allows only a few requests an hour: put your own key in its place "
                                   "if the link answers OVER_RATE_LIMIT") +
                                  ("" if pages == 1 else f"; page {i + 1} of up to {pages}, stop when data is empty")})
    return items


def parse_upload(pack, req, paths):
    from core import generic_rest as gr
    ep, _, _ = gr.resolve(pack, req)
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            doc = json.loads(f.read().decode("utf-8-sig"))
        rows += gr.dig(doc, ep.get("records", "response.data")) or []
    return normalize(req, ep, gr._clean(rows, ep))


def search(query, pack):
    from core import generic_rest as gr
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    scored = []
    for h in gr.search_catalog(pack, query, limit=500):
        text = " ".join(str(v) for v in h.values()).lower()
        scored.append((sum(1 for w in words if w in text), h))
    return [h for _, h in sorted(scored, key=lambda x: -x[0])][:20]

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
- keyless_files / parse_upload: a petroleum request for one series (facets {"series": [id]}) is also in EIA's
  keyless history file www.eia.gov/dnav/pet/hist_xls/<series><d|w|m|a>.xls, which --ingest reads (needs xlrd)
  and cuts to the request's dates; the route when the shared DEMO_KEY is rate-limited.
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
            kf = dnav_file(r, params)
            items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url,
                          "filename": f"{r['name']}.json" if pages == 1 else f"{r['name']}_{i + 1}.json",
                          "needs_key": False, "method": "GET", "body": None,
                          "alt_filenames": [kf["filename"]] if kf else [],
                          "note": ("DEMO_KEY allows only a few requests an hour: put your own key in its place "
                                   "if the link answers OVER_RATE_LIMIT") +
                                  ("" if pages == 1 else f"; page {i + 1} of up to {pages}, stop when data is empty") +
                                  (f"; without a key, EIA's file {kf['url']} (as downloaded) holds the same series"
                                   if kf else "")})
    return items


# ---------------------------------------------------------------------- dnav history files (the keyless route)
# EIA publishes every petroleum series of the dnav tables as a keyless Excel 97 workbook, one series per file:
# www.eia.gov/dnav/pet/hist_xls/<series><d|w|m|a>.xls (e.g. EMM_EPMR_PTE_NUS_DPGm.xls, 50 KB; RWTCd.xls, 470 KB;
# checked 2026-10-30). Sheet "Data 1": row 0 a title, row 1 "Sourcekey" and the series id, row 2 "Date" and the
# series description with its units in parentheses, then one row per period: an Excel date (monthly rows dated
# the 15th, annual rows July 1, weekly rows the API's day) and the value; a period with no value is an empty cell.
# A series lacks a file at a frequency EIA does not publish for it (MGFUPUS2w.xls is HTTP 404).
DNAV_URL = "https://www.eia.gov/dnav/pet/hist_xls/"
DNAV_FREQ = {"daily": "d", "weekly": "w", "monthly": "m", "annual": "a"}


def dnav_file(req, params) -> dict | None:
    """The keyless file that holds this request's rows ({"request", "url", "filename"}), None when there is none:
    a data request of a petroleum route for exactly one series, the value column and no other facet."""
    if (req.get("endpoint") or "data") != "data":
        return None
    p = params
    route = norm_route(p.get("route"))
    facets = p.get("facets") or {}
    series = facets.get("series")
    series = series if isinstance(series, list) else [series]
    data = p.get("data") or ["value"]
    freq = p.get("frequency") or (_entry(route) or {}).get("default_frequency")
    if (not route.startswith("petroleum/") or set(facets) != {"series"} or len(series) != 1
            or not re.fullmatch(r"[A-Za-z0-9_]+", str(series[0] or "")) or freq not in DNAV_FREQ
            or ([data] if isinstance(data, str) else list(data)) != ["value"]
            or any(p.get(k) is not None for k in ("state", "msn"))):
        return None
    fn = f"{series[0]}{DNAV_FREQ[freq]}.xls"
    return {"request": req["name"], "url": DNAV_URL + fn, "filename": fn, "frequency": freq}


def keyless_files(spec, pack) -> list[dict]:
    """The keyless files that hold every request of the spec; [] when one request has none or xlrd (which reads
    them) is not installed, so --check never offers a file --ingest could not read."""
    from core import generic_rest as gr
    try:
        import xlrd  # noqa: F401
    except ImportError:
        return []
    files = []
    for r in spec.get("requests") or []:
        _, params, probs = gr.resolve(pack, r)
        f = None if probs else dnav_file(r, params)
        if not f:
            return []
        files.append(f)
    return files


def _dnav_rows(path, req, params) -> list[dict]:
    from core.http import DataError
    name, fn = req["name"], os.path.basename(path)
    want = dnav_file(req, params)
    if not want:
        raise DataError(f"request {name!r}: {fn} is an EIA history file, which holds one petroleum series at one "
                        "frequency: the request must give a petroleum route, facets {\"series\": [one id]} and no "
                        "other facet", kind="bad_request")
    if not fn.lower().startswith(want["filename"][:-4].lower()):
        raise DataError(f"request {name!r}: {fn} is not {want['filename']} (the series and frequency letter "
                        "d/w/m/a must match the request)", kind="bad_request")
    try:
        import xlrd
    except ImportError:
        raise DataError(f"request {name!r}: reading {fn} needs the Python package xlrd, which is not installed. "
                        "Install it if the network allows (pip install xlrd), or upload the API's JSON instead "
                        "(manual_download/INSTRUCTIONS.md)", kind="install_deps")
    try:
        book = xlrd.open_workbook(path)
        sh = book.sheet_by_name("Data 1")
        keys = [str(c.value).strip() for c in sh.row(1)]
    except Exception as e:  # not an Excel 97 workbook, no "Data 1" sheet, a truncated download
        raise DataError(f"request {name!r}: cannot read {fn} as an EIA history file ({type(e).__name__}: {e})",
                        kind="bad_request")
    series = params["facets"]["series"]
    series = str(series[0] if isinstance(series, list) else series)
    if keys[:1] != ["Sourcekey"] or series not in keys:
        raise DataError(f"request {name!r}: {fn} has no Sourcekey {series} in sheet Data 1", kind="bad_request")
    col = keys.index(series)
    desc = str(sh.cell_value(2, col)).strip()
    units = re.search(r"\(([^()]*)\)\s*$", desc)
    freq = want["frequency"]
    lo, hi = (whole_year(params.get(k), freq, k) for k in ("start", "end"))
    rows = []
    for i in range(3, sh.nrows):
        d, v = sh.cell(i, 0), sh.cell(i, col)
        if d.ctype != xlrd.XL_CELL_DATE or v.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
            continue
        y, m, day = xlrd.xldate_as_tuple(d.value, book.datemode)[:3]
        period = {"annual": f"{y:04d}", "monthly": f"{y:04d}-{m:02d}"}.get(freq, f"{y:04d}-{m:02d}-{day:02d}")
        if (lo and period < str(lo)[:len(period)]) or (hi and period > str(hi)[:len(period)]):
            continue
        value = format(v.value, ".15g") if v.ctype == xlrd.XL_CELL_NUMBER else str(v.value).strip()
        rows.append({"period": period, "series": series, "series-description": desc, "value": value,
                     "units": units.group(1) if units else ""})
    if not rows and not req.get("allow_empty"):
        raise DataError(f"request {name!r}: {fn} has no value between {lo or 'the start'} and {hi or 'the end'}",
                        kind="bad_request")
    sort = params.get("sort") or "period:desc"
    asc = "asc" in str(sort if isinstance(sort, str) else sort[0].get("direction", "desc")).lower()
    return sorted(rows, key=lambda r: r["period"], reverse=not asc)


def parse_upload(pack, req, paths):
    """API pages saved as JSON (the manual route), or the series' dnav history file (.xls as downloaded)."""
    from core import generic_rest as gr
    ep, params, _ = gr.resolve(pack, req)
    rows = []
    for p in sorted(set(paths)):          # pages of one request may all match the same history file
        if p.lower().endswith(".xls"):
            rows += _dnav_rows(p, req, params)
            continue
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

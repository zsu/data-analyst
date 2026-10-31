"""DOL API v4 adapter: dataset paths, filter_object building, case-tolerant name search, key-only needs.

- Datasets are addressed as AGENCY/api_url (OSHA/inspection) or by catalog alias (osha_inspection); the
  catalog was built from the keyless Datasets API (https://apiprod.dol.gov/v4/datasets).
- filter_object: DOL takes one JSON object of {field, operator, value} conditions joined by {"and": [...]}
  or {"or": [...]}; operators eq, neq, gt, lt, in, not_in, like (DOL API User Guide). The pack builds it
  from `filters` ({field: value} eq, lists -> in, {"gt": ..} etc.), `search` and `state`, ANDed with any
  raw `filter_object`.
- search: DOL's "like" is case-sensitive and has no implicit wildcards, and there is no case-insensitive
  operator, so {field: text} becomes an OR of %text% in the spelling given, UPPER, lower and Title case
  (OSHA establishment names are stored upper case; WHD trade names in both).
- sort: defaults to the dataset's date field, descending (newest first), as listed in the catalog.
- Empty bodies (no match) count as zero rows. The key goes in the X-API-KEY query parameter (verified in
  the DOL API User Guide; the API's 401 text says the key is missing "from your query").
- key_only_needs: everything except the keyless catalog endpoints (datasets, dataset_info) and the OUI files
  needs a key.

Unemployment insurance files of the Office of Unemployment Insurance (oui.doleta.gov, keyless, fixed names
refreshed in place; endpoints with "host": "oui"):
- ui_trust_fund: ETA 2112 (UI financial transaction summary), /unemploy/csv/ar2112.csv, one row per state and
  month since 1979, columns coded c1..c136. The codes are renamed from the data map (ET Handbook 402, table
  ar2112; endpoints.json "columns"): <line item>_<account>, account total (column C, sum of the three accounts),
  clearing, utf (the state's account in the Unemployment Trust Fund) or bpa (benefit payment account). The map
  prints line 17 F as c23; the report edits (c21 = c22+c23+c24, c8 = c20+c24+...) make it c24. The identities
  c85 = c1+c5-c47, c87 = c3+c7-c49 and the deposit/disbursement sums hold on the live file (checked 2026-10).
  The whole file (23 MB) is downloaded once per fetch and filtered here by state, month and fields.
- ui_data_summary: the UI Data Summary workbook (SummaryTables.xlsx, latest calendar-year quarter). Read from row
  1 so the quarter in the title row is kept (cyq); the header row is found by its first cell ("State").
These go to oui.doleta.gov with a client that has no credentials: the DOL API key is never sent there.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
OUI = ("ui_trust_fund", "ui_data_summary")
KEYLESS = ("datasets", "dataset_info") + OUI


def _eps() -> dict:
    from core import packfiles
    return packfiles.read_json(HERE, "endpoints.json")


def _entry(ds: str) -> dict | None:
    cat = _eps()["catalog"]
    if ds in cat:
        return cat[ds]
    for c in cat.values():
        if c["id"].lower() == str(ds).lower():
            return c
    return None


def key_only_needs(spec) -> list[str]:
    reqs = [r for r in spec.get("requests") or [] if (r.get("endpoint") or "query") not in KEYLESS]
    return ([f"data queries ({', '.join(sorted({r.get('name', '?') for r in reqs}))}); only the dataset catalog "
             "(datasets, dataset_info) is keyless"] if reqs else [])


_FIELDS: dict = {}


def dataset_fields(pack, dataset_id) -> set[str] | None:
    """Column names of one dataset from DOL's keyless field dictionary, or None when it cannot be read (then no
    name is refused: the check is skipped, and a wrong name surfaces as the API's own HTTP 400)."""
    if dataset_id in _FIELDS:
        return _FIELDS[dataset_id]
    from core import generic_rest as gr
    from core.http import DataError
    try:
        doc = gr.client_for(pack, retries=0, timeout=10).get_json(f"/datasets/{int(dataset_id)}", use_key=False)
        cols = {str(m.get("column_name")) for m in (gr.dig(doc, "dataset.dataset_metadatum") or []) if m.get("column_name")}
    except (DataError, ValueError, TypeError):
        cols = set()
    _FIELDS[dataset_id] = cols or None
    return _FIELDS[dataset_id]


def _named_fields(p: dict) -> list[str]:
    """Every field a query request names: filters, search, sort_by and fields (state maps to the catalog's own
    state field)."""
    names = list((p.get("filters") or {}) if isinstance(p.get("filters"), dict) else [])
    names += list((p.get("search") or {}) if isinstance(p.get("search"), dict) else [])
    if p.get("sort_by"):
        names.append(str(p["sort_by"]))
    f = p.get("fields")
    if f:
        names += [x.strip() for x in (f if isinstance(f, list) else str(f).split(",")) if str(x).strip()]
    return list(dict.fromkeys(names))


def check_spec(spec, pack=None) -> list[str]:
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") in OUI:
            probs += _check_oui(r)
            continue
        if (r.get("endpoint") or "query") != "query":
            continue
        p = r.get("params") or {}
        ds = p.get("dataset") or r.get("dataset")
        e = _entry(ds) if ds else None
        if ds and not e and "/" not in str(ds):
            probs.append(f"request {r.get('name')!r}: unknown dataset {ds!r}: use a catalog alias "
                         "(fetch.py --source dol --search ...) or AGENCY/api_url")
        if p.get("state") and not (e or {}).get("state_field"):
            probs.append(f"request {r.get('name')!r}: 'state' is only known for datasets with a state field "
                         "(osha_inspection, whd_enforcement, eta_ui_state_weekly_claims); use filters instead")
        fo = p.get("filter_object")
        if isinstance(fo, str):
            try:
                json.loads(fo)
            except ValueError:
                probs.append(f"request {r.get('name')!r}: filter_object is not valid JSON")
        if r.get("max_rows") and int(r["max_rows"]) > 100000:
            probs.append(f"request {r.get('name')!r}: max_rows above 100,000: narrow the filters")
        # field names: DOL's field dictionary is keyless, so a wrong name is caught here, before a keyless user is
        # sent a bundle of failing links (a filter on a field the dataset lacks never matches)
        named = _named_fields(p) if r.get("check_fields", True) else []
        cols = dataset_fields(pack, e["dataset_id"]) if (pack is not None and named and e and e.get("dataset_id")) else None
        bad = [f for f in named if cols and f not in cols]
        if bad:
            probs.append(f"request {r.get('name')!r}: {', '.join(map(repr, bad))} not a field of {e['id']} (its fields: "
                         f"{', '.join(sorted(cols))}). Field names are DOL's own (dataset_info lists them with meanings); "
                         "if the API does have the field and the dictionary lacks it, set \"check_fields\": false "
                         "on the request")
    return probs


def _variants(text: str) -> list[str]:
    out = []
    for v in (text, text.upper(), text.lower(), text.title()):
        w = v if "%" in v else f"%{v}%"
        if w not in out:
            out.append(w)
    return out


def build_filter(params: dict, entry: dict | None) -> str | None:
    conds = []
    for field, v in (params.pop("filters", None) or {}).items():
        if isinstance(v, dict):
            for op, val in v.items():
                conds.append({"field": field, "operator": op, "value": val})
        elif isinstance(v, (list, tuple)):
            conds.append({"field": field, "operator": "in", "value": list(v)})
        else:
            conds.append({"field": field, "operator": "eq", "value": v})
    for field, text in (params.pop("search", None) or {}).items():
        alts = [{"field": field, "operator": "like", "value": w} for w in _variants(str(text))]
        conds.append(alts[0] if len(alts) == 1 else {"or": alts})
    st = params.pop("state", None)
    if st:
        conds.append({"field": (entry or {})["state_field"], "operator": "eq", "value": str(st).strip().upper()})
    raw = params.pop("filter_object", None)
    if raw:
        conds.append(json.loads(raw) if isinstance(raw, str) else raw)
    if not conds:
        return None
    return json.dumps(conds[0] if len(conds) == 1 else {"and": conds}, separators=(",", ":"))


def prepare(req, ep, params) -> tuple[dict, dict, list[str]]:
    """(endpoint with the path filled in, query params, notes) for one request."""
    params = dict(params)
    kind = req.get("endpoint") or "query"
    notes = []
    if kind == "dataset_info":
        v = str(params.get("dataset_id"))
        if not v.isdigit():
            e = _entry(v) or _entry((req.get("params") or {}).get("dataset_id", ""))
            if not e:
                raise ValueError(f"dataset_id {v!r} is not a numeric id or catalog alias")
            v = str(e["dataset_id"])
        return {**ep, "path": f"/datasets/{v}", "params": {}}, {}, notes
    if kind != "query":
        return ep, params, notes
    raw = (req.get("params") or {}).get("dataset") or req.get("dataset")
    e = _entry(raw) or _entry(params.get("dataset", ""))
    ds = e["id"] if e else str(params.get("dataset"))
    params.pop("dataset", None)
    agency, _, endpoint = ds.partition("/")
    if "sort_by" in params and params["sort_by"] in ("", None):     # explicit: the API's own order
        params.pop("sort_by")
        params.pop("sort", None)
    elif e and e.get("sort_by") and not params.get("sort_by"):
        params["sort_by"] = e["sort_by"]
        params.setdefault("sort", "desc")
    elif params.get("sort_by"):
        params.setdefault("sort", "desc")
    fo = build_filter(params, e)
    if fo:
        params["filter_object"] = fo
        notes.append(f"{req['name']}: filter_object {fo}")
    if e and e.get("columns"):
        notes.append(f"{req['name']}: column meanings: " + "; ".join(f"{k} = {v}" for k, v in e["columns"].items()))
    path = f"/get/{urllib.parse.quote(agency)}/{urllib.parse.quote(endpoint)}/json"
    return {**ep, "path": path, "params": {}}, params, notes


class _EmptyOk:
    """Client wrapper: an empty 200/204 body means no rows."""

    def __init__(self, c):
        self.c = c

    def request(self, method, path, params=None, json_body=None, **kw):
        r = self.c.request(method, path, params, json_body=json_body, ok_statuses=(200, 204))
        if r.status == 204 or not r.body.strip():
            r.body = b'{"data": [], "datasets": []}'
        return r


def fetch(spec, pack, route):
    from core import generic_rest as gr
    from core.http import DataError
    client = _EmptyOk(gr.client_for(pack))
    tables, notes = {}, []
    files: dict = {}
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        if r.get("endpoint") in OUI:
            rows, n = fetch_oui(pack, r, ep, params, files)
            tables[r["name"]] = rows
            notes += n
            continue
        try:
            ep2, q, n = prepare(r, ep, params)
        except (ValueError, KeyError) as e:
            raise DataError(f"request {r['name']!r}: {e}", kind="bad_request")
        rows, meta = gr.fetch_request(client, ep2, q, r.get("max_rows"))
        if meta["truncated"]:
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); narrow the filters or raise max_rows")
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: DOL 'eq' and 'like' are exact and "
                            "case-sensitive; check the values (or use search) and the field names (dataset_info)",
                            kind="bad_request")
        tables[r["name"]] = rows
        notes += n
    return tables, notes


def manual_items(spec, pack):
    from core import generic_rest as gr
    base = pack.endpoints["base_url"].rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        if r.get("endpoint") in OUI:
            items.append({"name": f"{r['name']}#1", "request": r["name"], "url": ep["path"],
                          "filename": ep["path"].rsplit("/", 1)[1], "needs_key": False, "method": "GET", "body": None,
                          "note": "the whole file (ar2112.csv is about 23 MB); the ingest filters it as the spec says"})
            continue
        try:
            ep2, q, _ = prepare(r, ep, params)
        except (ValueError, KeyError):
            continue
        keyed = (r.get("endpoint") or "query") not in KEYLESS
        pg = ep2.get("paging") or {"type": "none"}
        size = int(pg.get("size") or 1000)
        pages = 1
        if pg.get("type") == "offset":
            pages = min(25, max(1, -(-int(r.get("max_rows") or ep2.get("max_rows") or size) // size)))
        for i in range(pages):
            qq = dict(q)
            if pg.get("type") == "offset":
                qq[pg["limit_param"]], qq[pg["offset_param"]] = size, i * size
            url = f"{base}{ep2['path']}" + ("?" + urllib.parse.urlencode(qq) if qq else "")
            if keyed:
                url += ("&" if "?" in url else "?") + "X-API-KEY={API_KEY}"
            items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url,
                          "filename": f"{r['name']}.json" if pages == 1 else f"{r['name']}_{i + 1}.json",
                          "needs_key": keyed, "method": "GET", "body": None,
                          "note": "" if pages == 1 else f"page {i + 1} of up to {pages}; stop when \"data\" comes back empty"})
    return items


def parse_upload(pack, req, paths):
    from core import generic_rest as gr
    ep, params, _ = gr.resolve(pack, req)
    if req.get("endpoint") in OUI:
        p = sorted(paths)[-1]
        with open(p, "rb") as f:
            body = f.read()
        return _tidy_oui(req, ep, params, body, os.path.basename(p))[0]
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            data = f.read()
        if not data.strip():
            continue
        doc = json.loads(data.decode("utf-8-sig"))
        rows += gr.dig(doc, ep.get("records", "data")) or []
    return gr._clean(rows, ep)


# ---------------------------------------------------------------------- OUI unemployment insurance files
OUI_BASE = "https://oui.doleta.gov"


def _usps(x) -> str:
    s = str(x).strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    try:
        import sys
        p = os.path.join(HERE, "..", "..", "scripts", "analysis")
        if p not in sys.path:
            sys.path.insert(0, p)
        from combine import state
        return state(s, "usps")
    except (ImportError, ValueError):
        raise ValueError(f"unknown state {x!r}: use a two-letter code (WY)")


def _states(v) -> set[str]:
    vals = [v] if isinstance(v, str) else list(v or [])
    return {_usps(x) for x in vals if str(x).strip()}


def _bound(v, end: bool) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s + ("-31" if end else "-01")
    if re.fullmatch(r"\d{4}", s):
        return s + ("-12-31" if end else "-01-01")
    raise ValueError(f"{'end' if end else 'start'} {v!r}: use YYYY-MM or YYYY-MM-DD")


def _check_oui(r) -> list[str]:
    p, name, probs = r.get("params") or {}, r.get("name"), []
    if r.get("endpoint") != "ui_trust_fund":
        return probs
    try:
        _states(p.get("state"))
    except ValueError as e:
        probs.append(f"request {name!r}: {e}")
    for k in ("start", "end"):
        try:
            _bound(p.get(k), k == "end")
        except ValueError as e:
            probs.append(f"request {name!r}: {e}")
    if p.get("fields"):
        cols = _eps()["endpoints"]["ui_trust_fund"]["columns"]
        f = p["fields"] if isinstance(p["fields"], list) else str(p["fields"]).split(",")
        bad = [x.strip() for x in f if x.strip() and x.strip() not in cols and x.strip() not in cols.values()
               and x.strip() not in ("st", "rptdate")]
        if bad:
            probs.append(f"request {name!r}: unknown field(s) {', '.join(map(repr, bad))}: ui_trust_fund columns are "
                         "<line item>_<account> names such as balance_close_utf, dep_ui_contributions_total, "
                         "dis_ui_benefits_total, dep_treasury_interest_utf (endpoints.json 'columns' lists all), "
                         "or the file's c-codes")
    return probs


def fetch_oui(pack, r, ep, params, files: dict):
    """One OUI file (downloaded once per fetch, keyless) filtered and renamed as the request says."""
    from core.http import Client, DataError
    url = ep["path"]
    if url not in files:
        L = pack.policy["limits"]
        c = Client(OUI_BASE, name="dol (oui.doleta.gov)", auth={}, rps=L.get("rps") or 2, burst=L.get("burst") or 4,
                   timeout=120, deadline=600)
        try:
            files[url] = c.request("GET", url).body
        except DataError as e:
            raise DataError(f"request {r['name']!r} ({r.get('endpoint')}): {e}", kind=e.kind,
                            status=getattr(e, "status", None)) from None
    rows, notes = _tidy_oui(r, ep, params, files[url], url.rsplit("/", 1)[1])
    if not rows and not r.get("allow_empty"):
        raise DataError(f"request {r['name']!r} returned no rows: check state and the month range (the file runs "
                        "from 1979-01 to the latest month each state has reported)", kind="bad_request")
    return rows, notes


def _tidy_oui(req, ep, params, body: bytes, fname: str):
    from core.http import DataError
    if req.get("endpoint") == "ui_data_summary":
        return _data_summary(req, ep, params, body)
    import csv
    import io
    text = body.decode("utf-8-sig", errors="replace")
    first = text[:200].splitlines()[0] if text.strip() else ""
    if ('"st"' not in first and not first.startswith("st,")) or "rptdate" not in first:
        raise DataError(f"{fname}: expected the ETA 2112 CSV (header st, rptdate, c1, ...), got something else "
                        "(an HTML page?)", kind="api")
    cols = ep["columns"]
    states = _states(params.get("state"))
    lo, hi = _bound(params.get("start"), False), _bound(params.get("end"), True)
    want = None
    if params.get("fields"):
        f = params["fields"] if isinstance(params["fields"], list) else str(params["fields"]).split(",")
        want = {cols.get(x.strip(), x.strip()) for x in f}
    missing = set(ep.get("missing") or [""])
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        st, d = rec.get("st", ""), rec.get("rptdate", "")
        if (states and st not in states) or (lo and d < lo) or (hi and d > hi):
            continue
        out = {"st": st, "rptdate": d}
        for k, v in rec.items():
            if k in ("st", "rptdate"):
                continue
            name = cols.get(k, k)
            if want is None or name in want:
                out[name] = "" if (v is None or v.strip() in missing) else v
        rows.append(out)
    notes = []
    if params.get("latest") in (True, "true", "True", 1, "1"):
        last = {}
        for x in rows:
            last[x["st"]] = max(last.get(x["st"], ""), x["rptdate"])
        rows = [x for x in rows if x["rptdate"] == last[x["st"]]]
        top = max(last.values(), default="")
        lag = sorted(f"{s} {d[:7]}" for s, d in last.items() if d != top)
        if lag:
            notes.append(f"{req['name']}: latest month per state; the newest month in the file is {top[:7]}, these "
                         f"states have not reported it yet: {', '.join(lag)}")
    if rows:
        notes.append(f"{req['name']}: ETA 2112 ({fname}), {len(rows):,} state-months "
                     f"{min(x['rptdate'] for x in rows)[:7]} to {max(x['rptdate'] for x in rows)[:7]}; amounts in "
                     "dollars; the state's trust fund balance at month end is balance_close_utf (c87); "
                     "<item>_total is the sum of the clearing, utf and bpa accounts (column C of the report)")
    return rows, notes


def _summary_col(h: str) -> str:
    s = h.replace("*", "").strip()
    unit = ""
    if "(000)" in s:
        s, unit = s.replace("(000)", ""), "thousands"
    s = s.replace("(%)", " pct").replace("%", " pct ")
    slug = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return "_".join(x for x in (slug, unit) if x)


def _num(v: str) -> str:
    """A number as the workbook stores it ('319139.59999999998', '7.0000000000000007E-2') in its shortest form
    ('319139.6', '0.07'): the same value, without binary float noise. Text is kept as is."""
    if not re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", v or ""):
        return v
    f = float(v)
    return str(int(f)) if f.is_integer() and "e" not in v.lower() and "." not in v else repr(f)


def _data_summary(req, ep, params, body: bytes):
    """SummaryTables.xlsx, one sheet: the quarter from the title row, the header found by its first cell."""
    from core import generic_rest as gr
    from core.http import DataError
    e2 = gr.with_record_params(ep, params)
    rows = gr._clean(gr.sheet_records(e2, body), e2)
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    head = ["" if re.fullmatch(r"col\d+", k) else k for k in keys]
    grid = [head] + [[str(r.get(k, "")).strip() for k in keys] for r in rows]
    cyq = next((c for c in head if re.fullmatch(r"\d{4}\.[1-4]", c)), "")
    li = next((i for i, g in enumerate(grid) if g and g[0] == "State"), None)
    if li is None:
        raise DataError(f"SummaryTables.xlsx sheet {e2.get('sheet')!r}: the layout changed (no 'State' header row); "
                        "open the file and check", kind="api")
    names = ["state"] + [_summary_col(h) for h in grid[li][1:] if h]
    data, foot = [], []
    for g in grid[li + 1:]:
        if not any(v for v in g[1:len(names)]):
            if g and g[0]:
                foot.append(g[0])
            continue
        data.append(dict(zip(names, [g[0]] + [_num(v) for v in g[1:len(names)]])))
    star = next((f.lstrip("*").strip() for f in foot if f.startswith("*")), "")
    quarter_end = ""
    if cyq:
        y, q = cyq.split(".")
        quarter_end = f"{y}-{('03-31', '06-30', '09-30', '12-31')[int(q) - 1]}"
    out = []
    for d in data:
        name = d["state"]
        flagged = name.endswith("*")
        name = name.rstrip("*").strip()
        try:
            code = "US" if name == "United States" else _usps(name.replace("Virgin Islands", "U.S. Virgin Islands")
                                                               if name == "Virgin Islands" else name)
        except ValueError:
            code = ""
        out.append({"cyq": cyq, "quarter_end": quarter_end, "state": name, "st": code, **{k: v for k, v in d.items() if k != "state"},
                    "note": star if flagged else ""})
    notes = [f"{req['name']}: UI Data Summary, sheet {e2.get('sheet')}, calendar-year quarter {cyq}"
             + (": " + " ".join(foot) if foot else "")]
    return out, notes


def search(query, pack):
    from core import generic_rest as gr
    import re
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    scored = []
    for h in gr.search_catalog(pack, query, limit=500):
        h = {k: v for k, v in h.items() if k != "columns"}
        text = " ".join(str(v) for v in h.values()).lower()
        scored.append((sum(1 for w in words if w in text), h))
    out = [h for _, h in sorted(scored, key=lambda x: -x[0])][:20]
    return out

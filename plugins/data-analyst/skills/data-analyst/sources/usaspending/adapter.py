"""USAspending adapter: fiscal-year periods, award type groups, agency and state names, POST paging links.

- check_spec: fiscal years before FY2008 and dates before 2007-10-01 are refused before any request (the
  search API answers HTTP 422 for them); award type codes must come from ONE group (else HTTP 422);
  the awards endpoint needs an award type.
- before_request: the convenience parameters (fiscal_year, start_date/end_date, award_type, keyword,
  state, agency, funding_agency, recipient) become the API's "filters" object; an explicit "filters"
  object is merged over them. Agencies are given by toptier code, catalog alias (hhs, dod, treas) or
  name; states by USPS code, FIPS or name (core.combine.state).
- normalize: values stay as published. over_time rows get `fiscal_period` (FY2024, FY2024 Q1,
  FY2024 M01) and `period_start` (calendar YYYY-MM the period starts; fiscal month 1 = October);
  award rows get `award_url` (the usaspending.gov page).
- manual_items: POST links with page/limit in the JSON body (the bundle's download scripts send them).
Errors are never swallowed: a failing call (HTTP 4xx/5xx, with the API's "detail" text) stops the fetch.
"""
from __future__ import annotations

import copy
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FLOOR = "2007-10-01"
POST_FILTER_KEYS = ("fiscal_year", "start_date", "end_date", "award_type", "keyword", "keywords", "state",
                    "agency", "funding_agency", "recipient")


def _eps() -> dict:
    from core import packfiles
    return packfiles.read_json(HERE, "endpoints.json")


def _state(x, as_="usps"):
    sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts", "analysis"))
    from combine import state
    return state(x, as_)


def groups() -> dict:
    return {c["id"]: c["codes"] for c in _eps()["catalog"].values() if c.get("kind") == "award_type"}


def award_codes(v) -> list[str]:
    g = groups()
    if isinstance(v, str) and v.strip().lower() in g:
        return list(g[v.strip().lower()])
    if isinstance(v, str):
        v = [s.strip() for s in v.split(",") if s.strip()]
    return [str(x) for x in v]


def code_group(codes: list[str]) -> list[str]:
    """Names of the search-API groups (the set the API validates against) the codes fall in."""
    base = {k: set(v) for k, v in groups().items() if k != "insurance"}
    return sorted({g for c in codes for g, s in base.items() if c in s})


def _fy_bounds(fy) -> tuple[str, str]:
    if isinstance(fy, (list, tuple)):
        a, b = int(fy[0]), int(fy[-1])
    else:
        a = b = int(fy)
    return f"{a - 1}-10-01", f"{b}-09-30"


def agency_name(x) -> str:
    cat = _eps()["catalog"]
    s = str(x).strip()
    by_code = {c["id"]: c for c in cat.values() if c.get("kind") == "agency"}
    if s in by_code:
        return by_code[s]["name"]
    if s.lower() in cat and cat[s.lower()].get("kind") == "agency":
        return cat[s.lower()]["name"]
    names = [c["name"] for c in by_code.values()]
    exact = [n for n in names if n.lower() == s.lower()]
    if exact:
        return exact[0]
    part = [n for n in names if s.lower() in n.lower()]
    if len(part) == 1:
        return part[0]
    hint = f" Did you mean: {'; '.join(part[:6])}?" if part else " See fetch.py --source usaspending --search."
    raise ValueError(f"agency {x!r} is not a toptier agency code, alias or unique name.{hint}")


def toptier_code(x) -> str:
    cat = _eps()["catalog"]
    s = str(x).strip()
    if re.fullmatch(r"\d{3,4}", s):
        return s
    if s.lower() in cat and cat[s.lower()].get("kind") == "agency":
        return cat[s.lower()]["id"]
    n = agency_name(s)
    return next(c["id"] for c in cat.values() if c.get("kind") == "agency" and c["name"] == n)


def check_spec(spec) -> list[str]:
    probs = []
    eps = _eps()["endpoints"]
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        ep = eps.get(r.get("endpoint") or "", {})
        name = r.get("name")
        fy = p.get("fiscal_year")
        if fy is not None:
            try:
                start, end = _fy_bounds(fy)
                if start < FLOOR:
                    probs.append(f"request {name!r}: fiscal_year {fy} is before FY2008; the search API starts at "
                                 "2007-10-01 (older data: the bulk download files on usaspending.gov)")
            except (TypeError, ValueError):
                probs.append(f"request {name!r}: fiscal_year must be a year (2024) or [first, last]")
        sd = p.get("start_date")
        if sd and str(sd) < FLOOR:
            probs.append(f"request {name!r}: start_date {sd} is before 2007-10-01, the earliest date the search API takes")
        if p.get("award_type") is not None:
            codes = award_codes(p["award_type"])
            gs = code_group(codes)
            unknown = [c for c in codes if not code_group([c])]
            if unknown:
                probs.append(f"request {name!r}: unknown award type code(s) {unknown}; groups: {', '.join(groups())}")
            elif len(gs) > 1:
                probs.append(f"request {name!r}: award_type mixes groups {gs}; the API takes codes from one group "
                             "per request: split it into one request per group")
        if r.get("endpoint") == "awards" and p.get("award_type") is None and \
                not ((p.get("filters") or {}).get("award_type_codes")):
            probs.append(f"request {name!r}: the awards endpoint needs award_type (contracts, grants, loans, ...)")
        for k in ("agency", "funding_agency"):
            if p.get(k) is not None:
                try:
                    agency_name(p[k])
                except ValueError as e:
                    probs.append(f"request {name!r}: {e}")
        if p.get("state") is not None:
            try:
                _state(p["state"])
            except ValueError as e:
                probs.append(f"request {name!r}: {e}")
        if ep.get("method") == "POST" and r.get("endpoint") != "awards" and not (
                p.get("fiscal_year") or p.get("start_date") or (p.get("filters") or {}).get("time_period")):
            probs.append(f"request {name!r}: give fiscal_year (or start_date/end_date); without it the API "
                         "chooses a default period silently")
    return probs


def before_request(req, ep, params):
    from core.http import DataError
    try:
        return _before(req, ep, params)
    except ValueError as e:
        raise DataError(f"request {req.get('name')!r}: {e}", kind="bad_request")


def _before(req, ep, params):
    params = dict(params)
    if ep.get("method") == "POST":
        f = {}
        if params.get("fiscal_year") is not None:
            s, e = _fy_bounds(params.pop("fiscal_year"))
            f["time_period"] = [{"start_date": s, "end_date": e}]
        if params.get("start_date"):
            f["time_period"] = [{"start_date": params.pop("start_date"),
                                 "end_date": params.pop("end_date", None) or "9999-12-31"}]
            if f["time_period"][0]["end_date"] == "9999-12-31":
                from datetime import date
                f["time_period"][0]["end_date"] = date.today().isoformat()
        params.pop("end_date", None)
        if params.get("award_type") is not None:
            f["award_type_codes"] = award_codes(params.pop("award_type"))
        kw = params.pop("keyword", None) or params.pop("keywords", None)
        if kw:
            f["keywords"] = [kw] if isinstance(kw, str) else list(kw)
        if params.get("state") is not None:
            f["place_of_performance_locations"] = [{"country": "USA", "state": _state(params.pop("state"))}]
        ags = []
        if params.get("agency") is not None:
            ags.append({"type": "awarding", "tier": "toptier", "name": agency_name(params.pop("agency"))})
        if params.get("funding_agency") is not None:
            ags.append({"type": "funding", "tier": "toptier", "name": agency_name(params.pop("funding_agency"))})
        if ags:
            f["agencies"] = ags
        if params.get("recipient"):
            rcp = params.pop("recipient")
            f["recipient_search_text"] = [rcp] if isinstance(rcp, str) else list(rcp)
        f.update(params.pop("filters", None) or {})
        params["filters"] = f
        codes = f.get("award_type_codes") or []
        if req.get("endpoint") == "awards" and "sort" not in (req.get("params") or {}) and \
                code_group(codes) == ["loans"]:
            params["sort"] = "Loan Value"
            params["fields"] = [("Loan Value" if x == "Award Amount" else x) for x in ep["body"]["fields"]]
    else:
        if "toptier_code" in params:
            params["toptier_code"] = toptier_code(params["toptier_code"])
        if "{fips}" in ep.get("path", "") and params.get("state") is not None:
            params["fips"] = _state(params.pop("state"), "fips")
    return params


def _fiscal(fy, quarter, month):
    if not fy:
        return "", ""
    fy = int(fy)
    if month:
        m = int(month)
        cal = (m + 8) % 12 + 1
        year = fy - 1 if m <= 3 else fy
        return f"FY{fy} M{m:02d}", f"{year}-{cal:02d}"
    if quarter:
        q = int(quarter)
        cal = [10, 1, 4, 7][q - 1]
        return f"FY{fy} Q{q}", f"{fy - 1 if q == 1 else fy}-{cal:02d}"
    return f"FY{fy}", f"{fy - 1}-10"


def normalize(req, ep, rows):
    for r in rows:
        if "time_period.fiscal_year" in r:
            r["fiscal_period"], r["period_start"] = _fiscal(r.get("time_period.fiscal_year"),
                                                            r.get("time_period.quarter"), r.get("time_period.month"))
        gid = r.get("generated_internal_id")
        if gid:
            r["award_url"] = f"https://www.usaspending.gov/award/{gid}"
    return rows


def manual_items(spec, pack):
    """One link per page; POST bodies carry page and limit (the bundle's download scripts send them)."""
    from core import generic_rest as gr
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        try:
            params = _before(r, ep, params)
        except ValueError:
            continue
        path, query, body = gr.split(ep, params)
        url = f"{base}/{path.lstrip('/')}"
        if query:
            import urllib.parse
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        pg = ep.get("paging") or {"type": "none"}
        if ep.get("method") == "POST" and pg.get("type") == "page":
            size = int(pg["size"])
            pages = min(25, max(1, math.ceil(int(r.get("max_rows") or size) / size)))
            for i in range(pages):
                b = copy.deepcopy(body or {})
                b[pg["page_param"]], b[pg["size_param"]] = i + 1, size
                items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url,
                              "filename": f"{r['name']}.json" if pages == 1 else f"{r['name']}_{i + 1}.json",
                              "needs_key": False, "method": "POST", "body": b,
                              "note": "" if pages == 1 else f"page {i + 1} of up to {pages}; stop when a page "
                                                           "comes back with an empty results list"})
        else:
            items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url, "filename": f"{r['name']}.json",
                          "needs_key": False, "method": ep.get("method", "GET"), "body": body, "note": ""})
    return items


def search(query, pack):
    """Catalog search (agencies, award type groups) and endpoints, ranked by words matched."""
    from core import generic_rest as gr
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    hits = gr.search_catalog(pack, query, limit=500)
    scored = []
    for h in hits:
        text = " ".join(str(v) for v in h.values()).lower()
        scored.append((sum(1 for w in words if w in text), h))
    out = [h for _, h in sorted(scored, key=lambda x: -x[0])][:20]
    if len(hits) > 20:
        out.append({"note": f"{len(hits) - 20} more results: narrow the words"})
    return out

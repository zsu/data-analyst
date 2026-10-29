"""GSA CALC+ adapter: friendly filter parameters, the CSV export, aggregation tables, uploads.

- before_request: education_level, price_range, worksite, ... become repeated `filter=field:value`
  parameters, with the case each field needs (verified 2026-10: worksite lower case, education_level and
  business_size upper case; a wrong case is silently ignored or matches nothing). suggest_* turn
  `prefix` into `suggest-contains=<field>:<prefix>`.
- fetch: `rates_export` (export=y) answers CSV with a few lines of search and filter values before the
  header; it is parsed here and its columns are renamed to the JSON names (values recoded where the
  export spells them differently: business size, security clearance). An export with no matches is only
  that preamble (no header line): zero rows. Other endpoints use the generic fetch.
- check_spec: the API ignores an unknown filter field (HTTP 200, unfiltered) and answers HTTP 406 with an
  empty body to an unknown search or ordering field and to a page past row 10,000 (verified 2026-10-22),
  so raw `filter` entries must name a field in FILTERS and `search` one of SEARCH_FIELDS.
- normalize: JSON hits lose the `_source.` prefix and the search-engine metadata (_index, _score, sort);
  rate_stats and rate_facets become small tables from the aggregations.
- manual_items / parse_upload: the manual route is one export CSV link per request (all rows, no paging; identical rows once).
"""
from __future__ import annotations

import csv
import io
import json
import urllib.parse

from core import generic_rest as gr
from core.http import DataError

FILTERS = ("education_level", "experience_range", "min_years_experience", "price_range", "current_price",
           "worksite", "business_size", "security_clearance", "sin", "category", "subcategory")
SUGGEST = {"suggest_labor_category": "labor_category", "suggest_vendor_name": "vendor_name",
           "suggest_contract": "idv_piid"}
SEARCH_FIELDS = ("labor_category", "vendor_name", "idv_piid")
PREAMBLE = ("SEARCH VALUES", "FILTER VALUES")
EDU = {"HS", "AA", "BA", "MA", "PHD"}
WORKSITE = {"customer", "contractor", "virtual"}
EXPORT_COLS = {"Contract #": "idv_piid", "Labor Category": "labor_category", "Business Size": "business_size",
               "Schedule": "schedule", "Site": "worksite", "Security Clearance": "security_clearance",
               "Category": "category", "Subcategory": "subcategory", "Begin Date": "contract_start",
               "End Date": "contract_end", "SIN": "sin", "Vendor Name": "vendor_name",
               "Education Level": "education_level", "Minimum Years Experience": "min_years_experience",
               "Current Year Labor Price": "current_price", "Next Year Labor Price": "next_year_price",
               "Second Year Labor Price": "second_year_price"}
RECODE = {"business_size": {"small business": "S", "other than small business": "O"},
          "security_clearance": {"Yes": "True", "No": "False"}}
FACETS = ("education_level", "worksite", "business_size", "security_clearance", "min_years_experience",
          "labor_category")


def _endpoint(req):
    return req.get("endpoint") or ""


def before_request(req, ep, params):
    name = _endpoint(req)
    if name in SUGGEST:
        params["suggest-contains"] = f"{SUGGEST[name]}:{params.pop('prefix')}"
        return params
    filt = params.pop("filter", None) or []
    filt = [filt] if isinstance(filt, str) else list(filt)
    for k in FILTERS:
        v = params.pop(k, None)
        if v in (None, ""):
            continue
        if isinstance(v, (list, tuple)):
            v = ("," if k.endswith("_range") else "|").join(str(x) for x in v)
        v = str(v)
        if k == "worksite":
            v = v.lower().replace("_facility", "")
        elif k in ("education_level", "business_size"):
            v = v.upper()
        elif k == "security_clearance":
            v = {"true": "yes", "false": "no"}.get(v.lower(), v.lower())
        filt.append(f"{k}:{v}")
    if filt:
        params["filter"] = filt
    return params


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, n = r.get("params") or {}, r.get("name")
        edu = p.get("education_level")
        if edu:
            bad = [x for x in str("|".join(edu) if isinstance(edu, list) else edu).upper().split("|") if x not in EDU]
            if bad:
                probs.append(f"request {n!r}: education_level codes are HS, AA, BA, MA, PHD (got {bad})")
        ws = p.get("worksite")
        if ws and str(ws).lower().replace("_facility", "") not in WORKSITE:
            probs.append(f"request {n!r}: worksite is customer, contractor or virtual")
        bs = p.get("business_size")
        if bs and str(bs).upper() not in ("S", "O"):
            probs.append(f"request {n!r}: business_size is S (small) or O (other than small)")
        for k in ("keyword", "prefix"):
            if k in p and len(str(p[k]).strip()) < 2:
                probs.append(f"request {n!r}: {k} needs at least 2 characters")
        s = p.get("search")
        if s and ":" not in str(s):
            probs.append(f"request {n!r}: search is field:value, e.g. labor_category:Data Scientist")
        elif s and str(s).split(":", 1)[0] not in SEARCH_FIELDS:
            probs.append(f"request {n!r}: search field must be one of {', '.join(SEARCH_FIELDS)} (the API answers "
                         f"HTTP 406 to others; got {str(s).split(':', 1)[0]!r})")
        raw = p.get("filter") or []
        for f in [raw] if isinstance(raw, str) else raw:
            field = str(f).split(":", 1)[0]
            if ":" not in str(f) or field not in FILTERS:
                probs.append(f"request {n!r}: filter {str(f)!r} is not field:value with a known field ("
                             f"{', '.join(FILTERS)}); the API ignores unknown filter fields and would return "
                             "every rate. Labor category, vendor and contract go in search.")
    return probs


# ---------------------------------------------------------------------- CSV export
def parse_export(text: str) -> list[dict]:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("Contract #")), None)
    if start is None:
        first = next((l.strip() for l in lines if l.strip()), "")
        if not first or first in PREAMBLE:              # no matches: the export is only the preamble
            return []
        return list(csv.DictReader(io.StringIO(text)))
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        out = {}
        for k, v in r.items():
            col = EXPORT_COLS.get(k, k)
            out[col] = RECODE.get(col, {}).get(v, v)
        rows.append(out)
    return rows


def _fetch_one(client, r, ep, params):
    if _endpoint(r) == "rates_export":
        path, query, _ = gr.split(ep, params)
        return gr._clean(parse_export(client.get(path, query).text()), ep), None
    return gr.fetch_request(client, ep, params, r.get("max_rows"))


def fetch(spec, pack, route):
    client = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        params = before_request(r, ep, params)
        try:
            rows, meta = _fetch_one(client, r, ep, params)
        except DataError as e:
            if e.status != 406:
                raise
            raise DataError(f"request {r['name']!r}: HTTP 406 with an empty body. CALC+ answers 406 to an unsupported "
                            "ordering or search field and to a page past row 10,000 (use rates_export or "
                            "rate_stats for more rows).", kind="bad_request", status=406) from None
        if _endpoint(r) == "rates_export":
            limit = int(r.get("max_rows") or ep.get("max_rows") or 200000)
            short = export_shortfall(client, pack, params, len(rows))
            if short:
                notes.append(f"{r['name']}: {short}")
            if len(rows) > limit:
                notes.append(f"{r['name']}: stopped at {limit:,} of {len(rows):,} rows (max_rows).")
                rows = rows[:limit]
        elif meta["truncated"]:
            capped = (meta["total"] or 0) >= 10000
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows" +
                         (f" of {'10,000 or more' if capped else format(meta['total'], ',')}" if meta["total"] else "") +
                         " (max_rows; the JSON search returns at most 10,000). Use rates_export for every distinct row, "
                         "or rate_stats for summary statistics over every match.")
        rows = normalize(r, ep, rows)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check the search and filter values "
                            "(search is exact and case-sensitive; see source.md)", kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def export_shortfall(client, pack, params, n: int) -> str | None:
    """The export writes one line per distinct row of its 17 columns: rates identical in all of them (same
    contract, category and prices, different JSON `id`) collapse, so it can hold fewer rows than the search
    matches ("data scientist": 1,504 lines for 1,516 matches, verified 2026-10-28). One count request
    (rate_stats) says by how many."""
    if not n:
        return None
    ep, _, _ = gr.resolve(pack, {"name": "count", "endpoint": "rate_stats"})
    q = {**(ep.get("defaults") or {}), **{k: v for k, v in params.items() if k != "export"}}
    try:
        path, query, _ = gr.split(ep, q)
        total = ((client.get(path, query).json().get("aggregations") or {}).get("wage_stats") or {}).get("count")
    except (DataError, ValueError, AttributeError):
        return None
    if not total or int(total) <= n:
        return None
    return (f"the export has {n:,} rows but the search matches {int(total):,} rates: the export writes rates that "
            f"are identical in every exported column once ({int(total) - n:,} fewer). Count rates with rate_stats, or "
            "use rates (JSON, one row per rate with its id, at most 10,000) when every rate must be a row.")


# ---------------------------------------------------------------------- aggregations and hits
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def normalize(req, ep, rows):
    name = _endpoint(req)
    if name == "rate_stats" and rows:
        a = rows[0]
        if "wage_stats.count" not in a:
            return rows
        out = {"count": a.get("wage_stats.count"), "min": a.get("wage_stats.min"), "max": a.get("wage_stats.max"),
               "mean": a.get("wage_stats.avg"), "std_dev_sample": a.get("wage_stats.std_deviation_sampling"),
               "median": a.get("median_price.values.50.0")}
        for p in ("10.0", "25.0", "50.0", "75.0", "90.0"):
            out[f"p{p.split('.')[0]}"] = a.get(f"histogram_percentiles.values.{p}")
        return [out]
    if name == "rate_facets" and rows:
        a, out = rows[0], []
        for f in FACETS:
            raw = a.get(f"{f}.buckets")
            try:
                buckets = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except ValueError:
                buckets = []
            for b in buckets:
                out.append({"facet": f, "key": b.get("key_as_string", b.get("key")), "doc_count": b.get("doc_count")})
        return out or rows
    clean = []
    for r in rows:
        if any(k.startswith("_source.") for k in r):
            r = {k[len("_source."):] if k.startswith("_source.") else k: v for k, v in r.items()
                 if k not in ("_index", "_score", "sort", "_id")}
        clean.append(r)
    return clean


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    base = pack.endpoints["base_url"].rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        name = _endpoint(r)
        params = before_request(r, ep, dict(params))
        if name in SUGGEST or name in ("rate_stats", "rate_facets"):
            path, query, _ = gr.split(ep, params)
            fn, note = f"{r['name']}.json", ""
        else:
            for k in ("page", "page_size"):
                params.pop(k, None)
            params["export"] = "y"
            path, query, _ = gr.split(ep, params)
            fn, note = f"{r['name']}.csv", "CSV export of every matching rate, identical rates once (the browser saves it as ceilingrates_results.csv: rename it)"
        url = f"{base}/{path.lstrip('/')}?" + urllib.parse.urlencode(query, doseq=True)
        items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url, "filename": fn,
                      "needs_key": False, "method": "GET", "body": None, "note": note})
    return items


def parse_upload(pack, req, paths):
    ep, _, _ = gr.resolve(pack, req)
    rows = []
    for p in paths:
        with open(p, "rb") as f:
            data = f.read()
        if p.lower().endswith(".csv") or data[:1] not in (b"{", b"["):
            rows += gr._clean(parse_export(data.decode("utf-8-sig", errors="replace")), ep)
        else:
            rows += gr.parse_upload(pack, req, [p])          # runs normalize
    return rows

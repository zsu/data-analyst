"""CFPB Consumer Complaint Database adapter (search API v1, keyless).

Only two resources of the API answer (verified 2026-10-20): the search ("/") and one complaint
("/<id>"). The /trends, /geo/states and /_suggest_company resources now return the
web page (HTML), so this pack builds:
  complaints    search hits, paged with search_after (the "frm" offset is ignored by the API)
  aggregations  the search with size=0: counts by one field (the API returns every field's buckets)
  trends        one size=0 count query per year / quarter / month (both date bounds are inclusive)
  complaint     one complaint by id
A company filter that matches nothing is an error with a hint, never a silent switch to a fuzzy search.
The API ignores filters it does not know (HTTP 200, unfiltered totals; verified 2026-10-22 for sub_product,
sub_issue, has_narrative, consumer_disputed), so check_spec rejects every undeclared parameter, and
sub_product / sub_issue are sent the only way the API applies them: product='<product>•<sub_product>'
and issue='<issue>•<sub_issue>' (U+2022 bullet).
"""
from __future__ import annotations

import calendar
import json
import os
import sys
import urllib.parse
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

CONTROL = {"agg", "sub", "interval", "by", "page_size", "size", "complaint_id"}
AGGS = ("product", "issue", "company", "state", "timely", "company_response", "submitted_via",
        "company_public_response", "tags")
MAX_PERIODS = 120
BULLET = "•"
NESTED = (("product", "sub_product"), ("issue", "sub_issue"))
RETIRED = {"has_narrative": "CFPB stopped publishing complaint narratives on 2026-08-14 and the API ignores "
                            "has_narrative (see source.md for the narratives archive)",
           "consumer_disputed": "consumer_disputed was retired in 2017 and the API ignores it as a filter"}


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _fold(params: dict) -> dict:
    """sub_product / sub_issue folded into product / issue as 'parent<U+2022>child' (check_spec has made
    sure there is exactly one parent); a list of children stays an OR."""
    p = dict(params)
    for parent, child in NESTED:
        subs = p.pop(child, None)
        if subs in (None, "", []):
            continue
        top = _as_list(p.get(parent))[0]
        p[parent] = [f"{top}{BULLET}{s}" for s in _as_list(subs)]
    return p


def _q(params: dict) -> dict:
    """Filter parameters for the query string (booleans as true/false, lists repeated)."""
    q = {}
    for k, v in _fold(params).items():
        if k in CONTROL or v in (None, "", []):
            continue
        q[k] = ("true" if v else "false") if isinstance(v, bool) else v
    return q


def _total(data) -> int:
    t = ((data or {}).get("hits") or {}).get("total")
    return int(t.get("value", 0) if isinstance(t, dict) else t or 0)


def _hit_rows(data) -> list[dict]:
    return [gr.flatten(h.get("_source") or {}) for h in ((data or {}).get("hits") or {}).get("hits") or []]


def _buckets(data, field: str, sub: bool) -> list[dict]:
    a = ((data or {}).get("aggregations") or {}).get(field) or {}
    inner = a.get(field) or {}
    total = _total(data)
    rows = []
    for b in inner.get("buckets") or []:
        if not sub:
            rows.append({"field": field, "key": b.get("key"), "complaints": b.get("doc_count"), "total_matching": total})
            continue
        subs = [(k, v) for k, v in b.items() if isinstance(v, dict) and "buckets" in v]
        if not subs:
            rows.append({"field": field, "key": b.get("key"), "complaints": b.get("doc_count"), "sub_field": "",
                         "sub_key": "", "sub_complaints": "", "total_matching": total})
        for sk, sv in subs:
            for s in sv.get("buckets") or []:
                rows.append({"field": field, "key": b.get("key"), "complaints": b.get("doc_count"),
                             "sub_field": sk.replace(".raw", ""), "sub_key": s.get("key"),
                             "sub_complaints": s.get("doc_count"), "total_matching": total})
    if inner.get("sum_other_doc_count"):
        rows.append({"field": field, "key": "(other buckets not returned)", "complaints": inner["sum_other_doc_count"],
                     "total_matching": total})
    return rows


def periods(start: str, end: str, interval: str) -> list[tuple[str, str, str]]:
    """(label, first day, last day) for every period touching [start, end], clipped to the range."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    y, m = s.year, s.month
    step = {"year": 12, "quarter": 3, "month": 1}[interval]
    if interval == "year":
        m = 1
    elif interval == "quarter":
        m = (m - 1) // 3 * 3 + 1
    while date(y, m, 1) <= e:
        m2, y2 = m + step - 1, y
        while m2 > 12:
            m2, y2 = m2 - 12, y2 + 1
        first, last = date(y, m, 1), date(y2, m2, calendar.monthrange(y2, m2)[1])
        label = str(y) if interval == "year" else f"{y}-Q{(m - 1) // 3 + 1}" if interval == "quarter" else f"{y}-{m:02d}"
        out.append((label, max(first, s).isoformat(), min(last, e).isoformat()))
        m += step
        while m > 12:
            m, y = m - 12, y + 1
    return out


def _param_problems(r) -> list[str]:
    """Undeclared parameters (the API would ignore them silently) and sub-filters without one parent."""
    eps = (packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json") or {}).get("endpoints", {})
    ep, p, n, probs = eps.get(r.get("endpoint") or ""), r.get("params") or {}, r.get("name"), []
    if ep is None:
        return probs                                   # the generic check reports the unknown endpoint
    known = set(ep.get("params") or {}) | CONTROL | set(ep.get("defaults") or {})
    for k in p:
        if k in RETIRED:
            probs.append(f"request {n!r}: {RETIRED[k]}; remove it")
        elif k not in known:
            probs.append(f"request {n!r}: unknown parameter {k!r} (the API ignores unknown filters and would return "
                         f"unfiltered counts); known: {', '.join(sorted(ep.get('params') or {}))}")
    for parent, child in NESTED:
        if p.get(child) in (None, "", []):
            continue
        tops = [x for x in _as_list(p.get(parent)) if x not in (None, "")]
        if len(tops) != 1 or any(BULLET in str(x) for x in tops):
            probs.append(f"request {n!r}: {child} needs exactly one {parent} (the API filters a {child} only as "
                         f"{parent}='<{parent}>' + U+2022 bullet + '<{child}>'); for several {parent} values use separate "
                         "requests or write the bullet form in the list yourself")
    return probs


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, n = r.get("params") or {}, r.get("name")
        probs += _param_problems(r)
        if r.get("endpoint") == "trends":
            a, b = p.get("date_received_min"), p.get("date_received_max")
            if not (a and b):
                probs.append(f"request {n!r}: trends needs date_received_min and date_received_max (YYYY-MM-DD)")
                continue
            try:
                k = len(periods(a, b, p.get("interval") or "month"))
            except (ValueError, KeyError):
                probs.append(f"request {n!r}: dates must be YYYY-MM-DD and interval year, quarter or month")
                continue
            if k > MAX_PERIODS:
                probs.append(f"request {n!r}: {k} periods; at most {MAX_PERIODS} (use a longer interval)")
        if r.get("endpoint") == "complaints" and int(p.get("page_size") or 1000) > 10000:
            probs.append(f"request {n!r}: page_size is at most 10000")
    return probs


def _complaints(c, params, max_rows):
    limit = int(max_rows or 10000)
    size = min(int(params.get("page_size") or 1000), 10000)
    q, rows, after, total = _q(params), [], None, None
    while len(rows) < limit:
        page = {**q, "size": min(size, limit - len(rows)), "no_aggs": "true"}
        if after:
            page["search_after"] = after
        data = c.get_json("/", page)
        total = _total(data)
        hits = ((data.get("hits") or {}).get("hits")) or []
        rows += _hit_rows(data)
        if len(hits) < page["size"]:
            break
        last = hits[-1].get("sort") or []
        if len(last) < 2:
            break
        after = f"{last[0]}_{last[1]}"
    return rows[:limit], total


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        probs += _param_problems(r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        kind, name = r.get("endpoint"), r["name"]
        if kind == "complaints":
            rows, total = _complaints(c, params, r.get("max_rows"))
            if total and total > len(rows):
                notes.append(f"{name}: {len(rows):,} of {total:,} matching complaints (max_rows); use aggregations or "
                             "trends for counts, or narrow the filters.")
        elif kind == "aggregations":
            data = c.get_json("/", {**_q(params), "size": 0})
            rows = _buckets(data, params["agg"], str(params.get("sub")).lower() == "true")
        elif kind == "trends":
            rows = []
            by = params.get("by")
            for label, a, b in periods(params["date_received_min"], params["date_received_max"], params["interval"]):
                q = {**_q(params), "date_received_min": a, "date_received_max": b, "size": 0}
                if not by:
                    q["no_aggs"] = "true"
                data = c.get_json("/", q)
                if by:
                    for x in _buckets(data, by, False):
                        rows.append({"period": label, "period_start": a, "period_end": b, by: x["key"],
                                     "complaints": x["complaints"], "period_total": x["total_matching"]})
                else:
                    rows.append({"period": label, "period_start": a, "period_end": b, "complaints": _total(data)})
            if rows and rows[-1]["period_end"] >= date.today().isoformat():
                notes.append(f"{name}: the last period is incomplete (it runs to today or later).")
        elif kind == "complaint":
            rows = _hit_rows(c.get_json(f"/{params['complaint_id']}"))
        else:
            raise DataError(f"unknown endpoint {kind!r}", kind="bad_request")
        if not rows and not r.get("allow_empty"):
            hint = (" The company filter needs CFPB's exact name (e.g. 'WELLS FARGO & COMPANY'): run an aggregations "
                    "request with agg=company and search_term=<name> to find it." if params.get("company") else "")
            raise DataError(f"request {name!r} returned no complaints: check the filter values (exact, case-sensitive "
                            f"names; see source.md).{hint}", kind="bad_request")
        tables[name] = [{k: ("" if v is None else v) for k, v in row.items()} for row in rows]
    return tables, notes


# ---------------------------------------------------------------------- manual route
def _url(pack, path: str, q: dict) -> str:
    base = (pack.endpoints or {})["base_url"].rstrip("/")
    return f"{base}{path}?" + urllib.parse.urlencode(q, doseq=True)


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        kind, name = r.get("endpoint"), r["name"]
        if kind == "complaints":
            q = {**_q(params), "size": min(int(r.get("max_rows") or 10000), 10000), "format": "csv", "no_aggs": "true"}
            items.append({"name": f"{name}#1", "request": name, "url": _url(pack, "/", q), "filename": f"{name}.csv",
                          "needs_key": False, "note": "up to 10,000 complaints; the database web page "
                          "(consumerfinance.gov/data-research/consumer-complaints/search/) exports more"})
        elif kind == "aggregations":
            items.append({"name": f"{name}#1", "request": name, "url": _url(pack, "/", {**_q(params), "size": 0}),
                          "filename": f"{name}.json", "needs_key": False, "note": ""})
        elif kind == "trends":
            for i, (label, a, b) in enumerate(periods(params["date_received_min"], params["date_received_max"],
                                                       params["interval"])):
                q = {**_q(params), "date_received_min": a, "date_received_max": b, "size": 0}
                items.append({"name": f"{name}#{i + 1}", "request": name, "url": _url(pack, "/", q),
                              "filename": f"{name}_{label}.json", "needs_key": False, "note": f"period {label}"})
        elif kind == "complaint":
            items.append({"name": f"{name}#1", "request": name, "url": _url(pack, f"/{params['complaint_id']}", {}).rstrip("?"),
                          "filename": f"{name}.json", "needs_key": False, "note": ""})
    return items


def parse_upload(pack, req, paths):
    ep, params, _ = gr.resolve(pack, req)
    kind = req.get("endpoint")
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            raw = f.read().decode("utf-8-sig", "replace")
        if p.lower().endswith(".csv"):
            import csv
            import io
            rows += list(csv.DictReader(io.StringIO(raw)))
            continue
        data = json.loads(raw)
        if kind == "aggregations":
            rows += _buckets(data, params["agg"], str(params.get("sub")).lower() == "true")
        elif kind == "trends":
            label = os.path.splitext(os.path.basename(p))[0].rsplit("_", 1)[-1]
            rows.append({"period": label, "complaints": _total(data)})
        else:
            rows += _hit_rows(data)
    return [{k: ("" if v is None else v) for k, v in r.items()} for r in rows]

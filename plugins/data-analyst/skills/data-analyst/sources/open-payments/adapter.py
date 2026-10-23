"""Open Payments adapter (DKAN datastore on openpaymentsdata.cms.gov).

- Queries go to /api/1/datastore/query/{dataset_id}/0 (dataset id + distribution index; each dataset has
  one CSV distribution, checked 2026-10-20). Hard-coded ids stop existing in the
  metastore when CMS republishes; the pack names datasets by alias or id instead.
- limit is capped at 500 by this API (HTTP 400 above it, verified).
- Every column is stored as text (schema mysql_type "text", verified for amounts, dates and program_year),
  so sort and < > BETWEEN compare characters: sorting by total_amount_of_payment_usdollars descending gives
  '995.27', '99.99', ... and "> 900" matches '91.06'. check_spec refuses comparisons against numbers or
  MM/DD/YYYY dates unless the condition sets text_compare_ok; rank and sum in analysis.py.
- The payment files are large (millions of rows a year): a filter is required on them (check_spec).

Friendly spec parameters (expanded by before_request; manual links get the same expansion):
  where {"recipient_state": "VT"} (list value -> IN), conditions [{property, value, operator}],
  sort "col" | "-col" | [..], columns ["a", "b"].
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

OPERATORS = {"=", "<>", "<", "<=", ">", ">=", "LIKE", "IN", "NOT IN", "BETWEEN", "CONTAINS", "STARTS WITH"}
ORDERED = {"<", "<=", ">", ">=", "BETWEEN"}
TEXT_TRAP = re.compile(r"^\s*(-?\d+(\.\d+)?|\d{1,2}/\d{1,2}/\d{4})\s*$")
BIG = re.compile(r"^(general|research|ownership)_\d{4}$")
TIMEOUT = 120


def _catalog() -> dict:
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("catalog", {})


def _list(v) -> list[str]:
    if v in (None, ""):
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def before_request(req, ep, params):
    p = dict(params)
    where = p.pop("where", None) or {}
    conds = [{"property": k, "value": v, "operator": "IN" if isinstance(v, list) else "="} for k, v in where.items()]
    conds += p.pop("conditions", None) or []
    for i, c in enumerate(conds):
        p[f"conditions[{i}][property]"] = c["property"]
        op = str(c.get("operator") or "=").upper()
        if isinstance(c.get("value"), list):
            for j, v in enumerate(c["value"]):
                p[f"conditions[{i}][value][{j}]"] = v
        else:
            p[f"conditions[{i}][value]"] = c.get("value")
        if op != "=":
            p[f"conditions[{i}][operator]"] = op.lower() if op in ("IN", "NOT IN", "LIKE", "BETWEEN") else op
    for i, s in enumerate(_list(p.pop("sort", None))):
        p[f"sorts[{i}][property]"] = s.lstrip("-")
        p[f"sorts[{i}][order]"] = "desc" if s.startswith("-") else "asc"
    cols = p.pop("columns", None) or []
    for i, c in enumerate([c.strip() for c in cols.split(",")] if isinstance(cols, str) else cols):
        p[f"properties[{i}]"] = c
    return p


def check_spec(spec):
    probs = []
    cat = _catalog()
    for r in spec.get("requests") or []:
        prm = r.get("params") or {}
        name = r.get("name")
        if "where" in prm and not isinstance(prm["where"], dict):
            probs.append(f"request {name!r}: where must be an object {{column: value}}")
        conds = prm.get("conditions")
        if conds is not None and (not isinstance(conds, list) or
                                  not all(isinstance(c, dict) and "property" in c for c in conds)):
            probs.append(f"request {name!r}: conditions must be a list of {{property, value, operator}}")
            conds = None
        for c in conds or []:
            op = str(c.get("operator") or "=").upper()
            vals = c.get("value") if isinstance(c.get("value"), list) else [c.get("value")]
            if op not in OPERATORS:
                probs.append(f"request {name!r}: operator {c.get('operator')!r} is not one of {sorted(OPERATORS)}")
            elif op in ORDERED and not c.get("text_compare_ok") and any(TEXT_TRAP.match(str(v)) for v in vals):
                probs.append(f"request {name!r}: {c['property']} {op} {c.get('value')!r} would compare TEXT, not "
                             "numbers or dates (every Open Payments column is stored as text: '91.06' > '900'). "
                             "Filter with equality (program_year, recipient_state, NPI) and compare in analysis.py, "
                             "or set \"text_compare_ok\": true on the condition.")
        alias = prm.get("dataset", r.get("dataset"))
        if isinstance(alias, str) and BIG.match(alias) and not prm.get("where") and not conds:
            probs.append(f"request {name!r}: {alias} has millions of rows; add a where or conditions filter "
                         "(recipient_state, covered_recipient_npi, applicable_manufacturer_or_applicable_gpo_making_payment_name), "
                         "or use a summary table (state_totals, by_reporting_entity)")
        if isinstance(alias, str) and alias not in cat and not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", alias):
            probs.append(f"request {name!r}: dataset {alias!r} is neither a catalog alias nor a dataset UUID "
                         "(see --endpoints or --search)")
    return probs


def fetch(spec, pack, route):
    return gr.fetch(pack, spec, gr.client_for(pack, timeout=TIMEOUT))


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)          # core applies before_request to the links
    for it in items:
        it["note"] = (it["note"].rstrip(".") + ". " if it.get("note") else "") + ("JSON page; or use the dataset page's Download button "
                                              "(https://openpaymentsdata.cms.gov/dataset/<id>; payment files are "
                                              "several GB) and upload only the rows needed.")
    return items


def search(query, pack):
    """Live search of the Open Payments metastore (74 datasets), plus the catalog."""
    hits = gr.search_catalog(pack, query)
    words = [w for w in query.lower().split() if len(w) > 1]
    try:
        items = gr.client_for(pack, timeout=60).get_json("/api/1/metastore/schemas/dataset/items")
    except DataError as e:
        return hits + [{"note": f"live metastore search unavailable ({e.kind}); catalog results only"}]
    known = {h.get("id") for h in hits}
    live = []
    for it in items if isinstance(items, list) else []:
        text = f"{it.get('title', '')} {it.get('description', '')} {' '.join(map(str, it.get('keyword') or []))}".lower()
        s = sum(text.count(w) for w in words)
        if s and it.get("identifier") not in known:
            live.append((s, {"id": it.get("identifier"), "name": it.get("title"), "endpoint": "query",
                             "modified": it.get("modified"), "temporal": it.get("temporal"),
                             "page": f"https://openpaymentsdata.cms.gov/dataset/{it.get('identifier')}"}))
    out = (hits + [h for _, h in sorted(live, key=lambda x: -x[0])])[:20]
    if len(hits) + len(live) > 20:
        out.append({"note": "more than 20 matches; narrow the words"})
    return out

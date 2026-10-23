"""CMS adapter: two APIs on data.cms.gov.

provider  Provider Data Catalog (DKAN): /provider-data/api/1/datastore/query/{dataset_id}/{index}. Calling
          /datastore/query/{distribution_id}/0 answers 404 ("No resource found for dataset <uuid> at index 0"). The dataset id plus the distribution index is
          the documented form; index 0 is the dataset's CSV (each of the 237 datasets has exactly one
          distribution, text/csv, checked 2026-10-20). limit is capped at 1500 by the API; 500 a page is used
          because large pages take 30-50 s.
medicare  data-api: /data-api/v1/dataset/{uuid}/data with size/offset and filter[COLUMN]=value. An unknown
          filter column is silently ignored by the API (the whole table comes back) and an unknown column= name
          is silently dropped, so filter columns are always requested and normalize() fails the request when a
          filter or requested column is not in the returned rows. Suppressed cells arrive as the text "*";
          normalize() empties them and adds a <column>_flag column ("suppressed (*)").

Friendly spec parameters (expanded by before_request; manual links get the same expansion):
  where      {"state": "VT"}            provider: conditions[i][property|value]; a list value -> IN
                                        medicare: filter[STATE]=VT
  conditions [{"property", "value", "operator"}]   provider only (=, <>, <, <=, >, >=, LIKE, IN, NOT IN, BETWEEN)
  sort       "col" | "-col" | [..]      provider: sorts[i][property|order]; medicare: sort=col / -col
  columns    ["a", "b"]                 provider: properties[i]; medicare: column=a,b
Provider columns are stored as text, so < > BETWEEN on numbers or MM/DD/YYYY dates compare characters
('91.06' > '900' is true, verified); check_spec refuses such conditions unless text_compare_ok is set.
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
# numbers and US dates (MM/DD/YYYY) stored as text compare character by character
TEXT_TRAP = re.compile(r"^\s*(-?\d+(\.\d+)?|\d{1,2}/\d{1,2}/\d{4})\s*$")
TIMEOUT = 120                      # provider-data queries took up to 50 s when tested
SUPPRESSED = "*"                   # data-api text for a suppressed cell (small counts), verified 2026-10-22


def _kind(ep: dict) -> str:
    return "provider" if "/datastore/query/" in ep.get("path", "") else "medicare"


def _sorts(v) -> list[str]:
    if v in (None, ""):
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def before_request(req, ep, params):
    p = dict(params)
    where = p.pop("where", None) or {}
    conds = p.pop("conditions", None) or []
    sort = _sorts(p.pop("sort", None))
    cols = p.pop("columns", None) or []
    if isinstance(cols, str):
        cols = [c.strip() for c in cols.split(",") if c.strip()]
    if _kind(ep) == "provider":
        allc = [{"property": k, "value": v, "operator": "IN" if isinstance(v, list) else "="} for k, v in where.items()]
        allc += conds
        for i, c in enumerate(allc):
            p[f"conditions[{i}][property]"] = c["property"]
            op = str(c.get("operator") or "=").upper()
            if isinstance(c.get("value"), list):
                for j, v in enumerate(c["value"]):
                    p[f"conditions[{i}][value][{j}]"] = v
            else:
                p[f"conditions[{i}][value]"] = c.get("value")
            if op != "=":
                p[f"conditions[{i}][operator]"] = op.lower() if op in ("IN", "NOT IN", "LIKE", "BETWEEN") else op
        for i, s in enumerate(sort):
            p[f"sorts[{i}][property]"] = s.lstrip("-")
            p[f"sorts[{i}][order]"] = "desc" if s.startswith("-") else "asc"
        for i, c in enumerate(cols):
            p[f"properties[{i}]"] = c
    else:
        for k, v in where.items():
            p[f"filter[{k}]"] = v
        if sort:
            p["sort"] = sort[0]
        if cols:
            p["column"] = ",".join(list(cols) + [k for k in where if k not in cols])
    return p


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        prm = r.get("params") or {}
        name = r.get("name")
        if "where" in prm and not isinstance(prm["where"], dict):
            probs.append(f"request {name!r}: where must be an object {{column: value}}")
        conds = prm.get("conditions")
        if conds is not None:
            if not isinstance(conds, list) or not all(isinstance(c, dict) and "property" in c for c in conds):
                probs.append(f"request {name!r}: conditions must be a list of {{property, value, operator}}")
            else:
                for c in conds:
                    op = str(c.get("operator") or "=").upper()
                    if op not in OPERATORS:
                        probs.append(f"request {name!r}: operator {c.get('operator')!r} is not one of {sorted(OPERATORS)}")
                    elif op in ORDERED and not c.get("text_compare_ok") and any(
                            TEXT_TRAP.match(str(v)) for v in (c.get("value") if isinstance(c.get("value"), list) else [c.get("value")])):
                        probs.append(f"request {name!r}: {c['property']} {op} {c.get('value')!r} would compare TEXT, not "
                                     "numbers or dates (the datastore stores these columns as text: '91.06' > '900'). "
                                     "Fetch with equality filters and compare in analysis.py, or set "
                                     "\"text_compare_ok\": true on the condition if a text comparison is really meant.")
            if r.get("endpoint") == "medicare":
                probs.append(f"request {name!r}: the medicare endpoint takes where (equality) and keyword, not conditions")
        alias = prm.get("dataset", r.get("dataset"))
        cat = _catalog()
        if alias in cat and r.get("endpoint") and cat[alias]["endpoint"] != r["endpoint"]:
            probs.append(f"request {name!r}: dataset {alias!r} belongs to endpoint {cat[alias]['endpoint']!r}")
    return probs


def normalize(req, ep, rows):
    if _kind(ep) == "medicare" and rows:
        prm = req.get("params") or {}
        where = prm.get("where") or {}
        cols = prm.get("columns") or []
        cols = [c.strip() for c in cols.split(",")] if isinstance(cols, str) else cols
        missing = [k for k in list(where) + list(cols) if k not in rows[0]]
        if missing:
            raise DataError(f"request {req.get('name')!r}: column(s) {missing} are not in this dataset, and the "
                            "data-api ignores unknown filter and column names (filters are dropped, so rows come back "
                            "unfiltered). Column names are "
                            f"case-sensitive; the returned rows have: {', '.join(list(rows[0])[:25])}", kind="bad_request")
        _flag_suppressed(rows)
    return rows


def _flag_suppressed(rows: list[dict]) -> None:
    """data-api suppressed cells are the text "*" (monthly enrollment, geographic variation): make them empty and
    say so in a <column>_flag column. Columns that are themselves flags (Part D prescribers' GE65_Sprsn_Flag holds
    "*" or "#" next to blank values) keep their published text."""
    for r in rows:
        for c, v in list(r.items()):
            if isinstance(v, str) and v.strip() == SUPPRESSED and "flag" not in c.lower():
                r[c] = ""
                r[f"{c}_flag"] = "suppressed (*)"
    flagged = {k for r in rows for k in r if k.endswith("_flag") and k[:-5] in r}
    for r in rows:
        for k in flagged:
            r.setdefault(k, "")


def fetch(spec, pack, route):
    client = gr.client_for(pack, timeout=TIMEOUT)
    return gr.fetch(pack, spec, client)


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)          # core applies before_request to the links
    for it in items:
        it["note"] = (it["note"].rstrip(".") + ". " if it.get("note") else "") + ("JSON page; or open the dataset page and use its Download button "
                                              "(full CSV, display-style column names).")
    return items


def _catalog() -> dict:
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("catalog", {})


def search(query, pack):
    """Live search of the Provider Data Catalog (237 datasets), plus the pack's catalog (Medicare data-api)."""
    hits = gr.search_catalog(pack, query)
    words = [w for w in query.lower().split() if len(w) > 1]
    try:
        items = gr.client_for(pack, timeout=60).get_json("/provider-data/api/1/metastore/schemas/dataset/items")
    except DataError as e:
        return hits + [{"note": f"live Provider Data search unavailable ({e.kind}); catalog results only"}]
    live = []
    known = {h.get("id") for h in hits}
    for it in items if isinstance(items, list) else []:
        kw = " ".join(k.get("data", "") if isinstance(k, dict) else str(k) for k in it.get("keyword") or [])
        theme = " ".join(t.get("data", "") if isinstance(t, dict) else str(t) for t in it.get("theme") or [])
        text = f"{it.get('title', '')} {it.get('description', '')} {kw} {theme}".lower()
        s = sum(text.count(w) for w in words)
        if s and it.get("identifier") not in known:
            live.append((s, {"id": it.get("identifier"), "name": it.get("title"), "endpoint": "provider",
                             "modified": it.get("modified"), "theme": theme,
                             "page": f"https://data.cms.gov/provider-data/dataset/{it.get('identifier')}"}))
    live = [h for _, h in sorted(live, key=lambda x: -x[0])]
    out = (hits + live)[:20]
    if len(hits) + len(live) > 20:
        out.append({"note": "more than 20 matches; narrow the words"})
    return out

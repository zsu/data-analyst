"""College Scorecard adapter.

- before_request: short aliases (name, state, ownership, predominant_degree) become the API's dotted
  field filters; `fields` may be a list and may use catalog aliases (earnings_10yr); without `fields`
  the popular set is sent (the API would otherwise return about 3,000 fields per school).
- normalize: adds label columns for the coded ownership and degree fields (the codes stay).
- manual_items: links carry api_key=DEMO_KEY, because api.data.gov refuses a request without a key.
"""
from __future__ import annotations

import urllib.parse

from core import generic_rest as gr
from core import packfiles

POPULAR = ["id", "school.name", "school.state", "school.city", "school.ownership", "school.degrees_awarded.predominant",
           "latest.admissions.admission_rate.overall", "latest.student.size", "latest.cost.tuition.in_state",
           "latest.cost.tuition.out_of_state", "latest.cost.avg_net_price.overall",
           "latest.aid.median_debt.completers.overall", "latest.aid.pell_grant_rate",
           "latest.completion.rate_suppressed.overall", "latest.earnings.10_yrs_after_entry.median",
           "latest.earnings.6_yrs_after_entry.median"]
ALIASES = {"name": "school.name", "state": "school.state", "ownership": "school.ownership",
           "predominant_degree": "school.degrees_awarded.predominant"}
OWNERSHIP = {"1": "Public", "2": "Private nonprofit", "3": "Private for-profit"}
DEGREE = {"0": "Not classified", "1": "Certificate", "2": "Associate", "3": "Bachelor's", "4": "Graduate"}
LABELS = {"school.ownership": OWNERSHIP, "school.degrees_awarded.predominant": DEGREE,
          "school.degrees_awarded.highest": DEGREE}


def _catalog():
    import os
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("catalog", {})


def _fields(v) -> str:
    cat = _catalog()
    items = v if isinstance(v, list) else str(v).split(",")
    out = []
    for x in (str(i).strip() for i in items):
        if x:
            out.append(cat[x]["id"] if x in cat else x)
    if "id" not in out:
        out.insert(0, "id")
    return ",".join(dict.fromkeys(out))


def _sort(v) -> str:
    """sort=<field>[:asc|desc][,...] with the same short names as filters and fields (name -> school.name, catalog
    aliases): the API answers HTTP 400 to a short name inside sort."""
    cat = _catalog()
    out = []
    for part in (x.strip() for x in str(v).split(",")):
        if part:
            f, sep, d = part.partition(":")
            f = ALIASES.get(f.strip(), cat[f.strip()]["id"] if f.strip() in cat else f.strip())
            out.append(f + sep + d.strip())
    return ",".join(out)


def before_request(req, ep, params):
    p = dict(params)
    for short, full in ALIASES.items():
        if short in p:
            v = p.pop(short)
            p.setdefault(full, v)
    p["fields"] = _fields(p.get("fields") or POPULAR)
    if p.get("sort"):
        p["sort"] = _sort(p["sort"])
    p.pop("_fields", None)
    for k, v in list(p.items()):
        if isinstance(v, list):
            p[k] = ",".join(str(x) for x in v)
    return p


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        if "per_page" in p or "page" in p:
            probs.append(f"request {r.get('name')!r}: do not set page/per_page (the pack pages, 100 a page); "
                         "use max_rows to limit the rows")
        f = p.get("fields")
        f = ",".join(f) if isinstance(f, list) else str(f or "")
        if "programs" in f and not p.get("id"):
            probs.append(f"request {r.get('name')!r}: field-of-study (programs) fields are very large; "
                         "request them for one school id at a time")
    return probs


def normalize(req, ep, rows):
    for r in rows:
        for col, labels in LABELS.items():
            if col in r and r[col] != "":
                r[col + "_label"] = labels.get(str(r[col]), "")
    return rows


def manual_items(spec, pack):
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    by_name = {r.get("name"): r for r in spec.get("requests") or []}
    items = gr.manual_items(pack, spec)
    for it in items:
        r = by_name.get(it["request"])
        if r is None:
            continue
        ep, params, _ = gr.resolve(pack, r)
        path, query, _ = gr.split(ep, before_request(r, ep, params))
        part = it["name"].split("#")[-1]
        page = int(part) - 1 if part.isdigit() else 0
        query.update(page=page, per_page=100, api_key="DEMO_KEY")
        it["url"] = f"{base}/{path.lstrip('/')}?" + urllib.parse.urlencode(query)
        it["note"] = ((it.get("note") or "") + "; " if it.get("note") else "") + (
            "api_key=DEMO_KEY is api.data.gov's public demo key (about 10 requests an hour); replace it with your "
            "own free key from https://api.data.gov/signup/ if the link answers OVER_RATE_LIMIT.")
    return items

"""USDA NASS Quick Stats adapter (key in the "key" query parameter).

- Filter values in Quick Stats are upper case ("CORN", "IOWA", "AREA PLANTED"); before_request upper-cases
  the *_desc, state_name, county_name and state_alpha values so "corn" works. Operators (__GE, __LE,
  __LT, __GT, __LIKE, __NOT_LIKE, __NE) pass through unchanged.
- Value is published text: "510,000", or a suppression code such as "(D)". normalize() keeps Value and
  adds value_num (the number without commas, empty for codes) and value_flag (the code). "CV (%)" gets
  the same treatment (cv_num, cv_flag).
- Errors come back as {"error": ["exceeds limit = 50000"]} (also inside HTTP 200, handled here).
- get_param_values answers {"<param>": [values]} (or a bare list, per the documentation): one row per value.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402

UPPER = re.compile(r"^(.*_desc|state_name|county_name|state_alpha|asd_desc|region_desc)(__.*)?$")
CODE = re.compile(r"^\(([A-Z]{1,2})\)$")


def check_error(status, body):
    head = body[:400]
    if head[:1] == b"{" and b'"error"' in head:
        try:
            err = json.loads(body.decode("utf-8", "replace")).get("error")
        except ValueError:
            return None
        msg = "; ".join(err) if isinstance(err, list) else str(err)
        if "exceeds limit" in msg:
            msg += " (more than 50,000 rows: add filters such as year, state_alpha, short_desc, or split the years)"
        return msg
    return None


def before_request(req, ep, params):
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and UPPER.match(k) and k != "param":
            v = v.upper()
        out[k] = v
    return out


def _num(v):
    s = str(v or "").strip()
    m = CODE.match(s)
    if m:
        return "", m.group(1)
    t = s.replace(",", "")
    try:
        float(t)
        return t, ""
    except ValueError:
        return "", s


def normalize(req, ep, rows):
    if req.get("endpoint") == "param_values":
        out = []
        for r in rows:
            for k, v in (r.items() if isinstance(r, dict) else []):
                vals = v if isinstance(v, list) else [v]
                out += [{"param": (req.get("params") or {}).get("param", k) if k == "value" else k, "value": x}
                        for x in vals]
        return out
    for r in rows:
        if "Value" in r:
            r["value_num"], r["value_flag"] = _num(r["Value"])
        if "CV (%)" in r:
            r["cv_num"], r["cv_flag"] = _num(r["CV (%)"])
    return rows


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        if r.get("endpoint", "query") == "query" and not any(k.startswith(("year", "commodity_desc", "short_desc", "statisticcat_desc"))
                                                         for k in p) and not r.get("dataset"):
            probs.append(f"request {r.get('name')!r}: add at least a commodity_desc or short_desc and a year filter "
                         "(Quick Stats refuses queries over 50,000 rows)")
    return probs


def manual_items(spec, pack):
    """API links with format=CSV and {API_KEY}; the Quick Stats web page needs no key (see source.md)."""
    reqs = [{**r, "params": before_request(r, None, r.get("params") or {})} for r in spec.get("requests") or []]
    items = gr.manual_items(pack, {**spec, "requests": reqs})
    for it in items:
        if "/api/api_GET/" in it["url"]:
            parts = urllib.parse.urlsplit(it["url"])
            q = [(k, ("CSV" if k == "format" else v)) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
            it["url"] = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, safe="{}")))
            it["filename"] = re.sub(r"\.json$", ".csv", it["filename"])
        it["note"] = ("needs your free Quick Stats key in place of {API_KEY}; without a key, build the same query on "
                      "https://quickstats.nass.usda.gov/ and use Get Data > Spreadsheet")
    return items

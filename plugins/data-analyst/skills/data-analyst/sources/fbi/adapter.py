"""FBI Crime Data Explorer adapter.

- before_request: `from`/`to` given as a year become 01-YYYY / 12-YYYY (the API wants MM-YYYY, except the
  employee endpoints, which want YYYY); state codes are upper-cased.
- normalize: CDE answers with nested chart objects ({"offenses": {"rates": {"United States Offenses":
  {"01-2020": 29.06, ...}}}}). They become one long table: section, series, key, period, year, month,
  value, unit, plus data_through and last_refresh from cde_properties. Values are kept as published.
  Every row also carries span_from / span_to (YYYY-MM, the request's from/to): `type=totals` breakdowns
  have no period of their own and are summed over that span, so the span is the only record of what
  they cover (tables from one request per year then stack without losing the year).
- manual_items: links carry API_KEY=DEMO_KEY (api.data.gov's public demo key), because the API refuses a
  request without any key.
"""
from __future__ import annotations

import re
import urllib.parse

from core import generic_rest as gr

MMYYYY = re.compile(r"^(\d{2})-(\d{4})$")
YYYY = re.compile(r"^\d{4}$")
SKIP_TOP = ("cde_properties",)


def before_request(req, ep, params):
    p = dict(params)
    fmt = ep.get("date_format", "mm-yyyy")
    for k, month in (("from", "01"), ("to", "12")):
        v = str(p.get(k, "")).strip()
        if not v:
            continue
        if fmt == "mm-yyyy" and YYYY.match(v):
            p[k] = f"{month}-{v}"
        elif fmt == "yyyy" and MMYYYY.match(v):
            p[k] = v[-4:]
    if p.get("state"):
        p["state"] = str(p["state"]).upper()
    if p.get("ori"):
        p["ori"] = str(p["ori"]).upper()
    return p


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        for k in ("from", "to"):
            v = str(p.get(k, "")).strip()
            if v and not (MMYYYY.match(v) or YYYY.match(v)):
                probs.append(f"request {r.get('name')!r}: {k}={v!r} must be MM-YYYY (01-2019) or YYYY")
        st = p.get("state")
        if st and not re.fullmatch(r"[A-Za-z]{2}", str(st)):
            probs.append(f"request {r.get('name')!r}: state must be a two-letter code such as CA")
    return probs


def _units(body: dict) -> dict:
    t = ((body.get("tooltips") or {}).get("leftYAxisHeaders") or {}) if isinstance(body, dict) else {}
    return {"rates": t.get("yAxisHeaderRates", ""), "actuals": t.get("yAxisHeaderActual", "")}


def _meta(body: dict) -> dict:
    cp = body.get("cde_properties") if isinstance(body, dict) else None
    if not isinstance(cp, dict):
        lr = body.get("last_refresh_date") if isinstance(body, dict) else None
        return {"data_through": "", "last_refresh": (lr or {}).get("UCR", "") if isinstance(lr, dict) else ""}
    return {"data_through": (cp.get("max_data_date") or {}).get("UCR", ""),
            "last_refresh": (cp.get("last_refresh_date") or {}).get("UCR", "")}


def _unit_for(section: str, series: str, units: dict) -> str:
    last = section.split("/")[-1] if section else series
    if last == "rates" or series.endswith("per 1,000 People"):
        return units["rates"] if last == "rates" and units["rates"] else ("per 1,000 people" if "1,000" in series else "")
    if last == "actuals":
        return units["actuals"] or "count"
    if "Percent of Population Coverage" in section:
        return "percent of population"
    if section.startswith("populations"):
        return "persons"
    return ""


def _leaves(obj, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _leaves(v, path + [str(k)], out)
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) and not any(isinstance(y, (dict, list)) for y in x.values()) for x in obj):
            for x in obj:
                out.append((path, x))
        else:
            for i, x in enumerate(obj):
                _leaves(x, path + [str(i)] if not isinstance(x, dict) else path, out)
    else:
        out.append((path, obj))


def reshape(body, ep) -> list[dict]:
    if ep.get("shape") == "agencies" and isinstance(body, dict):
        rows = []
        for county, agencies in body.items():
            for a in agencies or []:
                rows.append({"county_group": county, **{k: ("" if v is None else v) for k, v in a.items()}})
        return rows
    units = _units(body) if isinstance(body, dict) else {"rates": "", "actuals": ""}
    meta = _meta(body) if isinstance(body, dict) else {"data_through": "", "last_refresh": ""}
    found = []
    if isinstance(body, dict):
        for k, v in body.items():
            if k in SKIP_TOP or k == "last_refresh_date":
                continue
            if k == "tooltips" and isinstance(v, dict):
                v = {kk: vv for kk, vv in v.items() if kk != "leftYAxisHeaders"}
            _leaves(v, [k], found)
    else:
        _leaves(body, [], found)
    rows = []
    for path, val in found:
        if isinstance(val, dict):                    # a record (e.g. LESDC counts)
            rows.append({"section": "/".join(path), **{k: ("" if v is None else v) for k, v in val.items()}, **meta})
            continue
        key = path[-1] if path else ""
        series = path[-2] if len(path) >= 2 else ""
        section = "/".join(path[:-2])
        if not section:                              # two-level breakdowns: {"Arrestee Sex": {"Male": n}}
            section, series = series, ""
        row = {"section": section, "series": series, "key": key, "period": "", "year": "", "month": ""}
        m = MMYYYY.match(key)
        if m:
            row.update(period=f"{m.group(2)}-{m.group(1)}", year=m.group(2), month=str(int(m.group(1))))
        elif YYYY.match(key):
            row.update(period=key, year=key)
        row.update(value="" if val is None else val, unit=_unit_for(section, series, units), **meta)
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("section", "")), str(r.get("series", "")), str(r.get("period", "")) or str(r.get("key", ""))))
    return rows


def _span(req, ep) -> dict:
    """The request's from/to as YYYY-MM (YYYY for the yearly employee endpoints); {} when it has none."""
    p = before_request(req, ep, (req or {}).get("params") or {})
    span = {}
    for k in ("from", "to"):
        v = str(p.get(k, "")).strip()
        m = MMYYYY.match(v)
        span[f"span_{k}"] = f"{m.group(2)}-{m.group(1)}" if m else v
    return span if any(span.values()) else {}


def normalize(req, ep, rows):
    out = []
    for r in rows:
        out += reshape(r, ep)
    span = _span(req, ep)
    return [{**r, **span} for r in out] if span else out


def manual_items(spec, pack):
    """One link per request, with the converted dates and api.data.gov's public DEMO_KEY."""
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    by_name = {r.get("name"): r for r in spec.get("requests") or []}
    items = gr.manual_items(pack, spec)
    for it in items:
        r = by_name.get(it["request"])
        if r is None:
            continue
        ep, params, _ = gr.resolve(pack, r)
        path, query, _ = gr.split(ep, before_request(r, ep, params))
        it["url"] = f"{base}/{path.lstrip('/')}?" + urllib.parse.urlencode({**query, "API_KEY": "DEMO_KEY"})
        it["note"] = ("API_KEY=DEMO_KEY is api.data.gov's public demo key (a few requests an hour); replace it with "
                      "your own free key from https://api.data.gov/signup/ if the link answers OVER_RATE_LIMIT.")
    return items

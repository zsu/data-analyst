"""HUD User FMR / Income Limits API adapter (token in an Authorization: Bearer header).

Responses are wrapped in {"data": ...} with a different shape per resource (documented on
https://www.huduser.gov/portal/dataset/fmr-api.html):
  fmr/data            one area; basicdata is an object (one row) or, for Small Area FMR metros, a list
                      with one entry per ZIP plus "MSA level"
  fmr/statedata       {"year", "metroareas": [...], "counties": [...]}  -> one row per area, area_type column
  il/data, mtspil     one area with nested limit objects -> one row, the nested keys become columns
  list*               a list (a bare list is accepted too)
il/statedata is not documented; lists inside it are turned into rows the same way (unverified).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

BEDROOMS = ("Efficiency", "One-Bedroom", "Two-Bedroom", "Three-Bedroom", "Four-Bedroom")
STATE_EPS = ("fmr_state", "il_state", "list_counties")
ENTITY_EPS = ("fmr", "income_limits", "mtspil")


def _payload(data):
    return data.get("data") if isinstance(data, dict) and "data" in data else data


def _limits(d: dict) -> dict:
    """Flatten nested limit groups (very_low.il50_p1 -> il50_p1); keep the group-free keys as they are."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update({kk: vv for kk, vv in v.items()})
        elif not isinstance(v, list):
            out[k] = v
    return out


def shape(kind: str, data, params: dict) -> list[dict]:
    d = _payload(data)
    if d is None:
        return []
    if kind == "fmr":
        head = {k: v for k, v in d.items() if k not in ("basicdata", "basicdata_prevyear", "percentage_change")}
        head = {"entityid": params.get("entityid", ""), **head}
        bd = d.get("basicdata")
        if isinstance(bd, list):
            return [{**head, **{k: v for k, v in b.items()}} for b in bd]
        if isinstance(bd, dict):
            b = dict(bd)
            if "year" in b and "year" not in head:
                head["year"] = b.pop("year")
            else:
                b.pop("year", None)
            return [{**head, "zip_code": "", **b}]
        return [head]
    if kind == "fmr_state":
        rows = []
        for group, label in (("metroareas", "metro"), ("counties", "county")):
            for a in d.get(group) or []:
                rows.append({"year": d.get("year", ""), "area_type": label, **a})
        return rows
    if kind in ("income_limits", "mtspil"):
        return [{"entityid": params.get("entityid", ""), **_limits(d)}]
    if kind == "il_state":
        if isinstance(d, list):
            return [_limits(x) for x in d if isinstance(x, dict)]
        rows = []
        for k, v in d.items():
            if isinstance(v, list):
                rows += [{"group": k, "year": d.get("year", ""), **_limits(x)} for x in v if isinstance(x, dict)]
        return rows or [_limits(d)]
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    return [d] if isinstance(d, dict) else []


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, n, ep = r.get("params") or {}, r.get("name"), r.get("endpoint")
        if ep in STATE_EPS and p.get("statecode") and not re.fullmatch(r"[A-Za-z]{2}", str(p["statecode"])):
            probs.append(f"request {n!r}: statecode must be a two-letter code such as MA")
        e = str(p.get("entityid") or "")
        if ep in ENTITY_EPS and e and re.fullmatch(r"\d{5}", e):
            probs.append(f"request {n!r}: a 5-digit county FIPS is not a HUD entity id; append 99999 "
                         f"({e}99999) or look it up with list_counties")
    return probs


def before(params: dict) -> dict:
    if params.get("statecode"):
        params = {**params, "statecode": str(params["statecode"]).upper()}
    return params


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        params = before(params)
        path, query, _ = gr.split(ep, params)
        rows = shape(r["endpoint"], c.get_json(path, query), params)
        if r["endpoint"] == "il_state":
            notes.append(f"{r['name']}: il/statedata is not documented by HUD; check the columns before use.")
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check the entity id or state code", kind="bad_request")
        tables[r["name"]] = [{k: ("" if v is None else v) for k, v in row.items()} for row in rows]
    return tables, notes


def manual_items(spec, pack):
    items = gr.manual_items(pack, {**spec, "requests": [{**r, "params": before(r.get("params") or {})}
                                                       for r in spec.get("requests") or []]})
    for it in items:
        it["note"] = ("needs your HUD_USER_TOKEN in an 'Authorization: Bearer' header: use the download script "
                      "(a browser link alone answers 401)")
        # without a token the user uploads HUD's published FMR / income-limits file instead (source.md, Access)
        it["also_ext"] = [".csv", ".xlsx", ".xls"]
    return items


# columns of HUD's published FMR and income-limits files that hold an entity id or a state code
ENTITY_COLS = ("hud_area_code", "metro_code", "fips", "fips2010", "fips2000")
STATE_COLS = ("stusps", "state_alpha")


def _read_excel(p: str):
    """A published HUD workbook; some carry a malformed creation date in docProps/core.xml (FY26_FMRs_revised.xlsx:
    "2026- 2-19T18:17:31Z"), which openpyxl refuses, so retry without that metadata part."""
    import io
    import zipfile
    import pandas as pd
    try:
        return pd.read_excel(p, dtype=str)
    except ValueError:
        if not zipfile.is_zipfile(p):
            raise
    buf = io.BytesIO()
    with zipfile.ZipFile(p) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename != "docProps/core.xml":
                dst.writestr(info, src.read(info.filename))
    buf.seek(0)
    return pd.read_excel(buf, dtype=str)


def _subset(rows: list[dict], params: dict, name: str, fname: str) -> list[dict]:
    """A published file covers every area of the year: keep the rows of the requested entity or state."""
    if not rows:
        return rows
    cols = set(rows[0])
    ent, st = str(params.get("entityid") or ""), str(params.get("statecode") or "").upper()
    if ent:
        use = [c for c in ENTITY_COLS if c in cols]
        if not use:
            return rows
        keep = [r for r in rows if any(str(r.get(c, "")).strip() == ent for c in use)]
        if not keep:
            raise DataError(f"request {name!r}: {fname} has no row for entity id {ent} (looked in {', '.join(use)}); "
                            "check the id and that the file is the requested fiscal year", kind="bad_request")
        return keep
    if st:
        use = [c for c in STATE_COLS if c in cols]
        keep = [r for r in rows if any(str(r.get(c, "")).strip().upper() == st for c in use)]
        return keep if use else rows
    return rows


def parse_upload(pack, req, paths):
    import json
    _, params, _ = gr.resolve(pack, req)
    rows = []
    for p in sorted(paths):
        if p.lower().endswith(".csv"):
            rows += _subset(gr.parse_upload(pack, req, [p]), params, req["name"], os.path.basename(p))
            continue
        if p.lower().endswith((".xlsx", ".xls")):
            try:
                df = _read_excel(p)
            except Exception as e:  # openpyxl missing or not a workbook
                raise DataError(f"cannot read {os.path.basename(p)} ({type(e).__name__}); save it as CSV and upload "
                                "again", kind="bad_request")
            rows += _subset(df.fillna("").to_dict("records"), params, req["name"], os.path.basename(p))
            continue
        with open(p, encoding="utf-8-sig") as f:
            rows += shape(req["endpoint"], json.load(f), before(params))
    return [{k: ("" if v is None else v) for k, v in row.items()} for row in rows]

"""FHFA adapter: published files (no API), read whole and filtered to the spec.

FHFA publishes its House Price Index, conforming loan limits and Public Use Databases as files with stable names
(overwritten in place) on www.fhfa.gov. The generic reader fetches a file ($csv / $xlsx); this adapter then
  - names the columns of headerless files (endpoint "header": hpi_at_state.csv has no header row),
  - renames loan-limit columns to snake_case ("One-Unit\\nLimit" in 2026, "One_Unit_Limit" in 2025 -> one_unit_limit),
    makes "$832,750 " a plain 832750 and zero-pads FIPS codes (2025 has "1"/"1", 2026 "01"/"001"),
  - writes .xlsx numbers stored as binary floats in their shortest form ("69.150000000000006" -> "69.15", the same
    double),
  - keeps the rows that match the spec's filters: params declared with "column" (hpi_flavor, level, place_id,
    state ...), "where" {column: value or list} and min_year / max_year on the endpoint's "year_column".
    Matching ignores case and surrounding spaces; a filter column the file lacks is an error naming its columns.
max_rows applies AFTER filtering (the files are read whole), so fetch() runs the generic fetch without the spec's
max_rows and cuts each table afterwards, with the usual "stopped at" note.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

LONG_FLOAT = re.compile(r"^-?\d+\.\d{9,}$|^-?\d+(\.\d+)?[eE][-+]?\d+$")
MONEY = re.compile(r"^\$?\s*-?[\d,]+(\.\d+)?$")


def check_error(status, body):
    head = (body or b"")[:200].lstrip().lower()
    if status == 200 and (head.startswith(b"<!doctype") or head.startswith(b"<html")):
        return "FHFA answered with a web page instead of the data file (the file may have moved: see source.md)"
    return None


def _norm(v) -> str:
    return " ".join(str(v).split()).lower()


def _filters(ep: dict, prm: dict) -> dict[str, set[str]]:
    out = {}
    for k, d in (ep.get("params") or {}).items():
        if d.get("column") and prm.get(k) not in (None, "", []):
            v = prm[k]
            out[d["column"]] = {_norm(x) for x in (v if isinstance(v, (list, tuple)) else [v])}
    for k, v in (prm.get("where") or {}).items():
        out[k] = {_norm(x) for x in (v if isinstance(v, (list, tuple)) else [v])}
    return out


def check_spec(spec, pack):
    probs = []
    eps = (pack.endpoints or {}).get("endpoints", {})
    for r in spec.get("requests") or []:
        prm = r.get("params") or {}
        if "where" in prm and not isinstance(prm["where"], dict):
            probs.append(f"request {r.get('name')!r}: where must be an object {{column: value or list}}")
        for k in ("min_year", "max_year"):
            if prm.get(k) not in (None, "") and not str(prm[k]).strip().isdigit():
                probs.append(f"request {r.get('name')!r}: {k} must be a year (e.g. 2020)")
        ep = eps.get(r.get("endpoint") or "")
        if ep and ("min_year" in prm or "max_year" in prm) and not ep.get("year_column"):
            probs.append(f"request {r.get('name')!r}: endpoint {r.get('endpoint')!r} has no year column; use where")
        if r.get("endpoint") == "loan_limits" and str(prm.get("year", "")).isdigit() and int(prm["year"]) < 2024:
            probs.append(f"request {r.get('name')!r}: loan limit CSV files start with 2024; earlier years are .xlsx "
                         "files on https://www.fhfa.gov/data/conforming-loan-limit (manual download)")
    return probs


def _reshape(ep: dict, rows: list[dict]) -> list[dict]:
    if ep.get("header") and rows:
        # the reader took the first data line as the header: put it back as a row
        names = ep["header"]
        first = list(rows[0].keys())
        if len(first) != len(names) or any(len(r) != len(names) or None in r for r in rows):
            raise DataError(f"the file no longer has {len(names)} columns ({', '.join(names)}): its layout changed; "
                            "check it on FHFA's HPI datasets page", kind="api")
        rows = [dict(zip(names, first))] + [dict(zip(names, r.values())) for r in rows]
    if ep.get("snake_columns"):
        rows = [{re.sub(r"[^0-9a-z]+", "_", k.lower()).strip("_"): v for k, v in r.items()} for r in rows]
        for r in rows:
            for k, v in r.items():
                if k.endswith("_limit") and isinstance(v, str) and MONEY.match(v.strip()):
                    r[k] = v.strip().lstrip("$").replace(",", "").strip()
            for k, w in (("fips_state_code", 2), ("fips_county_code", 3)):
                if str(r.get(k, "")).strip().isdigit():
                    r[k] = str(r[k]).strip().zfill(w)
    if ep.get("records") == "$xlsx":
        for r in rows:
            for k, v in r.items():
                if isinstance(v, str) and LONG_FLOAT.match(v):
                    r[k] = repr(float(v))
    return rows


def normalize(req, ep, rows):
    rows = _reshape(ep, rows)
    prm = req.get("params") or {}
    flt = _filters(ep, prm)
    if rows and flt:
        missing = [c for c in flt if c not in rows[0]]
        if missing:
            raise DataError(f"request {req.get('name')!r}: column(s) {missing} are not in this file; its columns are: "
                            f"{', '.join(rows[0])}", kind="bad_request")
        rows = [r for r in rows if all(_norm(r.get(c, "")) in vals for c, vals in flt.items())]
    yc = ep.get("year_column")
    lo, hi = prm.get("min_year"), prm.get("max_year")
    if yc and (lo not in (None, "") or hi not in (None, "")):
        def ok(r):
            y = str(r.get(yc, "")).strip()
            if not y.isdigit():
                return False
            return (lo in (None, "") or int(y) >= int(lo)) and (hi in (None, "") or int(y) <= int(hi))
        rows = [r for r in rows if ok(r)]
    return rows


def request_note(req, ep, params):
    if ep.get("snake_columns"):
        return (f"{req['name']}: columns renamed to snake_case, dollar limits written as plain numbers (\"$832,750\" -> "
                "832750) and FIPS codes zero-padded, so every year's file has the same layout.")
    return None


def fetch(spec, pack, route):
    """The generic fetch with max_rows applied after filtering (the files are read whole)."""
    caps = {r["name"]: r.get("max_rows") for r in spec.get("requests") or []}
    plain = {**spec, "requests": [{k: v for k, v in r.items() if k != "max_rows"} for r in spec.get("requests") or []]}
    tables, notes = gr.fetch(pack, plain, gr.client_for(pack))
    for name, cap in caps.items():
        if cap and name in tables and len(tables[name]) > int(cap):
            n = len(tables[name])
            tables[name] = tables[name][:int(cap)]
            notes.append(f"{name}: stopped at {int(cap):,} rows (max_rows); {n:,} rows matched the filters. Narrow "
                         "the filters or raise max_rows.")
    return tables, notes

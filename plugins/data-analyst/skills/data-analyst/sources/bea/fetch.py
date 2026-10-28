#!/usr/bin/env python
"""Get BEA data by the best route available here, and package it as one tidy CSV (the bea deep pack).

Run through the skill's dispatcher (paths below are relative to the skill folder):
  python scripts/fetch.py --source bea SPEC --out OUT --check          # pre-flight: which route? (downloads nothing)
  python scripts/fetch.py --source bea SPEC --out OUT                  # download + package (applies the route)
  python scripts/fetch.py --source bea SPEC --out OUT --dry-run        # the API calls / bulk files only
  python scripts/fetch.py --source bea SPEC --out OUT --prefer bulk    # BEA's public bulk files even with a key
  python scripts/fetch.py --source bea SPEC --out OUT --manual-bundle  # download scripts + instructions only
  python scripts/fetch.py --source bea SPEC --out OUT --ingest [FILES] # package hand downloads (no FILES: uploads)
  python scripts/fetch.py --source bea --find "real gdp" [--dataset NIPA]   # table names (NIPA, workbooks: no key)
  python scripts/fetch.py --source bea --search "real gdp"                 # the same as JSON (capped at 20)
  python scripts/fetch.py --source bea --lines T10106                      # lines of a NIPA table (no key)
  python scripts/fetch.py --source bea --endpoints | --dataset-info ITA    # datasets, parameters, rules (offline)
  python scripts/fetch.py --source bea --datasets | --params ITA | --ping  # live metadata (key; cached a day)
  python scripts/fetch.py --source bea --values Regional LineCode [--filter '{"TableName": "SAINC1"}']

Routes (--check prints one as "route", first match wins; shared names, see scripts/core/routes.py):
  fix_spec         the spec names something BEA does not have, or breaks a dataset's parameter rules
  reuse_existing   OUT/data/bea/ already holds exactly this request: nothing to download
  api              a key was found and BEA accepts it: download through the API
  bulk             no usable key, but every request is in BEA's public bulk files (NIPA flat files, state
                   and county zip files, GDP-by-industry workbooks): no key needed
  run_setup        no key, a request needs the API, and the user has a terminal (local or ssh)
  cloud_secret     no key in a cloud session and a request needs the API (a manual bundle is written too)
  cloud_allowlist  a cloud session cannot reach apps.bea.gov (bundle written)
  manual_download  a sandbox with no route to BEA, or no key and an API-only request: the user downloads
                   the files with the bundle in OUT/manual_download/bea/ and uploads them
  report_error     a key exists but BEA rejects it or has locked it out, or BEA cannot be reached from
                   the user's own machine: tell the user, stop
The normal run applies reuse_existing itself, so rerunning never downloads twice (--force refetches).

Exit codes: 0 ok; 2 BEA or spec error; 3 unexpected error; 4 cannot download (key missing or rejected,
rate-limited, or BEA unreachable). On 4 the JSON says what to tell the user.

spec.json:
{
  "description": "Real GDP growth and state GDP, 2019 to 2025",
  "requests": [
    {"id": "gdp_pct", "dataset": "NIPA", "table": "T10101", "frequency": "Q", "years": "2019-2025"},
    {"id": "state_rgdp", "dataset": "Regional", "table": "SAGDP9", "line_code": 1,
     "geo_fips": "STATE", "years": "2019-2025"},
    {"id": "va_ind", "dataset": "GDPbyIndustry", "table": "TVA105", "frequency": "A", "years": "2024-2025"},
    {"id": "ita", "dataset": "ITA", "params": {"Indicator": "BalGds", "AreaOrCountry": "China",
     "Frequency": "A", "Year": "2023,2024"}}
  ]
}
  years: "2019-2025", [2019, 2020], "ALL", or "LAST5" / "LAST10" (the last n years with data)
  frequency: A | Q | M (NIPA, NIUnderlyingDetail, GDPbyIndustry, ITA ...)
  line_code: a number, a list, or "ALL". Regional: the API takes one line per call, so a list becomes
             several calls. NIPA-like tables: BEA returns the whole table; the rows are filtered to these lines
  geo_fips: "STATE" | "COUNTY" | "MSA" | "PORT" | ... | a state postal code ("NY" = its counties) | a FIPS
            code or a list of them ("06" or "6" = "06000") (Regional). Every level comes back labeled in
            geo_level (COUNTY on the bulk route also holds state and U.S. rows): filter before summing.
  Keys that do not apply to a dataset are reported on stderr, never silently used.
  params: raw API parameters for any dataset; they override the friendly keys
  GDPbyIndustry "table" codes TVA1xx / TGO1xx / TII1xx name the tables in BEA's public GDP-by-industry
             workbooks (ValueAdded.xlsx, GrossOutput.xlsx, IntermediateInputs.xlsx): no key needed

Output (OUT/data/bea/):
  bea_data.csv      tidy long format, one row per series x area x period (columns: COLUMNS below); BEA's
                    non-numeric cells ((D), (NA), (NM), (L) ...) leave value empty and keep the code in flag
  notes.csv         request_id, table, note_ref, note_text (BEA's table notes)
  request.json      the spec, the exact API calls (key removed) or bulk files, route, vintage, checksums
  README.md         what is in the file, units, special values, citation and source line
  raw/              the API responses (key removed)
Bulk files are cached in <cache_root>/bea/bulk (core.env.cache_root(); DATA_ANALYST_CACHE overrides), not in
OUT; request.json records their URLs, dates and SHA-256.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder
import zipfile
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, os.path.join(SKILL_DIR, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bea_api import (DATASETS, KEY_VAR, SIGNUP_URL, UA, BeaClient, BeaError, dataset_problems,  # noqa: E402
                     find_api_key, first_error, redact, scrub_response, value_key)
from core import keys, manual, package as core_package, routes  # noqa: E402
from core import policy as pol  # noqa: E402
from core.env import cache_root, detect_environment, setup_command, upload_dirs  # noqa: E402
from core.http import SOURCES, Client, DataError, reachable  # noqa: E402
from core.package import data_dir  # noqa: E402

SOURCE = "bea"
POLICY = pol.load(HERE)
COLUMNS = ["request_id", "dataset", "table", "line", "series_code", "description", "geo_fips", "geo_name",
           "geo_level", "industry", "time_period", "date", "year", "subperiod", "frequency", "value", "flag", "unit",
           "metric", "unit_mult", "note_ref", "dims"]
NIPA_TXT = "https://apps.bea.gov/national/Release/TXT/"
REGIONAL_ZIP = "https://apps.bea.gov/regional/zip/"
INDUSTRY_XLS = "https://apps.bea.gov/industry/Release/XLS/GDPxInd/"
WORKBOOKS = {"TVA": "ValueAdded.xlsx", "TGO": "GrossOutput.xlsx", "TII": "IntermediateInputs.xlsx"}
WORKBOOK_TABLE = re.compile(r"^T(VA|GO|II)\d+$", re.I)
GUID_RE = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"


def say(msg: str):
    print(redact(msg), file=sys.stderr, flush=True)


def emit(obj, code: int = 0):
    print(redact(json.dumps(obj, indent=2, default=str)))
    sys.exit(code)


def cache_dir() -> str:
    """Where bulk files are kept between runs (outside the user's output folder): <core cache_root>/bea/bulk
    (DATA_ANALYST_CACHE sets the root). The policy allows a persistent cache (public-domain bulk files)."""
    pol.check_cache(POLICY, "persistent")
    d = os.path.join(cache_root(), "bea", "bulk")
    os.makedirs(d, exist_ok=True)
    return d


def clean_geo_name(name) -> str:
    """'Hawaii *' -> 'Hawaii' (BEA marks areas with a footnote by a trailing star)."""
    return re.sub(r"\s*\*+\s*$", "", str(name or "")).strip()


# Bulk zips that hold tables whose name does not match the zip name.
ZIP_ALIASES = {"SARPI": ["SARPP"], "SAIRPD": ["SARPP"], "MAIRPD": ["MARPP"], "SAOVA": ["SAOR"],
               "SAORSA": ["SAOR"]}
NIPA_LIKE = ("NIPA", "NIUnderlyingDetail", "FixedAssets")
GEO_KEYWORDS = ("STATE", "COUNTY", "MSA", "MIC", "CSA", "PORT", "DIV", "TERR")
STATE_FIPS = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
              "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
              "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
              "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
              "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
              "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
              "WV": "54", "WI": "55", "WY": "56"}
SPEC_KEYS = {"id", "dataset", "table", "frequency", "years", "line_code", "geo_fips", "params", "description"}
CITATION = ("U.S. Bureau of Economic Analysis, @TABLES@, @ROUTE@ (accessed @DATE@). BEA data are in the "
            "public domain; cite BEA as the source.")
API_NOTICE = ("This product uses the Bureau of Economic Analysis (BEA) Data API but is not endorsed or "
              "certified by BEA.")
FLAG_TEXT = {"(D)": "suppressed to avoid disclosure of confidential information",
             "(NA)": "not available", "(NM)": "not meaningful", "(L)": "less than $50,000 or less than 10 jobs "
             "(Regional) - the estimate is included in higher-level totals", "(S)": "suppressed: does not meet "
             "publication standards", "(X)": "not applicable", "---": "not available / not applicable",
             "(T)": "suppressed (see the table notes)", "(*)": "MNE: a nonzero value that rounds to zero",
             "n.s.": "MNE: not shown", "n.a.": "not available"}


# --------------------------------------------------------------------------- spec -> requests
def years_param(years, dataset: str) -> str:
    """'2019-2025' / [2019, 2020] / 'ALL' / 'LAST5' -> the API's Year value."""
    if years is None or str(years).upper() in ("ALL", "X", "*"):
        return "X" if dataset in NIPA_LIKE else "ALL"
    if isinstance(years, (list, tuple)):
        return ",".join(str(int(y)) for y in years)
    s = str(years).strip()
    if s.upper().startswith("LAST"):
        return s.upper()
    out = []
    for part in s.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d{4})\s*[-:–]\s*(\d{4})", part)
        if m:
            out += [str(y) for y in range(int(m.group(1)), int(m.group(2)) + 1)]
        elif re.fullmatch(r"\d{4}", part):
            out.append(part)
        elif part:
            raise ValueError(f"years: cannot read {part!r}; use '2019-2025', a list of years, 'ALL' or 'LAST5'")
    return ",".join(out)


def year_set(years_value: str) -> set[int] | None:
    """The years an API Year value stands for (None = all; LASTn handled by the caller)."""
    if years_value.upper() in ("X", "ALL") or years_value.upper().startswith("LAST"):
        return None
    return {int(y) for y in years_value.split(",") if y}


def norm_geo(geo) -> str:
    """Spec geo_fips -> the API's GeoFips: keywords and postal codes upper-cased, codes padded
    ('6' or '06' -> '06000' a state; '6037' -> '06037' a county)."""
    items = geo if isinstance(geo, (list, tuple)) else str(geo).split(",")
    out = []
    for g in items:
        s = str(g).strip()
        if re.fullmatch(r"\d{1,2}", s):
            s = s.zfill(2) + "000"
        elif re.fullmatch(r"\d{3,5}", s):
            s = s.zfill(5)
        else:
            s = s.upper()
        if s:
            out.append(s)
    return ",".join(out)


def normalize_requests(spec: dict) -> list[dict]:
    """Each request -> {'id','dataset','table','frequency','years','line_codes','geo','calls':[api params]}."""
    reqs = spec.get("requests") or ([spec] if spec.get("dataset") else [])
    if not reqs:
        raise ValueError("spec.json needs a 'requests' list")
    out, seen = [], set()
    for i, r in enumerate(reqs, start=1):
        ds = r.get("dataset") or "NIPA"
        raw = {k: str(v) for k, v in (r.get("params") or {}).items()}
        table = r.get("table") or raw.get("TableName") or raw.get("TableID") or ""
        freq = (r.get("frequency") or raw.get("Frequency") or "").upper()
        yv = raw.get("Year") or years_param(r.get("years"), ds)
        # The API knows LASTn for Regional only: elsewhere ask for every year and keep the last n in package()
        api_year = ("X" if ds in NIPA_LIKE else "ALL") if ds != "Regional" and yv.upper().startswith("LAST") else yv
        rid = re.sub(r"\W+", "_", str(r.get("id") or f"{ds}_{table}_{freq}".strip("_"))).strip("_").lower()
        if rid in seen:
            rid = f"{rid}_{i}"
        seen.add(rid)
        workbook = ds == "GDPbyIndustry" and bool(WORKBOOK_TABLE.match(table or ""))
        ignored = set(r) - SPEC_KEYS
        if "geo_fips" in r and ds != "Regional":
            ignored.add("geo_fips")
        if "line_code" in r and ds not in NIPA_LIKE + ("Regional",):
            ignored.add("line_code")
        if not (ds in NIPA_LIKE or ds == "Regional" or workbook):
            ignored |= {k for k in ("frequency", "years") if k in r}
        if ignored:
            say(f"[bea] note: request {rid}: {', '.join(sorted(ignored))} not used for {ds}"
                + (" (pass API parameters in \"params\")" if not (ds in NIPA_LIKE or ds == "Regional" or workbook)
                   else "") + "; see fetch.py --source bea --help")
        base: dict = {}
        line_codes: list[str] = []
        geo = None
        if ds in NIPA_LIKE:
            base = {"TableName": table, "Year": api_year}
            if ds != "FixedAssets":
                base["Frequency"] = freq or "A"
            lc = r.get("line_code")
            if lc is not None and str(lc).upper() != "ALL":    # BEA returns whole tables; package() filters
                line_codes = [str(x) for x in lc] if isinstance(lc, (list, tuple)) else [str(lc)]
        elif workbook:
            base = {"Table": table.upper(), "Frequency": freq or "A", "Year": yv}
        elif ds == "Regional":
            lc = r.get("line_code", raw.get("LineCode", "1"))
            line_codes = ([str(x) for x in lc] if isinstance(lc, (list, tuple)) else [str(lc)])
            geo = norm_geo(r.get("geo_fips", raw.get("GeoFips", "STATE")))
            if "GeoFips" in raw:
                raw["GeoFips"] = norm_geo(raw["GeoFips"])
            base = {"TableName": table, "GeoFips": geo, "Year": yv}
        base.update(raw)
        if ds == "Regional":
            base.pop("LineCode", None)
            calls = [dict(base, LineCode=lc) for lc in line_codes]
        else:
            calls = [base]
        out.append({"id": rid, "dataset": ds, "table": table, "frequency": freq or base.get("Frequency", ""),
                    "years": yv, "line_codes": line_codes, "geo": geo, "calls": calls,
                    "bulk_only": bool(ds == "GDPbyIndustry" and WORKBOOK_TABLE.match(table or ""))})
    return out


def spec_problems(reqs: list[dict]) -> list[str]:
    """Requests BEA would refuse (every refusal counts against 30 errors a minute): check them first."""
    probs = []
    for q in reqs:
        if q.get("bulk_only"):
            continue
        for call in q["calls"]:
            probs += [f"request {q['id']}: {p}" for p in dataset_problems(q["dataset"], call)]
    return list(dict.fromkeys(probs))


def validate_with_metadata(client: BeaClient, reqs: list[dict]) -> list[str]:
    """Check table names (and Regional line codes) against BEA's cached metadata before GetData, as
    bea_api.md section 9 advises. Metadata that cannot be read is skipped, never a reason to stop."""
    probs = []
    names: dict[str, set[str] | None] = {}
    for q in reqs:
        ds = q["dataset"]
        if q.get("bulk_only") or ds not in NIPA_LIKE + ("Regional",) or not q["table"]:
            continue
        if ds not in names:
            try:
                names[ds] = {value_key(v).upper() for v in client.parameter_values(ds, "TableName")} or None
            except (BeaError, DataError) as e:
                if getattr(e, "kind", "") in ("auth", "rate_limit", "budget"):
                    raise
                names[ds] = None
        known = names[ds]
        t = q["table"].upper()
        if known and t not in known and not (ds == "Regional" and (t + "N") in known or t.rstrip("N") in known):
            near = sorted(k for k in known if k[:4] == t[:4])[:8]
            probs.append(f"request {q['id']}: {ds} has no table {q['table']}" +
                         (f" (similar: {', '.join(near)})" if near else "") +
                         f"; list them with --values {ds} TableName or --find")
            continue
        if ds == "Regional" and q["line_codes"] and "ALL" not in [x.upper() for x in q["line_codes"]]:
            try:
                lines = {value_key(v) for v in client.parameter_values_filtered("Regional", "LineCode",
                                                                               TableName=q["table"])}
            except (BeaError, DataError) as e:
                if getattr(e, "kind", "") in ("auth", "rate_limit", "budget"):
                    raise
                lines = set()
            bad = [lc for lc in q["line_codes"] if lines and lc not in lines]
            if bad:
                probs.append(f"request {q['id']}: {q['table']} has no line {', '.join(bad)} "
                             f"(--values Regional LineCode --filter '{{\"TableName\": \"{q['table']}\"}}')")
    return probs


def fingerprint(reqs: list[dict]) -> str:
    parts = [r["calls"] for r in reqs] + [[r["dataset"] for r in reqs]]
    filters = [[r["id"], r["line_codes"]] for r in reqs if r["dataset"] in NIPA_LIKE and r["line_codes"]]
    if filters:                    # only when present, so specs without line filters keep their old hash
        parts.append(filters)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- shared helpers
def parse_value(v) -> tuple[float | None, str]:
    """(number, "") for a numeric DataValue ("1,337" -> 1337.0); (None, the published code) for BEA's
    special values ((D), (NA), (NM), (L), (*) ...): the code goes to the flag column, never a zero."""
    s = str(v if v is not None else "").strip()
    t = s.replace(",", "")
    try:
        return float(t), ""
    except ValueError:
        return None, s


def parse_period(tp: str, freq: str = "") -> tuple[str, int | None, str, str]:
    """'2024' / '2024Q1' / '2024:Q1' / '2024M01' -> (time_period, year, subperiod, frequency)."""
    s = str(tp).strip().replace(":", "")
    m = re.fullmatch(r"(\d{4})(Q[1-4]|M\d{1,2})?", s)
    if not m:
        return s, None, "", freq
    sub = m.group(2) or ""
    if sub.startswith("M") and len(sub) == 2:            # the guide writes YYYYMx; live rows use YYYYMxx
        sub = f"M{int(sub[1:]):02d}"
        s = m.group(1) + sub
    f = "Q" if sub.startswith("Q") else "M" if sub.startswith("M") else (freq or "A")
    return s, int(m.group(1)), sub, f


def geo_level(fips: str, requested: str | None = None) -> str:
    """nation, region, state, county, or the requested area type (msa, port ...). `requested` is the
    request's GeoFips (a keyword, a postal code or a list of codes): only area-type keywords change the answer."""
    req = (requested or "").upper()
    f = str(fips).strip().strip('"')
    if f in ("00000", "00"):
        return "nation"
    if req in ("MSA", "MIC", "CSA", "PORT", "DIV"):
        return req.lower()
    if re.fullmatch(r"9[1-8]000", f):
        return "region"
    if re.fullmatch(r"\d{2}000", f):
        return "state"
    if re.fullmatch(r"\d{5}", f):
        return "county"
    return ""


def section_unit(unit: str, section: str) -> str:
    """A GDP-by-industry table's combined unit ("Percent and percentage points") narrowed by the section the line
    sits in: TVA102 line 1 is under "Percent change", the industry lines under "Percentage points"."""
    sec = (section or "").lower()
    if " and " in (unit or "").lower():
        if sec.startswith("percentage point"):
            return "Percentage points"
        if sec.startswith("percent"):
            return "Percent"
    return unit


def plain_unit(unit: str, metric: str, unit_mult) -> str:
    """BEA's unit for a NIPA level is just "Level": name it from the metric (every MetricName in the NIPA
    SeriesRegister, checked 2026-10-21) and the scale. T10106 -> "millions of chained dollars ...";
    T20804 -> "index, reference year = 100"; T10102 -> "percentage points"."""
    u = (unit or "").strip().lower()
    if u not in ("level", "level change"):
        return unit
    scale = {"3": "thousands of ", "6": "millions of ", "9": "billions of "}.get(str(unit_mult).strip(), "")
    m = (metric or "").lower()
    rate = ("not at annual rates (period rate)" if "period rate" in m
            else "quarterly and monthly values at annual rates")
    change = "change in " if u == "level change" else ""
    if "contribution" in m:
        return "percentage points (contribution to the percent change; quarterly at an annual rate)"
    if "share" in m:
        return "percent (a share of the total)"
    if "ratio" in m:           # the saving rate (percent) and per capita income (dollars) are both "Ratio"
        return "a ratio: percent or dollars per person, as the line description says"
    if "ipd" in m:
        return "implicit price deflator, reference year = 100"
    if "index" in m:
        return "index, reference year = 100"
    if "chained" in m:
        return f"{change}{scale}chained dollars (reference year in the table title); {rate}"
    if "dollar" in m:
        return f"{change}{scale}current dollars; {rate}"
    if "person" in m:
        return f"{scale}persons"
    return f"{scale}{metric.lower()} (see the line description)" if metric else unit


def unit_mult_from_text(unit: str) -> int:
    u = unit.lower()
    for word, p in (("trillion", 12), ("billion", 9), ("million", 6), ("thousand", 3)):
        if word in u:
            return p
    return 0


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- API rows -> tidy rows
def tidy_api_rows(req: dict, call: dict, rows: list[dict]) -> list[dict]:
    ds = req["dataset"]
    out = []
    used = {"TableName", "SeriesCode", "LineNumber", "LineDescription", "TimePeriod", "METRIC_NAME", "CL_UNIT",
            "UNIT_MULT", "DataValue", "NoteRef", "Code", "GeoFips", "GeoName", "TableID", "Frequency", "Year",
            "Quarter", "Industry", "IndustrYDescription", "IndustryDescription", "TimeSeriesId",
            "TimeSeriesDescription", "AreaOrCountry", "Indicator"}
    for r in rows:
        value, flag = parse_value(r.get("DataValue"))
        tp = r.get("TimePeriod")
        if not tp and r.get("Year"):
            # GDPbyIndustry: Quarter equals the Year for annual rows and is I-IV for quarterly ones
            q = str(r.get("Quarter") or "").strip().upper().lstrip("Q")
            q = {"I": "1", "II": "2", "III": "3", "IV": "4"}.get(q, q)
            tp = f"{r['Year']}Q{q}" if q in ("1", "2", "3", "4") else str(r["Year"])
        tp, year, sub, freq = parse_period(tp or "", req.get("frequency") or r.get("Frequency", ""))
        table, line = r.get("TableName") or r.get("TableID") or req["table"], r.get("LineNumber", "")
        if r.get("Code") and "-" in str(r["Code"]):                         # Regional: "SAGDP9-1"
            table, line = str(r["Code"]).rsplit("-", 1)
        elif ds == "Regional":
            line = call.get("LineCode", "")
        dims = {k: v for k, v in r.items() if k not in used and v not in (None, "")}
        out.append({
            "request_id": req["id"], "dataset": ds, "table": table, "line": line,
            "series_code": r.get("SeriesCode") or r.get("TimeSeriesId") or "",
            "description": (r.get("LineDescription") or r.get("TimeSeriesDescription")
                            or r.get("IndustrYDescription") or r.get("IndustryDescription") or r.get("Indicator") or ""),
            "geo_fips": r.get("GeoFips", ""), "geo_name": clean_geo_name(r.get("GeoName") or r.get("AreaOrCountry") or ""),
            "geo_level": geo_level(r.get("GeoFips", ""), "MSA" if str(req["table"]).upper().startswith("MA")
                                   and (req.get("geo") or "").upper() not in GEO_KEYWORDS else req.get("geo"))
                         if r.get("GeoFips") else
                         ("nation" if ds in NIPA_LIKE + ("GDPbyIndustry", "UnderlyingGDPbyIndustry") else ""),
            "industry": r.get("Industry", ""), "time_period": tp, "year": year if year is not None else "",
            "subperiod": sub, "frequency": freq, "value": "" if value is None else repr(value), "flag": flag,
            "unit": r.get("CL_UNIT", ""), "metric": r.get("METRIC_NAME", ""), "unit_mult": r.get("UNIT_MULT", ""),
            "note_ref": r.get("NoteRef", ""), "dims": json.dumps(dims, ensure_ascii=False) if dims else ""})
    return out


def tidy_notes(req: dict, notes: list) -> list[dict]:
    out = []
    for n in notes or []:
        if isinstance(n, dict):
            out.append({"request_id": req["id"], "table": req["table"], "note_ref": n.get("NoteRef", ""),
                        "note_text": redact(n.get("NoteText", ""))})
    return out


# --------------------------------------------------------------------------- bulk files
_BULK: Client | None = None


def bulk_client() -> Client:
    """core.http.Client for the public files: offline fixtures, the restricted switch and the request log
    apply to them as to the API (no key is ever sent)."""
    global _BULK
    if _BULK is None:
        _BULK = Client("", name="bea-bulk", rps=4, burst=4, retries=2, timeout=300, deadline=1800, user_agent=UA)
    return _BULK


def http_get(url: str, dest: str | None = None) -> tuple[bytes | None, dict]:
    try:
        r = bulk_client().get(url)
    except DataError as e:
        raise BeaError(f"{url}: {e}", kind="network" if e.kind == "network" else
                       "api" if e.kind in ("not_found", "fixture_missing") else "http") from None
    meta = {"url": url, "last_modified": r.headers.get("last-modified", ""), "status": r.status}
    if dest is None:
        return r.body, meta
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(r.body)
    os.replace(tmp, dest)
    return None, meta


def head(url: str) -> dict:
    if os.environ.get("DATA_ANALYST_FIXTURES"):
        return {}
    try:
        r = bulk_client().request("HEAD", url)
    except DataError:
        return {}
    return {"status": r.status, "last_modified": r.headers.get("last-modified", ""),
            "length": int(r.headers.get("content-length") or 0), "type": r.headers.get("content-type", "")}


def bea_files_reachable() -> tuple[bool, str]:
    return reachable(NIPA_TXT + "TablesRegister.txt", timeout=20)


def bulk_geo_ok(geo: str, table: str) -> bool:
    """Geographies regional_from_zip can select: STATE, COUNTY, a state postal code (its counties), a list
    of 5-digit codes, or anything in a metro-area (MA*) table, whose files hold only those areas."""
    geo = geo.upper()
    codes = [g for g in geo.split(",") if g]
    return (table.upper().startswith("MA") or geo in ("STATE", "COUNTY")
            or geo in STATE_FIPS and table.upper().startswith("CA")
            or bool(codes) and all(re.fullmatch(r"\d{5}", g) for g in codes))


def bulk_coverage(req: dict) -> tuple[bool, str]:
    """Can BEA's public bulk files serve this request? (True, files) or (False, why not)."""
    ds, t, f = req["dataset"], req["table"].upper(), req["frequency"]
    if ds == "NIPA":
        if f not in ("A", "Q", "M"):
            return False, f"frequency {f!r} not in the NIPA flat files"
        return True, f"NIPA flat files NipaData{f}.txt + SeriesRegister.txt"
    if ds == "Regional":
        geo = str(req.get("geo") or "").upper()
        if geo in ("MSA", "MIC", "CSA", "PORT", "DIV") and not t.startswith("MA"):
            return False, (f"{geo} areas are not in the bulk zip files; the API is needed. Note: since February "
                           "2026 BEA no longer produces GDP or personal income for metro areas in the county "
                           "tables (CAINC*, CAGDP*); sum the component counties instead (docs/datasets.md)")
        if not re.match(r"^(SA|SQ|CA|MA)[A-Z]", t):
            return False, f"no bulk file known for Regional table {t}"
        if not bulk_geo_ok(geo or "STATE", t):
            return False, f"GeoFips {geo} cannot be selected from the bulk zip files; the API is needed"
        return True, "Regional bulk zip " + ", ".join(zip_candidates(t))
    if ds == "GDPbyIndustry" and req.get("bulk_only"):
        return True, f"GDP-by-industry workbook {WORKBOOKS[t[:3]]} (sheet {t}-{f or 'A'})"
    if ds == "GDPbyIndustry":
        return False, ("GDPbyIndustry with API TableIDs needs a key; the same national tables are in BEA's public "
                       "workbooks under codes TVA101-TVA113, TGO101-TGO107, TII1xx: use \"table\": \"TVA105\" etc. "
                       "(docs/datasets.md), which needs no key")
    return False, f"{ds} has no public bulk file this script can read; the API is needed"


def zip_candidates(table: str) -> list[str]:
    t = table.upper()
    fam = re.sub(r"\d.*$", "", t)
    t = re.sub(r"(?<=\d)N$", "", t) if not t.startswith("CA") else t
    cands = [t] if t.startswith("CA") else []
    cands += [fam] + ZIP_ALIASES.get(t, []) + ZIP_ALIASES.get(fam, [])
    if not t.startswith("CA"):
        cands.append(t)
    return list(dict.fromkeys(cands))


def fetch_source(url: str, src_dir: str, manifest: dict) -> str:
    """Download a bulk file into the cache, reusing a copy whose Last-Modified has not changed."""
    os.makedirs(src_dir, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = os.path.join(src_dir, name)
    meta_path = dest + ".meta.json"
    if os.path.exists(dest) and os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            old = json.load(f)
        lm = head(url).get("last_modified")
        if not lm or lm == old.get("last_modified"):
            manifest[name] = old
            return dest
    say(f"[bea] downloading {url}")
    _, meta = http_get(url, dest)
    with open(dest, "rb") as fh:
        magic = fh.read(4)
    if name.endswith(".zip") and magic[:2] != b"PK":
        os.remove(dest)
        raise BeaError(f"{url} is not a zip file (BEA answers a missing bulk file with an HTML page).", kind="api")
    meta.update(sha256=sha256(dest), bytes=os.path.getsize(dest), retrieved=datetime.now().isoformat(timespec="seconds"),
                path=dest)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    manifest[name] = meta
    return dest


def read_text_member(path: str) -> str:
    with open(path, "rb") as f:
        b = f.read()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", errors="replace")


def nipa_from_flat(req: dict, src_dir: str, manifest: dict, files: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Rebuild a NIPA table from the flat files: SeriesRegister maps TableId:LineNo -> SeriesCode."""
    f = req["frequency"] or "A"
    files = files or {}
    reg = files.get("SeriesRegister.txt") or fetch_source(NIPA_TXT + "SeriesRegister.txt", src_dir, manifest)
    dat = files.get(f"NipaData{f}.txt") or fetch_source(NIPA_TXT + f"NipaData{f}.txt", src_dir, manifest)
    tables_reg = files.get("TablesRegister.txt")
    if not tables_reg:
        try:
            tables_reg = fetch_source(NIPA_TXT + "TablesRegister.txt", src_dir, manifest)
        except (BeaError, OSError):
            tables_reg = None
    table = req["table"].upper()
    lines: dict[str, list[dict]] = {}      # one series can sit on several lines of a table (T10205 lines 3 and 6)
    for row in csv.reader(io.StringIO(read_text_member(reg))):
        if not row or row[0].startswith("%"):
            continue
        code, label, metric, calc, scale, locs = row[0], row[1], row[2], row[3], row[4], row[5]
        for loc in locs.split("|"):
            if ":" in loc and loc.split(":")[0].upper() == table:
                lines.setdefault(code, []).append(
                    {"line": loc.split(":")[1], "label": label, "metric": metric, "calc": calc,
                     "unit_mult": str(-int(scale)) if scale.lstrip("-").isdigit() else ""})
    if not lines:
        raise BeaError(f"NIPA table {table} is not in SeriesRegister.txt (check the name with --find).", kind="api")
    ys = year_set(req["years"])
    lastn = int(req["years"][4:]) if req["years"].upper().startswith("LAST") else None
    out, seen_years = [], set()
    for row in csv.reader(io.StringIO(read_text_member(dat))):
        if not row or row[0].startswith("%") or row[0] not in lines:
            continue
        tp, year, sub, freq = parse_period(row[1], f)
        seen_years.add(year)
        if ys is not None and year not in ys:
            continue
        value, flag = parse_value(row[2])
        for info in lines[row[0]]:
            out.append({"request_id": req["id"], "dataset": "NIPA", "table": table, "line": info["line"],
                        "series_code": row[0], "description": info["label"], "geo_fips": "",
                        "geo_name": "United States", "geo_level": "nation", "industry": "", "time_period": tp,
                        "year": year, "subperiod": sub, "frequency": freq,
                        "value": "" if value is None else repr(value), "flag": flag, "unit": info["calc"],
                        "metric": info["metric"], "unit_mult": info["unit_mult"], "note_ref": "", "dims": ""})
    if not out:
        have = sorted(y for y in seen_years if y is not None)
        raise BeaError(f"No rows for NIPA {table}, frequency {f}, years {req['years']}: " +
                       (f"NipaData{f}.txt holds {table} for {have[0]}-{have[-1]}." if have else
                        f"{table} is not published at frequency {f} (try A, Q or M; --lines {table})."), kind="api")
    if lastn:
        keep = sorted({r["year"] for r in out})[-lastn:]
        out = [r for r in out if r["year"] in keep]
    out.sort(key=lambda r: (int(r["line"]) if str(r["line"]).isdigit() else 0, r["time_period"]))
    title = ""
    if tables_reg:
        for row in csv.reader(io.StringIO(read_text_member(tables_reg))):
            if row and row[0].upper() == table:
                title = row[1]
    notes = [{"request_id": req["id"], "table": table, "note_ref": table,
              "note_text": (title + ". " if title else "") + "Rebuilt from the NIPA flat files (SeriesRegister.txt "
              "TableId:LineNo); line descriptions are BEA's series labels, which can differ slightly from the "
              "table's indented stub."}]
    return out, notes


def regional_csv_rows(text: str) -> tuple[list[str], list[list[str]], list[str]]:
    rows = list(csv.reader(io.StringIO(text)))
    header = [h.strip() for h in rows[0]]
    data, foot = [], []
    for r in rows[1:]:
        if len(r) >= len(header) - 1 and r and re.fullmatch(r"\s*\d{5}\s*", r[0].strip().strip('"') or "x"):
            data.append([c.strip() for c in r])
        elif r and any(c.strip() for c in r):
            foot.append(" ".join(c.strip() for c in r))
    return header, data, foot


def regional_footnotes(z: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    """BEA's footnotes for a Regional table, from <TABLE>__Footnotes.html in the zip: [(ref, text)], where
    ref is the footnote number or flag code ('1', '(D)'). They hold units, the reference year, what the U.S.
    row means and the flag codes; the CSV only says 'See the included footnote file'."""
    want = {f"{nm.lower()}__footnotes.html" for nm in names}
    member = next((n for n in z.namelist() if n.lower() in want), None)
    if not member:
        return []
    raw = z.read(member)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    out = []
    for li in re.findall(r"(?is)<li[^>]*>(.*?)</li>", text):
        s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", li)).replace("\xa0", " ")).strip()
        if not s:
            continue
        m = re.match(r"^(\d+)\.\s*(.*)$|^(\([A-Z]+\))\s*(.*)$", s)
        out.append((m.group(1) or m.group(3), m.group(2) if m.group(1) else m.group(4)) if m else ("", s))
    return out


def regional_from_zip(req: dict, src_dir: str, manifest: dict, files: dict | None = None) -> tuple[list[dict], list[dict]]:
    table = req["table"].upper()
    files = files or {}
    chosen = None
    errors = []
    for cand in zip_candidates(table):
        local = files.get(f"{cand}.zip")
        if not local:
            try:
                local = fetch_source(REGIONAL_ZIP + f"{cand}.zip", src_dir, manifest)
            except BeaError as e:
                if e.kind == "network":           # a lost connection is not "no such table"
                    raise
                errors.append(f"{cand}.zip: {str(e)[:80]}")
                continue
            except OSError as e:
                errors.append(f"{cand}.zip: {str(e)[:80]}")
                continue
        z = zipfile.ZipFile(local)
        names = {table, re.sub(r"(?<=\d)N$", "", table)}          # SAGDP9N in the API is SAGDP9 in the zip
        members = [n for n in z.namelist() if n.lower().endswith(".csv")
                   and any(re.match(rf"^{nm}_", n, re.I) for nm in names)]
        if members:
            chosen = (local, z, members)
            break
        errors.append(f"{cand}.zip has no {table} file")
    footnotes: list[tuple[str, str]] = []
    names = {table, re.sub(r"(?<=\d)N$", "", table)}
    if not chosen and files.get(f"{table}.csv"):
        text = read_text_member(files[f"{table}.csv"])
        members_text = [text]
    elif chosen:
        local, z, members = chosen
        allf = [m for m in members if "ALL_AREAS" in m.upper()]
        members_text = [z.read(m).decode("latin-1") for m in (allf or members)]
        footnotes = regional_footnotes(z, names)
    else:
        raise BeaError(f"Regional table {table} was not found in BEA's bulk files ({'; '.join(errors)}). "
                       "Use the API route (it needs a key) or check the table name.", kind="api")
    ys = year_set(req["years"])
    lastn = int(req["years"][4:]) if req["years"].upper().startswith("LAST") else None
    lcs = {str(x).upper() for x in (req.get("line_codes") or ["ALL"])}
    geo = norm_geo(req.get("geo") or "STATE")
    if not bulk_geo_ok(geo, table):
        raise BeaError(f"GeoFips {geo} cannot be selected from the bulk files for {table}; use STATE, COUNTY, a "
                       "state postal code, a list of FIPS codes, or the API route.", kind="api")
    geo_list = set(geo.split(",")) if re.match(r"^\d", geo) else None
    postal = STATE_FIPS.get(geo)
    out, notes, seen, have_years = [], [], set(), set()
    for text in members_text:
        header, data, foot = regional_csv_rows(text)
        periods = header[8:]
        for r in data:
            fips = r[0].strip().strip('"').strip()
            lvl = geo_level(fips, "MSA" if table.startswith("MA") else "")
            if geo_list is not None:
                if fips not in geo_list:
                    continue
            elif postal:                                    # the API's "NY": the counties of that state
                if lvl != "county" or not fips.startswith(postal):
                    continue
            elif geo == "STATE" and lvl not in ("state", "nation"):
                continue
            elif geo == "COUNTY" and lvl not in ("county", "state", "nation"):
                continue
            line = r[4].strip()
            if "ALL" not in lcs and line not in lcs:
                continue
            unit = r[7].strip()
            for k, p in enumerate(periods):
                tp, year, sub, freq = parse_period(p, "Q" if ":" in p else "A")
                have_years.add(year)
                if ys is not None and year not in ys:
                    continue
                key = (fips, line, tp)
                if key in seen:
                    continue
                seen.add(key)
                value, flag = parse_value(r[8 + k] if 8 + k < len(r) else "")
                out.append({"request_id": req["id"], "dataset": "Regional", "table": r[3].strip() or table,
                            "line": line, "series_code": f"{table}-{line}",
                            "description": re.sub(r"\s+\d+/\s*$", "", r[6].strip()),
                            "geo_fips": fips, "geo_name": clean_geo_name(r[1]), "geo_level": lvl,
                            "industry": "" if r[5].strip().strip('"') == "..." else r[5].strip().strip('"'),
                            "time_period": tp, "year": year, "subperiod": sub,
                            "frequency": freq, "value": "" if value is None else repr(value), "flag": flag,
                            "unit": unit, "metric": "", "unit_mult": str(unit_mult_from_text(unit)),
                            "note_ref": "", "dims": ""})
        for line in foot:
            if footnotes and re.search(r"(?i)see the included footnote file", line):
                continue
            notes.append({"request_id": req["id"], "table": table, "note_ref": "", "note_text": line})
    for ref, text in footnotes:
        if not any(n["note_text"] == text for n in notes):
            notes.append({"request_id": req["id"], "table": table, "note_ref": ref, "note_text": text})
    if lastn:
        keep = sorted({r["year"] for r in out})[-lastn:]
        out = [r for r in out if r["year"] in keep]
    if not out:
        have = sorted(y for y in have_years if y is not None)
        if have and ys is not None and not (set(ys) & set(have)):
            raise BeaError(f"No rows for {table}, years {req['years']}: the bulk file holds {have[0]}-{have[-1]} "
                           f"(BEA has not published later years of {table} yet; RPPs lag state income by about a "
                           "year).", kind="bad_request")
        raise BeaError(f"No rows for {table} with lines {sorted(lcs)}, geography {geo}, years {req['years']} in "
                       "the bulk file. Check the line code (--values Regional LineCode --filter "
                       f"'{{\"TableName\": \"{table}\"}}' needs a key; the bulk zip's definition.xml lists the lines).",
                       kind="api")
    return out, notes


def xlsx_sheets(path: str, only: set[str] | None = None) -> dict[str, list[list]]:
    """Read sheets of an .xlsx into lists of row values (standard library only)."""
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(x.text or "" for x in si.iter(f"{{{ns['m']}}}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    target = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in wb.find("m:sheets", ns):
        name = s.get("name")
        if only is not None and name not in only:
            continue
        tgt = target[s.get(f"{{{ns['r']}}}id")].lstrip("/")
        tgt = tgt if tgt.startswith("xl/") else "xl/" + tgt
        rows = []
        for row in ET.fromstring(z.read(tgt)).iter(f"{{{ns['m']}}}row"):
            vals = {}
            for c in row.findall("m:c", ns):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r", "A")).group(0):
                    col = col * 26 + ord(ch) - 64
                v, typ = c.find("m:v", ns), c.get("t")
                if typ == "s" and v is not None:
                    val = shared[int(v.text)]
                elif typ == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
                else:
                    val = v.text if v is not None else None
                vals[col - 1] = val
            rows.append([vals.get(i) for i in range(max(vals) + 1)] if vals else [])
        out[name] = rows
    return out


def workbook_unit(text: str) -> str:
    """'[Millions of dollars] Seasonally adjusted at annual rates' -> 'Millions of dollars; seasonally
    adjusted at annual rates'; '[Millions of dollars]' -> 'Millions of dollars'."""
    m = re.match(r"^\[([^\]]*)\]\s*(.*)$", text.strip())
    if not m:
        return text.strip()
    rest = m.group(2).strip()
    return m.group(1).strip() + (f"; {rest[:1].lower()}{rest[1:]}" if rest else "")


def industry_from_xlsx(req: dict, src_dir: str, manifest: dict, files: dict | None = None) -> tuple[list[dict], list[dict]]:
    """A GDP-by-industry table (TVA105 ...) from BEA's public workbook: sheet '<table>-<A|Q>'."""
    table, f = req["table"].upper(), (req["frequency"] or "A").upper()
    wbname = WORKBOOKS[table[:3]]
    local = (files or {}).get(wbname) or fetch_source(INDUSTRY_XLS + wbname, src_dir, manifest)
    sheet = f"{table}-{f}"
    sheets = xlsx_sheets(local, {sheet, "Contents"})
    if sheet not in sheets:
        raise BeaError(f"{wbname} has no sheet {sheet}; list the tables with --find '' --dataset GDPbyIndustry",
                       kind="api")
    rows = sheets[sheet]
    hdr_i = next(i for i, r in enumerate(rows) if r and str(r[0]).strip() == "Line")
    header = rows[hdr_i]
    meta = [str(r[0]) for r in rows[:hdr_i] if r and r[0]]
    unit = next((workbook_unit(m) for m in meta if m.startswith("[")), "")
    ys = year_set(req["years"])
    lastn = int(req["years"][4:]) if req["years"].upper().startswith("LAST") else None
    out, notes = [], [{"request_id": req["id"], "table": table, "note_ref": table, "note_text": " | ".join(meta)}]
    section = ""
    for r in rows[hdr_i + 1:]:
        if r and r[0] is None and len(r) > 1 and str(r[1] or "").strip().endswith(":"):
            section = str(r[1]).strip().rstrip(":").strip()   # "Percent change:", "Percentage points:", "Addenda:"
            continue
        if not r or r[0] is None:
            continue
        line = str(r[0]).strip()
        if not line.isdigit():
            notes.append({"request_id": req["id"], "table": table, "note_ref": "", "note_text": line})
            continue
        raw_desc = str(r[1] or "")
        indent = len(raw_desc) - len(raw_desc.lstrip(" "))
        desc = re.sub(r"\\\d+\\$", "", raw_desc.strip()).strip()
        for k in range(3, len(header)):
            if header[k] is None:
                continue
            tp, year, sub, freq = parse_period(str(header[k]).strip(), f)
            if ys is not None and year not in ys:
                continue
            value, flag = parse_value(r[k] if k < len(r) else "")
            out.append({"request_id": req["id"], "dataset": "GDPbyIndustry", "table": table, "line": line,
                        "series_code": f"{table}-{line}", "description": desc, "geo_fips": "00000",
                        "geo_name": "United States", "geo_level": "nation", "industry": desc, "time_period": tp,
                        "year": year, "subperiod": sub, "frequency": freq,
                        "value": "" if value is None else repr(value), "flag": flag,
                        "unit": section_unit(unit, section), "metric": "",
                        "unit_mult": str(unit_mult_from_text(unit)), "note_ref": "",
                        "dims": json.dumps({"indent": indent, "section": section})})
    if lastn:
        keep = sorted({r["year"] for r in out})[-lastn:]
        out = [r for r in out if r["year"] in keep]
    if not out:
        raise BeaError(f"No rows for {table} {f} in years {req['years']}", kind="api")
    return out, notes


def period_date(tp: str) -> str:
    m = re.fullmatch(r"(\d{4})(?:Q([1-4])|M(\d{2}))?", str(tp))
    if not m:
        return ""
    month = (int(m.group(2)) - 1) * 3 + 1 if m.group(2) else int(m.group(3)) if m.group(3) else 1
    return f"{m.group(1)}-{month:02d}-01"


# --------------------------------------------------------------------------- package
def finalize_rows(reqs: list[dict], rows: list[dict]) -> list[dict]:
    """What the API cannot do, applied the same way on every route: keep only the requested lines of
    NIPA-like tables, keep the last n years for LASTn, and fail loudly on a request that came back empty
    or without an area it asked for by code."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["request_id"], []).append(r)
    out = []
    for q in reqs:
        rs = by.get(q["id"], [])
        if q["dataset"] in NIPA_LIKE and q["line_codes"] and rs:
            want = set(q["line_codes"])
            have = {str(r["line"]) for r in rs}
            if want - have:
                lines = sorted(have, key=lambda x: int(x) if x.isdigit() else 0)
                raise BeaError(f"Request {q['id']}: {q['table']} has no line {', '.join(sorted(want - have))}. "
                               f"Its lines: {', '.join(lines)} (--lines {q['table']}).", kind="api")
            rs = [r for r in rs if str(r["line"]) in want]
        if str(q["years"]).upper().startswith("LAST") and rs:
            n = int(str(q["years"])[4:])
            keep = sorted({r["year"] for r in rs if isinstance(r["year"], int)})[-n:]
            rs = [r for r in rs if r["year"] in keep]
        if not rs:
            raise BeaError(f"Request {q['id']} ({q['dataset']} {q['table']}, years {q['years']}) returned no rows: "
                           "check the years, frequency, lines and areas.", kind="api")
        if q["dataset"] == "Regional":
            asked = {g for g in str(q.get("geo") or "").split(",") if re.fullmatch(r"\d{5}", g)}
            missing = asked - {r["geo_fips"] for r in rs}
            if missing:
                raise BeaError(f"Request {q['id']}: no {q['table']} rows for GeoFips {', '.join(sorted(missing))} "
                               "(states are SS000, counties SSCCC; see docs/datasets.md section 4).", kind="api")
        out += rs
    return out


ROUTE_TEXT = {"api": "retrieved through the BEA Data API", "bulk": "downloaded from BEA's public bulk files",
              "manual_download": "downloaded by the user and uploaded"}


def package(out: str, reqs: list[dict], spec: dict, rows: list[dict], notes: list[dict], route: str,
            calls_log: list[dict], manifest: dict, acquired: str, n_sources: int = 0) -> dict:
    rows = finalize_rows(reqs, rows)
    ddir = data_dir(out, SOURCE)
    os.makedirs(ddir, exist_ok=True)
    for r in rows:
        r["date"] = period_date(r.get("time_period", ""))
    csv_path = os.path.join(ddir, "bea_data.csv")
    write_csv(csv_path + ".part", rows, COLUMNS)
    os.replace(csv_path + ".part", csv_path)
    write_csv(os.path.join(ddir, "notes.csv"), notes, ["request_id", "table", "note_ref", "note_text"])
    vintage = sorted({n["note_text"] for n in notes if re.search(r"(?i)last (?:updated|revised)|release", n["note_text"])})
    vintage += [f"{k}: Last-Modified {v.get('last_modified')}" for k, v in manifest.items() if v.get("last_modified")]
    flags: dict[str, int] = {}
    for r in rows:
        if r["flag"]:
            flags[r["flag"]] = flags.get(r["flag"], 0) + 1
    by_req: dict[str, int] = {}
    for r in rows:
        by_req[r["request_id"]] = by_req.get(r["request_id"], 0) + 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec = {"source": SOURCE, "description": spec.get("description", ""), "fingerprint": fingerprint(reqs),
           "route": route, "acquired": acquired, "downloaded": date.today().isoformat(), "retrieved": now,
           "rows": len(rows), "rows_by_request": by_req, "flags": flags, "vintage": vintage, "spec": spec,
           "requests": [{k: v for k, v in q.items()} for q in reqs], "api_calls": calls_log,
           "bulk_files": manifest, "csv_sha256": sha256(csv_path),
           "tables": [{"name": "bea_data", "file": "bea_data.csv", "rows": len(rows)},
                      {"name": "notes", "file": "notes.csv", "rows": len(notes)}],
           "http_requests": SOURCES[n_sources:],
           "policy": {"redistribution": POLICY["redistribution"]["mode"], "cache": POLICY["cache"]["mode"]}}
    with open(os.path.join(ddir, "request.json"), "w", encoding="utf-8") as f:
        f.write(redact(json.dumps(rec, indent=2)))
    tables = sorted({f"{q['dataset']} {q['table']}" for q in reqs})
    cite = (CITATION.replace("@TABLES@", "; ".join(tables)).replace("@ROUTE@", ROUTE_TEXT.get(route, route))
            .replace("@DATE@", date.today().strftime("%B %d, %Y").replace(" 0", " ")))
    units = sorted({(r["table"], plain_unit(r.get("unit", ""), r.get("metric", ""), r.get("unit_mult", "")),
                     r["unit_mult"]) for r in rows})
    lines = [
        f"# BEA data: {spec.get('description', '')}", "",
        f"- Downloaded: {date.today().isoformat()} ({acquired})",
        f"- Route: {route}",
        f"- Rows: {len(rows):,} (" + ", ".join(f"{k}: {v:,}" for k, v in by_req.items()) + ")",
        "- File: `bea_data.csv`, tidy long format (one row per series, area and period)",
        "- Notes from BEA: `notes.csv`; exact request and checksums: `request.json`", "",
        "## Requests",
        *[f"- `{q['id']}`: {q['dataset']} {q['table']}" + (f", frequency {q['frequency']}" if q['frequency'] else "")
          + f", years {'ALL' if str(q['years']).upper() == 'X' else q['years']}" + (f", lines {','.join(q['line_codes'])}" if q['line_codes'] else "")
          + (f", areas {q['geo']}" if q['geo'] else "") for q in reqs], "",
        "## Columns",
        "- `value` is numeric as BEA publishes it; multiply by 10**`unit_mult` for base units (for example "
        "unit_mult 6 = millions of dollars). `stats.load()` adds `amount` = value x 10**unit_mult.",
        "- `unit` is BEA's kind of figure (Level, Percent change, annual rate, Index ...) and `metric` the measure "
        "(Current Dollars, Chained Dollars, Persons, Fisher Quantity Index; Ratio is a percent or dollars per "
        "person, as the line description says). Check both before combining series.",
        "- `flag` holds BEA's non-numeric codes ((D), (NA), (NM), (L) ...); `value` is then empty. Never treat a "
        "flagged cell as zero.",
        "- `time_period`: 2024, 2024Q1 or 2024M01; `date` is the first day of that period (for dashboards); "
        "`year`, `subperiod` and `frequency` split it.",
        "- Chained-dollar series use 2017 as the reference year (since BEA's 2023 comprehensive update); the "
        "NIPA bulk files say only \"Chained Dollars\", so state the year from the table header when you cite it.",
        "- GDP-by-industry rows keep the table's indentation in `dims` ({\"indent\": n}); line numbers differ "
        "between tables (TVA106 has an extra line), so match industries across tables by description and order.",
        "", "## Bulk files",
        *([f"- {k}: {v.get('url', '')} (Last-Modified {v.get('last_modified', '')}; cached at {v.get('path', '')})"
           for k, v in manifest.items()] or ["- none (API or upload)"]), "",
        "## Units by table",
        *[f"- {t}: " + "; ".join(f"{u or '(see notes)'}" + (f" (unit_mult {m})" if m not in ("", "0") else "")
                                 for t2, u, m in units if t2 == t)
          for t in list(dict.fromkeys(t for t, _, _ in units))[:40]],
        "", "## Special values found",
        *([f"- `{k}` x {v:,}: {FLAG_TEXT.get(k, 'see the table notes')}" for k, v in flags.items()] or ["- none"]),
        "", "## Vintage",
        *([f"- {v}" for v in vintage] or ["- BEA serves only the latest estimates; the download date above is the vintage."]),
        "Estimates are revised: annual updates each September and comprehensive updates every five years or so. "
        "Rerunning later can change the numbers; the checksum in `request.json` identifies this vintage.", "",
        "## Citation", cite, "",
        "Suggested source line: \"Analyst calculations using data from the Bureau of Economic Analysis.\" "
        "(or \"<Organization>, using data from the Bureau of Economic Analysis\")",
        "", "## Use and redistribution", "",
        f"Rule: **{POLICY['redistribution']['mode']}**. {POLICY['redistribution']['why']}",
    ]
    if route == "api":
        lines += ["", f"Required notice for products built with the API: \"{API_NOTICE}\""]
    with open(os.path.join(ddir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    core_package.copy_pack_lib(os.path.dirname(os.path.dirname(ddir)), SOURCE)   # analysis.py reruns alone
    return {"status": "ok", "source": SOURCE, "route": route, "rows": len(rows), "rows_by_request": by_req,
            "data_dir": ddir, "csv": csv_path, "notes": os.path.join(ddir, "notes.csv"),
            "readme": os.path.join(ddir, "README.md"), "flags": flags, "citation": cite,
            "read_next": os.path.join(HERE, "source.md")}


def run_api(client: BeaClient, reqs: list[dict], out: str) -> tuple[list, list, list]:
    raw_dir = os.path.join(data_dir(out, SOURCE), "raw")
    os.makedirs(raw_dir, exist_ok=True)
    rows, notes, log = [], [], []
    for q in reqs:
        for k, call in enumerate(q["calls"]):
            ds = q["dataset"]
            say(f"[bea] GetData {ds} {json.dumps(call)}")
            data, nts, info = client.get_data(ds, **call)
            rows += tidy_api_rows(q, call, data)
            notes += tidy_notes(q, nts)
            name = f"{q['id']}{'_' + str(k + 1) if len(q['calls']) > 1 else ''}.json"
            with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
                f.write(redact(json.dumps(scrub_response({"request": dict(call, DataSetName=ds), "Data": data,
                                                           **info}), indent=1)))
            log.append({"request_id": q["id"], "dataset": ds, "params": call, "rows": len(data), "file": f"raw/{name}"})
    return rows, notes, log


def run_bulk(reqs: list[dict], out: str, files: dict | None = None) -> tuple[list, list, dict]:
    src = cache_dir()
    rows, notes, manifest = [], [], {}
    for q in reqs:
        ok, why = bulk_coverage(q)
        if not ok:                         # --prefer bulk must not pass through what the files cannot select
            raise BeaError(f"Request {q['id']}: {why}", kind="api")
        if q["dataset"] == "NIPA":
            r, n = nipa_from_flat(q, src, manifest, files)
        elif q["dataset"] == "Regional":
            r, n = regional_from_zip(q, src, manifest, files)
        elif q["dataset"] == "GDPbyIndustry" and q.get("bulk_only"):
            r, n = industry_from_xlsx(q, src, manifest, files)
        else:
            raise BeaError(f"{q['dataset']} has no bulk route", kind="api")
        rows += r
        notes += n
    return rows, notes, manifest


# --------------------------------------------------------------------------- routes
REASON_TEXT = {
    "key_missing": f"no BEA API key was found (not in {KEY_VAR}, a key file, keys.json, Claude Code's settings.json, or ~/.bea/api_key)",
    "auth": "BEA rejected the API key (mistyped, or not yet activated from BEA's email)",
    "network": "BEA (apps.bea.gov) could not be reached from here",
    "rate_limit": "BEA has locked the key out (for a minute to an hour) after too many requests or errors in a minute",
    "budget": "this run stopped itself before BEA's per-minute limits (100 requests, 100 MB, 30 errors) were crossed",
}
BLOCKING_KINDS = tuple(REASON_TEXT)


def existing_data(out: str, reqs: list[dict]) -> dict | None:
    ddir = data_dir(out, SOURCE)
    rp = os.path.join(ddir, "request.json")
    cp = os.path.join(ddir, "bea_data.csv")
    if not os.path.exists(rp):
        return None
    try:
        with open(rp, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    same_ids = [q.get("id") for q in saved.get("requests") or []] == [q["id"] for q in reqs]
    return {"path": cp, "csv_exists": os.path.exists(cp), "route": saved.get("route"),
            "downloaded": saved.get("downloaded"), "rows": saved.get("rows"),
            "spec_matches": saved.get("fingerprint") == fingerprint(reqs) and same_ids}


MANUAL_TEMPLATE = """# Download the BEA data on your own computer

@HEADLINE@ The requests are ready. Download the files on your own computer, then upload them to
the chat (do not unzip or edit them).

## Option A: one command (recommended)

@KEYNOTE@ It saves every file next to the script and, when it finishes, prints the full paths of the
files to upload. If it is interrupted, run it again: files already downloaded are skipped.

@RUN@
## Option B: the same steps by hand

@BULK@
@API@
## Upload

Upload every file listed above to the chat. The assistant then packages them and continues the analysis.

Tips:
- A reply with "APIErrorCode" means a request has a problem. Upload it anyway; the analyst will fix the request.
- BEA allows 100 requests and 30 errors a minute per key; going over blocks the key for up to an hour.
"""

BULK_PART = """### Public files (no key needed; a web browser works)

Download each of these files:

@LIST@
"""

API_PART = """### API requests (need your free BEA key)

Get a key at @SIGNUP@ (BEA emails it; click the activation link first). The first line asks for it
with hidden input; it stays in your own terminal. Do not paste it into the chat. BEA repeats the key inside each reply, so the last command of
each block removes it from the saved files before you upload them.

#### macOS, Linux, Git Bash, WSL

```bash
read -rsp 'BEA API key (hidden): ' BEA_API_KEY && echo && export BEA_API_KEY
@BASH@
sed -i.bak -E 's/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}/REDACTED/g' @FILES@ && rm -f *.json.bak
unset BEA_API_KEY
```

#### Windows PowerShell (use curl.exe; plain curl is a different command there)

```powershell
$env:BEA_API_KEY = [Net.NetworkCredential]::new('', (Read-Host 'BEA API key (hidden)' -AsSecureString)).Password
@PS@
foreach ($f in @PSFILES@) { (Get-Content $f -Raw) -replace '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}','REDACTED' | Set-Content $f }
Remove-Item Env:\\BEA_API_KEY
```
"""


def api_url(dataset: str, call: dict, key_expr: str | None = None) -> str:
    """The GetData URL for one call. key_expr None leaves UserID out (the download scripts add it)."""
    from urllib.parse import quote
    q = "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in call.items())
    user = "" if key_expr is None else f"UserID={key_expr}&"
    return f"https://apps.bea.gov/api/data?{user}method=GetData&DataSetName={dataset}&{q}&ResultFormat=JSON"


def bulk_urls(q: dict) -> list[str]:
    if q["dataset"] == "GDPbyIndustry":
        return [INDUSTRY_XLS + WORKBOOKS[q["table"].upper()[:3]]]
    if q["dataset"] == "NIPA":
        return [NIPA_TXT + f"NipaData{q['frequency'] or 'A'}.txt", NIPA_TXT + "SeriesRegister.txt",
                NIPA_TXT + "TablesRegister.txt"]
    return [REGIONAL_ZIP + f"{zip_candidates(q['table'])[0]}.zip"]


DOWNLOAD_SCRIPTS = {"bash": "download_bea.sh", "powershell": "download_bea.ps1"}


def bundle_dir(out: str) -> str:
    return os.path.join(os.path.abspath(out), "manual_download", SOURCE)


def write_download_scripts(d: str, bulk: list[str], api: list[tuple]) -> dict:
    """Write download_bea.sh and download_bea.ps1 into d, with the file list built in.

    Each downloads the bulk files, asks for the key only when there are API calls, adds it to each call
    and removes it from each reply before saving. Returns {"bash": path, "powershell": path}.
    """
    lines = [f"bulk {u.rsplit('/', 1)[-1]} {u}" for u in bulk] + [f"api {n} {api_url(ds, c)}" for n, ds, c in api]
    paths = {}
    for kind, name in DOWNLOAD_SCRIPTS.items():
        with open(os.path.join(HERE, "assets", name), encoding="utf-8") as f:
            text = f.read().replace("@FILES@", "\n".join(lines))
        path = os.path.join(d, name)
        # bash needs LF line ends even when the bundle is written on Windows; PowerShell takes either
        with open(path, "w", encoding="utf-8", newline="\n" if kind == "bash" else "\r\n") as f:
            f.write(text)
        if kind == "bash":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        paths[kind] = path
    return paths


SEND_BUNDLE = ("Send INSTRUCTIONS.md, both download scripts (download_scripts: download_bea.sh for macOS/Linux, "
               "download_bea.ps1 for Windows) and spec.json (request_spec, the request the scripts have built in) with "
               "your file-sending tool when available, and print their full paths. Give the user the one command "
               "for their system (user_commands; both when unsure): the script asks for the BEA key with hidden input "
               "and downloads every file. Never ask for the key in the chat. Wait for the upload, then run "
               "next_command.")


def _script_paths(bundle):
    return {k: os.path.join(bundle, n) for k, n in DOWNLOAD_SCRIPTS.items()} if bundle else None


def _bundle_fields(bundle) -> dict:
    """What a route JSON says about the hand-download bundle."""
    if not bundle:
        return {"manual_bundle": None, "instructions": None, "download_scripts": None}
    spec = os.path.join(bundle, "spec.json")
    return {"manual_bundle": bundle, "instructions": os.path.join(bundle, "INSTRUCTIONS.md"),
            "download_scripts": _script_paths(bundle), "request_spec": spec if os.path.exists(spec) else None,
            "user_commands": manual.run_lines("download_bea")}


def cli(spec_path: str, out: str, *extra: str) -> str:
    return routes.cmd(f'--source {SOURCE} "{spec_path}" --out "{os.path.abspath(out)}"', *extra)


def write_manual_bundle(reqs: list[dict], out: str, reason: str, spec_path: str = "<out>/spec.json") -> str:
    """Write the download scripts, INSTRUCTIONS.md (script, links and curl steps) and requests.json into
    <out>/manual_download/bea/."""
    d = bundle_dir(out)
    os.makedirs(d, exist_ok=True)
    bulk, api, names = [], [], []
    for q in reqs:
        ok, _ = bulk_coverage(q)
        if ok:
            bulk += bulk_urls(q)
        else:
            for k, call in enumerate(q["calls"]):
                name = f"{q['id']}{'_' + str(k + 1) if len(q['calls']) > 1 else ''}.json"
                api.append((name, q["dataset"], call))
                names.append(name)
    bulk = list(dict.fromkeys(bulk))
    headline = {"network": "The analysis environment cannot reach BEA (apps.bea.gov).",
                "key_missing": "Some of these requests need BEA's Data API, and no API key is set up where the "
                               "analysis runs.",
                "auth": "BEA rejected the API key set up where the analysis runs.",
                "rate_limit": "BEA has temporarily blocked the API key used by the analysis.",
                "requested": "A manual download was requested."}.get(
        reason, f"A manual download was requested ({reason}).")
    keynote = (f"The script downloads every file below. For the API requests it asks for your free BEA API key "
               f"({SIGNUP_URL}; BEA emails it, click the activation link first) with hidden input, and removes the "
               f"key from each reply before saving it. Do not paste the key into the chat." if api else
               "The script downloads every file below. No key is needed.")
    has_spec = os.path.isfile(spec_path)
    if has_spec and os.path.abspath(spec_path) != os.path.abspath(os.path.join(d, "spec.json")):
        shutil.copyfile(spec_path, os.path.join(d, "spec.json"))     # the request, for reference
    run = "\n".join(manual.run_section("download_bea", spec_name="spec.json" if has_spec else ""))
    text = MANUAL_TEMPLATE.replace("@HEADLINE@", headline).replace("@KEYNOTE@", keynote).replace("@RUN@", run + "\n")
    text = text.replace("@BULK@", BULK_PART.replace("@LIST@", "\n".join(f"- {u}" for u in bulk)) if bulk else "")
    if api:
        bash = "\n".join(f'curl -sS -o {n} "{api_url(ds, c, "$BEA_API_KEY")}"' for n, ds, c in api)
        ps = "\n".join(f'curl.exe -sS -o {n} "{api_url(ds, c, "$env:BEA_API_KEY")}"' for n, ds, c in api)
        part = (API_PART.replace("@SIGNUP@", SIGNUP_URL).replace("@BASH@", bash).replace("@PS@", ps)
                .replace("@FILES@", " ".join(names)).replace("@PSFILES@", ",".join(f"'{n}'" for n in names)))
        text = text.replace("@API@", part)
    else:
        text = text.replace("@API@", "")
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
        f.write(text)
    write_download_scripts(d, bulk, api)
    with open(os.path.join(d, "requests.json"), "w", encoding="utf-8") as f:
        json.dump({"source": SOURCE, "bulk_files": bulk,
                   "api_calls": [{"file": n, "dataset": ds, "params": c} for n, ds, c in api]}, f, indent=2)
    return d


def route_check(spec: dict, reqs: list[dict], out: str, env: str, spec_path: str, force: bool = False,
                prefer: str = "auto") -> dict:
    out = os.path.abspath(out)
    run_cmd = cli(spec_path, out)
    ingest_cmd = cli(spec_path, out, "--ingest", "<the uploaded files or their folder>")
    key, source = find_api_key()
    cover = {q["id"]: bulk_coverage(q) for q in reqs}
    all_bulk = all(ok for ok, _ in cover.values())
    r = {"status": "route_check", "source": SOURCE, "environment": env, "os": sys.platform,
         "restricted": keys.restricted(), "key_env": KEY_VAR, "key_source": source,
         "bea_reachable": None, "auth_ok": None, "auth_detail": "", "existing_data": None,
         "bulk_coverage": {k: {"bulk": ok, "detail": why[:160]} for k, (ok, why) in cover.items()},
         "route": None, "message": "", "action": "", "next_command": ""}

    def done(route, message, action, next_command=""):
        r.update(route=route, message=message, action=action, next_command=next_command)
        return r

    def bundle(reason):
        b = write_manual_bundle(reqs, out, reason, spec_path)
        r.update(_bundle_fields(b))
        return b

    probs = spec_problems(reqs)
    if probs:
        return done("fix_spec", "The request has problems: " + "; ".join(probs),
                    "Fix spec.json (source.md, --dataset-info <dataset>), then run --check again.")
    ex = existing_data(out, reqs)
    r["existing_data"] = ex
    if ex and ex["csv_exists"] and ex["spec_matches"] and not force:
        return done("reuse_existing", f"The data for this request are already in {ex['path']} ({ex['rows']} rows, "
                    f"{ex['route']}, {ex['downloaded']}).", "Skip the download and continue with the analysis. "
                    "Pass --force to download again (for example after a BEA release).")
    reach, why = bea_files_reachable()
    r["bea_reachable"] = reach
    need_api = [k for k, (ok, _) in cover.items() if not ok]

    if key and prefer != "bulk":
        client = BeaClient(api_key=key)
        p = client.ping()
        r.update(bea_reachable=p["reachable"], auth_ok=p["auth_ok"], auth_detail=p["detail"])
        if p["kind"] == "ok":
            try:
                mprobs = validate_with_metadata(client, reqs)
            except BeaError:
                mprobs = []
            if mprobs:
                return done("fix_spec", "BEA's metadata does not have: " + "; ".join(mprobs),
                            "Fix spec.json, then run --check again (each failed GetData counts against BEA's "
                            "30 errors a minute).")
            return done("api", f"A BEA API key was found ({source}) and BEA accepts it.", "Run next_command.", run_cmd)
        if p["kind"] in ("auth", "rate_limit", "budget") and all_bulk and reach:
            return done("bulk", f"The key in {source} does not work right now ({p['detail']}), but every "
                        "request is in BEA's public bulk files, so no key is needed. Tell the user about the key "
                        "problem in one line, and that the bulk files hold the same, latest estimates.",
                        "Run next_command (it uses the bulk files).", cli(spec_path, out, "--prefer", "bulk"))
        if p["kind"] == "auth":
            return done("report_error", f"BEA rejected the API key found in {source}: {p['detail']} The key may "
                        f"be mistyped, or not activated yet (BEA emails an activation link). Requests that need the "
                        f"API: {', '.join(need_api)}.", "Tell the user; do not point at the setup script (a key "
                        "exists). Stop until it is fixed.")
        if p["kind"] in ("rate_limit", "budget"):
            return done("report_error", f"BEA has locked the key out (a minute to an hour): {p['detail']}",
                        "Tell the user and stop; retrying makes the lockout longer.")
        if p["kind"] != "network":
            return done("report_error", f"BEA answered the test call with an error: {p['detail']}",
                        "Tell the user and stop.")
        reach = False
    if prefer == "bulk" and not all_bulk:
        return done("fix_spec", "--prefer bulk was given, but these requests are not in the bulk files: "
                    + ", ".join(need_api), "Drop --prefer bulk or change the requests.")

    if reach and all_bulk:
        msg = ("Every request is in BEA's public bulk files, so no key is needed." if not key else
               "Using BEA's public bulk files as requested (--prefer bulk).")
        return done("bulk", msg, "Run next_command. The NIPA flat files are 12-37 MB each; county zips "
                    "up to about 50 MB (cached for the next run).", cli(spec_path, out, "--prefer", "bulk"))
    if not reach and env in ("local", "ssh") and not keys.restricted():
        return done("report_error", f"BEA (apps.bea.gov) could not be reached: {why} Check the internet "
                    "connection, VPN and proxy settings (HTTPS_PROXY), then run the request again.",
                    "Tell the user and stop. Do not substitute data from memory.")
    if not reach:
        b = bundle("network")
        if env == "cloud":
            return done("cloud_allowlist", f"This cloud session cannot reach apps.bea.gov ({why}). Allow apps.bea.gov "
                        "in the environment's network settings and restart, or download on your own computer with "
                        f"the script in {b}.", "Offer both options. For the manual one: " + SEND_BUNDLE, ingest_cmd)
        return done("manual_download", f"BEA cannot be reached from this {env} environment ({why}). In a hosted "
                    "sandbox (claude.ai, Codex cloud) an admin can allow apps.bea.gov; otherwise the user downloads the files on their own "
                    f"computer with one script ({DOWNLOAD_SCRIPTS['bash']} on macOS or Linux, "
                    f"{DOWNLOAD_SCRIPTS['powershell']} on Windows) or the steps in "
                    f"{os.path.join(b, 'INSTRUCTIONS.md')}, and uploads them.", SEND_BUNDLE, ingest_cmd)
    # reachable, no key, and at least one request needs the API
    if env in ("local", "ssh"):
        path, setup = setup_command(SOURCE)
        r.update(setup_script=path, run_command=setup, settings_file=keys.config_path())
        return done("run_setup", f"No BEA API key is set up, and these requests need the API: {', '.join(need_api)}. "
                    f"A key is free at {SIGNUP_URL} (BEA emails it with an activation link). The setup script asks "
                    "for it with hidden input, checks it with BEA and stores it in the skill's keys.json (~/.config/data-analyst).",
                    "Give the user run_command to run in their own terminal (do not run it for them). The scripts "
                    "read the key file directly, so when the user says it is done, run --check again. Never ask "
                    "for the key in the chat. If they would rather not register, offer to drop or replace the "
                    "API-only requests (bulk files cover NIPA tables, state and county Regional tables and the "
                    "GDP-by-industry workbooks; FRED, keyless or with its own key, mirrors the monthly trade "
                    "release: BOPGSTB, BOPGTB, BOPSTB, BOPTEXP, BOPTIMP, and goods by partner such as EXPCH / "
                    "IMPCH for China; and the net international investment position: IIPUSNETIQ).", setup)
    b = bundle("key_missing")
    if env == "cloud":
        return done("cloud_secret", f"This cloud session has no BEA API key, and these requests need the API: "
                    f"{', '.join(need_api)}. Either add {KEY_VAR} to the cloud environment's secrets and start the "
                    f"session again, or download on their own computer with the script in {b}.",
                    "Explain both options. If the user downloads on their own computer: " + SEND_BUNDLE, ingest_cmd)
    return done("manual_download", f"This environment has no BEA API key, and these requests need the API: "
                f"{', '.join(need_api)}. The user downloads the files on their own computer with one script "
                f"(it asks for the key there) or the steps in {os.path.join(b, 'INSTRUCTIONS.md')}.",
                SEND_BUNDLE + " Never ask for the key in the chat.", ingest_cmd)


def report_blocked(err: BeaError, env: str, reqs, out: str, spec_path: str):
    reason = err.kind
    out = os.path.abspath(out)
    detail = str(err).splitlines()[0][:300]
    result = {"status": "blocked", "source": SOURCE, "kind": reason, "environment": env, "error": detail,
              "message": f"Cannot download from BEA: {REASON_TEXT[reason]}.", "action": "", "next_command": ""}
    if reason == "key_missing" and env in ("local", "ssh"):
        path, setup = setup_command(SOURCE)
        result.update(route="run_setup", run_command=setup, next_command=setup, settings_file=keys.config_path(),
                      action="Ask the user to run run_command in their own terminal (key page: "
                             f"{SIGNUP_URL}); never ask for the key in the chat. Or rerun with --prefer bulk when "
                             "every request is in the bulk files.")
    elif reason in ("auth", "rate_limit", "budget") or (env in ("local", "ssh") and not keys.restricted()):
        result.update(route="report_error", action=("A key is set, so do not point the user to the setup script. "
                      "Relay the detail and stop." if reason in ("auth", "rate_limit", "budget") else
                      "Check the internet connection, VPN and proxy, then rerun."))
    else:
        bundle = write_manual_bundle(reqs, out, reason, spec_path) if reqs else None
        route = ("cloud_secret" if env == "cloud" and reason == "key_missing" else
                 "cloud_allowlist" if env == "cloud" else "manual_download")
        result.update(route=route, **_bundle_fields(bundle), action=SEND_BUNDLE,
                      next_command=cli(spec_path, out, "--ingest", "<the uploaded files or their folder>"))
    say(f"CANNOT DOWNLOAD FROM BEA: {REASON_TEXT[reason]}. {result['action']}")
    emit(result, routes.EXIT_BLOCKED)


# --------------------------------------------------------------------------- ingest
def ingest(paths: list[str], spec: dict, reqs: list[dict], out: str) -> dict:
    roots = paths or upload_dirs()
    if not roots:
        raise BeaError("--ingest needs the downloaded files or their folder (and DATA_ANALYST_UPLOADS is not set).",
                       kind="other")
    files = []
    for p in roots:
        if os.path.isdir(p):
            files += [os.path.join(r, f) for r, _, fs in os.walk(p) for f in fs]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise BeaError(f"Not found: {p}", kind="other")
    by_name = {os.path.basename(f): f for f in files}
    src = os.path.join(data_dir(out, SOURCE), "source")
    os.makedirs(src, exist_ok=True)
    rows, notes, log, manifest = [], [], [], {}
    for q in reqs:
        ok, _ = bulk_coverage(q)
        jsons = [by_name.get(f"{q['id']}{'_' + str(k + 1) if len(q['calls']) > 1 else ''}.json") for k in range(len(q["calls"]))]
        if all(jsons):
            for k, (jp, call) in enumerate(zip(jsons, q["calls"])):
                text = read_text_member(jp)
                payload = json.loads(text)
                if re.search(GUID_RE, text):
                    say(f"[bea] warning: {os.path.basename(jp)} still holds the user's key; it was removed from "
                        "the saved copy. Next time, point the user to the download script: it removes the key itself.")
                err = first_error(payload)
                if err:
                    raise BeaError(f"{os.path.basename(jp)} holds a BEA error: {err.get('APIErrorDescription')}", kind="api")
                res = payload.get("BEAAPI", {}).get("Results", {})
                res = res[0] if isinstance(res, list) and res else res
                data = res.get("Data", []) or payload.get("BEAAPI", {}).get("Data", [])
                rows += tidy_api_rows(q, call, data if isinstance(data, list) else [data])
                notes += tidy_notes(q, res.get("Notes", []))
                raw_dir = os.path.join(data_dir(out, SOURCE), "raw")
                os.makedirs(raw_dir, exist_ok=True)
                with open(os.path.join(raw_dir, os.path.basename(jp)), "w", encoding="utf-8") as f:
                    f.write(re.sub(GUID_RE, "[REDACTED]", redact(json.dumps(scrub_response(payload), indent=1))))
                log.append({"request_id": q["id"], "dataset": q["dataset"], "params": call, "rows": len(data),
                            "file": f"raw/{os.path.basename(jp)}"})
            continue
        if ok:
            local = {}
            for name, fp in by_name.items():
                if re.match(r"(?i)^(NipaData[AQM]\.txt|SeriesRegister\.txt|TablesRegister\.txt|[A-Z0-9]+\.zip|[A-Z0-9]+\.csv|[A-Za-z]+\.xlsx)$", name):
                    dest = os.path.join(src, name)
                    if os.path.abspath(fp) != os.path.abspath(dest):
                        shutil.copy2(fp, dest)
                    local[name] = dest
                    manifest[name] = {"file": name, "sha256": sha256(dest), "bytes": os.path.getsize(dest),
                                      "acquired": "uploaded by the user"}
            try:
                if q["dataset"] == "NIPA":
                    r, n = nipa_from_flat(q, src, {}, local)
                elif q["dataset"] == "GDPbyIndustry":
                    r, n = industry_from_xlsx(q, src, {}, local)
                else:
                    r, n = regional_from_zip(q, src, {}, local)
            except (BeaError, OSError, KeyError) as e:
                raise BeaError(f"Request {q['id']}: the uploaded files do not hold it ({str(e)[:200]}). Expected "
                               + ", ".join(u.rsplit('/', 1)[-1] for u in bulk_urls(q)), kind="other") from None
            rows += r
            notes += n
            continue
        raise BeaError(f"Request {q['id']}: no uploaded file for it. Expected "
                       + ", ".join(f"{q['id']}{'_' + str(k + 1) if len(q['calls']) > 1 else ''}.json"
                                   for k in range(len(q['calls'])))
                       + " (from the download script or the curl steps).", kind="other")
    return package(out, reqs, spec, rows, notes, "manual_download", log, manifest, "downloaded by the user and uploaded")


# --------------------------------------------------------------------------- discovery helpers
def find_tables(text: str, dataset: str | None) -> list[tuple[str, str, str]]:
    words = [w.lower() for w in text.split()]
    hits = []
    if dataset in (None, "NIPA"):
        try:
            reg = fetch_source(NIPA_TXT + "TablesRegister.txt", cache_dir(), {})
            for row in csv.reader(io.StringIO(read_text_member(reg))):
                if len(row) >= 2 and all(w in (row[0] + " " + row[1]).lower() for w in words):
                    hits.append(("NIPA", row[0], row[1]))
        except (BeaError, OSError) as e:
            say(f"[bea] NIPA table list unavailable: {e}")
    if dataset in (None, "GDPbyIndustry"):
        for wb in WORKBOOKS.values():
            try:
                local = fetch_source(INDUSTRY_XLS + wb, cache_dir(), {})
                for row in xlsx_sheets(local, {"Contents"})["Contents"]:
                    if len(row) > 2 and row[1] and WORKBOOK_TABLE.match(str(row[1])) and \
                            all(w in (str(row[1]) + " " + str(row[2])).lower() for w in words):
                        hits.append(("GDPbyIndustry", str(row[1]), f"{row[2]} (workbook {wb}; A and Q sheets; no key)"))
            except (BeaError, OSError, KeyError) as e:
                say(f"[bea] {wb} unavailable: {e}")
    if dataset not in (None, "NIPA", "GDPbyIndustry"):
        c = BeaClient()
        param = "TableID" if dataset in ("GDPbyIndustry", "UnderlyingGDPbyIndustry", "InputOutput") else "TableName"
        for v in c.parameter_values(dataset, param):
            k = value_key(v)
            d = str(v.get("Desc") or v.get("Description") or "")
            if all(w in (k + " " + d).lower() for w in words):
                hits.append((dataset, k, d))
    return hits


def list_lines(table: str):
    reg = fetch_source(NIPA_TXT + "SeriesRegister.txt", cache_dir(), {})
    found = []
    for row in csv.reader(io.StringIO(read_text_member(reg))):
        if len(row) < 6 or row[0].startswith("%"):
            continue
        for loc in row[5].split("|"):
            if ":" in loc and loc.split(":")[0].upper() == table.upper():
                found.append((int(loc.split(":")[1]), row[0], row[1], row[2], row[3]))
    for ln, code, label, metric, calc in sorted(found):
        print(f"{ln}\t{code}\t{label}\t{metric}\t{calc}")
    if not found:
        say(f"{table}: not in SeriesRegister.txt")


def endpoints_info() -> dict:
    return {"source": SOURCE, "read": os.path.join(HERE, "source.md"),
            "datasets": {k: {"about": v["about"], "required": v["required"], "bulk_route": bool(v.get("bulk"))}
                         for k, v in DATASETS.items()},
            "bulk_files": {"NIPA": NIPA_TXT + "NipaData{A,Q,M}.txt + SeriesRegister.txt + TablesRegister.txt",
                           "Regional": REGIONAL_ZIP + "<SAGDP|SQGDP|SAINC|SQINC|SAPCE|SARPP|SAOR|CAINC1|...>.zip",
                           "GDPbyIndustry": INDUSTRY_XLS + "{ValueAdded,GrossOutput,IntermediateInputs}.xlsx"},
            "discovery": ["--find WORDS [--dataset D]", "--search WORDS", "--lines T10106", "--dataset-info D",
                          "--datasets", "--params D", "--values D PARAM [--filter JSON]"]}


def utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if (getattr(s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


# --------------------------------------------------------------------------- main
def main():
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="fetch.py --source bea", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out", default="out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--prefer", choices=["auto", "api", "bulk"], default="auto")
    ap.add_argument("--route-override", choices=["api", "bulk"], help="same as --prefer (dispatcher name)")
    ap.add_argument("--manual-bundle", action="store_true")
    ap.add_argument("--ingest", nargs="*", metavar="PATH")
    ap.add_argument("--env", choices=["auto", "local", "ssh", "cloud", "sandbox"], default="auto")
    ap.add_argument("--no-cache", action="store_true", help="do not use the cached BEA metadata")
    ap.add_argument("--find", metavar="TEXT")
    ap.add_argument("--search", metavar="TEXT")
    ap.add_argument("--dataset")
    ap.add_argument("--lines", metavar="NIPA_TABLE")
    ap.add_argument("--values", nargs=2, metavar=("DATASET", "PARAMETER"))
    ap.add_argument("--filter", help="JSON filters for --values (GetParameterValuesFiltered)")
    ap.add_argument("--datasets", action="store_true")
    ap.add_argument("--params", metavar="DATASET")
    ap.add_argument("--dataset-info", metavar="DATASET")
    ap.add_argument("--endpoints", action="store_true")
    ap.add_argument("--ping", action="store_true")
    a = ap.parse_args()
    env = detect_environment(None if a.env == "auto" else a.env)
    prefer = a.route_override or a.prefer
    reqs = None
    spec_path = os.path.abspath(a.spec or os.path.join(a.out, "spec.json"))
    try:
        if a.endpoints:
            emit(endpoints_info())
        if a.dataset_info:
            name = next((k for k in DATASETS if k.lower() == a.dataset_info.lower()), None)
            if not name:
                emit({"status": "error", "source": SOURCE, "message": f"unknown dataset; known: {', '.join(DATASETS)}"},
                     routes.EXIT_SPEC)
            emit({"source": SOURCE, "dataset": name, **DATASETS[name],
                  "live": f"--params {name} and --values {name} <PARAMETER> list BEA's current values (key needed)"})
        if a.find is not None:
            for ds, k, d in find_tables(a.find, a.dataset):
                print(f"{ds}\t{k}\t{d}")
            return
        if a.search is not None:
            hits = find_tables(a.search, a.dataset)
            emit({"source": SOURCE, "query": a.search, "results": [{"dataset": d, "table": k, "title": t}
                                                                     for d, k, t in hits[:20]],
                  **({"note": f"{len(hits)} matches; showing 20: narrow the words or add --dataset"}
                     if len(hits) > 20 else {})})
        if a.lines:
            return list_lines(a.lines)
        if a.ping:
            key, src = find_api_key()
            if not key:
                emit({"source": SOURCE, "auth_ok": False, "kind": "key_missing", "detail": REASON_TEXT["key_missing"]},
                     routes.EXIT_BLOCKED)
            p = BeaClient(api_key=key).ping()
            emit({"source": SOURCE, "key_source": src, **p}, 0 if p["kind"] == "ok" else routes.EXIT_BLOCKED)
        if a.datasets:
            for v in BeaClient(use_cache=not a.no_cache).datasets():
                print("\t".join(str(x) for x in v.values()))
            return
        if a.params:
            for v in BeaClient(use_cache=not a.no_cache).parameters(a.params):
                print("\t".join(f"{k}={x}" for k, x in v.items()))
            return
        if a.values:
            c = BeaClient(use_cache=not a.no_cache)
            if a.filter:
                flt = {k: (",".join(map(str, v)) if isinstance(v, list) else str(v)) for k, v in json.loads(a.filter).items()}
                vals = c.parameter_values_filtered(a.values[0], a.values[1], **flt)
            else:
                vals = c.parameter_values(*a.values)
            for v in vals:
                print("\t".join(str(x) for x in v.values()))
            return
        with open(spec_path, encoding="utf-8-sig") as f:
            spec = json.load(f)
        try:
            reqs = normalize_requests(spec)
        except ValueError as e:
            if a.check:
                emit({"status": "route_check", "source": SOURCE, "route": "fix_spec", "message": str(e),
                      "action": "Fix spec.json (see source.md), then run --check again.", "next_command": ""},
                     routes.EXIT_SPEC)
            raise
        if a.dry_run:
            emit({"source": SOURCE, "problems": spec_problems(reqs),
                  "requests": [{"id": q["id"], "dataset": q["dataset"], "calls": q["calls"], "bulk": bulk_coverage(q)}
                               for q in reqs]})
        if a.manual_bundle:
            d = write_manual_bundle(reqs, a.out, "requested", spec_path)
            emit({"status": "ok", "source": SOURCE, **_bundle_fields(d)})
        if a.ingest is not None:
            emit(ingest(a.ingest, spec, reqs, a.out))
        if a.check:
            r = route_check(spec, reqs, a.out, env, spec_path, a.force, prefer)
            say(f"[bea] route: {r['route']} ({env}, key: {r['key_source'] or 'none'})\n[bea] {r['message']}\n"
                f"[bea] next: {r['action']}")
            emit(r, routes.EXIT_SPEC if r["route"] in ("fix_spec", "report_error") else 0)

        probs = spec_problems(reqs)
        if probs:
            raise BeaError("The request has problems: " + "; ".join(probs), kind="api")
        ex = existing_data(a.out, reqs)
        if ex and ex["csv_exists"] and ex["spec_matches"] and not a.force:
            say(f"[bea] reusing {ex['path']} ({ex['rows']} rows, {ex['route']}, {ex['downloaded']}); "
                "pass --force to download again")
            emit({"status": "ok", "source": SOURCE, "route": "reuse_existing", "csv": ex["path"], "rows": ex["rows"],
                  "data_dir": data_dir(a.out, SOURCE)})
        n0 = len(SOURCES)
        key, _ = find_api_key()
        cover_all = all(bulk_coverage(q)[0] for q in reqs)
        if prefer == "bulk" or (not key and cover_all and prefer != "api"):
            rows, notes, manifest = run_bulk(reqs, a.out)
            emit(package(a.out, reqs, spec, rows, notes, "bulk", [], manifest, "BEA public bulk files", n0))
        api_reqs = [q for q in reqs if not q.get("bulk_only")]
        file_reqs = [q for q in reqs if q.get("bulk_only")]      # workbook tables have no API form
        log, rows, notes = [], [], []
        if api_reqs:
            client = BeaClient(use_cache=not a.no_cache)
            mprobs = validate_with_metadata(client, api_reqs)
            if mprobs:
                raise BeaError("BEA's metadata does not have: " + "; ".join(mprobs), kind="api")
            rows, notes, log = run_api(client, api_reqs, a.out)
        manifest = {}
        if file_reqs:
            r2, n2, manifest = run_bulk(file_reqs, a.out)
            rows, notes = rows + r2, notes + n2
        route = "api" if api_reqs else "bulk"
        emit(package(a.out, reqs, spec, rows, notes, route, log, manifest,
                     "BEA Data API" + (" and public bulk files" if file_reqs else "") if api_reqs else "BEA public bulk files",
                     n0))
    except BeaError as e:
        if e.kind in BLOCKING_KINDS:
            report_blocked(e, env, reqs, a.out, spec_path)
        emit({"status": "error", "source": SOURCE, "kind": e.kind, "message": str(e)}, routes.EXIT_SPEC)
    except DataError as e:
        if e.kind == "network":
            report_blocked(BeaError(str(e), kind="network"), env, reqs, a.out, spec_path)
        emit({"status": "error", "source": SOURCE, "kind": e.kind, "message": str(e)}, routes.EXIT_SPEC)
    except (ValueError, KeyError, OSError, pol.PolicyError) as e:
        emit({"status": "error", "source": SOURCE, "kind": "spec", "message": redact(f"{type(e).__name__}: {e}")},
             routes.EXIT_SPEC)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:                       # unexpected: a traceback scrubbed of the key
        import traceback
        print(json.dumps({"status": "error", "source": SOURCE, "kind": "unexpected",
                          "message": redact(traceback.format_exc()[-1500:])}))
        sys.exit(routes.EXIT_UNEXPECTED)

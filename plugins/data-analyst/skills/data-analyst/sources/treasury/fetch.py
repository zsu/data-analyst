#!/usr/bin/env python
"""Download, package and describe U.S. Treasury Fiscal Data API datasets (the treasury deep pack).

Usage (through the skill's dispatcher; paths relative to the skill folder)
  python scripts/fetch.py --source treasury SPEC --out OUT --check          # pre-flight: which route? (no download)
  python scripts/fetch.py --source treasury SPEC --out OUT                  # download every dataset + package it
  python scripts/fetch.py --source treasury --search TERM                   # live catalog (or the bundled catalog.json)
  python scripts/fetch.py --source treasury --endpoints                     # the curated endpoints (rows, units, traps)
  python scripts/fetch.py --source treasury --fields ENDPOINT [--filter JSON]   # fields, types, row count, first/last
  python scripts/fetch.py --source treasury --fields ENDPOINT --filter JSON --values  # values of code columns
  python scripts/fetch.py --source treasury SPEC --out OUT --manual-bundle  # download links + instructions only
  python scripts/fetch.py --source treasury SPEC --out OUT --ingest [FILES] # package hand downloads (no FILES: uploads)
  python scripts/fetch.py --source treasury --context cpi|gdp --out OUT --start 2010   # CPI / GDP context series

Routes (--check prints one as "route", first match wins; shared names, see scripts/core/routes.py):
  reuse_existing   OUT/data/treasury/ already holds exactly this spec's data: nothing to download
  api              the API answers from here: download with Python (no key)
  fix_spec         the API answers, but a dataset's filter matches no rows or names an unknown field: fix spec.json
                   (see values with --fields ENDPOINT --filter JSON --values)
  report_error     local/ssh machine that cannot reach the API: fix internet / VPN / proxy (HTTPS_PROXY),
                   or the user runs the bundled curl / PowerShell commands in their own terminal (bundle written)
  cloud_allowlist  cloud session whose network policy blocks the API: allow api.fiscaldata.treasury.gov in the
                   environment's network settings and restart, or download by hand (bundle written)
  manual_download  sandbox (claude.ai, Cowork, ChatGPT, Codex cloud, container) with no route to the API: the user downloads the CSV
                   files with the bundled links and uploads them, then --ingest
The normal run applies reuse_existing itself, so rerunning never downloads twice (--refresh or --force
re-downloads).

Exit codes: 0 ok; 2 spec or API error (bad field, filter, endpoint, aggregation); 3 unexpected error;
4 cannot reach the API (the printed JSON says which route to take).

spec.json:
{
  "description": "Federal receipts and outlays by month, FY2016-FY2025",
  "as_of": "2025-09-30",                     # data cut-off: adds <date_field> <= as_of to every dataset
  "datasets": [
    {"name": "mts9",                         # file name: data/mts9.csv
     "endpoint": "v1/accounting/mts/mts_table_9",
     "filter": {"record_date": {"gte": "2015-10-01"}},   # optional; bare value = eq, list = in
     "fields": null,                         # optional; omit unless you include every row-identity column
     "date_field": "record_date",            # optional; a few endpoints use operation_date, eff_date ...
     "sort": "record_date,src_line_nbr",     # optional; default: date_field (+ src_line_nbr when present)
     "allow_empty": false}                   # optional; a filter that matches no rows is an error unless true
  ]
}
A "fields" subset that leaves out row-identity columns makes the API GROUP AND SUM rows (totals mixed with
details). The download compares the row count with and without "fields" and stops if they differ.

Output (OUT/data/treasury/):
  <name>.csv          one file per dataset, API field names as headers, values exactly as published ("null" -> empty)
  fetch_spec.json     the spec, exact query URLs, row counts, first/last date, field labels and types, unit hints,
                      SHA-256 of each file, retrieval time and route (provenance for the audit trail)
  README.md           what was downloaded, data dictionary per dataset, unit warnings, citation
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder
import tempfile
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, os.path.join(SKILL_DIR, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from treasury_api import (BASE, MAX_PAGE, FiscalDataClient, FiscalDataError, build_url,  # noqa: E402
                          filter_string, normalize_endpoint, query_params)
from core import manual, package as core_package, routes  # noqa: E402
from core import policy as cpol  # noqa: E402
from core.env import detect_environment, upload_dirs  # noqa: E402
from core.keys import redact  # noqa: E402
from core.package import data_dir  # noqa: E402

SOURCE = "treasury"
CATALOG_FILE = os.path.join(HERE, "catalog.json")
SEARCH_LIMIT = 20
CATALOG_URL = "https://api.fiscaldata.treasury.gov/services/dtg/metadata/"
ENVIRONMENTS = ("local", "ssh", "cloud", "sandbox")
CITATION = ("U.S. Department of the Treasury, Bureau of the Fiscal Service, Fiscal Data "
            "(https://fiscaldata.treasury.gov), {datasets}; retrieved {date} through the Fiscal Data API.")
# Website dataset pages (Download button: whole table or a date range, no 10,000-row page limit).
DATASET_PAGES = [("/mts/", "monthly-treasury-statement"), ("/dts/", "daily-treasury-statement"),
                 ("debt_to_penny", "debt-to-the-penny"), ("debt_outstanding", "historical-debt-outstanding"),
                 ("avg_interest_rates", "average-interest-rates-treasury-securities"),
                 ("interest_expense", "interest-expense-debt-outstanding"),
                 ("rates_of_exchange", "treasury-reporting-rates-exchange"),
                 ("/mspd/", "monthly-statement-public-debt"), ("auctions_query", "treasury-securities-auctions-data"),
                 ("record_setting_auction", "record-setting-auction-data"), ("/rcm", "revenue-collections-management"),
                 ("gift_contributions", "gift-contributions-reduce-debt-held-by-public")]


# --------------------------------------------------------------------------- output
def emit(obj, code: int = 0):
    print(redact(json.dumps(obj, indent=2, default=str)))
    sys.exit(code)


def say(msg: str):
    print(redact(msg), file=sys.stderr, flush=True)


def ddir(out: str) -> str:
    return data_dir(out, SOURCE)


def cli(spec_path: str, out: str, *extra: str) -> str:
    return routes.cmd(f'--source {SOURCE} "{spec_path}" --out "{os.path.abspath(out)}"', *extra)


# --------------------------------------------------------------------------- spec
def load_spec(path: str) -> dict:
    spec = json.load(open(path, encoding="utf-8"))
    ds = spec.get("datasets")
    if not ds:
        raise FiscalDataError("spec.json needs a non-empty \"datasets\" list", "bad_request")
    names = set()
    for d in ds:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", d.get("name", "")):
            raise FiscalDataError(f"dataset name {d.get('name')!r}: lowercase letters, digits and _ only",
                                  "bad_request")
        if d["name"] in names:
            raise FiscalDataError(f"dataset name {d['name']!r} is used twice", "bad_request")
        names.add(d["name"])
        d["endpoint"] = normalize_endpoint(d["endpoint"])
    return spec


def date_field(d: dict) -> str:
    return d.get("date_field") or "record_date"


def effective_filter(spec: dict, d: dict):
    """The dataset filter plus the spec's as_of cut-off (<date_field> <= as_of)."""
    f = d.get("filter") or {}
    df = date_field(d)
    if isinstance(f, str):
        return f + (f",{df}:lte:{spec['as_of']}" if spec.get("as_of") else "")
    f = json.loads(json.dumps(f))
    if spec.get("as_of"):
        cur = f.get(df)
        if cur is None:
            f[df] = {"lte": spec["as_of"]}
        elif isinstance(cur, dict) and "lte" not in cur and "lt" not in cur and "eq" not in cur:
            cur["lte"] = spec["as_of"]
    return f


# The API mishandles some characters in filter values [live, 2026-10-20]: a value with "&" or "," matches nothing
# (even URL-encoded), and in an "in" list a value with "," or parentheses is silently dropped. Conditions with such
# values are applied locally after the download instead (same rows, no silent loss).
SAFE_VALUE = re.compile(r"^[A-Za-z0-9 ._/:-]*$")
SAFE_EQ_VALUE = re.compile(r"^[A-Za-z0-9 ._/:()-]*$")      # parentheses work with eq, not inside in (...)


def split_filter(filt):
    """(api_part, local_part) of a friendly filter. A string filter is already API syntax: sent as is."""
    if not filt or isinstance(filt, str):
        return filt, {}
    api, local = {}, {}
    for field, cond in filt.items():
        if isinstance(cond, dict):
            vals = [(op, x) for op, v in cond.items() for x in (v if op == "in" and isinstance(v, (list, tuple)) else [v])]
        elif isinstance(cond, (list, tuple)):
            vals = [("in", x) for x in cond]
        else:
            vals = [("eq", cond)]
        safe = all((SAFE_VALUE if op == "in" else SAFE_EQ_VALUE).match(str(x).strip()) for op, x in vals)
        (api if safe else local)[field] = cond
    return api, local


def default_sort(d: dict, labels: dict | None = None) -> str:
    if d.get("sort"):
        return d["sort"]
    s = date_field(d)
    if labels and "src_line_nbr" in labels and "src_line_nbr" != s:
        s += ",src_line_nbr"                  # a tiebreaker keeps pages stable
    return s


def dataset_queries(spec: dict) -> list[dict]:
    out = []
    for d in spec["datasets"]:
        filt = effective_filter(spec, d)
        api_f, local_f = split_filter(filt)
        sort = default_sort(d)
        out.append({"name": d["name"], "endpoint": d["endpoint"], "fields": d.get("fields"), "date_field": date_field(d),
                    "filter": filter_string(filt), "local_filter": local_f, "sort": sort,
                    "url_json": build_url(d["endpoint"], query_params(d.get("fields"), api_f, sort)),
                    "url_csv": build_url(d["endpoint"], query_params(d.get("fields"), api_f, sort, fmt="csv"))})
    return out


def fingerprint(spec: dict) -> str:
    q = [{k: v for k, v in x.items() if k in ("endpoint", "fields", "filter", "sort")} for x in dataset_queries(spec)]
    return hashlib.sha256(json.dumps(q, sort_keys=True).encode()).hexdigest()[:16]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def unit_hint(endpoint: str, labels: dict, types: dict, formats: dict | None = None) -> str:
    """Plain-language unit warning for the README (amount units differ by dataset). A field is in millions when the
    API's own dataFormats says "$1,000,000" (MSPD table 3 issued_amt/outstanding_amt, Schedules of Federal Debt) or
    its name says mil_ (mts_receipts_outlays_deficit_surplus.mil_amt is millions though its format says "$10.20")."""
    cols = list(labels or types or [])
    formats = formats or {}
    money = [c for c in cols if (types or {}).get(c, "").startswith("CURRENCY") or (
        (c.endswith("_amt") or c.endswith("_bal")) and not (types or {}).get(c, "").startswith(("PERCENTAGE", "NUMBER")))]
    if "/dts/" in endpoint:
        return "DTS amounts are in MILLIONS of dollars (rounded to the nearest million)."
    mil = [c for c in money if "mil_" in c or c.endswith("_mil_amt") or formats.get(c) == "$1,000,000"]
    if "rates_of_exchange" in endpoint:
        return "exchange_rate is FOREIGN currency units per one U.S. dollar."
    pct = [c for c in cols if (types or {}).get(c, "").startswith("PERCENTAGE")]
    other = [c for c in money if c not in mil]
    note = f"Fields {', '.join(mil)} are in MILLIONS of dollars." if mil else ""
    if other and formats:
        note += " Other amounts are in dollars and cents." if mil else "Amounts are in dollars and cents."
    elif other:                              # a manual upload carries no API metadata
        note += (" Amount units were not confirmed from API metadata: before scaling, check the dataset's data "
                 "dictionary on fiscaldata.treasury.gov (format $1,000,000 = millions; e.g. MSPD table 3 and the "
                 "Schedules of Federal Debt are in millions).")
    if pct:
        note += f" Percentage fields ({', '.join(pct)}) are already in percent (3.870 means 3.87 percent)."
    return note.strip()


def existing_data(out: str, spec: dict) -> dict | None:
    p = os.path.join(ddir(out), "fetch_spec.json")
    if not os.path.exists(p):
        return None
    try:
        saved = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files_ok = all(os.path.exists(os.path.join(ddir(out), f"{d['name']}.csv")) for d in spec["datasets"])
    return {"retrieved": saved.get("retrieved"), "route": saved.get("route"), "files_ok": files_ok,
            "spec_matches": saved.get("fingerprint") == fingerprint(spec), "as_of": saved.get("spec", {}).get("as_of"),
            "datasets": {k: {"rows": v.get("rows"), "last_date": v.get("last_date")}
                         for k, v in (saved.get("datasets") or {}).items()}}


# --------------------------------------------------------------------------- writing
def write_csv(path: str, rows: list[dict], fields: list[str]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) in (None, "null") else r.get(k)) for k in fields})


def describe_csv(path: str, dfield: str) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []
        n, first, last = 0, None, None
        for r in rd:
            n += 1
            dt = r.get(dfield) or ""
            if dt:
                first = dt if first is None or dt < first else first
                last = dt if last is None or dt > last else last
    return {"rows": n, "columns": fields, "first_date": first, "last_date": last}


# Category columns worth listing: codes, descriptions, types and categories (not ids, dates or amounts).
CATEGORY_COL = re.compile(r"(_cd|_desc|_type|_catg|_category|_nm|_name|_txt|_term|_security|reopening)$")
NOT_CATEGORY = re.compile(r"(_id|_date|_dt|_amt|_bal|_rate|_pct|_nbr)$")
MAX_VALUES = 40          # list a column's values when it has at most this many distinct ones
MAX_CODE_VALUES = 12     # columns with at most this many values form the code combinations


def value_summary(rows: list[dict], columns: list[str], types: dict | None = None) -> dict:
    """Distinct values (with counts) of the category columns, and the combinations of the low-cardinality code
    columns with an example description each. Shows which column holds a value before anyone filters on it
    (e.g. MTS Table 1: "Year-to-Date" rows are record_type_cd SL + data_type_cd T)."""
    types = types or {}
    cols = [c for c in columns if CATEGORY_COL.search(c) and not NOT_CATEGORY.search(c)
            and (types.get(c) or "STRING").upper() == "STRING"]
    blank = ("", None, "null")
    per_col, codes = {}, []
    for c in cols:
        counts: dict[str, int] = {}
        for r in rows:
            v = r.get(c)
            v = "(empty)" if v in blank else str(v)
            counts[v] = counts.get(v, 0) + 1
        if len(counts) <= MAX_VALUES:
            per_col[c] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        else:
            per_col[c] = {"distinct": len(counts), "examples": list(counts)[:8]}
        if 1 < len(counts) <= MAX_CODE_VALUES and c.endswith("_cd"):
            codes.append(c)
    desc = next((c for c in cols if c.endswith("_desc")), None)
    combos: dict[tuple, dict] = {}
    if codes:
        for r in rows:
            k = tuple("(empty)" if r.get(c) in blank else str(r.get(c)) for c in codes)
            e = combos.setdefault(k, {"rows": 0, "examples": []})
            e["rows"] += 1
            if desc and r.get(desc) not in blank and r.get(desc) not in e["examples"] and len(e["examples"]) < 3:
                e["examples"].append(r.get(desc))
    return {"rows": len(rows), "columns": per_col,
            "code_combinations": [{**dict(zip(codes, k)), "rows": v["rows"], f"e.g. {desc}" if desc else "e.g.":
                                   v["examples"]} for k, v in sorted(combos.items(), key=lambda kv: -kv[1]["rows"])]}


def values_info(endpoint: str, filters=None, limit: int = 20000) -> dict:
    """--fields ENDPOINT --values: download the rows under the filter (refuses above `limit`) and summarize them."""
    client = FiscalDataClient(verbose=False)
    api_f, local_f = split_filter(filters)
    m = client.meta(endpoint, api_f, date_field=None)
    total = int(m.get("total-count") or 0)
    if total > limit:
        raise FiscalDataError(f"{total:,} rows match; --values reads at most {limit:,}. Narrow the filter, e.g. "
                              "{\"record_date\": \"<one date>\"}", "bad_request")
    rows, _ = client.fetch_all(endpoint, None, api_f, None)
    rows = _apply_filter(rows, local_f)
    return {"endpoint": normalize_endpoint(endpoint), "filter": filters, **value_summary(
        rows, list((m.get("labels") or {}).keys()), m.get("dataTypes") or {})}


def write_package(spec: dict, out: str, results: dict, route: str):
    """fetch_spec.json + README.md next to the CSV files."""
    data_dir = ddir(out)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    qs = {q["name"]: q for q in dataset_queries(spec)}
    prov = {"source": SOURCE, "publisher": "U.S. Treasury Fiscal Data API", "base_url": BASE, "retrieved": now,
            "route": route, "fingerprint": fingerprint(spec), "spec": spec, "datasets": {}}
    for name, r in results.items():
        q = qs[name]
        p = os.path.join(data_dir, f"{name}.csv")
        info = describe_csv(p, q["date_field"])
        prov["datasets"][name] = {
            "endpoint": q["endpoint"], "filter": q["filter"], "local_filter": q["local_filter"],
            "sort": r.get("sort") or q["sort"],
            "date_field": q["date_field"], "query_url": r.get("url") or q["url_json"],
            "file": f"data/{SOURCE}/{name}.csv", "sha256": sha256(p), **info, "api_total_count": r.get("total"),
            "units": unit_hint(q["endpoint"], r.get("labels") or {k: k for k in info["columns"]}, r.get("types") or {},
                               r.get("formats") or {}),
            "labels": r.get("labels") or {}, "data_types": r.get("types") or {}, "data_formats": r.get("formats") or {}}
    prov["tables"] = [{"name": k, "file": f"{k}.csv", "rows": v["rows"]} for k, v in prov["datasets"].items()]
    with open(os.path.join(data_dir, "fetch_spec.json"), "w", encoding="utf-8") as f:
        json.dump(prov, f, indent=2)
    names = ", ".join(sorted({v["endpoint"] for v in prov["datasets"].values()}))
    citation = CITATION.format(datasets=names, date=now[:10])
    how = "downloaded by hand with the bundled links, then packaged with --ingest" if route == "manual_download" \
        else "Fiscal Data API"
    lines = [f"# Treasury Fiscal Data download: {spec.get('description', '')}", "",
             f"- Retrieved: {cpol.local_stamp(now)} ({how})",
             f"- Data cut-off (as_of): {spec.get('as_of') or 'none (latest available at retrieval)'}",
             f"- Source: {BASE}", "",
             "Values are exactly as published. The API returns every value as a string; its \"null\" is an empty "
             "cell here. Units differ by dataset: read the unit line under each file before scaling.", ""]
    for name, d in prov["datasets"].items():
        local = (["- Applied locally after the download (the API mishandles `&`, `,` or parentheses in these "
                  f"values): `{json.dumps(d['local_filter'], ensure_ascii=False)}`"] if d["local_filter"] else [])
        lines += [f"## {name}.csv", "", f"- Endpoint: `{d['endpoint']}`", f"- Filter: `{d['filter'] or 'none'}`",
                  *local,
                  f"- Rows: {d['rows']:,}" + (f" (API total-count{' before the local filter' if local else ''}: "
                                              f"{d['api_total_count']:,})"
                                              if isinstance(d['api_total_count'], int) else ""),
                  f"- {d['date_field']} range: {d['first_date']} to {d['last_date']}",
                  f"- Units: {d['units'] or 'see the field types below'}",
                  f"- SHA-256: `{d['sha256']}`", f"- Query: {d['query_url']}", "",
                  "| Field | Label | Type | Format |", "|---|---|---|---|"]
        for c in d["columns"]:
            lines.append(f"| `{c}` | {d['labels'].get(c, '')} | {d['data_types'].get(c, '')} | "
                         f"{d['data_formats'].get(c, '')} |")
        lines.append("")
        with open(os.path.join(data_dir, f"{name}.csv"), newline="", encoding="utf-8-sig") as fh:
            vs = value_summary(list(csv.DictReader(fh)), d["columns"], d["data_types"])
        if vs["columns"]:
            lines += ["### Values in this file", "",
                      "Filter on the column a value actually appears in (exact text, case-sensitive).", "",
                      "| Field | Values (rows) |", "|---|---|"]
            for c, v in vs["columns"].items():
                txt = (f"{v['distinct']:,} distinct, e.g. " + "; ".join(map(str, v["examples"]))) if "distinct" in v \
                    else "; ".join(f"{k} ({n:,})" for k, n in v.items())
                lines.append(f"| `{c}` | {txt.replace('|', '/')} |")
            lines.append("")
        if vs["code_combinations"]:
            keys = [k for k in vs["code_combinations"][0] if k != "rows"]
            lines += ["Code combinations:", "", "| " + " | ".join(keys) + " | rows |",
                      "|" + "---|" * (len(keys) + 1)]
            for cmb in vs["code_combinations"][:30]:
                lines.append("| " + " | ".join(
                    "; ".join(cmb[k]) if isinstance(cmb[k], list) else str(cmb[k]) for k in keys) + f" | {cmb['rows']:,} |")
            lines.append("")
    lines += ["## Citation", "", citation, "",
              "Fiscal Data is published by the U.S. government and is in the public domain; redistribution is "
              "permitted. Cite the source and the retrieval date, because Treasury revises some series.", ""]
    with open(os.path.join(data_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    core_package.copy_pack_lib(os.path.dirname(os.path.dirname(os.path.abspath(data_dir))), SOURCE)
    return prov, citation


# --------------------------------------------------------------------------- download
def empty_message(name: str, endpoint: str, filt) -> str:
    return (f"{name}: the filter {filter_string(filt) or '(none)'} matches no rows in {endpoint}. Filter values must "
            "match exactly (case, spelling, YYYY-MM-DD dates; a malformed date returns no rows, not an error), and "
            "the value must be in the column you filter on (MTS: data_type_cd D/S/T vs record_type_cd). See which "
            f"column holds which value: fetch.py --fields {endpoint} --filter '{{\"record_date\": \"<one date>\"}}' "
            "--values. "
            "If no rows is a valid result, set \"allow_empty\": true on the dataset.")


def download(spec: dict, out: str, client: FiscalDataClient) -> dict:
    data_dir = ddir(out)
    os.makedirs(data_dir, exist_ok=True)
    results, warnings = {}, []
    for d in spec["datasets"]:
        name, ep, fields = d["name"], d["endpoint"], d.get("fields")
        filt = effective_filter(spec, d)
        api_f, local_f = split_filter(filt)                      # values the API mishandles are filtered here
        full = client.meta(ep, api_f, date_field=None)           # all columns: labels, types, row count
        labels = full.get("labels") or {}
        df = date_field(d)
        if df not in labels:
            dates = [k for k, t in (full.get("dataTypes") or {}).items() if t == "DATE"]
            raise FiscalDataError(f"{name}: {ep} has no {df!r} column; set \"date_field\" to one of {dates} "
                                  "and use it in the filter", "bad_request")
        unknown = [f for f in local_f if f not in labels]
        if unknown:
            raise FiscalDataError(f"{name}: filter fields not in {ep}: {unknown}. Fields: {list(labels)}", "bad_request")
        if int(full.get("total-count") or 0) == 0 and not d.get("allow_empty"):
            raise FiscalDataError(empty_message(name, ep, filt), "bad_request")
        if fields:
            fields = list(fields) + [f for f in local_f if f not in fields]   # the local filter needs its columns
            unknown = [f for f in fields if f not in labels]
            if unknown:
                raise FiscalDataError(f"{name}: fields not in {ep}: {unknown}. Fields: {list(labels)}", "bad_request")
            sub = client.meta(ep, api_f, date_field=None, fields=fields)
            if int(sub.get("total-count") or 0) != int(full.get("total-count") or 0):
                raise FiscalDataError(
                    f"{name}: requesting only {fields} returns {sub.get('total-count')} rows instead of "
                    f"{full.get('total-count')}: the API grouped and summed rows (it does this when the fields leave "
                    "out the columns that identify a row, and the sums can mix totals with details). Add the "
                    "row-identity columns (descriptions, src_line_nbr, type codes) or drop \"fields\".", "bad_request")
        sort = default_sort(d, labels)
        rows, meta = client.fetch_all(ep, fields, api_f, sort)
        if local_f:
            n_api = len(rows)
            rows = _apply_filter(rows, local_f)
            say(f"[fiscaldata] {name}: filtered locally on {sorted(local_f)}: kept {len(rows):,} of {n_api:,} rows")
            if not rows and not d.get("allow_empty"):
                raise FiscalDataError(empty_message(name, ep, filt), "bad_request")
        cols = list(fields or labels.keys())
        tmp = os.path.join(data_dir, f".{name}.csv.part")
        write_csv(tmp, rows, cols)
        os.replace(tmp, os.path.join(data_dir, f"{name}.csv"))
        if not rows:                        # only reached with "allow_empty": true
            warnings.append(f"{name}: the query matched no rows (allowed by \"allow_empty\").")
        results[name] = {"total": int(meta.get("total-count") or len(rows)), "labels": labels,
                         "types": full.get("dataTypes"), "formats": full.get("dataFormats"), "sort": sort,
                         "url": build_url(ep, query_params(fields, api_f, sort))}
    prov, citation = write_package(spec, out, results, "api")
    for w in warnings:
        say(f"[fiscaldata] WARNING: {w}")
    return {"status": "ok", "source": SOURCE, "route": "api", "out": os.path.abspath(data_dir),
            "data_dir": os.path.abspath(data_dir), "warnings": warnings,
            "files": {k: {"path": os.path.join(os.path.abspath(data_dir), f"{k}.csv"), "rows": v["rows"],
                          "first_date": v["first_date"], "last_date": v["last_date"], "units": v["units"]}
                      for k, v in prov["datasets"].items()},
            "readme": os.path.join(os.path.abspath(data_dir), "README.md"), "citation": citation,
            "read_next": os.path.join(HERE, "source.md")}


# --------------------------------------------------------------------------- manual route
MANUAL_TEMPLATE = """# Download the Treasury data by hand

The assistant could not reach the Fiscal Data API from its environment (@REASON@). The data are public and
need no account or key. Download the files below on your own computer, then upload them to the chat.
The assistant will check and package them.

@FILES@

## Option A: one command (recommended)

@RUN@
Each request returns at most 10,000 rows. If a file has exactly 10,000 data rows, there is more: get
the next pages with Option B.

## Option B: in a web browser
Open each link. The browser saves a `.csv` file named after the date and time. Rename it to the
name in the heading (for example `@FIRST@.csv`) before uploading.

Each link returns at most 10,000 rows. If a file has exactly 10,000 data rows, there is more: open
the same link with `page%5Bnumber%5D=2` (then `=3`, and so on until a file has fewer than 10,000
rows), save the pages as `<name>_2.csv`, `<name>_3.csv`, and upload every page.

## Option C: the dataset web page
The dataset page (listed under each file above) has a **Download** button: pick the table, the date
range and **CSV**, then unzip. This gives the whole table without the 10,000-row page limit, but
The assistant then has to apply the filter itself, so prefer the links when they work.

## Upload
Upload the `.csv` (or `.zip`) files as they are. Do not open and re-save them in Excel first: Excel
can change dates and long numbers.
"""

SEND_BUNDLE = ("Send INSTRUCTIONS.md, both download scripts (download_scripts: download_treasury.sh for macOS/Linux, "
               "download_treasury.ps1 for Windows) and spec.json (request_spec, the request the scripts have built "
               "in) with your file-sending tool when available, and print their full paths. Give the user the "
               "one command for their system (user_commands; both when unsure); no key is needed. Wait for the "
               "upload, then run next_command.")


def _bundle_fields(bundle: str) -> dict:
    """What a route JSON says about the hand-download bundle."""
    spec = os.path.join(bundle, "spec.json")
    return {"manual_bundle": bundle, "instructions": os.path.join(bundle, "INSTRUCTIONS.md"),
            "urls": os.path.join(bundle, "download_urls.txt"),
            "download_scripts": {k: os.path.join(bundle, "download_treasury" + e) for k, e in manual.SCRIPT_KINDS.items()},
            "request_spec": spec if os.path.exists(spec) else None, "user_commands": manual.run_lines("download_treasury")}


def dataset_page(endpoint: str) -> str:
    for key, slug in DATASET_PAGES:
        if key in "/" + endpoint:
            return f"https://fiscaldata.treasury.gov/datasets/{slug}/"
    return "https://fiscaldata.treasury.gov/datasets/ (search for the table)"


def write_manual_bundle(spec: dict, out: str, reason: str, spec_path: str = "<out>/spec.json") -> str:
    d = os.path.join(os.path.abspath(out), "manual_download", SOURCE)
    os.makedirs(d, exist_ok=True)
    blocks, items = [], []
    for q in dataset_queries(spec):
        note = (f"- The assistant applies the rest of the filter after upload: `{json.dumps(q['local_filter'])}`\n"
                if q["local_filter"] else "")
        blocks.append(f"### {q['name']}.csv\n- Endpoint: `{q['endpoint']}`\n- Filter: `{q['filter'] or 'none'}`\n"
                      f"{note}- Link: {q['url_csv']}\n- Dataset page: {dataset_page(q['endpoint'])}\n")
        items.append({"source": SOURCE, "name": q["name"], "filename": f"{q['name']}.csv", "url": q["url_csv"]})
    manual.write_scripts(d, items, "download_treasury")
    has_spec = os.path.isfile(spec_path)
    if has_spec and os.path.abspath(spec_path) != os.path.abspath(os.path.join(d, "spec.json")):
        shutil.copyfile(spec_path, os.path.join(d, "spec.json"))     # the request, for reference
    run = "\n".join(manual.run_section("download_treasury", spec_name="spec.json" if has_spec else ""))
    text = (MANUAL_TEMPLATE.replace("@REASON@", reason).replace("@FILES@", "\n".join(blocks))
            .replace("@FIRST@", spec["datasets"][0]["name"]).replace("@RUN@", run))
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(d, "download_urls.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(f"{q['name']}.csv\t{q['url_csv']}" for q in dataset_queries(spec)) + "\n")
    return d


def _collect_csvs(paths: list[str], tmpdir: str) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(r, f) for r, _, fs in os.walk(p) for f in fs
                      if f.lower().endswith((".csv", ".zip"))]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise FiscalDataError(f"not found: {p}", "bad_request")
    out = []
    for f in files:
        if f.lower().endswith(".zip"):
            with zipfile.ZipFile(f) as z:
                for m in z.namelist():
                    if m.lower().endswith(".csv") and ".." not in m and not m.startswith(("/", "\\")):
                        target = os.path.join(tmpdir, os.path.basename(m))
                        with z.open(m) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        out.append(target)
        else:
            out.append(f)
    return out


def parse_filter_string(flt: str) -> dict:
    """API filter syntax ("record_date:gte:2020-01-01,security_type_desc:in:(Bills,Notes)") as a friendly filter.
    Commas inside in (...) belong to the list."""
    parts, depth, cur = [], 0, ""
    for ch in flt:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out: dict = {}
    for part in filter(None, (p.strip() for p in parts)):
        field, op, val = (part.split(":", 2) + ["", ""])[:3]
        if op not in ("eq", "lt", "lte", "gt", "gte", "in"):
            raise FiscalDataError(f"filter condition {part!r}: the operator must be one of eq, lt, lte, gt, gte, in",
                                  "bad_request")
        if op == "in":
            val = [v.strip() for v in val.strip().strip("()").split(",")]
        out.setdefault(field.strip(), {})[op] = val
    return out


def _apply_filter(rows: list[dict], flt) -> list[dict]:
    """Apply a friendly filter (or an API filter string) locally (website downloads hold the whole table). String
    compare works for YYYY-MM-DD dates; numbers compare as floats."""
    if isinstance(flt, str):
        flt = parse_filter_string(flt)
    if not flt:
        return rows

    def cmp(v, op, x):
        try:
            a, b = float(v), float(x)
        except (TypeError, ValueError):
            a, b = str(v), str(x)
        return {"eq": a == b, "lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]
    keep = []
    for r in rows:
        ok = True
        for field, cond in flt.items():
            v = r.get(field)
            conds = cond if isinstance(cond, dict) else ({"in": cond} if isinstance(cond, list) else {"eq": cond})
            for op, x in conds.items():
                ok &= (str(v) in [str(i) for i in x]) if op == "in" else (v not in (None, "", "null") and cmp(v, op, x))
        if ok:
            keep.append(r)
    return keep


def ingest(paths: list[str], spec: dict, out: str) -> dict:
    """Package files the user downloaded by hand: API CSV links (filtered already, maybe several pages) or a
    website download (whole table: the spec's filter is applied here). Headers must be API field names."""
    data_dir = ddir(out)
    os.makedirs(data_dir, exist_ok=True)
    results, warnings = {}, []
    paths = paths or upload_dirs()
    if not paths:
        raise FiscalDataError("--ingest needs the downloaded files or their folder (and DATA_ANALYST_UPLOADS is not set "
                              "here)", "bad_request")
    with tempfile.TemporaryDirectory() as tmp:
        files = _collect_csvs(paths, tmp)
        for d in spec["datasets"]:
            want = set(d.get("fields") or [])
            mine = [f for f in files if re.match(rf"^{re.escape(d['name'])}(_p?\d+)?\.csv$", os.path.basename(f), re.I)]
            if not mine and len(spec["datasets"]) == 1:
                mine = files
            if not mine:
                raise FiscalDataError(f"no uploaded file for dataset {d['name']!r}; expected {d['name']}.csv "
                                      f"(pages: {d['name']}_2.csv ...). Got: {[os.path.basename(f) for f in files]}",
                                      "bad_request")
            rows, header = [], None
            for f in sorted(mine):
                with open(f, newline="", encoding="utf-8-sig") as fh:
                    rd = csv.DictReader(fh)
                    h = rd.fieldnames or []
                    if header and h != header:
                        raise FiscalDataError(f"{f}: columns differ from the other files of {d['name']}", "bad_request")
                    header = h
                    for r in rd:            # API CSV guards against formula injection: '-123.45 -> -123.45
                        rows.append({k: (v[1:] if isinstance(v, str) and re.match(r"^'[-+=@]", v) else v)
                                     for k, v in r.items()})
            if any(" " in c for c in header or []):
                raise FiscalDataError(f"{d['name']}: the headers are display labels ({header[:3]} ...), not API field "
                                      "names. Ask the user to use the links in INSTRUCTIONS.md.", "bad_request")
            if want - set(header or []):
                warnings.append(f"{d['name']}: requested fields missing from the upload: {sorted(want - set(header))}")
            n0 = len(rows)
            rows = _apply_filter(rows, effective_filter(spec, d))
            if len(rows) < n0:
                warnings.append(f"{d['name']}: applied the spec filter locally: kept {len(rows):,} of {n0:,} rows "
                                "(a whole-table website download)")
            if not rows and not d.get("allow_empty"):
                raise FiscalDataError(empty_message(d["name"], d["endpoint"], effective_filter(spec, d))
                                      + f" ({n0:,} rows were uploaded before filtering.)", "bad_request")
            seen, uniq = set(), []
            for r in rows:                      # a page uploaded twice would double count
                key = tuple(r.get(c) for c in header)
                if key not in seen:
                    seen.add(key)
                    uniq.append(r)
            if len(uniq) != len(rows):
                warnings.append(f"{d['name']}: dropped {len(rows) - len(uniq)} duplicate rows (overlapping pages)")
            uniq.sort(key=lambda r: tuple(str(r.get(c, "")) for c in default_sort(d, {c: c for c in header}).split(",")))
            write_csv(os.path.join(data_dir, f"{d['name']}.csv"), uniq, header)
            if n0 and n0 % MAX_PAGE == 0:
                warnings.append(f"{d['name']}: {n0:,} rows uploaded, a multiple of the 10,000-row page size; a page "
                                "may be missing. Ask the user whether there is one more page.")
            results[d["name"]] = {"total": None, "labels": {}, "types": {}, "formats": {}}
    prov, citation = write_package(spec, out, results, "manual_download")
    for w in warnings:
        say(f"[fiscaldata] WARNING: {w}")
    return {"status": "ok", "source": SOURCE, "route": "manual_download", "data_dir": os.path.abspath(data_dir),
            "warnings": warnings, "files": {k: v["rows"] for k, v in prov["datasets"].items()},
            "readme": os.path.join(os.path.abspath(data_dir), "README.md"), "citation": citation}


# --------------------------------------------------------------------------- route check
def route_check(spec: dict, out: str, env: str, spec_path: str, refresh: bool = False) -> dict:
    out = os.path.abspath(out)
    r = {"status": "route_check", "source": SOURCE, "environment": env, "os": sys.platform,
         "python": sys.version.split()[0], "api_reachable": None, "detail": "", "existing_data": None,
         "route": None, "message": "", "action": "", "next_command": ""}
    ex = existing_data(out, spec)
    r["existing_data"] = ex

    def done(route, message, action, next_command=""):
        r.update(route=route, message=message, action=action, next_command=next_command)
        return r

    if ex and ex["files_ok"] and ex["spec_matches"] and not refresh:
        stale = "" if spec.get("as_of") else (" No as_of cut-off is set, so newer records may exist; pass --refresh "
                                              "if the user wants the latest data.")
        return done("reuse_existing", f"The data for this spec are already in {ddir(out)} "
                    f"(retrieved {cpol.local_stamp(ex['retrieved'])}).{stale}", "Skip the download and continue with the analysis.")
    p = FiscalDataClient(timeout=20, verbose=False).ping()
    r.update(api_reachable=p["reachable"], detail=p["detail"])
    if p["reachable"]:
        counts, problems = {}, []
        client = FiscalDataClient(timeout=30, verbose=False)
        for d in spec["datasets"]:                      # one tiny request per dataset: does the filter match?
            try:                                        # count under the API part (a local part filters later)
                counts[d["name"]] = int(client.meta(d["endpoint"], split_filter(effective_filter(spec, d))[0],
                                                    date_field=None).get("total-count") or 0)
                if counts[d["name"]] == 0 and not d.get("allow_empty"):
                    problems.append(empty_message(d["name"], d["endpoint"], effective_filter(spec, d)))
            except FiscalDataError as e:
                if e.kind == "network":
                    raise
                counts[d["name"]] = None
                problems.append(f"{d['name']}: {e}")
        r["dataset_counts"] = counts
        later = [d["name"] for d in spec["datasets"] if split_filter(effective_filter(spec, d))[1]]
        if later:                                       # counts are before the local part of the filter
            r["dataset_counts_note"] = (f"{', '.join(later)}: counted before the conditions applied locally after the "
                                        "download (values with &, commas or parentheses); the fetch keeps fewer rows")
        if problems:
            return done("fix_spec", "The API answers, but the spec needs fixing before the download: "
                        + " | ".join(problems),
                        "Fix spec.json (check values with --fields ... --values), then run --check again.")
        return done("api", "The Fiscal Data API answers from here (no key is needed).",
                    "Run next_command. It takes seconds to a minute.", cli(spec_path, out))
    ingest_cmd = cli(spec_path, out, "--ingest", "<the uploaded files or their folder>")
    if env in ("local", "ssh"):
        bundle = write_manual_bundle(spec, out, "network error: " + p["detail"][:120], spec_path)
        r.update(_bundle_fields(bundle))
        return done("report_error",
                    f"This {env} machine cannot reach api.fiscaldata.treasury.gov ({p['detail'][:160]}). Check the "
                    "internet connection, VPN and proxy (set HTTPS_PROXY if the network needs one), then ask again. "
                    f"Or run the one-command download script in {bundle} (see INSTRUCTIONS.md) in your own terminal "
                    "and tell the assistant where the files are.",
                    "Tell the user both options. If they fix the network, run --check again. If they download by "
                    "hand, wait for the files, then run next_command.", ingest_cmd)
    bundle = write_manual_bundle(spec, out, f"no network route from this {env} environment", spec_path)
    r.update(_bundle_fields(bundle))
    if env == "cloud":
        return done("cloud_allowlist",
                    "This cloud session cannot reach api.fiscaldata.treasury.gov; its network policy probably blocks "
                    "the domain. Either allow api.fiscaldata.treasury.gov in the cloud environment's network settings "
                    f"and start the session again, or download the files by hand ({os.path.join(bundle, 'INSTRUCTIONS.md')}).",
                    "Explain both options. If the user changes the allowlist, stop and let them restart. If they "
                    "download by hand: " + SEND_BUNDLE, ingest_cmd)
    return done("manual_download",
                "This environment cannot reach the Fiscal Data API. On claude.ai an organization admin can allow "
                "api.fiscaldata.treasury.gov in the network settings; otherwise the download links (CSV, no key "
                f"needed), a one-command download script and the steps are in {os.path.join(bundle, 'INSTRUCTIONS.md')}: "
                "download the files on your own computer and upload them here.",
                SEND_BUNDLE + " Paste the links in the chat too.", ingest_cmd)


# --------------------------------------------------------------------------- discovery
def fields_info(endpoint: str, filters=None, dfield: str = "record_date") -> dict:
    api_f, local_f = split_filter(filters)
    c = FiscalDataClient(verbose=False)
    m = c.meta(endpoint, api_f, date_field=dfield)
    types = m.get("dataTypes") or {}
    dates = [k for k, t in types.items() if t == "DATE"]
    if not m.get("first_row") and dates and dfield not in dates:      # e.g. I bonds: earning_period_start
        m = c.meta(endpoint, api_f, date_field=dates[0])
        types = m.get("dataTypes") or types
    note = ({"note": f"total_count ignores {sorted(local_f)}: the API mishandles those values (&, comma or parentheses); "
             "fetch.py applies that part locally. Use --values for exact counts."} if local_f else {})
    return {"endpoint": normalize_endpoint(endpoint), **note, "total_count": m.get("total-count"),
            "units": unit_hint(normalize_endpoint(endpoint), m.get("labels") or {}, types, m.get("dataFormats") or {}),
            "date_columns": [k for k, t in types.items() if t == "DATE"],
            "fields": [{"field": k, "label": v, "type": types.get(k), "format": (m.get("dataFormats") or {}).get(k)}
                       for k, v in (m.get("labels") or {}).items()],
            "first_row": m.get("first_row"), "last_row": m.get("last_row")}


def load_catalog() -> list[dict]:
    with open(CATALOG_FILE, encoding="utf-8") as f:
        return json.load(f)["endpoints"]


def search_bundled(words: list[str]) -> list[dict]:
    """catalog.json: endpoint registry merged with the live catalog snapshot and the curated
    guidance (rows to use, stock/flow, units, pitfalls). Stale entries (HTTP 404) are listed last."""
    hits = []
    for e in load_catalog():
        g = e.get("guidance") or {}
        text = " ".join(str(x) for x in (e.get("dataset"), e.get("table"), e.get("description"), e.get("endpoint"),
                                         g.get("question"), g.get("rows_to_use"))).lower()
        if all(w in text for w in words):
            hits.append(e)
    return sorted(hits, key=lambda e: ("status" in e, "guidance" not in e, e["endpoint"]))


def search_catalog(term: str) -> tuple[list[dict], str]:
    """Search the live dataset catalog (all words must match); fall back to the bundled catalog.json."""
    words = term.lower().split()
    hits = []
    try:
        cat = json.loads(FiscalDataClient(verbose=False, retries=1).get_raw(CATALOG_URL).decode("utf-8"))
        guided = {e["endpoint"]: e.get("guidance") for e in load_catalog() if e.get("guidance")}
        for ds in cat if isinstance(cat, list) else cat.get("data", []):
            for api in ds.get("apis", []) or []:
                if not api.get("endpoint_txt"):
                    continue
                text = " ".join(str(x) for x in (ds.get("title"), ds.get("short_description"), api.get("table_name"),
                                                 api.get("table_description"), api.get("endpoint_txt"))).lower()
                if all(w in text for w in words):
                    ep = api["endpoint_txt"].split("/fiscal_service/")[-1].strip("/")
                    hits.append({"dataset": ds.get("title"), "table": api.get("table_name"), "endpoint": ep,
                                 "frequency": api.get("update_frequency"), "earliest": api.get("earliest_date"),
                                 "latest": api.get("latest_date"), "rows": api.get("row_count"),
                                 "page": f"https://fiscaldata.treasury.gov/datasets/{ds.get('dataset_path', '').strip('/')}/",
                                 "description": (api.get("table_description") or "")[:240],
                                 **({"guidance": guided[ep]} if ep in guided else {})})
        hits.sort(key=lambda h: "guidance" not in h)
        return hits, "live catalog"
    except (FiscalDataError, ValueError, AttributeError) as e:
        say(f"[fiscaldata] live catalog unavailable ({str(e)[:100]}); searching the bundled catalog.json")
    return search_bundled(words), "bundled catalog.json (snapshot; confirm with --fields)"


# --------------------------------------------------------------------------- main
def utf8_stdio():
    """Windows pipes default to cp1252, so printing "−" or "≥" would crash: write UTF-8 instead."""
    for s in (sys.stdout, sys.stderr):
        if (getattr(s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def main():
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="fetch.py --source treasury", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out", default="out")
    ap.add_argument("--check", action="store_true", help="pre-flight only: print the route (no download)")
    ap.add_argument("--refresh", action="store_true", help="download again even if data/treasury/ holds this spec")
    ap.add_argument("--force", action="store_true", help="the same as --refresh (the dispatcher's name)")
    ap.add_argument("--fields", metavar="ENDPOINT", help="fields, labels, types, units and row count of an endpoint")
    ap.add_argument("--filter", help="JSON filter for --fields (count rows / first and last row under a filter)")
    ap.add_argument("--date-field", default="record_date", help="date column for --fields first/last row")
    ap.add_argument("--values", action="store_true",
                    help="with --fields: distinct values of the code/description columns under --filter")
    ap.add_argument("--search", metavar="TERM", help="find endpoints whose title or description has every word")
    ap.add_argument("--endpoints", action="store_true", help="the curated endpoints with rows, units and pitfalls")
    ap.add_argument("--ping", action="store_true", help="does the API answer from here?")
    ap.add_argument("--manual-bundle", action="store_true", help="write the download links and instructions only")
    ap.add_argument("--ingest", nargs="*", metavar="PATH", help="CSV/ZIP files (or a folder) downloaded by hand")
    ap.add_argument("--context", choices=["cpi", "gdp"], help="CPI-U or GDP context series into OUT/data/treasury/")
    ap.add_argument("--start", type=int, default=1990, help="with --context: the first FISCAL year needed")
    ap.add_argument("--env", choices=["auto", *ENVIRONMENTS], default="auto")
    ap.add_argument("--dry-run", action="store_true", help="print the query URLs only")
    a = ap.parse_args()
    env = detect_environment(None if a.env == "auto" else a.env)
    refresh = a.refresh or a.force
    spec = None
    spec_path = os.path.abspath(a.spec or os.path.join(a.out, "spec.json"))
    try:
        if a.context:
            import context
            res, code = context.fetch(a.context, a.out, a.start)
            emit(res, code)
        if a.endpoints:
            eps = [{k: e[k] for k in ("endpoint", "table", "frequency", "earliest_date", "guidance") if k in e}
                   for e in load_catalog() if e.get("guidance")]
            emit({"source": SOURCE, "curated": eps, "all_endpoints": len(load_catalog()),
                  "note": "--search WORDS finds any of the endpoints; docs/datasets.md explains the rows to use",
                  "read": os.path.join(HERE, "source.md")})
        if a.ping:
            p = FiscalDataClient(timeout=20, verbose=False).ping()
            emit({"source": SOURCE, "auth_ok": p["reachable"], **p}, 0 if p["reachable"] else routes.EXIT_BLOCKED)
        if a.fields and a.values:
            emit(values_info(a.fields, json.loads(a.filter) if a.filter else None))
        if a.fields:
            emit(fields_info(a.fields, json.loads(a.filter) if a.filter else None, a.date_field))
        if a.search:
            hits, where = search_catalog(a.search)
            emit({"source": SOURCE, "query": a.search, "searched": where, "results": hits[:SEARCH_LIMIT],
                  **({"note": f"{len(hits)} matches; showing {SEARCH_LIMIT}: add words to narrow"}
                     if len(hits) > SEARCH_LIMIT else {})})
        spec = load_spec(spec_path)
        os.makedirs(a.out, exist_ok=True)
        here_spec = os.path.join(a.out, "spec.json")    # a copy beside the data, never over another source's spec
        in_plan = os.path.basename(os.path.dirname(os.path.abspath(spec_path))) == "specs"   # fetch.py --plan
        if (not in_plan and os.path.abspath(spec_path) != os.path.abspath(here_spec)
                and not os.path.exists(here_spec)):
            shutil.copyfile(spec_path, here_spec)
        if a.dry_run:
            emit({"source": SOURCE, "queries": dataset_queries(spec)})
        if a.manual_bundle:
            d = write_manual_bundle(spec, a.out, "a manual download was requested", spec_path)
            emit({"status": "ok", "source": SOURCE, **_bundle_fields(d)})
        if a.ingest is not None:
            emit(ingest(a.ingest, spec, a.out))
        if a.check:
            r = route_check(spec, a.out, env, spec_path, refresh)
            say(f"[fiscaldata] route: {r['route']} ({env})\n[fiscaldata] {r['message']}")
            emit(r, routes.EXIT_SPEC if r["route"] in ("fix_spec", "report_error") else 0)
        ex = existing_data(a.out, spec)
        if ex and ex["files_ok"] and ex["spec_matches"] and not refresh:
            say(f"[fiscaldata] reusing {ddir(a.out)} (retrieved {cpol.local_stamp(ex['retrieved'])}); pass --refresh to download again")
            emit({"status": "ok", "source": SOURCE, "route": "reuse_existing", "data_dir": ddir(a.out), **ex})
        emit(download(spec, a.out, FiscalDataClient()))
    except FiscalDataError as e:
        if e.kind == "network":
            say(f"CANNOT REACH THE FISCAL DATA API: {e}")
            r = route_check(spec, a.out, env, spec_path) if spec else {"route": "report_error", "detail": str(e)}
            r.update(status="blocked", source=SOURCE, error=str(e))
            emit(r, routes.EXIT_BLOCKED)
        emit({"status": "error", "source": SOURCE, "kind": e.kind, "message": str(e)}, routes.EXIT_SPEC)
    except (OSError, ValueError) as e:
        emit({"status": "error", "source": SOURCE, "kind": "spec", "message": f"{type(e).__name__}: {e}"},
             routes.EXIT_SPEC)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        print(json.dumps({"status": "error", "source": SOURCE, "kind": "unexpected",
                          "message": redact(traceback.format_exc()[-1500:])}))
        sys.exit(routes.EXIT_UNEXPECTED)

#!/usr/bin/env python
"""Get OPM Federal Workforce Data (FWD) files for an analysis, by the best route where this runs
(deep pack of the data-analyst skill).

Run through the dispatcher: python "<skill>/scripts/fetch.py" --source opm <arguments below>
    <out>/spec.json --out <out> --check      # pick the route; downloads nothing
    <out>/spec.json --out <out>              # download (or reuse), filter, package
    <out>/spec.json --out <out> --dry-run    # list the files it would fetch
    <out>/spec.json --out <out> --manual-bundle   # instructions for a manual download
    <out>/spec.json --out <out> --ingest [files, folder or ZIP]   # package hand-downloaded files
                                             # (no paths: the DATA_ANALYST_UPLOADS folders)
    --list employment                        # periods available (current versions)
The same file also runs on its own (python fetch.py spec.json --out .) from a manual bundle, which
carries a copy of the skill's core modules.

spec.json:
{
  "description": "Separations by agency, FY2025",
  "datasets": {
    "employment":  {"from": "2015-09", "to": "2025-09", "months": [9]},   # September snapshots only
    "separations": {"from": "2024-10", "to": "latest"}                    # or "periods": ["2025-09", ...]
  },
  "columns": ["agency_code", "agency", "separation_category", "age_bracket"],   # optional; default all
  "filters": {"department_code": ["VA", "HS"]},       # optional; keep rows whose value is in the list
  "aggregate": {"by": ["agency_code", "agency"]},     # optional; counts + pay sums instead of microdata
  "pin": {"employment_202509": 4}                     # optional; exact versions (written by --pin)
}
Always kept: count, snapshot_yyyymm (employment), personnel_action_effective_date_yyyymm (actions),
plus source_file and file_yyyymm added by this script. A requested column missing from an older file
is filled with nulls and listed in the manifest (files before about 2011 have 31 of the 64 columns).

Formats: the API serves Parquet only; reading it needs one Parquet engine (pyarrow, else fastparquet,
duckdb or polars). --ingest also takes the website's .TXT / .JSON downloads (plain, .gz or .zip) and a
data/ folder built elsewhere; neither needs a Parquet engine (see opm_io.py).

Output in <out>/data/opm/: opm_<dataset>.parquet when a Parquet writer (pyarrow or fastparquet) exists,
opm_<dataset>.csv up to 1,000,000 rows, and opm_<dataset>.csv.gz for larger tables when no writer exists;
manifest.json (every source file, version, publish date, SHA-256, rows), request.json (core provenance),
data_dictionary.csv, README.md (citation) and spec_used.json. Raw files are cached in $OPM_CACHE_DIR or
<cache root>/opm-fwd (core.env.cache_root(): ~/.cache/data-analyst, a temp folder in sandboxes), keyed by
file name (which includes the version), for policy.json cache.ttl_hours; --no-cache deletes them.

--check prints JSON: {route, source, environment, message, action, next_command, plan}. Routes (shared
names, core/routes.py):
  reuse_existing   <out>/data/opm already holds this exact request
  api              data.opm.gov answers: run next_command (in the background when est_minutes > 2)
  install_deps     pandas or a Parquet engine missing: run next_command, else fallback_commands, else
                   the manual bundle (CSV data/ folder or the website's TXT/JSON)
  fix_spec         a requested period or dataset does not exist: fix spec.json
  cloud_allowlist  a cloud session cannot reach data.opm.gov: allow it, or the manual bundle
  manual_download  data.opm.gov cannot be reached from here: the bundle in <out>/manual_download/opm
  report_error     data.opm.gov answers with an error (5xx/429), or this computer cannot reach it
Exit codes: 0 ok, 2 bad spec, 3 dependency missing, 4 blocked (no route to the API).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(SKILL, "scripts"), HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from core import keys, manual, package as core_package, packfiles, routes  # noqa: E402
from core.env import ENVIRONMENTS, cache_root, detect_environment, upload_dirs  # noqa: E402
from core.http import SOURCES  # noqa: E402
from opm_api import BASE, DATASETS, OpmClient, OpmError, current_index  # noqa: E402
import opm_io  # noqa: E402

SOURCE = "opm"
EST_MB = {"employment": 70, "accessions": 1, "separations": 2}
PAY_COLS = ("annualized_adjusted_basic_pay", "work_schedule_code", "pay_basis_code")   # read for aggregate pay sums
ALWAYS = {"employment": ["count", "snapshot_yyyymm"],
          "accessions": ["count", "personnel_action_effective_date_yyyymm"],
          "separations": ["count", "personnel_action_effective_date_yyyymm"]}
CSV_MAX_ROWS = 1_000_000
CITATION = ("U.S. Office of Personnel Management, Federal Workforce Data (FWD), Enterprise Human Resources "
            "Integration (EHRI) {datasets} files, accessed {date} through the FWD API "
            "(https://data.opm.gov/get-data/api-access).")
# files that travel with a manual bundle so Option A runs without the skill installed
BUNDLE_CODE = ("fetch.py", "opm_api.py", "opm_io.py", "policy.json")
DICT_PATHS = (os.path.join(HERE, "lib", "data_dictionary.csv"), os.path.join(HERE, "data_dictionary.csv"))


def emit(obj: dict, code: int = 0):
    print(keys.redact(json.dumps({"source": SOURCE, **obj}, indent=2, default=str)))
    sys.exit(code)


def data_dir(out: str) -> str:
    return core_package.data_dir(out, SOURCE)


def bundle_dir(out: str) -> str:
    return os.path.join(os.path.abspath(out), "manual_download", SOURCE)


def policy() -> dict:
    try:
        return packfiles.read_json(HERE, "policy.json") or {}
    except (OSError, ValueError):
        return {}


def cache_ttl_hours() -> float:
    return float(((policy().get("cache") or {}).get("ttl_hours")) or 720)


def dictionary_path() -> str | None:
    return next((p for p in DICT_PATHS if os.path.exists(p)), None)


def deps_ok(need_parquet: bool = True) -> tuple[bool, str]:
    """pandas is always needed; a Parquet engine only to read API files or uploaded .parquet files."""
    caps = opm_io.capabilities()
    if not caps["pandas"]:
        return False, "pandas is not installed"
    if need_parquet and not caps["parquet_engine"]:
        return False, "no Parquet reader is installed (pyarrow, fastparquet, duckdb or polars)"
    return True, ""


def raw_cache_root() -> str:
    return os.environ.get("OPM_CACHE_DIR") or os.path.join(cache_root(), "opm-fwd")


def cache_dir(no_cache: bool, out: str) -> str:
    d = os.path.join(data_dir(out), "_raw_tmp") if no_cache else raw_cache_root()
    os.makedirs(d, exist_ok=True)
    return d


def cached_fresh(path: str) -> bool:
    """A cached raw file is used while it is younger than policy cache.ttl_hours (file names carry the
    version, so a fresh copy is only needed when OPM's cleanup or the ttl says so)."""
    try:
        return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < cache_ttl_hours() * 3600
    except OSError:
        return False


# ---------------------------------------------------------------- spec
def ym(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-?(\d{1,2})", str(s).strip())
    if not m:
        raise ValueError(f"period {s!r}: use YYYY-MM")
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        raise ValueError(f"period {s!r}: month must be 1-12")
    return y, mo


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        spec = json.load(f)
    ds = spec.get("datasets")
    if isinstance(ds, list):                       # shorthand: ["employment"] + top-level periods
        ds = {d: {k: spec[k] for k in ("from", "to", "months", "periods") if k in spec} for d in ds}
        spec["datasets"] = ds
    if not isinstance(ds, dict) or not ds:
        raise ValueError('spec needs "datasets": {"employment": {"from": "2024-09", "to": "2025-09"}, ...}')
    bad = [d for d in ds if d not in DATASETS]
    if bad:
        raise ValueError(f"unknown dataset(s) {bad}; use {list(DATASETS)}")
    for col, vals in (spec.get("filters") or {}).items():
        nums = [v for v in (vals if isinstance(vals, list) else [vals]) if not isinstance(v, str)]
        if nums:      # codes are strings with leading zeros: 8 would never match "08"
            raise ValueError(f'filter {col!r}: values must be strings exactly as in the data '
                             f'(for example "08" or "0343", not {nums[0]!r})')
    agg = spec.get("aggregate")
    if agg and spec.get("columns"):           # aggregate keeps only its "by" columns: say so before data is lost
        auto = {c for cols in ALWAYS.values() for c in cols} | set(PAY_COLS)
        lost = [c for c in spec["columns"] if c not in (agg.get("by") or []) and c not in auto]
        if lost:
            raise ValueError(f"aggregate keeps only the aggregate.by columns, so {lost} would be dropped: add them "
                             "to aggregate.by (they stay empty for datasets without them) or remove them from columns")
    return spec


def spec_fingerprint(spec: dict) -> str:
    keep = {k: spec.get(k) for k in ("datasets", "columns", "filters", "aggregate", "pin")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def resolve_periods(rule: dict, available: list[tuple[int, int]]) -> tuple[list[tuple[int, int]], list[str]]:
    """Periods wanted by one dataset rule, and the ones that do not exist."""
    avail = sorted(available)
    if not avail:
        return [], ["no files listed"]
    latest = avail[-1]
    if rule.get("periods"):
        want = [latest if str(p).lower() == "latest" else ym(p) for p in rule["periods"]]
    else:
        lo = ym(rule["from"]) if rule.get("from") else avail[0]
        hi = latest if str(rule.get("to", "latest")).lower() == "latest" else ym(rule["to"])
        months = {int(x) for x in (rule.get("months") or range(1, 13))}      # 9 or "09"
        want = [p for p in avail if lo <= p <= hi and p[1] in months]
        if not want:
            return [], [f"nothing available from {lo[0]}-{lo[1]:02d} to {hi[0]}-{hi[1]:02d} "
                        f"(available: {avail[0][0]}-{avail[0][1]:02d} to {latest[0]}-{latest[1]:02d})"]
        # a requested month that a quarterly era skipped
        missing = []
        y, m = lo
        while (y, m) <= hi:
            if m in months and (y, m) not in set(avail) and (y, m) <= latest:
                missing.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return want, missing
    missing = [f"{y}-{m:02d}" for y, m in want if (y, m) not in set(avail)]
    # a period listed twice would load the same file twice and double its counts
    return sorted({p for p in want if p in set(avail)}), missing


def build_plan(spec: dict, client: OpmClient) -> dict:
    plan = {"files": [], "missing": {}, "est_mb": 0}
    for ds, rule in spec["datasets"].items():
        listing = client.list_files(ds)
        idx = current_index(listing)
        by_version = {(int(f["year"]), int(f["month"]), int(f["version"])): f for f in listing}
        periods, missing = resolve_periods(rule or {}, list(idx))
        if missing:
            plan["missing"][ds] = missing
        for (y, m) in periods:
            meta = dict(idx[(y, m)])
            pin = (spec.get("pin") or {}).get(f"{ds}_{y}{m:02d}")
            if pin and int(pin) != int(meta["version"]):
                # the pinned version's own metadata (publish date), not the current version's
                meta = dict(by_version.get((y, m, int(pin))) or {
                    "version": int(pin), "filename": f"{ds}_{y}{m:02d}_{int(pin)}", "publishDate": None})
                meta["current"] = False
            plan["files"].append({"dataset": ds, "year": y, "month": m, "version": int(meta["version"]),
                                  "filename": meta["filename"], "publishDate": meta.get("publishDate"),
                                  "current": bool(meta.get("current")),
                                  "url": client.download_url(ds, y, m, meta["version"])})
            plan["est_mb"] += EST_MB[ds]
    return plan


def dataset_columns() -> dict[str, set]:
    """{dataset: columns the data dictionary defines for it} (empty if the dictionary is missing)."""
    import csv
    p = dictionary_path()
    if not p:
        return {}
    out: dict[str, set] = {d: set() for d in DATASETS}
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for d in DATASETS:
                if r.get(d) == "Y":
                    out[d].add(r["field"])
    return out


# ---------------------------------------------------------------- existing data
def existing(out: str, spec: dict) -> dict | None:
    mp = os.path.join(data_dir(out), "manifest.json")
    if not os.path.exists(mp):
        return None
    try:
        with open(mp, encoding="utf-8") as f:
            M = json.load(f)
    except (OSError, ValueError):
        return None
    files_ok = all(os.path.exists(os.path.join(data_dir(out), f)) for f in M.get("outputs", []))
    return {"matches": M.get("spec_fingerprint") == spec_fingerprint(spec) and files_ok, "manifest": M}


# ---------------------------------------------------------------- processing
def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def process_file(path: str, ds: str, file_meta: dict, spec: dict):
    """Read one raw file (Parquet, TXT/CSV or JSON) in chunks, keep columns and rows.
    Returns (pandas DataFrame of strings, info)."""
    import pandas as pd
    engine = opm_io.capabilities()["parquet_engine"]
    have_list = opm_io.columns_of(path, engine)
    have = set(have_list)
    wanted = spec.get("columns")
    filters = spec.get("filters") or {}
    agg = spec.get("aggregate")
    if wanted:
        cols = list(dict.fromkeys(ALWAYS[ds] + list(wanted) + list(filters) + list((agg or {}).get("by", []))
                                  + (list(PAY_COLS) if agg else [])))
        cols = [c for c in cols if c not in ("file_yyyymm", "source_file")]   # added below
    else:
        cols = list(have_list)
    read_cols = [c for c in cols if c in have]
    missing_cols = [c for c in cols if c not in have]
    rows_raw, parts = 0, []
    for df in opm_io.iter_frames(path, read_cols, engine):
        rows_raw += len(df)
        for col, vals in filters.items():
            if col in df:
                vals = [str(v) for v in (vals if isinstance(vals, list) else [vals])]
                df = df[df[col].isin(vals)]
        parts.append(df)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=read_cols)
    for c in missing_cols:
        d[c] = pd.NA
    d["file_yyyymm"] = f"{file_meta['year']}{int(file_meta['month']):02d}"
    d["source_file"] = file_meta["filename"]
    d = d[cols + ["file_yyyymm", "source_file"]].astype("string")
    if agg:
        d = aggregate(d, ds, agg)
    info = {"rows_raw": rows_raw, "rows_kept": len(d), "missing_columns": missing_cols,
            "filters_not_applied": [c for c in filters if c not in have],
            "source_format": opm_io.file_kind(path)}
    return d, info


def aggregate(d, ds: str, agg: dict):
    """Collapse microdata to counts per group, with what pay statistics can be summed. pay_n / pay_sum cover
    the pay universe of stats.pay_summary (full-time, per annum, pay reported and positive), so pay_sum / pay_n
    is comparable to its mean; pay_redacted counts every redacted record in the group."""
    import pandas as pd
    period = "snapshot_yyyymm" if ds == "employment" else "personnel_action_effective_date_yyyymm"
    by = list(dict.fromkeys([period, "file_yyyymm", "source_file"] + list(agg.get("by", []))))
    d = d.copy()
    d["_n"] = pd.to_numeric(d["count"], errors="coerce").fillna(1)
    if "annualized_adjusted_basic_pay" in d:
        pay = pd.to_numeric(d["annualized_adjusted_basic_pay"], errors="coerce")
        pay = pay.where(pay > 0)
        for col, val in (("work_schedule_code", "F"), ("pay_basis_code", "PA")):
            if col in d:
                pay = pay.where(d[col].eq(val).fillna(False).astype(bool))
        d["_red"] = d["annualized_adjusted_basic_pay"].eq("REDACTED").fillna(False).astype(int)
        d["_payn"], d["_pays"] = pay.notna().astype(int), pay.fillna(0)
    else:
        d["_red"] = d["_payn"] = d["_pays"] = 0
    g = d.groupby(by, dropna=False, observed=True).agg(headcount=("_n", "sum"), pay_redacted=("_red", "sum"),
                                                        pay_n=("_payn", "sum"), pay_sum=("_pays", "sum")).reset_index()
    for c in by:
        g[c] = g[c].astype("string")
    return g


def package(out: str, spec: dict, spec_path: str, tables: dict, file_infos: list, route: str,
            acquired: str):
    """Write data/opm/: Parquet when a Parquet writer is installed, CSV up to CSV_MAX_ROWS rows, and
    gzip-compressed CSV for larger tables when no Parquet writer exists (the only format then)."""
    import pandas as pd
    writer = opm_io.capabilities()["parquet_writer"]
    data = data_dir(out)
    os.makedirs(data, exist_ok=True)
    for f in os.listdir(data):                       # drop outputs of an earlier run of this request
        if re.fullmatch(r"opm_\w+\.(parquet|csv|csv\.gz)", f):
            os.remove(os.path.join(data, f))
    outputs, ds_info, tables_meta = [], {}, []
    for ds, parts in tables.items():
        if not parts:
            continue
        t = pd.concat(parts, ignore_index=True)
        info = {"rows": len(t), "parquet": None, "csv": None}
        if writer:
            pq_path = os.path.join(data, f"opm_{ds}.parquet")
            t.to_parquet(pq_path, index=False, engine=writer,
                         **({"compression": "zstd"} if writer == "pyarrow" else {}))
            outputs.append(os.path.basename(pq_path))
            info["parquet"] = os.path.basename(pq_path)
        if len(t) <= CSV_MAX_ROWS or not writer:
            name = f"opm_{ds}.csv" + ("" if len(t) <= CSV_MAX_ROWS else ".gz")
            t.to_csv(os.path.join(data, name), index=False)
            outputs.append(name)
            info["csv"] = name
        ds_info[ds] = info
        tables_meta.append({"name": f"opm_{ds}", "file": info["csv"] or info["parquet"], "rows": len(t),
                            "columns": [str(c) for c in t.columns]})
    dict_src = dictionary_path()
    if dict_src:
        shutil.copy(dict_src, os.path.join(data, "data_dictionary.csv"))
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    M = {"source": "U.S. Office of Personnel Management, Federal Workforce Data (EHRI)",
         "api_base": BASE, "route": route, "acquired": acquired, "downloaded": now,
         "spec_fingerprint": spec_fingerprint(spec),
         "spec": {k: spec.get(k) for k in ("description", "datasets", "columns", "filters", "aggregate", "pin")},
         "columns": spec.get("columns") or [], "filters": spec.get("filters") or {},
         "aggregate": spec.get("aggregate"), "rows": sum(i["rows"] for i in ds_info.values()),
         "datasets": ds_info, "outputs": outputs, "files": file_infos,
         "parquet_writer": writer}
    with open(os.path.join(data, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(M, f, indent=2)
    if os.path.abspath(spec_path) != os.path.abspath(os.path.join(data, "spec_used.json")):
        shutil.copy(spec_path, os.path.join(data, "spec_used.json"))
    write_request_json(data, spec, route, acquired, tables_meta, file_infos)
    write_readme(out, M)
    return M


def write_request_json(d: str, spec: dict, route: str, acquired: str, tables: list, files: list) -> None:
    """core provenance file: route, retrieval time, credential-free URLs (OPM needs no key)."""
    req = {"source": SOURCE, "spec": spec, "fingerprint": core_package.fingerprint(spec), "route": route,
           "acquired": acquired, "retrieved": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "vintage": ", ".join(sorted({f"{f['dataset']}_{f['year']}{int(f['month']):02d}_v{f.get('version')}"
                                        for f in files}))[:400],
           "requests": [s for s in SOURCES if "data.opm.gov" in s.get("url", "")] or
                       [{"method": "GET", "url": f.get("url"), "sha256": f.get("sha256")} for f in files],
           "tables": tables, "problems": [], "notes": [],
           "policy": {"redistribution": "allowed", "cache": "persistent"}}
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2, default=str)


def write_readme(out: str, M: dict):
    ds = ", ".join(sorted({f["dataset"] for f in M["files"]}))
    date = M["downloaded"][:10]
    cit = CITATION.format(datasets=ds, date=date)
    late = [f for f in M["files"] if f.get("current") is False]
    miss = sorted({c for f in M["files"] for c in f.get("missing_columns", [])})
    lines = [f"# OPM Federal Workforce Data: {M['spec'].get('description') or ds}", "",
             f"Retrieved {M['downloaded']} by route `{M['route']}` ({M.get('acquired', '')}) from {M['api_base']}.", "",
             "## Citation", "", cit, "",
             "## Files", "", "| Dataset | Period | Version | Published | Rows kept | SHA-256 (raw) |", "|---|---|---|---|---|---|"]
    for f in M["files"]:
        lines.append(f"| {f['dataset']} | {f['year']}-{f['month']:02d} | {f['version']}{'' if f.get('current') is not False else ' (pinned)'} | "
                     f"{(f.get('publishDate') or '')[:10]} | {f.get('rows_kept', ''):,} | `{(f.get('sha256') or '')[:16]}…` |")
    lines += ["", "## Things to know before analyzing", "",
              "- Pay (`annualized_adjusted_basic_pay`), duty-station location, CBSA/CSA, locality pay area, "
              "bargaining unit and personnel office are `REDACTED` for all Department of Defense employees and for "
              "security/sensitive agencies and occupations (about 45 to 48 percent of employment records). "
              "Never average pay over the unredacted records and call it a governmentwide figure.",
              "- Coverage: EHRI excludes USPS, the intelligence agencies (CIA, NSA, DIA, NGA, ODNI), the White House "
              "Office, Foreign Service at State, TVA, the Fed Board of Governors, PHS Commissioned Corps, "
              "nonappropriated-fund staff, foreign nationals overseas, and the legislative and judicial branches.",
              "- Accession and separation files hold actions processed in the file month; some are effective in "
              "earlier months (`personnel_action_effective_date_yyyymm`). Recent months gain late actions in later files.",
              "- OPM revises files: `manifest.json` records each version; `fetch.py --source opm spec.json --pin` "
              "re-fetches exactly these.",
              "- Agency names change (e.g. Department of Defense / Department of War); group by `*_code` columns.",
              "- No sex, race, ethnicity, disability or individual identifiers are in the public files."]
    if miss:
        lines.append(f"- Columns absent from some (older) files and filled with nulls: {', '.join(miss)}.")
    if late:
        lines.append(f"- {len(late)} file(s) are pinned to a non-current version.")
    lines += ["", "## Use and redistribution", "",
              "Rule: **allowed**. OPM publishes these files as public data under its Public Data Release Policy; "
              "the filtered files may ship with a report and its replication package. Fields OPM withholds for "
              "privacy or security are published as `REDACTED`: disclose the coverage of every pay or location "
              "figure (`coverage.csv`)."]
    with open(os.path.join(data_dir(out), "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_download(spec: dict, spec_path: str, out: str, plan: dict, no_cache: bool, route="api",
                 local_files: dict | None = None, acquired: str = "FWD API download"):
    client = OpmClient()
    cdir = cache_dir(no_cache, out)
    tables: dict[str, list] = {}
    infos = []
    total = len(plan["files"])
    for i, f in enumerate(plan["files"], 1):
        raw = (local_files or {}).get(f["filename"]) or os.path.join(cdir, f["filename"] + ".parquet")
        fresh = local_files is not None or no_cache or cached_fresh(raw)
        if not os.path.exists(raw) or not fresh:
            if local_files is not None:
                raise OpmError(f"{f['filename']} was not among the ingested files")
            print(f"[{i}/{total}] downloading {f['filename']} ...", file=sys.stderr, flush=True)
            client.download(f["dataset"], f["year"], f["month"], f["version"], raw)
        else:
            print(f"[{i}/{total}] using {'ingested' if local_files else 'cached'} {f['filename']}", file=sys.stderr, flush=True)
        t, info = process_file(raw, f["dataset"], f, spec)
        tables.setdefault(f["dataset"], []).append(t)
        infos.append({**f, **info, "bytes": os.path.getsize(raw), "sha256": sha256(raw)})
        if no_cache and local_files is None:
            os.remove(raw)
    if no_cache and os.path.isdir(os.path.join(data_dir(out), "_raw_tmp")):
        shutil.rmtree(os.path.join(data_dir(out), "_raw_tmp"), ignore_errors=True)
    applies = dataset_columns()
    for f in infos:
        f["missing_columns"] = [c for c in f["missing_columns"] if not applies or c in applies.get(f["dataset"], ())]
    M = package(out, spec, spec_path, tables, infos, route, acquired)
    warn = []
    for f in infos:
        if f["missing_columns"]:
            warn.append(f"{f['filename']}: columns not in this file (nulls): {', '.join(f['missing_columns'][:8])}")
        if f["filters_not_applied"]:
            warn.append(f"{f['filename']}: filter column(s) {f['filters_not_applied']} absent, so rows were NOT filtered")
        elif spec.get("filters") and f["rows_raw"] and not f["rows_kept"]:
            warn.append(f"{f['filename']}: the filters kept 0 of {f['rows_raw']} rows; check the codes in spec.json")
    empty = bool(infos) and bool(spec.get("filters")) and not any(f["rows_kept"] for f in infos)
    emit({"status": "error" if empty else "ok", "route": route, "data_dir": data_dir(out), "datasets": M["datasets"],
          **({"message": "the filters kept no rows in any file: check the codes in spec.json (--values)"} if empty
             else {}),
          "files": len(infos), "warnings": (_INGEST_WARNINGS + warn)[:30],
          "cache": None if no_cache or local_files is not None else cdir,
          "citation": CITATION.format(datasets=", ".join(sorted(M["datasets"])), date=M["downloaded"][:10]),
          "read_next": os.path.join(HERE, "source.md")})


# ---------------------------------------------------------------- manual route
DOWNLOAD_SCRIPTS = {"bash": "download_opm.sh", "powershell": "download_opm.ps1"}


def script_rules(spec: dict) -> list[str]:
    """The request as the download scripts read it (tab-separated): periods / range / pin rows per dataset."""
    rows = []
    for ds, rule in spec["datasets"].items():
        rule = rule or {}
        if rule.get("periods"):
            ps = ["latest" if str(x).lower() == "latest" else "%04d%02d" % ym(x) for x in rule["periods"]]
            rows.append(f"{ds}\tperiods\t{','.join(ps)}")
        else:
            lo = "%04d%02d" % ym(rule["from"]) if rule.get("from") else "first"
            hi = "latest" if str(rule.get("to", "latest")).lower() == "latest" else "%04d%02d" % ym(rule["to"])
            months = ",".join(str(int(x)) for x in (rule.get("months") or range(1, 13)))
            rows.append(f"{ds}\trange\t{lo}\t{hi}\t{months}")
    for key, version in (spec.get("pin") or {}).items():
        m = re.fullmatch(r"([a-z]+)_(\d{6})", key)
        if m:
            rows.append(f"{m.group(1)}\tpin\t{m.group(2)}\t{int(version)}")
    return rows


def write_download_scripts(d: str, spec: dict) -> dict:
    """download_opm.sh / .ps1 with the request built in: they list OPM's current files, pick the requested
    months and download them (curl / Windows PowerShell, no Python, no key)."""
    rules = "\n".join(script_rules(spec))
    paths = {}
    for kind, name in DOWNLOAD_SCRIPTS.items():
        try:
            with open(os.path.join(HERE, "assets", name), encoding="ascii") as f:
                text = f.read()
        except OSError as e:
            print(f"[opm] warning: {name} not written ({e})", file=sys.stderr)
            continue
        path = os.path.join(d, name)
        with open(path, "w", encoding="ascii", newline="\n" if kind == "bash" else "\r\n") as f:
            f.write(text.replace("@RULES@", rules))
        if kind == "bash":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        paths[kind] = path
    return paths


def bundle_fields(bundle: str) -> dict:
    """What a route JSON says about the hand-download bundle."""
    return {"manual_bundle": bundle, "instructions": os.path.join(bundle, "INSTRUCTIONS.md"),
            "download_scripts": {k: os.path.join(bundle, n) for k, n in DOWNLOAD_SCRIPTS.items()},
            "request_spec": os.path.join(bundle, "spec.json"), "user_commands": manual.run_lines("download_opm")}


SEND_BUNDLE = ("Send INSTRUCTIONS.md, both download scripts (download_scripts: download_opm.sh for macOS/Linux, "
               "download_opm.ps1 for Windows) and spec.json (request_spec, the request the scripts have built in) "
               "with the file-sending tool when you have one, and print their full paths. Give the user the one "
               "command for their system (user_commands; both when unsure); no key is needed. Employment months are "
               "26-75 MB each: for many of them, Option B (the bundled fetch.py, with Python) uploads far less.")


def manual_bundle(spec_path: str, out: str, plan: dict | None, env: str, reason: str = "network") -> str:
    d = bundle_dir(out)
    os.makedirs(d, exist_ok=True)
    shutil.copy(spec_path, os.path.join(d, "spec.json"))
    write_download_scripts(d, load_spec(spec_path))
    for f in BUNDLE_CODE:
        if os.path.exists(os.path.join(HERE, f)):
            shutil.copy(os.path.join(HERE, f), os.path.join(d, f))
    if not os.path.exists(os.path.join(d, "policy.json")) and policy():   # the upload zip bundles it
        with open(os.path.join(d, "policy.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(policy(), fh, indent=2)
    if dictionary_path():
        shutil.copy(dictionary_path(), os.path.join(d, "data_dictionary.csv"))
    core_src = os.path.join(SKILL, "scripts", "core")          # the shared modules fetch.py imports
    if os.path.isdir(core_src) and os.path.abspath(core_src) != os.path.abspath(os.path.join(d, "core")):
        shutil.copytree(core_src, os.path.join(d, "core"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = (plan or {}).get("files") or []
    urls = [f["url"] for f in files]
    if urls:
        with open(os.path.join(d, "urls.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(urls) + "\n")
    url_note = "`urls.txt` lists the exact files found when the bundle was written." if urls else ""
    months = sorted({(f["dataset"], f"{f['year']}-{f['month']:02d}") for f in files})
    month_note = ("Files needed: " + "; ".join(f"{ds} {m}" for ds, m in months) + "." if months else
                  "Pick the months listed in `spec.json` (\"datasets\").")
    why = ("This session cannot reach https://data.opm.gov" if reason == "network" else
           "This session cannot read Parquet files (no pyarrow, fastparquet, duckdb or polars, and they could "
           "not be installed)")
    run = "\n".join(manual.run_section("download_opm", spec_name="spec.json"))
    text = f"""# Get the OPM files by hand

{why} (environment: {env}). The data are public and need no account or key. Pick one option, then
upload the result to the chat.

## Option A (recommended): one command

{run}
The script lists the files OPM publishes now, picks the months in the request and saves each as
`<dataset>_<YYYYMM>_<version>.parquet` (for example `employment_202509_4.parquet`). Employment files are
26-75 MB each; accession and separation files are under 4 MB. Zip large files before uploading. {month_note}

## Option B: run the same fetcher on your computer (much smaller upload)
Needs Python 3.10+ with `pip install pandas pyarrow`. Run it in this folder (it holds `fetch.py`, its
helper modules and a copy of the skill's `core/` package):

```bash
python fetch.py spec.json --out .
```
It downloads only the files in `spec.json`, keeps the requested columns and rows, and writes a small
`data/opm/` folder (Parquet and CSV). **Zip the `data/` folder and upload it.** It works even where Claude
cannot read Parquet, because the folder also holds CSV files. Prefer it for many employment months.

## Option C: download TXT or JSON from the website (no software needed)
On https://data.opm.gov/get-data/data-downloads, click **Download** on each file you need and choose
**.TXT** (preferred) or **.JSON**. {month_note} Keep the file names, or rename them like
`separations_202608_1.txt` (dataset, year and month, version) so they are matched to the right month.
TXT and JSON files are several times larger than Parquet: an employment month is hundreds of MB, so
zip them before uploading.

## Option D: no download
For simple counts, OPM's Table Builder (https://data.opm.gov/explore-data/data/table-builder) can
export an aggregate table; upload the CSV and say which table it is.

After the upload Claude reads the files and continues the analysis. {url_note}
"""
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return d


RAW_EXT = (".parquet", ".txt", ".csv", ".tsv", ".dat", ".json", ".jsonl", ".gz", ".zip")
_INGEST_WARNINGS: list[str] = []


def _prebuilt(spec: dict, out: str, src: str):
    """Copy a data/ folder that fetch.py built elsewhere (needs no Parquet reader: it has CSV)."""
    dst = data_dir(out)
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    with open(os.path.join(dst, "manifest.json"), encoding="utf-8") as f:
        M = json.load(f)
    M["route"], M["acquired"] = "manual_download", "manual_prebuilt"
    with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(M, f, indent=2)
    tables = [{"name": f"opm_{ds}", "file": (v or {}).get("csv") or (v or {}).get("parquet"), "rows": (v or {}).get("rows")}
              for ds, v in (M.get("datasets") or {}).items()]
    write_request_json(dst, spec, "manual_download", "manual_prebuilt", tables, M.get("files") or [])
    warn = [] if M.get("spec_fingerprint") == spec_fingerprint(spec) else [
        "the uploaded data/ folder was built from a different spec.json"]
    caps = opm_io.capabilities()
    if not caps["parquet_engine"] and not all((v or {}).get("csv") for v in (M.get("datasets") or {}).values()):
        warn.append("this environment cannot read Parquet and the folder lacks CSV for some datasets")
    emit({"status": "ok", "route": "manual_download", "acquired": "manual_prebuilt", "data_dir": dst,
          "datasets": M.get("datasets"), "warnings": warn})


def ingest(spec: dict, spec_path: str, out: str, paths: list[str], no_cache: bool):
    """Package hand-downloaded files: a prebuilt data/ folder (or its ZIP), API Parquet files, or the
    website's TXT/JSON files (also inside .gz/.zip)."""
    import gzip
    import zipfile
    if not paths:
        paths = upload_dirs()
    found = []
    tmp_root = os.path.join(data_dir(out), "_ingest")
    shutil.rmtree(tmp_root, ignore_errors=True)           # never re-read files from an earlier upload

    def walk(d):
        for root, _, fs in os.walk(d):
            if "manifest.json" in fs and any(re.fullmatch(r"opm_\w+\.(parquet|csv|csv\.gz)", f) for f in fs):
                _prebuilt(spec, out, root)
            found.extend(os.path.join(root, f) for f in fs if f.lower().endswith(RAW_EXT)
                         and f != "data_dictionary.csv")

    for i, p in enumerate(paths):
        if os.path.isdir(p):
            walk(p)
        elif p.lower().endswith(".zip") and len([n for n in zipfile.ZipFile(p).namelist() if not n.endswith("/")]) > 1:
            dst = os.path.join(tmp_root, f"zip{i}")       # one folder per ZIP, alongside the other arguments
            os.makedirs(dst)
            zipfile.ZipFile(p).extractall(dst)
            walk(dst)
        else:
            found.append(p)
    # a single Parquet file inside .zip or .gz: unpack it, since Parquet readers need the plain file
    for k, f in enumerate(found):
        if f.lower().endswith((".zip", ".gz")) and opm_io.file_kind(f) == "parquet":
            member = opm_io.inner_name(f)
            dst = os.path.join(tmp_root, f"pq{k}")
            os.makedirs(dst)
            target = os.path.join(dst, os.path.basename(member))
            src = zipfile.ZipFile(f).open(member) if f.lower().endswith(".zip") else gzip.open(f, "rb")
            with src, open(target, "wb") as o:
                shutil.copyfileobj(src, o)
            found[k] = target
    if not found:
        emit({"status": "error", "route": "fix_spec",
              "message": "no data files found (expected .parquet, .txt, .csv or .json, optionally zipped, or a "
                         "data/ folder built by fetch.py)"}, 2)
    caps = opm_io.capabilities()
    if not caps["pandas"]:
        emit({"route": "install_deps", "message": "pandas is not installed",
              "next_command": f'"{sys.executable}" -m pip install pandas'}, 3)
    if any(opm_io.file_kind(f) == "parquet" for f in found) and not caps["parquet_engine"]:
        emit({"route": "install_deps", "message": "uploaded Parquet files need a Parquet reader, and none is installed",
              "action": "Install one (pyarrow, else fastparquet, duckdb or polars). If nothing can be installed, ask the "
                        "user for Option A (a data/ folder with CSV) or Option C (TXT/JSON from the website).",
              "next_command": f'"{sys.executable}" -m pip install pyarrow'}, 3)
    files, local, problems, warn = [], {}, [], []
    best: dict[tuple, tuple] = {}                 # one file per dataset and month
    for path in found:
        who = opm_io.identify(path, caps["parquet_engine"])
        if not who["dataset"] or not who["year"]:
            problems.append(f"{os.path.basename(path)}: cannot tell the dataset and month; rename it like "
                            "separations_202608_1.txt")
            continue
        key = (who["dataset"], who["year"], who["month"])
        if key in best:
            # two copies of one month (two versions, or a website file and an API file) would double
            # the counts: keep the highest version; an API file (versioned) beats a website file
            old = best[key]
            keep, drop = ((path, who), old) if (who["version"] or 0) > (old[1]["version"] or 0) else (old, (path, who))
            warn.append(f"{os.path.basename(drop[0])} and {os.path.basename(keep[0])} are both {key[0]} "
                        f"{key[1]}-{key[2]:02d}; kept {os.path.basename(keep[0])}")
            best[key] = keep
        else:
            best[key] = (path, who)
    for (ds, y, m), (path, who) in sorted(best.items()):
        name = f"{ds}_{y}{m:02d}_{who['version'] if who['version'] is not None else 'web'}"
        if who["how"] == "content":
            warn.append(f"{os.path.basename(path)}: dataset and month inferred from the content ({ds} {y}-{m:02d}); "
                        "check that this is the month OPM labels the file with")
        rule = spec["datasets"].get(ds)
        if rule is None:
            warn.append(f"{os.path.basename(path)}: {ds} is not in spec.json; skipped")
            continue
        # "latest" means the latest month among the uploaded files of this dataset
        wanted, _ = resolve_periods(rule, [k[1:] for k in best if k[0] == ds])
        if (y, m) not in wanted:
            warn.append(f"{os.path.basename(path)}: {y}-{m:02d} is outside the periods in spec.json; skipped")
            continue
        local[name] = path
        files.append({"dataset": ds, "year": y, "month": m, "version": who["version"], "filename": name,
                      "publishDate": None, "current": None, "identified_by": who["how"],
                      "url": f"{BASE}/{ds}/{y}/{m:02d}/{who['version']}/download" if who["version"] else
                             "https://data.opm.gov/get-data/data-downloads"})
    if problems and not files:
        emit({"status": "error", "message": "; ".join(problems)}, 2)
    _INGEST_WARNINGS.extend(warn + problems)
    run_download(spec, spec_path, out, {"files": sorted(files, key=lambda f: (f["dataset"], f["year"], f["month"]))},
                 no_cache=True, route="manual_download", local_files=local,
                 acquired="manual_ingest (files downloaded by hand by the user and uploaded)")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="fetch.py --source opm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="download again even if <out>/data/opm matches")
    ap.add_argument("--pin", action="store_true", help="fetch the exact versions in <out>/data/opm/manifest.json")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--manual-bundle", action="store_true")
    ap.add_argument("--ingest", nargs="*")
    ap.add_argument("--list", choices=DATASETS)
    ap.add_argument("--env", choices=ENVIRONMENTS)
    a = ap.parse_args()

    if a.list:
        try:
            idx = current_index(OpmClient().list_files(a.list))
        except OpmError as e:
            emit({"status": "error", "route": "report_error" if e.kind != "network" else "manual_download",
                  "message": str(e)}, 4)
        ks = sorted(idx)
        emit({"dataset": a.list, "files": len(ks), "first": f"{ks[0][0]}-{ks[0][1]:02d}",
              "latest": f"{ks[-1][0]}-{ks[-1][1]:02d}",
              # years with only quarter-end files (employment before 2011), and other incomplete years
              # (the current year so far, or a year with missing months) with the months they have
              "quarterly_years": sorted({y for y, _ in ks if {m for yy, m in ks if yy == y} <= {3, 6, 9, 12}}),
              "partial_years": {str(y): [m for yy, m in ks if yy == y] for y in sorted({y for y, _ in ks})
                                if sum(1 for k in ks if k[0] == y) < 12
                                and not {m for yy, m in ks if yy == y} <= {3, 6, 9, 12}},
              "latest_files": [idx[k] for k in ks[-3:]]})
    if not a.spec:
        ap.error("spec.json is required")
    out = os.path.abspath(a.out or os.path.dirname(os.path.abspath(a.spec)))
    env = detect_environment(a.env)
    try:
        spec = load_spec(a.spec)
    except (ValueError, OSError) as e:
        emit({"route": "fix_spec", "environment": env, "message": f"spec.json: {e}",
              "action": "Fix spec.json and run --check again.", "next_command": ""}, 2)
    if a.pin:
        ex = existing(out, spec)
        if ex:
            spec["pin"] = {f["filename"].rsplit("_", 1)[0]: f["version"] for f in ex["manifest"]["files"]}
    base = f'--source {SOURCE} "{os.path.abspath(a.spec)}" --out "{out}"'
    me = routes.cmd(base)
    ingest_cmd = routes.cmd(base, "--ingest", "<the uploaded files or their folder>")

    if a.ingest is not None:
        ingest(spec, a.spec, out, a.ingest, a.no_cache)

    ex = existing(out, spec)
    if ex and ex["matches"] and not a.force and not a.manual_bundle and not a.dry_run:
        M = ex["manifest"]
        emit({"status": "route_check" if a.check else "ok", "route": "reuse_existing", "environment": env,
              "data_dir": data_dir(out),
              "message": f"{data_dir(out)} already holds this request ({M.get('rows', 0):,} rows, downloaded {M.get('downloaded')}).",
              "action": "Skip the download and continue with the analysis. Use --force to download again "
                        "(for example after OPM publishes revised versions).",
              "next_command": "", "datasets": M.get("datasets")})

    ok, why = deps_ok()
    client = OpmClient(retries=1 if a.check else 4)
    reachable, reason = client.ping()
    if not reachable:
        BLK, ERR = (0, 2) if a.check else (4, 4)     # --check reports a route; a fetch is blocked
        http_err = bool(re.search(r"HTTP 5|429|500|Internal Server", reason))
        if http_err:
            emit({"route": "report_error", "environment": env, "message": f"data.opm.gov answered with an error: {reason}",
                  "action": "Tell the user OPM's API is failing right now and stop; try again later.",
                  "next_command": ""},ERR)
        bundle = manual_bundle(a.spec, out, None, env)
        common = {"environment": env, "reason": reason, **bundle_fields(bundle), "next_command": ingest_cmd}
        if env in ("local", "ssh") and not keys.restricted():
            where = "This computer" if env == "local" else "The remote host"
            emit({"route": "report_error", **common,
                  "message": f"{where} cannot reach data.opm.gov ({reason}). Check the internet connection, VPN "
                             "and proxy (HTTPS_PROXY), then run --check again; or download by hand in a browser "
                             "with the bundle (the data are public, no account needed).",
                  "action": "Tell the user; offer the manual bundle. " + SEND_BUNDLE + " When they upload, run "
                            "next_command."},ERR)
        if env == "cloud":
            emit({"route": "cloud_allowlist", **common,
                  "message": "This cloud session cannot reach data.opm.gov. Add data.opm.gov to the environment's "
                             "allowed network domains and start a new session, or download by hand.",
                  "action": "Offer both options. For the manual one: " + SEND_BUNDLE + " When they upload, run "
                            "next_command."},BLK)
        emit({"route": "manual_download", **common,
              "message": "This sandbox has no route to data.opm.gov (on claude.ai an organization admin can allow "
                         "data.opm.gov in the network settings). The user downloads by hand and uploads the files.",
              "action": SEND_BUNDLE + " When they upload, run next_command with the uploaded files or their "
                        "folder."},BLK)

    try:
        plan = build_plan(spec, client)
    except OpmError as e:
        emit({"route": "report_error", "environment": env, "message": str(e), "next_command": ""}, 4)
    except ValueError as e:
        emit({"route": "fix_spec", "environment": env, "message": f"spec.json: {e}",
              "action": "Fix spec.json and run --check again.", "next_command": ""}, 2)
    if plan["missing"] and not plan["files"]:
        emit({"route": "fix_spec", "environment": env, "message": f"Requested periods do not exist: {plan['missing']}",
              "action": "Run fetch.py --source opm --list <dataset> to see what exists, fix spec.json, and run "
                        "--check again.", "next_command": ""}, 2)
    n_emp = sum(1 for f in plan["files"] if f["dataset"] == "employment")
    cdir = raw_cache_root()
    cached = sum(1 for f in plan["files"] if cached_fresh(os.path.join(cdir, f["filename"] + ".parquet")))
    todo = len(plan["files"]) - cached                 # cached files take a fraction of a second each
    share = todo / len(plan["files"]) if plan["files"] else 0
    est_min = round(max(0.2, (plan["est_mb"] * share / 15 + todo * 3 + cached * 0.2) / 60), 1)  # ~15 MB/s plus processing
    summary = {"files": len(plan["files"]), "employment_files": n_emp, "already_cached": cached,
               "est_download_mb": plan["est_mb"], "est_minutes": est_min, "missing_periods": plan["missing"],
               "first": plan["files"][0]["filename"] if plan["files"] else None,
               "last": plan["files"][-1]["filename"] if plan["files"] else None}
    if a.dry_run:
        emit({"status": "dry_run", "plan": summary, "files": plan["files"]})
    if a.manual_bundle:
        b = manual_bundle(a.spec, out, plan, env)
        emit({"status": "ok", "route": "manual_download", **bundle_fields(b), "plan": summary,
              "next_command": ingest_cmd})
    if not ok:
        caps = opm_io.capabilities()
        pkg = "pandas pyarrow" if not caps["pandas"] else "pyarrow"
        bundle = manual_bundle(a.spec, out, plan, env, reason="parquet")
        emit({"route": "install_deps", "environment": env, "message": f"Python packages missing ({why}).",
              "action": "Install them (ask first if this is the user's own environment), then run --check again. "
                        "If pyarrow will not install, try fastparquet, duckdb or polars (any one is enough). If "
                        "nothing can be installed, use the manual route: give the user manual_bundle/INSTRUCTIONS.md "
                        "(Option A: the one-command download script; Option B: a data/ folder with CSV; Option C: TXT/JSON from "
                        "the website) and --ingest the upload.",
              "next_command": f'"{sys.executable}" -m pip install {pkg}',
              "fallback_commands": [f'"{sys.executable}" -m pip install {x}' for x in ("fastparquet", "duckdb", "polars")],
              "manual_bundle": bundle, "plan": summary}, 3)
    if a.check:
        msg = (f"data.opm.gov is reachable; no key is needed. {summary['files']} file(s), about {plan['est_mb']} MB "
               f"({cached} already cached), roughly {est_min} minutes.")
        if plan["missing"]:
            msg += f" Not available and skipped: {plan['missing']}."
        if n_emp > 6 and not spec.get("columns") and not spec.get("aggregate"):
            msg += (" Many employment snapshots with all 64 columns will be large; set \"columns\" (or "
                    "\"aggregate\") in spec.json to keep only what the analysis needs.")
        emit({"status": "route_check", "route": "api", "environment": env, "key_env": None, "key_required": "none",
              "restricted": keys.restricted(), "message": msg,
              "action": "Run next_command" + (" in the background" if est_min > 2 else "") + ".",
              "next_command": me, "plan": summary})
    try:
        run_download(spec, a.spec, out, plan, a.no_cache)
    except OpmError as e:
        emit({"route": "report_error", "message": str(e), "action": "Rerun later; cached files are kept.",
              "next_command": me}, 4)


if __name__ == "__main__":
    main()

"""Analysis helpers for OPM Federal Workforce Data (EHRI status and dynamics files), pandas.

Import from an analysis script:
    import sys, os
    OUT = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [os.path.join(OUT, "lib"), os.path.join(OUT, "lib", "opm"), r"<skill>/sources/opm/lib"]
    # (the full path layout, with facts and combine, is in SKILL.md section 4)
    from stats import *

    emp = clean(load(f"{OUT}/data/opm", "employment"))   # one row per employee per snapshot
    sep = clean(load(f"{OUT}/data/opm", "separations"))  # one row per separation action
    (load(f"{OUT}/data", ...) also works: it looks in data/opm/ when data/ holds no opm_* files)
    hc  = headcount(emp, ["period", "department_code"])
    cov = redaction_coverage(emp, ["period", "department_code"]); cov.to_csv(f"{OUT}/coverage.csv", index=False)
    pay = pay_summary(emp, ["period", "department_code"])          # full-time, per annum, unredacted
    rates = action_rate(sep, emp, by=["department_code"], period="fiscal_year")

These are administrative records for the whole covered workforce, not a sample: there are no weights
and no sampling error. Uncertainty comes from coverage (excluded agencies), redaction, late or missing
agency submissions, revisions, and coding changes. Measure those instead of computing standard errors.

What clean() adds:
    pay              annualized_adjusted_basic_pay as a number (NaN when REDACTED or invalid)
    pay_redacted     True when pay is REDACTED (all of DoD; security/sensitive agencies and occupations)
    loc_redacted     True when duty-station location is REDACTED
    los              length_of_service_years as a number
    n                the `count` column as a number (always 1 in microdata); in aggregated data
                     (spec "aggregate") the `headcount` column, so counts sum to employees, not rows
    period           YYYYMM integer: the snapshot month (employment) or the effective month (actions)
    file_period      YYYYMM integer of the file the record came from (actions: the processing month)
    year, month, fiscal_year (October-September; FY2025 = Oct 2024 to Sep 2025)
Sentinels "NO DATA REPORTED", "INVALID", "UNSPECIFIED" and blanks become missing (NaN) in every column.

Facts: every number in report prose is registered here, traced to an exhibit CSV cell, and referenced
in report.json as {{name}}; build_report.py fills the value, audit_report.py re-checks it.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import re
from typing import Callable

import numpy as np
import pandas as pd

SENTINELS = {"REDACTED", "NO DATA REPORTED", "INVALID", "UNSPECIFIED", "", "*", "**", "***", "NAN", "NONE"}
LOCATION_COLS = ("duty_station_state", "duty_station_state_abbreviation", "duty_station_code",
                 "core_based_statistical_area_code", "locality_pay_area_code")
SMALL_N = 30            # rates and pay statistics on fewer records are flagged (low_n) and footnoted


# ---------------------------------------------------------------- loading
def load(path: str, dataset: str | None = None, columns: list[str] | None = None) -> pd.DataFrame:
    """Read data written by fetch.py: a data/ folder (with dataset=) or a .parquet/.csv/.csv.gz file.
    Uses Parquet when a reader is installed, otherwise the CSV copy (every value stays a string)."""
    if os.path.isdir(path):
        if not dataset:
            raise ValueError("load(<data folder>, dataset='employment'|'accessions'|'separations')")
        if not glob.glob(os.path.join(path, f"opm_{dataset}.*")) and os.path.isdir(os.path.join(path, "opm")):
            path = os.path.join(path, "opm")            # <out>/data -> <out>/data/opm (data-analyst layout)
        cands = [os.path.join(path, f"opm_{dataset}{ext}") for ext in (".parquet", ".csv", ".csv.gz")]
        if not _parquet_ok():
            cands = cands[1:] + cands[:1]          # no Parquet reader here: use the CSV copy
        path = next((c for c in cands if os.path.exists(c)), cands[0])
    if path.endswith(".parquet"):
        if not _parquet_ok():
            raise RuntimeError(f"{path} needs a Parquet reader (pip install pyarrow, or fastparquet/duckdb/polars), "
                               "and no CSV copy exists; rerun fetch.py where Parquet can be read, or ingest TXT/JSON")
        return pd.read_parquet(path, columns=columns)
    usecols = (lambda c: c in set(columns)) if columns else None
    return pd.read_csv(path, usecols=usecols, dtype=str, keep_default_na=False)


def _parquet_ok() -> bool:
    for mod in ("pyarrow", "fastparquet"):          # the engines pandas.read_parquet can use
        try:
            __import__(mod)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def fiscal_year(yyyymm) -> pd.Series:
    """Federal fiscal year of a YYYYMM value: October-December count toward the next year."""
    p = pd.to_numeric(pd.Series(yyyymm), errors="coerce")
    y, m = p // 100, p % 100
    return (y + (m >= 10)).astype("Int64")


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize strings, turn sentinels into NaN, add numeric and period columns (see module doc)."""
    d = df.copy()
    pay_raw = d.get("annualized_adjusted_basic_pay")
    loc_raw = next((d[c] for c in LOCATION_COLS if c in d), None)
    for c in d.columns:
        if d[c].dtype == object or pd.api.types.is_string_dtype(d[c]):
            s = d[c].astype("string").str.strip()
            d[c] = s.mask(s.str.upper().isin(SENTINELS))
    if pay_raw is not None:
        pr = pay_raw.astype("string").str.strip().str.upper()
        d["pay_redacted"] = pr.eq("REDACTED").fillna(False).astype(bool)
        d["pay"] = pd.to_numeric(d["annualized_adjusted_basic_pay"], errors="coerce")
    if loc_raw is not None:
        d["loc_redacted"] = loc_raw.astype("string").str.strip().str.upper().eq("REDACTED").fillna(False).astype(bool)
    if "length_of_service_years" in d:
        d["los"] = pd.to_numeric(d["length_of_service_years"], errors="coerce")
    # aggregated data (spec "aggregate"): one row per group, employees in `headcount`
    for c in ("headcount", "pay_n", "pay_sum") + (("pay_redacted",) if pay_raw is None else ()):
        if c in d:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    ncol = "count" if "count" in d else ("headcount" if "headcount" in d else None)
    d["n"] = pd.to_numeric(d[ncol], errors="coerce").fillna(1) if ncol else 1.0
    pcol = "snapshot_yyyymm" if "snapshot_yyyymm" in d else (
        "personnel_action_effective_date_yyyymm" if "personnel_action_effective_date_yyyymm" in d else None)
    if pcol:
        d["period"] = pd.to_numeric(d[pcol], errors="coerce").astype("Int64")
        d["year"] = (d["period"] // 100).astype("Int64")
        d["month"] = (d["period"] % 100).astype("Int64")
        d["fiscal_year"] = fiscal_year(d["period"]).values
    if "file_yyyymm" in d:
        d["file_period"] = pd.to_numeric(d["file_yyyymm"], errors="coerce").astype("Int64")
    return d


def latest_labels(df: pd.DataFrame, code: str, label: str, period: str = "period") -> dict:
    """{code: label from the most recent period}. Names change over time (e.g. Department of
    Defense / Department of War); group by codes and display the latest label."""
    d = df[[code, label, period]].dropna(subset=[code, label]).sort_values(period)
    return d.drop_duplicates(code, keep="last").set_index(code)[label].to_dict()


# ---------------------------------------------------------------- counts and shares
def headcount(df: pd.DataFrame, by: list[str], where: Callable | None = None, name: str = "headcount") -> pd.DataFrame:
    """Sum of `n` by group (employees on board for status data, actions for dynamics data)."""
    d = df[where(df)] if where else df
    return d.groupby(by, dropna=False, observed=True)["n"].sum().rename(name).reset_index()


def shares(df: pd.DataFrame, by: list[str], category: str, where: Callable | None = None,
           keep_missing: bool = False) -> pd.DataFrame:
    """Composition: count and share of each `category` value within each `by` group.
    Records with a missing category are counted in `n_missing` and left out of the shares; keep_missing=True
    keeps them in the denominator, as OPM's FedScope dashboards do ("Data not available" is a category there)."""
    d = df[where(df)] if where else df
    t = d.groupby(by + [category], dropna=False, observed=True)["n"].sum().rename("count").reset_index()
    if not by:                                   # overall composition
        n_missing = t.loc[t[category].isna(), "count"].sum()
        t = t[t[category].notna()].copy()
        t["share"] = t["count"] / (t["count"].sum() + (n_missing if keep_missing else 0))
        t["n_missing"] = n_missing
        return t
    miss = t[t[category].isna()].groupby(by, dropna=False)["count"].sum().rename("n_missing")
    t = t[t[category].notna()]
    t = t.merge(miss.reset_index(), on=by, how="left").fillna({"n_missing": 0})
    t["share"] = t["count"] / (t.groupby(by, dropna=False)["count"].transform("sum") + (t["n_missing"] if keep_missing else 0))
    return t


def pct_change(new, old):
    return (new - old) / old if old else np.nan


# ---------------------------------------------------------------- redaction and pay
def redaction_coverage(df: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """How many records have pay and location withheld, by group. Save it as <out>/coverage.csv and
    report it: any pay or location statistic describes only the unredacted part of the workforce."""
    d = df.assign(_one=df["n"])
    agg = {"records": ("_one", "sum")}
    # a flag counts the row's employees (n: 1 per record, the group size in aggregated files); an aggregated
    # file's pay_redacted is already a count
    for src, dst in (("pay_redacted", "pay_redacted"), ("loc_redacted", "location_redacted")):
        if src in d:
            d["_" + src] = d[src].astype(int) * d["n"] if d[src].dtype == bool else d[src]
            agg[dst] = ("_" + src, "sum")
    t = d.groupby(by or (lambda _: "all"), dropna=False, observed=True).agg(**agg).reset_index()
    if not by:
        t = t.rename(columns={"index": "group"})
    for c in ("pay_redacted", "location_redacted"):
        if c in t:
            t[c + "_share"] = t[c] / t["records"]
    return t


def missing_summary(df: pd.DataFrame, cols: list[str], by: list[str] | None = None) -> pd.DataFrame:
    """Records with a missing value in each field (blank, NO DATA REPORTED, INVALID and other sentinels,
    which clean() turns into NaN), by group; `by` defaults to ["period"]. REDACTED pay and location are
    not counted here: redaction_coverage() reports them. Report this for the fields an analysis uses."""
    by = list(by) if by is not None else (["period"] if "period" in df else [])
    parts = []
    for c in cols:
        m = df[c].isna()
        if c in ("pay", "annualized_adjusted_basic_pay") and "pay_redacted" in df:
            m &= ~df["pay_redacted"]
        elif c in LOCATION_COLS and "loc_redacted" in df:
            m &= ~df["loc_redacted"]
        t = (df.assign(_m=m.astype(float) * df["n"])
               .groupby(by or (lambda _: "all"), dropna=False, observed=True)
               .agg(records=("n", "sum"), missing=("_m", "sum")).reset_index())
        if not by:
            t = t.rename(columns={"index": "group"})
        t.insert(0, "field", c)
        t["missing_share"] = t["missing"] / t["records"]
        parts.append(t)
    return pd.concat(parts, ignore_index=True)


def pay_universe(df: pd.DataFrame) -> pd.Series:
    """Default pay universe: full-time, paid per annum, pay reported and positive."""
    def eq(col, val):          # missing codes (NA after clean) count as "not in the universe"
        return df[col].eq(val).fillna(False).astype(bool)
    m = (df["pay"].notna() & (df["pay"] > 0)).fillna(False).astype(bool)
    if "pay_basis_code" in df:
        m &= eq("pay_basis_code", "PA")
    elif "pay_basis" in df:
        m &= eq("pay_basis", "PER ANNUM")
    if "work_schedule_code" in df:
        m &= eq("work_schedule_code", "F")
    elif "work_schedule" in df:
        m &= eq("work_schedule", "FULL-TIME")
    return m


def pay_summary(df: pd.DataFrame, by: list[str] | None = None, universe: Callable | None = pay_universe,
                quantiles=(0.1, 0.25, 0.5, 0.75, 0.9)) -> pd.DataFrame:
    """Mean and percentiles of annualized adjusted basic pay, with coverage. `by=None` gives one
    overall row (group "all").

    records      all records in the group (before the universe filter)
    pay_n        records in the universe with reported pay
    pay_coverage pay_n / records that would be in the universe if their pay were published (redacted
                 records are judged on schedule, pay basis and the other universe conditions): the
                 share of the group the statistic describes. Low coverage means the figure does not
                 describe the group.
    """
    d = df.copy()
    inu = universe(d) if universe else d["pay"].notna()
    d["_in"] = pd.Series(inu, index=d.index).fillna(False).astype(bool)
    if "pay_redacted" in d and d["pay_redacted"].any():
        # stand in a typical reported pay for redacted records, so they pass any pay condition
        known = d.loc[d["pay"] > 0, "pay"]
        filler = float(known.median()) if len(known) else 1.0
        red = d.assign(pay=d["pay"].astype("float64").mask(d["pay_redacted"], filler))
        elig = universe(red) if universe else red["pay"].notna()
        d["_elig"] = d["_in"] | (pd.Series(elig, index=d.index).fillna(False).astype(bool) & d["pay_redacted"])
    else:
        d["_elig"] = d["_in"]
    if not by:
        by = ["group"]
        d["group"] = "all"
    rows = []
    for key, g in d.groupby(by, dropna=False, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        p = g.loc[g["_in"], "pay"]
        r = dict(zip(by, key))
        r.update(records=float(g["n"].sum()), pay_n=int(len(p)),
                 pay_coverage=len(p) / max(int(g["_elig"].sum()), 1),
                 mean_pay=float(p.mean()) if len(p) else np.nan)
        for q in quantiles:
            r[f"p{int(q * 100)}"] = float(p.quantile(q)) if len(p) else np.nan
        r["low_n"] = len(p) < SMALL_N
        rows.append(r)
    return pd.DataFrame(rows).rename(columns={"p50": "median_pay"})


def screen_pay(df: pd.DataFrame, low: float = 15_000, high: float | None = None, by: list[str] | None = None) -> pd.DataFrame:
    """Flag implausible pay before averaging: zero or below `low`, above `high` (pass the statutory
    cap for the pay plans studied, e.g. the Executive Schedule level), and non-annual pay bases."""
    d = df[df["pay"].notna()]
    by = by or (["pay_plan_code"] if "pay_plan_code" in d else [])
    flags = pd.DataFrame({"zero": d["pay"].eq(0), "below_low": d["pay"].between(0, low, inclusive="neither")})
    if high:
        flags["above_high"] = d["pay"] > high
    if "pay_basis_code" in d:
        flags["not_per_annum"] = d["pay_basis_code"].ne("PA")
    t = flags.groupby([d[c] for c in by] if by else (lambda _: "all")).sum() if len(d) else flags
    t["records_with_pay"] = d.groupby(by).size() if by else len(d)
    return t.reset_index()


# ---------------------------------------------------------------- dynamics: rates
def period_key(df: pd.DataFrame, period: str) -> pd.Series:
    if period in df:
        return df[period]
    raise KeyError(f"{period!r} not in data; use 'fiscal_year', 'year' or 'period'")


def average_headcount(emp: pd.DataFrame, by: list[str], period: str = "fiscal_year") -> pd.DataFrame:
    """Denominator for rates: mean of the snapshot headcounts in each period (e.g. the 12 monthly
    snapshots of a fiscal year). A group absent from some of the period's snapshots counts as zero
    there (a new or abolished agency), so its mean is not inflated. `snapshots` is how many snapshots
    of the period the data hold; `snapshots_with_group` how many of them include the group."""
    keys = list(dict.fromkeys(["period", period] + by))
    snap = emp.groupby(keys, dropna=False, observed=True)["n"].sum().reset_index()
    out = snap.groupby([period] + by, dropna=False, observed=True).agg(
        _sum=("n", "sum"), snapshots_with_group=("period", "nunique")).reset_index()
    nsnap = (emp[list(dict.fromkeys(["period", period]))].drop_duplicates()
             .groupby(period, dropna=False).size().rename("snapshots"))
    out = out.merge(nsnap.reset_index(), on=period, how="left")
    out["avg_headcount"] = out.pop("_sum") / out["snapshots"]
    return out[[period] + by + ["avg_headcount", "snapshots", "snapshots_with_group"]]


def action_rate(actions: pd.DataFrame, emp: pd.DataFrame, by: list[str] | None = None,
                period: str = "fiscal_year", where: Callable | None = None, name: str = "actions") -> pd.DataFrame:
    """Accession or separation rate = actions in the period / average headcount in the period.

    Count actions by effective date (`period`, the default here) or by processing month (`file_period`);
    say which. Recent periods are incomplete when late-processed actions are still arriving.
    """
    by = by or []
    a = actions[where(actions)] if where else actions
    num = a.groupby([period] + by, dropna=False, observed=True)["n"].sum().rename(name).reset_index()
    den = average_headcount(emp, by, period)
    t = num.merge(den, on=[period] + by, how="outer")
    # a group with headcount but no matching actions in a period the action files cover has 0, not NA
    covered = t[period].isin(set(actions[period].dropna()))
    t.loc[t[name].isna() & t["avg_headcount"].notna() & covered, name] = 0
    t["rate"] = t[name] / t["avg_headcount"]
    t["low_n"] = t["avg_headcount"].fillna(0) < SMALL_N
    if period in ("fiscal_year", "year"):
        # monthly snapshots exist from 2011 on; fewer than 12 means a partial year or missing snapshots
        t["partial_period"] = t["snapshots"].fillna(0) < 12
    return t


def late_reporting(actions: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """For each effective month, how many actions arrived in the same month's file vs. later files.
    Use it to show whether recent months are still filling in."""
    lag = ((actions["file_period"] // 100 * 12 + actions["file_period"] % 100)
           - (actions["period"] // 100 * 12 + actions["period"] % 100))
    # weight by n: an aggregated row stands for n actions (a row count understates late shares tenfold)
    d = actions.assign(lag=lag, same=actions["n"].where(lag <= 0, 0), late=actions["n"].where(lag > 0, 0))
    t = d.groupby(["period"] + (by or []), dropna=False).agg(
        actions=("n", "sum"), same_month=("same", "sum"), later=("late", "sum"),
        max_lag_months=("lag", "max")).reset_index()
    t["share_late"] = t["later"] / t["actions"]
    return t


def suppress(t: pd.DataFrame, cols: list[str], flag: str = "low_n") -> pd.DataFrame:
    """Blank the listed columns where `flag` is True (for published tables)."""
    t = t.copy()
    t.loc[t[flag].fillna(False).astype(bool), cols] = np.nan
    return t


# ---------------------------------------------------------------- facts registry
def _shared_module(name: str):
    """Load scripts/analysis/<name>.py, the one version every pack shares. Found from the pack's lib/
    (<skill>/sources/<id>/lib/ -> <skill>/scripts/analysis/) or from an analysis folder's lib/<id>/
    (-> lib/, where build_report.py and fetch.py copy the shared files)."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "..", "..", "..", "scripts", "analysis"), os.path.join(here, "..")):
        d = os.path.abspath(d)
        p = os.path.join(d, name + ".py")
        if os.path.exists(p) and d != here:
            if d not in sys.path:
                sys.path.append(d)
            spec = importlib.util.spec_from_file_location(f"_shared_{name}", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError(f"shared {name}.py not found: run from the skill or a folder whose lib/ holds it")


# The Facts registry is shared by every pack (scripts/analysis/facts.py); re-exported here so
# `from <this module> import *` keeps working in analysis.py.
Facts = _shared_module("facts").Facts


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- empirical models
def design_matrix(df: pd.DataFrame, numeric: list[str] = (), categorical: list[str] = (),
                  ref: dict | None = None) -> pd.DataFrame:
    """Intercept + numeric columns + dummies for categorical columns (first or `ref` level omitted)."""
    X = pd.DataFrame({"const": 1.0}, index=df.index)
    for c in numeric:
        X[c] = df[c].astype(float)
    for c in categorical:
        s = df[c].astype("category") if not isinstance(df[c].dtype, pd.CategoricalDtype) else df[c]
        s = s.cat.remove_unused_categories()     # an absent level would make the dummies collinear
        base = (ref or {}).get(c, s.cat.categories[0])
        if base not in s.cat.categories:
            raise ValueError(f"reference level {base!r} of {c!r} does not occur in the data")
        for lev in s.cat.categories:
            if lev != base:
                X[f"{c}[{lev}]"] = (s == lev).astype(float)
    return X


def wls(df: pd.DataFrame, y: str, numeric: list[str] = (), categorical: list[str] = (), weight: str | None = None,
        se: str = "hc1", cluster: str | None = None, ref: dict | None = None) -> pd.DataFrame:
    """Least squares (weighted when `weight` names a column), with robust standard errors.

    Administrative data cover the whole workforce, so coefficients describe it directly; the SEs
    (se="hc1" robust or se="cluster" by e.g. agency_code) only express how noisy a pattern is.
    Use it for composition-adjusted pay gaps, e.g. log(pay) on a group dummy plus grade, occupational
    series, length of service, education level and location (unredacted records only), and say so.
    Returns coef, se, t, p (normal), ci90_low, ci90_high, n_obs.
    """
    cols = [y] + ([weight] if weight else []) + list(numeric) + list(categorical) + ([cluster] if cluster else [])
    d = df.dropna(subset=cols)
    X = design_matrix(d, numeric, categorical, ref)
    Xv, yv = X.to_numpy(float), d[y].to_numpy(float)
    w = d[weight].to_numpy(float) if weight else np.ones(len(d))

    def fit(wt):
        sw = np.sqrt(wt)
        b, *_ = np.linalg.lstsq(Xv * sw[:, None], yv * sw, rcond=None)
        return b
    b = fit(w)
    e = yv - Xv @ b
    XtWX_inv = np.linalg.pinv((Xv * w[:, None]).T @ Xv)
    if se == "cluster":
        if not cluster:
            raise ValueError("se='cluster' needs cluster=")
        S = pd.DataFrame(Xv * (w * e)[:, None]).groupby(d[cluster].to_numpy()).sum().to_numpy()
        G = S.shape[0]
        meat = S.T @ S * G / max(G - 1, 1)
    else:
        u = Xv * (w * e)[:, None]
        n, k = Xv.shape
        meat = u.T @ u * n / max(n - k, 1)
    ses = np.sqrt(np.diag(XtWX_inv @ meat @ XtWX_inv))
    from math import erf, sqrt
    t = b / ses
    p = [2 * (1 - 0.5 * (1 + erf(abs(x) / sqrt(2)))) for x in t]
    return pd.DataFrame({"term": X.columns, "coef": b, "se": ses, "t": t, "p": p,
                         "ci90_low": b - 1.645 * ses, "ci90_high": b + 1.645 * ses, "n_obs": len(d)})


def standardize(df: pd.DataFrame, group: str, cells: list[str], rate: Callable, weight: str = "n",
                target=None) -> pd.DataFrame:
    """Direct standardization (composition adjustment), e.g. age-adjusted rates.

    rate(subframe, weight) -> float is computed in each group x cell; cells are weighted by the
    distribution of `target` group (or the pooled population if target is None). Answers "what would
    group A's rate be with group B's age/education mix?" empirically instead of by judgment.
    """
    base = df if target is None else df[df[group] == target]
    mix = base.groupby(cells, observed=True)[weight].sum()
    mix = mix / mix.sum()
    rows = []
    for g, d in df.groupby(group, observed=True):
        r = d.groupby(cells, observed=True).apply(lambda s: rate(s, weight))
        common = mix.index.intersection(r.index)
        rows.append({group: g, "crude": rate(d, weight),
                     "standardized": float((r.loc[common] * mix.loc[common]).sum() / mix.loc[common].sum()),
                     "cells_used": len(common), "cells_target": len(mix)})
    return pd.DataFrame(rows)


def sensitivity(df: pd.DataFrame, estimate: Callable, variants: dict) -> pd.DataFrame:
    """Re-run one estimate under alternative choices; use it for every judgment call.

    estimate(df) -> dict of named results (e.g. {"rate": 0.071, "headcount": 447225}).
    variants: {"Main specification": lambda d: d, "Count by file month": lambda d: d.assign(period=d.file_period), ...}
    The first variant is the baseline; the table reports each result and its change from the baseline.
    """
    from numbers import Number

    def num(v):                                  # Python and NumPy numbers, not booleans
        return isinstance(v, Number) and not isinstance(v, (bool, np.bool_))
    rows, base = [], None
    for name, transform in variants.items():
        d = transform(df)
        r = estimate(d)
        r = {"variant": name, "n_obs": len(d), **r}
        if base is None:
            base = r
        else:
            for k, v in list(r.items()):
                if k not in ("variant", "n_obs") and num(v) and num(base.get(k)):
                    r[f"{k}_change"] = v - base[k]
        rows.append(r)
    return pd.DataFrame(rows)

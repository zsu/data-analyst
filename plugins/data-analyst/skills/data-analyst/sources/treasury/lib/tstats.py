"""Time-series helpers for Treasury Fiscal Data (pandas).

Import from an analysis script:
    import sys, os
    OUT = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [os.path.join(OUT, "lib"), os.path.join(OUT, "lib", "treasury"), r"<skill>/sources/treasury/lib"]
    # (the full path layout, with facts and combine, is in SKILL.md section 4)
    from tstats import *

    debt = load(OUT, "debt")                         # numbers typed from fetch_spec.json, record_date as datetime
    fy_end = period_end(debt, "FY", ["tot_pub_debt_out_amt"])          # a STOCK: value at the end of each FY
    flows = period_sum(mts, "FY", ["current_month_gross_rcpt_amt"])    # a FLOW: sum within each FY (+ n_obs, complete)

Stocks vs. flows: debt outstanding, the operating cash balance and rates are levels (take the value at
the end of the period, or the average); receipts, outlays, deficits and interest expense are flows (sum
the months). Summing a stock or taking the last value of a flow is the most common silent error.

Fiscal years: the federal fiscal year runs from October 1 to September 30 and is named for the
calendar year in which it ends (FY2025 = Oct 1, 2024 to Sep 30, 2025). Most endpoints also carry
record_fiscal_year; fiscal_year() reproduces it from record_date.

Facts: every number cited in report prose is registered, traced to an exhibit CSV cell, and referenced
in report.json as {{name}}. build_report.py fills the value; audit_report.py re-checks each fact.
    facts = Facts(OUT)
    facts.add("debt_fy2025", table, where={"fiscal_year": 2025}, column="tot_pub_debt_out_amt",   # dollars
              source="exhibits/exhibit_1.csv", fmt="usd_t:2", label="Total public debt, end of FY2025")
    # The value is read back from the saved CSV cell (it must match the table within the CSV's rounding).
    # usd_b / usd_t take dollars; for a column already in billions use usd_b_scaled.
    facts.add("growth", value=x, fmt="pct:1", derivation="debt_fy2025 / debt_fy2015 - 1", label="...")
    facts.save()
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Callable

import sys

import numpy as np
import pandas as pd

for _s in (sys.stdout, sys.stderr):      # Windows pipes default to cp1252: "−" in a label would crash a print
    if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

NUMERIC_TYPES = {"CURRENCY", "CURRENCY0", "CURRENCY3", "NUMBER", "PERCENTAGE", "INTEGER", "RATE", "DECIMAL"}
TEXT_LIKE = re.compile(r"(_desc|_cd|_nm|_name|_txt|_id|_nbr|_code)$")
NUMERIC_LIKE = re.compile(r"(_amt|_rate|_pct|_cnt|_bal|_qty|_val|_rt|_yield|_price)$")


# ---------------------------------------------------------------- loading
def data_path(out: str, filename: str) -> str:
    """<out>/data/treasury/<filename> (the data-analyst layout); falls back to <out>/data/<filename>
    (single-source runs)."""
    new = os.path.join(out, "data", "treasury", filename)
    old = os.path.join(out, "data", filename)
    return new if os.path.exists(new) or not os.path.exists(old) else old


def load(out: str, name: str, parse_numbers: bool = True) -> pd.DataFrame:
    """Read <out>/data/treasury/<name>.csv (or <out>/data/<name>.csv) with types from fetch_spec.json (the API's dataTypes).

    Amount, rate and count fields become floats; record_date becomes datetime; fiscal/calendar
    year-quarter-month fields become ints. When the types are unknown (files ingested by hand), field-name
    suffixes decide (_amt, _rate, _pct ... numeric; _desc, _cd ... text). Prints any value that could not
    be read as a number so it is never silently dropped.
    """
    path = data_path(out, f"{name}.csv")
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    types = {}
    prov = data_path(out, "fetch_spec.json")
    if os.path.exists(prov):
        types = (json.load(open(prov, encoding="utf-8")).get("datasets", {}).get(name, {}) or {}).get("data_types", {})
    for c in df.columns:
        t = (types.get(c) or "").upper()
        if c == "record_date" or t == "DATE" or c.endswith("_date") or c.endswith("_dt"):
            df[c] = pd.to_datetime(df[c], errors="coerce")
        elif t in ("YEAR", "QUARTER", "MONTH", "DAY") or re.fullmatch(r"record_(fiscal|calendar)_\w+", c):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        elif parse_numbers and (t in NUMERIC_TYPES or (not t and NUMERIC_LIKE.search(c) and not TEXT_LIKE.search(c))):
            raw = df[c]
            num = pd.to_numeric(raw.str.replace(r"^'|[,$%\s]", "", regex=True), errors="coerce")
            bad = raw.notna() & num.isna()
            if bad.any():
                print(f"[tstats] {name}.{c}: {int(bad.sum())} value(s) not numeric, e.g. {raw[bad].iloc[0]!r} "
                      "(kept as NaN)")
            df[c] = num
    return df


def fiscal_year(dates: pd.Series) -> pd.Series:
    """Federal fiscal year (Oct 1 - Sep 30), named for the year it ends."""
    d = pd.to_datetime(dates)
    return (d.dt.year + (d.dt.month >= 10).astype(int)).astype("Int64")


def add_periods(df: pd.DataFrame, date_col: str = "record_date") -> pd.DataFrame:
    d = df.copy()
    dt = pd.to_datetime(d[date_col])
    d["fiscal_year"] = fiscal_year(dt)
    d["calendar_year"] = dt.dt.year.astype("Int64")
    d["year_month"] = dt.dt.to_period("M").astype(str)
    d["quarter"] = dt.dt.to_period("Q").astype(str)
    return d


_KEY = {"FY": "fiscal_year", "CY": "calendar_year", "M": "year_month", "Q": "quarter"}
_MONTHS = {"FY": 12, "CY": 12, "Q": 3, "M": 1}


def _warn_incomplete(res: pd.DataFrame, label: str, key: str, have: str | None = None, need: int | None = None) -> pd.DataFrame:
    """Print one line per period whose 'complete' flag is False, so a partial latest year is never reported
    as a full one by accident. Label it as partial or provisional (with its coverage) wherever it is used."""
    if "complete" in res:
        for _, r in res[~res["complete"].astype(bool)].iterrows():
            cov = f" ({r[have]} of {need})" if have and need else (f" (last observation {r['as_of_date']:%Y-%m-%d})"
                                                                   if "as_of_date" in r else "")
            if "last_obs" in r and pd.notna(r["last_obs"]):
                cov += f" (data end {r['last_obs']:%Y-%m-%d})"
            print(f"[tstats] WARNING: {label}: {key} {r[key]} is incomplete{cov}; label it partial/provisional")
    return res


END_SLACK_DAYS = 4      # daily data may end up to 4 days before a period's last day (weekend + holiday)


def _period_last_day(key: str, v) -> pd.Timestamp:
    if pd.isna(v):
        return pd.NaT
    if key == "fiscal_year":
        return pd.Timestamp(int(v), 9, 30)
    if key == "calendar_year":
        return pd.Timestamp(int(v), 12, 31)
    return pd.Period(str(v)).end_time.normalize()      # quarter "2026Q1" or month "2026-03"


def _reaches_end(d: pd.DataFrame, date_col: str, key: str, periods: pd.Series, last: pd.Series) -> pd.Series:
    """Daily data (several dates in a month) must run to within END_SLACK_DAYS of the period's last day, so
    data through September 10 is not a complete fiscal year. Monthly data passes here; month counts decide."""
    if not (d.groupby("year_month")[date_col].nunique() > 1).any():
        return pd.Series(True, index=periods.index)
    ends = pd.to_datetime(periods.map(lambda v: _period_last_day(key, v)))
    return pd.to_datetime(last).values >= (ends - pd.Timedelta(days=END_SLACK_DAYS)).values


def period_end(df: pd.DataFrame, freq: str, cols: list[str], by: list[str] | None = None,
               date_col: str = "record_date") -> pd.DataFrame:
    """STOCKS: the last observation in each period (FY, CY, Q, M), with its date and a 'complete' flag.

    complete = the last observation falls in the period's final month (FY: September; CY: December;
    Q: the quarter's third month) and, for daily data, within a few days of the period's last day, so a
    partial latest year is never mistaken for a year-end value.
    """
    d = add_periods(df, date_col).sort_values(date_col)
    key = _KEY[freq]
    g = d.groupby((by or []) + [key], dropna=False).tail(1)
    res = g[(by or []) + [key, date_col] + cols].rename(columns={date_col: "as_of_date"}).reset_index(drop=True)
    m = pd.to_datetime(res["as_of_date"]).dt.month
    month_ok = {"FY": m == 9, "CY": m == 12, "Q": m.isin([3, 6, 9, 12]), "M": pd.Series(True, index=res.index)}[freq]
    res["complete"] = month_ok & _reaches_end(d, date_col, key, res[key], res["as_of_date"])
    return _warn_incomplete(res, "period_end", key)


def period_mean(df: pd.DataFrame, freq: str, cols: list[str], by: list[str] | None = None,
                date_col: str = "record_date", expect: int | None = None, warn: bool = True) -> pd.DataFrame:
    """Average level within each period (e.g. average daily cash balance, average rate), with n_obs,
    n_months (distinct months observed) and a 'complete' flag (all months of the period observed;
    expect = months a complete period has, default FY/CY 12, Q 3)."""
    d = add_periods(df, date_col)
    key = _KEY[freq]
    g = d.groupby((by or []) + [key], dropna=False)
    res = g[cols].mean().reset_index()
    res["n_obs"] = g.size().values
    res["n_months"] = g["year_month"].nunique().values
    res["last_obs"] = g[date_col].max().values
    exp = expect or _MONTHS[freq]
    res["complete"] = (res["n_months"] >= exp) & _reaches_end(d, date_col, key, res[key], res["last_obs"])
    return _warn_incomplete(res, "period_mean", key, "n_months", exp) if warn else res


def period_sum(df: pd.DataFrame, freq: str, cols: list[str], by: list[str] | None = None,
               date_col: str = "record_date", expect: int | None = None) -> pd.DataFrame:
    """FLOWS: sum within each period, with n_obs (distinct months) and a 'complete' flag.

    expect = months a complete period has (FY/CY 12, Q 3). Use it on MONTHLY flow rows (one row per
    month per line item), never on cumulative fiscal-year-to-date rows (take period_end of those instead).
    """
    cum = [c for c in cols if re.search(r"(?i)(?:^|_)(?:fytd|cytd|ytd)(?:_|$)", c)]
    if cum:                              # summing running totals counts each month many times over
        raise ValueError(f"period_sum: {cum} are cumulative (year to date): take their value at the period's end "
                         "with period_end(), or sum the monthly column (e.g. month_expense_amt)")
    d = add_periods(df, date_col)
    key = _KEY[freq]
    g = d.groupby((by or []) + [key], dropna=False)
    res = g[cols].sum(min_count=1).reset_index()
    res["n_obs"] = g["year_month"].nunique().values
    res["last_obs"] = g[date_col].max().values
    exp = expect or _MONTHS[freq]
    res["complete"] = (res["n_obs"] >= exp) & _reaches_end(d, date_col, key, res[key], res["last_obs"])
    return _warn_incomplete(res, "period_sum", key, "n_obs", exp)


def trailing_sum(s: pd.Series, n: int = 12) -> pd.Series:
    """Rolling n-period total of a monthly flow (12-month trailing deficit, interest ...). NaN until n values."""
    return s.rolling(n, min_periods=n).sum()


def pct_change(new: float, old: float) -> float:
    return float(new) / float(old) - 1.0


def cagr(first: float, last: float, years: float) -> float:
    """Compound annual growth rate."""
    return (float(last) / float(first)) ** (1.0 / years) - 1.0


def decompose(base0: float, base1: float, rate0: float, rate1: float) -> dict:
    """Split the change in a product (amount = base x rate; e.g. interest = debt x effective rate,
    outlays = beneficiaries x average benefit) into a base effect and a rate effect that add up
    exactly to the change (Shapley / midpoint: the interaction is split evenly)."""
    total = base1 * rate1 - base0 * rate0
    base_eff = (base1 - base0) * (rate0 + rate1) / 2
    rate_eff = (rate1 - rate0) * (base0 + base1) / 2
    return {"change": total, "base_effect": base_eff, "rate_effect": rate_eff,
            "base_share": base_eff / total if total else float("nan"),
            "rate_share": rate_eff / total if total else float("nan")}


def yoy(df: pd.DataFrame, col: str, periods: int = 1, by: list[str] | None = None) -> pd.Series:
    """Change from `periods` rows earlier (sorted input). For monthly data use periods=12."""
    if by:
        return df.groupby(by)[col].pct_change(periods)
    return df[col].pct_change(periods)


# ---------------------------------------------------------------- context series (context.py)
def _context(out: str, kind: str) -> pd.DataFrame:
    p = data_path(out, f"context_{kind}.csv")
    if not os.path.exists(p):
        raise FileNotFoundError(f"{p} is missing: run scripts/fetch.py --source treasury --context {kind} "
                                f"--out {out} first")
    d = pd.read_csv(p, parse_dates=["date"])
    return d


def cpi_index(out: str, freq: str = "FY") -> pd.DataFrame:
    """Average CPI-U by fiscal year (FY), calendar year (CY) or month (M), with n_months and complete flag."""
    c = _context(out, "cpi").rename(columns={"date": "record_date", "value": "cpi"})
    if freq == "M":
        c["year_month"] = c["record_date"].dt.to_period("M").astype(str)
        return c[["year_month", "cpi"]]
    res = period_mean(c, freq, ["cpi"], warn=False).drop(columns=["n_obs"])
    return _warn_incomplete(res, "cpi_index", _KEY[freq], "n_months", _MONTHS[freq])


def deflate(values: pd.Series, period_cpi: pd.Series, base_cpi: float) -> pd.Series:
    """Express nominal values in the dollars of the base period: value * base_cpi / period_cpi."""
    return values.astype(float) * float(base_cpi) / period_cpi.astype(float)


def _bea_gdp(out: str) -> pd.DataFrame | None:
    """Quarterly nominal GDP (billions, SAAR) from the bea pack's download (<out>/data/bea/bea_data.csv, NIPA
    T10105 line 1, series A191RC), when the analysis fetched GDP from BEA instead of --context gdp."""
    p = os.path.join(out, "data", "bea", "bea_data.csv")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p, dtype={"series_code": str, "frequency": str})
    d = d[(d["series_code"] == "A191RC") & (d["frequency"] == "Q")]
    if d.empty:
        return None
    d = d.drop_duplicates("date")
    return pd.DataFrame({"date": pd.to_datetime(d["date"]),
                         "value": pd.to_numeric(d["value"]) * 10.0 ** (pd.to_numeric(d["unit_mult"]) - 9)})


def fy_gdp(out: str) -> pd.DataFrame:
    """Fiscal-year GDP (billions) = average of the four quarterly SAAR values Oct-Sep (Q4 of the prior
    calendar year + Q1-Q3), the convention OMB and CBO use. Needs quarterly data: `--context gdp` (FRED/BEA)
    or the bea pack's T10105 in data/bea/; refuses the calendar-year World Bank fallback rather than mixing
    calendar and fiscal years silently."""
    g = None if os.path.exists(data_path(out, "context_gdp.csv")) else _bea_gdp(out)
    if g is None:
        meta = json.load(open(data_path(out, "context_sources.json"), encoding="utf-8"))["gdp"]
        if meta.get("frequency") != "quarterly":
            raise ValueError("GDP is calendar-year only (World Bank fallback). Use cy_gdp() and calendar-year "
                             "data, or ask the user to upload quarterly GDP.")
        g = _context(out, "gdp")
    g["fiscal_year"] = fiscal_year(g["date"])
    r = g.groupby("fiscal_year").agg(gdp_billions=("value", "mean"), n_quarters=("value", "size")).reset_index()
    r["complete"] = r["n_quarters"] == 4
    # a first year cut by where the data start is flagged in `complete` but not warned about (ask for quarters
    # from Q4 of the year before the first fiscal year); an incomplete latest year is the one that misleads
    warn = r[~r["complete"] & (r["fiscal_year"] != r["fiscal_year"].min())] if len(r) > 1 else r
    _warn_incomplete(warn, "fy_gdp (an incomplete year averages the quarters available)", "fiscal_year",
                     "n_quarters", 4)
    return r


def cy_gdp(out: str) -> pd.DataFrame:
    g = _context(out, "gdp")
    g["calendar_year"] = g["date"].dt.year
    meta = json.load(open(data_path(out, "context_sources.json"), encoding="utf-8"))["gdp"]
    r = g.groupby("calendar_year").agg(gdp_billions=("value", "mean"), n_obs=("value", "size")).reset_index()
    n = 4 if meta.get("frequency") == "quarterly" else 1
    r["complete"] = r["n_obs"] == n
    return _warn_incomplete(r, "cy_gdp", "calendar_year", "n_obs", n)


# ---------------------------------------------------------------- checks
def check_unique(df: pd.DataFrame, keys: list[str], label: str = "") -> None:
    """Raise if the keys do not identify one row: catches summary rows mixed with detail rows, or a
    line item that appears under two parents, before they are double counted."""
    dup = df.duplicated(keys, keep=False)
    if dup.any():
        ex = df.loc[dup, keys].head(6).to_string(index=False)
        raise ValueError(f"{label or 'table'}: {int(dup.sum())} rows share the keys {keys}; filter to one level "
                         f"(e.g. record_type_cd, sequence_level_nbr, parent) before summing. Examples:\n{ex}")


def check_total(parts_sum: float, total: float, label: str, tol: float = 1e-6, abs_tol: float = 0.0,
                strict: bool = True) -> float:
    """Compare a sum of components with the published total; prove a breakdown neither misses nor double
    counts lines. Passes when |difference| <= max(tol * |total|, abs_tol), in the data's own units:
      tol      relative (default 1e-6: $7.4 million on $7.4 trillion; a missing line item is far larger)
      abs_tol  absolute, for data rounded to millions (DTS, MSPD): about the number of components
    strict (default) raises on a mismatch, because a mismatch means a filter or hierarchy error. Pass
    strict=False only for comparisons known to differ (e.g. two datasets with different coverage).
    Returns the relative gap."""
    diff = float(parts_sum) - float(total)
    gap = diff / float(total) if total else float("nan")
    ok = abs(diff) <= max(tol * abs(float(total)), abs_tol)
    msg = (f"{label}: components {parts_sum:,.2f} vs published total {total:,.2f} "
           f"(difference {diff:+,.2f}, {gap:+.6%}) {'OK' if ok else 'MISMATCH'}")
    print(f"[tstats] {msg}")
    if not ok and strict:
        raise ValueError(msg + ". Fix the rows (level, double counting, a missing line) or, for a known "
                         "difference, pass strict=False and explain it.")
    return gap


def sensitivity(estimate: Callable[[], dict], variants: dict[str, Callable[[], dict]] | None = None) -> pd.DataFrame:
    """Run one estimate under alternative choices (FY vs CY, end-of-period vs average, nominal vs real,
    including/excluding a line). variants: {"Main": fn, "Alternative": fn2}; the first is the baseline."""
    rows, base = [], None
    for name, fn in (variants or {"Main": estimate}).items():
        r = {"variant": name, **fn()}
        if base is None:
            base = r
        else:
            for k, v in list(r.items()):
                if k != "variant" and isinstance(v, (int, float)) and isinstance(base.get(k), (int, float)):
                    r[f"{k}_change"] = v - base[k]
        rows.append(r)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- facts registry
def _csv_cell(path: str, where: dict, column: str, label: str) -> tuple[float, str]:
    """The one cell of `column` in the row matching `where`, read the way audit_report.py reads it."""
    import csv
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        cols = rd.fieldnames or []
        rows = [r for r in rd if all(str(r.get(k)) == str(v) for k, v in where.items())]
    if column not in cols:
        raise ValueError(f"{label}: column {column!r} is not in {os.path.basename(path)}; its columns are {cols}")
    if len(rows) != 1:
        raise ValueError(f"{label}: {len(rows)} rows of {os.path.basename(path)} match {where} (need exactly 1; "
                         f"the CSV's own text must equal each value, e.g. 2026 vs 2026.0)")
    text = (rows[0][column] or "").strip()
    try:
        return float(text), text
    except ValueError:
        raise ValueError(f"{label}: {column} = {text!r} in {os.path.basename(path)} is not a number") from None


def _printed_precision(text: str) -> float:
    """Half a unit of the last printed digit ("1992.47" -> 0.005), the rounding a saved CSV may carry."""
    from decimal import Decimal, InvalidOperation
    try:
        return 0.5 * 10 ** Decimal(text).as_tuple().exponent
    except (InvalidOperation, TypeError):
        return 0.0


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

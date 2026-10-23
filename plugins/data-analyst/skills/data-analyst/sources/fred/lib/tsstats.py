"""Time-series helpers for FRED data, plus the Facts registry that the report cites.

    import sys; sys.path.insert(0, r"<out>/lib/fred")   # copied there by fetch.py; or <skill>/sources/fred/lib
    from tsstats import *
    df = load(OUT)                                   # <OUT>/data/fred/fred_observations.csv, long format
    q = wide(df, ["GDPC1", "GDPPOT"])                # quarterly, one column per variant, gaps kept as NaN,
                                                     # dates after today (projections) dropped
    gap = q["GDPC1"] / q["GDPPOT"] - 1               # output gap as a share (0.012 = 1.2 percent)

Every change is computed on a regular calendar index, so a missing month (October 2025 for the CPI) yields
NaN instead of silently pairing the wrong months. Conversions to a lower frequency (to_frequency,
fiscal_year) refuse incomplete periods unless told otherwise; FRED's own frequency conversion does not.
Shares are stored as 0-1 so that factfmt's pct/pp formats apply; FRED rates that are already in percent
(UNRATE 4.2) use pct100 / pp100.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

STEP_MONTHS = {"M": 1, "Q": 3, "S": 6, "A": 12}
PER_YEAR = {"D": 260, "W": 52, "M": 12, "Q": 4, "S": 2, "A": 1}


def period_end(dates, freq: str):
    """Last day of the period each date stands for. FRED dates most periods by their first day (2026-10-01 =
    2026 Q4) and fiscal-year dollar series by their last day (2025-09-30 = FY2025); daily and weekly dates are
    the day itself."""
    d = pd.DatetimeIndex(pd.to_datetime(dates))
    if freq not in STEP_MONTHS:
        return d
    starts = d.day == 1
    ends = d + pd.DateOffset(months=STEP_MONTHS[freq]) - pd.Timedelta(days=1)
    return pd.DatetimeIndex(np.where(starts, ends, d))


def data_file(out_dir: str, name: str) -> str:
    """<out>/data/fred/<name> (data-analyst layout); falls back to <out>/data/<name> (single-source runs)."""
    new = os.path.join(out_dir, "data", "fred", name)
    old = os.path.join(out_dir, "data", name)
    return new if os.path.exists(new) or not os.path.exists(old) else old


# ---------------------------------------------------------------- loading
def load(path: str) -> pd.DataFrame:
    """Read fred_observations.csv (or the folder that holds data/fred_observations.csv) into a long DataFrame."""
    if os.path.isdir(path):
        path = data_file(path, "fred_observations.csv")
    df = pd.read_csv(path, dtype={"series_id": str, "label": str, "variant": str, "vintage": str,
                                  "realtime_start": str, "realtime_end": str})
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def series_info(path: str) -> pd.DataFrame:
    """data/series.csv (titles, units, frequency, fiscal_year, source, copyright, citation), by variant."""
    if os.path.isdir(path):
        path = data_file(path, "series.csv")
    return pd.read_csv(path, dtype=str).set_index("variant")


def infer_freq(s) -> str:
    """D, W, M, Q, S or A from the spacing of the dates."""
    idx = pd.DatetimeIndex(s.index if isinstance(s, (pd.Series, pd.DataFrame)) else s).sort_values().unique()
    if len(idx) < 2:
        return "A"
    days = np.median(np.diff(idx.values).astype("timedelta64[D]").astype(int))
    if days <= 4:
        return "D"
    if days <= 10:
        return "W"
    steps = np.diff(idx.year * 12 + idx.month)
    steps = steps[steps > 0]
    step = int(np.median(steps)) if len(steps) else 1
    return {3: "Q", 6: "S", 12: "A"}.get(step, "M")


def wide(df: pd.DataFrame, variants=None, vintage: str = "latest", start=None, end=None,
         include_future: bool = False) -> pd.DataFrame:
    """One column per variant (series ID, or ID_transform for FRED-side conversions) on a regular calendar
    index; dates FRED did not publish stay NaN. All variants must share one frequency (convert first with
    to_frequency). Daily and weekly data keep FRED's own dates (no regular index).

    include_future=False drops periods that have not ended yet: CBO's GDPPOT, NGDPPOT and NROU are projected
    about ten years ahead, and a quarter that started but has not ended (2026 Q4 on October 20, 2026) can only
    hold a projection. A projection must never be read as data. Ask for them explicitly when the projection is
    the point (and say so in the text).

    The regular index keeps the series' own day of the month: FRED dates fiscal-year dollar series on
    September 30 (FYOINT), so their annual index steps from September 30 to September 30.
    """
    d = df[df["vintage"].astype(str) == str(vintage)]
    if variants is not None:
        variants = [variants] if isinstance(variants, str) else list(variants)
        missing = sorted(set(variants) - set(d["variant"]))
        if missing:
            raise KeyError(f"not in the data (vintage {vintage}): {missing}; variants here: "
                           f"{sorted(d['variant'].unique())}")
        d = d[d["variant"].isin(variants)]
    freqs = {v: infer_freq(pd.DatetimeIndex(d.loc[d["variant"] == v, "date"])) for v in d["variant"].unique()}
    if len(set(freqs.values())) > 1:
        raise ValueError(f"mixed frequencies {freqs}: build one wide() per frequency, or convert with to_frequency")
    f = next(iter(freqs.values()), "M")
    if not include_future:
        d = d[period_end(d["date"], f) <= pd.Timestamp(date.today())]
    w = d.pivot_table(index="date", columns="variant", values="value", aggfunc="first", dropna=False)
    if w.empty:
        return w
    if f in STEP_MONTHS:
        idx = [w.index.min()]
        while idx[-1] < w.index.max():
            idx.append(idx[0] + pd.DateOffset(months=STEP_MONTHS[f] * len(idx)))
        w = w.reindex(pd.DatetimeIndex(idx))
    w.index.name = "date"
    if start is not None:
        w = w[w.index >= pd.Timestamp(start)]
    if end is not None:
        w = w[w.index <= pd.Timestamp(end)]
    if variants is not None:
        w = w[[v for v in variants if v in w.columns]]
    w.columns.name = None
    return w


def vintages(df: pd.DataFrame, variant: str) -> pd.DataFrame:
    """One column per vintage of a variant (as-of dates, first_release, latest), indexed by date."""
    d = df[df["variant"] == variant]
    w = d.pivot_table(index="date", columns="vintage", values="value", aggfunc="first")
    w.columns.name = None
    return w


def real_time(df: pd.DataFrame, variant: str, as_of) -> pd.Series:
    """From a full revision history ("vintage": "all"): the series as it was known on as_of."""
    d = df[df["variant"] == variant].copy()
    t = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    end = d["realtime_end"].fillna("9999-12-31").replace("", "9999-12-31")
    d = d[(d["realtime_start"] <= t) & (end >= t)]
    return d.set_index("date")["value"].sort_index()


def gaps(s: pd.Series) -> list[str]:
    """Dates inside the series' span with no value (not collected, not published)."""
    s = s.loc[s.first_valid_index():s.last_valid_index()] if s.notna().any() else s
    return [d.strftime("%Y-%m-%d") for d in s.index[s.isna()]]


def last_valid(s: pd.Series) -> tuple[pd.Timestamp, float]:
    d = s.last_valid_index()
    return d, float(s.loc[d])


def common_end(*series: pd.Series) -> pd.Timestamp:
    """The last date every series has a value for: cut comparisons there (projections, lagging releases)."""
    return min(s.last_valid_index() for s in series)


# ---------------------------------------------------------------- changes
def change(s: pd.Series, periods: int = 1, kind: str = "pct") -> pd.Series:
    """Change over `periods` periods on the regular index: pct (share, 0.031), net (units), or log."""
    prev = s.shift(periods)
    if kind == "pct":
        return s / prev - 1
    if kind == "net":
        return s - prev
    if kind == "log":
        return np.log(s / prev)
    raise ValueError(kind)


def yoy(s: pd.Series, kind: str = "pct") -> pd.Series:
    """Change from the same period a year earlier (12 months, 4 quarters ...)."""
    return change(s, PER_YEAR[infer_freq(s)], kind)


def annualized(s: pd.Series, periods: int = 1, per_year: int | None = None) -> pd.Series:
    """Compound annual rate of change over `periods` periods. Quarterly GDP growth as BEA reports it is
    annualized(q, 1) (the quarter's change at an annual rate)."""
    ppy = per_year or PER_YEAR[infer_freq(s)]
    return (s / s.shift(periods)) ** (ppy / periods) - 1


def q4q4(s: pd.Series) -> pd.Series:
    """Fourth-quarter-over-fourth-quarter growth (the measure CBO and the Federal Reserve use for annual
    growth of GDP and inflation), indexed by year. Monthly input is averaged to quarters first (complete
    quarters only)."""
    if infer_freq(s) == "M":
        s = to_frequency(s, "Q")["value"]
    if infer_freq(s) != "Q":
        raise ValueError("q4q4 needs quarterly or monthly data")
    q4 = s[s.index.month == 10]
    out = q4 / q4.shift(1, freq=pd.DateOffset(years=1)).reindex(q4.index) - 1
    out.index = out.index.year
    out.index.name = "year"
    return out


def cagr(s: pd.Series, start, end) -> float:
    """Compound annual growth rate between two dates (both must have values)."""
    a, b = s.loc[pd.Timestamp(start)], s.loc[pd.Timestamp(end)]
    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    if any(pd.isna([a, b])) or years <= 0:
        return float("nan")
    return float((b / a) ** (1 / years) - 1)


def change_between(s: pd.Series, start, end, kind: str = "pct") -> float:
    a, b = s.loc[pd.Timestamp(start)], s.loc[pd.Timestamp(end)]
    if pd.isna(a) or pd.isna(b):
        return float("nan")
    return float(b / a - 1 if kind == "pct" else b - a)


def rebase(s: pd.Series, base) -> pd.Series:
    """Index to 100 at a date, or at the mean of a (start, end) period."""
    if isinstance(base, (tuple, list)):
        ref = s.loc[pd.Timestamp(base[0]):pd.Timestamp(base[1])].mean()
    else:
        ref = s.loc[pd.Timestamp(base)]
    return 100 * s / ref


def deflate(nominal: pd.Series, price: pd.Series, base) -> pd.Series:
    """Real values in dollars of the base date (or the mean of a (start, end) period) using a price index on
    the same dates (convert the index to the nominal series' frequency first)."""
    p = price.reindex(nominal.index)
    ref = (price.loc[pd.Timestamp(base[0]):pd.Timestamp(base[1])].mean() if isinstance(base, (tuple, list))
           else price.loc[pd.Timestamp(base)])
    return nominal * ref / p


# ---------------------------------------------------------------- frequency and fiscal years
def fiscal_year_of(d) -> int:
    """Federal fiscal year (October to September) a date falls in: FY2025 = Oct 2024 to Sep 2025."""
    d = pd.Timestamp(d)
    return d.year + (1 if d.month >= 10 else 0)


def fiscal_year_label(s: pd.Series) -> pd.Series:
    """Re-index a FRED fiscal-year series by its fiscal year. FRED dates fiscal-year dollar amounts at the end
    of the year (2025-09-30) and some percent-of-GDP series at January 1 (2025-01-01); both are FY2025, and
    fiscal_year_of gives 2025 for both."""
    out = s.copy()
    out.index = [fiscal_year_of(d) for d in s.index]
    out.index.name = "fiscal_year"
    if out.index.duplicated().any():
        raise ValueError("two observations map to one fiscal year: is this really a fiscal-year series?")
    return out


def to_frequency(s: pd.Series, to: str = "A", how: str = "mean", require_full: bool = True) -> pd.DataFrame:
    """Convert daily/weekly/monthly/quarterly data to M (from daily or weekly only), Q, A (calendar year) or FY
    (federal fiscal year).

    how: mean (rates, prices, levels at an annual rate such as GDP), sum (flows reported per period, such as
    monthly outlays), last (end of period: debt, a stock). Returns columns value and n_periods, indexed by
    the period start (Q, A) or by fiscal year (FY). A period with a missing month or quarter is NaN when
    require_full (the default): an 11-month mean is not an annual average.
    """
    s = s.dropna() if infer_freq(s) in ("D", "W") else s
    f = infer_freq(s)
    if to == "M" and f not in ("D", "W"):
        raise ValueError("to='M' converts daily or weekly data; this series is already monthly or coarser")
    need = None if f in ("D", "W") else {"Q": {"M": 3}, "A": {"M": 12, "Q": 4}, "FY": {"M": 12, "Q": 4}}.get(to, {}).get(f)
    if to == "M":
        key = s.index.to_period("M").start_time
    elif to == "Q":
        key = s.index.to_period("Q").start_time
    elif to == "A":
        key = pd.to_datetime(s.index.year.astype(str) + "-01-01")
    elif to == "FY":
        key = [fiscal_year_of(d) for d in s.index]
    else:
        raise ValueError("to must be M, Q, A or FY")
    g = s.groupby(key)
    agg = {"mean": g.mean, "sum": g.sum, "last": g.last}[how]()
    out = pd.DataFrame({"value": agg, "n_periods": g.count()})
    out = out[out["n_periods"] > 0]
    if need and require_full:
        out.loc[out["n_periods"] < need, "value"] = np.nan
    if f in ("D", "W") and require_full and len(out) > 1:
        # daily data: the first and last period may be partial; flag periods with clearly fewer days
        typical = out["n_periods"].median()
        out.loc[out["n_periods"] < 0.8 * typical, "value"] = np.nan
    if f in ("D", "W") and require_full and len(out):
        # the first and last period must be covered to their ends: 42 weeks of a year are not its average
        step = pd.Timedelta(days=8 if f == "W" else 4)
        first, last = s.index.min(), s.index.max()
        k0, k1 = out.index[0], out.index[-1]
        if to == "FY":
            start0, end1 = pd.Timestamp(f"{k0 - 1}-10-01"), pd.Timestamp(f"{k1}-09-30")
        else:
            per = {"M": "M", "Q": "Q", "A": "Y"}[to]
            start0, end1 = pd.Period(k0, per).start_time, pd.Period(k1, per).end_time.normalize()
        if first > start0 + step:
            out.loc[k0, "value"] = np.nan
        if last < end1 - step:
            out.loc[k1, "value"] = np.nan
    out.index.name = "fiscal_year" if to == "FY" else "date"
    return out


def share_of(numerator: pd.Series, denominator: pd.Series, scale: float = 1.0) -> pd.Series:
    """numerator / (denominator * scale) on matching dates, as a share (0-1). scale converts units: interest
    outlays in millions over GDP in billions -> scale=1000. Both must be on the same basis (fiscal year with
    fiscal year)."""
    j = pd.concat([numerator.rename("n"), denominator.rename("d")], axis=1, join="inner")
    return j["n"] / (j["d"] * scale)


def moving_average(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def extreme(s: pd.Series, start=None, end=None, how: str = "max") -> tuple[pd.Timestamp, float]:
    """(first date, value) of the series maximum or minimum. A value reached on several dates (rates are
    rounded) is printed with all its dates: say "in August and November", not only the first."""
    x = s.loc[pd.Timestamp(start) if start else None:pd.Timestamp(end) if end else None].dropna()
    d = x.idxmax() if how == "max" else x.idxmin()
    ties = x.index[x == x.loc[d]]
    if len(ties) > 1:
        print(f"[tsstats] extreme: {float(x.loc[d])} is reached on {len(ties)} dates: "
              + ", ".join(t.strftime("%Y-%m-%d") for t in ties[:12]), file=sys.stderr)
    return d, float(x.loc[d])


# ---------------------------------------------------------------- recessions
def recession_periods(usrec: pd.Series, start=None) -> list[tuple[str, str]]:
    """NBER recessions from FRED's USREC (1 in a recession month): [(first month, last month), ...]. Use the
    downloaded series (spec "recessions": true), never a list from memory."""
    s = usrec.dropna()
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    out, begin, prev = [], None, None
    for d, v in s.items():
        if v >= 0.5 and begin is None:
            begin = d
        if v < 0.5 and begin is not None:
            out.append((begin.strftime("%Y-%m-%d"), prev.strftime("%Y-%m-%d")))
            begin = None
        prev = d
    if begin is not None:
        out.append((begin.strftime("%Y-%m-%d"), prev.strftime("%Y-%m-%d")))
    return out


def in_recession(d, periods: list[tuple[str, str]]) -> bool:
    d = pd.Timestamp(d)
    return any(pd.Timestamp(a) <= d <= pd.Timestamp(b) + pd.offsets.MonthEnd(0) for a, b in periods)


def save_recessions(out_dir: str, periods: list[tuple[str, str]]) -> str:
    """Write exhibits/recessions.csv (start, end): build_report shades these spans on line charts whose
    chart spec says "recessions": true."""
    path = os.path.join(out_dir, "exhibits", "recessions.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pd.DataFrame(periods, columns=["start", "end"]).to_csv(path, index=False)
    return path


# ---------------------------------------------------------------- exhibits and checks
def period_label(d, freq: str = "M") -> str:
    """Chart and table labels: 'Sep 2026', '2026 Q3', '2026', 'Oct 17, 2026'. Fiscal years are labeled by
    their number ('2025'); the exhibit title and notes say they are fiscal years (style guide: never "FY")."""
    if freq == "FY":
        return str(int(d))
    d = pd.Timestamp(d)
    if freq == "M":
        return d.strftime("%b %Y")
    if freq == "Q":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    if freq in ("D", "W"):
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    return str(d.year)


def to_exhibit(w: pd.DataFrame, freq: str | None = None, label_col: str = "period") -> pd.DataFrame:
    """Wide frame -> exhibit CSV rows with a readable period label first and the ISO date kept for audit (a
    fiscal-year index gives the label '2025' and a fiscal_year column)."""
    out = w.copy()
    if freq == "FY" or out.index.name == "fiscal_year":
        out.insert(0, label_col, [period_label(y, "FY") for y in out.index])
        out.insert(1, "fiscal_year", [int(y) for y in out.index])
        # the middle of the fiscal year (April 1), so a line chart gets a date axis and recession shading
        out.insert(2, "date", [f"{int(y)}-04-01" for y in out.index])
        return out.reset_index(drop=True)
    if isinstance(out.index, pd.DatetimeIndex):
        f = freq or infer_freq(out)
        out.insert(0, label_col, [period_label(d, f) for d in out.index])
        out.insert(1, "date", out.index.strftime("%Y-%m-%d"))
    else:
        out.insert(0, label_col, [str(i) for i in out.index])
    return out.reset_index(drop=True)


def compare_published(computed: pd.Series, published: pd.Series, tol: float) -> pd.DataFrame:
    """Benchmark: computed vs. published values (a BEA or BLS table, a CBO data file, FRED's own transform)."""
    j = pd.DataFrame({"computed": computed, "published": published}).dropna()
    j["diff"] = j["computed"] - j["published"]
    j["ok"] = j["diff"].abs() <= tol
    return j


def sensitivity(estimate: Callable[..., float], variants: dict) -> pd.DataFrame:
    """Run estimate(**kwargs) for each named variant: {"main: CPI-U": {...}, "alt: PCE": {...}}."""
    rows = []
    for name, kw in variants.items():
        v = estimate(**kw)
        rows.append({"variant": name, **{k: str(x) for k, x in kw.items()}, "estimate": v})
    return pd.DataFrame(rows)


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


__all__ = ["load", "series_info", "wide", "period_end", "vintages", "real_time", "gaps", "last_valid", "common_end", "infer_freq",
           "change", "yoy", "annualized", "q4q4", "cagr", "change_between", "rebase", "deflate", "fiscal_year_of",
           "fiscal_year_label", "to_frequency", "share_of", "moving_average", "extreme", "recession_periods",
           "in_recession", "save_recessions", "period_label", "to_exhibit", "compare_published", "sensitivity",
           "Facts", "sha256", "pd", "np"]

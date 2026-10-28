"""Time-series helpers for BLS data, plus the Facts registry that the report cites.

    import sys; sys.path.insert(0, r"<out>/lib/bls")   # copied there by fetch.py; or <skill>/sources/bls/lib
    from tsstats import *
    df = load(OUT)                                    # <OUT>/data/bls/bls_series.csv, long format
    m = wide(df, "M")                                 # monthly, one column per series ID, gaps kept as NaN
    yoy = change(m["CUUR0000SA0"], 12)                # 12-month percent change (as a share: 0.031)

Every change is computed on a regular calendar index, so a missing month (for example October 2025,
not collected during the 2025 lapse in appropriations) yields NaN instead of silently pairing the
wrong months. Shares are stored as 0-1 so that factfmt's pct/pp formats apply.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import re
from typing import Callable

import numpy as np
import pandas as pd

FREQ_RULE = {"M": "MS", "Q": "QS", "S": "6MS", "A": "YS"}
PER_YEAR = {"M": 12, "B": 6, "Q": 4, "S": 2, "A": 1}

# Published reliability measures, read from BLS technical notes on 2026-10-19. BLS updates them; reread
# the current note (URL below) before relying on one, and cite it.
THRESHOLDS = {
    "ces_total_nonfarm_monthly_change": {
        "value": 122_000, "units": "jobs", "confidence": 0.90,
        "text": "90-percent confidence interval for the monthly change in total nonfarm employment (CES)",
        "url": "https://www.bls.gov/news.release/empsit.tn.htm"},
    "cps_unemployment_monthly_change": {
        "value": 425_000, "units": "people", "confidence": 0.90,
        "text": "90-percent confidence interval for the monthly change in unemployment (CPS), at a rate near 6 percent",
        "url": "https://www.bls.gov/news.release/empsit.tn.htm"},
    "cps_unemployment_rate_monthly_change": {
        "value": 0.3, "units": "percentage points", "confidence": 0.90,
        "text": "90-percent confidence interval for the monthly change in the unemployment rate (CPS)",
        "url": "https://www.bls.gov/news.release/empsit.tn.htm"},
}

# NBER business-cycle peaks and troughs (monthly), for notes and for choosing comparison dates.
RECESSIONS = [("2001-03-01", "2001-11-01"), ("2007-12-01", "2009-06-01"), ("2020-02-01", "2020-04-01")]


def data_file(out_dir: str, name: str) -> str:
    """<out>/data/bls/<name> (data-analyst layout); falls back to <out>/data/<name> (single-source runs)."""
    new = os.path.join(out_dir, "data", "bls", name)
    old = os.path.join(out_dir, "data", name)
    return new if os.path.exists(new) or not os.path.exists(old) else old


# ---------------------------------------------------------------- loading
def load(path: str) -> pd.DataFrame:
    """Read bls_series.csv (or the folder that holds data/bls_series.csv) into a long DataFrame."""
    if os.path.isdir(path):
        path = data_file(path, "bls_series.csv")
    df = pd.read_csv(path, dtype={"series_id": str, "period": str, "footnote_codes": str, "footnotes": str,
                                  "label": str, "value_raw": str})
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def wide(df: pd.DataFrame, freq: str = "M", columns: str = "series_id", include_preliminary: bool = True,
         start=None, end=None) -> pd.DataFrame:
    """One column per series on a regular calendar index; periods BLS did not publish stay NaN.

    freq: M monthly, Q quarterly, S semiannual, A BLS-published annual averages (indexed by Jan 1 of the year).
    """
    d = df[df["freq"] == freq].copy()
    if not include_preliminary:
        d.loc[d["preliminary"] == 1, "value"] = np.nan
    if freq == "A":
        d["date"] = pd.to_datetime(d["year"].astype(str) + "-01-01")
    w = d.pivot_table(index="date", columns=columns, values="value", aggfunc="first")
    if w.empty:
        return w
    idx = pd.date_range(w.index.min(), w.index.max(), freq=FREQ_RULE[freq])
    w = w.reindex(idx)
    w.index.name = "date"
    if start is not None:
        w = w[w.index >= pd.Timestamp(start)]
    if end is not None:
        w = w[w.index <= pd.Timestamp(end)]
    return w


def flags(df: pd.DataFrame, series_id: str, freq: str = "M") -> pd.DataFrame:
    """Preliminary flags and footnotes of one series, indexed like wide()."""
    d = df[(df["series_id"] == series_id) & (df["freq"] == freq)].set_index("date")
    return d[["preliminary", "footnote_codes", "footnotes"]]


def is_preliminary(df: pd.DataFrame, series_id: str, date, freq: str = "M") -> bool:
    """Did BLS mark this series' value for this period preliminary? Use it for Facts(preliminary=...):
    a derived fact is preliminary when any input value is, e.g. any(is_preliminary(df, s, d) for s in ids)."""
    d = df[(df["series_id"] == series_id) & (df["freq"] == freq) & (df["date"] == pd.Timestamp(date))]
    return bool(len(d)) and int(d["preliminary"].iloc[0]) == 1


def gaps(s: pd.Series) -> list[str]:
    """Dates inside the series' span with no value (not collected, not published)."""
    s = s.loc[s.first_valid_index():s.last_valid_index()] if s.notna().any() else s
    return [d.strftime("%Y-%m") for d in s.index[s.isna()]]


def infer_freq(s: pd.Series) -> str:
    """M, B (bimonthly: metro CPIs priced every other month), Q, S or A from the month spacing of the index
    (pandas names 6-month spacing '2QS-...')."""
    idx = pd.DatetimeIndex(s.index)
    steps = np.diff(idx.year * 12 + idx.month)
    steps = steps[steps > 0]
    step = int(steps.min()) if len(steps) else 1
    return {2: "B", 3: "Q", 6: "S", 12: "A"}.get(step, "M")


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
    """Change from the same date a year earlier, matched by date (so a bimonthly series or one with gaps is
    compared with the right month; NaN where that month has no value)."""
    if isinstance(s, pd.DataFrame):               # the table wide() returns: each column on its own
        return s.apply(yoy, kind=kind)
    idx = pd.DatetimeIndex(s.index)
    prev = pd.Series(s.reindex(idx - pd.DateOffset(years=1)).to_numpy(), index=s.index)
    if kind == "pct":
        return s / prev - 1
    if kind == "net":
        return s - prev
    if kind == "log":
        return np.log(s / prev)
    raise ValueError(kind)


def annualized(s: pd.Series, periods: int, per_year: int | None = None) -> pd.Series:
    """Compound annual rate of change over `periods` periods, e.g. 3-month annualized: annualized(s, 3)."""
    ppy = per_year or PER_YEAR[infer_freq(s)]
    return (s / s.shift(periods)) ** (ppy / periods) - 1


def cagr(s: pd.Series, start, end) -> float:
    """Compound annual growth rate between two dates (both must have values)."""
    a, b = s.loc[pd.Timestamp(start)], s.loc[pd.Timestamp(end)]
    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    if any(pd.isna([a, b])) or years <= 0:
        return float("nan")
    return float((b / a) ** (1 / years) - 1)


def change_between(s: pd.Series, start, end, kind: str = "pct") -> float:
    """Change from start to end: pct (a share, 0.033: register it with fmt "pct:1") or net (units)."""
    if kind not in ("pct", "net"):
        raise ValueError(f"kind must be 'pct' or 'net', got {kind!r}")
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
    """Real values in dollars of the base date (or the mean of a (start, end) period) using a price index."""
    p = price.reindex(nominal.index)
    ref = (price.loc[pd.Timestamp(base[0]):pd.Timestamp(base[1])].mean() if isinstance(base, (tuple, list))
           else price.loc[pd.Timestamp(base)])
    return nominal * ref / p


def annual_average(s: pd.Series, require_full: bool = True) -> pd.DataFrame:
    """Calendar-year averages computed from monthly or quarterly data, with the number of periods used.

    BLS publishes its own annual averages (period M13; fetch with a key and "annual_average": true) and
    those are preferred: a mean of published (rounded) months can differ from them by one in the last digit.
    A year with a missing month gets NaN when require_full (the default), because a 10- or 11-month mean is not
    an annual average. Returns a DataFrame indexed by year (int) with columns mean and n_periods:
    annual_average(s).loc[2019, "mean"].
    A median (CPS weekly earnings, LE series) or productivity (PR) has no annual average: BLS computes the
    annual figure from the year's data (the 2024 high school median is 930; the mean of its quarters is 935).
    """
    if re.match(r"(?:LE[SU]|PR[SU])", str(s.name or "")):
        print(f"[tsstats] WARNING: annual_average: {s.name} is a median or a productivity measure; BLS's annual "
              "value is not the mean of its quarters. Use the published annual figure (v2 annual_average, FRED "
              "'<id>A', or BLS's annual table in published/) and keep this mean as a cross-check only.",
              file=sys.stderr)
    ppy = PER_YEAR[infer_freq(s)]
    g = s.groupby(s.index.year)
    out = pd.DataFrame({"mean": g.mean(), "n_periods": g.count()})
    if require_full:
        out.loc[out["n_periods"] < ppy, "mean"] = np.nan
    out.index.name = "year"
    return out


def moving_average(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def extreme(s: pd.Series, start=None, end=None, how: str = "max") -> tuple[pd.Timestamp, float]:
    """(first date, value) of the series maximum or minimum. A value reached on several dates (rates are
    rounded to 0.1) is printed with all its dates: say "in August and November", not only the first."""
    x = s.loc[pd.Timestamp(start) if start else None:pd.Timestamp(end) if end else None].dropna()
    d = x.idxmax() if how == "max" else x.idxmin()
    ties = x.index[x == x.loc[d]]
    if len(ties) > 1:
        print(f"[tsstats] extreme: {float(x.loc[d])} is reached on {len(ties)} dates: "
              + ", ".join(t.strftime("%Y-%m") for t in ties), file=sys.stderr)
    return d, float(x.loc[d])


def significant(change_value: float, threshold: str | float) -> bool:
    """Is a change larger than a published 90-percent threshold? threshold: a THRESHOLDS key or a number."""
    t = THRESHOLDS[threshold]["value"] if isinstance(threshold, str) else float(threshold)
    return abs(change_value) > t


def in_recession(d) -> bool:
    d = pd.Timestamp(d)
    return any(pd.Timestamp(a) <= d <= pd.Timestamp(b) for a, b in RECESSIONS)


def period_label(d, freq: str = "M") -> str:
    """Chart and table labels: 'Sep 2026', '2026 Q3', '2026'."""
    d = pd.Timestamp(d)
    if freq == "M":
        return d.strftime("%b %Y")
    if freq == "Q":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    return str(d.year)


def to_exhibit(w: pd.DataFrame, freq: str = "M", label_col: str = "period") -> pd.DataFrame:
    """Wide frame -> exhibit CSV rows with a readable period label first and the ISO date kept for audit."""
    out = w.copy()
    out.insert(0, label_col, [period_label(d, freq) for d in out.index])
    out.insert(1, "date", out.index.strftime("%Y-%m-%d"))
    return out.reset_index(drop=True)


def compare_published(computed: pd.Series, published: pd.Series, tol: float) -> pd.DataFrame:
    """Benchmark: computed vs. BLS-published values (e.g. the bls_pct_12m column, or a news release).
    Both must be on one scale: change() returns a share (0.029), BLS publishes percent (2.9): pass
    change(...) * 100. A 100-fold gap between the two is refused rather than reported as a mismatch."""
    j = pd.DataFrame({"computed": computed, "published": published}).dropna()
    c, p = j["computed"].abs().median(), j["published"].abs().median()
    if len(j) and c > 0 and p > 0 and 50 < p / c < 200:
        raise ValueError("computed looks like a share and published like percent: multiply computed by 100")
    j["diff"] = j["computed"] - j["published"]
    j["ok"] = j["diff"].abs() <= tol
    return j


def sensitivity(estimate: Callable[..., float], variants: dict) -> pd.DataFrame:
    """Run estimate(**kwargs) for each named variant: {"main: Feb 2020 base": {...}, "alt: 2019 avg": {...}}."""
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


__all__ = ["load", "wide", "flags", "is_preliminary", "gaps", "change", "yoy", "annualized", "cagr", "change_between", "rebase",
           "deflate", "annual_average", "moving_average", "extreme", "significant", "in_recession",
           "period_label", "to_exhibit", "compare_published", "sensitivity", "Facts", "sha256", "THRESHOLDS",
           "RECESSIONS", "pd", "np"]

"""Time-series helpers for BEA data in data/bea/bea_data.csv, plus the Facts registry (bea pack lib/).

    import sys; sys.path.insert(0, r"<skill>/sources/bea/lib")
    from stats import *
    df = load(f"{OUT}/data/bea/bea_data.csv")
    gdp = series(df, table="T10106", line=1, frequency="Q")   # real GDP, chained dollars, by quarter
    g = growth(gdp)                     # quarterly: percent change at an annual rate; monthly: plain change (BEA's tables)
    check_against_published(g, series(df, table="T10101", line=1, frequency="Q"))   # must match BEA's own

Conventions (they match BEA's tables, so computed numbers can be checked against published ones):
  - Growth rates are returned in percent (2.3 means 2.3 percent); register them with fmt "pct100:1".
  - Quarterly growth is at a compound annual rate (T1.1.1); monthly growth is the plain change from the
    preceding month, as BEA's monthly tables print it (T2.8.7 PCE prices, T2.8.1 real PCE). annualize=True
    or False overrides either.
  - Quarterly and monthly dollar levels in NIPA tables are seasonally adjusted at annual rates (SAAR):
    average them for an annual figure; never add the quarters up.
  - `amount` = value x 10**unit_mult is in base units (dollars, people); `value` is as BEA prints it.
  - Chained-dollar components do not add up to their totals; use shares of current-dollar values instead.
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

__all__ = ["load", "pick", "series", "flagged", "growth", "yoy", "cagr", "change_between", "annual_average",
           "q4_over_q4", "share", "per_capita", "index_to", "deflate", "check_against_published", "rank_areas",
           "near_ties", "wide", "sensitivity", "Facts", "sha256", "pd", "np"]

STR_COLS = ["request_id", "dataset", "table", "line", "series_code", "description", "geo_fips", "geo_name",
            "geo_level", "industry", "time_period", "date", "subperiod", "frequency", "flag", "unit", "metric", "note_ref", "dims"]


# ---------------------------------------------------------------- loading and selecting
def load(path: str) -> pd.DataFrame:
    """Read bea_data.csv with codes kept as text (FIPS 06037 keeps its zero) and add `amount` and `period`."""
    df = pd.read_csv(path, dtype={c: str for c in STR_COLS}, keep_default_na=False, na_values=[""])
    for c in STR_COLS:
        if c in df:
            df[c] = df[c].fillna("")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["unit_mult"] = pd.to_numeric(df["unit_mult"], errors="coerce").fillna(0).astype(int)
    df["amount"] = df["value"] * (10.0 ** df["unit_mult"])
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["period"] = [_period(tp, f) for tp, f in zip(df["time_period"], df["frequency"])]
    return df


def _period(tp: str, freq: str):
    m = re.fullmatch(r"(\d{4})(?:Q([1-4])|M(\d{2}))?", str(tp))
    if not m:
        return pd.NaT
    if m.group(2):
        return pd.Period(year=int(m.group(1)), quarter=int(m.group(2)), freq="Q")
    if m.group(3):
        return pd.Period(year=int(m.group(1)), month=int(m.group(3)), freq="M")
    return pd.Period(year=int(m.group(1)), freq="Y")


def pick(df: pd.DataFrame, **where) -> pd.DataFrame:
    """Rows matching every condition, e.g. pick(df, table="SAGDP9", line=1, geo_level="state").
    A list value means 'any of'. Raises when nothing matches, so a typo cannot pass silently."""
    m = pd.Series(True, index=df.index)
    for k, v in where.items():
        if v is None:
            continue
        col = df[k].astype(str) if k in STR_COLS else df[k]
        if isinstance(v, (list, tuple, set)):
            m &= col.isin([str(x) for x in v] if k in STR_COLS else list(v))
        else:
            m &= col == (str(v) if k in STR_COLS else v)
    out = df[m]
    if out.empty:
        raise ValueError(f"no rows match {where}")
    return out


def series(df: pd.DataFrame, column: str = "value", **where) -> pd.Series:
    """One time series (indexed by time_period, sorted) from rows matching `where`.
    Raises if several rows share a period (add table/line/geo_fips to `where`) or if a cell is flagged."""
    d = pick(df, **where)
    freqs = sorted(set(d["frequency"]))
    if len(freqs) > 1:
        raise ValueError(f"rows for {where} mix frequencies {', '.join(freqs)}; add frequency=\"A\" (or Q, M)")
    d = d.sort_values("period")
    dup = d["time_period"].duplicated(keep=False)
    if dup.any():
        sample = d.loc[dup, ["table", "line", "geo_fips", "time_period"]].head(4).to_dict("records")
        raise ValueError(f"{int(dup.sum())} rows share a period for {where}; narrow the selection: {sample}")
    bad = d[d["flag"] != ""]
    if not bad.empty:
        raise ValueError(f"{len(bad)} flagged cells ({', '.join(sorted(set(bad['flag'])))}) in {where}; "
                         "handle them explicitly (see flagged()) instead of treating them as numbers")
    s = pd.Series(d[column].to_numpy(dtype=float), index=d["time_period"].to_numpy(), name=column)
    s.attrs.update(frequency=(d["frequency"].iloc[0] if len(d) else ""), unit=d["unit"].iloc[0] if len(d) else "",
                   description=d["description"].iloc[0] if len(d) else "")
    return s


def flagged(df: pd.DataFrame) -> pd.DataFrame:
    """Cells BEA did not publish as numbers ((D), (NA), (NM), (L) ...), with where they are."""
    return df.loc[df["flag"] != "", ["request_id", "table", "line", "description", "geo_fips", "geo_name",
                                      "time_period", "flag"]]


def wide(df: pd.DataFrame, index: str = "time_period", columns: str = "description", values: str = "value") -> pd.DataFrame:
    """Pivot tidy rows to one column per series (for exhibit CSVs). Duplicates raise."""
    return df.pivot(index=index, columns=columns, values=values).reset_index()


# ---------------------------------------------------------------- growth and levels
def _per_year(s: pd.Series) -> int:
    f = (s.attrs.get("frequency") or "").upper()
    if not f and len(s):
        f = "Q" if "Q" in str(s.index[0]) else "M" if "M" in str(s.index[0]) else "A"
    return {"Q": 4, "M": 12}.get(f, 1)


def _label(p: pd.Period) -> str:
    f = p.freqstr[:1]
    return f"{p.year}Q{p.quarter}" if f == "Q" else f"{p.year}M{p.month:02d}" if f == "M" else str(p.year)


def _prev(s: pd.Series, k: int) -> pd.Series:
    """The value k periods before each period, by date, not by row: NaN when that period is missing, so a
    gap (a dropped year, a missing quarter) never turns a two-period change into a one-period one."""
    periods = [_period(str(i), "") for i in s.index]
    if any(p is pd.NaT for p in periods):
        return s.shift(k)                               # index is not BEA periods: fall back to rows
    vals = {str(i): v for i, v in s.items()}
    return pd.Series([vals.get(_label(p - k), np.nan) for p in periods], index=s.index, dtype=float)


def growth(s: pd.Series, annualize: bool | None = None) -> pd.Series:
    """Percent change from the preceding period, as BEA publishes it: quarterly at a compound annual rate
    ((x_t / x_t-1) ** 4 - 1) * 100, monthly as the plain monthly change (BEA's monthly percent-change tables
    are not annualized). annualize=True or False forces either. NaN where the preceding period is missing."""
    per = _per_year(s)
    k = per if (annualize or (annualize is None and per == 4)) else 1
    out = ((s / _prev(s, 1)) ** k - 1) * 100
    out.attrs.update(s.attrs)
    return out


def yoy(s: pd.Series) -> pd.Series:
    """Percent change from the same period a year earlier (NaN where that period is missing)."""
    out = (s / _prev(s, _per_year(s)) - 1) * 100
    out.attrs.update(s.attrs)
    return out


def cagr(s: pd.Series, start: str, end: str) -> float:
    """Average annual growth, in percent, from period `start` to period `end` (e.g. '2019', '2025' or '2019Q4', '2025Q2')."""
    a, b = float(s.loc[str(start)]), float(s.loc[str(end)])
    years = (_period(str(end), "") - _period(str(start), "")).n / _per_year(s)
    if years <= 0:
        raise ValueError("end must come after start")
    return ((b / a) ** (1 / years) - 1) * 100


def change_between(s: pd.Series, start: str, end: str) -> float:
    """Cumulative percent change from period `start` to period `end` (e.g. '2019' to '2025'): the measure for
    "growth since <year>". It orders areas exactly as cagr() does; report both."""
    a, b = float(s.loc[str(start)]), float(s.loc[str(end)])
    return (b / a - 1) * 100


def annual_average(s: pd.Series, complete_only: bool = True) -> pd.Series:
    """Annual averages of a quarterly or monthly series (the right way to annualize SAAR levels, rates and
    indexes). Years with missing periods are dropped unless complete_only=False."""
    k = _per_year(s)
    yrs = pd.Series([str(i)[:4] for i in s.index], index=s.index)
    g = s.groupby(yrs.values)
    out = g.mean()
    if complete_only:
        out = out[g.count() == k]
    out.attrs.update(s.attrs, frequency="A")
    return out


def q4_over_q4(s: pd.Series) -> pd.Series:
    """Fourth quarter over fourth quarter percent change, by year (quarterly series only)."""
    q4 = s[[str(i).endswith("Q4") for i in s.index]]
    out = (q4 / _prev(q4, 4) - 1) * 100                  # NaN when the previous year's Q4 is missing
    out.index = [str(i)[:4] for i in out.index]
    return out


def share(part, total) -> pd.Series | float:
    """part / total in percent. Use current-dollar values: chained-dollar parts do not sum to totals."""
    return part / total * 100


def per_capita(amount, population):
    """Amount per person; both in base units (use the `amount` column, not `value`)."""
    return amount / population


def index_to(s: pd.Series, base: str | list[str]) -> pd.Series:
    """Rescale to 100 in the base period (or the average of several base periods)."""
    b = s.loc[[base] if isinstance(base, str) else base].mean()
    return s / b * 100


def deflate(nominal: pd.Series, price_index: pd.Series, base: str) -> pd.Series:
    """Nominal / (price index / index in base period): dollars of the base period. Use BEA's published
    chained-dollar series when they exist; deflate only what BEA does not publish in real terms, and say so."""
    p = price_index / float(price_index.loc[str(base)])
    return (nominal / p).dropna()


# ---------------------------------------------------------------- checks
def check_against_published(computed: pd.Series, published: pd.Series, tol: float | None = None) -> pd.DataFrame:
    """Compare computed numbers with BEA's published ones for the same periods (for example growth of
    real GDP from T10106 levels vs. the percent change in T10101). tol defaults to the rounding of the
    published series (0.1 for one decimal) plus a hair. Raises if any period is off by more than tol."""
    both = pd.concat({"computed": computed, "published": published}, axis=1).dropna()
    if both.empty:
        raise ValueError("no overlapping periods to compare")
    if tol is None:
        dec = max((len(f"{v}".split(".")[1]) if "." in f"{v}" else 0) for v in published.dropna().head(20))
        tol = 0.5 * 10 ** (-dec) + 1e-9 if dec else 0.5 + 1e-9
        tol = max(tol, 0.051)
    both["diff"] = both["computed"] - both["published"]
    both["ok"] = both["diff"].abs() <= tol
    if not both["ok"].all():
        bad = both[~both["ok"]].head(5)
        raise AssertionError(f"computed values differ from BEA's published values by more than {tol}:\n{bad}")
    return both.reset_index(names="time_period")


def rank_areas(df: pd.DataFrame, value: str = "value", by: str = "geo_name", ascending: bool = False) -> pd.DataFrame:
    """Rank rows (one per area) by a value; ties share the lower rank."""
    d = df.copy()
    d["rank"] = d[value].rank(ascending=ascending, method="min").astype(int)
    return d.sort_values("rank")


def near_ties(s: pd.Series, decimals: int = 1, ascending: bool = False) -> list[tuple[int, str, str]]:
    """Neighbors in a ranking that round to the same published value: [(rank of the first, first, second)].
    The order comes from the unrounded values; name these pairs in the text instead of reordering them."""
    r = s.dropna().sort_values(ascending=ascending)
    shown = r.round(decimals)
    return [(i, a, b) for i, (a, b) in enumerate(zip(r.index[:-1], r.index[1:]), start=1) if shown[a] == shown[b]]


def sensitivity(estimate: Callable[..., dict], variants: dict) -> pd.DataFrame:
    """Re-run one estimate under alternative choices; use it for every judgment call.

    estimate(**kwargs) -> dict of named results (for example {"growth": 2.1}).
    variants: {"Main: annual averages": {"measure": "annual"}, "Q4 over Q4": {"measure": "q4q4"}, ...}
    The first variant is the baseline; the table reports each result and its change from the baseline.
    """
    rows, base = [], None
    for name, kw in variants.items():
        r = {"variant": name, **estimate(**kw)}
        if base is None:
            base = r
        else:
            for k, v in list(r.items()):
                if k != "variant" and isinstance(v, (int, float)) and isinstance(base.get(k), (int, float)):
                    r[f"{k}_change"] = v - base[k]
        rows.append(r)
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

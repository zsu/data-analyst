"""Helpers for combining sources in one analysis (pandas). Import in analysis.py:

    from combine import fiscal_year, to_fiscal, state, align_check, deflate

Sources disagree on periods, geography codes, units and vintages. These helpers make the conversions
explicit and refuse silent ones:
- fiscal_year(dates)            federal fiscal year (October-September, named for the year it ends)
- to_fiscal(df, date, value, how="sum"|"mean"|"last", freq="M"|"Q")   calendar data to fiscal years,
                                with a `complete` flag (12 distinct months or 4 quarters); incomplete years are
                                kept but flagged, never presented as full years
- state(x, as_="usps"|"fips"|"name")   one state in any form to another (raises with the accepted forms)
- add_state_codes(df, col)      adds usps, fips and name columns from whatever form col holds
- align_check(frames, on, units)  before a join: keys present and unique, overlapping, units named
- deflate(values, index, base)  real dollars with a named index and base period
- pack_lib(pack, module, out, skill)   a pack's lib/<module>.py by path, so same-named helpers of two packs
                                (bls and fred tsstats.py; bea, ipums and opm stats.py) never shadow each other
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pandas as pd

STATES = [
    ("AL", "01", "Alabama"),
    ("AK", "02", "Alaska"),
    ("AZ", "04", "Arizona"),
    ("AR", "05", "Arkansas"),
    ("CA", "06", "California"),
    ("CO", "08", "Colorado"),
    ("CT", "09", "Connecticut"),
    ("DE", "10", "Delaware"),
    ("DC", "11", "District of Columbia"),
    ("FL", "12", "Florida"),
    ("GA", "13", "Georgia"),
    ("HI", "15", "Hawaii"),
    ("ID", "16", "Idaho"),
    ("IL", "17", "Illinois"),
    ("IN", "18", "Indiana"),
    ("IA", "19", "Iowa"),
    ("KS", "20", "Kansas"),
    ("KY", "21", "Kentucky"),
    ("LA", "22", "Louisiana"),
    ("ME", "23", "Maine"),
    ("MD", "24", "Maryland"),
    ("MA", "25", "Massachusetts"),
    ("MI", "26", "Michigan"),
    ("MN", "27", "Minnesota"),
    ("MS", "28", "Mississippi"),
    ("MO", "29", "Missouri"),
    ("MT", "30", "Montana"),
    ("NE", "31", "Nebraska"),
    ("NV", "32", "Nevada"),
    ("NH", "33", "New Hampshire"),
    ("NJ", "34", "New Jersey"),
    ("NM", "35", "New Mexico"),
    ("NY", "36", "New York"),
    ("NC", "37", "North Carolina"),
    ("ND", "38", "North Dakota"),
    ("OH", "39", "Ohio"),
    ("OK", "40", "Oklahoma"),
    ("OR", "41", "Oregon"),
    ("PA", "42", "Pennsylvania"),
    ("RI", "44", "Rhode Island"),
    ("SC", "45", "South Carolina"),
    ("SD", "46", "South Dakota"),
    ("TN", "47", "Tennessee"),
    ("TX", "48", "Texas"),
    ("UT", "49", "Utah"),
    ("VT", "50", "Vermont"),
    ("VA", "51", "Virginia"),
    ("WA", "53", "Washington"),
    ("WV", "54", "West Virginia"),
    ("WI", "55", "Wisconsin"),
    ("WY", "56", "Wyoming"),
    ("AS", "60", "American Samoa"),
    ("GU", "66", "Guam"),
    ("MP", "69", "Northern Mariana Islands"),
    ("PR", "72", "Puerto Rico"),
    ("VI", "78", "U.S. Virgin Islands"),
]
_BY = {}
for _u, _f, _n in STATES:
    for _k in (_u.lower(), _f, _n.lower(), str(int(_f))):
        _BY[_k] = (_u, _f, _n)


def state(x, as_: str = "usps") -> str:
    key = str(x).strip().lower()
    if key.isdigit():
        key = key.zfill(2) if len(key) <= 2 else key
    hit = _BY.get(key) or _BY.get(key.lstrip("0"))
    if not hit:
        raise ValueError(f"unknown state {x!r}: give a USPS code (MD), a FIPS code (24) or a name (Maryland)")
    return {"usps": hit[0], "fips": hit[1], "name": hit[2]}[as_]


def add_state_codes(df: pd.DataFrame, col: str) -> pd.DataFrame:
    df = df.copy()
    for k in ("usps", "fips", "name"):
        df[f"state_{k}"] = df[col].map(lambda v: state(v, k))
    return df


def fiscal_year(dates) -> pd.Series:
    d = pd.to_datetime(pd.Series(dates))
    return (d.dt.year + (d.dt.month >= 10).astype(int)).rename("fiscal_year")


def to_fiscal(df: pd.DataFrame, date: str, value: str, how: str = "sum", freq: str = "M") -> pd.DataFrame:
    """Calendar-period data to fiscal years. Flows: how="sum"; rates and indexes: "mean"; stocks: "last"."""
    d = df[[date, value]].dropna().copy()
    d[date] = pd.to_datetime(d[date])
    d["fiscal_year"] = fiscal_year(d[date]).values
    need = {"M": 12, "Q": 4}[freq]
    d["_period"] = d[date].dt.to_period(freq)
    dup = d.duplicated("_period", keep=False)
    if dup.any():                 # several rows per period (vintages, daily data): a sum would double count
        print(f"to_fiscal: {d.loc[dup, '_period'].nunique()} period(s) have more than one row; "
              "keep one row per period (or aggregate first) before converting")
    g = d.sort_values(date).groupby("fiscal_year")
    agg = {"sum": g[value].sum(), "mean": g[value].mean(), "last": g[value].last()}[how]
    out = pd.DataFrame({value: agg, "periods": g["_period"].nunique()})   # distinct months/quarters, not rows
    out["complete"] = (out["periods"] >= need) & ~d.assign(dup=dup).groupby("fiscal_year")["dup"].any()
    if (~out["complete"]).any():
        bad = ", ".join(f"FY{y}" for y in out.index[~out["complete"]])
        print(f"to_fiscal: incomplete fiscal years kept but flagged: {bad}")
    return out.reset_index()


def align_check(frames: dict, on: list[str], units: dict) -> None:
    """frames: {source: DataFrame}; units: {source: "billions of dollars, nominal, fiscal years"}.
    Raises when a frame lacks a join key, a unit description is missing, a frame repeats a key (a many-to-many
    join would multiply rows), or the keys do not overlap. Keys missing from some frames are reported, not
    refused. Units are printed side by side, not compared: convert scales and dollar bases before joining."""
    if not isinstance(frames, dict):
        raise TypeError("align_check(frames, ...): frames is a dict {source: DataFrame}, e.g. {'treasury': t, 'bea': b}")
    missing_units = [s for s in frames if not units.get(s)]
    if missing_units:
        raise ValueError(f"name the units and period basis of {missing_units} before joining")
    keysets = []
    for s, f in frames.items():
        lack = [k for k in on if k not in f.columns]
        if lack:
            raise ValueError(f"{s} lacks join columns {lack}")
        rep = int(f.duplicated(on).sum())
        if rep:
            raise ValueError(f"{s} has {rep} repeated {on} key(s): aggregate or filter to one row per key first")
        keysets.append(set(map(tuple, f[on].astype(str).values.tolist())))
    for s in frames:
        print(f"align_check: {s}: {units[s]}")
    common = set.intersection(*keysets) if keysets else set()
    if not common:
        raise ValueError(f"no overlapping {on} values across {list(frames)}: check codes and period labels")
    for s, ks in zip(frames, keysets):
        dropped = len(ks - common)
        if dropped:
            print(f"align_check: {s} has {dropped} key value(s) not in every source (they drop out of an inner join)")


def pack_lib(pack: str, module: str, out: str, skill: str):
    """Import sources/<pack>/lib/<module>.py under the unique name <pack>_<module>: the copy in <out>/lib/<pack>/
    first (a replication package has only that), the skill's as the fallback. Use it instead of a plain import
    whenever two packs in the analysis ship a helper with the same file name."""
    for d in (os.path.join(out, "lib", pack), os.path.join(skill, "sources", pack, "lib")):
        p = os.path.join(d, module + ".py")
        if os.path.exists(p):
            name = f"{pack.replace('-', '_')}_{module}"
            if name in sys.modules:
                return sys.modules[name]
            spec = importlib.util.spec_from_file_location(name, p)
            mod = importlib.util.module_from_spec(spec)
            if d not in sys.path:
                sys.path.append(d)                       # the helper's own sibling imports
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError(f"{pack} has no lib/{module}.py in {out}/lib/{pack} or the skill")


def deflate(values: pd.Series, index: pd.Series, base=None) -> pd.Series:
    """values and index aligned on the same period labels; base = a period label (index rebased to 1 there)
    or None (index used as given, e.g. already 2017=100 -> divide by 100 first)."""
    idx = index.astype(float)
    if base is not None:
        if base not in idx.index:
            raise ValueError(f"base period {base!r} is not in the index")
        idx = idx / idx.loc[base]
    common = values.index.intersection(idx.index)
    if len(common) < len(values.index):
        raise ValueError("the price index does not cover every period of the values")
    return values.loc[common] / idx.loc[common]

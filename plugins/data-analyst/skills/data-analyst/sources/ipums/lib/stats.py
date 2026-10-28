"""Weighted-statistics and recode helpers for IPUMS microdata (pandas).

Import from an analysis script:
    import sys; sys.path.insert(0, r"<skill>/sources/ipums/lib")
    from stats import *

    df = load("out/data/ipums/ipums_extract.csv")
    df = clean_missing(df, "usa", codebook="out/data/ipums/codebook.csv")
    df["educ"] = educ_group(df, "usa", scheme="educ4")
    df["emp"]  = emp_status(df, "usa")
    tab = grouped(df, ["YEAR", "SEX", "NATIVITY_GRP"], "PERWT", {
        "employment_rate": share(lambda d: d.emp == "employed"),
        "avg_wage": mean("INCWAGE", where=lambda d: d.INCWAGE > 0),
        "population_millions": total(scale=1e-6),
    })
    tab.to_csv("exhibit_1.csv", index=False)

Every estimate is weighted. `grouped` also returns the unweighted cell size (`n_obs`) and a
`low_n` flag (< 100 respondents) so thin cells can be suppressed or footnoted, as published statistics require.

NHIS and MEPS (no case selection in the API, no replicate weights): keep the whole file, mark groups by mask
    df["unins"] = uninsured(df, "nhis")             # 1 / 0 / NaN (unknown)
    kw = survey_design(df, "nhis")                  # DSTRATA / DPSU for lin_se and se_estimate
    dom = sample_adults(df) & df.AGE.between(18, 64) & df.unins.notna()
    est, se = se_estimate(df, share_z(df, df.unins == 1, dom, "SAMPWEIGHT"), **kw)

Accuracy helpers:
    rep_se(df, estimator, "usa")      replicate-weight standard error (ACS REPWTP1-80, CPS REPWTP1-160)
    survey_design(df, collection)     strata / PSU columns for lin_se (ACS, NHIS, MEPS)
    screen_outliers(df, "INCWAGE", "ASECWT")    top-coded / implausible values report (col, weight, by=...)
    median_binned("EARN") / wmedian_binned(x, w)   Census-style interpolated median for dollar amounts
    facts = Facts(out_dir)            registry of every number cited in the text (see below)

Facts: every number that appears in report prose is registered here, traced to an exhibit CSV
cell, and referenced in report.json as {{name}}. build_report.py fills the value and writes an
audit trail; audit_report.py re-checks each fact against its CSV.
    facts.add("emp_fb_men_2023", tab, where={"YEAR": 2023, "SEX": "Men", "fb": "Foreign-born"},
              column="employment_rate", source="exhibits/exhibit_1.csv", fmt="pct:0",
              label="Employment rate, foreign-born men ages 25 to 54, 2023")
    facts.add("gap_men_2023", value=0.038, fmt="pp:1", label="...", derivation="exhibit_1 rows A - B")
    facts.save()                      # writes facts.json
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import re
from typing import Callable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- loading / missing codes
# Fallback special codes for common continuous variables (used together with codebook labels).
MISSING = {
    "usa": {"INCWAGE": [999999, 999998], "INCTOT": [9999999, 9999998], "INCEARN": [],
            "HHINCOME": [9999999], "FTOTINC": [9999999, 9999998], "INCWELFR": [99999],
            "INCSS": [99999], "INCSUPP": [99999], "POVERTY": [0], "UHRSWORK": [0],
            "WKSWORK1": [0], "TRANTIME": [0]},
    "cps": {"INCWAGE": [99999999, 99999998], "INCBUS": [99999999, 99999998], "INCFARM": [99999999, 99999998],
            "INCTOT": [999999999, 999999998],
            "HHINCOME": [999999999], "FTOTVAL": [9999999999], "EARNWEEK": [9999.99], "EARNWEEK2": [999999.99],
            "HOURWAGE": [999.99], "HOURWAGE2": [999.99], "UHRSWORKT": [997, 999],
            "UHRSWORK1": [997, 999], "UHRSWORKLY": [999], "WKSWORK1": [],
            "AHRSWORKT": [999]},
    # AGE has no value labels in the DDI: top code 85; unknown codes per the IPUMS AGE pages [2026-10]
    "nhis": {"AGE": [997, 998, 999], "BMICALC": [0, 996]},
    "meps": {"AGE": [996], "INCTOT": [9999999.96, 9999999.97, 9999999.98, 9999999.99],
             "FTOTVAL": [999999]},                 # INCTOT may be negative (losses): a real value
}
HEALTH_SURVEYS = ("nhis", "meps")           # IPUMS Health Surveys: EDUC and USBORN share one coding
_MISSING_LABEL = re.compile(r"\b(n/?a|niu|n\.i\.u\.?|not in universe|missing|unknown|blank|"
                            r"refused|don'?t know|not applicable|illegible|suppressed)\b", re.I)


def _compact(df: pd.DataFrame) -> pd.DataFrame:
    """Integers -> int32 only when every value fits; never int8/int16, whose silent overflow in
    arithmetic (e.g. weeks * hours) would corrupt results. Decimals and weights stay float64."""
    lim = np.iinfo(np.int32)
    for c in df.columns:
        if pd.api.types.is_integer_dtype(df[c]) and len(df[c]):
            if df[c].min() >= lim.min and df[c].max() <= lim.max:
                df[c] = df[c].astype(np.int32)
    return df


def load(path: str, usecols=None, compact: bool = False, chunksize: int = 500_000, **kw) -> pd.DataFrame:
    """Read an IPUMS CSV in chunks (lower peak memory). Pass usecols=[...] to read only needed columns.

    compact=True stores integer columns as int32 (halves memory on large ACS files). Integer
    arithmetic can still overflow silently in numpy, so convert to float before multiplying
    (e.g. df.WKSWORK1.astype(float) * df.UHRSWORKLY). Default compact=False keeps int64.
    """
    if usecols is not None:
        wanted = {c.upper() for c in usecols}
        usecols = lambda c: c.upper() in wanted  # noqa: E731
    parts = []
    for ch in pd.read_csv(path, usecols=usecols, chunksize=chunksize, **kw):
        parts.append(_compact(ch) if compact else ch)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if compact:
        df = _compact(df)
    df.columns = [c.upper() for c in df.columns]
    return df


def missing_codes(codebook: str | pd.DataFrame) -> dict[str, list]:
    """Codes whose DDI value label says N/A, NIU, missing, unknown, etc."""
    # keep_default_na=False: a label of exactly "N/A" (VEHICLES 0) must stay text, not become NaN
    cb = pd.read_csv(codebook, dtype={"value": str}, keep_default_na=False) if isinstance(codebook, str) else codebook
    out: dict[str, list] = {}
    for r in cb.itertuples():
        if isinstance(r.value_label, str) and _MISSING_LABEL.search(r.value_label):
            try:
                out.setdefault(r.variable.upper(), []).append(float(r.value))
            except ValueError:
                pass
    return out


def clean_missing(df: pd.DataFrame, collection: str, codebook=None, columns=None,
                  keep: tuple = ("EMPSTAT", "LABFORCE", "NATIVITY", "SEX", "VETSTAT", "CLASSWKR",
                                 "EDUC", "EDUCD", "CITIZEN", "MARST", "RACE", "HISPAN",
                                 # NHIS / MEPS categorical items, recoded by the helpers below
                                 "USBORN", "HEALTH", "HINOTCOVE", "HINOTCOV", "HIPRIVATEE", "HIMCAIDE",
                                 "HIMCAREE", "HIPRIVATE", "HIMACHIP", "HIMCARE", "COVERTYPE", "POVCAT",
                                 "POORYN", "ASTATFLG", "CSTATFLG", "RACENEW", "RACEA", "HISPYN", "EDUCREC2",
                                 "EDRECODE", "WORKEV")) -> pd.DataFrame:
    """Replace IPUMS missing / N/A codes with NaN in continuous variables.

    Categorical variables in `keep` are left as-is (their N/A codes are handled by the recode
    helpers), because blanking them would silently drop people from denominators.
    """
    df = df.copy()
    codes = {k: list(v) for k, v in MISSING.get(collection, {}).items()}
    if codebook is not None:
        for k, v in missing_codes(codebook).items():
            codes.setdefault(k, []).extend(v)
    for col in (columns or df.columns):
        if col in keep or col not in codes or col not in df:
            continue
        df[col] = df[col].mask(df[col].isin(codes[col]))
    return df


# ---------------------------------------------------------------- weighted statistics
def wmean(x, w) -> float:
    x, w = np.asarray(x, float), np.asarray(w, float)
    m = ~(np.isnan(x) | np.isnan(w))
    return float(np.average(x[m], weights=w[m])) if m.any() and w[m].sum() > 0 else np.nan


def wquantile(x, w, q=0.5) -> float:
    x, w = np.asarray(x, float), np.asarray(w, float)
    m = ~(np.isnan(x) | np.isnan(w)) & (w > 0)
    if not m.any():
        return np.nan
    x, w = x[m], w[m]
    o = np.argsort(x)
    x, w = x[o], w[o]
    cw = np.cumsum(w) - 0.5 * w
    return float(np.interp(q * w.sum(), cw, x))


def wmedian(x, w) -> float:
    return wquantile(x, w, 0.5)


def wquantile_binned(x, w, q=0.5, width: float = 2500.0, offset: float = 0.0) -> float:
    """Census-style quantile of a dollar amount: find the $`width` interval that holds the quantile and
    interpolate linearly inside it (Census income and earnings medians use $2,500 intervals starting at $0).
    BLS weekly-earnings medians use $50 intervals centred on multiples of $50: width=50, offset=25. Reported
    dollars heap on round numbers (60,000, 75,000), so a plain weighted median sticks to a heap: it differs
    from the published figure and its replicate SE can come out 0."""
    x, w = np.asarray(x, float), np.asarray(w, float)
    m = ~(np.isnan(x) | np.isnan(w)) & (w > 0)
    if not m.any():
        return np.nan
    x, w = x[m], w[m]
    k = np.floor((x - offset) / width)
    bins, inv = np.unique(k, return_inverse=True)
    bw = np.bincount(inv, weights=w)
    cum = np.cumsum(bw)
    target = q * w.sum()
    i = int(np.searchsorted(cum, target))
    below = cum[i - 1] if i else 0.0
    return float(offset + bins[i] * width + width * (target - below) / bw[i])


def wmedian_binned(x, w, width: float = 2500.0, offset: float = 0.0) -> float:
    return wquantile_binned(x, w, 0.5, width, offset)


def wshare(mask, w) -> float:
    mask, w = np.asarray(mask, float), np.asarray(w, float)
    m = ~(np.isnan(mask) | np.isnan(w))
    return float((mask[m] * w[m]).sum() / w[m].sum()) if w[m].sum() > 0 else np.nan


# Measure factories for `grouped`. Each returns f(subframe, weight_col) -> float.
def mean(col: str, where: Callable | None = None):
    def f(d, w):
        d = d[where(d)] if where else d
        return wmean(d[col], d[w])
    return f


def median(col: str, where: Callable | None = None):
    def f(d, w):
        d = d[where(d)] if where else d
        return wmedian(d[col], d[w])
    return f


def median_binned(col: str, width: float = 2500.0, where: Callable | None = None, offset: float = 0.0):
    """Interpolated median (see wquantile_binned): Census dollar amounts (default) or BLS weekly earnings
    (width=50, offset=25)."""
    def f(d, w):
        d = d[where(d)] if where else d
        return wmedian_binned(d[col], d[w], width, offset)
    return f


def share(pred: Callable, where: Callable | None = None, col: str | None = None):
    """Weighted share of rows (within `where`, default all rows in the cell) satisfying `pred`. Name the variable
    the predicate reads as `col`: its missing and NIU rows (NaN after clean_missing) then leave the denominator.
    Without it, `d.ANYCOVLY == 1` is False for NIU rows and counts them as "no" (a silent universe error)."""
    def f(d, w):
        d = d[where(d)] if where else d
        if col is not None:
            d = d[d[col].notna()]
        return wshare(pred(d), d[w])
    return f


def total(col: str | None = None, scale: float = 1.0, where: Callable | None = None):
    """Weighted population count (col=None) or weighted sum of col, times scale."""
    def f(d, w):
        d = d[where(d)] if where else d
        v = d[w] if col is None else d[col] * d[w]
        return float(v.sum()) * scale
    return f


def grouped(df: pd.DataFrame, by: list[str], weight: str, measures: dict,
            min_n: int = 100) -> pd.DataFrame:
    rows = []
    for key, d in df.groupby(by, dropna=False, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        r = dict(zip(by, key))
        for name, fn in measures.items():
            r[name] = fn(d, weight)
        r["n_obs"] = len(d)
        r["low_n"] = len(d) < min_n
        rows.append(r)
    out = pd.DataFrame(rows)
    for col in by:                   # keep category order (e.g. education groups) for later pivots/charts
        if isinstance(df[col].dtype, pd.CategoricalDtype) and col in out:
            out[col] = pd.Categorical(out[col], categories=df[col].cat.categories,
                                      ordered=df[col].cat.ordered)
    return out.sort_values(by, kind="stable").reset_index(drop=True) if len(out) else out


# ---------------------------------------------------------------- common recodes
def _codes_for(collection: str, helper: str, known=("usa", "cps")) -> str:
    """Recodes are written per collection: codes differ (EMPSTAT, EDUC, CITIZEN), so an unknown one must fail
    loudly instead of borrowing another collection's codes."""
    if collection not in known:
        raise ValueError(f"{helper}() has no codes for collection {collection!r} (only {', '.join(known)}): "
                         "recode from data/ipums/codebook.csv instead")
    return collection


def emp_status(df: pd.DataFrame, collection: str) -> pd.Series:
    """'employed' / 'unemployed' / 'nilf' / NaN (not in universe, armed forces).

    NHIS EMPSTAT (sample adults): 100-122 employed, 210-217 unemployed, 220 NILF, 200 'not employed' without
    detail -> 'not employed' (the only split from 2019 on: 100/200/999). MEPS has no EMPSTAT variable.
    """
    _codes_for(collection, "emp_status", ("usa", "cps", "nhis"))
    e = df["EMPSTAT"]
    if collection == "cps":          # CPS EMPSTAT: 10,12 employed; 20-22 unemployed; 30-36 NILF; 1 armed forces
        out = np.select([e.between(10, 12), e.between(20, 22), e.between(30, 36)],
                        ["employed", "unemployed", "nilf"], default=None)
    elif collection == "nhis":
        out = np.select([e.between(100, 122), e.between(210, 217), e == 220, e == 200],
                        ["employed", "unemployed", "nilf", "not employed"], default=None)
    else:                            # USA EMPSTAT: 1 employed, 2 unemployed, 3 NILF, 0 N/A
        out = np.select([e == 1, e == 2, e == 3], ["employed", "unemployed", "nilf"], default=None)
    return pd.Series(out, index=df.index, dtype="object")


def foreign_born(df: pd.DataFrame, collection: str | None = None) -> pd.Series:
    """True if foreign born (Census definition: not a U.S. citizen at birth).

    CPS: CITIZEN 4 naturalized / 5 not a citizen = foreign born; 1-3 native (born in the U.S., in outlying
         areas, or abroad of American parents). NATIVITY 5 is NOT this: it also holds CITIZEN 2 and 3 (about 8
         percent of its earners in 2014-2024), and BLS's foreign-born medians match CITIZEN 4/5 (2024 full-time
         weekly: 1,003 against BLS's 1,001; NATIVITY 5 gives 1,012). NATIVITY is used only without CITIZEN.
    USA (ACS): NATIVITY is not available for ACS samples; use CITIZEN: 0 N/A (born in US), 1 born abroad
         of American parents -> native; 2-5 (naturalized, not a citizen, ...) -> foreign born;
         8 illegible, 9 missing -> NaN.
    NHIS, MEPS: USBORN 20 born in the U.S. or 11 in a U.S. territory -> native; 10 (no, no detail) and 12 -> foreign
         born (10 also holds territory births in samples that do not split them); 96-99 -> NaN.
    Without `collection`, the coding is told apart by the weight columns (ASECWT/WTFINL: CPS; PERWT: USA).
    """
    if collection in HEALTH_SURVEYS:
        u = df["USBORN"]
        return pd.Series(np.where(u.isin([10, 12]), True, np.where(u.isin([11, 20]), False, None)),
                         index=df.index, dtype="object")
    if collection is None:
        if "ASECWT" in df or "WTFINL" in df:
            collection = "cps"
        elif "PERWT" in df:
            collection = "usa"
        else:
            raise ValueError("foreign_born(): cannot tell the collection from the columns: pass collection='usa' or 'cps'")
    _codes_for(collection, "foreign_born")
    if collection == "cps" and "CITIZEN" not in df and "NATIVITY" in df:
        n = df["NATIVITY"]                          # birthplace only: a CITIZEN 2/3 person counts as foreign
        print("[stats] WARNING: foreign_born: CPS without CITIZEN; NATIVITY 5 includes people born in outlying "
              "areas or abroad of American parents. Add CITIZEN to the extract for the BLS definition.",
              file=sys.stderr)
        return pd.Series(np.where(n == 5, True, np.where(n.between(1, 4), False, None)),
                         index=df.index, dtype="object")
    c = df["CITIZEN"]
    if collection == "cps":
        return pd.Series(np.where(c.isin([4, 5]), True, np.where(c.between(1, 3), False, None)),
                         index=df.index, dtype="object")
    return pd.Series(np.where(c.between(2, 5), True, np.where(c.between(0, 1), False, None)),   # USA coding
                     index=df.index, dtype="object")


EDUC_LABELS = {
    "educ4": ["Less than high school", "High school diploma", "Some college",
             "Bachelor's degree or more"],
    "educ5": ["High school diploma or less", "Some college", "Bachelor's degree",
             "Master's degree", "Professional degree or doctorate"],
}


def educ_group(df: pd.DataFrame, collection: str, scheme: str = "educ4") -> pd.Series:
    """Common education groups.

    educ4: <HS, HS diploma/GED, some college (incl. associate), BA+   (4 groups)
    educ5: HS or less, some college, BA, MA, professional/doctorate   (5 groups, for pay comparisons)
    USA needs EDUCD (detailed); CPS uses EDUC (already detailed, 3-digit).
    NHIS, MEPS EDUC: 100-116 < HS, 200-202 HS/GED, 300-303 some college/associate, 400 BA, 510 MA, 520-522
    professional/doctorate; 500 (graduate, no detail) counts as BA+ in educ4 and stays unassigned in educ5;
    530 'Other degree' (about 8% of MEPS 2022 persons) and 0 NIU stay unassigned: say how they are treated.
    Restrict to adults first: MEPS codes children's grade in school (101-116), which would count as < HS.
    """
    if _codes_for(collection, "educ_group", ("usa", "cps") + HEALTH_SURVEYS) in HEALTH_SURVEYS:
        e = df["EDUC"]
        lt_hs, hs, some, ba, ma, prof = (e.between(100, 116), e.between(200, 202), e.between(300, 303),
                                         e == 400, e == 510, e.between(520, 522))
        if scheme == "educ4":
            ma = ma | (e == 500)
    elif collection == "usa":
        e = df["EDUCD"]
        # 60 "Grade 12", 100 "4 years of college" and 110-113 "5+ years" are pre-1990 census
        # years-of-schooling codes; 61 "12th grade, no diploma" (ACS) is less than high school
        lt_hs, hs, some, ba, ma, prof = (e.between(2, 59) | (e == 61), e.isin([60, 62, 63, 64]), e.between(65, 90),
                                         e.isin([100, 101]), e.between(110, 114), e.isin([115, 116]))
    else:
        e = df["EDUC"]
        # 72 "12th grade, diploma unclear", 110 "4 years of college" and 120-122 "5+ years" are
        # pre-1992 years-of-schooling codes
        lt_hs, hs, some, ba, ma, prof = (e.between(2, 71), e.isin([72, 73]), e.between(80, 100),
                                         e.isin([110, 111]), e.between(120, 123), e.isin([124, 125]))
    if scheme == "educ4":
        conds, labels = [lt_hs, hs, some, ba | ma | prof], EDUC_LABELS["educ4"]
    elif scheme == "educ5":
        conds, labels = [lt_hs | hs, some, ba, ma, prof], EDUC_LABELS["educ5"]
    else:
        raise ValueError(scheme)
    s = pd.Series(np.select(conds, labels, default=None), index=df.index, dtype="object")
    return pd.Categorical(s, categories=labels, ordered=True)


def age_group(age: pd.Series, edges=(25, 35, 45, 55), labels=None) -> pd.Categorical:
    """age_group(df.AGE, (22, 25, 30, 35, 40, 45)) -> '22-24', '25-29', ..."""
    edges = list(edges)
    labels = labels or [f"{a}-{b - 1}" for a, b in zip(edges[:-1], edges[1:])]
    return pd.cut(age, bins=edges, right=False, labels=labels)


def _yes_no(s: pd.Series, yes, no) -> pd.Series:
    """1.0 / 0.0 / NaN, so share() leaves unknown answers out of the denominator."""
    return pd.Series(np.where(s.isin(yes), 1.0, np.where(s.isin(no), 0.0, np.nan)), index=s.index)


def uninsured(df: pd.DataFrame, collection: str) -> pd.Series:
    """1.0 uninsured, 0.0 covered, NaN NIU or unknown.

    NHIS HINOTCOVE (at interview): 2 no coverage, 1 has coverage; 0 NIU, 7-9 unknown.
    MEPS HINOTCOV (all year): 2 no coverage, 1 has coverage; 0 NIU, 9 unknown.
    USA HCOVANY: 1 no coverage, 2 covered. CPS coverage changed in 2014 and 2019: see docs/variables.md.
    For a standard error, pass masks without NaN: share_z(df, u == 1, u.notna(), w).
    """
    c = _codes_for(collection, "uninsured", ("usa", "nhis", "meps"))
    col, yes, no = {"nhis": ("HINOTCOVE", [2], [1]), "meps": ("HINOTCOV", [2], [1]),
                    "usa": ("HCOVANY", [1], [2])}[c]
    return _yes_no(df[col], yes, no)


def fair_poor_health(df: pd.DataFrame) -> pd.Series:
    """1.0 fair or poor (HEALTH 4, 5), 0.0 excellent to good (1-3), NaN NIU (0) or unknown (7-9). NHIS and MEPS.

    MEPS HEALTH comes from the self-administered questionnaire (adults; 0 = NIU otherwise in 2022): use SAQWEIGHT.
    """
    return _yes_no(df["HEALTH"], [4, 5], [1, 2, 3])


def sample_adults(df: pd.DataFrame) -> pd.Series:
    """NHIS sample adults with a record (ASTATFLG 1): a domain mask; sample-adult items use SAMPWEIGHT."""
    return df["ASTATFLG"] == 1


def sample_children(df: pd.DataFrame) -> pd.Series:
    """NHIS sample children with a record (CSTATFLG 1): a domain mask; sample-child items use SAMPWEIGHT."""
    return df["CSTATFLG"] == 1


def default_weight(collection: str, years=None, universe: str = "person") -> str:
    """The usual weight; the variable's IPUMS page has the final say.

    nhis: SAMPWEIGHT for sample-adult / sample-child items (universe "sample") and for every item from 2019;
          PERWEIGHT for person-file items up to 2018.
    meps: PERWEIGHT; SAQWEIGHT for self-administered questionnaire items (universe "saq", e.g. HEALTH).
    usa: PERWT; cps: ASECWT (WTFINL for basic monthly, EARNWT for ORG earnings).
    """
    c = _codes_for(collection, "default_weight", ("usa", "cps") + HEALTH_SURVEYS)
    if c == "nhis":
        late = years is not None and max(int(y) for y in np.atleast_1d(years)) >= 2019
        return "SAMPWEIGHT" if universe == "sample" or late else "PERWEIGHT"
    if c == "meps":
        return "SAQWEIGHT" if universe == "saq" else "PERWEIGHT"
    return {"usa": "PERWT", "cps": "ASECWT"}[c]


def pool_weight(df: pd.DataFrame, weight: str) -> pd.Series:
    """Weight divided by the number of survey years in df, so pooled totals stay annual averages
    (the NHIS and MEPS pooling rule; also for pooled ACS or CPS years)."""
    return df[weight] / df["YEAR"].nunique()


def to_constant_dollars(amount: pd.Series, cpi99: pd.Series, target_cpi99: float) -> pd.Series:
    """Convert nominal dollars to a target year using IPUMS CPI99.

    CPI99 is the multiplier to 1999 dollars for each record's INCOME year, so amount * CPI99 gives 1999
    dollars; divide by the CPI99 of the income year you want. Get target_cpi99 from the IPUMS CPI99 table
    (cps.ipums.org/cps/cpi99.shtml, usa.ipums.org/usa/cpi99.shtml) or from the extract: in CPS ASEC the row
    with YEAR == Y carries the factor of income year Y-1 (the 2000 ASEC reports 1999 income, CPI99 = 1.000),
    so df.loc[df.YEAR == Y, 'CPI99'] gives income-year Y-1 dollars; in the ACS it gives year-Y dollars.
    """
    return amount * cpi99 / target_cpi99


def fmt_pct(x, digits=0):
    return "n/a" if pd.isna(x) else f"{100 * x:.{digits}f}%"


def fmt_dollars(x, digits=0):
    return "n/a" if pd.isna(x) else f"${x:,.{digits}f}"


# ---------------------------------------------------------------- standard errors
def rep_se(df: pd.DataFrame, estimator: Callable, collection: str, weight: str | None = None,
           prefix: str = "REPWTP") -> tuple[float, float]:
    """Replicate-weight SE (successive difference replication), the Census-recommended method.

    estimator(df, weight_col) -> float.  Request "REPWTP" in the extract (ACS: 80 replicates,
    CPS ASEC: 160). SE = sqrt(4/R * sum((theta_r - theta)^2)).  Returns (estimate, se).
    """
    if collection not in ("usa", "cps"):
        raise ValueError(f"rep_se(): no replicate weights for collection {collection!r}; use lin_se() with the "
                         "survey's strata and PSU: lin_se(df, z, **survey_design(df, collection))")
    if not weight:                   # CPS: ASECWT on ASEC records, WTFINL on basic monthly ones (ASECWT empty)
        asec = "ASECWT" in df and df["ASECWT"].notna().any()
        weight = {"usa": "PERWT", "cps": "ASECWT" if asec else "WTFINL"}[collection]
    reps = [c for c in df.columns if re.fullmatch(rf"{prefix}\d+", c)]
    if not reps:
        raise ValueError(f"No {prefix}1..n columns: add REPWTP (person estimates) or REPWT with weight='HHWT' "
                         "(ACS) or 'ASECWTH' (CPS ASEC) and prefix='REPWT' (household estimates) to the extract to use rep_se() (ACS and CPS ASEC "
                         "only: CPS basic monthly files have no replicate weights; use an approximate SE and say so).")
    theta = estimator(df, weight)
    th = np.array([estimator(df, r) for r in reps])
    return theta, float(np.sqrt(4.0 / len(reps) * np.sum((th - theta) ** 2)))


# NHIS sample-design periods: strata and PSUs are comparable only within a period
# (nhis.ipums.org/nhis/userNotes_variance.shtml). 1995-96 and 1997-2005 are treated as independent.
NHIS_DESIGN_PERIODS = ((1963, 1972), (1973, 1984), (1985, 1994), (1995, 1996), (1997, 2005), (2006, 2015),
                       (2016, 9999))


def survey_design(df: pd.DataFrame, collection: str) -> dict:
    """Add unique stratum and PSU columns (DSTRATA, DPSU) to df and return {"strata": ..., "cluster": ...}
    for lin_se / se_estimate(**kw) and wls(se="cluster", cluster="DPSU"). Pass the whole file.

    usa:  STRATA (made unique by year) and CLUSTER.
    nhis: STRATA and PSU, with strata kept apart across design periods (pooled years inside one period
          share their PSUs). Warns when the years span the 2019 redesign: IPUMS advises against pooling
          or comparing trends across it.
    meps: STRATANN and PSUANN for one year; STRATAPLD and PSUPLD (request them) for pooled years.
    cps:  use rep_se with REPWTP instead.
    """
    c = _codes_for(collection, "survey_design", ("usa",) + HEALTH_SURVEYS)
    years = df["YEAR"]
    if c == "usa":
        strata, psu = years.astype("int64") * 10**6 + df["STRATA"], df["CLUSTER"]
    elif c == "nhis":
        period = np.select([years.between(a, b) for a, b in NHIS_DESIGN_PERIODS],
                           range(len(NHIS_DESIGN_PERIODS)), -1)
        strata, psu = pd.Series(period, index=df.index) * 10**6 + df["STRATA"], df["PSU"]
        if years.min() < 2019 <= years.max():
            import warnings
            warnings.warn("NHIS years span the 2019 redesign: IPUMS advises against pooling or comparing across it")
    elif years.nunique() > 1:          # meps, pooled years
        if not {"STRATAPLD", "PSUPLD"} <= set(df.columns):
            raise ValueError("Pooled MEPS years need STRATAPLD and PSUPLD (pooled variance structure): add them "
                             "to the extract")
        strata, psu = df["STRATAPLD"], df["PSUPLD"]
    else:
        strata, psu = df["STRATANN"], df["PSUANN"]
    if c in HEALTH_SURVEYS:            # PSUs are numbered within strata: make the id unique for wls clusters
        psu = strata.astype("int64") * 10**6 + psu
    df["DSTRATA"], df["DPSU"] = strata.to_numpy(), psu.to_numpy()
    return {"strata": "DSTRATA", "cluster": "DPSU"}


def _z_ratio(y, x, w):
    """Linearized scores for the ratio R = sum(w*y) / sum(w*x)."""
    y, x, w = (np.asarray(a, float) for a in (y, x, w))
    sx = np.sum(w * x)
    r = np.sum(w * y) / sx
    return w * (y - r * x) / sx, r


def lin_se(df: pd.DataFrame, z: np.ndarray, strata: str = "STRATA", cluster: str = "CLUSTER") -> float:
    """Design-based SE from linearized scores, with stratified cluster (ultimate cluster) variance.

    ACS (IPUMS-USA) provides STRATA and CLUSTER automatically. For the CPS ASEC, use rep_se() instead; CPS
    basic monthly has no design variables: pass a constant strata column and cluster="CPSID" (approximate).
    For ACS, NHIS and MEPS pass **survey_design(df, collection): it picks the design variables and keeps strata
    apart across pooled years (ACS) or NHIS design periods. Keep the whole file and mark the sub-population
    with the domain mask (share_z / mean_z): filtering rows first drops design information.
    Vectorized: about one second per 4 million records.
    """
    d = pd.DataFrame({"h": df[strata].to_numpy(), "c": df[cluster].to_numpy(), "z": np.asarray(z, float)})
    zc = d.groupby(["h", "c"], sort=False)["z"].sum()
    h = zc.index.get_level_values(0)
    g = zc.groupby(h)
    n = g.transform("size").to_numpy(float)
    dev2 = (zc - g.transform("mean")).to_numpy() ** 2
    keep = n > 1
    return float(np.sqrt(np.sum(dev2[keep] * (n[keep] / (n[keep] - 1)))))


def share_z(df, pred_mask, domain_mask, w):
    """Scores for a weighted share of `pred` within `domain` (masks are boolean Series over df)."""
    dom = np.asarray(domain_mask, bool)
    return _z_ratio(np.asarray(pred_mask, bool) & dom, dom, df[w])


def mean_z(df, col, domain_mask, w):
    """Scores for a weighted mean of `col` within `domain` (NaN values of col are excluded)."""
    v = df[col].to_numpy(float)
    dom = np.asarray(domain_mask, bool) & ~np.isnan(v)
    return _z_ratio(np.where(dom, v, 0.0), dom, df[w])


def se_estimate(df, z_and_r, **kw) -> tuple[float, float]:
    """(estimate, se) from share_z / mean_z output.  Difference of two estimates:
       (za, ra), (zb, rb) = ...; se = lin_se(df, za - zb); diff = ra - rb."""
    z, r = z_and_r
    return r, lin_se(df, z, **kw)


# ---------------------------------------------------------------- outliers / top codes
def screen_outliers(df: pd.DataFrame, col: str, weight: str, by: list[str] | None = None,
                    low: float | None = None, high: float | None = None) -> pd.DataFrame:
    """Report values at the top code (the maximum, when it repeats) and outside [low, high].

    Use it on every dollar variable (and on derived hourly wages, e.g. low=2, high=500) before
    computing means. A few top-coded or implausible records can move a small group's mean by
    10+ percent, so show results with and without them when they matter.
    """
    s = df[col]
    top = s.max()
    at_top = (s == top) & (int((s == top).sum()) > 1)
    out_lo = s < low if low is not None else pd.Series(False, index=df.index)
    out_hi = s > high if high is not None else pd.Series(False, index=df.index)
    flag = at_top | out_lo | out_hi
    g = df.assign(_top=at_top, _lo=out_lo, _hi=out_hi, _flag=flag)
    keys = by or []
    rows = []
    for key, d in (g.groupby(keys, observed=True) if keys else [((), g)]):
        key = key if isinstance(key, tuple) else (key,)
        w = d[weight]
        rows.append({**dict(zip(keys, key)), "n": len(d), "n_at_top_code": int(d._top.sum()),
                     "top_code_value": top, "n_below_low": int(d._lo.sum()), "n_above_high": int(d._hi.sum()),
                     "mean_all": wmean(d[col], w), "mean_excl_flagged": wmean(d.loc[~d._flag, col], w[~d._flag])})
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


def wls(df: pd.DataFrame, y: str, numeric: list[str] = (), categorical: list[str] = (), weight: str = "PERWT",
        se: str = "hc1", cluster: str | None = None, rep_prefix: str = "REPWTP", ref: dict | None = None) -> pd.DataFrame:
    """Weighted least squares with survey-appropriate standard errors.

    se="replicate": successive-difference replicate weights (REPWTP1..R), the Census method (preferred
                    when REPWTP was extracted); se="cluster": cluster-robust by `cluster` (e.g. CLUSTER
                    for ACS; for NHIS and MEPS, which have no replicate weights, cluster="DPSU" after
                    survey_design()); se="hc1": heteroskedasticity-robust.
    Use it for regression-adjusted gaps (the standard federal-vs-private pay method: log hourly wage on a sector dummy
    plus education, experience, demographics, occupation, region...) and for linear probability models
    of employment or participation. Returns coef, se, t, p (normal), ci90_low, ci90_high, n_obs.
    """
    cols = [y, weight] + list(numeric) + list(categorical) + ([cluster] if cluster else [])
    d = df.dropna(subset=cols)
    X = design_matrix(d, numeric, categorical, ref)
    Xv, yv, w = X.to_numpy(float), d[y].to_numpy(float), d[weight].to_numpy(float)

    def fit(wt):
        # normal equations, not sqrt(w) rescaling: ACS replicate weights can be negative
        XtW = Xv.T * wt
        b, *_ = np.linalg.lstsq(XtW @ Xv, XtW @ yv, rcond=None)
        return b
    b = fit(w)
    if se == "replicate":
        reps = [c for c in d.columns if re.fullmatch(rf"{rep_prefix}\d+", c)]
        if not reps:
            raise ValueError("se='replicate' needs REPWTP1..n columns in the extract (NHIS and MEPS have none: "
                             "use se='cluster' with cluster='DPSU' from survey_design())")
        B = np.array([fit(d[r].to_numpy(float)) for r in reps])
        V = 4.0 / len(reps) * ((B - b) ** 2).sum(axis=0)
        ses = np.sqrt(V)
    else:
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


def standardize(df: pd.DataFrame, group: str, cells: list[str], weight: str, rate: Callable,
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

    estimate(df) -> dict of named results (e.g. {"fed": 34.3, "private": 24.8, "gap_pct": 0.38}).
    variants: {"Main specification": lambda d: d, "Exclude top-coded": lambda d: d[~d.topcoded], ...}
    The first variant is the baseline; the table reports each result and its change from the baseline.
    """
    rows, base = [], None
    for name, transform in variants.items():
        d = transform(df)
        r = estimate(d)
        r = {"variant": name, "n_obs": len(d), **r}
        if base is None:
            base = r
        else:
            for k, v in list(r.items()):
                if k not in ("variant", "n_obs") and isinstance(v, (int, float)) and isinstance(base.get(k), (int, float)):
                    r[f"{k}_change"] = v - base[k]
        rows.append(r)
    return pd.DataFrame(rows)

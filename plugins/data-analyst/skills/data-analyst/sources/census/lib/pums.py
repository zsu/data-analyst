"""Recodes and replicate-weight standard errors for Census microdata from the census pack (pandas).

ACS PUMS (acs/acs1/pums, acs/acs5/pums) and the CPS (cps/asec/mar, cps/basic/<month>) as the Census Data API
publishes them: Census variable names and codes (ESR, SCHL, HICOV; PEMLR, A_HGA, PRCITSHP), not IPUMS codes.
Every code below was read from the API's variable metadata (data/census/<request>_codebook.csv has them all).

    pums = pack_lib("census", "pums", OUT, SKILL)
    df = pums.load(f"{OUT}/data/census/md_pums.csv")
    df["unins"] = pums.uninsured(df, "acs")
    u = df[pums.civilian_noninst(df)]
    est, se = pums.rep_se(u, lambda d, w: pums.wshare(d.unins, d[w]))     # PWGTP and PWGTP1-80

For grouped tables, binned medians, outlier screens and regressions use the general helpers of the ipums pack
(st = pack_lib("ipums", "stats", OUT, SKILL): grouped, wmedian_binned, screen_outliers, wls); its recodes
(emp_status, educ_group, foreign_born, clean_missing) are for IPUMS codes and do not apply here.
"""
from __future__ import annotations

import re
from typing import Callable

import numpy as np
import pandas as pd

KEYS = ("SERIALNO", "HRHHID", "HRHHID2", "PERIDNUM", "H_SEQ", "state", "STATE", "PUMA", "ST")
SURVEYS = ("acs", "asec", "basic")


def load(path: str, **kw) -> pd.DataFrame:
    """The CSV with keys and geography codes as text (leading zeros kept) and every other column numeric."""
    head = pd.read_csv(path, nrows=0).columns
    df = pd.read_csv(path, dtype={c: str for c in head if c in KEYS}, low_memory=False, **kw)
    for c in df.columns:
        if c not in KEYS and (df[c].dtype == object or pd.api.types.is_string_dtype(df[c])):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _survey(survey: str, helper: str) -> str:
    if survey not in SURVEYS:
        raise ValueError(f"{helper}(): survey must be one of {', '.join(SURVEYS)} (acs = ACS PUMS, asec = CPS ASEC, "
                         f"basic = CPS basic monthly), not {survey!r}")
    return survey


def wshare(mask, w) -> float:
    """Weighted share of True among rows where mask is not missing."""
    m = pd.Series(mask)
    ok = m.notna().to_numpy()
    w = np.asarray(w, dtype=float)[ok]
    return float(np.sum(w * m[ok].astype(float).to_numpy()) / np.sum(w))


def wmean(x, w) -> float:
    x, w = np.asarray(x, dtype=float), np.asarray(w, dtype=float)
    ok = ~np.isnan(x)
    return float(np.sum(x[ok] * w[ok]) / np.sum(w[ok]))


def wtotal(mask, w) -> float:
    return float(np.sum(np.asarray(w, dtype=float) * pd.Series(mask).fillna(False).astype(float).to_numpy()))


REPLICATES = {"PWGTP": ("PWGTP", 80), "WGTP": ("WGTP", 80),          # ACS PUMS
              "MARSUPWT": ("PWWGT", 160), "HSUP_WGT": ("PWWGT", 160)}  # CPS ASEC (households: reference persons)


def rep_se(df: pd.DataFrame, estimator: Callable, weight: str = "PWGTP") -> tuple[float, float]:
    """(estimate, standard error) by Census's successive difference replication: the estimate with the full
    weight and with each replicate, SE = sqrt(4/R * sum((x_r - x)^2)). estimator(df, weight_column) -> float.
    ACS PUMS: PWGTP with PWGTP1-80 (replicates: true), WGTP with WGTP1-80 ("household"); R = 80.
    CPS ASEC: MARSUPWT with PWWGT1-160 (replicates: true, joined from Census's replicate weight file); household
    estimates on the reference persons (householders(df, "asec")) with HSUP_WGT and the same PWWGT; R = 160.
    CPS basic monthly files have none. 90 percent margin of error = 1.645 * SE."""
    if weight not in REPLICATES:
        raise ValueError(f"rep_se(): no replicate weights for {weight!r}; use one of {', '.join(REPLICATES)}")
    prefix, r = REPLICATES[weight]
    reps = [f"{prefix}{i}" for i in range(1, r + 1)]
    have = [c for c in reps if c in df.columns]
    if len(have) != r:
        raise ValueError(f"rep_se(): {len(have)} of the {r} {prefix}1-{r} columns are in the data: fetch the request "
                         "with replicates: true (ACS person or ASEC weights) or \"household\" (ACS WGTP); CPS basic "
                         "monthly files have none")
    x = estimator(df, weight)
    xr = np.array([estimator(df, c) for c in reps])
    return float(x), float(np.sqrt(4.0 / r * np.sum((xr - x) ** 2)))


def adjusted(df: pd.DataFrame, col: str, factor: str = "ADJINC") -> pd.Series:
    """An ACS dollar amount in the survey year's dollars: value * ADJINC / 1,000,000 (housing costs: ADJHSG).
    Census's published ACS dollar tables use the same factor. Codes below zero that mean N/A or a loss are left
    to the caller (see the codebook: PINCP -19999 = N/A, under 15)."""
    return df[col] * df[factor] / 1_000_000


def civilian_noninst(df: pd.DataFrame) -> pd.Series:
    """ACS: the civilian noninstitutionalized population (the universe of the published insurance tables, S2701):
    not in an institution (RELSHIPP 37) and not on active duty (ESR 4-5, or MIL 1 when ESR is absent)."""
    out = df["RELSHIPP"] != 37
    if "ESR" in df:
        out &= ~df["ESR"].isin([4, 5])
    elif "MIL" in df:
        out &= df["MIL"] != 1
    else:
        raise ValueError("civilian_noninst(): add ESR (or MIL) to the request to drop the armed forces")
    return out


def uninsured(df: pd.DataFrame, survey: str) -> pd.Series:
    """1.0 uninsured, 0.0 insured. ACS HICOV 2 (at the interview); ASEC NOW_COV 2 (at the interview; COV is any
    coverage during the previous calendar year, Census's P60 headline measure is the full-year one: COV 2)."""
    s = _survey(survey, "uninsured")
    if s == "acs":
        return (df["HICOV"] == 2).astype(float).where(df["HICOV"].isin([1, 2]))
    if s == "asec":
        return (df["NOW_COV"] == 2).astype(float).where(df["NOW_COV"].isin([1, 2]))
    raise ValueError("uninsured(): the CPS basic monthly file has no health insurance questions")


def emp_status(df: pd.DataFrame, survey: str) -> pd.Series:
    """'employed' / 'unemployed' / 'nilf' / NaN (under 16, armed forces, not in universe).
    ACS ESR: 1-2 employed, 3 unemployed, 6 NILF, 4-5 armed forces, 0 under 16.
    CPS PEMLR: 1-2 employed, 3-4 unemployed (on layoff, looking), 5-7 NILF, -1 not in universe."""
    s = _survey(survey, "emp_status")
    if s == "acs":
        e = df["ESR"]
        out = np.select([e.isin([1, 2]), e == 3, e == 6], ["employed", "unemployed", "nilf"], default=None)
    else:
        e = df["PEMLR"]
        out = np.select([e.isin([1, 2]), e.isin([3, 4]), e.between(5, 7)], ["employed", "unemployed", "nilf"],
                        default=None)
    return pd.Series(out, index=df.index, dtype="object")


def foreign_born(df: pd.DataFrame, survey: str) -> pd.Series:
    """True if foreign born (not a U.S. citizen at birth). ACS: NATIVITY 2 (CIT 4-5 is the same group);
    CPS: PRCITSHP 4 naturalized or 5 not a citizen (1-3 native, including born in outlying areas or abroad of
    American parents)."""
    s = _survey(survey, "foreign_born")
    if s == "acs":
        return df["NATIVITY"] == 2 if "NATIVITY" in df else df["CIT"].isin([4, 5])
    return df["PRCITSHP"].isin([4, 5])


def educ_group(df: pd.DataFrame, survey: str) -> pd.Series:
    """Four groups for adults: 'less than high school', 'high school', 'some college', 'bachelor's or higher'.
    ACS SCHL: 1-15 / 16-17 (diploma, GED) / 18-20 (some college, associate) / 21-24; 0 = under 3 -> NaN.
    CPS A_HGA (ASEC) or PEEDUCA (basic): 31-38 / 39 / 40-42 / 43-46; 0 or -1 -> NaN."""
    s = _survey(survey, "educ_group")
    labels = ["less than high school", "high school", "some college", "bachelor's or higher"]
    if s == "acs":
        e = df["SCHL"]
        conds = [e.between(1, 15), e.between(16, 17), e.between(18, 20), e.between(21, 24)]
    else:
        e = df["A_HGA"] if s == "asec" else df["PEEDUCA"]
        conds = [e.between(31, 38), e == 39, e.between(40, 42), e.between(43, 46)]
    return pd.Series(np.select(conds, labels, default=None), index=df.index, dtype="object")


def householders(df: pd.DataFrame, survey: str) -> pd.Series:
    """One row per household, for household estimates (with WGTP / HSUP_WGT): ACS SPORDER 1 in housing units
    (RELSHIPP 20, the reference person; group quarters have no household); ASEC A_EXPRRP 1-2 (reference person)."""
    s = _survey(survey, "householders")
    if s == "acs":
        return (df["SPORDER"] == 1) & (df["RELSHIPP"] == 20) if "RELSHIPP" in df else df["SPORDER"] == 1
    if s == "asec":
        return df["A_EXPRRP"].isin([1, 2])
    raise ValueError("householders(): use HRHHID/HRHHID2 with PULINENO for CPS basic households")

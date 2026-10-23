"""BLS series IDs: survey prefixes, a topic catalog of common series, and ID builders (standard library only).

    from series_ids import decode, build, search_catalog
    build("laus", state="MD", measure="unemployment_rate")   # [{"id": "LASST240000000000003", "label": ...}]
    build("laus", county="24005")                             # county LAUS (published unadjusted only)
    build("oews", occupation="15-1252", measure="annual_median_wage", state="MD")
    build("state", state="MD")                                # LAUS rate, employment, labor force + CES payrolls

Watch these IDs: U-6 is LNS13327709 (LNS13023621 is a level of job losers, not U-6); the ECI prefix is CI
(EI is import/export prices); WPUFD4 is PPI final demand, not finished goods. State codes come from scripts/analysis/combine.py (one state table for
the whole skill). IDs marked verified returned data from the BLS API on the date in VERIFIED, and each label was
compared with the series title on data.bls.gov/timeseries/<id> (--describe) on 2026-10-21.
"""
from __future__ import annotations

import os
import re
import sys

VERIFIED = "2026-10-20"

# Two-letter survey prefixes. The flag is True when the third character is S (adjusted) or U (not).
SURVEYS = {
    "LN": ("Current Population Survey (household survey), labor force statistics", True),
    "LE": ("Current Population Survey, weekly and hourly earnings", True),
    "CE": ("Current Employment Statistics (establishment survey), national", True),
    "SM": ("Current Employment Statistics, state and metro area", True),
    "CU": ("Consumer Price Index for All Urban Consumers (CPI-U)", True),
    "CW": ("Consumer Price Index for Urban Wage Earners and Clerical Workers (CPI-W)", True),
    "SU": ("Chained Consumer Price Index for All Urban Consumers (C-CPI-U)", True),
    "AP": ("CPI average prices", True),
    "WP": ("Producer Price Index, commodities and final demand", True),
    "PC": ("Producer Price Index, industries", True),
    "JT": ("Job Openings and Labor Turnover Survey (JOLTS)", True),
    "LU": ("Union affiliation (Current Population Survey, annual averages)", True),
    "LA": ("Local Area Unemployment Statistics (LAUS)", True),
    "EN": ("Quarterly Census of Employment and Wages (QCEW)", True),
    "OE": ("Occupational Employment and Wage Statistics (OEWS)", True),
    "CI": ("Employment Cost Index (ECI)", True),
    "CM": ("Employer Costs for Employee Compensation (ECEC)", True),
    "PR": ("Labor productivity and costs, major sectors", True),
    "IP": ("Labor productivity, detailed industries", True),
    "EI": ("Import and export price indexes", True),
    "BD": ("Business Employment Dynamics", True),
    "CX": ("Consumer Expenditure Surveys", False),
    "TU": ("American Time Use Survey", True),
    "NB": ("National Compensation Survey, benefits", False),
    "WS": ("Major work stoppages", False),
    "CF": ("Census of Fatal Occupational Injuries", True),
    "IS": ("Survey of Occupational Injuries and Illnesses", True),
}

# Topic catalog: (id, name, topic). Search with search_catalog() or `fetch.py --source bls --search <words>`.
CATALOG = [
    # employment (CES, thousands, seasonally adjusted)
    ("CES0000000001", "Total nonfarm employment (thousands, SA)", "employment"),
    ("CEU0000000001", "Total nonfarm employment (thousands, NSA)", "employment"),
    ("CES0500000001", "Total private employment (thousands)", "employment"),
    ("CES1000000001", "Mining and logging employment", "employment"),
    ("CES2000000001", "Construction employment", "employment"),
    ("CES3000000001", "Manufacturing employment", "employment"),
    ("CES4000000001", "Trade, transportation, and utilities employment", "employment"),
    ("CES5000000001", "Information employment", "employment"),
    ("CES5500000001", "Financial activities employment", "employment"),
    ("CES6000000001", "Professional and business services employment", "employment"),
    ("CES6500000001", "Education and health services employment", "employment"),
    ("CES7000000001", "Leisure and hospitality employment", "employment"),
    ("CES8000000001", "Other services employment", "employment"),
    ("CES9000000001", "Government employment", "employment"),
    # unemployment and participation (CPS, percent, SA)
    ("LNS14000000", "Unemployment rate, U-3, 16 years and over (SA)", "unemployment"),
    ("LNU04000000", "Unemployment rate, U-3 (NSA)", "unemployment"),
    ("LNS13327709", "U-6 unemployment rate: unemployed, marginally attached and part time for economic reasons (SA)", "unemployment"),
    ("LNS14000001", "Unemployment rate, men 16 years and over", "unemployment"),
    ("LNS14000002", "Unemployment rate, women 16 years and over", "unemployment"),
    ("LNS14000003", "Unemployment rate, White (SA)", "unemployment"),
    ("LNS14000006", "Unemployment rate, Black or African American (SA; from 1972)", "unemployment"),
    ("LNS14000009", "Unemployment rate, Hispanic or Latino (SA; from 1973)", "unemployment"),
    ("LNS14032183", "Unemployment rate, Asian (SA; from 2000)", "unemployment"),
    ("LNU04000003", "Unemployment rate, White (NSA)", "unemployment"),
    ("LNU04000006", "Unemployment rate, Black or African American (NSA)", "unemployment"),
    ("LNS14024887", "Unemployment rate, 16 to 24 years", "unemployment"),
    ("LNS14000012", "Unemployment rate, 16 to 19 years", "unemployment"),
    ("LNS14000036", "Unemployment rate, 20 to 24 years", "unemployment"),
    ("LUU0204899600", "Union members, percent of employed, United States (union membership rate, annual)", "unemployment"),
    ("LNS14027659", "Unemployment rate, 25 years and over, less than a high school diploma (education)", "unemployment"),
    ("LNS14027660", "Unemployment rate, 25 years and over, high school graduates, no college (education)", "unemployment"),
    ("LNS14027689", "Unemployment rate, 25 years and over, some college or associate degree (education)", "unemployment"),
    ("LNS14027662", "Unemployment rate, 25 years and over, bachelor's degree and higher (education)", "unemployment"),
    ("LNS11300000", "Labor force participation rate", "unemployment"),
    ("LNS11300060", "Labor force participation rate, 25 to 54 years", "unemployment"),
    ("LNS12300000", "Employment-population ratio", "unemployment"),
    ("LNS12300060", "Employment-population ratio, 25 to 54 years", "unemployment"),
    ("LNS12300061", "Employment-population ratio, 25 to 54 years, men", "unemployment"),
    ("LNS12300062", "Employment-population ratio, 25 to 54 years, women", "unemployment"),
    ("LNS12000000", "Employed, level (thousands)", "unemployment"),
    ("LNS13000000", "Unemployed, level (thousands)", "unemployment"),
    # wages and earnings
    ("CES0500000003", "Average hourly earnings, total private", "wages"),
    ("CES0500000008", "Average hourly earnings, production and nonsupervisory employees", "wages"),
    ("CES0500000011", "Average weekly earnings, total private", "wages"),
    ("CES0500000013", "Real average hourly earnings, total private (1982-84 dollars)", "wages"),
    ("CES0500000002", "Average weekly hours, total private", "wages"),
    ("CES3000000003", "Average hourly earnings, manufacturing", "wages"),
    ("CES6562000003", "Average hourly earnings, health care and social assistance", "wages"),
    ("LES1252881500", "Median usual weekly earnings, full-time wage and salary workers (quarterly, SA)", "wages"),
    ("LES1252881600", "Median usual weekly earnings, full-time, 1982-84 dollars (quarterly, SA)", "wages"),
    ("LEU0252881500", "Median usual weekly earnings, full-time wage and salary workers (quarterly, NSA)", "wages"),
    ("LEU0252881600", "Median usual weekly earnings, full-time, 1982-84 dollars (quarterly, NSA)", "wages"),
    ("CIS1010000000000I", "Employment Cost Index, total compensation, civilian (index, SA, quarterly)", "wages"),
    ("CIU1010000000000A", "Employment Cost Index, total compensation, civilian, 12-month percent change", "wages"),
    ("CIU1020000000000A", "Employment Cost Index, wages and salaries, civilian, 12-month percent change", "wages"),
    ("CIU2010000000000A", "Employment Cost Index, total compensation, private industry, 12-month percent change",
     "wages"),
    ("CIU2020000000000A", "Employment Cost Index, wages and salaries, private industry, 12-month percent change",
     "wages"),
    ("CIU2030000000000A", "Employment Cost Index, total benefits, private industry, 12-month percent change", "wages"),
    ("CIU3010000000000A", "Employment Cost Index, total compensation, state and local government, 12-month percent "
     "change", "wages"),
    ("CIU3020000000000A", "Employment Cost Index, wages and salaries, state and local government, 12-month percent "
     "change", "wages"),
    ("CIS2020000000000I", "Employment Cost Index, wages and salaries, private industry (index, SA, quarterly)", "wages"),
    # CPI
    ("CUUR0000SA0", "CPI-U, all items, U.S. city average (1982-84=100, NSA)", "cpi"),
    ("CUSR0000SA0", "CPI-U, all items (SA)", "cpi"),
    ("CUUR0000SAM", "CPI-U, medical care (NSA)", "cpi"),
    ("CWSR0000SA0", "CPI-W, all items (SA; the deflator of real earnings of production and nonsupervisory employees)",
     "cpi"),
    ("CES0500000013", "Real average hourly earnings, all employees, total private, 1982-84 dollars (SA)", "wages"),
    ("CES0500000032", "Real average hourly earnings, production and nonsupervisory employees, 1982-84 dollars (SA)",
     "wages"),
    ("CUUR0000SAM2", "CPI-U, medical care services (NSA)", "cpi"),
    ("CUUR0000SETE", "CPI-U, motor vehicle insurance (NSA)", "cpi"),
    ("CUUR0000SETA02", "CPI-U, used cars and trucks (NSA)", "cpi"),
    ("CUUR0000SEHA", "CPI-U, rent of primary residence (NSA)", "cpi"),
    ("CUUR0000SEHC", "CPI-U, owners' equivalent rent of residences (NSA)", "cpi"),
    ("CUUR0000SAF11", "CPI-U, food at home (NSA)", "cpi"),
    ("CUUR0000SEFV", "CPI-U, food away from home (NSA)", "cpi"),
    ("CUUR0000SAS", "CPI-U, services (NSA)", "cpi"),
    ("CUUR0000SA0L1E", "CPI-U, all items less food and energy (core, NSA)", "cpi"),
    ("CUSR0000SA0L1E", "CPI-U, all items less food and energy (core, SA)", "cpi"),
    ("CWUR0000SA0", "CPI-W, all items (NSA)", "cpi"),
    ("SUUR0000SA0", "Chained CPI-U, all items (NSA; interim for 10 to 12 months)", "cpi"),
    ("CUURS49ASA0", "CPI-U, all items, Los Angeles area (metro CPI, NSA)", "cpi"),
    ("CUURS11ASA0", "CPI-U, all items, Boston-Cambridge-Newton (metro CPI, NSA)", "cpi"),
    ("CUURS12ASA0", "CPI-U, all items, New York-Newark-Jersey City (metro CPI, NSA)", "cpi"),
    ("CUURS12BSA0", "CPI-U, all items, Philadelphia-Camden-Wilmington (metro CPI, NSA)", "cpi"),
    ("CUURS23ASA0", "CPI-U, all items, Chicago-Naperville-Elgin (metro CPI, NSA)", "cpi"),
    ("CUURS23BSA0", "CPI-U, all items, Detroit-Warren-Dearborn (metro CPI, NSA)", "cpi"),
    ("CUURS24ASA0", "CPI-U, all items, Minneapolis-St. Paul-Bloomington (metro CPI, NSA)", "cpi"),
    ("CUURS24BSA0", "CPI-U, all items, St. Louis (metro CPI, NSA)", "cpi"),
    ("CUURS35ASA0", "CPI-U, all items, Washington-Arlington-Alexandria (metro CPI, NSA)", "cpi"),
    ("CUURS35BSA0", "CPI-U, all items, Miami-Fort Lauderdale-West Palm Beach (metro CPI, NSA)", "cpi"),
    ("CUURS35CSA0", "CPI-U, all items, Atlanta-Sandy Springs-Roswell (metro CPI, NSA)", "cpi"),
    ("CUURS35DSA0", "CPI-U, all items, Tampa-St. Petersburg-Clearwater (metro CPI, NSA)", "cpi"),
    ("CUURS35ESA0", "CPI-U, all items, Baltimore-Columbia-Towson (metro CPI, NSA)", "cpi"),
    ("CUURS37ASA0", "CPI-U, all items, Dallas-Fort Worth-Arlington (metro CPI, NSA)", "cpi"),
    ("CUURS37BSA0", "CPI-U, all items, Houston-The Woodlands-Sugar Land (metro CPI, NSA)", "cpi"),
    ("CUURS48ASA0", "CPI-U, all items, Phoenix-Mesa-Scottsdale (metro CPI, NSA)", "cpi"),
    ("CUURS48BSA0", "CPI-U, all items, Denver-Aurora-Lakewood (metro CPI, NSA)", "cpi"),
    ("CUURS49BSA0", "CPI-U, all items, San Francisco-Oakland-Hayward (metro CPI, NSA)", "cpi"),
    ("CUURS49CSA0", "CPI-U, all items, Riverside-San Bernardino-Ontario (metro CPI, NSA)", "cpi"),
    ("CUURS49DSA0", "CPI-U, all items, Seattle-Tacoma-Bellevue (metro CPI, NSA)", "cpi"),
    ("CUURS49ESA0", "CPI-U, all items, San Diego-Carlsbad (metro CPI, NSA)", "cpi"),
    ("CUURS49FSA0", "CPI-U, all items, Urban Hawaii (metro CPI, NSA)", "cpi"),
    ("CUURS49GSA0", "CPI-U, all items, Urban Alaska (metro CPI, NSA)", "cpi"),
    ("CUUR0000SAF1", "CPI-U, food", "cpi_components"),
    ("CUUR0000SA0E", "CPI-U, energy", "cpi_components"),
    ("CUUR0000SAH1", "CPI-U, shelter", "cpi_components"),
    ("CUUR0000SETB01", "CPI-U, gasoline (all types)", "cpi_components"),
    ("CUUR0000SAM", "CPI-U, medical care", "cpi_components"),
    ("CUUR0000SAE", "CPI-U, education and communication", "cpi_components"),
    ("CUUR0000SAT", "CPI-U, transportation", "cpi_components"),
    ("CUUR0000SAR", "CPI-U, recreation", "cpi_components"),
    ("CUUR0000SAA", "CPI-U, apparel", "cpi_components"),
    # PPI
    ("WPSFD4", "PPI final demand (SA)", "ppi"),
    ("WPUFD4", "PPI final demand (NSA)", "ppi"),
    # import and export prices (MXP, BEA End Use; NSA only; revised for three months without a P flag)
    ("EIUIR", "Import price index, all imports (import prices, NSA)", "mxp"),
    ("EIUIR10", "Import price index, fuel imports (import prices, NSA)", "mxp"),
    ("EIUIREXFUELS", "Import price index, nonfuel imports (import prices, NSA)", "mxp"),
    ("EIUIQ", "Export price index, all exports (export prices, NSA)", "mxp"),
    ("EIUIQAG", "Export price index, agricultural exports (export prices, NSA)", "mxp"),
    ("EIUIQEXAG", "Export price index, nonagricultural exports (export prices, NSA)", "mxp"),
    # productivity
    ("PRS85006092", "Nonfarm business labor productivity, percent change at annual rate (quarterly)", "productivity"),
    ("PRS85006112", "Nonfarm business unit labor costs, percent change at annual rate (quarterly)", "productivity"),
    ("PRS85006093", "Nonfarm business labor productivity, index 2017=100 (quarterly; for growth since a date)",
     "productivity"),
    ("PRS85006103", "Nonfarm business hourly compensation, index 2017=100 (quarterly)", "productivity"),
    ("PRS85006153", "Nonfarm business real hourly compensation (CPI-deflated), index 2017=100 (quarterly)",
     "productivity"),
    ("PRS85006173", "Nonfarm business labor share, index 2017=100 (quarterly)", "productivity"),
    ("PRS85006143", "Nonfarm business value-added output price deflator, index 2017=100 (quarterly)", "productivity"),
    # JOLTS
    ("JTS000000000000000JOL", "Job openings, total nonfarm (thousands)", "jolts"),
    ("JTS000000000000000JOR", "Job openings rate", "jolts"),
    ("JTS000000000000000HIR", "Hires rate", "jolts"),
    ("JTS000000000000000QUR", "Quits rate", "jolts"),
    ("JTS000000000000000TSR", "Total separations rate", "jolts"),
    ("JTS000000000000000LDR", "Layoffs and discharges rate", "jolts"),
    # OEWS
    ("OEUN000000000000000000004", "OEWS annual mean wage, all occupations, United States (latest year only)", "oews"),
]

LAUS_MEASURES = {"unemployment_rate": "03", "unemployment": "04", "employment": "05", "labor_force": "06"}
OEWS_MEASURES = {"employment": "01", "hourly_mean_wage": "03", "annual_mean_wage": "04", "hourly_median_wage": "08",
                 "annual_10th_percentile_wage": "11", "annual_median_wage": "13", "annual_90th_percentile_wage": "15"}
# JOLTS national industries (titles read back with --describe on 2026-10-21); a few codes end in 099
JOLTS_INDUSTRIES = {
    "000000": "Total nonfarm", "100000": "Total private", "110099": "Mining and logging", "230000": "Construction",
    "300000": "Manufacturing", "320000": "Durable goods manufacturing", "340000": "Nondurable goods manufacturing",
    "400000": "Trade, transportation, and utilities", "420000": "Wholesale trade", "440000": "Retail trade",
    "480099": "Transportation, warehousing, and utilities", "510000": "Information", "510099": "Financial activities",
    "520000": "Finance and insurance", "530000": "Real estate and rental and leasing",
    "540099": "Professional and business services", "600000": "Private education and health services",
    "610000": "Private educational services", "620000": "Health care and social assistance",
    "700000": "Leisure and hospitality", "710000": "Arts, entertainment, and recreation",
    "720000": "Accommodation and food services", "810000": "Other services", "900000": "Government",
    "910000": "Federal", "920000": "State and local", "923000": "State and local government education",
    "929000": "State and local government, excluding education"}
JOLTS_ELEMENTS = {"job_openings": "JO", "hires": "HI", "total_separations": "TS", "quits": "QU",
                  "layoffs": "LD", "other_separations": "OS"}
BUILDERS = ("laus", "oews", "state", "ces_state", "jolts")


def _state(x) -> tuple[str, str, str]:
    """(usps, fips, name) from any form, through the skill's one state table."""
    here = os.path.dirname(os.path.abspath(__file__))
    analysis = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(here))), "scripts", "analysis")
    if analysis not in sys.path:
        sys.path.insert(0, analysis)
    from combine import state            # needs pandas (imported by combine)
    return state(x, "usps"), state(x, "fips"), state(x, "name")


def decode(sid: str) -> dict:
    sid = sid.strip().upper()
    pre = sid[:2]
    name, has_sa = SURVEYS.get(pre, ("unknown survey prefix: check https://www.bls.gov/help/hlpforma.htm", False))
    seasonal = ""
    if has_sa and len(sid) > 2 and sid[2] in "SU":
        seasonal = "seasonally adjusted" if sid[2] == "S" else "not seasonally adjusted"
    return {"series_id": sid, "prefix": pre, "survey": name, "seasonal": seasonal}


def laus(state=None, county=None, measure: str = "unemployment_rate", seasonal: str = "S") -> list[dict]:
    """LAUS: a state (LASST/LAUST + FIPS + 11 zeros + measure) or a county (LAUCN + 5-digit FIPS + 8 zeros +
    measure; counties are published not seasonally adjusted only)."""
    if measure not in LAUS_MEASURES:
        raise ValueError(f"LAUS measure must be one of {sorted(LAUS_MEASURES)}")
    words = measure.replace("_", " ")
    code = LAUS_MEASURES[measure]
    out = []
    for c in _list(county):
        if not re.fullmatch(r"\d{5}", c):
            raise ValueError(f"county must be a 5-digit FIPS code (state + county, e.g. 24005), got {c!r}")
        out.append({"id": f"LAUCN{c}00000000{code}", "label": f"County {c} {words} (not seasonally adjusted)"})
    for s in _list(state):
        usps, fips, name = _state(s)
        sa = (seasonal or "S").upper()[:1]
        if sa not in "SU":
            raise ValueError("seasonal must be S (adjusted) or U (not adjusted)")
        out.append({"id": f"LA{sa}ST{fips}00000000000{code}",
                    "label": f"{name} {words} ({'seasonally adjusted' if sa == 'S' else 'not seasonally adjusted'})"})
    if not out:
        raise ValueError("laus needs state (name, USPS or FIPS) or county (5-digit FIPS)")
    return out


def oews(occupation, measure: str = "annual_median_wage", state=None) -> list[dict]:
    """OEWS (annual; the API returns the latest survey year): OEU + area (N0000000 national, S<fips>00000 a
    state) + industry 000000 (all) + 6-digit SOC + datatype."""
    if measure not in OEWS_MEASURES:
        raise ValueError(f"OEWS measure must be one of {sorted(OEWS_MEASURES)}")
    out = []
    for occ in _list(occupation):
        soc = occ.replace("-", "")
        if not re.fullmatch(r"\d{6}", soc):
            raise ValueError(f'SOC occupation code must look like "15-1252", got {occ!r}')
        if state:
            usps, fips, name = _state(state)
            area, where = f"S{fips}00000", name
        else:
            area, where = "N0000000", "United States"
        out.append({"id": f"OEU{area}000000{soc}{OEWS_MEASURES[measure]}",
                    "label": f"SOC {soc[:2]}-{soc[2:]} {measure.replace('_', ' ')}, {where} (OEWS, annual)"})
    if not out:
        raise ValueError("oews needs occupation (SOC code, e.g. 15-1252)")
    return out


def ces_state(state, industry: str = "00000000", datatype: str = "01", seasonal: str = "S") -> list[dict]:
    """State CES (SM): SM + S/U + state FIPS + area 00000 (statewide) + 8-digit industry + 2-digit data type."""
    if not re.fullmatch(r"\d{8}", industry) or not re.fullmatch(r"\d{2}", datatype):
        raise ValueError("industry is 8 digits (00000000 = total nonfarm), datatype 2 digits (01 = employment)")
    out = []
    for s in _list(state):
        usps, fips, name = _state(s)
        sa = (seasonal or "S").upper()[:1]
        what = "total nonfarm employment (thousands)" if (industry, datatype) == ("00000000", "01") else \
            f"industry {industry}, data type {datatype}"
        out.append({"id": f"SM{sa}{fips}00000{industry}{datatype}",
                    "label": f"{name} {what} ({'SA' if sa == 'S' else 'NSA'})"})
    return out


def jolts(industry="000000", measure: str = "quits", rate: bool = True, seasonal: str = "S") -> list[dict]:
    """National JOLTS: JT + S/U + 6-digit industry + state 00 + area 00000 + size class 00 + element + R/L.
    industry: codes or names from JOLTS_INDUSTRIES, comma-separated, or "all"."""
    if measure not in JOLTS_ELEMENTS:
        raise ValueError(f"JOLTS measure must be one of {sorted(JOLTS_ELEMENTS)}")
    sa = (seasonal or "S").upper()[:1]
    if sa not in "SU":
        raise ValueError("seasonal must be S (adjusted) or U (not adjusted)")
    names = {v.lower(): k for k, v in JOLTS_INDUSTRIES.items()}
    want = list(JOLTS_INDUSTRIES) if str(industry).lower() == "all" else _list(industry) or ["000000"]
    out = []
    for i in want:
        code = "000000" if i == "00000000" else names.get(i.lower(), i)
        if code not in JOLTS_INDUSTRIES:
            raise ValueError(f"unknown JOLTS industry {i!r}; use a code or name from: "
                             + "; ".join(f"{k} {v}" for k, v in JOLTS_INDUSTRIES.items()))
        out.append({"id": f"JT{sa}{code}000000000{JOLTS_ELEMENTS[measure]}{'R' if rate else 'L'}",
                    "label": f"{measure.replace('_', ' ').capitalize()} {'rate' if rate else 'level (thousands)'}, "
                             f"{JOLTS_INDUSTRIES[code]} ({'SA' if sa == 'S' else 'NSA'})"})
    return out


def state_set(state) -> list[dict]:
    """The state employment set: LAUS unemployment rate, employment, labor force and CES payrolls."""
    out = []
    for m in ("unemployment_rate", "employment", "labor_force"):
        out += laus(state=state, measure=m)
    return out + ces_state(state)


def build(kind: str, **kw) -> list[dict]:
    kw = {k: v for k, v in kw.items() if v not in (None, "")}
    if kind == "laus":
        return laus(kw.get("state"), kw.get("county"), kw.get("measure", "unemployment_rate"), kw.get("seasonal", "S"))
    if kind == "oews":
        return oews(kw.get("occupation"), kw.get("measure", "annual_median_wage"), kw.get("state"))
    if kind == "state":
        return state_set(kw.get("state"))
    if kind == "ces_state":
        return ces_state(kw.get("state"), kw.get("industry", "00000000"), kw.get("datatype", "01"),
                         kw.get("seasonal", "S"))
    if kind == "jolts":
        return jolts(kw.get("industry", "000000"), kw.get("measure", "quits"),
                     str(kw.get("rate", True)).lower() not in ("false", "0", "level", "no"), kw.get("seasonal", "S"))
    raise ValueError(f"unknown builder {kind!r}; use one of {BUILDERS}")


def search_catalog(words: str, limit: int = 20) -> list[dict]:
    toks = [t for t in re.findall(r"[a-z0-9-]+", words.lower()) if t]
    hits = []
    for sid, name, topic in CATALOG:
        text = f"{sid} {name} {topic}".lower()
        score = sum(1 for t in toks if t in text) + (2 if topic in toks else 0)
        if score:
            hits.append((-score, {"id": sid, "name": name, "topic": topic}))
    hits.sort(key=lambda h: h[0])
    return [h[1] for h in hits[:limit]]


def topics() -> list[str]:
    return sorted({t for _, _, t in CATALOG})


def _list(x) -> list[str]:
    if x in (None, ""):
        return []
    if isinstance(x, (list, tuple)):
        return [str(v).strip() for v in x if str(v).strip()]
    return [v.strip() for v in str(x).split(",") if v.strip()]

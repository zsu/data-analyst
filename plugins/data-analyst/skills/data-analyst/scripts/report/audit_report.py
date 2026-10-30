#!/usr/bin/env python
"""Release gate for a report or paper. Run it before handing anything to the user.

Usage: python audit_report.py <out>/report.json [--html <out>/report.html] [--online] [--rerun]

One gate for every source pack: the shared checks, plus each used source's policy
(report.json "sources", else the data/<id>/ folders; policies in sources/<id>/policy.json).
FAIL-level checks (exit code 1 if any fails):
  facts        every fact traced to a CSV cell matches that cell exactly; derived facts state a derivation;
               a fact's source pack ("pack") is one of the sources used
  numbers      no number is typed into the prose by hand: each must be a {{fact}}, a year, or listed in
               report.json "audit_allow" with a reason (e.g. {"value": "25", "reason": "age bound"})
  exhibits     every exhibit CSV exists and has the columns its chart uses; every exhibit has a source line
               that names one of the sources used (agency, title or id)
  links        every relative link resolves and none points at data a source's terms keep local; with
               --online every external link answers HTTP < 400 (403 and script time-outs: check in a browser)
  html         report.html exists, is newer than report.json / facts.json / exhibit CSVs, loads nothing
               external, starts with the AI disclaimer, and has no unresolved {{placeholders}} or raw
               [@cite] / [^n] markup
  vintage      every used source has a provenance file (data/<id>/request.json or the pack's own); the page
               carries the Data Vintage block; when a source records vintage information the methods or notes
               page say which vintage or release and that the data are revised; every fact registered as
               preliminary, as a projection or from a past vintage says so in the prose that cites it
  policy       every notice of every used source is on the page; each source's audit hooks pass
               (core.policy.run_audit_hooks: require_file, require_text, require_fact_prefix); a withheld
               key (redistribution.withhold, e.g. FRED copyright) has a report.json "clearance" entry with
               its basis
  secrets      no API key known to core.keys, no key-shaped value, and (for sources that echo keys) no 32-hex
               token in any output file, including the members of replication_package.zip
  style        prose meets the style guide target (style lint >= 90%)
  structure    report mode: the exhibit-chartbook parts are present (At a Glance with 3-5 findings, Notes About
               This Report, Introduction, 2+ sections that open with a summary and show exhibits, narrative
               text for every exhibit, About This Document)
  questions    the method record (decisions.json merged with report.json; rules in method_audit.py): one
  literature   "Don't know" option per question and no "Skip"; every unanswered question resolved; the
  references   literature searched; references opened (a page fetch.py --cite saved, its quote on it), with URL
               and accessed date; each assumption's basis
  assumptions  (empirical > literature > precedent > user > judgment) backed by its evidence, judgment only
               with why_not_empirical and a row in exhibits/sensitivity.csv whose headlines are registered facts
  benchmark    exhibits/benchmark.csv compares the headline numbers with published figures (a row per
               headline=True fact)
  replication  replication_package.zip exists and holds REPLICATION.md, analysis.py, facts.json, the exhibit
               data and lib/; per source: allowed data with its provenance file, definition_only sources a
               reproduce/<id>/REPRODUCE.md, a .sh and a .ps1 download script, and no microdata, no file a
               source's terms or hooks forbid, no row
               of a withheld key; with --rerun, analysis.py (through lib/run_analysis.py) is run again and must
               reproduce facts.json and the exhibit CSVs exactly
The style score is printed here only; it is never written into the report.
"""
from __future__ import annotations

import argparse
import csv
import html as htmlmod
import io
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
for _p in (os.path.join(SCRIPTS, "analysis"), SCRIPTS, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from core import keys as K  # noqa: E402
from core import policy as pol  # noqa: E402
import method_audit as MA  # noqa: E402
import policyset as PS  # noqa: E402
import scan_secrets  # noqa: E402
import style_check  # noqa: E402

FACT_RE = re.compile(r"\{\{[^}]*\}\}")
NUM_RE = re.compile(r"(?<![\w.])[-−]?\$?\d[\d,]*(?:\.\d+)?%?")
PROSE_KEYS = ("subtitle", "summary", "sections", "methods", "footnotes", "about", "highlights", "exhibits",
              "at_a_glance", "notes_page", "introduction", "appendixes", "cover", "assumptions")
MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
          r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
UA = "data-analyst-audit/1.0"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) data-analyst-audit"
CHECKS = ("facts", "numbers", "exhibits", "links", "html", "vintage", "policy", "secrets", "style", "structure",
          "questions", "literature", "disclosures", "references", "assumptions", "data_needs", "benchmark",
          "replication")


def prose_strings(R):
    """Yield (where, text) for every human-readable string in the report (not data paths/charts)."""
    def walk(where, x):
        if isinstance(x, str):
            yield where, x
        elif isinstance(x, list):
            for i, v in enumerate(x):
                yield from walk(f"{where}[{i}]", v)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k in ("data", "chart", "path", "exhibits") and where.startswith("sections"):
                    continue
                if k == "exhibit" and where.startswith("cover"):
                    continue
                if k in ("data", "chart", "path", "source", "rows"):
                    continue
                yield from walk(f"{where}.{k}", v)
    for key in PROSE_KEYS:
        if key in R:
            yield from walk(key, R[key])
    if "title" in R:
        yield "title", R["title"]


def find_urls(s: str) -> list[str]:
    """URLs in text; balanced parentheses stay in ("...RA%20(GW).aspx"), a closing one after the URL does not."""
    return re.findall(r"https?://(?:[^\s\"'<>()\[\]\\]|\([^\s\"'<>()]*\))+", s)


def exempt(text: str) -> str:
    """The text with what is not a statistic blanked out: {{facts}}, code, links, footnote marks, table and
    exhibit references ("Tables 1.1.1, 1.1.2 and 2.6"), bill and law numbers (H.R. 4346, P.L. 118-31), roll
    calls and line numbers, dates in any common form, periods (FY2025, 2026Q2, Q4) and terms such as 10-year
    note or U-6. Shared by the report audit and the quick-answer audit."""
    t = FACT_RE.sub(" ", text)
    # code spans hold codes and labels (`CLASSWKR 25`, `65 or more`); a bare statistic in backticks is checked
    # a bare integer in backticks is a code (`0301`, `25`); a number with a unit, decimals or commas is checked
    t = re.sub(r"`([^`]*)`", lambda m: m.group(0).strip("`") if re.fullmatch(
        r"\s*[-−]?\$?[\d,.]+\s*(?:%|percent|percentage points?|pp|million|billion|trillion)?\s*", m.group(1))
        and not re.fullmatch(r"\s*\d+\s*", m.group(1)) else " ", t)
    t = re.sub(r"\[\^\d+\]", " ", t)                                 # footnote markers
    t = re.sub(r"\]\([^)]*\)|https?://\S+", " ", t)                  # link targets
    ref = r"\d+(?:\.\d+)*[A-Z]?"
    # "Tables 1.1.1, 1.1.2 and 2.6" / "Exhibits 2, 3, and 4" are lists; "Exhibit 2, 45 percent" is a reference
    # followed by a number to check
    t = re.sub(rf"\b(?:Exhibits|Figures|Tables|Appendixes|Boxes|Chapters|Lines|Sections)\s+{ref}"
               rf"(?:\s*(?:,?\s*(?:and|to)|[–,/-])\s*{ref})*\b", " ", t)
    t = re.sub(r"\bTables?\s+\d+(?:\.\d+)+[A-Z]?\b", " ", t)         # NIPA tables such as Table 1.1.6
    t = re.sub(r"(?i)\b\d{1,2}\s?(?:Yr|Mo|Wk)s?\b", " ", t)       # maturity labels: 10 Yr, 3 Mo
    t = re.sub(r"(?i)\b(?:Exhibit|Figure|Table|Appendix|Box|Chapter|Part|Title|Volume|Vol\.|Note|FAQ)\s+\d+[a-z]?"
               r"(?:\s*(?:[–-]|to)\s*\d+[a-z]?)?\b", " ", t)
    t = re.sub(r"\b(?:Executive [Oo]rders?|EOs?|E\.\s?O\.|Proclamations?|Public Land Orders?)\s+(?:No\.\s*)?"
               r"\d{3,6}(?:\s*(?:[–-]|to|through|,?\s*and|,?\s*or|,)\s*\d{3,6})*\b", " ", t)   # EO 14296
    # survey variable codes: RELATE 1, OWNERSHP in (1, 2), GQ = 2 (two-letter names need the operator); REPWT1-80
    t = re.sub(r"\b[A-Z]{2,}\d+\s*[-–]\s*\d+\b", " ", t)
    t = re.sub(r"\b(?:(?!(?:USA|GDP|GNP|CPI|PCE|PPI|ECI|EITC|ACS|CPS|BLS|BEA|OPM|FRED)\b)[A-Z][A-Z0-9_]{2,15}"
               r"(?:\s*==?\s*|\s+(?:in|is)\s+|\s+)|[A-Z]{2}(?:\s*==?\s*|\s+in\s+))\(?\d{1,3}(?:\s*(?:,|,?\s+(?:or|and))\s+\d{1,3})*\)?"
               r"(?![\d%/]|\.\d|[,–-]\d|\s*(?:%|percent|points?|million|billion|thousand))", " ", t)
    t = re.sub(r"(?i)\b(?:versions?|vintages?|editions?|waves?|releases?)\s+\d+(?:\s*(?:,|,?\s*(?:and|or))\s*\d+)*\b"
               r"(?!\s*(?:%|percent))", " ", t)   # version 1; file versions 4, 4 and 3
    t = re.sub(r"(?i)\bcongress(?:es)?\s*(?:=\s*)?\d{2,3}\b(?!\s*(?:%|percent))", " ", t)       # congress 118
    t = re.sub(r"(?i)\b(?:foot)?notes?\s+\d+[a-z]?(?:\s*(?:,|and|to|–|-)\s*\d+[a-z]?)*\b", " ", t)  # footnote 5
    t = re.sub(r"(?i)(?:=|equals?)\s*100\b(?!\s*(?:%|percent))", " ", t)   # an index base: (U.S. = 100), 2017=100
    # a confidence level is a convention, not a statistic: "90 percent margin of error", "significant at 90%"
    t = re.sub(r"(?i)\b(?:90|95|99)\s*(?:%|percent)(?=\s+(?:confidence|margins?|level|MOE|CI)\b)|"
               r"\b(?:significant|confidence)\s+(?:at|level\s+of)\s+(?:the\s+)?(?:90|95|99)\s*(?:%|percent)", " ", t)
    t = re.sub(r"(?i)\bin\s+sample\s+\d+(?:\s*(?:,|and|or)\s*\d+)*", " ", t)    # CPS months in sample 4 and 8
    t = re.sub(r"(?i)\b(?:IPUMS\s+\w+\s+)?extract\s+(?:no\.\s*|number\s+|#)?\d+\b", " ", t)   # IPUMS extract 30
    # ages define a universe: "aged 16", "ages 25 to 64", "65 and older", "children under 15 who"
    t = re.sub(r"(?i)\b(?:aged?|ages)\s+\d{1,3}(?:\s*(?:to|through|[–-]|and)\s*\d{1,3})?\b(?!\s*(?:%|percent|\.\d))", " ", t)
    t = re.sub(r"(?i)\b\d{1,3}\s+(?:years?\s+)?(?:(?:and|or)\s+(?:over|older|up|above)|or\s+more\b(?=\s*(?:years|[,.;)]|$)))",
               " ", t)
    t = re.sub(r"(?i)\b(?:age[sd]?\s+|adults\s+|people\s+|workers\s+)\d{1,3}\+", " ", t)    # "adults 25+"
    t = re.sub(r"(?i)\b(?:ch|chap|sec|art|para|pt|vol|no|pp?)\.\s*\d+(?:\.\d+)*(?:\s*[–-]\s*\d+)?\b", " ", t)
    t = re.sub(r"(?i)\bpages?\s+\d+(?:\s*(?:[–-]|to|and)\s*\d+)?(?!\s*(?:%|percent))\b", " ", t)   # page 9
    t = re.sub(r"\b[A-Z]{2,6}-\d{2}-[0-9A-Z]{1,8}\b", " ", t)                   # GAO-26-900644, USDL-26-0229       # "ch. 13", "sec. 4.2"
    t = re.sub(r"(?<![\d.])(?:1\.645|1\.96|2\.576)(?![\d])", " ", t)          # z critical values (90/95/99 percent)
    t = re.sub(r"(?i)\b(?:under|younger than|older than)\s+(?:age\s+)?\d{1,2}(?=\s+(?:years?\b|year-olds?\b|who\b|in\b|and\b|or\b)|\s*[(),;:]"
               r"|\s*\.(?!\d)|\s*$)", " ", t)
    t = re.sub(r"(?i)\b(?:at|those|workers|people|persons|adults|men|women|group)\s+\d{1,2}\s*(?:to|through|[–-])\s*\d{2}\b"
               r"(?!\s*(?:%|percent|\.\d))|\b\d{2}-to-\d{2}\b", " ", t)       # "5.7 percent at 25 to 64"
    # "youngest child is 6 to 17", "a child under 18", "children aged 6-17": the group's ages
    t = re.sub(r"(?i)\b(?:child|children|kids?|son|daughter)\s+(?:is\s+|are\s+|was\s+|aged?\s+)?"
               r"(?:(?:under|over|younger than|older than)\s+)?\d{1,2}(?:\s*(?:to|through|[–-])\s*\d{1,2})?\b"
               r"(?!\s*(?:%|percent|\.\d))", " ", t)
    t = re.sub(r"(?i)\b(?:employees|workers|people|persons|adults|children|staff|residents|individuals|those)\s+"
               r"(?:under|over|younger than|older than)\s+(?:age\s+)?\d{1,2}\b(?!\s*(?:%|percent|\.\d))", " ", t)
    t = re.sub(r"(?i)\b(?:Parts|Titles|Volumes|Vols\.)\s+\d+(?:\s*(?:[–-]|to|through|,?\s*and|,?\s*or|,)\s*\d+)*\b",
               " ", t)
    # statute and regulation citations: 2 U.S.C. 653, 42 USC § 1983, 40 CFR 60.5, 138 Stat. 2687, 89 FR 12345
    t = re.sub(r"\b\d+\s+(?:U\.?\s?S\.?\s?C\.?(?:A\.?)?|C\.?\s?F\.?\s?R\.?|Stat\.|F\.?\s?R\.?|Fed\.\s?Reg\.)\s*"
               r"(?:§+\s*|part\s+|pt\.\s*)?\d[\w.\-()]*(?:\s*(?:,|,?\s*(?:and|or|through|to))\s*\d[\w.\-()]*(?!\w)"
               r"(?!\s*(?:U\.?\s?S\.?\s?C|C\.?\s?F\.?\s?R|Stat\.|F\.?\s?R\b|Fed\.|%|percent|points?|million|billion|"
               r"thousand|(?!(?:and|or|through|to)\b)[a-z]{3,})))*", " ", t)   # not the next cite's title, nor a statistic ("and 12 percent")
    t = re.sub(r"\b\d{1,4}(?:\([a-zA-Z0-9]{1,4}\))+", " ", t)      # provisions: INA 236(c), 241(a)(2)
    t = re.sub(r"(?i)\b(?:status\s+|error\s+)?codes?\s+\d[\d\-.]*(?:\s*\([^)\n]{0,60}\))?"
               r"(?:\s*(?:,|and|or)\s*\d[\d\-.]*(?:\s*\([^)\n]{0,60}\))?)*", " ", t)   # code 2 (...), 4 (...) or 5
    t = re.sub(r"(?i)\b(?:issues?|volume)\s+(?:no\.\s*)?\d+(?:\s*(?:[–-]|to|through|and)\s*\d+)?\b"
               r"(?!\s*(?:%|percent))", " ", t)                     # Congressional Record volume 171, issues 204 to 206
    t = re.sub(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\s*(?:[–-]|to|through|"
               r"and)\s*\d{1,2}(?=,?\s+\d{4}\b)", " ", t)              # February 12 to 13, 2024
    t = re.sub(r"(?i)\b(?:returns?|returned|gives?|gave|got|answers?|answered)\s+(?:an?\s+)?(?:http\s+)?(?:status\s+)?"
               r"(?:200|301|302|400|401|403|404|406|410|429|500|502|503|504)\b(?!\s*(?:%|percent|rows?|records?))",
               " ", t)                                                  # "BUDGET-2026-PER returns 404"
    t = re.sub(r"(?i)\b(?:roll(?:\s+calls?)?(?:\s+votes?)?|votes?)\s+(?:nos?\.?\s*)?\d+(?:\s*(?:,|and|or|&)\s*\d+)+\b",
               " ", t)                                                  # "roll call votes 225 and 600"
    t = re.sub(r"(?i)\b(?:line|section|sec\.|§|session|roll(?: call)?(?: vote)?|vote|no\.|http(?: error)?|error code|"
               r"status code|function)"
               r"\s*\d[\d.\-/]*", " ", t)                            # line 32, roll 404, HTTP 403, session 2
    t = re.sub(r"(?i)\b(?:roll(?:\s+calls?)?(?:\s+votes?)?|votes?|lines|sections)\s+(?:nos?\.?\s*)?\d+\s*(?:to|through|[–-])\s*"
               r"\d+\b(?!\s*(?:%|percent))", " ", t)                 # "votes 1 to 659": a range of numbers
    # bill and law numbers, ranges too (H.R. 1-10,564). Case-sensitive, and a lone "S" needs its period and no
    # letter or dot before it: "Somerset's 10.7" and "U.S. 4.1" are statistics, "S. 5" is a bill
    t = re.sub(r"(?<![\w.'’])(?:H\.\s?R\.?|HR|S\.|H\.\s?J\.\s?Res\.?|S\.\s?J\.\s?Res\.?|H\.\s?Con\.\s?Res\.?|"
               r"S\.\s?Con\.\s?Res\.?|H\.\s?Res\.?|S\.\s?Res\.?|P\.\s?L\.?|PL|Pub\.\s?L\.?|Public Law|Private Law|S\.\s?Amdt\.?|H\.\s?Amdt\.?|S\.\s?Up\.\s?Amdt\.?|SA|"
               r"H\.\s?Rept\.?|S\.\s?Rept\.?)\s*\d[\d,]*(?:\s*[–-]\s*\d[\d,]*)?"
               r"(?:(?:,\s*|,?\s+(?:and|or)\s+|(?<=,)\s+)\d[\d,]*(?:[–-]\d+)?(?![\d.])"   # S.Res. 377, 412 and 532;
                                                                                      # S. Rept. 117-39, 117-63
               r"(?!\s*(?:%|percent|other|more|bills?|laws?|votes?|members?|senators?|times)\b))*", " ", t)
    t = re.sub(r"(?i)\bFIPS\s*\d+|\(\d{5}\)", " ", t)              # geography codes: FIPS 24031, (24510)
    t = re.sub(r"\b[A-Z]{1,2}\s?\d+\.[A-Z][A-Za-z]{0,4}\s?\d+(?:/\d+)?(?::[\w./-]*)?", " ", t)  # SuDoc: Y 4.J 89/2:
    t = re.sub(r"(?i)\b9/11\b|\bpost-9/11\b", " ", t)
    t = re.sub(r"\b\d{4}(?:[-–]\d{2,4})?\s*=\s*100\b", " ", t)     # index bases: 1982-84=100, 2017=100
    t = re.sub(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", t)   # ISO and m/d/y dates
    t = re.sub(r"(?i)(?<!from )(?<!rose )(?<!fell )\b(?:about|roughly|approximately|between|some|around)?\s*"
               r"\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?(?=\s*(?:%|percent)\s+(?:range|of\s+(?:the|all|total)|target|"
               r"guidance|recommended|band))", " ", t)                      # a guideline range: 15 to 20 percent range
    t = re.sub(r"(?i)\b(?:about|roughly|approximately|between)\s+\d+(?:\.\d+)?\s+(?:to|and)\s+\d+(?:\.\d+)?"
               r"(?=\s*(?:%|percent)?)", " ", t)                           # an approximate range: about 45 to 48
    t = re.sub(r"\b(?:17|18|19|20)\d\d\s*–\s*\d{2,4}\b", " ", t)    # year ranges with a dash: 1982–84, 2015–2025
    t = re.sub(r"(?i)(\d\.\d+)-(day|week|month|quarter|year)s?\b", r"\1 \2", t)   # a 1.6-year rise is a statistic
    t = re.sub(r"(?i)(?<![\d.])\b(?:1|2|3|4|5|6|7|8|9|10|12|13|14|15|17|18|20|21|24|25|26|28|30|31|36|40|45|"
               r"50|52|60|75|90|100|120|180|270|360|365)-(?:day|week|month|quarter|year)s?\b", " ", t)
    # standard terms and windows (4-week bill, 10-year note, 30-day average); "43-day shutdown" is a statistic
    t = re.sub(r"(?i)\b(?:1|3|6|12|24|36)[ -]months?\b(?!\s+(?:earlier|later|before|after|ahead|ago|lag|"
               r"lead|behind)\b)", " ", t)     # standard change windows; "the yield 8 months earlier" is a lag to check
    t = re.sub(r"\bU-[1-6]\b|\b\d{4}(?:-\d{2,4})?\s*=\s*100\b|\b[MQS]\d{2}\b|\bQ[1-4]\b", " ", t)
    t = re.sub(r"\b(?:19|20)\d\d\s?[QM]\d{1,2}\b", " ", t)           # periods such as 2026Q2 or 2026M09
    t = re.sub(r"\b[A-Za-z]+-\d+\b", " ", t)                         # names with a number: COVID-19, H-1
    t = re.sub(r"\b\d{1,2}\s+" + MONTHS + r"\b", " ", t)              # "30 September 2026"
    # "March 31, 2026", "October 1-3", "January 17 and 21, 2025", "January 9, 13, 17 and 20, 2025"
    # a comma list of days is dates only when it closes with "and 20" or a year ("On March 3, 12 states" is not)
    t = re.sub(MONTHS + r"\.?\s+\d{1,2}(?:\s*,\s*\d{1,2}\b)+(?:\s*,?\s*(?:and|or|&)\s*\d{1,2}\b(?:,?\s+\d{4}\b)?|"
               r",?\s+\d{4}\b)", " ", t)
    t = re.sub(MONTHS + r"\.?\s+\d{1,2}(?:\s*(?:[–-]|and|&|or)\s*\d{1,2}\b(?!\.\d))*\b,?", " ", t)
    # FY2024-25, FY2020 to FY2026, fiscal years 2024 and 2025; the second part must look like a year, so
    # "FY2024 and 146,482" keeps its number
    t = re.sub(r"(?i)\b(?:fiscal years?|FY)\s*\d{2,4}(?:\s*[–-]\s*(?:FY\s?)?(?:(?:19|20)\d\d|\d\d)\b(?![,.]\d)"
               r"|\s*(?:to|and|through)\s*(?:FY\s?)?(?:19|20)\d\d\b(?![,.]\d))?", " ", t)
    t = re.sub(r"\b(?:FY|Q)\s?\d{1,4}\b", " ", t)                    # FY2025, FY 2025, Q4
    t = re.sub(r"(?m)^\s*\d{1,2}[.)]\s", " ", t)                      # list markers "1. " "2) "
    t = re.sub(r"(?im)^(\s*\|\s*(?:\*\*)?(?:total|all)\b.*)$",            # a share table's total row: 100 percent
               lambda m: re.sub(r"\b100(?:\.0+)?\s*(?:%|percent)", " ", m.group(1)), t)
    # a table's first column is its labels ("35 to 44", "Under 35", "2019 Q1"), when the row has more cells
    t = re.sub(r"(?m)^(\s*\|)([^|\n]*)(?=\|[^\n]*\|)", lambda m: m.group(1) + re.sub(r"\d", " ", m.group(2)), t)
    t = re.sub(r"(?m)^\s*\|\s*\d+\s*(?:\(tie\)|=)?\s*\|", "| |", t)    # a rank or code in a table's first column
    t = re.sub(r"(?<![\d.,])0\d+\b(?![.,]\d)", " ", t)              # zero-padded codes: series 0301, FIPS 06037
    t = re.sub(r"(?i)\b(?:series|codes?|occupation|naics|soc|cip|sic|psc|nigp)\s+\(?\d{3,}[\d\-.]*\)?(?!\s*(?:%|percent))"
               r"|\b(?:series|codes?|occupation|naics|soc|cip|sic|psc|nigp)\s+\d[\d\-.]*"
               r"(?:\s*\([^)\n]{0,60}\))?(?:\s*(?:,|and|or)\s*\d[\d\-.]*(?:\s*\([^)\n]{0,60}\))?)*", " ", t)  # code 2 (...), 4 or 5
    t = re.sub(r"(?i)\b(?:90|95|99)\s*(?:%|percent)(?=[\s-]*(?:confidence|margin|moe|ci\b|interval|level|signific))",
               " ", t)                                               # 90 percent margin of error
    t = re.sub(r"\b\d+(?:st|nd|rd|th)\b", " ", t)                    # ordinals: 118th Congress
    return t


def hard_numbers(text: str, allow: set[str]) -> list[str]:
    """Numbers typed by hand (exempted: years, references, dates, periods)."""
    t = exempt(text)
    bad = []
    for m in NUM_RE.finditer(t):
        tok = m.group(0)
        core = tok.replace("$", "").replace("%", "").replace(",", "").replace("−", "-").lstrip("-")
        try:
            v = float(core)
        except ValueError:
            continue
        if tok.rstrip(",") == core and core.isdigit() and len(core) == 4 and 1900 <= v <= 2100:
            continue                                   # a year ("2025," too) but not $2,000, 1,999 or 2100%
        if tok in allow or core in allow:
            continue
        bad.append(tok)
    return bad


def structure_problems(R: dict) -> list[str]:
    """Missing or malformed parts of the exhibit-chartbook layout (report mode)."""
    out = []
    g = R.get("at_a_glance") or {}
    findings = g.get("findings") or R.get("summary") or []
    if not g.get("text"):
        out.append("at_a_glance.text: add the framing paragraph (what was examined, population or measures, period)")
    if not 3 <= len(findings) <= 5:
        out.append(f"At a Glance has {len(findings)} findings; a chartbook uses 3 to 5")
    if any(not (isinstance(f, dict) and f.get("lead")) for f in findings):
        out.append("each At a Glance finding needs a bold 'lead' naming its section, e.g. 'Unemployment.'")
    if not R.get("notes_page"):
        out.append("notes_page missing: calendar or fiscal years, rounding, seasonal adjustment, dollar year and "
                   "deflator, data sources, retrieval dates and vintages, definitions")
    intro = R.get("introduction") or {}
    if not intro.get("text"):
        out.append("introduction.text missing: a lead paragraph with the headline numbers")
    if not intro.get("subsections"):
        out.append("introduction.subsections missing: one question-style heading per section, plus the data used")
    secs = R.get("sections") or []
    if len(secs) < 2:
        out.append(f"{len(secs)} section(s); a chartbook has 2 to 5 thematic sections")
    for k, s in enumerate(secs, start=1):
        if not s.get("exhibits"):
            out.append(f"section {k} ({s.get('heading')!r}) shows no exhibit")
    for i, e in enumerate(R.get("exhibits", []), start=1):
        if not str(e.get("text", "")).strip():
            out.append(f"exhibit {i}: add 'text', the 2 to 4 paragraphs shown beside the chart")
        if not re.search(r"(?:19|20)\d\d(?:\s*Q[1-4])?\s*$", str(e.get("title", ""))):    # a warning only
            print(f"note: exhibit {i} title should end with its period, e.g. ', 2013 to 2023' or ', 2019 Q1 to 2026 Q2'")
    if not R.get("about"):
        out.append("about missing: About This Document (who asked, who prepared it, with what data and tools, date)")
    return out


def all_strings(x):                           # scan the strings themselves: json.dumps would glue "\n" to a URL
    if isinstance(x, str):
        yield x
    elif isinstance(x, list):
        for v in x:
            yield from all_strings(v)
    elif isinstance(x, dict):
        for v in x.values():
            yield from all_strings(v)


def check_url(u: str) -> tuple[bool, str]:
    status, err, codes = None, None, []
    for ua in (UA, BROWSER_UA):              # some hosts refuse a browser-like agent, others anything else
        try:
            req = urllib.request.Request(u, method="GET", headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=20) as r:
                status = r.status
            if status < 400:
                return True, f"{u}: HTTP {status}"
        except Exception as ex:  # noqa: BLE001
            status, err = getattr(ex, "code", None), ex
        if status is not None:
            codes.append(status)
    if any(c not in (401, 403, 429) and c >= 400 for c in codes):    # a real answer (404, 410, 500) beats a timeout
        bad = next(c for c in codes if c not in (401, 403, 429) and c >= 400)
        return False, f"{u}: HTTP {bad}"
    timed_out = status is None and "timed out" in str(err).lower()
    dropped = status is None and re.search(r"(?i)reset|10054|remote end closed|remotedisconnected|eof occurred", str(err))
    note = ("HTTP 403 to scripts" if status == 403 else "timed out for scripts" if timed_out
            else "connection dropped for scripts (throttling)" if dropped else None)
    return bool(note), f"{u}: {note + '; open it in a browser to confirm' if note else err or status}"


def names_source(text: str, policies: dict) -> bool:
    t = text.lower()
    for sid, P in policies.items():
        cands = [P.get("agency", ""), P.get("title", ""), *(P.get("aliases") or [])]
        if any(c and c.lower() in t for c in cands) or re.search(rf"(?<![a-z0-9]){re.escape(sid.lower())}(?![a-z0-9])", t):
            return True
        short = re.sub(r"\s*\(.*?\)", "", P.get("title", "")).strip().lower()
        if short and short in t:
            return True
    return False


def audit(report: str, html_path: str | None = None, online: bool = False, rerun: bool = False) -> list[tuple]:
    base = os.path.dirname(os.path.abspath(report))
    R = MA.merged(base, json.load(open(report, encoding="utf-8")))
    mode = R.get("mode", "report")
    html_path = html_path or os.path.join(base, "report.html")
    results: list[tuple] = []

    def res(check, ok, msg):
        results.append((check, bool(ok), msg))

    # ---- sources and their policies
    ids = PS.used_sources(base, R)
    try:
        policies = PS.load_policies(ids)
    except PS.SourceError as e:
        res("policy", False, str(e))
        policies = {}
    if not ids:
        res("policy", False, "no source used: list report.json \"sources\" or fetch into data/<id>/")
    page = open(html_path, encoding="utf-8").read() if os.path.exists(html_path) else ""
    page_text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", page))
    page_text = re.sub(r"\s+", " ", page_text)

    # ---- facts
    fpath = os.path.join(base, R.get("facts", "facts.json"))
    facts = json.load(open(fpath, encoding="utf-8")) if os.path.exists(fpath) else {}
    cited = set(re.findall(r"\{\{\s*([A-Za-z_]\w*)", json.dumps(R)))
    if cited and not facts:
        res("facts", False, f"report cites {len(cited)} facts but {fpath} is missing")
    for name in sorted(cited):
        f = facts.get(name)
        if f is None:
            res("facts", False, f"{{{{{name}}}}} is cited but not registered in facts.json")
            continue
        pk = f.get("pack")
        pks = [pk] if isinstance(pk, str) else list(pk or [])
        unknown = [p for p in pks if p not in policies]
        if unknown and policies:
            res("facts", False, f"{name}: source pack {unknown} is not one of the sources used ({ids})")
        res("facts", *MA.check_fact(name, f, base))
    if not cited:
        res("facts", False, "the prose cites no {{facts}}; every number in the text must come from facts.json")

    # ---- hand-typed numbers
    allow = {str(x["value"]) if isinstance(x, dict) else str(x) for x in R.get("audit_allow", [])}
    found = {}
    for where, text in prose_strings(R):
        for tok in hard_numbers(text, allow):
            found.setdefault(tok, []).append(where)
    for tok, wh in sorted(found.items()):
        res("numbers", False, f"hand-typed number '{tok}' in {', '.join(sorted(set(wh)))[:120]} "
                              f"-> make it a {{{{fact}}}} or add it to audit_allow with a reason")
    if not found:
        res("numbers", True, "every number in the prose is a registered fact, a year, or allowed")

    # ---- exhibits
    for i, e in enumerate(R.get("exhibits", []), start=1):
        if not e.get("source"):                       # every exhibit, including ones with inline rows
            res("exhibits", False, f"exhibit {i}: no source line")
        elif policies and not names_source(e["source"], policies):
            res("exhibits", False, f"exhibit {i}: the source line names none of the sources used "
                                   f"({', '.join(P['agency'] for P in policies.values())})")
        if "data" not in e:
            continue
        p = os.path.join(base, e["data"])
        if not os.path.exists(p):
            res("exhibits", False, f"exhibit {i}: {e['data']} missing")
            continue
        with open(p, encoding="utf-8-sig", newline="") as fh:
            cols = set(next(csv.reader(fh), []))
        c = e.get("chart") or {}
        need = [c.get("x"), c.get("series"), *(c.get("y") if isinstance(c.get("y"), list) else [c.get("y")]),
                *(c.get("filter") or {}).keys(), *(c.get("columns") or [])]
        miss = [n for n in need if n and n not in cols]
        res("exhibits", not miss, f"exhibit {i}: missing columns {miss}" if miss else f"exhibit {i}: ok")
    lin_path = os.path.join(base, "exhibits", "lineage.json")
    if os.path.exists(lin_path) and policies:
        lin = json.load(open(lin_path, encoding="utf-8"))
        for ex, pks in lin.items():
            bad = [p for p in pks if p not in policies]
            if bad:
                res("exhibits", False, f"{ex}: lineage names {bad}, which are not sources of this report")

    # ---- links
    withheld = {sid: PS.withheld_keys(base, sid, P, R) for sid, P in policies.items()}
    for d in R.get("data_files", []):
        ok = os.path.exists(os.path.join(base, d["path"]))
        res("links", ok, f"{d['path']}: {'exists' if ok else 'MISSING'}")
        dd = PS.decide(d["path"], base, policies, R, withheld)
        if not dd["ok"] and dd["policy"]:
            res("links", False, f"{d['path']}: linked, but {dd['reason']}")
    if page:
        hrefs = re.findall(r"href=['\"]([^'\"#]+)['\"]", page)
        local = sorted({h for h in hrefs if not re.match(r"[a-z]+:", h)})
        out_dir = os.path.dirname(os.path.abspath(html_path))
        for h in local:
            relp = os.path.relpath(os.path.join(out_dir, h), base).replace("\\", "/")
            dd = PS.decide(relp, base, policies, R, withheld)
            if not dd["ok"] and dd["policy"]:
                res("links", False, f"report.html links {relp}: {dd['reason']}")
            elif not os.path.exists(os.path.join(out_dir, h)):
                res("links", False, f"report.html links {h}, which does not exist")
    urls = sorted({u.rstrip(".,;:") for s in all_strings(R) for u in find_urls(s)})
    for u in urls:
        if not online:
            res("links", True, f"{u}: not requested (use --online to verify)")
        else:
            res("links", *check_url(u))

    # ---- html
    if not page:
        res("html", False, f"{html_path} does not exist; run build_report.py")
    else:
        dpath = os.path.join(base, "decisions.json")
        newest = max([os.path.getmtime(report)] + [os.path.getmtime(p) for p in (fpath, dpath) if os.path.exists(p)]
                     + [os.path.getmtime(os.path.join(base, e["data"])) for e in R.get("exhibits", [])
                        if e.get("data") and os.path.exists(os.path.join(base, e["data"]))])
        up = os.path.getmtime(html_path) >= newest
        res("html", up, "report.html is up to date" if up else "report.html is older than its inputs; rebuild it")
        ext = re.findall(r'(?:\bsrc\s*=\s*["\']|<link[^>]+href\s*=\s*["\']|@import\s+|url\(\s*["\']?)(?:https?:)?//', page, re.I)
        res("html", not ext, "no external resources" if not ext else f"{len(ext)} external resource loads")
        first = page.find('class="disclaimer"')
        res("html", first != -1 and first < page.find('class="masthead'), "AI disclaimer at top of page")
        visible = re.sub(r"<blockquote>.*?</blockquote>|<pre\b.*?</pre>|<script\b.*?</script>", "", page, flags=re.S)
        k = visible.find("{{")
        res("html", k == -1, "no unresolved {{placeholders}}" if k == -1 else
            f"unresolved placeholder near: {visible[max(0, k - 60):k + 40]!r}")
        left = re.findall(r"\[@[^\]]*\]|\[\^\d+\]", visible)
        res("html", not left, "no raw citation or footnote markup" if not left else
            f"raw markup left in the page: {sorted(set(left))[:5]}; use [@id] or [@a; @b] and [^n] with matching entries")

    # ---- vintage
    any_vintage = False
    for sid in policies:
        rel, P = PS.provenance(base, sid, R)
        res("vintage", bool(rel), f"{sid}: provenance {rel}" if rel else
            f"{sid}: no provenance file in data/{sid}/ (request.json): the retrieval date cannot be stated; fetch with "
            "fetch.py --source " + sid)
        any_vintage = any_vintage or bool(PS.vintage_fields(P))
    if page and policies:
        res("vintage", "Data Vintage" in page, "Data Vintage block present" if "Data Vintage" in page else
            "report.html lacks the Data Vintage block; rebuild with build_report.py")
    if any_vintage:
        methods = json.dumps(R.get("methods", "")) + json.dumps(R.get("notes_page", ""))
        ok = bool(re.search(r"(?i)vintage|released|release of|as of|revis", methods))
        res("vintage", ok, "methods state the vintage and revisions" if ok else
            "a source records vintage information: the methods or notes page must say which release (vintage) the "
            "data come from and that the source revises them")
    prose = {}
    for w, s in prose_strings(R):          # a tile, finding or exhibit is one unit: its parts are read together
        unit = re.sub(r"\.(?:label|value|note|lead|text|title|subtitle|notes)$", "", w)
        prose[unit] = (prose.get(unit, "") + " " + s).strip()
    labels = {"preliminary": (r"preliminar|subject to revision|advance estimate|revis",
                              "say so (\"preliminary\", \"advance estimate\", \"subject to revision\")"),
              "projection": (r"project|forecast|estimate[sd]? for|expect",
                             "say it is a projection (\"projected\", \"the agency projects\")"),
              "vintage": (r"as (?:first )?(?:reported|published|estimated)|first (?:release|estimate|reported)|"
                          r"initial(?:ly)?|vintage|real[- ]time|at the time|originally",
                          "say which vintage (\"as first reported\", \"as published in July 2020\")")}
    for name in sorted(cited):
        f = facts.get(name) or {}
        hits = [s for s in prose.values() if re.search(r"\{\{\s*" + re.escape(name) + r"\b", s)]
        for flag, (pat, fix) in labels.items():
            if f.get(flag):
                ok = all(re.search(pat, s, re.I) for s in hits)
                res("vintage", ok, f"{name}: {flag} value is labeled as such" if ok else
                    f"{name} is a {flag} value: {fix} where it is cited")

    # ---- policy: notices, audit hooks, clearances
    for n in pol.notices(list(policies.values())):
        norm = re.sub(r"\s+", " ", n).strip()
        ok = norm in page_text
        res("policy", ok, f"notice present: {norm[:60]}" if ok else
            f"required notice missing from report.html: {norm[:100]!r}; rebuild with build_report.py")
    for sid, P in policies.items():
        for status, hid, msg in pol.run_audit_hooks(P, base, R, facts, html=page_text):
            res("policy", status == "PASS", f"{sid} hook {hid}: {msg}")
        w, rows = PS.withhold_rows(base, sid, P)
        if w:
            clear = PS.clearances(R)
            for r in {str(x.get(w.get("key", ""), "")): x for x in rows}.values():
                k = str(r.get(w.get("key", ""), ""))
                c = clear.get((sid, k.upper())) or clear.get(("", k.upper()))
                ok = bool(c and str(c.get("basis", "")).strip())
                res("policy", ok, f"{sid} {k}: {r.get(w.get('column'))}, cleared ({c.get('basis')})" if ok else
                    f"{sid} {k}: {w.get('column')} {r.get(w.get('column'))}; read the terms for it or ask the user, then "
                    "add report.json \"clearance\": {\"source\", \"key\", \"basis\", \"redistribute\": true|false}")

    # ---- secrets
    used_echo = scan_secrets.echo_sources(policies)
    hits, _ = scan_secrets.scan_tree([base], scan_secrets.known_keys(), hex32=bool(used_echo))
    res("secrets", not hits, "no API key or credential in any output file" if not hits else "; ".join(hits[:10]))

    # ---- style
    score = style_check.score(json.loads(FACT_RE.sub("X", json.dumps(R))))[0]
    res("style", score >= style_check.TARGET,
        f"style guide {score:.0f}% (target {style_check.TARGET}%); run style_check.py for details")

    # ---- structure (report mode: exhibit chartbook)
    if mode == "report":
        for problem in structure_problems(R):
            res("structure", False, problem)
        res("structure", True, "chartbook parts checked")

    # ---- method record: questions, literature, references, assumptions; benchmarks
    results += MA.check_method(R, base, facts)
    results += MA.check_benchmark(base, facts)
    norm = lambda s: re.sub(r"\s+", " ", str(s)).strip().lower()
    for key, _ in MA.escapes(R):                 # each unverifiable claim must reach the reader
        text = (R.get("disclosures") or {}).get(key)
        if text:
            shown = norm(text) in norm(page_text)
            res("disclosures", shown, f"{key}: disclosure shown on the page" if shown else
                f"{key}: the disclosure {text!r} is not on the page: rebuild (the builder prints it in Appendix A)")

    # ---- replication package
    zp = os.path.join(base, "replication_package.zip")
    if not os.path.exists(zp):
        res("replication", False, "replication_package.zip missing; run build_report.py")
    else:
        z = zipfile.ZipFile(zp)
        names = set(z.namelist())
        need = ["REPLICATION.md", "analysis.py", "facts.json", "lib/facts.py", "lib/factfmt.py", "lib/run_analysis.py"]
        need += [x for x in ("spec.json", "plan.json") if os.path.exists(os.path.join(base, x))]
        need += [e["data"] for e in R.get("exhibits", []) if e.get("data")]
        for sid, P in policies.items():
            mode_ = P["redistribution"]["mode"]
            if mode_ == "definition_only":
                need.append(f"reproduce/{sid}/REPRODUCE.md")
                for ext in (".sh", ".ps1"):       # one-command download scripts with the request built in
                    if not any(n.startswith(f"reproduce/{sid}/") and n.endswith(ext) for n in names):
                        need.append(f"reproduce/{sid}/download_*{ext}")
            else:
                rel, _ = PS.provenance(base, sid, R)
                if rel:
                    need.append(rel)
            if os.path.isdir(PS.pack_lib(sid)) and any(f.endswith(".py") for f in os.listdir(PS.pack_lib(sid))):
                need.append(f"lib/{sid}/" + sorted(f for f in os.listdir(PS.pack_lib(sid)) if f.endswith(".py"))[0])
        miss = [n for n in need if n not in names]
        res("replication", not miss, "package complete" if not miss else f"package lacks {miss}")
        viol = []
        for n in sorted(names):
            if n == "REPLICATION.md" or n.endswith("/"):
                continue
            d = PS.decide(n, base, policies, R, withheld)
            if not d["ok"] and d["policy"]:
                viol.append(f"{n} ({d['reason']})")
        for sid, P in policies.items():                    # core's own check, on the source's own files
            scope = [n for n in names if n.startswith(f"data/{sid}/") or not n.startswith(("data/", "exhibits/", "lib/"))]
            viol += [f"{n} (core.policy.check_package: {sid})" for n in pol.check_package(P, scope)]
        res("replication", not viol, "no file the source terms forbid" if not viol else
            "package holds files the source terms forbid: " + "; ".join(sorted(set(viol))[:6]))
        for sid, P in policies.items():                    # withheld rows must not be in shared CSVs
            keys = withheld.get(sid) or set()
            w = (P["redistribution"].get("withhold") or {})
            if not keys:
                continue
            for n in names:
                if n.startswith(f"data/{sid}/") and n.endswith(".csv") and n != f"data/{sid}/{w.get('meta')}":
                    rd = csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8-sig"))
                    if w.get("key") in (rd.fieldnames or []):
                        leaked = sorted({r[w["key"]] for r in rd if r.get(w["key"]) in keys})
                        res("replication", not leaked, f"{n}: no withheld rows" if not leaked else
                            f"{n} holds rows of withheld {w['key']} {leaked[:5]}")
        z.close()
    if rerun:
        tracked = [os.path.join(base, e["data"]) for e in R.get("exhibits", []) if e.get("data")]
        results += MA.rerun(base, fpath, tracked, K.redact)
    return results


def main(argv=None) -> int:
    for _s in (sys.stdout, sys.stderr):       # Windows pipes default to cp1252; prose may hold "−" or "®"
        if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("--html")
    ap.add_argument("--online", action="store_true", help="also request every external link")
    ap.add_argument("--rerun", action="store_true", help="re-run analysis.py and compare facts.json")
    a = ap.parse_args(argv)
    results = audit(a.report, a.html, a.online, a.rerun)
    fails = [r for r in results if not r[1]]
    for check_name in CHECKS:
        rs = [r for r in results if r[0] == check_name]
        if not rs:
            continue
        bad = [r for r in rs if not r[1]]
        print(f"[{'FAIL' if bad else 'PASS'}] {check_name}: {len(rs) - len(bad)}/{len(rs)} ok")
        for r in bad:
            print(f"    - {K.redact(r[2])}")
    print("AUDIT " + ("PASSED" if not fails else f"FAILED ({len(fails)} problems)"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""Release gate for a quick answer (the default product). Run it before giving the answer.

Usage: python audit_answer.py <out> [--online] [--rerun]

Same method rules as a report (method_audit.py) on <out>/decisions.json, plus the parts of the report audit
that apply without a page. FAIL-level checks (exit code 1 if any fails):
  facts        facts.json exists; every fact traced to a CSV cell matches that cell; others state a derivation
  answer       <out>/answer.md (the reply as it will be sent): every number in it is a registered fact as rounded
               in the text, or listed in decisions.json "audit_allow" with a reason; dollar amounts of different
               years compared only with their basis stated (nominal or real), or "dollar_basis_ok" with a reason
  questions    one "Don't know" option per question, no "Skip"; skipped or unanswered = "Don't know", resolved;
               not asked only with reason_code no_user or settled_by_request (quoting the request)
  literature   the searches run (query, date, what was found), or reason_code no_search_tool / network_blocked
  disclosures  each no_user question and unavailable search has a line in "disclosures" for the answer to carry
  references   citation, URL or DOI, accessed date, opened; every [@id] resolves
  assumptions  basis empirical > literature > precedent > user > judgment with its evidence; judgment states
               why_not_empirical and has its row in exhibits/sensitivity.csv
  benchmark    exhibits/benchmark.csv compares the headline numbers with published figures: numeric pairs agree
               within the published rounding or a note explains the difference
  vintage      every source in data/<id>/ has its provenance file (route and retrieval date); every table read
               by hand from an agency page, in published/<name>.csv, gives source_url and accessed on each row
  links        with --online every external link in decisions.json answers HTTP < 400
  secrets      no API key or key-shaped value in any file of <out>
  replication  with --rerun, analysis.py runs again and reproduces facts.json and the exhibit CSVs exactly
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
for _p in (os.path.join(SCRIPTS, "analysis"), SCRIPTS, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from core import keys as K  # noqa: E402
from core import policy as pol  # noqa: E402
import audit_report as AR  # noqa: E402
import method_audit as MA  # noqa: E402
import policyset as PS  # noqa: E402
import scan_secrets  # noqa: E402

CHECKS = ("facts", "answer", "policy", "questions", "literature", "disclosures", "references", "assumptions",
          "data_needs", "benchmark", "vintage", "links", "secrets", "replication")


# ---------------------------------------------------------------------- the answer text
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12, "k": 1e3, "m": 1e6, "mn": 1e6,
          "bn": 1e9, "b": 1e9, "t": 1e12, "tn": 1e12}
ANSWER_NUM = re.compile(
    r"(?<![\w.,\-–/#@:])(?P<neg>[-−])?\$?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>[\s-]*(?:%|percent(?:age[\s-]points?)?|percentage[\s-]points?|pp\b|p\.p\.|basis[\s-]points?|bps?\b"
    r"|(?:thousand|million|billion|trillion)\b|(?:k|m|mn|bn|b|t|tn)\b))?"
    r"(?![\w/]|[-–](?!points?\b|percent|(?:days?|weeks?|months?|hours?|pages?|volumes?|records?|rows?|"
    r"people|persons|jobs|units|members|employees)\b)|\.\d|,\d)", re.I)   # "9.9-point drop"
                                                        # and "43-day shutdown" are checked; "243-187", "25-64",
                                                        # "10-yr" and "50-states" are not statistics
TALLY = re.compile(r"(?<![\w.,/$#-])(\d{1,3})\s*[-–]\s*(\d{1,3})(?![\w/%]|[.,]\d|\s*(?:percent|years?|months?))")
VOTE_WORDS = re.compile(r"(?i)\b(?:vote[sd]?|voting|roll|tally|yeas?|nays?|passed|failed|sustain\w*|overr\w*|"
                        r"adopted|confirmed|rejected|agreed|cloture|motion|margin)\b")
RANK_WORDS = re.compile(r"(?i)\b(?:top|bottom|first|last|next|past|other)\s+\d{1,2}\b"
                        # "all 50 states" names a universe; "all 79 succeeded" is a result to check
                        r"|\ball\s+\d{1,2}(?=\s+(?:[a-z-]+\s+)?(?:states?|areas?|jurisdictions?|departments?|"
                        r"agencies|counties|districts|metros?|cities|countries|sectors|industries|categories|groups|"
                        r"members|senators|committees|months|quarters|years|weeks|days|volumes|series|rows|columns|"
                        r"components|occupations|figures|values|estimates)\b)")
FLAG_WORDS = {"preliminary": r"preliminar|subject to revision|advance estimate|first estimate|early estimate|"
                             r"(?:will|to) be revised|provisional",
              "projection": r"project|forecast|estimate|expect|outlook",
              "vintage": r"as (?:first )?(?:reported|published|estimated)|first (?:release|estimate|reported)|"
                         r"initial(?:ly)?|vintage|real[- ]time|at the time|originally|first published|revis|"
                         r"(?:advance|second|third|earlier|annual|comprehensive|benchmark)\s+(?:estimates?|updates?|"
                         r"releases?|revisions?)"}


# a year qualifies these ("2017 dollars", "2025 estimates"); another plural after 1789-2100 makes it a count
YEAR_NOUNS = {"dollars", "prices", "levels", "estimates", "values", "data", "figures", "rates", "terms", "numbers",
              "quarters", "months", "years", "editions", "vintages", "releases", "budgets", "elections", "sessions",
              "laws", "acts", "amendments", "appropriations", "reports", "surveys", "tables", "files", "samples",
              "weights", "results", "rules", "standards", "projections", "baselines", "totals", "peaks", "highs",
              "lows", "averages", "volumes", "issues", "q1", "q2", "q3", "q4"}


_VERBS_S = {"does", "goes", "shows", "appears", "remains", "marks", "sees", "seems", "ends", "begins", "starts",
            "covers", "includes", "contains", "lists", "looks", "gives", "brings", "rises", "falls", "stands",
            "follows", "means", "makes", "takes", "holds", "runs", "gets", "comes", "becomes", "needs", "uses",
            "matches", "leads", "adds", "plus", "thus", "less", "across", "towards", "versus", "always", "also"}
# a bare 1789-2100 integer in prose is a count only after such a word and before a plural noun ("had 2017 pages")
_COUNT_BEFORE = re.compile(r"(?i)\b(?:had|has|have|against|totaled|totalled|totaling|totalling|were|are|was|is|"
                           r"about|nearly|almost|roughly|approximately|exactly)\s+$")
_SEP_ROW = r"\s*\|?[\s:|-]+\|?\s*"


def _year_like(t: str, m, text: str | None = None) -> bool:
    """Is this bare 1789-2100 integer a year? It is, unless it reads as a count: in prose a count word precedes
    and a plural noun follows ("had 2017 pages", not "their 2022 peaks" or "in 2026 refunds rose"); in a table it
    is a cell by itself, not in the first column or the header row, under a column that does not name a period."""
    a = t.rfind("\n", 0, m.start()) + 1
    b = t.find("\n", m.end())
    line = t[a:] if b < 0 else t[a:b]
    if line.lstrip().startswith("|"):
        cells = line.strip().strip("|").split("|")
        cell = line[:m.start() - a].count("|") - 1
        nxt = t[b + 1:].split("\n", 1)[0] if b >= 0 else ""
        if cell <= 0 or (re.fullmatch(_SEP_ROW, nxt) and "-" in nxt):
            return True                                         # first column or the header row
        orig = _source_line(text, t, m.start()).strip().strip("|").split("|") if text else cells
        if cell >= len(orig) or orig[cell].strip().strip("*").strip() != m.group(0).strip():
            return True                                         # part of a label or a date ("Jan 5, 2026")
        rows = t[:a].splitlines()
        sep = [i for i, l in enumerate(rows) if re.fullmatch(_SEP_ROW, l) and "-" in l]
        if sep and sep[-1] >= 1:
            head = [c.strip().lower() for c in rows[sep[-1] - 1].strip().strip("|").split("|")]
            col = head[cell] if cell < len(head) else ""
            return bool(re.match(r"\**\s*(?:years?|period|fy|fiscal|calendar|date|vintage|edition|session|"
                                 r"congress|month|quarter|as of|latest|release)\b", col))
        return True
    if not _COUNT_BEFORE.search(t[max(0, m.start() - 20):m.start()]):
        return True
    after = re.match(r"\s+([a-z]{4,}s)\b", t[m.end():m.end() + 40])
    return not (after and after.group(1) not in YEAR_NOUNS and after.group(1) not in _VERBS_S)


APPROX = re.compile(r"(?i)\b(?:about|nearly|almost|roughly|approximately|around|some|close to|more than|less than|over|under)\s*\$?$")


# a unit shared with the next number: "3.4 and 3.6 percent", "between 2.1 and 2.5 percent"
UNIT_AFTER = re.compile(r"(?i)(?:\s*(?:,|and|or|to|–|-)\s*(?:[A-Za-z]+\s+){0,3}[-−+]?\d[\d,.]*)*\s*(?:%|percent|percentage points?|points?|pp\b|basis points?)")


COUNT_NOUN = re.compile(r"(?i)\s+(?:of\s+(?:the\s+)?\d|(?!percent|points?|pp\b|basis|in\b|and\b|or\b|to\b|vs\b|from\b|at\b|for\b|on\b|by\b)[a-z]{3,})")


def _at(x: str, tok: str) -> int:
    """Where the number tok stands in x as a whole number (not "0.6 percent" inside "30.6 percent"); -1 if not."""
    m = re.search(r"(?<![\w.,$-])" + re.escape(tok) + r"(?![\d])", x)
    return m.start() if m else -1


def _has(x: str, tok: str) -> bool:
    return _at(x, tok) >= 0


def _source_line(text: str, t: str, pos: int) -> str:
    """The line of the original text that holds position pos of its exempted copy t (exempt keeps line breaks;
    the original keeps the first column's labels, which exempt blanks)."""
    i = t.count("\n", 0, pos)
    lines = text.split("\n")
    if text.count("\n") == t.count("\n") and i < len(lines):
        return lines[i]
    return t[t.rfind("\n", 0, pos) + 1:(t.find("\n", pos) + 1 or len(t) + 1) - 1]


def answer_numbers(text: str) -> list[dict]:
    """The statistics in an answer: {tok, num, unit ("pct", "bp", "scaled" or ""), scale, tol}. Years,
    dates, table and line references, bill and law numbers, ordinals, list markers and "top 5" are skipped."""
    t = PERIOD_COUNT.sub(" ", RANK_WORDS.sub(" ", AR.exempt(text)))
    out = []
    for m in ANSWER_NUM.finditer(t):
        num, unit = m.group("num").replace(",", ""), (m.group("unit") or "").strip(" -").lower()
        v = float(num)
        if not unit and "." not in num and "," not in m.group("num") and 1789 <= v <= 2100 and _year_like(t, m, text):
            continue                                            # a year
        kind = ("pp" if unit.startswith(("percentage", "pp", "p.p")) else "pct" if unit.startswith(("%", "percent"))
                else "bp" if unit.startswith(("basis", "bp")) else "scaled" if unit else "")
        tol = 0.5 * 10 ** -(len(num.split(".")[1]) if "." in num else 0)
        out.append({"tok": m.group(0).strip(), "num": v, "unit": kind, "scale": _SCALE.get(unit, 1.0), "tol": tol,
                    "dec": len(num.split(".")[1]) if "." in num else 0,
                    "neg": bool(m.group("neg")), "dollar": "$" in m.group(0),
                    "plus": not m.group("neg") and t[max(0, m.start() - 1):m.start()] == "+",
                    "line": _source_line(text, t, m.start()),
                    "approx": bool(APPROX.search(t[max(0, m.start() - 25):m.start()])),
                    "unit_after": bool(UNIT_AFTER.match(t[m.end():m.end() + 40])),
                    "counts": "." not in num and bool(COUNT_NOUN.match(t[m.end():m.end() + 25])),
                    "table": _source_line(text, t, m.start()).lstrip().startswith("|")})
    for m in TALLY.finditer(t):           # "passed 236-188", "| 248-177 |": yeas and nays, each a fact
        if not VOTE_WORDS.search(t[max(0, m.start() - 45):m.end() + 25]) \
                and not _source_line(text, t, m.start()).lstrip().startswith("|"):
            continue
        if re.search(r"(?i)\b(?:votes?|rolls?|roll\s+calls?|numbers?|nos?\.?|#)\s*$", t[max(0, m.start() - 15):m.start()]):
            continue                          # "votes 1-659": a range of vote numbers, not a tally
        for g in (1, 2):
            out.append({"tok": m.group(g), "num": float(m.group(g)), "unit": "", "scale": 1.0, "tol": 0.5, "dec": 0,
                        "neg": False, "dollar": False, "plus": False, "line": _source_line(text, t, m.start()),
                        "approx": False, "unit_after": False, "counts": True,
                        "table": _source_line(text, t, m.start()).lstrip().startswith("|")})
    return out


def fact_display(f: dict) -> tuple[float, str, int | None] | None:
    """A fact's value (with its sign) in the unit its format shows (percent, basis points or base units) and
    that unit's kind: pct, pp, bp, money (dollar formats), base, or "" when the format is unknown."""
    try:
        v = float(f.get("value"))
    except (TypeError, ValueError):
        return None
    nums =[p for p in str(f.get("fmt") or "").split(":") if p.lstrip("-").isdigit()]
    digits = int(nums[0]) if nums else None
    parts = [p for p in str(f.get("fmt") or "").split(":") if p and not p.lstrip("-").isdigit() and p != "abs"]
    mult = 1.0
    while parts and parts[0] in ("signed", "abs", "from_thousands", "from_millions", "from_billions"):
        mult *= {"from_thousands": 1e3, "from_millions": 1e6, "from_billions": 1e9}.get(parts.pop(0), 1.0)
    kind = parts[0] if parts else ""
    v *= mult
    if kind in ("pct", "pp"):
        return v * 100, kind, digits
    if kind in ("pct100", "pp100"):
        return v, kind[:-3], digits
    if kind in ("bp", "bp100"):
        return v * (1e4 if kind == "bp" else 100), "bp", digits
    if kind in ("thousands", "kmillions"):
        return v * 1e3, "base", digits
    if kind.endswith("_scaled"):
        return v * {"m": 1e6, "b": 1e9, "t": 1e12}[kind[4]], "money", digits
    if kind.startswith(("dollars", "usd")):
        return v, "money", digits
    if kind:
        return v, "base", digits
    return v, "", digits


def _matches(n: dict, shown: float, kind: str, digits: int | None = None) -> bool:
    """Does answer number n show this fact? A number written with a minus sign must be a negative fact (a fall
    written "fell 2.2 percent" may drop the sign), and a dollar amount must be a dollar fact (or one of unknown
    format), never an index or a count."""
    if (n["neg"] and shown >= 0) or (n["dollar"] and kind not in ("money", "", "pub", "pubnum")) \
            or (n.get("plus") and shown < -n["tol"]) \
            or (n.get("table") and shown < -n["tol"] and not n["neg"]):   # a table cell states its sign (not "0.0")
        return False
    shown = abs(shown)
    kind = "base" if kind == "money" else kind
    x, tol = n["num"], n["tol"] * 1.0001 + 1e-12
    if kind in ("pubpct", "pubpp", "pubnum", "pub"):  # a published benchmark figure, as benchmark.csv writes it
        if digits is not None and n.get("dec", 0) < digits:
            return False           # quoted at its published precision: "2.4" is not BEA's "2.35", "5" not "5.2"
        units = {"pubpct": ("pct", ""), "pubpp": ("pp", ""), "pubnum": ("", "scaled")}.get(kind)
        ok_unit = (units is None or n["unit"] in units) and not (n["dollar"] and kind in ("pubpct", "pubpp"))
        return ok_unit and abs(x - shown) <= tol
    if n["unit"] in ("pct", "pp"):          # a percent is not a percentage-point difference (fmt pp / pp100)
        if kind in ("pct", "pp") and digits is not None and n.get("dec", 0) < max(digits, 0) and not n.get("approx"):
            return False                    # "22 percent" is not the fact shown as "22.4 percent"
        cands = [shown] if kind == n["unit"] else [shown, shown * 100] if kind == "" else []
    elif n["unit"] == "bp":
        cands = [shown] if kind == "bp" else [shown * 1e4, shown * 100] if kind == "" else []
    elif n["unit"] == "scaled":
        if kind == "" and abs(x - shown) <= tol:      # a published figure kept in its table's unit: "14,665 thousand"
            return True
        x, tol = x * n["scale"], tol * n["scale"]
        cands = [shown] if kind in ("base", "") else []
    elif kind in ("pct", "pp", "bp") and not n.get("table") and n.get("counts") and not n.get("unit_after"):
        return False               # a bare count ("13 of the 15", "13 departments") is not the fact "-13 percent"
    elif kind in ("pct", "pp", "bp") and digits is not None and n.get("dec", 0) != max(digits, 0):
        return False               # a bare "70" is not the fact shown as "70.4 percent"
    else:                      # a bare number: a table cell under a "$ billions" or "%" header, a count, a ratio
        # a scaled-down match (1,992.5 for $1.99 trillion in a "$ billions" column) needs the number written with
        # a decimal or a comma: a plain small integer ("52 offices") would match too many facts by chance
        if (kind == "base" and digits is not None and digits > 0 and n.get("dec", 0) < digits
                and not n.get("approx") and abs(x - shown) <= tol):
            return False           # "0.2" is not the fact shown as "0.19" (unless "about 0.2")
        down = (1e-3, 1e-6, 1e-9, 1e-12) if (n["tok"].count(".") or n["tok"].count(",")) and n.get("table") else ()
        cands = ([shown] if kind in ("pct", "pp", "bp") else [shown * k for k in (1,) + down] if kind == "base"
                 else [shown * k for k in (1, 100) + down])
    return any(abs(x - c) <= tol for c in cands)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`>]", "", str(s))).strip().strip(".").lower()


NUMBER_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
DOWN_WORDS = re.compile(r"(?i)\b(?:fell|fall(?:s|ing|en)?|declin\w*|decreas\w*|drop\w*|down|lower\w*|less|loss\w*|"
                        r"lost|negative|shr[ai]nk\w*|shrunk|contract\w*|slow\w*|below|cut|reduc\w*|minus|deficit\w*|"
                        r"behind|short(?:fall)?|trail\w*|lagg\w*|"
                        r"weak\w*|slid\w*|sank|sunk|tumbl\w*|plung\w*|gap|narrow\w*|smaller|fewer|worse|"
                        r"subtract\w*|drag\w*|detract\w*|held back|hold\w* back|offset\w*|outflow\w*|"
                        r"withdr[ae]w\w*|erod\w*|deduct\w*|refund\w*|deficit|shortfall|net of)\b")


UP_WORDS = re.compile(r"(?i)\b(?:rose|rise[sn]?|rising|gain\w*|increas\w*|grew|grow\w*|added|adding|up|higher|improv\w*|"
                      r"climb\w*|jump\w*|surg\w*|expand\w*)\b")


def _allow_key(s: str) -> str:
    """An audit_allow entry and an answer number compare as written, unit included: "90 percent" = "90-percent"
    = "90%", but "90" does not allow "90 percent"."""
    m = re.match(r"\s*(\$?[\d,.]+)\s+([a-z]+)\s*$", str(s), re.I)
    if m and not re.match(r"(?i)(?:percent\w*|thousand|million|billion|trillion|basis|points?|pp|bps?)$", m.group(2)):
        s = m.group(1)                          # "60 days": the answer token is "60"
    k = re.sub(r"[\s$,\-]", "", str(s)).lower()
    return re.sub(r"percent$", "%", k)


def _below0(f: dict) -> bool:
    """A fact whose value is negative, however it is printed: "+1.1" cannot show a fall of 1.1."""
    try:
        return float(f.get("value")) < 0
    except (TypeError, ValueError):
        return False


def _negative(f: dict) -> bool:
    try:
        return float(f.get("value")) < 0 and "abs" not in str(f.get("fmt") or "")
    except (TypeError, ValueError):
        return False


def _is_change(f: dict) -> bool:
    """A difference (pp, bp, signed): "above"/"below" after it reads as its direction."""
    fmt = str(f.get("fmt") or "")
    return bool(re.search(r"(?:^|:)(?:pp|pp100|bp|bp100|signed)(?::|$)", fmt))


def _cell_key(x) -> str:
    return re.sub(r"[\s*_`]+", " ", str(x)).strip().lower()


def _row_label(n: dict) -> str | None:
    """The label of a table cell's row (the nearest name to its left); None outside tables, for a period or an
    unreadable row."""
    line = n.get("line") or ""
    if not line.lstrip().startswith("|"):
        return None
    cells = line.strip().strip("|").split("|")
    k = next((i for i, c in enumerate(cells) if _has(c, n["tok"])), len(cells))
    names = [c for c in cells[:k] if re.search(r"[A-Za-z]", c) and not re.search(r"\d", c)
             and not re.fullmatch(r"(?i)\s*\**\s*(?:n/?a|none|nil|not\s+(?:published|available|reported|applicable|"
                                  r"checked|on\s+\w+)|missing|withheld|suppressed|[(]?na[)]?|x|—|–|-|\.\.\.)\s*\**\s*", c)]
    rank = re.fullmatch(r"(?i)\s*\**\s*(?:#?\d+|(?:rank|no\.?|oldest|youngest|top|bottom|highest|lowest|first|"
                        r"last)\s*\d+)\s*(?:\(tie\)|=)?\s*\**\s*", cells[0])
    if names and (not re.search(r"[A-Za-z]", cells[0]) or rank):
        cells = [names[-1]]                   # a rank column: the row is named by the nearest name to the left
    elif len(names) > 1:
        cells = [names[-1]]                   # two entities in one row ("Texas | ... | South Carolina | ...")
    row = _cell_key(cells[0])
    if not row or len(row) > 60 or _period_key(re.sub(r"\s*\(.*\)\s*$", "", row)) is not None \
            or not re.search(r"[a-z]{3}|\b\d+(?:st|nd|rd|th)\b|\b[a-z][a-z.]{0,10}\s*\d{1,6}\b", row) \
            or re.fullmatch(r"(?:fy\s?)?\d{4}\s*(?:[–-]|to)\s*\d{2,4}|\d{4}\s*q[1-4]|q[1-4]\s*\d{4}", row):
        return None                 # a period ("2024", "Sep 2026") can hold other periods' values (a COLA year)
    return row


def _forms(v) -> set[str]:
    """A name as written and its short form: "Natural Resources Committee" -> natural resources, "Department of
    Energy" -> energy."""
    v = _cell_key(v)
    short = re.sub(r"^(?:the\s+|u\.?s\.?\s+|(?:house|senate)\s+)?(?:committee\s+on\s+(?:the\s+)?|department\s+of\s+(?:the\s+)?)", "", v)
    short = re.sub(r"\s+(?:committee|department|agency|administration|bureau|office|county|state)$", "", short)
    alias = {"u.s."} if v in ("united states", "u.s. total", "united states total") else set()
    party = {"democratic": {"democrat", "democrats"}, "democrat": {"democrats", "democratic"},
             "republican": {"republicans"}, "independent": {"independents"}}   # "Democrats split" is the D row
    alias |= party.get(v, set())
    return {v} | alias | ({short} if short != v and len(short) >= 4 and re.search(r"[a-z]{3}", short) else set())


def _entities(facts: dict) -> dict[str, tuple]:
    """Names the facts are keyed by, each form to (keys, the name): {"state": "Colorado"} -> {"colorado":
    ({"state"}, "colorado")}; periods and codes are not names."""
    out: dict[str, tuple] = {}
    for f in (facts or {}).values():
        for k, v in ((f or {}).get("where") or {}).items() if isinstance(f, dict) else ():
            name = _cell_key(v)
            if 3 <= len(name) <= 60 and re.search(r"[a-z]{3}", name) and not re.search(r"\d", name):
                for form in _forms(name):
                    out.setdefault(form, (set(), name))[0].add(str(k))
    return out


_ABBR = re.compile(r"(?:\b(?:U\.S|U\.K|St|Mt|Ft|Dr|Mr|Ms|Mrs|No|vs|etc|Inc|Co|Corp|Jr|Sr|Gov|Sen|Rep|e\.g|i\.e|approx)\.|\b[A-Z]\.)$")


def _sentences(block: str) -> list[str]:
    """The block's sentences, not cut after an abbreviation or initial (U.S., St., e.g.)."""
    out = []
    for piece in re.split(r"(?<=[.;!?])\s+|(?<=[.;!?]\*\*)\s+|(?<=[.;!?]\*)\s+", block):
        if out and _ABBR.search(out[-1]):
            out[-1] += " " + piece
        else:
            out.append(piece)
    return out


def _misnamed(sents: list[str], n: dict, fhits: list, ents: dict) -> str | None:
    """Prose that names an entity right before a number ("Colorado (16.9 percent)") gives that entity's figure:
    when every matching fact is keyed by the same kind of name (state, industry) and none by this one, the
    number belongs to another entity. Judged only when the name, capitalized, stands right before the number
    ("Colorado (19.8", "Utah: 16.0", "Ohio at 4.1") with no other name or number between, and no keyed name
    of a matching fact follows it ("709 House, 286 Senate"). Short forms count ("Natural Resources")."""
    if not fhits or not all(isinstance(f.get("where"), dict) and f["where"] for f in fhits):
        return None
    pat = lambda e: r"(?i)(?<![\w-])" + re.escape(e) + r"(?![\w-])"      # noqa: E731
    own = {form for f in fhits for v in f["where"].values() for form in _forms(v)}
    spots = [(x, m.start()) for x in sents
             for m in re.finditer(r"(?<![\w.,$-])" + re.escape(n["tok"]) + r"(?![\d])", x)]
    for x, i in spots:
        win = x[max(0, i - 40):i]
        win = re.sub(r"\(\s*\d{3,5}\s*,", lambda m: "(" + " " * (len(m.group(0)) - 1), win)   # "(1863, "
        cut = max((m.end() for m in re.finditer(r"\d", win)), default=0)
        win = win[cut:]                                                         # since the previous number
        seen = {e: m for e in ents for m in [re.search(pat(e), win)] if m}
        if cut and seen and not win[:min(m.start() for m in seen.values())].strip():
            continue                    # the name follows the previous number: it is that number's ("254 House, 85.3")
        named = [(e, ents[e][0]) for e, m in seen.items()
                 if m.group(0)[0].isupper() or re.fullmatch(r"[\s(:+-]*", win[m.end():])]
        if not named or len({ents[e][1] for e in seen}) > 1:
            continue                    # no name, or a list of them ("bills, notes and bonds ... gives 3.52")
        gap = win[max(m.end() for e, _ in named for m in re.finditer(pat(e), win)):]
        if not re.fullmatch(r"(?i)[\s(:,$–+-]*(?:(?:at|was|is|of|with|had|reached|down|up|fell|rose)\s+)?[\s($+]*", gap):
            continue                    # the name is not the number's own ("Mississippi is lowest: places 2")
        after = x[i + len(n["tok"]):i + len(n["tok"]) + 60]
        if any(m.start() < 25 and re.fullmatch(r"(?i)\s*(?:[a-z%$]+\s+){0,3}", after[:m.start()])
               for e in own if e in ents
               for m in [re.search(pat(e) + r"(?!\s*[(:]\s*\$?\d)", after)] if m):
            continue                    # the number's name follows it ("$1,072.6 billion on Medicare", "286 Senate"),
                                        # not after a comma ("Native Americans with 9, Energy with 9")
        if any(e in own for e, _ in named):
            continue
        if any(k in f["where"] for _, keys in named for k in keys for f in fhits):
            return ents[named[-1][0]][1]
    return None


_OTHER = re.compile(r"(?i)\b(?:than|compar\w*|versus|vs\.?|against|above|below|average|whereas|while|relative|"
                    r"ratio|share|of\s+(?:the|its|their)\s+\w+|percent\s+of)\b")


def _row_name(v) -> str | None:
    """A where value that names a row (Colorado, 117th, Energy), not a period, code or number."""
    v = _cell_key(v)
    if 2 <= len(v) <= 60 and (re.search(r"[a-z]{3}", v) and not re.search(r"\d", v)
                              or re.fullmatch(r"\d{1,3}(?:st|nd|rd|th)(?:\s+[a-z]+)?", v)):
        return v
    return None


def _clause_owner(sents: list[str], n: dict, fhits: list, facts: dict) -> str | None:
    """A clause about one row ("the 117th (2021-2022) enacted 344 public laws") must cite that row's figure: the
    number's facts are all keyed by other rows of one key, the clause names exactly one row of that key and no
    comparison word stands between the name and the number, and a fact of the named row gives the same measure
    (its label with the row name swapped) with another value. Returns the named row, else None."""
    if not fhits or not all(isinstance(f.get("where"), dict) and f["where"] for f in fhits):
        return None
    keys = set.intersection(*({k for k, v in f["where"].items() if _row_name(v)} for f in fhits))
    for k in keys:
        rows = {_row_name(f2.get("where", {}).get(k)) for f2 in (facts or {}).values()
                if isinstance(f2, dict) and isinstance(f2.get("where"), dict)} - {None}
        own = {_row_name(f["where"][k]) for f in fhits}
        pat = lambda e: r"(?i)(?<![\w-])" + re.escape(e) + r"(?![\w-])"      # noqa: E731
        for x in sents:
            i = _at(x, n["tok"])
            if i < 0:
                continue
            named = {r for r in rows for form in _forms(r) if re.search(pat(form), x)}
            if len(named) != 1 or named & own:
                continue
            row = next(iter(named))
            m = [m for form in _forms(row) for m in re.finditer(pat(form), x[:i])
                 if m.group(0)[0].isupper() or m.group(0)[0].isdigit()]   # a name, not a phrase ("all civilian workers")
            if not m or _OTHER.search(x[max(mm.end() for mm in m):i]):
                continue            # the name comes after the number, or a comparison sits between them

            def stem(f, name):
                return re.sub(pat(name), "", _cell_key(f.get("label", ""))).strip()

            sib = [g for g in (facts or {}).values() if isinstance(g, dict) and isinstance(g.get("where"), dict)
                   and _row_name(g["where"].get(k)) == row
                   and any({a: b for a, b in g["where"].items() if a != k} ==
                           {a: b for a, b in f["where"].items() if a != k}
                           and stem(g, row) == stem(f, _row_name(f["where"][k])) for f in fhits)]
            if sib and not any(_value(g) == _value(f) for g in sib for f in fhits):
                return row
    return None


_GENERIC_ACR = {"GDP", "USA", "BLS", "BEA", "OPM", "FRED", "IPUMS", "CPS", "ACS", "OMB", "CBO", "GAO", "MTS", "API",
                "NSA", "SA", "SAAR", "FY", "CY", "NAICS", "MOE", "SE", "CI", "USD", "DC"}


def _measure_named(sents: list[str], n: dict, fhits: list, facts: dict) -> str | None:
    """A measure named by its acronym right before the number ("CPI-U, up 26.9 percent", "the ECI rose 3.3") must be
    a fact of that measure: when some facts' labels carry the acronym and none of the number's facts does, the
    number belongs to another measure. Acronyms that most labels share (GDP, BLS) say nothing and are skipped."""
    labels = [str(f.get("label") or "") for f in (facts or {}).values() if isinstance(f, dict)]
    if not fhits or not labels or not all(f.get("label") for f in fhits):
        return None
    for x in sents:
        i = _at(x, n["tok"])
        if i < 0:
            continue
        # the acronym is the number's subject: right before it, with punctuation or one linking word between
        m = re.search(r"\b([A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)?)\s*[,:(]?\s*(?:(?:was|is|at|of|up|down|rose|fell|grew|"
                      r"increased|decreased|declined|by|=)\s+){0,2}[+\-−]?\$?$", x[max(0, i - 40):i])
        if not m:
            continue
        before = x[max(0, i - 40 - 30):max(0, i - 40) + m.start()]
        if re.search(r"(?i)\b(?:instead\s+of|rather\s+than|except|excluding|without|not|than|vs\.?|versus)\s*$",
                     before):
            continue                       # "CPI-W instead of CPI-U, +$21": the acronym is not the number's measure
        for a in [m.group(1)]:
            if a in _GENERIC_ACR or len(a) < 3:
                continue
            pat = re.compile(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])")
            carry = [lab for lab in labels if pat.search(lab)]
            if not carry or len(carry) > len(labels) / 2:
                continue
            if not any(pat.search(str(f.get("label"))) for f in fhits):
                return a
    return None


def _column_misfits(block: str, facts: dict) -> list[str]:
    """A table's columns each hold one measure: a cell whose facts (by label, with the row's names and years taken
    out) give another column's measure and not its own was moved across columns ("| -13.5 | -15.1 |" swapped).
    Judged only where facts carry labels and a column has at least three cells that agree."""
    lines = [l for l in block.strip().splitlines() if l.lstrip().startswith("|")]
    if len(lines) < 5:
        return []
    named = {k: f for k, f in (facts or {}).items() if isinstance(f, dict) and f.get("label")}
    if not named:
        return []
    shown = [(k, fact_display(f), f) for k, f in named.items() if fact_display(f)]

    def stem(f):
        s = _cell_key(f["label"])
        for v in (f.get("where") or {}).values():
            s = s.replace(_cell_key(v), " ")
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()

    cols: dict[int, list] = {}
    for l in lines[2:]:
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        for k, c in enumerate(cells[1:], start=1):
            for n in answer_numbers(c):
                n["table"] = True
                stems = {stem(f) for _, d, f in shown if _matches(n, *d)}
                if stems:
                    cols.setdefault(k, []).append((n["tok"], stems))
    mode = {}
    for k, cells in cols.items():
        if len(cells) < 3:
            continue
        counts: dict[str, int] = {}
        for _, st in cells:
            for s in st:
                counts[s] = counts.get(s, 0) + 1
        top, n_top = max(counts.items(), key=lambda kv: kv[1])
        if n_top >= max(3, len(cells) - 1) and list(counts.values()).count(n_top) == 1:
            mode[k] = top
    out = []
    for k, cells in cols.items():
        if k not in mode:
            continue
        others = {m for j, m in mode.items() if j != k and m != mode[k]}
        out += [tok for tok, st in cells if mode[k] not in st and st & others]
    return out


def _unbound_table(block: str, shown: list, facts: dict) -> str | None:
    """A table whose rows hold facts but none of whose row labels keys a fact (`where=`): its cells could be
    moved between rows unnoticed. Period-keyed rows are judged by _period_misfits instead."""
    rows = []
    for l in [l for l in block.strip().splitlines() if l.lstrip().startswith("|")][2:]:
        nums = answer_numbers(l)
        for n in nums:
            n["table"] = True
        if not any(name in (facts or {}) and _matches(n, *d) for n in nums for name, d, f in shown):
            continue
        row = _row_label(nums[0]) if nums else None
        if row is not None:
            rows.append(row)
    if len(rows) < 2:
        return None
    vals = [_cell_key(v) for f in (facts or {}).values() if isinstance(f, dict) for v in (f.get("where") or {}).values()]
    return None if any(re.match(re.escape(r) + r"(?:$|\s*[,;:(–-])", v) for r in rows for v in vals) else rows[0]


_FROM_TO = re.compile(
    r"(?i)\b(rose|risen|increased|grew|grown|climbed|jumped|fell|fallen|declined|dropped|decreased|slipped|shrank|"
    r"shrunk)\s+(?:[a-z]+\s+){0,3}?from\s+(?:about\s+|roughly\s+)?(-?\$?\d[\d,]*(?:\.\d+)?)\s*"
    r"(%|percent|thousand|million|billion|trillion)?[^.;\n]{0,40}?\bto\s+(?:about\s+|roughly\s+)?"
    r"(-?\$?\d[\d,]*(?:\.\d+)?)\s*(%|percent|thousand|million|billion|trillion)?")


_VS_FROM = re.compile(
    r"(?i)(?<![\w.,])(-?\$?\d[\d,]*(?:\.\d+)?)\s*(%|percent|thousand|million|billion|trillion)?"
    r"(?:\s+(?:in|for)\s+(?:fy\s?)?(?:19|20)\d\d)?\s*,?\s+(up|down|higher|lower)\s+from\s+(?:about\s+)?"
    r"(-?\$?\d[\d,]*(?:\.\d+)?)\s*(%|percent|thousand|million|billion|trillion)?")


def _from_to_misreads(text: str) -> list[str]:
    """ "fell from $890.8 billion to $984.0 billion": a direction word whose own from-to numbers move the other
    way. Years ("rose from 2019 to 2024 by ...") are not levels."""
    out = []
    for m in _FROM_TO.finditer(text):
        a, ua, b, ub = m.group(2), (m.group(3) or "").lower(), m.group(4), (m.group(5) or "").lower()
        num = lambda t: float(t.replace("$", "").replace(",", ""))      # noqa: E731
        if re.fullmatch(r"(?:1[89]|20)\d\d", a) and re.fullmatch(r"(?:1[89]|20)\d\d", b) and not (ua or ub):
            continue
        sa = _SCALE.get(ua or ub, 1.0)
        sb = _SCALE.get(ub or ua, 1.0)
        x, y = num(a) * sa, num(b) * sb
        up = not re.match(r"(?i)fell|fallen|declin|drop|decreas|slip|shr", m.group(1))
        if (up and y < x) or (not up and y > x):
            out.append(m.group(0)[:60])
    for m in _VS_FROM.finditer(text):         # "18.3 percent in 2025, down from 16.8 percent": now vs then
        a, ua, word, b, ub = m.group(1), (m.group(2) or "").lower(), m.group(3).lower(), m.group(4), (m.group(5) or "").lower()
        a, b = a.rstrip(","), b.rstrip(",")
        if not ua and re.fullmatch(r"(?:19|20)\d\d", a):
            continue                          # "in 2025, down from 17.6": the year is not the level
        if (ua in ("%", "percent")) != (ub in ("%", "percent")) and ua and ub:
            continue
        num = lambda t: float(t.replace("$", "").replace(",", ""))      # noqa: E731
        x, y = num(a) * _SCALE.get(ua or ub, 1.0), num(b) * _SCALE.get(ub or ua, 1.0)
        up = word in ("up", "higher", "rising", "rose")
        if (up and x < y) or (not up and x > y):
            out.append(m.group(0)[:60])
    return out


def _row_ok(n: dict, fhits: list, facts: dict) -> bool:
    """A table cell matches a fact of its own row, when facts are registered by that row: some fact's `where`
    value is the row's label exactly ("2026", "Veterans Affairs"). Other rows are not judged."""
    row = _row_label(n)
    if row is None:
        return True
    base = re.sub(r"\s*\(.*\)\s*$", "", row) or row         # "retirement (voluntary, early out)" -> retirement
    pat = re.compile(re.escape(row) + r"(?:$|\s*[,;:(–-])")   # the label is the value or begins it
    keyed = lambda f: any(pat.match(_cell_key(v)) or _cell_key(v) == base     # noqa: E731
                          for v in (f.get("where") or {}).values())
    if not any(keyed(f) for f in (facts or {}).values() if isinstance(f, dict)):
        return True                                       # no fact is registered by this row label
    return any(keyed(f) for f in fhits)


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                        "nov", "dec"), start=1)}


def _period_key(x) -> int | None:
    """A period as year*100 + month (month 0 when only a year is given; a quarter is its first month, as BEA and
    FRED date it): "Apr 2023", "2023-04-01", "2024", "2025 Q3" (= July), "202508"."""
    x = str(x).lower().strip().strip("*").strip()
    ym = re.fullmatch(r"((?:19|20)\d\d)(0[1-9]|1[0-2])", x)      # OPM's yyyymm (202508)
    if ym:
        return int(ym.group(1)) * 100 + int(ym.group(2))
    if not re.fullmatch(r"(?:fy\s?)?\d{4}(?:-\d{2}(?:-\d{2})?)?(?:\s*:?\s*q[1-4])?|q[1-4]\s+\d{4}|[a-z]{3,9}\.?\s+\d{4}", x):
        return None                             # a period itself, not a name with a year in it
    y = re.search(r"(?<!\d)(17|18|19|20)\d\d(?!\d)", x)
    if not y:
        return None
    q = re.search(r"q([1-4])", x)
    m = re.search(r"(?<!\d)(?:17|18|19|20)\d\d-(\d\d)", x) or re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\b", x)
    month = 3 * int(q.group(1)) - 2 if q else (int(m.group(1)) if m.group(1).isdigit() else _MONTHS[m.group(1)[:3]]) if m else 0
    return int(y.group(0)) * 100 + month


def _lag(ref: int, fk: int) -> int:
    """Months from period fk to period ref; whole years when either gives only a year."""
    if ref % 100 and fk % 100:
        return (ref // 100 * 12 + ref % 100) - (fk // 100 * 12 + fk % 100)
    return (ref // 100 - fk // 100) * 12


def _fact_periods(f: dict) -> list[int]:
    """The periods a fact is about, from its `where`; a vintage, release or retrieval date is not one. A year and
    a month in separate fields are one month ({"fiscal_year": "2026", "month": "12"} is December 2025)."""
    w = {str(name): v for name, v in (f.get("where") or {}).items()
         if not re.search(r"(?i)vintage|release|as_?of|retriev|published|accessed", str(name))}
    per = lambda v: _period_key(re.sub(r"\s*\(.*\)\s*$", "", str(v)))   # noqa: E731  "2026 (to Oct 15)"
    keys = [k for v in w.values() for k in [per(v)] if k is not None]
    mon = next((v for name, v in w.items() if re.search(r"(?i)month", name)), None)
    mon = str(mon).strip().lower() if mon is not None else ""
    m = int(mon) if re.fullmatch(r"0?[1-9]|1[0-2]", mon) else _MONTHS.get(mon[:3]) if re.fullmatch(r"[a-z]{3,9}\.?", mon) else None
    years = [(k, name) for name, v in w.items() for k in [per(v)] if k is not None and k % 100 == 0]
    if m and len(years) == 1:
        y = years[0][0] // 100
        fiscal = re.search(r"(?i)fiscal|\bfy", years[0][1]) or str(w[years[0][1]]).lower().startswith("fy")
        keys = [k for k in keys if k != years[0][0]] + [(y - 1 if fiscal and m >= 10 else y) * 100 + m]
    if any(k % 100 for k in keys):
        keys = [k for k in keys if k % 100]      # the month is the fact's period; its fiscal year adds nothing
    if not keys:                                 # a wide table's column ("share_2019", "2025"): its one year
        col = str(f.get("column") or "")
        ys = [] if re.search(r"(?i)fy|fiscal", col) else re.findall(r"(?<!\d)((?:19|20)\d\d)(?!\d)", col)
        if len(ys) == 1:
            keys = [int(ys[0]) * 100]
    return keys


def _period_misfits(block: str, shown: list, facts: dict) -> list[str]:
    """In a table keyed by periods, each column relates its cells to their rows the same way (a COLA year's row
    holds the prior year's average: the same lag on every row). A cell whose matching facts all break its
    column's lag is a value moved from another row."""
    lines = [l for l in block.strip().splitlines() if l.lstrip().startswith("|")]
    if len(lines) < 4:
        return []
    cols: dict[int, list] = {}
    head = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    for l in lines[2:]:
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        rk = _period_key(re.sub(r"\s*\(.*\)\s*$", "", cells[0])) if cells else None
        for k, c in enumerate(cells[1:], start=1):
            base = r"(?!\s*(?:dollars|prices|\$|=\s*100))"   # "2024 dollars" is a price base, not the column's period
            hm = re.match(r"(?i)\s*\**\s*((?:fy\s?)?\d{4}(?:-\d{2})?|[a-z]{3,9}\.?\s+\d{4})\b" + base,
                          head[k]) if k < len(head) else None
            if hm is None and k < len(head) and not re.search(
                    r"(?i)\b(?:since|from|vs\.?|versus|change|minus|relative|compared|than|over)\b", head[k]):
                # one period anywhere in the header: "Share, 2026 (%)", but not "Change since September 2024"
                found = re.findall(r"(?i)(?<![\w-])(?<!constant\s)(?<!chained\s)((?:fy\s?)?(?:19|20)\d\d(?:-\d{2})?)"
                                   r"(?![\w-]|\s*[–-]\s*\d)" + base, head[k])
                hm = re.search(re.escape(found[0]), head[k]) if len(found) == 1 else None
                hm = re.match(r"(.*)", found[0]) if hm else None
            hk = _period_key(hm.group(1)) if hm else None      # a header that begins with its period
            for key, ref in (("r", rk), ("h", hk)):
                if ref is None:
                    continue
                for n in answer_numbers(c):
                    n["table"] = True
                    lags, matched = set(), False
                    for name, d, f in shown:
                        if name in (facts or {}) and _matches(n, *d):
                            matched = True
                            keys = _fact_periods(f)
                            fy = re.match(r"(?i)\s*\**\s*(?:fy|fiscal)", cells[0] if key == "r" else head[k])
                            for fk in keys:
                                if fy and fk % 100 >= 10:
                                    fk = (fk // 100 + 1) * 100      # Oct-Dec belong to the next fiscal year
                                lags.add(_lag(ref, fk))
                    if lags or matched:               # judged by its dated facts; a cell that matches only
                        cols.setdefault((key, k), []).append((n["tok"], lags))   # undated ones fits no row
    out = []
    for k, cells in cols.items():
        if len(cells) == 2 and any(0 in lags for _, lags in cells):
            out += [tok for tok, lags in cells if 0 not in lags]   # "FY2025 | FY2026": each row its own period
            continue
        if len(cells) == 2 and all(lags for _, lags in cells) and not set.intersection(*(lags for _, lags in cells)):
            out += [tok for tok, _ in cells]      # two rows with no lag in common: their values were swapped
            continue
        if len(cells) < 3:
            continue
        if sum(1 for _, lags in cells if lags) < 3:
            continue                                      # too few dated cells to read the column's lag
        counts: dict[int, int] = {}
        for _, lags in cells:
            for g in lags:
                counts[g] = counts.get(g, 0) + 1
        mode, top = max(counts.items(), key=lambda kv: kv[1])
        if top < len(cells) - 1:
            continue                                      # no lag shared by the column: nothing to compare
        out += [tok for tok, lags in cells if mode not in lags]
    return out


YEAR_AFTER = re.compile(r"(?i)^\s*(?:percent(?:age points?)?|points?|pp)?(?:\s+of\s+(?:the\s+)?(?:[a-z.]{2,15}\s+){0,3}[a-z]{2,15})?\s*"
                        r"(?:in|for|during)\s+(?:the\s+)?"                           # "percent of GDP in 2019"
                        r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?"   # in September 2024
                        r"(?:fy\s?)?((?:17|18|19|20)\d\d)"
                        r"\b(?!\s*(?:-|–|to|through)\s*(?:\d|[a-z]{3,9}\.?\s+\d)|\s*(?:dollars|prices|terms|chained|constant|real))|^\s*(?:percent(?:age points?)?|points?)?\s*\((?:fy\s?)?((?:17|18|19|20)\d\d)\)")


def _year_before(before: str, after: str = "") -> str:
    """ "for 2025 (men 20.2": a year that dates the number from just before it, as " in 2025" for _wrong_year;
    empty when another number, a comma or a from/to/than comparison word stands between them, or when a year
    follows the number ("146 in 2025 (143 in 2024)", "$1,177.1 billion for 2025, $404.3 billion for 2019")."""
    m = re.search(r"(?i)\b(?:in|for|during)\s+((?:fy\s?)?(?:17|18|19|20)\d\d)\b([^\d.;,:]{0,25})$", before) or \
        re.search(r"(?i)(?<![\w-])((?:fy\s?)?(?:17|18|19|20)\d\d)()\s*\(\s*(?:about\s+)?[$+\-−]?$", before)  # "1929 (0.55"
    own = re.split(r"[,;]", after[:50], maxsplit=1)[0]      # the number's own clause: a year after a comma is the next one's
    if not m or re.search(r"(?i)\b(?:to|from|than|vs|versus|compared|since|over|until|through|and)\b", m.group(2)) \
            or re.search(r"(?<!\d)(?:17|18|19|20)\d\d(?!\d)", own):
        return ""
    return f" in {m.group(1)}"


def _wrong_year(after: str, fhits: list) -> bool:
    """"6.3 percent in 2019": when every fact the number matches is keyed to periods and none falls in the year
    the sentence gives right after the number, the number belongs to another year."""
    m = YEAR_AFTER.match(after)
    if not m:
        return False
    year = int(m.group(2) or m.group(3))
    mon = _MONTHS.get((m.group("mon") or "").lower()[:3])
    keys = []
    for f in fhits:
        ks = _fact_periods(f)
        if not ks:                            # a sensitivity fact keyed by its alternative: the one year its label names
            ys = set(re.findall(r"(?<!since )(?<!from )(?<!to )(?<!through )(?<!until )(?<!before )(?<!after )"
                                r"(?<![\d-])(?:fy\s?)?((?:19|20)\d\d)(?![\d-])", str(f.get("label") or ""), re.I))
            ks = [int(ys.pop()) * 100] if len(ys) == 1 else []
        if not ks:
            return False                      # a fact without a period could be any year
        keys += ks
    if mon and all(k % 100 for k in keys):    # month-level facts and a month in the text: the month must match
        return year * 100 + mon not in keys
    return bool(keys) and year not in [k // 100 for k in keys]


def _dated(sents: list[str], n: dict, fhits: list) -> list:
    """The facts of the year the sentence gives for the number ("fell 2.5 percent in 2020"), when some match it:
    a number that also matches another year's fact is judged by its own year's fact. Otherwise all of fhits."""
    for x in sents:
        i = _at(x, n["tok"])
        if i < 0:
            continue
        after = x[i + len(n["tok"]):]
        m = YEAR_AFTER.match(after[:40]) or YEAR_AFTER.match(_year_before(x[:i], after))
        if m:
            year = int(m.group(2) or m.group(3))
            own = [f for f in fhits if year in [k // 100 for k in _fact_periods(f)]]
            if own:
                return own
    return fhits


def _pub_year_fits(period: str, spots: list, tok: str) -> bool:
    """Does a published figure's period hold every year the text gives for the number? (A benchmark row of 2014
    does not excuse "19.5 percent in 2024".) True when the row names no year."""
    have = set(re.findall(r"(?<!\d)((?:17|18|19|20)\d\d)(?!\d)", period))
    if not have:
        return True
    for x, i in spots:
        after = x[i + len(tok):]
        m = YEAR_AFTER.match(after[:40]) or YEAR_AFTER.match(_year_before(x[:i], after))
        if m and (m.group(2) or m.group(3)) not in have:
            return False
    return True


def _value(f: dict) -> float:
    try:
        return float(f.get("value"))
    except (TypeError, ValueError):
        return 0.0


# a citing word: the number is someone else's ("BLS publishes 770", "matches the published 23.3")
CITING = re.compile(r"(?i)benchmark|publish|\breports?\b|\breported\b|\brelease[sd]?\b|according to|official|"
                    r"\bmatch(?:es|ed)?\b|\bagrees?\b|compared? with|vintage|first (?:estimate|release)|superseded|"
                    r"as (?:first )?(?:reported|published)")
_COMMON = {"the", "and", "for", "from", "table", "tables", "data", "release", "report", "page", "series", "news",
           "annual", "monthly", "latest", "estimates", "estimate", "summary", "file", "files", "statistics",
           "united", "states", "national", "official", "published", "labor", "force", "current", "population",
           "survey", "department", "bureau", "office", "averages", "average", "income", "employment", "economic",
           "analysis", "interactive", "database", "dashboard", "workforce", "federal", "public", "monthly",
           "weekly", "quarterly", "detailed", "historical", "with", "https", "http", "www", "gov", "htm", "html",
           "pdf", "xlsx", "csv"}


def _source_words(sources: list[str]) -> set[str]:
    """The publisher's names in benchmark.csv's source column: acronyms (BLS, OFR), codes (DP02, cpsaat37b) and
    proper names (Census, Freddie), not common words of a title ("from", "labor")."""
    out = set()
    for src in sources:
        src = re.sub(r"https?://\S+", " ", str(src))
        for w in re.findall(r"[A-Za-z][A-Za-z0-9&'-]+", src):
            low = w.lower()
            if low in _COMMON or len(w) < 2:
                continue
            if re.fullmatch(r"[A-Z]{2,}[A-Za-z0-9]*", w) or re.search(r"\d", w) or (w[0].isupper() and len(w) >= 4):
                out.add(low)
        # the initials of a capitalized name: "Office of the Federal Register" -> ofr
        for run in re.findall(r"[A-Z][a-z]+(?:\s+(?:(?:of|the|and|for|on)\s+)*[A-Z][a-z]+)+", str(src)):
            ini = "".join(x[0] for x in re.findall(r"[A-Z][a-z]+", run))
            if len(ini) >= 2:
                out.add(ini.lower())
    return out


def _cites(text: str, words: set[str]) -> bool:
    low = text.lower()
    return bool(CITING.search(text)) or any(re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", low) for w in words)


def _attributed(block: str, n: dict, is_table: bool, sources: list[str] | None = None) -> bool:
    """Is a number that is only a published benchmark figure (no fact) credited to its publisher? In prose its
    sentence (or the paragraph's bold lead-in) uses a citing word or names that row's source; in a table its
    column header does."""
    words = _source_words(sources or [])
    if not is_table:
        lead = re.match(r"\s*(?:[-*]\s+)?\*\*(.+?)\*\*", block)     # a bold lead-in labels its paragraph
        if lead and CITING.search(lead.group(1)):
            return True
        sent = [x for x in re.split(r"(?<=[.!?])\s+(?=[A-Z*(\[])", block) if _has(x, n["tok"])] or [block]
        return any(_cites(x, words) for x in sent)
    lines = [l for l in block.strip().splitlines() if l.lstrip().startswith("|")]
    if not lines:
        return True
    head = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    seen = False                                  # every cell holding the number sits under a credited header
    body = lines[2:] if len(lines) > 2 else lines
    own = [l for l in body if (n.get("line") or "").strip() == l.strip()]
    for l in own or body:                         # in the number's own row, when it is known
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        for k, c in enumerate(cells):
            if _has(c, n["tok"]):
                seen = True
                if not (k < len(head) and _cites(head[k], words)):
                    return False
    return seen


PERIOD_COUNT = re.compile(r"(?i)\b\d{1,2}\s+of\s+(?:the\s+)?\d{1,2}\s+(?:days|weeks|months|quarters|years)\b"
                          r"|\(\d\)")              # "3 of 4 quarters"; list markers "(2)"
BENCH_WORDS = re.compile(r"(?i)benchmark|published|\bmatch(?:es|ed)?\b|\bagrees?\b|compared? with|same as the")

# ---------------------------------------------------------------------- dollar basis
# dollars of two different years are compared only on a stated basis: nominal (current dollars) or real (which
# index, which year's dollars). SKILL.md makes the basis a framing choice; "$X in 2025, up from $Y in 2019"
# without it reads as real growth when it may be mostly inflation.
CURRENCY = re.compile(r"(?i)(?:US)?\$\s?\d|\bUSD\s?\d|\d(?:[\d,.]*\d)?\s*(?:thousand|million|billion|trillion)?\s+dollars\b")
YEAR = re.compile(r"(?<![\d$.,])(?:FY\s?)?((?:19|20)\d{2})(?!\d|,\d{3}|\.\d)")
CHANGE = re.compile(r"(?i)\b(?:up|down)\s+(?:\S+\s+){0,3}?from\b|\b(?:rose|risen|rising|rises?|grew|grown|grows?|growth|"
                    r"increas\w*|decreas\w*|fell|fallen|falls?|declin\w*|jump\w*|doubl\w*|tripl\w*|halv\w*|climb\w*|"
                    r"dropp?\w*|compared\s+(?:with|to)|versus|vs\.?|(?:higher|lower|more|less)\s+than)\b"
                    r"|\bfrom\s+\$[\d.,]+[^.;]{0,40}?\bto\s+\$")
BASIS = re.compile(r"(?i)\bnominal\b|\bcurrent[- ](?:dollars?|prices|\$)|\b(?:not|un|without|before)[- ]?"
                   r"(?:being\s+)?(?:adjust\w*|correct\w*)\s+for\s+inflation|\b(?:inflation|price|cpi|pce)[- ]adjusted\b|"
                   r"\badjust\w*\s+(?:\w+\s+)?for\s+(?:inflation|prices)|\breal\s+(?:terms|dollars?|values?|spending|"
                   r"income|incomes|wages?|earnings|growth|prices?|gdp|basis|change|increase|decline|rise|fall)\b|"
                   r"\bin\s+real\b|\bconstant[- ](?:(?:19|20)\d{2}\s+)?(?:dollars?|prices|\$)|\bchained\b|"
                   r"\b(?:19|20)\d{2}\s+dollars\b|\(\s*(?:19|20)\d{2}\s*\$\s*\)|\btoday'?s\s+dollars\b|\bdeflat\w*")


def _years(s: str) -> set[str]:
    return {m.group(1) for m in YEAR.finditer(s)}


def _dollar_comparisons(text: str) -> list[str]:
    """Sentences (and tables) that put dollar amounts of two or more different years side by side: two amounts
    and two years in one sentence, or one amount, two years and a change word ("rose to $5 million in 2025 from
    2019"), or two amounts and two years over two sentences, the second with a change word; a table whose header row or first column holds two or more years and whose cells or header hold
    dollars."""
    found = []
    for block, _ in _blocks(text):
        lines = block.strip().splitlines()
        if all(l.lstrip().startswith("|") for l in lines):
            rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines
                    if not re.fullmatch(r"\|?[\s:|-]+\|?", l.strip())]
            if len(rows) < 2:
                continue
            in_head = {y for c in rows[0] for y in _years(c)}
            years = in_head if len(in_head) > 1 else {y for r in rows[1:] if r for y in _years(r[0])}
            money = re.search(r"(?i)\$|dollar|\bUSD\b", " ".join(rows[0])) or \
                sum(bool(CURRENCY.search(c)) for r in rows[1:] for c in r) > 1
            if len(years) > 1 and money:
                found.append(lines[0].strip()[:80])
            continue
        sents = _sentences(block)
        for i, s in enumerate(sents):
            amounts = len(CURRENCY.findall(s))
            if amounts and len(_years(s)) > 1 and (amounts > 1 or CHANGE.search(s)):
                found.append(" ".join(s.split())[:80])
                continue
            two = " ".join(sents[i:i + 2])             # "$X in 2019. By 2024 it reached $Y, an increase ..."
            if (i + 1 < len(sents) and amounts and len(CURRENCY.findall(two)) > 1 and len(_years(two)) > 1
                    and CHANGE.search(sents[i + 1])):
                found.append(" ".join(two.split())[:80])
    return found


def check_dollar_basis(text: str, D: dict) -> tuple:
    """FAIL when answer.md compares dollar amounts across years and nowhere says nominal or real (unless
    decisions.json "dollar_basis_ok" gives a reason)."""
    entry = D.get("dollar_basis_ok")
    reason = entry.get("reason") if isinstance(entry, dict) else entry
    found = _dollar_comparisons(text)
    if not found:
        return ("answer", True, "no dollar amounts compared across years")
    if BASIS.search(text):
        return ("answer", True, "dollar amounts compared across years carry their basis (nominal or real)")
    if isinstance(reason, str) and reason.strip():
        return ("answer", True, f"dollar amounts compared across years without a basis: {reason.strip()[:80]}")
    return ("answer", False,
            f"answer.md compares dollar amounts of different years ({'; '.join(repr(x) for x in found[:2])}) and "
            "never says their basis: say whether they are nominal (current dollars, not adjusted for inflation) or "
            "real (which index, in which year's dollars) next to the first comparison, and record the choice in "
            "decisions.json. When the basis truly does not matter, give decisions.json \"dollar_basis_ok\": "
            "{\"reason\": \"...\"}")


def _blocks(text: str) -> list[tuple[str, str]]:
    """(text to scan, text a label may sit in): each paragraph alone; a table with the paragraph before it (its
    caption) and after it (its notes), so "provisional" in a caption covers the table's numbers."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for i, p in enumerate(paras):
        is_table = all(l.lstrip().startswith("|") for l in p.strip().splitlines())
        ctx = "\n".join(([paras[i - 1]] if is_table and i else []) + [p]
                        + ([paras[i + 1]] if is_table and i + 1 < len(paras) else []))
        out.append((p, ctx))
    return out


def _table_words(block: str) -> list[dict]:
    """Counts spelled out as a whole table cell ("| four |"): factfmt writes 0-9 in words."""
    out = []
    for line in block.splitlines():
        if line.lstrip().startswith("|"):
            for cell in line.strip().strip("|").split("|"):
                w = cell.strip().lower()
                if w in NUMBER_WORDS:
                    out.append({"tok": w, "num": float(NUMBER_WORDS[w]), "unit": "", "scale": 1.0, "tol": 0.5,
                                "neg": False, "dollar": False})
    return out


_WORD_ALT = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
SUBSET_WORDS = re.compile(r"(?i)\b(" + _WORD_ALT + r")\s+of\s+(?:the\s+|these\s+|those\s+)?(" + _WORD_ALT + r"|\d{1,3})\b")


def _table_sizes(text: str) -> set[int]:
    """Data rows of each markdown table in the answer (header and |---| rule not counted)."""
    sizes = set()
    for block, _ in _blocks(text):
        rows = [l for l in block.strip().splitlines() if l.lstrip().startswith("|")]
        if rows and len(rows) == len(block.strip().splitlines()):
            body = [l for l in rows[1:] if not re.fullmatch(r"\s*\|[\s:|-]+\|?\s*", l)]
            sizes.add(len(body))
    return sizes


def _subset_words(block: str, sizes: set[int]) -> list[dict]:
    """"four of the five" in prose, when the answer holds a table of five rows, summarizes that table: like
    "4 of the 5" it must be a registered count (computed in analysis.py), so the words cannot contradict the rows.
    Other spelled-out phrases ("one of the two parties", "two of the four largest banks" with no such table) are
    ordinary prose and are left alone."""
    out = []
    t = AR.exempt(block)
    for m in SUBSET_WORDS.finditer(t):
        line = _source_line(block, t, m.start())
        if line.lstrip().startswith("|"):
            continue
        w, total = m.group(1).lower(), m.group(2).lower()
        total = NUMBER_WORDS[total] if total in NUMBER_WORDS else int(total)
        if total not in sizes or not 0 < NUMBER_WORDS[w] < total:
            continue
        out.append({"tok": m.group(1), "num": float(NUMBER_WORDS[w]), "unit": "", "scale": 1.0, "tol": 0.5, "dec": 0,
                    "neg": False, "dollar": False, "plus": False, "line": line, "approx": False,
                    "unit_after": False, "counts": True, "table": False})
    return out


def check_answer(base: str, facts: dict, D: dict) -> tuple[list[tuple], str]:
    """<out>/answer.md, the reply as it will be sent: every number in it is a registered fact as the format of
    that fact shows it (or a published benchmark figure, or in decisions.json audit_allow); it carries every
    disclosure line; a preliminary, projected or past-vintage fact is labelled in its own paragraph or table;
    and it reports the benchmark check. Returns (results, answer text)."""
    p = os.path.join(base, "answer.md")
    if not os.path.exists(p):
        return [("answer", False, "answer.md missing: write the reply exactly as you will send it to <out>/answer.md "
                                  "and run the audit again")], ""
    text = open(p, encoding="utf-8-sig").read()
    out = []
    shown = [(name, fact_display(f or {}), f or {}) for name, f in (facts or {}).items()]
    shown = [(name, d, f) for name, d, f in shown if d]
    # a fact printed without its sign (fmt ...:abs) may stand in a table cell as printed or with its sign
    shown += [(name, (abs(d[0]),) + tuple(d[1:]), f) for name, d, f in shown
              if d[0] < 0 and "abs" in str(f.get("fmt") or "").split(":")]
    bp = os.path.join(base, "exhibits", "benchmark.csv")      # a published figure the answer compares with
    bench_rows = MA._read_csv(bp)[1] if os.path.exists(bp) else []
    for r in bench_rows:
        b = MA.bench_number(r.get("published"))
        if b:
            # a published figure keeps its kind when the row says it (percent or an amount), never rescaled
            # ("0.01" is not 7.40 / 1000)
            desc = f"{r.get('published', '')} {r.get('measure', '')} {r.get('units', '')}"
            pct = re.search(r"%|percent|\bpct\b|\bpp\b|\brates?\b|\bshares?\b|\byields?\b|\bratios?\b", desc, re.I)
            amt = re.search(r"\$|dollar|\bthousands?\b|\bmillions?\b|\bbillions?\b|\btrillions?\b|\bcount|number",
                            f"{desc} {r.get('source', '')}", re.I)
            if not pct and not amt and re.search(r"(?i)\bgrowth\b|\bchanges?\b", desc):
                pct = True                     # a change with no unit anywhere: read it as a percent
            kind = "pubpct" if pct and not amt else "pubnum" if amt and not pct else "pub"
            if kind == "pubpct" and re.search(r"(?i)\bpoints?\b|\bpp\b", desc):
                kind = "pubpp"
            dec = round(-math.log10(2 * b[1])) if b[1] < 0.5 else 0
            meta = {"_source": f"{r.get('source', '')} {r.get('url', '')}", "_period": str(r.get("period") or ""),
                    "_measure": " ".join(str(r.get(c) or "") for c in ("measure", "geography", "geo",
                                         "area", "state", "name", "label", "item", "series", "group"))}
            shown.append((f"published {r.get('measure', '')}", (b[0], kind, dec), meta))
            # a figure published in thousands (or millions) may be written out: -179 thousand as "179,000"
            per = re.search(r"(?i)\b(thousands|millions)\b", f"{r.get('measure', '')} {r.get('units', '')}")
            if per and kind != "pubpct":
                k = 3 if per.group(1).lower() == "thousands" else 6
                shown.append((f"published {r.get('measure', '')}", (b[0] * 10 ** k, "pubnum", max(0, dec - k)), meta))
    fact_toks = {n["tok"].lower() for _, _, f in shown for n in answer_numbers(str(f.get("text", "")))}
    allowed = {_allow_key(str(a.get("value") if isinstance(a, dict) else a)) for a in D.get("audit_allow") or ()}
    bad, unlabelled, unsigned, unattributed, misrow, wrongyear = [], {}, [], [], [], []
    misnamed, ents, unbound, cited = [], _entities(facts), [], set()
    scan = text
    lead = "\n".join(b for b, _ in _blocks(text)[:2])   # a label in the opening (title + first paragraph) covers all
    sizes = _table_sizes(text)                         # a spelled-out "N of the M" over a table of M rows is a count
    for block, ctx in _blocks(scan):
        is_table = all(l.lstrip().startswith("|") for l in block.strip().splitlines())
        if is_table:
            misrow += _period_misfits(block, shown, facts)
            first = _unbound_table(block, shown, facts)
            if first:
                unbound.append(first)
        for n in answer_numbers(block) + _table_words(block) + _subset_words(block, sizes):
            n["table"] = is_table or n.get("table", False)    # a table under a caption line too
            if _allow_key(n["tok"]) in allowed:
                continue
            # a count with its noun ("3 months", "12 states") is no percent: it cannot be a pct, pp or bp fact
            counted = not n["unit"] and bool(n.get("counts"))
            hits = [(name, f) for name, d, f in shown if _matches(n, *d) and not (n["plus"] and _below0(f))
                    and not (counted and name in (facts or {}) and d[1] in ("pct", "pp", "bp"))]
            if not hits and n["tok"].lower() not in fact_toks:
                bad.append(n["tok"])
            fhits = [f for name, f in hits if name in (facts or {})]
            if len(fhits) == 1:                         # the one fact this number prints
                cited.update(name for name, f in hits if name in (facts or {}))
            if hits and not fhits and n["tok"].lower() not in fact_toks and not _attributed(block, n, is_table, [f.get("_source", "") for _, f in hits]):
                unattributed.append(n["tok"])              # a published figure passed off as the analysis's own
            sent0 = [x for x in _sentences(block) if _has(x, n["tok"])]
            spots = [(x, m.start()) for x in sent0          # every place the number stands, not only the first
                     for m in re.finditer(r"(?<![\w.,$-])" + re.escape(n["tok"]) + r"(?![\d])", x)]
            if fhits and not is_table and not n.get("table") and any(
                    _wrong_year(x[i + len(n["tok"]):][:40], fhits)
                    or _wrong_year(_year_before(x[:i], x[i + len(n["tok"]):]), fhits)
                    for x, i in spots) and not (              # a credited published figure of that year ("BLS: 4.0 in 2024")
                    (pub := [f.get("_source", "") for name, f in hits if name not in (facts or {})
                             and _pub_year_fits(f.get("_period", ""), spots, n["tok"])])
                    and _attributed(block, n, False, pub)):
                wrongyear.append(n["tok"])
            if fhits and not is_table and not n.get("table"):
                who = (_misnamed(sent0, n, fhits, ents) or _clause_owner(sent0, n, fhits, facts)
                       or _measure_named(sent0, n, fhits, facts))
                if who and not any(form in _cell_key(f.get("_measure", "")) for name, f in hits
                                   if name not in (facts or {}) for form in _forms(who)):
                    misnamed.append(f"{n['tok']} (next to {who})")
            row = _row_label(n) if is_table else None
            pub_srcs = [f.get("_source", "") for name, f in hits if name not in (facts or {})
                        and (row is None or any(form in _cell_key(f.get("_measure", ""))      # the row's own
                                                for form in _forms(row) | {re.split(r"[-,(]", row)[0].strip()}
                                                if len(form) >= 3))]                        # ("Miami" for "Miami-Fort ...")
            if (is_table and fhits and not _row_ok(n, fhits, facts)
                    and not (pub_srcs and _attributed(block, n, True, pub_srcs))):
                misrow.append(n["tok"])
            dhits = _dated(sent0, n, fhits) if fhits and not n.get("table") else fhits   # the sentence's year
            if (fhits and not n["neg"] and not n.get("table") and all(_value(f) > 0 for f in dhits)
                    and (any(re.search(r"(?i)\b(?:down|fell|fallen|declined|decreased|dropped|(?:an?\s+)?(?:decline|"
                                       r"drop|fall|decrease|reduction|loss)\s+of)\s+(?:by\s+)?(?:about\s+)?$",
                                       x[max(0, _at(x, n["tok"]) - 25):_at(x, n["tok"])]) for x in sent0)
                         or (all(_is_change(f) for f in dhits)
                             and any(re.match(r"(?i)\s*(?:percentage\s+points?|points?|pp|basis\s+points?)?\s*"
                                              r"(?:below|lower|less|down|fewer|smaller|under)\b",
                                              x[_at(x, n["tok"]) + len(n["tok"]):]) for x in sent0)))
                    and not any(re.search(r"(?i)declin|decreas|drop|fall|fell|loss|fewer|reduc|lower|cut|down|"
                                          r"\bgap\b|differen|shortfall|spread|margin|premium|\bminus\b|\bvs\.?\b|"
                                          r"\bbelow\b|\bbehind\b|\bshort\b",   # a size: "0.1 point below the raise"
                                          str(f.get("label", "")) + " " + str(f.get("derivation", ""))) for f in dhits)):
                unsigned.append(n["tok"])                   # "down 0.8" for a rise of 0.8
            if (fhits and not n["neg"] and not n.get("table") and all(_negative(f) for f in dhits)
                    and n["num"] != 0 and not (n["unit"] == "" and n["num"] <= 12 and n["num"] == int(n["num"]))):
                sent = [x for x in _sentences(block) if _has(x, n["tok"])] or [block]
                # the words just before the number say its direction ("Virginia rose 25,600"); else the sentence
                near = [x[max(0, _at(x, n["tok"]) - 60):_at(x, n["tok"])] for x in sent if _has(x, n["tok"])]
                # or just after it ("426 fewer pages", "0.3 points lower")
                after = [x[_at(x, n["tok"]) + len(n["tok"]):_at(x, n["tok"]) + len(n["tok"]) + 30] for x in sent
                         if _has(x, n["tok"])]
                up_near = any(UP_WORDS.search(w) and not DOWN_WORDS.search(w) for w in near)
                down = any(DOWN_WORDS.search(w) for w in near + after)
                if not down:                  # a fall word earlier in the sentence with no rise word after it
                    for x in sent:
                        pre = x[:max(_at(x, n["tok"]), 0)]
                        last = list(DOWN_WORDS.finditer(pre))
                        if last and not UP_WORDS.search(pre[last[-1].end():]):
                            down = True
                if not down and any(re.search(r"(?i)\bby\s*$", w) for w in near):   # a change ("below ... (by 0.19")
                    down = any(DOWN_WORDS.search(x) for x in sent)
                # a comparison right after the number says the direction ("grew 3.4 points less", "was 3.4 lower")
                cmp_after = any(re.match(r"(?i)\s*(?:percent(?:age)?\s+points?|points?|pp|percent|basis\s+points?)?"
                                         r"\s*(?:less|lower|below|fewer|smaller|slower|under)\b", w) for w in after)
                # a size without a direction ("within 0.01", "differ by 0.3", "a gap of 1.2") needs no fall word
                size = any(re.search(r"(?i)(?:\bwithin|\bdiffer\w*\s+by|\bdifference\s+of|\bgap\s+of|±)\s*(?:about\s+)?$", w)
                           for w in near)
                up_after = any(re.match(r"(?i)\s*(?:percent(?:age)?\s+points?|points?|pp|percent|basis\s+points?)?"
                                        r"\s*(?:above|higher|more|greater|up|faster)\b", w) for w in after)
                if (up_near and not cmp_after) or up_after or (not down and not size):   # "rose 4.2" for -4.2
                    unsigned.append(n["tok"])
            elif (fhits and not n["neg"] and not n.get("table") and n["num"] != 0
                  and all(_below0(f) and "abs" in str(f.get("fmt") or "").split(":") for f in dhits)):
                # a fall printed without its sign (fmt ...:abs) may carry no fall word, but never a rise word
                close = [x[max(0, _at(x, n["tok"]) - 25):_at(x, n["tok"])] for x in _sentences(block) if _has(x, n["tok"])]
                if any(UP_WORDS.search(w) and not DOWN_WORDS.search(w) for w in close):
                    unsigned.append(n["tok"])                # "up $70.7 billion" for a fall of 70.7
            if fhits and n["neg"] and not n.get("table") and any(
                    re.search(r"(?i)\b(?:fell|falls?|fallen|falling|declined|declines?|decreased|decreases?|dropped|"
                              r"drops?|down|lost|shr[ai]nk|shrunk)\s+(?:by\s+)?(?:about\s+|nearly\s+|almost\s+)?$",
                              x[max(0, _at(x, n["tok"]) - 30):_at(x, n["tok"])]) for x in sent0):
                unsigned.append(n["tok"])                    # "fell -8.3 percent": a fall word and a minus sign
            for flag, pat in FLAG_WORDS.items():         # every reading of this number is flagged: say so here
                # "these figures are preliminary" in the opening covers the answer; a vintage or a projection is
                # named where its number is
                near = ctx + ("\n" + lead if flag == "preliminary" else "")
                flagged = (any if flag == "preliminary" else all)(f.get(flag) for f in fhits)
                pub = [f.get("_source", "") for name, f in hits if name not in (facts or {})]
                if pub and _attributed(block, n, n.get("table", False), pub):
                    # another publisher's figure, credited ("GAO: about 1 percent"): our facts count only when they
                    # print at the number's own precision (not 1.31 read as "1")
                    exact = [f for f in fhits if ((fact_display(f) or (0, "", 0))[2] or 0) <= n.get("dec", 0)]
                    flagged = (any if flag == "preliminary" else all)(f.get(flag) for f in exact) if exact else False
                if fhits and flagged and not re.search(pat, near, re.I):
                    unlabelled.setdefault(flag, []).append(n["tok"])
    from facts import rounding_tie
    ok_ties = {str(t.get("fact") if isinstance(t, dict) else t) for t in D.get("rounding_ties_ok") or ()}
    ties = sorted(n for n in cited if n not in ok_ties and (facts[n] or {}).get("source")
                  and rounding_tie(float(facts[n]["value"]), repr(float(facts[n]["value"])), str(facts[n].get("fmt") or "")))
    out.append(("answer", not ties, "no cited figure sits on an unexplained rounding tie" if not ties else
                f"answer.md cites {', '.join(ties)}, stored on a rounding tie (one digit past the format, ending in 5): "
                "the printed figure depends on digits the CSV dropped. Save the exhibit at full precision (no .round()) "
                "and register again, or, when the value is exact at that precision (published so), list it in "
                "decisions.json \"rounding_ties_ok\": [{\"fact\": \"<name>\", \"reason\": \"...\"}]"))
    out.append(("answer", not bad, "every number in answer.md is a registered fact or allowed" if not bad else
                f"answer.md: {', '.join(dict.fromkeys(bad))} not found among the facts (as their formats show them): "
                "register each number with facts.add (a count in words such as \"three of the four\" is a number too: "
                "count it in analysis.py; a difference in percentage points takes fmt pp or pp100, in basis points bp or bp100; "
                "a number written \"44,347 thousand\" or \"$1.2 billion\" is read in base units, so a fact stored in "
                "thousands takes fmt thousands:0, and a published figure counts as benchmark.csv gives it), fix "
                "the typo, or list it in decisions.json audit_allow {\"value\", \"reason\"} when it is not a "
                "statistic (the value as written, with its unit: \"90 percent\")"))
    unsigned += _from_to_misreads("\n".join(b for b, _ in _blocks(scan)
                                           if not all(l.lstrip().startswith("|") for l in b.strip().splitlines())))
    out.append(("answer", not unsigned, "every change reads in its own direction" if not unsigned else
                f"answer.md: {', '.join(dict.fromkeys(unsigned))}: the direction words disagree with the sign of the "
                "facts they match (a negative fact needs fell, declined, down, lower, negative ... next to it; a "
                "positive one cannot be written as down or fell): give the sign or fix the direction"))
    out.append(("answer", not wrongyear, "every number dated in the text is a fact of that date" if not wrongyear
                else f"answer.md: {', '.join(dict.fromkeys(wrongyear))}: the year the text gives next to the number is "
                "not one any of the facts it matches is about (those facts are keyed to other periods): check the year "
                "or the number"))
    if unbound:
        print(f"note: answer.md: the table starting with row {', '.join(repr(u) for u in unbound)} has no fact "
              "registered by its row labels, so a value moved between rows would pass: register each row's facts "
              "with where= the row's label as printed")
    out.append(("answer", not misnamed, "every number beside a name is that name's fact" if not misnamed else
                f"answer.md: {', '.join(dict.fromkeys(misnamed))}: the number matches only facts of other "
                "entries of the same kind (another state, industry, group): check the pairing, or register the "
                "fact with where= that name"))
    out.append(("answer", not misrow, "every table cell is a fact of its own row" if not misrow else
                f"answer.md: table cells {', '.join(dict.fromkeys(misrow))} match facts of other rows only (their labels "
                "name another row): check the row, or register the cell's fact with where= the row's label"))
    miscol = [tok for block, _ in _blocks(scan) if all(l.lstrip().startswith("|") for l in block.strip().splitlines())
              for tok in _column_misfits(block, facts)]
    out.append(("answer", not miscol, "every table cell is a fact of its own column" if not miscol else
                f"answer.md: table cells {', '.join(dict.fromkeys(miscol))} hold the measure of another column (their "
                "facts' labels match that column's other cells): check the column order"))
    out.append(("answer", not unattributed, "every published figure is credited to its source" if not unattributed
                else f"answer.md: {', '.join(dict.fromkeys(unattributed))} match only a published figure in "
                "benchmark.csv, not one of this analysis's facts: say whose figure it is (\"BLS publishes ...\", a "
                "\"Published\" column), or cite the computed fact"))
    body = _norm(text)
    opening = _norm("\n".join(b for b, _ in _blocks(text)[:2]))
    blocking = {q.get("id") for q in D.get("questions") or [] if isinstance(q, dict) and q.get("blocking")
                and not q.get("asked", False)}
    for key, line in (D.get("disclosures") or {}).items():
        if key in blocking and _norm(line) in body and _norm(line) not in opening:
            out.append(("disclosures", False, f"{key}: a blocking question decided without the user: put its "
                                              "disclosure in the answer's opening (title and first paragraph)"))
        ok = _norm(line) in body
        out.append(("disclosures", ok, f"{key}: carried in answer.md" if ok else
                    f"{key}: answer.md must carry this disclosure word for word: {line}"))
    for flag, toks in unlabelled.items():
        out.append(("answer", False, f"answer.md cites {flag} values ({', '.join(dict.fromkeys(toks))}) without "
                                     "saying so in the opening paragraph or next to them (same paragraph, or the "
                                     "table and its caption)"))
    answer_toks = answer_numbers(scan)
    fact_vals = MA._fact_values(facts)
    for r in bench_rows:                  # a gap beyond rounding on a cited figure: the answer says it and why
        c, pb = MA.bench_number(r.get("computed")), MA.bench_number(r.get("published"))
        cited = c and MA._is_fact(c[0], max(c[1], abs(c[0]) * 0.005), fact_vals)
        says = next((str(r[k]).strip().lower() for k in MA._SAYS if r.get(k)), "")
        if c and pb and cited and abs(c[0] - pb[0]) > max(pb[1] * 1.0001, abs(pb[0]) * 1e-6) + 1e-12 and not (
                says.startswith("yes") and MA.rounding_gap(c, pb, r.get("published"), r.get("note") or r.get("notes"))):
            ok = any(_matches(n, pb[0], "") or abs(n["num"] - abs(pb[0])) <= n["tol"] * 1.0001 + 1e-12
                     for n in answer_toks)
            out.append(("answer", ok, f"benchmark {r.get('measure', '')}: the published {r.get('published')} is in "
                                      "answer.md" if ok else
                        f"benchmark {r.get('measure', '')} ({r.get('period', '')}): computed {r.get('computed')} "
                        f"differs from the published {r.get('published')}; answer.md must give the published figure "
                        f"and why ({str(r.get('note') or '').strip()[:80]})"))
    out.append(check_dollar_basis(text, D))
    if any("not checked" not in str(r.get("published", "")).lower() for r in bench_rows):
        ok = bool(BENCH_WORDS.search(text))
        out.append(("answer", ok, "answer.md reports the benchmark check" if ok else
                    "answer.md does not report the benchmark check: say which published figure each headline was "
                    "compared with and whether it matches (exhibits/benchmark.csv)"))
    return out, text


def audit(out: str, online: bool = False, rerun: bool = False) -> list[tuple]:
    base = os.path.abspath(out)
    results: list[tuple] = []

    def res(check, ok, msg):
        results.append((check, bool(ok), msg))

    if not os.path.exists(os.path.join(base, "decisions.json")):
        res("questions", False, "decisions.json missing: record the questions, literature searched, references "
                                "and assumptions (scripts/report/method_audit.py)")
    D = MA.merged(base)

    fpath = os.path.join(base, "facts.json")
    facts = json.load(open(fpath, encoding="utf-8")) if os.path.exists(fpath) else {}
    if not facts:
        res("facts", False, "facts.json missing or empty: register every number the answer cites (facts.py)")
    for name, f in sorted(facts.items()):
        res("facts", *MA.check_fact(name, f or {}, base))

    ans, answer_text = check_answer(base, facts, D)
    results += ans
    results += MA.check_method(D, base, facts)
    results += MA.check_benchmark(base, facts)

    ids = PS.used_sources(base, {})
    try:
        policies = PS.load_policies(ids)
    except PS.SourceError as e:
        res("vintage", False, str(e))
        policies = {}
    if not ids:
        res("vintage", False, "no source in data/<id>/: fetch the data with fetch.py --out " + base)
    for sid, P in policies.items():             # each source's required statements, in the answer itself
        for status, hid, msg in pol.run_audit_hooks(P, base, None, facts, html=answer_text):
            res("policy", status == "PASS", f"{sid} hook {hid}: {msg.replace('report mentions', 'answer.md mentions')}")
    for sid in policies:
        rel, _ = PS.provenance(base, sid, {})
        res("vintage", bool(rel), f"{sid}: provenance {rel}" if rel else
            f"{sid}: no provenance file in data/{sid}/ (request.json): the retrieval date cannot be stated")

    pub_urls = []
    for name, ok, msg, urls_here in check_published(base):
        res("vintage", ok, msg)
        pub_urls += urls_here
    bp = os.path.join(base, "exhibits", "benchmark.csv")       # the published figures' pages are links too
    bench_urls = [str(r.get("url") or "") for r in (MA._read_csv(bp)[1] if os.path.exists(bp) else [])] + pub_urls
    urls = sorted({u.rstrip(".,;:") for s in list(AR.all_strings(D)) + bench_urls
                   for u in AR.find_urls(s)})
    for u in urls:
        res("links", *(AR.check_url(u) if online else (True, f"{u}: not requested (use --online to verify)")))

    hits, _ = scan_secrets.scan_tree([base], scan_secrets.known_keys(),
                                     hex32=bool(scan_secrets.echo_sources(policies)))
    res("secrets", not hits, "no API key or credential in any output file" if not hits else "; ".join(hits[:10]))

    if rerun:
        exdir = os.path.join(base, "exhibits")
        tracked = [os.path.join(exdir, f) for f in sorted(os.listdir(exdir)) if f.endswith(".csv")] \
            if os.path.isdir(exdir) else []
        results += MA.rerun(base, fpath, tracked, K.redact)
    return results


def check_published(base: str) -> list[tuple]:
    """Tables read by hand from an agency page (a dashboard, a PDF table) that no pack serves: each row of
    published/<name>.csv names its page (source_url) and the date it was read (accessed)."""
    d = os.path.join(base, "published")
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.lower().endswith(".csv"):
            continue
        head, rows = MA._read_csv(os.path.join(d, f))
        missing = [c for c in ("source_url", "accessed") if c not in head]
        blank = sum(1 for r in rows if not str(r.get("source_url") or "").startswith("http")
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("accessed") or "").strip()))
        ok = bool(rows) and not missing and not blank
        msg = (f"published/{f}: {len(rows)} rows read from agency pages, each with its source_url and accessed date"
               if ok else f"published/{f}: every row needs source_url (the page) and accessed (YYYY-MM-DD)"
               + (f"; missing columns {missing}" if missing else f"; {blank} row(s) lack them" if rows else "; no rows"))
        out.append((f, ok, msg, sorted({str(r.get("source_url") or "") for r in rows})))
    return out


def main(argv=None) -> int:
    for _s in (sys.stdout, sys.stderr):       # Windows pipes default to cp1252
        if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="the analysis folder (holds decisions.json, facts.json, analysis.py)")
    ap.add_argument("--online", action="store_true", help="also request every external link")
    ap.add_argument("--rerun", action="store_true", help="re-run analysis.py and compare facts.json")
    a = ap.parse_args(argv)
    results = audit(a.out, a.online, a.rerun)
    fails = [r for r in results if not r[1]]
    for check_name in CHECKS:
        rs = [r for r in results if r[0] == check_name]
        if not rs:
            continue
        bad = [r for r in rs if not r[1]]
        print(f"[{'FAIL' if bad else 'PASS'}] {check_name}: {len(rs) - len(bad)}/{len(rs)} ok")
        if check_name == "benchmark" and not bad:
            for r in rs:                       # the row counts, not just "1/1"
                print(f"    {K.redact(r[2])}")
        for r in bad:
            print(f"    - {K.redact(r[2])}")
    ap_ = os.path.join(a.out, "answer.md")
    low = open(ap_, encoding="utf-8-sig").read().lower() if os.path.exists(ap_) else ""
    for r in results:                          # what the answer must disclose
        if r[1] and ("disclose" in r[2] or "opening paragraph of the answer" in r[2]):
            line = r[2].split("disclose in the answer: ", 1)[-1].strip().lower() if "disclose in the answer: " in r[2] else ""
            if line and line in low:
                continue                       # already there
            if "opening paragraph of the answer" in r[2] and low and not any(
                    not x[1] for x in results if x[0] == "disclosures"):
                continue                       # the disclosures check found it in the opening already
            print(f"note: {r[2]}")
    print("AUDIT " + ("PASSED" if not fails else f"FAILED ({len(fails)} problems)"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""Check the prose in report.json against the style guide. Results print to the terminal only;
nothing is written into the report.

Usage: python style_check.py report.json [--json] [--min 90]

Lints both modes for common house-style errors (references/style_guide.md). One rule set for every
source pack: the Roman-numeral exceptions for "I" (Title I, level I,
Schedule I), U.S.C./C.F.R. citations, and dates, quarters, fiscal years, titles, levels and codes before a
small numeral are allowed.
Score = percent of sentences with no issue. Target: >= 90.

Exit code 0 when the score meets --min (default 90), else 1.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)

# ---------------------------------------------------------------- text collection
ABBREV = ["U.S.C.", "C.F.R.", "U.S.", "e.g.", "i.e.", "vs.", "etc.", "No.", "Dr.", "Mr.", "Ms.", "Jan.", "Feb.", "Aug.",
          "Sept.", "Oct.", "Nov.", "Dec.", "a.m.", "p.m.", "approx.", "Inc.", "Jr.", "St."]


def strip_md(s: str) -> str:
    s = re.sub(r"\{\{[^}]*\}\}", "X", s)                 # {{fact|fmt}} placeholders stand for one number
    s = re.sub(r"`[^`]*`", "X", s)                       # code spans count as one technical name
    s = re.sub(r"\[\^\d+\]", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.replace("**", "").replace("*", "")


def paragraphs(R: dict) -> list[tuple[str, str]]:
    """(where, paragraph) pairs of prose that the style rules apply to."""
    out = []

    def add(where, text):
        if isinstance(text, list):
            for t in text:
                add(where, t)
            return
        for block in re.split(r"\n\s*\n", str(text or "").strip()):
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if lines and all(l.startswith(("- ", "* ")) for l in lines):
                for l in lines:
                    out.append((where, strip_md(l[2:])))
            elif lines:
                out.append((where, strip_md(" ".join(lines))))
    def lead_text(s):
        return (s.get("lead", "") + " " + s.get("text", "")) if isinstance(s, dict) else s
    add("subtitle", R.get("subtitle"))
    for s in R.get("summary") or []:
        add("summary", lead_text(s))
    g = R.get("at_a_glance") or {}
    add("at a glance", g.get("text"))
    for s in g.get("findings") or []:
        add("at a glance", lead_text(s))
    add("notes page", R.get("notes_page"))
    intro = R.get("introduction") or {}
    add("introduction", intro.get("text"))
    for x in intro.get("subsections") or []:
        add(f"introduction '{x.get('heading', '')}'", x.get("body"))
    add("introduction footnotes", intro.get("footnotes"))
    for s in R.get("sections") or []:
        add(f"section '{s.get('heading', '')}'", s.get("body"))
        add(f"section '{s.get('heading', '')}' footnotes", s.get("footnotes"))
    for i, e in enumerate(R.get("exhibits") or [], start=1):
        add(f"exhibit {i} text", e.get("text"))
        add(f"exhibit {i} notes", e.get("notes"))
    add("methods", R.get("methods"))
    for a in R.get("appendixes") or []:
        add(f"appendix '{a.get('title', '')}'", a.get("body"))
    add("footnotes", R.get("footnotes"))
    add("about", R.get("about"))
    return [(w, p) for w, p in out if p.strip()]


def sentences(par: str) -> list[str]:
    t = par
    for i, a in enumerate(ABBREV):
        t = t.replace(a, f"\x00{i}\x00")
    parts = re.split(r"(?<=[.!?;:])\s+(?=[A-Z0-9\"“($])", t)
    res = []
    for p in parts:
        for i, a in enumerate(ABBREV):
            p = p.replace(f"\x00{i}\x00", a)
        if p.strip():
            res.append(p.strip())
    return res


def words(sent: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9$%][\w$%’'.,/-]*", sent) if re.search(r"\w", w)]


# ---------------------------------------------------------------- style guide rules
STYLE_RULES = [
    (r"\d\s?%", "use 'percent' in text, not '%'"),
    (r"\bcompared to\b", "'compared with' for differences"),
    (r"\butiliz", "'use', not 'utilize'"),
    (r"\bimpact(?:s|ed|ing)?\b(?=\s+(?:the|their|its|on)\b)", "avoid 'impact' as a verb; use 'affect'"),
    (r"\bdata (?:is|was|shows|suggests|indicates)\b", "'data' takes a plural verb"),
    (r"^\d", "do not start a sentence with a numeral; spell it out or recast"),
    (r"\b(?:e\.g\.|i\.e\.|etc\.)", "write 'for example' / 'that is'; avoid 'etc.' in text"),

    (r"\bin order to\b", "'to', not 'in order to'"),
    # "I" as a pronoun, not the Roman numeral ("Executive Schedule level I", "Title I", "Phase I")
    (r"\b(?:[Ww]e|[Oo]urs?|us|[Yy]ou|[Yy]ours?)\b|(?<!level )(?<!Level )(?<!Title )(?<!Part )(?<!Phase )(?<!Tier )"
     r"(?<!schedule )(?<!Schedule )(?<!Appendix )(?<!Volume )(?<!War )(?<!Class )(?<!Category )\bI\b(?![.-]\w)",
     "no first or second person; name the organization or write 'this analysis'"),

    (r"!", "no exclamation marks"),
    (r"(?<![\d./-])\b\d{1,4}\s?-\s?\d{1,4}\b(?![-./\d])", "ranges: 'from X to Y' or an en dash (2013–2023), never a hyphen"),
    (r"\baged\s+\d", "'ages 25 to 54', not 'aged'"),
    (r"\bpersons\b", "'people', not 'persons'"),
    (r"\bgender\b", "'sex' for survey classifications"),
    (r"\b(?:fell|rose|increased|decreased|declined|grew|changed)\s+due to\b", "'because of' after a verb; 'due to' only after a noun"),
    (r"\bper cent\b", "'percent'"),
    (r"\bimpact(?:s|ed)?\s+(?:the|their|its|on)\b", "never 'impact' as a verb; use 'affect'"),
    (r"(?i)\b(?:he or she|his or her|him or her)\b", "avoid 'he or she'; make it plural, rewrite, or use singular 'they'"),
    (r"(?i)\btimes (?:less|lower|smaller|fewer|more|higher|larger|greater)\b", "not 'times less/more'; write 'one-third as large' or 'six times as much as'"),
    (r"(?i)\b(?:appears?|appeared|seems?|seemed) to\b", "avoid vague 'appear to' / 'seem to'; state what the data show"),
    (r"(?i)\baround\s+\$?\d", "'about' or 'roughly', not 'around'"),
    (r"(?i)\bpercentage points? of\b", "never 'percentage points of'; write 'by 2 percentage points' or 'by 2 percent of GDP'"),
    (r"(?i)\b(?:fairly|quite|somewhat)\b|\brather\b(?!\s+than)", "cut vague modifiers ('fairly', 'quite', 'rather', 'somewhat')"),
    (r"(?i)\b(?:prior|subsequent) to\b", "'before' / 'after'"),
    (r"(?i)\b(?:regarding|concerning|upon)\b", "'about' or 'on' (plain English)"),
    (r"(?i)\bthe elderly\b|\belderly (?:people|adults|population)\b", "give the age range ('people age 65 or older'), not 'elderly'"),
    (r"\b(?:Blacks|Whites)\b", "'Black' and 'White' are adjectives ('Black workers'), never nouns for people"),
    (r"\b(?:black|white) (?:people|workers|men|women|adults|children|residents|households|families|respondents)\b", "capitalize 'Black' and 'White' as racial designations"),
    (r"\bAmericans\b", "'U.S. residents' (or 'U.S. citizens'), not 'Americans'"),
    (r"\breform\b", "'reform' is not neutral; use 'change' or 'restructure'"),
    (r"(?i)\b(?:full|part) time\b", "'full-time' / 'part-time' are always hyphenated"),
    (r"(?i)\band/or\b", "'X, Y, or both', not 'and/or'"),
    (r"(?i)\bdata set\b", "'dataset' (one word)"),
    (r"(?<=\s)Internet\b", "'internet' is lowercase"),
    (r"\bthe Fed\b", "'the Federal Reserve', not 'the Fed'"),
    (r"(?i)\banalytic\b", "'analytical', not 'analytic'"),
    (r"(?i)\bmethodology\b", "'method' or 'approach', not 'methodology'"),
    (r"(?i)\blion's share\b", "avoid 'lion's share'; give the share"),
    (r"(?i)\bbetween (?:19|20)\d\d and (?:19|20)\d\d\b", "date ranges: 'from 2013 to 2023', not 'between ... and'"),
    (r"(?i)\bfrom (?:19|20)\d\d\s?[–-]\s?(?:19|20)\d\d\b", "do not mix 'from' with a dash; 'from 2013 to 2023'"),
]
SMALL_NUM = re.compile(r"(?<![\w$.,–-])([1-9])(?![\w.,%–-])"
                       r"(?!\s*(?:percent|percentage|million|billion|trillion|to\s+1|U\.S\.C\.|C\.F\.R\.))")
REF_BEFORE = re.compile(r"(?:(?:Exhibits?|Figures?|Tables?)(?:\s+\d+\s*(?:,?\s*(?:and|to)|[–,-]))*|Appendix|Box|Chapter|"
                        r"ages?|age|grade|Grade|Section|step|[Nn]o\.|group|code|level|[Tt]itle|[Pp]art|Q|FY|"
                        r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
                        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?)\s*$")


def style_issues(sent: str) -> list[str]:
    out = []
    for pat, msg in STYLE_RULES:
        if msg and re.search(pat, sent):
            out.append(msg)
    for m in SMALL_NUM.finditer(sent):
        if not REF_BEFORE.search(sent[: m.start()]):
            out.append(f"spell out '{m.group(1)}' (zero through nine in words, unless percent, money, or a reference)")
            break
    if len(words(sent)) > 40:
        out.append(f"{len(words(sent))} words: consider splitting (prefer plain, direct sentences)")
    return out


# ---------------------------------------------------------------- scoring
TARGET = 90


def score(R: dict) -> tuple[float, int, int, list[dict]]:
    """(score %, sentences, compliant, issue rows) for the report's prose."""
    rows, total, clean = [], 0, 0
    for where, par in paragraphs(R):
        for s in sentences(par):
            iss = style_issues(s)
            total += 1
            clean += not iss
            if iss:
                rows.append({"where": where, "sentence": s, "issues": iss})
    return (round(100 * clean / total, 1) if total else 100.0), total, clean, rows


# ---------------------------------------------------------------- main
def main(argv=None):
    for _s in (sys.stdout, sys.stderr):       # Windows pipes default to cp1252; prose may hold "−" or "≥"
        if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min", type=float)
    a = ap.parse_args(argv)
    R = json.load(open(a.report, encoding="utf-8"))
    target = a.min if a.min is not None else TARGET
    sc, total, clean, rows = score(R)
    res = {"mode": R.get("mode", "report"), "standard": "Style guide",
           "sentences": total, "compliant": clean, "score": sc, "target": target,
           "pass": sc >= target, "issues": rows}
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{res['standard']}: {clean}/{total} sentences compliant = {sc}% (target {target}%) "
              f"-> {'PASS' if res['pass'] else 'REVISE'}")
        for r in rows:
            print(f"- [{r['where']}] {'; '.join(r['issues'])}\n    {r['sentence'][:160]}")
    sys.exit(0 if res["pass"] else 1)


if __name__ == "__main__":
    main()

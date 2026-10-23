"""Formatting of registered facts for prose (standard library only; shared by facts.py, build_report and
audit_report). One version for every source pack: every format code the packs use.

A format string is  [prefix:]... kind[:digits][:abs]    e.g. "pct:1", "signed:pct100:1", "pct:1:abs",
"from_millions:dollars_b:1". digits may be negative (round to tens, hundreds ...); default 0.

Kinds (value as stored -> text):
  raw          value as given: integral numbers without decimals or grouping (2015.0 -> "2015"), other floats
               in their shortest form without binary noise (0.1 + 0.2 -> "0.3"), text unchanged
  pct:0        0.8734  -> "87 percent"            share stored as 0-1
  pct100:1     87.34   -> "87.3 percent"          already in percent (FRED and BLS rates: 4.2)
  pp:1         0.038   -> "3.8 percentage points" difference of two shares (0-1), absolute value
  pp100:1      3.8     -> "3.8 percentage points" already in points, absolute value
  bp:0         0.0025  -> "25 basis points"       difference of two rates stored as 0-1, absolute value
  bp100:0      0.25    -> "25 basis points"       difference of two rates already in percent, absolute value
  dollars:0    80612   -> "$80,612"; dollars:-2 -> "$80,600"; dollars:2 34.3 -> "$34.30"
  num:0        25812   -> "25,812"; in reports whole numbers 0-9 are spelled out ("seven") as the style guide
               requires; mode="answer" (facts.json text) keeps digits, which the answer gate can check
  thousands:-3 254     -> "254,000"               value in thousands -> the full number
  millions:1   25.8e6  -> "25.8 million"          a count in units (no dollar sign)
  billions:1   25.8e9  -> "25.8 billion";  trillions:1  29.4e12 -> "29.4 trillion"
  kmillions:1  159044  -> "159.0 million"         value in thousands (CES payrolls, CPS levels)
  usd_m:0      1.83e9  -> "$1,832 million"        value in DOLLARS (aliases: dollars_m)
  usd_b:1      1.83e12 -> "$1,832.8 billion"      value in DOLLARS (aliases: dollars_b)
  usd_t:2      3.62e13 -> "$36.21 trillion"       value in DOLLARS (aliases: dollars_t)
  usd_m_scaled / usd_b_scaled / usd_t_scaled
               1832.8  -> "$1,832.8 billion"      value ALREADY in millions / billions / trillions of dollars
  ratio:2      0.991   -> "0.99";   index:1  123.456 -> "123.5" (an index level)
  year         2015.0  -> "2015"                  a year found by the analysis (the low point)

Prefixes (combine freely, outermost first):
  signed:      "+" or "-" in front ("signed:pct100:1" 0.4 -> "+0.4 percent"); no sign when it rounds to zero
  abs:         the absolute value (same as the ":abs" suffix)
  from_thousands: / from_millions: / from_billions:
               the value is in the source's units, not base units: "from_millions:dollars_b:1" 970065 ->
               "$970.1 billion"; "from_billions:dollars_t:1" 31422.5 -> "$31.4 trillion"
Suffix:
  :abs         any numeric format of the absolute value: "fell {{chg|pct:1:abs}}" -> "fell 12.3 percent"

Conventions (where formats could be read two ways, this is the rule; tests/test_report_chain.py pins each one):
- Rounding is half away from zero on the shortest decimal form of the value (2.25 -> "2.3", 0.145 as
  pct:0 -> "15 percent"), as the statistical agencies round their tables; never Python's half-even round().
- A value that rounds to zero never prints a minus sign ("-0.0", "-$0").
- Percentage points are plural except "1 percentage point" and "0.1 percentage point" (style guide), also
  "1.0 percentage point"; basis points are plural except "1 basis point".
- Percent is always spelled out ("percent"). pp, pp100, bp, bp100 print the size of a change; the prose says
  its direction ("fell by {{x}}"). A pp value above 1 in absolute value prints a warning (use pp100).
- raw never adds digit grouping (use num for "25,812").
"""
from __future__ import annotations

import math
import re
import sys
from decimal import ROUND_HALF_UP, Decimal

WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
FROM_SCALES = {"from_thousands": 1e3, "from_millions": 1e6, "from_billions": 1e9}
PREFIXES = ("signed", "abs", *FROM_SCALES)
DOLLAR_SCALES = {"m": (10**6, "million"), "b": (10**9, "billion"), "t": (10**12, "trillion")}
ALIASES = {"dollars_m": "usd_m", "dollars_b": "usd_b", "dollars_t": "usd_t"}
KINDS = ("raw", "pct", "pct100", "pp", "pp100", "bp", "bp100", "dollars", "num", "thousands", "millions",
         "billions", "trillions", "kmillions", "usd_m", "usd_b", "usd_t", "usd_m_scaled", "usd_b_scaled",
         "usd_t_scaled", "ratio", "index", "year", *ALIASES)
# The placeholder grammar build_report and audit_report accept: {{name}} or {{name|fmt}}.
FMT_PATTERN = r"[a-z0-9_]+(?::[a-z0-9_]+)*(?::-?\d+)?(?::abs)?"


def _round(x: float, digits: int, mul: int = 1, div: int = 1) -> float:
    """x * mul / div rounded half away from zero, computed in decimal on the shortest form of x (so 0.145 * 100
    is 14.5, not 14.4999...); digits < 0 rounds to 10s, 100s ... A result of zero is never negative."""
    x = float(x)
    if not math.isfinite(x):
        return x
    d = Decimal(repr(x)) * Decimal(mul) / Decimal(div)
    r = float(d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))
    return 0.0 if r == 0 else r


def _num(x: float, digits: int, mul: int = 1, div: int = 1) -> str:
    return f"{_round(x, digits, mul, div):,.{max(digits, 0)}f}"


def _neg(x: float, digits: int, mul: int = 1, div: int = 1) -> str:
    return "-" if _round(x, digits, mul, div) < 0 else ""


def _points(n: str) -> str:
    return f"{n} percentage point{'' if float(n.replace(',', '')) in (1.0, 0.1) else 's'}"


def _raw(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.15g}"


def parse(fmt: str) -> tuple[list[str], str, int, bool]:
    """(prefixes, kind, digits, abs) of a format string; raises ValueError for anything unknown."""
    parts = [p for p in str(fmt).split(":")]
    pre = []
    while parts and parts[0] in PREFIXES:
        pre.append(parts.pop(0))
    if not parts:
        raise ValueError(f"fact format {fmt!r} has no kind")
    kind = parts.pop(0)
    if kind not in KINDS:
        raise ValueError(f"unknown fact format {fmt!r}: kind {kind!r} is not one of {', '.join(KINDS)} (a whole "
                         "number: num:0; a rate already in percent: pct100:1; a share 0-1: pct:1)")
    digits, absolute = 0, False
    if parts and re.fullmatch(r"-?\d+", parts[0]):
        digits = int(parts.pop(0))
    if parts and parts[0] == "abs":
        absolute = True
        parts.pop(0)
    if parts:
        raise ValueError(f"unknown fact format modifier {':'.join(parts)!r} in {fmt!r} (only ':digits' and ':abs')")
    return pre, ALIASES.get(kind, kind), digits, absolute


def format_value(value, fmt: str = "raw", mode: str = "report") -> str:
    """The text of a fact value in a format. mode "answer" prints 0-9 as digits; any other mode (report,
    paper) spells them out."""
    if fmt in (None, "", "raw"):
        return _raw(value)
    pre, kind, digits, absolute = parse(fmt)
    if pre:
        head, rest = pre[0], ":".join(fmt.split(":")[1:])
        v = float(value)
        if head == "signed":
            body = format_value(abs(v), rest, mode)
            if (body == "zero") if body in WORDS else not re.search(r"[1-9]", body):
                return body                                   # rounds to zero: no sign
            if body in WORDS:                                 # a signed count stays in digits: "+6", not "+six"
                body = str(WORDS.index(body))
            return ("+" if v > 0 else "-" if v < 0 else "") + body
        if head == "abs":
            return format_value(abs(v), rest, mode)
        return format_value(v * FROM_SCALES[head], rest, mode)
    if kind == "raw":
        return _raw(abs(float(value)) if absolute else value)
    v = float(value)
    if absolute:
        v = abs(v)
    if kind == "pct":
        return f"{_num(v, digits, 100)} percent"
    if kind == "pct100":
        return f"{_num(v, digits)} percent"
    if kind == "pp":
        if abs(v) > 1:
            print(f"WARNING: pp format got {v}, which would print as {_num(abs(v), digits, 100)} percentage "
                  f"points; pp expects a difference of two shares (0-1). If the value is already in points, "
                  f"use pp100.", file=sys.stderr)
        return _points(_num(abs(v), digits, 100))
    if kind == "pp100":
        return _points(_num(abs(v), digits))
    if kind in ("bp", "bp100"):
        n = _num(abs(v), digits, 10000 if kind == "bp" else 100)
        return f"{n} basis point{'' if n == '1' else 's'}"
    if kind == "dollars":
        return f"{_neg(v, digits)}${_num(abs(v), digits)}"
    if kind == "num":
        r = _round(v, digits)
        if mode != "answer" and digits <= 0 and float(r).is_integer() and 0 <= r <= 9:
            return WORDS[int(r)]
        return _num(v, digits)
    if kind == "thousands":
        return f"{_neg(v, digits, 1000)}{_num(abs(v), digits, 1000)}"
    if kind == "millions":
        return f"{_num(v, digits, 1, 10**6)} million"
    if kind == "billions":
        return f"{_num(v, digits, 1, 10**9)} billion"
    if kind == "trillions":
        return f"{_num(v, digits, 1, 10**12)} trillion"
    if kind == "kmillions":
        return f"{_num(v, digits, 1, 1000)} million"
    if kind in ("usd_m", "usd_b", "usd_t"):
        scale, word = DOLLAR_SCALES[kind[-1]]
        return f"{_neg(v, digits, 1, scale)}${_num(abs(v), digits, 1, scale)} {word}"
    if kind in ("usd_m_scaled", "usd_b_scaled", "usd_t_scaled"):
        word = DOLLAR_SCALES[kind[4]][1]
        return f"{_neg(v, digits)}${_num(abs(v), digits)} {word}"
    if kind in ("ratio", "index"):
        return _num(v, digits)
    if kind == "year":
        return str(int(_round(v, 0)))
    raise ValueError(f"unknown fact format {fmt!r}")

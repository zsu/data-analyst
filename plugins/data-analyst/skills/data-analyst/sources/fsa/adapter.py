"""Federal Student Aid Data Center library workbooks (.xls) as tidy rows.

The Data Center pages (studentaid.gov/data-center) are an Angular app with no scrapable links, but the library
files have fixed names (portfolio reports, refreshed in place) or names built from the award year and quarter
(Title IV volume by school: dl-dashboard-ay2025-2026-q4.xls, grants-ay24-25-q4.xls; verified for 2018-19 through
2025-26 in 2026-10). Only the .xls files exist (the .xlsx names return 404).

Every workbook has title rows, a two-row header and notes under the table:
  row: title ("Federal Student Aid Portfolio Summary"), source, "Data as of ..." lines
  group row: one label per block of columns (Direct Loans, In-School, 25 to 34, DL SUBSIDIZED, ...)
  measure row: Federal Fiscal Year | (quarter) | Dollars Outstanding (in billions) | Recipients (in millions) ...
               or OPE ID | School | State | Zip Code | School Type | Recipients | # of Loans Originated ...
  data rows, then footnotes.
The endpoints read each sheet from its first row ("header_row": 1), so normalize sees the whole sheet. It finds
the measure row by its first cell (a layout change fails loudly instead of shifting columns), pairs each column
with its group, and returns one row per period (or school) and group, the measures as columns named from the
header with the unit (dollars_outstanding_billions, recipients_millions, loans_originated_dollars).
Footnote marks (Total1, Recipients3, Cumulative in Default*) are dropped from labels; the published "-" and
NA** cells are empty (endpoints.json "missing"). School rows where every measure of a program is empty (the
file shows "-": no activity) are left out.
"""
from __future__ import annotations

import re
from datetime import date, datetime

FIRST_AY = 2018                  # earliest award year whose volume files carry these names (verified 2026-10)
QEND = {"Q1": (-1, 12, 31), "Q2": (0, 3, 31), "Q3": (0, 6, 30), "Q4": (0, 9, 30)}


def _err(msg):
    from core.http import DataError
    return DataError(msg, kind="api")


# ---------------------------------------------------------------------- request parameters
def _award_year(v) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{4})\s*[-/]\s*(\d{2}|\d{4})\s*", str(v or ""))
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    b = b if b > 99 else (a // 100) * 100 + b
    return (a, b) if b == a + 1 else None


def _quarter(v) -> int | None:
    m = re.fullmatch(r"\s*[qQ]?([1-4])\s*", str(v if v is not None else ""))
    return int(m.group(1)) if m else None


def check_spec(spec) -> list[str]:
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") not in ("dl_volume_by_school", "grant_volume_by_school"):
            continue
        p = r.get("params") or {}
        ay = _award_year(p.get("award_year"))
        if p.get("award_year") is not None and not ay:
            probs.append(f"request {r.get('name')!r}: award_year {p.get('award_year')!r}: write it as 2025-26 "
                         "(July 1, 2025 - June 30, 2026); a bare year is ambiguous")
        elif ay and ay[0] < FIRST_AY:
            probs.append(f"request {r.get('name')!r}: award_year {p['award_year']}: the volume-by-school files under "
                         f"these names start with {FIRST_AY}-{(FIRST_AY + 1) % 100:02d}; older years are on the Data "
                         "Center page (manual download)")
        if p.get("quarter") is not None and not _quarter(p.get("quarter")):
            probs.append(f"request {r.get('name')!r}: quarter {p.get('quarter')!r} must be 1-4 (award-year quarter)")
    return probs


def before_request(req, ep, params):
    if "{ay_long}" not in ep.get("path", "") and "{ay_short}" not in ep.get("path", ""):
        return params
    ay, q = _award_year(params.get("award_year")), _quarter(params.get("quarter"))
    if not ay or not q:
        return params                      # check_spec has said why
    names = {"ay_long": f"{ay[0]}-{ay[1]}", "ay_short": f"{ay[0] % 100:02d}-{ay[1] % 100:02d}", "q": q}
    # only the placeholders this path has: any other key would be sent as a query parameter
    return {**params, **{k: v for k, v in names.items() if "{" + k + "}" in ep["path"]}}


# ---------------------------------------------------------------------- labels
def _text(v) -> str:
    return " ".join(str(v if v is not None else "").split())


def _label(v) -> str:
    """A group label without footnote marks: 'Total1' -> 'Total', 'Cumulative in Default*' -> 'Cumulative in Default'."""
    s = _text(v).rstrip("*").strip()
    return re.sub(r"(?<=[a-z])\d$", "", s).strip()


def _measure(v) -> str:
    """'Dollars Outstanding (in billions)' -> dollars_outstanding_billions; '# of Loans Originated' ->
    loans_originated_count; '$ of Disbursements' -> disbursements_dollars; 'Sum of Recipients' -> recipients."""
    s = re.sub(r"(?<=[A-Za-z])\d+\b", "", _text(v))           # footnote numbers: Recipients3
    unit = ""
    m = re.search(r"\(\s*in\s+(billions|millions|thousands)\s*\)", s, re.I)
    if m:
        unit, s = m.group(1).lower(), s[:m.start()] + s[m.end():]
    s = re.sub(r"^(sum of|ytd)\s+", "", s.strip(), flags=re.I)
    suffix = ""
    if s.startswith("#"):
        s, suffix = re.sub(r"^#\s*(of\s+)?", "", s), "count"
    elif s.startswith("$"):
        s, suffix = re.sub(r"^\$\s*(of\s+)?", "", s), "dollars"
    elif s.lower() == "disbursements":                        # grant files: "Sum of Disbursements" is dollars
        suffix = "dollars"
    slug = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return "_".join(x for x in (slug, suffix, unit) if x)


def _slug(v) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _label(v).lower()).strip("_")


def _date(text: str) -> str:
    t = text.strip().rstrip(".")
    for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return text.strip()


# ---------------------------------------------------------------------- the sheet
def _grid(rows: list[dict]) -> list[list[str]]:
    """The sheet back as rows of cells: the header-row names (row 1) then each record's values, by position."""
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    head = ["" if re.fullmatch(r"col\d+", k) else k for k in keys]
    return [head] + [[_text(r.get(k, "")) for k in keys] for r in rows]


def _find(grid, pattern: str, what: str, url_hint: str) -> int:
    for i, row in enumerate(grid):
        if row and re.match(pattern, row[0] or "", re.I):
            return i
    raise _err(f"the workbook layout changed: no {what} header row found ({url_hint}); open the file, check the "
               "layout, and report it")


def _groups(group_row, label_row, start: int) -> list[tuple[int, str, str]]:
    """(column, group, measure) for each measure column at or after `start`; groups fill right."""
    out, cur = [], ""
    for j in range(start, len(label_row)):
        g = _label(group_row[j]) if j < len(group_row) else ""
        cur = g or cur
        if label_row[j] and cur:
            out.append((j, cur, _measure(label_row[j])))
    return out


def _period_end(fy: int, q: str) -> str:
    if not q:
        return date(fy, 9, 30).isoformat()
    dy, m, d = QEND[q]
    return date(fy + dy, m, d).isoformat()


def _portfolio(grid, hint):
    title = grid[0][0] if grid and grid[0] else ""
    li = _find(grid, r"Federal Fiscal Year", "'Federal Fiscal Year'", hint)
    if li == 0:
        raise _err(f"the workbook layout changed: no group row above the header ({hint})")
    cols = _groups(grid[li - 1], grid[li], 2)
    if not cols:
        raise _err(f"the workbook layout changed: no measure columns under the group row ({hint})")
    out, fy = [], None
    for row in grid[li + 1:]:
        y, q = (row + ["", ""])[:2]
        if re.fullmatch(r"\d{4}", y):
            fy = int(y)
        elif y:
            continue                                         # a note
        q = q.upper()
        if fy is None or (q and q not in QEND) or (not q and not y):
            continue
        by_group: dict[str, dict] = {}
        for j, g, m in cols:
            by_group.setdefault(g, {})[m] = row[j] if j < len(row) else ""
        for g, vals in by_group.items():
            if not any(v != "" for v in vals.values()):
                continue
            out.append({"report": title, "fiscal_year": str(fy), "quarter": q, "period_end": _period_end(fy, q),
                        "category": g, **vals})
    return out


def _as_of(grid, upto: int) -> str:
    for row in grid[:upto]:
        m = re.match(r"\s*Data as of\s+(.+)", row[0] or "", re.I)
        if m:
            return _date(m.group(1))
    return ""


def _snapshot(grid, hint):
    title = grid[0][0] if grid and grid[0] else ""
    li = _find(grid, r"Location\s*$", "'Location'", hint)
    names = ["location"] + [_measure(x) for x in grid[li][1:] if x]
    as_of = _as_of(grid, li)
    out = []
    for row in grid[li + 1:]:
        if not row or not row[0] or row[0].startswith("*") or not any(v != "" for v in row[1:len(names)]):
            continue
        out.append({"report": title, "as_of": as_of, **dict(zip(names, row[:len(names)]))})
    return out


def _wanted(params: dict):
    st = params.get("state")
    states = {s.strip().upper() for s in ([st] if isinstance(st, str) else (st or [])) if str(s).strip()}
    school = str(params.get("school") or "").strip().lower()
    return lambda rec: ((not states or rec.get("state", "").upper() in states)
                        and (not school or school in rec.get("school", "").lower()))


def _school(grid, hint, params):
    title = grid[0][0] if grid and grid[0] else ""
    li = _find(grid, r"OPE ID", "'OPE ID'", hint)
    group_row, label_row = (grid[li - 1] if li else []), grid[li]
    first = next((j for j in range(len(label_row)) if j < len(group_row) and _label(group_row[j])), None)
    if first is None:
        raise _err(f"the workbook layout changed: no program row above the header ({hint})")
    ids = [(j, _measure(label_row[j])) for j in range(first) if label_row[j]]
    cols = _groups(group_row, label_row, first)
    m = re.match(r"(\d{4})-(\d{4})", title)
    ay = f"{m.group(1)}-{m.group(2)[2:]}" if m else ""
    activity, start, end = "", "", ""
    for row in grid[:li]:
        t = row[0] or ""
        if "Quarterly Activity" in t:
            activity = "quarter"
            d = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", t)
            start, end = (_date(d[0]), _date(d[1])) if len(d) == 2 else ("", "")
        elif "Cumulative" in t:
            activity = "award year to date"
            d = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", t)
            end = _date(d[-1]) if d else ""
            start = f"{m.group(1)}-07-01" if m else ""
    as_of = _as_of(grid, li)
    keep = _wanted(params)
    out = []
    for row in grid[li + 1:]:
        if not row or not row[0] or not any(v != "" for v in row[first:]):
            continue
        rec = {name: (row[j] if j < len(row) else "") for j, name in ids}
        if not keep(rec):
            continue
        by_group: dict[str, dict] = {}
        for j, g, mname in cols:
            by_group.setdefault(g, {})[mname] = row[j] if j < len(row) else ""
        for g, vals in by_group.items():
            if any(v != "" for v in vals.values()):
                out.append({"award_year": ay, "activity": activity, "period_start": start, "period_end": end,
                            "as_of": as_of, **rec, "program": g, **vals})
    return out


def normalize(req, ep, rows):
    if not rows:
        return rows
    layout = ep.get("layout")
    if layout not in ("portfolio", "snapshot", "school"):
        return rows
    grid = _grid(rows)
    hint = f"{ep.get('path', '')}, sheet {(req.get('params') or {}).get('sheet') or ep.get('sheet', 1)}"
    if layout == "portfolio":
        return _portfolio(grid, hint)
    if layout == "snapshot":
        return _snapshot(grid, hint)
    return _school(grid, hint, req.get("params") or {})


def request_note(req, ep, params):
    if ep.get("layout") == "school":
        return (f"{req['name']}: award-year quarter {params.get('quarter')} of {params.get('award_year')} "
                "(award years run July 1-June 30: quarter 1 = Jul-Sep, 4 = Apr-Jun); one row per school and "
                "program with any activity; recipients are counted per program (do not add them across programs)")
    if ep.get("layout") == "portfolio":
        return (f"{req['name']}: federal fiscal-year quarters (Q1 ends Dec 31 of the prior calendar year, Q4 ends "
                "Sep 30); dollars outstanding are principal plus interest in billions, recipients in millions")
    return None

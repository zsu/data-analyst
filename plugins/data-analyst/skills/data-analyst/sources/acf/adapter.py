"""acf adapter: HHS Administration for Children and Families program data (TANF and MOE financial data, TANF
caseload data, CCDF expenditure data). Manual route only.

acf.gov (acf.hhs.gov redirects there) answers every non-browser request, including direct .xlsx links, with an AWS
WAF JavaScript challenge: HTTP 202, an empty body and the header x-amzn-waf-action: challenge (verified 2026-10-30).
The pack never works around it: refused_hosts sends --check to the manual bundle, fetch raises
DataError(kind="blocked"), manual_items lists the acf.gov page and workbook links to open in a browser, and
parse_upload reads the saved workbook.

File names change from year to year (no pattern), so endpoints.json lists the ones verified from Internet Archive
captures of the acf.gov pages; for other years the manual item is the year's page (page patterns in
endpoints.json "pages") and the user saves the workbook linked there.

Every sheet is read the same way (tidy()): the first row with a text label and a number is the first data row;
the rows above it with two or more cells (or one short cell over a value column, e.g. "State Share of
Expenditures") are the column names, a group label spread over the columns to its right; single cells above them
are titles. A header that repeats its first column name further right (the caseload fiscal/calendar-year
averages: two side-by-side blocks, each starting with "State") is split into blocks stacked as rows with a
`block` column holding the block's group label. A single text cell between data rows is the `section` of the
rows below it; a row of two or more text cells between data rows is a second table's header and renames the
columns of the rows below it (CCDF Table 1b); text rows after the last data row are notes (not written). A month
header row stored as Excel dates is a header, not data. U+FFFD in a cell is read as a space. Month columns
stored as Excel dates (45566) or as "Oct-20" become "2024-10". Values are as published except: floats in their shortest form
(3162142819.8699999 -> 3162142819.87), a cell holding only "-" (data inapplicable) is empty. A `state_code`
column (USPS, US for the national total) follows the label column when the labels are states.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

REFUSED = ("acf.gov (and acf.hhs.gov, which redirects there) answers every script, direct .xlsx links included, "
           "with an AWS WAF JavaScript challenge (HTTP 202, empty body, x-amzn-waf-action: challenge; verified "
           "2026-10-30), so ACF files cannot be downloaded from here: open the links in a web browser, save the "
           "workbook and upload it")
EPS_NAMES = ("tanf_financial", "tanf_caseload", "ccdf_expenditures")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]

_EPS: dict | None = None


def _eps(pack=None) -> dict:
    global _EPS
    if pack is not None and getattr(pack, "endpoints", None):
        return pack.endpoints
    if _EPS is None:
        _EPS = packfiles.read_json(HERE, "endpoints.json")
    return _EPS


# ---------------------------------------------------------------------- cells
NUM = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def _num(v: str) -> str:
    """Shortest exact text of a stored number (Excel keeps 15 significant digits)."""
    try:
        d = Decimal("%.15g" % float(v)) if ("." in v or "e" in v.lower()) else Decimal(v)
    except (InvalidOperation, ValueError):
        return v
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def _cell(v) -> str:
    if v is None:
        return ""
    s = " ".join(str(v).replace("\xa0", " ").replace("�", " ").split())   # U+FFFD: a lost space or quote
    if s in ("-", "–", "—"):
        return "-"
    return _num(s) if NUM.fullmatch(s) else s


def _isnum(s: str) -> bool:
    return s == "-" or bool(NUM.fullmatch(s))


def _excel_date(s: str):
    if re.fullmatch(r"\d{5}(\.0+)?", s) and 20000 <= float(s) <= 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))
    return None


def _month_header(s: str) -> str:
    """'45566' (an Excel date) or 'Oct-20' / 'October 2020' -> '2024-10'; anything else unchanged."""
    d = _excel_date(s)
    if d:
        return f"{d.year}-{d.month:02d}"
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?[- ]'?(\d{2}|\d{4})", s)
    if m:
        mon = next((i for i, n in enumerate(MONTHS) if n.startswith(m.group(1).lower())), None)
        if mon is not None:
            y = int(m.group(2))
            y = y + 2000 if y < 100 else y
            return f"{y}-{mon + 1:02d}"
    return s


# ---------------------------------------------------------------------- states
_STATE_FN = []
STATE_ALIASES = {"dist of columbia": "DC", "district of columbia": "DC", "virgin islands": "VI",
                 "u s total": "US", "u s totals": "US", "u s": "US", "total": "US", "national total": "US",
                 "united states": "US"}


def _state_fn():
    if not _STATE_FN:
        try:
            sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts", "analysis"))
            from combine import state
            _STATE_FN.append(state)
        except ImportError:                   # combine needs pandas; without it no state_code column
            _STATE_FN.append(None)
    return _STATE_FN[0]


def state_code(label: str) -> str:
    key = " ".join(re.sub(r"[. ]", " ", str(label)).lower().split())
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    fn = _state_fn()
    if fn is None or not key:
        return ""
    try:
        return fn(key, "usps")
    except ValueError:
        return ""


# ---------------------------------------------------------------------- table structure
def _cells(r: list[str]) -> list[tuple[int, str]]:
    return [(i, v) for i, v in enumerate(r) if v != ""]


def _month_serials(vals: list[str]) -> bool:
    """A header of month columns stored as Excel dates: whole numbers one month (28-31 days) apart."""
    if len(vals) < 2 or not all(_excel_date(v) for v in vals):
        return False
    xs = [int(float(v)) for v in vals]
    return all(28 <= b - a <= 31 for a, b in zip(xs, xs[1:]))


def _is_data(r: list[str]) -> bool:
    c = _cells(r)
    if len(c) < 2 or _isnum(c[0][1]):
        return False
    nums = [v for _, v in c[1:] if _isnum(v)]
    return bool(nums) and not _month_serials(nums)


TITLE = re.compile(r"^(?:[A-F](?:\.\d+)?\.?:|Table \d|CHILD CARE|FISCAL YEAR|Quarter End)", re.I)


def _header_rows(grid: list[list[str]], start: int) -> list[int]:
    """Rows above the first data row that name the columns (bottom-up, contiguous)."""
    hrs = []
    i = start - 1
    while i >= 0:
        c = _cells(grid[i])
        if not c:
            if hrs:
                break
            i -= 1
            continue
        if len(c) >= 2 or (hrs and len(c) == 1 and c[0][0] > 0 and len(c[0][1]) <= 45 and not TITLE.match(c[0][1])):
            hrs.insert(0, i)
            i -= 1
            continue
        break
    return hrs


def _names(grid, hrs, width) -> tuple[list[str], list[str], list[str]]:
    """(column names, the last header row alone, group label per column). A group label (a header row above the
    last one) covers the columns to its right that have a name in the last row, until the next group label."""
    rows = [list(grid[h]) + [""] * (width - len(grid[h])) for h in hrs]
    last = rows[-1] if rows else [""] * width
    spread = []
    for r in rows[:-1]:
        out, cur = [], ""
        for c in range(width):
            if r[c]:
                cur = r[c]
            elif not last[c]:
                cur = ""
            out.append(cur)
        spread.append(out)
    names = []
    for c in range(width):
        parts = []
        for r in spread + [last]:
            if r[c] and (not parts or parts[-1] != r[c]):
                parts.append(r[c])
        names.append(" ".join(_month_header(x) for x in parts))
    return names, [_month_header(x) for x in last], (spread[-1] if spread else [""] * width)


def tidy(grid_raw: list[list], header_row=None) -> list[dict]:
    grid = [[_cell(v) for v in r] for r in grid_raw]
    n = len(grid)
    if header_row not in (None, ""):
        hrs = [int(h) - 1 for h in (header_row if isinstance(header_row, list) else [header_row])]
        if not hrs or min(hrs) < 0 or max(hrs) >= n:
            raise DataError(f"header_row {header_row} is outside the sheet's {n} rows", kind="bad_request")
        start = max(hrs) + 1
    else:
        start = next((i for i, r in enumerate(grid) if _is_data(r)), None)
        if start is None:
            raise DataError("no table found in the sheet (no row with a label and a number): pick another table "
                            "or give header_row", kind="bad_request")
        hrs = _header_rows(grid, start)
    data_idx = [i for i in range(start, n) if _is_data(grid[i])]
    if not data_idx:
        raise DataError("no data rows below the header rows (check header_row)", kind="bad_request")
    width = max(len(grid[i]) for i in data_idx + hrs)
    names, last, groups = _names(grid, hrs, width)
    section = ""
    if not hrs:                                  # a list without column names (A.5): label / value
        names = ["label"] + ["value" if c == 1 else f"value_{c}" for c in range(1, width)]
        above = _cells(grid[start - 1]) if start else []
        section = above[0][1] if len(above) == 1 and not _isnum(above[0][1]) else ""
    # side-by-side blocks: the first column's name repeated further right (each block starts with "State")
    first = last[0] if last and last[0] else ""
    starts = [0] + [c for c in range(1, width) if first and last[c] == first]   # "STATE" vs "State" (state funds)
    if len(starts) > 1:
        names = last
    out = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else width
        raw = (grid[hrs[-2]] + [""] * width)[:width] if len(hrs) > 1 else groups   # the group cells as written
        label = next((raw[c] for c in range(s, e) if raw[c]), "") if len(starts) > 1 else ""
        cols = [c for c in range(s, e) if names[c] or any(c < len(grid[i]) and grid[i][c] for i in data_idx)]

        def column_names(nms):
            out_, seen = {}, set()
            for c in cols:
                nm = nms[c] or ("label" if c == s else f"col{c + 1}")
                nm = nm if nm not in seen else f"{nm}_col{c + 1}"
                seen.add(nm)
                out_[c] = nm
            return out_
        cname = column_names(names)
        sec = section
        for i in range(start, data_idx[-1] + 1):
            r = (grid[i] + [""] * width)[:width]
            c = _cells(r[s:e])
            if not c:
                continue
            if not _is_data(r[s:e]):
                if len(c) == 1 and not _isnum(c[0][1]):
                    sec = c[0][1]                # a label row inside the table ("Carryover from Prior Years")
                elif len(c) >= 2 and not any(_isnum(v) for _, v in c):
                    cname = column_names([_month_header(x) for x in r])   # a second table below the first
                    sec = ""
                continue
            rec = {"row": i + 1}
            if label:
                rec["block"] = label
            rec["section"] = sec
            for col in cols:
                rec[cname[col]] = "" if r[col] == "-" else r[col]
            out.append(rec)
    if not any(r.get("section") for r in out):
        for r in out:
            r.pop("section", None)
    _add_state_codes(out)
    return out


def _add_state_codes(rows: list[dict]) -> None:
    if not rows:
        return
    keys = [k for k in rows[0] if k not in ("row", "block", "section")]
    if not keys:
        return
    lab = keys[0]
    codes = [state_code(r.get(lab, "")) for r in rows]
    if sum(1 for c in codes if c) < max(3, len(rows) // 2):
        return
    for r, c in zip(rows, codes):
        items = list(r.items())
        pos = [k for k, _ in items].index(lab) + 1
        items.insert(pos, ("state_code", c))
        r.clear()
        r.update(items)


# ---------------------------------------------------------------------- sheet choice
def _norm(s: str) -> str:
    return " ".join(str(s).replace("\xa0", " ").lower().split())


def _title_text(grid, rows=4) -> str:
    """The title lines of a sheet: rows near the top holding a single cell."""
    lines = []
    for r in grid[:rows]:
        c = [v for v in r if v not in (None, "") and str(v).strip()]
        if len(c) == 1:
            lines.append(str(c[0]))
    return _norm(" | ".join(lines))


def _sheet_list(sheets) -> str:
    return "; ".join(list(sheets)[:80])


def pick_sheet(endpoint: str, sheets: dict, p: dict, label: str) -> str:
    table = p.get("table")
    names = list(sheets)
    if endpoint == "tanf_caseload":
        table = table or "families"
    elif endpoint == "tanf_financial":
        table = table or "A.1"
    else:
        table = table or ("1b" if p.get("file") == "grant_year" else "4a")
    t = _norm(table)
    exact = [n for n in names if _norm(n) == t]
    if exact:
        return exact[0]
    if endpoint == "tanf_financial":
        m = re.fullmatch(r"([a-f])(?:\.?(\d+))?\.?", t)
        if m:
            code = m.group(1) + (("." + m.group(2)) if m.group(2) else "")
            hits = [n for n in names if re.match(re.escape(code) + r"(?:[ .:]|$)", _norm(n))
                    and not (not m.group(2) and re.match(re.escape(code) + r"\.\d", _norm(n)))]
            if len(hits) == 1:
                return hits[0]
        code = state_code(table)
        if code and code != "US":
            for n in names:
                if state_code(n) == code:
                    return n
    elif endpoint == "tanf_caseload":
        key = _eps()["caseload_tables"].get(t.replace(" ", "_").replace("-", "_"))
        if key:
            hits = [n for n in names if key in _title_text(sheets[n])]
            if hits:
                return hits[0]
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
        if m and 1 <= int(m.group(2)) <= 12:
            want = f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
            hits = [n for n in names if _norm(next((v for r in sheets[n][:2] for v in r if v not in (None, "")), ""))
                    == want]
            if hits:
                return hits[0]
            raise DataError(f"{label} has no sheet for {table} (the workbook covers the months named in its "
                            f"sheets: {_sheet_list(sheets)})", kind="not_found")
    else:
        m = re.fullmatch(r"(?:table\s*)?(\d+)\s*([ab])", t)
        if m:
            want = f"table {m.group(1)}{m.group(2)}"
            hits = [n for n in names if _norm(n) == want] or \
                   [n for n in names if re.search(r"\b" + want + r"\b", _title_text(sheets[n]))]
            if hits:
                return hits[0]
    sub = [n for n in names if t in _norm(n)]
    if len(sub) == 1:
        return sub[0]
    raise DataError(f"no table {table!r} in {label} (sheets: {_sheet_list(sheets)})", kind="bad_request")


# ---------------------------------------------------------------------- hooks
def _year(p, key="fiscal_year"):
    try:
        return int(p.get(key))
    except (TypeError, ValueError):
        return None


def refused_hosts(spec) -> str:
    """--check: every ACF request goes to the manual route (no request is sent to acf.gov)."""
    reqs = [r for r in spec.get("requests") or [] if isinstance(r, dict)]
    return REFUSED if reqs else ""


def fetch(spec, pack, route):
    names = ", ".join(str(r.get("name")) for r in spec.get("requests") or [])
    raise DataError(f"{REFUSED} (request(s) {names})", kind="blocked")


def check_spec(spec):
    probs = []
    this = dt.date.today().year
    for r in spec.get("requests") or []:
        p, n, e = r.get("params") or {}, r.get("name"), r.get("endpoint")
        if e not in EPS_NAMES:
            continue
        y = p.get("fiscal_year")
        if y not in (None, ""):
            yi = _year(p)
            if yi is None:
                probs.append(f"request {n!r}: fiscal_year must be a four-digit year such as 2024")
            elif not 1997 <= yi <= this + 1:
                probs.append(f"request {n!r}: fiscal_year {yi} is outside the years ACF publishes (1997 on)")
            elif e == "ccdf_expenditures" and p.get("file") == "grant_year" and yi < 2018:
                probs.append(f"request {n!r}: grant-year CCDF tables start with GY 2018 (OCC began separating "
                             "fiscal and grant years then); use file all_years")
        if e != "ccdf_expenditures" and (p.get("file") not in (None, "") or p.get("as_of") not in (None, "")):
            probs.append(f"request {n!r}: file and as_of are ccdf_expenditures parameters")
        if e == "ccdf_expenditures" and p.get("as_of") not in (None, ""):
            a = _year(p, "as_of")
            if p.get("file") != "grant_year":
                probs.append(f"request {n!r}: as_of applies only to file grant_year")
            elif a is None or (_year(p) and not _year(p) <= a <= _year(p) + 3):
                probs.append(f"request {n!r}: as_of is the year of the 9/30 report date, from the grant year to "
                             "about three years later")
        if e != "tanf_caseload" and p.get("program") not in (None, ""):
            probs.append(f"request {n!r}: program is a tanf_caseload parameter")
        hr = p.get("header_row")
        if hr not in (None, ""):
            hs = hr if isinstance(hr, list) else [hr]
            if not all(isinstance(h, int) and h >= 1 or (isinstance(h, str) and h.isdigit() and int(h) >= 1)
                       for h in hs):
                probs.append(f"request {n!r}: header_row is a 1-based row number or a list of them")
    return probs


def _item_for(r: dict, eps: dict) -> dict:
    p = r.get("params") or {}
    e = r.get("endpoint")
    y = _year(p)
    base = eps["base_url"].rstrip("/")
    pages = eps["pages"]
    url, page, what = None, None, ""
    if e == "tanf_financial":
        page = base + pages["tanf_financial"].format(year=y)
        f = eps["tanf_financial_files"].get(str(y))
        url = base + f if f else None
        what = f"the 'FY {y} TANF Financial Data Tables (XLSX)' link"
    elif e == "tanf_caseload":
        prog = p.get("program") or "tanf"
        page = base + pages["tanf_caseload"].format(year=y)
        f = (eps["tanf_caseload_files"].get(str(y)) or {}).get(prog)
        url = base + f if f else None
        label = {"tanf": "TANF Caseload", "ssp": "SSP-MOE Caseload", "tanf_ssp": "TANF & SSP-MOE Caseload"}[prog]
        what = f"the 'FY{y} {label} (XLSX)' link (the '15 Months' one when the page has two)"
    else:
        kind = p.get("file") or "all_years"
        year_page = base + (pages["ccdf_year"] if y >= 2018 else pages["ccdf_year_2011_2017"]).format(year=y)
        if kind == "all_years":
            d = eps["ccdf_files"]["all_years"].get(str(y))
            what = f"'CCDF Expenditures for FY {y} (all appropriation years)', then its XLSX link"
        else:
            byyear = eps["ccdf_files"]["grant_year"].get(str(y)) or {}
            a = str(p.get("as_of") or (max(byyear) if byyear else ""))
            d = byyear.get(a)
            what = (f"'CCDF Expenditures for GY {y} as of 9/30/{a} (Tables 1b-...)', then its XLSX link" if a else
                    f"the latest 'CCDF Expenditures for GY {y} as of 9/30/<year> (Tables 1b-...)', then its XLSX link")
        page = base + d["page"] if d else year_page
        url = base + d["file"] if d else None
        if not d:
            what += f" (from {year_page})"
    if url:
        fname = os.path.basename(url).replace("%20", " ")
        note = (f"open in a web browser (acf.gov refuses scripts, so the download script will get an empty "
                f"HTTP 202 challenge page); if the link has moved, open {page} and click {what}. Upload the .xlsx "
                "as saved.")
    else:
        fname = f"{r.get('name')}_{e}_{y}.xlsx"
        url = page
        note = (f"no verified file link for this year: open {page} in a web browser, click {what}, save the "
                "workbook and upload it (any file name when it is the only file uploaded; else rename it to "
                f"{fname}). Some older years publish only PDF or web tables, which --ingest cannot read.")
    return {"name": f"{r['name']}#1", "request": r["name"], "url": url, "filename": fname,
            "also_ext": [".xlsx", ".xls"], "needs_key": False, "method": "GET", "body": None, "note": note}


def manual_items(spec, pack):
    eps = _eps(pack)
    return [_item_for(r, eps) for r in spec.get("requests") or []
            if isinstance(r, dict) and r.get("endpoint") in EPS_NAMES and _year(r.get("params") or {})]


def _hr(p):
    hr = p.get("header_row")
    if isinstance(hr, list):
        return [int(h) for h in hr]
    return int(hr) if hr not in (None, "") else None


def parse_upload(pack, req, paths):
    p = req.get("params") or {}
    rows = []
    for path in sorted(paths):
        base = os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] == b"PK\x03\x04":
            sheets = gr.xlsx_sheets(data)
        elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            sheets = gr.xls_sheets(data)
        else:
            raise DataError(f"{base}: not an Excel workbook (a saved web page or the empty challenge page?): open "
                            "the link in a web browser and save the .xlsx itself", kind="bad_request")
        sheet = pick_sheet(req.get("endpoint"), sheets, p, base)
        got = tidy(sheets[sheet], _hr(p))
        for r in got:
            r["sheet"] = sheet
        rows += got
    return rows

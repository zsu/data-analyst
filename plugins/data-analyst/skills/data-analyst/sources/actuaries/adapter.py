"""actuaries adapter: Medicare Trustees Report and National Health Expenditure tables (CMS), Social Security
Trustees Report single-year tables (SSA, manual route only).

CMS publishes these as ZIP files linked from three pages (verified 2026-10-30, see endpoints.json "pages"):
  - Trustees Report & Trust Funds: "<year> Expanded and Supplementary Tables and Figures (ZIP)" for 2022 on; the
    file names change from year to year, so the pack reads the link from the page (endpoints.json
    "medicare_known" holds the names seen on 2026-10-30, used only for manual-download links when the page
    cannot be read). Each ZIP holds one .xlsx per table (a table sheet and a "Notes" sheet), the same tables as
    display-formatted CSV + .txt notes under CSV/ (not read: the .xlsx has the unformatted numbers), and
    "<year>TR Figures.xlsx" with one sheet per figure.
  - NHE historical and projected pages: fixed-name ZIPs (endpoints.json "nhe_downloads"); the summary and the
    type-of-service-by-source-of-funds files are CSV (with an old .xls copy that is not read), the tables are
    one .xlsx per table.
  - www.ssa.gov answers every script with HTTP 403 (Akamai "Access Denied"), so SSA requests are never fetched:
    refused_hosts sends --check to the manual route, fetch raises DataError(kind="blocked"), and parse_upload
    reads the workbook the user saves from a browser (SingleYearTRTables_TR<year>.xlsx, one sheet per table).

Every table is read the same way (tidy()): title lines above the column names and footnote lines below the data
go to the fetch notes; the column-name rows are detected (or given with header_row) and joined per column, with
merged header cells spread over their columns; a row holding a single label inside the data ("Intermediate:",
"Amount in Billions") becomes the `section` column of the rows below it; `row` is the sheet row number. Values
are as published with these exceptions, each stated in source.md: cells holding only a dash (not applicable)
are empty; CSV "$" and thousands separators are dropped; .xlsx floats are written in their shortest exact form
(74.099999999999994 -> 74.1); a cell stored as an Excel percent-format fraction (0.106 shown as 10.6%) is written
as displayed (10.6), with a note giving the count. An indented label column gets `indent` (spaces as published),
`group` (the nearest row above with no indent) and `parent` (the nearest row above with a smaller indent), and a
first column of years ("2033b" with a footnote letter) gets a numeric `year` column. A second header inside the
sheet (SSA V.A4: a historical block, then intermediate / low-cost / high-cost columns) renames the columns of the
rows below it; a header repeated on a continued page is skipped. header_row overrides the detection.
"""
from __future__ import annotations

import csv
import html
import io
import os
import re
import sys
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE_EPS = ("medicare_tr_table", "nhe_table")
SSA_REFUSED = ("www.ssa.gov answers every script with HTTP 403 (Akamai 'Access Denied' bot filter, verified "
               "2026-10-30; no API key is involved), so Social Security Trustees Report tables (ssa_tr_table) "
               "cannot be fetched from here")
MAX_NOTE_LINES = 6

_EPS: dict | None = None


def _eps(pack=None) -> dict:
    global _EPS
    if pack is not None and getattr(pack, "endpoints", None):
        return pack.endpoints
    if _EPS is None:
        _EPS = packfiles.read_json(HERE, "endpoints.json")
    return _EPS


# ---------------------------------------------------------------------- cell values
NUM = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
DASH = "\x00dash"      # a published "not applicable" dash: counts as a value while the table is read, written empty
DASHES = {"-", "\u2014", "\u2013", "\u2212", "--", "\ufffd"}


def _fmt_decimal(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def _xlsx_num(v: str, pct: bool) -> str:
    """Shortest exact text of a stored float; a percent-format cell as displayed (x100)."""
    if not NUM.fullmatch(v):
        return v
    try:
        # Excel keeps 15 significant digits (74.099999999999994 is shown as 74.1, 495.00000000000006 as 495)
        d = Decimal("%.15g" % float(v)) if ("." in v or "e" in v.lower()) else Decimal(v)
    except (InvalidOperation, ValueError):
        return v
    return _fmt_decimal(d * 100 if pct else d)


def _csv_num(v: str) -> str:
    s = v.strip()
    t = s.replace("$", "").replace(",", "").strip()
    if t and NUM.fullmatch(t) and re.search(r"\d", t) and ("," in s or "$" in s):
        return t
    return v


def _clean(v, pct: bool = False, from_csv: bool = False) -> str:
    if v is None:
        return ""
    v = str(v)
    if v.strip() in DASHES:
        return DASH
    if from_csv:
        return _csv_num(v)
    return _xlsx_num(v.strip(), pct) if NUM.fullmatch(v.strip()) else v


# ---------------------------------------------------------------------- workbook reading
def _sheet_names(data: bytes) -> list[str]:
    z = zipfile.ZipFile(io.BytesIO(data))
    wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
    return [html.unescape(n) for n in re.findall(r'<sheet\b[^>]*\bname="([^"]*)"', wb)]


def _sheet_meta(data: bytes, sheet: str) -> tuple[list[tuple[int, int, int, int]], set[tuple[int, int]]]:
    """(merged ranges as 0-based (r1, c1, r2, c2), cells stored with a percent number format) of one sheet."""
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(io.BytesIO(data))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    target = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    path = None
    for s in wb.find("m:sheets", ns):
        if s.get("name") == sheet:
            path = target[s.get(f"{{{ns['r']}}}id")].lstrip("/")
            path = path if path.startswith("xl/") else "xl/" + path
    if path is None:
        return [], set()
    pct_styles: set[int] = set()
    if "xl/styles.xml" in z.namelist():
        st = ET.fromstring(z.read("xl/styles.xml"))
        fmts = {9, 10}
        for f in st.iter(f"{{{ns['m']}}}numFmt"):
            code = re.sub(r'"[^"]*"|\\.|_.', "", f.get("formatCode") or "")
            if "%" in code:
                fmts.add(int(f.get("numFmtId")))
        xfs = st.find("m:cellXfs", ns)
        for i, xf in enumerate(xfs if xfs is not None else []):
            if int(xf.get("numFmtId") or 0) in fmts:
                pct_styles.add(i)

    def rc(ref: str) -> tuple[int, int]:
        m = re.match(r"([A-Z]+)(\d+)", ref)
        col = 0
        for ch in m.group(1):
            col = col * 26 + ord(ch) - 64
        return int(m.group(2)) - 1, col - 1
    text = z.read(path).decode("utf-8", "replace")
    merges = []
    for ref in re.findall(r'<mergeCell\b[^>]*\bref="([A-Z]+\d+:[A-Z]+\d+)"', text):
        a, b = ref.split(":")
        (r1, c1), (r2, c2) = rc(a), rc(b)
        merges.append((r1, c1, r2, c2))
    pct = set()
    if pct_styles:
        for ref, s in re.findall(r'<c\b[^>]*\br="([A-Z]+\d+)"[^>]*\bs="(\d+)"', text):
            if int(s) in pct_styles:
                pct.add(rc(ref))
        for s, ref in re.findall(r'<c\b[^>]*\bs="(\d+)"[^>]*\br="([A-Z]+\d+)"', text):
            if int(s) in pct_styles:
                pct.add(rc(ref))
    return merges, pct


def _pick_sheet(names: list[str], want, label: str) -> str:
    if want in (None, ""):
        rest = [n for n in names if n.strip().lower() != "notes"]
        return (rest or names)[0]
    if isinstance(want, int) or str(want).strip().isdigit():
        i = int(want)
        if not 1 <= i <= len(names):
            raise DataError(f"{label} has {len(names)} sheet(s), not sheet {want} (sheets: {', '.join(names)})",
                            kind="bad_request")
        return names[i - 1]
    for n in names:
        if n == want or n.strip().lower() == str(want).strip().lower():
            return n
    raise DataError(f"no sheet {want!r} in {label} (sheets: {', '.join(names)})", kind="bad_request")


def xlsx_grid(data: bytes, sheet) -> tuple[str, list[list[str]], list, int]:
    """(sheet name, cleaned grid, merges, percent cells converted) of one sheet."""
    names = _sheet_names(data)
    name = _pick_sheet(names, sheet, "the workbook")
    raw = gr.xlsx_sheets(data, {name})[name]
    merges, pct = _sheet_meta(data, name)
    grid, n_pct = [], 0
    for i, r in enumerate(raw):
        row = []
        for j, v in enumerate(r):
            p = (i, j) in pct and v is not None and bool(NUM.fullmatch(str(v).strip()))
            n_pct += p
            row.append(_clean(v, pct=p))
        grid.append(row)
    return name, grid, merges, n_pct


def csv_grid(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return [[_clean(v, from_csv=True) for v in r] for r in csv.reader(io.StringIO(text))]


# ---------------------------------------------------------------------- table structure
def _cells(r) -> list[tuple[int, str]]:
    return [(i, v) for i, v in enumerate(r) if v is not None and str(v).strip()]


def _isnum(v) -> bool:
    if v == DASH:
        return True
    return bool(NUM.fullmatch(str(v).strip()))


YEAR = re.compile(r"(1[89]|2[01])\d\d")


def _year_header(r) -> bool:
    ne = _cells(r)
    if not ne or _isnum(ne[0][1]) and ne[0][0] == 0:
        return False
    nums = [v.strip() for i, v in ne if _isnum(v)]
    if len(nums) < 3 or not all(YEAR.fullmatch(v) for v in nums):
        return False
    ys = [int(v) for v in nums]
    return all(b > a for a, b in zip(ys, ys[1:]))


YEAR_ROW = re.compile(r"(1[89]|2[01])\d\d[a-z]{0,2}")


def _is_data(r) -> bool:
    """A row of values: a label or year and at least one number, or a year alone (its values all published as
    dashes, e.g. Part D before 2004)."""
    ne = _cells(r)
    if ne and ne[0][0] == 0 and YEAR_ROW.fullmatch(str(ne[0][1]).strip()) and all(_isnum(v) for _, v in ne[1:]):
        return True
    return len(ne) >= 2 and any(_isnum(v) for i, v in ne[1:]) and not _year_header(r)


def _label(v: str) -> str:
    if v == DASH:
        return ""
    return " ".join(str(v).replace("\xa0", " ").split())


def tidy(grid: list[list[str]], merges=(), header_row=None) -> tuple[list[dict], list[str], str]:
    """Rows, notes (titles and footnotes) and the title of one published table."""
    n = len(grid)
    if header_row not in (None, ""):
        hrs = [int(h) - 1 for h in (header_row if isinstance(header_row, list) else [header_row])]
        if not hrs or min(hrs) < 0 or max(hrs) >= n:
            raise DataError(f"header_row {header_row} is outside the sheet's {n} rows", kind="bad_request")
        start = max(hrs) + 1
        titles = [_label(c[0][1]) for c in (_cells(grid[i]) for i in range(min(hrs))) if len(c) == 1]
        first_section = ""
    else:
        start = next((i for i, r in enumerate(grid) if _is_data(r)), None)
        if start is None:                      # a notes sheet: one text column
            lines = [_label(" ".join(v for _, v in _cells(r))) for r in grid]
            rows = [{"row": i + 1, "text": t} for i, t in enumerate(lines) if t]
            return rows, [], rows[0]["text"] if rows else ""
        hrs, titles, first_section = [], [], ""
        for i in range(start):
            c = _cells(grid[i])
            nums = any(_isnum(v) for _, v in c) and not _year_header(grid[i])
            if len(c) == 2 and not nums and {j for j, _ in c} == {0, 1} and \
                    any(len(_cells(grid[h])) >= 3 for h in hrs):
                first_section = _label(" ".join(v for _, v in c))      # "Year 2017 | Amount in Billions"
            elif len(c) >= 2:
                hrs.append(i)
                first_section = ""
            elif len(c) == 1:
                if hrs:
                    first_section = _label(c[0][1])
                elif c[0][0] > 0 and titles:                   # a group label over columns ("Projected")
                    hrs.append(i)
                else:
                    titles.append(_label(c[0][1]))
    width = max((len(grid[i]) for i in range(start, n)), default=0)
    width = max([width] + [len(grid[h]) for h in hrs])

    def column_names(block: list[int]) -> tuple[list[str], set]:
        head = {h: [v if _label(v) else "" for v in grid[h]] + [""] * (width - len(grid[h])) for h in block}
        for r1, c1, r2, c2 in merges:            # spread merged header cells over their columns and rows
            top = grid[r1][c1] if r1 < n and c1 < len(grid[r1]) else ""
            for h in block:
                if r1 <= h <= r2:
                    for c in range(c1, min(c2, width - 1) + 1):
                        if not head[h][c]:
                            head[h][c] = top
        if block:                                # a label over year columns ("Projected") covers the years to its right
            last = head[block[-1]]
            for h in block[:-1]:
                for c in range(1, width):
                    if not head[h][c] and head[h][c - 1] and YEAR.fullmatch(str(last[c]).strip()) \
                            and YEAR.fullmatch(str(last[c - 1]).strip()):
                        head[h][c] = head[h][c - 1]
        out, seen = [], set()
        for c in range(width):
            parts = []
            for h in block:
                p = _label(head[h][c])
                if p and (not parts or parts[-1] != p):
                    parts.append(p)
            nm = " ".join(parts) or f"col{c + 1}"
            nm = nm if nm not in seen else f"{nm}_col{c + 1}"
            seen.add(nm)
            out.append(nm)
        text = {tuple(_label(v) for v in head[h] if _label(v)) for h in block} | \
            {tuple(_label(v) for v in grid[h] if _label(v)) for h in block}
        return out, text

    names, header_text = column_names(hrs)
    order = list(names)
    rows, pending, block, section, notes = [], [], [], first_section, []
    for i in range(start, n):
        r = grid[i]
        c = _cells(r)
        if not c:
            continue
        if tuple(_label(v) for _, v in c) in header_text:       # a header repeated on a continued page
            continue
        blank_row = len(c) == 1 and c[0][0] == 0 and rows and str(c[0][1]).replace("\xa0", " ").startswith(" ")
        if not _is_data(r) and not blank_row and (len(c) == 1 or not any(_isnum(v) for _, v in c)):
            if len(c) >= 3:                     # a new header inside the sheet (a second block of columns)
                block.append(i)
            else:
                pending.append(_label(" ".join(v for _, v in c)))
            continue
        if block:
            names, text = column_names(block)
            header_text |= text
            order += [nm for nm in names if nm not in order]
            block = []
        if pending:
            section = pending[-1]
            notes += pending[:-1]
            pending = []
        rows.append((i, section, {names[j]: (r[j] if j < len(r) else "") for j in range(width)}))
    notes += pending
    names = order
    rows = [(i, sec, {nm: rec.get(nm, "") for nm in names}) for i, sec, rec in rows]
    used = [nm for nm in names if any(_label(rec[nm]) for _, _, rec in rows)] or names
    has_section = any(s for _, s, _ in rows)
    first = used[0] if used else None
    lab = None
    for cand in used[:2]:
        if any(str(rec[cand]).replace("\xa0", " ").startswith(" ") and _label(rec[cand]) for _, _, rec in rows):
            lab = cand
            break
    vals = [str(rec[first]).strip() for _, _, rec in rows if first and str(rec[first]).strip()]
    # a first column of years with footnote letters ("2033b"): add a plain `year` column
    yearcol = first if vals and all(re.fullmatch(r"(1[89]|2[01])\d\d[a-z ,]*", v) for v in vals) \
        and any(len(v) > 4 for v in vals) else None
    out, stack = [], []
    group = ""
    for i, sec, rec in rows:
        o = {"row": i + 1}
        if has_section:
            o["section"] = sec.rstrip(":").strip()
        for nm in used:
            v = rec[nm]
            if nm == lab:
                raw = "" if v == DASH else str(v).replace("\xa0", " ")
                ind = len(raw) - len(raw.lstrip(" "))
                label = _label(raw)
                scope = (sec, rec[used[0]] if lab != used[0] else "")
                stack = [(s, k, lb) for s, k, lb in stack if s == scope and k < ind]
                parent = stack[-1][2] if stack else ""
                stack.append((scope, ind, label))
                if ind == 0 and lab == used[0]:
                    group = label
                o[nm] = label
                o["indent"] = ind
                if lab == used[0]:
                    o["group"] = group if ind else label
                o["parent"] = parent
            else:
                o[nm] = _label(v) if isinstance(v, str) else v
            if nm == yearcol:
                o["year"] = _label(v)[:4]
        out.append(o)
    title = titles[0] if titles else ""
    return out, titles + notes, title


YEAR_COL = re.compile(r"(?:([A-Za-z][A-Za-z ]*?) )?((?:1[89]|2[01])\d\d)(?: ([A-Za-z][A-Za-z ]*))?")


def _year_col(name: str):
    """(year, label) of a year column ("2024", "2025 Projected", "Projected 2025"), else None."""
    m = YEAR_COL.fullmatch(name)
    if not m:
        return None
    label = " ".join(x for x in (m.group(1), m.group(3)) if x)
    return (m.group(2), label) if len(label.split()) <= 2 else None


def long_layout(rows: list[dict]) -> tuple[list[dict], bool]:
    """Melt year columns ("2024", "2025 Projected", "Projected 2025") into year / value rows."""
    if not rows:
        return rows, True
    ycols = {k: _year_col(k) for k in rows[0] if _year_col(k)}
    if not ycols:
        return rows, False
    ids = [k for k in rows[0] if k not in ycols]
    labelled = any(lab for _, lab in ycols.values())
    out = []
    for r in rows:
        for k, (year, lab) in ycols.items():
            o = {i: r[i] for i in ids}
            o["year"] = year
            if labelled:
                o["year_label"] = lab
            o["value"] = r[k]
            out.append(o)
    return out, True


# ---------------------------------------------------------------------- ZIP contents
def zip_tables(data: bytes) -> list[tuple[str, str]]:
    """(member, kind) of the tables in a ZIP: .xlsx members, and .csv members that have no .xlsx twin."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DataError("expected a .zip file, got something else (an HTML page?)", kind="api")
    names = [n for n in z.namelist() if not n.endswith("/")]
    stems = {os.path.splitext(os.path.basename(n))[0].lower() for n in names if n.lower().endswith(".xlsx")}
    out = []
    for n in sorted(names, key=lambda s: (s.count("/"), s.lower())):
        low = n.lower()
        if low.endswith(".xlsx"):
            out.append((n, "xlsx"))
        elif low.endswith(".csv") and os.path.splitext(os.path.basename(n))[0].lower() not in stems:
            out.append((n, "csv"))
    return out


def _norm(s: str) -> str:
    return " ".join(re.sub(r"\.(xlsx|csv)$", "", os.path.basename(str(s)), flags=re.I).lower().split())


def pick_member(tables: list[tuple[str, str]], want, label: str) -> tuple[str, str]:
    if not tables:
        raise DataError(f"{label} holds no .xlsx or .csv table", kind="api")
    if want in (None, ""):
        if len(tables) == 1:
            return tables[0]
        raise DataError(f"{label} holds {len(tables)} tables: name one with \"table\" (" +
                        "; ".join(_norm(m) for m, _ in tables[:40]) + ")", kind="bad_request")
    q = _norm(want)
    exact = [t for t in tables if _norm(t[0]) == q]
    if exact:
        return exact[0]
    pre = [t for t in tables if _norm(t[0]).startswith(q) and (len(_norm(t[0])) == len(q) or _norm(t[0])[len(q)] == " ")]
    if len(pre) == 1:
        return pre[0]
    sub = [t for t in tables if q in _norm(t[0])]
    if len(sub) == 1:
        return sub[0]
    hits = pre or sub
    if hits:
        raise DataError(f"table {want!r} matches {len(hits)} files in {label}: " +
                        "; ".join(_norm(m) for m, _ in hits[:20]) + ". Give more of the name.", kind="bad_request")
    raise DataError(f"no table {want!r} in {label} (tables: " + "; ".join(_norm(m) for m, _ in tables[:40]) + ")",
                    kind="bad_request")


def read_member(data: bytes, member: str, kind: str, sheet=None) -> tuple[str, list[list[str]], list, int]:
    z = zipfile.ZipFile(io.BytesIO(data))
    raw = z.read(member)
    if kind == "csv":
        if sheet not in (None, "", 1, "1"):
            raise DataError(f"{os.path.basename(member)} is a CSV file: it has no sheets", kind="bad_request")
        return "", csv_grid(raw), [], 0
    return xlsx_grid(raw, sheet)


def list_zip(data: bytes) -> list[dict]:
    rows = []
    z = zipfile.ZipFile(io.BytesIO(data))
    for member, kind in zip_tables(data):
        raw = z.read(member)
        if kind == "csv":
            g = csv_grid(raw)
            first = next((_label(v) for r in g for _, v in _cells(r)), "")
            rows.append({"table": _norm(member), "sheet": "", "title": first, "format": "csv", "member": member,
                         "grid_rows": len(g)})
            continue
        sheets = gr.xlsx_sheets(raw)
        for name, g in sheets.items():
            cl = [[_clean(v) for v in r] for r in g]
            first = next((_label(v) for r in cl for _, v in _cells(r)), "")
            rows.append({"table": _norm(member), "sheet": name, "title": first, "format": "xlsx", "member": member,
                         "grid_rows": len(g)})
    return rows


# ---------------------------------------------------------------------- CMS pages
_CACHE: dict[str, bytes] = {}


def page_links(text: str) -> list[tuple[str, str]]:
    out = []
    for href, inner in re.findall(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', text, flags=re.S | re.I):
        out.append((html.unescape(href), _label(html.unescape(re.sub(r"<[^>]+>", " ", inner)))))
    return out


def _get(client, path: str) -> bytes:
    if path not in _CACHE:
        _CACHE[path] = client.get(path).body
    return _CACHE[path]


def _rel(href: str) -> str:
    return re.sub(r"^https?://www\.cms\.gov", "", href)


def medicare_zips(client, eps: dict) -> dict[int, str]:
    text = _get(client, eps["pages"]["medicare"]).decode("utf-8", "replace")
    pat = re.compile(eps["medicare_link"], re.I)
    found = {}
    for href, t in page_links(text):
        m = pat.match(t)
        if m and href.lower().endswith(".zip"):
            found.setdefault(int(m.group(1)), _rel(href))
    if not found:
        raise DataError("the CMS Trustees Report page lists no 'Expanded and Supplementary Tables and Figures (ZIP)' "
                        "link (the page changed): see source.md, Access", kind="api")
    return found


def nhe_zip(client, eps: dict, key: str) -> tuple[str, str]:
    d = eps["nhe_downloads"][key]
    text = _get(client, eps["pages"][d["page"]]).decode("utf-8", "replace")
    pat = re.compile(d["link"], re.I)
    for href, t in page_links(text):
        if pat.match(t) and href.lower().endswith(".zip"):
            return _rel(href), t
    raise DataError(f"the CMS NHE {d['page']} page has no link matching {d['link']!r} (the page changed): see "
                    "source.md, Access", kind="api")


def _medicare_year(p: dict, years: dict[int, str], name: str) -> int:
    y = p.get("report_year")
    if y in (None, ""):
        return max(years)
    y = int(y)
    if y not in years:
        raise DataError(f"request {name!r}: no {y} Medicare Trustees Report tables ZIP on the CMS page (years: "
                        f"{', '.join(map(str, sorted(years)))})", kind="not_found")
    return y


# ---------------------------------------------------------------------- hooks
def is_ssa(r: dict) -> bool:
    return r.get("endpoint") == "ssa_tr_table"


def refused_hosts(spec) -> str:
    """--check: an SSA request goes to the manual route (no request is sent to www.ssa.gov)."""
    reqs = [r for r in spec.get("requests") or [] if isinstance(r, dict)]
    if not any(is_ssa(r) for r in reqs):
        return ""
    other = [r.get("name") for r in reqs if not is_ssa(r)]
    return SSA_REFUSED + (f"; the CMS requests ({', '.join(map(str, other))}) can be fetched: put them in their own "
                          "spec" if other else "")


def check_spec(spec):
    probs = []
    this = date.today().year
    for r in spec.get("requests") or []:
        p, n = r.get("params") or {}, r.get("name")
        y = p.get("report_year")
        if y not in (None, ""):
            try:
                y = int(y)
                if not 2000 <= y <= this:
                    probs.append(f"request {n!r}: report_year {y} is not a published report year")
            except (TypeError, ValueError):
                probs.append(f"request {n!r}: report_year must be a four-digit year")
        if r.get("endpoint") == "medicare_tr_files" or r.get("endpoint") == "medicare_tr_table":
            if isinstance(y, int) and y < 2022:
                probs.append(f"request {n!r}: the CMS page has the tables ZIP for 2022 on (asked {y}); older "
                             "reports are PDFs on the 'Trustees Reports (current and prior)' page")
        hr = p.get("header_row")
        if hr not in (None, ""):
            hs = hr if isinstance(hr, list) else [hr]
            if not all(isinstance(h, int) and h >= 1 or (isinstance(h, str) and h.isdigit() and int(h) >= 1)
                       for h in hs):
                probs.append(f"request {n!r}: header_row is a 1-based row number or a list of them")
        if is_ssa(r) and p.get("table") and not re.fullmatch(r"[IVX]+\.[A-Z]\d+", str(p["table"]).strip()):
            probs.append(f"request {n!r}: SSA table is a table number such as 'IV.B1', 'V.A3' or 'VI.G2'")
    return probs


def _hr(p):
    hr = p.get("header_row")
    if isinstance(hr, list):
        return [int(h) for h in hr]
    return int(hr) if hr not in (None, "") else None


def _table_rows(data: bytes, p: dict, req: dict, label: str, notes: list) -> list[dict]:
    member, kind = pick_member(zip_tables(data), p.get("table"), label)
    sheet, grid, merges, n_pct = read_member(data, member, kind, p.get("sheet"))
    rows, extra, title = tidy(grid, merges, _hr(p))
    where = os.path.basename(member) + (f", sheet {sheet}" if sheet else "")
    notes.append(f"{req['name']}: {label}: {where}" + (f" ({title})" if title else ""))
    _add_notes(req["name"], extra[1:] if title else extra, notes)
    if n_pct:
        notes.append(f"{req['name']}: {n_pct} cell(s) were stored as Excel percent-format fractions (0.106 shown as "
                     "10.6%) and are written as displayed (10.6)")
    if p.get("layout") == "long":
        rows, ok = long_layout(rows)
        if not ok:
            notes.append(f"{req['name']}: layout long ignored: the table has no year columns (its years are rows)")
    return rows


def _add_notes(name: str, lines: list[str], notes: list) -> None:
    for t in lines[:MAX_NOTE_LINES]:
        notes.append(f"{name}: table note: {t[:300]}" + ("..." if len(t) > 300 else ""))
    if len(lines) > MAX_NOTE_LINES:
        notes.append(f"{name}: {len(lines) - MAX_NOTE_LINES} more footnote line(s) in the sheet")


def fetch(spec, pack, route):
    eps = _eps(pack)
    ssa = [r["name"] for r in spec["requests"] if is_ssa(r)]
    if ssa:
        raise DataError(f"{SSA_REFUSED} (request(s) {', '.join(ssa)}): save the workbook from a web browser and "
                        "upload it (manual route)", kind="blocked")
    client = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, p, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        e = r["endpoint"]
        try:
            if e.startswith("medicare_tr"):
                years = medicare_zips(client, eps)
                y = _medicare_year(p, years, r["name"])
                data = _get(client, years[y])
                label = f"{y} Medicare Trustees Report tables ZIP ({os.path.basename(years[y])})"
            else:
                path, text = nhe_zip(client, eps, p["file"])
                data = _get(client, path)
                label = f"NHE download '{text}' ({os.path.basename(path)})"
        except DataError as x:
            if x.kind == "not_found":
                raise
            raise DataError(f"request {r['name']!r} ({e}): {x}", kind=x.kind, status=getattr(x, "status", None)) from None
        if e.endswith("_files"):
            rows = list_zip(data)
            notes.append(f"{r['name']}: {label}: {len(rows)} sheet(s)")
        else:
            rows = _table_rows(data, p, r, label, notes)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r}: the table has no rows (check table, sheet and header_row)",
                            kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def manual_items(spec, pack):
    eps = _eps(pack)
    base = eps["base_url"].rstrip("/")
    items, client, years = [], None, None
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        e = r.get("endpoint")
        if e == "ssa_tr_table":
            y = int(p.get("report_year") or eps["ssa_latest_known"])
            url = eps["ssa_workbook"].format(year=y)
            items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url,
                          "filename": os.path.basename(url), "also_ext": [".xlsx"], "needs_key": False,
                          "method": "GET", "body": None,
                          "note": ("open in a web browser (www.ssa.gov refuses scripts, so the download script will "
                                   f"likely get HTTP 403); the page {eps['ssa_index'].format(year=y)} links it as "
                                   "'Single-Year Tables ... Excel'. Upload the .xlsx as saved.")})
            continue
        url, page = None, None
        if e in ("medicare_tr_files", "medicare_tr_table"):
            page = base + eps["pages"]["medicare"]
            y = p.get("report_year")
            try:
                if client is None:          # a quick look only: the bundle is written when the host may be unreachable
                    client = gr.client_for(pack, timeout=10, retries=0, deadline=20)
                years = years or medicare_zips(client, eps)
                y = int(y) if y not in (None, "") else max(years)
                url = base + years[y] if y in years else None
            except (DataError, OSError):
                known = eps["medicare_known"]
                y = str(y) if y not in (None, "") else max(known)
                url = base + known[str(y)] if str(y) in known else None
            hint = f"'{y} Expanded and Supplementary Tables and Figures (ZIP)'"
        elif e in ("nhe_files", "nhe_table") and p.get("file") in eps["nhe_downloads"]:
            d = eps["nhe_downloads"][p["file"]]
            page = base + eps["pages"][d["page"]]
            try:
                if client is None:          # a quick look only: the bundle is written when the host may be unreachable
                    client = gr.client_for(pack, timeout=10, retries=0, deadline=20)
                url = base + nhe_zip(client, eps, p["file"])[0]
            except (DataError, OSError):
                url = base + d["known"]
            hint = f"the link for {d['what']}"
        else:
            continue
        fname = os.path.basename(url) if url else f"{r['name']}.zip"
        items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url or page, "filename": fname,
                      "also_ext": [".zip"], "needs_key": False, "method": "GET", "body": None,
                      "note": f"the ZIP from {page} ({hint}); upload it as downloaded"})
    return items


def parse_upload(pack, req, paths):
    p = req.get("params") or {}
    notes: list[str] = []
    rows = []
    for path in sorted(paths):
        with open(path, "rb") as f:
            data = f.read()
        base = os.path.basename(path)
        low = base.lower()
        if is_ssa(req):
            if not low.endswith(".xlsx"):
                raise DataError(f"{base}: upload the SSA single-year tables workbook (.xlsx) as saved from "
                                "the lrIndex page; a saved HTML page cannot be read", kind="bad_request")
            sheet, grid, merges, n_pct = xlsx_grid(data, str(p.get("table") or "").strip() or None)
            got, extra, title = tidy(grid, merges, _hr(p))
            rows += got
            continue
        if low.endswith(".zip"):
            if req.get("endpoint", "").endswith("_files"):
                rows += list_zip(data)
            else:
                rows += _table_rows(data, p, req, base, notes)
        elif low.endswith((".xlsx", ".csv")):            # one table taken out of the ZIP
            if low.endswith(".xlsx"):
                sheet, grid, merges, _ = xlsx_grid(data, p.get("sheet"))
            else:
                grid, merges = csv_grid(data), ()
            got = tidy(grid, merges, _hr(p))[0]
            rows += long_layout(got)[0] if p.get("layout") == "long" else got
        else:
            raise DataError(f"{base}: upload the CMS ZIP as downloaded (or one .xlsx / .csv table from it)",
                            kind="bad_request")
    return rows

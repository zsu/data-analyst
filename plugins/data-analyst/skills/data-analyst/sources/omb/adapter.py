"""omb adapter: the President's Budget data files that OMB publishes through GovInfo (no API, no key).

GovInfo serves each budget volume as a package BUDGET-<FY>-<PKG> with stable URLs (verified 2026-10-30):
  https://www.govinfo.gov/content/pkg/BUDGET-<FY>-<PKG>/xls/BUDGET-<FY>-<PKG>-<granule>.xlsx   one file
  https://www.govinfo.gov/content/pkg/BUDGET-<FY>-<PKG>.zip                                    the package
A file that does not exist answers HTTP 302 to https://www.govinfo.gov/error, an HTML page with status 200, so
every body is checked: a workbook or CSV is read, an HTML page means "not there".
  - Historical Tables (PKG=TAB): Table s.t is granule TAB-<s+1>-<t> (Table 1.1 = TAB-2-1; TAB-1 is the
    introduction PDF). Not every edition follows it: FY2026 publishes one workbook (BUDGET-2026-TAB.xlsx, one
    sheet per table, hist01z1 ...) and its granule URLs redirect to the error page; FY2015 and earlier use other
    granule numbers and .xls files. So the granule is tried first and its title row ("Table 1.1 - ...") must
    name the table; otherwise the package zip is read and the table found by its title.
  - Public Budget Database (PKG=DB): granule 1 budget authority, 2 outlays, 3 receipts (xlsx; older years such
    as FY2015 publish them as csv/BUDGET-<FY>-DB-<n>.csv only). FY2026 has no DB package on GovInfo.
  - Federal Credit Supplement (PKG=FCS): one workbook BUDGET-<FY>-FCS.xlsx with sheets Table1 ... Table10 (FY2024
    on); FY2018-FY2023 publish one granule per table, BUDGET-<FY>-FCS-<n>.xlsx.

A sheet is read as published (core.generic_rest.xlsx_sheets, standard library):
  - the title rows above the header (one filled cell: "Table 1.1 - SUMMARY ...", "(in millions of dollars)")
    go into the notes; "Return to Table of Contents" link cells are ignored;
  - the header is the first row with two or more filled cells (or header_row); following rows with an empty
    first cell are more header rows, and their labels are joined top-down with the group label spread over the
    columns it spans ("Total" over "Receipts | Outlays" gives "Total Receipts", "Total Outlays");
  - the footer (trailing rows with one filled cell: "* $500 thousand or less.", "Note: ...", footnotes) is cut
    from the rows and written into the notes;
  - label-only rows inside the body ("050 National Defense:", "Department of Agriculture") stay as rows with
    empty values, in published order; the column `sheet_row` gives each row's row number in the sheet; a
    Historical Table that stacks blocks in different units (3.1, 6.1: "In millions of dollars:", "As
    percentages of GDP:") gets a column `units_block` naming the block of each row;
  - cells keep their published text ("..........", "*", "N/A" are not blanked); leader dots after a label
    ("Farm Ownership.......") are cut; a number stored with binary noise (78.900000000000006) is written with
    15 significant digits (78.9), the same value.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import zipfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

BASE = "https://www.govinfo.gov/content/pkg"
DB_KINDS = {"budget_authority": 1, "outlays": 2, "receipts": 3}
DB_UNITS = "thousands of dollars"
FIRST_FY = 1996           # GovInfo's BUDGET collection starts with the FY1996 budget
WH_HIST = "https://www.whitehouse.gov/omb/information-resources/budget/historical-tables/"
WH_SUPP = "https://www.whitehouse.gov/omb/information-resources/budget/supplemental-materials/"


class NoPackage(DataError):
    """The edition (package) does not exist on GovInfo: the next older edition may be tried."""

    def __init__(self, msg: str):
        super().__init__(msg, kind="not_found", status=404)


# ---------------------------------------------------------------------- reading published grids
def _txt(v) -> str:
    return " ".join(str(v).split()) if v not in (None, "") else ""


def _filled(row) -> list[int]:
    return [i for i, v in enumerate(row) if _txt(v)]


NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
NOISY = re.compile(r"^-?\d+\.\d{9,}$|^-?\d+(\.\d+)?[eE][-+]?\d+$")


def _num_clean(s: str) -> str:
    """78.900000000000006 -> 78.9; 6.3600000000000004E-2 -> 0.0636 (the same double, 15 significant digits)."""
    if NOISY.match(s):
        f = format(float(s), ".15g")
        if "e" not in f:
            return f
    return s


def parse_grid(grid: list[list], header_row=None, header_rows=None, strip_dots=True) -> tuple[list[dict], dict]:
    """Rows of a published sheet as dicts, plus {"top": title lines, "footer": footer lines, "header": (first,
    last) 1-based header rows}. See the module docstring."""
    rows = [[None if _txt(v).lower() == "return to table of contents" else v for v in r] for r in grid]
    if header_row:
        start = int(header_row) - 1
        if not 0 <= start < len(rows):
            raise DataError(f"header_row {header_row} is outside the sheet ({len(rows)} rows)", kind="bad_request")
    else:
        start = next((i for i, r in enumerate(rows) if len(_filled(r)) >= 2), None)
        if start is None:
            return [], {"top": [_txt(v) for r in rows for v in r if _txt(v)], "footer": [], "header": None}
    top = [_txt(v) for r in rows[:start] for v in r if _txt(v)]
    end = start + 1
    if header_rows:
        end = start + int(header_rows)
    else:
        label_seen = bool(_txt(rows[start][0]) if rows[start] else "")
        while end < len(rows):
            f = _filled(rows[end])
            if not f:
                end += 1           # a blank row inside the header block is skipped (re-checked below)
                continue
            if (0 in f and label_seen) or sum(bool(NUM.match(_txt(rows[end][i]).replace(",", ""))) for i in f)                     > len(f) / 2:
                break
            label_seen = label_seen or 0 in f      # a group row with an empty first cell: the labels come next
            end += 1
        while end > start + 1 and not _filled(rows[end - 1]):
            end -= 1
    head = rows[start:end]
    width = max((len(r) for r in rows), default=0)
    orig = [[_txt(r[c]) if c < len(r) else "" for c in range(width)] for r in head]
    lab = [list(r) for r in orig]
    for k in range(len(lab) - 1):                  # spread a group label over the columns it spans
        for c in range(1, width):
            if lab[k][c] or not lab[k][c - 1]:
                continue
            if any(orig[j][c] for j in range(k)):  # an upper group starts here
                continue
            if not any(orig[j][c] for j in range(k + 1, len(orig))):
                continue                           # nothing below: an empty column, not a spanned one
            lab[k][c] = lab[k][c - 1]
    names, seen = [], set()
    for c in range(width):
        parts = []
        for k in range(len(lab)):
            if lab[k][c] and (not parts or parts[-1] != lab[k][c]):
                parts.append(lab[k][c])
        n = " ".join(parts) or f"col{c + 1}"
        n = n if n not in seen else f"{n}_col{c + 1}"
        seen.add(n)
        names.append(n)
    body = list(enumerate(rows[end:], start=end + 1))
    cut = len(body)
    while cut > 0 and len(_filled(body[cut - 1][1])) <= 1:
        cut -= 1
    footer = [_txt(v) for _, r in body[cut:] for v in r if _txt(v)]
    out, used = [], set()
    for n, r in body[:cut]:
        if not _filled(r):
            continue
        rec = {}
        for c, v in enumerate(r):
            s = str(v).strip() if v is not None else ""
            if c == 0 and strip_dots:
                s = re.sub(r"\s*\.{4,}\s*$", "", s)
            rec[names[c]] = s = _num_clean(s)
            if s:
                used.add(names[c])
        out.append((n, rec))
    # a column without a header and without a value (trailing empty cells) is dropped
    cols = [x for c, x in enumerate(names) if x in used or orig and any(o[c] for o in orig)]
    recs = [{**{k: rec.get(k, "") for k in cols}, "sheet_row": n} for n, rec in out]
    return recs, {"top": top, "footer": footer, "header": (start + 1, end)}


def _first_text(grid: list[list], rows: int = 4) -> str:
    for r in grid[:rows]:
        for v in r:
            t = _txt(v)
            if t and t.lower() != "return to table of contents":
                return t
    return ""


def _title_pat(table: str):
    s, t = table.split(".")
    return re.compile(rf"^\s*Table\s+{int(s)}\.{int(t)}(?![\d.])", re.I)


def find_table(sheets: dict, table: str):
    """(sheet name, grid) of the sheet whose title names Historical Table s.t, or (None, None)."""
    pat = _title_pat(table)
    for name, grid in sheets.items():
        if pat.match(_first_text(grid)):
            return name, grid
    return None, None


def _titles(sheets: dict) -> list[str]:
    out = []
    for g in sheets.values():
        m = re.match(r"^\s*Table\s+(\d+\.\d+)", _first_text(g), re.I)
        if m:
            out.append(m.group(1))
    return out


# ---------------------------------------------------------------------- bodies
def _kind(body: bytes) -> str:
    if body[:4] == b"PK\x03\x04":
        try:
            names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        except zipfile.BadZipFile:
            return "other"
        return "xlsx" if "xl/workbook.xml" in names else "zip"
    if body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    head = body[:400].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")) or b"<html" in head:
        return "html"
    return "text"


def _sheets(body: bytes, kind: str) -> dict:
    if kind == "xlsx":
        return gr.xlsx_sheets(body)
    if kind == "xls":
        return gr.xls_sheets(body)
    raise DataError(f"expected a workbook, got {kind}", kind="api")


def _csv_grid(body: bytes) -> list[list]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1252", errors="replace")
    return [list(r) for r in csv.reader(io.StringIO(text))]


def _get(c, url: str) -> tuple[bytes | None, str]:
    """(body, kind) of a GovInfo file, or (None, "missing") when GovInfo answers its error page or 404."""
    try:
        body = c.get(url).body
    except DataError as e:
        if e.kind == "not_found":
            return None, "missing"
        raise
    k = _kind(body)
    return (None, "missing") if k == "html" else (body, k)


def _zip_members(body: bytes):
    z = zipfile.ZipFile(io.BytesIO(body))
    key = lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)]   # noqa: E731
    for n in sorted(z.namelist(), key=key):
        low = n.lower()
        if low.endswith((".xlsx", ".xls")) and not n.endswith("/"):
            yield n, z.read(n)


# ---------------------------------------------------------------------- the three products
def hist_table(c, fy: int, table: str):
    s, t = (int(x) for x in table.split("."))
    url = f"{BASE}/BUDGET-{fy}-TAB/xls/BUDGET-{fy}-TAB-{s + 1}-{t}.xlsx"
    notes = []
    body, k = _get(c, url)
    if body is not None and k in ("xlsx", "xls"):
        name, grid = find_table(_sheets(body, k), table)
        if grid is not None:
            return grid, url, notes
        notes.append(f"{url} holds {_first_text(next(iter(_sheets(body, k).values())))[:60]!r}, not Table "
                     f"{table}: the table was looked up by its title in the package zip")
    zurl = f"{BASE}/BUDGET-{fy}-TAB.zip"
    zbody, zk = _get(c, zurl)
    if zbody is None or zk != "zip":
        raise NoPackage(f"GovInfo has no Historical Tables for the FY{fy} budget (BUDGET-{fy}-TAB): {url} and "
                        f"{zurl} answer GovInfo's error page")
    found = []
    for member, data in _zip_members(zbody):
        sh = _sheets(data, "xls" if member.lower().endswith(".xls") else "xlsx")
        name, grid = find_table(sh, table)
        if grid is not None:
            if body is None:
                notes.append(f"the FY{fy} edition has no file {url.rsplit('/', 1)[-1]} (GovInfo answers its error "
                             f"page): Table {table} was read from {member} in {zurl}"
                             + (f", sheet {name}" if len(sh) > 1 else ""))
            return grid, f"{zurl}#{member}" + (f"!{name}" if len(sh) > 1 else ""), notes
        found += _titles(sh)
    have = sorted(set(found), key=lambda x: [int(p) for p in x.split(".")])
    raise DataError(f"Table {table} is not in the FY{fy} Historical Tables (GovInfo package BUDGET-{fy}-TAB); "
                    f"its tables: {', '.join(have) or 'none found'}. Check the number against source.md (Table "
                    "s.t, e.g. 1.1, 3.2, 8.5)", kind="not_found", status=404)


def budget_db(c, fy: int, kind: str):
    n = DB_KINDS[kind]
    tried = []
    for url, fmt in ((f"{BASE}/BUDGET-{fy}-DB/xls/BUDGET-{fy}-DB-{n}.xlsx", "xlsx"),
                     (f"{BASE}/BUDGET-{fy}-DB/csv/BUDGET-{fy}-DB-{n}.csv", "csv")):
        body, k = _get(c, url)
        tried.append(url)
        if body is None:
            continue
        if fmt == "csv" and k == "text":
            return _csv_grid(body), url, [f"FY{fy} publishes this file as CSV only ({url})"]
        if k in ("xlsx", "xls"):
            return next(iter(_sheets(body, k).values())), url, []
    raise NoPackage(f"GovInfo has no Public Budget Database {kind} file for the FY{fy} budget ({' and '.join(tried)} "
                    "answer GovInfo's error page; the FY2026 budget published no database there)")


def _fcs_n(table) -> int:
    m = re.fullmatch(r"\s*(?:table\s*)?(\d{1,2})\s*", str(table), re.I)
    if not m:
        raise DataError(f"credit_supplement table {table!r}: give the table number, e.g. 1 (Table 1)",
                        kind="bad_request")
    return int(m.group(1))


def _fcs_sheet(sheets: dict, n: int):
    for name, grid in sheets.items():
        if re.fullmatch(rf"table\s*{n}", name.strip(), re.I):
            return name, grid
    pat = re.compile(rf"^\s*Table\s+{n}\.", re.I)
    for name, grid in sheets.items():
        if pat.match(_first_text(grid)):
            return name, grid
    return None, None


def credit_table(c, fy: int, table):
    n = _fcs_n(table)
    url = f"{BASE}/BUDGET-{fy}-FCS/xls/BUDGET-{fy}-FCS.xlsx"
    body, k = _get(c, url)
    if body is not None and k in ("xlsx", "xls"):
        sh = _sheets(body, k)
        name, grid = _fcs_sheet(sh, n)
        if grid is None:
            raise DataError(f"the FY{fy} Federal Credit Supplement has no Table {n} (sheets: {', '.join(sh)})",
                            kind="not_found", status=404)
        return grid, f"{url}!{name}", []
    url2 = f"{BASE}/BUDGET-{fy}-FCS/xls/BUDGET-{fy}-FCS-{n}.xlsx"
    body, k = _get(c, url2)
    if body is not None and k in ("xlsx", "xls"):
        sh = _sheets(body, k)
        name, grid = _fcs_sheet(sh, n)
        if grid is None:
            name, grid = next(iter(sh.items()))
        return grid, url2, [f"FY{fy} publishes one file per credit supplement table ({url2})"]
    raise NoPackage(f"GovInfo has no Federal Credit Supplement Table {n} for the FY{fy} budget ({url} and {url2} "
                    "answer GovInfo's error page)")


# ---------------------------------------------------------------------- spec handling
def _fy_candidates(params: dict) -> list[int]:
    if params.get("fy") not in (None, ""):
        return [int(params["fy"])]
    y = date.today().year
    return [y + 1, y, y - 1]                   # the budget for FY y+1 is released early in calendar year y


def _norm_table(v) -> str:
    m = re.fullmatch(r"\s*(?:table\s*)?(\d{1,2})\s*[.\-_ ]\s*(\d{1,2})\s*", str(v), re.I)
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else ""


def check_spec(spec):
    probs = []
    hi = date.today().year + 1
    for r in spec.get("requests") or []:
        p, n = r.get("params") or {}, r.get("name")
        if p.get("fy") not in (None, ""):
            try:
                fy = int(p["fy"])
                if not FIRST_FY <= fy <= hi:
                    probs.append(f"request {n!r}: fy {fy} is outside {FIRST_FY}-{hi} (the budget's fiscal year, "
                                 "e.g. 2027 for the budget released in 2026)")
            except (TypeError, ValueError):
                probs.append(f"request {n!r}: fy is a four-digit budget fiscal year, e.g. 2027")
        for k in ("header_row", "header_rows"):
            if p.get(k) not in (None, "") and not str(p[k]).isdigit():
                probs.append(f"request {n!r}: {k} is a positive whole number")
        ep = r.get("endpoint")
        if ep == "historical_table" and p.get("table") not in (None, "") and not _norm_table(p["table"]) \
                and not str(p["table"]).startswith("table_"):
            probs.append(f"request {n!r}: table {p['table']!r} is not a Historical Table number like \"1.1\" or "
                         "\"8.5\" (section.table)")
        if ep == "credit_supplement" and p.get("table") not in (None, ""):
            try:
                _fcs_n(p["table"])
            except DataError as e:
                probs.append(f"request {n!r}: {e}")
    return probs


def _one(c, ep_name: str, params: dict):
    """(grid, url, notes, fy, label) for one request, trying older editions when fy is not given."""
    last = None
    for fy in _fy_candidates(params):
        try:
            if ep_name == "historical_table":
                g, u, n = hist_table(c, fy, _norm_table(params["table"]))
                return g, u, n, fy, f"Historical Table {_norm_table(params['table'])}"
            if ep_name == "budget_database":
                g, u, n = budget_db(c, fy, params["kind"])
                return g, u, n, fy, f"Public Budget Database, {params['kind'].replace('_', ' ')}"
            if ep_name == "credit_supplement":
                g, u, n = credit_table(c, fy, params["table"])
                return g, u, n, fy, f"Federal Credit Supplement Table {_fcs_n(params['table'])}"
        except NoPackage as e:
            last = e
            if params.get("fy") not in (None, ""):
                raise
    raise last if last else DataError(f"unknown endpoint {ep_name!r}", kind="bad_request")


UNIT_BLOCK = re.compile(r"^(in|as)\b.*\b(dollars|percent|percentages)\b.*:$", re.I)


def unit_blocks(rows: list[dict]) -> list[dict]:
    """Tables 3.1 and 6.1 stack blocks in different units under label rows ("In millions of dollars:", "As
    percentages of GDP:"), so "Total, Federal outlays" appears once per block: add `units_block` (the label of
    the block a row is in) when a table has such label rows."""
    if not rows:
        return rows
    first = next(iter(rows[0]))
    blank = lambda r: not any(v for k, v in r.items() if k not in (first, "sheet_row"))  # noqa: E731
    if not any(blank(r) and UNIT_BLOCK.match(str(r[first]).strip()) for r in rows):
        return rows
    cur, out = "", []
    for r in rows:
        if blank(r) and UNIT_BLOCK.match(str(r[first]).strip()):
            cur = str(r[first]).strip().rstrip(":")
        row = {k: v for k, v in r.items() if k != "sheet_row"}
        out.append({**row, "units_block": cur, "sheet_row": r["sheet_row"]})
    return out


def _shape(ep_name: str, grid, params) -> tuple[list[dict], dict]:
    rows, meta = parse_grid(grid, params.get("header_row"), params.get("header_rows"),
                            strip_dots=ep_name != "budget_database")
    return (unit_blocks(rows) if ep_name == "historical_table" else rows), meta


def _notes(name: str, ep_name: str, label: str, fy, url: str, meta: dict, rows: list[dict]) -> list[str]:
    out = []
    top = " | ".join(meta["top"])[:400]
    head = meta.get("header")
    out.append(f"{name}: {label}, FY{fy} budget ({url})" + (f": {top}" if top else "")
               + (f"; header row{'s' if head and head[1] > head[0] else ''} "
                  f"{head[0]}{'-' + str(head[1]) if head and head[1] > head[0] else ''} of the sheet" if head else ""))
    if ep_name == "budget_database":
        out.append(f"{name}: amounts are in {DB_UNITS}; columns 1962 (1976 for budget authority) ... are fiscal "
                   f"years and TQ is the 1976 transition quarter; years after FY{fy - 2} are estimates (FY{fy - 1} "
                   f"the current-year estimate, FY{fy} on the President's proposals)")
    if ep_name == "historical_table" and rows:
        first = next(iter(rows[0]))
        est_rows = [r[first] for r in rows if "estimate" in str(r.get(first, "")).lower()]
        est_cols = [k for k in rows[0] if "estimate" in k.lower()]
        if est_rows or est_cols:
            e = est_rows or est_cols
            out.append(f"{name}: {len(e)} {'rows' if est_rows else 'columns'} marked estimate ({e[0]} to {e[-1]}): "
                       "projections of the President's budget, not actual results; the budget year and later are "
                       "proposals")
        else:
            out.append(f"{name}: no estimate years in this table (this edition's table ends with actual years)")
    if meta["footer"]:
        out.append(f"{name}: footnotes: " + " | ".join(meta["footer"])[:1200])
    return out


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    tables, notes = {}, []
    cap = int(pack.policy["limits"].get("max_rows") or 50000)
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        ep_name = r.get("endpoint") or ""
        if not ep_name:
            ep_name = ((pack.endpoints or {}).get("catalog") or {}).get(r.get("dataset"), {}).get("endpoint", "")
        try:
            grid, url, extra, fy, label = _one(c, ep_name, params)
        except DataError as e:
            raise DataError(f"request {r['name']!r} ({ep_name}): {e}", kind=e.kind, status=e.status) from None
        rows, meta = _shape(ep_name, grid, params)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r}: {label} (FY{fy}, {url}) has no data rows under the detected "
                            "header: set header_row (the 1-based row of the column names)", kind="bad_request")
        limit = min(int(r.get("max_rows") or cap), cap)
        if len(rows) > limit:
            notes.append(f"{r['name']}: stopped at {limit:,} rows (max_rows) of {len(rows):,}. Raise max_rows.")
            rows = rows[:limit]
        notes += [f"{r['name']}: {x}" for x in extra] + _notes(r["name"], ep_name, label, fy, url, meta, rows)
        tables[r["name"]] = rows
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    items = []
    y = date.today().year
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        name, epn = r["name"], r.get("endpoint")
        fy = int(params["fy"]) if params.get("fy") not in (None, "") else y + 1
        if epn == "historical_table":
            t = _norm_table(params.get("table"))
            if not t:
                continue
            s, tt = t.split(".")
            url = f"{BASE}/BUDGET-{fy}-TAB/xls/BUDGET-{fy}-TAB-{int(s) + 1}-{tt}.xlsx"
            note = (f"Table {t} of the FY{fy} Historical Tables. If GovInfo shows an error page (some editions, e.g. "
                    f"FY2026, have one workbook instead), download {BASE}/BUDGET-{fy}-TAB.zip and upload it as "
                    f"{name}.zip, or save hist{int(s):02d}z{tt}_fy{fy}.xlsx from {WH_HIST} as {name}.xlsx")
            ext = [".zip", ".xls"]
        elif epn == "budget_database":
            n = DB_KINDS.get(params.get("kind"), 2)
            url = f"{BASE}/BUDGET-{fy}-DB/xls/BUDGET-{fy}-DB-{n}.xlsx"
            note = (f"Public Budget Database ({params.get('kind')}), FY{fy}. Older editions publish CSV: "
                    f"{BASE}/BUDGET-{fy}-DB/csv/BUDGET-{fy}-DB-{n}.csv (upload as {name}.csv). The same file is on "
                    f"{WH_SUPP} (budauth/outlays/receipts_fy{fy}.xlsx)")
            ext = [".csv"]
        else:
            try:
                n = _fcs_n(params.get("table"))
            except DataError:
                continue
            url = f"{BASE}/BUDGET-{fy}-FCS/xls/BUDGET-{fy}-FCS.xlsx"
            note = (f"Federal Credit Supplement, FY{fy} (the pack reads Table {n}). FY2018-FY2023: "
                    f"{BASE}/BUDGET-{fy}-FCS/xls/BUDGET-{fy}-FCS-{n}.xlsx. Also on {WH_SUPP} (cr_supp_fy{fy}.xlsx)")
            ext = []
        items.append({"name": f"{name}#1", "request": name, "url": url, "filename": f"{name}.xlsx",
                      "also_ext": ext, "needs_key": False, "method": "GET", "body": None, "note": note})
    return items


def parse_upload(pack, req, paths):
    ep, params, probs = gr.resolve(pack, req)
    if probs:
        raise DataError("; ".join(probs), kind="bad_request")
    epn = req.get("endpoint")
    rows = []
    for p in paths:
        with open(p, "rb") as f:
            body = f.read()
        k = _kind(body)
        base = os.path.basename(p)
        if epn == "historical_table":
            t = _norm_table(params.get("table"))
            grid = None
            if k in ("xlsx", "xls"):
                _, grid = find_table(_sheets(body, k), t)
            elif k == "zip":
                for member, data in _zip_members(body):
                    _, grid = find_table(_sheets(data, "xls" if member.lower().endswith(".xls") else "xlsx"), t)
                    if grid is not None:
                        break
            if grid is None:
                raise DataError(f"{base}: no sheet titled \"Table {t} - ...\" (upload the Historical Tables file "
                                "for that table, or the package zip)", kind="bad_request")
        elif epn == "budget_database":
            if k == "text":
                grid = _csv_grid(body)
            elif k in ("xlsx", "xls"):
                grid = next(iter(_sheets(body, k).values()))
            else:
                raise DataError(f"{base}: not a Public Budget Database .xlsx or .csv file", kind="bad_request")
        else:
            if k not in ("xlsx", "xls"):
                raise DataError(f"{base}: not a Federal Credit Supplement workbook", kind="bad_request")
            sh = _sheets(body, k)
            n = _fcs_n(params.get("table"))
            _, grid = _fcs_sheet(sh, n)
            if grid is None and len(sh) == 1:
                grid = next(iter(sh.values()))
            if grid is None:
                raise DataError(f"{base}: no sheet Table{n} (sheets: {', '.join(sh)})", kind="bad_request")
        rows += _shape(epn, grid, params)[0]
    return rows

"""pbgc adapter: PBGC Pension Insurance Data Tables (one workbook per edition, no API, no key).

The index page https://www.pbgc.gov/about/reports/pension-insurance-data (https://www.pbgc.gov/prac/data-books
redirects there) lists one row per edition: "2024 Pension Insurance Data Tables (1st Installment) (08/04/2026)"
with a PDF and an Excel link. The file paths are not one pattern (verified 2026-10-30):
  2020-2024  /sites/default/files/documents/<YYYY>-pension-data-tables.xlsx
  2019, 2015 /sites/default/files/<YYYY>-pension-data-tables.xlsx
  2016-2018  /sites/default/files/<YYYY>_pension_data_tables.xlsx
  2010-2014  /sites/default/files/legacy/docs/... (2011 is an .xls)
so the Excel link is scraped from the page, never built. A workbook holds one sheet per table: S-1 ... S-49
(single-employer program), M-1 ... M-21 (multiemployer program), plus "DATA BOOK LISTING" (the table titles and
year ranges), "Overview" and "GLANCE". An installment edition holds only some tables (the 2024 1st Installment:
S-1 to S-21, S-29, S-43, M-1 to M-5, M-14, M-15, M-20, M-21); the listing still names them all.

A sheet is read as published (core.generic_rest.xlsx_sheets, standard library), with the same rules as the omb
pack: title rows above the header go into the notes; the header is the first row with two or more filled cells
(or header_row), following rows with an empty first cell are joined into it; the trailing footer (Source,
Notes, numbered footnotes) goes into the notes; label-only rows stay as rows; `sheet_row` is the sheet's row
number; cells keep their published text; a number stored with binary noise (2480.3000000000002) is written with
15 significant digits.
"""
from __future__ import annotations

import html
import io
import os
import re
import sys
import urllib.parse
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

SITE = "https://www.pbgc.gov"
INDEX = SITE + "/about/reports/pension-insurance-data"


# ---------------------------------------------------------------------- reading published grids (as in omb)
def _txt(v) -> str:
    return " ".join(str(v).split()) if v not in (None, "") else ""


def _filled(row) -> list[int]:
    return [i for i, v in enumerate(row) if _txt(v)]


NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
NOISY = re.compile(r"^-?\d+\.\d{9,}$|^-?\d+(\.\d+)?[eE][-+]?\d+$")


def _num_clean(s: str) -> str:
    """2480.3000000000002 -> 2480.3; 6.3600000000000004E-2 -> 0.0636 (the same double, 15 significant digits)."""
    if NOISY.match(s):
        f = format(float(s), ".15g")
        if "e" not in f:
            return f
    return s


def parse_grid(grid: list[list], header_row=None, header_rows=None) -> tuple[list[dict], dict]:
    """Rows of a published sheet as dicts, plus {"top", "footer", "header"} (see the module docstring)."""
    rows = [list(r) for r in grid]
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
                end += 1
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
            if any(orig[j][c] for j in range(k)):
                continue
            if not any(orig[j][c] for j in range(k + 1, len(orig))):
                continue
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
            rec[names[c]] = s = _num_clean(s)
            if s:
                used.add(names[c])
        out.append((n, rec))
    cols = [x for c, x in enumerate(names) if x in used or orig and any(o[c] for o in orig)]
    recs = [{**{k: rec.get(k, "") for k in cols}, "sheet_row": n} for n, rec in out]
    return recs, {"top": top, "footer": footer, "header": (start + 1, end)}


# ---------------------------------------------------------------------- the index page
def editions(body: str) -> list[dict]:
    """One row per edition on the index page: year, label, url (the Excel link), format; newest first."""
    out = []
    for tr in re.findall(r"<tr\b.*?</tr>", body, re.S | re.I):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S | re.I)
        if not cells:
            continue
        label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cells[0])).replace("\xa0", " ").split())
        m = re.match(r"(\d{4})\b", label)
        link = next((h for h in re.findall(r"href=\"([^\"]+)\"", tr, re.I)
                     if re.search(r"\.xlsx?$", h.split("?")[0], re.I)), None)
        if not m or not link:
            continue
        url = urllib.parse.urljoin(INDEX, html.unescape(link))
        out.append({"year": int(m.group(1)), "label": label, "url": url,
                    "format": url.rsplit(".", 1)[-1].lower()})
    return sorted(out, key=lambda e: -e["year"])


class Site:
    """The index page and workbooks, fetched once per run."""

    def __init__(self, pack):
        self.c = gr.client_for(pack)
        self._eds = None
        self._books: dict[str, dict] = {}

    def editions(self) -> list[dict]:
        if self._eds is None:
            self._eds = editions(self.c.get(INDEX).text())
            if not self._eds:
                raise DataError(f"no Excel links found on {INDEX} (the page layout changed?): open it in a browser "
                                "and use the manual route", kind="api")
        return self._eds

    def edition(self, year) -> dict:
        eds = self.editions()
        if year in (None, ""):
            return eds[0]
        hit = [e for e in eds if e["year"] == int(year)]
        if not hit:
            raise DataError(f"no {year} edition of the Pension Insurance Data Tables on {INDEX} (editions with an "
                            f"Excel file: {', '.join(str(e['year']) for e in eds)})", kind="not_found", status=404)
        return hit[0]

    def book(self, ed: dict) -> dict:
        if ed["url"] not in self._books:
            body = self.c.get(ed["url"]).body
            self._books[ed["url"]] = read_book(body, ed["url"])
        return self._books[ed["url"]]


def read_book(body: bytes, where: str) -> dict:
    if body[:4] == b"PK\x03\x04":
        try:
            if "xl/workbook.xml" in zipfile.ZipFile(io.BytesIO(body)).namelist():
                return gr.xlsx_sheets(body)
        except zipfile.BadZipFile:
            pass
    elif body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return gr.xls_sheets(body)
    raise DataError(f"{where} is not an Excel workbook (an HTML page in its place?)", kind="api")


# ---------------------------------------------------------------------- tables
def norm_table(v) -> str:
    """"s1", "S1", "s-1", "s_1", "Table S-1" -> "S-1"; "glance" -> "GLANCE"; anything else as given."""
    s = str(v or "").strip()
    m = re.fullmatch(r"(?:table\s*)?([SM])\s*[-_]?\s*(\d{1,2})", s, re.I)
    if m:
        return f"{m.group(1).upper()}-{int(m.group(2))}"
    return s


def listing(sheets: dict) -> list[dict]:
    """The DATA BOOK LISTING sheet(s): table, title, program, section; in workbook order."""
    out, program, section = [], "", ""
    for name, grid in sheets.items():
        if "LISTING" not in name.upper():
            continue
        for r in grid:
            cells = [_txt(v) for v in r if _txt(v)]
            if not cells:
                continue
            t = " ".join(cells)                    # 2018 and earlier: "S-1" and the title in two cells
            m = re.match(r"^(?:Table\s+)?([SM])\s*-\s*(\d{1,2})\s+(.+)$", t)
            if m:
                out.append({"table": f"{m.group(1)}-{int(m.group(2))}", "title": m.group(3).strip(),
                            "program": program, "section": section})
            elif re.search(r"single-employer program", t, re.I):
                program = "single-employer"
            elif re.search(r"multiemployer program", t, re.I):
                program = "multiemployer"
            elif len(cells) == 1 and len(t) < 90 and program:
                section = t
    seen, uniq = set(), []
    for x in out:
        if x["table"] not in seen:
            seen.add(x["table"])
            uniq.append(x)
    return uniq


def find_sheet(sheets: dict, table: str):
    want = norm_table(table)
    for name in sheets:
        if norm_table(name) == want or name.strip().lower() == want.lower():
            return name
    return None


def data_table(site: Site, params: dict):
    ed = site.edition(params.get("year"))
    sheets = site.book(ed)
    want = norm_table(params["table"])
    name = find_sheet(sheets, want)
    lst = {x["table"]: x for x in listing(sheets)}
    if name is None:
        tabs = [s for s in sheets if re.fullmatch(r"[SM]-\d+", s.strip())]
        listed = lst.get(want)
        why = (f"it is listed (\"{listed['title']}\") but not in this workbook: the edition \"{ed['label']}\" holds "
               "only some tables; ask for the previous year's edition" if listed else
               "no such table")
        raise DataError(f"table {want!r} is not a sheet of the {ed['year']} edition ({ed['url']}): {why}. Its "
                        f"table sheets: {', '.join(tabs)}", kind="not_found", status=404)
    rows, meta = parse_grid(sheets[name], params.get("header_row"), params.get("header_rows"))
    return rows, meta, ed, name, lst.get(norm_table(name))


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, n = r.get("params") or {}, r.get("name")
        if p.get("year") not in (None, "") and not re.fullmatch(r"(19|20)\d\d", str(p["year"]).strip()):
            probs.append(f"request {n!r}: year is the edition year, e.g. 2024")
        for k in ("header_row", "header_rows"):
            if p.get(k) not in (None, "") and not str(p[k]).isdigit():
                probs.append(f"request {n!r}: {k} is a positive whole number")
        if r.get("endpoint") == "data_table" and p.get("table") not in (None, ""):
            t = norm_table(p["table"])
            if not (re.fullmatch(r"[SM]-\d{1,2}", t) or t.upper() in ("GLANCE", "OVERVIEW")):
                probs.append(f"request {n!r}: table {p['table']!r} is not a table name like \"S-1\" or \"M-4\" "
                             "(single-employer S-, multiemployer M-)")
    return probs


def fetch(spec, pack, route):
    site = Site(pack)
    tables, notes = {}, []
    cap = int(pack.policy["limits"].get("max_rows") or 50000)
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        name, epn = r["name"], r.get("endpoint")
        try:
            if epn == "editions":
                rows = [{k: e[k] for k in ("year", "label", "url", "format")} for e in site.editions()]
                notes.append(f"{name}: editions listed on {INDEX}")
            elif epn == "table_list":
                ed = site.edition(params.get("year"))
                sheets = site.book(ed)
                rows = [{**x, "in_workbook": "yes" if find_sheet(sheets, x["table"]) else "no"}
                        for x in listing(sheets)]
                notes.append(f"{name}: tables listed in the {ed['label']} workbook ({ed['url']}); in_workbook = no "
                             "means the edition does not hold that sheet")
            else:
                rows, meta, ed, sheet, lst = data_table(site, params)
                title = (f"Table {sheet}: {lst['title']}" if lst else " | ".join(meta["top"]) or f"sheet {sheet}")
                prog = (lst or {}).get("program") or ("single-employer" if sheet.startswith("S-") else
                                                      "multiemployer" if sheet.startswith("M-") else "")
                notes.append(f"{name}: {title}" + (f", {prog} program" if prog else "")
                             + f"; {ed['label']} ({ed['url']}, sheet {sheet!r}); header row"
                             + (f"s {meta['header'][0]}-{meta['header'][1]}" if meta["header"] and
                                meta["header"][1] > meta["header"][0] else f" {(meta['header'] or ('?',))[0]}"))
                if meta["top"] and lst:
                    notes.append(f"{name}: rows above the header (title, units): " + " | ".join(meta["top"])[:300])
                if meta["footer"]:
                    notes.append(f"{name}: source and notes: " + " | ".join(meta["footer"])[:1500])
        except DataError as e:
            raise DataError(f"request {name!r} ({epn}): {e}", kind=e.kind, status=e.status) from None
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {name!r}: no data rows under the detected header: set header_row (the 1-based "
                            "row of the column names)", kind="bad_request")
        limit = min(int(r.get("max_rows") or cap), cap)
        if len(rows) > limit:
            notes.append(f"{name}: stopped at {limit:,} rows (max_rows) of {len(rows):,}. Raise max_rows.")
            rows = rows[:limit]
        tables[name] = rows
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        name, epn = r["name"], r.get("endpoint")
        if epn == "editions":
            items.append({"name": f"{name}#1", "request": name, "url": INDEX, "filename": f"{name}.html",
                          "needs_key": False, "method": "GET", "body": None,
                          "note": "save the page (File > Save as, HTML only)"})
            continue
        y = params.get("year")
        what = f"the {y} edition's Excel file" if y not in (None, "") else "the newest edition's Excel file"
        items.append({"name": f"{name}#1", "request": name, "url": INDEX, "filename": f"{name}.xlsx",
                      "also_ext": [".xls"], "needs_key": False, "method": "GET", "body": None,
                      "note": f"open the page, click the Excel link of {what} (row \"<year> Pension Insurance Data "
                              f"Tables\") and save it as {name}.xlsx"
                              + (f"; the pack reads sheet {norm_table(params.get('table'))}" if epn == "data_table"
                                 else "")})
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
        if epn == "editions":
            rows += [{k: e[k] for k in ("year", "label", "url", "format")}
                     for e in editions(body.decode("utf-8", errors="replace"))]
            continue
        sheets = read_book(body, os.path.basename(p))
        if epn == "table_list":
            rows += [{**x, "in_workbook": "yes" if find_sheet(sheets, x["table"]) else "no"} for x in listing(sheets)]
            continue
        name = find_sheet(sheets, params.get("table"))
        if name is None:
            raise DataError(f"{os.path.basename(p)} has no sheet {norm_table(params.get('table'))!r} (sheets: "
                            f"{', '.join(sheets)})", kind="bad_request")
        rows += parse_grid(sheets[name], params.get("header_row"), params.get("header_rows"))[0]
    return rows

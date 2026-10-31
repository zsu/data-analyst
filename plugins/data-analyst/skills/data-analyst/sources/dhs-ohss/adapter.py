"""dhs-ohss adapter: DHS Office of Homeland Security Statistics published workbooks (no API, no key).

ohss.dhs.gov posts the Yearbook of Immigration Statistics as one page per fiscal year
(https://ohss.dhs.gov/topics/immigration/yearbook/<YYYY>) that links one workbook per topic; the file names carry
upload dates and suffixes (2026_0604_ohss_yearbook_lawful_permanent_residents_fy2024.xlsx, ..._fy2023_0.xlsx), so
they are scraped from the page, never built (verified 2026-10-30). FY2022 on: one .xlsx per topic, one sheet per
table ("Table 6", "LPRSuppTable 1", "NISuppTable1") after a "TOC" sheet. FY2015-2021: a .zip per topic holding
one .xlsx per table (fy2019_table6.xlsx, fy2019_table7d.xlsx; sheet "Table 7D"). The quarterly Legal Immigration
and Adjustment of Status Report has one .xlsx per fiscal year on its own page.

A sheet is read as published (core.generic_rest.xlsx_sheets, standard library):
  - title rows (one filled cell: "Return to Table of Contents", "Table 6.", the upper-case title) are skipped;
    the header is the first row with two or more filled cells (or header_row). A second header row (first cell
    empty, labels only, under a header with gaps: "Total | | | Adjustments of status" over "Total | Quarter 1 |
    ...") is joined to it: "Adjustments of status Quarter 1".
  - section label rows (one filled cell, upper case: TOTAL, ADJUSTMENTS OF STATUS, NEW ARRIVALS, REGION, COUNTRY,
    AGE) are not rows: their label goes in a first column `section`.
  - the footer (the "Notes:"/"Source:" lines and the footnote lines just above them: "1 Includes orphans.",
    "X Not applicable.", "D Data withheld to limit disclosure.") is cut from the rows and written into the notes.
  - cells keep their published text: codes (D, X, NA, -) are not blanked, footnote markers stay in labels
    ("Children1", "All other countries1").
"""
from __future__ import annotations

import html
import io
import os
import re
import sys
import urllib.parse
import zipfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

BASE = "https://ohss.dhs.gov"
INDEX = "/topics/immigration/yearbook"
LIR_PAGE = "/topics/immigration/legal-immigration-and-adjustment-status-report"
FIRST_YEAR = 1996          # year pages exist from 1996; data files (xlsx/zip) from FY2015 (verified 2026-10-30)
FILE_YEARS = 2015
DEFAULT_TRIES = 4          # newest year pages tried when fiscal_year is not given

# topic -> pattern on the link text (lower case, spaces collapsed); fallback: the topic whose workbook holds the
# same tables in years without a separate file (FY2022 on, the supplementary tables are in the main workbook)
TOPICS = {
    "lpr": (r"lawful permanent residents? \d{4} data tables", None),
    "lpr_supplementary": (r"lawful permanent residents? \d{4} supplementary", "lpr"),
    "lpr_expanded": (r"expanded \d{4} yearbook tables 8-11", None),
    "refugees": (r"\brefugees (and asylees )?\d{4} data tables", None),
    "asylees": (r"\b(refugees and )?asylees \d{4} data tables", None),
    "refugees_asylees": (r"\b(refugees and asylees|refugees|asylees) \d{4} data tables", None),
    "naturalizations": (r"naturalizations \d{4} data tables", None),
    "naturalizations_supplementary": (r"naturalizations \d{4} supplementary", "naturalizations"),
    "nonimmigrant": (r"nonimmigrant admissions \d{4} data tables", None),
    "nonimmigrant_supplementary": (r"nonimmigrant admissions \d{4} supplementary", "nonimmigrant"),
    "enforcement": (r"enforcement actions \d{4} data tables", None),
}
TABLE_EPS = ("yearbook_table", "lir_table")

# a footnote line: "1 Includes ...", "¹ Includes ..." (older files use superscripts), "- Represents zero.", "X Not
# applicable.", "D Data withheld to limit disclosure.", "NA Not available."
FOOTNOTE = re.compile(r"^(\d{1,2}|[¹²³⁰-⁹]{1,2}|[^\w\s]|[A-Z]{1,2})\s+\S")
FOOTER = re.compile(r"^\s*(notes?|sources?)\s*:", re.I)
NUMERIC = re.compile(r"^-?[\d,]+(\.\d+)?$")
CODES = ("D", "X", "NA", "-")


# ---------------------------------------------------------------------- pages and links
def _text(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").split())


def page_links(body: str, page_url: str) -> list[dict]:
    """Data-file links (.xlsx / .zip) on an ohss.dhs.gov page, first occurrence of each URL, in page order. The href
    is kept as published (some are percent-encoded twice: YRBK%25202015%2520LPR...zip is the real file name)."""
    out, seen = [], set()
    for m in re.finditer(r"<a\s[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", body, re.S | re.I):
        href, label = html.unescape(m.group(1)), _text(m.group(2))
        if not re.search(r"\.(xlsx|zip)$", href.split("?")[0], re.I):
            continue
        url = urllib.parse.urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": label, "url": url, "format": url.rsplit(".", 1)[-1].lower()})
    return out


def topics_of(title: str) -> list[str]:
    t = title.lower()
    return [k for k, (pat, _) in TOPICS.items() if re.search(pat, t)]


class Site:
    """Pages fetched once per run."""

    def __init__(self, pack):
        self.c = gr.client_for(pack)
        self.pages: dict[str, str] = {}

    def page(self, path: str) -> str:
        if path not in self.pages:
            self.pages[path] = self.c.get(path).text()
        return self.pages[path]

    def years(self) -> list[int]:
        ys = {int(y) for y in re.findall(r"/topics/immigration/yearbook/(\d{4})\"", self.page(INDEX))}
        return sorted(ys, reverse=True)

    def year_files(self, year: int) -> list[dict]:
        path = f"{INDEX}/{year}"
        try:
            body = self.page(path)
        except DataError as e:
            if e.kind == "not_found":
                raise DataError(f"no yearbook page for fiscal year {year} ({BASE}{path} answered 404)",
                                kind="not_found") from None
            raise
        return [{"fiscal_year": year, "topic": ", ".join(topics_of(f["title"])), **f}
                for f in page_links(body, BASE + path)]

    def lir_files(self) -> list[dict]:
        out = []
        for f in page_links(self.page(LIR_PAGE), BASE + LIR_PAGE):
            fy = re.search(r"FY ?(\d{4})", f["title"])
            q = re.findall(r"q(\d)", f["url"].rsplit("/", 1)[-1].lower())
            out.append({"fiscal_year": int(fy.group(1)) if fy else "", "through_quarter": max(q) if q else "",
                        "title": f["title"], "url": f["url"], "format": f["format"]})
        return out


def pick(files: list[dict], topic: str, year: int) -> tuple[dict | None, str]:
    """The topic's file among one year's files (falling back to the workbook that holds the same tables)."""
    pat, fallback = TOPICS[topic]
    hits = [f for f in files if re.search(pat, f["title"].lower())]
    note = ""
    if not hits and fallback:
        hits = [f for f in files if re.search(TOPICS[fallback][0], f["title"].lower())]
        note = (f"FY{year} has no separate {topic} file; its tables are in the {fallback} workbook "
                f"({hits[0]['title']})" if hits else "")
    if len(hits) > 1:
        raise DataError(f"topic {topic!r} matches {len(hits)} FY{year} files: "
                        + "; ".join(f"{h['title']} ({h['url']})" for h in hits)
                        + ". Ask for one of: refugees, asylees", kind="bad_request")
    return (hits[0] if hits else None), note


def locate(site: Site, topic: str, year) -> tuple[dict, int, list[str]]:
    """(file, fiscal year, notes) for a topic; without a year, the newest year page that posts the file."""
    notes = []
    if year not in (None, ""):
        y = int(year)
        f, n = pick(site.year_files(y), topic, y)
        if not f:
            have = sorted({t for x in site.year_files(y) for t in topics_of(x["title"])})
            raise DataError(f"the FY{y} yearbook page ({BASE}{INDEX}/{y}) posts no {topic} file (it has: "
                            f"{', '.join(have) or 'no data files'}); files are posted topic by topic as they are "
                            "ready, and data files start with FY2015", kind="not_found")
        return f, y, [n] if n else []
    skipped = []
    for y in [y for y in site.years() if y >= FILE_YEARS][:DEFAULT_TRIES]:
        f, n = pick(site.year_files(y), topic, y)
        if f:
            if skipped:
                notes.append(f"fiscal_year not given: FY{', FY'.join(map(str, skipped))} has no {topic} file yet "
                             f"(posted topic by topic), so the newest is FY{y}")
            return f, y, notes + ([n] if n else [])
        skipped.append(y)
    raise DataError(f"no {topic} file on the newest yearbook pages (FY{', FY'.join(map(str, skipped))})",
                    kind="not_found")


def locate_lir(site: Site, year) -> tuple[dict, int]:
    files = [f for f in site.lir_files() if f["fiscal_year"]]
    if not files:
        raise DataError(f"no report workbooks found on {BASE}{LIR_PAGE}", kind="api")
    if year in (None, ""):
        f = max(files, key=lambda x: x["fiscal_year"])
        return f, f["fiscal_year"]
    for f in files:
        if f["fiscal_year"] == int(year):
            return f, int(year)
    raise DataError(f"no Legal Immigration and Adjustment of Status Report for FY{year} (posted: FY"
                    f"{', FY'.join(str(f['fiscal_year']) for f in files)})", kind="not_found")


# ---------------------------------------------------------------------- workbooks
def _key(name: str) -> str:
    """'Table 7D' / 'fy2019_table7d.xlsx' / '7' -> 'table7'; 'LPRSuppTable 1' -> 'lprsupptable1'."""
    s = os.path.splitext(os.path.basename(str(name)))[0].lower()
    s = re.sub(r"^fy\d{4}_?", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    s = re.sub(r"(\d)d$", r"\1", s)
    return "table" + s if s.isdigit() else s


def workbook_sheets(data: bytes, fmt: str) -> dict[str, list[list]]:
    """{sheet name: grid} of an .xlsx, or of every .xlsx in a .zip (FY2015-2021 yearbook files)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DataError(f"expected an .{fmt} file, got something else (an HTML page, or an old .xls)", kind="api")
    if "[Content_Types].xml" in z.namelist():         # an .xlsx (itself a zip)
        return gr.xlsx_sheets(data)
    out = {}
    for n in sorted(z.namelist(), key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]):
        if n.lower().endswith(".xlsx") and not n.endswith("/"):
            for s, g in gr.xlsx_sheets(z.read(n)).items():
                out[s if s not in out else f"{s} ({os.path.basename(n)})"] = g
    if not out:
        raise DataError(f"no .xlsx in the zip ({', '.join(z.namelist()[:10])})", kind="api")
    return out


def _cell(v) -> str:
    return "" if v is None else " ".join(str(v).split())


def _filled(r: list) -> int:
    return sum(1 for v in r if _cell(v))


def sheet_title(grid: list[list]) -> str:
    for r in grid[:8]:
        t = _cell(r[0]) if r else ""
        if _filled(r) == 1 and len(t) > 25 and t.upper() == t:
            return t
    return ""


def sheet_list(sheets: dict) -> str:
    toc = {}
    for name, g in sheets.items():
        if name.strip().lower() in ("toc", "table of contents", "contents"):
            toc = {_cell(r[0]): _cell(r[1]) for r in g if len(r) > 1 and _cell(r[0]) and _cell(r[1])}
    items = []
    for name, g in sheets.items():
        if name in ("TOC",):
            continue
        t = toc.get(name) or sheet_title(g).capitalize()
        items.append(f"{name!r}" + (f" ({t[:160]})" if t else ""))
    return "; ".join(items)


def choose_sheet(sheets: dict, want) -> str:
    if want in (None, ""):
        raise DataError(f"no table given: set params.table to one of the workbook's sheets: {sheet_list(sheets)}",
                        kind="bad_request")
    if str(want) in sheets:
        return str(want)
    by_key = {}
    for n in sheets:
        by_key.setdefault(_key(n), n)
    k = _key(str(want))
    if k in by_key:
        return by_key[k]
    raise DataError(f"no sheet {want!r} in the workbook; its sheets: {sheet_list(sheets)}", kind="bad_request")


def _is_subheader(top: list, row: list) -> bool:
    """A second header row under `top`: two or more labels and no numbers (years allowed), filling cells that
    `top` leaves empty, where one of the two rows has an empty first cell (group labels over column labels:
    "Total | | Adjustments of status" over "| Quarter 1 | Quarter 2", or "| | Sex | | Age" over "Region and
    country of birth | Total | Female | Male | Under 1 year")."""
    if not row or not top or _filled(row) < 2 or (_cell(row[0]) and _cell(top[0])):
        return False
    vals = [_cell(v) for v in row if _cell(v)]
    if any(NUMERIC.match(v) and not re.fullmatch(r"(19|20)\d{2}", v) for v in vals):
        return False
    gaps = [i for i in range(1, len(row)) if _cell(row[i]) and (i >= len(top) or not _cell(top[i]))]
    return bool(gaps)


def find_header(grid: list[list], header_row=None) -> tuple[int, int]:
    """(index of the header row, number of header rows: 1 or 2)."""
    if header_row not in (None, ""):
        i = int(header_row) - 1
        if not 0 <= i < len(grid):
            raise DataError(f"header_row {header_row} is outside the sheet ({len(grid)} rows)", kind="bad_request")
    else:
        i = next((k for k, r in enumerate(grid) if _filled(r) >= 2), None)
        if i is None:
            raise DataError("no header row found (no row with two or more filled cells): set header_row",
                            kind="bad_request")
    two = i + 1 < len(grid) and _is_subheader(grid[i], grid[i + 1])
    return i, 2 if two else 1


def header_names(grid: list[list], i: int, n: int) -> list[str]:
    top = [_cell(v) for v in grid[i]]
    width = max(len(top), len(grid[i + 1]) if n == 2 else 0)
    top += [""] * (width - len(top))
    if n == 2:
        low = [_cell(v) for v in grid[i + 1]] + [""] * width
        names, group = [], ""
        for k in range(width):
            if top[k] and k > 0:                        # the row-label column starts no group
                group = top[k]
            up = top[k] or (group if k > 0 and low[k] else "")
            if k == 0:
                names.append(top[0] or low[0])
            elif up and low[k] and up != low[k]:
                names.append(f"{up} {low[k]}")
            else:
                names.append(low[k] or up)
    else:
        names = top
    out, seen = [], set()
    for k, h in enumerate(names):
        h = h or f"col{k + 1}"
        h = h if h not in seen else f"{h}_col{k + 1}"
        seen.add(h)
        out.append(h)
    return out


def _footer_start(rows: list[list]) -> int:
    """Index (into rows below the header) where the footnotes / Notes / Source block begins."""
    end = next((k for k, r in enumerate(rows) if r and FOOTER.match(_cell(r[0])) and _filled(r) <= 2), len(rows))
    k = end
    while k > 0:
        r = rows[k - 1]
        if _filled(r) == 0 or (_filled(r) == 1 and _cell(r[0]) and FOOTNOTE.match(_cell(r[0]))):
            k -= 1
            continue
        break
    return k


def read_table(sheets: dict, table, header_row=None) -> tuple[str, list[dict], dict]:
    """(sheet name, rows, info {title, header_row, footer: [...], codes: {code: n}})."""
    name = choose_sheet(sheets, table)
    grid = sheets[name]
    i, n = find_header(grid, header_row)
    head = header_names(grid, i, n)
    body = grid[i + n:]
    cut = _footer_start(body)
    footer = [" ".join(_cell(v) for v in r if _cell(v)) for r in body[cut:] if _filled(r)]
    rows, section, has_section, codes = [], "", False, {}
    for r in body[:cut]:
        if not _filled(r):
            continue
        first = _cell(r[0]) if r else ""
        if _filled(r) == 1 and first and re.search(r"[A-Z]", first) and first.upper() == first:
            section, has_section = first, True
            continue
        row = {"section": section}
        for k, v in enumerate(r):
            col = head[k] if k < len(head) else f"col{k + 1}"
            val = "" if v is None else str(v).strip()
            row[col] = val
            if k and val in CODES:
                codes[val] = codes.get(val, 0) + 1
        for col in head:
            row.setdefault(col, "")
        rows.append(row)
    if not has_section:
        for row in rows:
            row.pop("section", None)
    info = {"title": sheet_title(grid[:i]) or sheet_title(grid), "header_row": i + 1, "header_rows": n,
            "footer": footer, "codes": codes}
    return name, rows, info


def table_notes(req: str, what: str, url: str, sheet: str, info: dict) -> list[str]:
    hdr = (f"rows {info['header_row']}-{info['header_row'] + 1} joined" if info["header_rows"] == 2
           else f"row {info['header_row']}")
    notes = [f"{req}: {what}, sheet {sheet!r}" + (f" ({info['title']})" if info["title"] else "")
             + f"; header {hdr}; from {url}"]
    if info["footer"]:
        notes.append(f"{req}: published footnotes and notes: " + " | ".join(info["footer"]))
    if info["codes"]:
        notes.append(f"{req}: cells keep the published codes ("
                     + ", ".join(f"{c} x{n}" for c, n in sorted(info["codes"].items()))
                     + "): D = withheld to limit disclosure, X = not applicable, NA = not available, - = zero "
                     "(or zero or not available, see the footnotes); they are not numbers")
    return notes


# ---------------------------------------------------------------------- hooks
def check_spec(spec):
    probs = []
    this = date.today().year
    for r in spec.get("requests") or []:
        p, n, ep = r.get("params") or {}, r.get("name"), r.get("endpoint")
        years = p.get("fiscal_year")
        ys = years if isinstance(years, list) else ([] if years in (None, "") else [years])
        try:
            ys = [int(y) for y in ys]
        except (TypeError, ValueError):
            probs.append(f"request {n!r}: fiscal_year is a four-digit fiscal year such as 2024")
            continue
        if isinstance(years, list) and ep != "yearbook_files":
            probs.append(f"request {n!r}: one fiscal_year per request for {ep} (a list works for yearbook_files)")
        for y in ys:
            if y > this + 1 or y < FIRST_YEAR:
                probs.append(f"request {n!r}: fiscal_year {y} is outside {FIRST_YEAR}-{this}")
            elif ep in ("yearbook_table", "yearbook_files") and y < FILE_YEARS:
                probs.append(f"request {n!r}: yearbook data files (xlsx/zip) start with FY{FILE_YEARS}; FY{y} is "
                             f"published as a PDF only ({BASE}{INDEX})")
            elif ep == "lir_table" and y < 2016:
                probs.append(f"request {n!r}: the Legal Immigration and Adjustment of Status Report starts with FY2016")
        t = p.get("topic")
        if ep == "yearbook_files" and t not in (None, "") and t not in TOPICS:
            probs.append(f"request {n!r}: topic {t!r} is not one of {sorted(TOPICS)}")
        hr = p.get("header_row")
        if hr not in (None, "") and not str(hr).isdigit():
            probs.append(f"request {n!r}: header_row is a 1-based row number")
    return probs


def fetch(spec, pack, route):
    site = Site(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        e = r.get("endpoint")
        try:
            if e == "yearbook_files":
                ys = params.get("fiscal_year")
                ys = ys if isinstance(ys, list) else ([ys] if ys not in (None, "") else
                                                      [y for y in site.years() if y >= FILE_YEARS][:1])
                rows = []
                for y in ys:
                    rows += site.year_files(int(y))
                if params.get("topic"):
                    rows = [x for x in rows if params["topic"] in x["topic"].split(", ")]
                notes.append(f"{r['name']}: data files on the FY{', FY'.join(map(str, ys))} yearbook page(s); "
                             "topic is the pack's topic name(s) for the file (empty: not a table workbook)")
            elif e == "lir_files":
                rows = site.lir_files()
            else:
                if e == "yearbook_table":
                    f, y, extra = locate(site, params["topic"], params.get("fiscal_year"))
                    what = f"FY{y} yearbook, {params['topic']} ({f['title']})"
                else:
                    f, y = locate_lir(site, params.get("fiscal_year"))
                    extra = [] if params.get("fiscal_year") else [f"fiscal_year not given: newest report is FY{y}"]
                    what = (f"FY{y} Legal Immigration and Adjustment of Status Report"
                            + (f" (through quarter {f['through_quarter']})" if f.get("through_quarter") else ""))
                data = site.c.get(f["url"]).body
                sheet, rows, info = read_table(workbook_sheets(data, f["format"]), params.get("table"),
                                               params.get("header_row"))
                notes += [f"{r['name']}: {x}" for x in extra] + table_notes(r["name"], what, f["url"], sheet, info)
                if e == "lir_table":
                    notes.append(f"{r['name']}: quarterly report figures are preliminary (data available one month "
                                 "after the quarter) and are revised; final annual counts are in the yearbook")
        except DataError as err:
            raise DataError(f"request {r['name']!r} ({e}): {err}", kind=err.kind,
                            status=getattr(err, "status", None)) from None
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows", kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def manual_items(spec, pack):
    """The workbook link for each table request (scraped from the year page); when the page cannot be read here
    (offline, sandbox, or the site refuses scripts), the page itself, with the link text to look for."""
    items = []
    site = None
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        e = r.get("endpoint")
        y = params.get("fiscal_year")
        if e in ("yearbook_files", "lir_files"):
            page = LIR_PAGE if e == "lir_files" else (f"{INDEX}/{y}" if y not in (None, "") and
                                                      not isinstance(y, list) else INDEX)
            items.append({"name": f"{r['name']}#1", "request": r["name"], "url": BASE + page,
                          "filename": f"{r['name']}.html", "needs_key": False, "method": "GET", "body": None,
                          "note": "a list of files, not data: open the page and read the links (save it as "
                                  "HTML only if asked)", "also_ext": [".htm"]})
            continue
        url, note, ext = None, "", "xlsx"
        try:
            site = site or Site(pack)
            if e == "yearbook_table":
                f, fy, _ = locate(site, params["topic"], y)
            else:
                f, fy = locate_lir(site, y)
            url, ext = f["url"], f["format"]
            note = f"the FY{fy} file \"{f['title']}\"; the request reads sheet {params.get('table')!r}"
        except DataError:
            if e == "yearbook_table":
                page = BASE + (f"{INDEX}/{y}" if y not in (None, "") else INDEX)
                note = (f"open the page{'' if y not in (None, '') else ', choose the newest year'}, and download the "
                        f"{params['topic']} data tables workbook (.xlsx, or .zip up to FY2021); the request reads "
                        f"sheet {params.get('table')!r}")
            else:
                page = BASE + LIR_PAGE
                note = (f"open the page and download the FY{y or ' (newest)'} report workbook (.xlsx); the request "
                        f"reads sheet {params.get('table')!r}")
            url = page
        items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url,
                      "filename": f"{r['name']}.{ext}", "also_ext": [".zip" if ext == "xlsx" else ".xlsx"],
                      "needs_key": False, "method": "GET", "body": None, "note": note})
    return items


def parse_upload(pack, req, paths):
    """An uploaded workbook (.xlsx, or the .zip of FY2015-2021) read like the fetched one."""
    _, params, _ = gr.resolve(pack, req)
    if req.get("endpoint") not in TABLE_EPS:
        raise DataError(f"request {req['name']!r}: {req.get('endpoint')} lists files from a web page; run it on the "
                        "script route, or read the page in a browser", kind="bad_request")
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            data = f.read()
        fmt = "zip" if p.lower().endswith(".zip") else "xlsx"
        if data.lstrip()[:1] == b"<":                   # the year page saved in place of the workbook
            raise DataError(f"{os.path.basename(p)} is a web page, not the workbook: open the link in a browser and "
                            "download the data tables file it lists (.xlsx, or .zip up to FY2021), then upload that",
                            kind="bad_request")
        try:
            _, got, _ = read_table(workbook_sheets(data, fmt), params.get("table"), params.get("header_row"))
        except DataError as e:
            raise DataError(f"{os.path.basename(p)}: {e}", kind="bad_request") from None
        rows += got
    return rows

"""usda-fna Program Data tables: FNA's XLSX workbooks (SNAP, WIC, child nutrition) read into tidy rows.

The listing pages (/pd/supplemental-nutrition-assistance-program-snap, /pd/wic-program, /pd/child-nutrition-tables)
link one workbook per table, named <stem>-<release>.xlsx (e.g. snap-persons-10.xlsx); the release number changes,
so the adapter reads the current name from the listing page. Layouts (verified 2026-10-30):
  annual        national annual summary: "Fiscal Year" header, one row per year (1969-)
  monthly       national monthly: ANNUAL SUMMARY (FY rows) then MONTHLY DATA ("FY 2023" headings, "Oct 2022" rows)
  state_fy      one measure by state, five fiscal years ("FY 2021" ... columns); WIC indents its tribal parts
  state_latest  one measure by state for three months (a year ago, last month, this month) and percent changes
  snap_zip      snap-zip-fy69tocurrent-NN.zip: FYyy.xlsx (FY 2020-) by region sheet, state blocks of months
  wic_agency    wicagencies<FY>ytd.xlsx: sixteen sheets (one per measure), agencies x months
Gates (a mismatch drops the data and says so, never guesses): the title row must match the table, every column used
must carry the label endpoints.json gives it, a value under an unlabeled visible column fails the table (hidden
columns are skipped), months must fall in their fiscal year in order, and month headers of state_latest tables
must be a year ago / last month / this month. Agency sums are compared with the printed totals (a note, not a fix).
Standard library only (zipfile + ElementTree).
"""
from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
import xml.etree.ElementTree as ET

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}
MISSING = ("--", "-", "—", "–", "N/A", "NA", "#DIV/0!", "#N/A", "#VALUE!")
NUM = re.compile(r"(-?\d+(?:\.\d+)?(?:E[-+]?\d+)?)\s*(\*+|\d\])?", re.I)
EXTRA_AREAS = [("AS", "American Samoa"), ("PR", "Puerto Rico"), ("MP", "Northern Mariana Islands")]
AREA_ALIASES = {"commonwealth of northern mariana islands": "MP", "northern marianas": "MP",
                "u.s. virgin islands": "VI", "us virgin islands": "VI"}
INCOMPLETE = ": incomplete:"          # core.generic_rest.INCOMPLETE (kept equal; a test checks it)
REGIONS = {"NERO": "Northeast", "MARO": "Mid-Atlantic", "SERO": "Southeast", "MWRO": "Midwest", "SWRO": "Southwest",
           "MPRO": "Mountain Plains", "WRO": "Western"}


class LayoutError(ValueError):
    """The workbook does not have the layout endpoints.json describes: its rows are dropped."""


# ---------------------------------------------------------------------- XLSX reader
def read_book(body: bytes) -> list[tuple[str, list[list], set[int]]]:
    """[(sheet name, rows, hidden column indexes)]; a row is a list of cell texts (None for empty), in sheet row
    order with empty rows kept, so rows[i] is sheet row i + 1."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        raise LayoutError("not an XLSX file (the server may have returned an HTML page)") from None
    with z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = ["".join(t.text or "" for t in si.iter(f"{M}t"))
                      for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{M}si")]
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        target = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        out = []
        for s in wb.iter(f"{M}sheet"):
            path = target[s.get(f"{REL}id")]
            path = path.lstrip("/") if path.startswith("/") else "xl/" + path
            root = ET.fromstring(z.read(path))
            hidden = set()
            for c in root.iter(f"{M}col"):
                if c.get("hidden") in ("1", "true"):
                    hidden |= set(range(int(c.get("min")) - 1, int(c.get("max"))))
            rows: list[list] = []
            for r in root.iter(f"{M}row"):
                vals = {}
                for c in r.findall(f"{M}c"):
                    m = re.match(r"[A-Z]+", c.get("r", ""))
                    col = 0
                    for ch in (m.group(0) if m else "A"):
                        col = col * 26 + ord(ch) - 64
                    v, t = c.find(f"{M}v"), c.get("t")
                    if t == "s" and v is not None:
                        vals[col - 1] = shared[int(v.text)]
                    elif t == "inlineStr":
                        vals[col - 1] = "".join(x.text or "" for x in c.iter(f"{M}t"))
                    elif v is not None:
                        vals[col - 1] = v.text
                n = int(r.get("r") or len(rows) + 1)
                rows += [[] for _ in range(n - 1 - len(rows))]
                rows.append([vals.get(i) for i in range(max(vals) + 1)] if vals else [])
            out.append((s.get("name"), rows, hidden))
        return out


def norm(x) -> str:
    return re.sub(r"\s+", " ", str(x if x is not None else "").replace("\xa0", " ")).strip()


def cell(v) -> tuple[str, str, str, str]:
    """(value, value_num, value_flag, footnote) of a published cell. Numbers are written with up to 15 significant
    digits (Excel's own precision: 549.70000000000005 is 549.7); codes go to the flag and are never a zero."""
    s = norm(v)
    if not s:
        return "", "", "blank", ""
    if s in MISSING:
        return s, "", s, ""
    m = NUM.fullmatch(s)
    if not m:
        return s, "", s, ""                         # "1]" (a footnote standing for a value, e.g. included with free)
    f = float(m.group(1))
    num = f"{f:.15g}"
    if "e" in num:
        num = f"{f:.15f}".rstrip("0").rstrip(".")
    return num, num, "", m.group(2) or ""


def as_of(rows: list[list]) -> str:
    for r in rows[:6]:
        m = re.search(r"Data as of\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})", " ".join(norm(x) for x in r if x))
        if m:
            try:
                return dt.datetime.strptime(re.sub(r",\s*", ", ", m.group(1)), "%B %d, %Y").date().isoformat()
            except ValueError:
                return m.group(1)
    return ""


def label_ok(head, label: str) -> bool:
    h = re.sub(r"\s+", "", norm(head)).lower()
    return bool(h) and all(w.lower() in h for w in re.findall(r"[A-Za-z0-9]+", label))


def month(s) -> tuple[int, int] | None:
    """(year, month) of "Oct 2022", "July 2026 Initial", "June\\n2026\\nPreliminary"; None otherwise."""
    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b", norm(s))
    if m and m.group(1).lower() in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1).lower()]
    return None


def fy_of(ym: tuple[int, int]) -> int:
    return ym[0] + 1 if ym[1] >= 10 else ym[0]


def excel_month(v) -> tuple[int, int] | None:
    try:
        d = dt.date(1899, 12, 30) + dt.timedelta(days=int(float(v)))
    except (TypeError, ValueError, OverflowError):
        return month(v)
    return d.year, d.month


def prelim_years(rows: list[list]) -> set[int]:
    text = " ".join(norm(x) for r in rows for x in r if isinstance(x, str))
    return {int(y) for y in re.findall(r"FY ?(\d{4}) data are preliminary", text, re.I)}


# ---------------------------------------------------------------------- areas
class Areas:
    """The states, DC and territories under the names the workbooks use (USPS codes; AS, PR, MP added)."""

    def __init__(self, agencies: list[tuple[str, str]]):
        self.name = dict(agencies + EXTRA_AREAS)
        self.abbr = {n.lower(): a for a, n in self.name.items()}
        self.abbr.update(AREA_ALIASES)

    def code(self, x) -> str | None:
        k = norm(x).lower().rstrip("*").strip()
        if k.upper() in self.name:
            return k.upper()
        return self.abbr.get(k)

    def codes(self, v) -> list[str]:
        vals = v if isinstance(v, list) else [x for x in str(v).split(",") if x.strip()]
        out = []
        for x in vals:
            c = self.code(x)
            if not c:
                raise ValueError(f"unknown state or territory {x!r}: give a USPS code (TX) or a name (Texas); "
                                 "territories: AS, GU, MP, PR, VI")
            out.append(c)
        return list(dict.fromkeys(out))

    def classify(self, raw) -> tuple[str, str, str]:
        """(row_type, state, state_abbr) of a state-table row name."""
        n = norm(raw)
        if n.upper() == "TOTAL":
            return "total", "Total", ""
        if n.startswith(("DOD", "Dept. of Defense")):
            return "dod", n, ""
        c = self.code(n)
        if c:
            return "state", n, c
        return "other", n, ""


# ---------------------------------------------------------------------- table rows
def _row(t, title, asof, state, abbr, agency, rtype, period, ptype, fy, meas, unit, raw, status) -> dict:
    v, num, flag, foot = cell(raw)
    return {"table": t, "table_title": title, "data_as_of": asof, "state": state, "state_abbr": abbr,
            "agency": agency, "row_type": rtype, "period": period, "period_type": ptype, "fiscal_year": fy,
            "measure": meas, "unit": unit, "value": v, "value_num": num, "value_flag": flag, "status": status,
            "footnote": foot}


def title_of(rows: list[list]) -> str:
    return next((norm(r[0]) for r in rows if r and r[0] and norm(r[0])), "")


def layout_of(rows: list[list]) -> str:
    firsts = [norm(r[0]) for r in rows[:12] if r and r[0]]
    if "ANNUAL SUMMARY" in [f.upper() for f in firsts]:
        return "monthly"
    for r in rows[:8]:
        if r and r[0] and norm(r[0]).lower().startswith("state"):
            c = [norm(x) for x in r[1:] if x]
            if c and re.match(r"FY \d{4}", c[0]):
                return "state_fy"
            if c and month(c[0]):
                return "state_latest"
        if r and r[0] and norm(r[0]).lower().startswith("fiscal year"):
            return "annual"
    return ""


def find_sheet(book, d: dict):
    """The (name, rows, hidden) sheet holding table d: its title matches and its layout is d's."""
    for name, rows, hidden in book:
        if re.search(d["title"], title_of(rows), re.I) and layout_of(rows) == d["layout"]:
            return name, rows, hidden
    return None


def _header(rows: list[list], first: str) -> int:
    for i, r in enumerate(rows[:10]):
        if r and r[0] and norm(r[0]).lower().startswith(first):
            return i
    raise LayoutError(f"no header row starting with {first!r}")


def _check_columns(rows, start: int, hidden: set[int], used: set[int], what: str):
    """A value under a visible column the table does not use means the columns moved: fail."""
    for r in rows[start:]:
        for j, x in enumerate(r[1:], 1):
            if j not in used and j not in hidden and norm(x) and cell(x)[1]:
                raise LayoutError(f"{what}: a value in unlabeled column {j + 1} ({norm(r[0])!r} row)")


def _labeled(head: list, cols: list, hidden: set[int], what: str) -> list[tuple[int, list]]:
    """[(column index, [measure, label, unit])]: the visible header cells in order, each matching its label."""
    vis = [j for j in range(1, len(head)) if j not in hidden and norm(head[j])]
    if len(vis) != len(cols):
        raise LayoutError(f"{what}: {len(vis)} labeled columns, endpoints.json lists {len(cols)}")
    for j, c in zip(vis, cols):
        if not label_ok(head[j], c[1]):
            raise LayoutError(f"{what}: column {j + 1} is {norm(head[j])!r}, expected {c[1]!r}")
    return list(zip(vis, cols))


def parse_annual(key: str, d: dict, rows, hidden) -> list[dict]:
    title, asof, pre = title_of(rows), as_of(rows), prelim_years(rows)
    h = _header(rows, "fiscal year")
    cols = _labeled(rows[h], d["columns"], hidden, key)
    _check_columns(rows, h + 1, hidden, {j for j, _ in cols}, key)
    out, last = [], 0
    for r in rows[h + 1:]:
        m = re.fullmatch(r"(\d{4})(?:\.0)?\s*(\d\])?", norm(r[0]) if r else "")
        if not m:
            continue
        fy = int(m.group(1))
        if fy <= last:
            raise LayoutError(f"{key}: fiscal year {fy} after {last}")
        last = fy
        for j, (meas, _, unit) in cols:
            row = _row(key, title, asof, "U.S.", "", "", "national", f"FY {fy}", "fiscal_year", fy, meas, unit,
                       r[j] if j < len(r) else None, "preliminary" if fy in pre else "")
            row["footnote"] = " ".join(x for x in (m.group(2) or "", row["footnote"]) if x)
            out.append(row)
    if not out:
        raise LayoutError(f"{key}: no fiscal-year rows")
    return out


def parse_monthly(key: str, d: dict, rows, hidden) -> list[dict]:
    title, asof, pre = title_of(rows), as_of(rows), prelim_years(rows)
    h = _header(rows, "fiscal year")
    cols = _labeled(rows[h], d["columns"], hidden, key)
    _check_columns(rows, h + 1, hidden, {j for j, _ in cols}, key)
    out, section, cur, prev = [], "", None, None
    for r in rows[h + 1:]:
        f = norm(r[0]) if r else ""
        if f.upper() in ("ANNUAL SUMMARY", "MONTHLY DATA"):
            section = f.upper()
            continue
        has = any(norm(x) for x in r[1:])
        mfy = re.fullmatch(r"FY ?(\d{4})", f)
        ym = month(f)
        if mfy and section == "MONTHLY DATA" and not has:
            cur, prev = int(mfy.group(1)), None
            continue
        if mfy and section == "ANNUAL SUMMARY":
            fy = int(mfy.group(1))
            period, ptype = f"FY {fy}", "fiscal_year"
        elif ym and section == "MONTHLY DATA":
            if cur is None or fy_of(ym) != cur or (prev and ym <= prev):
                raise LayoutError(f"{key}: month {f!r} is out of place (under FY {cur})")
            prev, fy = ym, cur
            period, ptype = f"{ym[0]}-{ym[1]:02d}", "month"
        else:
            continue
        for j, (meas, _, unit) in cols:
            out.append(_row(key, title, asof, "U.S.", "", "", "national", period, ptype, fy, meas, unit,
                            r[j] if j < len(r) else None, "preliminary" if fy in pre else ""))
    if not any(x["period_type"] == "month" for x in out):
        raise LayoutError(f"{key}: no monthly rows")
    return out


def _state_rows(rows, start: int, areas: Areas):
    """[(row_type, state, abbr, agency, row)] down to the TOTAL row; indented names are parts of the state above."""
    out, parent = [], None
    for r in rows[start:]:
        if not r or not r[0] or not norm(r[0]):
            continue
        raw = str(r[0])
        rtype, state, abbr = areas.classify(raw)
        if rtype != "total" and raw[:1] == " " and parent:
            out.append(("sub_agency", parent[0], parent[1], norm(raw), r))
            continue
        out.append((rtype, state, abbr, "", r))
        if rtype == "total":
            return out
        parent = (state, abbr)
    raise LayoutError("no TOTAL row")


def _sum_notes(key, rows: list[dict], tol_rel=0.0005) -> list[str]:
    """Agency sums vs the printed totals (and WIC tribal parts vs their state), per period and measure."""
    notes = []
    by = {}
    for x in rows:
        by.setdefault((x["measure"], x["period"]), []).append(x)
    for (meas, period), xs in by.items():
        tot = [x for x in xs if x["row_type"] == "total" and x["value_num"]]
        parts = [float(x["value_num"]) for x in xs if x["row_type"] in ("state", "dod", "other") and x["value_num"]]
        if tot and parts and "avg" not in meas and "share" not in meas:
            t = float(tot[0]["value_num"])
            if abs(sum(parts) - t) > max(2.0, tol_rel * abs(t)):
                notes.append(f"{key} {meas} {period}: agency sum {sum(parts):,.0f} vs printed total {t:,.0f}")
    return notes


def parse_state_fy(key: str, d: dict, rows, hidden, areas: Areas) -> tuple[list[dict], list[str]]:
    title, asof = title_of(rows), as_of(rows)
    h = _header(rows, "state")
    cols = [(j, int(re.match(r"FY (\d{4})", norm(x)).group(1))) for j, x in enumerate(rows[h]) if j and
            j not in hidden and re.match(r"FY \d{4}", norm(x))]
    if not cols or [y for _, y in cols] != list(range(cols[0][1], cols[0][1] + len(cols))):
        raise LayoutError(f"{key}: fiscal-year columns {[norm(x) for x in rows[h][1:]]}")
    _check_columns(rows, h + 1, hidden, {j for j, _ in cols}, key)
    status = {}
    for r in rows[h + 1:h + 3]:
        if r and not norm(r[0] if r else ""):
            for j, _ in cols:
                if j < len(r) and re.search(r"prelim", norm(r[j]), re.I):
                    status[j] = "preliminary"
    pre = prelim_years(rows)
    out = []
    for rtype, state, abbr, agency, r in _state_rows(rows, h + 1, areas):
        for j, fy in cols:
            out.append(_row(key, title, asof, state, abbr, agency, rtype, f"FY {fy}", "fiscal_year", fy,
                            d["measure"], d["unit"], r[j] if j < len(r) else None,
                            status.get(j) or ("preliminary" if fy in pre else "")))
    return out, _sum_notes(key, out) + _sub_notes(key, out)


def _sub_notes(key, rows: list[dict]) -> list[str]:
    notes, by = [], {}
    for x in rows:
        if x["row_type"] in ("state", "sub_agency") and x["value_num"]:
            by.setdefault((x["state"], x["period"]), {}).setdefault(x["row_type"], []).append(float(x["value_num"]))
    for (st, period), g in by.items():
        if any("avg" in x["measure"] for x in rows[:1]):
            break
        if "sub_agency" in g and "state" in g:
            s, t = sum(g["sub_agency"]), g["state"][0]
            if abs(s - t) > max(2.0, 0.0005 * abs(t)):
                notes.append(f"{key} {st} {period}: its agencies sum to {s:,.0f}, the state row is {t:,.0f}")
    return notes


def parse_state_latest(key: str, d: dict, rows, hidden, areas: Areas) -> tuple[list[dict], list[str]]:
    title, asof = title_of(rows), as_of(rows)
    h = _header(rows, "state")
    head = rows[h]
    cols = [(j, month(x), norm(x)) for j, x in enumerate(head) if j and j not in hidden and month(x)]
    used = {j for j, _, _ in cols} | {j for j, x in enumerate(head) if re.match(r"percent change", norm(x), re.I)}
    _check_columns(rows, h + 1, hidden, used, key)
    if len(cols) != 3:
        raise LayoutError(f"{key}: {len(cols)} month columns, expected 3")
    notes, keep = [], []
    y, m = cols[2][1]
    want = [(y - 1, m), (y if m > 1 else y - 1, m - 1 if m > 1 else 12), (y, m)]
    for (j, ym, lab), w in zip(cols, want):
        if ym != w:
            notes.append(f"{key}{INCOMPLETE} column {lab!r} dropped: its month is not {w[0]}-{w[1]:02d} (a year "
                         f"before / the month before {y}-{m:02d}), so the header cannot be trusted")
        else:
            st = "initial" if re.search(r"initial", lab, re.I) else "preliminary" if re.search(r"prelim", lab, re.I) else ""
            keep.append((j, ym, st))
    out = []
    for rtype, state, abbr, agency, r in _state_rows(rows, h + 1, areas):
        for j, ym, st in keep:
            out.append(_row(key, title, asof, state, abbr, agency, rtype, f"{ym[0]}-{ym[1]:02d}", "month", fy_of(ym),
                            d["measure"], d["unit"], r[j] if j < len(r) else None, st))
    return out, notes + _sum_notes(key, out) + _sub_notes(key, out)



def parse_table(key: str, d: dict, body: bytes, areas: Areas) -> tuple[list[dict], list[str]]:
    """Rows and notes of catalog table key from its workbook; LayoutError when it does not match."""
    found = find_sheet(read_book(body), d)
    if not found:
        raise LayoutError(f"{key}: no sheet titled like {d['title']!r} with the {d['layout']} layout")
    _, rows, hidden = found
    if d["layout"] == "annual":
        return parse_annual(key, d, rows, hidden), []
    if d["layout"] == "monthly":
        return parse_monthly(key, d, rows, hidden), []
    if d["layout"] == "state_fy":
        return parse_state_fy(key, d, rows, hidden, areas)
    return parse_state_latest(key, d, rows, hidden, areas)


# ---------------------------------------------------------------------- SNAP state monthly (zip of FYyy.xlsx)
SNAP_COLS = [("households", "households (monthly)"), ("persons", "persons (monthly)"), ("benefits", "dollars"),
             ("benefit_per_household", "dollars per household per month"),
             ("benefit_per_person", "dollars per person per month")]


def snap_zip_member(z: zipfile.ZipFile, fy: int) -> bytes | None:
    for n in z.namelist():
        if re.fullmatch(rf"(?i)FY{fy % 100:02d}\.xlsx", n.rsplit("/", 1)[-1]):
            return z.read(n)
    return None


def snap_zip_years(z: zipfile.ZipFile) -> list[int]:
    out = []
    for n in z.namelist():
        m = re.fullmatch(r"(?i)FY(\d{2})\.(xlsx?)", n.rsplit("/", 1)[-1])
        if m:
            out.append((2000 if int(m.group(1)) < 69 else 1900) + int(m.group(1)))
    return sorted(out)


def parse_snap_year(body: bytes, fy: int, areas: Areas) -> tuple[list[dict], list[str]]:
    out, notes = [], []
    t = "snap_state_monthly"
    for sheet, rows, _ in read_book(body):
        head = " ".join(norm(x) for r in rows[:3] for x in r if x)
        if not re.search(rf"Fiscal Year {fy}\b", head):
            raise LayoutError(f"FY{fy % 100:02d}.xlsx sheet {sheet!r} is not fiscal year {fy}: {head[:120]!r}")
        title = norm(rows[1][0]) if len(rows) > 1 and rows[1] else head
        h = next((i for i, r in enumerate(rows[:12]) if r and norm(r[0]).lower().startswith("fiscal year and month")),
                 None)
        sub = rows[h + 1] if h is not None and h + 1 < len(rows) else []
        labs = [norm(x).lower() for x in sub[1:6]] if sub else []
        if h is None or labs != ["household", "persons", "", "household", "persons"] or \
                not label_ok(rows[h][3] if len(rows[h]) > 3 else "", "Cost"):
            raise LayoutError(f"FY {fy} sheet {sheet!r}: the header is not Participation (Household, Persons), "
                              f"Cost, Cost Per (Household, Persons): {labs}")
        block, prev = None, None
        for r in rows[h + 2:]:
            f = norm(r[0]) if r else ""
            if not f:
                continue
            if f.lower().startswith("footnotes"):
                break
            ym = month(f)
            if not ym and f != "Total":
                if f in REGIONS:
                    block = ("region", REGIONS[f] + " Region", "", f)
                elif f.lower() == "us summary":
                    block = ("national", "U.S.", "", "")
                else:
                    rt, st, ab = areas.classify(f)
                    block = (rt, st, ab, "")
                prev = None
                continue
            if block is None:
                raise LayoutError(f"FY {fy} sheet {sheet!r}: {f!r} before any state name")
            if ym:
                if fy_of(ym) != fy or (prev and ym <= prev):
                    raise LayoutError(f"FY {fy} sheet {sheet!r} {block[1]}: month {f!r} is out of place")
                prev, period, ptype = ym, f"{ym[0]}-{ym[1]:02d}", "month"
            else:
                period, ptype = f"FY {fy}", "fiscal_year"
            for j, (meas, unit) in enumerate(SNAP_COLS, 1):
                out.append(_row(t, title, "", block[1], block[2], block[3], block[0], period, ptype, fy,
                                meas, unit if ptype == "month" or meas != "benefits" else "dollars (fiscal year)",
                                r[j] if j < len(r) else None, ""))
    # sums: states of a region vs the region, regions vs the U.S., per month
    by = {}
    for x in out:
        if x["period_type"] == "month" and x["measure"] in ("persons", "households", "benefits") and x["value_num"]:
            by.setdefault((x["measure"], x["period"]), {}).setdefault(x["row_type"], []).append(float(x["value_num"]))
    for (meas, period), g in sorted(by.items()):
        if "national" in g and "region" in g and abs(sum(g["region"]) - g["national"][0]) > 2:
            notes.append(f"snap_state_monthly FY {fy} {meas} {period}: regions sum to {sum(g['region']):,.0f}, "
                         f"U.S. {g['national'][0]:,.0f}")
        if "region" in g and "state" in g and abs(sum(g["state"]) - sum(g["region"])) > 2:
            notes.append(f"snap_state_monthly FY {fy} {meas} {period}: states sum to {sum(g['state']):,.0f}, "
                         f"regions {sum(g['region']):,.0f}")
    if not any(x["row_type"] == "state" for x in out):
        raise LayoutError(f"FY {fy}: no state rows")
    return out, notes


# ---------------------------------------------------------------------- WIC agency monthly (wicagencies<FY>ytd.xlsx)
def wic_sheet_key(name: str, sheets: dict) -> str | None:
    for k, d in sheets.items():
        if re.search(d["sheet"], name, re.I):
            return k
    return None


def parse_wic_sheet(key: str, d: dict, rows, hidden, fy: int, areas: Areas) -> tuple[list[dict], list[str]]:
    title = title_of(rows)
    top = " ".join(norm(x) for r in rows[:4] for x in r if x)
    if not re.search(rf"FISCAL YEAR {fy}\b", top, re.I):
        raise LayoutError(f"{key}: the sheet is not fiscal year {fy} ({top[:80]!r})")
    asof = as_of(rows)
    pre = "preliminary" if re.search(r"all data are preliminary",
                                     " ".join(norm(x) for r in rows[-8:] for x in r if x), re.I) else ""
    h = _header(rows, "state agency")
    head = rows[h]
    months, total = [], None
    for j, x in enumerate(head[1:], 1):
        if j in hidden or not norm(x):
            continue
        if re.match(r"(average|cumulative)", norm(x), re.I):
            total = (j, norm(x))
            continue
        ym = excel_month(x)
        if not ym:
            raise LayoutError(f"{key}: header cell {norm(x)!r} is neither a month nor the year column")
        months.append((j, ym))
    want = [(fy - 1, 10), (fy - 1, 11), (fy - 1, 12)] + [(fy, m) for m in range(1, 10)]
    if [ym for _, ym in months] != want[:len(months)]:
        raise LayoutError(f"{key}: month columns {[ym for _, ym in months]} are not October {fy - 1} onward")
    if total is None:
        raise LayoutError(f"{key}: no average or cumulative column")
    _check_columns(rows, h + 1, hidden, {j for j, _ in months} | {total[0]}, key)
    out, notes, agencies = [], [], []
    for r in rows[h + 1:]:
        f = norm(r[0]) if r and r[0] else ""
        if not f or not any(norm(x) for x in r[1:]):
            continue
        m = re.fullmatch(r"(.+), ([A-Z]{2})", f)
        if f.upper() == "TOTAL":
            rt, st, ab, ag = "total", "Total", "", ""
        elif f.removesuffix(" Region") in REGIONS.values():         # "Mountain Plains" has no " Region"
            rt, st, ab, ag = "region", f, "", ""
        elif m and areas.code(m.group(2)):
            rt, st, ab, ag = "tribal_org", areas.name[areas.code(m.group(2))], areas.code(m.group(2)), m.group(1)
        else:
            rt, st, ab = areas.classify(f)
            rt, ag = ("state_agency", "") if rt == "state" else (rt, "")
        for j, ym in months:
            out.append(_row(f"wic_agency_monthly", title, asof, st, ab, ag, rt, f"{ym[0]}-{ym[1]:02d}", "month", fy,
                            key, d["unit"], r[j] if j < len(r) else None, pre))
        out.append(_row("wic_agency_monthly", title, asof, st, ab, ag, rt, f"FY {fy}", "fiscal_year", fy, key,
                        d["unit"] + (" (" + total[1].split(":")[0].lower() + ")"), r[total[0]] if total[0] < len(r) else None,
                        pre))
        agencies.append((rt, r))
    # agencies since the last region add up to that region; regions add up to TOTAL
    if "avg" not in key:
        for j, _ in months + [(total[0], None)]:
            run, regions = 0.0, 0.0
            for rt, r in agencies:
                v = cell(r[j] if j < len(r) else None)[1]
                v = float(v) if v else 0.0
                if rt == "region":
                    if abs(run - v) > max(2.0, 0.0005 * abs(v)):
                        notes.append(f"wic_agency_monthly FY {fy} {key} {norm(r[0])} column {j + 1}: agencies sum "
                                     f"to {run:,.0f}, region {v:,.0f}")
                    regions, run = regions + v, 0.0
                elif rt == "total":
                    if abs(regions - v) > max(2.0, 0.0005 * abs(v)):
                        notes.append(f"wic_agency_monthly FY {fy} {key} column {j + 1}: regions sum to "
                                     f"{regions:,.0f}, TOTAL {v:,.0f}")
                else:
                    run += v
    return out, notes

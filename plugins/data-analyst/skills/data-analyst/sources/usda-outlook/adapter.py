"""usda-outlook adapter: WASDE monthly CSVs (USDA OCE/WAOB), ERS farm income releases and the USDA Agricultural
Baseline (ERS). Published files only, no key (verified 2026-10-30).

- WASDE: one long-format CSV per report month at a predictable name,
  https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-YYYY-MM.csv (2021-01 on; HEAD answers
  Content-Length 0, so it is fetched with GET). Months with no CSV at that name: 2025-10 (no October 2025 report,
  lapse in federal funding) and 2026-05 / 2026-06 (reissued as corrected "v2" reports; HTTP 404 at the name).
  "latest" = the newest report date on the ESMIS publication page (esmis.nal.usda.gov), stepping back a month when
  its CSV is not posted yet (USDA posts it the day after the release). The usda.gov WASDE web pages answer scripts
  with HTTP 403 (Akamai "Access Denied"); the files do not, and the pack never reads those pages.
- Farm income (ERS): the data-files page links one ZIP per release whose media id changes ("September 3, 2026
  release" -> /media/29517/september-3-2026-release.zip), so links are scraped by their text. The ZIP holds one
  CSV of 111 MB (cp1252: unit_desc is padded with non-breaking spaces); it is streamed and filtered row by row,
  never loaded whole. Releases up to November 2017 are plain CSVs. The same page links the one-page summary
  workbook ("U.S. farm sector financial indicators, 2019-2026F") and the forecast archive ZIP.
- Baseline (ERS): the Agricultural Baseline Database page links each edition's ZIP under "Most Recent Early-Release",
  "Most Recent Full-Release" and "Historic Full-Release" headings; both 2035 ZIPs share a file name and differ by
  media id, so the heading decides which is which.

Values are as published, except: unit_desc is stripped of the padding spaces; .xlsx floats are written in their
shortest exact form (369.31670200000002 -> 369.316702). Added columns: farm_income `release_date` (from the
file's PublicationDate) and `estimate_type` (forecast/estimate by ERS's stated rule), farm_income_summary
`section` and `unit`.
"""
from __future__ import annotations

import csv
import fnmatch
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
from core.http import DataError  # noqa: E402

WASDE_CSV = "https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-{ym}.csv"
WASDE_HIST = "https://www.usda.gov/oce/commodity-markets/wasde/historical-wasde-report-data"
ESMIS = "https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates"
ERS = "https://www.ers.usda.gov"
FIW_PAGE = ERS + "/data-products/farm-income-and-wealth-statistics/data-files-us-and-state-level-farm-income-and-wealth-statistics"
BASE_PAGE = ERS + "/data-products/agricultural-baseline-database"
WASDE_FIRST = "2021-01"
NO_REPORT = {"2025-10": "no October 2025 WASDE was issued (lapse in federal funding)"}
# known HTTP 404s at the usual CSV name (verified 2026-10-30); still requested, in case USDA posts them later
WASDE_GAPS = {**NO_REPORT,
              "2026-05": "the May 2026 report was reissued as a corrected v2 report; no CSV at the usual name",
              "2026-06": "the June 2026 report was reissued as a corrected v2 report; no CSV at the usual name"}
WASDE_MAX_MONTHS = 72
WASDE_COLS = ("WasdeNumber", "ReportDate", "ReportTitle", "Attribute", "Commodity", "Region", "MarketYear",
              "ProjEstFlag", "Value", "Unit")
FI_FIRST = date(2014, 8, 1)
MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), start=1)}
NUM = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
YM = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


# ---------------------------------------------------------------------- small helpers
def _norm(v) -> str:
    return " ".join(str(v or "").split()).lower()


def _flag(v) -> str:
    """ProjEstFlag 'Proj.', '(Proj.)', 'Est.', '(Est.)', '' -> proj / est / none."""
    s = re.sub(r"[().\s]", "", str(v or "")).lower()
    return s or "none"


def _list(v) -> list:
    if v in (None, ""):
        return []
    return [x for x in (v if isinstance(v, list) else [v]) if x not in (None, "")]


def _match(value, wanted: list) -> bool:
    return not wanted or _norm(value) in {_norm(w) for w in wanted}


def _num(v):
    """Shortest exact text of an .xlsx float (Excel keeps 15 significant digits)."""
    if v is None:
        return ""
    s = str(v).strip()
    if NUM.fullmatch(s) and ("." in s or "e" in s.lower()):
        try:
            d = Decimal("%.15g" % float(s))
        except (InvalidOperation, ValueError):
            return s
        t = format(d.normalize(), "f")
        return "0" if t in ("-0", "") else t
    return s


def _ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _ym_range(a: str, b: str) -> list[str]:
    out, cur = [], a
    while cur <= b and len(out) <= WASDE_MAX_MONTHS:
        out.append(cur)
        cur = _ym_add(cur, 1)
    return out


def _text(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _years(p: dict) -> tuple[int | None, int | None]:
    if p.get("year") not in (None, ""):
        return int(p["year"]), int(p["year"])
    return (int(p["start_year"]) if p.get("start_year") not in (None, "") else None,
            int(p["end_year"]) if p.get("end_year") not in (None, "") else None)


def _in_years(y, lo, hi) -> bool:
    try:
        y = int(str(y)[:4])
    except ValueError:
        return lo is None and hi is None
    return (lo is None or y >= lo) and (hi is None or y <= hi)


def _client(pack):
    return gr.client_for(pack)


# ---------------------------------------------------------------------- pages (links scraped by their text)
def page_links(body: str, base: str = ERS) -> list[dict]:
    out = []
    for m in re.finditer(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', body, re.S | re.I):
        href = html.unescape(m.group(1))
        url = href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href
        out.append({"url": url, "text": _text(m.group(2)), "pos": m.start()})
    return out


def _date_of(text: str):
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)"
                  r"\s+(\d{1,2}),?\s+(\d{4})", text, re.I)
    return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))) if m else None


def fiw_releases(body: str) -> list[dict]:
    """Release files on the farm income data-files page, newest first: {url, label, date, revised}."""
    out = []
    for a in page_links(body):
        m = re.match(r"(\w+ \d{1,2}, \d{4}) release(.*)$", a["text"], re.I)
        if m and re.search(r"\.(zip|csv)(\?|$)", a["url"], re.I):
            d = _date_of(m.group(1))
            if d:
                out.append({"url": a["url"], "label": a["text"], "date": d})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def fiw_link(body: str, starts: str) -> dict | None:
    for a in page_links(body):
        if a["text"].lower().startswith(starts.lower()) and "/media/" in a["url"]:
            return a
    return None


def baseline_editions(body: str) -> list[dict]:
    """Baseline ZIPs on the database page: {url, to_year, kind (early/full), updated}."""
    heads = [(m.start(), "early" if "early" in m.group(0).lower() else "full")
             for m in re.finditer(r"(Most Recent Early-Release|Most Recent Full-Release|Historic Full-Release)", body)]
    out = []
    for a in page_links(body):
        m = re.search(r"projections-to-(\d{4})\.zip", a["url"], re.I)
        if not m:
            continue
        kind = "full"
        for pos, k in heads:
            if pos < a["pos"]:
                kind = k
        upd = re.search(r"Last Updated\s*(\d{1,2}/\d{1,2}/\d{4})", _text(body[a["pos"]:a["pos"] + 1500]))
        out.append({"url": a["url"], "to_year": int(m.group(1)), "kind": kind,
                    "updated": upd.group(1) if upd else ""})
    return out


def pick_edition(eds: list[dict], edition) -> dict:
    e = str(edition or "latest").strip().lower()
    full = sorted([x for x in eds if x["kind"] == "full"], key=lambda x: x["to_year"], reverse=True)
    if e == "latest":
        if not full:
            raise DataError("no full-release baseline ZIP found on the Agricultural Baseline Database page", kind="api")
        return full[0]
    if e == "early":
        early = sorted([x for x in eds if x["kind"] == "early"], key=lambda x: x["to_year"], reverse=True)
        if not early:
            raise DataError("no early-release baseline ZIP is posted now (the early release comes in December and "
                            "is replaced by the full release in February): use edition \"latest\"", kind="not_found")
        return early[0]
    hit = [x for x in full if str(x["to_year"]) == e]
    if not hit:
        raise DataError(f"no baseline edition with projections to {edition} (posted: "
                        f"{', '.join(str(x['to_year']) for x in full)})", kind="not_found")
    return hit[0]


def pick_release(rels: list[dict], release) -> dict:
    r = str(release or "latest").strip()
    if not rels:
        raise DataError("no release files found on the ERS farm income data-files page", kind="api")
    if r.lower() == "latest":
        return rels[0]
    hit = [x for x in rels if x["date"].isoformat() == r or x["date"].isoformat()[:7] == r]
    if not hit:
        raise DataError(f"no farm income release on {r} (releases: "
                        f"{', '.join(x['date'].isoformat() for x in rels[:12])} ...)", kind="not_found")
    return hit[0]


class Site:
    """The three index pages, each read once per run."""

    def __init__(self, pack, client=None):
        self.c = client or _client(pack)
        self._pages = {}

    def page(self, url: str) -> str:
        if url not in self._pages:
            self._pages[url] = self.c.get(url).text()
        return self._pages[url]

    def wasde_latest(self) -> str:
        body = self.page(ESMIS)
        ds = re.findall(r"/publication/world-agricultural-supply-and-demand-estimates/(\d{4}-\d{2})-\d{2}", body)
        if not ds:
            raise DataError("no WASDE release dates found on the ESMIS publication page", kind="api")
        return max(ds)


# ---------------------------------------------------------------------- WASDE
def wasde_rows(data: bytes, label: str) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    rd = csv.DictReader(io.StringIO(text))
    miss = [c for c in WASDE_COLS if c not in (rd.fieldnames or [])]
    if miss:
        raise DataError(f"{label} is not a WASDE data CSV (no {', '.join(miss)} column; an HTML page in its place?)",
                        kind="api")
    return [{k: ("" if v is None else v) for k, v in r.items() if k is not None} for r in rd]


def wasde_filter(rows: list[dict], p: dict) -> list[dict]:
    keep_rel = str(p.get("include_reliability", "")).lower() in ("true", "1", "yes")
    pe = _list(p.get("proj_est"))
    out = []
    for r in rows:
        if not keep_rel and r.get("ReportTitle", "").lower().startswith("reliability"):
            continue
        if not (_match(r.get("Commodity"), _list(p.get("commodity"))) and _match(r.get("Region"), _list(p.get("region")))
                and _match(r.get("Attribute"), _list(p.get("attribute")))
                and _match(r.get("ReportTitle"), _list(p.get("report_title")))
                and _match(r.get("MarketYear"), _list(p.get("market_year")))):
            continue
        if pe and _flag(r.get("ProjEstFlag")) not in {str(x).lower() for x in pe}:
            continue
        out.append(r)
    return out


def wasde_notes(name: str, rows: list[dict]) -> list[str]:
    notes = []
    tables = sorted({r.get("ReportTitle", "") for r in rows})
    units = sorted({r.get("Unit", "") for r in rows})
    if len(tables) > 1 and len(rows) <= 5000:
        groups = {}
        for r in rows:
            groups.setdefault((r.get("Commodity"), r.get("Region"), r.get("Attribute"), r.get("MarketYear"),
                               r.get("WasdeNumber"), r.get("AnnualQuarterFlag")), set()).add(r.get("ReportTitle"))
        dup = [k for k, v in groups.items() if len(v) > 1]
        if dup:
            c, rg, a = dup[0][:3]
            notes.append(f"{name}: the same item comes from {len(tables)} report tables ({'; '.join(tables[:4])}"
                         f"{' ...' if len(tables) > 4 else ''}), e.g. {c} / {rg} / {a}; units: {', '.join(units)}. "
                         "Do not add or compare across tables: set report_title (U.S. tables are in U.S. units such "
                         "as million bushels, world tables in million metric tons).")
    flags = {_flag(r.get("ProjEstFlag")) for r in rows}
    if "proj" in flags:
        notes.append(f"{name}: rows flagged Proj. are projections for the current marketing year and Est. are "
                     "estimates for the year before; they are revised in every monthly report (blank = history).")
    return notes


def wasde_months(site: Site, p: dict) -> tuple[list[str], bool]:
    """(months to read, latest?) for one request."""
    if p.get("start_report") not in (None, ""):
        end = p.get("end_report")
        end = site.wasde_latest() if end in (None, "", "latest") else str(end)
        return _ym_range(str(p["start_report"]), end), False
    rep = p.get("report")
    if rep in (None, "", "latest"):
        return [site.wasde_latest()], True
    return [str(rep)], False


def fetch_wasde(site: Site, r: dict, p: dict, max_rows: int) -> tuple[list[dict], list[str]]:
    months, latest = wasde_months(site, p)
    notes, rows, missing = [], [], []
    if latest:
        tried = []
        for k in range(3):
            ym = _ym_add(months[0], -k)
            if ym in NO_REPORT:
                tried.append(f"{ym} ({NO_REPORT[ym]})")
                continue
            try:
                got = wasde_rows(site.c.get(WASDE_CSV.format(ym=ym)).body, f"the {ym} WASDE CSV")
            except DataError as e:
                if e.kind not in ("not_found", "fixture_missing"):
                    raise
                tried.append(f"{ym} ({WASDE_GAPS.get(ym, 'CSV not posted yet')})")
                continue
            if tried:
                notes.append(f"{r['name']}: newest report listed is {months[0]}, but its CSV is not available "
                             f"({'; '.join(tried)}); used the {ym} report")
            notes.append(f"{r['name']}: report = {ym} (WASDE {got[0]['WasdeNumber'] if got else '?'}, released "
                         f"{got[0].get('ReleaseDate', '') if got else '?'})")
            rows = wasde_filter(got, p)
            break
        else:
            raise DataError(f"request {r['name']!r}: no WASDE CSV found for the latest months ({'; '.join(tried)})",
                            kind="not_found")
    else:
        for ym in months:
            if ym in NO_REPORT:
                missing.append(f"{ym}: {NO_REPORT[ym]}")
                continue
            try:
                got = wasde_rows(site.c.get(WASDE_CSV.format(ym=ym)).body, f"the {ym} WASDE CSV")
            except DataError as e:
                if e.kind != "not_found":
                    raise
                missing.append(f"{ym}: {WASDE_GAPS.get(ym, 'no CSV at ' + WASDE_CSV.format(ym=ym) + ' (HTTP 404)')}")
                continue
            rows += wasde_filter(got, p)
            if len(rows) >= max_rows:
                break
        if missing and len(missing) == len(months):
            raise DataError(f"request {r['name']!r}: no WASDE CSV for {', '.join(months)} ({'; '.join(missing)}). "
                            f"Older or reissued months are in the consolidated CSV on {WASDE_HIST} (a browser page; "
                            "download it and upload it with --ingest)", kind="not_found")
        if missing:
            notes.append(f"{r['name']}: report month(s) skipped: {'; '.join(missing)}. The consolidated CSV on "
                         f"{WASDE_HIST} (browser download, then --ingest) may hold them (unverified)")
    if len(rows) > max_rows:
        notes.append(f"{r['name']}: stopped at {max_rows:,} rows (max_rows); the reports have more. Narrow the filter "
                     "or raise max_rows.")
        rows = rows[:max_rows]
    return rows, notes + wasde_notes(r["name"], rows)


# ---------------------------------------------------------------------- farm income
def release_date(pubdate: str):
    return _date_of(pubdate or "")


def estimate_type(year, rel) -> str:
    """ERS (archive readme, Sept 2026): a year is first forecast in February of that year, updated in late
    August/early September, late November/early December and the next February, and becomes an estimate in the
    late August/early September release of the next year."""
    if rel is None:
        return ""
    try:
        y = int(str(year)[:4])
    except ValueError:
        return ""
    if y >= rel.year or (y == rel.year - 1 and rel.month <= 4):
        return "forecast"
    return "estimate"


def _csv_stream(data: bytes, label: str):
    """A text stream over the CSV of a release (a ZIP with one CSV member, or the CSV itself)."""
    if data[:4] == b"PK\x03\x04":
        z = zipfile.ZipFile(io.BytesIO(data))
        members = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise DataError(f"{label}: no CSV in the ZIP ({', '.join(z.namelist()[:5])})", kind="api")
        return io.TextIOWrapper(z.open(members[0]), encoding="cp1252", errors="replace", newline=""), members[0]
    if data.lstrip()[:1] == b"<":
        raise DataError(f"{label}: an HTML page came back instead of the data file", kind="api")
    return io.StringIO(data.decode("cp1252", errors="replace"), newline=""), label


def farm_income_rows(data: bytes, p: dict, max_rows: int, label: str, rel_default=None) -> tuple[list[dict], dict]:
    f, member = _csv_stream(data, label)
    rd = csv.DictReader(f)
    need = ("Year", "State", "artificialKey", "VariableDescriptionTotal", "Amount")
    miss = [c for c in need if c not in (rd.fieldnames or [])]
    if miss:
        raise DataError(f"{label}: not an ERS farm income CSV (no {', '.join(miss)} column)", kind="api")
    var = {_norm(v) for v in _list(p.get("variable"))}
    key = {str(v).strip().upper() for v in _list(p.get("key"))}
    st = {str(v).strip().upper() for v in _list(p.get("state"))}
    srch = _norm(p.get("search")) if p.get("search") not in (None, "") else ""
    lo, hi = _years(p)
    out, read, truncated, rel = [], 0, False, None
    for row in rd:
        read += 1
        if st and str(row.get("State", "")).strip().upper() not in st:
            continue
        if key and str(row.get("artificialKey", "")).strip().upper() not in key:
            continue
        d = _norm(row.get("VariableDescriptionTotal"))
        if var and d not in var:
            continue
        if srch and srch not in d:
            continue
        if not _in_years(row.get("Year"), lo, hi):
            continue
        if len(out) >= max_rows:
            truncated = True
            break
        rel = rel or release_date(row.get("PublicationDate", ""))
        rec = {k: ("" if v is None else v.strip() if k == "unit_desc" else v) for k, v in row.items() if k is not None}
        rec["unit_desc"] = rec.get("unit_desc", "").replace("\xa0", " ").strip()
        rec["release_date"] = rel.isoformat() if rel else ""
        rec["estimate_type"] = estimate_type(row.get("Year"), rel)
        out.append(rec)
    return out, {"member": member, "read": read, "truncated": truncated, "release": rel}


def fetch_farm_income(site: Site, r: dict, p: dict, max_rows: int) -> tuple[list[dict], list[str]]:
    rel = pick_release(fiw_releases(site.page(FIW_PAGE)), p.get("release"))
    data = site.c.get(rel["url"]).body
    rows, info = farm_income_rows(data, p, max_rows, os.path.basename(rel["url"].split("?")[0]), rel["date"])
    notes = [f"{r['name']}: ERS farm income release of {rel['date'].isoformat()} ({rel['label']}; {info['member']}); "
             "amounts are nominal (ChainType_GDP_Deflator, latest year = 100, deflates them); estimate_type follows "
             "ERS's rule (forecast until the late-August/September release of the next year)"]
    fc = sorted({x["Year"] for x in rows if x["estimate_type"] == "forecast"})
    if fc:
        notes.append(f"{r['name']}: forecast year(s) {', '.join(fc)} (ERS marks them F); a forecast is revised in "
                     "the next releases")
    if info["truncated"]:
        notes.append(f"{r['name']}: stopped at {max_rows:,} rows (max_rows); the release has more matching rows. "
                     "Narrow variable, state or years.")
    return rows, notes


def archive_rows(data: bytes, p: dict, label: str) -> list[dict]:
    if data[:4] == b"PK\x03\x04":
        z = zipfile.ZipFile(io.BytesIO(data))
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise DataError(f"{label}: no CSV in the ZIP", kind="api")
        data = z.read(names[0])
    rd = csv.DictReader(io.StringIO(data.decode("cp1252", errors="replace")))
    if "release_number" not in (rd.fieldnames or []):
        raise DataError(f"{label}: not the ERS farm income forecast archive (no release_number column)", kind="api")
    items = {str(x).lower() for x in _list(p.get("item"))}
    lo, hi = _years(p)
    return [{k: ("" if v is None else v) for k, v in x.items() if k is not None} for x in rd
            if (not items or x.get("item", "").lower() in items) and _in_years(x.get("year"), lo, hi)]


def summary_rows(data: bytes) -> tuple[list[dict], list[str]]:
    """The one-sheet 'U.S. farm sector financial indicators' table: header row = the row starting with years."""
    sheets = gr.xlsx_sheets(data)
    grid = next(iter(sheets.values()))
    hi = next((i for i, row in enumerate(grid) if sum(1 for v in row[1:] if re.fullmatch(r"\d{4}F?", str(v or "").strip())) >= 3),
              None)
    if hi is None:
        raise DataError("the farm sector indicators workbook has no row of years", kind="api")
    cols = [str(v).strip() for v in grid[hi][1:] if v not in (None, "")]
    notes, out = [], []
    asof = next((row[2] for row in grid[:hi] if len(row) > 2 and str(row[1] or "").startswith("Data as of")), None)
    if asof and str(asof).isdigit():
        notes.append(f"data as of {date.fromordinal(date(1899, 12, 30).toordinal() + int(asof)).isoformat()}")
    section, unit = "", ""
    for row in grid[hi + 1:]:
        label = str(row[0] or "").strip() if row else ""
        vals = [row[i] if i < len(row) else None for i in range(1, len(cols) + 1)]
        texts = [str(v).strip() for v in vals if v not in (None, "") and not NUM.fullmatch(str(v).strip())
                 and str(v).strip() not in ("NA", "")]
        nums = [v for v in vals if v not in (None, "") and (NUM.fullmatch(str(v).strip()) or str(v).strip() == "NA")]
        if not label and not nums and not texts:
            continue
        if texts and not nums:
            unit = texts[0]
            if label:
                section = label
            continue
        if label and not nums:
            if label.startswith("Note:") or re.match(r"\d+/", label):
                notes.append(label)
            else:
                section = label
            continue
        rec = {"section": section, "item": label, "unit": unit}
        for c, v in zip(cols, vals):
            rec[c] = _num(v)
        out.append(rec)
    return out, notes


def _xlsx_header(grid: list[list], header_row) -> int:
    if header_row not in (None, ""):
        return int(header_row) - 1
    for i, row in enumerate(grid):
        if sum(1 for v in row if v not in (None, "")) >= 3:
            return i
    return 0


def baseline_member_rows(z: zipfile.ZipFile, p: dict, label: str) -> tuple[list[dict], str]:
    glob = str(p.get("member") or "Projections*.csv").lower()
    names = [n for n in z.namelist() if not n.endswith("/")]
    hit = [n for n in names if fnmatch.fnmatch(n.lower(), glob) or fnmatch.fnmatch(os.path.basename(n).lower(), glob)]
    if len(hit) != 1:
        raise DataError(f"{label}: member {p.get('member') or 'Projections*.csv'!r} matches {len(hit)} files; members: "
                        f"{'; '.join(names)}", kind="bad_request")
    m = hit[0]
    if m.lower().endswith(".csv"):
        data = z.read(m)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        com, att, yr = _list(p.get("commodity")), _list(p.get("attribute")), _list(p.get("year"))
        return [{k: ("" if v is None else v) for k, v in x.items() if k is not None} for x in rows
                if _match(x.get("commodity"), com) and _match(x.get("attribute"), att) and _match(x.get("year"), yr)], m
    if m.lower().endswith(".xlsx"):
        sheets = gr.xlsx_sheets(z.read(m))
        want = p.get("sheet") or 1
        try:
            name = list(sheets)[int(want) - 1] if str(want).isdigit() else want
            grid = sheets[name]
        except (IndexError, KeyError):
            raise DataError(f"{m}: no sheet {want!r} (sheets: {', '.join(sheets)})", kind="bad_request") from None
        h = _xlsx_header(grid, p.get("header_row"))
        recs = gr.grid_records({"sheet": 1, "header_row": h + 1}, {name: grid})
        return [{k: _num(v) for k, v in x.items()} for x in recs], m
    raise DataError(f"{m}: only the CSV and .xlsx members are read (this is {os.path.splitext(m)[1]})",
                    kind="bad_request")


def fetch_baseline(site: Site, r: dict, p: dict) -> tuple[list[dict], list[str]]:
    ed = pick_edition(baseline_editions(site.page(BASE_PAGE)), p.get("edition"))
    data = site.c.get(ed["url"]).body
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DataError(f"{ed['url']}: not a ZIP file (an HTML page in its place?)", kind="api") from None
    rows, m = baseline_member_rows(z, p, os.path.basename(ed["url"].split("?")[0]))
    notes = [f"{r['name']}: USDA Agricultural Projections to {ed['to_year']} ({ed['kind']} release"
             f"{', last updated ' + ed['updated'] if ed['updated'] else ''}), {m}. A conditional baseline under "
             "current law and policy, not a forecast; the first years follow the WASDE of the November before"]
    if ed["kind"] == "early":
        notes.append(f"{r['name']}: early-release tables (before the full report); the full release in February "
                     "can differ and adds farm income, trade and horticulture tables")
    return rows, notes


# ---------------------------------------------------------------------- hooks
def check_spec(spec):
    probs = []
    this = date.today()
    now_ym = f"{this.year:04d}-{this.month:02d}"
    for r in spec.get("requests") or []:
        p, n, e = r.get("params") or {}, r.get("name"), r.get("endpoint")
        if e == "wasde":
            for k in ("report", "start_report", "end_report"):
                v = p.get(k)
                if v in (None, "") or (k != "start_report" and v == "latest"):
                    continue
                if not YM.fullmatch(str(v)):
                    probs.append(f"request {n!r}: {k} must be YYYY-MM such as 2026-10" +
                                 (" or \"latest\"" if k != "start_report" else ""))
                elif str(v) < WASDE_FIRST:
                    probs.append(f"request {n!r}: {k} {v}: the monthly WASDE CSVs start with {WASDE_FIRST}; earlier "
                                 f"reports are in the consolidated CSV on {WASDE_HIST} (browser download, then --ingest)")
                elif str(v) > now_ym:
                    probs.append(f"request {n!r}: {k} {v} is in the future")
            if p.get("report") not in (None, "") and p.get("start_report") not in (None, ""):
                probs.append(f"request {n!r}: give report or start_report/end_report, not both")
            if p.get("end_report") not in (None, "") and p.get("start_report") in (None, ""):
                probs.append(f"request {n!r}: end_report needs start_report")
            s, t = str(p.get("start_report") or ""), str(p.get("end_report") or now_ym)
            if YM.fullmatch(s) and YM.fullmatch(t if t != "latest" else now_ym):
                k = len(_ym_range(s, t if t != "latest" else now_ym))
                if k > WASDE_MAX_MONTHS:
                    probs.append(f"request {n!r}: a range of more than {WASDE_MAX_MONTHS} reports; split it")
                elif t != "latest" and s > t:
                    probs.append(f"request {n!r}: end_report is before start_report")
        if e == "farm_income":
            if not any(p.get(k) not in (None, "", []) for k in ("variable", "key", "search")):
                probs.append(f"request {n!r}: farm_income needs variable, key or search (the release holds about "
                             "470,000 rows); e.g. \"variable\": \"Net farm income\"")
            for s in _list(p.get("state")):
                if not re.fullmatch(r"[A-Za-z]{2}", str(s)):
                    probs.append(f"request {n!r}: state {s!r} must be US or a two-letter state code")
            rel = str(p.get("release") or "latest")
            if rel != "latest" and not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", rel):
                probs.append(f"request {n!r}: release must be \"latest\", YYYY-MM-DD or YYYY-MM")
            elif rel != "latest" and rel[:7] < FI_FIRST.isoformat()[:7]:
                probs.append(f"request {n!r}: release files on the ERS page start with August 2014; older vintages "
                             "of net farm income, net cash income and cash receipts are in farm_income_archive")
        if e in ("farm_income", "farm_income_archive"):
            try:
                _years(p)
            except (TypeError, ValueError):
                probs.append(f"request {n!r}: year, start_year and end_year are four-digit years")
        if e == "baseline":
            ed = str(p.get("edition") or "latest").lower()
            if ed not in ("latest", "early") and not re.fullmatch(r"20\d\d", ed):
                probs.append(f"request {n!r}: edition must be \"latest\", \"early\" or the last projection year "
                             "(e.g. 2035)")
            hr = p.get("header_row")
            if hr not in (None, "") and not str(hr).isdigit():
                probs.append(f"request {n!r}: header_row is a 1-based row number")
    return probs


def fetch(spec, pack, route):
    site = Site(pack)
    tables, notes = {}, []
    default_max = int(pack.policy["limits"].get("max_rows") or 50000)
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        e = r.get("endpoint")
        mx = int(r.get("max_rows") or default_max)
        try:
            if e == "wasde":
                rows, nn = fetch_wasde(site, r, params, mx)
            elif e == "farm_income":
                rows, nn = fetch_farm_income(site, r, params, mx)
            elif e == "farm_income_summary":
                a = fiw_link(site.page(FIW_PAGE), "U.S. farm sector financial indicators")
                if not a:
                    raise DataError("no 'U.S. farm sector financial indicators' workbook on the data-files page",
                                    kind="api")
                rows, extra = summary_rows(site.c.get(a["url"]).body)
                nn = [f"{r['name']}: {a['text']} ({a['url']}); {'; '.join(extra[:1])}; values in the unit column "
                      "(billion dollars or percent); F = forecast"]
            elif e == "farm_income_archive":
                a = fiw_link(site.page(FIW_PAGE), "ERS Farm Income and Wealth Statistics Data Archive")
                if not a:
                    raise DataError("no data archive ZIP on the data-files page", kind="api")
                rows = archive_rows(site.c.get(a["url"]).body, params, os.path.basename(a["url"]))
                nn = [f"{r['name']}: ERS archive of past forecasts and estimates ({a['url']}); release_number 1-4 are "
                      "forecasts, 5 on estimates; ERS says it is a research file, not an official record"]
            elif e == "baseline":
                rows, nn = fetch_baseline(site, r, params)
            else:
                raise DataError(f"unknown endpoint {e!r}", kind="bad_request")
        except DataError as err:
            raise DataError(f"request {r['name']!r} ({e}): {err}", kind=err.kind,
                            status=getattr(err, "status", None)) from None
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check the filter values against the "
                            "published names (case is ignored; see source.md)", kind="bad_request")
        notes += nn
        tables[r["name"]] = rows
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    items = []
    site = None
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        e, n = r.get("endpoint"), r["name"]
        if e == "wasde":
            try:
                site = site or Site(pack)
                months, _ = wasde_months(site, params)
            except DataError:
                t = date.today()
                rep = params.get("report")
                months = [str(rep) if rep not in (None, "", "latest") else f"{t.year:04d}-{t.month:02d}"]
                if params.get("start_report"):
                    months = _ym_range(str(params["start_report"]), str(params.get("end_report") or months[0]))
            for ym in months:
                if ym in NO_REPORT:
                    continue
                url = WASDE_CSV.format(ym=ym)
                items.append({"name": f"{n}#{ym}", "request": n, "url": url, "filename": f"{n}_{ym}.csv",
                              "alt_filenames": [os.path.basename(url)], "needs_key": False, "method": "GET",
                              "body": None,
                              "note": f"the {ym} WASDE data CSV (if it answers 404, take the previous month; for "
                                      f"older or reissued months download the consolidated CSV on {WASDE_HIST})"})
            continue
        url, note, ext = None, "", "zip"
        try:
            site = site or Site(pack)
            if e == "farm_income":
                rel = pick_release(fiw_releases(site.page(FIW_PAGE)), params.get("release"))
                url, note = rel["url"], f"the ERS farm income {rel['label']} (ZIP of one large CSV)"
                ext = "csv" if url.lower().split("?")[0].endswith(".csv") else "zip"
            elif e in ("farm_income_summary", "farm_income_archive"):
                start = ("U.S. farm sector financial indicators" if e == "farm_income_summary"
                         else "ERS Farm Income and Wealth Statistics Data Archive")
                a = fiw_link(site.page(FIW_PAGE), start)
                if not a:
                    raise DataError("link not found", kind="api")
                url, note = a["url"], a["text"]
                ext = "xlsx" if e == "farm_income_summary" else "zip"
            else:
                ed = pick_edition(baseline_editions(site.page(BASE_PAGE)), params.get("edition"))
                url, note = ed["url"], f"USDA Agricultural Projections to {ed['to_year']} ({ed['kind']} release) ZIP"
        except DataError:
            page = BASE_PAGE if e == "baseline" else FIW_PAGE
            url = page
            note = {"farm_income": "open the page and download the release ZIP (e.g. 'September 3, 2026 release'); "
                                   "upload it unchanged",
                    "farm_income_summary": "open the page and download 'U.S. farm sector financial indicators' (.xlsx)",
                    "farm_income_archive": "open the page and download the 'ERS Farm Income and Wealth Statistics "
                                           "Data Archive' ZIP",
                    "baseline": "open the page and download the ZIP under 'Most Recent Full-Release' (or the edition "
                                "asked for)"}.get(e, "")
            ext = "xlsx" if e == "farm_income_summary" else "zip"
        base = os.path.basename(url.split("?")[0])
        items.append({"name": f"{n}#1", "request": n, "url": url, "filename": f"{n}.{ext}",
                      "also_ext": [".csv", ".zip", ".xlsx"], "alt_filenames": [base] if "." in base else [],
                      "needs_key": False, "method": "GET", "body": None, "note": note})
    return items


def parse_upload(pack, req, paths):
    _, p, _ = gr.resolve(pack, req)
    e = req.get("endpoint")
    mx = int(req.get("max_rows") or pack.policy["limits"].get("max_rows") or 50000)
    rows = []
    for path in sorted(paths):
        with open(path, "rb") as f:
            data = f.read()
        label = os.path.basename(path)
        try:
            if e == "wasde":
                got = wasde_rows(data, label)
                months = []
                if p.get("start_report"):
                    months = _ym_range(str(p["start_report"]), str(p.get("end_report") or "9999-12"))
                elif p.get("report") not in (None, "", "latest"):
                    months = [str(p["report"])]
                ym = lambda x: f"{int(x.get('ForecastYear') or 0):04d}-{int(x.get('ForecastMonth') or 0):02d}" \
                    if str(x.get("ForecastYear", "")).isdigit() and str(x.get("ForecastMonth", "")).isdigit() else ""
                if months:
                    got = [x for x in got if ym(x) in months]
                elif got and any(ym(x) for x in got):
                    newest = max(ym(x) for x in got)
                    got = [x for x in got if ym(x) == newest]
                rows += wasde_filter(got, p)
            elif e == "farm_income":
                got, _ = farm_income_rows(data, p, mx, label)
                rows += got
            elif e == "farm_income_archive":
                rows += archive_rows(data, p, label)
            elif e == "farm_income_summary":
                rows += summary_rows(data)[0]
            elif e == "baseline":
                if data[:4] == b"PK\x03\x04" and label.lower().endswith(".zip"):
                    rows += baseline_member_rows(zipfile.ZipFile(io.BytesIO(data)), p, label)[0]
                else:                                   # the member itself (CSV or .xlsx) uploaded on its own
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w") as z:
                        z.writestr(label, data)
                    rows += baseline_member_rows(zipfile.ZipFile(buf), {**p, "member": label}, label)[0]
        except (DataError, zipfile.BadZipFile) as err:
            raise DataError(f"{label}: {err}", kind="bad_request") from None
    if not rows:
        raise DataError(f"request {req['name']!r}: the uploaded file(s) have no rows matching the request",
                        kind="bad_request")
    return rows

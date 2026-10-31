"""irs-soi adapter: IRS Statistics of Income individual income tax files (ZIP code, county, state, migration).

SOI publishes each product as one all-states CSV per tax year (or filing-year pair) under
https://www.irs.gov/pub/irs-soi/ (verified 2026-10-30; no key, plain GET):
  zip_agi     {yy}zpallagi.csv / {yy}zpallnoagi.csv        TY2011-2023, 216 MB for TY2022 (streamed to a temp file)
  county_agi  {yy}incyallagi.csv / {yy}incyallnoagi.csv    TY2011-2023, 36 MB
  state_agi   {yy}in55cmcsv.csv (19-23), 18in55cmagi.csv, {yy}in54cmcsv.csv (12, 14-17)   0.8 MB; 2013 is .xlsx only
  migration   {state|county}{inflow|outflow}{y1y2}.csv     pairs 2011-2012 to 2022-2023; 0.1 MB (state), 4.5 MB (county)
A year not posted answers HTTP 404 (an HTML page).

This adapter
  - maps the spec (tax_year or start_year/end_year; migration y1 or start_y1/end_y1, level, direction) to file names,
  - keeps only the asked rows while reading (state, zipcode, county, agi_stub; migration: the focus state/county),
    so a 216 MB file never becomes a table, and optionally only the asked variables (columns),
  - upper-cases the column names (the files mix agi_stub/AGI_STUB, zipcode/ZIPCODE, mars1/MARS1, n1/N1 between
    products and years) and puts TAX_YEAR (or Y1_YEAR, Y2_YEAR for migration) first,
  - writes numbers without thousands separators and trailing zero decimals (the state files print "3,095,140",
    the county and ZIP files "40530.0000"); values are otherwise as published,
  - migration: the documented suppression code -1 in N1/N2/AGI becomes an empty cell named in SUPPRESSED, and
    ROW_TYPE says what each row is (totals, non-migrants, a flow, other flows), because the files mix summary
    rows with flows (state files even use code 97 for two different totals).
The files are cp1252 text (Doña Ana County); they are decoded as UTF-8 when valid, else cp1252.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import sys
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

BASE = "https://www.irs.gov/pub/irs-soi"
INDEX = {
    "zip_agi": "https://www.irs.gov/statistics/soi-tax-stats-individual-income-tax-statistics-zip-code-data-soi",
    "county_agi": "https://www.irs.gov/statistics/soi-tax-stats-county-data",
    "state_agi": "https://www.irs.gov/statistics/soi-tax-stats-historic-table-2",
    "migration": "https://www.irs.gov/statistics/soi-tax-stats-migration-data",
}
STATE_FILES = {2012: "12in54cmcsv.csv", 2014: "14in54cmcsv.csv", 2015: "15in54cmcsv.csv",
               2016: "16in54cmcsv.csv", 2017: "17in54cmcsv.csv", 2018: "18in55cmagi.csv"}   # 2019+: yyin55cmcsv.csv
STATE_ONLY_XLSX = {2013: "13in54cmcsv.xlsx"}
STATE_AREAS = {"US": "United States", "OA": "Other areas", "PR": "Puerto Rico"}   # extra STATE codes of state_agi
YEAR_KEYS = ("tax_year", "start_year", "end_year")
PAIR_KEYS = ("y1", "start_y1", "end_y1")
MIG_VALUES = ("N1", "N2", "AGI")
NUM = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?|-?\d+\.\d+")

_EPS: dict = {}
try:
    _EPS = packfiles.read_json(HERE, "endpoints.json")["endpoints"]
except (OSError, ValueError, KeyError, TypeError):
    _EPS = {}


# ---------------------------------------------------------------------- spec helpers
def _list(v) -> list:
    if v in (None, "", []):
        return []
    return [x for x in (v if isinstance(v, (list, tuple)) else [v]) if x not in (None, "")]


def _years(r: dict) -> list[int]:
    """Tax years (or migration y1 years) of a request; default the latest verified one."""
    p = r.get("params") or {}
    ep = _EPS.get(r.get("endpoint") or "") or {}
    one, start, end = PAIR_KEYS if r.get("endpoint") == "migration" else YEAR_KEYS
    if p.get(one) not in (None, ""):
        return [int(p[one])]
    if p.get(start) not in (None, "") or p.get(end) not in (None, ""):
        y0 = int(p.get(start) or p.get(end))
        y1 = int(p.get(end) or ep.get("last_year_verified") or y0)
        return list(range(y0, y1 + 1))
    return [int(ep.get("last_year_verified") or date.today().year - 3)]


def _agi_classes(p: dict) -> bool:
    v = p.get("agi_classes", True)
    return not (v is False or str(v).strip().lower() in ("false", "0", "no"))


def _file(endpoint: str, year: int, p: dict) -> str:
    yy = f"{year % 100:02d}"
    if endpoint == "zip_agi":
        return f"{yy}zpall{'agi' if _agi_classes(p) else 'noagi'}.csv"
    if endpoint == "county_agi":
        return f"{yy}incyall{'agi' if _agi_classes(p) else 'noagi'}.csv"
    if endpoint == "state_agi":
        return STATE_FILES.get(year, f"{yy}in55cmcsv.csv")
    if endpoint == "migration":
        return f"{p.get('level')}{p.get('direction')}{yy}{(year + 1) % 100:02d}.csv"
    raise DataError(f"unknown endpoint {endpoint!r}", kind="bad_request")


def _state(x, as_: str = "usps") -> str:
    p = os.path.join(HERE, "..", "..", "scripts", "analysis")
    if p not in sys.path:
        sys.path.insert(0, p)
    import combine
    return combine.state(x, as_)


def _states(p: dict, endpoint: str) -> tuple[set, set]:
    """(USPS codes, FIPS codes) of the spec's state filter; raises ValueError for an unknown state."""
    usps, fips = set(), set()
    for s in _list(p.get("state")):
        k = str(s).strip().upper()
        if endpoint == "state_agi" and k in ("US", "OA"):
            usps.add(k)
            continue
        usps.add(_state(s, "usps"))
        fips.add(_state(s, "fips"))
    return usps, fips


def _counties(p: dict, fips_states: set) -> tuple[set, set, set]:
    """(5-digit FIPS, 3-digit county codes, lower-case names) of the spec's county filter."""
    full, code, names = set(), set(), set()
    for c in _list(p.get("county")):
        s = str(c).strip()
        if s.isdigit() and len(s) == 5:
            full.add(s)
        elif s.isdigit() and len(s) <= 3:
            code.add(s.zfill(3))
        else:
            names.add(s.lower())
    return full, code, names


def check_spec(spec):
    probs = []
    this = date.today().year
    for r in spec.get("requests") or []:
        name, endpoint = r.get("name"), r.get("endpoint")
        if endpoint not in _EPS:
            continue                                   # generic resolve() names the unknown endpoint
        p = r.get("params") or {}
        ep = _EPS[endpoint]
        keys = PAIR_KEYS if endpoint == "migration" else YEAR_KEYS
        other = YEAR_KEYS if endpoint == "migration" else PAIR_KEYS
        wrong = [k for k in other if p.get(k) not in (None, "")]
        if wrong:
            probs.append(f"request {name!r}: {endpoint} takes {', '.join(keys)}, not {', '.join(wrong)}"
                         + (" (y1 = the first filing year of the pair: 2022 for 2022-2023)" if endpoint == "migration"
                            else ""))
            continue
        try:
            for k in keys:
                if p.get(k) not in (None, ""):
                    int(p[k])
            ys = _years(r)
        except (TypeError, ValueError):
            probs.append(f"request {name!r}: {', '.join(keys)} must be four-digit years")
            continue
        if not ys:
            probs.append(f"request {name!r}: the end year is before the start year")
            continue
        first, last = ep.get("first_year"), ep.get("last_year_verified")
        what = "filing-year pairs" if endpoint == "migration" else "tax years"
        if ys[0] < first:
            probs.append(f"request {name!r}: {endpoint} CSV files cover {what} from {first}"
                         + (f"-{first + 1}" if endpoint == "migration" else "") + f" (asked {ys[0]}); older years "
                         f"are zipped Excel files on {INDEX[endpoint]} (manual)")
        if ys[-1] > this - 1:
            probs.append(f"request {name!r}: {ys[-1]} cannot be posted yet (latest verified: {last})")
        if len(ys) > 15:
            probs.append(f"request {name!r}: {len(ys)} years is one file each; narrow the range")
        if endpoint == "state_agi":
            for y in ys:
                if y in STATE_ONLY_XLSX:
                    probs.append(f"request {name!r}: state data for tax year {y} is published only as "
                                 f"{BASE}/{STATE_ONLY_XLSX[y]} (not read here); use another year or the manual route")
        try:
            usps, fips = _states(p, endpoint)
        except ValueError as e:
            probs.append(f"request {name!r}: {e}")
            continue
        zips = [str(z).strip() for z in _list(p.get("zipcode"))]
        if endpoint == "zip_agi":
            if not usps and not zips:
                probs.append(f"request {name!r}: zip_agi needs state (or zipcode): the all-states file has about "
                             "166,000 rows of 165 columns")
            bad = [z for z in zips if not re.fullmatch(r"\d{5}", z)]
            if bad:
                probs.append(f"request {name!r}: zipcode must be 5-digit strings (got {bad})")
        elif zips:
            probs.append(f"request {name!r}: zipcode applies to zip_agi only")
        if p.get("county") not in (None, "", []):
            if endpoint not in ("county_agi", "migration") or (endpoint == "migration" and p.get("level") != "county"):
                probs.append(f"request {name!r}: county applies to county_agi and to migration with level county")
            full, code, names = _counties(p, fips)
            if (code or names) and not fips:
                probs.append(f"request {name!r}: a 3-digit county code or county name needs state (or give the "
                             "5-digit state+county FIPS, e.g. 24031)")
            if names and endpoint == "migration":
                probs.append(f"request {name!r}: migration takes county FIPS codes, not names ({sorted(names)})")
        stubs = _list(p.get("agi_stub"))
        if stubs:
            if endpoint == "migration":
                probs.append(f"request {name!r}: agi_stub does not apply to migration files")
            else:
                top = {"zip_agi": 6, "county_agi": 8, "state_agi": 10}[endpoint]
                low = 0 if endpoint == "state_agi" else 1
                if any(not str(s).strip().isdigit() or not low <= int(s) <= top for s in stubs):
                    probs.append(f"request {name!r}: agi_stub for {endpoint} is {low}-{top} (see source.md)")
                if endpoint in ("zip_agi", "county_agi") and not _agi_classes(p):
                    probs.append(f"request {name!r}: agi_stub needs agi_classes true (the no-class file has "
                                 "AGI_STUB 0 only)")
        if p.get("columns") not in (None, "", []) and endpoint == "migration":
            probs.append(f"request {name!r}: columns does not apply to migration files (9 columns)")
    return probs


# ---------------------------------------------------------------------- reading and filtering
def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _open_text(path: str):
    """A text stream of a downloaded file: UTF-8 when the whole file is valid UTF-8, else cp1252."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            for _ in iter(lambda: f.read(1 << 20), ""):
                pass
        return open(path, encoding="utf-8-sig", newline="")
    except UnicodeDecodeError:
        return open(path, encoding="cp1252", errors="replace", newline="")


def _clean(v: str) -> str:
    v = (v or "").strip()
    if NUM.fullmatch(v):
        v = v.replace(",", "")
        if "." in v:
            v = v.rstrip("0").rstrip(".")
            v = "0" if v in ("", "-", "-0") else v
    return v


def _row_type(row: dict, direction: str, level: str) -> str:
    """What a migration row is. Focus = the state/county whose inflow (Y2) or outflow (Y1) the file lists."""
    f, o = ("Y2", "Y1") if direction == "inflow" else ("Y1", "Y2")
    fs, os_ = row.get(f"{f}_STATEFIPS", ""), row.get(f"{o}_STATEFIPS", "")
    fc, oc = row.get(f"{f}_COUNTYFIPS", ""), row.get(f"{o}_COUNTYFIPS", "")
    other_name = (row.get(f"{o}_STATE_NAME") or row.get(f"{o}_COUNTYNAME") or "").lower()
    if level == "county" and fc == "000":
        return "state_summary"                         # 2011-2017 county files carried state header rows
    if os_ == "96":
        return "total_us_and_foreign"
    if os_ == "98":
        return "total_foreign"
    if os_ == "97":
        if level == "county":
            return {"000": "total_us", "001": "total_same_state", "003": "total_different_state"}.get(oc, "total")
        return "total_same_state" if "same state" in other_name else "total_us"
    if os_ == "57":
        return "foreign"
    if os_ in ("58", "59"):
        return "other_flows"
    if os_ == fs and (level == "state" or oc == fc):
        return "non_migrants"
    return "flow"


def _name_hit(published: str, names: set) -> bool:
    """County name match. The county files cut COUNTYNAME to 20 characters ("Prince George's Coun", TY2022), so a
    published name of that length also matches an asked name it begins."""
    pub = published.strip().lower()
    return any(pub == n or pub.removesuffix(" county") == n or (len(pub) >= 18 and n.startswith(pub))
               for n in names)


def _filter(endpoint: str, p: dict, head: list[str]):
    """A predicate on raw CSV rows (lists) for the spec's filters, given the upper-cased header."""
    ix = {h: i for i, h in enumerate(head)}
    usps, fips = _states(p, endpoint)
    tests = []

    def col(*names):
        for n in names:
            if n in ix:
                return ix[n]
        raise DataError(f"the file has no {names[0]} column (columns: {', '.join(head[:8])} ...): the layout "
                        "changed; see source.md", kind="api")

    if endpoint == "migration":
        f = "Y2" if p.get("direction") == "inflow" else "Y1"
        si = col(f"{f}_STATEFIPS")
        if fips:
            tests.append(lambda r: r[si].strip().zfill(2) in fips)
        if p.get("level") == "county" and _list(p.get("county")):
            ci = col(f"{f}_COUNTYFIPS")
            full, code, _ = _counties(p, fips)
            tests.append(lambda r: (r[si].strip().zfill(2) + r[ci].strip().zfill(3)) in full
                         or r[ci].strip().zfill(3) in code)
    else:
        if usps or fips:
            si = col("STATE")
            fi = ix.get("STATEFIPS")
            tests.append(lambda r: r[si].strip().upper() in usps
                         or (fi is not None and r[fi].strip().zfill(2) in fips))
        zips = {str(z).strip() for z in _list(p.get("zipcode"))}
        if zips and endpoint == "zip_agi":
            zi = col("ZIPCODE")
            tests.append(lambda r: r[zi].strip().zfill(5) in zips)
        if endpoint == "county_agi" and _list(p.get("county")):
            full, code, names = _counties(p, fips)
            sfi, ci, ni = col("STATEFIPS"), col("COUNTYFIPS"), col("COUNTYNAME")
            tests.append(lambda r: (r[sfi].strip().zfill(2) + r[ci].strip().zfill(3)) in full
                         or r[ci].strip().zfill(3) in code or _name_hit(r[ni], names))
        stubs = {str(int(s)) for s in _list(p.get("agi_stub"))}
        if stubs:
            ai = col("AGI_STUB")
            tests.append(lambda r: r[ai].strip() in stubs)
    return lambda r: all(t(r) for t in tests)


ID_COLS = {"STATEFIPS", "STATE", "ZIPCODE", "COUNTYFIPS", "COUNTYNAME", "AGI_STUB"}


def _read(endpoint: str, p: dict, year: int, text_rows, limit: int, notes: list, name: str) -> tuple[list, bool]:
    """Filtered, tidied rows of one file (an iterator of CSV rows, header first). Returns (rows, truncated)."""
    try:
        head = [h.strip().upper() for h in next(text_rows)]
    except StopIteration:
        return [], False
    if head and head[0].startswith("<"):
        raise DataError(f"{name}: got an HTML page instead of the CSV (the file moved or the host refused the "
                        "request)", kind="api")
    if endpoint != "migration" and "AGI_STUB" not in head:
        raise DataError(f"{name}: tax year {year} file has no AGI_STUB column (columns: {', '.join(head[:6])} ...); "
                        "the layout changed", kind="api")
    keep = None
    want = [str(c).strip().upper() for c in _list(p.get("columns"))]
    if want:
        missing = [c for c in want if c not in head]
        if missing:
            notes.append(f"{name}: tax year {year} has no {', '.join(missing)} (variables change between years; "
                         "see the year's users guide)")
        keep = [i for i, h in enumerate(head) if h in ID_COLS or h in want]
    ok = _filter(endpoint, p, head)
    direction, level = p.get("direction"), p.get("level")
    rows, truncated = [], False
    for raw in text_rows:
        if not raw or not any(x.strip() for x in raw):
            continue
        if not ok(raw):
            continue
        if len(rows) >= limit:
            truncated = True
            break
        idx = keep if keep is not None else range(len(head))
        rec = {head[i]: _clean(raw[i] if i < len(raw) else "") for i in idx}
        if endpoint == "migration":
            sup = [k for k in MIG_VALUES if rec.get(k) == "-1"]
            for k in sup:
                rec[k] = ""
            out = {"Y1_YEAR": year, "Y2_YEAR": year + 1, **rec, "SUPPRESSED": ";".join(sup),
                   "ROW_TYPE": _row_type(rec, direction, level)}
        else:
            out = {"TAX_YEAR": year, **rec}
        rows.append(out)
    return rows, truncated


def _label(endpoint: str, p: dict, y: int) -> str:
    if endpoint == "migration":
        return f"{p.get('level')} {p.get('direction')} {y}-{y + 1}"
    return f"tax year {y}"


def fetch(spec, pack, route):
    client = gr.client_for(pack)
    tables, notes = {}, []
    tmp = tempfile.mkdtemp(prefix="data-analyst-irs-soi-")    # outside the skill folder; removed below
    got: dict[str, str] = {}                                    # file name -> local path (one download per run)
    try:
        for r in spec["requests"]:
            ep, params, probs = gr.resolve(pack, r)
            if probs:
                raise DataError("; ".join(probs), kind="bad_request")
            endpoint = r["endpoint"]
            p = r.get("params") or {}
            limit = int(r.get("max_rows") or ep.get("max_rows") or pack.policy["limits"].get("max_rows") or 200000)
            rows, missing = [], []
            for y in _years(r):
                fname = _file(endpoint, y, p)
                if fname not in got:
                    dest = os.path.join(tmp, fname)
                    try:
                        if ep.get("stream"):
                            client.download(f"/{fname}", dest)
                        else:
                            resp = client.get(f"/{fname}")
                            with open(dest, "w", encoding="utf-8", newline="") as f:
                                f.write(_decode(resp.body))
                    except DataError as e:
                        if e.kind != "not_found":
                            raise DataError(f"request {r['name']!r} ({fname}): {e}", kind=e.kind, status=e.status)
                        missing.append(f"{_label(endpoint, p, y)} ({fname})")
                        continue
                    got[fname] = dest
                with _open_text(got[fname]) as f:
                    part, cut = _read(endpoint, p, y, csv.reader(f), limit - len(rows), notes, r["name"])
                rows += part
                if cut:
                    notes.append(f"{r['name']}: stopped at {limit:,} rows (max_rows); narrow the filter "
                                 "(state, county, agi_stub) or raise max_rows")
                    break
            if missing:
                last = (_EPS.get(endpoint) or {}).get("last_year_verified")
                raise DataError(f"request {r['name']!r}: not posted on irs.gov (HTTP 404): {', '.join(missing)}. "
                                f"Latest verified on 2026-10-30: {_label(endpoint, p, last)}; see {INDEX[endpoint]}",
                                kind="not_found")
            if not rows and not r.get("allow_empty"):
                raise DataError(f"request {r['name']!r} matched no rows: check state/county/zipcode/agi_stub "
                                "(see source.md); add \"allow_empty\": true if none is a correct answer",
                                kind="bad_request")
            notes.append(f"{r['name']}: {len(rows):,} rows from {', '.join(_file(endpoint, y, p) for y in _years(r))}"
                         + ("; amounts in thousands of dollars" if endpoint != "migration" else
                            "; AGI in thousands of dollars; SUPPRESSED names cells published as -1"))
            tables[r["name"]] = rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        endpoint = r.get("endpoint")
        if endpoint not in _EPS:
            continue
        p = r.get("params") or {}
        try:
            years = _years(r)
        except (TypeError, ValueError):
            continue
        for y in years:
            fname = _file(endpoint, y, p)
            items.append({"name": f"{r['name']}#{y}", "request": r["name"], "url": f"{BASE}/{fname}",
                          "filename": fname, "needs_key": False, "method": "GET", "body": None,
                          "note": f"{_label(endpoint, p, y)}: the all-states CSV, uploaded unchanged (--ingest keeps "
                                  f"the asked rows); listed on {INDEX[endpoint]}"})
    return items


def parse_upload(pack, req, paths):
    endpoint = req.get("endpoint")
    p = req.get("params") or {}
    years = _years(req)
    by_name = {_file(endpoint, y, p).lower(): y for y in years}
    rows, notes = [], []
    limit = int(req.get("max_rows") or (_EPS.get(endpoint) or {}).get("max_rows") or 200000)
    for path in sorted(paths):
        base = os.path.basename(path).lower()
        y = by_name.get(base) or next((yy for n, yy in by_name.items() if base.startswith(os.path.splitext(n)[0])),
                                      years[0] if len(years) == 1 else None)
        if y is None:
            raise DataError(f"{os.path.basename(path)}: cannot tell its year; upload the file under its published "
                            f"name ({', '.join(sorted(by_name))})", kind="bad_request")
        with _open_text(path) as f:
            part, cut = _read(endpoint, p, y, csv.reader(f), limit - len(rows), notes, req["name"])
        rows += part
    return rows

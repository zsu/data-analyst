"""snapqc adapter: the SNAP Quality Control public-use database (snapqcdata.net, Mathematica for USDA FNA).

There is no API (verified 2026-10-31). The site lists one group of files per fiscal year on /datafiles?page=N
(a "Year YYYY" heading, then rows Resource Type | Data format | File); the file URLs sit in upload folders by
month (/sites/default/files/2026-08/qcfy2024_csv.zip), so a fetch reads the listing and never builds a URL. CSV
zips exist from FY 2014; earlier years are SAS, Stata and SPSS files this pack does not read.

What the listing showed on 2026-10-31, and how the adapter handles it:
  - FY 2018 lists two CSV zips (2020-07/qcfy2018_csv.zip and 2020-08/qcfy2018_csv_0.zip); the later one is the
    reweighted file (different weighted totals). Several CSVs of one database: the latest upload folder wins and a
    note names the other; two in the same folder is an error (no guessing).
  - FY 2020 has three databases: qcfy2020 (Oct 2019-Feb 2020 and Jun-Sep 2020; no QC reviews Mar-May 2020),
    qcfy2020_per1 (Oct-Feb) and qcfy2020_per2 (Jun-Sep). In the period files FYWGT is the full FY 2020 file's weight
    (HWGT / 9), so summing it over a period does not give that period's average month.
  - FY 2021 holds July-September 2021 only (FYWGT = HWGT / 3).
Each zip must hold one CSV named qc_pub_fy<YYYY>[_perN].csv. Gates on every read (a failure stops the request):
the required columns, unique column names, every row as long as the header, unique HHLDNO, YRMONTH inside the
fiscal year, positive numeric weights. summary also checks, in full-year files, that HWGT divided by the state's
number of sample months equals FYWGT on every row (how FNA defines it), and for FY 2024 compares the national
totals with the technical documentation.
"""
from __future__ import annotations

import csv
import html
import io
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EPS = packfiles.read_json(HERE, "endpoints.json") or {}

# The 53 SNAP state agencies in the file (STATE is the FIPS code without a leading zero; FY 2024 tech doc Table E.1)
AREAS = [("AL", 1, "Alabama"), ("AK", 2, "Alaska"), ("AZ", 4, "Arizona"), ("AR", 5, "Arkansas"),
         ("CA", 6, "California"), ("CO", 8, "Colorado"), ("CT", 9, "Connecticut"), ("DE", 10, "Delaware"),
         ("DC", 11, "District of Columbia"), ("FL", 12, "Florida"), ("GA", 13, "Georgia"), ("HI", 15, "Hawaii"),
         ("ID", 16, "Idaho"), ("IL", 17, "Illinois"), ("IN", 18, "Indiana"), ("IA", 19, "Iowa"), ("KS", 20, "Kansas"),
         ("KY", 21, "Kentucky"), ("LA", 22, "Louisiana"), ("ME", 23, "Maine"), ("MD", 24, "Maryland"),
         ("MA", 25, "Massachusetts"), ("MI", 26, "Michigan"), ("MN", 27, "Minnesota"), ("MS", 28, "Mississippi"),
         ("MO", 29, "Missouri"), ("MT", 30, "Montana"), ("NE", 31, "Nebraska"), ("NV", 32, "Nevada"),
         ("NH", 33, "New Hampshire"), ("NJ", 34, "New Jersey"), ("NM", 35, "New Mexico"), ("NY", 36, "New York"),
         ("NC", 37, "North Carolina"), ("ND", 38, "North Dakota"), ("OH", 39, "Ohio"), ("OK", 40, "Oklahoma"),
         ("OR", 41, "Oregon"), ("PA", 42, "Pennsylvania"), ("RI", 44, "Rhode Island"), ("SC", 45, "South Carolina"),
         ("SD", 46, "South Dakota"), ("TN", 47, "Tennessee"), ("TX", 48, "Texas"), ("UT", 49, "Utah"),
         ("VT", 50, "Vermont"), ("VA", 51, "Virginia"), ("WA", 53, "Washington"), ("WV", 54, "West Virginia"),
         ("WI", 55, "Wisconsin"), ("WY", 56, "Wyoming"), ("GU", 66, "Guam"), ("VI", 78, "Virgin Islands")]
BY_FIPS = {f: (a, n) for a, f, n in AREAS}
LOOKUP = {**{a.lower(): f for a, f, _ in AREAS}, **{n.lower(): f for _, f, n in AREAS},
          **{str(f): f for _, f, _ in AREAS}, **{f"{f:02d}": f for _, f, _ in AREAS},
          "u.s. virgin islands": 78, "us virgin islands": 78, "washington dc": 11}
NAME = re.compile(r"[A-Z][A-Z0-9_]*\*?")
_PAGES: dict[int, str] = {}
_ZIPS: dict[str, bytes] = {}


# ---------------------------------------------------------------------- spec parameters
def _endpoint(req: dict) -> str:
    return req.get("endpoint") or ""


def _years(v) -> list[int]:
    lo = EPS["csv_first_year"]
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        try:
            y = int(str(x).strip())
        except ValueError:
            raise ValueError(f"fiscal_year {x!r} is not a year") from None
        if not lo <= y <= 2100:
            raise ValueError(f"fiscal_year {y}: CSV files exist from FY {lo} (FY 1996-{lo - 1} are SAS, Stata and "
                             "SPSS files only; source.md Access)")
        out.append(y)
    if not out:
        raise ValueError("fiscal_year is required")
    return list(dict.fromkeys(out))


def _database(fy: int, period) -> str:
    period = (period or "full").strip().lower()
    if period not in ("full", "per1", "per2"):
        raise ValueError(f"period {period!r} is not full, per1 or per2")
    if period != "full" and fy != 2020:
        raise ValueError(f"period {period!r} exists for FY 2020 only")
    return f"qcfy{fy}" + ("" if period == "full" else f"_{period}")


def _states(v) -> set[int] | None:
    if v in (None, "", []):
        return None
    out = set()
    for x in (v if isinstance(v, list) else [v]):
        f = LOOKUP.get(str(x).strip().lower())
        if f is None:
            raise ValueError(f"state {x!r} is not one of the 53 SNAP state agencies (50 states, DC, Guam, Virgin "
                             "Islands; USPS code, FIPS code or name)")
        out.add(f)
    return out


def _month(v, fy: int) -> str | None:
    if v in (None, ""):
        return None
    m = str(v).strip()
    if not re.fullmatch(r"\d{6}", m) or m not in _fy_months(fy):
        raise ValueError(f"month {v!r}: give YYYYMM inside FY {fy} ({fy - 1}10 to {fy}09)")
    return m


def _fy_months(fy: int) -> list[str]:
    return [f"{fy - 1}{m:02d}" for m in (10, 11, 12)] + [f"{fy}{m:02d}" for m in range(1, 10)]


def _names(v, what: str) -> list[str] | str | None:
    if v in (None, "", []):
        return None
    if isinstance(v, str) and v.strip().lower() == "all":
        return "all"
    vals = v if isinstance(v, list) else re.split(r"[\s,]+", str(v).strip())
    out = []
    for x in vals:
        x = str(x).strip().upper()
        if not NAME.fullmatch(x):
            raise ValueError(f"{what}: {x!r} is not a variable name (letters, digits, _; a stem may end in *)")
        out.append(x)
    return out


def _bool(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, e, n = r.get("params") or {}, _endpoint(r), r.get("name")
        if e not in ("units", "persons", "summary"):
            continue
        try:
            for fy in _years(p.get("fiscal_year")):
                _database(fy, p.get("period"))
                _month(p.get("month"), fy)
            _states(p.get("state"))
            _names(p.get("variables"), "variables")
            if e == "persons":
                pv = _names(p.get("person_variables"), "person_variables")
                if pv == "all" or any(x.endswith("*") for x in pv or []):
                    probs.append(f"request {n!r}: person_variables are stems without a number or * (AGE, SEX)")
        except ValueError as ex:
            probs.append(f"request {n!r}: {ex}")
    return probs


# ---------------------------------------------------------------------- the listing
def listing_files(page_html: str) -> list[dict]:
    """Rows of one /datafiles page: {year, resource, format, path, size}, in page order."""
    start = page_html.find("view-content")
    out, year = [], None
    for m in re.finditer(r"<h3>(.*?)</h3>|<tr>(.*?)</tr>", page_html[max(start, 0):], re.S):
        if m.group(1) is not None:
            y = re.match(r"\s*Year\s+(\d{4})\b", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))))
            year = int(y.group(1)) if y else None
            continue
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        a = re.search(r'href="https?://snapqcdata\.net(/sites/default/files/[^"]+)"', m.group(2))
        if year is None or len(cells) < 3 or not a:
            continue
        out.append({"year": year, "resource": cells[0], "format": cells[1], "path": a.group(1),
                    "size": (re.search(r"length=(\d+)", m.group(2)) or [None, ""])[1]})
    return out


def _page(pack, i: int) -> str:
    if i not in _PAGES:
        L = EPS["listing"]
        _PAGES[i] = gr.client_for(pack).get(L["path"], {L["page_param"]: i}).body.decode("utf-8", "replace")
    return _PAGES[i]


def csv_candidates(rows: list[dict], fy: int, db: str) -> list[dict]:
    """The listing's QC Data CSV rows of one database (qcfy2020_per1 is not qcfy2020)."""
    out = []
    for r in rows:
        name = r["path"].rsplit("/", 1)[-1].lower()
        m = re.fullmatch(r"(qcfy\d{4}(?:_per\d)?)_csv(?:_\d+)?\.zip", name)
        if r["year"] == fy and r["resource"] == "QC Data" and r["format"] == "CSV" and m and m.group(1) == db:
            out.append(r)
    return out


def pick_csv(cands: list[dict], db: str) -> tuple[dict, list[str]]:
    if not cands:
        raise DataError(f"snapqc: the snapqcdata.net listing has no CSV file for {db} (CSV zips exist from FY "
                        f"{EPS['csv_first_year']}; a year not yet released is not listed)", kind="not_found")
    folder = lambda r: re.search(r"/files/(\d{4}-\d{2})/", r["path"]).group(1) if re.search(
        r"/files/(\d{4}-\d{2})/", r["path"]) else ""
    newest = max(folder(r) for r in cands)
    top = list({r["path"]: r for r in cands if folder(r) == newest}.values())
    if len(top) > 1:
        raise DataError(f"snapqc: the listing has several {db} CSV files uploaded in {newest} "
                        f"({', '.join(r['path'] for r in top)}); not guessing which is current", kind="api")
    older = sorted({r["path"] for r in cands} - {top[0]["path"]})
    notes = [f"the listing also has {', '.join(older)} for {db}; used the latest upload, {top[0]['path']}"] if older else []
    return top[0], notes


def find_csv(pack, fy: int, db: str) -> tuple[dict, list[str]]:
    rows = []
    for i in range(EPS["listing"]["max_pages"]):
        got = listing_files(_page(pack, i))
        rows += got
        if not got or min(r["year"] for r in got) < fy:   # the year's group is complete (years run newest first)
            break
    return pick_csv(csv_candidates(rows, fy, db), db)


def _zip(pack, path: str) -> bytes:
    if path not in _ZIPS:
        body = gr.client_for(pack).get(path).body
        if not body.startswith(b"PK"):
            raise DataError(f"snapqc: {path} did not return a zip file (see the listing at "
                            f"{EPS['base_url']}{EPS['listing']['path']})", kind="api")
        _ZIPS[path] = body
    return _ZIPS[path]


# ---------------------------------------------------------------------- reading one database
def member_name(db: str) -> str:
    return "qc_pub_" + db[2:] + ".csv"                     # qcfy2020_per1 -> qc_pub_fy2020_per1.csv


def csv_text(body: bytes, db: str) -> io.TextIOBase:
    """The database's CSV from a zip (or a bare CSV upload), as text."""
    want = member_name(db)
    if body[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(body))
        hits = [n for n in z.namelist() if n.rsplit("/", 1)[-1].lower() == want]
        if len(hits) != 1:
            raise DataError(f"snapqc: the {db} zip should hold one {want}; it holds {z.namelist()}", kind="api")
        return io.TextIOWrapper(z.open(hits[0]), encoding="latin-1", newline="")
    return io.StringIO(body.decode("latin-1"), newline="")


class Database:
    """One database's rows, gated. rows: list of dicts (only the columns asked for plus the ids)."""

    def __init__(self, body: bytes, fy: int, db: str, keep):
        self.fy, self.db = fy, db
        rd = csv.reader(csv_text(body, db))
        try:
            self.header = [h.strip().upper() for h in next(rd)]
        except StopIteration:
            raise DataError(f"snapqc: {member_name(db)} is empty", kind="api") from None
        if len(set(self.header)) != len(self.header):
            raise DataError(f"snapqc: {member_name(db)} repeats column names; not reading it", kind="api")
        miss = [c for c in EPS["required_columns"] if c not in self.header]
        if miss:
            raise DataError(f"snapqc: {member_name(db)} lacks {miss}: not the SNAP QC layout this pack reads", kind="api")
        cols = keep(self.header)
        ix = {c: i for i, c in enumerate(self.header)}
        months = set(_fy_months(fy))
        self.rows, seen, k = [], set(), len(self.header)
        for n, row in enumerate(rd, 2):
            if not row:
                continue
            if len(row) != k:
                raise DataError(f"snapqc: {member_name(db)} line {n} has {len(row)} fields, the header {k}; not "
                                "reading a misaligned file", kind="api")
            if row[ix["YRMONTH"]].strip() not in months:
                raise DataError(f"snapqc: {member_name(db)} line {n}: YRMONTH {row[ix['YRMONTH']]!r} is outside FY "
                                f"{fy}: wrong file", kind="api")
            hh = row[ix["HHLDNO"]].strip()
            if hh in seen:
                raise DataError(f"snapqc: {member_name(db)}: HHLDNO {hh} repeats (line {n})", kind="api")
            seen.add(hh)
            for w in ("HWGT", "FYWGT"):
                try:
                    ok = float(row[ix[w]]) > 0
                except ValueError:
                    ok = False
                if not ok:
                    raise DataError(f"snapqc: {member_name(db)} line {n}: {w} {row[ix[w]]!r} is not a positive "
                                    "number", kind="api")
            self.rows.append({c: row[ix[c]].strip() for c in cols})
        if not self.rows:
            raise DataError(f"snapqc: {member_name(db)} has no rows", kind="api")
        self.months = sorted({r["YRMONTH"] for r in self.rows})


def resolve_vars(spec_vars, header: list[str], defaults: list[str], fy: int) -> tuple[list[str], list[str]]:
    """Variables to keep, in the file's order for stems; (columns, notes). Unknown names asked for: an error."""
    if spec_vars == "all":
        return list(header), []
    H = set(header)
    if spec_vars is None:
        missing = [v for v in defaults if v not in H]
        return [v for v in defaults if v in H], ([f"default variables not in the FY {fy} file: {', '.join(missing)}"]
                                                 if missing else [])
    out, bad = [], []
    for v in spec_vars:
        if v.endswith("*"):
            hits = [c for c in header if re.fullmatch(re.escape(v[:-1]) + r"\d+", c)]
            out += hits
            if not hits:
                bad.append(v)
        elif v in H:
            out.append(v)
        else:
            bad.append(v)
    if bad:
        raise DataError(f"snapqc: not in the FY {fy} file: {', '.join(bad)} (variables change by year: --search "
                        "the catalog, and read that year's technical documentation)", kind="bad_request")
    return list(dict.fromkeys(out)), []


def _ids(header) -> list[str]:
    return [c for c in EPS["id_columns"] if c in header]


def _filter(rows, states, month):
    return [r for r in rows if (states is None or _fips(r["STATE"]) in states) and (month is None or r["YRMONTH"] == month)]


def _fips(v: str) -> int | None:
    try:
        return int(float(v))
    except ValueError:
        return None


def _abbr(v: str) -> str:
    return BY_FIPS.get(_fips(v), ("", ""))[0]


def unit_rows(db: Database, cols: list[str], states, month) -> list[dict]:
    lead = {"fiscal_year": db.fy, "database": db.db}
    return [{**lead, **{c: r[c] for c in _ids(db.header)}, "state_abbr": _abbr(r["STATE"]),
             **{c: r[c] for c in cols if c not in EPS["id_columns"]}} for r in _filter(db.rows, states, month)]


def person_stems(header: list[str], stems: list[str], fy: int) -> dict[str, list[int]]:
    out, bad = {}, []
    for s in stems:
        nums = sorted(int(re.search(r"(\d+)$", c).group(1)) for c in header if re.fullmatch(re.escape(s) + r"\d+", c))
        if nums:
            out[s] = nums
        else:
            bad.append(s)
    if bad:
        raise DataError(f"snapqc: no person-level columns {', '.join(s + '1..' for s in bad)} in the FY {fy} file "
                        "(--search the catalog; DIS64_ is a stem with its underscore)", kind="bad_request")
    return out


def person_rows(db: Database, stems: dict[str, list[int]], unit_cols: list[str], states, month,
                participants_only: bool) -> list[dict]:
    """One row per person slot that has any value in FSAFIL or the asked stems."""
    top = max(n for v in stems.values() for n in v)
    out = []
    for r in _filter(db.rows, states, month):
        base = {"fiscal_year": db.fy, "database": db.db, **{c: r[c] for c in _ids(db.header)},
                "state_abbr": _abbr(r["STATE"]), **{c: r[c] for c in unit_cols if c not in EPS["id_columns"]}}
        for i in range(1, top + 1):
            vals = {s: r.get(f"{s}{i}", "") for s in stems}
            if not any(vals.values()):
                continue
            if participants_only and r.get(f"FSAFIL{i}", "") != "1":
                continue
            out.append({**base, "person": i, **vals})
    return out


def summary_rows(db: Database, states, month) -> tuple[list[dict], list[str]]:
    """Weighted totals by state and U.S.: average month over the file's months (HWGT / the state's months), or one
    month (HWGT)."""
    per = "_per" in db.db
    months_of: dict[int, set] = {}
    for r in db.rows:
        months_of.setdefault(_fips(r["STATE"]), set()).add(r["YRMONTH"])
    notes, bad = [], 0
    if month is None and not per:
        for r in db.rows:
            w = float(r["HWGT"]) / len(months_of[_fips(r["STATE"])])
            if abs(w - float(r["FYWGT"])) > 1e-6 * float(r["FYWGT"]) + 1e-6:
                bad += 1
        if bad:
            raise DataError(f"snapqc: in {db.db}, HWGT / the state's sample months differs from FYWGT on {bad:,} rows: "
                            "the weights no longer follow the documented definition; not computing totals",
                            kind="api")
    groups: dict[int, list] = {}
    for r in _filter(db.rows, states, month):
        f = _fips(r["STATE"])
        w = float(r["HWGT"]) if month else float(r["HWGT"]) / len(months_of[f])
        groups.setdefault(f, []).append((w, float(r["FSUSIZE"] or 0), float(r["FSBEN"] or 0)))
    if not groups:
        raise DataError(f"snapqc: no rows in {db.db} for that state/month", kind="bad_request")
    if states is None:
        groups[0] = [x for f in list(groups) for x in groups[f]]
    span = f"{month}" if month else f"{db.months[0]}-{db.months[-1]} ({len(db.months)} months)"
    basis = "HWGT (one month)" if month else "HWGT / the state's sample months (= FYWGT in full-year files)"
    rows = []
    for f in sorted(groups, key=lambda x: (x == 0, BY_FIPS.get(x, ("ZZ",))[0])):
        g = groups[f]
        units, persons, ben = sum(w for w, _, _ in g), sum(w * s for w, s, _ in g), sum(w * b for w, _, b in g)
        a, nm = ("US", "United States") if f == 0 else BY_FIPS.get(f, ("", f"FIPS {f}"))
        for m, v, u in (("units", units, "SNAP units, average month"), ("persons", persons, "persons, average month"),
                        ("benefits", ben, "dollars a month"),
                        ("benefit_per_unit", ben / units, "dollars a month"),
                        ("benefit_per_person", ben / persons if persons else None, "dollars a month")):
            rows.append({"fiscal_year": db.fy, "database": db.db, "months": span, "state_fips": "" if f == 0 else f,
                         "state_abbr": a, "state_name": nm, "measure": m,
                         "value": "" if v is None else round(v, 4), "unit": u, "sample_units": len(g),
                         "weight": basis})
    vt = (EPS.get("verified_totals") or {}).get(db.db)
    if vt and states is None and month is None:
        us = {r["measure"]: r["value"] for r in rows if r["state_abbr"] == "US"}
        off = [f"{k} {us[k]:,.0f} vs {vt[k]:,}" for k in ("units", "persons", "benefits") if abs(us[k] - vt[k]) > 1]
        notes.append(f"U.S. totals differ from {vt['source'].split(',')[0]} ({'; '.join(off)}): the file may have been "
                     "reissued; read the current technical documentation" if off else
                     f"U.S. units, persons and benefits equal {vt['source'].split(';')[0]}")
    if per:
        notes.append(f"{db.db} is a period file: its FYWGT is the full FY 2020 file's weight (HWGT / 9), so these "
                     "totals use HWGT / the state's sample months in this period")
    small = sorted(r["state_abbr"] for r in rows if r["measure"] == "units" and r["sample_units"] < 100)
    if small:
        notes.append(f"fewer than 100 sample units: {', '.join(small)} (wide sampling error; no standard errors here)")
    return rows, notes


# ---------------------------------------------------------------------- the fetch
def _cap(rows, req, pack):
    cap = int(req.get("max_rows") or pack.policy["limits"]["max_rows"])
    if len(rows) > cap:
        return rows[:cap], [f"{req['name']}: stopped at {cap:,} rows (max_rows); the file has {len(rows):,} for this "
                            "request: filter by state or month, or raise max_rows"]
    return rows, []


def rows_for(req: dict, p: dict, fy: int, body: bytes, notes: list[str]) -> list[dict]:
    e, name = _endpoint(req), req["name"]
    db_name = _database(fy, p.get("period"))
    states, month = _states(p.get("state")), _month(p.get("month"), fy)
    if e == "summary":
        db = Database(body, fy, db_name, lambda h: _ids(h) + ["FSUSIZE", "FSBEN"])
        rows, ns = summary_rows(db, states, month)
    elif e == "units":
        holder = {}

        def keep(h):
            holder["cols"], holder["notes"] = resolve_vars(_names(p.get("variables"), "variables"), h,
                                                           EPS["default_variables"], fy)
            return list(dict.fromkeys(_ids(h) + holder["cols"]))
        db = Database(body, fy, db_name, keep)
        rows, ns = unit_rows(db, holder["cols"], states, month), holder["notes"]
    else:
        holder = {}

        def keep(h):
            uv = _names(p.get("variables"), "variables")
            holder["cols"], holder["notes"] = resolve_vars(uv, h, ["FSUSIZE", "FSBEN"], fy)
            stems = _names(p.get("person_variables"), "person_variables") or EPS["default_person_variables"]
            stems = list(dict.fromkeys((["FSAFIL"] if _bool(p.get("participants_only")) else []) + stems))
            holder["stems"] = person_stems(h, stems, fy)
            return list(dict.fromkeys(_ids(h) + holder["cols"] + [f"{s}{i}" for s, ns_ in holder["stems"].items()
                                                                    for i in ns_]))
        db = Database(body, fy, db_name, keep)
        rows = person_rows(db, holder["stems"], holder["cols"], states, month, _bool(p.get("participants_only")))
        ns = holder["notes"]
    notes += [f"{name}: check: {n}" for n in ns]
    span = f"{db.months[0]}-{db.months[-1]}, {len(db.months)} months"
    notes.append(f"{name}: {db_name} ({member_name(db_name)}, {len(db.rows):,} sample units, {span}): "
                 f"{len(rows):,} rows")
    return rows


def fetch(spec, pack, route):
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, p, probs = gr.resolve(pack, r)
        probs += check_spec({"requests": [r]})
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        rows = []
        for fy in _years(p.get("fiscal_year")):
            db_name = _database(fy, p.get("period"))
            hit, ns = find_csv(pack, fy, db_name)
            notes += [f"{r['name']}: check: {n}" for n in ns]
            rows += rows_for(r, p, fy, _zip(pack, hit["path"]), notes)
        tables[r["name"]], cut = _cap(rows, r, pack)
        notes += cut
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    base = EPS["base_url"].rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, p, probs = gr.resolve(pack, r)
        if probs or check_spec({"requests": [r]}) or _endpoint(r) not in ("units", "persons", "summary"):
            continue
        for i, fy in enumerate(_years(p.get("fiscal_year")), 1):
            db = _database(fy, p.get("period"))
            path = EPS["csv_files"].get(db)
            items.append({"request": r["name"], "name": f"{r['name']}#{i}", "needs_key": False, "method": "GET",
                          "body": None, "url": base + (path or EPS["listing"]["path"]),
                          "filename": f"{r['name']}_{db}_csv.zip",
                          "note": f"SNAP QC public-use data, FY {fy}{'' if db == f'qcfy{fy}' else f' ({db})'}, CSV zip. "
                                  f"If the link is gone, open {base}{EPS['listing']['path']}, find 'Year {fy}' and "
                                  "download 'QC Data | CSV'"})
    return items


def parse_upload(pack, req, paths):
    ep, p, probs = gr.resolve(pack, req)
    probs += check_spec({"requests": [req]})
    if probs:
        raise DataError("; ".join(probs), kind="bad_request")
    bodies = {}
    for path in sorted(paths):
        with open(path, "rb") as f:
            body = f.read()
        for fy in _years(p.get("fiscal_year")):
            db = _database(fy, p.get("period"))
            if body[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    if any(n.rsplit("/", 1)[-1].lower() == member_name(db) for n in z.namelist()):
                        bodies[fy] = body
            elif os.path.basename(path).lower().endswith(member_name(db)):
                bodies[fy] = body
    want = _years(p.get("fiscal_year"))
    if sorted(bodies) != sorted(want):
        raise DataError(f"request {req['name']!r} needs the SNAP QC CSV zip for FY {want} (holding "
                        f"{', '.join(member_name(_database(fy, p.get('period'))) for fy in want)}); the upload has "
                        f"{sorted(bodies) or 'none of them'}", kind="bad_request")
    notes, rows = [], []
    for fy in want:
        rows += rows_for(req, p, fy, bodies[fy], notes)
    return rows

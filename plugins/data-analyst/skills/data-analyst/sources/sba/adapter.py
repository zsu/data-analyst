"""sba adapter: SBA 7(a) and 504 loan-level FOIA files from data.sba.gov.

SBA posts each program as a few CSVs by approval fiscal year range on https://data.sba.gov/dataset/7a-504-foia
(verified 2026-11-11; no key, plain GET), for example
  7(a): FOIA_7a_FY1991_FY1999_asof_260930.csv ... FOIA_7a_FY2020_Present_asof_260930.csv   (147 to 319 MB)
  504:  FOIA_504_FY1991_FY2009_asof_260930.csv, FOIA_504_FY2010_Present_asof_260930.csv   (54 and 60 MB)
The as-of date (YYMMDD, a quarter end) in the names changes every quarter, so the names are read from the
dataset page; the list in endpoints.json ("files_verified") is used only when the page cannot be read.

This adapter
  - picks the files that hold the asked approval fiscal years and streams each to a temporary file once per run,
  - keeps only the asked rows while reading (fiscal years, borrower or project state, NAICS prefix, status,
    processing method), so a 300 MB file never becomes a table,
  - strips the padding SBA leaves in some values (Program " 7A") and adds loan_status_code, the published
    LoanStatus without spaces ("P I F" in the 7(a) files is PIF) and with both undisbursed codes (7(a) COMMIT,
    504 NOT FUNDED) as UNDISBURSED; LoanStatus itself stays as published,
  - cohorts: sums per ApprovalFY (and group_by columns) while reading: loan counts by status, gross approval and
    guaranteed dollars for all and for disbursed loans, and GrossChargeOffAmount.
"""
from __future__ import annotations

import csv
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

BASE = "https://data.sba.gov"
FILE_DIR = "/sites/default/files/uploaded_resources/"
FILE_RE = re.compile(r"FOIA_(7a|504)_FY(\d{4})_(?:FY(\d{4})|Present)_asof_(\d{6})\.csv", re.I)
PROGRAMS = {"7a": "7a", "7(a)": "7a", "504": "504"}
STATUS = {"PIF": "PIF", "CHGOFF": "CHGOFF", "CANCLD": "CANCLD", "COMMIT": "UNDISBURSED",
          "NOTFUNDED": "UNDISBURSED", "EXEMPT": "EXEMPT", "CLOSED": "CLOSED"}
STATUS_CODES = sorted(set(STATUS.values()))
GROUPS = ("BorrState", "ProjectState", "ProcessingMethod", "naics2", "NaicsCode", "BusinessType", "BusinessAge",
          "FixedorVariableInterestInd", "TermInMonths", "RevolverStatus", "SoldSecMrktInd", "loan_status_code")
NOT_DISBURSED = ("CANCLD", "UNDISBURSED")

_EP_FILE: dict = {}
try:
    _EP_FILE = packfiles.read_json(HERE, "endpoints.json")
except (OSError, ValueError, TypeError):
    _EP_FILE = {}
_EPS = _EP_FILE.get("endpoints") or {}


# ---------------------------------------------------------------------- spec helpers
def _list(v) -> list:
    if v in (None, "", []):
        return []
    return [x for x in (v if isinstance(v, (list, tuple)) else [v]) if x not in (None, "")]


def _program(p: dict) -> str:
    return PROGRAMS.get(str(p.get("program") or "").strip().lower().replace(" ", ""), "")


def _years(p: dict) -> tuple[set | None, int | None, int | None]:
    """(explicit years or None, start, end) of the approval fiscal year filter; None/None = every year."""
    if _list(p.get("approval_fy")):
        ys = {int(y) for y in _list(p["approval_fy"])}
        return ys, min(ys), max(ys)
    s = int(p["start_fy"]) if p.get("start_fy") not in (None, "") else None
    e = int(p["end_fy"]) if p.get("end_fy") not in (None, "") else None
    return None, s, e


def _state(x, as_: str = "usps") -> str:
    path = os.path.join(HERE, "..", "..", "scripts", "analysis")
    if path not in sys.path:
        sys.path.insert(0, path)
    import combine
    return combine.state(x, as_)


def _status_code(v: str) -> str:
    k = re.sub(r"\s+", "", (v or "").upper())
    return STATUS.get(k, k)


def check_spec(spec):
    probs = []
    this = date.today().year + 1
    for r in spec.get("requests") or []:
        name, endpoint = r.get("name"), r.get("endpoint")
        if endpoint not in _EPS:
            continue
        p = r.get("params") or {}
        if not _program(p):
            probs.append(f"request {name!r}: program must be 7a or 504 (got {p.get('program')!r})")
        try:
            ys, s, e = _years(p)
        except (TypeError, ValueError):
            probs.append(f"request {name!r}: approval_fy, start_fy and end_fy are fiscal years such as 2015")
            continue
        if s is None and e is None:
            probs.append(f"request {name!r}: give approval_fy or start_fy/end_fy (the full history of one program "
                         "is several files of 54 to 319 MB; ask for the years needed)")
        for y in [y for y in (s, e) if y is not None]:
            if not 1991 <= y <= this:
                probs.append(f"request {name!r}: approval fiscal year {y} is outside FY1991-FY{this}")
        if ys is not None and (p.get("start_fy") not in (None, "") or p.get("end_fy") not in (None, "")):
            probs.append(f"request {name!r}: give approval_fy or start_fy/end_fy, not both")
        for k in ("state", "project_state"):
            for st in _list(p.get(k)):
                try:
                    _state(st, "usps")
                except (KeyError, ValueError):
                    probs.append(f"request {name!r}: unknown {k} {st!r}")
        bad = [x for x in _list(p.get("loan_status")) if _status_code(str(x)) not in STATUS_CODES]
        if bad:
            probs.append(f"request {name!r}: loan_status {bad} not one of {', '.join(STATUS_CODES)}")
        if endpoint == "cohorts":
            badg = [g for g in _list(p.get("group_by")) if g not in GROUPS]
            if badg:
                probs.append(f"request {name!r}: group_by {badg} not one of {', '.join(GROUPS)}")
    return probs


# ---------------------------------------------------------------------- files
def _pick(names: list[str], program: str, start: int | None, end: int | None) -> list[str]:
    """The files of one program whose fiscal-year range overlaps [start, end], oldest first."""
    out = []
    for n in names:
        m = FILE_RE.fullmatch(n)
        if not m or m.group(1).lower() != program:
            continue
        lo = int(m.group(2))
        hi = int(m.group(3)) if m.group(3) else 9999
        if (end is None or lo <= end) and (start is None or hi >= start):
            out.append((lo, n))
    return [n for _, n in sorted(out)]


def _listed(client, notes: list) -> list[str]:
    """File names on the dataset page (newest as-of date per range); falls back to files_verified."""
    page = _EP_FILE.get("index_page") or "/dataset/7a-504-foia"
    try:
        html = client.get(page).body.decode("utf-8", "replace")
        found = {}
        for m in FILE_RE.finditer(html):
            k = (m.group(1).lower(), m.group(2), m.group(3) or "Present")
            if m.group(4) > found.get(k, ("",))[0]:
                found[k] = (m.group(4), m.group(0))
        if found:
            return [v[1] for v in found.values()]
        why = "no FOIA_*.csv link on the page"
    except DataError as e:
        why = str(e)
    fv = _EP_FILE.get("files_verified") or {}
    notes.append(f"sba: could not read the file list from {BASE}{page} ({why}); used the names verified on "
                 f"{fv.get('checked')} (as of {fv.get('as_of')}), which SBA replaces each quarter")
    return list(fv.get("7a") or []) + list(fv.get("504") or [])


def _as_of(fname: str) -> str:
    m = FILE_RE.fullmatch(fname)
    d = m.group(4) if m else ""
    return f"20{d[:2]}-{d[2:4]}-{d[4:]}" if len(d) == 6 else ""


# ---------------------------------------------------------------------- reading
def _matcher(p: dict, head: list[str]):
    ys, s, e = _years(p)
    states = {_state(x, "usps") for x in _list(p.get("state"))}
    pstates = {_state(x, "usps") for x in _list(p.get("project_state"))}
    naics = [str(x).strip() for x in _list(p.get("naics"))]
    status = {_status_code(str(x)) for x in _list(p.get("loan_status"))}
    methods = {str(x).strip().lower() for x in _list(p.get("processing_method"))}
    if "ApprovalFY" not in head:
        raise DataError("sba: the file has no ApprovalFY column (layout changed?): " + ", ".join(head[:12]),
                        kind="api")

    def ok(row: dict) -> bool:
        try:
            fy = int(row.get("ApprovalFY") or 0)
        except ValueError:
            return False
        if ys is not None and fy not in ys:
            return False
        if (s is not None and fy < s) or (e is not None and fy > e):
            return False
        if states and row.get("BorrState", "").upper() not in states:
            return False
        if pstates and row.get("ProjectState", "").upper() not in pstates:
            return False
        if naics and not any(row.get("NaicsCode", "").startswith(n) for n in naics):
            return False
        if status and row["loan_status_code"] not in status:
            return False
        if methods and row.get("ProcessingMethod", "").lower() not in methods:
            return False
        return True
    return ok


def _rows(f):
    """Rows of one file with values stripped and loan_status_code added."""
    rd = csv.reader(f)
    head = [h.strip().lstrip("﻿") for h in next(rd, [])]
    yield head
    for rec in rd:
        row = dict(zip(head, (v.strip() for v in rec)))
        row["loan_status_code"] = _status_code(row.get("LoanStatus", ""))
        yield row


def _num(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


class _Cohorts:
    def __init__(self, groups: list[str]):
        self.groups = groups
        self.cells: dict = {}

    def add(self, row: dict, as_of: str):
        g = []
        for k in self.groups:
            g.append(row.get("NaicsCode", "")[:2] if k == "naics2" else row.get(k, ""))
        key = (int(row["ApprovalFY"]), *g)
        c = self.cells.setdefault(key, {"as_of": as_of, "loans": 0, "gross_approval": 0.0, "sba_guaranteed": 0.0,
                                        "loans_disbursed": 0, "disbursed_gross_approval": 0.0,
                                        "disbursed_sba_guaranteed": 0.0, **{f"loans_{s.lower()}": 0
                                                                            for s in STATUS_CODES},
                                        "gross_chargeoff_amount": 0.0, "loans_other_status": 0})
        st = row["loan_status_code"]
        ga, gu = _num(row.get("GrossApproval")), _num(row.get("SBAGuaranteedApproval"))
        c["loans"] += 1
        c["gross_approval"] += ga
        c["sba_guaranteed"] += gu
        if st in STATUS_CODES:
            c[f"loans_{st.lower()}"] += 1
        else:
            c["loans_other_status"] += 1
        if st not in NOT_DISBURSED:
            c["loans_disbursed"] += 1
            c["disbursed_gross_approval"] += ga
            c["disbursed_sba_guaranteed"] += gu
        c["gross_chargeoff_amount"] += _num(row.get("GrossChargeOffAmount"))

    def table(self, program: str) -> list[dict]:
        out = []
        for key in sorted(self.cells, key=lambda k: tuple(str(x) for x in k)):
            c = self.cells[key]
            row = {"program": program, "ApprovalFY": str(key[0]), **dict(zip(self.groups, key[1:]))}
            for k, v in c.items():
                if program == "504" and k in ("sba_guaranteed", "disbursed_sba_guaranteed"):
                    continue                            # the 504 files have no guaranty column
                row[k] = _fmt(v) if isinstance(v, float) else v
            out.append(row)
        return out


def _read(endpoint: str, p: dict, f, fname: str, limit: int, rows: list, agg) -> bool:
    """Add the file's matching rows (or totals) to rows/agg; True when max_rows cut the table."""
    it = _rows(f)
    head = next(it)
    ok = _matcher(p, head)
    cols = [str(c) for c in _list(p.get("columns"))]
    if cols:
        unknown = [c for c in cols if c not in head and c != "loan_status_code"]
        if unknown:
            raise DataError(f"sba: unknown column(s) {unknown}; the {_program(p)} files have: {', '.join(head)}",
                            kind="bad_request")
        keep = [c for c in cols if c != "loan_status_code"] + ["loan_status_code"]
    as_of = _as_of(fname)
    for row in it:
        if not ok(row):
            continue
        if agg is not None:
            agg.add(row, row.get("AsOfDate") or as_of)
            continue
        if len(rows) >= limit:
            return True
        rows.append({k: row.get(k, "") for k in keep} if cols else row)
    return False


def _label(p: dict) -> str:
    ys, s, e = _years(p)
    span = (", ".join(str(y) for y in sorted(ys)) if ys is not None
            else f"{s or 1991}-{e if e is not None else 'latest'}")
    return f"SBA {'7(a)' if _program(p) == '7a' else '504'} loans approved FY{span}"


def fetch(spec, pack, route):
    client = gr.client_for(pack)
    tables, notes = {}, []
    tmp = tempfile.mkdtemp(prefix="data-analyst-sba-")         # outside the skill folder; removed below
    got: dict[str, str] = {}
    names = None
    try:
        for r in spec["requests"]:
            ep, params, probs = gr.resolve(pack, r)
            if probs:
                raise DataError("; ".join(probs), kind="bad_request")
            endpoint, p = r["endpoint"], r.get("params") or {}
            limit = int(r.get("max_rows") or ep.get("max_rows") or pack.policy["limits"].get("max_rows") or 200000)
            if names is None:
                names = _listed(client, notes)
            _, s, e = _years(p)
            files = _pick(names, _program(p), s, e)
            if not files:
                raise DataError(f"request {r['name']!r}: no {p.get('program')} file covers the asked years on "
                                f"{BASE}{_EP_FILE.get('index_page')}", kind="not_found")
            rows: list = []
            agg = _Cohorts([g for g in _list(p.get("group_by"))]) if endpoint == "cohorts" else None
            cut = False
            for fname in files:
                if fname not in got:
                    dest = os.path.join(tmp, fname)
                    try:
                        client.download(FILE_DIR + fname, dest)
                    except DataError as err:
                        raise DataError(f"request {r['name']!r} ({fname}): {err}", kind=err.kind, status=err.status)
                    got[fname] = dest
                with open(got[fname], encoding="utf-8-sig", errors="replace", newline="") as f:
                    cut = _read(endpoint, p, f, fname, limit, rows, agg)
                if cut:
                    notes.append(f"{r['name']}: stopped at {limit:,} rows (max_rows); narrow the filter (years, "
                                 "state, naics) or use the cohorts endpoint for totals")
                    break
            if agg is not None:
                rows = agg.table(_program(p))
            if not rows and not r.get("allow_empty"):
                raise DataError(f"request {r['name']!r} matched no loans: check the years, state, naics, "
                                "loan_status and processing_method values (see source.md); add \"allow_empty\": "
                                "true if none is a correct answer", kind="bad_request")
            notes.append(f"{r['name']}: {len(rows):,} rows ({_label(p)}) from {', '.join(files)}, data as of "
                         f"{_as_of(files[-1])}; dollars nominal")
            tables[r["name"]] = rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    items = []
    fv = _EP_FILE.get("files_verified") or {}
    names = list(fv.get("7a") or []) + list(fv.get("504") or [])
    for r in spec.get("requests") or []:
        if r.get("endpoint") not in _EPS:
            continue
        p = r.get("params") or {}
        try:
            _, s, e = _years(p)
        except (TypeError, ValueError):
            continue
        for fname in _pick(names, _program(p), s, e):
            items.append({"name": f"{r['name']}#{fname}", "request": r["name"], "url": f"{BASE}{FILE_DIR}{fname}",
                          "filename": fname, "needs_key": False, "method": "GET", "body": None,
                          "note": f"{_label(p)}: the published CSV, uploaded unchanged (--ingest keeps the asked "
                                  f"rows). If the link is gone, SBA has posted a newer quarter: take the file for the "
                                  f"same years from {BASE}/dataset/7a-504-foia"})
    return items


def parse_upload(pack, req, paths):
    endpoint, p = req.get("endpoint"), req.get("params") or {}
    limit = int(req.get("max_rows") or (_EPS.get(endpoint) or {}).get("max_rows") or 200000)
    rows: list = []
    agg = _Cohorts([g for g in _list(p.get("group_by"))]) if endpoint == "cohorts" else None
    for path in sorted(paths):
        m = FILE_RE.fullmatch(os.path.basename(path))
        if m and m.group(1).lower() != _program(p):
            continue
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            if _read(endpoint, p, f, os.path.basename(path), limit, rows, agg):
                break
    return agg.table(_program(p)) if agg is not None else rows

"""usda-fna adapter: SNAP State Options Report (dashboard CSV exports and PDFs), SNAP State Activity Reports (PDF), and
FNA's Program Data tables (XLSX: SNAP, WIC, child nutrition; read by program_data.py).

Neither report has an API (verified 2026-10-30). What this adapter reads:
  - The State Options Report dashboard is a Tableau view on publicdashboards.dl.usda.gov. Appending ".csv" to a
    view URL exports the view's first sheet, and URL parameters drive the export:
      StateAgencyComparison.csv?State 1 Param=<agency>&State 2 Param =<agency>&Edition=<label>
          every policy option of two agencies (column State names the agency; the second parameter's name ends in
          a space); waivers and demonstration projects are not in this view;
      OptionsProjectsWaivers.csv?Policy Option Param 1=<policy>&Option Selection Parameter 1=<selection>&Edition=<label>
          every agency that made that selection, waivers and projects included.
    An unknown parameter value is ignored silently (the view falls back to its default selection), so every
    exported row is checked against what was asked for.
  - The report PDFs (www.fna.usda.gov, redirected to an Azure Front Door host) are read with pypdf, an optional
    package. Pages come back as text (Unicode NFKC, so ligatures read as letters); the State Activity Report state
    tables for FY 2019-2023 are also parsed into tidy rows. pypdf prints each table either column by column (the 53
    agency names, then 53 values per column) or row by row (an agency name and its values on one line), and the
    layout changes between years and tables, so both are read. A table is kept only when its value count equals
    its column count and every column label's words appear in the printed header (endpoints.json
    activity_tables); otherwise its rows are dropped and the table is named as incomplete. Agency sums are checked
    against the printed U.S. row.
  - The Program Data workbooks are linked from three listing pages; their file names carry a release number that
    changes (snap-persons-10.xlsx), so each fetch reads the current name from the listing page (endpoints.json
    program_pages) and never builds it. program_data.py documents the layouts and their gates.
State agencies are the reports' 53 (50 states, DC, Guam, Virgin Islands) under the reports' own names, so the
table is here, not core.combine (pandas-only analysis code that names them differently).
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import unicodedata
import urllib.parse
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import Client, DataError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import program_data as pdx  # noqa: E402

EPS = packfiles.read_json(HERE, "endpoints.json") or {}

AGENCIES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"),
    ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"), ("FL", "Florida"),
    ("GA", "Georgia"), ("GU", "Guam"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"), ("IN", "Indiana"),
    ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"), ("MO", "Missouri"),
    ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"), ("NJ", "New Jersey"),
    ("NM", "New Mexico"), ("NY", "New York"), ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"),
    ("VI", "Virgin Islands"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"),
    ("WY", "Wyoming")]
NAME = dict(AGENCIES)
ABBR = {n.lower(): a for a, n in AGENCIES}
ALIASES = {"u.s. virgin islands": "VI", "us virgin islands": "VI", "dc": "DC", "washington dc": "DC",
           "washington, d.c.": "DC"}
AREAS = pdx.Areas(AGENCIES)                                  # + AS, PR, MP: the Program Data tables have them
BY_LENGTH = sorted(NAME.values(), key=len, reverse=True)     # "West Virginia" before "Virginia"
ABBR_CASE = set(NAME.values())
MISSING = ("N/A", "NA", "-", "--", "—", "–", "*", "")
RANGE = re.compile(r"FY\s*(\d{4})\s*[-–]\s*(?:FY\s*)?(\d{4})")
_CLIENTS: dict[str, Client] = {}
_LISTINGS: dict[str, str] = {}


# ---------------------------------------------------------------------- spec helpers
def agency(x) -> str:
    """USPS code of one of the 53 SNAP state agencies (name or code, any case)."""
    k = re.sub(r"\s+", " ", str(x or "")).strip().lower()
    if k.upper() in NAME:
        return k.upper()
    if k in ABBR:
        return ABBR[k]
    if k in ALIASES:
        return ALIASES[k]
    raise ValueError(f"unknown SNAP state agency {x!r}: give a USPS code (TX) or a name (Texas); the 53 agencies "
                     "are the 50 states, DC, Guam (GU) and the Virgin Islands (VI)")


def _agencies(v) -> list[str]:
    if v in (None, "", "all", "ALL", ["all"]):
        return [a for a, _ in AGENCIES]
    vals = v if isinstance(v, list) else [x for x in str(v).split(",") if x.strip()]      # "TX,CA"
    return list(dict.fromkeys(agency(x) for x in vals))


def _ints(v, lo: int, hi: int, what: str) -> list[int]:
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        try:
            n = int(str(x).strip())
        except ValueError:
            raise ValueError(f"{what} {x!r} is not a number") from None
        if not lo <= n <= hi:
            raise ValueError(f"{what} {n} is outside {lo}-{hi}")
        out.append(n)
    return out


def _pages(v) -> set[int] | None:
    if v in (None, ""):
        return None
    out = set()
    for part in str(v).replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"pages {v!r}: use page numbers like \"9-12\" or \"44,45\"")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out |= set(range(min(a, b), max(a, b) + 1))
    return out


def _edition(v, allowed=("16", "17")) -> str:
    e = str(v or "17").strip().lower().replace("th", "")
    if e not in allowed:
        raise ValueError(f"edition {v!r}: this endpoint has editions {', '.join(allowed)}")
    return e


def _policy(params: dict) -> dict:
    """The catalog entry of the requested policy (alias, or its exact name)."""
    p = str(params.get("policy") or "").strip()
    for alias, c in (EPS.get("catalog") or {}).items():
        if c.get("endpoint") == "option_states" and (p in (alias, c["id"]) or p.lower() == c["id"].lower()):
            return c
    raise ValueError(f"policy {p!r} is not in the catalog: use an alias or name from --endpoints / --search "
                     "(e.g. bbce, abawd_waiver, telephonic_signature_waiver)")


def _table(params: dict) -> tuple[str, dict]:
    """(key, definition) of a Program Data table (catalog entries with endpoint program_table)."""
    k = str(params.get("table") or "").strip()
    c = (EPS.get("catalog") or {}).get(k)
    if not c or c.get("endpoint") != "program_table":
        keys = [a for a, d in (EPS.get("catalog") or {}).items() if d.get("endpoint") == "program_table"]
        raise ValueError(f"table {k!r} is not a Program Data table; known: {', '.join(keys)}")
    return k, c


def _wic_measures(v) -> list[str]:
    sheets = EPS["wic_agency_sheets"]
    if v in (None, ""):
        return ["participants_total"]
    if v in ("all", ["all"]):
        return list(sheets)
    vals = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
    bad = [x for x in vals if x not in sheets]
    if bad:
        raise ValueError(f"unknown WIC measure(s) {bad}; known: {', '.join(sheets)}")
    return vals


def _endpoint(req: dict) -> str:
    e = req.get("endpoint")
    if not e and req.get("dataset") in (EPS.get("catalog") or {}):
        e = EPS["catalog"][req["dataset"]].get("endpoint")
    return e or ""


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, e, n = r.get("params") or {}, _endpoint(r), r.get("name")
        try:
            if e == "options_by_state":
                _agencies(p.get("state"))
                _edition(p.get("edition"))
            elif e == "option_states":
                c = _policy({"policy": p.get("policy") or r.get("dataset")})
                _edition(p.get("edition"))
                if p.get("selection") not in (None, "") and p["selection"] not in c["selections"]:
                    probs.append(f"request {n!r}: selection {p['selection']!r} is not one of {c['id']!r}'s: "
                                 + "; ".join(c["selections"]))
            elif e == "options_report":
                _edition(p.get("edition"), tuple(EPS.get("options_reports") or {}))
                if p.get("state") not in (None, ""):
                    agency(p["state"])
                _pages(p.get("pages"))
            elif e == "activity_tables":
                yrs = EPS.get("activity_parsed_years") or []
                _ints(p.get("fiscal_year"), min(yrs), max(yrs), "fiscal_year (parsed tables: FY 2019-2023; "
                      "other years: activity_report)")
                t = p.get("tables")
                if t not in (None, "", "all"):
                    bad = [x for x in (t if isinstance(t, list) else [t]) if x not in EPS["activity_tables"]]
                    if bad:
                        probs.append(f"request {n!r}: unknown table(s) {bad}; known: {', '.join(EPS['activity_tables'])}")
                if p.get("state") not in (None, ""):
                    _agencies(p["state"])
            elif e == "program_table":
                k, c = _table({"table": p.get("table") or r.get("dataset")})
                if p.get("state") not in (None, ""):
                    if c["layout"] in ("annual", "monthly"):
                        probs.append(f"request {n!r}: {k} is a national table; drop state (state tables: the "
                                     "catalog entries ending in _state or _latest)")
                    AREAS.codes(p["state"])
                if p.get("fiscal_year") not in (None, ""):
                    _ints(p["fiscal_year"], 1969, 2100, "fiscal_year")
            elif e == "snap_state_monthly":
                lo = EPS["snap_zip"]["first_xlsx_year"]
                _ints(p.get("fiscal_year"), lo, 2100, f"fiscal_year (FY {lo} onward; earlier years are .xls files "
                      "this pack does not read: snap_annual has national totals)")
                if p.get("state") not in (None, ""):
                    AREAS.codes(p["state"])
            elif e == "wic_agency_monthly":
                yrs = EPS["wic_agency_files"]["years"]
                fys = _ints(p.get("fiscal_year"), min(yrs), 2100, f"fiscal_year (FY {min(yrs)} onward)")
                if len(fys) > 1:
                    probs.append(f"request {n!r}: wic_agency_monthly takes one fiscal_year a request")
                _wic_measures(p.get("measures"))
                if p.get("state") not in (None, ""):
                    AREAS.codes(p["state"])
            elif e == "activity_report":
                _ints(p.get("fiscal_year"), 2004, 2023, "fiscal_year")
                if len(p["fiscal_year"] if isinstance(p.get("fiscal_year"), list) else [1]) > 1:
                    probs.append(f"request {n!r}: activity_report takes one fiscal_year a request")
                _pages(p.get("pages"))
        except ValueError as ex:
            probs.append(f"request {n!r}: {ex}")
    return probs


# ---------------------------------------------------------------------- network
def _tableau() -> Client:
    t = EPS["tableau"]
    if "tableau" not in _CLIENTS:
        _CLIENTS["tableau"] = Client(t["base_url"], name="usda-fna (publicdashboards.dl.usda.gov)", rps=1, burst=2,
                                     timeout=60)
    return _CLIENTS["tableau"]


def _view(view: str, query: dict) -> str:
    return f"{EPS['tableau']['workbook']}/{view}.csv?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def _get_view(path: str) -> bytes:
    try:
        return _tableau().get(path).body
    except DataError as e:
        if e.kind == "not_found":
            raise DataError(f"usda-fna: the State Options dashboard view is gone (HTTP 404): FNA has republished the "
                            f"dashboard ({EPS['tableau']['page']}), so endpoints.json tableau needs the new view "
                            "path. Until then use options_report (the PDF).", kind="not_found", status=404) from None
        raise


def _get_pdf(pack, path: str) -> bytes:
    body = gr.client_for(pack).get(path).body
    if not body.startswith(b"%PDF"):
        raise DataError(f"usda-fna: {path} did not return a PDF (the site may have moved the file; see the listing "
                        "pages in source.md Access)", kind="api")
    return body


def _listing(pack, program: str) -> str:
    if program not in _LISTINGS:
        _LISTINGS[program] = gr.client_for(pack).get(EPS["program_pages"][program]).body.decode("utf-8", "replace")
    return _LISTINGS[program]


def _listed(pack, program: str, rx: str, what: str) -> list[str]:
    """The resource-file paths on a listing page whose file name matches rx (whole name)."""
    found = re.findall(r'href="(/sites/default/files/resource-files/([^"/]+))"', _listing(pack, program))
    hits = list(dict.fromkeys(path for path, name in found if re.fullmatch(rx, name, re.I)))
    if not hits:
        raise DataError(f"usda-fna: no {what} link on {EPS['base_url']}{EPS['program_pages'][program]} (FNA may have "
                        "renamed or moved the file; endpoints.json program_pages / catalog stem need updating)",
                        kind="api")
    return hits


def _one(hits: list[str], what: str) -> str:
    if len(hits) > 1:
        raise DataError(f"usda-fna: the listing page links several {what} files ({', '.join(hits)}); not guessing "
                        "which is current", kind="api")
    return hits[0]


def _get_file(pack, path: str, kind: str) -> bytes:
    body = gr.client_for(pack).get(path).body
    if not body.startswith(b"PK"):
        raise DataError(f"usda-fna: {path} did not return an {kind} file (the site may have moved it; see the listing "
                        "pages in source.md Access)", kind="api")
    return body


# ---------------------------------------------------------------------- State Options Report: dashboard CSVs
def _csv_rows(body: bytes, need: set[str], what: str) -> list[dict]:
    text = body.decode("utf-8-sig", errors="replace")
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]
    head = set(rows[0]) if rows else {h.strip() for h in (text.splitlines() or [""])[0].split(",")}
    if not text.strip():
        return []
    if not need <= head:
        raise DataError(f"usda-fna: the dashboard's {what} export no longer has the columns {sorted(need - head)} "
                        f"(it has {sorted(head)[:8]}): the dashboard was changed; use options_report (the PDF)",
                        kind="api")
    return rows


def _pairs(abbrs: list[str]) -> list[tuple[str, str]]:
    names = [NAME[a] for a in abbrs]
    return [(names[i], names[i + 1] if i + 1 < len(names) else names[i]) for i in range(0, len(names), 2)]


def _by_state_query(pair, label) -> dict:
    return {"State 1 Param": pair[0], "State 2 Param ": pair[1], "Edition": label}


def by_state_rows(bodies: list[bytes], abbrs: list[str], label: str) -> tuple[list[dict], list[str]]:
    want = {NAME[a] for a in abbrs}
    out, seen = [], set()
    for b in bodies:
        for r in _csv_rows(b, {"State", "Option Selection", "Policy Option Field", "Counting Option Selection"},
                           "StateAgencyComparison"):
            st = r["State"]
            if st not in want:
                continue
            row = {"edition": label, "state": st, "state_abbr": agency(st), "policy_option": r["Policy Option Field"],
                   "option_selection": r["Option Selection"],
                   "agencies_with_selection": r["Counting Option Selection"],
                   "share_of_agencies": r.get("% Counting Option Selection", "")}
            key = tuple(row.values())
            if key not in seen:
                seen.add(key)
                out.append(row)
    got = {r["state"] for r in out}
    notes = []
    if want - got:
        notes.append(f"{gr.INCOMPLETE} no rows for {', '.join(sorted(want - got))} ({label})")
    return out, notes


def _selections(params: dict) -> tuple[dict, list[str]]:
    c = _policy(params)
    sel = params.get("selection")
    return c, ([sel] if sel not in (None, "") else list(c["selections"]))


def _option_query(policy: str, sel: str, label: str) -> dict:
    return {"Policy Option Param 1": policy, "Option Selection Parameter 1": sel, "Edition": label}


def option_rows(bodies: list[bytes], c: dict, sels: list[str], label: str) -> list[dict]:
    out = []
    for b in bodies:
        for r in _csv_rows(b, {"Edition", "Option Selection", "Policy Option", "State Agency"}, "OptionsProjectsWaivers"):
            if r["Policy Option"] != c["id"] or r["Option Selection"] not in sels or r["Edition"] != label:
                # the view ignored a parameter (an unknown value falls back to the default selection)
                raise DataError(f"usda-fna: asked for {c['id']!r} / {sels} ({label}) but the dashboard returned "
                                f"{r['Policy Option']!r} / {r['Option Selection']!r} ({r['Edition']}): the policy or "
                                "selection name no longer matches the dashboard; check it in the dashboard and "
                                "endpoints.json catalog", kind="api")
            out.append({"edition": label, "policy_option": r["Policy Option"], "option_selection": r["Option Selection"],
                        "state": r["State Agency"], "state_abbr": agency(r["State Agency"])})
    return out


# ---------------------------------------------------------------------- PDFs
def _pypdf():
    try:
        import pypdf
    except ImportError:
        raise DataError("usda-fna: reading the report PDFs needs the Python package pypdf (pip install pypdf). "
                        "Without it, the State Options rows come from options_by_state / option_states (no PDF).",
                        kind="install_deps") from None
    return pypdf


def pdf_pages(body: bytes) -> list[str]:
    reader = _pypdf().PdfReader(io.BytesIO(body))
    return [unicodedata.normalize("NFKC", p.extract_text() or "") for p in reader.pages]


def _lines(text: str) -> list[str]:
    ls = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]
    out = []
    for ln in ls:
        # a cell split by pypdf: "$ 5,860,922" and "$867, 586" are one number each
        ln = re.sub(r"(\d), (\d{3})(?!\d)", r"\1,\2", re.sub(r"\$ (?=[\d(])", "$", ln))
        ln = re.sub(r"(\d) %", r"\1%", ln)                   # "-56.1 %"
        prev = out[-1] if out else ""
        joined = f"{prev} {ln}"
        # an agency name printed on two lines ("District of" / "Columbia", "New" / "Hampshire")
        if prev and prev not in ABBR_CASE and any(joined == n or joined.startswith(n + " ") for n in BY_LENGTH
                                                    if n.startswith(prev + " ")):
            out[-1] = joined
        else:
            out.append(ln)
    return out


def profile_states(pages: list[str]) -> dict[int, str]:
    """{page index: agency name} for the State Agency Profile pages (16th and 17th editions): a page whose first
    line (after the running header) is an agency name followed by the policy table, and the pages that continue it."""
    out, cur = {}, None
    for i, t in enumerate(pages):
        ls = [ln for ln in _lines(t) if not ln.startswith("USDA Food and Nutrition Service")]
        if len(ls) >= 2 and ls[0] in ABBR_CASE and ls[1].startswith("Policy Option"):
            cur = ls[0]
            out[i] = cur
        elif cur and ls and re.match(r"(Waiver Implementation|Demonstration Project|Policy Option)", ls[0]):
            out[i] = cur
        else:
            cur = None
    return out


def report_rows(body: bytes, ed: str, params: dict) -> list[dict]:
    pages = pdf_pages(body)
    prof = profile_states(pages)
    keep = _pages(params.get("pages"))
    want = NAME[agency(params["state"])] if params.get("state") not in (None, "") else None
    if want and want not in prof.values():
        raise DataError(f"usda-fna: no State Agency Profile pages for {want} were found in edition {ed} (the 16th and "
                        "17th editions have them; older editions are laid out differently): request the edition "
                        "without state, or the pages you need", kind="bad_request")
    label = EPS["options_reports"][ed]["label"]
    return [{"edition": int(ed), "edition_label": label, "page": i + 1, "state": prof.get(i, ""), "text": t.strip()}
            for i, t in enumerate(pages)
            if (keep is None or i + 1 in keep) and (want is None or prof.get(i) == want)]


def report_year(pages: list[str]) -> int | None:
    """The State Activity Report's fiscal year: from the cover ("State Activity Report FY 2023" / "Fiscal Year
    2019"), else the latest year most table titles end with ("TABLE 5: ... - FY 2023")."""
    m = re.search(r"State Activity Report\s*(?:FY|Fiscal Year)\s*(\d{4})", re.sub(r"\s+", " ", pages[0] if pages else ""))
    if m:
        return int(m.group(1))
    ends = [max(map(int, re.findall(r"(?:FY\s*)?(\d{4})", ln))) for t in pages for ln in _lines(t)
            if re.match(r"(?i)TABLE\s+\d+\s*:", ln) and re.search(r"\d{4}", ln)]
    return max(set(ends), key=ends.count) if ends else None


def activity_page_rows(body: bytes, fy: int, params: dict) -> list[dict]:
    keep = _pages(params.get("pages"))
    out = []
    for i, t in enumerate(pdf_pages(body)):
        if keep is not None and i + 1 not in keep:
            continue
        title = next((ln for ln in _lines(t) if re.match(r"(?i)table\s+\d+", ln)), "")
        out.append({"report_fy": fy, "page": i + 1, "table_title": title, "text": t.strip()})
    return out


# ---------------------------------------------------------------------- SAR state tables
def _value(v: str) -> tuple[str, str]:
    """(value_num, value_flag) for a published cell: numbers lose $ , %; codes go to the flag."""
    s = v.strip()
    if s.upper() in MISSING:
        return "", s or "blank"
    neg = s.startswith("(") and s.endswith(")")
    x = s.strip("()").replace("$", "").replace(",", "").replace("%", "")
    try:
        f = float(x)
    except ValueError:
        return "", s
    f = -f if neg else f
    return (str(int(f)) if f == int(f) and "." not in x else repr(f)), ""


def _agency_line(ln: str) -> str | None:
    for n in BY_LENGTH:
        if ln == n or ln.startswith(n + " "):
            return n
    return None


def _head_has(head_ns: str, label: str) -> bool:
    return all(w.lower() in head_ns for w in re.findall(r"[A-Za-z0-9]+", label))


def parse_table_page(text: str, page: int, fy: int) -> tuple[str | None, list[dict], str | None]:
    """(table key, rows, problem) for one PDF page; (None, [], None) when it is not a parsed state table."""
    ls = _lines(text)
    t = next((i for i, ln in enumerate(ls) if re.match(r"(?i)TABLE\s+\d+\s*:", ln)), None)
    if t is None:
        return None, [], None
    a = next((i for i in range(t + 1, len(ls)) if _agency_line(ls[i])), None)
    if a is None:
        return None, [], None
    head = " ".join(ls[t:a])
    num = int(re.match(r"(?i)TABLE\s+(\d+)", ls[t]).group(1))
    m = re.match(r"(?i)(TABLE\s+\d+\s*:.*?FY\s*\d{4}(?:\s*[-–]\s*(?:FY\s*)?\d{4})?)", head)
    title = m.group(1) if m else ls[t]
    if re.search(r"(?i)ranked", head):
        return None, [], None
    rng = RANGE.search(head)
    defs = EPS["activity_tables"]
    keys = [k for k, d in defs.items()
            if re.search(d["title"], head, re.I) and not (d.get("not") and re.search(d["not"], head, re.I))
            and bool(d.get("multi")) == bool(rng)]
    if len(keys) != 1:
        return None, [], (f"FY {fy} page {page} {title!r}: " + ("no table definition matches" if not keys else
                                                               f"several definitions match ({', '.join(keys)})"))
    key, d = keys[0], defs[keys[0]]
    # values: column by column (the 53 names alone, then 53 values per column) or row by row (name and values)
    names, cells, total = [], [], None
    if ls[a] in NAME.values():
        i = a
        while i < len(ls) and ls[i] in NAME.values():
            names.append(ls[i])
            i += 1
        toks = []
        for ln in ls[i:]:
            if ln.startswith(("U.S.", "•", "Total ")):
                if ln.startswith("U.S."):
                    total = ln[4:].split()
                break
            toks += ln.split()
        k = len(toks) // len(names) if names else 0
        if names and len(toks) == k * len(names) + 1 and toks[-1].isdigit():
            toks = toks[:-1]                                  # the page number
        if not names or len(toks) % len(names):
            return key, [], f"FY {fy} page {page} {title!r}: {len(toks)} values for {len(names)} agencies"
        cells = [[toks[j * len(names) + r] for j in range(k)] for r in range(len(names))]
    else:
        for ln in ls[a:]:
            n = _agency_line(ln)
            if n:
                names.append(n)
                cells.append(ln[len(n):].split())
            elif ln.startswith("U.S."):
                total = ln[4:].split()
        k = max((len(c) for c in cells), default=0)
        for c in cells:
            if not c:                              # an agency printed without values (a footnote says missing)
                c += [""] * k
            elif len(c) == k - 1 and (d.get("blank_last") or d.get("multi")):
                c.append("")                       # a ratio left blank when its base is 0 (0 hearings last year)
        widths = {len(c) for c in cells}
        if len(widths) != 1:
            return key, [], f"FY {fy} page {page} {title!r}: rows have {sorted(widths)} values"
    if len(set(names)) != len(names) or len(names) != len(AGENCIES):
        return key, [], f"FY {fy} page {page} {title!r}: {len(set(names))} distinct agencies, expected {len(AGENCIES)}"
    if d.get("multi"):
        y0, y1 = int(rng.group(1)), int(rng.group(2))
        cols = [(d["multi"], y) for y in range(y0, y1 + 1)] + [(d["multi"] + "_percent_change", y1)]
        if len(cols) != k:
            return key, [], f"FY {fy} page {page} {title!r}: {k} columns, expected {len(cols)} for FY {y0}-{y1}"
    else:
        head_ns = re.sub(r"\s+", "", head).lower()
        v = next((v for v in d["variants"] if len(v) == k and all(_head_has(head_ns, lab) for _, lab in v)), None)
        if v is None:
            return key, [], (f"FY {fy} page {page} {title!r}: {k} value columns match no column list of "
                             f"{key} (endpoints.json activity_tables)")
        cols = [(meas, fy) for meas, _ in v]
    rows = []
    for n, cs in zip(names, cells):
        for (meas, dfy), val in zip(cols, cs):
            num_, flag = _value(val)
            rows.append({"report_fy": fy, "table": key, "table_number": num, "table_title": title, "page": page,
                         "state": n, "state_abbr": agency(n), "row_type": "state", "measure": meas, "data_fy": dfy,
                         "value": val, "value_num": num_, "value_flag": flag})
    problem = None
    if total and len(total) == k:
        sums = d.get("sum")
        idx = range(k) if sums is True else (sums or [])
        off = []
        for j in idx:
            if cols[j][0].endswith("_percent_change"):
                continue
            us, _ = _value(total[j])
            vals = [float(_value(c[j])[0]) for c in cells if _value(c[j])[0]]
            if us and vals and abs(sum(vals) - float(us)) > max(2.0, 0.002 * abs(float(us))):
                off.append(f"{cols[j][0]} ({sum(vals):,.0f} vs U.S. {total[j]})")
        for (meas, dfy), val in zip(cols, total):
            num_, flag = _value(val)
            rows.append({"report_fy": fy, "table": key, "table_number": num, "table_title": title, "page": page,
                         "state": "U.S.", "state_abbr": "", "row_type": "us_total", "measure": meas, "data_fy": dfy,
                         "value": val, "value_num": num_, "value_flag": flag})
        if off:
            problem = f"check: FY {fy} {key}: agency sum differs from the printed U.S. total: {'; '.join(off)}"
    return key, rows, problem


def activity_table_rows(pages: list[str], fy: int, params: dict) -> tuple[list[dict], list[str]]:
    want = params.get("tables")
    want = None if want in (None, "", "all") else set(want if isinstance(want, list) else [want])
    states = set(_agencies(params["state"])) if params.get("state") not in (None, "") else None
    rows, notes, found, bad = [], [], set(), []
    for i, t in enumerate(pages):
        key, rs, prob = parse_table_page(t, i + 1, fy)
        if want is not None and key not in want:
            continue
        if key and rs:
            found.add(key)
            rows += [r for r in rs if states is None or r["state_abbr"] in states]
        if prob and prob.startswith("check:"):
            notes.append(prob[7:])
        elif prob and key:
            bad.append(prob)
        elif prob and want is None:
            notes.append(f"not parsed: {prob} (read it with activity_report)")
    if bad:
        notes.append(f"FY {fy}{gr.INCOMPLETE} table(s) dropped because their values did not line up with their "
                     f"columns: {' | '.join(bad)}. Read those pages with activity_report.")
    if want:
        for k in sorted(want - found):
            if not any(k in b for b in bad):
                notes.append(f"FY {fy}: table {k} is not in this year's report")
    return rows, notes


# ---------------------------------------------------------------------- Program Data tables (XLSX)
def _keep(rows: list[dict], p: dict) -> list[dict]:
    if p.get("state") not in (None, ""):
        codes = set(AREAS.codes(p["state"]))
        rows = [x for x in rows if x["state_abbr"] in codes]
    if p.get("fiscal_year") not in (None, ""):
        fys = set(_ints(p["fiscal_year"], 1969, 2100, "fiscal_year"))
        rows = [x for x in rows if x["fiscal_year"] in fys]
    return rows


def _layout(f):
    try:
        return f()
    except pdx.LayoutError as ex:
        raise DataError(f"usda-fna: the workbook does not have the expected layout ({ex}); its rows were not read. "
                        "FNA changed the table: check it and update endpoints.json (catalog columns / title)",
                        kind="api") from None


def program_rows(k: str, c: dict, body: bytes, p: dict) -> tuple[list[dict], list[str]]:
    rows, notes = _layout(lambda: pdx.parse_table(k, c, body, AREAS))
    return _keep(rows, p), notes


def snap_zip_years(body: bytes, fys: list[int], what: str) -> dict[int, bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        raise DataError(f"usda-fna: {what} is not a zip file", kind="api") from None
    with z:
        out = {fy: pdx.snap_zip_member(z, fy) for fy in fys}
        missing = [fy for fy, b in out.items() if b is None]
        if missing:
            have = [y for y in pdx.snap_zip_years(z) if y >= EPS["snap_zip"]["first_xlsx_year"]]
            raise DataError(f"usda-fna: the SNAP data zip has no FY{missing[0] % 100:02d}.xlsx (its .xlsx years: "
                            f"{have})", kind="bad_request")
    return out


def snap_monthly_rows(bodies: dict[int, bytes], p: dict) -> tuple[list[dict], list[str]]:
    rows, notes = [], []
    for fy, body in sorted(bodies.items()):
        rs, ns = _layout(lambda: pdx.parse_snap_year(body, fy, AREAS))
        rows += rs
        notes += ns
    return _keep(rows, {"state": p.get("state")}), notes


def wic_agency_rows(body: bytes, fy: int, p: dict) -> tuple[list[dict], list[str]]:
    want = _wic_measures(p.get("measures"))
    book = _layout(lambda: pdx.read_book(body))
    rows, notes, seen = [], [], set()
    for name, rs, hidden in book:
        k = pdx.wic_sheet_key(name, EPS["wic_agency_sheets"])
        if k not in want:
            continue
        try:
            r2, n2 = pdx.parse_wic_sheet(k, EPS["wic_agency_sheets"][k], rs, hidden, fy, AREAS)
        except pdx.LayoutError as ex:
            notes.append(f"{gr.INCOMPLETE} sheet {name!r} dropped: {ex}")
            continue
        seen.add(k)
        rows += r2
        notes += n2
    for k in want:
        if k not in seen and not any(k in n for n in notes):
            notes.append(f"{gr.INCOMPLETE} no sheet for {k} in the FY {fy} WIC agency workbook")
    return _keep(rows, {"state": p.get("state")}), notes


def _named(name: str, notes: list[str]) -> list[str]:
    """Request-named notes: an incomplete one keeps the INCOMPLETE marker right after the name; the rest are checks."""
    out = []
    for n in notes:
        if gr.INCOMPLETE in n:
            out.append(f"{name}{n[n.index(gr.INCOMPLETE):]}")
        else:
            out.append(f"{name}: check: {n}")
    return out


# ---------------------------------------------------------------------- the fetch
def _fetch_one(pack, r: dict, params: dict) -> tuple[list[dict], list[str]]:
    e, p = _endpoint(r), params
    if e == "options_by_state":
        ed = _edition(p.get("edition"))
        label, abbrs = EPS["tableau"]["editions"][ed], _agencies(p.get("state"))
        bodies = [_get_view(_view("StateAgencyComparison", _by_state_query(pr, label))) for pr in _pairs(abbrs)]
        rows, notes = by_state_rows(bodies, abbrs, label)
        return rows, [f"{r['name']}{n}" if n.startswith(gr.INCOMPLETE) else n for n in notes] + [
            f"{r['name']}: {len(rows):,} rows for {len({x['state'] for x in rows})} agencies ({label}); policy options "
            "only (waivers and demonstration projects: option_states or options_report)"]
    if e == "option_states":
        ed = _edition(p.get("edition"))
        label = EPS["tableau"]["editions"][ed]
        c, sels = _selections({**p, "policy": p.get("policy") or r.get("dataset")})
        bodies = [_get_view(_view("OptionsProjectsWaivers", _option_query(c["id"], s, label))) for s in sels]
        rows = option_rows(bodies, c, sels, label)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r}: no agency has {c['id']!r} = {sels} in the {label}: the option may "
                            "not be in that edition (the catalog description says which)", kind="bad_request")
        n = len({x["state"] for x in rows})
        return rows, [f"{r['name']}: {c['id']} ({label}): {len(rows)} rows, {n} of 53 agencies"
                      + ("" if p.get("selection") else "; an agency missing here has no published selection")]
    if e == "options_report":
        ed = _edition(p.get("edition"), tuple(EPS["options_reports"]))
        rows = report_rows(_get_pdf(pack, EPS["options_reports"][ed]["url"]), ed, p)
        return rows, [f"{r['name']}: {EPS['options_reports'][ed]['label']}, {len(rows)} page(s) of text"]
    if e == "activity_report":
        fy = _ints(p.get("fiscal_year"), 2004, 2023, "fiscal_year")[0]
        rows = activity_page_rows(_get_pdf(pack, EPS["activity_reports"][str(fy)]), fy, p)
        return rows, [f"{r['name']}: SNAP State Activity Report FY {fy}, {len(rows)} page(s) of text"]
    if e == "program_table":
        k, c = _table({"table": p.get("table") or r.get("dataset")})
        path = _one(_listed(pack, c["program"], re.escape(c["stem"]) + r"-\d+\.xlsx", f"{k} ({c['stem']}-NN.xlsx)"), k)
        rows, notes = program_rows(k, c, _get_file(pack, path, "XLSX"), p)
        asof = rows[0]["data_as_of"] if rows else ""
        return rows, _named(r["name"], notes) + [f"{r['name']}: {k} from {path.rsplit('/', 1)[-1]} (data as of "
                                                  f"{asof or 'not stated'}): {len(rows):,} values"]
    if e == "snap_state_monthly":
        z = EPS["snap_zip"]
        path = _one(_listed(pack, "snap", re.escape(z["stem"]) + r"-\d+\.zip", "SNAP data zip"), "SNAP data zip")
        fys = _ints(p.get("fiscal_year"), z["first_xlsx_year"], 2100, "fiscal_year")
        rows, notes = snap_monthly_rows(snap_zip_years(_get_file(pack, path, "zip"), fys, path), p)
        return rows, _named(r["name"], notes) + [f"{r['name']}: SNAP by state and month, FY "
                                                  f"{', '.join(map(str, fys))}, from {path.rsplit('/', 1)[-1]}: "
                                                  f"{len(rows):,} values"]
    if e == "wic_agency_monthly":
        fy = _ints(p.get("fiscal_year"), 2022, 2100, "fiscal_year")[0]
        hits = _listed(pack, "wic", r"wicagencies\d{4}ytd\.xlsx", "WIC agency monthly")
        mine = [h for h in hits if h.lower().endswith(f"wicagencies{fy}ytd.xlsx")]
        if not mine:
            have = sorted(int(re.search(r"(\d{4})ytd", h).group(1)) for h in hits)
            raise DataError(f"request {r['name']!r}: the WIC page lists agency files for FY {have}, not FY {fy}",
                            kind="bad_request")
        rows, notes = wic_agency_rows(_get_file(pack, mine[0], "XLSX"), fy, p)
        return rows, _named(r["name"], notes) + [f"{r['name']}: WIC by agency and month, FY {fy}, from "
                                                  f"{mine[0].rsplit('/', 1)[-1]}: {len(rows):,} values"]
    if e == "activity_tables":
        rows, notes = [], []
        for fy in _ints(p.get("fiscal_year"), 2019, 2023, "fiscal_year"):
            rs, ns = activity_table_rows(pdf_pages(_get_pdf(pack, EPS["activity_reports"][str(fy)])), fy, p)
            rows += rs
            notes += ns
        tabs = sorted({x["table"] for x in rows})
        return rows, [f"{r['name']}: {n}" if gr.INCOMPLETE not in n else f"{r['name']}{n[n.index(gr.INCOMPLETE):]}"
                      for n in notes] + [f"{r['name']}: {len(rows):,} values from {len(tabs)} table(s)"]
    raise DataError(f"request {r['name']!r}: unknown endpoint {e!r}", kind="bad_request")


def fetch(spec, pack, route):
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        rows, ns = _fetch_one(pack, r, params)
        tables[r["name"]] = rows
        notes += ns
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    fna = EPS["base_url"].rstrip("/")
    tab = EPS["tableau"]["base_url"].rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, p, probs = gr.resolve(pack, r)
        if probs or check_spec({"requests": [r]}):
            continue
        e, name = _endpoint(r), r["name"]
        base = {"request": name, "needs_key": False, "method": "GET", "body": None}
        if e == "options_by_state":
            label = EPS["tableau"]["editions"][_edition(p.get("edition"))]
            for i, pr in enumerate(_pairs(_agencies(p.get("state"))), 1):
                items.append({**base, "name": f"{name}#{i}", "url": tab + _view("StateAgencyComparison",
                                                                                  _by_state_query(pr, label)),
                              "filename": f"{name}_{i:02d}.csv", "note": f"{pr[0]} and {pr[1]} ({label})"})
        elif e == "option_states":
            label = EPS["tableau"]["editions"][_edition(p.get("edition"))]
            c, sels = _selections({**p, "policy": p.get("policy") or r.get("dataset")})
            for i, s in enumerate(sels, 1):
                items.append({**base, "name": f"{name}#{i}", "url": tab + _view("OptionsProjectsWaivers",
                                                                                  _option_query(c["id"], s, label)),
                              "filename": f"{name}_{i:02d}.csv", "note": f"{c['id']}: {s} ({label})"})
        elif e == "program_table":
            k, c = _table({"table": p.get("table") or r.get("dataset")})
            page = fna + EPS["program_pages"][c["program"]]
            items.append({**base, "name": f"{name}#1", "url": f"{fna}/sites/default/files/resource-files/{c['stem']}-"
                          f"{EPS['program_release']}.xlsx", "filename": f"{name}_{c['stem']}.xlsx",
                          "note": f"{k}: {c['description']} (Excel). If the link is gone, the release number changed: "
                                  f"open {page} and download the Excel file of this table"})
        elif e == "snap_state_monthly":
            items.append({**base, "name": f"{name}#1", "url": fna + EPS["snap_zip"]["file"],
                          "filename": f"{name}_{EPS['snap_zip']['stem']}.zip",
                          "note": "SNAP data zip (persons, households, benefits by state and month, FY 1969 on). If "
                                  f"the link is gone, open {fna}{EPS['program_pages']['snap']} and download the "
                                  "'FY 69 through FY ..' zip"})
        elif e == "wic_agency_monthly":
            fy = _ints(p.get("fiscal_year"), 2022, 2100, "fiscal_year")[0]
            items.append({**base, "name": f"{name}#1", "url": fna + EPS["wic_agency_files"]["file"].format(fy=fy),
                          "filename": f"{name}_wicagencies{fy}ytd.xlsx",
                          "note": f"WIC monthly data by state agency, FY {fy} (Excel), from "
                                  f"{fna}{EPS['program_pages']['wic']}"})
        elif e in ("options_report", "activity_report", "activity_tables"):
            if e == "options_report":
                ed = _edition(p.get("edition"), tuple(EPS["options_reports"]))
                files = [(EPS["options_reports"][ed]["url"], EPS["options_reports"][ed]["label"])]
            else:
                files = [(EPS["activity_reports"][str(fy)], f"SNAP State Activity Report FY {fy}")
                         for fy in _ints(p.get("fiscal_year"), 2004, 2023, "fiscal_year")]
            for i, (path, label) in enumerate(files, 1):
                items.append({**base, "name": f"{name}#{i}", "url": fna + path,
                              "filename": f"{name}_{urllib.parse.unquote(path.rsplit('/', 1)[-1]).replace(' ', '_')}",
                              "note": f"{label} (PDF)"})
    return items


def parse_upload(pack, req, paths):
    ep, p, probs = gr.resolve(pack, req)
    if probs:
        raise DataError("; ".join(probs), kind="bad_request")
    e = _endpoint(req)

    def read(path):
        with open(path, "rb") as f:
            return f.read()
    paths = sorted(paths)
    if e == "program_table":
        k, c = _table({"table": p.get("table") or req.get("dataset")})
        for x in paths:
            body = read(x)
            if body[:2] == b"PK":
                try:
                    if pdx.find_sheet(pdx.read_book(body), c):
                        return program_rows(k, c, body, p)[0]
                except pdx.LayoutError:
                    continue
        raise DataError(f"request {req['name']!r} needs the Excel file of {k} ({c['stem']}-NN.xlsx); none of the "
                        "uploaded files is that table (checked by its title row)", kind="bad_request")
    if e == "snap_state_monthly":
        fys = _ints(p.get("fiscal_year"), EPS["snap_zip"]["first_xlsx_year"], 2100, "fiscal_year")
        bodies = {}
        for x in paths:
            body = read(x)
            if body[:2] != b"PK":
                continue
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                if "xl/workbook.xml" in z.namelist():           # one FYyy.xlsx uploaded on its own
                    fy = next((y for y in fys if f"Fiscal Year {y}" in " ".join(
                        pdx.norm(v) for r in pdx.read_book(body)[0][1][:3] for v in r if v)), None)
                    if fy:
                        bodies[fy] = body
                    continue
                for fy in fys:
                    b = pdx.snap_zip_member(z, fy)
                    if b is not None:
                        bodies[fy] = b
        if sorted(bodies) != sorted(fys):
            raise DataError(f"request {req['name']!r} needs the SNAP data zip (or its FYyy.xlsx files) for FY {fys}; "
                            f"the upload has {sorted(bodies) or 'none of them'}", kind="bad_request")
        return snap_monthly_rows(bodies, p)[0]
    if e == "wic_agency_monthly":
        fy = _ints(p.get("fiscal_year"), 2022, 2100, "fiscal_year")[0]
        for x in paths:
            body = read(x)
            if body[:2] != b"PK":
                continue
            try:
                book = pdx.read_book(body)
            except pdx.LayoutError:
                continue
            if any(re.search(rf"FISCAL YEAR {fy}\b", " ".join(pdx.norm(v) for r in rs[:4] for v in r if v), re.I)
                   and pdx.wic_sheet_key(n, EPS["wic_agency_sheets"]) for n, rs, _ in book):
                return wic_agency_rows(body, fy, p)[0]
        raise DataError(f"request {req['name']!r} needs wicagencies{fy}ytd.xlsx; no uploaded file is the FY {fy} WIC "
                        "agency workbook", kind="bad_request")
    if e == "options_by_state":
        abbrs = _agencies(p.get("state"))
        rows, notes = by_state_rows([read(x) for x in paths], abbrs, EPS["tableau"]["editions"][_edition(p.get("edition"))])
        if notes:
            raise DataError(f"the uploaded files hold {notes[0].split(gr.INCOMPLETE)[-1].strip()}: upload every "
                            "StateAgencyComparison file listed in INSTRUCTIONS.md", kind="bad_request")
        return rows
    if e == "option_states":
        c, sels = _selections({**p, "policy": p.get("policy") or req.get("dataset")})
        return option_rows([read(x) for x in paths], c, sels, EPS["tableau"]["editions"][_edition(p.get("edition"))])
    pdfs = [x for x in paths if read(x)[:4] == b"%PDF"]
    if not pdfs:
        raise DataError(f"request {req['name']!r} needs the report PDF; the upload has none", kind="bad_request")
    if e == "options_report":
        return report_rows(read(pdfs[0]), _edition(p.get("edition"), tuple(EPS["options_reports"])), p)
    years = _ints(p.get("fiscal_year"), 2004, 2023, "fiscal_year")
    by_year = {}
    for path in pdfs:                              # the agency's file names do not sort by year: read each PDF
        body = read(path)
        fy = report_year(pdf_pages(body))
        if fy in years:
            by_year[fy] = body
    if sorted(by_year) != sorted(years):
        raise DataError(f"request {req['name']!r} needs the State Activity Report PDF for FY {years}; the upload has "
                        f"{sorted(by_year) or 'none of them'} (the year is read from each PDF's cover or table titles)",
                        kind="bad_request")
    rows = []
    for fy in years:
        if e == "activity_report":
            rows += activity_page_rows(by_year[fy], fy, p)
        else:
            rows += activity_table_rows(pdf_pages(by_year[fy]), fy, p)[0]
    return rows

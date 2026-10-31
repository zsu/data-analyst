"""Federal Reserve Board data: Data Download Program (DDP) packages, the Z.1 bulk CSV zip, SCF chart data and tables.

DDP (www.federalreserve.gov/datadownload, verified 2026-10-30): Output.aspx?rel=<release>&series=<package id>
&lastobs=&from=MM/DD/YYYY&to=&filetype=csv&label=include&layout=seriescolumn&type=package returns a CSV whose first
rows are labels ("Series Description", "Unit:", "Multiplier:", "Currency:", "Unique Identifier:" - sometimes with a
trailing space - and "Time Period" with the series codes), then one row per date. A package id that is not one of
the release's packages answers HTTP 200 with an EMPTY body; an unknown release answers HTTP 400 with an HTML page.
The package ids are only listed on the release's Choose page (radio inputs, or <option>s for Z1's 435 packages),
so a package named by title is looked up there. ND in a cell = no data.

Z.1 bulk zip: csv/<table with . as _>.csv (date, then one column per series) and data_dictionary/<same>.txt
(tab-separated: series, description, "Line N", table title, units). z1_table_mapping.csv maps the new table codes
(S1M.b) to the published names they replaced (B.101).
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
from core.http import DataError  # noqa: E402

BASE = "https://www.federalreserve.gov"
CHOOSE = "/datadownload/Choose.aspx"
OUTPUT = "/datadownload/Output.aspx"
Z1_ZIP = "/releases/z1/current/z1_csv_files.zip"
Z1_MAP = "/releases/z1/current/z1_table_mapping.csv"
HASH = re.compile(r"[0-9a-f]{32}")
PKG = re.compile(r'<(?:option|input)\b[^>]*?\bvalue="(rel=[^"]*?series=([0-9a-f]{32})[^"]*)"[^>]*>'
                 r'(?:\s*<label[^>]*>)?([^<]+)<', re.I)
FLAGS = {"ND", "NC", "NA", "N/A"}
LABELS = {"series description": "description", "unit:": "unit", "multiplier:": "multiplier",
          "currency:": "currency", "unique identifier:": "unique_id"}
DDP_COLS = ["date", "series", "value", "value_flag", "description", "unit", "multiplier", "currency", "unique_id"]
Z1_COLS = ["date", "series", "value", "value_flag", "description", "line", "units", "table", "table_published"]


def check_error(status, body: bytes):
    if not body.strip():
        return ("the Board's server returned an empty file. For a Data Download Program package this means the "
                "package id is not one of the release's packages: list them with the ddp_packages endpoint (or give "
                "the package title) and check the release code.")
    return None


# ---------------------------------------------------------------------- DDP
def parse_packages(text: str, rel: str) -> list[dict]:
    out, seen = [], set()
    for value, h, label in PKG.findall(text):
        if h in seen:
            continue
        seen.add(h)
        label = " ".join(html.unescape(label).split())
        m = re.match(r"^(.*?)\s*\[([^\]]*)\]\s*$", label)
        title, listed = (m.group(1), m.group(2)) if m else (label, "")
        out.append({"rel": rel, "package_title": title, "package": h, "listed": listed})
    return out


class _Cache:
    def __init__(self):
        self.pkgs, self.blobs = {}, {}


def packages(c, rel: str, cache: _Cache) -> list[dict]:
    rel = rel.upper()
    if rel not in cache.pkgs:
        try:
            text = c.get(CHOOSE, {"rel": rel}).text()
        except DataError as e:
            if e.kind == "bad_request":
                raise DataError(f"release {rel!r} is not a Data Download Program release (HTTP 400). Codes: G19, Z1, "
                                "SLOOS, SCOOS, H8, H6, H15, H41, G17, G20, CHGDEL, DSR, FOR, CP, H10, H3, E2, PRATES",
                                kind="bad_request") from None
            raise
        cache.pkgs[rel] = parse_packages(text, rel)
        if not cache.pkgs[rel]:
            raise DataError(f"no preformatted packages found on the {rel} Choose page "
                            f"({BASE}{CHOOSE}?rel={rel}); the page layout may have changed", kind="api")
    return cache.pkgs[rel]


def match_package(pkgs: list[dict], want: str, rel: str) -> dict:
    w = " ".join(str(want).split()).lower()
    for test in (lambda t: t == w, lambda t: t.startswith(w), lambda t: w in t):
        hits = [p for p in pkgs if test(p["package_title"].lower())]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            exact = [p for p in hits if p["package_title"].lower() == w]
            if len(exact) == 1:
                return exact[0]
            names = "; ".join(p["package_title"] for p in hits[:12])
            raise DataError(f"package {want!r} matches {len(hits)} packages of {rel}: {names}"
                            + (" ..." if len(hits) > 12 else "") + ". Give the full title or the package id.",
                            kind="bad_request")
    names = "; ".join(p["package_title"] for p in pkgs[:15])
    raise DataError(f"no {rel} package titled {want!r}. Packages: {names}" + (" ..." if len(pkgs) > 15 else "")
                    + " (all of them: the ddp_packages endpoint)", kind="bad_request")


def date_key(v, end: bool = False) -> str:
    """A sortable YYYY-MM-DD for DDP and spec dates: 2026-08-14, 2026-08, 2026Q3, 2026:Q3, 2026 (end=True: the
    period's last day, for an inclusive "to")."""
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{'31' if end else '01'}"
    m = re.fullmatch(r"(\d{4}):?Q([1-4])", s, re.I)
    if m:
        q = int(m.group(2))
        return f"{m.group(1)}-{3 * q if end else 3 * q - 2:02d}-{'31' if end else '01'}"
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return f"{s}-12-31" if end else f"{s}-01-01"
    return s


def filter_rows(rows: list[dict], start=None, end=None, lastobs=None) -> list[dict]:
    """from / to / lastobs applied to the full package (the DDP's own date subsetting answered some new
    combinations with an empty file in testing, 2026-10-30, so the pack always asks for the whole package)."""
    lo = date_key(start) if start not in (None, "") else ""
    hi = date_key(end, end=True) if end not in (None, "") else ""
    out = [r for r in rows if (not lo or date_key(r["date"]) >= lo) and (not hi or date_key(r["date"]) <= hi)]
    if lastobs not in (None, ""):
        n, by = int(lastobs), {}
        for r in out:
            by.setdefault(r["series"], []).append(r)
        keep = {id(x) for v in by.values() for x in sorted(v, key=lambda r: date_key(r["date"]))[-n:]}
        out = [r for r in out if id(r) in keep]
    return out


def catalog_rel(pack, package: str) -> str | None:
    for c in ((pack.endpoints or {}).get("catalog") or {}).values():
        if c.get("id") == package:
            return c.get("rel")
    return None


def _ids(v) -> set[str]:
    if v in (None, ""):
        return set()
    items = v if isinstance(v, list) else str(v).split(",")
    return {str(x).strip().upper() for x in items if str(x).strip()}


def parse_ddp_csv(text: str, keep: set[str] | None = None) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text)))
    meta, start = {}, None
    for i, r in enumerate(rows):
        if not r:
            continue
        lab = r[0].strip().lower()
        if lab == "time period":
            meta["series"] = r[1:]
            start = i + 1
            break
        if lab in LABELS:
            meta[LABELS[lab]] = r[1:]
    if start is None or "series" not in meta:
        head = text[:200].replace("\n", " ")
        raise DataError(f"not a Data Download Program CSV (no 'Time Period' header row): {head!r}", kind="api")
    codes = meta["series"]
    cols = []
    for j, code in enumerate(codes):
        info = {k: (meta.get(k) or [""] * len(codes))[j] if j < len(meta.get(k) or []) else ""
                for k in ("description", "unit", "multiplier", "currency", "unique_id")}
        info = {k: " ".join(str(v).split()) for k, v in info.items()}
        ok = not keep or code.strip().upper() in keep or info["unique_id"].upper() in keep
        cols.append((code.strip(), info, ok))
    out = []
    for r in rows[start:]:
        if not r or not r[0].strip():
            continue
        for j, v in enumerate(r[1:]):
            if j >= len(cols) or not cols[j][2]:
                continue
            v = v.strip()
            if v == "":
                continue
            flag = v if v.upper() in FLAGS else ""
            out.append({"date": r[0].strip(), "series": cols[j][0], "value": "" if flag else v, "value_flag": flag,
                        **cols[j][1]})
    return out


def fetch_ddp(c, pack, r: dict, params: dict, cache: _Cache, notes: list) -> list[dict]:
    pkg = str(params.get("package") or "").strip()
    rel = str(params.get("rel") or "").strip().upper() or (catalog_rel(pack, pkg) or "")
    if not rel:
        raise DataError(f"request {r['name']!r}: give \"rel\" (the release code, e.g. G19) with a package id or title",
                        kind="bad_request")
    title = None
    if not HASH.fullmatch(pkg.lower()):
        p = match_package(packages(c, rel, cache), pkg, rel)
        pkg, title = p["package"], p["package_title"]
    resp = None
    for wait in (5, 15, None):                 # an empty body was sometimes transient (it answered a moment later)
        try:
            resp = c.get(OUTPUT, ddp_query(rel, pkg))
            break
        except DataError as e:
            if wait is None or "empty file" not in str(e) or os.environ.get("DATA_ANALYST_FIXTURES"):
                raise
            import time
            time.sleep(wait)
    rows = parse_ddp_csv(resp.text(), _ids(params.get("series_ids")))
    rows = filter_rows(rows, params.get("from"), params.get("to"), params.get("lastobs"))
    notes.append(f"{r['name']}: DDP release {rel}, package " + (f"{title!r} " if title else "") + f"({pkg.lower()}); "
                 "value x multiplier is in unit (Currency x 1000000 = millions of dollars).")
    return rows


def ddp_query(rel: str, pkg: str) -> dict:
    """The whole package, labels included, series in columns (the Choose page's own default form)."""
    return {"rel": rel, "series": pkg.lower(), "lastobs": "", "from": "", "to": "", "filetype": "csv",
            "label": "include", "layout": "seriescolumn", "type": "package"}


# ---------------------------------------------------------------------- Z.1
def z1_mapping(c, cache: _Cache) -> list[dict]:
    if "map" not in cache.blobs:
        cache.blobs["map"] = list(csv.DictReader(io.StringIO(c.get(Z1_MAP).text())))
    return cache.blobs["map"]


def z1_table(c, want: str, cache: _Cache) -> tuple[str, str, str]:
    """(new code, published name, title) for a new code (S1M.b) or a published name (B.101)."""
    w = str(want).strip()
    rows = z1_mapping(c, cache)
    for row in rows:
        if w.lower() in (row.get("new", "").lower(), row.get("old", "").lower()):
            return row["new"], row.get("old", ""), row.get("title", "")
    near = [f"{x['new']} ({x['old']})" for x in rows if w.split(".")[0].lower() in (x["new"] + x["old"]).lower()][:12]
    raise DataError(f"no Z.1 table {want!r} in z1_table_mapping.csv" + (f"; similar: {', '.join(near)}" if near else "")
                    + ". List them with the z1_tables endpoint.", kind="bad_request")


def z1_rows(zbytes: bytes, new: str, old: str, keep: set[str], start: str, end: str) -> list[dict]:
    try:
        z = zipfile.ZipFile(io.BytesIO(zbytes))
    except zipfile.BadZipFile:
        raise DataError("expected the Z.1 zip file, got something else (an HTML page?)", kind="api")
    stem = new.replace(".", "_")
    names = {n.lower(): n for n in z.namelist()}
    member = names.get(f"csv/{stem}.csv".lower()) or names.get(f"{stem}.csv".lower())
    if not member:
        raise DataError(f"table {new} ({stem}.csv) is not in the Z.1 zip", kind="not_found")
    info = {}
    dd = names.get(f"data_dictionary/{stem}.txt".lower())
    if dd:
        for line in z.read(dd).decode("utf-8-sig", errors="replace").splitlines():
            f = [x.strip() for x in line.split("\t")]
            if len(f) >= 2 and f[0]:
                info[f[0]] = {"description": f[1], "line": (f[2] if len(f) > 2 else "").replace("Line", "").strip(),
                              "units": f[4] if len(f) > 4 else ""}
    return z1_from_csv(z.read(member).decode("utf-8-sig", errors="replace"), info, new, old, keep, start, end)


def z1_from_csv(text: str, info: dict, new: str, old: str, keep: set[str], start: str, end: str) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    keep = {k if "." in k else k + ".Q" for k in keep} | keep
    out = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        d = r[0].strip()
        if (start and d < start) or (end and d[:len(end)] > end):
            continue
        for j, v in enumerate(r[1:], start=1):
            s = head[j] if j < len(head) else f"col{j}"
            if keep and s.upper() not in keep:
                continue
            v = v.strip()
            if v == "":
                continue
            flag = v if v.upper() in FLAGS else ""
            m = info.get(s, {})
            out.append({"date": d, "series": s, "value": "" if flag else v, "value_flag": flag,
                        "description": m.get("description", ""), "line": m.get("line", ""),
                        "units": m.get("units", ""), "table": new, "table_published": old})
    return out


def fetch_z1(c, r: dict, params: dict, cache: _Cache, notes: list) -> list[dict]:
    new, old, title = z1_table(c, params["table"], cache)
    if "zip" not in cache.blobs:
        cache.blobs["zip"] = c.get(Z1_ZIP).body
    keep = {k.upper() for k in _ids(params.get("series_ids"))}
    rows = z1_rows(cache.blobs["zip"], new, old, keep, str(params.get("start") or ""), str(params.get("end") or ""))
    notes.append(f"{r['name']}: Z.1 table {new} ({old}, {title}) from the current release's bulk CSV zip.")
    return rows


# ---------------------------------------------------------------------- SCF
def scf_member(params: dict) -> str:
    return f"interactive_bulletin_charts_{params['group']}_{params['stat']}.csv"


def scf_chart_rows(zbytes: bytes, member: str) -> list[dict]:
    try:
        z = zipfile.ZipFile(io.BytesIO(zbytes))
    except zipfile.BadZipFile:
        raise DataError("expected the SCF chart-data zip, got something else (an HTML page?)", kind="api")
    names = {os.path.basename(n).lower(): n for n in z.namelist()}
    if member.lower() not in names:
        raise DataError(f"{member} is not in scf.zip ({', '.join(sorted(names)[:6])} ...)", kind="not_found")
    return list(csv.DictReader(io.StringIO(z.read(names[member.lower()]).decode("utf-8-sig", errors="replace"))))


def sheet_names(xbytes: bytes) -> list[str]:
    try:
        z = zipfile.ZipFile(io.BytesIO(xbytes))
        return re.findall(r'<sheet\b[^>]*\bname="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "replace"))
    except (zipfile.BadZipFile, KeyError):
        raise DataError("not an .xlsx workbook (an HTML page in its place?)", kind="api")


def scf_table_rows(xbytes: bytes, sheet: str) -> list[dict]:
    names = [html.unescape(n) for n in sheet_names(xbytes)]
    if sheet not in names:
        low = {n.lower(): n for n in names}
        if sheet.lower() not in low:
            raise DataError(f"no sheet {sheet!r} in the SCF tables workbook (sheets: {', '.join(names)})",
                            kind="bad_request")
        sheet = low[sheet.lower()]
    grid = gr.xlsx_sheets(xbytes, {sheet}).get(sheet, [])
    width = max((len(r) for r in grid), default=0)
    out = []
    for i, row in enumerate(grid, start=1):
        if not any(v not in (None, "") for v in row):
            continue
        out.append({"row": i, **{f"c{j + 1}": (row[j] if j < len(row) and row[j] is not None else "")
                                 for j in range(width)}})
    return out


# ---------------------------------------------------------------------- hooks
def check_spec(spec, pack):
    probs = []
    for r in spec.get("requests") or []:
        p, n, ep = r.get("params") or {}, r.get("name"), r.get("endpoint")
        if ep == "ddp":
            pkg = str(p.get("package") or "")
            cat = (pack.endpoints or {}).get("catalog") or {}
            if not p.get("rel") and pkg not in cat and not catalog_rel(pack, pkg):
                probs.append(f"request {n!r}: ddp needs \"rel\" (release code, e.g. G19) unless the package is a "
                             "catalog alias")
            for k in ("from", "to"):
                if p.get(k) and not re.fullmatch(r"\d{4}(-\d{1,2}(-\d{1,2})?)?|\d{4}:?Q[1-4]", str(p[k]), re.I):
                    probs.append(f"request {n!r}: {k} must be YYYY, YYYY-MM, YYYY-MM-DD or YYYYQn")
            if p.get("lastobs") not in (None, "") and not str(p["lastobs"]).isdigit():
                probs.append(f"request {n!r}: lastobs must be a whole number")
        if ep == "z1":
            for k in ("start", "end"):
                if p.get(k) and not re.fullmatch(r"\d{4}(:Q[1-4])?", str(p[k])):
                    probs.append(f"request {n!r}: {k} must be YYYY or YYYY:Qn (e.g. 2020:Q1)")
    return probs


def _cut(r: dict, rows: list[dict], ep: dict, notes: list) -> list[dict]:
    limit = int(r.get("max_rows") or ep.get("max_rows") or 200000)
    if len(rows) > limit:
        notes.append(f"{r['name']}: stopped at {limit:,} rows (max_rows); the source has {len(rows):,}. Narrow it with "
                     "series_ids, dates or lastobs, or raise max_rows.")
        return rows[:limit]
    return rows


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    cache = _Cache()
    tables, notes, miss = {}, [], gr.Misses()
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        kind = r.get("endpoint") or ""
        try:
            if kind == "ddp":
                rows = fetch_ddp(c, pack, r, params, cache, notes)
            elif kind == "ddp_packages":
                rows = packages(c, str(params["rel"]), cache)
            elif kind == "z1":
                rows = fetch_z1(c, r, params, cache, notes)
            elif kind == "scf_chart":
                if "scf" not in cache.blobs:
                    cache.blobs["scf"] = c.get(ep["path"]).body
                rows = scf_chart_rows(cache.blobs["scf"], scf_member(params))
            elif kind == "scf_table":
                path, _, _ = gr.split(ep, params)
                rows = scf_table_rows(c.get(path).body, str(params["sheet"]))
            else:
                t, n = gr.fetch(pack, {"requests": [r]}, c)
                rows = t.get(r["name"], [])
                notes += n
        except DataError as e:
            if e.kind == "not_found":
                miss.not_found(r, tables, notes)
                continue
            raise DataError(f"request {r['name']!r} ({kind}): {e}", kind=e.kind, status=getattr(e, "status", None)) \
                from None
        rows = _cut(r, rows, ep, notes)
        miss.check_empty(r, rows)
        tables[r["name"]] = rows
    miss.raise_if_any(missing_hint="check the table code or file", empty_hint="check series_ids, dates and lastobs")
    return tables, notes


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        kind, n = r.get("endpoint"), r["name"]
        it = {"name": f"{n}#1", "request": n, "method": "GET", "body": None, "needs_key": False, "optional": False,
              "also_ext": [], "note": ""}
        if kind == "ddp":
            pkg = str(params.get("package") or "")
            rel = str(params.get("rel") or catalog_rel(pack, pkg) or "").upper()
            if HASH.fullmatch(pkg.lower()):
                q = "&".join(f"{k}={v}" for k, v in ddp_query(rel, pkg).items() if v != "")
                it.update(url=f"{BASE}{OUTPUT}?{q}", filename=f"{n}.csv",
                          note="the whole package; the pack applies series_ids, from, to and lastobs when it reads it")
            else:
                it.update(url=f"{BASE}{CHOOSE}?rel={rel}", filename=f"{n}.csv",
                          note=f"choose the preformatted package titled {pkg!r}, click Format package, keep CSV "
                               "(labels included, series in columns), then Download file")
        elif kind == "ddp_packages":
            it.update(url=f"{BASE}{CHOOSE}?rel={str(params.get('rel', '')).upper()}", filename=f"{n}.html",
                      note="save the page as HTML (File > Save as)")
        elif kind == "z1":
            it.update(url=BASE + Z1_ZIP, filename=f"{n}.zip", also_ext=[".csv"],
                      note=f"the whole Z.1 zip (8 MB); or upload only csv/<table>.csv for table {params.get('table')}")
        elif kind == "scf_chart":
            it.update(url=BASE + ep["path"], filename=f"{n}.zip", also_ext=[".csv"],
                      note=f"or upload only {scf_member(params)} from the zip")
        elif kind == "scf_table":
            path, _, _ = gr.split(ep, params)
            it.update(url=BASE + path, filename=f"{n}.xlsx")
        else:
            items += gr.manual_items(pack, {"requests": [r]})
            continue
        items.append(it)
    return items


def parse_upload(pack, req, paths):
    ep, params, _ = gr.resolve(pack, req)
    kind = req.get("endpoint")
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            data = f.read()
        low = p.lower()
        if kind == "ddp":
            rows += filter_rows(parse_ddp_csv(data.decode("utf-8-sig", errors="replace"), _ids(params.get("series_ids"))),
                                params.get("from"), params.get("to"), params.get("lastobs"))
        elif kind == "ddp_packages":
            rows += parse_packages(data.decode("utf-8", errors="replace"), str(params.get("rel", "")).upper())
        elif kind == "z1":
            keep = {k.upper() for k in _ids(params.get("series_ids"))}
            new = str(params.get("table") or "")
            if re.match(r"^[A-Z]\.\d", new):                         # the mapping file is not in the zip
                raise DataError(f"request {req['name']!r}: for an uploaded Z.1 file give the new table code, not the "
                                f"published name {new!r} (z1_tables maps them: B.101 = S1M.b, L.101 = S1M.s)",
                                kind="bad_request")
            start, end = str(params.get("start") or ""), str(params.get("end") or "")
            if low.endswith(".zip"):
                rows += z1_rows(data, new, "", keep, start, end)
            else:
                rows += z1_from_csv(data.decode("utf-8-sig", errors="replace"), {}, new, "", keep, start, end)
        elif kind == "scf_chart":
            if low.endswith(".zip"):
                rows += scf_chart_rows(data, scf_member(params))
            else:
                rows += list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
        elif kind == "scf_table":
            rows += scf_table_rows(data, str(params["sheet"]))
        else:
            rows += gr.parse_upload(pack, req, [p])
    return [{k: ("" if v is None else v) for k, v in row.items()} for row in rows]

"""FRED / ALFRED / GeoFRED client for the fred pack (standard library only).

Two ways in:
  FredClient   the FRED API (api.stlouisfed.org) with the user's own key: data, vintages (ALFRED), series
               metadata, the release and source behind a series (for the citation), search, GeoFRED
               regional cross-sections, the release calendar (release/dates, releases/dates) and whole
               releases (v2 release/observations, cursor-paged).
  Keyless      the CSV and XLSX files that FRED's own graph pages download (fred.stlouisfed.org/graph/
               fredgraph.csv, .xlsx; alfred.stlouisfed.org/graph/alfredgraph.csv). No key, but no source,
               notes or copyright status; titles, units, frequency, seasonal adjustment and the last update
               come from the XLSX README sheet.

The key comes from core.keys.find_key("FRED_API_KEY", "fred") (environment, *_FILE, keys.json, Claude
Code settings.json, ~/.fred/api_key) and is never printed. FRED v1 takes it as the api_key query parameter, which
core.http.Client drops from every recorded URL; FRED v2 takes it as "Authorization: Bearer <key>" (checked live
2026-10-20: api_key in the query answers HTTP 401 "Missing or invalid credentials."). Every request goes
through core.http.Client, so offline fixtures, the restricted switch and the request log apply.

    from fred_api import FredClient, Keyless
    c = FredClient(); c.series("GDPC1"); c.observations("GDPC1", observation_start="2019-01-01")
    Keyless().csv("UNRATE", start="2019-01-01")

Facts checked live on 2026-10-20: a bad key answers HTTP 400 with a JSON error_message naming api_key; the
API allows 120 requests a minute; missing values are "." in the API and empty cells in fredgraph.csv; an
unknown ID in fredgraph.csv answers 404 with an HTML page; several IDs of mixed frequencies come back as a
zip with one CSV per frequency, so this module asks for one ID per CSV request; a graph download holds at
most 12 series (GRAPH_MAX) and silently drops the rest; v2 error bodies arrive gzip-compressed.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import sys
import urllib.parse
import zipfile
from datetime import date, datetime
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from core import keys  # noqa: E402
from core.http import Client, DataError, reachable as http_reachable  # noqa: E402

API = "https://api.stlouisfed.org/fred/"
API_V2 = "https://api.stlouisfed.org/fred/v2/"
GEO = "https://api.stlouisfed.org/geofred/"
GRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
GRAPH_XLSX = "https://fred.stlouisfed.org/graph/fredgraph.xlsx"
ALFRED_CSV = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
SERIES_URL = "https://fred.stlouisfed.org/series/{}"
ALFRED_SERIES_URL = "https://alfred.stlouisfed.org/series?seid={}"
KEY_URL = "https://fredaccount.stlouisfed.org/apikeys"
TERMS_URL = "https://fred.stlouisfed.org/docs/api/terms_of_use.html"
LEGAL_URL = "https://fred.stlouisfed.org/legal/"
NOTICE = ("This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank "
          "of St. Louis.")
UA = "data-analyst/1.0 (fred pack; Python urllib)"
GRAPH_MAX = 12                 # a FRED graph (and so its CSV/XLSX download) holds at most 12 series; more are
                               # silently dropped (checked live 2026-10-20)
KEY_ENV, KEY_HOME = "FRED_API_KEY", "fred"
AUTH_V1 = {"env": KEY_ENV, "home": KEY_HOME, "placement": "query", "param": "api_key"}
AUTH_V2 = {"env": KEY_ENV, "home": KEY_HOME, "placement": "header", "param": "Authorization", "prefix": "Bearer "}
RPS, BURST = 1.8, 4            # under FRED's 120 requests a minute

FREQ_NAMES = {"d": "Daily", "w": "Weekly", "bw": "Biweekly", "m": "Monthly", "q": "Quarterly",
              "sa": "Semiannual", "a": "Annual"}
UNITS = ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log")
AGGREGATION = ("avg", "sum", "eop")


def restricted() -> bool:
    return keys.restricted()


def find_api_key() -> tuple[str | None, str | None]:
    return keys.find_key(KEY_ENV, KEY_HOME)


def redact(text) -> str:
    return keys.redact(text)


class FredError(RuntimeError):
    """Error whose message is always scrubbed of the key.

    kind says why the call failed, so callers can pick the right recovery:
      key_missing  no key in any location
      auth         FRED rejected the key ("not registered", or not set)
      network      the host could not be reached (offline, proxy, sandbox, DATA_ACCESS_FORCE=restricted)
      not_found    the series (or release, group) does not exist
      rate         HTTP 429 after retries (more than 120 requests a minute)
      http         any other HTTP error
      request      FRED refused the request for another reason (a bad parameter: the message says which)
    """
    def __init__(self, msg: str, kind: str = "request"):
        super().__init__(redact(msg))
        self.kind = kind


def _wrap(e: DataError, what: str) -> FredError:
    kind = {"network": "network", "rate": "rate", "not_found": "not_found", "auth": "auth",
            "key_missing": "key_missing"}.get(e.kind, "http")
    return FredError(f"{what}: {e}", kind=kind)


def _text(body: bytes) -> str:
    if body[:2] == b"\x1f\x8b":                     # FRED v2 sends gzip error bodies unasked
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    return body.decode("utf-8-sig", errors="replace")


def reachable(url: str, timeout: int = 15) -> tuple[bool, str]:
    """Does the host answer at all? Any HTTP status counts as reachable."""
    return http_reachable(url, timeout)


def parse_value(v) -> float | None:
    """FRED's '.' (API) and '' (CSV) mean no value."""
    s = str(v if v is not None else "").strip()
    if s in ("", ".", "#N/A", "NA"):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def copyright_status(notes: str | None, from_api: bool) -> str:
    """FRED's terms: copyrighted series contain the word "Copyright" in their notes. Without the API's notes
    (the keyless route) the status is unknown and must be checked on the series page."""
    if not from_api:
        return "unknown"
    if notes and re.search(r"copyright", notes, re.I):
        return "copyrighted"
    return "no_copyright_notice"


def copyright_tag_of(tags: dict) -> str:
    """The copyright tag in a series/tags reply (group "cc"): the series page's "Copyrighted: Citation
    Required" label. Series notes often say nothing about copyright (T10Y2Y), so the tag is the record."""
    for t in (tags or {}).get("tags") or []:
        n = str(t.get("name", "")).lower()
        if t.get("group_id") == "cc" or n.startswith(("copyrighted", "public domain")):
            return t.get("name", "")
    return ""


def copyright_from_tag(tag: str | None) -> str:
    """FRED v2's copyright_id ("public domain: citation requested", "copyrighted: citation required",
    "copyrighted: pre-approval required") -> the spec's labels."""
    t = (tag or "").lower()
    if "pre-approval" in t or "preapproval" in t:
        return "preapproval_required"
    if t.startswith("copyrighted"):
        return "citation_required"
    if t.startswith("public domain"):
        return "public_domain"
    return ""


def citation(sid: str, title: str, source: str, retrieved: str) -> str:
    """FRED's suggested citation: "<Source>, <Title> [ID], retrieved from FRED, Federal Reserve Bank of St.
    Louis; <url>, <Month D, YYYY>." """
    try:                                   # the user's calendar day: an evening in the U.S. is tomorrow in UTC
        d = datetime.fromisoformat(str(retrieved).replace("Z", "+00:00"))
        d = d.astimezone() if d.tzinfo else d
        when = f"{d.strftime('%B')} {d.day}, {d.year}"
    except ValueError:
        when = retrieved
    lead = f"{source}, " if source else ""
    return (f"{lead}{title or sid} [{sid}], retrieved from FRED, Federal Reserve Bank of St. Louis; "
            f"{SERIES_URL.format(sid)}, {when}.")


# ------------------------------------------------------------------ the API
class FredClient:
    def __repr__(self) -> str:                   # never show the key in tracebacks or logs
        return "FredClient(key=[REDACTED])"

    def __init__(self, api_key: str | None = None, timeout: int = 60):
        if api_key:
            keys.remember(api_key)
            self.key, self.key_source = api_key, "argument"
        else:
            self.key, self.key_source = find_api_key()
        if not self.key:
            raise FredError(f"No {KEY_ENV} key found. Get a free key at {KEY_URL}, then run the skill's setup "
                            "script (setup.ps1 -Source fred / setup.sh fred); it stores the key in keys.json, "
                            "which these scripts read at once.", kind="key_missing")
        self.http = Client(API, name="fred", auth=AUTH_V1, key=self.key, rps=RPS, burst=BURST, timeout=timeout,
                           user_agent=UA)
        self.http2 = Client(API_V2, name="fred-v2", auth=AUTH_V2, key=self.key, rps=RPS, burst=BURST,
                            timeout=timeout, user_agent=UA)
        self.calls: list[str] = []          # credential-free URLs, for request.json

    def _json(self, client: Client, url: str, params: dict, what: str) -> dict:
        try:
            r = client.get(url, params, ok_statuses=(200, 400, 401, 403, 404))
        except DataError as e:
            raise _wrap(e, what) from None
        self.calls.append(r.url)
        text = _text(r.body)
        try:
            j = json.loads(text) if text.strip() else {}
        except ValueError:
            if text.lstrip()[:1] == "<":         # a maintenance or error page, not an API answer
                raise FredError(f"{what}: FRED answered with an HTML page (HTTP {r.status}) instead of data: the "
                                "service is probably down for maintenance; try again later", kind="unavailable") from None
            j = {}
        if r.status == 200 and "error_code" not in j:
            return j
        msg = str(j.get("error_message") or j.get("message") or text[:300])
        low = msg.lower()
        if r.status in (401, 403) or ("api_key" in low and ("not registered" in low or "not set" in low
                                                            or "invalid" in low)):
            raise FredError(f"FRED rejected the API key: {msg}", kind="auth")
        if "does not exist" in low or r.status == 404:
            raise FredError(f"{what}: {msg}", kind="not_found")
        raise FredError(f"{what} failed (HTTP {r.status}): {msg}", kind="request" if r.status == 400 else "http")

    def get(self, path: str, base: str = API, **params) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["file_type"] = "json"
        return self._json(self.http, base + path, params, path)

    def ping(self) -> dict:
        """One small call. Returns {'auth_ok', 'kind', 'detail'}; never raises."""
        try:
            self.get("series", series_id="GNPCA")
            return {"auth_ok": True, "kind": "ok", "detail": ""}
        except FredError as e:
            return {"auth_ok": False, "kind": e.kind, "detail": str(e).splitlines()[0][:300]}

    # -- metadata
    def series(self, sid: str, **rt) -> dict:
        r = self.get("series", series_id=sid, **rt)
        s = (r.get("seriess") or [{}])[0]
        if not s:
            raise FredError(f"series {sid}: no metadata returned", kind="not_found")
        return s

    def release_of(self, sid: str) -> dict:
        return (self.get("series/release", series_id=sid).get("releases") or [{}])[0]

    def release_sources(self, release_id) -> list[dict]:
        return self.get("release/sources", release_id=release_id).get("sources") or []

    def vintagedates(self, sid: str) -> list[str]:
        out, offset = [], 0
        while True:
            r = self.get("series/vintagedates", series_id=sid, realtime_start="1776-07-04",
                         realtime_end="9999-12-31", limit=10000, offset=offset)
            got = r.get("vintage_dates") or []
            out += got
            if len(got) < 10000:
                return out
            offset += len(got)

    def search(self, text: str, limit: int = 25, order_by: str = "popularity", **filters) -> list[dict]:
        r = self.get("series/search", search_text=text, limit=limit, order_by=order_by,
                     sort_order="desc", **filters)
        return r.get("seriess") or []

    # -- data
    def observations(self, sid: str, **params) -> list[dict]:
        """All observations (paged by 100,000). Params as the API names them: observation_start, units,
        frequency, aggregation_method, realtime_start, realtime_end, vintage_dates, output_type."""
        out, offset = [], 0
        while True:
            r = self.get("series/observations", series_id=sid, limit=100000, offset=offset, **params)
            got = r.get("observations") or []
            out += got
            if len(got) < 100000:
                return out
            offset += len(got)

    def series_group(self, sid: str) -> dict:
        return self.get("series/group", base=GEO, series_id=sid).get("series_group") or {}

    def regional(self, **params) -> dict:
        """geofred/regional/data: series_group, region_type, date, season, units are required."""
        return self.get("regional/data", base=GEO, **params)

    # -- releases
    def release_dates(self, release_id=None, start: str | None = None, end: str | None = None,
                      limit: int = 10000) -> dict:
        """Release calendar, scheduled future dates included. One release: its dates from start (default
        today) on; all releases: the dates between start and end (default the next 14 days)."""
        start = start or date.today().isoformat()
        if not end:
            end = "9999-12-31" if release_id else date.fromordinal(date.today().toordinal() + 14).isoformat()
        p = {"realtime_start": start, "realtime_end": end, "include_release_dates_with_no_data": "true",
             "sort_order": "asc"}
        path, extra = ("release/dates", {"release_id": release_id}) if release_id else ("releases/dates", {})
        j, got = {}, []
        while len(got) < limit:                     # paged by offset; FRED's count says how many exist
            page = self.get(path, **extra, **p, limit=min(10000, limit - len(got)),
                            **({"offset": len(got)} if got else {}))
            j = page
            got += page.get("release_dates") or []
            if not page.get("release_dates") or len(got) >= int(page.get("count") or 0):
                break
        return {**j, "offset": 0, "limit": limit, "release_dates": got}

    def release_observations(self, release_id, limit: int = 10000, max_pages: int = 5,
                             next_cursor: str | None = None) -> tuple[dict, list[dict]]:
        """FRED v2 release/observations: every series of a release with its observations, paged by cursor.
        Returns (release, series) and stops after max_pages pages (has_more / next_cursor recorded in the
        release dict). limit is observations per page (FRED allows up to 500,000)."""
        release, series, cursor = {}, [], next_cursor
        for _ in range(max(1, max_pages)):
            p = {"release_id": release_id, "format": "json", "limit": limit}
            if cursor:
                p["next_cursor"] = cursor
            j = self._json(self.http2, API_V2 + "release/observations", p, "v2 release/observations")
            release = release or dict(j.get("release") or {})
            for x in j.get("series") or []:      # a page can end inside a series: the next page continues it
                if series and series[-1].get("series_id") == x.get("series_id"):
                    series[-1]["observations"] = (series[-1].get("observations") or []) + (x.get("observations") or [])
                else:
                    series.append(dict(x))
            cursor = j.get("next_cursor") if j.get("has_more") else None
            release.update(has_more=bool(cursor), next_cursor=cursor or "")
            if not cursor:
                break
        return release, series


def observation_rows(sid: str, obs: list[dict]) -> list[dict]:
    """API observations -> rows {date, value, realtime_start, realtime_end}. output_type 2/3 put each vintage
    in a column named <ID>_<YYYYMMDD>; those become one row per vintage with realtime_start = the vintage."""
    rows = []
    for o in obs:
        if "value" in o:
            rows.append({"date": o.get("date", ""), "value": parse_value(o.get("value")),
                         "realtime_start": o.get("realtime_start", ""), "realtime_end": o.get("realtime_end", "")})
            continue
        for k, v in o.items():
            m = re.fullmatch(rf"_?{re.escape(sid)}_(\d{{8}})", k, re.I)
            if m:
                d = m.group(1)
                rows.append({"date": o.get("date", ""), "value": parse_value(v),
                             "realtime_start": f"{d[:4]}-{d[4:6]}-{d[6:]}", "realtime_end": ""})
    return rows


def regional_rows(payload: dict) -> list[dict]:
    """geofred regional/data JSON ({title: {date: [{region, code, value, series_id}]}}) -> flat rows."""
    rows = []
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else None
    body = meta.get("data") if meta and isinstance(meta.get("data"), dict) else payload
    title = (meta or {}).get("title", "")
    for k, v in body.items():
        if isinstance(v, dict):                      # {title: {date: [...]}}
            for d, items in v.items():
                for it in items or []:
                    rows.append({"title": k, "date": d, **_region_item(it)})
        elif isinstance(v, list) and re.match(r"\d{4}", str(k)):      # {date: [...]} under meta
            for it in v:
                rows.append({"title": title, "date": k, **_region_item(it)})
    return rows


def _region_item(it: dict) -> dict:
    return {"region": it.get("region", ""), "code": str(it.get("code", "")), "series_id": it.get("series_id", ""),
            "value": parse_value(it.get("value"))}


# ------------------------------------------------------------------ keyless downloads
def graph_params(sid: str, start: str | None = None, end: str | None = None, frequency: str | None = None,
                 aggregation_method: str | None = None, units: str | None = None,
                 vintage_dates: list[str] | None = None) -> dict:
    """Query parameters of FRED's graph download. Several vintages of one series are asked as repeated IDs
    with one vintage_date each (checked live: alfredgraph returns one <ID>_<YYYYMMDD> column per vintage)."""
    n = max(1, len(vintage_dates or []))
    p = {"id": ",".join([sid] * n)}
    if start:
        p["cosd"] = ",".join([start] * n)
    if end:
        p["coed"] = ",".join([end] * n)
    if frequency:
        p["fq"] = ",".join([FREQ_NAMES.get(frequency, frequency)] * n)
        p["fam"] = ",".join([aggregation_method or "avg"] * n)
    if units and units != "lin":
        p["transformation"] = ",".join([units] * n)
    if vintage_dates:
        p["vintage_date"] = ",".join(vintage_dates)
    return p


def graph_url(sid: str, **kw) -> str:
    base = ALFRED_CSV if kw.get("vintage_dates") else GRAPH_CSV
    return f"{base}?{urllib.parse.urlencode(graph_params(sid, **kw), safe=',')}"


def parse_graph_csv(data: bytes) -> dict[str, list[dict]]:
    """fredgraph.csv / alfredgraph.csv (or the zip FRED sends for mixed frequencies) ->
    {column: [{date, value}]}. Columns are the series ID, <ID>_<TRANSFORM> or <ID>_<YYYYMMDD> (a vintage)."""
    if data[:2] == b"PK":
        out: dict[str, list[dict]] = {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.lower().endswith(".csv"):
                    out.update(parse_graph_csv(z.read(n)))
        return out
    text = data.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        raise FredError("FRED returned an HTML page instead of CSV (unknown series ID?)", kind="not_found")
    reader = csv.reader(io.StringIO(text))
    head = next(reader, [])
    if not head or head[0].strip().lower() not in ("observation_date", "date"):
        raise FredError(f"not a FRED graph CSV (header {head[:3]})", kind="request")
    cols = [h.strip() for h in head[1:]]
    out = {c: [] for c in cols}
    for row in reader:
        if not row or not row[0].strip():
            continue
        for c, v in zip(cols, row[1:]):
            out[c].append({"date": row[0].strip()[:10], "value": parse_value(v)})
    return out


def _xlsx_rows(z: zipfile.ZipFile, sheet_path: str, shared: list[str]) -> list[list]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(z.read(sheet_path))
    rows = []
    for r in root.iter(f"{{{ns['m']}}}row"):
        vals: dict[int, object] = {}
        for c in r.findall("m:c", ns):
            ref = c.get("r", "")
            col = 0
            for ch in re.match(r"[A-Z]+", ref).group(0) if re.match(r"[A-Z]+", ref) else "A":
                col = col * 26 + ord(ch) - 64
            v = c.find("m:v", ns)
            t = c.get("t")
            if t == "s" and v is not None:
                vals[col - 1] = shared[int(v.text)]
            elif t == "inlineStr":
                vals[col - 1] = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            elif v is not None:
                vals[col - 1] = v.text
        rows.append([vals.get(i) for i in range(max(vals) + 1)] if vals else [])
    return rows


def parse_graph_xlsx(data: bytes) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """fredgraph.xlsx -> (metadata {id: {title, units, frequency, seasonal_adjustment, last_updated}},
    observations {id: [{date, value}]}). The README sheet lists one line per series:
    'ID', 'Title, Units, Frequency, Seasonal adjustment', 'Data Updated: YYYY-MM-DD'."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels}
        sheets = [(s.get("name"), "xl/" + target[s.get(f"{rel_ns}id")].lstrip("/").replace("xl/", ""))
                  for s in wb.iter(f"{ns}sheet")]
        meta, obs = {}, {}
        for name, path in sheets:
            rows = _xlsx_rows(z, path, shared)
            if name.lower() == "readme":
                for r in rows:
                    if len(r) >= 2 and r[0] and r[1] and re.fullmatch(r"[A-Z0-9_.\-]+", str(r[0])):
                        meta[str(r[0])] = _split_description(str(r[1]), str(r[2] or "") if len(r) > 2 else "")
                continue
            if not rows:
                continue
            head = [str(h or "") for h in rows[0]]
            for j, sid in enumerate(head[1:], start=1):
                pts = []
                for r in rows[1:]:
                    if not r or r[0] in (None, ""):
                        continue
                    d = _excel_date(r[0])
                    pts.append({"date": d, "value": parse_value(r[j] if j < len(r) else None)})
                obs[sid] = pts
                if sid in meta:
                    meta[sid]["sheet"] = name
    return meta, obs


def _split_description(desc: str, updated: str) -> dict:
    """'Federal Outlays: Interest, Millions of Dollars, Annual, Not Seasonally Adjusted' -> parts. The title
    can contain commas, so the parts are taken from the right: seasonal adjustment, frequency, units."""
    parts = [p.strip() for p in desc.split(",")]
    seas = parts.pop() if parts and "season" in parts[-1].lower() else ""
    freq = ""
    if parts and re.match(r"(daily|weekly|biweekly|monthly|quarterly|semiannual|annual)", parts[-1], re.I):
        freq = parts.pop()
        if freq.lower() == "annual" and parts and parts[-1].lower().startswith("fiscal"):
            freq = "Annual, " + parts.pop()
    title, units = (", ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (", ".join(parts), "")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", updated)
    return {"title": title, "units": units, "frequency": freq, "seasonal_adjustment": seas,
            "last_updated": m.group(1) if m else "", "description": desc}


def _excel_date(v) -> str:
    s = str(v)
    if re.fullmatch(r"\d+(\.\d+)?", s):                   # an Excel serial day number
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(float(s))).isoformat()
    return s[:10]


class Keyless:
    """FRED's graph downloads, with no key. One series per request (mixed frequencies would come as a zip)."""

    def __init__(self, timeout: int = 60):
        self.http = Client(name="fred-graph", rps=RPS, burst=BURST, timeout=timeout, user_agent=UA)
        self.calls: list[str] = []

    def _get(self, url: str) -> tuple[bytes, dict]:
        try:
            r = self.http.get(url, ok_statuses=(200,), allow_404=True)
        except DataError as e:
            raise _wrap(e, url.split("?")[0]) from None
        self.calls.append(r.url)
        if r.status != 404 and r.body.lstrip()[:1] == b"<" and re.search(rb"(?i)maintenance|unavailable|try again",
                                                                          r.body[:20000]):
            raise FredError(f"{url}: FRED answered with a maintenance page (HTTP {r.status}); try again later",
                            kind="unavailable")
        if r.status == 404 or r.body.lstrip()[:1] == b"<":
            raise FredError(f"{url}: no such series on FRED (HTTP {r.status}, an HTML page came back)",
                            kind="not_found")
        return r.body, r.headers

    def csv(self, sid: str, **kw) -> tuple[dict[str, list[dict]], bytes, str]:
        url = graph_url(sid, **kw)
        body, headers = self._get(url)
        return parse_graph_csv(body), body, headers.get("last-modified", "")

    def metadata(self, ids: list[str]) -> tuple[dict[str, dict], bytes]:
        """Titles, units, frequency, seasonal adjustment and last update for up to GRAPH_MAX IDs per request."""
        if len(ids) > GRAPH_MAX:
            raise ValueError(f"at most {GRAPH_MAX} IDs per graph download (FRED drops the rest silently)")
        url = f"{GRAPH_XLSX}?{urllib.parse.urlencode({'id': ','.join(ids)}, safe=',')}"
        body, _ = self._get(url)
        meta, _obs = parse_graph_xlsx(body)
        return meta, body


if __name__ == "__main__":
    # quick check: python fred_api.py GDPC1   (uses the key when one is set up, otherwise the keyless route)
    sid = sys.argv[1] if len(sys.argv) > 1 else "GNPCA"
    key, src = find_api_key()
    if key:
        s = FredClient().series(sid)
        print(f"{sid}: {s.get('title')} | {s.get('units')} | {s.get('frequency')} | updated {s.get('last_updated')}")
    else:
        m, _ = Keyless().metadata([sid])
        print(f"{sid} (keyless): {m.get(sid)}")

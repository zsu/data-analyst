"""BLS Public Data API client for the bls pack (standard library only).

The key comes from core.keys.find_key("BLS_API_KEY", "bls") (environment, *_FILE, keys.json, Claude Code settings.json,
~/.bls/api_key) and is never printed. Without a key the client uses API v1 (no registration, lower limits);
with a key, v2. Every request goes through core.http.Client, so offline fixtures (DATA_ANALYST_FIXTURES),
the restricted switch (DATA_ACCESS_FORCE=restricted) and the request log (core.http.SOURCES) apply.

    from bls_api import BlsClient
    c = BlsClient()
    series, messages, raw = c.fetch(["LNS14000000"], 2015, 2026)

Limits (https://www.bls.gov/developers/api_faqs.htm, read 2026-10-19):
                         v2 (registered)   v1 (no key)
    queries per day            500             25
    series per query            50             25
    years per query             20             10
    requests per 10 s           50             50
    catalog, annual averages,
    net/percent changes         yes            no
BLS answers HTTP 200 even for most errors: the outcome is in "status" and "message". A rejected key is
echoed in the message, so the pack registers a 32-hex redaction pattern with core.keys.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
import tempfile
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from core import keys  # noqa: E402
from core.http import Client, DataError  # noqa: E402

URLS = {"v1": "https://api.bls.gov/publicAPI/v1/timeseries/data/",
        "v2": "https://api.bls.gov/publicAPI/v2/timeseries/data/"}
LIMITS = {"v2": {"series": 50, "years": 20, "daily": 500},
          "v1": {"series": 25, "years": 10, "daily": 25}}
REGISTER_URL = "https://data.bls.gov/registrationEngine/"
KEY_ENV, KEY_HOME = "BLS_API_KEY", "bls"
AUTH = {"env": KEY_ENV, "home": KEY_HOME, "placement": "body", "param": "registrationkey"}
UA = "data-analyst/1.0 (bls pack; Python urllib)"
SERIES_PROBLEMS = ("does not exist", "no data available", "database is locked", "invalid series")

# BLS echoes a rejected key inside its error message: scrub every 32-hex token as well as known keys.
keys.add_pattern(r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def find_api_key() -> tuple[str | None, str | None]:
    return keys.find_key(KEY_ENV, KEY_HOME)


def redact(text) -> str:
    return keys.redact(text)


class BlsError(RuntimeError):
    """Error whose message is always scrubbed of the key.

    kind says why the call failed, so callers can pick the right recovery:
      auth      the key was rejected (wrong, or expired: keys must be renewed every year)
      quota     the daily query limit is used up (resets the next day)
      network   api.bls.gov could not be reached (offline, proxy, sandbox, DATA_ACCESS_FORCE=restricted)
      http      any other HTTP error
      request   BLS refused the request (REQUEST_NOT_PROCESSED for another reason)
    """
    def __init__(self, msg: str, kind: str = "request"):
        super().__init__(redact(msg))
        self.kind = kind


# ------------------------------------------------------------------ local usage counter
def _usage_file() -> str:
    """~/.bls/usage.json; a temp folder when the home directory is not writable (sandboxes)."""
    home = os.path.join(os.path.expanduser("~"), ".bls")
    try:
        os.makedirs(home, exist_ok=True)
        probe = os.path.join(home, ".write_test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return os.path.join(home, "usage.json")
    except OSError:
        return os.path.join(tempfile.gettempdir(), "data-analyst-bls-usage.json")


def usage_today(version: str) -> int:
    """Queries this machine sent today (local count; BLS's own count may differ slightly)."""
    try:
        with open(_usage_file(), encoding="utf-8") as f:
            return int(json.load(f).get(date.today().isoformat(), {}).get(version, 0))
    except (OSError, ValueError):
        return 0


def _count_query(version: str):
    if os.environ.get("DATA_ANALYST_FIXTURES"):         # replayed responses cost no query
        return
    path = _usage_file()
    try:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        today = date.today().isoformat()
        data = {today: data.get(today, {})}                # keep only today
        data[today][version] = int(data[today].get(version, 0)) + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


# ------------------------------------------------------------------ planning
def plan_queries(series: list[str], start: int, end: int, version: str) -> list[tuple[list[str], int, int]]:
    """Split a request into queries that respect the per-query limits: [(series_chunk, y0, y1), ...]."""
    lim = LIMITS[version]
    windows, y = [], start
    while y <= end:
        windows.append((y, min(end, y + lim["years"] - 1)))
        y += lim["years"]
    chunks = [series[i:i + lim["series"]] for i in range(0, len(series), lim["series"])]
    return [(c, y0, y1) for (y0, y1) in windows for c in chunks]


class BlsClient:
    def __repr__(self) -> str:
        return f"BlsClient(version={self.version}, key=[REDACTED])"

    def __init__(self, api_key: str | None = None, use_key: bool = True, timeout: int = 60):
        if api_key:
            keys.remember(api_key)
            self.key, self.key_source = api_key, "argument"
        elif use_key:
            self.key, self.key_source = find_api_key()
        else:
            self.key, self.key_source = None, None
        self.version = "v2" if self.key else "v1"
        self.queries_sent = 0
        # v1 sends no key at all (auth {}); v2 puts it in the JSON body as registrationkey
        self.http = Client(URLS[self.version], name="bls", auth=AUTH if self.key else {}, key=self.key,
                           rps=4, burst=5, retries=3, timeout=timeout, user_agent=UA)

    # ---- low level
    def _post(self, body: dict, retries: int | None = None) -> dict:
        if retries is not None:
            self.http.retries = retries
        try:
            r = self.http.request("POST", URLS[self.version], json_body=body)   # full URL: keep the trailing /
        except DataError as e:
            if getattr(e, "status", None) in (500, 502, 503, 504):     # "The BLS website is busy" (HTML page)
                raise BlsError(f"BLS API: HTTP {e.status}: the BLS site is busy or down (after retries)",
                               kind="unavailable") from None
            kind = {"network": "network", "rate": "quota", "auth": "auth", "key_missing": "auth"}.get(e.kind, "http")
            raise BlsError(f"BLS API: {e}", kind=kind) from None
        try:
            out = json.loads(r.body or b"{}")
        except ValueError:
            raise BlsError(f"BLS API answered with something that is not JSON: {r.text()[:200]}", kind="http") from None
        self.queries_sent += 1
        _count_query(self.version)
        status = out.get("status", "")
        msgs = [redact(m) for m in out.get("message") or []]
        if status != "REQUEST_SUCCEEDED":
            text = " ".join(msgs) or status
            low = text.lower()
            kind = ("auth" if "key" in low and ("invalid" in low or "expired" in low or "not valid" in low)
                    else "quota" if "threshold" in low or "daily" in low or "limit" in low else "request")
            raise BlsError(f"BLS refused the request ({status}): {text}", kind=kind)
        out["message"] = msgs
        return out

    def ping(self) -> dict:
        """One small query (it counts against the daily limit). Returns reachable/auth_ok/kind/detail; never raises."""
        try:
            y = str(date.today().year - 1)
            self._post({"seriesid": ["LNS14000000"], "startyear": y, "endyear": y}, retries=1)
            return {"reachable": True, "auth_ok": True, "kind": "ok", "detail": ""}
        except BlsError as e:
            return {"reachable": e.kind != "network", "auth_ok": False if e.kind == "auth" else None,
                    "kind": e.kind, "detail": str(e)[:300]}

    # ---- data
    def fetch(self, series: list[str], start: int, end: int, catalog: bool = False,
              annualaverage: bool = False, calculations: bool = False, aspects: bool = False,
              log=lambda m: print(m, file=sys.stderr, flush=True)):
        """Download every series for start..end, splitting into as many queries as the limits require.

        Returns (by_series, messages, raw):
          by_series  {series_id: {"data": [obs, ...], "catalog": {...} or None}}
          messages   every BLS message (per-series problems such as "Series does not exist" included)
          raw        the request bodies (without the key) and responses, for the audit trail
        v2-only options are dropped on v1 (fetch.py records that).
        """
        series = [s.strip().upper() for s in series]
        plan = plan_queries(series, start, end, self.version)
        extras = {}
        if self.version == "v2":
            extras = {k: True for k, v in (("catalog", catalog), ("annualaverage", annualaverage),
                                            ("calculations", calculations), ("aspects", aspects)) if v}
        by_series: dict[str, dict] = {s: {"data": [], "catalog": None} for s in series}
        messages, raw = [], []
        for i, (chunk, y0, y1) in enumerate(plan, start=1):
            log(f"[bls] query {i}/{len(plan)} ({self.version}): {len(chunk)} series, {y0}-{y1}")
            body = {"seriesid": chunk, "startyear": str(y0), "endyear": str(y1), **extras}
            out = self._post(body)
            raw.append({"request": body, "response": out})
            for m in out.get("message", []):
                if "reduced to the system-allowed limit" in m.lower():
                    raise BlsError(f"BLS truncated the year range ({m}); the query plan is wrong", kind="request")
                messages.append(m)
            for s in (out.get("Results") or {}).get("series", []):
                sid = s.get("seriesID", "").upper()
                rec = by_series.setdefault(sid, {"data": [], "catalog": None})
                rec["data"].extend(s.get("data") or [])
                if s.get("catalog") and not rec["catalog"]:
                    rec["catalog"] = s["catalog"]
            if i < len(plan) and not os.environ.get("DATA_ANALYST_FIXTURES"):
                time.sleep(0.25)                      # far below 50 requests per 10 seconds
        return by_series, messages, raw


# ------------------------------------------------------------------ series metadata without a key
DESCRIBE_URL = "https://data.bls.gov/timeseries/{}"
_DESCRIBE = Client("https://data.bls.gov", name="bls-describe", rps=2, burst=3, retries=1, timeout=30,
                   user_agent="Mozilla/5.0 (data-analyst bls pack)")


def describe(series_id: str) -> dict:
    """Series title and dimensions from https://data.bls.gov/timeseries/<id> (works without a key and costs
    no API query). Returns {"series_id", "title", "seasonal", "fields": {...}, "url"} or {"error": ...}."""
    sid = series_id.strip().upper()
    url = DESCRIBE_URL.format(sid)
    try:
        page = _DESCRIBE.get(url).text()
    except DataError as e:
        err = "series not found" if e.kind == "not_found" else f"{e.kind}: {e}"
        return {"series_id": sid, "url": url, "error": err[:200]}
    m = re.search(r"(?s)<table[^>]*catalog.*?</table>", page)
    seg = m.group(0) if m else ""
    cells = [_html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", seg)]
    cells = [re.sub(r"\s+", " ", c) for c in cells if c.strip()]
    cells = [re.sub(r"^(" + re.escape(sid) + r")\s*\(\w{1,2}\)$", r"\1", c) for c in cells]   # "ID (3)": footnote
    fields, seasonal = {}, ""
    for i, c in enumerate(cells):
        if c.endswith(":") and i + 1 < len(cells):
            fields[c[:-1].strip()] = cells[i + 1]
        if c.lower() in ("seasonally adjusted", "not seasonally adjusted"):
            seasonal = c
    if not fields and re.fullmatch(r"OE[US][NSM]\d{7}\d{6}\d{6}\d{2}", sid):
        return _oews_decoded(sid, url)  # data.bls.gov shows no catalog for OEWS: decode, do not call it missing
    if not fields and re.fullmatch(r"C[UW][US][RS]A\d{3}\w+", sid):
        # FRED keeps BLS's pre-2018 metro area codes in its CPI IDs (CUURA101SA0); BLS now uses S-codes
        return {"series_id": sid, "url": url, "error": "series not found. If the ID came from FRED: FRED keeps "
                "pre-2018 metro area codes in many CPI IDs (FRED CUURA101SA0 = BLS CUURS12ASA0, New York; "
                "CUURA207SA0 = CUURS23ASA0, Chicago); take the current area code (here " + sid[4:8] + ") from "
                "https://download.bls.gov/pub/time.series/cu/cu.area and check the title"}
    if not fields:
        return {"series_id": sid, "url": url, "error": "series not found, or the page layout changed"}
    title = next((v for k, v in fields.items() if k.lower() == "series title"), "")
    if not title:                    # LAUS and some other pages list dimensions only
        title = "; ".join(f"{k}: {v}" for k, v in fields.items() if k.lower() != "series id")
    return {"series_id": sid, "title": title, "seasonal": seasonal, "fields": fields, "url": url}


_OEWS_TYPES = {"01": "employment", "02": "employment percent relative standard error", "03": "hourly mean wage",
               "04": "annual mean wage", "05": "wage percent relative standard error", "06": "hourly 10th percentile wage",
               "07": "hourly 25th percentile wage", "08": "hourly median wage", "09": "hourly 75th percentile wage",
               "10": "hourly 90th percentile wage", "11": "annual 10th percentile wage",
               "12": "annual 25th percentile wage", "13": "annual median wage", "14": "annual 75th percentile wage",
               "15": "annual 90th percentile wage", "16": "employment per 1,000 jobs", "17": "location quotient"}


def _oews_decoded(sid: str, url: str) -> dict:
    """OEWS IDs read by their layout: OE + seasonal + area type + area (7) + industry (6) + SOC (6) + data type (2)."""
    area_type, area, ind, soc, dt = sid[3], sid[4:11], sid[11:17], sid[17:23], sid[23:25]
    where = {"N": "national", "S": "state", "M": "metropolitan or nonmetropolitan area"}[area_type]
    fields = {"Area type": where, "Area code": area, "Industry code": ind,
              "Occupation code (SOC)": f"{soc[:2]}-{soc[2:]}" if soc != "000000" else "00-0000 (all occupations)",
              "Data type": _OEWS_TYPES.get(dt, f"code {dt} (unknown)")}
    return {"series_id": sid, "title": f"OEWS {fields['Data type']}, SOC {fields['Occupation code (SOC)']}, {where} "
            f"area {area}", "fields": fields, "url": url,
            "note": "decoded from the ID layout: data.bls.gov has no catalog page for OEWS series, so the ID is not "
                    "verified here. A wrong OEWS ID returns no data at fetch time; check the SOC code against the "
                    "OEWS occupation list (bls.gov/oes)."}


if __name__ == "__main__":
    c = BlsClient()
    print(f"version {c.version}; key: {c.key_source or 'none'}; queries today: {usage_today(c.version)}")
    print(json.dumps(c.ping()))


# ---------------------------------------------------------------------- key check (scripts/setup_key.py)
def verify(key: str) -> tuple[bool | None, str]:
    """Key check for setup.ps1 / setup.sh: one small v2 query with the key in the POST body. (True, detail)
    accepted, (False, detail) rejected (wrong or expired: BLS keys expire after a year), (None, detail) unclear
    (daily limit, BLS refused for another reason). Network errors raise DataError(kind="network"). The key is
    never printed: BLS echoes a rejected key in its message, and every detail goes through redact."""
    p = BlsClient(api_key=key).ping()          # one query of the key's 500 a day
    if p["kind"] == "network":
        raise DataError(f"api.bls.gov could not be reached: {p['detail']}", kind="network")
    if p["auth_ok"]:
        return True, "BLS accepted the key (API v2)."
    if p["auth_ok"] is False:
        return False, redact(f"BLS rejected the key (wrong, or expired: renew at "
                             f"https://data.bls.gov/registrationEngine/): {p['detail']}")
    return None, redact(f"BLS did not confirm the key ({p['kind']}): {p['detail']}")

"""BEA Data API client for the bea pack (standard library only).

Key lookup, redaction, the restricted switch and HTTP all come from the skill's core: the key is found with
core.keys.find_key("BEA_API_KEY", "bea") (environment, BEA_API_KEY_FILE, keys.json, Claude Code settings.json,
~/.bea/api_key), every call goes through core.http.Client (offline fixtures, DATA_ACCESS_FORCE=restricted,
credential-free request log), and every message passes core.keys.redact. BEA puts the key (its "UserID")
in the URL and echoes it in every reply, so scrub_response() removes the echo before anything is saved.

BEA's per-key limits (user guide, 2026): 100 requests, 100 MB and 30 errors a minute; going over any of
them blocks the key (the guide says a minute, BEA's own client plans for an hour). This client keeps a
0.75 s floor between calls (under 80 a minute), waits before a call that could cross 100 MB in the last
minute, stops after 30 errors a minute (core error budget) or 10 in one run, never retries an API error
code, and turns HTTP 429 into kind "rate_limit" without retrying (retrying extends the block).
Metadata calls (GetDataSetList, GetParameterList, GetParameterValues, GetParameterValuesFiltered) are
cached for a day under <cache_root>/bea/meta (policy cache "persistent"), so validating a request costs
no quota the second time.

    from bea_api import BeaClient
    c = BeaClient()
    c.parameter_values("NIPA", "TableName")
    rows, notes, info = c.get_data("NIPA", TableName="T10101", Frequency="Q", Year="2024,2025")
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from core import keys  # noqa: E402
from core import policy as pol  # noqa: E402
from core.cache import Cache  # noqa: E402
from core.http import Client, DataError  # noqa: E402

BASE = "https://apps.bea.gov/api/data"
KEY_VAR = "BEA_API_KEY"
KEY_HOME = "bea"
SIGNUP_URL = "https://apps.bea.gov/API/signup/"
UA = "Mozilla/5.0 (compatible; data-analyst bea pack)"
AUTH = {"env": KEY_VAR, "home": KEY_HOME, "placement": "query", "param": "UserID"}

MIN_INTERVAL_S = 0.75                 # 80 requests a minute at most (BEA allows 100)
MAX_ERRORS_PER_RUN = 10
ERRORS_PER_MIN = 30
BYTES_PER_MIN = 100e6
META_METHODS = {"getdatasetlist", "getparameterlist", "getparametervalues", "getparametervaluesfiltered"}
GUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
KEY_LOCATIONS = keys.key_locations(KEY_VAR, KEY_HOME)

# A BEA key is a 36-character GUID: redact any GUID in messages, so a key pasted into an uploaded reply
# (not one this process looked up) cannot leak either. A GUID that is a whole path segment (a session or temp
# folder, ...\<guid>\...) is a folder name, not a key: redacting it made printed paths unusable.
keys.add_pattern(r"(?<![\\/])" + GUID + r"(?![\\/])")


def policy() -> dict:
    return pol.load(HERE)


def find_api_key() -> tuple[str | None, str | None]:
    return keys.find_key(KEY_VAR, KEY_HOME)


def redact(text) -> str:
    """core.keys.redact plus BEA's echoed request parameter ("ParameterName": "USERID", "ParameterValue": ...)."""
    text = keys.redact(text)
    return re.sub(r'(?i)("ParameterName"\s*:\s*"USERID"\s*,\s*"ParameterValue"\s*:\s*")[^"]*', r"\1[REDACTED]", text)


def scrub_response(obj):
    """A copy of a decoded BEA response with the echoed UserID removed and every string redacted."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and k.lower() == "parametervalue" and str(obj.get("ParameterName", "")).upper() == "USERID":
                out[k] = "[REDACTED]"
            else:
                out[k] = scrub_response(v)
        return out
    if isinstance(obj, list):
        return [scrub_response(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


class BeaError(DataError):
    """A DataError whose message is scrubbed of the key. kind:
      key_missing  no key in any of KEY_LOCATIONS
      auth         BEA rejected the key (APIErrorCode 1, 3 or 4: invalid, unknown, or not activated)
      rate_limit   HTTP 429 or code 7: the key is blocked for a minute to an hour
      budget       this run stopped itself before BEA's per-minute limits were crossed
      network      BEA could not be reached (offline, proxy, sandbox, DATA_ACCESS_FORCE=restricted)
      api          BEA answered with an error about the request (bad table, parameter or year)
      http         any other HTTP error;   other  anything else
    """

    def __init__(self, msg: str, kind: str = "other", code: str | None = None):
        super().__init__(redact(msg), kind=kind)
        self.code = code


def first_error(payload: dict) -> dict | None:
    """BEA puts errors in BEAAPI.Error or BEAAPI.Results.Error (a dict or a list)."""
    b = payload.get("BEAAPI", payload) if isinstance(payload, dict) else {}
    res = b.get("Results") if isinstance(b, dict) else None
    if isinstance(res, list):
        res = res[0] if res else {}
    for holder in (b, res if isinstance(res, dict) else {}):
        err = holder.get("Error") if isinstance(holder, dict) else None
        if isinstance(err, list) and err:
            err = err[0]
        if isinstance(err, dict) and err:
            return err
    return None


def error_detail(err: dict) -> str:
    """APIErrorDescription plus ErrorDetail, which can be a string, {Description} or [{Description}]."""
    desc = str(err.get("APIErrorDescription") or "")
    d = err.get("ErrorDetail")
    if isinstance(d, list):
        d = "; ".join(str(x.get("Description", x)) if isinstance(x, dict) else str(x) for x in d)
    elif isinstance(d, dict):
        d = d.get("Description") or json.dumps(d)
    if d:
        desc = f"{desc} ({d})" if desc else str(d)
    return desc or json.dumps(err)


def is_auth_error(code: str, desc: str) -> bool:
    d = desc.lower()
    return code in ("1", "3", "4") or "userid" in d or "user id" in d or "api key" in d or "not active" in d


def unwrap_results(payload: dict) -> dict:
    """BEAAPI.Results as a dict: a one-item list is unwrapped, IIP's BEAAPI.Data is moved inside, and
    APIDatasetMetaData (not wrapped in Results) is returned whole."""
    b = payload.get("BEAAPI", {}) if isinstance(payload, dict) else {}
    res = b.get("Results")
    if isinstance(res, list):
        res = res[0] if res else {}
    res = dict(res or {})
    if "Data" in b and "Data" not in res:
        res["Data"] = b["Data"]
    if not res and isinstance(b, dict):
        res = {k: v for k, v in b.items() if k != "Request"}
    return res


def as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class BeaClient:
    def __repr__(self) -> str:
        return "BeaClient(key=[REDACTED])"

    def __init__(self, api_key: str | None = None, timeout: int = 120, use_cache: bool = True):
        if api_key:
            keys.remember(api_key)
            self.key, self.key_source = api_key, "argument"
        else:
            self.key, self.key_source = find_api_key()
        if not self.key:
            raise BeaError("No BEA API key found. Looked in: " + "; ".join(KEY_LOCATIONS) + f". Get a free key "
                           f"at {SIGNUP_URL} (BEA emails it; click the activation link), then store it with the "
                           "skill's setup script (setup.ps1 -Source bea / setup.sh bea).", kind="key_missing")
        self.client = Client(BASE, name="bea", auth=AUTH, key=self.key, rps=1 / MIN_INTERVAL_S, burst=1,
                             retries=0, timeout=timeout, deadline=timeout * 3, user_agent=UA,
                             errors_per_min=ERRORS_PER_MIN, bytes_per_min=BYTES_PER_MIN,
                             headers={"Accept": "application/json"})
        self.timeout = timeout
        self.errors = 0
        self.calls = 0
        self._bytes: list[tuple[float, int]] = []
        try:
            self.meta_cache = Cache(os.path.join("bea", "meta"), policy(), enabled=use_cache)
        except OSError:
            self.meta_cache = Cache(os.path.join("bea", "meta"), policy(), enabled=False)

    # ---- budgets -----------------------------------------------------------------
    def _wait_for_bytes(self):
        """Pause before a call that could push the last minute's download over 100 MB (headroom: the
        largest reply seen in that minute), as BEA's own client does."""
        while True:
            now = time.monotonic()
            self._bytes = [(t, n) for t, n in self._bytes if now - t < 60]
            used = sum(n for _, n in self._bytes)
            head = max((n for _, n in self._bytes), default=0)
            if not self._bytes or used + head <= BYTES_PER_MIN * 0.95:
                return
            wait = 60 - (now - self._bytes[0][0]) + 0.5
            print(f"[bea] {used / 1e6:.0f} MB in the last minute: waiting {wait:.0f} s to stay under BEA's 100 MB "
                  "a minute", file=sys.stderr, flush=True)
            time.sleep(max(wait, 0.5))

    def _error(self):
        self.errors += 1
        try:
            self.client.error_budget.spend()
        except DataError as e:
            raise BeaError(str(e), kind="budget") from None

    # ---- one call ----------------------------------------------------------------
    def request(self, method: str, retries: int = 2, cache: bool | None = None, **params) -> dict:
        """One API call. Returns BEAAPI.Results (a dict). Raises BeaError with a kind on failure."""
        if self.errors >= MAX_ERRORS_PER_RUN:
            raise BeaError(f"Stopped after {self.errors} failed calls in this run, to stay under BEA's limit of "
                           "30 errors a minute (going over locks the key out). Fix the request first.", kind="budget")
        q = {"method": method, "ResultFormat": "JSON"}
        q.update({k: v for k, v in params.items() if v is not None and v != ""})
        use_cache = (method.lower() in META_METHODS) if cache is None else cache
        ckey = json.dumps({k.lower(): str(v) for k, v in q.items()}, sort_keys=True)
        if use_cache and self.meta_cache.on:
            hit = self.meta_cache.get(ckey)
            if hit:
                try:
                    return json.loads(hit[0].decode("utf-8"))
                except ValueError:
                    pass
        shown = f"{method} {redact(json.dumps(params))}"
        for attempt in range(retries + 1):
            self._wait_for_bytes()
            self.calls += 1
            try:
                r = self.client.request("GET", "", q, ok_statuses=(200, 429, 500, 502, 503, 504))
            except DataError as e:
                if e.kind == "network":
                    if attempt < retries and not keys.restricted():
                        time.sleep(5 * 2 ** attempt)
                        continue
                    raise BeaError(f"BEA (apps.bea.gov) could not be reached: {e}", kind="network") from None
                if e.kind == "rate_limit":
                    raise BeaError(str(e), kind="budget") from None
                if e.kind == "fixture_missing":
                    raise BeaError(str(e), kind="other") from None
                self.errors += 1
                raise BeaError(f"{shown} failed: {e}", kind="http") from None
            self._bytes.append((time.monotonic(), len(r.body)))
            if r.status == 429:
                self._error()
                retry = r.headers.get("retry-after", "unknown")
                raise BeaError(f"HTTP 429 from BEA: the key is rate-limited (Retry-After {retry} s). BEA allows "
                               "100 requests, 100 MB and 30 errors a minute. The guide says the block lasts a "
                               "minute; BEA's own client plans for up to an hour. Wait at least Retry-After, then "
                               "rerun once; retrying during the block extends it.", kind="rate_limit")
            if r.status >= 500:
                if attempt < retries:
                    time.sleep(5 * 2 ** attempt)
                    continue
                self._error()
                raise BeaError(f"{shown} failed: HTTP {r.status}", kind="http")
            raw = r.body
            if not raw.strip():
                self._error()
                raise BeaError(f"BEA returned an empty reply for {shown}. That usually means the request timed out "
                               "on BEA's side (or the UserID was missing): narrow it (fewer years, a GeoFips list "
                               "instead of COUNTY, one line) and try again.", kind="api")
            try:
                try:
                    payload = json.loads(raw.decode("utf-8-sig"))
                except UnicodeDecodeError:
                    payload = json.loads(raw.decode("iso-8859-1"))
            except ValueError:
                self._error()
                raise BeaError(f"BEA returned something that is not JSON for {shown}: {raw[:300]!r}",
                               kind="http") from None
            err = first_error(payload)
            if err:
                self._error()
                code = str(err.get("APIErrorCode", "")).strip()
                desc = error_detail(err)
                if is_auth_error(code, desc):
                    raise BeaError(f"BEA rejected the API key: {desc} (APIErrorCode {code}). The key may be "
                                   f"mistyped or not yet activated (BEA emails an activation link): {SIGNUP_URL}",
                                   kind="auth", code=code)
                if code == "7" or "quota" in desc.lower() or ("limit" in desc.lower() and "exceed" in desc.lower()):
                    raise BeaError(f"BEA rate limit: {desc}", kind="rate_limit", code=code)
                raise BeaError(f"BEA could not answer {shown}: {desc} (APIErrorCode {code})", kind="api", code=code)
            res = scrub_response(unwrap_results(payload))
            if use_cache and self.meta_cache.on:
                self.meta_cache.put(ckey, json.dumps(res).encode("utf-8"), {"method": method})
            return res
        raise BeaError("unreachable")

    def ping(self) -> dict:
        """One cheap, uncached call (GetDataSetList). {'reachable', 'auth_ok', 'kind', 'detail'}; never raises."""
        try:
            self.request("GetDataSetList", retries=1, cache=False)
            return {"reachable": True, "auth_ok": True, "kind": "ok", "detail": ""}
        except BeaError as e:
            reachable = e.kind != "network"
            auth_ok = None if not reachable else (False if e.kind == "auth" else None)
            return {"reachable": reachable, "auth_ok": auth_ok, "kind": e.kind,
                    "detail": str(e).splitlines()[0][:300]}

    # ---- metadata (cached) ---------------------------------------------------------
    def datasets(self) -> list[dict]:
        return as_list(self.request("GetDataSetList").get("Dataset"))

    def parameters(self, dataset: str) -> list[dict]:
        return as_list(self.request("GetParameterList", DataSetName=dataset).get("Parameter"))

    def parameter_values(self, dataset: str, parameter: str) -> list[dict]:
        return as_list(self.request("GetParameterValues", DataSetName=dataset, ParameterName=parameter).get("ParamValue"))

    def parameter_values_filtered(self, dataset: str, target: str, **filters) -> list[dict]:
        return as_list(self.request("GetParameterValuesFiltered", DataSetName=dataset, TargetParameter=target,
                                    **filters).get("ParamValue"))

    # ---- data ----------------------------------------------------------------------
    def get_data(self, dataset: str, **params) -> tuple[list[dict], list[dict], dict]:
        """GetData. Returns (rows, notes, results-without-Data): the last holds Dimensions, Statistic, etc."""
        res = self.request("GetData", DataSetName=dataset, **params)
        rows = as_list(res.get("Data"))
        notes = as_list(res.get("Notes"))
        info = {k: v for k, v in res.items() if k not in ("Data",)}
        return rows, notes, info


def value_key(v: dict) -> str:
    """The code in a GetParameterValues row (attribute names differ by dataset)."""
    for k in ("Key", "TableName", "TableID", "TableNumber", "Code"):
        if v.get(k) not in (None, ""):
            return str(v[k])
    return str(next(iter(v.values()), ""))


# ------------------------------------------------------------------ datasets beyond NIPA and Regional
# Required GetData parameters and the multi-value rules of each dataset (BEA user guide
# appendices). Used to refuse a request before it costs an API error.
DATASETS = {
    "NIPA": {"required": ["TableName", "Frequency", "Year"], "bulk": True,
             "about": "National income and product accounts; TableName T10101 ...; Frequency A, Q, M; Year list or X"},
    "NIUnderlyingDetail": {"required": ["TableName", "Frequency", "Year"],
                           "about": "Underlying detail tables (U20305 ...); lower quality than published aggregates; API only (the flat files hold only the lines shared with published tables)"},
    "FixedAssets": {"required": ["TableName", "Year"],
                    "about": "Fixed assets (FAAt101 ...); annual only; updated late August to early October"},
    "Regional": {"required": ["TableName", "LineCode", "GeoFips"], "bulk": True,
                 "about": "State, county and metro tables (SAGDP9, CAINC1 ...); LineCode ALL allows one GeoFips only"},
    "GDPbyIndustry": {"required": ["TableID", "Frequency", "Year", "Industry"],
                      "about": "API TableIDs (1 value added, 5 contributions, 6 shares, 25 real value added); "
                               "Q from 2005; prefer the keyless workbook codes TVA1xx/TGO1xx/TII1xx"},
    "UnderlyingGDPbyIndustry": {"required": ["TableID", "Frequency", "Year", "Industry"], "freq_only": ["A"],
                                "about": "More industry detail, annual only (Q is error 206), from 1997"},
    "ITA": {"required": [], "about": "International transactions: Indicator (BalGds, BalServ, BalCurrAcct, ExpGds, "
                                     "ImpGds ...), AreaOrCountry (AllCountries = total, All = every area, China ...), "
                                     "Frequency A/QSA/QNSA, Year",
            "one_of_many": ("Indicator", "AreaOrCountry")},
    "IIP": {"required": [], "about": "International investment position: TypeOfInvestment (FinAssetsExclFinDeriv, "
                                     "FinLiabsExclFinDeriv, DirInvAssets ...), Component (Pos, ChgPosTrans, "
                                     "ChgPosPrice, ChgPosXRate), Frequency A/QNSA, Year",
            "one_of_many": ("TypeOfInvestment", "Year")},
    "IntlServTrade": {"required": [], "about": "Trade in services, annual: TypeOfService, TradeDirection "
                                               "(Exports, Imports, Balance, SupplementalIns), Affiliation, "
                                               "AreaOrCountry, Year",
                      "one_of_many": ("TypeOfService", "AreaOrCountry")},
    "IntlServSTA": {"required": [], "about": "Services supplied through affiliates: Channel, Destination, Industry, "
                                             "AreaOrCountry, Year"},
    "MNE": {"required": ["DirectionOfInvestment", "Classification", "Year"],
            "about": "Direct investment (DI) and activities of multinationals (AMNE): DirectionOfInvestment "
                     "Outward/Inward (DI and AMNE) or State/Parent (AMNE), Classification (Country, Industry, "
                     "CountryByIndustry); AMNE also needs OwnershipLevel (0/1) and NonBankAffiliatesOnly (0/1)"},
    "InputOutput": {"required": ["TableID", "Year"],
                    "about": "Make, use and requirements tables (TableID 56, 57 ...); values change: list them first"},
    "APIDatasetMetaData": {"required": ["dataset"], "about": "Release dates and line structure of NIPA, "
                                                            "NIUnderlyingDetail and FixedAssets tables (not wrapped "
                                                            "in Results)"},
}
_ALL = {"all", "allcountries", "x", ""}


def dataset_problems(dataset: str, params: dict) -> list[str]:
    """Problems BEA would answer with an error (each error counts against 30 a minute)."""
    probs = []
    spec = next((v for k, v in DATASETS.items() if k.lower() == str(dataset).lower()), None)
    if spec is None:
        return [f"unknown dataset {dataset!r}; BEA's datasets are {', '.join(DATASETS)}"]
    have = {k.lower(): str(v) for k, v in params.items()}
    for p in spec["required"]:
        if p.lower() not in have:
            probs.append(f"{dataset} needs the parameter {p} (see fetch.py --source bea --dataset-info {dataset})")
    if spec.get("freq_only") and have.get("frequency") and set(have["frequency"].upper().split(",")) - set(spec["freq_only"]):
        probs.append(f"{dataset} has frequency {', '.join(spec['freq_only'])} only")
    pair = spec.get("one_of_many")
    if pair:
        a, b = (have.get(pair[0].lower(), ""), have.get(pair[1].lower(), ""))
        many_a = "," in a or a.lower() in _ALL
        many_b = "," in b or b.lower() in _ALL
        if "," in a and many_b or "," in b and many_a:
            probs.append(f"{dataset}: several {pair[0]} values need exactly one specific {pair[1]} (and vice "
                         "versa); BEA refuses both lists at once")
    if dataset.lower() == "mne" and have.get("directionofinvestment", "").lower() in ("state", "parent"):
        for p in ("OwnershipLevel", "NonBankAffiliatesOnly"):
            if p.lower() not in have:
                probs.append(f"MNE AMNE requests ({have['directionofinvestment']}) need {p}")
    if dataset.lower() == "regional" and have.get("linecode", "").upper() == "ALL" and "," in have.get("geofips", ""):
        probs.append("Regional: LineCode ALL allows only one GeoFips")
    return probs


if __name__ == "__main__":
    c = BeaClient()
    ds = c.datasets()
    print(redact(f"key from {c.key_source}: {len(ds)} datasets: {', '.join(d.get('DatasetName', '') for d in ds)}"))


# ---------------------------------------------------------------------- key check (scripts/setup_key.py)
def verify(key) -> tuple[bool | None, str]:
    """Key check for the skill's setup scripts: one GetDataSetList call with the key. An Error in the reply
    (codes 1, 3, 4: invalid, unknown or not yet activated) is a rejection; HTTP 429 or another failure is
    "unclear"; no connection raises a network DataError so setup_key reports a network problem. The key is
    never printed."""
    try:
        p = BeaClient(api_key=str(key).strip(), timeout=30, use_cache=False).ping()
    except BeaError as e:
        return None, str(e)
    if p["kind"] == "ok":
        return True, "BEA accepted the key (GetDataSetList answered)."
    if p["kind"] == "network":
        raise DataError(p["detail"], kind="network")
    if p["kind"] == "auth":
        return False, "BEA rejected the key: it is mistyped, or not activated yet (click the link in BEA's email)."
    if p["kind"] in ("rate_limit", "budget"):
        return None, "BEA is rate-limiting this key right now (HTTP 429); try again in an hour."
    return None, p["detail"]

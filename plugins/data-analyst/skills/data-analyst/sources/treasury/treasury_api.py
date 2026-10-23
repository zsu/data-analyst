"""Client for the U.S. Treasury Fiscal Data API (standard library only; the treasury pack).

    from treasury_api import FiscalDataClient, filter_string
    c = FiscalDataClient()
    meta = c.meta("v2/accounting/od/debt_to_penny")           # labels, dataTypes, total-count, one row
    rows, meta = c.fetch_all("v2/accounting/od/debt_to_penny",
                             fields=["record_date", "tot_pub_debt_out_amt"],
                             filters={"record_date": {"gte": "2015-01-01", "lte": "2025-09-30"}},
                             sort="record_date")

API facts this client relies on (https://fiscaldata.treasury.gov/api-documentation/; docs/api_guide.md):
  - Base URL https://api.fiscaldata.treasury.gov/services/api/fiscal_service/ + endpoint path.
  - No API key, no registration. Plain HTTPS GET.
  - Parameters: fields=a,b  filter=f:op:value,f2:in:(x,y)  sort=-f,g  format=json|csv|xml
    page[number]=1  page[size]=N (at most 10000).
  - Operators: eq, lt, lte, gt, gte, in.
  - Every value comes back as a string; missing values are the string "null".
  - meta: count, labels, dataTypes, dataFormats, total-count, total-pages.
Every request goes through core.http.Client (offline fixtures, DATA_ACCESS_FORCE=restricted, retries with
backoff on 429/5xx, the credential-free request log). Errors raise FiscalDataError with .kind: "network"
(cannot reach the API), "bad_request" (400: a field, filter or sort the endpoint does not have),
"not_found" (404: wrong endpoint path), "http" (anything else). Proxies: urllib honours HTTPS_PROXY.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from core.http import Client, DataError  # noqa: E402
from core.keys import redact  # noqa: E402

BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
MAX_PAGE = 10000
OPERATORS = ("eq", "lt", "lte", "gt", "gte", "in")
USER_AGENT = "data-analyst/1.0 treasury pack (+https://fiscaldata.treasury.gov/api-documentation/)"


class FiscalDataError(Exception):
    def __init__(self, msg: str, kind: str = "http", status: int | None = None):
        super().__init__(redact(msg))
        self.kind = kind
        self.status = status


def normalize_endpoint(endpoint: str) -> str:
    """Accept a bare path ("v2/accounting/od/debt_to_penny"), one with a leading slash, or a full URL."""
    e = endpoint.strip()
    if e.startswith("http"):
        e = e.split("/fiscal_service/", 1)[-1]
    e = e.split("?", 1)[0].strip("/")
    if not e.startswith(("v1/", "v2/")):
        raise FiscalDataError(f"endpoint {endpoint!r} should look like v1/accounting/mts/mts_table_1", "bad_request")
    return e


def _value(v) -> str:
    return str(v).strip()


def filter_string(filters) -> str:
    """Friendly filter -> API filter string.

    {"record_date": {"gte": "2015-10-01", "lte": "2025-09-30"},
     "security_type_desc": {"in": ["Marketable", "Non-marketable"]},
     "classification_desc": "Total Receipts"}                       # bare value means eq
    -> "record_date:gte:2015-10-01,record_date:lte:2025-09-30,security_type_desc:in:(Marketable,Non-marketable),
        classification_desc:eq:Total Receipts"
    A string is passed through unchanged (already in API syntax).
    """
    if not filters:
        return ""
    if isinstance(filters, str):
        return filters
    parts = []
    for field, cond in filters.items():
        if isinstance(cond, dict):
            for op, val in cond.items():
                if op not in OPERATORS:
                    raise FiscalDataError(f"filter operator {op!r} on {field}: use one of {', '.join(OPERATORS)}",
                                          "bad_request")
                if op == "in":
                    vals = val if isinstance(val, (list, tuple)) else [val]
                    parts.append(f"{field}:in:({','.join(_value(x) for x in vals)})")
                else:
                    parts.append(f"{field}:{op}:{_value(val)}")
        elif isinstance(cond, (list, tuple)):
            parts.append(f"{field}:in:({','.join(_value(x) for x in cond)})")
        else:
            parts.append(f"{field}:eq:{_value(cond)}")
    return ",".join(parts)


def query_params(fields=None, filters=None, sort=None, page_size=MAX_PAGE, page_number=1, fmt="json") -> dict:
    p = {}
    if fields:
        p["fields"] = ",".join(fields) if isinstance(fields, (list, tuple)) else str(fields)
    f = filter_string(filters)
    if f:
        p["filter"] = f
    if sort:
        p["sort"] = ",".join(sort) if isinstance(sort, (list, tuple)) else str(sort)
    if fmt and fmt != "json":
        p["format"] = fmt
    p["page[number]"] = str(page_number)
    p["page[size]"] = str(min(int(page_size), MAX_PAGE))
    return p


def build_url(endpoint: str, params: dict) -> str:
    # Encode brackets as %5B %5D so the URL works in curl (no globbing) and PowerShell alike.
    # Spaces become %20 (not "+"), which the API decodes reliably in filter values.
    return BASE + normalize_endpoint(endpoint) + "?" + urllib.parse.urlencode(
        params, safe=",:()-", quote_via=urllib.parse.quote)


class FiscalDataClient:
    def __init__(self, timeout: int = 60, retries: int = 4, verbose: bool = True):
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self._client = None

    def _log(self, msg: str):
        if self.verbose:
            print(redact(f"[fiscaldata] {msg}"), file=sys.stderr, flush=True)

    def client(self) -> Client:
        if self._client is None or self._client.retries != self.retries:
            self._client = Client("https://api.fiscaldata.treasury.gov", name="treasury", rps=4, burst=8,
                                  retries=self.retries, timeout=self.timeout, deadline=self.timeout * (self.retries + 2),
                                  user_agent=USER_AGENT)
        return self._client

    def get_raw(self, url: str) -> bytes:
        try:
            r = self.client().request("GET", url, ok_statuses=(200, 400, 404))
        except DataError as e:
            if e.kind == "network":
                raise FiscalDataError(f"cannot reach api.fiscaldata.treasury.gov: {e}", "network") from None
            raise FiscalDataError(str(e), "http", e.status) from None
        if r.status == 200:
            return r.body
        body = r.body.decode("utf-8", "replace")[:600]
        msg = body
        try:
            j = json.loads(body)
            msg = j.get("message") or j.get("error") or body
            if isinstance(msg, (list, dict)):
                msg = json.dumps(msg)
        except ValueError:
            pass
        if r.status == 404:             # the API answers a wrong path or version with an HTML page
            msg = ("no such endpoint. Each endpoint has one fixed version (v1 or v2); find the exact "
                   "path with fetch.py --source treasury --search")
            raise FiscalDataError(f"HTTP 404 for {url}: {msg}", "not_found", 404)
        raise FiscalDataError(f"HTTP {r.status} for {url}: {msg}", "bad_request", r.status)

    def get_json(self, endpoint: str, params: dict) -> dict:
        raw = self.get_raw(build_url(endpoint, params))
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise FiscalDataError(f"the API returned something that is not JSON: {raw[:200]!r}", "http") from e

    def ping(self) -> dict:
        """One tiny request. Returns {"reachable": bool, "detail": str}."""
        saved = self.retries
        self.retries = 1
        try:
            self.get_json("v2/accounting/od/debt_to_penny", {"fields": "record_date", "page[size]": "1",
                                                              "sort": "-record_date"})
            return {"reachable": True, "detail": "ok"}
        except FiscalDataError as e:
            return {"reachable": False, "detail": str(e)[:300], "kind": e.kind}
        finally:
            self.retries = saved

    def meta(self, endpoint: str, filters=None, date_field: str | None = "record_date", fields=None) -> dict:
        """labels, dataTypes, dataFormats, total-count (under the filter and fields) and the first and last
        row by date_field (when the endpoint has that column; some use operation_date, eff_date ...)."""
        j = self.get_json(endpoint, query_params(fields=fields, filters=filters, page_size=1))
        m = dict(j.get("meta") or {})
        m["first_row"] = m["last_row"] = None
        if date_field and date_field in (m.get("labels") or {}):
            for key, sort in (("first_row", date_field), ("last_row", "-" + date_field)):
                try:
                    r = self.get_json(endpoint, query_params(fields=fields, filters=filters, page_size=1, sort=sort))
                    m[key] = (r.get("data") or [None])[0]
                except FiscalDataError as e:
                    if e.kind == "network":
                        raise
        return m

    def fetch_all(self, endpoint: str, fields=None, filters=None, sort=None, page_size: int = MAX_PAGE,
                  max_rows: int | None = None) -> tuple[list[dict], dict]:
        """Every row that matches, across pages. Checks the row count against meta.total-count."""
        rows: list[dict] = []
        page = 1
        meta: dict = {}
        while True:
            j = self.get_json(endpoint, query_params(fields, filters, sort, page_size, page))
            meta = j.get("meta") or meta
            data = j.get("data") or []
            rows.extend(data)
            total = int(meta.get("total-count") or 0)
            pages = int(meta.get("total-pages") or 1)
            self._log(f"{normalize_endpoint(endpoint)}: page {page}/{pages}, {len(rows):,}/{total:,} rows")
            if max_rows and len(rows) >= max_rows:
                rows = rows[:max_rows]
                break
            if page >= pages or not data:
                break
            page += 1
        total = int(meta.get("total-count") or len(rows))
        if not max_rows and len(rows) != total:
            raise FiscalDataError(f"row count check failed for {endpoint}: got {len(rows):,}, the API reports "
                                  f"{total:,}. The data may have been updated during the download; run again.",
                                  "http")
        return rows, meta

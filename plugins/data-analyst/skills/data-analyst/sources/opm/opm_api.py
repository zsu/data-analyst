"""Minimal client for the OPM Federal Workforce Data (FWD) file API (standard library only), on the
shared core.http.Client (rate limits, retries, request log, offline fixtures, restricted switch).

No account or key is needed. Endpoints (https://data.opm.gov/get-data/api-access):
    GET /api/v1/files/{dataset}                       list files (JSON); filters: current, year, month,
                                                      version, dataCapturedThrough, publishedBefore/After
    GET /api/v1/files/{dataset}/{year}/{month}/{version}/download     one Parquet file
Datasets: employment (monthly status snapshot, ~2 million rows, 26-75 MB), accessions and separations
(personnel actions processed in the month, 0.1-4 MB).

    from opm_api import OpmClient
    c = OpmClient()
    files = c.list_files("separations", current=True)
    c.download("separations", "2026", "08", 1, "sep.parquet")
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from core import keys
from core.http import SOURCES, Client, DataError

BASE = "https://data.opm.gov/api/v1/files"
DATASETS = ("employment", "accessions", "separations")
UA = "data-analyst/1.0 opm pack (+https://data.opm.gov/get-data/api-access)"

HINTS = {
    400: "400 Bad Request: the request is malformed or over the 25 MB request limit.",
    404: "404 Not Found: no file for that dataset/year/month/version. List the files to see what exists.",
    429: "429 Too Many Requests: OPM rate limit reached; wait a minute and retry.",
    500: "500 Internal Server Error at OPM; retry later.",
}


class OpmError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, kind: str = "http"):
        super().__init__(keys.redact(msg))
        self.status = status
        self.kind = kind


def _offline() -> bool:
    return bool(os.environ.get("DATA_ANALYST_FIXTURES")) or keys.restricted()


class OpmClient:
    def __init__(self, base: str = BASE, timeout: int = 60, retries: int = 4):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.http = Client(self.base, name="data.opm.gov", rps=4, burst=8, retries=retries, timeout=timeout,
                           deadline=max(180, timeout * (retries + 1)), user_agent=UA)

    # ------------------------------------------------------------------ low level
    def _get(self, url: str, timeout: int | None = None):
        if timeout:
            self.http.timeout = timeout
        try:
            return self.http.request("GET", url, ok_statuses=(200, 204))
        except DataError as e:
            if e.kind in ("network", "fixture_missing"):
                raise OpmError(f"cannot reach data.opm.gov ({e})", kind="network") from None
            raise OpmError(HINTS.get(e.status, str(e)), e.status, kind=e.kind) from None

    def _stream(self, url: str, path: str, timeout: int) -> tuple[int, str]:
        """Large files: stream to disk (core.http reads whole bodies into memory)."""
        last = None
        for attempt in range(self.retries + 1):
            n, h = 0, hashlib.sha256()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        n += len(chunk)
                SOURCES.append({"method": "GET", "url": url, "status": 200, "bytes": n, "sha256": h.hexdigest(),
                                "cached": False, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                return n, h.hexdigest()
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(60, 2 ** attempt * 3) + random.random())
                    continue
                raise OpmError(HINTS.get(e.code, f"HTTP {e.code} from {url}"), e.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
                if attempt < self.retries:
                    time.sleep(2 ** attempt + random.random())
                    continue
                raise OpmError(f"cannot reach data.opm.gov ({getattr(e, 'reason', e)})", kind="network") from None
        raise OpmError(str(last))

    # ------------------------------------------------------------------ API
    def list_files(self, dataset: str, **params) -> list[dict]:
        """File metadata: [{filename, publishDate, version, current, month, year}, ...]."""
        if dataset not in DATASETS:
            raise ValueError(f"dataset must be one of {DATASETS}")
        q = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
        url = f"{self.base}/{dataset}"
        if q:
            url += "?" + urllib.parse.urlencode(q)
        r = self._get(url)
        if r.status == 204 or not r.body.strip():
            return []
        return json.loads(r.body.decode("utf-8-sig"))

    def download_url(self, dataset: str, year, month, version) -> str:
        return f"{self.base}/{dataset}/{int(year):04d}/{int(month):02d}/{int(version)}/download"

    def download(self, dataset: str, year, month, version, path: str, progress=None) -> int:
        """Stream one Parquet file to `path` (atomic). Returns bytes written."""
        url = self.download_url(dataset, year, month, version)
        tmp = path + ".part"
        if _offline():
            r = self._get(url, timeout=max(self.timeout, 300))
            with open(tmp, "wb") as f:
                f.write(r.body)
            n = len(r.body)
        else:
            n, _ = self._stream(url, tmp, max(self.timeout, 300))
        if progress:
            progress(n)
        with open(tmp, "rb") as f:
            if f.read(4) != b"PAR1":
                os.remove(tmp)
                raise OpmError(f"{url} did not return a Parquet file")
        os.replace(tmp, path)
        return n

    def ping(self) -> tuple[bool, str]:
        """(True, "") if the list endpoint answers; else (False, reason)."""
        try:
            self.list_files("accessions", current=True, year=2025, month=1)
            return True, ""
        except OpmError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, keys.redact(f"{type(e).__name__}: {e}")


def current_index(files: list[dict]) -> dict[tuple[int, int], dict]:
    """{(year, month): metadata of the current version}. Falls back to the highest version."""
    out: dict[tuple[int, int], dict] = {}
    for f in files:
        k = (int(f["year"]), int(f["month"]))
        old = out.get(k)
        if old is None or (f.get("current") and not old.get("current")) or (
                f.get("current") == old.get("current") and int(f["version"]) > int(old["version"])):
            out[k] = f
    return out

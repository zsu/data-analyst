"""Minimal IPUMS API v2 client (standard library only), on the shared core.http.Client.

The key comes from core.keys.find_key("IPUMS_API_KEY", "ipums"): the IPUMS_API_KEY environment variable,
the file named by IPUMS_API_KEY_FILE, env.IPUMS_API_KEY in keys.json or Claude Code's settings.json, or
~/.ipums/api_key, in that order. It is sent only as the Authorization header and never printed; every
message goes through core.keys.redact.

    from ipums_api import IpumsClient
    c = IpumsClient()
    c.list_samples("usa")

Offline tests: DATA_ANALYST_FIXTURES serves every call (downloads included) from fixtures;
DATA_ACCESS_FORCE=restricted makes every call a network error and hides the key.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core import keys
from core.http import SOURCES, UA, Client, DataError, open_url

BASE = "https://api.ipums.org"
VERSION = "2"
KEY_ENV, KEY_HOME = "IPUMS_API_KEY", "ipums"
AUTH = {"env": KEY_ENV, "home": KEY_HOME, "placement": "header", "param": "Authorization", "prefix": ""}

HINTS = {
    401: ("401 Unauthorized. The key is wrong, or the account is not registered for this collection "
          "(each collection, e.g. usa.ipums.org or cps.ipums.org, has its own registration and terms of use)."),
    403: "403 Forbidden. The account may not have access to this collection or sample.",
    429: "429 Too Many Requests. Rate limited by IPUMS; wait and retry.",
}
ALL_STATUSES = tuple(range(100, 600))


def find_api_key() -> tuple[str | None, str | None]:
    """(key, source label). The label may be printed; the key never is."""
    return keys.find_key(KEY_ENV, KEY_HOME)


class IpumsError(RuntimeError):
    """Error whose message is always scrubbed of the API key.

    kind says why the call failed, so callers can pick the right recovery:
      key_missing  no key in any of the four locations
      auth         HTTP 401/403 (key invalid, or account not registered for the collection)
      network      IPUMS could not be reached (offline, proxy, sandbox, DATA_ACCESS_FORCE=restricted)
      http         any other HTTP error
      other        anything else (failed or canceled extract, timeout, checksum)
    """

    def __init__(self, msg: str, kind: str = "other"):
        super().__init__(keys.redact(msg))
        self.kind = kind


class IpumsClient:
    def __repr__(self) -> str:                   # never show the key in tracebacks or logs
        return "IpumsClient(key=[REDACTED])"

    def __init__(self, api_key: str | None = None, timeout: int = 120):
        if api_key:
            keys.remember(api_key)
            self.key, self.key_source = api_key, "argument"
        else:
            self.key, self.key_source = find_api_key()
        if not self.key:
            locs = "; ".join(keys.key_locations(KEY_ENV, KEY_HOME))
            raise IpumsError(f"No IPUMS API key found. Looked in: {locs}. Get a key at "
                             "https://account.ipums.org/api_keys, then run the skill's setup script "
                             "(setup.ps1 -Source ipums on Windows, setup.sh ipums on macOS/Linux); it stores the "
                             "key in keys.json, which is read at once (no restart needed).", kind="key_missing")
        self.timeout = timeout
        self.http = Client(BASE, name="IPUMS", auth=AUTH, key=self.key, rps=1.5, burst=5, retries=4,
                           timeout=timeout, deadline=900, headers={"Content-Type": "application/json"})

    def ping(self, collection: str = "usa") -> dict:
        """One cheap metadata call. Returns {'reachable', 'auth_ok', 'kind', 'detail'}; never raises."""
        try:
            self._request("GET", "/metadata/samples", {"collection": collection, "pageSize": 1}, retries=1)
            return {"reachable": True, "auth_ok": True, "kind": "ok", "detail": ""}
        except IpumsError as e:
            reachable = e.kind != "network"
            return {"reachable": reachable, "auth_ok": False if reachable else None, "kind": e.kind,
                    "detail": str(e).splitlines()[0][:300]}

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, retries: int = 4) -> dict:
        params = dict(params or {})
        params.setdefault("version", VERSION)
        self.http.retries = retries
        try:
            r = self.http.request(method, path, params, json_body=body, ok_statuses=ALL_STATUSES)
        except DataError as e:
            kind = {"network": "network", "auth": "auth", "key_missing": "auth"}.get(e.kind, "http")
            raise IpumsError(f"{method} {path} {'network error' if kind == 'network' else 'failed'}: {e}",
                             kind=kind) from None
        if 200 <= r.status < 300:
            return r.json() if r.body.strip() else {}
        detail = r.body[:2000].decode("utf-8", "replace")
        raise IpumsError(f"{method} {path} failed: HTTP {r.status}. {HINTS.get(r.status, '')}\n{detail}",
                         kind="auth" if r.status in (401, 403) else "http")

    # ---- metadata --------------------------------------------------------
    def list_samples(self, collection: str) -> list[dict]:
        """Return [{'name': 'us2023a', 'description': '2023 ACS'}, ...]."""
        out, page = [], 1
        while True:
            r = self._request("GET", "/metadata/samples",
                              {"collection": collection, "pageNumber": page, "pageSize": 500})
            out.extend(r.get("data", []))
            links = r.get("links") or {}
            if not links.get("nextPage") or not r.get("data"):
                return out
            page += 1

    # ---- extracts --------------------------------------------------------
    def submit(self, collection: str, definition: dict) -> int:
        r = self._request("POST", "/extracts", {"collection": collection}, body=definition, retries=1)
        return int(r["number"])

    def info(self, collection: str, number: int) -> dict:
        return self._request("GET", f"/extracts/{number}", {"collection": collection})

    def wait(self, collection: str, number: int, max_wait_s: int = 4 * 3600,
             log=lambda m: print(m, file=sys.stderr, flush=True)) -> dict:
        """Poll until completed. Backoff 10s -> 60s. Raises on failed/canceled."""
        start, delay, last = time.time(), 10, None
        while True:
            info = self.info(collection, number)
            status = info.get("status")
            if status != last:
                log(f"[ipums] extract {collection}#{number}: {status} "
                    f"({int(time.time() - start)}s elapsed)")
                last = status
            if status == "completed":
                if info.get("downloadLinks"):
                    return info
                # an expired extract stays "completed" but loses its links: waiting will not bring them back
                raise IpumsError(f"Extract {collection}#{number} is complete but its files have expired (IPUMS "
                                 "deletes them about 72 hours after they are built). Submit it again: run "
                                 "fetch.py --source ipums with the spec (and --force).")
            if status in ("failed", "canceled"):
                errs = info.get("errors") or info.get("errorMessages") or ""
                raise IpumsError(f"Extract {collection}#{number} {status}. {errs}")
            if time.time() - start > max_wait_s:
                raise IpumsError(f"Timed out after {max_wait_s}s waiting for extract "
                                 f"{collection}#{number} (last status {status}). Resume later with "
                                 f"fetch.py --source ipums --resume {collection}:{number}")
            time.sleep(delay)
            delay = min(60, delay + 10)

    def download(self, url: str, dest: str, sha256: str | None = None) -> str:
        """Stream one extract file to dest (atomic), checking the SHA-256 IPUMS reports."""
        tmp = dest + ".part"
        h = hashlib.sha256()
        n = 0
        if os.environ.get("DATA_ANALYST_FIXTURES") or keys.restricted():
            try:                                  # offline tests and sandbox replay: through core.http
                r = self.http.request("GET", url, ok_statuses=(200,))
            except DataError as e:
                kind = {"network": "network", "auth": "auth", "key_missing": "auth"}.get(e.kind, "http")
                raise IpumsError(f"Download failed for {url}: {e}", kind=kind) from None
            with open(tmp, "wb") as f:
                f.write(r.body)
            h.update(r.body)
            n = len(r.body)
        else:
            req = urllib.request.Request(url, headers={"Authorization": self.key, "User-Agent": UA})
            try:
                with open_url(req, self.timeout) as resp, open(tmp, "wb") as f:   # no key past a redirect
                    while chunk := resp.read(1 << 20):
                        h.update(chunk)
                        f.write(chunk)
                        n += len(chunk)
            except urllib.error.HTTPError as e:
                raise IpumsError(f"Download failed for {url}: HTTP {e.code}",
                                 kind="auth" if e.code in (401, 403) else "http") from None
            except (urllib.error.URLError, OSError) as e:
                raise IpumsError(f"Download failed for {url}: {e}", kind="network") from None
            SOURCES.append({"method": "GET", "url": keys.redact(url), "status": 200, "bytes": n,
                            "sha256": h.hexdigest(), "cached": False,
                            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if sha256 and h.hexdigest() != sha256:
            os.remove(tmp)
            raise IpumsError(f"Checksum mismatch for {url}")
        os.replace(tmp, dest)
        return dest


if __name__ == "__main__":
    # quick auth check: python ipums_api.py usa   (needs SKILL/scripts on sys.path)
    coll = sys.argv[1] if len(sys.argv) > 1 else "usa"
    c = IpumsClient()
    s = c.list_samples(coll)
    print(f"{coll}: {len(s)} samples; latest: {[x['name'] for x in s[-5:]]}")


# ---------------------------------------------------------------------- key check (scripts/setup_key.py)
def verify(key: str) -> tuple[bool | None, str]:
    """Key check for the setup scripts: one cheap authenticated metadata call. (True, detail) when IPUMS
    accepts the key; a rejected key raises DataError(kind="auth") (setup_key counts it as rejected); an
    unreachable host raises kind "network". The key travels only in the Authorization header."""
    c = Client(BASE, name="IPUMS", auth=AUTH, key=key, retries=1, timeout=30, deadline=60)
    r = c.get("/metadata/samples", params={"collection": "usa", "version": "2", "pageSize": 1})
    data = r.json() if r.body.strip() else {}
    if isinstance(data, dict) and data.get("data") is not None:
        return True, "IPUMS accepted the key (metadata call answered). Register for each collection you use (usa, cps, ...)."
    return None, "IPUMS answered, but not with the expected metadata; the key could not be confirmed."

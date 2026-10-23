"""One HTTP client for every source pack (standard library only).

    from core.http import Client, DataError
    c = Client("https://api.example.gov/v1", name="example", auth={"env": "EXAMPLE_KEY", "placement": "query",
               "param": "api_key"}, rps=5, burst=10)
    data = c.get_json("/series", params={"id": "X"})

What it does for every pack (merged from the old
*-analyst clients):
- auth: the key goes where policy.json says (query parameter, header with optional prefix, or JSON body
  field); it is read at call time with core.keys.find_key and never appears in a message, a recorded URL
  or a cache key. A 401/403 without a key says which variable to set.
- a token bucket per host (rps, burst), an optional per-minute error budget (BEA locks a key out after
  30 errors a minute) and byte budget (BEA's 100 MB a minute).
- retries on 429/500/502/503/504 and network errors with jittered exponential backoff; Retry-After is
  honoured up to MAX_RETRY_AFTER seconds, beyond that the call fails at once (kind "rate").
- a per-attempt timeout and an overall deadline across retries.
- check_error(status, body_bytes) -> message | None catches APIs that answer HTTP 200 with an error.
- every request is recorded (credential-free URL, method, status, time, bytes, sha256) in SOURCES, which
  package.py writes into manifest.json: the audit trail of where each file came from.
- DATA_ANALYST_FIXTURES=<dir> serves responses from <dir>/index.json instead of the network (offline
  tests); DATA_ACCESS_FORCE=restricted makes every request a network error (sandbox replay).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import keys

UA = "data-analyst/1.0 (Python urllib)"            # no URL: SEC refuses User-Agents that contain one
CREDENTIAL_HEADERS = ("authorization", "x-api-key", "cookie")


def error_snippet(body: bytes, limit: int = 400) -> str:
    """A short, redacted excerpt of an error body for messages. An HTML error page (a CDN's "website is busy"
    page, a gateway error) is reduced to its <title>, not 400 characters of markup and scripts."""
    text = keys.redact(body[:20000].decode("utf-8", "replace")).strip()
    head = text[:200].lstrip().lower()
    if head.startswith(("<!doctype html", "<html")) or "<html" in head:
        import re
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        title = " ".join(m.group(1).split()) if m else ""
        return f"HTML page: {title[:200]}" if title else "HTML error page (no title)"
    return text[:limit]


class _CredentialSafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry credentials to another host or from https to http: urllib's own
    handler copies every request header (X-Api-Key, Authorization) to wherever Location points."""

    def __init__(self, secret_headers=(), secret_params=()):
        self.secret_headers = {h.lower() for h in (*CREDENTIAL_HEADERS, *secret_headers) if h}
        self.secret_params = {p.lower() for p in secret_params if p}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old, to = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
        if old.netloc.lower() != to.netloc.lower() or (old.scheme == "https" and to.scheme != "https"):
            for k in list(new.headers):
                if k.lower() in self.secret_headers:
                    del new.headers[k]
            if self.secret_params and to.query:
                q = [(k, v) for k, v in urllib.parse.parse_qsl(to.query, keep_blank_values=True)
                     if k.lower() not in self.secret_params]
                new.full_url = urllib.parse.urlunsplit(to._replace(query=urllib.parse.urlencode(q)))
        return new


def open_url(req: urllib.request.Request, timeout: float, secret_headers=(), secret_params=()):
    """urllib.request.urlopen with credential-safe redirects (see _CredentialSafeRedirect)."""
    opener = urllib.request.build_opener(_CredentialSafeRedirect(secret_headers, secret_params))
    return opener.open(req, timeout=timeout)
RETRYABLE = (429, 500, 502, 503, 504)
MAX_RETRY_AFTER = 20.0
SOURCES: list[dict] = []
_BUCKETS: dict[str, "TokenBucket"] = {}
_LOCK = threading.Lock()


class DataError(RuntimeError):
    """An error whose message is always scrubbed of keys. kind tells the caller which recovery applies:
      key_missing  no key in any location        auth        the API rejected the key
      network      host unreachable (offline, proxy, sandbox, allow-list)
      not_found    the series / endpoint / record does not exist
      rate         429 after retries, or Retry-After too long, or the pack's quota is spent
      rate_limit   the pack's own error or byte budget is spent (stop before the API locks the key out)
      bad_request  the API refused a parameter (the message says which)
      api          an error reported inside an HTTP 200 body
      http         any other HTTP status       fixture_missing  offline test without a recorded response
    """

    def __init__(self, msg: str, kind: str = "http", status: int | None = None):
        super().__init__(keys.redact(msg))
        self.kind = kind
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    url: str                      # credential-free
    cached: bool = False

    def json(self):
        return json.loads(self.body.decode("utf-8-sig"))

    def text(self) -> str:
        return self.body.decode("utf-8-sig", errors="replace")


class TokenBucket:
    def __init__(self, rps: float, burst: int):
        self.rate, self.cap = max(rps, 0.01), max(burst, 1)
        self.tokens, self.t = float(self.cap), time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                time.sleep((1 - self.tokens) / self.rate)


@dataclass
class Budget:
    """Per-minute budget (errors or bytes). spend() raises once the limit would be crossed."""
    limit: float | None
    what: str
    events: list = field(default_factory=list)

    def spend(self, n: float = 1):
        if not self.limit:
            return
        now = time.monotonic()
        self.events = [(t, v) for t, v in self.events if now - t < 60]
        if sum(v for _, v in self.events) + n > self.limit:
            raise DataError(f"stopped before the source's limit of {self.limit:g} {self.what} a minute was crossed "
                            "(the API locks keys out above it); wait a minute and run again", kind="rate_limit")
        self.events.append((now, n))


def clean_url(url: str, secret_params: tuple[str, ...] = ()) -> str:
    """URL without credentials: secret query parameters dropped, known keys redacted."""
    parts = urllib.parse.urlsplit(url)
    drop = {p.lower() for p in secret_params}
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in drop]
    return keys.redact(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q))))


def _norm(u: str):
    p = urllib.parse.urlsplit(u)
    return p._replace(query="").geturl(), sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True))


def _fixture(method: str, url: str, body: bytes | None) -> Response:
    d = os.environ["DATA_ANALYST_FIXTURES"]
    with open(os.path.join(d, "index.json"), encoding="utf-8") as f:
        idx = json.load(f)
    key = f"{method} {url}"
    hit = idx.get(key)
    if hit is None and body:
        hit = idx.get(f"{key} {hashlib.sha256(body).hexdigest()[:12]}")
    if hit is None:                                   # same URL with the query in another order
        for k, v in idx.items():
            m, _, u = k.partition(" ")
            if m == method and _norm(u.split(" ")[0]) == _norm(url):
                hit = v
                break
    if hit is None:
        raise DataError(f"no fixture for {key}", kind="fixture_missing")
    if isinstance(hit, str):
        hit = {"file": hit}
    if hit.get("file"):
        with open(os.path.join(d, hit["file"]), "rb") as f:
            data = f.read()
    else:
        data = str(hit.get("body", "")).encode()
    return Response(hit.get("status", 200), {k.lower(): v for k, v in (hit.get("headers") or {}).items()}, data, url)


class Client:
    def __init__(self, base_url: str = "", *, name: str, auth: dict | None = None, key: str | None = None,
                 rps: float = 5, burst: int = 10, retries: int = 2, timeout: float = 30, deadline: float = 180,
                 check_error=None, headers: dict | None = None, user_agent: str = UA,
                 errors_per_min: float | None = None, bytes_per_min: float | None = None):
        self.base = base_url.rstrip("/")
        self.name, self.auth = name, dict(auth or {})
        self.retries, self.timeout, self.deadline = retries, timeout, deadline
        self.check_error, self.headers, self.ua = check_error, dict(headers or {}), user_agent
        self.error_budget = Budget(errors_per_min, "errors")
        self.byte_budget = Budget(bytes_per_min, "bytes")
        host = urllib.parse.urlsplit(self.base or "http://x").netloc or name
        with _LOCK:
            self.bucket = _BUCKETS.setdefault(host, TokenBucket(rps or 5, burst or 10))
        if key:
            keys.remember(key)
            self.key, self.key_source = key, "argument"
        else:
            self.key, self.key_source = keys.find_auth_key(self.auth)

    def __repr__(self):                               # never show the key in tracebacks
        return f"Client({self.name!r}, key=[REDACTED])"

    # ------------------------------------------------------------------ building a request
    def _url(self, path: str, params: dict | None) -> str:
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = f"{self.base}/{path.lstrip('/')}" if path else self.base
        q = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        if q:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q, doseq=True)
        return url

    def _with_key(self, url: str, headers: dict, body: dict | None):
        a, k = self.auth, self.key
        # extra credentials some APIs pair with the key, e.g. EPA AQS {"email": "AQS_EMAIL"}
        for param, env in (a.get("extra") or {}).items():
            v, _ = keys.find_key(env)
            if v:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode({param: v})
        if not k and a.get("demo_key"):               # api.data.gov's DEMO_KEY: keyless, low limits
            k = a["demo_key"]
        if not k or a.get("placement") in (None, "none"):
            return url, headers, body
        if a["placement"] == "query":
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({a.get("param") or "api_key": k})
        elif a["placement"] == "header":
            headers[a.get("param") or "X-Api-Key"] = (a.get("prefix") or "") + k
        elif a["placement"] == "body" and body is not None:
            body = {**body, a.get("param") or "registrationkey": k}
        return url, headers, body

    def _open(self, req: urllib.request.Request):
        """Open with redirects that drop this client's credentials when they leave the host."""
        a = self.auth
        hdr = (a.get("param") or "X-Api-Key") if a.get("placement") == "header" else None
        par = ((a.get("param") or "api_key") if a.get("placement") == "query" else None, *(a.get("extra") or {}))
        return open_url(req, self.timeout, (hdr,) if hdr else (), tuple(p for p in par if p))

    # ------------------------------------------------------------------ the call
    def request(self, method: str, path: str = "", params: dict | None = None, json_body: dict | None = None,
                headers: dict | None = None, use_key: bool = True, ok_statuses=(200,), allow_404: bool = False) -> Response:
        url = self._url(path, params)
        secret = tuple(p for p in ((self.auth.get("param") if self.auth.get("placement") == "query" else None),
                                   *(self.auth.get("extra") or {})) if p)
        shown = clean_url(url, secret)
        hdrs = {"User-Agent": self.ua, "Accept": "*/*", **self.headers, **(headers or {})}
        body = json_body
        if use_key:
            url, hdrs, body = self._with_key(url, hdrs, body)
        data = json.dumps(body).encode() if body is not None else None
        self._rec_body = json.dumps(json_body, sort_keys=True).encode() if json_body is not None else None
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        if os.environ.get("DATA_ANALYST_FIXTURES"):
            r = _fixture(method, shown, self._rec_body)
            return self._finish(r, method, ok_statuses, allow_404)
        if keys.restricted():
            raise DataError(f"{self.name}: DATA_ACCESS_FORCE=restricted: network access is switched off", kind="network")
        start, last = time.monotonic(), ""
        for attempt in range(self.retries + 1):
            if time.monotonic() - start > self.deadline:
                break
            self.bucket.take()
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with self._open(req) as resp:
                    r = Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(), shown)
                self.byte_budget.spend(len(r.body))
                return self._finish(r, method, ok_statuses, allow_404)
            except urllib.error.HTTPError as e:
                r = Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"", shown)
                if e.code in RETRYABLE and attempt < self.retries:
                    wait = self._retry_after(r.headers.get("retry-after"))
                    if wait is not None and wait > MAX_RETRY_AFTER:
                        self._record(r, method)
                        raise DataError(f"{self.name}: HTTP {e.code}, Retry-After {wait:.0f}s exceeds "
                                        f"{MAX_RETRY_AFTER:.0f}s; wait and run again", kind="rate", status=e.code)
                    time.sleep(wait if wait is not None else self._backoff(attempt))
                    continue
                return self._finish(r, method, ok_statuses, allow_404)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                # HTTPException: a connection cut mid-body (IncompleteRead, RemoteDisconnected) is a network error
                last = str(getattr(e, "reason", e)) or type(e).__name__
                if attempt < self.retries:
                    time.sleep(self._backoff(attempt))
                    continue
        raise DataError(f"{method} {shown.split('?')[0]}: network error: {last or 'deadline exceeded'}", kind="network")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 2.0 ** attempt * (0.5 + random.random() * 0.5)

    @staticmethod
    def _retry_after(v) -> float | None:
        if not v:
            return None
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                return max(0.0, (parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return None

    def _record(self, r: Response, method: str, body: bytes | None = None):
        SOURCES.append({"method": method, "url": r.url, "status": r.status, "bytes": len(r.body),
                        "sha256": hashlib.sha256(r.body).hexdigest(), "cached": r.cached,
                        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        rec = os.environ.get("DATA_ANALYST_RECORD")
        if rec and not os.environ.get("DATA_ANALYST_FIXTURES"):
            record_fixture(rec, method, r, body)

    def _finish(self, r: Response, method: str, ok_statuses, allow_404: bool) -> Response:
        self._record(r, method, getattr(self, "_rec_body", None))
        snippet = error_snippet(r.body)
        if r.status in ok_statuses or (allow_404 and r.status == 404):
            msg = self.check_error(r.status, r.body) if self.check_error else None
            if msg:
                self.error_budget.spend()
                raise DataError(f"{self.name}: {msg}", kind="api", status=r.status)
            return r
        self.error_budget.spend()
        if r.status in (401, 403):
            if not self.key and self.auth.get("env"):
                raise DataError(f"{self.name}: API key required (HTTP {r.status}). Set the {self.auth['env']} "
                                "environment variable or run the setup script.", kind="key_missing", status=r.status)
            raise DataError(f"{self.name}: HTTP {r.status} (key rejected or access denied): {snippet}", kind="auth",
                            status=r.status)
        if r.status == 404:
            raise DataError(f"{self.name}: not found (HTTP 404) {r.url.split('?')[0]}", kind="not_found", status=404)
        if r.status == 429:
            raise DataError(f"{self.name}: rate limited (HTTP 429) after retries", kind="rate", status=429)
        if 400 <= r.status < 500:
            raise DataError(f"{self.name}: HTTP {r.status}: {snippet}", kind="bad_request", status=r.status)
        if r.status in RETRYABLE:
            raise DataError(f"{self.name}: HTTP {r.status} after {self.retries} retries (the server is busy or down; "
                            f"wait a few minutes and run again): {snippet}", kind="http", status=r.status)
        raise DataError(f"{self.name}: HTTP {r.status}: {snippet}", kind="http", status=r.status)

    # ------------------------------------------------------------------ helpers
    def download(self, path: str, dest: str, params: dict | None = None, sha256: str | None = None,
                 chunk: int = 1 << 20) -> dict:
        """Stream a large file to dest (atomic .part rename), with the same auth, throttle, restricted switch,
        fixtures and SOURCES record as request(). Raises DataError on a checksum mismatch."""
        url = self._url(path, params)
        secret = tuple(p for p in ((self.auth.get("param") if self.auth.get("placement") == "query" else None),
                                   *(self.auth.get("extra") or {})) if p)
        shown = clean_url(url, secret)
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        part = dest + ".part"
        h = hashlib.sha256()
        if os.environ.get("DATA_ANALYST_FIXTURES"):
            r = _fixture("GET", shown, None)
            self._finish(r, "GET", (200,), False)
            with open(part, "wb") as f:
                f.write(r.body)
            h.update(r.body)
            n = len(r.body)
        else:
            if keys.restricted():
                raise DataError(f"{self.name}: DATA_ACCESS_FORCE=restricted: network access is switched off", kind="network")
            hdrs = {"User-Agent": self.ua, **self.headers}
            url, hdrs, _ = self._with_key(url, hdrs, None)
            self.bucket.take()
            n, status = 0, 0
            try:
                with self._open(urllib.request.Request(url, headers=hdrs)) as resp,                         open(part, "wb") as f:
                    status = resp.status
                    for b in iter(lambda: resp.read(chunk), b""):
                        f.write(b)
                        h.update(b)
                        n += len(b)
                        self.byte_budget.spend(len(b))
            except urllib.error.HTTPError as e:
                self._finish(Response(e.code, {}, e.read() or b"", shown), "GET", (200,), False)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                if os.path.exists(part):
                    os.remove(part)                      # never leave a truncated file behind
                raise DataError(f"GET {shown.split('?')[0]}: network error: {getattr(e, 'reason', e)}", kind="network")
            SOURCES.append({"method": "GET", "url": shown, "status": status, "bytes": n, "sha256": h.hexdigest(),
                            "cached": False, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if sha256 and h.hexdigest().lower() != sha256.lower():
            os.remove(part)
            raise DataError(f"{self.name}: checksum mismatch for {os.path.basename(dest)}", kind="http")
        os.replace(part, dest)
        return {"path": dest, "bytes": n, "sha256": h.hexdigest(), "url": shown}

    def get(self, path: str = "", params: dict | None = None, **kw) -> Response:
        return self.request("GET", path, params, **kw)

    def get_json(self, path: str = "", params: dict | None = None, **kw):
        return self.get(path, params, **kw).json()

    def post_json(self, path: str = "", body: dict | None = None, params: dict | None = None, **kw):
        return self.request("POST", path, params, json_body=body or {}, **kw).json()


MAX_FIXTURE = 200_000


def record_fixture(d: str, method: str, r: Response, body: bytes | None) -> None:
    """DATA_ANALYST_RECORD=<dir>: save each live response as a fixture (credential-free URL as the key).
    Bodies over MAX_FIXTURE bytes are skipped: trim the request instead. Refuses to save anything that
    still contains a known key."""
    if len(r.body) > MAX_FIXTURE or any(k.encode() in r.body for k in keys.secret_values()):
        return
    os.makedirs(d, exist_ok=True)
    ip = os.path.join(d, "index.json")
    idx = {}
    if os.path.exists(ip):
        with open(ip, encoding="utf-8") as f:
            idx = json.load(f)
    key = f"{method} {r.url}" + (f" {hashlib.sha256(body).hexdigest()[:12]}" if body else "")
    ctype = r.headers.get("content-type", "")
    head = r.body[:1]
    ext = ("json" if "json" in ctype or head in (b"{", b"[") else "csv" if "csv" in ctype else
           "xml" if "xml" in ctype or head == b"<" else "bin")
    prev = idx.get(key)
    if prev:                                     # re-recording a key reuses its file
        name = prev if isinstance(prev, str) else prev["file"]
    else:
        nums = [int(f[1:4]) for f in os.listdir(d) if f[:1] == "r" and f[1:4].isdigit()]
        name = f"r{max(nums, default=0) + 1:03d}.{ext}"
    with open(os.path.join(d, name), "wb") as f:
        f.write(r.body)
    idx[key] = {"file": name, "status": r.status} if r.status != 200 else name
    with open(ip, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=1)


def reachable(url: str, timeout: float = 12) -> tuple[bool, str]:
    """Does the host answer at all? Any HTTP status counts (a 403 bot block is reachable, with a note).
    Network errors, a proxy refusing the host, and DATA_ACCESS_FORCE=restricted count as unreachable."""
    if os.environ.get("DATA_ANALYST_FIXTURES"):
        return True, ""
    if keys.restricted():
        return False, "DATA_ACCESS_FORCE=restricted"
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True, ""
    except urllib.error.HTTPError as e:
        why = " (the host answers; this probe sends no key, so a key check is separate)" if e.code in (401, 403) else ""
        return True, f"HTTP {e.code}{why}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, keys.redact(str(getattr(e, "reason", e)))[:200]

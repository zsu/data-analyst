"""Key check for setup.ps1 / setup.sh (scripts/setup_key.py):
GET series?series_id=GNPCA with the key; HTTP 200 = accepted, HTTP 400 whose body names api_key = rejected.

verify(key) -> (True | False | None, detail). Network errors raise core.http.DataError(kind="network").
The key is never printed (core.http.Client drops api_key from every recorded URL; details are redacted).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from fred_api import API, AUTH_V1, RPS, UA, redact  # noqa: E402
from core.http import Client  # noqa: E402


def verify(key: str) -> tuple[bool | None, str]:
    c = Client(API, name="fred-keycheck", auth=AUTH_V1, key=key, rps=RPS, retries=1, timeout=30, user_agent=UA)
    r = c.get("series", {"series_id": "GNPCA", "file_type": "json"}, ok_statuses=(200, 400, 401, 403))
    body = r.body.decode("utf-8", "replace").lower()
    if r.status == 200 and body.lstrip()[:1] == "<":
        return None, "FRED answered with an HTML page (maintenance?): the key could not be checked; try again later."
    if r.status == 200:
        return True, "FRED accepted the key."
    if r.status == 400 and "api_key" in body:
        return False, redact(f"FRED rejected the key (HTTP 400): {body[:200]}")
    return None, redact(f"FRED answered HTTP {r.status}; the key may still be valid: {body[:200]}")

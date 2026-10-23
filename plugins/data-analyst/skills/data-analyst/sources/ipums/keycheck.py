"""Key check for the setup scripts (scripts/setup_key.py): one cheap authenticated IPUMS metadata call.

verify(key) -> (True, detail) when IPUMS accepts the key. A rejected key raises core.http.DataError with
kind "auth" (setup_key counts it as rejected); an unreachable host raises kind "network". The key is
never printed: it travels only in the Authorization header, and messages pass through core.keys.redact.
"""
from __future__ import annotations

from core.http import Client

BASE = "https://api.ipums.org"
AUTH = {"env": "IPUMS_API_KEY", "home": "ipums", "placement": "header", "param": "Authorization", "prefix": ""}


def verify(key: str) -> tuple[bool | None, str]:
    c = Client(BASE, name="IPUMS", auth=AUTH, key=key, retries=1, timeout=30, deadline=60)
    r = c.get("/metadata/samples", params={"collection": "usa", "version": "2", "pageSize": 1})
    data = r.json() if r.body.strip() else {}
    if isinstance(data, dict) and data.get("data") is not None:
        return True, "IPUMS accepted the key (metadata call answered). Register for each collection you use (usa, cps, ...)."
    return None, "IPUMS answered, but not with the expected metadata; the key could not be confirmed."

"""Key check for the skill's setup scripts (scripts/setup_key.py): one GetDataSetList call with the key.

An Error in the reply (codes 1, 3, 4: invalid, unknown
or not yet activated) is a rejection; HTTP 429 or another failure is "unclear"; no connection raises a
network DataError so setup_key reports a network problem. The key is never printed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bea_api import BeaClient, BeaError  # noqa: E402
from core.http import DataError  # noqa: E402


def verify(key) -> tuple[bool | None, str]:
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

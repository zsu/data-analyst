"""Key check for setup.ps1 / setup.sh (scripts/setup_key.py): one small v2 query with the key in the POST body.

verify(key) -> (True, detail) accepted, (False, detail) rejected (wrong or expired: BLS keys expire after a
year), (None, detail) unclear (daily limit, BLS refused for another reason). Network errors raise
core.http.DataError(kind="network") so the setup script says "could not reach api.bls.gov". The key is never
printed: BLS echoes a rejected key in its message, and every detail goes through core.keys.redact.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from bls_api import BlsClient, redact  # noqa: E402
from core.http import DataError  # noqa: E402


def verify(key: str) -> tuple[bool | None, str]:
    p = BlsClient(api_key=key).ping()          # one query of the key's 500 a day
    if p["kind"] == "network":
        raise DataError(f"api.bls.gov could not be reached: {p['detail']}", kind="network")
    if p["auth_ok"]:
        return True, "BLS accepted the key (API v2)."
    if p["auth_ok"] is False:
        return False, redact(f"BLS rejected the key (wrong, or expired: renew at "
                             f"https://data.bls.gov/registrationEngine/): {p['detail']}")
    return None, redact(f"BLS did not confirm the key ({p['kind']}): {p['detail']}")

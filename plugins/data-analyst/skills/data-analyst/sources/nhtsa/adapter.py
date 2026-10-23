"""NHTSA adapter: date columns in a sortable form, vPIC (second host) manual links, 400-with-Count-0 hint.

Recalls publish ReportReceivedDate as DD/MM/YYYY ("28/05/2020"), complaints publish dateOfIncident and
dateComplaintFiled as MM/DD/YYYY ("07/24/2025"). The published columns are kept; an *_iso column is added.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402

DMY = ("ReportReceivedDate",)
MDY = ("dateOfIncident", "dateComplaintFiled")


def _iso(v: str, order: str) -> str:
    parts = str(v or "").strip().split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    a, b, y = (int(p) for p in parts)
    d, m = (a, b) if order == "dmy" else (b, a)
    if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 < y < 2200):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def normalize(req, ep, rows):
    for r in rows:
        for col in DMY:
            if col in r:
                r[col + "_iso"] = _iso(r[col], "dmy")
        for col in MDY:
            if col in r:
                r[col + "_iso"] = _iso(r[col], "mdy")
    return rows


def manual_items(spec, pack):
    """Generic links, except that vPIC endpoints have an absolute path on another host."""
    items = gr.manual_items(pack, spec)
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    for it in items:
        for scheme in ("https://", "http://"):
            bad = f"{base}/{scheme}"
            if it["url"].startswith(bad):
                it["url"] = scheme + it["url"][len(bad):]
    return items


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        vin = p.get("vin")
        if r.get("endpoint") == "vin_decode" and vin and len(str(vin)) > 17:
            probs.append(f"request {r.get('name')!r}: a VIN has at most 17 characters")
        y = p.get("modelYear")
        if y not in (None, "") and not str(y).isdigit():
            probs.append(f"request {r.get('name')!r}: modelYear must be a single year such as 2019 "
                         "(one request per year for a range)")
    return probs

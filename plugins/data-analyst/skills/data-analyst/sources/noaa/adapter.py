"""NOAA NCEI adapter: keyless Access Data Service first, CDO v2 (token) only where it is needed.

- before_request: ADS station ids without the CDO "GHCND:" prefix; YYYY / YYYY-MM dates padded to full dates
  (ADS refuses them, even for monthly data).
- fetch: the generic fetch, except that cdo_* requests without NOAA_API_KEY stop as key_missing (CDO
  answers a bare HTTP 400 "Token parameter is required").
- normalize: ADS values are padded text ("   83"); they are trimmed (the digits are unchanged). The
  station search returns nested documents; they become one row per station. A data request none of
  whose data types came back fails instead of returning only DATE and STATION.
- key_only_needs: the cdo_* endpoints need NOAA_API_KEY (sent as the "token" header).
"""
from __future__ import annotations

import calendar
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core.http import DataError  # noqa: E402

ADS_DATA = "/access/services/data/"
ADS_SEARCH = "/access/services/search/"


def _full_date(v, end: bool):
    """YYYY or YYYY-MM -> YYYY-MM-DD (first day for a start, last day for an end); anything else unchanged."""
    s = str(v).strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", s)
    if not m:
        return v
    y, mo = int(m.group(1)), m.group(2)
    if mo is None:
        return f"{y:04d}-12-31" if end else f"{y:04d}-01-01"
    mo = int(mo)
    if not 1 <= mo <= 12:
        return v
    return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}" if end else f"{y:04d}-{mo:02d}-01"


def before_request(req, ep, params):
    path = ep.get("path", "")
    if path.startswith((ADS_DATA, ADS_SEARCH)) and params.get("stations"):
        ids = [s.strip() for s in str(params["stations"]).split(",") if s.strip()]
        params["stations"] = ",".join(s.split(":", 1)[1] if ":" in s else s for s in ids)
    if path.startswith(ADS_DATA):
        for k, end in (("startDate", False), ("endDate", True)):
            if params.get(k) not in (None, ""):
                params[k] = _full_date(params[k], end)
    return params


def fetch(spec, pack, route):
    """The generic fetch, after one check: cdo_* requests without NOAA_API_KEY stop as key_missing."""
    from core import generic_rest as gr
    c = gr.client_for(pack)
    cdo = [r.get("name") for r in spec.get("requests") or [] if str(r.get("endpoint", "")).startswith("cdo_")]
    if cdo and not c.key:
        raise DataError(f"request(s) {', '.join(map(repr, cdo))}: the Climate Data Online v2 endpoints (cdo_*) need a "
                        "free NOAA token: set NOAA_API_KEY (https://www.ncei.noaa.gov/cdo-web/token), or use the "
                        "keyless data / station_search endpoints", kind="key_missing")
    return gr.fetch(pack, spec, c)


def _station_rows(rows):
    out = []
    for r in rows:
        try:
            st = json.loads(r.get("stations") or "[]")
        except ValueError:
            st = []
        try:
            dts = json.loads(r.get("dataTypes") or "[]")
        except ValueError:
            dts = []
        coords = str(r.get("location.coordinates") or "").split(";")
        s0 = st[0] if st else {}
        out.append({"station_id": s0.get("id", ""), "name": s0.get("name", ""),
                    "longitude": coords[0].strip() if len(coords) == 2 else "",
                    "latitude": coords[1].strip() if len(coords) == 2 else "",
                    "start_date": r.get("startDate", ""), "end_date": r.get("endDate", ""),
                    "data_types": " ".join(sorted(d.get("id", "") for d in dts if isinstance(d, dict))),
                    "data_types_count": r.get("dataTypesCount", ""), "file_path": r.get("filePath", "")})
    return out


def normalize(req, ep, rows):
    path = ep.get("path", "")
    if path.startswith("/access/services/search/"):
        return _station_rows(rows)
    if path.startswith(ADS_DATA):
        rows = [{k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in rows]
        asked = [t.strip() for t in str((req.get("params") or {}).get("dataTypes") or "").split(",") if t.strip()]
        cols = {k.upper() for r in rows for k in r}
        if rows and asked and not any(t.upper() in cols for t in asked):
            raise DataError(f"request {req.get('name')!r}: none of the data types {', '.join(asked)} came back (ADS "
                            "returns only DATE and STATION for a misspelled or wrongly cased type, or one the station "
                            "did not record in that period). Check the ids (case-sensitive: TMAX, PRCP ...; normals "
                            "MLY-TAVG-NORMAL ...) and the "
                            "station's data_types with station_search.", kind="bad_request")
        return rows
    return rows


def key_only_needs(spec):
    needs = sorted({r.get("endpoint") for r in spec.get("requests") or [] if str(r.get("endpoint", "")).startswith("cdo_")})
    return [f"{n} (Climate Data Online v2 needs a free NOAA token)" for n in needs]


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        if r.get("endpoint") == "cdo_data" and p.get("startdate") and p.get("enddate"):
            try:
                span = int(str(p["enddate"])[:4]) - int(str(p["startdate"])[:4])
            except ValueError:
                continue
            ds = str(p.get("datasetid", "")).upper()
            if span > (10 if ds in ("GSOM", "GSOY") else 1):
                probs.append(f"request {r.get('name')!r}: CDO limits a request to 1 year ({ds or 'daily'}) or 10 years "
                             "(GSOM/GSOY); split it, or use the keyless data endpoint with stations")
    return probs


def ping(client):
    """Check the CDO token (the keyless ADS endpoints never need it). Without a token, say so: CDO would
    answer HTTP 400, which reads as a rejected key."""
    if not getattr(client, "key", None):
        return {"auth_ok": False, "kind": "key_missing",
                "detail": "no NOAA_API_KEY found: the keyless data and station_search endpoints work; only the "
                          "cdo_* endpoints need the token"}
    try:
        client.get("/cdo-web/api/v2/datasets", {"limit": 1})
        return {"auth_ok": True, "kind": "", "detail": "CDO v2 accepted the token"}
    except DataError as e:
        kind = "auth" if e.kind == "bad_request" else e.kind     # CDO answers 400 for a missing or bad token
        return {"auth_ok": kind not in ("auth", "key_missing"), "kind": kind, "detail": str(e)}

"""NIST NVD CVE API 2.0 adapter.

- Flags (hasKev, keywordExactMatch, noRejected, isVulnerable) take no value: endpoints.json marks them
  "flag": true, so the core sends a bare ?hasKev for true and nothing for false. "hasKev=true" is refused by the
  API with HTTP 404 (verified 2026-10-30).
- Each date pair (published, last modified, added to KEV) needs both ends and covers at most 120 days; the API
  refuses anything else with HTTP 404 and the reason only in a response header, so check_spec says it first.
  A plain YYYY-MM-DD is widened to the start or the end of that day (the API wants full ISO 8601 date-times).
- Severities are sent upper case.
- Records are nested (cve.metrics.cvssMetricV31[...].cvssData.baseScore ...). normalize() writes one flat row per
  CVE: NVD's own (Primary) CVSS v3.x score where there is one, else the first listed (its source column says
  whose it is), the v4 and v2 scores, CWE ids, KEV dates and the English description. Values are as published.
- cve_count asks for one record and keeps only totalResults.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import packfiles  # noqa: E402


def _enums() -> dict:
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("enums", {})


def _when(v, end: bool):
    """datetime of a spec date, or None when it cannot be read."""
    s = str(v).strip().replace("Z", "")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            d = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end:
            d = d.replace(hour=23, minute=59, second=59, microsecond=999000)
        return d
    return None


def _iso(v, end: bool) -> str:
    s = str(v).strip()
    if len(s) == 10:
        return _when(s, end).strftime("%Y-%m-%dT%H:%M:%S.") + ("999" if end else "000")
    return s


def check_spec(spec):
    probs = []
    en = _enums()
    for r in spec.get("requests") or []:
        name = r.get("name")
        prm = r.get("params") or {}
        for a, b in en["date_pairs"]:
            if (a in prm) != (b in prm):
                probs.append(f"request {name!r}: {a} and {b} go together (give both ends)")
                continue
            if a not in prm:
                continue
            s, e = _when(prm[a], False), _when(prm[b], True)
            if s is None or e is None:
                probs.append(f"request {name!r}: {a}/{b} must be YYYY-MM-DD or ISO 8601 date-times")
            elif e < s:
                probs.append(f"request {name!r}: {b} is before {a}")
            elif (e - s).total_seconds() > en["max_range_days"] * 86400:
                probs.append(f"request {name!r}: {a} to {b} spans {(e - s).days + 1} days; the API allows at most "
                             f"{en['max_range_days']}. Split it into several requests (a calendar year is four: "
                             "Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec) and add them up.")
        for k, allowed in (("cvssV3Severity", en["severity_v3"]), ("cvssV4Severity", en["severity_v3"]),
                           ("cvssV2Severity", en["severity_v2"])):
            if k in prm and str(prm[k]).upper() not in allowed:
                probs.append(f"request {name!r}: {k} {prm[k]!r} is not one of {allowed}")
        if not any(k in prm for k in ("cveId", "keywordSearch", "cpeName", "virtualMatchString", "cweId",
                                      "pubStartDate", "lastModStartDate", "kevStartDate", "hasKev",
                                      "sourceIdentifier", "cvssV3Severity", "cvssV4Severity", "cvssV2Severity")):
            probs.append(f"request {name!r}: give a filter (cveId, keywordSearch, cpeName, cweId, a date range, "
                         "hasKev ...); the database holds 300,000+ CVEs")
    return probs


def before_request(req, ep, params):
    p = dict(params)
    en = _enums()
    for a, b in en["date_pairs"]:
        if a in p:
            p[a] = _iso(p[a], False)
        if b in p:
            p[b] = _iso(p[b], True)
    for k in ("cvssV3Severity", "cvssV4Severity", "cvssV2Severity"):
        if k in p:
            p[k] = str(p[k]).upper()
    return p


def request_note(req, ep, params):
    if str(params.get("noRejected")).strip().lower() not in ("true", "1", "yes"):
        return f"{req['name']}: REJECTED CVEs are included (noRejected was set to false)."
    return None


def _metric(metrics: dict, keys: tuple) -> dict:
    for k in keys:
        lst = metrics.get(k) or []
        if lst:
            prim = [m for m in lst if m.get("type") == "Primary"]
            return (prim or lst)[0]
    return {}


def _row(cve: dict) -> dict:
    m = cve.get("metrics") or {}
    v3, v4, v2 = (_metric(m, ("cvssMetricV31", "cvssMetricV30")), _metric(m, ("cvssMetricV40",)),
                  _metric(m, ("cvssMetricV2",)))
    d3, d4, d2 = v3.get("cvssData") or {}, v4.get("cvssData") or {}, v2.get("cvssData") or {}
    cwes = []
    for w in cve.get("weaknesses") or []:
        for x in w.get("description") or []:
            if x.get("value") and x["value"] not in cwes:
                cwes.append(x["value"])
    desc = next((x.get("value") for x in cve.get("descriptions") or [] if x.get("lang") == "en"), "")
    return {
        "cve_id": cve.get("id"),
        "published": cve.get("published"),
        "last_modified": cve.get("lastModified"),
        "vuln_status": cve.get("vulnStatus"),
        "source_identifier": cve.get("sourceIdentifier"),
        "cvss_v3_score": d3.get("baseScore"),
        "cvss_v3_severity": d3.get("baseSeverity"),
        "cvss_v3_version": d3.get("version"),
        "cvss_v3_vector": d3.get("vectorString"),
        "cvss_v3_source": v3.get("source"),
        "cvss_v3_type": v3.get("type"),
        "cvss_v4_score": d4.get("baseScore"),
        "cvss_v4_severity": d4.get("baseSeverity"),
        "cvss_v4_source": v4.get("source"),
        "cvss_v2_score": d2.get("baseScore"),
        "cvss_v2_severity": v2.get("baseSeverity"),
        "cwe_ids": "; ".join(cwes),
        "kev_date_added": cve.get("cisaExploitAdd"),
        "kev_action_due": cve.get("cisaActionDue"),
        "kev_name": cve.get("cisaVulnerabilityName"),
        "reference_count": len(cve.get("references") or []),
        "description": desc,
    }


def normalize(req, ep, rows):
    if ep.get("records") == "":                     # cve_count: the response body itself
        out = []
        for r in rows:
            if isinstance(r, str):
                r = json.loads(r)
            out.append({"total_results": r.get("totalResults"), "timestamp": r.get("timestamp")})
        return out
    out = []
    for r in rows:
        cve = r.get("cve") if isinstance(r, dict) else None
        out.append({k: ("" if v is None else v) for k, v in _row(cve or {}).items()})
    return out

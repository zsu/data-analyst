"""EPA AQS Data Mart adapter (aqs.epa.gov/data/api).

- Credentials: AQS needs two query parameters, key (AQS_API_KEY) and email (AQS_EMAIL); policy.json maps
  both (auth.param and auth.extra) so core.http adds them and keeps them out of recorded URLs.
- AQS answers with {"Header": [{"status": ...}], "Data": [...]} ("Body" in the documentation's
  examples); "Failed" is an error, "No data matched your selection" is an empty result.
- bdate and edate must fall in the same year (except for monitors): a longer range is split into one
  call per year here, and the rows are concatenated. The API asks for at most 10 requests a minute and
  a 5-second pause between them; policy limits (0.2 requests a second, burst 1) enforce that.
- States may be given as FIPS, USPS code or name; counties and sites are zero-padded.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

BASE = "https://aqs.epa.gov/data/api"
NEEDS = {"byState": ["state"], "byCounty": ["state", "county"], "bySite": ["state", "county", "site"],
         "byCBSA": ["cbsa"], "byBox": ["minlat", "maxlat", "minlon", "maxlon"]}


def _header(d):
    h = d.get("Header") if isinstance(d, dict) else None
    return (h[0] if isinstance(h, list) and h else h) or {}


def check_error(status, body: bytes):
    if body.lstrip()[:1] != b"{":
        return None
    try:
        h = _header(json.loads(body.decode("utf-8-sig")))
    except ValueError:
        return None
    if str(h.get("status", "")).lower() == "failed":
        return "AQS request failed: " + "; ".join(map(str, h.get("error") or []))[:400]
    return None


def _state_fips(v):
    s = str(v).strip()
    if s.isdigit():
        return s.zfill(2)
    try:
        from analysis.combine import state
        return state(s, "fips")
    except (ImportError, ValueError):
        return s


def before_request(req, ep, params):
    if params.get("state") not in (None, ""):
        params["state"] = _state_fips(params["state"])
    for k, n in (("county", 3), ("site", 4)):
        if params.get(k) not in (None, "") and str(params[k]).isdigit():
            params[k] = str(params[k]).zfill(n)
    return params


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") != "data":
            continue
        p, name = r.get("params") or {}, r.get("name")
        for k in ("bdate", "edate", "cbdate", "cedate"):
            if p.get(k) not in (None, "") and not re.fullmatch(r"\d{8}", str(p[k])):
                probs.append(f"request {name!r}: {k} must be YYYYMMDD, e.g. 20230101")
        if len(str(p.get("param", "")).split(",")) > 5:
            probs.append(f"request {name!r}: at most 5 parameter codes per request")
        missing = [k for k in NEEDS.get(p.get("geo"), []) if p.get(k) in (None, "")]
        if missing:
            probs.append(f"request {name!r}: {p.get('geo')} needs {', '.join(missing)}")
        if p.get("service") == "sampleData" and str(p.get("bdate", ""))[:4] != str(p.get("edate", ""))[:4]:
            probs.append(f"request {name!r}: sampleData is very large; ask for at most one year at a time")
    return probs


def year_windows(params):
    """[(bdate, edate)] per calendar year (monitors take any range)."""
    b, e = str(params.get("bdate", "")), str(params.get("edate", ""))
    if params.get("service") == "monitors" or not (b[:4].isdigit() and e[:4].isdigit()) or b[:4] == e[:4]:
        return [(b, e)]
    out = []
    for y in range(int(b[:4]), int(e[:4]) + 1):
        out.append((b if y == int(b[:4]) else f"{y}0101", e if y == int(e[:4]) else f"{y}1231"))
    return out


def _rows(d):
    if isinstance(d, dict):
        rows = d.get("Data", d.get("Body"))
        return rows if isinstance(rows, list) else []
    return d if isinstance(d, list) else []


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        params = before_request(r, ep, params)
        wins = year_windows(params) if "bdate" in params else [(None, None)]
        rows = []
        for b, e in wins:
            p = dict(params)
            if b is not None:
                p.update(bdate=b, edate=e)
            path, query, _ = gr.split(ep, p)
            d = c.get_json(path, query, allow_404=True)
            status = str(_header(d).get("status", ""))
            got = _rows(d)
            if not got and status and "no data" not in status.lower() and status.lower() != "success":
                notes.append(f"{r['name']}: AQS status {status!r} for {b}-{e}")
            rows += got
        if len(wins) > 1:
            notes.append(f"{r['name']}: {len(wins)} calls, one per year (AQS needs bdate and edate in the same year)")
        rows = gr._clean(rows, ep)
        if r.get("max_rows"):
            rows = rows[: int(r["max_rows"])]
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r}: no data matched (check parameter code, dates and geography; "
                            "AQS data lag about 6 months)", kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        params = before_request(r, ep, params)
        wins = year_windows(params) if "bdate" in params else [(None, None)]
        for i, (b, e) in enumerate(wins):
            p = dict(params)
            if b is not None:
                p.update(bdate=b, edate=e)
            path, query, _ = gr.split(ep, p)
            url = f"{BASE}{path}?" + urllib.parse.urlencode(query) + "&email={AQS_EMAIL}&key={API_KEY}"
            fn = f"{r['name']}.json" if len(wins) == 1 else f"{r['name']}_{i + 1}.json"
            items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url, "filename": fn,
                          "needs_key": True, "method": "GET", "body": None,
                          "note": "replace {AQS_EMAIL} with the email your AQS key is registered to"
                                  + ("" if len(wins) == 1 else f"; year {str(b)[:4]}")})
    return items


def parse_upload(pack, req, paths):
    ep, _, _ = gr.resolve(pack, req)
    rows = []
    for p in sorted(paths):
        with open(p, encoding="utf-8-sig") as f:
            rows += _rows(json.load(f))
    return gr._clean(rows, ep)


def ping(client):
    try:
        d = client.get_json("/list/classes", allow_404=True)
    except DataError as e:
        # a 400 or a "Failed" header on a list call means the key or email was not accepted
        kind = "auth" if e.kind in ("api", "bad_request") else e.kind
        return {"auth_ok": kind not in ("auth", "key_missing"), "kind": kind, "detail": str(e)}
    st = str(_header(d).get("status", ""))
    return {"auth_ok": st.lower() == "success", "kind": "" if st.lower() == "success" else "auth",
            "detail": f"AQS status: {st}"}

"""EPA AQS Data Mart adapter (aqs.epa.gov/data/api).

- Credentials: AQS needs two query parameters, key (AQS_API_KEY) and email (AQS_EMAIL); policy.json maps
  both (auth.param and auth.extra) so core.http adds them and keeps them out of recorded URLs.
- AQS answers with {"Header": [{"status": ...}], "Data": [...]} ("Body" in the documentation's
  examples); "Failed" is an error, "No data matched your selection" is an empty result.
- bdate and edate must fall in the same year (except for monitors): a longer range is split into one
  call per year here, and the rows are concatenated. The API asks for at most 10 requests a minute and
  a 5-second pause between them; policy limits (0.2 requests a second, burst 1) enforce that.
- States may be given as FIPS, USPS code or name; counties and sites are zero-padded.
- Keyless route: EPA's AirData yearly files (annualData, dailyData) go through --ingest as downloaded (.zip or
  its .csv); parse_upload keeps the request's rows and uses the API's column names (see AIRDATA_FILES).
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
            alt = airdata_names(params, str(b or "")[:4])
            items.append({"name": f"{r['name']}#{i + 1}", "request": r["name"], "url": url, "filename": fn,
                          "needs_key": True, "method": "GET", "body": None, "alt_filenames": alt,
                          "note": "replace {AQS_EMAIL} with the email your AQS key is registered to"
                                  + ("" if len(wins) == 1 else f"; year {str(b)[:4]}")
                                  + (f"; without a key, the AirData file {AIRDATA_URL}{alt[0]} (as downloaded) "
                                     "holds the same rows" if alt else "")})
    return items


# ---------------------------------------------------------------------- AirData files (the keyless route)
# EPA's pre-generated yearly files hold the same AQS summaries for every monitor in the country: daily files
# per parameter (daily_<code>_<year>.zip, 2-10 MB zipped) and one annual file for all parameters
# (annual_conc_by_monitor_<year>.zip, about 4 MB; hourly files are 70+ MB and not used here; sizes checked
# 2026-10-28). --ingest reads the zip as downloaded (or its CSV), keeps the rows the request asks for (state,
# county, site or box, parameter, dates) and names the columns as the API does where the two differ.
AIRDATA_URL = "https://aqs.epa.gov/aqsweb/airdata/"
AIRDATA_FILES = {"dailyData": "daily_{param}_{year}", "annualData": "annual_conc_by_monitor_{year}"}
AIRDATA_COLS = {"site_num": "site_number", "parameter_name": "parameter", "method_name": "method",
                "address": "site_address", "state_name": "state", "county_name": "county", "city_name": "city",
                "cbsa_name": "cbsa", "arithmetic_standard_dev": "standard_deviation",
                "99th_percentile": "ninety_ninth_percentile", "98th_percentile": "ninety_eighth_percentile",
                "95th_percentile": "ninety_fifth_percentile", "90th_percentile": "ninetieth_percentile",
                "75th_percentile": "seventy_fifth_percentile", "50th_percentile": "fiftieth_percentile",
                "10th_percentile": "tenth_percentile"}
ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth"}


def _codes(params) -> list[str]:
    return [c.strip() for c in str(params.get("param") or "").split(",") if c.strip()]


def airdata_names(params, year: str) -> list[str]:
    """The AirData file names that hold this request's rows for one year ([] when no file does)."""
    pat = AIRDATA_FILES.get(params.get("service"))
    codes = _codes(params)
    if not pat or not year.isdigit() or ("{param}" in pat and len(codes) != 1):
        return []                                       # daily files hold one parameter each
    stem = pat.format(param=codes[0] if codes else "", year=year)
    return [stem + ".zip", stem + ".csv"]


def _airdata_col(h: str) -> str:
    c = re.sub(r"[^0-9a-z]+", "_", h.strip().lower()).strip("_")
    c = AIRDATA_COLS.get(c, c)
    first = c.split("_", 1)[0]
    return ORDINALS[first] + c[len(first):] if first in ORDINALS else c


def _airdata_csv(f):
    """Rows of one AirData CSV (a text stream), with API-style column names."""
    import csv
    reader = csv.reader(f)
    head = [_airdata_col(h) for h in next(reader, [])]
    for row in reader:
        yield dict(zip(head, row))


def _airdata_rows(path):
    import io
    import zipfile
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith(".csv"):
                    with z.open(n) as raw:
                        yield from _airdata_csv(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from _airdata_csv(f)


def airdata_filter(params, name: str):
    """A test that keeps the AirData rows this request asks for (the file covers every U.S. monitor)."""
    service, geo = params.get("service"), params.get("geo")
    if service not in AIRDATA_FILES:
        raise DataError(f"request {name!r}: AirData files hold annualData and dailyData only (not {service}); "
                        "the other services need the AQS key", kind="bad_request")
    if geo == "byCBSA":
        raise DataError(f"request {name!r}: AirData files carry CBSA names, not codes: ask byCounty (one request per "
                        "county of the CBSA) or byBox", kind="bad_request")
    want = {"state_code": _state_fips(params["state"]) if params.get("state") not in (None, "") else None,
            "county_code": str(params.get("county") or "").zfill(3) if params.get("county") not in (None, "") else None,
            "site_number": str(params.get("site") or "").zfill(4) if params.get("site") not in (None, "") else None}
    want = {k: v for k, v in want.items() if v and geo in ("byState", "byCounty", "bySite")}
    codes = set(_codes(params))
    b, e = str(params.get("bdate") or ""), str(params.get("edate") or "")
    lo, hi = f"{b[:4]}-{b[4:6]}-{b[6:8]}", f"{e[:4]}-{e[4:6]}-{e[6:8]}"
    box = [float(params[k]) for k in ("minlat", "maxlat", "minlon", "maxlon")] if geo == "byBox" else None

    def keep(r):
        if any(str(r.get(k, "")).zfill(len(v)) != v for k, v in want.items()):
            return False
        if codes and r.get("parameter_code") not in codes:
            return False
        if service == "dailyData":
            if not (lo <= str(r.get("date_local", "")) <= hi):
                return False
        elif not (b[:4] <= str(r.get("year", "")) <= e[:4]):
            return False
        if box:
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
            except (KeyError, ValueError):
                return False
            return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]
        return True
    return keep


def parse_upload(pack, req, paths):
    """API pages saved as JSON (the keyed manual route), or AirData files (.zip as downloaded, or its .csv)."""
    ep, params, _ = gr.resolve(pack, req)
    params = before_request(req, ep, params)
    rows = []
    for p in sorted(paths):
        if p.lower().endswith((".zip", ".csv")):
            keep = airdata_filter(params, req["name"])
            mark = "date_local" if params["service"] == "dailyData" else "metric_used"
            for i, r in enumerate(_airdata_rows(p)):
                if i == 0 and mark not in r:            # an annual file for a daily request, or another CSV
                    raise DataError(f"request {req['name']!r}: {os.path.basename(p)} is not an AirData "
                                    f"{params['service']} file ("
                                    f"{AIRDATA_FILES[params['service']].format(param='<code>', year='<year>')}.zip)",
                                    kind="bad_request")
                if keep(r):
                    rows.append(r)
            continue
        with open(p, encoding="utf-8-sig") as f:
            rows += _rows(json.load(f))
    if not rows and not req.get("allow_empty"):
        raise DataError(f"request {req['name']!r}: no row of {', '.join(os.path.basename(p) for p in paths)} matches "
                        "the request (state, county, site, parameter, dates): check them, or that the file is the "
                        "right year and parameter", kind="bad_request")
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

"""USGS adapter: earthquakes (FDSN event service, keyless) and the Water Data OGC API (optional key).

- fetch: earthquake requests use a keyless client (the api.data.gov key is never sent to
  earthquake.usgs.gov); water requests follow the OGC "next" links (cursor paging) that the generic
  fetcher cannot read.
- before_request: site numbers get the "USGS-" agency prefix; state names or USPS codes become FIPS;
  skipGeometry defaults to true except for monitoring-locations, where a site id goes in `id`.
- earthquakes with `eventid`: the service answers a single Feature, which becomes one row.
- check_spec: an earthquake box crossing the 180th meridian is one box with maxlongitude above 180 (the service
  takes -360..360; verified 2026-10: 170..190 returns exactly the events of 170..180 plus -180..-170). A box
  with minlongitude at or above maxlongitude is refused (HTTP 400 upstream): when min is east (> 0) and max
  west (< 0) the message gives the +360 form, otherwise it says the bounds look swapped.
- normalize: GeoJSON "properties." prefixes go; earthquake times (epoch milliseconds) get ISO UTC columns
  and the coordinates become longitude, latitude, depth_km; points get longitude, latitude.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import Client, DataError  # noqa: E402

WATER_BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"
QUAKE_EPS = ("earthquakes", "earthquake_count")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") not in QUAKE_EPS:
            continue
        p = r.get("params") or {}
        lo, hi = _num(p.get("minlongitude")), _num(p.get("maxlongitude"))
        if lo is None or hi is None or lo < hi:
            continue
        if lo > 0 > hi:             # east of 180 written as negative: a box across the 180th meridian
            fix = (f"a box crossing the 180th meridian is one box with maxlongitude above 180 (minlongitude {lo:g}, "
                   f"maxlongitude {hi + 360:g})")
        else:
            fix = "the two values look swapped"
        probs.append(f"request {r.get('name')!r}: minlongitude must be below maxlongitude; {fix}")
    return probs


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
    if ep.get("path", "").startswith("/collections/"):
        ids = params.get("monitoring_location_id")
        if ids not in (None, ""):
            parts = [p.strip() for p in str(ids).split(",") if p.strip()]
            params["monitoring_location_id"] = ",".join(p.upper() if "-" in p else f"USGS-{p}" for p in parts)
            if params.get("collection") == "monitoring-locations":
                # the sites themselves: the site is the feature id, and this collection answers 400
                # to monitoring_location_id (its queryables have `id`; verified 2026-10)
                params["id"] = params.pop("monitoring_location_id")
        if params.get("state_code") not in (None, ""):
            params["state_code"] = _state_fips(params["state_code"])
        if params.get("county_code") not in (None, "") and str(params["county_code"]).isdigit():
            params["county_code"] = str(params["county_code"]).zfill(3)
        if "skipGeometry" not in params:
            params["skipGeometry"] = "false" if params.get("collection") == "monitoring-locations" else "true"
    return params


def _water(c, ep, params, max_rows):
    limit = int(max_rows or ep.get("max_rows") or 200000)
    size = int((ep.get("paging") or {}).get("size") or 10000)
    path, query, _ = gr.split(ep, params)
    query["limit"] = min(size, limit)
    rows, url, pages, more = [], None, 0, False
    while True:
        resp = c.get(url) if url else c.get(path, query)
        pages += 1
        data = resp.json()
        got = data.get("features") or []
        rows += got
        url = next((l.get("href") for l in data.get("links") or [] if l.get("rel") == "next"), None)
        if len(rows) >= limit:
            more = bool(url)
            rows = rows[:limit]
            break
        if not url or not got or pages >= 500:
            more = bool(url and got)
            break
    return gr._clean(rows, ep), more


def _features(data) -> list:
    """GeoJSON features of an earthquake response. A query with `eventid` answers one Feature, not a
    FeatureCollection (verified 2026-10): it becomes one row. Its `products` catalog (ShakeMap, PAGER files,
    tens of KB) is left out so the row has the same columns as a search row; the event `url` links to it."""
    if isinstance(data, dict) and data.get("type") == "Feature":
        f = dict(data)
        f["properties"] = {k: v for k, v in (data.get("properties") or {}).items() if k != "products"}
        return [f]
    return (data.get("features") or []) if isinstance(data, dict) else []


def _quake(c, ep, params, max_rows):
    if params.get("eventid") in (None, "") or ep.get("records") != "features":
        rows, meta = gr.fetch_request(c, ep, params, max_rows)
        return rows, meta["truncated"]
    path, query, _ = gr.split(ep, params)
    return gr._clean(_features(c.get(path, query).json()), ep), False


def _iso_ms(v):
    try:
        return datetime.fromtimestamp(int(float(v)) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def normalize(req, ep, rows):
    if ep.get("records") != "features":
        return rows
    quake = "earthquake.usgs.gov" in ep.get("path", "")
    out = []
    for r in rows:
        n = {}
        for k, v in r.items():
            n[k[len("properties."):] if k.startswith("properties.") else k] = v
        coords = [p.strip() for p in str(n.pop("geometry.coordinates", "") or "").split(";") if p.strip()]
        n.pop("type", None)
        if quake:
            n["time_utc"] = _iso_ms(n.get("time"))
            n["updated_utc"] = _iso_ms(n.get("updated"))
            if len(coords) == 3:
                n["longitude"], n["latitude"], n["depth_km"] = coords
        elif len(coords) >= 2:
            n["longitude"], n["latitude"] = coords[0], coords[1]
        out.append(n)
    return out


def fetch(spec, pack, route):
    water = gr.client_for(pack)
    quake = Client("", name="usgs-earthquake", auth={"placement": "none"}, rps=5, burst=10,
                   headers=(pack.endpoints or {}).get("headers"))
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        params = before_request(r, ep, params)
        if ep["path"].startswith("http"):
            rows, more = _quake(quake, ep, params, r.get("max_rows"))
        else:
            rows, more = _water(water, ep, params, r.get("max_rows"))
        if more:
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); the source has more. Narrow the "
                         "filter (time, sites, parameter) or raise max_rows.")
        rows = normalize(r, ep, rows)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check site ids, parameter code, statistic "
                            "and time window (see source.md)", kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    for it in items:
        for scheme in ("https://", "http://"):
            if it["url"].startswith(f"{base}/{scheme}"):
                it["url"] = scheme + it["url"][len(base) + 1 + len(scheme):]
        if it["url"].startswith(WATER_BASE):
            req = next((r for r in spec.get("requests") or [] if r.get("name") == it["request"]), {})
            ep, params, _ = gr.resolve(pack, req)
            path, query, _ = gr.split(ep, before_request(req, ep, params))
            import urllib.parse
            query["limit"] = 10000
            it["url"] = f"{WATER_BASE}{path}?" + urllib.parse.urlencode(query)
            it["note"] = ("up to 10,000 rows; if the JSON has a links entry with rel \"next\", open that URL too and "
                          "save it as the next numbered file")
    return items


def ping(client):
    """Check the key against the water API only (the earthquake host never receives it)."""
    try:
        client.get(f"{WATER_BASE}/collections/parameter-codes/items", {"f": "json", "limit": 1})
        return {"auth_ok": True, "kind": "", "detail": "water data API answered with the key"}
    except DataError as e:
        return {"auth_ok": e.kind not in ("auth", "key_missing"), "kind": e.kind, "detail": str(e)}

"""National Weather Service (api.weather.gov) adapter.

- fetch: forecast endpoints first resolve the point (/points/lat,lon) to its forecast grid, then read
  the forecast URL that the point names; everything else runs through the generic fetcher.
- User-Agent: NWS asks every client for an identifying User-Agent. The shared one is sent; set
  NWS_USER_AGENT (e.g. "my-analysis (me@example.com)") to send your own.
- normalize: GeoJSON features lose their "properties." prefix; alert polygons are dropped (they are
  large; geometry.type stays); station points get longitude/latitude columns.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402


def _client(pack):
    ua = (os.environ.get("NWS_USER_AGENT") or "").strip()
    return gr.client_for(pack, **({"user_agent": ua} if ua else {}))


def _round(params):
    # /points accepts at most 4 decimal places; more gives a 301 redirect to the rounded point
    for k in ("lat", "lon"):
        if params.get(k) not in (None, ""):
            params[k] = f"{float(params[k]):.4f}".rstrip("0").rstrip(".")
    return params


def _forecast(c, ep, params, max_rows):
    path, _, _ = gr.split(ep, params)
    point = c.get_json(path)
    props = point.get("properties") or {}
    url = props.get(ep["resolve"])
    if not url:
        raise DataError(f"nws: the point {params.get('lat')},{params.get('lon')} has no {ep['resolve']} "
                        "(NWS forecasts cover the U.S. and its territories only)", kind="bad_request")
    fc = c.get_json(url)
    fp = fc.get("properties") or {}
    rows = gr._clean(fp.get("periods") or [], ep)
    extra = {"office": props.get("gridId", ""), "grid_x": props.get("gridX", ""), "grid_y": props.get("gridY", ""),
             "generated_at": fp.get("generatedAt", ""), "update_time": fp.get("updateTime", "")}
    rows = [{**r, **extra} for r in rows]
    return rows[: int(max_rows)] if max_rows else rows


def fetch(spec, pack, route):
    c = _client(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        if "lat" in params:
            params = _round(params)
        if ep.get("resolve"):
            rows = _forecast(c, ep, params, r.get("max_rows"))
        else:
            rows, meta = gr.fetch_request(c, ep, params, r.get("max_rows"))
            if meta["truncated"]:
                notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows or page limit); the source has more.")
        rows = normalize(r, ep, rows)
        if not rows and not r.get("allow_empty"):
            if ep.get("path") == "/alerts/active":
                notes.append(f"{r['name']}: no active alerts matched (a valid answer: nothing is in effect now).")
            else:
                raise DataError(f"request {r['name']!r} returned no rows: check the parameters (see source.md)",
                                kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def normalize(req, ep, rows):
    if ep.get("records") != "features":
        return rows
    out = []
    for r in rows:
        n = {}
        for k, v in r.items():
            if k.startswith("properties."):
                k = k[len("properties."):]
            if k.startswith("@") or k in ("@id", "@type"):
                continue
            n[k] = v
        coords = n.pop("geometry.coordinates", "")
        if n.get("geometry.type") == "Point" and coords:
            parts = [p.strip() for p in str(coords).split(";")]
            if len(parts) >= 2:
                n["longitude"], n["latitude"] = parts[0], parts[1]
        out.append(n)
    return out


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)
    eps = (pack.endpoints or {}).get("endpoints", {})
    for it in items:
        req = next((r for r in spec.get("requests") or [] if r.get("name") == it["request"]), {})
        ep = eps.get(req.get("endpoint", ""), {})
        if ep.get("resolve"):
            it["note"] = (f"This link returns the point. Open the URL in its properties.{ep['resolve']} field and save "
                          f"THAT page as {it['filename']}.")
    return items

"""treasury-yields adapter: one CSV request per calendar year, stacked into one tidy table per request.

home.treasury.gov serves each interest-rate feed as one CSV per calendar year (verified 2026-10-20):
  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/<year>/all
      ?type=<feed>&field_tdr_date_value=<year>&_format=csv
(the trailing "&page" seen on the site's own links is not needed; "all" for the year is refused with HTTP
403; a year before the feed starts, or in the future, returns HTTP 200 with an empty body). Rows come newest
first with a "Date" column in MM/DD/YYYY. This adapter
  - requests every year from start_year to end_year (default: the current year),
  - writes `date` as YYYY-MM-DD (the published MM/DD/YYYY converted; nothing else is changed) and keeps every
    maturity column with the published header ("1 Mo", "1.5 Month", "10 Yr", "4 WEEKS BANK DISCOUNT" ...),
  - stacks the years oldest first; maturities a year did not publish (2 Mo before Oct 2018, 30 Yr from Feb
    2002 to Feb 2006, 4 Mo before Oct 2022, 1.5 Month before 2025) are empty cells; a published 0.00 is a real
    value and is kept as published.
"""
from __future__ import annotations

import csv
import io
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

SPEC_ONLY = ("start_year", "end_year", "year")


def _years(req: dict, ep: dict) -> list[int]:
    p = req.get("params") or {}
    this = date.today().year
    if p.get("year") not in (None, ""):
        y0 = y1 = int(p["year"])
    else:
        y0 = int(p.get("start_year") or this)
        y1 = int(p.get("end_year") or (this if p.get("start_year") else y0))
    return list(range(y0, y1 + 1))


def _iso(v: str) -> str:
    parts = str(v or "").strip().split("/")
    if len(parts) == 3 and all(x.isdigit() for x in parts):
        m, d, y = (int(x) for x in parts)
        return f"{y:04d}-{m:02d}-{d:02d}"
    return str(v or "").strip()


def _tidy(chunks: list[list[dict]]) -> list[dict]:
    """Stack yearly chunks (each newest first) into one table, oldest first, with `date` first and the union of
    the maturity columns in the order the latest year publishes them."""
    cols: list[str] = []
    for rows in sorted(chunks, key=lambda rs: max((_iso(r.get("Date", "")) for r in rs), default=""), reverse=True):
        for r in rows:
            for k in r:
                if k != "Date" and k not in cols:
                    cols.append(k)
    out, seen = [], set()
    for rows in chunks:
        for r in rows:
            d = _iso(r.get("Date", ""))
            if not d or d in seen:
                continue
            seen.add(d)
            out.append({"date": d, **{c: r.get(c, "") for c in cols}})
    return sorted(out, key=lambda r: r["date"])


def check_spec(spec):
    probs = []
    this = date.today().year
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        try:
            for k in SPEC_ONLY:
                if p.get(k) not in (None, ""):
                    int(p[k])
        except (TypeError, ValueError):
            probs.append(f"request {r.get('name')!r}: year, start_year and end_year are four-digit years")
            continue
        ep = (_ENDPOINTS or {}).get(r.get("endpoint") or "") or {}
        ys = _years(r, ep)
        if not ys:
            probs.append(f"request {r.get('name')!r}: end_year is before start_year")
            continue
        first = ep.get("first_year")
        if first and ys[0] < first:
            probs.append(f"request {r.get('name')!r}: {r.get('endpoint')} starts in {first} (asked from {ys[0]})")
        if ys[-1] > this:
            probs.append(f"request {r.get('name')!r}: {ys[-1]} is in the future")
        if len(ys) > 40:
            probs.append(f"request {r.get('name')!r}: {len(ys)} years is one request each; split it or narrow it")
    return probs


_ENDPOINTS: dict | None = None
try:
    _ENDPOINTS = packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json")["endpoints"]
except (OSError, ValueError, KeyError, TypeError):
    _ENDPOINTS = None


def fetch(spec, pack, route):
    client = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        base = {k: v for k, v in params.items() if k not in SPEC_ONLY}
        chunks, empty = [], []
        for y in _years(r, ep):
            rows, _ = gr.fetch_request(client, ep, {**base, "year": y, "field_tdr_date_value": y})
            (chunks.append(rows) if rows else empty.append(y))
        if empty:
            notes.append(f"{r['name']}: no rows published for {', '.join(map(str, empty))}")
        rows = _tidy(chunks)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows for {_years(r, ep)} (see source.md: feed "
                            "start years)", kind="bad_request")
        if rows:
            notes.append(f"{r['name']}: {rows[0]['date']} to {rows[-1]['date']}, {len(rows):,} days")
        tables[r["name"]] = rows
    return tables, notes


def manual_items(spec, pack):
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        feed = params.get("type")
        for y in _years(r, ep):
            items.append({"name": f"{r['name']}#{y}", "request": r["name"],
                          "url": f"{base}/daily-treasury-rates.csv/{y}/all?type={feed}&field_tdr_date_value={y}"
                                 "&_format=csv",
                          "filename": f"{r['name']}_{y}.csv", "needs_key": False, "method": "GET", "body": None,
                          "note": "or: Interest Rate Statistics page > choose the rate type and the year > Download CSV"})
    return items


def parse_upload(pack, req, paths):
    chunks = []
    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(io.StringIO(f.read())))
        if rows and "Date" not in rows[0]:
            raise DataError(f"{os.path.basename(p)} has no Date column: upload the CSV from the download link",
                            kind="bad_request")
        chunks.append([{k: ("" if v is None or str(v).strip() in ("", "N/A") else v) for k, v in r.items()}
                       for r in rows])
    return _tidy(chunks)

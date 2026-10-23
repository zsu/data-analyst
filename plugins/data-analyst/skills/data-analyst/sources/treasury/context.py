#!/usr/bin/env python
"""Fetch non-Treasury context series: CPI (to express dollars in real terms) and GDP (to express
amounts as a share of the economy). Use only when the user wants real dollars or "% of GDP".

Usage (through the dispatcher; paths relative to the skill folder)
  python scripts/fetch.py --source treasury --context cpi --out OUT [--start 2000]   # CPI-U, monthly, NSA
  python scripts/fetch.py --source treasury --context gdp --out OUT [--start 2000]   # GDP, quarterly SAAR
  (or python sources/treasury/context.py cpi --out OUT --start 2000)
  --start is the first FISCAL year needed: data are kept from October 1 of the year before.

Sources, tried in order (the first that answers wins; the file records which one was used):
  cpi  1. FRED  CPIAUCNS  (https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCNS, no key)
       2. BLS   CUUR0000SA0 (https://api.bls.gov/publicAPI/, no key: 10 years per request, 25 requests/day;
                 a BLS_API_KEY found by core.keys gives v2 limits). Same series as CPIAUCNS: CPI-U, U.S. city
                 average, all items, 1982-84=100, not seasonally adjusted.
  gdp  1. FRED  GDP (quarterly, billions of dollars, seasonally adjusted annual rate)
       2. World Bank NY.GDP.MKTP.CN (annual, CALENDAR years, current dollars). Calendar-year only: it cannot
                 give fiscal-year GDP. Say so if it is the source and the analysis is by fiscal year.
Some networks block FRED (it can hang rather than refuse); each source has a short timeout.
When every source fails, the script prints the URLs for the user to download by hand and exits 4.
Never type CPI or GDP values from memory. (The skill also has fred and bls packs; this helper keeps the
treasury pack's fiscal-year conventions in one place.)

Output: OUT/data/treasury/context_<cpi|gdp>.csv (date, value) and OUT/data/treasury/context_sources.json
(source, series id, URL, units, retrieval time, SHA-256). tstats.cpi_index()/fy_gdp() read these files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from core import keys  # noqa: E402
from core.http import Client  # noqa: E402
from core.package import data_dir  # noqa: E402

UA = "Mozilla/5.0 (data-analyst treasury pack)"
SERIES = {
    "cpi": {"fred": "CPIAUCNS", "bls": "CUUR0000SA0",
            "units": "Index 1982-84=100, not seasonally adjusted (CPI-U, U.S. city average, all items)",
            "frequency": "monthly"},
    "gdp": {"fred": "GDP", "worldbank": "NY.GDP.MKTP.CN",
            "units": "Billions of dollars, seasonally adjusted annual rate (FRED/BEA); World Bank fallback is "
                     "annual calendar-year dollars, converted to billions",
            "frequency": "quarterly"},
}


BLS_AUTH = {"env": "BLS_API_KEY", "home": "bls", "placement": "body", "param": "registrationkey"}


def _client(timeout: int, auth: dict | None = None) -> Client:
    return Client("", name="treasury-context", auth=auth, rps=2, burst=2, retries=0, timeout=timeout,
                  deadline=timeout * 2, user_agent=UA)


def _get(url: str, timeout: int, body: dict | None = None, auth: dict | None = None) -> bytes:
    c = _client(timeout, auth)
    if body is not None:
        return c.request("POST", url, json_body=body).body
    return c.get(url).body


def fy_start(start: int) -> str:
    """--start is the first FISCAL year needed; it begins on October 1 of the previous calendar year."""
    return f"{start - 1}-10-01"


def missing_months(rows: list[tuple[str, float]]) -> list[str]:
    """Months absent between the first and last monthly date (October 2025 CPI was never collected): a fiscal-year
    average over the rest has fewer months, so say so."""
    dates = [r[0][:7] for r in rows]
    if len(dates) < 2 or not all(d[8:10] in ("", "01") for d in (r[0] for r in rows)):
        return []
    y, m = int(dates[0][:4]), int(dates[0][5:7])
    have, out = set(dates), []
    while f"{y}-{m:02d}" <= dates[-1]:
        if f"{y}-{m:02d}" not in have:
            out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out if len(out) < len(dates) else []


def from_fred(series: str, start: int, timeout: int) -> tuple[list[tuple[str, float]], str]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    text = _get(url, timeout).decode("utf-8")
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2 or not r[0][:4].isdigit():
            continue
        if r[1] in (".", ""):
            continue
        if r[0][:10] >= fy_start(start):
            rows.append((r[0], float(r[1])))
    return rows, url


def from_bls(series: str, start: int, timeout: int) -> tuple[list[tuple[str, float]], str]:
    key, _ = keys.find_key("BLS_API_KEY", "bls")
    ver, span = ("v2", 20) if key else ("v1", 10)
    url = f"https://api.bls.gov/publicAPI/{ver}/timeseries/data/"
    end = date.today().year
    rows = []
    for y0 in range(start - 1, end + 1, span):
        body = {"seriesid": [series], "startyear": str(y0), "endyear": str(min(y0 + span - 1, end))}
        j = json.loads(_get(url, timeout, body, BLS_AUTH))      # the key (if any) is added by core.http
        if j.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(keys.redact(f"BLS: {j.get('status')} {j.get('message')}"))
        for d in j["Results"]["series"][0]["data"]:
            if d["period"].startswith("M") and d["period"] != "M13" and d["value"] not in ("-", ""):
                rows.append((f"{d['year']}-{d['period'][1:]}-01", float(d["value"])))
    return [r for r in sorted(set(rows)) if r[0] >= fy_start(start)], url + series


def from_worldbank(indicator: str, start: int, timeout: int) -> tuple[list[tuple[str, float]], str]:
    url = (f"https://api.worldbank.org/v2/country/USA/indicator/{indicator}?format=json&per_page=200"
           f"&date={start - 1}:{date.today().year}")
    j = json.loads(_get(url, timeout))
    rows = [(f"{d['date']}-01-01", d["value"] / 1e9) for d in j[1] if d.get("value") is not None]
    return sorted(rows), url


def fetch(kind: str, out: str, start: int, timeout: int = 25) -> tuple[dict, int]:
    """(result, exit code): 0 when a source answered, 4 with manual links when none did."""
    s = SERIES[kind]
    attempts = [("FRED", lambda: from_fred(s["fred"], start, timeout), s["fred"], s["frequency"])]
    if kind == "cpi":
        attempts.append(("BLS", lambda: from_bls(s["bls"], start, timeout), s["bls"], "monthly"))
    else:
        attempts.append(("World Bank", lambda: from_worldbank(s["worldbank"], start, timeout), s["worldbank"],
                         "annual (calendar year)"))
    errors = []
    for name, fn, sid, freq in attempts:
        try:
            rows, url = fn()
            if not rows:
                raise RuntimeError("no rows returned")
        except Exception as e:  # noqa: BLE001 - any failure moves on to the next source
            errors.append(keys.redact(f"{name}: {str(e)[:160]}"))
            print(keys.redact(f"[context] {name} failed ({str(e)[:120]}); trying the next source"), file=sys.stderr)
            continue
        d = data_dir(out, "treasury")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"context_{kind}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "value"])
            w.writerows(rows)
        meta_path = os.path.join(d, "context_sources.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        meta[kind] = {"source": name, "series_id": sid, "url": url, "frequency": freq,
                      "units": s["units"] if name != "World Bank" else "Billions of current dollars, calendar year",
                      "first": rows[0][0], "last": rows[-1][0], "rows": len(rows),
                      "retrieved": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "sha256": digest, "failed_sources": errors, "missing_months": missing_months(rows)}
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return {"status": "ok", "kind": kind, "path": path, **meta[kind], "series_source": name,
                "source": "treasury"}, 0
    manual = {"cpi": ["https://fred.stlouisfed.org/series/CPIAUCNS (Download > CSV)",
                      "https://data.bls.gov/timeseries/CUUR0000SA0 (More formatting options > Download)"],
              "gdp": ["https://fred.stlouisfed.org/series/GDP (Download > CSV)",
                      "https://apps.bea.gov/iTable/?reqid=19&step=2&isuri=1&categories=survey (NIPA Table 1.1.5)"]}[kind]
    return {"status": "blocked", "source": "treasury", "route": "manual_download", "kind": kind, "errors": errors,
            "manual_download": manual,
            "message": f"No {kind.upper()} source answered from here. Ask the user whether to continue in nominal "
                       "dollars (and say so in the product) or to download the series by hand and upload it as "
                       f"context_{kind}.csv with columns date,value (put it in OUT/data/treasury/)."}, 4


def main():
    for _s in (sys.stdout, sys.stderr):       # Windows pipes default to cp1252
        if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=sorted(SERIES))
    ap.add_argument("--out", default="out")
    ap.add_argument("--start", type=int, default=1990,
                    help="first FISCAL year needed (data are kept from October 1 of the year before)")
    ap.add_argument("--timeout", type=int, default=25)
    a = ap.parse_args()
    res, code = fetch(a.kind, a.out, a.start, a.timeout)
    print(keys.redact(json.dumps(res, indent=2)))
    sys.exit(code)


if __name__ == "__main__":
    main()

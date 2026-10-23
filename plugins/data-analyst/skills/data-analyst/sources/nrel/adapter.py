"""NREL / NLR developer API adapter: errors inside HTTP 200, and monthly outputs reshaped into rows.

solar_resource returns one object per measure with monthly values (avg_dni.monthly.jan ...); pvwatts returns
12-value lists (ac_monthly ...). Both are reshaped to one row per month (plus an "annual" row for
solar_resource). pvwatts with timeframe=hourly also returns one value per hour of a typical year (ac, dc, poa
...; 8,760 values): it is reshaped to one row per hour instead. The published values are kept exactly, only
their arrangement changes.
"""
from __future__ import annotations

import json

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def check_error(status, body: bytes):
    if body.lstrip()[:1] != b"{":
        return None
    try:
        d = json.loads(body.decode("utf-8-sig"))
    except ValueError:
        return None
    errs = d.get("errors") if isinstance(d, dict) else None
    if errs:
        return "API error: " + "; ".join(str(e) for e in errs)[:400]
    err = d.get("error") if isinstance(d, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"{err.get('code', 'error')}: {err['message']}"
    return None


def _solar(rows):
    if not rows:
        return rows
    r = rows[0]
    measures = sorted({k.split(".")[0] for k in r if "." in k})
    out = []
    for i, m in enumerate(MONTHS + ["annual"]):
        row = {"period": m, "month": i + 1 if m != "annual" else ""}
        for ms in measures:
            row[f"{ms}_kwh_m2_day"] = r.get(f"{ms}.annual" if m == "annual" else f"{ms}.monthly.{m}", "")
        out.append(row)
    return out


def _split(v):
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return v
    return [x.strip() for x in str(v).split(";")]


# timeframe=hourly arrays (PVWatts v8 docs): name -> column with its unit
HOURLY = {"ac": "ac_w", "dc": "dc_w", "poa": "poa_w_m2", "dn": "dn_w_m2", "df": "df_w_m2", "tamb": "tamb_c",
          "tcell": "tcell_c", "wspd": "wspd_m_s", "alb": "alb"}
HOURS_PER_YEAR = 8760                                 # typical meteorological year: 365 days, no Feb 29


def _pvwatts_hourly(r, cols):
    from datetime import datetime, timedelta
    n = max(len(v) for v in cols.values())
    if n % HOURS_PER_YEAR or any(len(v) != n for v in cols.values()):
        from core.http import DataError
        raise DataError(f"pvwatts timeframe=hourly returned arrays of {sorted({len(v) for v in cols.values()})} "
                        f"values, not a multiple of {HOURS_PER_YEAR} hours; the pack cannot place them in time. "
                        "Use timeframe=monthly or check the request.", kind="bad_request")
    per_hour = n // HOURS_PER_YEAR
    step_min = 60 // per_hour if 60 % per_hour == 0 else None
    t0 = datetime(2001, 1, 1)                         # any non-leap year; only month/day/hour are written
    annual = {k: r[k] for k in ("ac_annual", "solrad_annual", "capacity_factor") if k in r}
    out = []
    for i in range(n):
        t = t0 + timedelta(minutes=i * step_min) if step_min else t0 + timedelta(hours=i // per_hour)
        row = {"step": i, "month": t.month, "day": t.day, "hour": t.hour}
        if per_hour > 1:
            row["minute"] = t.minute if step_min else ""
        row.update({c: v[i] for c, v in cols.items()})
        row.update(annual)
        out.append(row)
    return out


def _pvwatts(rows):
    if not rows:
        return rows
    r = rows[0]
    hourly = {new: _split(r.get(old)) for old, new in HOURLY.items() if r.get(old) not in (None, "")}
    if hourly and max(len(v) for v in hourly.values()) > 12:
        return _pvwatts_hourly(r, hourly)
    monthly = {"ac_monthly": "ac_kwh", "dc_monthly": "dc_kwh", "poa_monthly": "poa_kwh_m2",
               "solrad_monthly": "solrad_kwh_m2_day"}
    cols = {new: _split(r.get(old)) for old, new in monthly.items() if r.get(old) not in (None, "")}
    if not cols or not any(len(v) == 12 for v in cols.values()):
        return rows                                   # an unexpected shape: keep as is
    annual = {k: r[k] for k in ("ac_annual", "solrad_annual", "capacity_factor") if k in r}
    return [{"month": i + 1, **{c: (v[i] if i < len(v) else "") for c, v in cols.items()}, **annual}
            for i in range(12)]


def normalize(req, ep, rows):
    path = ep.get("path", "")
    if path.startswith("/solar/solar_resource"):
        return _solar(rows)
    if path.startswith("/pvwatts/"):
        return _pvwatts(rows)
    return rows

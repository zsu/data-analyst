"""HHS ASPE Poverty Guidelines API adapter.

The API answers one guideline per call: GET <base>/<year>/<state>/<household_size>. This adapter
  - lets one request ask for several years, state groups and household sizes (a value, a list, or a range string
    "2020-2026"; state "all" = us, ak, hi) and returns them as one tidy table: year, state, area, household_size,
    guideline_usd, value_note;
  - rejects what the API would silently replace with a default (verified 2026-11-18: an unknown year answers the
    latest year, an unknown state answers "us", an unknown or zero size answers size 1 or the per-person
    increment): years outside 1983 to the current year, states other than us/ak/hi, sizes outside 1 to 20;
  - after each call, checks that the echoed year, state and size are the ones asked for, and fails otherwise
    (a year not yet published, e.g. in early January, comes back as the latest year);
  - keeps the published dollar value as is (the API gives it as a string or a number) and marks sizes above 8,
    which the API computes as the size-8 guideline plus the per-person increment, in value_note.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

FIRST_YEAR = 1983
MAX_SIZE = 20
MAX_CALLS = 600
AREAS = {"us": "48 contiguous states and DC", "ak": "Alaska", "hi": "Hawaii"}
ABOVE_8 = ("computed by the API: the size-8 guideline plus the per-person increment for each person above 8 "
           "(the published rule; the published tables stop at 8)")


def last_year() -> int:
    """The latest year that can have guidelines: the current calendar year (they are issued each January)."""
    return datetime.date.today().year


def _items(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _ints(v, what: str, lo: int, hi: int, probs: list, name) -> list[int]:
    out = []
    for x in _items(v):
        s = str(x).strip()
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                probs.append(f"request {name!r}: {what} range {s!r} runs backwards")
                continue
            vals = list(range(a, b + 1))
        elif s.isdigit():
            vals = [int(s)]
        else:
            probs.append(f"request {name!r}: {what} {x!r} is not a number or a range like \"{lo}-{hi}\"")
            continue
        bad = [y for y in vals if not lo <= y <= hi]
        if bad:
            probs.append(f"request {name!r}: {what} {bad[0]} is outside {lo} to {hi}"
                         + (" (the API would silently answer another year: the latest guidelines are those of "
                            "the current year at most, issued in January)" if what == "year" else
                            " (size 0 returns the per-person increment, not a guideline; the published tables "
                            "cover 1 to 8 persons)" if what == "household_size" else ""))
            continue
        out += vals
    return list(dict.fromkeys(out))


def _states(v, probs: list, name) -> list[str]:
    out = []
    for x in _items(v):
        s = str(x).strip().lower()
        if s == "all":
            out += list(AREAS)
        elif s in AREAS:
            out.append(s)
        else:
            probs.append(f"request {name!r}: state {x!r} is not one of us, ak, hi (or all). The guidelines have "
                         "three schedules: 'us' covers the 48 contiguous states and the District of Columbia, so "
                         "use 'us' for any other state; 'ak' Alaska, 'hi' Hawaii. Territories have no guidelines.")
    return list(dict.fromkeys(out))


def combos(req: dict) -> tuple[list[tuple[int, str, int]], list[str]]:
    """Every (year, state, household_size) one request asks for, and the problems with it."""
    prm, name, probs = req.get("params") or {}, req.get("name"), []
    for k in ("year", "state", "household_size"):
        if prm.get(k) in (None, "", []):
            probs.append(f"request {name!r}: parameter {k!r} is required")
    if probs:
        return [], probs
    years = _ints(prm["year"], "year", FIRST_YEAR, last_year(), probs, name)
    states = _states(prm["state"], probs, name)
    sizes = _ints(prm["household_size"], "household_size", 1, MAX_SIZE, probs, name)
    return [(y, s, n) for y in years for s in states for n in sizes], probs


def check_spec(spec):
    probs, total = [], 0
    for r in spec.get("requests") or []:
        c, p = combos(r)
        probs += p
        total += len(c)
    if total > MAX_CALLS:
        probs.append(f"the spec asks for {total} guidelines (one API call each; at most {MAX_CALLS}): ask only for "
                     "the years, state groups and sizes the question needs")
    return probs


def row(year: int, state: str, size: int, body) -> dict:
    """The tidy row for one answer, after checking that the API answered what was asked."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("income") in (None, ""):
        raise DataError(f"aspe {year}/{state}/{size}: the answer has no data.income ({str(body)[:200]})", kind="api")
    got_y, got_s, got_n = str(data.get("year", "")).strip(), str(data.get("state", "")).strip().lower(), \
        str(data.get("household_size", "")).strip()
    if got_y != str(year):
        raise DataError(f"aspe {year}/{state}/{size}: the API answered with the {got_y or '?'} guidelines, not "
                        f"{year} (it silently replaces a year it does not have with the latest one): the {year} "
                        "guidelines are not published yet, or not in the API; see the prior guidelines page "
                        "(source.md, Access)", kind="bad_request")
    if got_s != state or got_n != str(size):
        raise DataError(f"aspe {year}/{state}/{size}: the API answered state {got_s!r}, household size {got_n!r} "
                        "instead (it silently replaces values it does not accept)", kind="api")
    inc = str(data["income"]).strip()
    if not re.fullmatch(r"\d+(\.0+)?", inc):
        raise DataError(f"aspe {year}/{state}/{size}: income {inc!r} is not a dollar amount", kind="api")
    return {"year": year, "state": state, "area": AREAS[state], "household_size": size,
            "guideline_usd": inc.split(".")[0], "value_note": ABOVE_8 if size > 8 else ""}


def fetch(spec, pack, route):
    probs = gr.check_spec(pack, spec)
    if probs:
        raise DataError("; ".join(probs), kind="bad_request")
    c = gr.client_for(pack)
    ep = pack.endpoints["endpoints"]["guidelines"]
    tables, notes = {}, []
    for r in spec["requests"]:
        rows = []
        for y, s, n in combos(r)[0]:
            path, _, _ = gr.split(ep, {"year": y, "state": s, "household_size": n})
            rows.append(row(y, s, n, c.get_json(path)))
        if any(x["household_size"] > 8 for x in rows):
            notes.append(f"{r['name']}: household sizes above 8 are computed by the API (size-8 guideline plus the "
                         "per-person increment), as the published rule says; see value_note")
        tables[r["name"]] = rows
    return tables, notes


def _fname(req, y, s, n) -> str:
    return f"{req['name']}_{y}_{s}_{n:02d}.json"


def manual_items(spec, pack):
    base = pack.endpoints["base_url"].rstrip("/")
    items = []
    for r in spec.get("requests") or []:
        for i, (y, s, n) in enumerate(combos(r)[0], start=1):
            items.append({"name": f"{r['name']}#{i}", "request": r["name"], "url": f"{base}/{y}/{s}/{n}",
                          "filename": _fname(r, y, s, n), "also_ext": [], "needs_key": False, "method": "GET",
                          "body": None, "note": "", "optional": False})
    return items


def parse_upload(pack, req, paths):
    want = combos(req)[0]
    got = {}
    for p in paths:
        with open(p, "rb") as f:
            try:
                body = json.loads(f.read().decode("utf-8-sig"))
            except ValueError:
                raise DataError(f"{os.path.basename(p)} is not the API's JSON answer (save the page as JSON)",
                                kind="bad_request")
        key = next((k for k in want if os.path.basename(p).lower().startswith(_fname(req, *k)[:-5].lower())), None)
        if key is None:
            raise DataError(f"{os.path.basename(p)}: not one of the files this request expects", kind="bad_request")
        got[key] = row(*key, body)
    missing = [k for k in want if k not in got]
    if missing:
        raise DataError("missing uploads: " + ", ".join(_fname(req, *k) for k in missing), kind="bad_request")
    return [got[k] for k in want]

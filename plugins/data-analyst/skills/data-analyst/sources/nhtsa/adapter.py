"""NHTSA adapter: date columns in a sortable form, vPIC (second host) manual links, model variants.

Variants (recalls_by_vehicle, complaints_by_vehicle): NHTSA files hybrid, EV and cab-style versions under their
own model names, each with its own recalls and complaints, which the plain name does not return (verified live
2026-10: a 2023 sedan's base name gave 5 campaigns, its hybrid 7). A name that starts with the requested one
can also be a different model (a "<MODEL> SPORT" or "<MODEL> <NAME>" line), so nothing is merged unasked: the
fetch lists the make's models for that year (products_models, issueType r or c, one call per make and year)
and notes the other names that start with the requested model, so the run can request the ones it wants. With
`"variants": true` it also fetches the names whose extra words are only powertrain, body, cab, door or drive
qualifiers (HYBRID, PHEV, EV, SEDAN, "(CREW CAB)", 4DR, AWD ...), keeps each campaign / complaint once, and adds
`model_requested`. Recall names sometimes drop a parenthesized cab style ("<MODEL> (CREW CAB) EV" matches as
"<MODEL> EV"), so that form is tried too. A name that matches nothing answers HTTP 400 (Count 0): an empty
result for that name, with a note. A 5xx is not a "no record": the name is reported as not answered (the table
is marked incomplete, status partial; NHTSA has answered both no-match lookups and valid ones with 502, verified
2026-10), and the fetch fails only when no vehicle lookup answered at all (the source is down).

Recalls publish ReportReceivedDate as DD/MM/YYYY ("28/05/2020"), complaints publish dateOfIncident and
dateComplaintFiled as MM/DD/YYYY ("07/24/2025"). The published columns are kept; an *_iso column is added.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core.http import DataError  # noqa: E402

DMY = ("ReportReceivedDate",)
MDY = ("dateOfIncident", "dateComplaintFiled")


def _iso(v: str, order: str) -> str:
    parts = str(v or "").strip().split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    a, b, y = (int(p) for p in parts)
    d, m = (a, b) if order == "dmy" else (b, a)
    if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 < y < 2200):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def normalize(req, ep, rows):
    for r in rows:
        for col in DMY:
            if col in r:
                r[col + "_iso"] = _iso(r[col], "dmy")
        for col in MDY:
            if col in r:
                r[col + "_iso"] = _iso(r[col], "mdy")
    return rows


def manual_items(spec, pack):
    """Generic links, except that vPIC endpoints have an absolute path on another host."""
    items = gr.manual_items(pack, spec)
    base = (pack.endpoints or {}).get("base_url", "").rstrip("/")
    for it in items:
        for scheme in ("https://", "http://"):
            bad = f"{base}/{scheme}"
            if it["url"].startswith(bad):
                it["url"] = scheme + it["url"][len(bad):]
    return items


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        vin = p.get("vin")
        if r.get("endpoint") == "vin_decode" and vin and len(str(vin)) > 17:
            probs.append(f"request {r.get('name')!r}: a VIN has at most 17 characters")
        y = p.get("modelYear")
        if y not in (None, "") and not str(y).isdigit():
            probs.append(f"request {r.get('name')!r}: modelYear must be a single year such as 2019 "
                         "(one request per year for a range)")
    return probs


VEHICLE = {"recalls_by_vehicle": ("r", "NHTSACampaignNumber"), "complaints_by_vehicle": ("c", "odiNumber")}


def _key(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


QUALIFIERS = {"HYBRID", "HEV", "PHEV", "PLUG-IN", "PLUGIN", "EV", "BEV", "ELECTRIC", "DIESEL", "GAS", "FFV", "CNG",
              "SEDAN", "COUPE", "HATCH", "HATCHBACK", "WAGON", "CONVERTIBLE", "VAN", "CAB", "CREW", "SUPER", "REGULAR",
              "DOUBLE", "EXTENDED", "QUAD", "ACCESS", "KING", "LONG", "SHORT", "BED", "AWD", "FWD", "RWD", "2WD", "4WD",
              "4X4", "4X2", "SWB", "LWB"}
_DOORS = re.compile(r"^\d-?(DR|DOOR)$")


def _qualifier_only(rest: str) -> bool:
    """True when the words after the base name only say powertrain, body, cab, door or drive (brackets count as
    a cab or body style)."""
    words = re.sub(r"\([^)]*\)", " ", rest).replace(",", " ").split()
    return bool(rest.strip()) and all(w in QUALIFIERS or _DOORS.match(w) for w in words)


def prefixed(base: str, listed: list[str]) -> tuple[list[str], list[str]]:
    """Listed names that start with the base (next character a space or "("), each once in listed order, split into
    (qualifier-only variants, other names)."""
    b, seen, variants, others = _key(base), {_key(base)}, [], []
    for n in listed:
        k = _key(n)
        if k in seen or not (k.startswith(b) and k[len(b):len(b) + 1] in (" ", "(")):
            continue
        seen.add(k)
        (variants if _qualifier_only(k[len(b):]) else others).append(n)
    return variants, others


def _forms(name: str, recalls: bool) -> list[str]:
    forms = [name]
    bare = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", name)).strip()
    if recalls and bare and _key(bare) != _key(name):
        forms.append(bare)
    return forms


def _models(c, pack, p, issue, state) -> list[str]:
    key = (_key(p["make"]), str(p["modelYear"]), issue)
    if key not in state.setdefault("models", {}):
        mep = pack.endpoints["endpoints"]["products_models"]
        try:
            listed, _ = gr.fetch_request(c, mep, {"modelYear": p["modelYear"], "make": p["make"], "issueType": issue})
        except DataError:
            listed = []                      # the list only suggests names: never fail a fetch for it
        state["models"][key] = [x.get("model", "") for x in listed]
    return state["models"][key]


def _vehicle(c, pack, r, ep, params, notes, state):
    issue, idcol = VEHICLE[r["endpoint"]]
    p = {k: v for k, v in params.items() if k != "variants"}
    variants, others = prefixed(p["model"], _models(c, pack, p, issue, state))
    want = str(params.get("variants", "")).strip().lower() in ("true", "1", "yes", "on")
    names = [p["model"]] + (variants if want else [])
    rows, seen, matched, unmatched, failed = [], set(), [], [], []
    for n in names:
        got, err = None, None
        for form in _forms(n, issue == "r"):
            try:
                got, _ = gr.fetch_request(c, ep, {**p, "model": form}, r.get("max_rows"))
                break
            except DataError as e:
                st = getattr(e, "status", None)
                if e.kind == "http" and st in (500, 502, 503, 504):
                    err = st
                elif not (e.kind == "bad_request" and st == 400):
                    raise
        if got is None:
            (failed if err else unmatched).append(f"{n} (HTTP {err})" if err else n)
            if err:
                state["5xx"] = DataError(f"nhtsa: HTTP {err} after retries for every vehicle lookup", kind="http",
                                         status=err)
            continue
        state["answered"] = True
        new = [x for x in got if not (x.get(idcol) and x.get(idcol) in seen)]
        seen.update(x.get(idcol) for x in got if x.get(idcol))
        for x in new:
            x["model_requested"] = n
        rows += new
        matched.append(f"{n} ({len(new)})")
    label = f"{p['make']} {p['model']} {p['modelYear']}"
    what = "campaign" if issue == "r" else "complaint"
    if len(names) > 1 and matched:
        notes.append(f"{r['name']}: {label} with its powertrain/body variants (variants: true); rows per name, each "
                     f"{what} once: {', '.join(matched)}" + (f"; no record under: {', '.join(unmatched)}" if unmatched else ""))
    if failed:
        notes.append(f"{r['name']}{gr.INCOMPLETE} NHTSA gave no answer for {', '.join(failed)} after retries (a 5xx "
                     "can be an outage or, for 502, a name with no record): those rows are missing. Fetch again "
                     "(--force) later.")
    elif not matched:
        notes.append(f"{r['name']}: no vehicle matched {label} (HTTP 400, Count 0): the name is spelled differently "
                     f"there or that model year has no records. Kept as an empty table; list the names with "
                     f"products_models (issueType {issue}) and fetch again.")
    rest = [n for n in (others + ([] if want else variants))]
    if rest:
        notes.append(f"{r['name']}: NHTSA also lists {', '.join(rest)} for {p['make']} {p['modelYear']}: separate "
                     f"model names with their own {what}s, not included. Add a request for each one the question "
                     "covers" + ("" if want or not variants else " (or \"variants\": true for the powertrain/body "
                                                                 "versions)") + ".")
    return normalize(r, ep, rows)


def fetch(spec, pack, route):
    """Vehicle lookups run here (variants, a non-matching name is a note); every other request goes through the
    generic fetch, so its paging and empty-result rules apply."""
    c = gr.client_for(pack)
    tables, notes, rest, state = {}, [], [], {}
    for r in spec["requests"]:
        if r.get("endpoint") not in VEHICLE:
            rest.append(r)
            continue
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        tables[r["name"]] = _vehicle(c, pack, r, ep, params, notes, state)
    if state.get("5xx") and not state.get("answered"):     # nothing answered: the source is down, not a name
        raise state["5xx"]
    if rest:
        t, n = gr.fetch(pack, {**spec, "requests": rest}, client=c)
        tables.update(t)
        notes += n
    return {r["name"]: tables[r["name"]] for r in spec["requests"] if r["name"] in tables}, notes

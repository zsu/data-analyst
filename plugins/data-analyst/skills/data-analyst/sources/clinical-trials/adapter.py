"""ClinicalTrials.gov API v2 adapter.

- Lists (filter.overallStatus, filter.ids, fields, sort) may be given as JSON lists; they are sent joined
  with "|" (the API accepts "|" or ",").
- phase / study_type convenience keys become aggFilters codes. Codes verified 2026-10-20 against the
  registry's field counts: phase 0 = EARLY_PHASE1, 1-4, NA = not applicable; studyType int / obs / exp.
  "early_phase1" and "not_applicable" match no study.
- Records are nested (protocolSection.identificationModule.nctId ...). normalize() renames every column
  to its shortest unique path suffix within the table (nctId, briefTitle, startDateStruct.date,
  leadSponsor.name; generic last parts such as name, count, type keep their parent); values are untouched. Lists of objects (interventions) stay JSON text.
- pageSize is capped at 1,000 by the API (larger values are silently reduced, verified); paging follows
  nextPageToken.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402

LISTS = ("filter.overallStatus", "filter.ids", "fields", "sort")


def _enums() -> dict:
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("enums", {})


def _as_list(v):
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [s for s in str(v).replace(",", "|").split("|") if s]


def before_request(req, ep, params):
    p = dict(params)
    for k in LISTS:
        if isinstance(p.get(k), list):
            p[k] = "|".join(str(x) for x in p[k])
    en = _enums()
    agg = [a for a in str(p.pop("aggFilters", "") or "").split(",") if a]
    ph = [en["phase"][x.upper()] for x in _as_list(p.pop("phase", None)) if x.upper() in en["phase"]]
    if ph:
        agg.append("phase:" + " ".join(ph))
    st = [en["studyType"][x.upper()] for x in _as_list(p.pop("study_type", None)) if x.upper() in en["studyType"]]
    if st:
        agg.append("studyType:" + " ".join(st))
    if agg:
        p["aggFilters"] = ",".join(agg)
    return p


def check_spec(spec):
    probs = []
    en = _enums()
    for r in spec.get("requests") or []:
        name = r.get("name")
        prm = r.get("params") or {}
        for x in _as_list(prm.get("phase")):
            if x.upper() not in en["phase"]:
                probs.append(f"request {name!r}: phase {x!r} is not one of {list(en['phase'])}")
        for x in _as_list(prm.get("study_type")):
            if x.upper() not in en["studyType"]:
                probs.append(f"request {name!r}: study_type {x!r} is not one of {list(en['studyType'])}")
        for x in _as_list(prm.get("filter.overallStatus")):
            if x not in en["overallStatus"]:
                probs.append(f"request {name!r}: status {x!r} is not one of {en['overallStatus']} (upper case)")
        if r.get("endpoint") == "field_values" and len(_as_list(prm.get("fields"))) > 1:
            probs.append(f"request {name!r}: field_values takes one field per request")
        if r.get("endpoint") in (None, "studies") and not any(k.startswith(("query.", "filter.")) or k in ("aggFilters", "phase", "study_type")
                                                              for k in prm):
            probs.append(f"request {name!r}: give a query.* search or a filter (the registry has 600,000+ studies)")
    return probs


GENERIC = {"name", "class", "count", "type", "date", "value", "id", "status", "role", "description", "title"}


def _short_names(keys: list[str]) -> dict:
    parts = {k: k.split(".") for k in keys}
    out = {}
    for k, segs in parts.items():
        start = 2 if segs[-1] in GENERIC and len(segs) > 1 else 1
        for n in range(start, len(segs) + 1):
            cand = ".".join(segs[-n:])
            if sum(1 for o in parts.values() if ".".join(o[-n:]) == cand) == 1:
                out[k] = cand
                break
        else:
            out[k] = k
    return out


def normalize(req, ep, rows):
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    m = _short_names(keys)
    return [{m[k]: v for k, v in r.items()} for r in rows]


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)          # core applies before_request to the links
    for it in items:
        if "/studies?" in it["url"]:
            it["note"] = ("First 1,000 studies only. For all of them, run the same search on https://clinicaltrials.gov/ "
                          "(Advanced Search) and use Download > CSV, then upload that file.")
    return items

"""NIH RePORTER API v2 adapter (POST /projects/search, /publications/search).

- Friendly spec keys are moved into the "criteria" object: text / phrase -> advanced_text_search, spending_categories
  names -> RCDC ids (table in endpoints.json "rcdc_categories"), everything else (fiscal_years, agencies,
  org_states, activity_codes, pi_names, ...) as given.
- exclude_subprojects defaults to true: a multi-project award (P01, U54, ...) is listed once as the parent
  and again for each subproject, and the subproject amounts are part of the parent's award_amount
  (NCI P01 awards FY2024: 105 parents sum to $187.4M; the 599 subproject rows add $187.4M of repeated
  money, verified 2026-10-22).
- include_fields defaults to a compact set (abstracts and terms are long); "all" sends none (every field).
- Paging: limit at most 500 (HTTP 400 above) and offset at most 14,999, with offset + limit capped at
  15,000 (offset 14,999 with limit 10 returns one record), so one search yields at most 15,000 records.
  /publications/search stops earlier: offset above 9,999 answers HTTP 400 ("System doesn't support offset
  value greater than 9,999"), so its endpoint max_rows is 10,000 (pages at offsets 0-9,500; verified
  2026-10-22). check_spec refuses searches with no criteria and max_rows above the endpoint's cap.
- Paging needs a stable order: without sort_field, RePORTER's order shifts between pages, so a 2,500-row
  search repeated 3 awards at the offset-2,500 boundary and skipped 3 others (count right, rows wrong;
  verified 2026-10-28). Project searches that may need more than one page (no max_rows, or above 500)
  therefore sort by appl_id (unique) unless the spec sorts; a cut at max_rows then keeps the lowest appl_ids
  and the note says so (truncated_note, added only when the request was sent with that sort). The core
  drops rows repeated across pages (unique_key) with a note that rows may be missing.
- include_fields: unknown names are dropped without an error; the organization block is "Organization"
  (sub-field names such as OrgName return nothing).
- Spending-category ids are easy to get wrong: id 27 is "Adolescent Sexual Activity" and 30 is
  "Agent Orange & Dioxin", not Cancer and Cardiovascular (Cancer is 132). Its spending summary also added
  only the top 50 award amounts. Sum award_amount over all rows instead.
"""
from __future__ import annotations

import difflib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402

TOP = {"criteria", "include_fields", "exclude_fields", "sort_field", "sort_order", "offset", "limit"}
DEFAULT_FIELDS = ["ApplId", "ProjectNum", "CoreProjectNum", "SubprojectId", "FiscalYear", "ProjectTitle",
                  "Organization", "ContactPiName", "AgencyIcAdmin", "AgencyIcFundings", "ActivityCode",
                  "FundingMechanism", "AwardAmount", "DirectCostAmt", "IndirectCostAmt", "ProjectStartDate",
                  "ProjectEndDate", "AwardNoticeDate", "IsActive", "SpendingCategoriesDesc", "CongDist",
                  "ProjectDetailUrl"]
NO_SUBPROJECT_KEYS = ("exclude_subprojects",)


def _eps() -> dict:
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json")


def rcdc_ids(values) -> tuple[list[int], list[str]]:
    """RCDC names or ids -> ids; returns (ids, unknown names)."""
    table = _eps().get("rcdc_categories", {})
    by_name = {v.lower(): int(k) for k, v in table.items()}
    ids, bad = [], []
    for v in values if isinstance(values, list) else [values]:
        if isinstance(v, int) or str(v).isdigit():
            ids.append(int(v))
        elif str(v).lower() in by_name:
            ids.append(by_name[str(v).lower()])
        else:
            bad.append(str(v))
    return ids, bad


def _projects(ep) -> bool:
    return ep.get("path", "").startswith("/projects")


def before_request(req, ep, params):
    p = dict(params)
    crit = dict(p.pop("criteria", None) or {})
    text, phrase = p.pop("text", None), p.pop("phrase", None)
    fields = p.pop("text_fields", None) or "projecttitle,terms,abstracttext"
    if phrase:      # exact phrase: RePORTER's advanced operator with the phrase quoted (a list: any of them)
        crit["advanced_text_search"] = {"operator": "advanced", "search_field": fields,
                                        "search_text": _phrase_query(phrase)}
    elif text:      # every word, anywhere in the fields, any order
        crit["advanced_text_search"] = {"operator": "and", "search_field": fields, "search_text": text}
    cats = p.pop("spending_categories", None)
    if cats:
        ids, _ = rcdc_ids(cats)
        crit["spending_categories"] = {"values": ids, "match_all": "false"}
    for k in list(p):
        if k not in TOP:
            crit[k] = p.pop(k)
    if _projects(ep):
        crit.setdefault("exclude_subprojects", True)
        one_page = req.get("max_rows") and int(req["max_rows"]) <= 500
        if not p.get("sort_field") and not one_page:   # several pages need a stable order, or pages repeat and skip
            p["sort_field"], p["sort_order"] = "appl_id", p.get("sort_order") or "asc"
        inc = p.get("include_fields")
        if inc in (None, "", []):
            p["include_fields"] = DEFAULT_FIELDS
        elif inc == "all":
            p.pop("include_fields")
    p["criteria"] = crit
    return p


def _phrase_query(phrase) -> str:
    items = phrase if isinstance(phrase, list) else [phrase]
    return " OR ".join('"' + str(x).replace('"', " ").strip() + '"' for x in items if str(x).strip())


def request_note(req, ep, params):
    """Say how a text search was matched: the same words as a phrase or anywhere give different rows (two-word
    topic, FY2024: 312 projects with every word anywhere, 301 with the exact phrase; verified 2026-10)."""
    p = req.get("params") or {}
    where = p.get("text_fields") or "title, terms and abstract"
    if p.get("phrase"):
        return (f"{req['name']}: phrase {p['phrase']!r} matched as an exact phrase in {where} (RePORTER operator "
                "advanced); `text` would match every word anywhere.")
    if p.get("text") and len(str(p["text"]).split()) > 1:
        return (f"{req['name']}: text {p['text']!r} matched every word anywhere in {where}, in any order (RePORTER "
                "operator and), so rows need not hold the phrase; use `phrase` for the exact phrase.")
    return None


def check_spec(spec):
    probs = []
    table = _eps().get("rcdc_categories", {})
    for r in spec.get("requests") or []:
        name = r.get("name")
        prm = dict(r.get("params") or {})
        crit = {**(prm.get("criteria") or {}), **{k: v for k, v in prm.items() if k not in TOP}}
        crit = {k: v for k, v in crit.items() if k not in NO_SUBPROJECT_KEYS + ("text_fields",) and v not in (None, "", [], {})}
        if not crit:
            probs.append(f"request {name!r}: give at least one criterion (fiscal_years, agencies, text, org_states, "
                         "...); one search returns at most 15,000 records")
        if prm.get("spending_categories"):
            _, bad = rcdc_ids(prm["spending_categories"])
            for b in bad:
                near = difflib.get_close_matches(b, list(table.values()), n=4, cutoff=0.5)
                probs.append(f"request {name!r}: RCDC category {b!r} is not known" +
                             (f"; close: {near}" if near else "; use --search to find names or pass the numeric id"))
        if prm.get("text") and prm.get("phrase"):
            probs.append(f"request {name!r}: give text (every word, anywhere) or phrase (exact phrase), not both")
        if isinstance(prm.get("fiscal_years"), (int, str)):
            probs.append(f"request {name!r}: fiscal_years must be a list, e.g. [2024]")
        cap = int((_eps().get("endpoints", {}).get(r.get("endpoint") or "", {}) or {}).get("max_rows") or 15000)
        if r.get("max_rows") and int(r["max_rows"]) > cap:
            how = ("split the project numbers or PMIDs into several requests" if cap < 15000
                   else "split by fiscal year, agency or state")
            probs.append(f"request {name!r}: RePORTER returns at most {cap:,} records per {r.get('endpoint')} search; "
                         f"{how}")
    return probs


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)          # core applies before_request to the links
    for it in items:
        it["note"] = ((it["note"].rstrip(".") + ". " if it.get("note") else "") + "POST request: run a download script, or use the RePORTER website "
                      "search (https://reporter.nih.gov/advanced-search) and Export, then upload the CSV.").strip()
    return items


def search(query, pack):
    """Search RCDC spending categories and agencies (offline tables in endpoints.json)."""
    words = [w for w in query.lower().split() if len(w) > 1]
    e = _eps()
    hits = []
    for k, v in e.get("rcdc_categories", {}).items():
        s = sum(v.lower().count(w) for w in words)
        if s:
            hits.append((s, {"rcdc_category": v, "id": int(k), "use": f'"spending_categories": ["{v}"]'}))
    for k, v in e.get("agencies", {}).items():
        s = sum(f"{k} {v}".lower().count(w) for w in words)
        if s:
            hits.append((s, {"agency": k, "name": v, "use": f'"agencies": ["{k}"]'}))
    out = [h for _, h in sorted(hits, key=lambda x: -x[0])]
    return out[:20] + ([{"note": "more than 20 matches; narrow the words"}] if len(out) > 20 else []) + \
        ([] if out else [{"note": "no RCDC category or agency matched; use text for free-text search"}])

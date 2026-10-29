"""Senate lobbying (LDA.gov) pack hooks.

- check_spec: the Filings and Contribution Reports endpoints refuse to page without at least one filter.
- before_request: issue_code is not an API filter (the LDA API has none); it is removed here and applied
  to the downloaded activities in normalize. Filter names that /clients/ and /registrants/ silently ignore
  (state, client_id, registrant_id) are sent under the names those endpoints filter on (ALIASES).
- normalize: filing_activities -> one row per lobbying activity; contributions -> one row per contribution
  item; filings get is_amendment / is_termination flags derived from filing_type (values unchanged).
  First, every list is cut to one row per filing (filing_uuid) or record (id): a filter on what a filing holds
  (its activities' issue text, lobbyists, foreign entities, contribution items) makes the API return the
  filing once per matching item (verified 2026-10-28: an issue-text filter gave 25 rows for 17 filings on one
  page), which would inflate counts and income/expense sums. fetch notes how many repeats were dropped.
"""
from __future__ import annotations

import json

from core import generic_rest as gr

PAGED_FILTERED = ("/filings/", "/contributions/")
NOT_FILTERS = {"page_size", "ordering", "issue_code"}


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") in ("filings", "filing_activities", "contributions"):
            if not any(k not in NOT_FILTERS and v not in (None, "") for k, v in (r.get("params") or {}).items()):
                probs.append(f"request {r.get('name')!r}: LDA needs at least one filter (filing_year, registrant_name, "
                             "client_name, ...) to page past the first 25 results")
    return probs


# filter names the list endpoints ignore without an error (every row comes back), mapped to the real ones
# (verified 2026-10-22: /clients/ filters on client_state and id, /registrants/ on id)
ALIASES = {"/clients/": {"state": "client_state", "client_id": "id"}, "/registrants/": {"registrant_id": "id"}}


def before_request(req, ep, params):
    out = {k: v for k, v in params.items() if k != "issue_code"}
    for old, new in ALIASES.get(ep.get("path", ""), {}).items():
        if old in out:
            v = out.pop(old)
            out.setdefault(new, v)
    return out


def _load(v):
    if isinstance(v, list):
        return v
    if not v:
        return []
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return []


def _flags(row: dict) -> dict:
    ft = str(row.get("filing_type", ""))
    row["is_amendment"] = ft == "RA" or (len(ft) > 1 and ft[1] in "A@")
    row["is_termination"] = "T" in ft or "@" in ft
    return row


def _names(lobbyists) -> str:
    out = []
    for l in lobbyists or []:
        p = (l or {}).get("lobbyist") or {}
        name = " ".join(x for x in (p.get("first_name"), p.get("middle_name"), p.get("last_name")) if x)
        out.append(name or str((l or {}).get("lobbyist_full_display_name") or ""))
    return "; ".join(n for n in out if n)


BASE = ("filing_uuid", "filing_type", "filing_type_display", "filing_year", "filing_period", "dt_posted",
        "registrant.id", "registrant.name", "client.id", "client.name", "client.client_id", "income", "expenses",
        "filing_document_url")


# filters on what a filing holds: the API joins to those items and returns the filing once per match
NESTED = ("filing_specific_lobbying_issues", "lobbyist_name", "lobbyist_covered_position", "foreign_entity_name",
          "foreign_entity_country", "affiliated_organization_name", "lobbyist_conviction_disclosure_indicator",
          "foreign_entity_listed_indicator", "contribution_contributor", "contribution_payee", "contribution_honoree",
          "contribution_type", "contribution_amount_min", "contribution_amount_max", "contribution_date_after",
          "contribution_date_before", "lobbyist_id")
UNIQUE = {"/filings/": "filing_uuid", "/contributions/": "filing_uuid", "/registrants/": "id", "/clients/": "id",
          "/lobbyists/": "id"}
_REPEATS: dict[str, list] = {}      # request (or for_each group) -> [rows the API sent, distinct records, key]


def one_per_record(req, ep, rows) -> list[dict]:
    """The rows with each filing (or registrant, client, lobbyist) once, in the API's order; the repeats dropped
    are counted for fetch's note."""
    col = UNIQUE.get(ep.get("path", ""))
    if not col or not rows or any(col not in r for r in rows):
        return rows
    seen, out = set(), []
    for r in rows:
        if r[col] not in seen:
            seen.add(r[col])
            out.append(r)
    if len(out) < len(rows):
        got = _REPEATS.setdefault(req.get("_group") or req.get("name") or "", [0, 0, col, []])
        got[0] += len(rows)
        got[1] += len(out)
        got[3] += [k for k in (req.get("params") or {}) if k in NESTED and k not in got[3]]
    return out


def repeat_notes() -> list[str]:
    out = []
    for name, (sent, kept, col, filters) in _REPEATS.items():
        why = f" (here: {', '.join(filters)})" if filters else ""
        unit = "filings" if col == "filing_uuid" else "records"
        out.append(f"{name}: the LDA API sent {sent:,} rows for {kept:,} distinct {unit}: a filter on what a filing "
                   f"holds{why} returns a filing once per matching activity, lobbyist or item. The {sent - kept:,} "
                   f"repeats were dropped (one row per {col} before any per-activity or per-item rows), so counts and "
                   "income/expense sums are per filing. The API's own count (and max_rows) counts the repeats too.")
    return out


def fetch(spec, pack, route):
    _REPEATS.clear()
    tables, notes = gr.fetch(pack, spec)
    notes += repeat_notes()
    _REPEATS.clear()
    return tables, notes


def normalize(req, ep, rows):
    rows = one_per_record(req, ep, rows)
    if ep.get("path") not in PAGED_FILTERED and not ep.get("path", "").startswith("/filings/{"):
        return rows
    name = req.get("endpoint")
    if name == "filing_activities":
        want = str((req.get("params") or {}).get("issue_code") or "").upper()
        out = []
        for r in rows:
            base = _flags({k: r.get(k, "") for k in BASE})
            for a in _load(r.get("lobbying_activities")):
                code = str(a.get("general_issue_code") or "").upper()
                if want and code != want:
                    continue
                ents = a.get("government_entities") or []
                out.append({**base, "general_issue_code": code,
                            "general_issue_code_display": a.get("general_issue_code_display", ""),
                            "description": a.get("description", ""),
                            "foreign_entity_issues": a.get("foreign_entity_issues", ""),
                            "lobbyist_count": len(a.get("lobbyists") or []), "lobbyists": _names(a.get("lobbyists")),
                            "government_entities": "; ".join(str((e or {}).get("name", "")) for e in ents)})
        return out
    if name == "contributions":
        out = []
        for r in rows:
            base = {k: r.get(k, "") for k in ("filing_uuid", "filing_type", "filing_year", "filing_period", "filer_type",
                                               "dt_posted", "registrant.id", "registrant.name", "lobbyist.id",
                                               "lobbyist.first_name", "lobbyist.last_name", "no_contributions",
                                               "filing_document_url")}
            items = _load(r.get("contribution_items"))
            if not items:
                out.append({**base, "contribution_type": "", "contributor_name": "", "payee_name": "", "honoree_name": "",
                            "amount": "", "date": ""})
            for it in items:
                out.append({**base, **{k: it.get(k, "") for k in ("contribution_type", "contributor_name", "payee_name",
                                                                   "honoree_name", "amount", "date")}})
        return out
    if name == "filings":
        return [_flags(dict(r)) for r in rows]
    return rows

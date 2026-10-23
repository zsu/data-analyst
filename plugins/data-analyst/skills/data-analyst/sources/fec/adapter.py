"""OpenFEC pack hooks.

- fetch: Schedules A, B and E page with keyset ("seek") pagination: each response's
  pagination.last_indexes (last_index plus the sort column's last value) goes into the next request. Other
  endpoints use the generic page paging.
- check_spec: itemized schedules need at least one narrowing filter (the unfiltered tables hold hundreds of
  millions of rows).
- manual_items: links carry the public DEMO_KEY; seek endpoints give the first page only.
- key_only_needs: DEMO_KEY allows only a few requests an hour.
"""
from __future__ import annotations

import json
import math
import os

from core import generic_rest as gr
from core import packfiles

HERE = os.path.dirname(os.path.abspath(__file__))
SEEK_FILTERS = {
    "schedule_a": ("committee_id", "contributor_name", "contributor_employer", "contributor_occupation",
                   "contributor_zip", "contributor_city"),
    "schedule_b": ("committee_id", "recipient_name", "recipient_committee_id"),
    "schedule_e": ("candidate_id", "committee_id"),
}
DEMO_LIMIT = 8


def _eps() -> dict:
    return packfiles.read_json(HERE, "endpoints.json")["endpoints"]


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        need = SEEK_FILTERS.get(r.get("endpoint") or "")
        if need and not any((r.get("params") or {}).get(k) for k in need):
            probs.append(f"request {r.get('name')!r}: {r['endpoint']} needs one of {', '.join(need)} "
                         "(the unfiltered table is far too large)")
    return probs


def _seek(client, ep: dict, params: dict, max_rows) -> tuple[list[dict], dict]:
    size = min(int(params.pop("per_page", None) or ep["paging"].get("size") or 100), 100)
    limit = min(int(max_rows or ep.get("max_rows") or 50000), int(ep.get("max_rows") or 50000))
    path, query, _ = gr.split(ep, params)
    q = {**query, "per_page": size}
    rows, meta = [], {"pages": 0, "total": None, "truncated": False}
    seen = set()
    while True:
        data = client.get(path, q).json()
        meta["pages"] += 1
        got = data.get("results") or []
        pag = data.get("pagination") or {}
        if meta["total"] is None and pag.get("count") is not None:
            meta["total"] = int(pag["count"])
        rows += got
        if len(rows) >= limit:
            meta["truncated"] = (meta["total"] or 0) > limit or len(got) >= size
            rows = rows[:limit]
            break
        last = pag.get("last_indexes") or {}
        key = json.dumps(last, sort_keys=True)
        if not got or len(got) < size or not last or key in seen:
            break
        seen.add(key)
        q.update({k: v for k, v in last.items() if v is not None})
    return gr._clean(rows, ep), meta


def fetch(spec, pack, route):
    tables, notes = {}, []
    client = None
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if (ep.get("paging") or {}).get("type") != "seek":
            t, n = gr.fetch(pack, {"requests": [r]})
            tables.update(t)
            notes += n
            continue
        client = client or gr.client_for(pack)
        rows, meta = _seek(client, ep, params, r.get("max_rows"))
        if meta["truncated"]:
            notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); FEC reports about "
                         f"{meta['total'] or 0:,} matching rows. Narrow the filter or raise max_rows.")
        if not rows and not r.get("allow_empty"):
            from core.http import DataError
            raise DataError(f"request {r['name']!r} returned no rows: check the ids, cycle and filters (see source.md)",
                            kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


def _calls(spec) -> int:
    eps = _eps()
    n = 0
    for r in spec.get("requests") or []:
        ep = eps.get(r.get("endpoint") or "") or {}
        pg = ep.get("paging") or {}
        size = int((r.get("params") or {}).get("per_page") or pg.get("size") or 100)
        default = ep.get("max_rows") if pg.get("type") == "seek" else size   # seek: no total known in advance
        n += max(1, math.ceil(int(r.get("max_rows") or default or size) / size))
    return n


def key_only_needs(spec):
    n = _calls(spec)
    if n > DEMO_LIMIT:
        return [f"about {n} API calls (the keyless DEMO_KEY allows only about 10 an hour on OpenFEC)"]
    return []


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        ep, _, probs = gr.resolve(pack, r)
        if probs:
            continue
        seek = (ep.get("paging") or {}).get("type") == "seek"
        rr = {**r, "max_rows": min(int(r.get("max_rows") or 100), 100)} if seek else r
        for it in gr.manual_items(pack, {"requests": [rr]}):
            it["url"] += ("&" if "?" in it["url"] else "?") + ("per_page=100&" if seek else "") + "api_key=DEMO_KEY"
            note = "DEMO_KEY is api.data.gov's public demo key (a few requests an hour); replace it with your own key"
            if seek:
                note = "first 100 rows only (keyset paging needs the script route for more); " + note
            it["note"] = (it.get("note", "") + "; " if it.get("note") else "") + note
            items.append(it)
    return items

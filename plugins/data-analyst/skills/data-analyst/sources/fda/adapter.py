"""openFDA adapter (api.fda.gov).

- Datasets live at /<category>/<noun>.json (drug/event, device/recall, ...). The spec names one with
  "dataset" (catalog alias such as drug_event, or the path "drug/event"); before_request splits it into the
  two path parts, so URLs keep a plain slash (a single path parameter would be sent as drug%2Fevent).
- openFDA answers a search that matches nothing with HTTP 404 {"error": {"code": "NOT_FOUND", "message":
  "No matches found!"}}; fetch() turns that into a clear "no records match" error, or an empty table when
  the request sets "allow_empty": true. Other 404 answers carry their own message (a count on a field that
  cannot be counted: "Nothing to count", e.g. drug/shortages status.exact); fetch() reads 404 bodies and
  reports openFDA's message for those instead of calling them "no match".
- Paging: limit <= 1000 and skip <= 25,000 (HTTP 400 "Skip value must 25000 or less", verified), so a search
  returns at most 26,000 records; the endpoint caps max_rows there. Count requests return at most 1,000
  terms (HTTP 400 above). Without a key, count limit=1000 answers HTTP 403 API_KEY_MISSING on some datasets
  (drug/event and device/event, even for a two-term field such as serious) while limit=999 works (verified
  2026-10-22), so fetch() sends keyless counts with limit at most 999. A date-field count ignores limit and
  returns one row per date. A term count that comes back as long as the limit is reported as cut off.
- Errors inside a body ({"error": {...}}) are reported with openFDA's code and message.
- The key (DATA_GOV_API_KEY, optional) is sent as api_key; without one openFDA allows 1,000 requests a day
  per IP (documented), with one 120,000. The authentication page says a key is required, but keyless calls
  work (verified 2026-10-22), so the key stays optional.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import packfiles  # noqa: E402
from core.http import DataError  # noqa: E402

MAX_SKIP = 25000
COUNT_MAX = 1000                   # count terms with a key
COUNT_MAX_KEYLESS = 999            # without a key, limit=1000 is refused (HTTP 403) on drug/event and device/event
NO_MATCH = "No matches found"


def _catalog() -> dict:
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("catalog", {})


def _paths() -> set[str]:
    return {c["id"] for c in _catalog().values()}


def check_error(status, body):
    if body[:1] == b"{" and b'"error"' in body[:300]:
        try:
            err = json.loads(body.decode("utf-8", "replace")).get("error") or {}
        except ValueError:
            return None
        if err.get("message"):
            return f"{err.get('code', 'FDA_ERROR')}: {err['message']}"
    return None


def before_request(req, ep, params):
    p = dict(params)
    ds = str(p.pop("dataset", "") or "").strip("/")
    if ds.endswith(".json"):
        ds = ds[:-5]
    cat = _catalog()
    if ds in cat:
        ds = cat[ds]["id"]
    if "/" in ds:
        p["category"], p["noun"] = ds.split("/", 1)
    s = p.get("search")
    if isinstance(s, str):                       # the "+AND+" style also works: make it plain spaces
        p["search"] = s.replace("+AND+", " AND ").replace("+OR+", " OR ").replace("+NOT+", " NOT ")
    return p


def check_spec(spec):
    probs = []
    cat, paths = _catalog(), _paths()
    for r in spec.get("requests") or []:
        name = r.get("name")
        prm = r.get("params") or {}
        ds = str(prm.get("dataset", r.get("dataset")) or "").strip("/").removesuffix(".json")
        if not ds:
            probs.append(f"request {name!r}: parameter 'dataset' is required (alias such as drug_event, or drug/event)")
        elif ds not in cat and ds not in paths:
            probs.append(f"request {name!r}: dataset {ds!r} is not an openFDA dataset (aliases: {', '.join(sorted(cat))})")
        if r.get("endpoint") == "count":
            try:
                if int(prm.get("limit", 1000)) > 1000:
                    probs.append(f"request {name!r}: count limit cannot exceed 1000")
            except (TypeError, ValueError):
                probs.append(f"request {name!r}: limit must be a number")
        if r.get("endpoint") == "search" and int(prm.get("skip", 0) or 0) > MAX_SKIP:
            probs.append(f"request {name!r}: skip cannot exceed {MAX_SKIP} (openFDA limit); narrow the search, "
                         "e.g. by date range, and make several requests")
    return probs


class _Read404:
    """The core client raises a bare "not found" on HTTP 404 and drops openFDA's message; this wrapper asks it to
    hand 404 bodies to check_error, so the error carries openFDA's own code and message."""

    def __init__(self, client):
        self._c = client

    def __getattr__(self, k):
        return getattr(self._c, k)

    def request(self, *a, **kw):
        kw.setdefault("allow_404", True)
        r = self._c.request(*a, **kw)
        if r.status == 404:                      # a 404 without an openFDA error body
            raise DataError(f"fda: not found (HTTP 404) {r.url.split('?')[0]}", kind="not_found", status=404)
        return r


def count_limit(req: dict, keyed: bool) -> int:
    """The limit a count request is sent with: the spec's (default 1000), at most 999 without a key."""
    try:
        lim = int((req.get("params") or {}).get("limit", COUNT_MAX))
    except (TypeError, ValueError):
        lim = COUNT_MAX
    return lim if keyed else min(lim, COUNT_MAX_KEYLESS)


def fetch(spec, pack, route):
    client = gr.client_for(pack)
    keyed = bool(client.key)
    wrapped = _Read404(client)
    tables, notes = {}, []
    for r in spec["requests"]:
        sent = None
        if r.get("endpoint") == "count":
            sent = count_limit(r, keyed)
            r = {**r, "params": {**(r.get("params") or {}), "limit": sent}}
        try:
            t, n = gr.fetch(pack, {**spec, "requests": [r]}, wrapped)
        except DataError as e:
            if e.status == 404 and NO_MATCH not in str(e) and e.kind != "not_found":
                hint = (" The field cannot be counted in this dataset: check its name, and try it without (or with) "
                        ".exact." if r.get("endpoint") == "count" and "Nothing to count" in str(e) else "")
                raise DataError(f"request {r['name']!r}: openFDA answered HTTP 404: "
                                f"{str(e).split('fda: ', 1)[-1]}.{hint}", kind="bad_request") from None
            if e.kind == "not_found" or e.status == 404:
                if r.get("allow_empty"):
                    tables[r["name"]] = []
                    notes.append(f"{r['name']}: openFDA found no matching records (empty table kept: allow_empty).")
                    continue
                raise DataError(f"request {r['name']!r}: openFDA found no records matching the search (HTTP 404 "
                                "'No matches found'). Check field names and values; .exact fields need the whole term "
                                "exactly as stored (drug names are upper-case).", kind="bad_request")
            raise
        rows = t.get(r["name"]) or []
        if sent is not None and sent >= COUNT_MAX_KEYLESS and len(rows) >= sent and "term" in rows[0]:
            notes.append(f"{r['name']}: stopped at {len(rows):,} terms (openFDA's count limit"
                         + ("" if keyed else "; 999 without a key") + "); the least frequent terms are cut off. "
                         "Narrow the search to rank within a subset.")
        tables.update(t)
        notes += n
    return tables, notes


def manual_items(spec, pack):
    # the links are opened without a key, so count links ask for at most 999 terms
    reqs = [{**r, "params": {**(r.get("params") or {}), "limit": count_limit(r, False)}}
            if r.get("endpoint") == "count" else r for r in spec.get("requests") or []]
    items = gr.manual_items(pack, {**spec, "requests": reqs})   # core applies before_request to the links
    for it in items:
        it["note"] = ((it["note"].rstrip(".") + ". " if it.get("note") else "") + "Without a key openFDA allows 1,000 requests a day per IP; whole "
                      "datasets are at https://open.fda.gov/data/downloads/ (zipped JSON).").strip()
    return items

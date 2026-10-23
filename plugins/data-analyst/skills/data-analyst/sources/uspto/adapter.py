"""USPTO ODP adapter: manual items say that the key goes in a header (browser links cannot send it), and
spec checks for the ODP query conventions."""
from __future__ import annotations

from core import generic_rest as gr

HEADER_NOTE = ("needs USPTO_API_KEY in the X-API-KEY header: use download_data.sh / .ps1 (a browser link "
               "alone answers 401 Unauthorized)")


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)
    for it in items:
        it["note"] = (it.get("note") + "; " if it.get("note") else "") + HEADER_NOTE
    return items


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        n = p.get("applicationNumberText")
        if n is not None and not str(n).isdigit():
            probs.append(f"request {r.get('name')!r}: applicationNumberText is digits only (14412875, not 14/412,875); "
                         "PCT and other forms: search with q=applicationNumberText:...")
        for k in ("filters", "rangeFilters"):
            v = p.get(k)
            if v and " " not in str(v).strip():
                probs.append(f"request {r.get('name')!r}: {k} is 'field value' with a space, e.g. "
                             + ("'applicationMetaData.applicationTypeCode UTL'" if k == "filters"
                                else "'applicationMetaData.grantDate 2020-01-01:2020-12-31'"))
        if p.get("limit") or p.get("offset"):
            probs.append(f"request {r.get('name')!r}: do not set limit/offset; paging is automatic (use max_rows)")
    return probs

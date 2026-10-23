"""BTS adapter: --search also finds Monthly Transportation Statistics columns (130 indicators in one dataset).

endpoints.json holds "monthly_stats_fields" {field name: published column title}, taken from the
data.bts.gov metadata of crem-w557, so "fetch.py --source bts --search 'transit ridership'" names the
right column (system_use_transit_ridership is urban rail only; buses are system_use_transit_ridership_1).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402


def search(query, pack, limit: int = 20):
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    hits = []
    for field, title in ((pack.endpoints or {}).get("monthly_stats_fields") or {}).items():
        text = f"{field} {title}".lower()
        s = sum(text.count(w) for w in words)
        if s:
            hits.append((s, {"dataset": "monthly_stats", "column": field, "title": title}))
    ranked = [h for _, h in sorted(hits, key=lambda x: -x[0])]
    out = gr.search_catalog(pack, query, limit) + ranked
    note = [] if len(out) <= limit else [{"note": f"{len(out)} matches; showing {limit}. Narrow the words."}]
    return out[:limit] + note

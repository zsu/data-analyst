"""Regulations.gov pack hooks.

- normalize: JSON:API rows come as id/type/attributes.*; drop the "attributes." prefix from column names
  (values unchanged) so tables read naturally.
- manual_items: links carry api_key=DEMO_KEY, which works in any browser at low limits (api.data.gov's
  public demo key); the user may replace it with their own key.
- key_only_needs: DEMO_KEY allows only about 10 requests an hour on this API, so large requests need a key.
"""
from __future__ import annotations

import math
import os

from core import generic_rest as gr
from core import packfiles

DEMO_LIMIT = 8


def normalize(req, ep, rows):
    if ep.get("records") == "meta":                  # comment_count: the filters asked for and the total
        filters = {k: v for k, v in (req.get("params") or {}).items() if k.startswith("filter[")}
        return [{**filters, "totalElements": r.get("totalElements", "")} for r in rows]
    out = []
    for r in rows:
        n = {}
        for k, v in r.items():
            k2 = k[len("attributes."):] if k.startswith("attributes.") else k
            if k2 in n and k2 != k:
                k2 = k
            n[k2] = v
        out.append(n)
    return out


def _calls(spec) -> int:
    eps = packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json")["endpoints"]
    n = 0
    for r in spec.get("requests") or []:
        ep = eps.get(r.get("endpoint") or "", {})
        pg = ep.get("paging") or {}
        if pg.get("type") == "page" and r.get("max_rows"):
            n += max(1, math.ceil(int(r["max_rows"]) / int(pg.get("size") or 250)))
        else:
            n += 1
    return n


def key_only_needs(spec):
    n = _calls(spec)
    if n > DEMO_LIMIT:
        return [f"about {n} API calls (the keyless DEMO_KEY allows about 10 an hour on Regulations.gov)"]
    return []


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)
    for it in items:
        it["url"] += ("&" if "?" in it["url"] else "?") + "api_key=DEMO_KEY"
        it["note"] = (it.get("note", "") + "; " if it.get("note") else "") + \
            "DEMO_KEY is api.data.gov's public demo key (about 10 requests an hour); replace it with your own key for more"
    return items

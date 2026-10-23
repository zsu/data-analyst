"""World Bank adapter: errors inside HTTP 200, country groups, aggregate flag, indicator search.

- check_error: the API answers HTTP 200 with [{"message": [{"id", "key", "value"}]}] for an unknown or
  archived indicator ("The indicator was not found. It may have been deleted or archived.") or a bad
  parameter; that becomes an error instead of an empty table.
- before_request: "country" takes a list, lower case, or a group alias from endpoints.json "groups"
  (g7, g20_countries, brics); "indicator" takes a code, a catalog alias, or a list of them (or the request's
  "dataset"). The pack resolves aliases itself (the param is not a core "catalog" param, whose lookup cannot
  take a list). Several indicators go ';'-joined in one call, which the API accepts only with a source
  database id: source=2 (WDI) unless the spec sets one.
- check_spec: each indicator request needs an indicator (or dataset), and every list item must be a code.
- normalize: values stay as published (null -> empty). Adds `is_aggregate` (Y/N) from the list of
  regional and income aggregates (region.id "NA" in /country), so sums over "all" do not double count.
- search: the catalog first, then (when the API answers) the ~1,500 World Development Indicators by
  name, code and definition.
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def _eps() -> dict:
    from core import packfiles
    return packfiles.read_json(HERE, "endpoints.json")


def check_error(status, body: bytes):
    if body[:2] != b'[{' or b'"message"' not in body[:200]:
        return None
    try:
        data = json.loads(body.decode("utf-8-sig"))
        msgs = data[0].get("message") or []
        text = "; ".join(f"{m.get('key', '')}: {m.get('value', '')} (id {m.get('id', '')})" for m in msgs)
    except (ValueError, AttributeError, IndexError, TypeError):
        return None
    return f"World Bank API error: {text}. Check the indicator code (fetch.py --source world-bank --search ...) and the country codes."


WDI_SOURCE = "2"


def _indicator_list(req, params) -> list:
    v = params.get("indicator")
    if v in (None, "", []):
        v = req.get("dataset")
    if v in (None, "", []):
        return []
    items = v if isinstance(v, (list, tuple)) else str(v).split(";")
    return [x.strip() if isinstance(x, str) else x for x in items if not (isinstance(x, str) and not x.strip())]


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint", "indicator" if r.get("dataset") else None) not in ("indicator", "indicator_info"):
            continue
        items = _indicator_list(r, r.get("params") or {})
        if not items:
            probs.append(f"request {r.get('name')!r}: parameter 'indicator' is required (a code such as "
                         "NY.GDP.PCAP.CD, a catalog alias, or a list of them)")
        elif any(not isinstance(x, str) for x in items):
            probs.append(f"request {r.get('name')!r}: 'indicator' must be a code, an alias or a list of them "
                         f"(text), not {items!r}")
        elif r.get("endpoint") == "indicator_info" and len(items) > 1:
            probs.append(f"request {r.get('name')!r}: indicator_info takes one indicator; use one request each")
    return probs


def before_request(req, ep, params):
    groups = _eps().get("groups", {})
    c = params.get("country")
    if c is not None:
        if isinstance(c, (list, tuple)):
            c = ";".join(str(x) for x in c)
        c = str(c).strip()
        c = groups.get(c.lower(), c)
        params["country"] = c if c.lower() == "all" else c.upper().replace(",", ";")
    if "{indicator}" in ep.get("path", ""):
        cat = _eps().get("catalog", {})
        codes = [cat[x]["id"] if x in cat else x for x in _indicator_list(req, params)]
        params["indicator"] = ";".join(codes)
        if len(codes) > 1 and params.get("source") in (None, ""):
            params["source"] = WDI_SOURCE            # the API refuses several indicators without a source id
    return params


def normalize(req, ep, rows):
    if not rows or "countryiso3code" not in rows[0]:
        return rows
    agg = set(_eps().get("aggregates_iso3", []))
    for r in rows:
        r["is_aggregate"] = "Y" if r.get("countryiso3code") in agg else "N"
    return rows


def search(query, pack):
    from core import generic_rest as gr
    from core.http import DataError
    words = [w for w in re.findall(r"[a-z0-9.]+", query.lower()) if len(w) > 1]
    scored = []
    for h in gr.search_catalog(pack, query, limit=200):
        text = " ".join(str(v) for v in h.values()).lower()
        n = sum(1 for w in words if w in text)
        if "alias" in h and n >= max(1, len(words) - 1):
            scored.append((n, h))
    hits = [h for _, h in sorted(scored, key=lambda x: -x[0])]
    try:
        c = gr.client_for(pack)
        data = c.get_json("/source/2/indicator", {"format": "json", "per_page": 2000})
        live = []
        for i in (data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []):
            name, iid, note = (i.get("name") or ""), (i.get("id") or ""), (i.get("sourceNote") or "")
            s = sum(3 * name.lower().count(w) + 3 * iid.lower().count(w) + note.lower().count(w) for w in words)
            if s and all(w in f"{name} {iid} {note}".lower() for w in words):
                live.append((s, {"id": iid, "name": name, "source": "World Development Indicators",
                                 "unit": i.get("unit") or ""}))
        known = {h.get("id") for h in hits}
        hits += [h for _, h in sorted(live, key=lambda x: -x[0]) if h["id"] not in known]
    except (DataError, ValueError) as e:
        hits.append({"note": f"live indicator search unavailable ({getattr(e, 'kind', 'error')}); "
                             "catalog results only. Browse https://data.worldbank.org/indicator"})
    out = hits[:20]
    if len(hits) > 20:
        out.append({"note": f"{len(hits) - 20} more results: narrow the words"})
    return out


def manual_items(spec, pack):
    """The generic links, after the same name resolution the fetch applies (group aliases, lists)."""
    import copy
    from core import generic_rest as gr
    spec2 = copy.deepcopy(spec)
    for r in spec2.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if not probs:
            r["params"] = before_request(r, ep, dict(params))
    return gr.manual_items(pack, spec2)

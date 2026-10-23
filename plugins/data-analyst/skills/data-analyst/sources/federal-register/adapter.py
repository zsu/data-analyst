"""Federal Register pack hooks.

- normalize: facet counts arrive as one object keyed by group; they become rows. Document lists are
  de-duplicated on document_number: page-number paging over a non-unique order (order=oldest, many
  documents per date) can repeat a document on two pages and skip another (2 of 10,000 on 2026-10-22).
"""
from __future__ import annotations


def _dedupe(rows):
    seen, out = set(), []
    for r in rows:
        k = r.get("document_number")
        if k not in (None, ""):
            if k in seen:
                continue
            seen.add(k)
        out.append(r)
    return out


def normalize(req, ep, rows):
    if ep.get("path", "") == "/documents.json":
        return _dedupe(rows)
    if not ep.get("path", "").startswith("/documents/facets/"):
        return rows
    out = []
    for r in rows:                                   # one flattened row: {"2025-01-01.count": 5, "2025-01-01.name": "2025"}
        groups: dict = {}
        for k, v in r.items():
            key, _, field = str(k).rpartition(".")
            if not key:
                continue
            groups.setdefault(key, {})[field] = v
        for key, g in groups.items():
            out.append({"key": key, "name": g.get("name", ""), "count": g.get("count", "")})
    return out

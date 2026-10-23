"""FDIC BankFind adapter.

- Each record arrives as {"data": {...fields...}, "score": n, "highlight": {...}}; normalize() unwraps it
  so columns are the FDIC field names (CERT, NAME, ASSET, ...), not "data.CERT".
- Dates come in three shapes: FAILDATE "7/17/2026" (M/D/YYYY), institutions REPDTE "06/30/2026",
  financials REPDTE "20260630", history EFFDATE "2010-03-20T00:00:00". The published column is kept and
  an <col>_iso column (YYYY-MM-DD) is added.
- Manual links ask for format=csv so a browser saves a spreadsheet; parse_upload reads either form.
- check_error: FDIC answers a bad parameter with HTTP 400 {"errors": [{"detail": ...}]}; the detail is
  surfaced by the client already, so only an "errors" list inside HTTP 200 is caught here.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402

DATE_COLS = ("FAILDATE", "REPDTE", "EFFDATE", "ESTYMD", "ENDEFYMD", "PROCDATE", "RUNDATE", "DATEUPDT", "RISDATE")


def iso(v) -> str:
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})(T.*)?$", s)
    return m.group(1) if m else ""


def check_error(status, body):
    if body[:1] == b"{" and b'"errors"' in body[:200]:
        try:
            errs = json.loads(body.decode("utf-8", "replace")).get("errors") or []
            return "; ".join(str(e.get("detail") or e.get("title") or e) for e in errs) or None
        except ValueError:
            return None
    return None


def normalize(req, ep, rows):
    out = []
    for r in rows:
        new = {}
        for k, v in r.items():
            if k == "score" or k.startswith("highlight."):
                continue
            new[k[5:] if k.startswith("data.") else k] = v
        for c in DATE_COLS:
            if c in new and new[c] not in ("", None):
                new[c + "_iso"] = iso(new[c])
        out.append(new)
    return out


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)
    for it in items:
        parts = urllib.parse.urlsplit(it["url"])
        q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + [("format", "csv")]
        it["url"] = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
        it["filename"] = re.sub(r"\.json$", ".csv", it["filename"])
    return items


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        if not p.get("fields") and r.get("endpoint") in ("financials", "institutions", "demographics"):
            probs.append(f"request {r.get('name')!r}: set params.fields (e.g. 'CERT,NAME,REPDTE,ASSET,DEP'); "
                         "without it every row carries hundreds of columns")
    return probs


def search(query, pack, limit: int = 20):
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    hits = []
    for f, d in ((pack.endpoints or {}).get("fields") or {}).items():
        s = sum(f"{f} {d}".lower().count(w) for w in words)
        if s:
            hits.append((s, {"field": f, "description": d}))
    out = gr.search_catalog(pack, query, limit) + [h for _, h in sorted(hits, key=lambda x: -x[0])]
    return out[:limit]

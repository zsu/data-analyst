"""DOJ News adapter (justice.gov API v1).

- since/until (pack options, never sent): the API filters only by one day, so a date range pages through
  the list newest first and stops once dates fall before `since` (at most MAX_PAGES pages).
- normalize: the published values stay; date_iso (UTC calendar day of the bare Unix timestamp `date`, which
  is the Eastern release day: releases are stamped at Eastern midnight or later) and created_iso /
  changed_iso are added. `created`/`changed` arrive as HTML (<time datetime="2009-01-05T14:20:07-05:00">
  1231183207</time>, verified live 2026-10-22): their day is the date in the datetime attribute (Eastern
  time), else the UTC day of the digits. Title, component and topic names are HTML-unescaped (&#039; -> ');
  component_names / topic_names and teaser_text / body_text (HTML tags removed) are added. since/until are
  applied here too, so uploaded files are filtered the same way.
"""
from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime, timezone

from core import generic_rest as gr

MAX_PAGES = 600          # 30,000 records scanned per request at most
# The archive holds U.S. Attorney's Office releases only from January 2013 (verified live 2026-10: a title
# search sorted oldest first shows Main Justice components only through December 2012); before then every
# count covers Main Justice components alone.
USAO_FROM = "2013-01-01"
PACK_OPTS = ("since", "until")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DT_ATTR = re.compile(r"""datetime\s*=\s*["']?(\d{4}-\d{2}-\d{2})""")


def _iso(ts) -> str:
    """YYYY-MM-DD from a bare Unix timestamp, or from <time datetime="...">digits</time> (the attribute's
    own calendar day when present, else the UTC day of the digits inside the tag)."""
    s = str(ts or "").strip()
    if "<" in s:
        m = _DT_ATTR.search(s)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                pass
        s = re.sub(r"<[^>]*>", " ", s).strip()
    if not s.lstrip("-").isdigit():
        return ""
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _text(h) -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", str(h or ""), flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\u00a0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def _names(v) -> str:
    if not v:
        return ""
    try:
        items = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return str(v)
    if isinstance(items, list):
        return "; ".join(html.unescape(str(i.get("name", "")) if isinstance(i, dict) else str(i)) for i in items)
    return html.unescape(str(items))


def normalize(req, ep, rows):
    p = req.get("params") or {}
    since, until = p.get("since") or "", p.get("until") or ""
    out = []
    for r in rows:
        if isinstance(r.get("title"), str):
            r["title"] = html.unescape(r["title"])        # the API sends entities (&#039;) in titles
        for col in ("date", "created", "changed"):
            if col in r:
                r[col + "_iso"] = _iso(r[col])
        for col in ("component", "topic"):
            if col in r:
                r[col + "_names"] = _names(r[col])
        for col in ("teaser", "body"):
            if col in r:
                r[col + "_text"] = _text(r[col])
        d = r.get("date_iso", "")
        if (since or until) and d and ((since and d < since) or (until and d > until)):
            continue
        out.append(r)
    return out


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        for k in PACK_OPTS:
            if p.get(k) and not _DAY.match(str(p[k])):
                probs.append(f"request {r.get('name')!r}: {k} must be YYYY-MM-DD")
        if (p.get("since") or p.get("until")) and (p.get("sort", "date") != "date" or p.get("direction", "DESC") != "DESC"):
            probs.append(f"request {r.get('name')!r}: since/until need sort=date and direction=DESC (the defaults)")
        if p.get("since") and p.get("until") and str(p["since"]) > str(p["until"]):
            probs.append(f"request {r.get('name')!r}: since is after until")
        for k in ("page", "pagesize"):
            if k in p:
                probs.append(f"request {r.get('name')!r}: do not set {k}; paging is automatic (use max_rows)")
    return probs


def _strip(params: dict) -> dict:
    return {k: v for k, v in params.items() if k not in PACK_OPTS}


def fetch(spec, pack, route):
    c = gr.client_for(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise gr.DataError("; ".join(probs), kind="bad_request")
        since = params.get("since") or ""
        limit = int(r.get("max_rows") or ep.get("max_rows") or 20000)
        pg = ep.get("paging") or {"type": "none"}
        if pg.get("type") != "page":
            rows, meta = gr.fetch_request(c, ep, _strip(params), limit)
            rows = normalize(r, ep, rows)
        else:
            path, query, _ = gr.split(ep, _strip(params))
            size, page, rows, total, stopped = int(pg["size"]), int(pg.get("start", 0)), [], None, None
            while True:
                resp = c.request("GET", path, {**query, pg["page_param"]: page, pg["size_param"]: size})
                data = resp.json()
                got = gr.dig(data, ep.get("records", "results")) or []
                try:
                    total = int(gr.dig(data, ep.get("total", "")) or 0)
                except (TypeError, ValueError):
                    total = None
                kept = normalize(r, ep, gr._clean(got, ep))
                rows += kept
                oldest = min((_iso(g.get("date")) for g in got if _iso(g.get("date"))), default="")
                if len(rows) >= limit:
                    stopped = "max_rows"
                    rows = rows[:limit]
                    break
                if len(got) < size or (since and oldest and oldest < since):
                    break
                page += 1
                if page - int(pg.get("start", 0)) >= MAX_PAGES:
                    stopped = "pages"
                    break
            if stopped == "max_rows":
                notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); the source has more"
                             + (f" (the API reports {total:,} records for the title/day filter)" if total else "")
                             + ". Narrow the filter or raise max_rows.")
            elif stopped == "pages":
                notes.append(f"{r['name']}: stopped after scanning {MAX_PAGES * size:,} records newest first; "
                             "older matches were not reached. Narrow the title filter or the date range.")
        if not rows and not r.get("allow_empty"):
            raise gr.DataError(f"request {r['name']!r} returned no rows: check the title words and dates "
                               "(see source.md)", kind="bad_request")
        tables[r["name"]] = rows
        note = coverage_note(r["name"], rows)
        if note:
            notes.append(note)
    return tables, notes


def coverage_note(name: str, rows: list[dict]) -> str:
    """A note when rows predate USAO_FROM: counts across that date compare different sets of offices."""
    days = [x.get("date_iso") or "" for x in rows]
    early = sum(1 for d in days if d and d < USAO_FROM)
    if not early:
        return ""
    return (f"{name}: {early:,} row(s) are dated before {USAO_FROM}, when the justice.gov archive held no U.S. "
            "Attorney's Office releases (Main Justice components only). Counts before and after that date are "
            "not comparable (and from 2013 one announcement is often posted by both): start a trend in 2013, "
            "or count releases listing a Main Justice component (component_names not starting 'USAO').")


def manual_items(spec, pack):
    s = copy.deepcopy(spec)
    ranged = set()
    for r in s.get("requests") or []:
        p = r.get("params") or {}
        if any(k in p for k in PACK_OPTS):
            ranged.add(r.get("name"))
        r["params"] = _strip(p)
    items = gr.manual_items(pack, s)
    for it in items:
        if it["request"] in ranged:
            it["note"] = ((it["note"] + "; ") if it["note"] else "") + \
                "newest first; the date range (since/until) is applied when the files are ingested"
    return items

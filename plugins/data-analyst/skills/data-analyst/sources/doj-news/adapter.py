"""DOJ News adapter (justice.gov API v1).

- since/until (pack options, never sent): the API filters only by one day, so a date range pages through
  the list newest posted first and stops CREATED_MARGIN_DAYS of postings past `since` (at most MAX_PAGES pages).
- Paging order: `date` is a day stamp shared by about 100 releases a day, and the API does not keep ties in a
  stable order, so paging by sort=date repeats some releases at page boundaries and skips as many (verified
  live 2026-10: 30 pages unfiltered held 379 repeats; sort=created held 1, a release posted meanwhile). So
  sort=date (the default) pages by `created` (posting time to the second) and the rows are then put in date
  order; `since` reads CREATED_MARGIN_DAYS past it, since a release can be posted days before its date (no
  margin without `since`). A max_rows cut keeps the newest posted rows, and its note says so.
  Repeated uuids are dropped with a note.
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
CREATED_MARGIN_DAYS = 14  # posting time seen up to 11 days before the release date (live sample, 2026-10)
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


def _page_by_created(query: dict) -> tuple[bool, bool]:
    """sort=date (shared day stamps, unstable ties) is sent as sort=created, a near-unique posting time, with
    `created` added to fields when missing. Returns (date order wanted, created was added)."""
    if str(query.get("sort", "date")) != "date":
        return False, False
    query["sort"] = "created"
    fields = [f.strip() for f in str(query.get("fields") or "").split(",") if f.strip()]
    if fields and "created" not in fields:
        query["fields"] = ",".join(fields + ["created"])
        return True, True
    return True, False


def _ts(v) -> int:
    """Unix seconds from a bare timestamp or from <time ...>digits</time>; 0 when absent."""
    m = re.search(r"(-?\d{6,})\s*(?:</time>|$)", str(v or "").strip())
    return int(m.group(1)) if m else 0


def _minus_days(day: str, n: int) -> str:
    from datetime import timedelta
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d")


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
            by_date, added = _page_by_created(query)
            cutoff = _minus_days(since, CREATED_MARGIN_DAYS) if (since and by_date) else since
            size, page, rows, total, stopped = int(pg["size"]), int(pg.get("start", 0)), [], None, None
            seen, repeats = set(), 0
            while True:
                resp = c.request("GET", path, {**query, pg["page_param"]: page, pg["size_param"]: size})
                data = resp.json()
                got = gr.dig(data, ep.get("records", "results")) or []
                try:
                    total = int(gr.dig(data, ep.get("total", "")) or 0)
                except (TypeError, ValueError):
                    total = None
                for x in normalize(r, ep, gr._clean(got, ep)):
                    u = x.get("uuid")
                    if u and u in seen:
                        repeats += 1
                        continue
                    seen.add(u)
                    rows.append(x)
                order_col = "created" if by_date else "date"
                oldest = min((_iso(g.get(order_col)) for g in got if _iso(g.get(order_col))), default="")
                if len(rows) >= limit:
                    stopped = "max_rows"
                    rows = rows[:limit]
                    break
                if len(got) < size or (cutoff and oldest and oldest < cutoff):
                    break
                page += 1
                if page - int(pg.get("start", 0)) >= MAX_PAGES:
                    stopped = "pages"
                    break
            if by_date:
                rows.sort(key=lambda x: (_ts(x.get("date")), _ts(x.get("created"))),
                          reverse=str(query.get("direction", "DESC")).upper() != "ASC")
                for x in rows if added else []:
                    x.pop("created", None)
                    x.pop("created_iso", None)
            if repeats:
                notes.append(f"{r['name']}: {repeats:,} release(s) came back twice while paging (a release posted "
                             "or removed during the fetch shifts the pages); the repeats were dropped, and about as "
                             "many may be missing. Fetch again (--force) if the count matters.")
            if stopped == "max_rows":
                notes.append(f"{r['name']}: stopped at {len(rows):,} rows (max_rows); the source has more"
                             + (f" (the API reports {total:,} records for the title/day filter)" if total else "")
                             + (". The cut follows posting time (the newest posted rows, then put in date order), "
                                "so releases near the cut can differ from a cut by date" if by_date else "")
                             + ". Narrow the filter or raise max_rows.")
            elif stopped == "pages":
                notes.append(f"{r['name']}: stopped after scanning {MAX_PAGES * size:,} records newest first"
                             + (f" (by posting time, reading {CREATED_MARGIN_DAYS} days of postings past `since`)"
                                if by_date and since else "")
                             + "; older matches were not reached. Narrow the title filter or the date range.")
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
        if str(p.get("sort", "date")) == "date":   # date ties shift between pages: list by posting time
            r["params"]["sort"] = "created"
    items = gr.manual_items(pack, s)
    for it in items:
        if it["request"] in ranged:
            it["note"] = ((it["note"] + "; ") if it["note"] else "") + \
                "newest first; the date range (since/until) is applied when the files are ingested"
    return items

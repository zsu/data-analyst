"""GovInfo pack hooks.

- search: GovInfo filters by collection, congress, bill type, publisher and date only through field:value
  terms INSIDE the query string (the same names as JSON body fields are silently ignored; verified Oct 2026),
  so before_request builds the query. User text is escaped: query operators, field syntax and punctuation
  are removed from `text` and `phrase` (a stray ':' or '\\' once drew HTTP 500). `query` is raw
  syntax for experts and is passed unchanged.
- text: fetch a package's summary, then its text link (api.govinfo.gov, with key) or the public content
  link (www.govinfo.gov, never with key), strip the HTML and return one row.
- sections: a public law's USLM XML (GPO, 113th Congress on), one row per section, with the U.S. Code
  references split into amended (an <amendingAction> in the same chapeau/content/paragraph), cited and
  classified (margin notes). Sections inside <quotedContent> are the amended Code text, not the law's own.
- manual_items: api links carry the public DEMO_KEY; text and sections items use keyless www.govinfo.gov links.
"""
from __future__ import annotations

import datetime
import html
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

from core import generic_rest as gr
from core import packfiles
from core.http import Client, DataError

HERE = os.path.dirname(os.path.abspath(__file__))
BILL_TYPES = ("hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres")
SEARCH_KEYS = ("text", "phrase", "query", "collection", "congress", "billtype", "publisher", "date_from", "date_to", "sort")
SORTS = {"relevance": [{"field": "score", "sortOrder": "DESC"}],
         "newest": [{"field": "publishdate", "sortOrder": "DESC"}],
         "oldest": [{"field": "publishdate", "sortOrder": "ASC"}]}
OPERATORS = {"and", "or", "not"}
DEMO_LIMIT = 8


def _eps() -> dict:
    return packfiles.read_json(HERE, "endpoints.json")


def _collections() -> set[str]:
    return {c["id"] for c in _eps()["catalog"].values() if c["id"].isupper() and " " not in c["id"]}


def _coverage() -> dict[str, tuple[int, int]]:
    """{collection: (first year, last year)} for collections whose catalog entry names a closed range."""
    out = {}
    for c in _eps()["catalog"].values():
        m = re.search(r"\((\d{4})-(\d{4})\)", c.get("description", ""))
        if m and c["id"].isupper():
            out[c["id"]] = (int(m.group(1)), int(m.group(2)))
    return out


def _outside_coverage(name, p) -> list[str]:
    """A date window wholly outside a closed collection's years returns nothing: say so before fetching."""
    years = [int(str(p[k])[:4]) for k in ("start_date", "end_date", "date_from", "date_to")
             if re.match(r"\d{4}", str(p.get(k) or ""))]
    if not years:
        return []
    cov, out = _coverage(), []
    for c in _list(p.get("collection") or []):
        rng = cov.get(str(c).upper())
        if rng and (min(years) > rng[1] or max(years) < rng[0]):
            out.append(f"request {name!r}: {c.upper()} on GovInfo covers {rng[0]}-{rng[1]}; nothing is dated "
                       f"{min(years)}-{max(years)} (later items are on the agency's own site, which no pack serves)")
    return out


def _words(text: str) -> list[str]:
    """User text reduced to plain words: no field syntax, operators, quotes, wildcards or brackets."""
    t = re.sub(r"[()\[\]{}:\"'\\^~*?!/<>=|&+]", " ", str(text))
    out = []
    for w in t.split():
        w = w.strip("-,;.")
        if w and w.lower() not in OPERATORS:
            out.append(w)
    return out


def _date(v: str) -> str:
    v = str(v).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise DataError(f"govinfo: dates must be YYYY-MM-DD, got {v!r}", kind="bad_request")
    return v


def _list(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def build_query(p: dict) -> str:
    """The GovInfo query string for search parameters (filters first, then the escaped text)."""
    terms = []
    colls = [c.upper() for c in _list(p.get("collection") or [])]
    if colls:
        terms.append(f"collection:{colls[0]}" if len(colls) == 1 else "(" + " OR ".join(f"collection:{c}" for c in colls) + ")")
    if p.get("congress"):
        terms.append(f"congress:{int(p['congress'])}")
    if p.get("billtype"):
        terms.append(f"billtype:{str(p['billtype']).lower()}")
    if p.get("publisher"):
        terms.append('publisher:"' + " ".join(_words(p["publisher"])) + '"')
    if p.get("date_from") or p.get("date_to"):
        terms.append(f"publishdate:range({_date(p['date_from']) if p.get('date_from') else ''},"
                     f"{_date(p['date_to']) if p.get('date_to') else ''})")
    if p.get("text"):
        terms.append("(" + " ".join(_words(p["text"])) + ")")
    if p.get("phrase"):
        terms.append('("' + " ".join(_words(p["phrase"])) + '")')
    if p.get("query"):
        terms.append("(" + str(p["query"]) + ")")
    return " AND ".join(terms)


def bill_package_id(p: dict) -> str:
    if p.get("packageId"):
        return str(p["packageId"]).strip()
    miss = [k for k in ("congress", "billtype", "number") if not p.get(k)]
    if miss:
        raise DataError(f"govinfo text: give packageId (e.g. CRPT-118hrpt730, PLAW-118publ31), or congress, billtype "
                        f"and number for a bill; missing {miss} (params given: {sorted(p)})", kind="bad_request")
    return f"BILLS-{int(p['congress'])}{str(p['billtype']).lower()}{int(p['number'])}{str(p.get('version') or 'enr').lower()}"


def check_spec(spec):
    probs = []
    known = _collections()
    for r in spec.get("requests") or []:
        p, ep = r.get("params") or {}, r.get("endpoint")
        n = r.get("name")
        if ep == "search":
            if not any(p.get(k) for k in ("text", "phrase", "query", "collection", "publisher")):
                probs.append(f"request {n!r}: search needs text, phrase, query, collection or publisher")
            if (p.get("text") and not _words(p["text"])) or (p.get("phrase") and not _words(p["phrase"])):
                probs.append(f"request {n!r}: text/phrase has no searchable words")
            if "*" in " ".join(str(p.get(k) or "") for k in ("text", "phrase", "query")):
                probs.append(f"request {n!r}: GovInfo search answers a wildcard (*) with HTTP 500: list the word "
                             "forms instead (title:tariff OR title:tariffs)")
            bad = [c for c in _list(p.get("collection") or []) if c.upper() not in known]
            if bad:
                probs.append(f"request {n!r}: unknown collection {bad} (see --endpoints catalog)")
            if p.get("billtype") and str(p["billtype"]).lower() not in BILL_TYPES:
                probs.append(f"request {n!r}: billtype must be one of {BILL_TYPES}")
            if p.get("sort") and p["sort"] not in SORTS:
                probs.append(f"request {n!r}: sort must be one of {sorted(SORTS)}")
            for k in ("date_from", "date_to"):
                if p.get(k) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p[k])):
                    probs.append(f"request {n!r}: {k} must be YYYY-MM-DD")
        elif ep == "text":
            if not p.get("packageId") and not all(p.get(k) for k in ("congress", "billtype", "number")):
                probs.append(f"request {n!r}: text needs packageId, or congress + billtype + number")
            elif not p.get("packageId") and str(p["billtype"]).lower() not in BILL_TYPES:
                probs.append(f"request {n!r}: billtype must be one of {BILL_TYPES}")
        elif ep == "sections":
            if not re.fullmatch(r"PLAW-\d+(publ|pvtl)\d+", str(p.get("packageId") or "")):
                probs.append(f"request {n!r}: sections needs a PLAW packageId (e.g. PLAW-117publ169). {NO_USLM}")
        probs += _outside_coverage(n, p)
    return probs


def before_request(req, ep, params):
    if str(ep.get("path", "")).startswith("/published"):
        # GovInfo's end date is exclusive [live 2026-10-21: 2025-12-31 to 2025-12-31 returns nothing; to 2026-01-01
        # returns the Dec 31 Federal Register]: the spec's end_date is inclusive, so ask for the next day
        end = str(params.get("end_date") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            nxt = datetime.date.fromisoformat(end) + datetime.timedelta(days=1)
            params = {**params, "end_date": nxt.isoformat()}
        return params
    if ep.get("path") != "/search":
        return params
    out = {k: v for k, v in params.items() if k not in SEARCH_KEYS}
    out["query"] = build_query(params)
    out["sorts"] = SORTS[params.get("sort") or "relevance"]
    return out


def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", raw)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


MAX_GRANULES = 25          # a package whose text is only at granule level (CRPT, CHRG): read at most this many


def _text_row(pid: str, summary: dict, text: str, link: str, max_chars, committees: str = "") -> dict:
    full = len(text)
    if max_chars:
        text = text[:int(max_chars)]
    return {"package_id": pid, "title": summary.get("title", ""), "collection": summary.get("collectionCode", ""),
            "date_issued": summary.get("dateIssued", ""), "pages": summary.get("pages", ""), "committees": committees,
            "text_url": link, "characters": full, "truncated": bool(max_chars) and full > int(max_chars), "text": text}


def _get_raw(api, link: str) -> tuple[str, str]:
    """(body, the link shown without a key) of a GovInfo link."""
    u = urllib.parse.urlsplit(link)
    if u.scheme != "https" or u.hostname not in ("api.govinfo.gov", "www.govinfo.gov"):
        raise DataError(f"govinfo: not fetching a non-GovInfo link {u.scheme}://{u.hostname}", kind="api")
    q = {k: v for k, v in urllib.parse.parse_qsl(u.query) if k.lower() != "api_key"}
    if u.hostname == "api.govinfo.gov":
        raw = api.get(u.path, q).text()
    else:                                           # the key is only ever sent to api.govinfo.gov
        www = Client("https://www.govinfo.gov", name="govinfo-content", rps=2, burst=4)
        raw = www.get(u.path, q).text()
    return raw, urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def _link_text(api, link: str) -> tuple[str, str]:
    """(plain text, the link shown without a key) of a GovInfo txt or htm link."""
    raw, shown = _get_raw(api, link)
    return html_to_text(raw), shown


def _granule_texts(api, pid: str) -> tuple[list[str], list[str], list[str]]:
    """Texts, links and committees of a package whose text renditions are per granule (committee reports and
    hearings keep theirs there; the package summary has none)."""
    got = api.get_json(f"/packages/{urllib.parse.quote(pid)}/granules", {"offsetMark": "*", "pageSize": "100"})
    grans = got.get("granules") or []
    if len(grans) > MAX_GRANULES:
        raise DataError(f"govinfo: package {pid} has {len(grans)} granules and no package-level text; ask for "
                        "the granules you need (endpoint granules) instead of the whole text", kind="bad_request")
    texts, links, committees = [], [], []
    for g in grans:
        gs = api.get_json(f"/packages/{urllib.parse.quote(pid)}/granules/{urllib.parse.quote(g['granuleId'])}/summary")
        committees += [c.get("committeeName", "") for c in gs.get("committees") or [] if c.get("committeeName")]
        d = gs.get("download") or {}
        link = d.get("txtLink") or d.get("htmlLink")
        if link:
            t, shown = _link_text(api, link)
            texts.append(t)
            links.append(shown)
    return texts, links, list(dict.fromkeys(committees))


def _fetch_text(pack, req) -> list[dict]:
    p = req.get("params") or {}
    pid = bill_package_id(p)
    api = gr.client_for(pack)
    try:
        summary = api.get_json(f"/packages/{urllib.parse.quote(pid)}/summary")
    except DataError as e:
        if e.kind == "not_found":
            raise DataError(f"govinfo: package {pid} not found (check the version code: ih, rh, eh, es, enr, ...)",
                            kind="bad_request") from None
        raise
    d = summary.get("download") or {}
    link = d.get("txtLink") or d.get("htmlLink") or summary.get("txtLink") or ""
    if link:
        text, shown = _link_text(api, link)
        return [_text_row(pid, summary, text, shown, p.get("max_chars"))]
    texts, links, committees = _granule_texts(api, pid)
    if not texts:
        raise DataError(f"govinfo: package {pid} has no text rendition (PDF only); see its pdfLink", kind="bad_request")
    shown = links[0] + (f" (+{len(links) - 1} granules)" if len(links) > 1 else "")
    return [_text_row(pid, summary, "\n\n".join(texts), shown, p.get("max_chars"), "; ".join(committees))]


USLM_NS = "{http://schemas.gpo.gov/xml/uslm}"
CONTAINERS = ("division", "title", "subtitle", "part", "subpart", "chapter", "subchapter")
BLOCKS = ("chapeau", "content", "continuation", "p")   # where "Section X of title Y is amended" sits with its action
SPACED = BLOCKS + ("heading", "subsection", "paragraph", "subparagraph", "clause", "subclause", "item")
NO_USLM = ("GovInfo has USLM XML for public laws from the 113th Congress (2013) on, not for bills, Statutes at "
           "Large granules or most private laws; check the id, or use endpoint text")


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _walk(el, skip=("quotedContent", "sidenote")):
    """el and its descendants, not entering amended (quoted) text or margin notes."""
    yield el
    for c in el:
        if _local(c.tag) not in skip:
            yield from _walk(c, skip)


def _plain(el) -> str:
    """Text of an element without its margin notes, whitespace and soft hyphens normalised."""
    parts = []

    def go(e):
        parts.append(e.text or "")
        for c in e:
            if _local(c.tag) != "sidenote":
                go(c)
                if _local(c.tag) in SPACED:          # "HEADING.(a)" -> "HEADING. (a)"
                    parts.append(" ")
            parts.append(c.tail or "")
    go(el)
    return re.sub(r"\s+", " ", "".join(parts).replace("\xad", "")).strip()


def usc_cite(href: str) -> str:
    """/us/usc/t31/s1120/a/2 -> 31 U.S.C. 1120(a)(2); /us/usc/t31/ch11 -> 31 U.S.C. ch. 11."""
    m = re.fullmatch(r"/us/usc/t(\w+)/(s|ch)([^/]+)((?:/[^/]+)*)", href)      # section numbers like 666c–1
    if not m:
        return href
    if m.group(2) == "ch":
        return f"{m.group(1)} U.S.C. ch. {m.group(3)}"
    return f"{m.group(1)} U.S.C. {m.group(3)}" + "".join(f"({x})" for x in m.group(4).split("/") if x)


def _child_text(el, name: str) -> str:
    c = el.find(USLM_NS + name)
    return _plain(c) if c is not None else ""


def _usc(refs) -> list[str]:
    return [r.get("href", "") for r in refs if _local(r.tag) == "ref" and r.get("href", "").startswith("/us/usc/")]


def _section_row(pid: str, sec, path: list[str], max_chars) -> dict:
    cited = list(dict.fromkeys(_usc(_walk(sec))))
    amended = []
    for b in _walk(sec):
        if _local(b.tag) in BLOCKS and any(_local(x.tag) == "amendingAction" for x in _walk(b)):
            amended += _usc(_walk(b))
    amended = list(dict.fromkeys(amended))
    classified = [h for h in dict.fromkeys(_usc(_walk(sec, ("quotedContent",)))) if h not in cited]
    num = sec.find(USLM_NS + "num")
    text = _plain(sec)
    return {"package_id": pid, "identifier": sec.get("identifier", ""),
            "num": num.get("value", _plain(num)) if num is not None else "", "heading": _child_text(sec, "heading"),
            "within": " > ".join(path),
            "usc_amended": "; ".join(usc_cite(h) for h in amended),
            "usc_cited": "; ".join(usc_cite(h) for h in cited if h not in amended),
            "usc_classified": "; ".join(usc_cite(h) for h in classified),
            "amending_actions": sum(1 for x in _walk(sec) if _local(x.tag) == "amendingAction"),
            "characters": len(text), "text": text[:int(max_chars)] if max_chars else text}


def uslm_sections(pid: str, raw: str, max_chars=None) -> list[dict]:
    """One row per section of a USLM law (a law without sections gives one row for its body)."""
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError as e:
        raise DataError(f"govinfo: {pid} USLM is not well-formed XML ({e})", kind="api") from None
    main = root.find(USLM_NS + "main")
    if main is None:
        raise DataError(f"govinfo: {pid} has no <main> in its USLM; use endpoint text", kind="api")
    rows = []

    def go(el, path):
        for c in el:
            name = _local(c.tag)
            if name == "section":
                rows.append(_section_row(pid, c, path, max_chars))
            elif name in CONTAINERS:
                go(c, path + [" ".join(x for x in (_child_text(c, "num"), _child_text(c, "heading")) if x)])
            elif name not in ("quotedContent", "sidenote"):
                go(c, path)
    go(main, [])
    if not rows:
        rows.append(_section_row(pid, main, [], max_chars))
        rows[0]["identifier"] = rows[0]["identifier"] or pid
    return rows


def _fetch_sections(pack, req) -> list[dict]:
    p = req.get("params") or {}
    pid = str(p.get("packageId") or "").strip()
    api = gr.client_for(pack)
    try:
        raw, _ = _get_raw(api, f"https://api.govinfo.gov/packages/{urllib.parse.quote(pid)}/uslm")
    except DataError as e:
        if e.kind in ("not_found", "bad_request"):     # GovInfo answers a missing rendition with HTTP 400
            raise DataError(f"govinfo: no USLM XML for {pid}. {NO_USLM}", kind="bad_request") from None
        raise
    return uslm_sections(pid, raw, p.get("max_chars"))


def fetch(spec, pack, route):
    tables, notes = {}, []
    for r in spec["requests"]:
        if r.get("endpoint") == "text":
            tables[r["name"]] = _fetch_text(pack, r)
        elif r.get("endpoint") == "sections":
            tables[r["name"]] = _fetch_sections(pack, r)
        else:
            t, n = gr.fetch(pack, {"requests": [r]})
            tables.update(t)
            notes += n
    return tables, notes


def _calls(spec) -> int:
    eps = _eps()["endpoints"]
    n = 0
    for r in spec.get("requests") or []:
        ep = eps.get(r.get("endpoint") or "", {})
        size = int((ep.get("paging") or {}).get("size") or 1000)
        n += 4 if r.get("endpoint") == "text" else 1 if r.get("endpoint") == "sections" else max(1, math.ceil(int(r.get("max_rows") or size) / size))
    return n


def key_only_needs(spec):
    n = _calls(spec)
    if n > DEMO_LIMIT:
        return [f"about {n} API calls (the keyless DEMO_KEY allows about 10 an hour on GovInfo)"]
    return []


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") == "text":
            try:
                pid = bill_package_id(r.get("params") or {})
            except (KeyError, ValueError, TypeError):
                continue
            items.append({"name": f"{r['name']}#1", "request": r["name"], "filename": f"{r['name']}.htm",
                          "url": f"https://www.govinfo.gov/content/pkg/{pid}/html/{pid}.htm", "needs_key": False,
                          "method": "GET", "body": None,
                          "note": "public text page, no key; if it is missing, open https://www.govinfo.gov/app/details/" + pid})
            continue
        if r.get("endpoint") == "sections":
            pid = str((r.get("params") or {}).get("packageId") or "")
            items.append({"name": f"{r['name']}#1", "request": r["name"], "filename": f"{r['name']}.xml",
                          "url": f"https://www.govinfo.gov/content/pkg/{pid}/uslm/{pid}.xml", "needs_key": False,
                          "method": "GET", "body": None, "note": "public USLM XML, no key"})
            continue
        one = {"requests": [r]}
        if r.get("endpoint") == "search":
            ep, params, _ = gr.resolve(pack, r)
            r = {**r, "params": {k: v for k, v in before_request(r, ep, params).items()}}
            one = {"requests": [r]}
        for it in gr.manual_items(pack, one):
            if it.get("body") is not None and r.get("endpoint") == "search":
                it["body"] = {**ep.get("body", {}), **it["body"], "pageSize": 100}
            it["url"] += ("&" if "?" in it["url"] else "?") + "api_key=DEMO_KEY"
            it["note"] = (it.get("note", "") + "; " if it.get("note") else "") + \
                "DEMO_KEY is api.data.gov's public demo key (about 10 requests an hour); replace it with your own key for more"
            items.append(it)
    return items


def parse_upload(pack, req, paths):
    if req.get("endpoint") == "sections":
        p = req.get("params") or {}
        rows = []
        for pth in paths:
            with open(pth, "rb") as f:
                rows += uslm_sections(str(p.get("packageId") or ""), f.read().decode("utf-8-sig", errors="replace"),
                                      p.get("max_chars"))
        return rows
    if req.get("endpoint") != "text":
        return gr.parse_upload(pack, req, paths)
    rows = []
    for pth in paths:
        with open(pth, "rb") as f:
            raw = f.read().decode("utf-8-sig", errors="replace")
        pid = bill_package_id(req.get("params") or {})
        rows.append(_text_row(pid, {}, html_to_text(raw), os.path.basename(pth), (req.get("params") or {}).get("max_chars")))
    return rows

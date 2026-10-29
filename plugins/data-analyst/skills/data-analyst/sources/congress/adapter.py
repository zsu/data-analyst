"""Congress.gov pack hooks, plus roll-call votes from the House Clerk and Senate XML files.

- before_request: "congress": "current" becomes the current Congress number (a Congress starts at noon on
  January 3 of an odd year, so January 1-2 of an odd year still belong to the previous one);
  title_contains is removed (applied in normalize); a "+" in sort becomes a space (Congress.gov
  silently ignores an encoded "+").
- normalize: title_contains filter (the phrase as given, each word matching at a word start, any case; a
  list matches any of its phrases; a multi-word phrase that keeps nothing says the order is required);
  text_plain added to CRS summaries (HTML stripped, text kept as is).
- fetch: roll_call, senate_votes and bill_votes read the official XML (clerk.house.gov 1990 on,
  www.senate.gov 1989 on; no key). roll_call for the House falls back to the Congress.gov house-vote API
  (115th Congress on) when the Clerk file cannot be read. Every other endpoint goes through generic_rest.
- manual_items / parse_upload: XML links for the vote endpoints; DEMO_KEY on Congress.gov links.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime

from core import generic_rest as gr
from core import packfiles
from core.http import Client, DataError

HERE = os.path.dirname(os.path.abspath(__file__))
CLERK = "https://clerk.house.gov"
SENATE = "https://www.senate.gov"
CUSTOM = ("roll_call", "senate_votes", "bill_votes")
DEMO_LIMIT = 8
PARTY = {"republican": "R", "democratic": "D", "democrat": "D", "independent": "I"}
_CLIENTS: dict = {}

HOUSE_API_FIRST = 115     # Congress.gov house-vote data start with the 115th Congress (2017; verified 2026-10)


# ---------------------------------------------------------------------- congress numbers
def current_congress(today: date | None = None) -> int:
    """The Congress whose term includes a date. The n-th Congress starts in year 1789 + 2(n-1): on January 3
    since 1935 (20th Amendment), on March 4 before."""
    d = today or date.today()
    n = (d.year - 1789) // 2 + 1
    start = (1, 3) if d.year >= 1935 else (3, 4)
    if d.year % 2 == 1 and (d.month, d.day) < start and n > 1:
        n -= 1
    return n


def congress_years(n: int) -> tuple[int, int]:
    start = 1789 + 2 * (n - 1)
    return start, start + 1


def session_year(congress: int, session: int) -> int:
    """Calendar year of a regular session (session 1 = the odd year, session 2 = the even year)."""
    return congress_years(int(congress))[0] + int(session) - 1


def ordinal(n: int) -> str:
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _eps() -> dict:
    return packfiles.read_json(HERE, "endpoints.json")["endpoints"]


# ---------------------------------------------------------------------- spec hooks
def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, n = r.get("params") or {}, r.get("name")
        if r.get("endpoint") == "roll_call":
            ch = str(p.get("chamber", "")).lower()
            if ch == "senate" and not (p.get("congress") and p.get("session")):
                probs.append(f"request {n!r}: a Senate roll_call needs congress and session")
            if ch == "house" and not (p.get("year") or (p.get("congress") and p.get("session"))):
                probs.append(f"request {n!r}: a House roll_call needs year, or congress and session")
        c = p.get("congress")
        if c not in (None, "", "current") and not str(c).isdigit():
            probs.append(f"request {n!r}: congress must be a number or 'current'")
    return probs


def before_request(req, ep, params):
    out = dict(params)
    if str(out.get("congress", "")).lower() == "current":
        out["congress"] = current_congress()
    out.pop("title_contains", None)
    if isinstance(out.get("sort"), str):
        # Congress.gov reads "introducedDate+asc" with a literal "+" (a space); sent encoded as %2B the sort
        # is ignored without an error and the default order comes back (verified 2026-10)
        out["sort"] = " ".join(out["sort"].replace("+", " ").split())
    return out


def _strip_html(s) -> str:
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", str(s or ""))
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def title_matcher(value):
    """title_contains -> a test on a title, or None. Each phrase matches as given: its words in that order, each
    at the START of a word (so "port" never matches "support", while "veteran" still finds "Veterans" and
    "small business" finds "Small Businesses"), any case, any spacing or punctuation between words. A list
    matches a title holding any of its phrases (other word forms: ["levy", "levies"])."""
    phrases = value if isinstance(value, (list, tuple)) else [value]
    pats = [r"(?<!\w)" + r"\w*\W+".join(re.escape(w) for w in str(p).split())
            for p in phrases if str(p or "").split()]
    if not pats:
        return None
    rx = re.compile("|".join(pats), re.IGNORECASE)
    return lambda title: bool(rx.search(str(title or "")))


def phrase_order_hint(req) -> str | None:
    """For a multi-word title_contains that kept nothing: the words must stand together and in that order, which
    is easy to miss, so say so and how to match other orders."""
    v = (req.get("params") or {}).get("title_contains")
    multi = [str(p) for p in (v if isinstance(v, (list, tuple)) else [v]) if len(str(p or "").split()) > 1]
    if not multi:
        return None
    return (f"title_contains needs each phrase's words adjacent and in that order ({', '.join(map(repr, multi))}); "
            "to match them in another order or apart, list each form as its own phrase (a list matches any of "
            "them), or filter on one distinctive word and check the titles in the CSV.")


TITLE_ROUTE = {"bills": "BILLS", "bills_congress": "BILLS", "laws": "PLAW", "laws_congress": "PLAW"}


def display_title_note(req) -> str | None:
    """title_contains on a bill or law list reads Congress.gov's display title only; a bill whose official (or
    short) title alone holds the phrase is not in the table. Say so on every such fetch, with the title search
    that finds them (GovInfo's title field) and the check that confirms each one (bill_titles)."""
    p = req.get("params") or {}
    v = p.get("title_contains")
    phrases = [str(x).strip() for x in (v if isinstance(v, (list, tuple)) else [v]) if str(x or "").strip()]
    coll = TITLE_ROUTE.get(req.get("endpoint") or "")
    if not phrases or not coll:
        return None
    c = p.get("congress")
    cong = f'"congress": {c}' if str(c or "").isdigit() else '"congress": <the Congress number>'
    search = "; ".join(f'{{"collection": "{coll}", {cong}, "title": "{x}"}}' for x in phrases[:3])
    return (f"{req.get('_group') or req['name']}: title_contains matched the list's display title only, not official or short titles, so "
            "bills whose official title alone holds the phrase are missing here. For 'in the title' answers, search "
            f"titles at GovInfo: source govinfo, endpoint search, params {search}; strip the version suffix from "
            "packageId (BILLS-<congress><type><number><version>) to get each bill, deduplicate, then confirm each "
            "with this pack's bill_titles (for_each on congress, billType, billNumber) and say which title the "
            "count uses.")


def normalize(req, ep, rows):
    match = title_matcher((req.get("params") or {}).get("title_contains"))
    if match:
        rows = [r for r in rows if match(r.get("title"))]
    if req.get("endpoint") in ("bill_summaries", "summaries"):
        rows = [{**r, "text_plain": _strip_html(r.get("text"))} for r in rows]
    return rows


# ---------------------------------------------------------------------- XML votes
def _client(base: str, name: str) -> Client:
    if base not in _CLIENTS:
        _CLIENTS[base] = Client(base, name=name, rps=2, burst=4)
    return _CLIENTS[base]


def _xml(url: str) -> ET.Element:
    u = urllib.parse.urlsplit(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in (CLERK, SENATE):
        raise DataError(f"congress: not fetching vote XML from {base}", kind="api")
    c = _client(base, "house-clerk" if base == CLERK else "senate-lis")
    body = c.get(u.path).body
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise DataError(f"congress: {u.path} is not valid XML ({e}); the site may be blocking automated requests",
                        kind="api") from None


def house_url(year: int, roll: int) -> str:
    return f"{CLERK}/evs/{int(year)}/roll{int(roll):03d}.xml"


def senate_url(congress: int, session: int, roll: int) -> str:
    c, s = int(congress), int(session)
    return f"{SENATE}/legislative/LIS/roll_call_votes/vote{c}{s}/vote_{c}_{s}_{int(roll):05d}.xml"


def senate_menu_url(congress: int, session: int) -> str:
    return f"{SENATE}/legislative/LIS/roll_call_lists/vote_menu_{int(congress)}_{int(session)}.xml"


def refused_hosts(spec) -> str:
    """--check probes only api.congress.gov. When the spec needs Senate vote XML, try senate.gov once: a refusal
    (HTTP 403 bot filter) is returned as the error a fetch would raise, so --check routes it before the fetch."""
    def senate(r):
        p, ep = r.get("params") or {}, r.get("endpoint")
        ch = str(p.get("chamber") or "").lower()
        return ep == "senate_votes" or (ep == "roll_call" and ch == "senate") or (ep == "bill_votes" and ch != "house")
    if not any(senate(r) for r in spec.get("requests") or [] if isinstance(r, dict)):
        return ""
    try:
        _client(SENATE, "senate-lis").get(urllib.parse.urlsplit(senate_menu_url(119, 1)).path)
    except DataError as e:
        if e.kind == "blocked":
            return (f"{e}. Senate vote XML cannot be fetched from here; House votes can (bill_votes with "
                    "\"chamber\": \"house\"), and source.md (Access) gives the Congressional Record route for Senate "
                    "roll calls")
    except Exception:                               # an outage or a fixture gap: the fetch will say
        pass
    return ""


def _t(el, path, default=""):
    x = el.find(path) if el is not None else None
    return " ".join("".join(x.itertext()).split()) if x is not None else default


def _iso(s: str, fmts) -> str:
    s = " ".join(str(s).split())
    for f in fmts:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return ""


def _position(v: str) -> str:
    v = v.strip().lower()
    return {"yea": "yea", "aye": "yea", "nay": "nay", "no": "nay", "present": "present",
            "not voting": "not_voting"}.get(v, "other")


def parse_house(root: ET.Element, url: str) -> tuple[dict, list[dict]]:
    m = root.find("vote-metadata")
    sess = re.sub(r"\D", "", _t(m, "session"))
    date_raw = _t(m, "action-date")
    vote = {"chamber": "house", "congress": _t(m, "congress"), "session": sess, "roll": _t(m, "rollcall-num"),
            "date": _iso(date_raw, ("%d-%b-%Y",)), "date_published": date_raw, "question": _t(m, "vote-question"),
            "vote_type": _t(m, "vote-type"), "result": _t(m, "vote-result"), "legislation": _t(m, "legis-num"),
            "description": _t(m, "vote-desc")}
    tv = m.find("vote-totals/totals-by-vote") if m is not None else None
    vote.update(yea=_t(tv, "yea-total"), nay=_t(tv, "nay-total"), present=_t(tv, "present-total"),
                not_voting=_t(tv, "not-voting-total"))
    for tp in (m.findall("vote-totals/totals-by-party") if m is not None else []):
        p = PARTY.get(_t(tp, "party").lower(), _t(tp, "party")[:1].upper() or "?")
        for k, tag in (("yea", "yea-total"), ("nay", "nay-total"), ("present", "present-total"), ("not_voting", "not-voting-total")):
            vote[f"{k}_{p}"] = _t(tp, tag)
    vote["source_url"] = url
    members = []
    for rv in root.findall("vote-data/recorded-vote"):
        leg = rv.find("legislator")
        a = leg.attrib if leg is not None else {}
        cast = _t(rv, "vote")
        members.append({"chamber": "house", "congress": vote["congress"], "session": sess, "roll": vote["roll"],
                        "date": vote["date"], "member_id": a.get("name-id", ""), "name": a.get("unaccented-name") or _t(rv, "legislator"),
                        "party": a.get("party", ""), "state": a.get("state", ""), "vote": cast, "position": _position(cast),
                        "vote_source": "clerk.house.gov"})
    return vote, members


def parse_senate(root: ET.Element, url: str) -> tuple[dict, list[dict]]:
    date_raw = _t(root, "vote_date")
    d = _iso(date_raw.split(",")[0] + "," + date_raw.split(",")[1] if date_raw.count(",") >= 1 else date_raw,
             ("%B %d, %Y",))
    vote = {"chamber": "senate", "congress": _t(root, "congress"), "session": _t(root, "session"),
            "roll": str(int(_t(root, "vote_number") or 0)), "date": d, "date_published": date_raw,
            "question": _t(root, "vote_question_text") or _t(root, "question"), "vote_type": _t(root, "majority_requirement"),
            "result": _t(root, "vote_result"), "legislation": _t(root, "document/document_name"),
            "description": _t(root, "vote_title") or _t(root, "vote_document_text"),
            "yea": _t(root, "count/yeas"), "nay": _t(root, "count/nays"), "present": _t(root, "count/present"),
            "not_voting": _t(root, "count/absent"), "tie_breaker": _t(root, "tie_breaker/by_whom")}
    members = []
    for mm in root.findall("members/member"):
        cast = _t(mm, "vote_cast")
        members.append({"chamber": "senate", "congress": vote["congress"], "session": vote["session"], "roll": vote["roll"],
                        "date": d, "member_id": _t(mm, "lis_member_id"),
                        "name": f"{_t(mm, 'first_name')} {_t(mm, 'last_name')}".strip(), "party": _t(mm, "party"),
                        "state": _t(mm, "state"), "vote": cast, "position": _position(cast), "vote_source": "senate.gov"})
    parties = {x["party"] for x in members}
    tally: dict = {(pos, p): 0 for pos in ("yea", "nay", "present", "not_voting") for p in parties}
    for x in members:
        tally[(x["position"], x["party"])] = tally.get((x["position"], x["party"]), 0) + 1
    for (pos, party), n in sorted(tally.items()):
        vote[f"{pos}_{party}"] = n
    for pos in ("present", "not_voting"):     # the XML leaves <present/> and <absent/> empty when the count is 0
        if members and vote[pos] == "":
            vote[pos] = str(sum(1 for x in members if x["position"] == pos))
    vote["source_url"] = url
    return vote, members


def parse_vote_xml(root: ET.Element, url: str) -> tuple[dict, list[dict]]:
    if root.tag == "rollcall-vote":
        return parse_house(root, url)
    if root.tag == "roll_call_vote":
        return parse_senate(root, url)
    raise DataError(f"congress: {url} is not a roll-call vote file", kind="api")


def parse_senate_menu(root: ET.Element, url: str) -> list[dict]:
    year = _t(root, "congress_year")
    rows = []
    for v in root.findall("votes/vote"):
        # an en bloc vote (several nominations at once) nests issue, question and result under <en_bloc><matter>
        matters = v.findall("en_bloc/matter")
        pick = lambda tag: _t(v, tag) or "; ".join(dict.fromkeys(_t(m, tag) for m in matters if _t(m, tag)))  # noqa: E731
        rows.append({"chamber": "senate", "congress": _t(root, "congress"), "session": _t(root, "session"),
                     "roll": str(int(_t(v, "vote_number") or 0)), "date": _iso(f"{_t(v, 'vote_date')}-{year}", ("%d-%b-%Y",)),
                     "issue": pick("issue"), "question": pick("question"), "result": pick("result"),
                     "en_bloc": str(len(matters)) if matters else "",
                     "yea": _t(v, "vote_tally/yeas"), "nay": _t(v, "vote_tally/nays"),
                     "title": _t(v, "title"), "source_url": senate_url(_t(root, "congress"), _t(root, "session"), _t(v, "vote_number") or 0)})
    return rows


def _house_api_members(pack, congress, session, roll) -> list[dict]:
    data = gr.client_for(pack).get_json(f"/house-vote/{int(congress)}/{int(session)}/{int(roll)}/members", {"format": "json"})
    v = data.get("houseRollCallVoteMemberVotes") or {}
    out = []
    for m in v.get("results") or []:
        cast = str(m.get("voteCast", ""))
        out.append({"chamber": "house", "congress": str(congress), "session": str(session), "roll": str(roll),
                    "date": str(v.get("startDate", ""))[:10], "member_id": m.get("bioguideID", ""),
                    "name": f"{m.get('firstName', '')} {m.get('lastName', '')}".strip(), "party": m.get("voteParty", ""),
                    "state": m.get("voteState", ""), "vote": cast, "position": _position(cast), "vote_source": "api.congress.gov"})
    return out


def _roll_call(pack, p: dict) -> tuple[list[dict], list[str]]:
    ch = str(p["chamber"]).lower()
    # summary=true: one row for the vote itself (question, result, type, totals) instead of one per member
    part = 0 if str(p.get("summary", "")).lower() in ("true", "1", "yes") else 1
    if ch == "senate":
        url = senate_url(p["congress"], p["session"], p["roll"])
        got = parse_senate(_xml(url), url)[part]
        return (got if part else [got]), []
    year = int(p.get("year") or session_year(int(p["congress"]), int(p["session"])))
    url = house_url(year, p["roll"])
    try:
        got = parse_house(_xml(url), url)[part]
        return (got if part else [got]), []
    except DataError as e:
        if not part:
            raise
        congress = int(p.get("congress") or current_congress(date(year, 6, 1)))
        session = int(p.get("session") or (1 if year % 2 else 2))
        if congress < HOUSE_API_FIRST:
            raise
        rows = _house_api_members(pack, congress, session, p["roll"])
        return rows, [f"House Clerk file unavailable ({e}); member votes came from the Congress.gov house-vote API"]


def _bill_votes(pack, req: dict) -> list[dict]:
    p = dict(req.get("params") or {})
    if str(p.get("congress", "")).lower() == "current":
        p["congress"] = current_congress()
    eps = _eps()
    ep = eps["bill_actions"]
    params = {**ep.get("defaults", {}), **{k: p[k] for k in ("congress", "billType", "billNumber")}}
    actions, _ = gr.fetch_request(gr.client_for(pack), ep, params, 5000)
    want = str(p.get("chamber") or "").lower()
    seen, votes = set(), []
    for a in actions:
        rv = a.get("recordedVotes")
        for v in (json.loads(rv) if isinstance(rv, str) and rv.startswith("[") else []):
            url = v.get("url") or ""
            ch = str(v.get("chamber", "")).lower()
            if not url or url in seen or (want and ch != want):
                continue
            seen.add(url)
            votes.append((v, a))
    rows = []
    members = str(p.get("members", "")).lower() in ("true", "1", "yes")
    for v, a in sorted(votes, key=lambda x: (str(x[0].get("date", "")), x[0].get("rollNumber", 0))):
        vote, mem = parse_vote_xml(_xml(v["url"]), v["url"])
        ctx = {"bill": f"{p['congress']} {str(p['billType']).upper()} {p['billNumber']}", "action_date": a.get("actionDate", ""),
               "action_text": a.get("text", "")}
        if members:
            rows += [{**ctx, **m} for m in mem]
        else:
            rows.append({**ctx, **vote})
    keys = []
    for r in rows:                                   # same columns on every row (party columns vary by vote)
        keys += [k for k in r if k not in keys]
    party = sorted(k for k in keys if re.fullmatch(r"(yea|nay|present|not_voting|other)_.+", k))
    keys = [k for k in keys if k not in party and k != "source_url"] + party + (["source_url"] if "source_url" in keys else [])
    return [{k: r.get(k, "") for k in keys} for r in rows]


def fetch(spec, pack, route):
    tables, notes = {}, []
    for r in spec["requests"]:
        ep_name = r.get("endpoint")
        if ep_name not in CUSTOM:
            hint = phrase_order_hint(r)
            try:
                t, n = gr.fetch(pack, {"requests": [r]})
            except DataError as e:
                if hint and "returned no rows" in str(e):
                    raise DataError(f"{e} {hint}", kind=e.kind) from None
                raise
            tables.update(t)
            notes += n + ([f"{r['name']}: no titles matched. {hint}"] if hint and not t.get(r["name"]) else [])
            title_note = display_title_note(r)
            notes += [title_note] if title_note and title_note not in notes else []     # for_each: once
            continue
        p = r.get("params") or {}
        if ep_name == "roll_call":
            rows, n = _roll_call(pack, p)
            notes += n
        elif ep_name == "senate_votes":
            url = senate_menu_url(p["congress"], p["session"])
            rows = parse_senate_menu(_xml(url), url)
        else:
            rows = _bill_votes(pack, r)
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows (no recorded votes, or a wrong roll number)",
                            kind="bad_request")
        tables[r["name"]] = rows
    return tables, notes


# ---------------------------------------------------------------------- keys, manual route
def _calls(spec) -> int:
    eps = _eps()
    n = 0
    for r in spec.get("requests") or []:
        if r.get("endpoint") in ("roll_call", "senate_votes"):
            continue                                 # official XML: no key, no quota
        pg = (eps.get(r.get("endpoint") or "") or {}).get("paging") or {}
        size = int(pg.get("size") or 250)
        n += max(1, math.ceil(int(r.get("max_rows") or size) / size)) if pg.get("type") == "offset" else 1
    return n


def key_only_needs(spec):
    n = _calls(spec)
    if n > DEMO_LIMIT:
        return [f"about {n} Congress.gov API calls (the keyless DEMO_KEY allows only about 10 an hour)"]
    return []


def manual_items(spec, pack):
    items = []
    for r in spec.get("requests") or []:
        p, name = r.get("params") or {}, r.get("name")
        try:
            if r.get("endpoint") == "roll_call":
                if str(p.get("chamber")).lower() == "senate":
                    url = senate_url(p["congress"], p["session"], p["roll"])
                else:
                    url = house_url(p.get("year") or session_year(int(p["congress"]), int(p["session"])), p["roll"])
                items.append({"name": f"{name}#1", "request": name, "url": url, "filename": f"{name}.xml",
                              "needs_key": False, "method": "GET", "body": None, "note": "official roll-call XML, no key"})
                continue
            if r.get("endpoint") == "senate_votes":
                items.append({"name": f"{name}#1", "request": name, "url": senate_menu_url(p["congress"], p["session"]),
                              "filename": f"{name}.xml", "needs_key": False, "method": "GET", "body": None,
                              "note": "Senate vote list XML, no key"})
                continue
        except (KeyError, ValueError, TypeError):
            continue
        rr = r
        if r.get("endpoint") == "bill_votes":
            rr = {**r, "endpoint": "bill_actions", "params": {k: v for k, v in p.items() if k in ("congress", "billType", "billNumber")}}
        if str((rr.get("params") or {}).get("congress", "")).lower() == "current":
            rr = {**rr, "params": {**rr["params"], "congress": current_congress()}}
        rr = {**rr, "params": {k: v for k, v in (rr.get("params") or {}).items() if k not in ("title_contains",)}}
        for it in gr.manual_items(pack, {"requests": [rr]}):
            if "api_key=" not in it["url"]:              # the generic items may carry it already
                it["url"] += ("&" if "?" in it["url"] else "?") + "api_key=DEMO_KEY"
            note = "DEMO_KEY is api.data.gov's public demo key (about 10 requests an hour); replace it with your own key"
            if r.get("endpoint") == "bill_votes":
                note = ("the bill's actions; each recordedVotes entry links a vote XML file (clerk.house.gov or "
                        "senate.gov) to save as well, from a web browser if a script is refused; " + note)
            it["note"] = (it.get("note", "") + "; " if it.get("note") else "") + note
            items.append(it)
    return items


def parse_upload(pack, req, paths):
    ep = req.get("endpoint")
    if ep in ("roll_call", "senate_votes"):
        rows = []
        for pth in paths:
            with open(pth, "rb") as f:
                root = ET.fromstring(f.read())
            summary = str((req.get("params") or {}).get("summary", "")).lower() in ("true", "1", "yes")
            rows += parse_senate_menu(root, os.path.basename(pth)) if ep == "senate_votes" else \
                [parse_vote_xml(root, os.path.basename(pth))[0]] if summary else parse_vote_xml(root, os.path.basename(pth))[1]
        return rows
    if ep == "bill_votes":
        rows = []
        for pth in paths:
            if pth.lower().endswith(".xml"):
                with open(pth, "rb") as f:
                    rows.append(parse_vote_xml(ET.fromstring(f.read()), os.path.basename(pth))[0])
                continue
            with open(pth, encoding="utf-8-sig") as f:
                data = json.load(f)
            for a in data.get("actions") or []:
                for v in a.get("recordedVotes") or []:
                    rows.append({"action_date": a.get("actionDate", ""), "chamber": str(v.get("chamber", "")).lower(),
                                 "congress": v.get("congress", ""), "session": v.get("sessionNumber", ""),
                                 "roll": v.get("rollNumber", ""), "date": str(v.get("date", ""))[:10], "source_url": v.get("url", ""),
                                 "action_text": a.get("text", "")})
        seen, out = set(), []
        for r in rows:                               # actions repeat each vote: keep one row per vote
            k = r.get("source_url") or id(r)
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out
    return gr.parse_upload(pack, req, paths)

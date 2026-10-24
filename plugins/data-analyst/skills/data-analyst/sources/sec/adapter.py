"""SEC EDGAR adapter (data.sec.gov XBRL and submissions APIs, efts.sec.gov full-text search). Keyless.

User-Agent: SEC's fair-access policy asks every automated client to identify itself with a contact
email ("Sample Company Name AdminContact@<sample company domain>.com") and to stay under 10 requests a
second. The pack sends SEC_USER_AGENT (environment variable, or "env" in keys.json or Claude Code's settings.json)
when set, else a generic tool name. It never sends the shared default User-Agent, because SEC's
firewall answers HTTP 403 to User-Agents that contain a GitHub URL (verified 2026-10-20).
data.sec.gov and efts.sec.gov accept the generic name; www.sec.gov (company_tickers.json, the
Archives) needs a contact email, so this pack resolves tickers with efts instead.

Every response is reshaped here (columnar submissions arrays, nested XBRL units), so fetch() replaces
the generic fetch. Published values are never changed: rows keep val, unit, fy, fp, form, filed, frame.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import keys  # noqa: E402
from core.http import DataError  # noqa: E402

DEFAULT_UA = "data-analyst/1.0 (U.S. government open data analysis tool)"
UA_ENV = "SEC_USER_AGENT"
EFTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
CO_EPS = ("submissions", "company_concept", "company_facts", "insider_filings")


def user_agent() -> str:
    ua, _ = keys.find_key(UA_ENV)
    ua = (ua or "").strip()
    if not ua or "github" in ua.lower():
        return DEFAULT_UA
    return ua


def client(pack):
    return gr.client_for(pack, user_agent=user_agent())


def check_error(status, body):
    head = body[:300]
    if head[:1] == b"{" and (b'"errorType"' in head or b'"errorMessage"' in head):
        return "EDGAR search is temporarily failing (" + head.decode("utf-8", "replace")[:160] + "); try again in a minute"
    if head.lstrip()[:1] == b"<" and b"Request Rate Threshold" in body[:2000]:
        return ("SEC refused the request (rate threshold / undeclared automated tool). Set SEC_USER_AGENT to "
                "'Your Name your.email@example.org' and keep under 10 requests a second")
    return None


def pad(cik) -> str:
    s = re.sub(r"\D", "", str(cik))
    if not s:
        raise DataError(f"not a CIK: {cik!r}", kind="bad_request")
    return s.zfill(10)


def lookup(c, text: str, limit: int = 10) -> list[dict]:
    data = c.get_json(EFTS, {"keysTyped": text.strip()})
    out = []
    for h in ((data.get("hits") or {}).get("hits") or [])[:limit]:
        src = h.get("_source") or {}
        cik = str(h.get("_id") or "")
        if not cik.isdigit():
            continue
        out.append({"cik": cik.zfill(10), "name": re.sub(r"\s*\([^)]*\)\s*$", "", str(src.get("entity") or "")).strip(),
                    "tickers": src.get("tickers") or "", "entity": src.get("entity") or ""})
    return out


def resolve_cik(c, params) -> str:
    if params.get("cik"):
        return pad(params["cik"])
    t = str(params.get("ticker") or "").strip()
    if re.fullmatch(r"\d{1,10}", t):
        return pad(t)
    hits = lookup(c, t)
    want = t.upper()
    exact = [h for h in hits if want in [x.strip().upper() for x in str(h["tickers"]).split(",")]]
    if exact:
        return exact[0]["cik"]
    named = [h for h in hits if h["name"].upper() == want]
    if named:
        return named[0]["cik"]
    raise DataError(f"no SEC registrant has the ticker or exact name {t!r}. Candidates: "
                    + "; ".join(f"{h['entity']} CIK {h['cik']}" for h in hits[:5])
                    + ". Put the right one in params.cik.", kind="bad_request")


def _split(v) -> list[str]:
    if v in (None, ""):
        return []
    return [x.strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]


def _true(v) -> bool:
    return str(v).lower() in ("true", "1", "yes")


DURATIONS = {"annual": (350, 380), "quarter": (80, 100)}


def _days(start, end):
    """Length of a duration fact in days (end - start + 1); "" for instant facts (no start)."""
    from datetime import date
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except (TypeError, ValueError):
        return ""


def unit_for(endpoint: str, unit):
    """A ratio unit is keyed 'USD/shares' in company_concept and company_facts but written 'USD-per-shares'
    in the frames URL (SEC's API doc; verified 2026-10-22: company_concept with USD-per-shares keeps no
    facts, frames with USD/shares is HTTP 404). Accept either spelling and send the one the endpoint uses."""
    if not unit:
        return unit
    u = str(unit)
    return u.replace("/", "-per-") if endpoint == "frames" else u.replace("-per-", "/")


def _fact_rows(cik, entity, taxonomy, tag, concept, params) -> list[dict]:
    forms, unit = _split(params.get("forms")), unit_for("company_concept", params.get("unit"))
    rows = []
    for u, obs in (concept.get("units") or {}).items():
        if unit and u != unit:
            continue
        for o in obs:
            if forms and not any(str(o.get("form", "")).startswith(f) for f in forms):
                continue
            if _true(params.get("frames_only")) and not o.get("frame"):
                continue
            days = _days(o.get("start"), o.get("end"))
            want = params.get("duration")
            if want and not (days != "" and DURATIONS[want][0] <= days <= DURATIONS[want][1]):
                continue
            rows.append({"cik": cik, "entity_name": entity, "taxonomy": taxonomy, "tag": tag,
                         "label": concept.get("label") or "", "unit": u, "start": o.get("start", ""), "end": o.get("end", ""),
                         "period_days": days,
                         "val": o.get("val"), "fy": o.get("fy", ""), "fp": o.get("fp", ""), "form": o.get("form", ""),
                         "filed": o.get("filed", ""), "accn": o.get("accn", ""), "frame": o.get("frame", "")})
    return rows


def _filings(cik, cols: dict, profile: dict) -> list[dict]:
    keys_ = list(cols.keys())
    n = max((len(v) for v in cols.values() if isinstance(v, list)), default=0)
    rows = []
    for i in range(n):
        r = {k: (cols[k][i] if isinstance(cols[k], list) and i < len(cols[k]) else "") for k in keys_}
        acc = str(r.get("accessionNumber", ""))
        doc = r.get("primaryDocument") or ""
        r["url"] = f"{ARCHIVES}/{int(cik)}/{acc.replace('-', '')}/{doc}" if acc and doc else ""
        r["index_url"] = f"{ARCHIVES}/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm" if acc else ""
        rows.append({**profile, **r})
    return rows


def _efts_rows(data) -> list[dict]:
    rows = []
    for h in ((data.get("hits") or {}).get("hits") or []):
        s = h.get("_source") or {}
        adsh = s.get("adsh") or str(h.get("_id", "")).split(":")[0]
        fname = str(h.get("_id", "")).split(":", 1)[1] if ":" in str(h.get("_id", "")) else ""
        ciks = s.get("ciks") or []
        first = str(int(ciks[0])) if ciks and str(ciks[0]).isdigit() else ""
        rows.append({"adsh": adsh, "form": s.get("form", ""), "file_date": s.get("file_date", ""),
                     "period_ending": s.get("period_ending") or "", "file_type": s.get("file_type") or "",
                     "file_description": s.get("file_description") or "",
                     "display_names": "; ".join(s.get("display_names") or []), "ciks": "; ".join(ciks),
                     "sics": "; ".join(s.get("sics") or []), "biz_states": "; ".join(s.get("biz_states") or []),
                     "items": "; ".join(s.get("items") or []),
                     "url": f"{ARCHIVES}/{first}/{adsh.replace('-', '')}/{fname}" if first and fname else ""})
    return rows


def _efts_all(c, q: dict, limit: int) -> tuple[list[dict], int | None]:
    rows, start, total = [], 0, None
    while len(rows) < limit and start < 10000:
        data = c.get_json(EFTS, {**q, "from": start} if start else q)
        t = (data.get("hits") or {}).get("total") or {}
        total = t.get("value") if isinstance(t, dict) else t
        got = _efts_rows(data)
        rows += got
        if len(got) < 100:
            break
        start += len(got)
    return rows[:limit], total


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        p, n, ep = r.get("params") or {}, r.get("name"), r.get("endpoint")
        if ep in CO_EPS and not (p.get("cik") or p.get("ticker")):
            probs.append(f"request {n!r}: {ep} needs params.cik or params.ticker")
        if ep == "frames" and p.get("period") and not re.fullmatch(r"CY\d{4}(Q[1-4]I?)?", str(p["period"])):
            probs.append(f"request {n!r}: period must look like CY2023, CY2023Q1 or CY2023Q4I")
        if p.get("duration") and p["duration"] not in DURATIONS:
            probs.append(f"request {n!r}: duration must be annual or quarter")
    return probs


def fetch(spec, pack, route):
    c = client(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            raise DataError("; ".join(probs), kind="bad_request")
        kind, name = r.get("endpoint"), r["name"]
        limit = int(r.get("max_rows") or ep.get("max_rows") or 100000)
        if kind == "company_search":
            rows = lookup(c, params["text"], 20)
        elif kind == "submissions":
            cik = resolve_cik(c, params)
            d = c.get_json(f"/submissions/CIK{cik}.json")
            prof = {"cik": cik, "entity_name": d.get("name", ""), "sic": d.get("sic", ""),
                    "sic_description": d.get("sicDescription", ""), "tickers": "; ".join(d.get("tickers") or [])}
            rows = _filings(cik, (d.get("filings") or {}).get("recent") or {}, prof)
            older = (d.get("filings") or {}).get("files") or []
            if _true(params.get("all")):
                for f in older:
                    rows += _filings(cik, c.get_json(f"/submissions/{f['name']}"), prof)
            elif older:
                notes.append(f"{name}: older filings are in {len(older)} more file(s); set params.all=true to include them.")
            forms, since = _split(params.get("forms")), params.get("since")
            if forms:
                rows = [x for x in rows if x.get("form") in forms]
            if since:
                rows = [x for x in rows if str(x.get("filingDate", "")) >= since]
        elif kind == "company_concept":
            cik = resolve_cik(c, params)
            tax = params.get("taxonomy") or "us-gaap"
            d = c.get_json(f"/api/xbrl/companyconcept/CIK{cik}/{tax}/{params['tag']}.json")
            rows = _fact_rows(cik, d.get("entityName", ""), tax, d.get("tag", params["tag"]), d, params)
        elif kind == "company_facts":
            cik = resolve_cik(c, params)
            d = c.get_json(f"/api/xbrl/companyfacts/CIK{cik}.json")
            tags, tax_only = set(_split(params.get("tags"))), params.get("taxonomy")
            rows = []
            for tax, concepts in (d.get("facts") or {}).items():
                if tax_only and tax != tax_only:
                    continue
                for tag, concept in concepts.items():
                    if tags and tag not in tags:
                        continue
                    rows += _fact_rows(cik, d.get("entityName", ""), tax, tag, concept, params)
            missing = sorted(tags - {x["tag"] for x in rows})
            if missing:
                notes.append(f"{name}: not reported by this company (or filtered out): {', '.join(missing)}")
        elif kind == "frames":
            tax, unit = params.get("taxonomy") or "us-gaap", unit_for("frames", params.get("unit") or "USD")
            d = c.get_json(f"/api/xbrl/frames/{tax}/{params['tag']}/{unit}/{params['period']}.json")
            base = {"taxonomy": tax, "tag": d.get("tag", params["tag"]), "unit": d.get("uom", unit), "period": d.get("ccp", params["period"])}
            rows = [{**base, **{k: v for k, v in x.items()}} for x in d.get("data") or []]
        elif kind == "full_text_search":
            q = {k: params[k] for k in ("q", "forms", "startdt", "enddt", "ciks") if params.get(k)}
            rows, total = _efts_all(c, q, limit)
            if total and total > len(rows):
                notes.append(f"{name}: {len(rows):,} of {total:,}{'+' if total >= 10000 else ''} matching documents; "
                             "narrow the dates or forms (the index returns at most 10,000).")
        elif kind == "insider_filings":
            cik = resolve_cik(c, params)
            q = {"forms": params.get("forms") or "4", "ciks": cik}
            q.update({k: params[k] for k in ("startdt", "enddt") if params.get(k)})
            docs, total = _efts_all(c, q, limit)
            rows = []
            for x in docs:
                cks, names = x["ciks"].split("; "), x["display_names"].split("; ")
                i = next((j for j, v in enumerate(cks) if v != cik), None)
                rows.append({"issuer_cik": cik, "form": x["form"], "file_date": x["file_date"],
                             "period_ending": x["period_ending"],
                             "insider": re.sub(r"\s*\(CIK \d+\)\s*$", "", names[i]).strip() if i is not None and i < len(names) else "",
                             "insider_cik": cks[i] if i is not None else "", "adsh": x["adsh"],
                             "index_url": f"{ARCHIVES}/{int(cik)}/{x['adsh'].replace('-', '')}/{x['adsh']}-index.htm"})
            rows.sort(key=lambda z: z["file_date"], reverse=True)
        else:
            raise DataError(f"unknown endpoint {kind!r}", kind="bad_request")
        if len(rows) > limit:
            notes.append(f"{name}: stopped at {limit:,} of {len(rows):,} rows (max_rows).")
            rows = rows[:limit]
        if not rows and not r.get("allow_empty"):
            raise DataError(f"request {name!r} returned no rows: check the tag (case-sensitive), unit, forms filter "
                            "or period (see source.md)", kind="bad_request")
        tables[name] = [{k: ("" if v is None else v) for k, v in row.items()} for row in rows]
    return tables, notes


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    """data.sec.gov and efts JSON links; a browser sends its own User-Agent, which SEC accepts."""
    items = []
    for r in spec.get("requests") or []:
        p, kind, name = r.get("params") or {}, r.get("endpoint"), r.get("name")
        cik = None
        if p.get("cik") or re.fullmatch(r"\d{1,10}", str(p.get("ticker") or "")):
            cik = pad(p.get("cik") or p.get("ticker"))
        url, note = None, ""
        tax = p.get("taxonomy") or "us-gaap"
        if kind == "submissions" and cik:
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        elif kind == "company_concept" and cik and p.get("tag"):
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{tax}/{p['tag']}.json"
        elif kind == "company_facts" and cik:
            url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        elif kind == "frames" and p.get("tag") and p.get("period"):
            url = f"https://data.sec.gov/api/xbrl/frames/{tax}/{p['tag']}/{unit_for('frames', p.get('unit') or 'USD')}/{p['period']}.json"
        elif kind in ("full_text_search", "company_search", "insider_filings"):
            q = ({"keysTyped": p.get("text", "")} if kind == "company_search" else
                 {k: p[k] for k in ("q", "forms", "startdt", "enddt", "ciks") if p.get(k)})
            if kind == "insider_filings" and cik:
                q.update(forms=p.get("forms") or "4", ciks=cik)
            url, note = EFTS + "?" + urllib.parse.urlencode(q), "first 100 hits; add &from=100, &from=200 ... for more"
        if kind in CO_EPS and not cik:
            note = "look up the CIK first (https://www.sec.gov/edgar/searchedgar/companysearch) and put it in params.cik"
        if url:
            items.append({"name": f"{name}#1", "request": name, "url": url, "filename": f"{name}.json",
                          "needs_key": False, "note": note})
    return items


def parse_upload(pack, req, paths):
    ep, params, _ = gr.resolve(pack, req)
    kind = req.get("endpoint")
    rows = []
    for p in sorted(paths):
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        if kind == "submissions":
            cik = pad(d.get("cik") or params.get("cik"))
            prof = {"cik": cik, "entity_name": d.get("name", ""), "sic": d.get("sic", ""),
                    "sic_description": d.get("sicDescription", ""), "tickers": "; ".join(d.get("tickers") or [])}
            recent = (d.get("filings") or {}).get("recent") if "filings" in d else d
            rows += _filings(cik, recent or {}, prof)
        elif kind == "company_concept":
            rows += _fact_rows(pad(d.get("cik")), d.get("entityName", ""), d.get("taxonomy", ""), d.get("tag", ""), d, params)
        elif kind == "company_facts":
            tags = set(_split(params.get("tags")))
            for tax, concepts in (d.get("facts") or {}).items():
                for tag, concept in concepts.items():
                    if not tags or tag in tags:
                        rows += _fact_rows(pad(d.get("cik")), d.get("entityName", ""), tax, tag, concept, params)
        elif kind == "frames":
            base = {"taxonomy": d.get("taxonomy", ""), "tag": d.get("tag", ""), "unit": d.get("uom", ""), "period": d.get("ccp", "")}
            rows += [{**base, **x} for x in d.get("data") or []]
        elif kind == "company_search":
            rows += [{"cik": str(h.get("_id", "")).zfill(10), "entity": (h.get("_source") or {}).get("entity", ""),
                      "tickers": (h.get("_source") or {}).get("tickers", "")} for h in (d.get("hits") or {}).get("hits") or []]
        else:
            rows += _efts_rows(d)
    return [{k: ("" if v is None else v) for k, v in r.items()} for r in rows]


def search(query, pack, limit: int = 20):
    """Common XBRL concepts (endpoints.json "concepts") plus the endpoint descriptions."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    hits = []
    for tag, desc in ((pack.endpoints or {}).get("concepts") or {}).items():
        s = sum(f"{tag} {desc}".lower().count(w) for w in words)
        if s:
            hits.append((s, {"tag": tag, "description": desc}))
    out = [h for _, h in sorted(hits, key=lambda x: -x[0])] + gr.search_catalog(pack, query, limit)
    return out[:limit]

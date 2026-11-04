"""USITC Harmonized Tariff Schedule REST API: rate columns made usable for duty estimates.

The API gives duty rates as published text ("2.5%", "Free", "1.7¢/kg on lead content", "The duty provided in the
applicable subheading + 25%") and only on the line that carries the rate (usually the 8-digit subheading); the
10-digit statistical lines below it are blank. normalize keeps every published column and adds:
  rate_line            the HTS number whose rates apply to this line: itself, or the nearest line above it
                       with a general rate whose number this line's number extends (statistical suffixes);
                       empty for label rows and for search hits whose rate line is not in the reply
  general_effective    the general (column 1) rate of that line, as published
  general_rate_type    free | ad_valorem | specific | compound | chapter99 | other | none
  general_ad_valorem_pct  the percent as a number when the effective rate is Free (0) or a plain percent;
                       empty for specific and compound rates (they need unit values to convert)
  chapter99_refs       9903.xx.xx headings named in the line's footnotes (additional duties that may apply)
and writes footnotes ("[other] See 9903.90.08.") and units ("No., kg") as text.
"""
import json
import re

_PCT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_CH99 = re.compile(r"\b99\d\d\.\d\d\.\d\d\b")
_SPECIFIC = re.compile(r"(¢|\$)\s*\d|\d\s*(¢|\$)|/(kg|liter|l|doz|no|pr|m2|t|each|head|gross|pf\.? liter)\b", re.I)


def rate_type(text):
    t = (text or "").strip()
    if not t:
        return "none"
    if t.lower().startswith("free"):
        return "free"
    if t.lower().startswith("the duty provided"):
        return "chapter99"
    if _PCT.match(t):
        return "ad_valorem"
    has_pct = "%" in t
    has_spec = bool(_SPECIFIC.search(t))
    if has_pct and has_spec:
        return "compound"
    if has_spec:
        return "specific"
    return "other"


def _pct(text, kind):
    if kind == "free":
        return 0.0
    if kind == "ad_valorem":
        return float(_PCT.match(text).group(1))
    return ""


def _digits(hts):
    return re.sub(r"\D", "", hts or "")


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return []


def _foot_text(v):
    out = []
    for f in _as_list(v):
        if isinstance(f, dict):
            val = str(f.get("value") or "").strip()
            cols = ",".join(f.get("columns") or [])
            if val:
                out.append(f"[{cols}] {val}" if cols else val)
        elif f:
            out.append(str(f))
    return " | ".join(out)


def dotted(hts):
    """HTS number in the API's dotted form (8703.80.00.20); "" when it is not 4 to 10 digits in pairs."""
    d = _digits(hts)
    if len(d) not in (4, 6, 8, 10):
        return ""
    return ".".join([d[:4]] + [d[i:i + 2] for i in range(4, len(d), 2)])


def _after(hts):
    """The first number past every line under hts at its level: 8703 -> 8704, 8703.80 -> 8703.81."""
    head, _, last = hts.rpartition(".")
    nxt = str(int(last) + 1).zfill(len(last))
    if len(nxt) > len(last):                 # 9999 or .99: no next number at this level
        return hts + ".99" * ((10 - len(_digits(hts))) // 2) if len(_digits(hts)) < 10 else hts
    return f"{head}.{nxt}" if head else nxt


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        if r.get("endpoint") != "schedule":
            continue
        p = r.get("params") or {}
        for k in ("from", "to"):
            if p.get(k) not in (None, "") and not dotted(str(p[k])):
                probs.append(f"{r.get('name')}: {k}={p[k]!r} is not an HTS number (4, 6, 8 or 10 digits, "
                             "e.g. 8703 or 8703.80)")
        if dotted(str(p.get("from") or "")) and dotted(str(p.get("to") or "")) and \
                dotted(str(p["from"])) > dotted(str(p["to"])):
            probs.append(f"{r.get('name')}: from {p['from']} is after to {p['to']}")
    return probs


def before_request(req, ep, params):
    # The API compares numbers as text and keeps lines <= to, so to=8703 returns the heading row alone, and
    # it answers undotted numbers with nothing. Send dotted numbers and the number just past `to`;
    # normalize cuts the reply there, so `to` includes every line under it.
    if ep.get("path") != "/exportList":
        return params
    params = dict(params)
    if dotted(str(params.get("from") or "")):
        params["from"] = dotted(str(params["from"]))
    if dotted(str(params.get("to") or "")):
        params["to"] = _after(dotted(str(params["to"])))
    return params


def normalize(req, ep, rows):
    if ep.get("path") not in ("/exportList", "/search"):
        return rows
    if ep.get("path") == "/exportList":
        to = dotted(str(((req or {}).get("params") or {}).get("to") or ""))
        if to:
            stop = _after(to)
            for i, r in enumerate(rows):
                hts = (r.get("htsno") or "").strip()
                if hts and hts >= stop:
                    rows = rows[:i]
                    break
    out = []
    parent = {}            # indent -> (htsno, general) of the nearest line above with a general rate
    for r in rows:
        new = {k: v for k, v in r.items() if k not in ("addiitionalDuties", "superior")
               and not k.startswith("highlight") and k not in ("hasHighlight", "htmlStatNote", "selector", "unique")}
        indent = int(str(r.get("indent") or 0) or 0)
        general = (r.get("general") or "").strip()
        hts = (r.get("htsno") or "").strip()
        for k in [k for k in parent if k >= indent]:
            del parent[k]
        if general:
            parent[indent] = (hts, general)
            line, eff = hts, general
        else:
            # only a line whose number the child extends (search results skip lines, label rows have none)
            digits = _digits(hts)
            above = [parent[k] for k in sorted(parent) if k < indent
                     and digits and digits.startswith(_digits(parent[k][0]))]
            line, eff = above[-1] if above else ("", "")
        kind = rate_type(eff)
        foot = _foot_text(r.get("footnotes"))
        new["footnotes"] = foot
        new["units"] = ", ".join(str(u) for u in _as_list(r.get("units")))
        new["rate_line"] = line
        new["general_effective"] = eff
        new["general_rate_type"] = kind
        new["general_ad_valorem_pct"] = _pct(eff, kind)
        new["chapter99_refs"] = " ".join(dict.fromkeys(_CH99.findall(foot)))
        out.append(new)
    return out

"""NAEP adapter: subject aliases and default scales, error-flag decoding, errors inside HTTP 200.

- before_request: subject names are case-insensitive and take aliases (math -> mathematics); the
  subscale defaults to the subject's composite (math grade 12 -> MWPCM, the 2005-framework scale);
  "year" is accepted for "Year".
- normalize: the published values stay as they are. Suppressed estimates arrive as value 999 with
  stdError -1 and isStatDisplayable 0; a `value_flag` column says "suppressed" for those rows, and every
  errorFlag column (errorFlag, focalErrorFlag, errorFlag1, ...) gets a decoded `<column>_notes` column
  from the bit table in the NAEP API documentation.
- fetch / parse_upload: the generic fetch, but a body that is not valid JSON is repaired before parsing when
  the only fault is unescaped double quotes inside string values (type=independentvariables for 2024 puts
  quoted survey items inside longLabel, verified live 2026-10-22); a note says so. Any other malformed
  body still fails.
"""
from __future__ import annotations

import dataclasses
import json
import os

# Error flag bits (https://www.nationsreportcard.gov/api_documentation.aspx, "Error Codes"), and whether
# the statistic is withheld (the Data Explorer shows a symbol instead of the number).
FLAGS = {
    1: ("Reporting standard not met", True), 2: ("Extreme p", True), 4: ("High CV", False),
    8: ("Non-accommodated sample", False), 16: ("Exclusion list", True),
    32: ("Non-qualified independent variable", True), 64: ("No data", True), 128: ("Rounds to zero", False),
    256: ("SE not applicable", False), 512: ("Race note type 1", False),
    2048: ("Response rate below 85%", False), 4096: ("Response rate below 50%", True),
    16384: ("Significance", False), 32768: ("Zero percent response rate", True),
    131072: ("Jurisdiction note", False), 4194304: ("Race note type 2", False),
    8388608: ("Race note type 3", False), 16777216: ("IEP note", False), 33554432: ("IEP2009 note", False),
    67108864: ("DoDEA note", False), 134217728: ("Dallas note", False), 268435456: ("Skipped variables note", False),
}
DISPLAY_COLS = ("isStatDisplayable", "isFocalStatDisplayable", "isTargetStatDisplayable", "statDisplayable1",
                "statDisplayable2", "statDisplayable3", "statDisplayable4", "isSigDisplayable", "isGapDisplayable")


def _catalog():
    from core import packfiles
    return packfiles.read_json(os.path.dirname(os.path.abspath(__file__)), "endpoints.json").get("catalog", {})


def _subject(cat, v):
    s = str(v or "").strip().lower()
    if s in cat and cat[s].get("kind", "").startswith("subject"):
        return cat[s]
    for c in cat.values():
        if c.get("kind", "").startswith("subject") and c["id"] == s:
            return c
    return None


def decode(flag) -> str:
    try:
        n = int(float(flag))
    except (TypeError, ValueError):
        return ""
    return "; ".join(label for bit, (label, _) in FLAGS.items() if n & bit)


def withheld(flag) -> bool:
    try:
        n = int(float(flag))
    except (TypeError, ValueError):
        return False
    return any(n & bit for bit, (_, hide) in FLAGS.items() if hide)


def before_request(req, ep, params):
    cat = _catalog()
    if "year" in params and "Year" not in params:
        params["Year"] = params.pop("year")
    subj = _subject(cat, params.get("subject"))
    if subj:
        params["subject"] = subj["id"]
        if not params.get("subscale") and "subscale" in (ep.get("params") or {}):
            g12 = str(params.get("grade")) == "12" and subj.get("subscale_grade12")
            params["subscale"] = subj["subscale_grade12"] if g12 else subj["subscale"]
    for k in ("Year", "jurisdiction", "stattype"):
        if isinstance(params.get(k), list):
            params[k] = ",".join(str(x) for x in params[k])
    return params


def manual_items(spec, pack):
    """Generic links, built from the same parameters the fetch sends (aliases and default scales)."""
    from core import generic_rest as gr
    s = json.loads(json.dumps(spec))
    for r in s.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if not probs:
            r["params"] = before_request(r, ep, params)
    return gr.manual_items(pack, s)


def check_spec(spec):
    cat = _catalog()
    probs = []
    for r in spec.get("requests") or []:
        p = r.get("params") or {}
        name, ep = r.get("name"), r.get("endpoint")
        subj = _subject(cat, p.get("subject"))
        if p.get("subject") and not subj:
            probs.append(f"request {name!r}: unknown subject {p['subject']!r} (reading, math, science, writing, "
                         "civics, history, geography, economics, tel, music)")
        if subj and subj.get("grades") and p.get("grade") not in (None, "") and int(p["grade"]) not in subj["grades"]:
            probs.append(f"request {name!r}: {subj['id']} is assessed in grades {subj['grades']}")
        years = str(p.get("Year", p.get("year", "")) if not isinstance(p.get("Year"), list) else ",".join(map(str, p["Year"])))
        juris = p.get("jurisdiction", "")
        juris = ",".join(juris) if isinstance(juris, list) else str(juris)
        if ep == "compare_years" and len([y for y in years.split(",") if y]) < 2:
            probs.append(f"request {name!r}: compare_years needs two or more years in Year")
        if ep in ("compare_jurisdictions", "gap_years_across_jurisdictions", "gap_groups_across_jurisdictions") \
                and len([j for j in juris.split(",") if j]) < 2:
            probs.append(f"request {name!r}: {ep} needs two or more jurisdictions")
        if ep == "gap_years_across_jurisdictions" and len([y for y in years.split(",") if y]) != 2:
            probs.append(f"request {name!r}: gap_years_across_jurisdictions needs exactly two years")
        if ep in ("compare_groups", "gap_groups_across_years", "gap_groups_across_jurisdictions") \
                and str(p.get("variable", "")).upper() in ("", "TOTAL"):
            probs.append(f"request {name!r}: {ep} needs a group variable such as GENDER or SDRACE, not TOTAL")
    return probs


def check_error(status, body):
    try:
        d = json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(d, dict) and (d.get("statusCode") not in (None, 200) or isinstance(d.get("result"), str)):
        return "NAEP data service error: " + str(d.get("result"))[:300].split("\n")[0]
    return None


def normalize(req, ep, rows):
    for r in rows:
        hidden = False
        for col in list(r):
            if col.lower().endswith("errorflag") or col.startswith("errorFlag"):
                r[col + "_notes"] = decode(r[col])
                hidden = hidden or withheld(r[col])
        for col in DISPLAY_COLS[:7]:
            if col in r and str(r[col]) == "0":
                hidden = True
        if any(k in r for k in ("value", "focalValue", "stat1")):
            r["value_flag"] = "suppressed" if hidden else ""
    return rows


# ---------------------------------------------------------------------- malformed JSON (stray quotes)
_WS = " \t\r\n"


def repair_quotes(text: str) -> str:
    """Escape double quotes inside JSON strings that the server left unescaped. A quote inside a string
    closes it only when the next non-blank character is structural: ':' '}' ']' or the end, or ','
    followed by the start of another key or value ('"', '{', '[', a digit or '-'). Otherwise it is text."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if not in_str:
            in_str = ch == '"'
            out.append(ch)
        elif ch == "\\":
            out.append(text[i:i + 2])
            i += 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] in _WS:
                j += 1
            nxt = text[j] if j < n else ""
            closes = nxt in ("", ":", "}", "]")
            if nxt == ",":
                k = j + 1
                while k < n and text[k] in _WS:
                    k += 1
                closes = k < n and (text[k] in '"{[-' or text[k].isdigit())
            if closes:
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def loads_tolerant(raw: bytes):
    """(data, repaired): strict JSON first; the stray-quote repair only when that fails. A body the repair
    cannot fix raises the original error."""
    text = raw.decode("utf-8-sig")
    try:
        return json.loads(text), False
    except ValueError as e:
        first = e
    try:
        return json.loads(repair_quotes(text)), True
    except ValueError:
        raise first from None


class _Tolerant:
    """Wraps a core Client: a response that needs the quote repair comes back with the repaired body."""

    def __init__(self, client):
        self._c, self.repaired = client, []

    def __getattr__(self, name):
        return getattr(self._c, name)

    def request(self, *a, **kw):
        resp = self._c.request(*a, **kw)
        if resp.body[:1] not in (b"{", b"["):
            return resp
        try:
            data, fixed = loads_tolerant(resp.body)
        except (ValueError, UnicodeDecodeError):
            return resp                                  # the generic code reports the real error
        if not fixed:
            return resp
        self.repaired.append(resp.url)
        return dataclasses.replace(resp, body=json.dumps(data, ensure_ascii=False).encode("utf-8"))


def fetch(spec, pack, route):
    """The generic fetch through the tolerant client; a note says how many responses needed the repair."""
    from core import generic_rest as gr
    c = _Tolerant(gr.client_for(pack))
    tables, notes = gr.fetch(pack, spec, c)
    if c.repaired:
        notes.append(f"{len(c.repaired)} NAEP response(s) were malformed JSON (unescaped quotes inside text "
                     "labels, e.g. type=independentvariables); the pack escaped them before parsing. Labels "
                     "are as published.")
    return tables, notes


def parse_upload(pack, req, paths):
    """Uploaded pages (JSON as saved from the links, or CSV), with the same quote repair."""
    import csv
    import io
    from core import generic_rest as gr
    ep, _, _ = gr.resolve(pack, req)
    rows = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            raw = f.read()
        if p.lower().endswith(".csv"):
            rows += list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
            continue
        recs = gr.dig(loads_tolerant(raw)[0], ep.get("records", ""))
        rows += [] if recs is None else (recs if isinstance(recs, list) else [recs])
    return normalize(req, ep, gr._clean(rows, ep))


"""Load, check and enforce a source pack's policy.json (standard library only).

policy.json is where a source's rules live so code can enforce them, not just document them. Schema
(fields marked * are required; docs/pack_contract.md in the data-analyst repo explains each with examples):

  *id, *title, *agency, *depth ("deep" | "standard"), *domains [..], keywords [..], questions [..]
  *homepage, terms_url
  *auth        {env, required: "required"|"optional"|"none", signup_url, placement: "query"|"header"|"body"|
                "none", param, prefix, home, echoes_key: bool, key_only: [features a keyless route lacks]}
  *limits      {rps, burst, daily_quota, max_series, max_years, errors_per_min, bytes_per_min, max_rows}
  *routes      {api: {base, probe, hosts}, api_keyless: {...}, bulk: {...}, manual: {how}}  (manual required)
  *cache       {mode: "none"|"run_only"|"persistent", ttl_hours, why}
  *redistribution {mode: "allowed"|"aggregates_only"|"definition_only", microdata_globs: [..], why,
                package_include: [globs under data/<id>/ that MAY leave the analysis folder under
                definition_only, e.g. "request.json", "extract_spec.json", "README.md", "codebook.csv"],
                withhold: {"meta": "series.csv", "key": "series_id", "column": "copyright",
                           "values": [...]} or "shareable": [...] instead of "values" (a whitelist:
                           rows whose meta value is not listed are withheld; FRED uses it)}
  notices      [text that must appear in any report that uses this source]
  retention    {upstream_days, note}
  *citation    template; placeholders {title} {dataset} {retrieved} {retrieved_long} {vintage} {url}
  audit_hooks  [{id, kind: "require_file"|"require_text"|"require_fact_prefix"|"forbid_in_package", ...
               (require_text: "any" phrases and/or "any_pattern" regexes, matched on the lowercased text),
               when_route: [routes] (optional: the hook applies only when data/<id>/request.json has that route),
               when_data: {glob, column, pattern?} (optional: only when some row of those CSVs has that column
               set, or with pattern, a value matching it),
               when_columns: [name parts] (optional: only when data/<id>/request.json's spec fetched such a
               column, or all columns)}]
  clarify      [questions to settle before fetching when the request leaves them open]
  formats      [what the API returns: json, csv, xml, xlsx, parquet, zip, ...]
  aliases      [other names a report's source line may use for this source, e.g. "Bureau of Labor Statistics"]
"""
from __future__ import annotations

import fnmatch
import json
import re
import os

from core import packfiles

REQUIRED = ("id", "title", "agency", "depth", "domains", "homepage", "auth", "limits", "routes", "cache",
            "redistribution", "citation")
CACHE_MODES = ("none", "run_only", "persistent")
REDIST_MODES = ("allowed", "aggregates_only", "definition_only")
AUTH_REQ = ("required", "optional", "none")
PLACEMENTS = ("query", "header", "body", "none")
ROUTE_NAMES = ("api", "api_keyless", "bulk", "manual")
HOOK_KINDS = ("require_file", "require_text", "require_fact_prefix", "forbid_in_package")
# Strictness order when several sources feed one product: the strictest rule wins for shared files.
REDIST_ORDER = {"allowed": 0, "aggregates_only": 1, "definition_only": 2}


class PolicyError(ValueError):
    pass


def load(pack_dir: str) -> dict:
    p = packfiles.read_json(pack_dir, "policy.json")
    if p is None:
        raise FileNotFoundError(os.path.join(pack_dir, "policy.json"))
    p.setdefault("keywords", [])
    p.setdefault("questions", [])
    p.setdefault("notices", [])
    p.setdefault("audit_hooks", [])
    p.setdefault("clarify", [])
    p.setdefault("formats", [])
    p.setdefault("retention", None)
    p["auth"] = {"env": None, "alt_env": [], "required": "none", "placement": "none", "param": None, "prefix": "", "home": None,
                 "echoes_key": False, "key_only": [], "signup_url": None, **(p.get("auth") or {})}
    p["limits"] = {"rps": 5, "burst": 10, **(p.get("limits") or {})}
    p["redistribution"] = {"microdata_globs": [], "package_include": ["request.json", "README.md"], "withhold": None,
                           **(p.get("redistribution") or {})}
    return p


def validate(p: dict, pack_dir: str | None = None) -> list[str]:
    """Problems with a policy (empty list = valid). Used by the structure test and by fetch.py at load."""
    bad = [f"missing field: {k}" for k in REQUIRED if k not in p or p[k] in (None, "", [])]
    if pack_dir and p.get("id") and os.path.basename(os.path.normpath(pack_dir)) != p["id"]:
        bad.append(f"id {p['id']!r} does not match folder {os.path.basename(os.path.normpath(pack_dir))!r}")
    if p.get("depth") not in ("deep", "standard"):
        bad.append("depth must be deep or standard")
    a = p.get("auth") or {}
    if a.get("required") not in AUTH_REQ:
        bad.append(f"auth.required must be one of {AUTH_REQ}")
    if a.get("placement") not in PLACEMENTS:
        bad.append(f"auth.placement must be one of {PLACEMENTS}")
    if a.get("required") in ("required", "optional") and not a.get("env"):
        bad.append("auth.env is required when a key is used")
    if a.get("required") in ("required", "optional") and not a.get("signup_url"):
        bad.append("auth.signup_url is required when a key is used")
    r = p.get("routes") or {}
    if "manual" not in r or not (r["manual"] or {}).get("how"):
        bad.append("routes.manual.how is required: every source needs a manual-download route")
    for name in r:
        if name not in ROUTE_NAMES:
            bad.append(f"unknown route {name!r}")
    for name in ("api", "api_keyless", "bulk"):
        if name in r and not (r[name] or {}).get("probe"):
            bad.append(f"routes.{name}.probe (a cheap URL to test reachability) is required")
    if (p.get("cache") or {}).get("mode") not in CACHE_MODES:
        bad.append(f"cache.mode must be one of {CACHE_MODES}")
    rd = p.get("redistribution") or {}
    if rd.get("mode") not in REDIST_MODES:
        bad.append(f"redistribution.mode must be one of {REDIST_MODES}")
    if rd.get("mode") in ("definition_only", "aggregates_only") and not rd.get("microdata_globs"):
        bad.append(f"redistribution.microdata_globs is required for {rd.get('mode')}")
    if "aliases" in p and not isinstance(p["aliases"], list):
        bad.append("aliases must be a list of names a source line may use for this source")
    if not rd.get("why"):
        bad.append("redistribution.why is required (the terms that set the rule)")
    for h in p.get("audit_hooks") or []:
        if h.get("kind") not in HOOK_KINDS:
            bad.append(f"audit hook {h.get('id')!r}: kind must be one of {HOOK_KINDS}")
        if not h.get("why"):
            bad.append(f"audit hook {h.get('id')!r}: why is required")
    if "{retrieved" not in (p.get("citation") or ""):
        bad.append("citation must contain {retrieved} or {retrieved_long}")
    return bad


# ---------------------------------------------------------------------- enforcement points
def check_cache(p: dict, want: str) -> None:
    """fetch: refuse a cache mode the source's terms do not allow."""
    allowed = p["cache"]["mode"]
    order = {"none": 0, "run_only": 1, "persistent": 2}
    if order[want] > order[allowed]:
        raise PolicyError(f"{p['id']}: cache mode {want!r} is not allowed ({allowed}): {p['cache'].get('why', '')}")


def is_microdata(p: dict, relpath: str) -> bool:
    if p["redistribution"]["mode"] not in ("definition_only", "aggregates_only"):
        return False
    rel = relpath.replace("\\", "/")
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(os.path.basename(rel), g)
               for g in p["redistribution"]["microdata_globs"])


def check_package(p: dict, members: list[str]) -> list[str]:
    """build_report / package: files that must not leave the analysis folder under this source's rules."""
    bad = [m for m in members if is_microdata(p, m)]
    for h in p.get("audit_hooks") or []:
        if h["kind"] == "forbid_in_package":
            bad += [m for m in members if any(fnmatch.fnmatch(m.replace("\\", "/"), g) for g in h.get("globs", []))]
    return sorted(set(bad))


def strictest(policies: list[dict]) -> str:
    return max((p["redistribution"]["mode"] for p in policies), key=lambda m: REDIST_ORDER[m], default="allowed")


def notices(policies: list[dict]) -> list[str]:
    seen, out = set(), []
    for p in policies:
        for n in p.get("notices") or []:
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _has_content(path: str) -> bool:
    """A file with data in it: a CSV needs a row under its header."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [l for l in f.read(1 << 16).splitlines() if l.strip()]
    except OSError:
        return False
    return len(lines) >= (2 if path.lower().endswith(".csv") else 1)


def _route_of(out: str, pid: str) -> str:
    """The route data/<pid>/request.json records (api, bulk, manual_download ...), "" when unknown."""
    try:
        with open(os.path.join(out, "data", pid, "request.json"), encoding="utf-8") as f:
            return str(json.load(f).get("route") or "")
    except (OSError, ValueError):
        return ""


def _columns_used(out: str, pid: str, parts: list[str]) -> bool:
    """Did the fetch take a column whose name contains one of parts? Unknown or all columns (no list) count as yes."""
    try:
        with open(os.path.join(out, "data", pid, "request.json"), encoding="utf-8") as f:
            cols = (json.load(f).get("spec") or {}).get("columns")
    except (OSError, ValueError):
        return True
    return not cols or any(p in str(c).lower() for c in cols for p in parts)


def _data_flagged(out: str, cond: dict) -> bool:
    """Does any row of the CSV files cond["glob"] (under out) have cond["column"] set (1, true, yes, Y, P), or,
    with cond["pattern"], a value matching that regex (a footnote's text)?"""
    import csv
    import glob
    pat = re.compile(cond["pattern"], re.I) if cond.get("pattern") else None
    for path in glob.glob(os.path.join(out, cond.get("glob", ""))):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                if any((pat.search(str(r.get(cond.get("column", ""), ""))) if pat else
                        str(r.get(cond.get("column", ""), "")).strip().lower() in ("1", "true", "yes", "y", "p"))
                       for r in csv.DictReader(f)):
                    return True
        except OSError:
            continue
    return False


def run_audit_hooks(p: dict, out: str, report: dict | None, facts: dict | None,
                    html: str | None = None) -> list[tuple[str, str, str]]:
    """audit_report: (status, hook id, message) per hook; status PASS or FAIL. require_text searches the
    report.json text and, when given, the built HTML."""
    res = []
    text = (json.dumps(report or {}, ensure_ascii=False) + " " + (html or "")).lower()
    for h in p.get("audit_hooks") or []:
        hid, kind = h.get("id", "?"), h["kind"]
        if h.get("when_route"):              # e.g. an API-terms notice: only when that route brought the data
            route = _route_of(out, p.get("id", ""))
            if route not in h["when_route"]:
                res.append(("PASS", hid, f"not needed: data came by route {route or 'unknown'} ({h['why']})"))
                continue
        if h.get("when_columns") and not _columns_used(out, p.get("id", ""), h["when_columns"]):
            res.append(("PASS", hid, f"not needed: no column matching {h['when_columns']} was fetched ({h['why']})"))
            continue
        if h.get("when_data") and not _data_flagged(out, h["when_data"]):   # e.g. only if a value is preliminary
            res.append(("PASS", hid, f"not needed: no row of {h['when_data'].get('glob')} has "
                                     f"{h['when_data'].get('column')} set ({h['why']})"))
            continue
        if kind == "require_file":
            import glob
            hits = [x for x in glob.glob(os.path.join(out, h["glob"])) if _has_content(x)]   # an empty file is not one
            res.append(("PASS" if hits else "FAIL", hid, f"{h['glob']}: {'found' if hits else 'missing'} ({h['why']})"))
        elif kind == "require_text":
            words = [w.lower() for w in h.get("any", [])]
            ok = any(w in text for w in words) or any(re.search(x, text) for x in h.get("any_pattern", []))
            res.append(("PASS" if ok else "FAIL", hid, f"report mentions one of {words}: {ok} ({h['why']})"))
        elif kind == "require_fact_prefix":
            ok = any(str(k).startswith(h["prefix"]) for k in (facts or {}))
            res.append(("PASS" if ok else "FAIL", hid, f"a fact named {h['prefix']}*: {ok} ({h['why']})"))
    return res


def local_day(stamp) -> str:
    """The user's calendar date of a retrieval time: "2026-10-23T00:02:50+00:00" is October 22 at 8 pm in New
    York. A plain date is kept."""
    from datetime import datetime
    s = str(stamp or "")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    return (d.astimezone() if d.tzinfo else d).date().isoformat()


def citation(p: dict, **vals) -> str:
    from datetime import datetime
    if vals.get("retrieved"):
        vals["retrieved"] = local_day(vals["retrieved"])
    r = vals.get("retrieved", "")
    try:
        d = datetime.fromisoformat(str(r)[:10])
        vals.setdefault("retrieved_long", f"{d.strftime('%B')} {d.day}, {d.year}")
    except ValueError:
        vals.setdefault("retrieved_long", r)
    vals.setdefault("title", p["title"])
    vals.setdefault("dataset", "")
    vals.setdefault("vintage", "")
    vals.setdefault("url", p.get("homepage", ""))

    class _D(dict):
        def __missing__(self, k):
            return "{" + k + "}"
    return p["citation"].format_map(_D(vals))

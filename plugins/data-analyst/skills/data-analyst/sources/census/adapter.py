"""Census Data API adapter (standard pack, own fetch because dataset paths contain slashes).

What it adds to the generic REST flow:
- datasets: "latest/acs/acs5", "latest/acs/acs1/subject", "latest/pep/charv" (or the catalog aliases acs5,
  acs1, ...) resolve to the newest vintage the API actually serves, by asking the dataset discovery
  document /data/<year>/<path>.json from last year downward (no hard-coded year).
- geography: state (name, USPS or FIPS, via core combine.state), county and place names resolve to codes
  with "Did you mean" suggestions. Names come from the same dataset's NAME list when a key is set (right
  vintage); otherwise, or when that call fails, from the Census 2020 reference files on www2.census.gov
  (noted in the output; Connecticut's planning regions used from ACS 2022 on are not in them).
- margins of error: for ACS every ...E estimate gets its ...M margin of error (...PE gets ...PM) as its
  own column. They are never dropped (moe: false is refused for ACS).
- normalize: Census special values (-666666666, -999999999, -888888888, -222222222, -333333333,
  -555555555; anything <= -1,000,000) become empty cells with a `<column>_flag` column that says what the
  code means; null cells get a flag too; top-coded medians (home value 2000001, gross rent 2001/3501/4001)
  keep their value and get a flag. Values are otherwise exactly as published (text).
- errors: since 2026-05-12 the API requires a key and redirects keyless data queries to missing_key.html
  (bad keys to invalid_key.html); these become key errors, so --check routes to setup or manual download.
  A query that matches no geography answers HTTP 204: reported as "no rows".
- search: variables.json of a dataset (default: latest ACS 5-year detailed tables), cached on disk for
  30 days (metadata only; data queries are never cached), plus the catalog of common variables.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import re
import sys
import urllib.parse
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
WWW2 = "https://www2.census.gov/geo/docs/reference/codes2020"
SENTINELS = {
    -666666666: "-666666666: estimate could not be computed (too few sample observations, or a median in an open-ended interval)",
    -999999999: "-999999999 (N): cannot be displayed, too few sample cases in this area",
    -888888888: "-888888888 (X): not applicable or not available",
    -222222222: "-222222222 (**): margin of error could not be computed, too few sample observations",
    -333333333: "-333333333 (***): margin of error could not be computed, median in an open-ended interval",
    -555555555: "-555555555 (*****): estimate is controlled; no sampling error, treat the margin of error as zero",
}
TOPCODES = {"B25077_001E": {"2000001": "top-coded: $2,000,000 or more"},
            "B25064_001E": {"2001": "top-coded: $2,000 or more", "3501": "top-coded: $3,500 or more",
                            "4001": "top-coded: $4,000 or more"}}
MAX_VARS = 50
_LATEST: dict = {}
# SIPP microdata: one row per person-month; these come with every request (keys to link months, the weight)
SIPP_KEEP = ["SSUID", "PNUM", "MONTHCODE", "WPFINWGT"]


# ---------------------------------------------------------------------- helpers
def _combine():
    p = os.path.join(HERE, "..", "..", "scripts", "analysis")
    if p not in sys.path:
        sys.path.insert(0, p)
    import combine
    return combine


def _state(x, as_="fips"):
    combine = _combine()
    try:
        return combine.state(x, as_)
    except ValueError as e:
        names = [n for _, _, n in combine.STATES]
        close = difflib.get_close_matches(str(x).strip().title(), names, n=3, cutoff=0.6)
        raise ValueError(f"{e}." + (f" Did you mean: {'; '.join(close)}?" if close else "")) from None


_PACK = []


def _pack():
    if not _PACK:
        from core import packs
        _PACK.append(packs.load("census"))
    return _PACK[0]


def _client(pack=None):
    from core import generic_rest as gr
    return gr.client_for(pack or _pack())


def check_error(status, body: bytes):
    head = body[:600].lstrip().lower()
    if head.startswith(b"<"):
        if b"missing key" in head:
            return ("the Census Data API refused a request without a key (redirect to missing_key.html; a key is "
                    "required for every data query since 2026-05-12): set CENSUS_API_KEY")
        if b"invalid key" in head:
            return "the Census Data API rejected the key (redirect to invalid_key.html): check CENSUS_API_KEY"
    return None


def _get(client, path, params=None, allow_404=False):
    from core.http import DataError
    try:
        r = client.request("GET", path, params, ok_statuses=(200, 204), allow_404=allow_404)
    except DataError as e:
        m = str(e)
        if e.kind == "api" and "missing_key" in m:
            raise DataError(m, kind="key_missing", status=e.status)
        if e.kind == "api" and "invalid_key" in m:
            raise DataError(m, kind="auth", status=e.status)
        raise
    return r


def _json(r):
    if r.status == 204 or not r.body.strip():
        return None
    return r.json()


def _ensure_list(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


def is_acs(ds: str) -> bool:
    return "/acs/" in f"/{ds.strip('/')}/"


def is_sipp(ds: str) -> bool:
    """SIPP microdata (2024/sipp, latest/sipp): rows are person-months, no geography but the state recode."""
    return bool(re.search(r"(^|/)sipp$", str(ds).strip().strip("/")))


def with_sipp_keys(vars_: list[str]) -> list[str]:
    return vars_ + [v for v in SIPP_KEEP if v not in vars_]


# ---------------------------------------------------------------------- ACS PUMS and CPS microdata
# Person-level public-use microdata on the API: ACS PUMS (acs/acs1/pums, acs/acs5/pums, acs/acs1/pumspr) and
# the CPS (cps/asec/mar, cps/basic/<month>). Every request gets the person keys and the weights; columns past
# the 50-variable limit (replicate weights) are fetched in further calls and joined on the keys; a national
# ACS request is fetched state by state. Rows stream to a CSV on disk (3.4 million ACS persons do not fit in
# memory as dicts), values exactly as published, and a <request>_codebook table gives every code's label.
MICRO = {
    "acs_pums": {"keys": ["SERIALNO", "SPORDER"], "weights": ["PWGTP", "WGTP"],
                 "replicates": {"person": [f"PWGTP{i}" for i in range(1, 81)],
                                "household": [f"WGTP{i}" for i in range(1, 81)]}},
    "cps_asec": {"keys": ["H_SEQ", "A_LINENO", "PERIDNUM"],      # PERIDNUM alone repeats within a household
                 "weights": ["MARSUPWT", "HSUP_WGT"]},
    "cps_basic": {"keys": ["HRHHID", "HRHHID2", "PULINENO"], "weights": ["PWSSWGT", "PWCMPWGT"]},
}
MICRO_NOT_PARAMS = ("dataset", "get", "state", "replicates", "moe", "county", "place", "zcta")


def micro_kind(ds: str) -> str | None:
    """'acs_pums', 'cps_asec' or 'cps_basic' for a microdata dataset path (with or without the year)."""
    d = str(ds).strip().strip("/")
    if re.search(r"(^|/)acs/acs[15]/pums(pr)?$", d):
        return "acs_pums"
    if re.search(r"(^|/)cps/asec/mar$", d):
        return "cps_asec"
    if re.search(r"(^|/)cps/basic/[a-z]{3}$", d):
        return "cps_basic"
    return None


def _replicate_sets(kind: str, value) -> list[str]:
    """replicates: true / "person" -> person replicate weights; "household"; "both"."""
    if value in (None, False, "", "false", "False", "none"):
        return []
    sets = MICRO[kind].get("replicates") or {}
    v = "person" if value is True or str(value).lower() == "true" else str(value).lower()
    if not sets:
        raise ValueError({"cps_asec": "CPS ASEC replicate weights are not on the Census API (Census posts them "
                                      "as a separate file per year on census.gov): use the ipums pack (REPWTP, "
                                      "160 replicates) for ASEC standard errors, or drop replicates",
                          "cps_basic": "the CPS basic monthly files have no replicate weights: drop replicates "
                                       "and give an approximate standard error, saying so"}[kind])
    if v not in ("person", "household", "both"):
        raise ValueError(f"replicates must be true, \"person\", \"household\" or \"both\", not {value!r}")
    return (sets["person"] if v in ("person", "both") else []) + (sets["household"] if v in ("household", "both") else [])


def micro_geos(ds: str, kind: str, p: dict) -> tuple[list[dict], list[str]]:
    """The geography parameters of each call: one state, a PUMA list, or (national ACS) one call per state."""
    st, notes = p.get("state"), []
    for k in ("county", "place", "zcta"):
        if p.get(k) not in (None, ""):
            raise ValueError("microdata have no county, place or ZIP geography: the smallest area is the PUMA "
                             "(ACS: for \"public use microdata area:*\" with a state); CPS has states only")
    fips = _state(st, "fips") if st not in (None, "", "*") else None
    year = dataset_year(str(ds).strip("/"))
    if kind == "cps_asec" and year and year < 2001 and (fips or p.get("for") or p.get("ucgid")):
        raise ValueError(f"the API's ASEC files before 2001 take no state geography (HTTP 500; checked 2026-11-04): "
                         f"fetch the nation (no state) and filter on the file's state variable (--search "
                         f"\"{year}/cps/asec/mar: state\"), or use the ipums pack (STATEFIP)")
    if p.get("ucgid"):
        return [{"ucgid": p["ucgid"]}], notes
    if p.get("for"):
        g = {"for": p["for"]}
        if p.get("in") or fips:
            g["in"] = p.get("in") or f"state:{fips}"
        return [g], notes
    if fips:
        return [{"for": f"state:{fips}"}], notes
    if kind == "acs_pums" and not str(ds).rstrip("/").endswith("pumspr"):
        states = [f for _, f, _ in _combine().STATES if int(f) <= 56]
        notes.append(f"national ACS PUMS fetched state by state ({len(states)} calls: 50 states and DC; Puerto "
                     "Rico is the separate pumspr dataset)")
        return [{"for": f"state:{f}"} for f in states], notes
    return [{}], notes                        # the nation: CPS files answer without a geography (any year)


def micro_plan(ds: str, params: dict, meta: dict | None = None, absent: set | frozenset = frozenset()) -> dict:
    """{cols, chunks, join, geos, preds, notes}: the columns asked for, keys and weights added, split into calls
    of at most MAX_VARS variables (each later call repeats the join keys), with the predicates (AGEP=25:54) every
    call repeats so all chunks cover the same persons."""
    kind = micro_kind(ds)
    spec = MICRO[kind]
    p = dict(params)
    user = []
    for v in _ensure_list(p.get("get")):
        if v.upper() != v:
            raise ValueError(f"microdata variable names are upper case: {v!r}")
        if v not in user:
            user.append(v)
    if not user:
        raise ValueError("'get' needs at least one variable (AGEP,HICOV)")
    notes = []
    adj = []
    for v in user:                           # dollar amounts need the adjustment factor beside them
        label = str(((meta or {}).get(v) or {}).get("label", ""))
        for f in ("ADJINC", "ADJHSG"):
            if f in label and f not in user and f not in adj:
                adj.append(f)
    if adj:
        notes.append(f"added {', '.join(adj)}: the dollar variables say to multiply by it (value * {adj[0]} / "
                     "1,000,000 gives the survey year's dollars)")
    reps = _replicate_sets(kind, p.get("replicates"))
    added = [v for v in spec["keys"] + spec["weights"] + adj if v not in user and v not in absent]
    if absent:
        notes.append(f"not in this file, so not added: {', '.join(sorted(absent))} (older years lack some keys "
                     "and weights)")
    notes.append(f"microdata keys and weights added: {', '.join(added)} (weight every estimate; never count rows)")
    if reps:
        notes.append(f"{len(reps)} replicate weights added ({reps[0]}-{reps[-1]}): SE = sqrt(4/80 * sum over r of "
                     "(estimate_r - estimate)^2), Census's method for ACS PUMS")
    cols = user + added + [r for r in reps if r not in user]
    join = [k for k in (spec.get("join") or spec["keys"]) if k not in absent]
    first = [c for c in cols if c in spec["keys"] or c in spec["weights"]]
    rest = [c for c in cols if c not in first]
    room = MAX_VARS - len(first)
    chunks = [rest[:room] + first]
    rest = rest[room:]
    while rest:
        n = MAX_VARS - len(join)
        chunks.append(rest[:n] + join)
        rest = rest[n:]
    if len(chunks) > 1:
        notes.append(f"{len(cols)} columns in {len(chunks)} calls of at most {MAX_VARS} variables, joined on "
                     f"{', '.join(join)}")
    preds = {k: (",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v)
             for k, v in p.items() if k not in MICRO_NOT_PARAMS + ("for", "in", "ucgid") and v not in (None, "")}
    for k in preds:
        if k.upper() != k:
            raise ValueError(f"unknown parameter {k!r} for microdata: filters are variable names in upper case "
                             "(AGEP: \"25:54\", HICOV: 2)")
    geos, gnotes = micro_geos(ds, kind, p)
    return {"kind": kind, "cols": cols, "chunks": chunks, "join": join, "geos": geos, "preds": preds,
            "notes": notes + gnotes}


def _iter_rows(body: bytes):
    """Rows of a Census API JSON array, one line at a time (the API writes one row per line), so a 100 MB body
    is never parsed whole; a body on one line (hand-written fixtures) is parsed at once."""
    if body.count(b"\n") < 2:
        yield from (json.loads(body.decode("utf-8")) or [])
        return
    for line in body.split(b"\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith(b"[["):
            s = s[1:]
        if s.endswith(b"]]"):
            s = s[:-1]
        s = s.rstrip(b",")
        if s:
            yield json.loads(s.decode("utf-8"))


def _merge_chunks(parts: list[tuple[list, object]], join: list[str]) -> tuple[list[str], object]:
    """[(header, row iterator)] of the calls for one geography -> (columns, joined row iterator). The calls return
    the persons in the same order, so rows are joined in step (nothing is held in memory) and every row's keys are
    checked; from the first row out of step on, the rest is joined through a key index; a person missing from a
    call is an error, never a silent blank."""
    from core.http import DataError
    cols, picks = [], []
    for head, _ in parts:
        pick = []
        for i, c in enumerate(head):
            if c not in cols:
                cols.append(c)
                pick.append(i)
        picks.append(pick)
    if len(parts) == 1:
        return cols, ([("" if row[i] is None else row[i]) for i in picks[0]] for row in parts[0][1])
    kidx = [[head.index(k) for k in join] for head, _ in parts]
    its = [iter(rows) for _, rows in parts]

    def take(j, row):
        return [("" if row[i] is None else row[i]) for i in picks[j]]

    def key(j, row):
        return tuple(row[i] for i in kidx[j])

    def gen():
        pending = None
        for row0 in its[0]:
            others = [next(it, None) for it in its[1:]]
            if all(o is not None and key(j, o) == key(0, row0) for j, o in enumerate(others, start=1)):
                yield take(0, row0) + [v for j, o in enumerate(others, start=1) for v in take(j, o)]
                continue
            pending = (row0, others)
            break
        if pending is None:
            if any(next(it, None) is not None for it in its[1:]):
                raise DataError(f"census microdata: a later call returned persons the first did not ({', '.join(join)})",
                                kind="api")
            return
        row0, others = pending
        idx = []
        for j, it in enumerate(its[1:], start=1):
            m = {} if others[j - 1] is None else {key(j, others[j - 1]): others[j - 1]}
            m.update((key(j, o), o) for o in it)
            idx.append(m)
        rows0 = [row0]
        rows0 += its[0]
        for r0 in rows0:
            got = [m.pop(key(0, r0), None) for m in idx]
            if any(g is None for g in got):
                raise DataError(f"census microdata: person {key(0, r0)} is missing from a later call; fetch again",
                                kind="api")
            yield take(0, r0) + [v for j, g in enumerate(got, start=1) for v in take(j, g)]
    return cols, gen()


def micro_meta(client, pack, ds: str, vars_: list[str], strict: bool = True) -> dict:
    """{variable: metadata} from /variables/<VAR>.json (cached 30 days); an unknown name is a spec error (strict),
    else left out of the result."""
    from core.cache import Cache
    from core.http import DataError
    out, bad = {}, []
    use_cache = not (os.environ.get("DATA_ANALYST_FIXTURES") or os.environ.get("DATA_ANALYST_RECORD"))
    cache = Cache("census", pack.policy) if use_cache else None
    for v in vars_:
        url = f"{client.base}/{ds}/variables/{v}.json"
        hit = cache.get(url) if cache else None
        if hit:
            out[v] = json.loads(hit[0].decode("utf-8"))
            continue
        r = _get(client, f"/{ds}/variables/{v}.json", allow_404=True)
        if r.status == 404 or r.body.lstrip()[:1] != b"{":
            bad.append(v)
            continue
        out[v] = r.json()
        if cache:
            cache.put(url, r.body, {"dataset": ds})
    if bad and strict:
        raise DataError(f"not variables of {ds}: {', '.join(bad)}; find names with --search \"{ds}: <words>\"",
                        kind="bad_request")
    return out


def micro_codebook(meta: dict) -> list[dict]:
    """One row per code (variable, label, value, value_label); ranges as "min..max"; weights and keys one row."""
    def order(x):
        try:
            return (0, float(x), "")
        except ValueError:
            return (1, 0.0, str(x))
    rows = []
    for v, m in meta.items():
        base = {"variable": v, "label": m.get("label", ""), "predicate_type": m.get("predicateType", ""),
                "suggested_weight": m.get("suggested-weight", "")}
        vals = m.get("values") or {}
        items = sorted((vals.get("item") or {}).items(), key=lambda kv: order(kv[0]))
        rng = vals.get("range") or []
        for code, lab in items:
            rows.append({**base, "value": code, "value_label": lab})
        for r in rng:
            rows.append({**base, "value": f"{r.get('min', '')}..{r.get('max', '')}", "value_label": r.get("description", "")})
        if not items and not rng:
            rows.append({**base, "value": "", "value_label": ""})
    return rows


def fetch_micro(client, pack, r, ds: str, params: dict) -> tuple[dict, list[str]]:
    """{name: FileTable, name_codebook: rows}, notes. Streams each geography's joined rows to a temp CSV."""
    import csv
    import tempfile
    from core.http import DataError
    from core.package import FileTable
    name = r["name"]
    kind = micro_kind(ds)
    asked = [v for v in _ensure_list(params.get("get"))]
    meta = micro_meta(client, pack, ds, [v for v in asked if v == v.upper()])
    try:
        plan = micro_plan(ds, params, meta)
    except ValueError as e:
        raise DataError(f"request {name!r}: {e}", kind="bad_request")
    reps = set(_replicate_sets(kind, params.get("replicates")))
    extra = [c for c in plan["cols"] if c not in meta and c not in reps]
    found = micro_meta(client, pack, ds, extra, strict=False)
    meta.update(found)
    missing = {c for c in extra if c not in found}
    if missing:                            # an older file without HRHHID2 or PWCMPWGT: plan again without them
        plan = micro_plan(ds, params, meta, absent=missing)
        if len(plan["chunks"]) > 1 and not plan["join"]:
            raise DataError(f"request {name!r}: {ds} has none of the join keys, so its columns cannot be split "
                            f"across calls: ask for at most {MAX_VARS} variables", kind="bad_request")
    fd, tmp = tempfile.mkstemp(prefix=f"census_{name}_", suffix=".csv")
    os.close(fd)
    cols, n, nonmiss, sample, empty_geos = None, 0, {}, [], 0
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for g in plan["geos"]:
                parts = []
                for chunk in plan["chunks"]:
                    resp = _get(client, f"/{ds}", {"get": ",".join(chunk), **plan["preds"], **g})
                    if resp.status == 204 or not resp.body.strip():
                        parts = None
                        break
                    it = _iter_rows(resp.body)
                    head = next(it, None)
                    parts.append((head or [], it))
                if not parts:
                    empty_geos += 1
                    continue
                gcols, rows = _merge_chunks(parts, plan["join"])
                if cols is None:            # asked order: variables, keys, weights, replicates, then geography
                    cols = [c for c in plan["cols"] if c in gcols] + [c for c in gcols if c not in plan["cols"]]
                    w.writerow(cols)
                    nonmiss = {c: 0 for c in cols}
                order = [gcols.index(c) if c in gcols else None for c in cols]
                for row in rows:
                    out = [row[i] if i is not None else "" for i in order]
                    w.writerow(out)
                    n += 1
                    for c, v in zip(cols, out):
                        if v != "":
                            nonmiss[c] += 1
                    if len(sample) < 500:
                        sample.append(dict(zip(cols, out)))
    except BaseException:
        os.remove(tmp)
        raise
    notes = [f"{name}: {x}" for x in plan["notes"]]
    if empty_geos:
        notes.append(f"{name}: {empty_geos} of {len(plan['geos'])} geographies had no matching persons")
    if cols is None:
        os.remove(tmp)
        return {name: [], f"{name}_codebook": micro_codebook(meta)}, notes
    notes.append(f"{name}: codebook (every code's label, from the API's variable metadata) in {name}_codebook.csv")
    return {name: FileTable(tmp, cols, n, sample, nonmiss), f"{name}_codebook": micro_codebook(meta)}, notes


def moe_of(v: str) -> str | None:
    if re.fullmatch(r"[A-Z]{1,3}\d{2,6}[A-Z]{0,2}_\d{3,4}PE", v):
        return v[:-2] + "PM"
    if re.fullmatch(r"[A-Z]{1,3}\d{2,6}[A-Z]{0,2}(_C\d{2})?_\d{3,4}[A-Z]?E", v):
        return v[:-1] + "M"
    return None


def with_moe(vars_: list[str]) -> list[str]:
    out = []
    for v in vars_:
        if v not in out:
            out.append(v)
        m = moe_of(v)
        if m and m not in vars_ and m not in out:
            out.append(m)
    return out


# ---------------------------------------------------------------------- datasets
def guess_year(rest: str, today: date | None = None) -> int:
    """Release-calendar guess (only for offline manual links): ACS 1-year in September, 5-year in December."""
    t = today or date.today()
    if "acs5" in rest:
        return t.year - 1 if t.month == 12 else t.year - 2
    if "acs1" in rest or "acsse" in rest:
        return t.year - 1 if t.month >= 10 else t.year - 2
    return t.year - 2


def latest_year(client, rest: str) -> int:
    from core.http import DataError
    if rest in _LATEST:
        return _LATEST[rest]
    this = date.today().year
    first = this if rest.startswith("cps/") else this - 1      # a CPS file carries its survey year (ASEC: March)
    for y in range(first, this - 8, -1):
        try:
            r = _get(client, f"/{y}/{rest}.json", allow_404=True)
        except DataError as e:
            if e.kind == "fixture_missing":          # offline tests: years not recorded count as absent
                continue
            raise
        if r.status == 200 and r.body.lstrip()[:1] == b"{":
            _LATEST[rest] = y
            return y
    raise DataError(f"census: no vintage of {rest} found on the API for {this - 7}-{this - 1}", kind="not_found")


def resolve_dataset(client, ds: str) -> tuple[str, str]:
    ds = str(ds).strip().strip("/")
    if ds.startswith("latest/"):
        rest = ds[len("latest/"):]
        y = latest_year(client, rest)
        return f"{y}/{rest}", f"'{ds}' resolved to {y}/{rest} (newest vintage on the API)"
    return ds, ""


def dataset_year(ds: str) -> int | None:
    m = re.match(r"(\d{4})/", ds)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------- geography names
_SUFFIX = re.compile(r"\s+(city and borough|census area|municipality|borough|parish|county|city|town|village|cdp|"
                     r"planning region|municipio)$")


def _base(s: str) -> str:
    s = s.lower().split(",")[0].strip()
    prev = None
    while prev != s:
        prev, s = s, _SUFFIX.sub("", s).strip()
    return s


def geo_list(client, ds: str, level: str, st_fips: str) -> tuple[list[dict], str]:
    """[{name, code, incorporated}], note. API NAME list of the dataset when a key is set, else 2020 files."""
    from core.http import DataError
    why = "no CENSUS_API_KEY"
    if client.key:
        try:
            data = _json(_get(client, f"/{ds}", {"get": "NAME", "for": f"{level}:*", "in": f"state:{st_fips}"}))
            if data:
                head, *rows = data
                i, j = head.index("NAME"), head.index(level)
                return [{"name": r[i].rsplit(",", 1)[0].strip(), "code": r[j],
                         "incorporated": bool(re.search(r" (city|town|village|borough|municipality)$",
                                                        r[i].rsplit(",", 1)[0]))} for r in rows], ""
        except DataError as e:
            why = f"the API name list failed ({e.kind})"
    usps = _state(st_fips, "usps").lower()
    kind = "cou" if level == "county" else "place"
    url = f"{WWW2}/{kind}/st{st_fips}_{usps}_{kind}2020.txt"
    text = _get(client, url).text()
    out = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    head = lines[0].split("|")
    for ln in lines[1:]:
        f = dict(zip(head, ln.split("|")))
        if level == "county":
            out.append({"name": f.get("COUNTYNAME", ""), "code": f.get("COUNTYFP", ""), "incorporated": False})
        else:
            out.append({"name": f.get("PLACENAME", ""), "code": f.get("PLACEFP", ""),
                        "incorporated": f.get("TYPE", "").upper().startswith("INCORPORATED")})
    return out, (f"{level} names matched against the Census 2020 reference file ({why}); codes that changed after "
                 "2020 (Connecticut planning regions from ACS 2022 on) are not in it")


def resolve_geo(client, ds: str, level: str, value, st_fips: str) -> tuple[str, str, str]:
    """(code, matched name, note) for a county or place given by name or code."""
    raw = str(value).strip()
    digits = 3 if level == "county" else 5
    if raw == "*" or re.fullmatch(rf"\d{{{digits}}}", raw):
        return raw, raw, ""
    if level == "county" and re.fullmatch(r"\d{5}", raw):
        if raw[:2] != st_fips:
            raise ValueError(f"county FIPS {raw} is not in state {st_fips}")
        return raw[2:], raw, ""
    items, note = geo_list(client, ds, level, st_fips)
    st_name = _state(st_fips, "name")
    exact = [x for x in items if x["name"].lower() == raw.lower().split(",")[0].strip()]
    hits = exact or [x for x in items if _base(x["name"]) == _base(raw)]
    if len(hits) > 1 and level == "place":
        inc = [x for x in hits if x["incorporated"]]
        hits = inc[:1] if len(inc) == 1 else hits
    if len(hits) == 1:
        return hits[0]["code"], hits[0]["name"], note
    if len(hits) > 1:
        raise ValueError(f"{level} {raw!r} is ambiguous in {st_name}: " + "; ".join(
            f"{x['name']} ({x['code']})" for x in hits[:8]) + ". Give the full name or the code.")
    names = [x["name"] for x in items]
    close = [n for n in names if _base(raw) in n.lower()][:5]
    bases = {_base(n): n for n in names}
    for c in difflib.get_close_matches(raw, names, n=5, cutoff=0.6) +             [bases[b] for b in difflib.get_close_matches(_base(raw), list(bases), n=5, cutoff=0.6)]:
        if c not in close:
            close.append(c)
    raise ValueError(f"no {level} named {raw!r} in {st_name}." +
                     (f" Did you mean: {'; '.join(close[:6])}?" if close else ""))


def build_query(client, ds: str, params: dict) -> tuple[dict, list[str]]:
    """API query parameters from the spec's params (convenience names resolved). Raises ValueError."""
    p = dict(params)
    notes = []
    vars_ = _ensure_list(p.pop("get", ""))
    moe = p.pop("moe", True)
    if is_acs(ds):
        if moe is False or str(moe).lower() == "false":
            raise ValueError("ACS margins of error are never dropped: remove moe: false")
        full = with_moe(vars_)
        added = [v for v in full if v not in vars_]
        if added:
            notes.append(f"margins of error added: {', '.join(added)}")
        vars_ = full
    st, co, pl, z = (p.pop(k, None) for k in ("state", "county", "place", "zcta"))
    if is_sipp(ds):
        full = with_sipp_keys(vars_)
        added = [v for v in full if v not in vars_]
        if added:
            notes.append(f"SIPP person-month keys and weight added: {', '.join(added)} (weight every estimate by "
                         "WPFINWGT)")
        q = {"get": ",".join(full)}
        if any(v not in (None, "") for v in (co, pl, z)) or p.get("for") or p.get("in"):
            raise ValueError("SIPP has no geography below the state (the API has none at all): use state, "
                             "which filters on TST_INTV, the state of the interview address")
        if st not in (None, "", "*"):
            q["TST_INTV"] = _state(st, "fips")
            notes.append(f"state {st!r} -> TST_INTV={q['TST_INTV']} (state of the interview address; SIPP is "
                         "designed for national estimates, so state figures from small samples are unreliable)")
        for k, v in p.items():
            if v is not None and v != "":
                q[k] = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
        if "MONTHCODE" not in q:
            notes.append("no MONTHCODE filter: each person appears once per reference month (up to 12 rows); "
                         "filter MONTHCODE (12 = December) or average over months")
        return q, notes
    q = {"get": ",".join(vars_)}
    fips = None
    if st not in (None, "") and str(st).strip() != "*":
        fips = _state(st, "fips")
    if z not in (None, ""):
        if not re.fullmatch(r"\d{5}|\*", str(z).strip()):
            raise ValueError(f"zcta must be a 5-digit ZIP code area or *, not {z!r}")
        q["for"] = f"zip code tabulation area:{str(z).strip()}"
    elif pl not in (None, ""):
        if not fips:
            raise ValueError("place needs a state")
        code, name, note = resolve_geo(client, ds, "place", pl, fips)
        q["for"], q["in"] = f"place:{code}", f"state:{fips}"
        notes += [f"place {pl!r} -> {name} ({fips}{code})"] + ([note] if note else [])
    elif co not in (None, ""):
        if not fips:
            raise ValueError("county needs a state")
        code, name, note = resolve_geo(client, ds, "county", co, fips)
        if p.get("for"):                       # e.g. tract:* within the county
            q["for"], q["in"] = p.pop("for"), f"state:{fips} county:{code}"
        else:
            q["for"], q["in"] = f"county:{code}", f"state:{fips}"
        if code != "*":
            notes += [f"county {co!r} -> {name} ({fips}{code})"] + ([note] if note else [])
    elif st not in (None, ""):
        if p.get("for"):
            q["for"] = p.pop("for")
            q["in"] = p.pop("in", None) or f"state:{fips}"
        else:
            q["for"] = f"state:{fips or '*'}"
    for k, v in p.items():
        if v is None or v == "":
            continue
        q[k] = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
    return q, notes


# ---------------------------------------------------------------------- spec checks
def check_spec(spec) -> list[str]:
    from core.http import DataError
    probs = []
    client = None
    for r in spec.get("requests") or []:
        if (r.get("endpoint") or "query") != "query":
            continue
        name = r.get("name")
        p = r.get("params") or {}
        ds = str(p.get("dataset") or r.get("dataset") or "")
        cat = (_pack().endpoints or {}).get("catalog", {})
        ds = cat[ds]["id"] if ds in cat else ds
        vars_ = _ensure_list(p.get("get"))
        if micro_kind(ds):
            try:
                micro_plan(ds, p)
            except ValueError as e:
                probs.append(f"request {name!r}: {e}")
            continue
        if not vars_:
            probs.append(f"request {name!r}: 'get' needs at least one variable (NAME,B19013_001E)")
        n = len(with_moe(vars_) if is_acs(ds) else with_sipp_keys(vars_) if is_sipp(ds) else vars_)
        if n > MAX_VARS:
            probs.append(f"request {name!r}: {n} variables (margins of error or SIPP keys included); the API takes "
                         f"at most {MAX_VARS} per call: split the request")
        if is_acs(ds) and str(p.get("moe", True)).lower() == "false":
            probs.append(f"request {name!r}: ACS margins of error are never dropped: remove moe: false")
        if is_sipp(ds):
            if any(p.get(k) not in (None, "") for k in ("county", "place", "zcta", "for", "in", "ucgid")):
                probs.append(f"request {name!r}: SIPP has no geography below the state: use state (it filters "
                             "TST_INTV) or no geography for national estimates")
            continue
        if ds.strip("/").startswith("timeseries/intltrade"):
            if not p.get("time"):
                probs.append(f"request {name!r}: international trade needs a time (2026-08, or 'from 2025-01 to "
                             "2025-12'); it has no for= geography (filter CTY_CODE, COMM_LVL, I_COMMODITY)")
            continue
        if ds.strip("/").startswith("timeseries/") and not p.get("time") and not p.get("YEAR"):
            probs.append(f"request {name!r}: a time series needs time (2026, 2026-Q2, 2026-08, or 'from 2024 to "
                         "2026'); without it the API returns every period")
        if not any(p.get(k) not in (None, "") for k in ("for", "ucgid", "state", "zcta")):
            probs.append(f"request {name!r}: give a geography: for (state:*, county:*), state, or zcta")
        if (p.get("county") or p.get("place")) and not p.get("state"):
            probs.append(f"request {name!r}: county and place need a state")
        geo = f"{p.get('for', '')} {p.get('zcta', '')}".lower()
        if "acs1" in ds and any(g in geo for g in ("tract", "block group", "zip code")) or ("acs1" in ds and p.get("zcta")):
            probs.append(f"request {name!r}: ACS 1-year has no tracts, block groups or ZCTAs (areas of 65,000+ "
                         "only): use the 5-year dataset (acs5)")
        if p.get("state") not in (None, "", "*"):
            try:
                fips = _state(p["state"], "fips")
            except ValueError as e:
                probs.append(f"request {name!r}: {e}")
                continue
            for level in ("county", "place"):
                if p.get(level) in (None, ""):
                    continue
                try:
                    client = client or _client()
                    year_ds = ds if not ds.startswith("latest/") else f"{guess_year(ds)}/{ds[7:]}"
                    resolve_geo(client, year_ds, level, p[level], fips)
                except ValueError as e:
                    probs.append(f"request {name!r}: {e}")
                except DataError:
                    pass                            # unreachable here: the fetch resolves it later
    return probs


# ---------------------------------------------------------------------- normalize
def _is_var(col: str) -> bool:
    return col == col.upper() and any(ch.isdigit() for ch in col) and col not in ("NAME", "GEO_ID")


def normalize(req, ep, rows):
    if not rows:
        return rows
    cols = [c for c in rows[0] if _is_var(c) and not c.endswith(("EA", "MA", "_flag"))]
    flagged = set()
    for r in rows:
        for c in cols:
            v = r.get(c)
            flag = ""
            if v is None or v == "":
                flag = "null: no data for this geography"
                v = ""
            else:
                try:
                    x = float(str(v))
                except ValueError:
                    x = None
                if x is not None and x <= -1_000_000:
                    flag = SENTINELS.get(int(x), f"{int(x)}: Census special value (not a number)")
                    v = ""
                elif str(v) in TOPCODES.get(c, {}):
                    flag = TOPCODES[c][str(v)]
            r[c] = v
            if flag:
                r[f"{c}_flag"] = flag
                flagged.add(c)
    if flagged:
        out = []
        for r in rows:
            o = {}
            for k, v in r.items():
                if k.endswith("_flag") and k[:-5] in flagged:
                    continue
                o[k] = v
                if k in flagged:
                    o[f"{k}_flag"] = r.get(f"{k}_flag", "")
            out.append(o)
        rows = out
    return rows


# ---------------------------------------------------------------------- fetch
def _table(data) -> list[dict]:
    if not data:
        return []
    head, *rows = data
    return [dict(zip(head, r)) for r in rows]


def fetch_one(client, pack, r) -> tuple[list[dict] | dict, list[str]]:
    """(rows, notes); for microdata ({name: FileTable, name_codebook: rows}, notes)."""
    from core import generic_rest as gr
    from core.http import DataError
    ep, params, probs = gr.resolve(pack, r)
    if probs:
        raise DataError("; ".join(probs), kind="bad_request")
    kind = r.get("endpoint") or "query"
    ds, note = resolve_dataset(client, params.pop("dataset"))
    notes = [f"{r['name']}: {note}"] if note else []
    if kind == "group_variables":
        data = _json(_get(client, f"/{ds}/groups/{params['group']}.json")) or {}
        rows = [{"variable": k, **gr.flatten(v)} for k, v in sorted((data.get("variables") or {}).items())]
        return gr._clean(rows, {"missing": [""]}), notes
    if kind == "groups":
        data = _json(_get(client, f"/{ds}/groups.json")) or {}
        return gr._clean(data.get("groups") or [], {"missing": [""]}), notes
    if micro_kind(ds):
        tables, mnotes = fetch_micro(client, pack, r, ds, params)
        return tables, notes + mnotes
    try:
        q, gnotes = build_query(client, ds, params)
    except ValueError as e:
        raise DataError(f"request {r['name']!r}: {e}", kind="bad_request")
    notes += [f"{r['name']}: {n}" for n in gnotes]
    data = _json(_get(client, f"/{ds}", q))
    if data is None:
        raise DataError(f"request {r['name']!r}: the Census API returned no rows (HTTP 204: no geography matched "
                        f"for={q.get('for')} in={q.get('in', '')}); check the codes", kind="bad_request")
    # SIPP microdata has no geography sentinels: blanks mean "not in universe", values stay as published
    rows = _table(data) if is_sipp(ds) else normalize(r, ep, _table(data))
    return gr._clean(rows, {"missing": [""], "flatten": False}), notes


def fetch(spec, pack, route):
    from core.http import DataError
    client = _client(pack)
    tables, notes = {}, []
    for r in spec["requests"]:
        got, n = fetch_one(client, pack, r)
        got = got if isinstance(got, dict) else {r["name"]: got}
        if not got[r["name"]] and not r.get("allow_empty"):
            raise DataError(f"request {r['name']!r} returned no rows: check the variables and geography"
                            + (" (and the filters: no person matched them)" if len(got) > 1 else ""),
                            kind="bad_request")
        tables.update(got)
        notes += n
    return tables, notes


def ping(client):
    from core.http import DataError
    try:
        _get(client, "/2023/acs/acs5", {"get": "NAME", "for": "us:1"})
        return {"auth_ok": True, "kind": "", "detail": "data query answered with the key"}
    except DataError as e:
        return {"auth_ok": e.kind not in ("auth", "key_missing"), "kind": e.kind, "detail": str(e)}


# ---------------------------------------------------------------------- manual route
def manual_items(spec, pack):
    from core.http import DataError
    from core import generic_rest as gr
    base = pack.endpoints["base_url"].rstrip("/")
    client = _client(pack)
    items = []
    for r in spec.get("requests") or []:
        ep, params, probs = gr.resolve(pack, r)
        if probs:
            continue
        params = copy.deepcopy(params)
        raw_ds = str(params.pop("dataset")).strip("/")
        note = []
        try:
            ds, _ = resolve_dataset(client, raw_ds)
        except DataError:
            ds = f"{guess_year(raw_ds)}/{raw_ds[7:]}" if raw_ds.startswith("latest/") else raw_ds
            if raw_ds.startswith("latest/"):
                note.append(f"year {ds[:4]} guessed from the release calendar: if the link fails, try one year earlier")
        kind = r.get("endpoint") or "query"
        if kind == "group_variables":
            url, needs = f"{base}/{ds}/groups/{params.get('group')}.json", False
        elif kind == "groups":
            url, needs = f"{base}/{ds}/groups.json", False
        elif micro_kind(ds):
            try:
                plan = micro_plan(ds, params)
            except ValueError:
                continue                         # --check reports it as fix_spec
            calls = [(g, c) for g in plan["geos"] for c in plan["chunks"]]
            for i, (g, chunk) in enumerate(calls, start=1):
                q = {"get": ",".join(chunk), **plan["preds"], **g}
                url = f"{base}/{ds}?" + urllib.parse.urlencode(q, safe=":*,()") + "&key={API_KEY}"
                items.append({"name": f"{r['name']}#{i}", "request": r["name"], "url": url,
                              "filename": f"{r['name']}_{i:03d}.json", "also_ext": [], "needs_key": True,
                              "method": "GET", "body": None,
                              "note": "; ".join(note + ([f"call {i} of {len(calls)}; --ingest joins the files on "
                                                         f"{', '.join(plan['join'])}"] if len(calls) > 1 else []))})
            continue
        else:
            try:
                q, _ = build_query(client, ds, params)
            except (DataError, ValueError) as e:
                q = {k: v for k, v in params.items() if k not in ("county", "place", "moe")}
                q["get"] = ",".join(with_moe(_ensure_list(params.get("get"))) if is_acs(ds) else _ensure_list(params.get("get")))
                note.append(f"geography names could not be resolved here ({e}); put the FIPS codes in for/in")
            url = f"{base}/{ds}?" + urllib.parse.urlencode(q, safe=":*,()") + "&key={API_KEY}"
            needs = True
        items.append({"name": f"{r['name']}#1", "request": r["name"], "url": url, "filename": f"{r['name']}.json",
                      "also_ext": [".csv"], "needs_key": needs, "method": "GET", "body": None,
                      "note": "; ".join(note)})
    return items


def _dcg_csv(path: str) -> list[dict]:
    """A data.census.gov "Download Table" CSV: one row per label, columns "<geography>!!<measure>". Returned long:
    label, geography, measure, value (a number: commas, %, ± removed; empty when not one) and value_text."""
    import csv
    from core.http import DataError
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = list(csv.reader(f))
    if not rd or "!!" not in "".join(rd[0][1:]):
        raise DataError(f"{os.path.basename(path)}: not a data.census.gov table export (its columns read "
                        "\"<geography>!!Estimate\"); upload that CSV or the API JSON", kind="bad_request")
    head, out = rd[0], []
    for row in rd[1:]:
        label = (row[0] if row else "").replace("\xa0", " ").strip()
        for col, text in zip(head[1:], row[1:]):
            geo, _, measure = col.partition("!!")
            t = str(text).strip()
            num = re.sub(r"[,%±+]", "", t).strip()
            try:
                val = float(num) if num else ""
            except ValueError:
                val = ""
            out.append({"label": label, "geography": geo.strip(), "measure": measure.replace("!!", " ").strip(),
                        "value": val, "value_text": t})
    return out


def _upload_vintage(resp: dict, req: dict, path: str) -> dict:
    """The year and product of a data.census.gov table JSON (objectId "ACSDT1Y2024.B24091", dataAPIURI
    ".../data/2024/acs/acs1"), kept on every row; a file of another product or year than the spec asks is refused,
    so a 5-year table is never ingested as a 1-year one."""
    from core.http import DataError
    m = re.search(r"/data/(\d{4})/([\w/]+?)(?:\?|$)", str(resp.get("dataAPIURI") or ""))
    if not m:
        return {}
    year, product = m.group(1), m.group(2)
    want = str((req.get("params") or {}).get("dataset") or req.get("dataset") or "")
    wy = re.match(r"(\d{4})/", want)
    wp, gp = re.search(r"acs[135]", want), re.search(r"acs[135]", product)   # acs1 / acs5, alias or path
    if (wp and gp and wp.group(0) != gp.group(0)) or (wy and wy.group(1) != year):
        raise DataError(f"{os.path.basename(path)} is {resp.get('objectId') or product + ' ' + year}, but request "
                        f"{req.get('name')!r} asks for {want}: upload the matching table", kind="bad_request")
    return {"vintage": year, "census_dataset": product, "table_id": str(resp.get("objectId") or "")}


def parse_upload(pack, req, paths):
    from core import generic_rest as gr
    from core.http import DataError
    params = req.get("params") or {}
    ds = str(params.get("dataset") or req.get("dataset") or "")
    cat = (pack.endpoints or {}).get("catalog", {})
    ds = cat[ds]["id"] if ds in cat else ds
    if micro_kind(ds):              # the manual links' files, <request>_001.json ...: each geography's calls joined
        try:
            plan = micro_plan(ds, params)
        except ValueError as e:
            raise DataError(f"request {req.get('name')!r}: {e}", kind="bad_request")
        files, k, rows = sorted(p for p in paths if p.lower().endswith(".json")), len(plan["chunks"]), []
        if not files or len(files) % k:
            raise DataError(f"{len(files)} JSON files for request {req.get('name')!r}, but each geography takes {k} "
                            "calls: upload every file the instructions list", kind="bad_request")
        for i in range(0, len(files), k):
            parts = []
            for fp in files[i:i + k]:
                with open(fp, "rb") as f:
                    data = json.loads(f.read().decode("utf-8-sig")) or [[]]
                parts.append((data[0], iter(data[1:])))
            cols, it = _merge_chunks(parts, plan["join"])
            rows += [dict(zip(cols, r)) for r in it]
        return rows
    rows = []
    for p in sorted(paths):
        if p.lower().endswith(".csv"):
            rows += _dcg_csv(p)
            continue
        with open(p, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
        if isinstance(data, dict) and isinstance((data.get("response") or {}).get("data"), list):
            # data.census.gov's own table JSON: the API layout with every column of the table; keep the asked ones
            vintage = _upload_vintage(data["response"], req, p)
            t = _table(data["response"]["data"])
            want = _ensure_list((req.get("params") or {}).get("get"))
            keep = ["GEO_ID", "NAME"] + [c for v in want for c in ((v, v[:-1] + "M") if v.endswith("E") else (v,))]
            t = [{**({k: r[k] for k in keep if k in r} if want else r),
                  "geo_fips": str(r.get("GEO_ID", "")).split("US")[-1], **vintage} for r in t]
            rows += normalize(req, {}, t)
        elif isinstance(data, list):
            rows += normalize(req, {}, _table(data))
        elif isinstance(data, dict) and "variables" in data:
            rows += [{"variable": k, **gr.flatten(v)} for k, v in sorted(data["variables"].items())]
        elif isinstance(data, dict) and "groups" in data:
            rows += data["groups"]
        else:                      # never report an unreadable file as an empty table
            raise DataError(f"{os.path.basename(p)}: not a Census API response (a JSON array with a header row) nor "
                            "a data.census.gov table JSON or CSV", kind="bad_request")
    return gr._clean(rows, {"missing": [""], "flatten": False})


# ---------------------------------------------------------------------- search
def _variables(client, pack, ds: str) -> dict:
    from core.cache import Cache
    url = f"{client.base}/{ds}/variables.json"
    use_cache = not os.environ.get("DATA_ANALYST_FIXTURES")
    cache = Cache("census", pack.policy) if use_cache else None
    hit = cache.get(url) if cache else None
    if hit:
        return json.loads(hit[0].decode("utf-8"))
    r = _get(client, f"/{ds}/variables.json")
    if cache:
        cache.put(url, r.body, {"dataset": ds})
    return r.json()


def search(query, pack):
    """'median household income' searches the latest ACS 5-year tables; prefix a dataset to choose another:
    '2020/dec/pl: total population', 'acs1_subject: poverty'."""
    from core.http import DataError
    from core import generic_rest as gr
    m = re.match(r"^\s*([\w/]+)\s*:\s*(.+)$", query)
    cat = (pack.endpoints or {}).get("catalog", {})
    ds_in, text = (m.group(1), m.group(2)) if m else ("latest/acs/acs5", query)
    ds_in = cat[ds_in]["id"] if ds_in in cat else ds_in
    words = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 1]
    hits = [h for h in gr.search_catalog(pack, text, limit=200) if "alias" in h and
            all(w in " ".join(map(str, h.values())).lower() for w in words)]
    out = hits[:5]
    client = _client(pack)
    try:
        ds, _ = resolve_dataset(client, ds_in)
        vs = _variables(client, pack, ds).get("variables", {})
        scored = []
        for vid, v in vs.items():
            label = str(v.get("label", "")).replace("!!", " > ").strip(" >")
            concept = str(v.get("concept", ""))
            hay = f"{vid} {label} {concept}".lower()
            if words and all(w in hay for w in words):
                s = sum(1 for w in words if w in label.lower()) * 1000 - len(label) - 0.1 * len(concept)
                attrs = str(v.get("attributes", ""))
                scored.append((s, {"variable": vid, "label": label, "concept": concept, "group": v.get("group", ""),
                                   "moe": next((a for a in attrs.split(",") if a.endswith("M") and not a.endswith("MA")), ""),
                                   "dataset": ds}))
        scored.sort(key=lambda x: -x[0])
        out += [h for _, h in scored[:20 - len(out)]]
        if len(scored) > 20 - len(hits[:5]):
            out.append({"note": f"{len(scored)} variables match in {ds}: add words, or list a table with the "
                                "group_variables endpoint"})
    except DataError as e:
        out.append({"note": f"variable search needs api.census.gov ({e.kind}): {e}"})
    return out

#!/usr/bin/env python
"""FRED / ALFRED / GeoFRED data, packaged for analysis (the fred deep pack).

Run through the skill's dispatcher (SKILL = the skill folder):
  python SKILL/scripts/fetch.py --source fred <out>/spec.json --out <out> --check          # which route works here?
  python SKILL/scripts/fetch.py --source fred <out>/spec.json --out <out>                  # download + package
  python SKILL/scripts/fetch.py --source fred --describe GDPC1 GDPPOT                      # title, units, source ...
  python SKILL/scripts/fetch.py --source fred --search "potential gdp" [--limit 15]        # find IDs (needs a key)
  python SKILL/scripts/fetch.py --source fred --vintagedates GDPC1                         # ALFRED revision dates (key)
  python SKILL/scripts/fetch.py --source fred --release-dates [RELEASE_ID] [--start D] [--end D]   # calendar (key)
  python SKILL/scripts/fetch.py --source fred <out>/spec.json --out <out> --manual-bundle  # links + scripts
  python SKILL/scripts/fetch.py --source fred <out>/spec.json --out <out> --ingest [FILES] # package uploads

Routes (--check prints one as "route"; first match wins):
  reuse_existing   <out>/data/fred/ already holds this exact request: nothing to download
  api              a FRED API key was found and FRED accepts it: full metadata, sources, copyright status
  api_keyless      no working key, but fred.stlouisfed.org answers and the request needs no key-only
                   feature: download FRED's own graph CSVs now (no source or copyright status)
  run_setup        a key-only feature is needed (first release, full revision history, GeoFRED regional
                   data, release calendar or release observations), no key works, and the user has a terminal
  cloud_secret     a key is needed in a cloud session: add FRED_API_KEY to the environment's secrets
  cloud_allowlist  a cloud session cannot reach FRED: allow its hosts, or use the bundle
  manual_download  no FRED host can be reached from here (sandbox): the user downloads and uploads the files
  fix_spec         spec.json has a problem
  report_error     FRED cannot be reached from the user's own machine
The normal run applies reuse_existing itself (--force downloads again). The routes are chosen from what
was actually reached and which key works, not from the environment's name; the environment only decides
how the user can supply a key. DATA_ACCESS_FORCE=restricted makes every host unreachable (to test).

Exit codes: 0 ok; 2 FRED or spec error (including a series that returned no data); 3 unexpected error;
4 cannot download (the printed JSON has the route to follow).

spec.json:
{
  "description": "Output gap since 2019",
  "series": [{"id": "GDPC1", "label": "Real GDP"},                     # or plain "GDPC1"
             {"id": "GDPPOT", "label": "Real potential GDP (CBO)"}],
  "start": "2018-01-01", "end": null,                                    # end: latest
  "recessions": true,          # adds USREC (NBER recession indicator, monthly) for notes and chart shading
  # optional, globally or per series (per series wins):
  "frequency": "q", "aggregation_method": "avg",   # FRED-side conversion; prefer tsstats (it checks gaps)
  "units": "lin",                                  # FRED transformation (pc1, pca ...); prefer tsstats
  "vintage": "latest" | {"as_of": ["2020-07-30", "2021-07-29"]} | "first_release" | "all",
  "regional": [{"series_id": "CAUR", "region_type": "state", "date": "2025-01-01"}],  # GeoFRED, needs a key
  "release_dates": {"release_id": 50, "start": "2026-10-01", "end": "2026-12-31"},     # calendar, needs a key
  "release_observations": [{"release_id": 53, "limit": 10000, "max_pages": 2,
                            "series": ["A191RL1Q225SBEA"]}]                          # v2, needs a key
}
Vintages: "as_of" works with or without a key (ALFRED); "first_release" and "all" need a key.
Per series, also (they do not change the download): "source" (the agency, for the citation) and
"copyright": "public_domain" | "citation_required" | "preapproval_required" with "copyright_basis" (what was
read where, e.g. "series page tag, read 2026-10-20"); the keyless route cannot supply either.

Output (<out>/data/fred/):
  fred_observations.csv  one row per series, vintage and date: series_id, label, vintage, date, value
                         (empty = no value), realtime_start, realtime_end
  series.csv             one row per series: title, units, frequency, seasonal adjustment, last updated,
                         fiscal_year, first/last date, counts (missing, future = projections), source,
                         release, copyright status, citation
  regional.csv           GeoFRED cross-sections (when requested): group, date, region, code, series_id, value
  release_dates.csv      the release calendar (when requested): release_id, release_name, date
  release_observations.csv / release_series.csv   whole-release downloads (when requested)
  raw/                   every response as received (URLs without the key), for the audit trail
  request.json           spec, route, requests, retrieval time, problems (to reproduce)
  README.md              what is in the data, the traps, the citations, the FRED notice and terms
tsstats.py is copied to <out>/lib/fred/ (facts.py, factfmt.py to <out>/lib/) so analysis.py reruns alone.
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

PACK = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(PACK))
for _p in (os.path.join(SKILL, "scripts"), PACK, os.path.join(PACK, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from core import env as cenv  # noqa: E402
from core import package as cpkg  # noqa: E402
from core import policy as cpol  # noqa: E402
from core import routes as croutes  # noqa: E402
from core.http import SOURCES  # noqa: E402
from fred_api import (API, API_V2, GRAPH_CSV, GRAPH_MAX, GRAPH_XLSX, KEY_URL, LEGAL_URL, NOTICE, SERIES_URL,  # noqa: E402
                      TERMS_URL, FredClient, FredError, Keyless, citation, copyright_from_tag, copyright_status,
                      copyright_tag_of,
                      find_api_key, graph_url, observation_rows, parse_graph_csv, parse_graph_xlsx, parse_value,
                      reachable, redact, regional_rows, restricted)

SOURCE = "fred"
POLICY = cpol.load(PACK)
EXIT_OK, EXIT_SPEC, EXIT_UNEXPECTED, EXIT_BLOCKED = (croutes.EXIT_OK, croutes.EXIT_SPEC, croutes.EXIT_UNEXPECTED,
                                                     croutes.EXIT_BLOCKED)
FREQS = ("d", "w", "bw", "m", "q", "sa", "a")
ID_RE = r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,40}"
# FRED's labels on a series page: "Public Domain: Citation Requested", "Copyrighted: Citation Required",
# "Copyrighted: Pre-Approval Required"
COPYRIGHT_LABELS = ("public_domain", "citation_required", "preapproval_required")
NOT_DATA = ("label", "copyright", "copyright_basis", "source", "auto")   # fields that do not change the download
DATE_RE = r"\d{4}-\d{2}-\d{2}"


# --------------------------------------------------------------------------- spec
def norm_vintage(v) -> dict:
    if v in (None, "", "latest"):
        return {"kind": "latest", "dates": []}
    if v in ("first_release", "initial"):
        return {"kind": "first_release", "dates": []}
    if v in ("all", "all_vintages"):
        return {"kind": "all", "dates": []}
    if isinstance(v, dict) and "as_of" in v:
        dates = v["as_of"] if isinstance(v["as_of"], list) else [v["as_of"]]
        dates = sorted({str(d)[:10] for d in dates})
        bad = [d for d in dates if not re.fullmatch(DATE_RE, d)]
        if bad or not dates:
            raise ValueError(f"vintage as_of needs YYYY-MM-DD dates, got {v['as_of']!r}")
        return {"kind": "as_of", "dates": dates}
    raise ValueError(f'vintage must be "latest", {{"as_of": [dates]}}, "first_release" or "all", got {v!r}')


def norm_spec(spec: dict) -> dict:
    g = {k: spec.get(k) for k in ("start", "end", "frequency", "aggregation_method", "units")}
    gv = spec.get("vintage")
    series = []
    for x in spec.get("series") or []:
        x = {"id": x} if isinstance(x, str) else dict(x)
        sid = str(x["id"]).strip().upper()
        if not re.fullmatch(ID_RE, sid):
            raise ValueError(f"not a FRED series ID: {x['id']!r}")
        item = {"id": sid, "label": x.get("label", "")}
        if x.get("copyright"):                  # read on the series page when the keyless route gave no notes
            if x["copyright"] not in COPYRIGHT_LABELS:
                raise ValueError(f"{sid}: copyright must be one of {COPYRIGHT_LABELS}")
            item["copyright"] = x["copyright"]
            item["copyright_basis"] = x.get("copyright_basis") or "set in spec.json"
        if x.get("source"):                     # the source agency, for the citation (keyless route)
            item["source"] = str(x["source"])
        for k in g:
            val = x.get(k, g[k])
            item[k] = str(val).strip() if val not in (None, "") else None
        item["vintage"] = norm_vintage(x.get("vintage", gv))
        for k in ("start", "end"):
            if item[k] and not re.fullmatch(DATE_RE, item[k]):
                raise ValueError(f"{sid}: {k} must be YYYY-MM-DD, got {item[k]!r}")
        if item["frequency"] and item["frequency"].lower() not in FREQS:
            raise ValueError(f"{sid}: frequency must be one of {FREQS}")
        if item["frequency"]:
            item["frequency"] = item["frequency"].lower()
        if item["aggregation_method"] and item["aggregation_method"] not in ("avg", "sum", "eop"):
            raise ValueError(f"{sid}: aggregation_method must be avg, sum or eop")
        if item["units"] == "lin":
            item["units"] = None
        series.append(item)
    if spec.get("recessions") and "USREC" not in [s["id"] for s in series]:
        # the same entry as an explicit {"id": "USREC"} would get, so listing it later (to add its source and
        # copyright) is the same request
        series.append({"id": "USREC", "label": "NBER recession indicator", "start": g["start"], "end": g["end"],
                       "frequency": None, "aggregation_method": None, "units": None,
                       "vintage": norm_vintage(gv), "auto": "recessions"})
    keys = [(s["id"], s["units"], s["frequency"], s["vintage"]["kind"], tuple(s["vintage"]["dates"])) for s in series]
    dup = sorted({k[0] for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"duplicate series in spec.json: {dup}")
    regional = []
    for r in spec.get("regional") or []:
        if not (r.get("series_id") or r.get("series_group")):
            raise ValueError("each regional block needs series_id (any series of the group) or series_group")
        regional.append({k: r.get(k) for k in ("series_id", "series_group", "region_type", "date", "start_date",
                                                "season", "units", "frequency", "aggregation_method",
                                                "transformation", "label") if r.get(k) not in (None, "")})
    rdates = None
    if spec.get("release_dates"):
        rd = spec["release_dates"]
        rd = {"release_id": rd} if isinstance(rd, (int, str)) else dict(rd)
        for k in ("start", "end"):
            if rd.get(k) and not re.fullmatch(DATE_RE, str(rd[k])):
                raise ValueError(f"release_dates {k} must be YYYY-MM-DD, got {rd[k]!r}")
        if rd.get("release_id") not in (None, "") and not str(rd["release_id"]).isdigit():
            raise ValueError(f"release_dates release_id must be a number, got {rd['release_id']!r}")
        rdates = {"release_id": int(rd["release_id"]) if rd.get("release_id") not in (None, "") else None,
                  "start": rd.get("start"), "end": rd.get("end"), "limit": int(rd.get("limit") or 10000)}
    robs = []
    for ro in spec.get("release_observations") or []:
        ro = {"release_id": ro} if isinstance(ro, (int, str)) else dict(ro)
        if not str(ro.get("release_id", "")).isdigit():
            raise ValueError(f"release_observations needs a numeric release_id, got {ro.get('release_id')!r}")
        lim, pages = int(ro.get("limit") or 10000), int(ro.get("max_pages") or 5)
        if not (1 <= lim <= 500000) or not (1 <= pages <= 50):
            raise ValueError("release_observations: limit 1-500000 observations per page, max_pages 1-50")
        keep = [str(x).strip().upper() for x in (ro.get("series") or [])]
        robs.append({"release_id": int(ro["release_id"]), "limit": lim, "max_pages": pages, "series": keep,
                     "label": ro.get("label", "")})
    if not series and not regional and not rdates and not robs:
        raise ValueError("spec.json has no series, regional, release_dates or release_observations block")
    return {"description": spec.get("description", ""), "series": series, "regional": regional,
            "recessions": bool(spec.get("recessions")), "release_dates": rdates, "release_observations": robs}


def fingerprint(s: dict) -> str:
    """Two specs asking for the same data hash the same (labels and description do not matter)."""
    core = {"series": sorted([{k: v for k, v in x.items() if k not in NOT_DATA} for x in s["series"]],
                             key=lambda x: json.dumps(x, sort_keys=True)),
            "regional": sorted([{k: v for k, v in r.items() if k != "label"} for r in s["regional"]],
                               key=lambda x: json.dumps(x, sort_keys=True))}
    if s.get("release_dates"):
        core["release_dates"] = s["release_dates"]
    if s.get("release_observations"):
        core["release_observations"] = [{k: v for k, v in r.items() if k != "label"} for r in s["release_observations"]]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def key_only_needs(s: dict) -> list[str]:
    """What in this request only the API (with a key) can deliver."""
    out = [f"{x['id']}: {x['vintage']['kind'].replace('_', ' ')} vintages" for x in s["series"]
           if x["vintage"]["kind"] in ("first_release", "all")]
    if s["regional"]:
        out.append(f"{len(s['regional'])} GeoFRED regional cross-section(s)")
    if s.get("release_dates"):
        out.append("the release calendar (release/dates)")
    for r in s.get("release_observations") or []:
        out.append(f"release {r['release_id']} observations (v2 release/observations)")
    return out


def vintage_chunks(x: dict) -> list[list[str] | None]:
    """The as-of dates of one series in groups of GRAPH_MAX: one keyless graph download holds at most 12 columns
    and FRED drops the rest silently (checked live: 14 as-of dates returned 12 vintages). [None] = latest only."""
    d = x["vintage"]["dates"]
    return [d[i:i + GRAPH_MAX] for i in range(0, len(d), GRAPH_MAX)] if d else [None]


def chunk_file(x: dict, n: int, of: int) -> str:
    return f"{tag(x)}.csv" if of == 1 else f"{tag(x)}_p{n}.csv"


def tag(x: dict) -> str:
    """A file-name-safe name for one requested series variant."""
    parts = [x["id"]]
    if x["units"]:
        parts.append(x["units"])
    if x["frequency"]:
        parts.append(x["frequency"] + (x["aggregation_method"] or "avg"))
    if x["vintage"]["kind"] != "latest":
        parts.append(x["vintage"]["kind"] if x["vintage"]["kind"] != "as_of" else "asof")
    return "_".join(parts)


# --------------------------------------------------------------------------- environment (core.env)
def setup_script() -> tuple[str, str]:
    """The skill's generic setup script for this source (core.env.setup_command)."""
    return cenv.setup_command(SOURCE)


def data_dir(out: str) -> str:
    return cpkg.data_dir(out, SOURCE)


def bundle_dir(out: str) -> str:
    return os.path.join(os.path.abspath(out), "manual_download", SOURCE)


def emit(obj: dict, code: int = 0):
    obj.setdefault("source", SOURCE)
    print(redact(json.dumps(obj, indent=2, default=str)))
    sys.exit(code)


# --------------------------------------------------------------------------- what is on disk
def existing_data(out: str, fp: str) -> dict | None:
    d = data_dir(out)
    rq, csv_path = os.path.join(d, "request.json"), os.path.join(d, "fred_observations.csv")
    if not os.path.exists(rq):
        return None
    try:
        saved = json.load(open(rq, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    retrieved = saved.get("retrieved", "")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(retrieved)).days
    except ValueError:
        age = None
    return {"path": csv_path, "csv_exists": os.path.exists(csv_path), "matches": saved.get("fingerprint") == fp,
            "meta_fingerprint": saved.get("meta_fingerprint"), "acquired": saved.get("acquired", ""),
            "retrieved": retrieved, "age_days": age, "route": saved.get("route"),
            "latest": saved.get("latest"), "problems": saved.get("problems") or []}


# --------------------------------------------------------------------------- route check
def route_check(s: dict, out: str, env: str, force: bool = False, spec_path: str | None = None) -> dict:
    out = os.path.abspath(out)
    spec_path = os.path.abspath(spec_path or os.path.join(out, "spec.json"))
    base = f'--source {SOURCE} "{spec_path}" --out "{out}"'
    run_cmd = croutes.cmd(base)
    ingest_cmd = croutes.cmd(base, "--ingest", "<the uploaded files or their folder>")
    key, source = find_api_key()
    needs = key_only_needs(s)
    r = {"status": "route_check", "source": SOURCE, "environment": env, "os": sys.platform,
         "key_env": "FRED_API_KEY", "key_source": source, "restricted": restricted(), "api_reachable": None,
         "keyless_reachable": None, "key_ok": None, "key_only_needs": needs[:6],
         "route": None, "message": "", "action": "", "next_command": ""}
    ex = existing_data(out, fingerprint(s))

    def done(route, message, action, next_command=""):
        r.update(route=route, message=message, action=action, next_command=next_command)
        return r

    def bundle(reason):
        b = write_manual_bundle(s, out, reason, spec_path)
        r.update(manual_bundle=b, instructions=os.path.join(b, "INSTRUCTIONS.md"), download_scripts=download_scripts(b))
        return b

    if ex and ex["csv_exists"] and ex["matches"] and not ex["problems"] and not force:
        r["existing_data"] = {k: ex[k] for k in ("path", "retrieved", "route")}
        if ex.get("meta_fingerprint") != meta_fingerprint(s):
            return done("reuse_existing",
                        f"The data for this request are already in {ex['path']}; only labels, sources or copyright "
                        "changed in spec.json.",
                        "Run next_command: it rebuilds data/fred/ from data/fred/raw/ without downloading again.",
                        run_cmd)
        stale = ex["age_days"] is not None and ex["age_days"] >= 7
        return done("reuse_existing",
                    f"The data for this request are already in {ex['path']} (retrieved {ex['retrieved'][:10]} by "
                    f"the {ex['route']} route)." + (" FRED has probably published newer data or revisions since."
                                                     if stale else ""),
                    "Skip the download and continue with the analysis." +
                    (" Ask whether to refresh (rerun with --force) if the latest observation matters." if stale
                     else " Pass --force to download again."))

    api_ok, api_why = reachable(API + "series?series_id=GNPCA&file_type=json")
    kl_ok, kl_why = reachable(GRAPH_CSV + "?id=GNPCA&cosd=2020-01-01")
    r.update(api_reachable=api_ok, keyless_reachable=kl_ok)
    key_benefit = (f" A free FRED API key ({KEY_URL}) adds each series' source, notes and copyright status (needed "
                   "for a citable report), search, first-release and full revision histories, GeoFRED regional "
                   "data, the release calendar and whole-release downloads.")

    if key and api_ok:
        p = FredClient(api_key=key).ping()
        r.update(key_ok=p["auth_ok"], key_detail=p["detail"][:200])
        if p["auth_ok"]:
            return done("api", f"A FRED API key was found ({source}) and FRED accepts it.", "Run next_command.",
                        run_cmd)
        if p["kind"] != "auth":
            return done("report_error", f"The key check failed: {p['detail']}", "Tell the user and stop.")
        r["key_detail"] = (f"FRED rejected the key found in {source} ({p['detail'][:120]}). Request a new key at "
                           f"{KEY_URL} and store it with the setup script.")

    rejected = r["key_ok"] is False
    lead = (r["key_detail"] + " " if rejected else
            "No FRED API key is set up. " if not key else
            "A FRED API key is stored, but api.stlouisfed.org cannot be reached from here. ")

    if kl_ok and not needs:
        if env in ("local", "ssh"):
            path, cmd = setup_script()
            r.update(setup_script=path, run_command=cmd)
        return done("api_keyless",
                    lead + "fred.stlouisfed.org answers, so the data can come from FRED's own graph downloads (the "
                    "same CSV files as the Download button), with titles, units, frequency and seasonal adjustment."
                    + key_benefit,
                    "Run next_command now. Tell the user once, in one sentence, what a key adds and give "
                    "run_command (local/ssh) or the cloud-secret option; do not wait for it. For a report, the "
                    "source and copyright status of each series must be read from its FRED page (see source.md, "
                    "Data format and traps) or the user sets up a key.", run_cmd + " --no-key")

    if kl_ok or api_ok:                      # reachable, but the request needs a key
        need = "This request needs a FRED API key for: " + "; ".join(needs) + "."
        if env in ("local", "ssh"):
            path, cmd = setup_script()
            r.update(setup_script=path, run_command=cmd)
            return done("run_setup",
                        lead + need + f" A free key is at {KEY_URL} (sign in, then Request API Key); the setup "
                        "script asks for it with hidden input, checks it with FRED and stores it in Claude Code's "
                        "settings.json.",
                        "Give the user run_command to run in their own terminal (do not run it for them, and never "
                        "ask for the key in the chat). Offer the keyless alternative: as-of vintages instead of "
                        "first-release or full histories, explicit per-area series IDs instead of a GeoFRED "
                        "cross-section. When the user says the key is stored, run --check again.", cmd)
        b = bundle("the request needs a FRED API key")
        if env == "cloud":
            return done("cloud_secret",
                        lead + need + " Add FRED_API_KEY to the cloud environment's secrets and start the session "
                        f"again, or download on your own computer with the files in {b}.",
                        "Explain both options (and the keyless alternative). If the user downloads by hand: "
                        + SEND, ingest_cmd)
        return done("manual_download", lead + need + " A key cannot be stored in this environment; the download "
                    f"links and scripts are in {b}.",
                    SEND, ingest_cmd)

    why = api_why or kl_why
    if env in ("local", "ssh") and not restricted():
        return done("report_error",
                    f"FRED could not be reached from this computer ({why}). Check the internet connection, VPN "
                    "and proxy settings, then run the request again.",
                    "Tell the user and stop. Do not substitute data from memory.")
    b = bundle("FRED cannot be reached from the analysis environment")
    if env == "cloud":
        return done("cloud_allowlist",
                    f"This cloud session cannot reach FRED ({why}). Allow fred.stlouisfed.org, "
                    "alfred.stlouisfed.org and api.stlouisfed.org in the environment's network settings and "
                    f"restart, or download by hand with the files in {b}.",
                    "Offer both options. For the manual one: " + SEND, ingest_cmd)
    return done("manual_download",
                f"This {env} environment cannot reach FRED ({why}). On claude.ai an organization admin can allow "
                "fred.stlouisfed.org (keyless) or api.stlouisfed.org in the network settings; otherwise the "
                f"download links and scripts are in {b}: open the links (or run one command) on your own computer "
                "and upload the files.",
                SEND, ingest_cmd)


# --------------------------------------------------------------------------- manual bundle
MANUAL_TEMPLATE = """# Download the FRED data on your own computer

The analysis environment cannot download the data itself (@REASON@). Everything it needs is
listed below. Pick one option, then upload the downloaded files to the chat.

## Option A: click the links (no key, no software)

Each link downloads one file from FRED in your browser. Save them all into one folder.

@LINKS@

## Option B: one command (every file, including the ones that need a key)

Save `download_fred.sh` (macOS, Linux) or `download_fred.ps1` (Windows) in your Downloads folder, or note the
folder you saved it in, then paste one line into a terminal. The files are saved next to the script.

- macOS or Linux (Terminal; needs only curl):
  ```
  bash ~/Downloads/download_fred.sh
  ```
- Windows (PowerShell):
  ```
  powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\\Downloads\\download_fred.ps1"
  ```

If you saved the script in another folder, put that folder in place of Downloads. `spec.json` records the
request; you do not need to open it.

@KEYNOTE@

## Upload

Upload every downloaded file (`*.csv`, `*.xlsx`, `*.json`) or the whole folder. Do not edit or rename
them. After the upload Claude packages the files.
"""

KEYNOTE_NEEDED = ("Some files come from the FRED API and need your own free API key ({url}): {needs}. The scripts "
                  "ask for it with hidden input (or use `FRED_API_KEY` when it is set in that terminal). The key "
                  "is only sent to api.stlouisfed.org and is never written into the downloaded files. Never "
                  "paste the key into the chat. Without a key those files are skipped.")
KEYNOTE_NONE = "No key is needed for any of these files."

DOWNLOAD_SCRIPTS = {"bash": "download_fred.sh", "powershell": "download_fred.ps1"}
SEND = ("Send INSTRUCTIONS.md, download_fred.sh, download_fred.ps1 and spec.json from the bundle to the user "
        "(present_files / SendUserFile when available; print the full paths), wait for the upload, then run "
        "next_command.")


def manual_items(s: dict) -> list[dict]:
    """Every file the user downloads: FRED's keyless graph files where they serve, API calls otherwise."""
    items = []
    for x in s["series"]:
        kind = x["vintage"]["kind"]
        if kind in ("latest", "as_of"):
            chunks = vintage_chunks(x)
            for n, dates in enumerate(chunks, 1):
                url = graph_url(x["id"], start=x["start"], end=x["end"], frequency=x["frequency"],
                                aggregation_method=x["aggregation_method"], units=x["units"], vintage_dates=dates)
                items.append({"file": chunk_file(x, n, len(chunks)), "url": url, "keyed": False,
                              "what": (x["label"] or x["id"]) + (f" (vintages {dates[0]} to {dates[-1]})"
                                                                 if len(chunks) > 1 else "")})
            continue
        p = {"series_id": x["id"], "observation_start": x["start"], "observation_end": x["end"],
             "units": x["units"], "frequency": x["frequency"], "aggregation_method": x["aggregation_method"]}
        p.update(realtime_start="1776-07-04", realtime_end="9999-12-31")
        if kind == "first_release":
            p["output_type"] = 4
        items.append({"file": f"{tag(x)}.json", "url": _api_url("series/observations", p), "keyed": True,
                      "what": f"{x['id']} {kind.replace('_', ' ')}"})
        items.append({"file": f"{x['id']}__series.json", "url": _api_url("series", {"series_id": x["id"]}),
                      "keyed": True, "what": f"{x['id']} notes (source, copyright)"})
    ids = sorted({x["id"] for x in s["series"]})          # the XLSX gives titles and units without a key
    for i in range(0, len(ids), GRAPH_MAX):
        chunk = ids[i:i + GRAPH_MAX]
        items.append({"file": f"metadata_{i // GRAPH_MAX + 1:02d}.xlsx", "keyed": False, "what": "titles, units, frequency",
                      "url": f"{GRAPH_XLSX}?{urllib.parse.urlencode({'id': ','.join(chunk)}, safe=',')}"})
    for j, rb in enumerate(s["regional"], start=1):
        p = {k: v for k, v in rb.items() if k not in ("label", "series_id")}
        if "series_group" in p:
            items.append({"file": f"regional_{j:02d}.json", "keyed": True, "what": "GeoFRED cross-section",
                          "url": _api_url("regional/data", p, geo=True)})
            continue
        # two steps: the group of the member series, then the cross-section; the download scripts fill the
        # __GROUP_*__ placeholders from the first reply
        tpl = {"series_group": "__GROUP_SERIES_GROUP__", "region_type": "__GROUP_REGION_TYPE__",
               "season": "__GROUP_SEASON__", "units": "__GROUP_UNITS__", "frequency": "__GROUP_FREQUENCY__",
               "date": "__GROUP_MAX_DATE__", **p}
        items.append({"file": f"regional_{j:02d}__group.json", "keyed": True, "what": "GeoFRED group",
                      "url": _api_url("series/group", {"series_id": rb["series_id"]}, geo=True),
                      "then": {"file": f"regional_{j:02d}.json", "url": _api_url("regional/data", tpl, geo=True)}})
    rd = s.get("release_dates")
    if rd:
        start = rd.get("start") or date.today().isoformat()
        end = rd.get("end") or ("9999-12-31" if rd.get("release_id") else
                                date.fromordinal(date.today().toordinal() + 14).isoformat())
        p = {"realtime_start": start, "realtime_end": end, "include_release_dates_with_no_data": "true",
             "sort_order": "asc", "limit": rd["limit"]}
        path = "release/dates" if rd.get("release_id") else "releases/dates"
        if rd.get("release_id"):
            p = {"release_id": rd["release_id"], **p}
        items.append({"file": "release_dates.json", "keyed": True, "what": "release calendar",
                      "url": _api_url(path, p)})
    for ro in s.get("release_observations") or []:
        q = urllib.parse.urlencode({"release_id": ro["release_id"], "format": "json", "limit": ro["limit"]})
        items.append({"file": f"release_{ro['release_id']}.json", "keyed": True, "bearer": True,
                      "pages": ro["max_pages"], "what": f"release {ro['release_id']} observations (FRED API v2)",
                      "url": f"{API_V2}release/observations?{q}"})
    seen, out = set(), []
    for it in items:                                  # one notes file per series, however many variants
        if it["file"] not in seen:
            seen.add(it["file"])
            out.append(it)
    return out


def _api_url(path: str, params: dict, geo: bool = False) -> str:
    q = {k: v for k, v in params.items() if v not in (None, "")}
    q.update(api_key="__FRED_KEY__", file_type="json")
    base = API.replace("/fred/", "/geofred/") if geo else API
    return f"{base}{path}?{urllib.parse.urlencode(q, safe=',')}"


def manifest_lines(items: list[dict]) -> list[str]:
    """The manifest the download scripts carry: file, kind, pages, url, then_file, then_url ("-" = empty)."""
    rows = []
    for it in items:
        kind = ("plain" if not it["keyed"] else "bearer" if it.get("bearer") else "group" if it.get("then")
                else "key")
        nxt = it.get("then") or {}
        rows.append("\t".join([it["file"], kind, str(it.get("pages") or 0), it["url"], nxt.get("file") or "-",
                               nxt.get("url") or "-"]))
    return rows


def write_script_templates(d: str, items: list[dict]) -> None:
    """Write assets/download_fred.sh and .ps1 into the bundle (ASCII) with the manifest built in."""
    block = "\n".join(manifest_lines(items))
    for kind in ("bash", "powershell"):
        name = DOWNLOAD_SCRIPTS[kind]
        try:
            with open(os.path.join(PACK, "assets", name), encoding="ascii") as f:
                text = f.read().replace("@MANIFEST@", block)
            text.encode("ascii")
        except (OSError, UnicodeError) as e:
            print(f"[fred] warning: {name} not written ({e})", file=sys.stderr)
            continue
        path = os.path.join(d, name)
        # bash needs LF line ends even when the bundle is written on Windows; PowerShell 5.1 prefers CRLF
        with open(path, "w", encoding="ascii", newline="\n" if kind == "bash" else "\r\n") as f:
            f.write(text)
        if kind == "bash":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass


def download_scripts(bundle: str) -> dict:
    return {k: os.path.join(bundle, n) for k, n in DOWNLOAD_SCRIPTS.items() if os.path.exists(os.path.join(bundle, n))}


def write_manual_bundle(s: dict, out: str, reason: str, spec_path: str | None = None) -> str:
    """Write the links, spec.json, a manifest (for --ingest) and the two download scripts. Needs no network
    and no key."""
    d = bundle_dir(out)
    os.makedirs(d, exist_ok=True)
    items = manual_items(s)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"source": SOURCE, "items": items}, f, indent=1)
    if spec_path and os.path.isfile(spec_path):
        shutil.copyfile(spec_path, os.path.join(d, "spec.json"))
    else:
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1, default=str)
    write_script_templates(d, items)
    links = [f"{i}. [{it['file']}]({it['url']}) ({it['what']})" for i, it in enumerate(items, start=1)
             if not it["keyed"]]
    keyed = [it for it in items if it["keyed"]]
    if keyed:
        links.append("\nThe following files need an API key, so use Option B for them: "
                     + ", ".join(f"`{it['file']}`" for it in keyed) + ".")
    keynote = (KEYNOTE_NEEDED.format(url=KEY_URL, needs="; ".join(key_only_needs(s)) or "notes and sources")
               if keyed else KEYNOTE_NONE)
    if any(it.get("bearer") for it in items):
        keynote += (" Whole-release downloads (FRED API v2) come in pages: the script saves `release_<id>.json`, "
                    "`release_<id>__p02.json` and so on; upload them all.")
    if any(it.get("then") for it in items):
        keynote += (" GeoFRED cross-sections take two calls: the script looks up the series group "
                    "(`regional_NN__group.json`), then downloads the cross-section (`regional_NN.json`).")
    text = (MANUAL_TEMPLATE.replace("@REASON@", reason).replace("@LINKS@", "\n".join(links) or "(none)")
            .replace("@KEYNOTE@", keynote))
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return d


# --------------------------------------------------------------------------- download
def geo_freq(f) -> str:
    """GeoFRED's series-group frequency ("a", "Annual", "Semiannual", "bw" ...) as the regional/data code
    (d, w, bw, m, q, sa, a). The first letter alone turns "sa" into "s" and "bw" into "b"."""
    f = str(f or "a").strip().lower()
    if f in FREQS:
        return f
    words = {"daily": "d", "weekly": "w", "biweekly": "bw", "monthly": "m", "quarterly": "q",
             "semiannual": "sa", "annual": "a"}
    return next((code for w, code in words.items() if f.replace("-", "").startswith(w)), "a")


def vintage_label(x: dict, rt_start: str = "", column: str = "") -> str:
    k = x["vintage"]["kind"]
    if k == "latest":
        return "latest"
    if k == "first_release":
        return "first_release"
    if k == "all":
        return rt_start
    m = re.search(r"_(\d{8})$", column)
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else ""


def new_parts(calls: list) -> dict:
    return {"obs": [], "meta": {}, "raw": [], "regional": [], "problems": [], "calls": calls, "release_dates": [],
            "release_series": [], "release_obs": [], "notes": []}


def series_copyright_tag(c: FredClient, sid: str) -> str:
    """The series' copyright tag ("copyrighted: citation required" ...), "" when the tags cannot be read."""
    try:
        return copyright_tag_of(c.get("series/tags", series_id=sid))
    except FredError as e:
        if e.kind in ("auth", "network", "rate", "unavailable"):
            raise
        return ""


def fetch_api(s: dict, c: FredClient) -> dict:
    """Everything through the API. Returns the collected parts for package()."""
    parts = new_parts(c.calls)
    sources: dict = {}
    for x in s["series"]:
        sid = x["id"]
        try:
            if sid not in parts["meta"]:
                m = c.series(sid)
                rel = c.release_of(sid)
                rid = rel.get("id")
                if rid is not None and rid not in sources:
                    sources[rid] = c.release_sources(rid)
                srcs = sources.get(rid) or []
                ctag = series_copyright_tag(c, sid)
                m.update(release=rel.get("name", ""), release_id=rid, copyright_tag=ctag,
                         source="; ".join(z.get("name", "") for z in srcs if z.get("name")), metadata_source="api")
                parts["meta"][sid] = m
                parts["raw"].append({"name": f"{sid}__series.json", "request": f"series, series/release, "
                                     f"release/sources for {sid}", "response": {"seriess": [m]}})
            p = {"observation_start": x["start"], "observation_end": x["end"], "units": x["units"],
                 "frequency": x["frequency"], "aggregation_method": x["aggregation_method"]}
            k = x["vintage"]["kind"]
            if k == "as_of":
                p["vintage_dates"] = ",".join(x["vintage"]["dates"])
            elif k == "first_release":             # needs the whole real-time window (checked live 2026-10-20)
                p.update(output_type=4, realtime_start="1776-07-04", realtime_end="9999-12-31")
            elif k == "all":
                p.update(realtime_start="1776-07-04", realtime_end="9999-12-31")
            obs = c.observations(sid, **p)
            parts["raw"].append({"name": f"{tag(x)}.json", "request": c.calls[-1], "response": {"observations": obs}})
            rows = observation_rows(sid, obs)
            if k == "as_of":                       # assign each real-time period to the vintages it covers
                exp = []
                for r in rows:
                    for v in x["vintage"]["dates"]:
                        if r["realtime_start"] <= v and (not r["realtime_end"] or v <= r["realtime_end"]):
                            exp.append({**r, "vintage": v})
                rows = exp
            else:
                for r in rows:
                    r["vintage"] = vintage_label(x, r["realtime_start"])
            parts["obs"].append((x, rows))
        except FredError as e:
            if e.kind in ("auth", "network", "rate", "unavailable"):
                raise
            parts["problems"].append(f"{tag(x)}: {e}")
    for j, rb in enumerate(s["regional"], start=1):
        try:
            p = {k: v for k, v in rb.items() if k not in ("label", "series_id")}
            if "series_group" not in p:
                grp = c.series_group(rb["series_id"])
                p["series_group"] = grp.get("series_group")
                for k, gk in (("region_type", "region_type"), ("season", "season"), ("units", "units")):
                    p.setdefault(k, grp.get(gk))
                p.setdefault("frequency", geo_freq(grp.get("frequency")))
                p.setdefault("date", grp.get("max_date"))
            payload = c.regional(**p)
            parts["raw"].append({"name": f"regional_{j:02d}.json", "request": c.calls[-1], "response": payload})
            for r in regional_rows(payload):
                parts["regional"].append({"group": p["series_group"], "label": rb.get("label", ""), **r,
                                          "units": p.get("units", ""), "season": p.get("season", ""),
                                          "frequency": p.get("frequency", "")})
        except FredError as e:
            if e.kind in ("auth", "network", "rate", "unavailable"):
                raise
            parts["problems"].append(f"regional block {j}: {e}")
    rd = s.get("release_dates")
    if rd:
        try:
            j = c.release_dates(rd.get("release_id"), rd.get("start"), rd.get("end"), rd["limit"])
            parts["raw"].append({"name": "release_dates.json", "request": c.calls[-1], "response": j})
            parts["release_dates"] = release_date_rows(j)
            if rd.get("release_id") and any(not r["release_name"] for r in parts["release_dates"]):
                try:                       # release/dates for one release carries no name: look it up once
                    name = (c.get("release", release_id=rd["release_id"]).get("releases") or [{}])[0].get("name", "")
                except FredError as e:
                    if e.kind in ("auth", "network", "rate", "unavailable"):
                        raise
                    name = ""
                for r in parts["release_dates"]:
                    r["release_name"] = r["release_name"] or name
            if int(j.get("count") or 0) > len(parts["release_dates"]):
                parts["problems"].append(f"release_dates: {j.get('count')} dates exist, {len(parts['release_dates'])} "
                                         f"kept (limit {rd['limit']}); narrow start/end or raise limit")
        except FredError as e:
            if e.kind in ("auth", "network", "rate", "unavailable"):
                raise
            parts["problems"].append(f"release_dates: {e}")
    for ro in s.get("release_observations") or []:
        try:
            rel, series = c.release_observations(ro["release_id"], ro["limit"], ro["max_pages"])
            parts["raw"].append({"name": f"release_{ro['release_id']}.json", "request": c.calls[-1],
                                 "response": {"release": rel, "series": series}})
            add_release_rows(parts, ro, rel, series)
        except FredError as e:
            if e.kind in ("auth", "network", "rate", "unavailable"):
                raise
            parts["problems"].append(f"release {ro['release_id']} observations: {e}")
    return parts


def release_date_rows(j: dict) -> list[dict]:
    return [{"release_id": d.get("release_id", ""), "release_name": d.get("release_name", ""), "date": d.get("date", "")}
            for d in j.get("release_dates") or []]


def merge_release_pages(pages: list[dict]) -> tuple[dict, list[dict]]:
    """Uploaded v2 release/observations pages in order (the scripts save one file per page; the API route
    saves one merged reply) -> (release, series), as FredClient.release_observations returns them. A page
    can end inside a series: the next page continues it."""
    release, series = {}, []
    for j in pages:
        release = release or dict(j.get("release") or {})
        for x in j.get("series") or []:
            if series and series[-1].get("series_id") == x.get("series_id"):
                series[-1]["observations"] = (series[-1].get("observations") or []) + (x.get("observations") or [])
            else:
                series.append(dict(x))
    last = pages[-1] if pages else {}
    if "has_more" in last:                       # a raw page; a merged reply keeps these in its release
        cursor = last.get("next_cursor") if last.get("has_more") else None
        release.update(has_more=bool(cursor), next_cursor=cursor or "")
    return release, series


def add_release_rows(parts: dict, ro: dict, rel: dict, series: list[dict]) -> None:
    """v2 release/observations -> release_series rows (one per series, with FRED's copyright tag) and
    release_observations rows (one per series and date)."""
    keep = set(ro.get("series") or [])
    n = 0
    for x in series:
        sid = str(x.get("series_id", "")).upper()
        if keep and sid not in keep:
            continue
        n += 1
        tag_ = x.get("copyright_id", "")
        parts["release_series"].append({
            "release_id": rel.get("release_id", ro["release_id"]), "release": rel.get("name", ""), "series_id": sid,
            "title": x.get("title", ""), "units": x.get("units", ""), "frequency": x.get("frequency", ""),
            "seasonal_adjustment": x.get("seasonal_adjustment", ""), "last_updated": x.get("last_updated", ""),
            "copyright_tag": tag_, "copyright": copyright_from_tag(tag_) or copyright_status(x.get("notes"), True),
            "n_obs": len(x.get("observations") or [])})
        for o in x.get("observations") or []:
            v = parse_value(o.get("value"))
            parts["release_obs"].append({"release_id": rel.get("release_id", ro["release_id"]), "series_id": sid,
                                         "date": o.get("date", ""), "value": "" if v is None else repr(v)})
    if keep and not n:
        parts["problems"].append(f"release {ro['release_id']}: none of {sorted(keep)} in the pages downloaded")
    if rel.get("has_more"):
        parts["notes"].append(f"release {ro['release_id']}: stopped after max_pages={ro['max_pages']} (more pages "
                              f"exist; next_cursor {rel.get('next_cursor')}). Raise max_pages or filter with series.")


def graph_rows(x: dict, cols: dict[str, list[dict]]) -> list[dict]:
    """Graph-CSV columns -> observation rows. An as-of vintage ends where that vintage's data ended: the
    empty cells after its last value are dates not yet published then, not missing values."""
    rows = []
    for col, pts in cols.items():
        v = vintage_label(x, column=col)
        if x["vintage"]["kind"] == "as_of":
            last = max((i for i, p in enumerate(pts) if p["value"] is not None), default=-1)
            pts = pts[:last + 1]
        rows += [{**p, "realtime_start": "", "realtime_end": "", "vintage": v} for p in pts]
    return rows


def fetch_keyless(s: dict, k: Keyless) -> dict:
    parts = new_parts(k.calls)
    for need in key_only_needs(s):
        parts["problems"].append(f"needs a FRED API key, not downloaded: {need}")
    ids = sorted({x["id"] for x in s["series"]})
    for i in range(0, len(ids), GRAPH_MAX):
        try:
            meta, body = k.metadata(ids[i:i + GRAPH_MAX])
            parts["raw"].append({"name": f"metadata_{i // GRAPH_MAX + 1:02d}.xlsx", "request": k.calls[-1], "bytes": body})
            for sid, m in meta.items():
                m["metadata_source"] = "fredgraph.xlsx"
                parts["meta"][sid] = m
        except FredError as e:
            if e.kind == "network":
                raise
            parts["problems"].append(f"metadata for {', '.join(ids[i:i + GRAPH_MAX])}: {e}")
    for x in s["series"]:
        if x["vintage"]["kind"] not in ("latest", "as_of"):
            continue
        chunks, rows = vintage_chunks(x), []
        for n, dates in enumerate(chunks, 1):
            try:
                cols, body, last_mod = k.csv(x["id"], start=x["start"], end=x["end"], frequency=x["frequency"],
                                             aggregation_method=x["aggregation_method"], units=x["units"],
                                             vintage_dates=dates)
            except FredError as e:
                if e.kind == "network":
                    raise
                parts["problems"].append(f"{x['id']}" + (f" vintages {dates[0]} to {dates[-1]}" if dates else "")
                                         + f": {e}")
                continue
            parts["raw"].append({"name": chunk_file(x, n, len(chunks)), "request": k.calls[-1], "bytes": body})
            rows += graph_rows(x, cols)
            if last_mod and x["id"] in parts["meta"]:
                parts["meta"][x["id"]].setdefault("http_last_modified", last_mod)
        if rows:
            parts["obs"].append((x, rows))
        if x["vintage"]["kind"] == "as_of":
            missing = sorted(set(x["vintage"]["dates"]) - {r["vintage"] for r in rows})
            if missing:
                parts["problems"].append(f"{x['id']}: no data for as-of vintage(s) {', '.join(missing)}")
    return parts


# --------------------------------------------------------------------------- package
def write_csv(path: str, rows: list[dict], fields: list[str] | None = None):
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


CREDIT = ("Data from FRED\u00ae, Federal Reserve Bank of St. Louis (downloaded without the API); this product is "
          "not endorsed or certified by the Federal Reserve Bank of St. Louis.")


def period_end_iso(d: str, dated: list[str]) -> str:
    """Last day of the period an observation stands for: FRED dates most periods by their first day (2026-10-01
    = 2026 Q4, which has not ended on 2026-10-20) and fiscal-year dollar series by their last day."""
    if not d or d[8:10] != "01" or len(dated) < 2:
        return d
    a, b = date.fromisoformat(dated[0]), date.fromisoformat(dated[1])
    if (b - a).days < 28:                   # daily, weekly, biweekly: the date is the observation itself
        return d
    step = round((b - a).days / 30.4)
    if step not in (1, 3, 6, 12):
        return d
    y, mth = int(d[:4]), int(d[5:7]) + step
    y, mth = y + (mth - 1) // 12, (mth - 1) % 12 + 1
    return (date(y, mth, 1) - timedelta(days=1)).isoformat()


TRANSFORM_UNITS = {"chg": "Change", "ch1": "Change from Year Ago", "pch": "Percent Change",
                   "pc1": "Percent Change from Year Ago", "pca": "Compounded Annual Rate of Change",
                   "cch": "Continuously Compounded Rate of Change", "cca": "Continuously Compounded Annual Rate of "
                   "Change", "log": "Natural Log"}


def is_fiscal(m: dict, sid: str = "") -> bool:
    """Federal fiscal-year series: FRED says so in the frequency ("Annual, Fiscal Year"), and the annual OMB/
    Treasury series it does not label (FYOIGDA188S, FYFSGDA188S ...) start with FY."""
    text = " ".join(str(m.get(k, "")) for k in ("frequency", "sheet", "title", "notes"))
    if "fiscal year" in text.lower():
        return True
    return sid.upper().startswith("FY") and str(m.get("frequency", "")).lower().startswith("annual")


def meta_fingerprint(s: dict) -> str:
    """The spec fields that change only the packaging (labels, sources, copyright)."""
    core = [[x["id"], *[x.get(k) for k in NOT_DATA]] for x in s["series"]]
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]


def package(parts: dict, s: dict, out: str, route: str, acquired: str, retrieved: str | None = None,
            n0: int = 0) -> dict:
    d = data_dir(out)
    raw_dir = os.path.join(d, "raw")
    if os.path.isdir(raw_dir):
        shutil.rmtree(raw_dir)
    os.makedirs(raw_dir, exist_ok=True)
    for x in parts["raw"]:
        path = os.path.join(raw_dir, x["name"])
        if "bytes" in x:
            with open(path, "wb") as f:
                f.write(x["bytes"])
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(redact(json.dumps({"request": x["request"], "response": x["response"]}, indent=1)))
    now = retrieved or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    today = date.today().isoformat()
    rows, problems = [], list(parts["problems"])
    for x, obs in parts["obs"]:
        if not obs:
            problems.append(f"{x['id']}: no observations returned for {x['start'] or 'start'} to {x['end'] or 'latest'}")
        for o in obs:
            v = o["value"]
            rows.append({"series_id": x["id"], "label": x["label"], "variant": tag(x), "vintage": o["vintage"],
                         "date": o["date"], "value": "" if v is None else repr(v),
                         "realtime_start": o.get("realtime_start", ""), "realtime_end": o.get("realtime_end", "")})
    rows.sort(key=lambda r: (r["series_id"], r["variant"], r["vintage"], r["date"]))
    write_csv(os.path.join(d, "fred_observations.csv"), rows,
              ["series_id", "label", "variant", "vintage", "date", "value", "realtime_start", "realtime_end"])
    if parts["regional"]:
        write_csv(os.path.join(d, "regional.csv"), parts["regional"],
                  ["group", "label", "title", "date", "region", "code", "series_id", "value", "units", "season",
                   "frequency"])
    elif os.path.exists(os.path.join(d, "regional.csv")):
        os.remove(os.path.join(d, "regional.csv"))
    for name, key, cols in (("release_dates", "release_dates", ["release_id", "release_name", "date"]),
                            ("release_series", "release_series", ["release_id", "release", "series_id", "title",
                                                                   "units", "frequency", "seasonal_adjustment",
                                                                   "last_updated", "copyright_tag", "copyright",
                                                                   "n_obs"]),
                            ("release_observations", "release_obs", ["release_id", "series_id", "date", "value"])):
        path = os.path.join(d, f"{name}.csv")
        if parts.get(key):
            write_csv(path, parts[key], cols)
        elif os.path.exists(path):
            os.remove(path)

    meta_rows = []
    for x in s["series"]:
        m = parts["meta"].get(x["id"], {})
        mine = [r for r in rows if r["variant"] == tag(x)]
        dated = sorted({r["date"] for r in mine})          # vintages repeat dates
        from_api = m.get("metadata_source") == "api"
        notes = (m.get("notes") or "").strip()
        title = m.get("title", "")
        if not m:
            problems.append(f"{x['id']}: no metadata (title, units) could be read; check the ID")
        meta_rows.append({
            "series_id": x["id"], "label": x["label"], "variant": tag(x), "vintage": x["vintage"]["kind"]
            + (":" + ",".join(x["vintage"]["dates"]) if x["vintage"]["dates"] else ""),
            "title": title, "units": (TRANSFORM_UNITS.get(x["units"] or "lin", "") + f" (of {m.get('units', '')})"
                                      if (x["units"] or "lin") != "lin" else m.get("units", "")) + (
                " (as FRED labels the series today; this vintage may use an earlier base year or definition)"
                if x["vintage"]["kind"] != "latest" and m.get("units") else ""),
            "frequency": m.get("frequency", ""),
            "seasonal_adjustment": m.get("seasonal_adjustment", ""), "last_updated": m.get("last_updated", ""),
            "fiscal_year": int(is_fiscal(m, x["id"])), "transformation": x["units"] or "", "converted_to": (
                f"{x['frequency']} ({x['aggregation_method'] or 'avg'})" if x["frequency"] else ""),
            "first_date": dated[0] if dated else "", "last_date": dated[-1] if dated else "",
            "n_obs": len(mine), "n_missing": sum(1 for r in mine if r["value"] == ""),
            "n_future": sum(1 for r in mine if period_end_iso(r["date"], dated) > today),
            "source": m.get("source", "") or x.get("source", ""), "release": m.get("release", ""),
            "copyright": x.get("copyright") or copyright_from_tag(m.get("copyright_tag"))
            or copyright_status(notes, from_api),
            "copyright_basis": x.get("copyright_basis") or (f"series tag \"{m['copyright_tag']}\" (FRED API)"
                                                            if m.get("copyright_tag") else
                                                            "series notes (FRED API)" if from_api else ""),
            "metadata_source": m.get("metadata_source", ""),
            "citation": citation(x["id"], title, m.get("source", "") or x.get("source", ""), now),
            "notes": re.sub(r"\s+", " ", notes)[:600]})
    write_csv(os.path.join(d, "series.csv"), meta_rows)
    latest, by_variant = {}, {}
    for r in rows:
        by_variant.setdefault(r["variant"], set()).add(r["date"])
    first_two = {v: sorted(ds)[:2] for v, ds in by_variant.items()}
    for r in rows:
        if (r["value"] != "" and r["vintage"] in ("latest", "first_release")
                and period_end_iso(r["date"], first_two[r["variant"]]) <= today):   # an in-progress quarter is not data
            latest[r["variant"]] = max(latest.get(r["variant"], ""), r["date"])
    tables = []
    for name in ("fred_observations", "series", "regional", "release_dates", "release_series", "release_observations"):
        path = os.path.join(d, f"{name}.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                n = max(0, sum(1 for _ in f) - 1)
            tables.append({"name": name, "file": f"{name}.csv", "rows": n, "sha256": cpkg.sha256(path)})
    req = {"source": SOURCE, "fingerprint": fingerprint(s), "meta_fingerprint": meta_fingerprint(s),
           "retrieved": retrieved or now, "route": route, "acquired": acquired,
           "keyless": route != "api" and not any(m.get("metadata_source") == "api" for m in parts["meta"].values()),
           "spec": s, "requests": [redact(u) for u in parts["calls"]], "n_requests": len(parts["calls"]),
           "request_log": SOURCES[n0:], "tables": tables, "notes": parts.get("notes") or [],
           "problems": problems, "latest": latest, "rows": len(rows), "regional_rows": len(parts["regional"]),
           "release_rows": len(parts.get("release_obs") or []), "release_date_rows": len(parts.get("release_dates") or []),
           "policy": {"redistribution": POLICY["redistribution"]["mode"], "cache": POLICY["cache"]["mode"]}}
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        f.write(redact(json.dumps(req, indent=2)))
    write_readme(d, s, meta_rows, rows, req)
    copy_lib(out)
    return req


def copy_lib(out: str) -> str:
    """analysis.py imports tsstats from <out>/lib/fred first, so the folder reruns without the skill.
    Copied from the pack's lib/ to the analysis folder; nothing is written inside the skill."""
    lib = os.path.join(os.path.abspath(out), "lib", SOURCE)
    os.makedirs(lib, exist_ok=True)
    shutil.copy2(os.path.join(PACK, "lib", "tsstats.py"), os.path.join(lib, "tsstats.py"))
    shared = os.path.join(PACK, "..", "..", "scripts", "analysis")      # tsstats.py loads these from lib/
    for f in ("facts.py", "factfmt.py", "combine.py"):
        shutil.copy2(os.path.join(shared, f), os.path.join(os.path.dirname(lib), f))
    return lib


def write_readme(d: str, s: dict, meta: list[dict], rows: list[dict], req: dict):
    keyless = req["keyless"]
    lines = [
        f"# FRED data: {s.get('description') or ', '.join(m['series_id'] for m in meta)}",
        "",
        f"- Retrieved: {req['retrieved']} ({req['acquired']}; route `{req['route']}`)",
        f"- Rows: {len(rows):,} in `fred_observations.csv` (one per series variant, vintage and date); series "
        "details in `series.csv`" + ("; GeoFRED cross-sections in `regional.csv`" if req["regional_rows"] else ""),
        "- Raw responses: `raw/`; the exact requests (without the key): `request.json`"
        + ("; release calendar in `release_dates.csv`" if req.get("release_date_rows") else "")
        + ("; whole-release downloads in `release_observations.csv` and `release_series.csv`"
           if req.get("release_rows") else ""),
        "",
        "## Series",
        "| Series ID | Label | Title | Units | Freq | SA | Vintage | Dates | Missing | Future | Updated | Copyright |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *[f"| `{m['series_id']}` | {m['label']} | {m['title']} | {m['units']} | {m['frequency']}"
          f"{' (fiscal year)' if m['fiscal_year'] and 'fiscal' not in m['frequency'].lower() else ''} | "
          f"{m['seasonal_adjustment']} | {m['vintage']} | {m['first_date']} to {m['last_date']} ({m['n_obs']}) | "
          f"{m['n_missing']} | {m['n_future']} | {m['last_updated']} | {m['copyright']} |" for m in meta],
        "",
        "## Read this before analyzing",
        "- `value` is empty where FRED has no value (`.` in the API): a day without trading, a month not "
        "collected (October 2025 for the CPI, CPS and LAUS). A change across a gap needs both endpoints.",
        "- **Future dates are projections, not data.** `n_future` > 0 (for example CBO's GDPPOT, NGDPPOT and "
        "NROU run 10 years ahead): cut those series at the last actual date of the series you compare them with.",
        "- **Fiscal years.** `fiscal_year` = 1 marks federal fiscal-year series (October to September). FRED dates "
        "dollar amounts at the end of the fiscal year (2025-09-30 = FY2025) and some percent-of-GDP series at "
        "January 1 of the fiscal year's number (2025-01-01 = FY2025). Use `tsstats.fiscal_year_label`.",
        "- `variant` names any FRED-side conversion (`_pc1`, `_qavg` ...). FRED's own frequency conversion "
        "averages whatever observations exist, so a quarter with a missing month still gets a value; prefer "
        "native-frequency data and `tsstats.to_frequency`, which refuses incomplete periods.",
        "- `vintage` is `latest`, an as-of date (ALFRED: the data as published on that date), `first_release`, "
        "or for full histories the date a value was first published (`realtime_start`).",
    ]
    if keyless:
        lines.append("- These data came from FRED's keyless graph downloads: `source` and `copyright` are not "
                     "known. Read them on each series page (links in the citations) before publishing.")
    for m in meta:      # FRED posts some series late at the publisher's request (UMCSENT: one month)
        late = re.search(r"(?i)[^.]*\b(?:delayed|embargoed)\b[^.]*\.?", str(m.get("notes") or ""))
        if late:
            lines.append(f"- **`{m['series_id']}` reaches FRED late:** \"{late.group(0).strip()}\" The publisher's own "
                         "site has newer values: when the question asks for the latest, read them there, save them "
                         "in published/ with source_url and accessed, and say which month FRED ends on.")
    if req.get("regional_rows"):
        lines += ["- **`regional.csv` (GeoFRED):** one row per region and date; `units`, `season` and `frequency` "
                  "as requested. `title` is GeoFRED's group title and can name another month than `date` "
                  "(\"2026 September ... \" for 2026-08-01 data): trust `date`. The regional series are not in "
                  "`series.csv`: cite GeoFRED with the group title and retrieval date, and the source agency named "
                  "on any one series page (e.g. BLS LAUS for state unemployment rates); recent LAUS months are "
                  "preliminary."]
    if req.get("notes"):
        lines += ["", "## Notes", *[f"- {n}" for n in req["notes"]]]
    if req.get("problems"):
        lines += ["", "## Problems", *[f"- {p}" for p in req["problems"]]]
    lines += [
        "",
        "## Reading the data",
        "```python",
        "import pandas as pd",
        "df = pd.read_csv('fred_observations.csv', parse_dates=['date'])",
        "w = df[df.vintage == 'latest'].pivot(index='date', columns='variant', values='value')",
        "```",
        "",
        "## Citations",
        *[f"- {c}" for c in dict.fromkeys(m["citation"] for m in meta)],
        "",
        "## FRED terms of use",
        NOTICE if req["route"] == "api" else CREDIT,
        "",
        f"FRED's terms ({TERMS_URL}, {LEGAL_URL}) require each user to use their own API key, forbid wholesale "
        "downloading and building an archive of FRED content, and ask that each series be cited. Series whose "
        "notes carry a copyright (`copyright` = preapproval_required) need the owner's permission before they "
        "are redistributed; public-domain series may be shared with a citation. An `unknown` status must be "
        "checked on the series page before the data are shared."
        + ("" if not [m for m in meta if m.get("copyright") == "preapproval_required"] else
           " **Pre-approval required here:** " + ", ".join(f"`{m['series_id']}`" for m in meta
                                                           if m.get("copyright") == "preapproval_required")
           + ": say in the answer that these values may not be republished without the owner's permission."),
        "",
        "## Use and redistribution",
        f"Rule: **{POLICY['redistribution']['mode']}**. {POLICY['redistribution'].get('why', '')} Cache: "
        f"**{POLICY['cache']['mode']}** ({POLICY['cache'].get('why', '')})",
    ]
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- ingest uploaded files
def ingest(paths: list[str], s: dict, out: str, route: str = "manual_download",
           acquired: str = "downloaded by the user and uploaded", retrieved: str | None = None) -> dict:
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(f for f in glob.glob(os.path.join(p, "**", "*"), recursive=True) if os.path.isfile(f))
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise FredError(f"Not found: {p}", kind="request")
    by_tag = {tag(x): x for x in s["series"]}
    parts = new_parts([])
    got: dict[str, list[dict]] = {}
    api_obs, groups, release_pages = {}, [], {}
    for fp in files:
        name = os.path.basename(fp)
        low = name.lower()
        body = open(fp, "rb").read()
        try:
            if low.endswith(".xlsx"):
                meta, _ = parse_graph_xlsx(body)
                for sid, m in meta.items():
                    m["metadata_source"] = "fredgraph.xlsx"
                    parts["meta"].setdefault(sid, m)
                parts["raw"].append({"name": name, "request": "uploaded", "bytes": body})
            elif low.endswith(".csv") or low.endswith(".zip"):
                if name in ("fred_observations.csv", "series.csv", "regional.csv", "release_dates.csv",
                            "release_series.csv", "release_observations.csv", "data_dictionary.csv"):
                    continue
                cols = parse_graph_csv(body)
                t = re.sub(r"_p\d+$", "", os.path.splitext(name)[0])      # vintage chunk files <tag>_pN.csv
                x = by_tag.get(t) or next((v for v in s["series"] if any(c == v["id"] or c.startswith(v["id"] + "_")
                                                                         for c in cols)), None)
                if not x:
                    print(f"[fred] {name}: no spec series matches its columns {list(cols)[:4]}; skipped",
                          file=sys.stderr)
                    continue
                got.setdefault(tag(x), []).extend(graph_rows(x, cols))
                parts["raw"].append({"name": name, "request": "uploaded", "bytes": body})
            elif low.endswith(".json"):
                j = json.loads(body.decode("utf-8-sig"))
                if not isinstance(j, dict) or "response" not in j or name in ("manifest.json", "request.json"):
                    continue
                resp, url = j["response"], str(j.get("request", ""))
                parts["raw"].append({"name": name, "request": redact(url), "response": resp})
                if "seriess" in resp:
                    m = resp["seriess"][0]
                    m["metadata_source"] = "api"
                    parts["meta"][m["id"].upper()] = m
                elif "observations" in resp:
                    api_obs[os.path.splitext(name)[0]] = resp["observations"]
                elif "series_group" in resp:
                    groups.append(name)
                elif "release_dates" in resp:
                    parts["release_dates"] = release_date_rows(resp)
                elif "series" in resp and "release" in resp:     # v2 pages: release_<id>.json, __p02.json, ...
                    m = re.search(r"__p(\d+)\.json$", low)
                    rid = (resp.get("release") or {}).get("release_id")
                    release_pages.setdefault(rid, []).append((int(m.group(1)) if m else 1, resp))
                else:
                    parts["regional"] += [{"group": "", "label": "", **r} for r in regional_rows(resp)]
        except (FredError, ValueError, KeyError, OSError) as e:
            parts["problems"].append(f"{name}: could not read ({e})")
    for rid, pages in release_pages.items():
        rel, series = merge_release_pages([r for _, r in sorted(pages, key=lambda p: p[0])])
        ro = next((r for r in s.get("release_observations") or [] if r["release_id"] == rid),
                  {"release_id": rid, "series": [], "max_pages": len(pages)})
        add_release_rows(parts, ro, rel, series)
    if groups and not parts["regional"]:
        parts["problems"].append(f"{', '.join(groups)}: a GeoFRED group lookup but no cross-section; run "
                                 "download_fred.sh or download_fred.ps1 again (it fetches both), then upload "
                                 "regional_NN.json")
    for t, obs in api_obs.items():
        x = by_tag.get(t)
        if x:
            got[t] = [{**r, "vintage": vintage_label(x, r["realtime_start"])} for r in observation_rows(x["id"], obs)]
    for x in s["series"]:
        if tag(x) in got:
            parts["obs"].append((x, got[tag(x)]))
        else:
            parts["problems"].append(f"{x['id']} ({tag(x)}): no uploaded file holds it")
    if not got and not parts["regional"] and not parts["release_dates"] and not parts["release_obs"]:
        raise FredError("None of the uploaded files holds FRED data for this spec.", kind="request")
    parts["calls"] = [f["url"] for f in manual_items(s)] if route == "manual_download" else []
    return package(parts, s, out, route, acquired, retrieved)


def repackage(s: dict, out: str, ex: dict) -> dict:
    """Labels, sources or copyright changed in spec.json, the data did not: rebuild data/ from data/raw/
    without downloading again, keeping the original route and retrieval time."""
    raw = os.path.join(data_dir(out), "raw")
    tmp = os.path.join(data_dir(out), "_raw_prev")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    shutil.copytree(raw, tmp)
    try:
        with open(os.path.join(data_dir(out), "request.json"), encoding="utf-8") as f:
            saved = json.load(f)
        req = ingest([tmp], s, out, route=saved.get("route", ""), acquired=saved.get("acquired", ""),
                     retrieved=saved.get("retrieved"))
        req["requests"] = saved.get("requests", [])
        req["n_requests"] = saved.get("n_requests", 0)
        with open(os.path.join(data_dir(out), "request.json"), "w", encoding="utf-8") as f:
            f.write(redact(json.dumps(req, indent=2)))
        return req
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- blocked
def report_blocked(err: FredError, env: str, s: dict | None, out: str, spec_path: str | None = None):
    """Tell the model how to get unblocked. Prints JSON to stdout and exits with code 4."""
    result = {"status": "blocked", "source": SOURCE, "kind": err.kind, "environment": env,
              "error": str(err)[:300], "next_command": ""}
    if s and spec_path:
        result["next_command"] = croutes.cmd(f'--source {SOURCE} "{os.path.abspath(spec_path)}" '
                                             f'--out "{os.path.abspath(out)}"', "--ingest",
                                             "<the uploaded files or their folder>")
    if err.kind in ("auth", "key_missing") and env in ("local", "ssh"):
        path, cmd = setup_script()
        result.update(route="run_setup", run_command=cmd, next_command="",
                      message=f"FRED rejected the key, or none is stored (request one at {KEY_URL}). Rerun with "
                              "--no-key if the request needs no key-only feature, or have the user store a key with "
                              "run_command in their own terminal.",
                      action="Give the user run_command for their own terminal; never ask for the key in the chat.")
    elif err.kind == "unavailable":
        result.update(route="report_error", next_command="", action="Tell the user and stop; try again later.",
                      message="FRED is down (it answered with a maintenance page, not data). The spec is fine: run "
                              "the same command again later.")
    elif err.kind == "rate":
        result.update(route="report_error", next_command="", action="Tell the user and stop.",
                      message="FRED's rate limit (120 requests a minute) was hit; wait a minute and run again.")
    elif err.kind == "network" and env in ("local", "ssh") and not restricted():
        result.update(route="report_error", next_command="", action="Tell the user and stop.",
                      message="FRED could not be reached: check the connection, VPN or proxy, then run again.")
    elif s:
        bundle = write_manual_bundle(s, out, f"{err.kind}: {str(err)[:120]}", spec_path)
        result.update(route="cloud_secret" if env == "cloud" and err.kind in ("auth", "key_missing") else
                      "cloud_allowlist" if env == "cloud" else "manual_download",
                      manual_bundle=bundle, instructions=os.path.join(bundle, "INSTRUCTIONS.md"),
                      download_scripts=download_scripts(bundle),
                      message="The data cannot be downloaded here. Send the bundle to the user and continue with "
                              "--ingest after the upload.",
                      action=SEND)
    else:
        result.update(route="report_error", message=str(err), action="Tell the user and stop.", next_command="")
    print(f"CANNOT DOWNLOAD FROM FRED: {result['message']}", file=sys.stderr)
    emit(result, EXIT_BLOCKED)


# --------------------------------------------------------------------------- lookups
def describe(ids: list[str], no_key: bool = False) -> list[dict]:
    key, _ = (None, None) if no_key else find_api_key()
    out = []
    if key:
        c = FredClient(api_key=key)
        for sid in ids:
            try:
                m = c.series(sid.upper())
                rel = c.release_of(sid.upper())
                src = c.release_sources(rel["id"]) if rel.get("id") is not None else []
                out.append({"series_id": sid.upper(), "title": m.get("title"), "units": m.get("units"),
                            "frequency": m.get("frequency"), "seasonal_adjustment": m.get("seasonal_adjustment"),
                            "observation_start": m.get("observation_start"), "observation_end": m.get("observation_end"),
                            "last_updated": m.get("last_updated"), "release": rel.get("name"),
                            "source": "; ".join(z.get("name", "") for z in src),
                            "copyright": copyright_from_tag(series_copyright_tag(c, sid.upper()))
                            or copyright_status(m.get("notes"), True),
                            "notes": (m.get("notes") or "")[:400], "url": SERIES_URL.format(sid.upper())})
            except FredError as e:
                out.append({"series_id": sid.upper(), "error": str(e)})
        return out
    k, meta = Keyless(), {}
    up = [x.upper() for x in ids]
    for i in range(0, len(up), GRAPH_MAX):
        meta.update(k.metadata(up[i:i + GRAPH_MAX])[0])
    for sid in ids:
        m = meta.get(sid.upper())
        out.append({"series_id": sid.upper(), **(m or {"error": "not found on FRED"}),
                    "source": "(needs a key, or read the series page)", "copyright": "unknown",
                    "url": SERIES_URL.format(sid.upper()), "metadata_source": "fredgraph.xlsx (keyless)"})
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="fetch.py --source fred", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out", default="out")
    ap.add_argument("--check", action="store_true", help="pre-flight only: print the route and what to do")
    ap.add_argument("--describe", nargs="+", metavar="SERIES_ID", help="title, units, frequency, source")
    ap.add_argument("--search", metavar="TEXT", help="search FRED for series (needs a key)")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--vintagedates", metavar="SERIES_ID", help="ALFRED vintage dates of a series (needs a key)")
    ap.add_argument("--release-dates", nargs="?", const="", metavar="RELEASE_ID",
                    help="release calendar (needs a key): one release's next dates, or every release in a window")
    ap.add_argument("--start", help="--release-dates: first date (default today)")
    ap.add_argument("--end", help="--release-dates: last date (default: 14 days ahead; no limit for one release)")
    ap.add_argument("--manual-bundle", action="store_true", help="write download links + scripts for another computer")
    ap.add_argument("--ingest", nargs="*", metavar="PATH",
                    help="downloaded files (or a folder) to package; none: the DATA_ANALYST_UPLOADS folders")
    ap.add_argument("--force", action="store_true", help="download again even if <out>/data/fred/ holds this request")
    ap.add_argument("--no-key", action="store_true", help="ignore any stored key and use the keyless downloads")
    ap.add_argument("--route-override", choices=["api", "api_keyless"], help="api_keyless = --no-key")
    ap.add_argument("--no-cache", action="store_true", help="accepted for the dispatcher; FRED data are never cached")
    ap.add_argument("--env", choices=["auto", *cenv.ENVIRONMENTS], default="auto")
    a = ap.parse_args()
    env = cenv.detect_environment(None if a.env == "auto" else a.env)
    no_key = a.no_key or a.route_override == "api_keyless"
    s = None
    try:
        if a.describe:
            emit({"series": describe([i for d in a.describe for i in d.replace(",", " ").split()], no_key)})
        if a.search or a.vintagedates or a.release_dates is not None:
            key, _ = find_api_key()
            if not key:
                q = urllib.parse.quote_plus(a.search or a.vintagedates or "release calendar")
                emit({"status": "needs_key", "message": "Search, vintage dates and the release calendar need a FRED "
                      "API key. Without one, search on the website (in-app browser or WebSearch): "
                      f"https://fred.stlouisfed.org/searchresults/?st={q} (calendar: "
                      "https://fred.stlouisfed.org/releases/calendar), and confirm each ID with --describe.",
                      "run_command": setup_script()[1] if env in ("local", "ssh") else ""}, EXIT_BLOCKED)
            c = FredClient(api_key=key)
            if a.search:
                res = c.search(a.search, limit=min(a.limit, 100))
                emit({"query": a.search, "results": [{k: r.get(k) for k in (
                    "id", "title", "units_short", "frequency_short", "seasonal_adjustment_short", "observation_start",
                    "observation_end", "popularity")} for r in res]})
            if a.vintagedates:
                v = c.vintagedates(a.vintagedates.upper())
                emit({"series_id": a.vintagedates.upper(), "n": len(v), "first": v[:5], "last": v[-5:], "all": v})
            if a.release_dates and not str(a.release_dates).isdigit():
                emit({"status": "error", "message": f"--release-dates takes a release ID (21 is H.6, 53 is GDP), not "
                      f"{a.release_dates!r}; a series' vintages are --vintagedates {a.release_dates}"}, EXIT_SPEC)
            rid = int(a.release_dates) if a.release_dates else None
            j = c.release_dates(rid, a.start, a.end, limit=min(a.limit if a.limit != 15 else 100, 1000))
            rows = release_date_rows(j)
            emit({"release_id": rid, "count": j.get("count"), "shown": min(len(rows), 20), "release_dates": rows[:20],
                  "note": "Capped at 20 rows; put \"release_dates\" in spec.json for the full list as a CSV. Common "
                          "releases: 10 CPI, 50 Employment Situation, 53 GDP, 18 H.15 interest rates."})
        spec_path = a.spec or os.path.join(a.out, "spec.json")
        if not os.path.exists(spec_path):
            emit({"status": "error", "kind": "spec", "message": f"spec.json not found: {spec_path}"}, EXIT_SPEC)
        try:
            with open(spec_path, encoding="utf-8-sig") as f:
                s = norm_spec(json.load(f))
        except (ValueError, KeyError, TypeError) as e:
            if a.check:
                emit({"status": "route_check", "route": "fix_spec", "message": f"spec.json has a problem: {redact(e)}",
                      "action": "Fix spec.json (see source.md; --describe, --search), then run --check again.",
                      "next_command": ""}, EXIT_SPEC)
            emit({"status": "error", "kind": "spec", "message": f"spec.json: {redact(e)}"}, EXIT_SPEC)
        if a.ingest is not None:
            paths = a.ingest or cenv.upload_dirs()
            if not paths:
                emit({"status": "error", "kind": "spec", "message": "--ingest needs the uploaded files or a folder "
                      "(and DATA_ANALYST_UPLOADS is not set)"}, EXIT_SPEC)
            req = ingest(paths, s, a.out)
            emit({"status": "ingested", "route": "manual_download", "rows": req["rows"], "latest": req["latest"],
                  "problems": req["problems"], "data_dir": data_dir(a.out),
                  "csv": os.path.join(data_dir(a.out), "fred_observations.csv"),
                  "read_next": os.path.join(PACK, "source.md")}, EXIT_SPEC if req["problems"] else EXIT_OK)
        if a.manual_bundle:
            d = write_manual_bundle(s, a.out, "a manual download was requested", spec_path)
            emit({"status": "ok", "route": "manual_download", "manual_bundle": d,
                  "instructions": os.path.join(d, "INSTRUCTIONS.md"), "download_scripts": download_scripts(d)})
        if a.check:
            r = route_check(s, a.out, env, a.force, spec_path)
            print(f"[fred] route: {r['route']} ({env}, key: {r['key_source'] or 'none'})\n[fred] {r['message']}\n"
                  f"[fred] next: {r['action']}", file=sys.stderr)
            emit(r, EXIT_SPEC if r["route"] in ("report_error", "fix_spec") else EXIT_OK)

        ex = existing_data(a.out, fingerprint(s))
        if (ex and ex["csv_exists"] and ex["matches"] and not ex["problems"] and not a.force
                and ex.get("meta_fingerprint") != meta_fingerprint(s)):
            req = repackage(s, a.out, ex)
            emit({"status": "repackaged", "route": "reuse_existing", "data_dir": data_dir(a.out),
                  "note": "labels, sources or copyright changed; data/fred/ rebuilt from data/fred/raw without "
                          "downloading", "retrieved": req["retrieved"], "problems": req["problems"]})
        if ex and ex["csv_exists"] and ex["matches"] and not ex["problems"] and not a.force:
            print(f"[fred] reusing {ex['path']} (retrieved {ex['retrieved']}); pass --force to download again",
                  file=sys.stderr)
            emit({"status": "reused_existing", "route": "reuse_existing", "data_dir": data_dir(a.out),
                  "csv": ex["path"], "retrieved": ex["retrieved"], "latest": ex["latest"]})
        n0 = len(SOURCES)
        key, source = (None, None) if no_key else find_api_key()
        if key:
            print(f"[fred] FRED API (key: {source}); {len(s['series'])} series", file=sys.stderr)
            parts = fetch_api(s, FredClient(api_key=key))
            route, acquired = "api", "FRED API download"
        else:
            if key_only_needs(s):
                raise FredError("needs a key: " + "; ".join(key_only_needs(s)), kind="key_missing")
            print(f"[fred] keyless graph downloads; {len(s['series'])} series", file=sys.stderr)
            parts = fetch_keyless(s, Keyless())
            route, acquired = "api_keyless", "FRED graph downloads (fredgraph.csv / alfredgraph.csv, no key)"
        req = package(parts, s, a.out, route, acquired, n0=n0)
        res = {"status": "downloaded", "route": route, "requests": req["n_requests"], "rows": req["rows"],
               "latest": req["latest"], "data_dir": data_dir(a.out),
               "csv": os.path.join(data_dir(a.out), "fred_observations.csv"),
               "series_info": os.path.join(data_dir(a.out), "series.csv"),
               "tables": [{k: t[k] for k in ("file", "rows")} for t in req["tables"]],
               "notes": req["notes"], "problems": req["problems"],
               "citation": "Series citations: data/fred/series.csv (citation column) and data/fred/README.md.",
               "read_next": os.path.join(PACK, "source.md")}
        if req["problems"]:
            print("ERROR: some series returned no data; check the IDs with --describe and fix spec.json:\n  "
                  + "\n  ".join(req["problems"]), file=sys.stderr)
            emit(res, EXIT_SPEC)
        emit(res)
    except FredError as e:
        if e.kind in ("auth", "key_missing", "network", "rate"):
            report_blocked(e, env, s, a.out, a.spec)
        emit({"status": "error", "kind": e.kind, "message": str(e)}, EXIT_SPEC)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        print("ERROR: " + redact(traceback.format_exc()), file=sys.stderr)
        emit({"status": "error", "kind": "unexpected", "message": redact(traceback.format_exc().splitlines()[-1])},
             EXIT_UNEXPECTED)


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):             # a Windows cp1252 console must not crash on "−" or "®"
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(errors="replace")
    main()

#!/usr/bin/env python
"""BLS time series through the BLS Public Data API, packaged for analysis (the bls deep pack).

Run through the skill's dispatcher (SKILL = the skill folder):
  python SKILL/scripts/fetch.py --source bls <out>/spec.json --out <out> --check          # which route works here?
  python SKILL/scripts/fetch.py --source bls <out>/spec.json --out <out>                  # download + package
  python SKILL/scripts/fetch.py --source bls --describe LNS14000000 CUUR0000SA0           # title, SA/NSA, dimensions
  python SKILL/scripts/fetch.py --source bls --decode LNS14000000                         # offline: survey, SA code
  python SKILL/scripts/fetch.py --source bls --search "unemployment rate black"           # topic catalog
  python SKILL/scripts/fetch.py --source bls --build laus --state MD --measure unemployment_rate
  python SKILL/scripts/fetch.py --source bls --build laus --county 24005                  # county LAUS (NSA)
  python SKILL/scripts/fetch.py --source bls --build oews --occupation 15-1252 [--state MD] [--measure ...]
  python SKILL/scripts/fetch.py --source bls --build state --state MD                     # LAUS + CES payrolls
  python SKILL/scripts/fetch.py --source bls --build jolts --industry all --measure quits  # JOLTS by industry
  python SKILL/scripts/fetch.py --source bls <out>/spec.json --out <out> --manual-bundle  # files for another computer
  python SKILL/scripts/fetch.py --source bls <out>/spec.json --out <out> --ingest [FILES] # package uploaded responses

Routes (--check prints one as "route"; first match wins):
  reuse_existing   <out>/data/bls/ already holds this exact request: nothing to download
  api              a registration key was found and BLS accepts it: download with API v2
  api_keyless      no key (or a rejected key), but the request fits the keyless v1 limits: download now
  run_setup        the request needs a key, none works, and the user has a terminal (local or ssh)
  cloud_secret     the request needs a key in a cloud session: add BLS_API_KEY to the environment's secrets
  manual_download  api.bls.gov cannot be reached from here (sandbox): the user downloads and uploads the JSON
  fix_spec         spec.json has a problem (no series, a malformed ID, start after end)
  report_error     BLS cannot be reached from the user's own machine, or the daily limit is used up
The normal run applies reuse_existing itself (--force downloads again).

Exit codes: 0 ok; 2 BLS or spec error (including a series that returned no data); 3 unexpected error;
4 cannot download (the printed JSON has the route to follow).

spec.json:
{
  "description": "Unemployment and job openings since 2015",
  "series": [{"id": "LNS14000000", "label": "Unemployment rate"},      # or plain "LNS14000000"
             {"id": "JTS000000000000000JOL", "label": "Job openings"}],
  "build": [{"kind": "laus", "state": "MD", "measure": "unemployment_rate"}],   # optional: built IDs
  "start_year": 2015, "end_year": 2026,                                  # end defaults to this year
  "annual_average": true,     # v2 only: BLS-published annual averages (period M13 / Q05 / S03)
  "catalog": true,            # v2 only: series titles; without a key, titles come from data.bls.gov
  "calculations": false       # v2 only: BLS's own 1-, 3-, 6- and 12-month net and percent changes
}

Output (<out>/data/bls/):
  bls_series.csv  one row per series and period: series_id, label, year, period, period_name, freq,
                  date, value (empty when BLS shows "-"), value_raw, preliminary, footnote_codes, footnotes, latest
                  (+ BLS calculations columns when requested)
  series.csv      one row per series: id, label, title, survey, seasonal, frequency, first/last period,
                  counts of observations, missing values and preliminary values
  raw/            every API response (request bodies without the key), for the audit trail
  request.json    the spec, API version, query plan, credential-free request log, retrieval time and route
  README.md       what is in the data, the gaps and flags, the citation and the BLS terms of use
tsstats.py is copied to <out>/lib/bls/ (facts.py, factfmt.py to <out>/lib/) so analysis.py reruns alone.
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
sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder
from datetime import date, datetime, timezone

PACK = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(PACK))
for p in (os.path.join(SKILL, "scripts"), PACK, os.path.join(PACK, "lib")):
    if p not in sys.path:
        sys.path.insert(0, p)
from core import env as cenv  # noqa: E402
from core import package as cpkg  # noqa: E402
from core import policy as cpol  # noqa: E402
from core import routes as croutes  # noqa: E402
from core.http import SOURCES, reachable as http_reachable  # noqa: E402
from bls_api import (LIMITS, REGISTER_URL, URLS, BlsClient, BlsError, describe, find_api_key,  # noqa: E402
                     plan_queries, redact, usage_today)
import series_ids  # noqa: E402
from series_ids import decode  # noqa: E402

SOURCE = "bls"
POLICY = cpol.load(PACK)
DESCRIBE_MAX = 60          # series looked up on data.bls.gov when there is no v2 catalog
EXIT_OK, EXIT_SPEC, EXIT_UNEXPECTED, EXIT_BLOCKED = (croutes.EXIT_OK, croutes.EXIT_SPEC, croutes.EXIT_UNEXPECTED,
                                                     croutes.EXIT_BLOCKED)
STATEMENT = POLICY["notices"][0] if POLICY.get("notices") else ""


def data_dir(out: str) -> str:
    return cpkg.data_dir(out, SOURCE)


def bundle_dir(out: str) -> str:
    return os.path.join(os.path.abspath(out), "manual_download", SOURCE)


def emit(obj: dict, code: int = 0):
    obj.setdefault("source", SOURCE)
    print(redact(json.dumps(obj, indent=2, default=str)))
    sys.exit(code)


# --------------------------------------------------------------------------- spec
def norm_spec(spec: dict) -> dict:
    series = []
    for s in spec.get("series") or []:
        if isinstance(s, str):
            series.append({"id": s.strip().upper(), "label": ""})
        else:
            series.append({"id": str(s["id"]).strip().upper(), "label": s.get("label", "")})
    for b in spec.get("build") or []:
        b = dict(b)
        for x in series_ids.build(b.pop("kind", ""), **b):
            series.append({"id": x["id"], "label": x["label"]})
    if not series:
        raise ValueError("spec.json has no series (give \"series\" or \"build\")")
    ids = [s["id"] for s in series]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"duplicate series in spec.json: {dup}")
    bad = [i for i in ids if not re.fullmatch(r"[A-Z0-9]{5,40}", i)]
    if bad:
        raise ValueError(f"not BLS series IDs (capital letters and digits only): {bad}")
    end = int(spec.get("end_year") or date.today().year)
    start = int(spec.get("start_year") or end - 9)
    if start > end:
        raise ValueError(f"start_year {start} is after end_year {end}")
    return {"description": spec.get("description", ""), "series": series, "start_year": start, "end_year": end,
            "annual_average": bool(spec.get("annual_average")), "catalog": bool(spec.get("catalog", True)),
            "calculations": bool(spec.get("calculations")), "aspects": bool(spec.get("aspects"))}


def fingerprint(s: dict) -> str:
    """Two specs asking for the same data hash the same (labels and description do not matter)."""
    core = {"series": sorted(x["id"] for x in s["series"]), "start": s["start_year"], "end": s["end_year"],
            "annual_average": s["annual_average"], "calculations": s["calculations"]}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- what is on disk
def existing_data(out: str, fp: str) -> dict | None:
    d = data_dir(out)
    rq, csv_path = os.path.join(d, "request.json"), os.path.join(d, "bls_series.csv")
    if not os.path.exists(rq):
        return None
    try:
        with open(rq, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    retrieved = saved.get("retrieved", "")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(retrieved)).days
    except ValueError:
        age = None
    return {"path": csv_path, "csv_exists": os.path.exists(csv_path), "matches": saved.get("fingerprint") == fp,
            "retrieved": retrieved, "age_days": age, "api_version": saved.get("api_version"),
            "route": saved.get("route"), "latest_period": saved.get("latest_period"),
            "problems": saved.get("problems") or []}


# --------------------------------------------------------------------------- route check
def route_check(s: dict, out: str, env: str, force: bool = False, spec_path: str | None = None) -> dict:
    out = os.path.abspath(out)
    spec_path = os.path.abspath(spec_path or os.path.join(out, "spec.json"))
    base = f'--source {SOURCE} "{spec_path}" --out "{out}"'
    run_cmd = croutes.cmd(base)
    ingest_cmd = croutes.cmd(base, "--ingest", "<the uploaded .json files or their folder>")
    ids = [x["id"] for x in s["series"]]
    plans = {v: plan_queries(ids, s["start_year"], s["end_year"], v) for v in ("v1", "v2")}
    key, source = find_api_key()
    r = {"status": "route_check", "source": SOURCE, "environment": env, "os": sys.platform,
         "restricted": cenv_restricted(), "key_env": "BLS_API_KEY", "key_source": source,
         "bls_reachable": None, "key_ok": None,
         "queries_needed": {"v1_keyless": len(plans["v1"]), "v2_with_key": len(plans["v2"])},
         "queries_used_today": {"v1": usage_today("v1"), "v2": usage_today("v2")},
         "v2_only_options_requested": [o for o in ("annual_average", "calculations") if s[o]],
         "route": None, "message": "", "action": "", "next_command": ""}
    ex = existing_data(out, fingerprint(s))

    def done(route, message, action, next_command=""):
        r.update(route=route, message=message, action=action, next_command=next_command)
        return r

    def bundle(reason):
        b = write_manual_bundle(s, out, reason)
        r.update(manual_bundle=b, instructions=os.path.join(b, "INSTRUCTIONS.md"), download_scripts=download_scripts(b),
                 send_files=[os.path.join(b, n) for n in SEND_FILES])
        return b

    if ex and ex["csv_exists"] and ex["matches"] and not ex["problems"] and not force:
        stale = ex["age_days"] is not None and ex["age_days"] >= 7
        r["existing_data"] = {k: ex[k] for k in ("path", "retrieved", "route", "latest_period")}
        return done("reuse_existing",
                    f"The data for this request are already in {ex['path']} (retrieved {cpol.local_day(ex['retrieved'])}, "
                    f"latest period {ex.get('latest_period')})." +
                    (" BLS has probably published newer data or revisions since then." if stale else ""),
                    "Skip the download and continue with the analysis." +
                    (" Ask whether to refresh (rerun with --force) if the latest month matters." if stale else
                     " Pass --force to download again."))

    ok, why = http_reachable(URLS["v1"])
    r["bls_reachable"] = ok
    v1_fits = len(plans["v1"]) <= LIMITS["v1"]["daily"]   # the request itself; the local tally only warns below
    over_tally = len(plans["v1"]) + r["queries_used_today"]["v1"] > LIMITS["v1"]["daily"]
    tally_note = (f" This computer's tally already shows {r['queries_used_today']['v1']} keyless queries today "
                  f"(BLS allows about {LIMITS['v1']['daily']} a day per IP address, counted by BLS, not here): if "
                  "BLS answers with a daily-threshold message, stop and offer the key setup." if over_tally else "")
    v1_cost = (f"{len(plans['v1'])} keyless quer{'y' if len(plans['v1']) == 1 else 'ies'} of the "
               f"{LIMITS['v1']['daily']} allowed per day")
    lost = r["v2_only_options_requested"]
    lost_note = (f" Without a key BLS does not return {', '.join(lost)}; the analysis computes them from the "
                 "monthly data and says so (not for medians or productivity: CPS median earnings, LE series, and PR "
                 "annual values are not means of their quarters; take BLS's annual table into published/, or FRED's "
                 "annual series '<id>A')." if lost else "")
    key_benefit = (f" A free key ({REGISTER_URL}) raises the limits to 500 queries a day, 50 series and 20 years "
                   "per query, and adds published annual averages and BLS's own net and percent changes.")

    if not ok:
        if env in ("local", "ssh") and not cenv_restricted():
            return done("report_error",
                        f"api.bls.gov could not be reached from this computer ({why}). Check the internet "
                        "connection, VPN and proxy settings, then run the request again.",
                        "Tell the user and stop. Do not substitute data from memory.")
        b = bundle("api.bls.gov cannot be reached from the analysis environment")
        if env == "cloud":
            return done("cloud_allowlist",
                        f"This cloud session cannot reach api.bls.gov ({why}). Allow api.bls.gov (and data.bls.gov "
                        f"for series titles) in the environment's network settings and restart, or download by hand "
                        f"with the files in {b}.",
                        "Offer both options; for the manual one: " + SEND_ACTION, ingest_cmd)
        return done("manual_download",
                    f"This {env} environment cannot reach api.bls.gov ({why}). In a hosted sandbox (claude.ai, "
                    f"Codex cloud) an admin can allow api.bls.gov in the network settings; otherwise the request files and the steps are "
                    f"in {b}: run them on your own computer and upload the JSON responses.",
                    SEND_ACTION, ingest_cmd)

    if key:
        p = BlsClient(api_key=key).ping()
        r.update(key_ok=p["auth_ok"], key_detail=p["detail"][:200])
        if p["auth_ok"]:
            if len(plans["v2"]) + r["queries_used_today"]["v2"] > LIMITS["v2"]["daily"]:
                return done("report_error",
                            f"The request needs {len(plans['v2'])} queries, more than remain of today's 500.",
                            "Tell the user; narrow the request (fewer series or years) or continue tomorrow.")
            return done("api", f"A BLS registration key was found ({source}) and BLS accepts it; the request "
                               f"takes {len(plans['v2'])} quer{'y' if len(plans['v2']) == 1 else 'ies'}.",
                        "Run next_command.", run_cmd)
        if p["kind"] == "quota":
            return done("report_error", f"BLS says the daily query limit is used up: {p['detail']}",
                        "Tell the user the limit resets tomorrow; offer --manual-bundle so they can download "
                        "from another computer, or wait.")
        if p["kind"] != "auth":
            return done("report_error", f"The key check failed: {p['detail']}", "Tell the user and stop.")
        fix = (f"BLS rejected the key found in {source}. BLS keys expire after a year: renew it at "
               f"{REGISTER_URL} and store the new one with the setup script.")
        if v1_fits:
            if env in ("local", "ssh"):
                path, cmd = cenv.setup_command(SOURCE)
                r.update(setup_script=path, run_command=cmd)
            return done("api_keyless", fix + f" The request fits the keyless limits ({v1_cost}), so the "
                        "download can go ahead now without the key." + tally_note + lost_note,
                        "Tell the user about the rejected key in one sentence, give run_command for later, and "
                        "run next_command now (it downloads without the key).", run_cmd + " --no-key")
        r["key_detail"] = fix

    # no working key
    if v1_fits:
        if env in ("local", "ssh"):
            path, cmd = cenv.setup_command(SOURCE)
            r.update(setup_script=path, run_command=cmd)
        return done("api_keyless",
                    f"No BLS registration key is set up; this request fits the keyless limits ({v1_cost})."
                    + tally_note + lost_note + key_benefit,
                    "Run next_command now. Mention once, in one sentence, that a free key raises the limits "
                    "(setup command in run_command); do not block on it.",
                    run_cmd + (" --no-key" if key else ""))
    need = (f"This request needs {len(plans['v1'])} keyless quer{'y' if len(plans['v1']) == 1 else 'ies'} (limit {LIMITS['v1']['daily']} a day, "
            f"{r['queries_used_today']['v1']} used today) or {len(plans['v2'])} with a key.")
    if env in ("local", "ssh"):
        path, cmd = cenv.setup_command(SOURCE)
        r.update(setup_script=path, run_command=cmd)
        return done("run_setup",
                    need + f" Register for a free key at {REGISTER_URL} (BLS emails it within minutes); the setup "
                    "script asks for it with hidden input, checks it with BLS and stores it in "
                    "the skill's keys.json (~/.config/data-analyst).",
                    "Give the user run_command to run in their own terminal (do not run it for them, and never "
                    "ask for the key in the chat). Offer the alternative of a smaller request (fewer series or "
                    "years) that fits the keyless limits. When the user says the key is stored, run --check again.",
                    cmd)
    b = bundle("the request needs a BLS registration key")
    if env == "cloud":
        return done("cloud_secret",
                    need + " Add BLS_API_KEY to the cloud environment's secrets and start the session again, or "
                    f"download on your own computer with the files in {b}.",
                    "Explain both options (and the smaller-request alternative). If the user downloads by hand: "
                    + SEND_ACTION, ingest_cmd)
    return done("manual_download",
                need + f" A key cannot be stored in this environment; the request files and the steps are in {b}: "
                "run them on your own computer and upload the JSON responses.",
                SEND_ACTION, ingest_cmd)


def cenv_restricted() -> bool:
    from core.keys import restricted
    return restricted()


# --------------------------------------------------------------------------- manual bundle
MANUAL_TEMPLATE = """# Download the BLS data on your own computer

The analysis environment cannot download the data itself (@REASON@). The two download scripts,
`download_bls.sh` and `download_bls.ps1`, already contain the @N@ API request(s): each needs no
other file.

@KEYNOTE@

## Option A: one command (recommended)

Save the script for your system in your Downloads folder, open a terminal and paste one line:

- macOS, Linux, Git Bash or WSL (needs only curl):
  ```bash
  bash ~/Downloads/download_bls.sh
  ```
- Windows, in PowerShell:
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\\Downloads\\download_bls.ps1"
  ```

If you saved the script somewhere else, use the folder you saved it in instead of `Downloads`.
The script sends every request and saves `response_01.json`, `response_02.json`, ... next to
itself. At the end it prints the full paths of the files to upload.

@KEYLINE@

## Option B (fallback): curl, macOS / Linux / Git Bash / WSL

```bash
@BASH@
```

## Option C (fallback): curl in Windows PowerShell

```powershell
@PS@
```

## Upload

Upload every `response_*.json` file to the chat (do not edit them). After the upload the assistant
packages the files. A response with `"status": "REQUEST_NOT_PROCESSED"` means BLS refused it (often the daily
limit); send it anyway: the message says why. One exception for the curl steps: when BLS says the key
is invalid, its reply repeats the key, so do not upload that file; check the key and run again (the
download scripts remove the key from the reply themselves).
"""

DOWNLOAD_SCRIPTS = {"bash": "download_bls.sh", "powershell": "download_bls.ps1"}
SEND_FILES = ("INSTRUCTIONS.md", "download_bls.sh", "download_bls.ps1", "spec.json")
SEND_ACTION = ("Send INSTRUCTIONS.md, download_bls.sh, download_bls.ps1 and spec.json from the bundle to the user "
               "(your file-sending tool when available; print the full paths), wait for the upload, then "
               "run next_command.")
QUERIES_MARK = "# @QUERIES@"


def embed_requests(kind: str, files: dict) -> str:
    """The script lines that write each request file (name -> JSON text) into the script's temp folder."""
    out = []
    for name, text in files.items():
        lines = text.splitlines()
        if not text.isascii() or "BLS_REQUEST_EOF" in lines or any(x.startswith("'@") for x in lines):
            raise ValueError(f"{name} cannot be built into the download scripts")
        if kind == "bash":     # quoted heredoc: no expansion inside
            out.append(f"cat > \"$REQ/{name}\" <<'BLS_REQUEST_EOF'\n{text}\nBLS_REQUEST_EOF")
        else:                  # single-quoted here-string: no expansion inside
            out.append(f"$Embedded['{name}'] = @'\n{text}\n'@")
    return "\n".join(out)


def write_script_templates(d: str, files: dict) -> None:
    """Write download_bls.sh and .ps1 (ASCII) from assets/, with the request files built in at @QUERIES@."""
    for kind in ("bash", "powershell"):
        name = DOWNLOAD_SCRIPTS[kind]
        try:
            with open(os.path.join(PACK, "assets", name), encoding="ascii") as f:
                text = f.read()
            if text.count(QUERIES_MARK) != 1:
                raise ValueError(f"no single {QUERIES_MARK} line")
            text = text.replace(QUERIES_MARK, embed_requests(kind, files))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            print(f"[bls] warning: {name} not written ({e})", file=sys.stderr)
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


def write_manual_bundle(s: dict, out: str, reason: str) -> str:
    """Write the two self-contained download scripts, spec.json, the curl steps and the request files the
    scripts carry. Needs no network and no key."""
    d = bundle_dir(out)
    os.makedirs(d, exist_ok=True)
    for old in glob.glob(os.path.join(d, "query_*.json")) + glob.glob(os.path.join(d, "download_bls.py")):
        os.remove(old)
    ids = [x["id"] for x in s["series"]]
    v1 = plan_queries(ids, s["start_year"], s["end_year"], "v1")
    version = "v2" if len(v1) > LIMITS["v1"]["daily"] else "v1"      # keyless whenever it fits
    v2_options = {k: True for k, opt in (("annualaverage", "annual_average"), ("calculations", "calculations"),
                                         ("catalog", "catalog")) if s.get(opt)}
    plan = plan_queries(ids, s["start_year"], s["end_year"], version)
    bash = ["cd ~/Downloads   # the folder for the responses"]
    ps = ["cd $HOME\\Downloads   # the folder for the responses"]
    if version == "v2":
        # the key is typed at a hidden prompt and reaches curl inside the body on stdin, never on a command line
        bash.append("printf 'BLS key (hidden): '; read -rs BLS_API_KEY; echo"
                    "   # in your own terminal only; never in the chat")
        ps.append("$env:BLS_API_KEY = [Net.NetworkCredential]::new('', (Read-Host 'BLS key (hidden)' -AsSecureString))"
                  ".Password   # in your own terminal only; never in the chat")
    files = {"manifest.json": json.dumps({"source": SOURCE, "api_version": version, "urls": URLS,
                                          "v2_options": v2_options, "queries": len(plan)})}
    url = URLS[version]
    hdr = '-H "Content-Type: application/json"'
    for i, (chunk, y0, y1) in enumerate(plan, start=1):
        body = {"seriesid": chunk, "startyear": str(y0), "endyear": str(y1)}
        if version == "v2":
            body.update(v2_options)
            body["registrationkey"] = "__BLS_KEY__"      # filled in from BLS_API_KEY on the user's machine
        q, r_ = f"query_{i:02d}.json", f"response_{i:02d}.json"
        files[q] = json.dumps(body, indent=1)
        one = json.dumps(body, separators=(",", ":"))   # series IDs are letters and digits: safe inside '...'
        if version == "v2":
            bash.append(f"b='{one}'; printf \"%s\" \"${{b/__BLS_KEY__/$BLS_API_KEY}}\" | "
                        f"curl -sS -X POST {hdr} --data-binary @- {url} -o {r_}")
            ps.append(f"'{one}'.Replace('__BLS_KEY__', $env:BLS_API_KEY) | "
                      f"curl.exe -sS -X POST {hdr} --data-binary '@-' {url} -o {r_}")
        else:
            bash.append(f"curl -sS -X POST {hdr} --data-binary '{one}' {url} -o {r_}")
            ps.append(f"'{one}' | curl.exe -sS -X POST {hdr} --data-binary '@-' {url} -o {r_}")
    for name, text in files.items():                    # kept as the record of what the scripts send
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    keynote = ("These requests need a free BLS registration key (" + REGISTER_URL + "): there are more than "
               "the 25 keyless queries a day." if version == "v2" else
               "No key is needed: these fit the keyless limits (25 queries a day).")
    keyline = ("The script asks for your BLS key with hidden input (nothing shows while you paste it), or uses "
               "`BLS_API_KEY` when it is set in that terminal. The key is only sent to api.bls.gov; it is never "
               "shown or saved. Never paste the key into the chat." if version == "v2" else
               "No key is needed. If you have a BLS key, set `BLS_API_KEY` in that terminal first and the script "
               "uses API v2, which also returns published annual averages and series titles. Never paste the key "
               "into the chat.")
    write_script_templates(d, files)
    text = (MANUAL_TEMPLATE.replace("@REASON@", reason).replace("@N@", str(len(plan)))
            .replace("@KEYNOTE@", keynote).replace("@KEYLINE@", keyline)
            .replace("@BASH@", "\n".join(bash)).replace("@PS@", "\n".join(ps)))
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return d


# --------------------------------------------------------------------------- parse + package
def period_info(year: int, period: str) -> tuple[str, str]:
    """(freq, date) for a BLS period code. Annual averages get freq 'A' and the mid-year date."""
    p = period.upper()
    if p == "M13" or p == "Q05" or p == "S03" or p.startswith("A"):
        return "A", f"{year}-07-01"
    if p.startswith("M"):
        return "M", f"{year}-{int(p[1:]):02d}-01"
    if p.startswith("Q"):
        return "Q", f"{year}-{3 * int(p[1:]) - 2:02d}-01"
    if p.startswith("S"):
        return "S", f"{year}-{1 if p == 'S01' else 7:02d}-01"
    return "?", f"{year}-01-01"


def period_end_month(freq: str, period: str) -> int:
    """Last month a period covers (M09 -> 9, Q03 -> 9, S01 -> 6), to order periods of mixed frequencies."""
    try:
        n = int(period[1:])
    except ValueError:
        return 0
    return {"M": n, "Q": 3 * n, "S": 6 * n}.get(freq, 0)


def absent_periods(sub: list[dict]) -> list[str]:
    """Monthly, quarterly or semiannual periods between a series' first and last row that BLS returned no row
    for at all (not even "-"): the API leaves some out (CPI items, November 2025)."""
    out = []
    for f, k in (("M", 12), ("Q", 4), ("S", 2)):
        have = {r["year"] * k + int(r["period"][1:]) - 1 for r in sub if r["freq"] == f and r["period"][1:].isdigit()}
        if have:
            out += [f"{i // k} {f}{i % k + 1:02d}" for i in range(min(have), max(have) + 1) if i not in have]
    return out


def tidy(by_series: dict, labels: dict) -> list[dict]:
    rows = []
    for sid, rec in by_series.items():
        for o in rec["data"]:
            year, period = int(o["year"]), o["period"]
            freq, dt = period_info(year, period)
            raw = str(o.get("value", "")).strip()
            try:
                val = float(raw.replace(",", ""))
            except ValueError:
                val = None
            fns = [f for f in (o.get("footnotes") or []) if f and (f.get("code") or f.get("text"))]
            codes = ";".join(str(f.get("code", "")) for f in fns)
            texts = " | ".join(str(f.get("text", "")) for f in fns)
            prelim = any(str(f.get("code", "")).upper() == "P" or "preliminary" in str(f.get("text", "")).lower()
                         for f in fns)
            row = {"series_id": sid, "label": labels.get(sid, ""), "year": year, "period": period,
                   "period_name": o.get("periodName", ""), "freq": freq, "date": dt,
                   "value": "" if val is None else repr(val), "value_raw": raw, "preliminary": int(prelim),
                   "footnote_codes": codes, "footnotes": texts, "latest": int(str(o.get("latest", "")) == "true")}
            calc = o.get("calculations") or {}
            for kind in ("net_changes", "pct_changes"):
                for k, v in (calc.get(kind) or {}).items():
                    row[f"bls_{kind[:-8] if kind.endswith('_changes') else kind}_{k}m"] = v
            rows.append(row)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["series_id"], r["date"], r["period"])):
        k = (r["series_id"], r["year"], r["period"])
        if k not in seen:                     # overlapping query windows never duplicate a period
            seen.add(k)
            out.append(r)
    return out


def write_csv(path: str, rows: list[dict], fields: list[str] | None = None):
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def copy_lib(out: str) -> str:
    """analysis.py imports tsstats from <out>/lib/bls first, so the folder reruns without the skill."""
    lib = os.path.join(os.path.abspath(out), "lib", SOURCE)
    os.makedirs(lib, exist_ok=True)
    shutil.copy2(os.path.join(PACK, "lib", "tsstats.py"), os.path.join(lib, "tsstats.py"))
    shared = os.path.join(PACK, "..", "..", "scripts", "analysis")      # tsstats.py loads these from lib/
    for f in ("facts.py", "factfmt.py", "combine.py"):
        shutil.copy2(os.path.join(shared, f), os.path.join(os.path.dirname(lib), f))
    return lib


def package(by_series: dict, messages: list[str], raw: list[dict], s: dict, out: str, api_version: str,
            acquired: str, route: str, describe_titles: bool = True,
            extra_problems: list[str] | None = None, n0: int = 0) -> dict:
    d = data_dir(out)
    os.makedirs(os.path.join(d, "raw"), exist_ok=True)
    for old in glob.glob(os.path.join(d, "raw", "response_*.json")):
        os.remove(old)
    for i, x in enumerate(raw, start=1):
        with open(os.path.join(d, "raw", f"response_{i:02d}.json"), "w", encoding="utf-8") as f:
            f.write(redact(json.dumps(x, indent=1)))
    labels = {x["id"]: x["label"] for x in s["series"]}
    rows = tidy({k: v for k, v in by_series.items() if k in labels or v["data"]}, labels)
    base = ["series_id", "label", "year", "period", "period_name", "freq", "date", "value", "value_raw",
            "preliminary", "footnote_codes", "footnotes", "latest"]
    extra = sorted({k for r in rows for k in r} - set(base))
    write_csv(os.path.join(d, "bls_series.csv"), rows, base + extra)

    meta, problems = [], list(extra_problems or [])
    looked_up = 0
    for x in s["series"]:
        sid = x["id"]
        mine = [r for r in rows if r["series_id"] == sid]
        sub = [r for r in mine if r["freq"] != "A"] or mine
        cat = (by_series.get(sid) or {}).get("catalog") or {}
        dec = decode(sid)
        title, seasonal, source = cat.get("series_title", ""), cat.get("seasonality", "") or dec["seasonal"], "catalog"
        if not title and describe_titles and looked_up < DESCRIBE_MAX:
            looked_up += 1
            info = describe(sid)
            if info.get("error") and "not found" not in info["error"]:
                describe_titles = False       # data.bls.gov unreachable: do not wait on every other series
            title = info.get("title") or "; ".join(f"{k}: {v}" for k, v in (info.get("fields") or {}).items()
                                                  if k.lower() != "series id")
            seasonal = seasonal or info.get("seasonal", "")
            source = "data.bls.gov" if title else ""
        if not title:
            source = ""
        sid_msgs = [m for m in messages if sid in m.upper()]
        if not mine:
            problems.append(f"{sid}: no data returned" + (f" ({'; '.join(sid_msgs)})" if sid_msgs else ""))
        freqs = sorted({r["freq"] for r in sub})
        meta.append({"series_id": sid, "label": x["label"], "title": title, "title_source": source,
                     "survey": dec["survey"], "seasonal": seasonal, "frequency": "/".join(freqs),
                     "first_period": f"{sub[0]['year']} {sub[0]['period']}" if sub else "",
                     "last_period": f"{sub[-1]['year']} {sub[-1]['period']}" if sub else "",
                     "n_obs": len(sub), "n_missing": sum(1 for r in sub if r["value"] == "") + len(absent_periods(sub)),
                     "absent_periods": " ".join(absent_periods(sub)),
                     "n_preliminary": sum(r["preliminary"] for r in sub),
                     "has_annual_average": int(any(r["freq"] == "A" for r in mine) and freqs != ["A"]),
                     "bls_messages": " | ".join(sid_msgs)})
    meta_fields = ["series_id", "label", "title", "title_source", "survey", "seasonal", "frequency", "first_period",
                   "last_period", "n_obs", "n_missing", "absent_periods", "n_preliminary", "has_annual_average",
                   "bls_messages"]
    write_csv(os.path.join(d, "series.csv"), meta, meta_fields)

    sub = [r for r in rows if r["freq"] != "A"] or rows    # annual-only pulls (OEWS) report their year
    latest = max(((r["year"], period_end_month(r["freq"], r["period"]), f"{r['year']} {r['period']}")
                  for r in sub), default=(0, 0, ""))[2]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tables = []
    for name in ("bls_series", "series"):
        p = os.path.join(d, f"{name}.csv")
        tables.append({"name": name, "file": f"{name}.csv", "rows": len(rows) if name == "bls_series" else len(meta),
                       "sha256": cpkg.sha256(p)})
    req = {"source": SOURCE, "fingerprint": fingerprint(s), "retrieved": now, "api_version": api_version,
           "route": route, "acquired": acquired, "spec": s, "queries": [x["request"] for x in raw],
           "n_queries": len(raw), "requests": SOURCES[n0:], "tables": tables,
           "messages": messages, "problems": problems, "latest_period": latest, "rows": len(rows),
           "v2_options_dropped": [] if api_version == "v2" else
           [o for o in ("annual_average", "calculations", "catalog") if s.get(o)],
           "policy": {"redistribution": POLICY["redistribution"]["mode"], "cache": POLICY["cache"]["mode"]}}
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        f.write(redact(json.dumps(req, indent=2)))
    write_readme(d, s, meta, rows, req)
    copy_lib(out)
    return req


def write_readme(d: str, s: dict, meta: list[dict], rows: list[dict], req: dict):
    retrieved = cpol.local_day(req["retrieved"])          # the user's date, not UTC's
    missing = [r for r in rows if r["value"] == ""]
    prelim = [r for r in rows if r["preliminary"]]
    lines = [
        f"# BLS time series: {s.get('description') or ', '.join(m['series_id'] for m in meta)}",
        "",
        f"- Retrieved: {cpol.local_stamp(req['retrieved'])} from the BLS Public Data API {req['api_version']} ({req['acquired']}; "
        f"route `{req['route']}`)",
        f"- Years requested: {s['start_year']} to {s['end_year']}; latest period in the data: {req['latest_period']}",
        f"- Rows: {len(rows):,} in `bls_series.csv` (one per series and period); series details in `series.csv`",
        "- Raw API responses: `raw/`; the exact requests: `request.json`",
        "",
        "## Series",
        "| Series ID | Label | Title | Seasonal | Freq | Periods | Missing | Preliminary |",
        "|---|---|---|---|---|---|---|---|",
        *[f"| `{m['series_id']}` | {m['label']} | {m['title']} | {m['seasonal']} | {m['frequency']} | "
          f"{m['first_period']} to {m['last_period']} ({m['n_obs']}) | {m['n_missing']} | {m['n_preliminary']} |"
          for m in meta],
        "",
        "## Read this before analyzing",
        "- `value` is empty where BLS shows `-` (not collected or not published); `footnotes` says why. Never fill "
        "these by interpolation without saying so; a change across a gap needs both endpoints.",
        "- `preliminary` = 1 marks estimates BLS will revise (CES: the two latest months; JOLTS and LAUS: the "
        "latest month). Say so when citing them.",
        "- Rows with `freq` = A are annual: M13, Q05 and S03 are BLS-published annual averages, not a month; A01 "
        "is a survey published once a year (OEWS refers to May of that year, not an average). `date` is July 1 "
        "for all of them: describe the period from `period` and the survey, not from `date`.",
    ]
    if missing:
        gaps = sorted({f"{r['year']} {r['period_name']}" for r in missing})
        lines.append(f"- Missing values in this pull: {len(missing)} ({', '.join(gaps[:8])}"
                     f"{', ...' if len(gaps) > 8 else ''}).")
    if prelim:
        lines.append(f"- Preliminary values in this pull: {len(prelim)}.")
    dropped = [o for o in req.get("v2_options_dropped") or [] if o != "catalog"]   # titles come from data.bls.gov
    if dropped:
        lines.append(f"- Requested but not available without a key: {', '.join(dropped)}. "
                     "Annual averages must be computed from the monthly data (state it in the methods).")
    if req.get("problems"):
        lines += ["", "## Problems", *[f"- {p}" for p in req["problems"]]]
    lines += [
        "",
        "## Reading the data",
        "```python",
        "import pandas as pd",
        "df = pd.read_csv('bls_series.csv', parse_dates=['date'])",
        "m = df[df.freq == 'M'].pivot(index='date', columns='series_id', values='value')",
        "```",
        "",
        "## Citation and terms of use",
        f"Source: U.S. Bureau of Labor Statistics, series {', '.join(m['series_id'] for m in meta)}, retrieved "
        f"{retrieved} through the BLS Public Data API (https://www.bls.gov/developers/).",
        "",
        "BLS terms of service (https://www.bls.gov/developers/termsOfService.htm) require citing the date the "
        f"data were retrieved and stating: \"{STATEMENT}\" The BLS logo may not be used.",
        "",
        "## Use and redistribution",
        f"Rule: **{POLICY['redistribution']['mode']}**. {POLICY['redistribution'].get('why', '')}",
    ]
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- ingest uploaded responses
def uncovered_years(s: dict, requests: list[dict]) -> list[str]:
    """Spec series/years that no successful request asked for (an upload missing some responses)."""
    covered: dict[str, set] = {}
    for q in requests:
        try:
            y0, y1 = int(q["startyear"]), int(q["endyear"])
        except (KeyError, TypeError, ValueError):
            continue
        for sid in q.get("seriesid") or []:
            covered.setdefault(str(sid).upper(), set()).update(range(y0, y1 + 1))
    out = []
    for x in s["series"]:
        miss = sorted(set(range(s["start_year"], s["end_year"] + 1)) - covered.get(x["id"], set()))
        if miss:
            span = f"{miss[0]}" if len(miss) == 1 else f"{miss[0]}-{miss[-1]}"
            out.append(f"{x['id']}: no successful response covers {span} ({len(miss)} year(s)); "
                       "upload every response_*.json")
    return out


def ingest(paths: list[str], s: dict, out: str, describe_titles: bool = True) -> dict:
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "**", "*.json"), recursive=True))
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise BlsError(f"Not found: {p}", kind="request")
    by_series, messages, raw, refused = {}, [], [], []
    for fp in files:
        try:
            with open(fp, encoding="utf-8-sig") as f:
                j = json.loads(f.read())
        except (OSError, ValueError):
            print(f"[bls] skipping {fp}: not JSON", file=sys.stderr)
            continue
        if isinstance(j, dict) and isinstance(j.get("response"), dict):     # our own raw/ wrapper
            j = {**j["response"], "_request": j.get("request")}
        if not isinstance(j, dict) or ("Results" not in j and "status" not in j):
            continue                              # a query_*.json or another file
        if j.get("status") != "REQUEST_SUCCEEDED":
            refused.append(redact(f"{os.path.basename(fp)}: BLS refused the request ({j.get('status')}) "
                                  f"{' '.join(j.get('message') or [])}"))
            continue
        req = j.pop("_request", {"file": os.path.basename(fp)})
        raw.append({"request": req, "response": j})
        messages += [redact(m) for m in j.get("message") or []]
        for ser in (j.get("Results") or {}).get("series", []):
            sid = ser.get("seriesID", "").upper()
            rec = by_series.setdefault(sid, {"data": [], "catalog": None})
            rec["data"].extend(ser.get("data") or [])
            rec["catalog"] = rec["catalog"] or ser.get("catalog")
    if refused:
        print("[bls] BLS refused some uploaded requests:\n  " + "\n  ".join(refused), file=sys.stderr)
    if not raw:
        raise BlsError("No successful BLS API responses among the uploaded files.", kind="request")
    v2_signs = (any(rec["catalog"] for rec in by_series.values())
                or any(o.get("period") in ("M13", "Q05", "S03") or o.get("calculations")
                       for rec in by_series.values() for o in rec["data"]))
    version = "v2" if v2_signs else "v1"
    reqs = [x["request"] for x in raw]
    if all(isinstance(q, dict) and "seriesid" in q for q in reqs):
        problems = uncovered_years(s, reqs)          # a refused request that was later retried is fine
    else:
        problems = refused                          # cannot tell what is missing: every refusal counts
        print("[bls] some uploaded responses do not say which request they answer (curl fallback); "
              "check that every response_*.json was uploaded", file=sys.stderr)
    return package(by_series, messages, raw, s, out, version, "downloaded by the user and uploaded", "manual_download",
                   describe_titles=describe_titles, extra_problems=problems, n0=0)


# --------------------------------------------------------------------------- blocked
def report_blocked(err: BlsError, env: str, s: dict | None, out: str, spec_path: str | None):
    """Tell the model how to get unblocked. Prints JSON to stdout and exits with code 4."""
    result = {"status": "blocked", "source": SOURCE, "kind": err.kind, "environment": env,
              "error": str(err)[:300], "next_command": ""}
    if s and spec_path:
        result["next_command"] = croutes.cmd(f'--source {SOURCE} "{os.path.abspath(spec_path)}" '
                                             f'--out "{os.path.abspath(out)}"', "--ingest",
                                             "<the uploaded .json files or their folder>")
    if err.kind == "auth" and env in ("local", "ssh"):
        path, cmd = cenv.setup_command(SOURCE)
        result.update(route="run_setup", run_command=cmd, next_command="",
                      message=f"BLS rejected the key (keys expire yearly; renew at {REGISTER_URL}). Rerun with "
                              "--no-key if the request fits the keyless limits, or have the user store a new key "
                              "with run_command in their own terminal.",
                      action="Give the user run_command for their own terminal; never ask for the key in the chat.")
    elif err.kind == "quota":
        result.update(route="report_error", next_command="",
                      message="The BLS daily query limit is used up. It resets the next day; --manual-bundle lets "
                              "the user download from another computer meanwhile.", action="Tell the user and stop.")
    elif err.kind == "network" and env in ("local", "ssh") and not cenv_restricted():
        result.update(route="report_error", next_command="",
                      message="api.bls.gov could not be reached: check the connection, VPN or proxy, then run again.",
                      action="Tell the user and stop. Do not substitute data from memory.")
    elif s:
        bundle = write_manual_bundle(s, out, f"{err.kind}: {str(err)[:120]}")
        result.update(route="cloud_secret" if env == "cloud" and err.kind == "auth" else
                      "cloud_allowlist" if env == "cloud" else "manual_download",
                      manual_bundle=bundle, instructions=os.path.join(bundle, "INSTRUCTIONS.md"),
                      download_scripts=download_scripts(bundle),
                      message="The data cannot be downloaded here. Send the bundle to the user and continue with "
                              "--ingest after the upload.",
                      send_files=[os.path.join(bundle, n) for n in SEND_FILES], action=SEND_ACTION)
    else:
        result.update(route="report_error", message=str(err), action="Tell the user and stop.", next_command="")
    print(f"CANNOT DOWNLOAD FROM BLS: {result['message']}", file=sys.stderr)
    emit(result, EXIT_BLOCKED)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="fetch.py --source bls", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true", help="pre-flight only: print the route and what to do")
    ap.add_argument("--describe", nargs="+", metavar="SERIES_ID", help="title and dimensions of each series")
    ap.add_argument("--decode", nargs="+", metavar="SERIES_ID", help="offline: survey and seasonal code")
    ap.add_argument("--search", metavar="WORDS", help="search the topic catalog of common series (offline)")
    ap.add_argument("--build", choices=series_ids.BUILDERS, help="build series IDs (laus, oews, state, ces_state, jolts)")
    ap.add_argument("--state", help="--build: state (USPS code, FIPS or name)")
    ap.add_argument("--county", help="--build laus: 5-digit county FIPS code(s), comma-separated")
    ap.add_argument("--measure", help="--build: LAUS or OEWS measure")
    ap.add_argument("--occupation", help="--build oews: SOC code(s) such as 15-1252, comma-separated")
    ap.add_argument("--seasonal", default="S", help="--build laus/ces_state: S (adjusted, default) or U")
    ap.add_argument("--industry", default="00000000", help="--build ces_state: 8-digit CES industry code; "
                    "--build jolts: JOLTS industry code(s) or name(s), or all")
    ap.add_argument("--manual-bundle", action="store_true", help="write request files + steps for another computer")
    ap.add_argument("--ingest", nargs="*", metavar="PATH",
                    help="API response JSON files (or a folder) to package; none: the DATA_ANALYST_UPLOADS folders")
    ap.add_argument("--force", action="store_true", help="download again even if <out>/data/bls/ holds this request")
    ap.add_argument("--no-key", action="store_true", help="ignore any stored key and use keyless API v1")
    ap.add_argument("--route-override", choices=["api", "api_keyless"], help="api_keyless = --no-key")
    ap.add_argument("--no-describe", action="store_true", help="skip series-title lookups on data.bls.gov")
    ap.add_argument("--env", choices=["auto", *cenv.ENVIRONMENTS], default="auto")
    ap.add_argument("--no-cache", action="store_true", help="accepted for the dispatcher; BLS keeps no cache")
    a = ap.parse_args()
    env = cenv.detect_environment(None if a.env == "auto" else a.env)
    no_key = a.no_key or a.route_override == "api_keyless"
    s = None
    out = a.out or "out"
    try:
        if a.decode:
            emit({"series": [decode(x) for x in a.decode]})
        if a.describe:
            # the page's empty fields (no SA cell for PR series) never hide what the ID itself says
            emit({"series": [{**decode(x), **{k: v for k, v in describe(x).items() if v not in ("", None)}}
                             for x in a.describe]})
        if a.search:
            hits = series_ids.search_catalog(a.search)
            emit({"query": a.search, "results": hits, "topics": series_ids.topics(),
                  "note": "A curated list of common series, not a BLS search. Build area and occupation IDs with "
                          "--build; confirm any ID with --describe. More: docs/series_ids.md."})
        if a.build:
            try:
                built = series_ids.build(a.build, state=a.state, county=a.county, occupation=a.occupation,
                                         measure=a.measure, seasonal=a.seasonal, industry=a.industry)
            except ValueError as e:
                emit({"status": "error", "kind": "spec", "message": str(e)}, EXIT_SPEC)
            emit({"build": a.build, "series": built,
                  "note": "Put these in spec.json \"series\" (or the same arguments in \"build\"); confirm titles "
                          "with --describe."})
        spec_path = a.spec or os.path.join(out, "spec.json")
        if not os.path.exists(spec_path):
            emit({"status": "error", "kind": "spec", "message": f"spec.json not found: {spec_path}"}, EXIT_SPEC)
        try:
            with open(spec_path, encoding="utf-8-sig") as f:
                s = norm_spec(json.load(f))
        except (ValueError, KeyError, TypeError) as e:
            if a.check:
                emit({"status": "route_check", "route": "fix_spec", "message": f"spec.json has a problem: {e}",
                      "action": "Fix spec.json (see source.md; --search, --build, --describe), then run --check "
                                "again.", "next_command": ""}, EXIT_SPEC)
            emit({"status": "error", "kind": "spec", "message": f"spec.json: {e}"}, EXIT_SPEC)
        if a.ingest is not None:
            paths = a.ingest or cenv.upload_dirs()
            if not paths:
                emit({"status": "error", "kind": "spec", "message": "--ingest needs the uploaded files or a folder "
                      "(and DATA_ANALYST_UPLOADS is not set)"}, EXIT_SPEC)
            req = ingest(paths, s, out, describe_titles=not a.no_describe)
            emit({"status": "ingested", "route": "manual_download", "rows": req["rows"],
                  "latest_period": req["latest_period"], "problems": req["problems"], "data_dir": data_dir(out),
                  "csv": os.path.join(data_dir(out), "bls_series.csv"), "read_next": os.path.join(PACK, "source.md")},
                 EXIT_SPEC if req["problems"] else EXIT_OK)
        if a.manual_bundle:
            d = write_manual_bundle(s, out, "a manual download was requested")
            emit({"status": "ok", "route": "manual_download", "manual_bundle": d,
                  "instructions": os.path.join(d, "INSTRUCTIONS.md"), "download_scripts": download_scripts(d),
                  "send_files": [os.path.join(d, n) for n in SEND_FILES], "action": SEND_ACTION,
                  "next_command": croutes.cmd(f'--source {SOURCE} "{os.path.abspath(spec_path)}" '
                                              f'--out "{os.path.abspath(out)}"', "--ingest",
                                              "<the uploaded .json files or their folder>")})
        if a.check:
            r = route_check(s, out, env, a.force, spec_path)
            print(f"[bls] route: {r['route']} ({env}, key: {r['key_source'] or 'none'})\n[bls] {r['message']}\n"
                  f"[bls] next: {r['action']}", file=sys.stderr)
            emit(r, EXIT_SPEC if r["route"] in ("report_error", "fix_spec") else EXIT_OK)

        ex = existing_data(out, fingerprint(s))
        if ex and ex["csv_exists"] and ex["matches"] and not ex["problems"] and not a.force:
            print(f"[bls] reusing {ex['path']} (retrieved {cpol.local_stamp(ex['retrieved'])}); pass --force to download again",
                  file=sys.stderr)
            emit({"status": "reused_existing", "route": "reuse_existing", "data_dir": data_dir(out),
                  "csv": ex["path"], "retrieved": ex["retrieved"], "latest_period": ex["latest_period"]})
        n0 = len(SOURCES)
        c = BlsClient(use_key=not no_key)
        print(f"[bls] API {c.version} ({'key: ' + c.key_source if c.key else 'no key'}); "
              f"{len(s['series'])} series, {s['start_year']}-{s['end_year']}", file=sys.stderr)
        by_series, messages, raw = c.fetch([x["id"] for x in s["series"]], s["start_year"], s["end_year"],
                                           catalog=s["catalog"], annualaverage=s["annual_average"],
                                           calculations=s["calculations"], aspects=s["aspects"])
        route = "api" if c.version == "v2" else "api_keyless"
        req = package(by_series, messages, raw, s, out, c.version, "BLS Public Data API download", route,
                      describe_titles=not a.no_describe, n0=n0)
        res = {"status": "downloaded", "route": route, "api_version": c.version, "queries": c.queries_sent,
               "rows": req["rows"], "latest_period": req["latest_period"], "data_dir": data_dir(out),
               "csv": os.path.join(data_dir(out), "bls_series.csv"),
               "series_info": os.path.join(data_dir(out), "series.csv"),
               "messages": messages[:10], "problems": req["problems"],
               "citation": cpol.citation(POLICY, retrieved=req["retrieved"],
                                         dataset=", ".join(x["id"] for x in s["series"][:8])
                                         + (f" and {len(s['series']) - 8} more series" if len(s["series"]) > 8 else "")),
               "read_next": os.path.join(PACK, "source.md")}
        if req["problems"]:
            print("ERROR: some series returned no data; check the IDs with --describe and fix spec.json:\n  "
                  + "\n  ".join(req["problems"]), file=sys.stderr)
            emit(res, EXIT_SPEC)
        emit(res)
    except BlsError as e:
        if e.kind in ("auth", "quota", "network"):
            report_blocked(e, env, s, out, a.spec)
        if e.kind == "unavailable":
            emit({"status": "error", "kind": "unavailable", "message": str(e),
                  "action": "A temporary BLS outage, not a problem with the spec: run the same command once more in "
                            "about 30 minutes (do not loop; failed attempts may count against the keyless quota). "
                            "Meanwhile FRED carries the headline BLS series (UNRATE, PAYEMS, CPIAUCSL): use it if the "
                            "question allows, and say which source the figures came from."}, EXIT_SPEC)
        emit({"status": "error", "kind": e.kind, "message": str(e)}, EXIT_SPEC)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        print("ERROR: " + redact(traceback.format_exc()), file=sys.stderr)
        emit({"status": "error", "kind": "unexpected", "message": redact(traceback.format_exc().splitlines()[-1])},
             EXIT_UNEXPECTED)


if __name__ == "__main__":
    main()

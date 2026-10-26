#!/usr/bin/env python
"""Define, submit, download and package an IPUMS microdata extract (deep pack of the data-analyst skill).

Run through the dispatcher: python "<skill>/scripts/fetch.py" --source ipums <arguments below>
  spec.json --out OUTDIR --check          # pre-flight: which route gets the data here? (no extract)
  --list-samples usa [--match 2023]       # find sample IDs
  spec.json --out OUTDIR                  # submit + wait + download + package
  spec.json --out OUTDIR --dry-run        # print the API request body only
  --resume usa:123 --out OUTDIR           # download a previously submitted extract
  spec.json --out OUTDIR --manual-bundle  # request body, download scripts + website and curl steps only
  spec.json --out OUTDIR --ingest [FILES] # package a data file + DDI the user downloaded by hand
                                          # (no FILES: the DATA_ANALYST_UPLOADS folders)
  spec.json --out OUTDIR --reproduce-bundle DIR   # reproduce/ folder for a replication package

Routes (--check prints one as "route", first match wins; shared names from core/routes.py):
  reuse_existing   OUTDIR/data/ipums/ already holds this exact extract: nothing to download
  api              a key was found and IPUMS accepts it (with "resume" set when the extract was
                   submitted earlier and is still at IPUMS: next_command collects it)
  run_setup        no key, and the user has a terminal (local or ssh): run the skill's setup script there
  cloud_secret     no key in a cloud session: add IPUMS_API_KEY to the environment's secrets (bundle written too)
  cloud_allowlist  a cloud session with a key cannot reach api.ipums.org: allow it, or use the bundle
  manual_download  a sandbox with no key or no internet: the user downloads at home (website or script), uploads
  fix_spec         spec.json is incomplete or a case selection is malformed
  report_error     a key exists but IPUMS rejects it (401/403), or IPUMS cannot be reached from the user's machine
The normal run applies reuse_existing and resume automatically, so rerunning never downloads twice
(--force resubmits anyway).

Exit codes: 0 ok; 2 IPUMS or spec error; 3 unexpected error; 4 cannot download (key missing or
rejected, or IPUMS unreachable): the printed JSON has the route. In a sandbox or cloud session
OUTDIR/manual_download/ipums/ holds the request body, INSTRUCTIONS.md (website steps that need no API
key, and API steps) and download_ipums.sh / download_ipums.ps1 (scripts the user runs on their own
computer: they ask for the key, submit, wait and download the two files).

spec.json (friendly format, converted to the IPUMS API v2 body):
{
  "collection": "usa",                         # usa | cps | ipumsi | atus | nhis | meps ...
  "description": "Prime-age employment by nativity",
  "samples": ["us2013a", "us2018a", "us2023a"],
  "variables": ["AGE", "SEX", "CITIZEN", "EDUCD", "EMPSTAT", "INCWAGE", "PERWT"],   # ACS has no NATIVITY
  "case_selections": {"AGE": {"min": 25, "max": 54}, "SEX": ["1"]},   # list of codes, or a numeric range
  "case_select_who": "individuals",            # or "households" (keep whole households)
  "attached_characteristics": {"EDUCD": ["spouse"]},
  "data_quality_flags": ["INCWAGE"],           # or true for all variables
  "adjust_monetary_values": ["INCWAGE"]        # CPI-adjust to a common year (usa/cps/ipumsi)
}

Output (OUTDIR/data/ipums/):
  ipums_extract.csv     one row per person, uppercase IPUMS mnemonics, raw IPUMS codes  (never shared)
  codebook.csv          variable, variable_label, value, value_label (from the DDI)
  variables.csv         variable, label, type, start/width, decimals
  <name>.xml            the original DDI codebook
  extract_spec.json     the friendly spec + exact API body + extract number (to reproduce)
  request.json          route, retrieval time, credential-free request URLs, files (core provenance)
  README.md             what is in the extract, weights, missing codes, citation, terms
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(SKILL, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import keys, manual, package as core_package, routes  # noqa: E402
from core.env import ENVIRONMENTS, detect_environment, setup_command, upload_dirs  # noqa: E402
from core.http import SOURCES, reachable  # noqa: E402
from ipums_api import KEY_ENV, KEY_HOME, IpumsClient, IpumsError, find_api_key  # noqa: E402

SOURCE = "ipums"
redact = keys.redact
EXIT_OK, EXIT_SPEC, EXIT_UNEXPECTED, EXIT_BLOCKED = (routes.EXIT_OK, routes.EXIT_SPEC, routes.EXIT_UNEXPECTED,
                                                     routes.EXIT_BLOCKED)

# Rough persons-per-sample, used only for a size warning before submitting.
APPROX_ROWS = {
    "usa_acs1": 3_300_000, "usa_acs5": 16_000_000, "usa_census": 3_000_000,
    "cps_asec": 160_000, "cps_monthly": 120_000,
    "nhis": 45_000, "nhis_pre2019": 100_000, "meps": 25_000,   # NHIS 2024: 41k sample adults and children; MEPS 2022: 22k
}
SELECTABLE = ("usa", "cps", "ipumsi")      # collections with case selection and monetary adjustment
CITATIONS = {
    "usa": ("Steven Ruggles, Sarah Flood, Matthew Sobek, et al. IPUMS USA [dataset]. "
            "Minneapolis, MN: IPUMS. Use the exact version and DOI cited at https://usa.ipums.org/usa/cite.shtml "
            "(the unversioned DOI does not resolve)"),
    "cps": ("Sarah Flood, Miriam King, Renae Rodgers, Steven Ruggles, J. Robert Warren, et al. "
            "IPUMS CPS [dataset]. Minneapolis, MN: IPUMS. Use the exact version and DOI cited at "
            "https://cps.ipums.org/cps/citation.shtml (the unversioned DOI does not resolve)"),
    "nhis": ("IPUMS NHIS [dataset]. Minneapolis, MN: IPUMS. Use the exact version and DOI cited at "
             "https://nhis.ipums.org/nhis/citation.shtml, and acknowledge NCHS as the source of the NHIS"),
    "meps": ("IPUMS MEPS [dataset]. Minneapolis, MN: IPUMS. Use the exact version and DOI cited at "
             "https://meps.ipums.org/meps/citation.shtml, and acknowledge AHRQ as the source of the MEPS"),
}
SOURCE_LINE = {"usa": "IPUMS-USA (American Community Survey / decennial census)",
               "cps": "IPUMS-CPS (Current Population Survey)",
               "nhis": "IPUMS NHIS (National Health Interview Survey, NCHS)",
               "meps": "IPUMS MEPS (Medical Expenditure Panel Survey, AHRQ)"}
WEIGHT_HINTS = {
    "PERWT": "person weight (USA): use for person-level estimates",
    "HHWT": "household weight (USA): use for household-level estimates (one row per household, PERNUM==1)",
    "ASECWT": "ASEC person weight (CPS March supplement): use for annual income/insurance/poverty",
    "WTFINL": "final basic monthly weight (CPS): use for monthly labor-force estimates",
    "EARNWT": "earnings weight (CPS outgoing rotation groups): use for EARNWEEK2/HOURWAGE2 and UNION",
    "ASECWTH": "ASEC household weight (CPS)",
    "SAMPWEIGHT": "sample adult / sample child weight (NHIS): the only person weight from 2019 on",
    "PERWEIGHT": "person weight (MEPS annual weight; NHIS person file before 2019)",
    "PARTWEIGHT": "2020 NHIS sample adults: use instead of SAMPWEIGHT when pooling 2019-2020",
    "SAQWEIGHT": "MEPS self-administered questionnaire weight: use for SAQ items (adults)",
    "DIABWEIGHT": "MEPS diabetes care survey weight",
}
TERMS_LINE = ("The extract is for the user's own use under the IPUMS terms of use: it must not be shared, "
              "attached to a report or reposted. Share extract_spec.json and the reproduce scripts instead; "
              "aggregated estimates (exhibit tables) may be published. IPUMS deletes extract files about "
              "72 hours after they are built; the definition can be resubmitted at any time.")


def emit(obj: dict, code: int = 0):
    print(redact(json.dumps(obj, indent=2, default=str)))
    sys.exit(code)


def data_dir(out: str) -> str:
    return core_package.data_dir(out, SOURCE)


def bundle_dir(out: str) -> str:
    return os.path.join(os.path.abspath(out), "manual_download", SOURCE)


# --------------------------------------------------------------------------- spec -> API body
def _codes(sel) -> list[str]:
    if isinstance(sel, dict) and ("min" in sel or "max" in sel):
        lo, hi = int(sel.get("min", 0)), int(sel.get("max"))
        width = int(sel.get("width", 0))
        return [str(v).zfill(width) for v in range(lo, hi + 1)]
    if isinstance(sel, (list, tuple)):
        return [str(v) for v in sel]
    raise ValueError(f"Bad case selection {sel!r}: use a list of codes or {{'min':..,'max':..}}")


def spec_problems(spec: dict) -> list[str]:
    """What is wrong with spec.json before anything is sent (route fix_spec)."""
    bad = []
    if not isinstance(spec, dict):
        return ["spec.json must be a JSON object"]
    if not isinstance(spec.get("collection"), str) or not spec.get("collection"):
        bad.append('"collection" is required (usa, cps, ipumsi, atus, nhis, meps, ...)')
    for k in ("samples", "variables"):
        if not isinstance(spec.get(k), list) or not spec.get(k):
            bad.append(f'"{k}" must be a non-empty list')
    for name, sel in (spec.get("case_selections") or {}).items():
        if isinstance(sel, dict) and "detailed" in sel:
            continue
        try:
            _codes(sel)
        except (ValueError, TypeError) as e:
            bad.append(f"case_selections.{name}: {e}")
    if spec.get("case_select_who") not in (None, "individuals", "households"):
        bad.append('"case_select_who" must be "individuals" or "households"')
    c = spec.get("collection")
    if isinstance(c, str) and c not in SELECTABLE:       # IPUMS would refuse the extract with HTTP 400
        for k in ("case_selections", "adjust_monetary_values"):
            if spec.get(k):
                bad.append(f'"{k}" is available only for {", ".join(SELECTABLE)}: remove it and filter the '
                           f"{c} extract after download")
    return bad


def build_body(spec: dict) -> dict:
    variables = {}
    names = [v.upper() for v in spec["variables"]]
    cs = {k.upper(): v for k, v in (spec.get("case_selections") or {}).items()}
    ac = {k.upper(): v for k, v in (spec.get("attached_characteristics") or {}).items()}
    dq = spec.get("data_quality_flags") or []
    adj = {v.upper() for v in (spec.get("adjust_monetary_values") or [])}
    for extra in list(cs) + list(ac):
        if extra not in names:
            names.append(extra)          # a selected/attached variable must be in the extract
    for name in names:
        v: dict = {}
        if name in cs:
            sel = cs[name]
            if isinstance(sel, dict) and "detailed" in sel:
                v["caseSelections"] = {"detailed": [str(x) for x in sel["detailed"]]}
            else:
                v["caseSelections"] = {"general": _codes(sel)}
        if name in ac:
            v["attachedCharacteristics"] = ac[name]
        if dq is True or (isinstance(dq, list) and name in [d.upper() for d in dq]):
            v["dataQualityFlags"] = True
        if name in adj:
            v["adjustMonetaryValues"] = True
        variables[name] = v
    body = {
        "description": spec.get("description", "extract via data-analyst")[:250],
        "dataStructure": spec.get("data_structure") or {"rectangular": {"on": "P"}},
        "dataFormat": spec.get("data_format", "csv"),
        "samples": {s: {} for s in spec["samples"]},
        "variables": variables,
    }
    if spec.get("case_select_who"):
        body["caseSelectWho"] = spec["case_select_who"]
    return body


def approx_rows(samples: list[str]) -> int:
    n = 0
    for s in samples:
        if s.startswith("us"):
            n += APPROX_ROWS["usa_acs5"] if s.endswith("c") else APPROX_ROWS["usa_acs1"]
        elif s.startswith("cps") and s.endswith("_03s"):
            n += APPROX_ROWS["cps_asec"]
        elif s.startswith("cps"):
            n += APPROX_ROWS["cps_monthly"]
        elif re.fullmatch(r"ih\d{4}", s):
            n += APPROX_ROWS["nhis" if int(s[2:]) >= 2019 else "nhis_pre2019"]
        elif re.fullmatch(r"mp\d{4}", s):
            n += APPROX_ROWS["meps"]
        else:
            n += 500_000
    return n


# --------------------------------------------------------------------------- DDI parsing
def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _strip(c.tag) == name:
            return c
    return None


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


# "99999999 = N.I.U. (Not in Universe)." in a continuous variable's coding notes (INCBUS, INCFARM ...): the DDI has
# no category rows for these, so without this the code survives clean_missing and adds $100M to a sum.
SPECIAL_CODE = re.compile(r"(?<![\d.])(\d{2,}(?:\.\d+)?)\s*=\s*((?:N\.?\s?I\.?\s?U\.?|N/A|Not in universe|Missing|Not applicable"
                          r"|Unknown)[^.;=\d]{0,30})", re.I)


def parse_ddi(path: str) -> tuple[list[dict], list[dict], str]:
    root = ET.parse(path).getroot()
    var_rows, code_rows, citation, series, doi = [], [], "", "", ""
    for el in root.iter():
        t = _strip(el.tag)
        if t == "biblCit" and not citation:
            citation = _text(el)
        if t == "serName" and not series:
            series = _text(el)
        if t == "serInfo" and not doi:
            doi = _text(el).replace("DOI:", "").strip()
        if t != "var":
            continue
        name = el.get("name") or el.get("ID")
        label = _text(_child(el, "labl"))
        loc = _child(el, "location")
        fmt = _child(el, "varFormat")
        var_rows.append({
            "variable": name, "label": label,
            "type": (fmt.get("type") if fmt is not None else ""),
            "start": loc.get("StartPos") if loc is not None else "",
            "width": loc.get("width") if loc is not None else "",
            "decimals": el.get("dcml", ""),
        })
        n0 = len(code_rows)
        for cat in el:
            if _strip(cat.tag) != "catgry":
                continue
            val = _text(_child(cat, "catValu"))
            if val.isdigit():
                val = str(int(val))          # '073' -> '73' so it matches the numeric CSV value
            code_rows.append({"variable": name, "variable_label": label,
                              "value": val,
                              "value_label": _text(_child(cat, "labl"))})
        if len(code_rows) == n0:             # continuous: special codes are only stated in the description
            for val, lab in SPECIAL_CODE.findall(_text(_child(el, "codInstr")) + " " + _text(_child(el, "txt"))):
                code_rows.append({"variable": name, "variable_label": label, "value": str(int(val)) if "." not in val else val,
                                  "value_label": f"{lab.strip()} (stated in the variable description)"})
    if not citation and series and doi:
        citation = (f"{series} [dataset], DOI {doi} (https://doi.org/{doi}). Minneapolis, MN: IPUMS. "
                    f"Full author list: see the collection's citation page.")
    return var_rows, code_rows, citation


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- download + package
def package(client: IpumsClient, collection: str, info: dict, spec: dict, body: dict, out: str):
    d = data_dir(out)
    os.makedirs(d, exist_ok=True)
    links = info["downloadLinks"]

    def fetch(key):
        link = links.get(key)
        if not link:
            return None
        url = link["url"]
        dest = os.path.join(d, url.rsplit("/", 1)[-1])
        print(f"[ipums] downloading {key}: {os.path.basename(dest)}", file=sys.stderr, flush=True)
        return client.download(url, dest, link.get("sha256"))

    data_path = fetch("data")
    ddi_path = fetch("ddiCodebook")
    fetch("codebookPreview")
    return package_files(collection, info["number"], data_path, ddi_path, spec, body, out,
                         client=client, remove_source=True, route="api")


def write_request_json(d: str, spec: dict, body: dict, number, route: str, acquired: str, files: list[dict],
                       problems: list[str] | None = None) -> None:
    """core provenance file (request.json): route, retrieval time, credential-free URLs, files.
    Lets core.package.existing() and the report builder see this folder like any other source's."""
    req = {"source": SOURCE, "spec": spec, "fingerprint": core_package.fingerprint(spec), "route": route,
           "acquired": acquired, "retrieved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "vintage": "", "extract": f"{spec.get('collection', '')}:{number}",
           "requests": [s for s in SOURCES if "/extracts" in s.get("url", "") or "/downloads/" in s.get("url", "")],
           "tables": files, "problems": problems or [],
           "notes": [TERMS_LINE], "policy": {"redistribution": "definition_only", "cache": "run_only"},
           "api_body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2, default=str)


def package_files(collection: str, number, data_path: str, ddi_path: str | None, spec: dict, body: dict,
                  out: str, client: IpumsClient | None = None, remove_source: bool = False,
                  acquired: str = "IPUMS API download", route: str = "api") -> list[str]:
    """Turn a data file (.csv or .csv.gz) and its DDI into the standard data/ipums/ package."""
    d = data_dir(out)
    os.makedirs(d, exist_ok=True)

    # Decompress the data to a plain CSV (what most tools expect) and count rows.
    csv_path = os.path.join(d, "ipums_extract.csv")
    tmp_path = csv_path + ".part"
    nrows = 0
    with open(data_path, "rb") as probe:
        gzipped = probe.read(2) == b"\x1f\x8b"          # by content: a browser may rename or unzip
    opener = gzip.open if gzipped else open
    with opener(data_path, "rt", encoding="utf-8", newline="") as src, \
            open(tmp_path, "w", encoding="utf-8", newline="") as dst:
        header = src.readline()
        columns = [c.strip().upper() for c in next(csv.reader([header]))]
        dst.write(",".join(columns) + "\n")
        for line in src:
            dst.write(line)
            nrows += 1
    os.replace(tmp_path, csv_path)
    if remove_source and os.path.abspath(data_path) != os.path.abspath(csv_path):
        os.remove(data_path)

    if ddi_path and os.path.dirname(os.path.abspath(ddi_path)) != os.path.abspath(d):
        shutil.copy2(ddi_path, os.path.join(d, os.path.basename(ddi_path)))
    if ddi_path:                                  # an earlier extract's DDI no longer describes ipums_extract.csv
        keep = os.path.basename(ddi_path)
        for old in os.listdir(d):
            if old.lower().endswith(".xml") and old != keep:
                os.replace(os.path.join(d, old), os.path.join(d, old + ".superseded"))

    var_rows, code_rows, ddi_cite = parse_ddi(ddi_path) if ddi_path else ([], [], "")
    write_csv(os.path.join(d, "variables.csv"), var_rows,
              ["variable", "label", "type", "start", "width", "decimals"])
    write_csv(os.path.join(d, "codebook.csv"), code_rows,
              ["variable", "variable_label", "value", "value_label"])

    try:
        sample_desc = ({s["name"]: s["description"] for s in client.list_samples(collection)}
                       if client else {})
    except IpumsError:
        sample_desc = {}

    with open(os.path.join(d, "extract_spec.json"), "w", encoding="utf-8") as f:
        json.dump({"collection": collection, "extract_number": number,
                   "downloaded": date.today().isoformat(), "acquired": acquired, "rows": nrows,
                   "spec": spec, "api_body": body}, f, indent=2)

    labels = {r["variable"]: r["label"] for r in var_rows}
    weights = [c for c in columns if c in WEIGHT_HINTS]
    citation = ddi_cite or CITATIONS.get(collection, "Cite IPUMS per https://www.ipums.org/about/citation")
    lines = [
        f"# IPUMS extract {collection.upper()} #{number}",
        "",
        f"- Description: {body.get('description', '')}",
        f"- Downloaded: {date.today().isoformat()} ({acquired})",
        f"- Rows ({'households' if 'householdOnly' in json.dumps(body.get('dataStructure') or {}) else 'persons'}): {nrows:,}",
        f"- File: `ipums_extract.csv` (comma-separated, header = IPUMS mnemonics, raw IPUMS codes)",
        "- Value labels: `codebook.csv`; variable labels/types: `variables.csv`; full DDI: the `.xml`",
        "",
        "## Samples",
        *[f"- `{s}`: {sample_desc.get(s, '')}" for s in body["samples"]],
        "",
        "## Variables",
        *[f"- `{c}`: {labels.get(c, '')}" for c in columns],
        "",
        "## Case selections",
        *([f"- `{k}` in {v['caseSelections']}" for k, v in body["variables"].items()
           if "caseSelections" in v] or ["- none (full sample)"]),
        "",
        "## Weights",
        *([f"- `{w}`: {WEIGHT_HINTS[w]}" for w in weights] or
          ["- No weight variable in extract. Estimates will NOT be population-representative."]),
        "",
        "## Reading the data",
        "```python",
        "import pandas as pd",
        "df = pd.read_csv('ipums_extract.csv')",
        "cb = pd.read_csv('codebook.csv', dtype={'value': str})",
        "```",
        "Watch for IPUMS missing/top codes (e.g. INCWAGE 999999 = N/A, 999998 = missing; "
        "EMPSTAT 0 = N/A). Check `codebook.csv` before computing means.",
        "",
        "## Citation",
        citation,
        "",
        f"Suggested source line: \"Analyst calculations using data from {SOURCE_LINE.get(collection, 'IPUMS')}.\" "
        f"(or \"<Organization>, using data from ...\" when the product names an organization)",
        "",
        "## Use and redistribution",
        "",
        f"Rule: **definition_only**. {TERMS_LINE}",
    ]
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    files = [{"name": "ipums_extract", "file": "ipums_extract.csv", "rows": nrows, "columns": columns,
              "sha256": core_package.sha256(csv_path)},
             {"name": "codebook", "file": "codebook.csv", "rows": len(code_rows)},
             {"name": "variables", "file": "variables.csv", "rows": len(var_rows)}]
    write_request_json(d, spec, body, number, route, acquired, files)

    print(redact(json.dumps({"status": "ok", "source": SOURCE, "route": route,
                             "extract": f"{collection}:{number}", "rows": nrows, "columns": columns,
                             "data_dir": d, "csv": csv_path, "codebook": os.path.join(d, "codebook.csv"),
                             "readme": os.path.join(d, "README.md"), "citation": citation,
                             "terms": TERMS_LINE, "read_next": os.path.join(HERE, "source.md")}, indent=2)))
    return columns


# --------------------------------------------------------------------------- cannot download: recovery
REASON_TEXT = {
    "key_missing": ("no IPUMS API key was found (not in the IPUMS_API_KEY environment variable, a key file, "
                    "keys.json, Claude Code's settings.json, or ~/.ipums/api_key)"),
    "auth": ("IPUMS rejected the API key (HTTP 401 or 403): the key is wrong, or the account is not "
             "registered for this collection"),
    "network": "IPUMS (api.ipums.org) could not be reached from here",
}
BLOCKING_KINDS = tuple(REASON_TEXT)

MANUAL_HEAD = """# Download the IPUMS extract by hand

The analysis environment cannot download from IPUMS (@REASON@). The extract definition is
ready. Get the two files with one of the options below, on your own computer, then upload them.

@OPTIONS@"""

WEB_TEMPLATE = """## Option @LETTER@: the IPUMS website (no API key needed)

You need only a free IPUMS account registered for @COLLECTION_UPPER@ (register and accept the terms of
use at @SITE@). Build the extract below in the website's extract builder. Select exactly these items:
the analysis relies on them.

1. Sign in at @SITE@ and open **Select Data** (@BROWSE@).
2. **Select Samples.** Clear the samples that are checked by default, then check only these:
@SAMPLES@
   Click **Submit sample selections**.
3. **Add variables to the cart.** Search for each name in the variable search box and click the
   plus sign to add it:
@VARIABLES@
   IPUMS adds technical variables (such as YEAR, SERIAL, PERNUM and the weights) by itself. Keep them.
4. Click **View Cart**, then **Create Data Extract**.
@CASES@@OPTIONS_EXTRA@5. **Data format:** click *Change* and choose **Comma delimited (.csv)**. Keep the structure
   **Rectangular (person)**.
6. Add the description "@DESCRIPTION@" and click **Submit Extract**. IPUMS emails you when the
   extract is ready (minutes for CPS, up to about 30 minutes for multi-year ACS).
7. On **My Data** (@MYDATA@), download the **data file** (`.csv.gz`) and the **DDI codebook**: right-click
   the *DDI* link and choose *Save link as*, so it is saved as an `.xml` file.

"""

API_TEMPLATE = """## Option @LETTER@: the IPUMS API, with one script (needs an API key)

You need an IPUMS API key: https://account.ipums.org/api_keys (the account must be registered for
the @COLLECTION_UPPER@ collection). The script asks for it with hidden input on your own computer.
Do not paste it into the chat.

The extract definition is built into the script (`extract_spec.json` holds the same definition, for
reference and for the curl steps below). The script submits the extract, waits until IPUMS has built
it (minutes for CPS, up to about 30 minutes for multi-year ACS), and saves the data file (`.csv.gz`)
and the codebook (`.xml`) next to itself. If it is interrupted, run it again: it continues with the
same extract. To submit a new extract instead (for example after IPUMS deleted the files, about 72
hours after they were built), add `--new` (bash) or `-New` (PowerShell).
@NUMBER_NOTE@
@RUN@
When it finishes, it prints the full paths of the two files.

### If the script does not work: the same steps with curl

`extract_spec.json` in this folder is the request body (the extract definition). Run the commands
from the folder that holds it, or give the full path after `@`. The first line asks for your key with
hidden input; it stays in this terminal only.

#### macOS, Linux, Git Bash, WSL

```bash
read -rsp 'IPUMS API key (hidden): ' IPUMS_API_KEY && echo && export IPUMS_API_KEY

# 1. Submit the extract. The reply has "number" (the extract number).
curl -sS -X POST -H "Authorization: $IPUMS_API_KEY" -H "Content-Type: application/json" -d @extract_spec.json "https://api.ipums.org/extracts?collection=@COLLECTION@&version=2"

# 2. Check status. Repeat until the reply shows "status": "completed" (a large extract can take up to 30 minutes).
curl -sS -H "Authorization: $IPUMS_API_KEY" "https://api.ipums.org/extracts/<NUMBER>?collection=@COLLECTION@&version=2"

# 3. Download the data file and the codebook. Use the URLs in the completed reply:
#    downloadLinks.data.url and downloadLinks.ddiCodebook.url
curl -sS -L -H "Authorization: $IPUMS_API_KEY" -o @COLLECTION@_data.csv.gz "<DATA_URL>"
curl -sS -L -H "Authorization: $IPUMS_API_KEY" -o @COLLECTION@_ddi.xml "<DDI_URL>"
```

#### Windows PowerShell

Use `curl.exe`. Plain `curl` in PowerShell is a different command.

```powershell
$env:IPUMS_API_KEY = [Net.NetworkCredential]::new('', (Read-Host 'IPUMS API key (hidden)' -AsSecureString)).Password

# 1. Submit the extract. The reply has "number" (the extract number).
curl.exe -sS -X POST -H "Authorization: $env:IPUMS_API_KEY" -H "Content-Type: application/json" -d "@extract_spec.json" "https://api.ipums.org/extracts?collection=@COLLECTION@&version=2"

# 2. Check status. Repeat until the reply shows "status": "completed".
curl.exe -sS -H "Authorization: $env:IPUMS_API_KEY" "https://api.ipums.org/extracts/<NUMBER>?collection=@COLLECTION@&version=2"

# 3. Download the data file and the codebook (downloadLinks.data.url and downloadLinks.ddiCodebook.url).
curl.exe -sS -L -H "Authorization: $env:IPUMS_API_KEY" -o @COLLECTION@_data.csv.gz "<DATA_URL>"
curl.exe -sS -L -H "Authorization: $env:IPUMS_API_KEY" -o @COLLECTION@_ddi.xml "<DDI_URL>"
```

A reply with `"status": "failed"` means the extract definition has a problem: send that reply to the
analyst and do not resubmit by hand. Close the terminal afterwards (or run `unset IPUMS_API_KEY` /
`Remove-Item Env:IPUMS_API_KEY`). The script needs no clean-up: it keeps the key only while it runs.

"""

def run_block(here: bool = False) -> str:
    """The one command per system that runs the download script: from the Downloads folder (a hand-download
    bundle), or from the folder that holds the script (here=True: reproduce/DOWNLOAD.md)."""
    if here:
        c = {"bash": f"bash {DOWNLOAD_SCRIPTS['bash']}",
             "powershell": f"powershell -NoProfile -ExecutionPolicy Bypass -File .\\{DOWNLOAD_SCRIPTS['powershell']}"}
        lead = "Open a terminal in this folder and run the command for your system:"
        tail = ("Downloaded only the script (for example from the report page)? Run it where you saved it, e.g. "
                f"`{manual.run_lines('download_ipums')['bash']}`.")
    else:
        c = manual.run_lines("download_ipums")
        lead = ("Save `download_ipums.sh` (macOS, Linux) or `download_ipums.ps1` (Windows) in your Downloads "
                "folder, then copy and paste the command for your system into a terminal:")
        tail = "If you saved the script in another folder, put that folder in the command instead of Downloads."
    return (f"{lead}\n\n**macOS, Linux, Git Bash, WSL** (needs only curl):\n\n```bash\n{c['bash']}\n```\n\n"
            f"**Windows PowerShell:**\n\n```powershell\n{c['powershell']}\n```\n\n{tail} On Windows you can also "
            "right-click the `.ps1` file and choose *Run with PowerShell*. The script asks for your API key with "
            "hidden input (or uses `IPUMS_API_KEY` when it is set).\n")


UPLOAD_TEMPLATE = """## Upload

Upload these two files to the chat (do not unzip or edit them):
1. the data file (the `.csv.gz` that IPUMS named, for example `@COLLECTION@_00012.csv.gz`);
2. the codebook (the `.xml` DDI file, for example `@COLLECTION@_00012.xml`).

The assistant then packages them and continues the analysis.

Tips:
- The files are for your own use under the IPUMS terms of use: do not share or repost them.
- An error message about a variable or sample means the extract definition has a problem. Send that
  message to the analyst; do not change the selection by hand.
- IPUMS removes extract files after about 72 hours. Download soon after the extract is ready.
"""

# Website of each collection: (site, extract-builder path prefix). Unknown collections fall back to ipums.org.
WEB_SITES = {
    "usa": ("https://usa.ipums.org", "usa-action"),
    "cps": ("https://cps.ipums.org", "cps-action"),
    "ipumsi": ("https://international.ipums.org", "international-action"),
    "atus": ("https://www.atusdata.org", "atus-action"),
    "nhis": ("https://nhis.ipums.org", "nhis-action"),
    "meps": ("https://meps.ipums.org", "meps-action"),
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def sample_label(sample: str) -> str:
    """Best-effort label for a sample ID as the website lists it ("" when unknown; the ID is always shown)."""
    m = re.fullmatch(r"us(\d{4})([a-z])", sample)
    if m:
        y, kind = int(m.group(1)), m.group(2)
        return {"a": f"{y} ACS, 1-year", "c": f"{y - 4}-{y} ACS, 5-year"}.get(kind, "")
    m = re.fullmatch(r"cps(\d{4})_(\d{2})([sb])", sample)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if mo == 3 and m.group(3) == "s":
            return f"ASEC {y}"
        return f"{MONTHS[mo - 1]} {y} basic monthly" if 1 <= mo <= 12 else ""
    m = re.fullmatch(r"(ih|mp)(\d{4})", sample)
    if m:
        return f"{m.group(2)} {'NHIS' if m.group(1) == 'ih' else 'MEPS'}"
    return ""


def _code_list(codes: list[str]) -> str:
    nums = [int(c) for c in codes if c.lstrip("-").isdigit()]
    if len(nums) == len(codes) and len(nums) > 3 and nums == list(range(nums[0], nums[-1] + 1)):
        return f"codes {codes[0]} through {codes[-1]}"
    return ("code " if len(codes) == 1 else "codes ") + ", ".join(codes)


def web_section(collection: str, body: dict) -> str:
    """Step-by-step website instructions that rebuild the API body in the IPUMS extract builder."""
    site, action = WEB_SITES.get(collection, ("https://www.ipums.org", ""))
    browse = f"{site}/{action}/variables/group" if action else site
    mydata = f"{site}/{action}/data_requests/download" if action else site
    samples = "\n".join(f"   - `{s}`" + (f" ({sample_label(s)})" if sample_label(s) else "")
                        for s in body["samples"])
    variables = "\n".join(f"   - `{v}`" for v in body["variables"])
    cases, extra = [], []
    for name, v in body["variables"].items():
        cs = v.get("caseSelections")
        if cs:
            level = "detailed" if "detailed" in cs else "general"
            cases.append(f"   - `{name}`: {_code_list(cs[level])} ({level} codes)")
        if v.get("attachedCharacteristics"):
            extra.append(f"   - `{name}`: *Attach characteristics* of {', '.join(v['attachedCharacteristics'])}")
        if v.get("dataQualityFlags"):
            extra.append(f"   - `{name}`: include its *Data quality flag*")
        if v.get("adjustMonetaryValues"):
            extra.append(f"   - `{name}`: *Adjust monetary values* for inflation")
    case_text = ""
    if cases:
        keep = ("all members of households with at least one matching person"
                if body.get("caseSelectWho") == "households" else "only the persons who match")
        case_text = ("   **Select cases:** click *Select cases*, check these variables, pick the codes, and keep "
                     f"{keep}:\n" + "\n".join(cases) + "\n")
    extra_text = ("   **Options** (on the same page):\n" + "\n".join(extra) + "\n") if extra else ""
    return (WEB_TEMPLATE.replace("@SITE@", site).replace("@BROWSE@", browse).replace("@MYDATA@", mydata)
            .replace("@SAMPLES@", samples).replace("@VARIABLES@", variables)
            .replace("@CASES@", case_text).replace("@OPTIONS_EXTRA@", extra_text)
            .replace("@DESCRIPTION@", body.get("description", "")))


def spec_fingerprint(body: dict) -> str:
    """Hash of the API request body: two specs that would produce the same extract hash the same."""
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def existing_data(out: str, body: dict | None) -> dict | None:
    """What OUTDIR/data/ipums/ already holds for this spec, or None when there is nothing."""
    d = data_dir(out)
    spec_file = os.path.join(d, "extract_spec.json")
    csv_path = os.path.join(d, "ipums_extract.csv")
    if not os.path.exists(spec_file):
        return None
    try:
        with open(spec_file, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    saved_body = saved.get("api_body") or {}
    return {"path": csv_path, "csv_exists": os.path.exists(csv_path),
            "extract_number": saved.get("extract_number"), "status": saved.get("status", "downloaded"),
            "downloaded": saved.get("downloaded"), "rows": saved.get("rows"),
            "spec_matches": bool(body) and bool(saved_body) and spec_fingerprint(saved_body) == spec_fingerprint(body)}


def _commands(spec_path: str, out: str) -> tuple[str, str]:
    base = f'--source {SOURCE} "{spec_path}" --out "{out}"'
    return routes.cmd(base), routes.cmd(base, "--ingest", "<the uploaded files or their folder>")


def _bundle_fields(r: dict, bundle: str) -> None:
    r.update(manual_bundle=bundle, instructions=os.path.join(bundle, "INSTRUCTIONS.md"),
             request_body=os.path.join(bundle, "extract_spec.json"), download_scripts=_script_paths(bundle),
             user_commands=manual.run_lines("download_ipums"))


# What the model does with the hand-download bundle (every manual route says the same).
SEND_BUNDLE = ("Send INSTRUCTIONS.md, both download scripts (download_scripts: download_ipums.sh for macOS/Linux, "
               "download_ipums.ps1 for Windows) and extract_spec.json (request_body, the extract definition the "
               "scripts have built in) with present_files / SendUserFile when available, and print their full paths. "
               "Give the user the one command for their system (user_commands; both when unsure): the script asks "
               "for the IPUMS key with hidden input, submits, waits and downloads the .csv.gz and .xml. Never ask for "
               "the key in the chat. Wait for the upload, then run next_command.")


def route_check(spec: dict, body: dict, spec_path: str, out: str, env: str, force: bool = False) -> dict:
    """Decide the cheapest way to get this extract here, without submitting anything.

    Returns the JSON printed by --check: the facts (environment, key source, reachability, what is on disk)
    plus "route", "message" (say this to the user), "action" (what the model does next) and, when there is
    one, "next_command".
    """
    out = os.path.abspath(out)
    collection = spec["collection"]
    run_cmd, ingest_cmd = _commands(spec_path, out)
    key, source = find_api_key()
    r = {"status": "route_check", "source": SOURCE, "environment": env, "os": sys.platform,
         "restricted": keys.restricted(), "key_env": KEY_ENV, "key_source": source, "key_required": "required",
         "ipums_reachable": None, "auth_ok": None, "auth_detail": "", "existing_data": None,
         "pending_extract": None, "route": None, "message": "", "action": "", "next_command": ""}
    ex = existing_data(out, body)
    r["existing_data"] = ex
    pending = None
    if ex and ex.get("spec_matches") and ex.get("extract_number") and not ex["csv_exists"]:
        pending = f"{collection}:{ex['extract_number']}"
        r["pending_extract"] = pending

    def done(route, message, action, next_command=""):
        r.update(route=route, message=message, action=action, next_command=next_command)
        return r

    if ex and ex["csv_exists"] and ex["spec_matches"] and not force:
        return done("reuse_existing",
                    f"The data for this extract are already in {ex['path']} (IPUMS extract "
                    f"{collection}#{ex['extract_number']}, downloaded {ex.get('downloaded')}).",
                    "Skip the download and continue with the analysis. Pass --force to resubmit.")

    if key:
        p = IpumsClient(api_key=key).ping(collection)
        r.update(ipums_reachable=p["reachable"], auth_ok=p["auth_ok"], auth_detail=p["detail"])
        if p["auth_ok"]:
            if pending:
                r["resume"] = pending
                return done("api", f"Extract {pending} was submitted earlier and can be collected now.",
                            "Run next_command; it waits for IPUMS and packages the files.",
                            routes.cmd(f'--source {SOURCE} --resume {pending} --out "{out}"'))
            return done("api", f"An IPUMS API key was found ({source}) and IPUMS accepts it.",
                        "Run next_command (in the background for ACS extracts; they can take 5-30 minutes).",
                        run_cmd)
        if p["kind"] == "auth":
            return done("report_error",
                        f"IPUMS rejected the API key found in {source}: {p['detail']} The key may be wrong, or "
                        f"the account may not be registered for the {collection.upper()} collection "
                        f"(https://account.ipums.org/api_keys).",
                        "Tell the user; do not point at the setup script (a key exists). Stop until it is fixed.")
        if env in ("local", "ssh") and not keys.restricted():
            return done("report_error",
                        f"A key was found ({source}) but IPUMS could not be reached: {p['detail']} Check the "
                        "internet connection, VPN and proxy settings (HTTPS_PROXY), then run the request again.",
                        "Tell the user and stop. Do not substitute data from memory.")
        # a container with a key but no route to IPUMS: allow the host, or the user downloads at home
        bundle = write_manual_bundle(collection, body, out, "network", ex["extract_number"] if pending else None)
        _bundle_fields(r, bundle)
        if env == "cloud":
            return done("cloud_allowlist",
                        "This cloud session cannot reach IPUMS (api.ipums.org). Allow api.ipums.org in the "
                        "environment's network settings and start the session again, or download by hand on your "
                        f"own computer with {DOWNLOAD_SCRIPTS['bash']} / {DOWNLOAD_SCRIPTS['powershell']} or the "
                        f"website steps in {bundle}.",
                        "Offer both options. For the manual one: " + SEND_BUNDLE, ingest_cmd)
        return done("manual_download",
                    f"IPUMS cannot be reached from this {env} environment. On your own computer, save "
                    f"{DOWNLOAD_SCRIPTS['bash']} (macOS, Linux) or {DOWNLOAD_SCRIPTS['powershell']} (Windows) from "
                    f"{bundle} and run one command (`{manual.run_lines('download_ipums')['bash']}` or "
                    f"`{manual.run_lines('download_ipums')['powershell']}`): it asks for your key with hidden input, "
                    "submits the extract, waits and downloads the .csv.gz and .xml files. Then upload those two files.",
                    SEND_BUNDLE, ingest_cmd)

    # no key anywhere: the bundle needs no network; the website steps in it need only an IPUMS account
    bundle = write_manual_bundle(collection, body, out, "key_missing", ex["extract_number"] if pending else None)
    instructions = os.path.join(bundle, "INSTRUCTIONS.md")
    _bundle_fields(r, bundle)
    r["ingest_command"] = ingest_cmd
    if env in ("local", "ssh"):
        path, setup = setup_command(SOURCE)
        r.update(setup_script=path, run_command=setup, settings_file=keys.config_path())
        return done("run_setup",
                    "No IPUMS API key is set up yet. The setup script asks for the key with hidden input, checks "
                    "it with IPUMS and stores it in the skill's keys.json (~/.config/data-analyst). Get a key at "
                    "https://account.ipums.org/api_keys (register for the collection you need). Without a key, "
                    "the same extract can be built on the IPUMS website with only a free account: the steps are "
                    f"in {instructions}.",
                    "Offer both. Key: give the user run_command to run in their own terminal (do not run it for "
                    "them: it needs their key typed in); the key file is read directly, so when the user says it "
                    "is done, run --check again. Hand download (website, or a key the user would rather not store): "
                    "point to INSTRUCTIONS.md and the download scripts, then run ingest_command on the downloaded "
                    "files. Never ask for the key in the chat.",
                    setup)
    if env == "cloud":
        return done("cloud_secret",
                    "This cloud session has no IPUMS API key. Either add IPUMS_API_KEY to the cloud environment's "
                    "secrets (environment settings) and start the session again, or download the extract by hand "
                    "on the IPUMS website (free account, no key) or with a key and the download script "
                    f"({DOWNLOAD_SCRIPTS['bash']} or {DOWNLOAD_SCRIPTS['powershell']}, run on your own computer): "
                    f"the steps are in {instructions}.",
                    "Explain both options. If the user chooses the secret, stop and let them restart. If they "
                    "download by hand: " + SEND_BUNDLE,
                    ingest_cmd)
    return done("manual_download",
                f"This environment cannot download from IPUMS ({REASON_TEXT['key_missing']}). Build the extract on "
                "your own computer, either on the IPUMS website with a free account (no API key needed) or with "
                f"a key and one command that runs a script ({DOWNLOAD_SCRIPTS['bash']} for macOS and Linux, "
                f"{DOWNLOAD_SCRIPTS['powershell']} for Windows; it asks for the key with hidden input, submits, waits "
                f"and downloads); the steps for both are in {instructions}. Then upload the .csv.gz data file and "
                "the .xml codebook.",
                SEND_BUNDLE + " Without a key, the website steps in INSTRUCTIONS.md work too.",
                ingest_cmd)


DOWNLOAD_SCRIPTS = {"bash": "download_ipums.sh", "powershell": "download_ipums.ps1"}


def write_download_scripts(d: str, collection: str, body: dict, number=None) -> dict:
    """Write download_ipums.sh and download_ipums.ps1 into d, with the extract definition built in.

    Each asks for the key on the user's computer, submits (or reuses `number`), waits and downloads
    the data file and DDI. Returns {"bash": path, "powershell": path}, or {} when the templates in
    assets/ are missing (the bundle still has the website and curl steps).
    """
    spec = json.dumps(body, indent=2)          # ASCII only: safe in a heredoc and in PowerShell 5.1
    # names the script's saved-number file, so a file left by another extract in the same folder is ignored
    spec_id = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
    texts = {}
    try:
        for name in DOWNLOAD_SCRIPTS.values():
            with open(os.path.join(HERE, "assets", name), encoding="utf-8") as f:
                texts[name] = f.read()
    except OSError as e:
        print(f"[ipums] warning: download scripts not written ({e})", file=sys.stderr)
        return {}
    paths = {}
    for kind, name in DOWNLOAD_SCRIPTS.items():
        text = (texts[name].replace("@COLLECTION_UPPER@", collection.upper())
                .replace("@COLLECTION@", collection)
                .replace("@NUMBER@", str(number or ""))
                .replace("@SPEC_ID@", spec_id)
                .replace("@SPEC_JSON@", spec))       # last, so the spec text is never rewritten
        path = os.path.join(d, name)
        # bash needs LF line ends even when the bundle is written on Windows; PowerShell takes either
        with open(path, "w", encoding="utf-8", newline="\n" if kind == "bash" else "\r\n") as f:
            f.write(text)
        if kind == "bash":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        paths[kind] = path
    return paths


def _script_paths(bundle):
    if not bundle:
        return None
    return {k: os.path.join(bundle, n) for k, n in DOWNLOAD_SCRIPTS.items()
            if os.path.exists(os.path.join(bundle, n))} or None


def write_manual_bundle(collection: str, body: dict, out: str, reason: str, number=None,
                        has_key: bool | None = None) -> str:
    """Write the request body, the download scripts and INSTRUCTIONS.md (website and API steps) into
    <out>/manual_download/ipums/.

    Needs no network and no key. With no key the website comes first: it needs only an IPUMS account.
    has_key defaults to what the reason implies.
    """
    if has_key is None:
        has_key = reason != "key_missing"
    d = bundle_dir(out)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "extract_spec.json"), "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
        f.write("\n")
    note = ""
    if number:
        note = (f"\nAn extract was already submitted: number {number}. The script uses it and does not "
                f"submit a new one. With curl, skip step 1 and use {number} in place of `<NUMBER>`.\n")
    write_download_scripts(d, collection, body, number)
    sections = [web_section(collection, body), API_TEMPLATE.replace("@NUMBER_NOTE@", note).replace("@RUN@", run_block())]
    if has_key:
        sections.reverse()                      # a key exists: curl is the shorter way
    options = "".join(t.replace("@LETTER@", "AB"[i]) for i, t in enumerate(sections)) + UPLOAD_TEMPLATE
    text = (MANUAL_HEAD.replace("@OPTIONS@", options)
            .replace("@REASON@", REASON_TEXT.get(reason, reason))
            .replace("@COLLECTION_UPPER@", collection.upper())
            .replace("@COLLECTION@", collection))
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
        f.write(text)
    # machine-readable list of what the user will upload (the same shape core.manual writes)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"reason": REASON_TEXT.get(reason, reason), "source": SOURCE, "collection": collection,
                   "extract_number": number, "request_body": "extract_spec.json",
                   "scripts": list(DOWNLOAD_SCRIPTS.values()),
                   "items": [{"source": SOURCE, "name": "data", "filename": f"{collection}_NNNNN.csv.gz",
                              "needs_key": True, "key_env": KEY_ENV,
                              "signup_url": "https://account.ipums.org/api_keys",
                              "note": "the data file IPUMS names (website: no key needed)"},
                             {"source": SOURCE, "name": "ddi", "filename": f"{collection}_NNNNN.xml",
                              "needs_key": True, "key_env": KEY_ENV,
                              "signup_url": "https://account.ipums.org/api_keys",
                              "note": "the DDI codebook"}]}, f, indent=2)
    return d


REPRODUCE_HEAD = """# Reproduce the IPUMS extract

The IPUMS terms of use do not permit redistributing the extract, so this report ships its definition
instead. Request the same extract from IPUMS with your own free account, with one of the options below.
Each download script is one self-contained file with the extract definition built in, so it works on
its own; `extract_spec.json` is the same definition (the API request body), for reference and curl.

"""

REPRODUCE_TAIL = """## Then

Keep the data file (`.csv.gz`) and the codebook (`.xml`) in this folder (the script saves them next
to itself; move them here if you ran it elsewhere). Then package them: from the folder that holds the
report's `spec.json`, run (`<this folder>` is the path of this folder, e.g. `reproduce/ipums`)

```
python <skill>/scripts/fetch.py --source ipums spec.json --out . --ingest <this folder>
```

This writes `data/ipums/ipums_extract.csv` and `data/ipums/codebook.csv`. Compare the SHA-256 of
`data/ipums/ipums_extract.csv` with the audit trail in the report; if IPUMS has revised the samples since,
small differences can occur. The files are for your own use under the IPUMS terms: do not share or repost them.
"""


def write_reproduce_bundle(collection: str, body: dict, d: str) -> str:
    """Write the files a reader needs to request this extract again: the request body
    (extract_spec.json), the download scripts with the same definition built in (always a fresh
    submission: an extract number belongs to one account), and DOWNLOAD.md.

    Used for the replication materials of a report. No microdata, no key.
    """
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "extract_spec.json"), "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
        f.write("\n")
    for stale in os.listdir(d):                  # a saved extract number belongs to whoever ran the script
        if stale.startswith("extract_number_"):
            os.remove(os.path.join(d, stale))
    write_download_scripts(d, collection, body)
    sections = [API_TEMPLATE.replace("@NUMBER_NOTE@", "").replace("@RUN@", run_block(here=True)),
                web_section(collection, body)]
    text = (REPRODUCE_HEAD + "".join(t.replace("@LETTER@", "AB"[i]) for i, t in enumerate(sections))
            + REPRODUCE_TAIL).replace("@COLLECTION_UPPER@", collection.upper()).replace("@COLLECTION@", collection)
    with open(os.path.join(d, "DOWNLOAD.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return d


def report_blocked(err: IpumsError, env: str, collection, body, spec_path: str | None, out: str, number=None):
    """Tell the user how to get unblocked. Prints the route JSON to stdout and exits with code 4."""
    reason = err.kind
    out = os.path.abspath(out)
    detail = str(err).splitlines()[0][:300]
    sp = spec_path or os.path.join(out, "spec.json")
    _, ingest_cmd = _commands(os.path.abspath(sp), out)
    r = {"status": "blocked", "source": SOURCE, "reason": reason, "kind": reason, "environment": env,
         "os": sys.platform, "restricted": keys.restricted(), "detail": detail, "key_env": KEY_ENV}
    bundle = write_manual_bundle(collection, body, out, reason, number) if (body and collection) else None
    if bundle:
        _bundle_fields(r, bundle)
    lead = f"Cannot download from IPUMS: {REASON_TEXT[reason]}."
    where = (f" The hand-download steps (website, no key; or script, with a key) are in "
             f"{os.path.join(bundle, 'INSTRUCTIONS.md')}." if bundle else "")
    if reason == "key_missing" and env in ("local", "ssh"):
        path, setup = setup_command(SOURCE)
        r.update(route="run_setup", setup_script=path, run_command=setup, settings_file=keys.config_path(),
                 message=lead + " The setup script asks for the key with hidden input, checks it and stores it in "
                 "the skill's keys.json (~/.config/data-analyst) (read directly, no restart). Key page: "
                 "https://account.ipums.org/api_keys." + where,
                 action="Give the user run_command to run in their own terminal (never run it for them, never ask "
                        "for the key in chat). Then run the same request again; or --ingest hand-downloaded files.",
                 next_command=setup, ingest_command=ingest_cmd)
    elif reason == "key_missing" and env == "cloud":
        r.update(route="cloud_secret",
                 message=lead + " Add IPUMS_API_KEY to the cloud environment's secrets and start the session "
                 "again, or download by hand." + where,
                 action="Explain both options. For the manual one: " + SEND_BUNDLE, next_command=ingest_cmd)
    elif reason == "auth" or (env in ("local", "ssh") and not keys.restricted()):
        r.update(route="report_error",
                 message=lead + (" The key may be wrong, or the account may not be registered for "
                                 f"{(collection or 'the collection').upper()} at https://account.ipums.org/api_keys."
                                 if reason == "auth" else " Check the internet connection, VPN and proxy settings, "
                                 "then run the request again.") + f" ({detail})",
                 action="Tell the user and stop. A key is set, so do not point at the setup script.",
                 next_command="")
    elif env == "cloud":
        r.update(route="cloud_allowlist",
                 message=lead + " Allow api.ipums.org in the cloud environment's network settings and restart, "
                 "or download by hand." + where,
                 action="Offer both options. For the manual one: " + SEND_BUNDLE, next_command=ingest_cmd)
    else:
        r.update(route="manual_download",
                 message=lead + " This environment cannot download the data. On your own computer, use the IPUMS "
                 f"website (free account, no key) or {DOWNLOAD_SCRIPTS['bash']} / {DOWNLOAD_SCRIPTS['powershell']} "
                 "(with a key), then upload the .csv.gz data file and the .xml codebook." + where,
                 action=SEND_BUNDLE, next_command=ingest_cmd)
    print(redact(f"[ipums] route: {r['route']}: {r['message']}"), file=sys.stderr)
    emit(r, EXIT_BLOCKED)


# --------------------------------------------------------------------------- ingest a hand-downloaded extract
def find_uploads(paths: list[str]) -> tuple[str, str | None]:
    """Pick the data file and the DDI codebook out of uploaded files and folders."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(r, f) for r, _, fs in os.walk(p) for f in fs]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise IpumsError(f"Not found: {p}")
    skip = {"codebook.csv", "variables.csv"}
    data = [f for f in files if re.search(r"\.csv(\.gz)?$", f, re.I) and os.path.basename(f).lower() not in skip]
    named = [f for f in data if re.match(r"^[a-z]+_\d+\.csv(\.gz)?$", os.path.basename(f), re.I)]
    # ipums_extract.csv is our own output name: use it only when nothing else was uploaded,
    # so a folder holding an earlier package does not shadow the real extract.
    other = [f for f in data if os.path.basename(f).lower() != "ipums_extract.csv"]
    data = named or other or data
    if len(data) != 1:
        raise IpumsError("Expected exactly one IPUMS data file (.csv or .csv.gz); found "
                         + (", ".join(data) if data else "none")
                         + ". Pass the data file and the .xml codebook with --ingest.")
    stem = re.sub(r"\.csv(\.gz)?$", "", os.path.basename(data[0]), flags=re.I).lower()
    xml = [f for f in files if f.lower().endswith(".xml")]
    same = [f for f in xml if os.path.splitext(os.path.basename(f))[0].lower() == stem]
    ddi = (same or xml or [None])[0]
    if len(same or xml) > 1:
        raise IpumsError("More than one .xml file found; pass only the IPUMS codebook: " + ", ".join(same or xml))
    return data[0], ddi


def ingest(paths: list[str], spec: dict, out: str):
    collection = spec["collection"]
    body = build_body(spec)
    if not paths:
        paths = upload_dirs()
        if not paths:
            emit({"status": "error", "source": SOURCE, "kind": "spec",
                  "message": "--ingest needs the data file and the .xml codebook (or their folder); no uploads "
                             "folder was found."}, EXIT_SPEC)
    data_path, ddi_path = find_uploads(paths)
    m = re.search(r"_(\d+)\.csv", os.path.basename(data_path), re.I)
    number = int(m.group(1)) if m else "manual"
    if m and not os.path.basename(data_path).lower().startswith(collection.lower() + "_"):
        print(f"[ipums] warning: {os.path.basename(data_path)} does not look like a {collection} extract",
              file=sys.stderr)
    if not ddi_path:
        print("[ipums] warning: no .xml codebook found; codebook.csv and variables.csv will be empty. "
              "Ask the user to upload the DDI file too.", file=sys.stderr)
    print(f"[ipums] packaging {os.path.basename(data_path)} ({collection}#{number})", file=sys.stderr)
    columns = package_files(collection, number, data_path, ddi_path, spec, body, out,
                            acquired="downloaded by hand by the user (IPUMS website, download script or "
                                     "curl) and uploaded", route="manual_download")
    missing = [v for v in body["variables"] if v not in columns]
    if missing:
        print("[ipums] warning: requested variables not in the uploaded file: " + ", ".join(missing)
              + ". The file may belong to a different extract; check the extract definition.", file=sys.stderr)


def load_spec(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        emit({"status": "error", "source": SOURCE, "route": "fix_spec", "kind": "spec",
              "message": f"cannot read {path}: {e}", "action": "Fix spec.json and run --check again.",
              "next_command": ""}, EXIT_SPEC)


def main():
    ap = argparse.ArgumentParser(prog="fetch.py --source ipums", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out", default="out")
    ap.add_argument("--list-samples", metavar="COLLECTION")
    ap.add_argument("--match", default="", help="filter --list-samples by substring (id or description)")
    ap.add_argument("--resume", metavar="COLLECTION:NUMBER")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--manual-bundle", action="store_true",
                    help="write OUT/manual_download/ipums/ (request body, download scripts, instructions) "
                         "without contacting IPUMS")
    ap.add_argument("--reproduce-bundle", metavar="DIR",
                    help="write a reproduce/ folder (request body, scripts reading it, DOWNLOAD.md) into DIR")
    ap.add_argument("--ingest", nargs="*", metavar="PATH",
                    help="data file (.csv.gz) and DDI (.xml) downloaded by hand, or a folder holding them "
                         "(none: the DATA_ANALYST_UPLOADS folders)")
    ap.add_argument("--check", action="store_true",
                    help="pre-flight only: print the route that gets the data here and what to do (no extract)")
    ap.add_argument("--force", action="store_true", help="resubmit even if OUT/data/ipums/ holds this extract")
    ap.add_argument("--env", choices=["auto", *ENVIRONMENTS], default="auto",
                    help="where this runs; decides how the user can supply a key (default: guess)")
    ap.add_argument("--max-wait", type=int, default=4 * 3600, help="seconds to wait for IPUMS")
    ap.add_argument("--no-cache", action="store_true", help="accepted for the shared command line (IPUMS: run-only)")
    a = ap.parse_args()
    env = detect_environment(None if a.env == "auto" else a.env)

    collection = body = number = None
    spec_path = None
    try:
        if a.list_samples:
            collection = a.list_samples
            c = IpumsClient()
            m = a.match.lower()
            for s in c.list_samples(a.list_samples):
                if m in s["name"].lower() or m in s["description"].lower():
                    print(f"{s['name']}\t{s['description']}")
            return

        if a.resume:
            collection, num = a.resume.split(":")
            number = int(num)
            spec_file = os.path.join(data_dir(a.out), "extract_spec.json")
            spec, body = {}, None
            if os.path.exists(spec_file):
                with open(spec_file, encoding="utf-8") as f:
                    saved = json.load(f)
                spec, body = saved.get("spec", {}), saved.get("api_body")
            c = IpumsClient()
            info = c.wait(collection, number, a.max_wait)
            package(c, collection, info, spec, body or info.get("extractDefinition", {}), a.out)
            return

        spec_path = a.spec or (os.path.join(a.out, "spec.json") if a.ingest is not None else None)
        if not spec_path:
            ap.error("spec.json is required (or use --list-samples / --resume)")
        spec_path = os.path.abspath(spec_path)
        spec = load_spec(spec_path)
        probs = spec_problems(spec)
        if probs:
            emit({"status": "route_check" if a.check else "error", "source": SOURCE, "route": "fix_spec",
                  "environment": env, "message": "The request has problems: " + "; ".join(probs),
                  "action": "Fix spec.json (see sources/ipums/source.md and docs/variables.md), then run --check "
                            "again.", "next_command": ""}, EXIT_SPEC)
        collection = spec["collection"]
        body = build_body(spec)

        if a.ingest is not None:
            ingest(a.ingest, spec, a.out)
            return

        est, unit = approx_rows(spec["samples"]), "persons"
        if "householdOnly" in json.dumps(body.get("dataStructure") or {}):
            est, unit = int(est / 2.1), "households"   # households, not persons (2024 ACS: 1.63M of 3.4M)
        names = [str(v).upper() for v in body["variables"]]
        reps = 159 if spec.get("collection") == "cps" else 79          # 160 replicates in the CPS ASEC, 80 in the ACS
        cols = len(names) + sum(reps if v in ("REPWTP", "REPWT") else 0 for v in names)
        mb = est * cols * 5 / 1e6                         # about 5 bytes a value in the CSV
        print(f"[ipums] {len(spec['samples'])} sample(s), {len(body['variables'])} variables ({cols} columns); "
              f"~{est:,} {unit} before case selection, roughly {mb:,.0f} MB of CSV before case selection"
              + (" (large: check free disk space, or case-select a second, smaller extract for the replicate "
                 "weights)" if mb > 1000 else ""), file=sys.stderr)
        if a.dry_run:
            print(json.dumps(body, indent=2))
            return
        if a.reproduce_bundle:
            d = write_reproduce_bundle(collection, body, a.reproduce_bundle)
            emit({"status": "ok", "source": SOURCE, "reproduce_bundle": os.path.abspath(d),
                  "files": sorted(os.listdir(d)), "scripts": dict(DOWNLOAD_SCRIPTS), "doc": "DOWNLOAD.md",
                  "request": "extract_spec.json"})
        if a.manual_bundle:
            d = write_manual_bundle(collection, body, a.out, "a manual download was requested",
                                    has_key=bool(find_api_key()[0]))
            emit({"status": "manual_bundle_written", "source": SOURCE, "manual_bundle": d,
                  "request_body": os.path.join(d, "extract_spec.json"),
                  "download_scripts": _script_paths(d), "instructions": os.path.join(d, "INSTRUCTIONS.md")})
        if a.check:
            r = route_check(spec, body, spec_path, a.out, env, a.force)
            print(redact(f"[ipums] route: {r['route']} ({env}, key: {r['key_source'] or 'none'})\n"
                         f"[ipums] {r['message']}\n[ipums] next: {r['action']}"), file=sys.stderr)
            emit(r, EXIT_SPEC if r["route"] in ("report_error", "fix_spec") else EXIT_OK)

        # Cheapest routes first: the data are already here, or the extract is already queued at IPUMS.
        ex = existing_data(a.out, body)
        if ex and ex["csv_exists"] and ex["spec_matches"] and not a.force:
            print(f"[ipums] reusing {ex['path']} ({collection}#{ex['extract_number']}, downloaded "
                  f"{ex.get('downloaded')}); pass --force to resubmit", file=sys.stderr)
            emit({"status": "ok", "source": SOURCE, "route": "reuse_existing",
                  "extract": f"{collection}:{ex['extract_number']}", "rows": ex.get("rows"), "csv": ex["path"],
                  "data_dir": data_dir(a.out), "codebook": os.path.join(data_dir(a.out), "codebook.csv"),
                  "readme": os.path.join(data_dir(a.out), "README.md")})
        c = IpumsClient()
        if ex and ex["spec_matches"] and ex.get("extract_number") and not ex["csv_exists"] and not a.force:
            number = int(ex["extract_number"])
            print(f"[ipums] resuming {collection}#{number}, submitted earlier", file=sys.stderr, flush=True)
            try:
                info = c.wait(collection, number, a.max_wait)
                package(c, collection, info, spec, body, a.out)
                return
            except IpumsError as e:
                if e.kind in BLOCKING_KINDS or "Timed out" in str(e):
                    raise
                print(f"[ipums] cannot resume {collection}#{number} ({str(e)[:120]}); submitting again",
                      file=sys.stderr, flush=True)
        number = c.submit(collection, body)
        print(f"[ipums] submitted {collection}#{number}", file=sys.stderr, flush=True)
        os.makedirs(data_dir(a.out), exist_ok=True)
        with open(os.path.join(data_dir(a.out), "extract_spec.json"), "w", encoding="utf-8") as f:
            json.dump({"collection": collection, "extract_number": number, "spec": spec,
                       "api_body": body, "status": "submitted"}, f, indent=2)
        info = c.wait(collection, number, a.max_wait)
        package(c, collection, info, spec, body, a.out)
    except IpumsError as e:
        if e.kind in BLOCKING_KINDS:
            report_blocked(e, env, collection, body, spec_path, a.out, number)
        print(f"ERROR: {e}", file=sys.stderr)
        emit({"status": "error", "source": SOURCE, "kind": e.kind, "message": str(e)[:1500]}, EXIT_SPEC)
    except SystemExit:
        raise
    except Exception:                       # unexpected: print a traceback scrubbed of the key
        import traceback
        print("ERROR: " + redact(traceback.format_exc()), file=sys.stderr)
        emit({"status": "error", "source": SOURCE, "kind": "unexpected",
              "message": redact(traceback.format_exc().strip().splitlines()[-1])}, EXIT_UNEXPECTED)


if __name__ == "__main__":
    main()

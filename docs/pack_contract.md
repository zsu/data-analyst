# Source pack contract

Every data source is a folder `plugins/data-analyst/skills/data-analyst/sources/<id>/` (below: `sources/<id>/`), with its
tests outside the skill in `tests/sources/<id>/`, so the skill folder can be zipped and uploaded as is. The model reads only the packs a question needs, so each
pack must be self-contained: its rules, formats and traps live in the pack, not in SKILL.md.

## Files

| File | Required | What it holds |
|---|---|---|
| `policy.json` | yes | Machine-readable rules: key, limits, routes, cache, redistribution, notices, audit hooks, clarifying questions. `core/policy.py` validates it and enforces it. |
| `source.md` | yes | What the model reads before using the source (sections below). |
| `endpoints.json` | standard packs | The API, declaratively (format: docstring of `scripts/core/generic_rest.py`). |
| `adapter.py` | when needed | Python hooks for what `endpoints.json` cannot express. |
| `fetch.py` | deep packs only | The pack's own command line. `scripts/fetch.py --source <id> ...` runs it with the remaining arguments. |
| `docs/` | optional (deep packs) | Long references (API guides, variable lists) that `source.md` points to for a specific need ("for GDP-by-industry tables read docs/gdp_by_industry.md"). Read only on that need. |
| `lib/` | optional | The pack's own analysis helpers (e.g. treasury `tstats.py`, ipums `stats.py`). Imported by `analysis.py` from `<skill>/sources/<id>/lib`. |


Rules for every file:
- Standard library only for network code; pandas and numpy for analysis. Nothing is installed at run time.
- Never write inside the skill folder at run time (it is read-only on claude.ai). Write under `--out`.
- Every message that can reach the user goes through `core.keys.redact`. Keys are never printed, logged,
  written to a file, put in a cache key, or asked for in chat.
- No invented numbers, no sample data presented as real data, no silent fallbacks to another source.
- Read `policy.json` and `endpoints.json` only through `core.packfiles.read_json(pack_dir, name)` (or the
  loaded `pack.policy` / `pack.endpoints`), never with `open()`. The claude.ai upload zip
  (`tools/package_skill.py`) puts every pack's two JSON files into one `sources/packs.json`, because
  claude.ai rejects a zip of more than 200 files. The model reads a policy with `fetch.py --source <id> --policy`.

## Keeping context small (guidance, not a hard limit)
The model reads only SKILL.md and the `source.md` of the packs a question needs; everything else is read by
code. Keep `source.md` to what the model needs to use the source correctly; put long catalogs (endpoint
registries, series IDs, variable lists) in `endpoints.json` (`catalog`) or `lib/`, reached with `--search` /
`--endpoints`, and long references in `docs/`. Never drop a trap or caveat to make a file shorter.
Commands print summaries (row counts, paths, citation), never data. A request that hits `max_rows` must say so
("stopped at N rows"): fetch.py then reports `status: partial`, and the data are not reused as complete.

## policy.json

```json
{
  "id": "cdc",                                   // = folder name
  "title": "CDC Open Data (data.cdc.gov)",
  "agency": "Centers for Disease Control and Prevention",
  "depth": "standard",                           // "deep" = analyst-grade checks; "standard" = tidy fetch + provenance
  "domains": ["health", "mortality"],            // broad areas (routing)
  "keywords": ["death", "mortality", "overdose", "life expectancy", "covid", "births", "obesity"],
  "questions": ["leading causes of death by state", "drug overdose deaths trend"],
  "homepage": "https://data.cdc.gov/",
  "terms_url": "https://www.cdc.gov/other/agencymaterials.html",
  "auth": {
    "env": null,                                 // key variable, e.g. "CENSUS_API_KEY"
    "required": "none",                          // "required" | "optional" | "none"
    "placement": "none",                         // "query" | "header" | "body" | "none"
    "param": null,                               // query/body field or header name
    "prefix": "",                                // header prefix, e.g. "Bearer "
    "home": null,                                // ~/.<home>/api_key fallback file
    "signup_url": null,
    "echoes_key": false,                         // the API echoes keys in errors (BLS, BEA)
    "key_only": [],                              // what a keyless route lacks (shown to the user)
    "extra": {},                                 // more credential params: {"email": "AQS_EMAIL"}
    "demo_key": null                             // api.data.gov packs: "DEMO_KEY" (keyless, low limits)
  },
  "limits": {"rps": 2, "burst": 5, "daily_quota": null, "errors_per_min": null, "bytes_per_min": null, "max_rows": 50000},
  "routes": {
    "api": {"base": "https://data.cdc.gov/resource", "probe": "https://data.cdc.gov/resource/bi63-dtpu.json?$limit=1",
            "hosts": ["data.cdc.gov"]},
    "manual": {"how": "Open the dataset page, Export > CSV, upload the file."}
  },
  "cache": {"mode": "run_only", "ttl_hours": null, "why": "small queries; data/ is reused per run"},
  "redistribution": {"mode": "allowed", "why": "U.S. government work, public domain (17 U.S.C. 105)."},
  "notices": [],
  "retention": null,
  "citation": "Centers for Disease Control and Prevention, {dataset}, data.cdc.gov, retrieved {retrieved_long}.",
  "audit_hooks": [],
  "clarify": ["Final or provisional counts? Provisional data are revised for months."],
  "formats": ["json", "csv"]
}
```

Field rules (`core/policy.py: validate` enforces the starred ones):
- **`auth`**\*: when a key is used, `env` and `signup_url` are required. `required: "optional"` means the API
  works without a key at lower limits. Shared keys: every api.data.gov API uses `DATA_GOV_API_KEY` with
  `home: "data-gov"` and `demo_key: "DEMO_KEY"`.
- **`routes`**\*: `manual.how` is always required: every source must be obtainable by a user with a browser.
  Each network route needs a cheap `probe` URL that needs no key and returns quickly (any HTTP status means
  reachable). Use `api_keyless` for a different keyless host (FRED graph CSVs), `bulk` for flat files.
  `manual.keyless_file` (optional) names a direct, small agency file that holds a keyed request's rows and
  that the pack's `--ingest` reads and filters by the spec (epa-aqs AirData files); `--check` returns it with
  `run_setup`, and it is the only keyless download an unattended run makes. Never set it for an interactive
  page, a large bulk file or a file that must be filtered by hand.
- **`cache.mode`**\*: `none` when the terms forbid copies (FRED); `run_only` by default; `persistent` (with
  `ttl_hours`) only for large, slowly changing files whose terms allow caching.
- **`redistribution.mode`**\*: `allowed` (public domain or open licence), `aggregates_only` (row-level files
  stay local; tables of estimates may be published), `definition_only` (IPUMS: no data file leaves the
  analysis folder; the request definition and reproduce scripts go instead). `why` cites the terms.
  `definition_only` needs `microdata_globs` (files the report builder must refuse) and may list
  `package_include` (globs under `data/<id>/` that MAY go in a package: the request definition, README,
  codebook). Everything else in `data/<id>/` stays local. The report builder adds `reproduce/<id>/` with
  one-command download scripts (`.sh` and `.ps1`, the request built in) and REPRODUCE.md, so anyone can
  rebuild the data under their own account; the audit requires both scripts. A deep pack provides them
  with `--reproduce-bundle DIR` (deep-pack rule 9); a standard pack's come from the manual bundle's items.
  `withhold` (optional): `{"meta": "series.csv", "key": "series_id", "column": "copyright", "values": [...]}`
  drops rows of shared data files for series whose terms forbid redistribution; use `"shareable": [...]`
  instead of `"values"` to withhold everything not on a whitelist (FRED: anything not known to be shareable).
  A report can lift it per series with a clearance (see build_report.py).
- **`notices`**: text the report must carry when it uses the source (FRED, BEA API notices; BLS's statement).
- **`audit_hooks`**: `{"id", "kind", "why", ...}` with kind `require_file` (`glob` under `<out>`),
  `require_text` (`any`: words the report must contain, e.g. "redact"), `require_fact_prefix` (`prefix`), or
  `forbid_in_package` (`globs`).
- **`clarify`**: questions that change the numbers and that a request often leaves open. SKILL.md has the
  model ask them (at most four, in one round) or decide them by the stated default.
- **`citation`**\*: must contain `{retrieved}` or `{retrieved_long}`. Placeholders: `{title}` `{dataset}`
  `{retrieved}` `{retrieved_long}` `{vintage}` `{url}`.

## source.md sections (in this order; headings exactly)

```
# <Title>
Depth: standard | deep. Key: none | optional (VAR) | required (VAR). One line on what it covers.

## When to use
Questions it answers; what it does NOT cover and which pack does (e.g. "par yields: treasury_yields").

## Endpoints and datasets
Table: name, what it returns, key parameters, units, frequency, coverage years. Catalog aliases.

## Spec examples
Two or three complete spec.json examples that work.

## Data format and traps
Units and scaling, missing-value codes, revisions and vintages, preliminary values, fiscal vs calendar,
geography codes, suppression, case-sensitivity of filters, paging limits. Verified facts only; mark
anything unverified as such.

## Access
Key and limits; what each route gives; manual download steps in detail.

## Citation and terms
Citation, required notices, redistribution rule and why.
```

## adapter.py hooks (all optional)

Standard packs run through `scripts/fetch.py` → `core.generic_rest`. An adapter may define:

| Hook | Signature | Purpose |
|---|---|---|
| `check_error` | `(status, body: bytes) -> str \| None` | Error inside an HTTP 200 (BLS `REQUEST_NOT_PROCESSED`, BEA `Error`). |
| `check_spec` | `(spec) -> list[str]` | Extra spec validation (problems become route `fix_spec`). |
| `before_request` | `(req, ep, params) -> params` | Resolve names (state name → FIPS), build IDs, escape query text. |
| `request_note` | `(req, ep, params) -> str \| None` | A fetch note saying how the pack read a request when the reading changes the rows (exact phrase vs every word). |
| `normalize` | `(req, ep, rows) -> rows` | Source quirks: `(NA)`/`(D)` → empty with a flag column, sentinel values, unit columns. Never change published values silently: add a column instead (e.g. `value_flag`). |
| `fetch` | `(spec, pack, route) -> (tables, notes)` | Replace the generic fetch entirely (XML APIs, multi-step lookups). Use `core.http.Client` for every call. |
| `ping` | `(client) -> {"auth_ok", "kind", "detail"}` | One cheap call that proves the key works. |
| `search` | `(query, pack) -> list[dict]` | Live search (e.g. Census variables). Default: search the catalog in endpoints.json. |
| `key_only_needs` | `(spec) -> list[str]` | Features of this request that need a key even when a keyless route exists. |
| `manual_items` | `(spec, pack) -> list[item]` | Manual download links when the generic ones do not fit. |
| `parse_upload` | `(pack, req, paths) -> rows` | Read uploaded files (e.g. a dataset page's CSV export). |

Use `core.combine` (in `scripts/analysis/combine.py`) for state codes; do not copy the table.

## Deep packs

A deep pack keeps its own `fetch.py` command line and its own tested logic, with these rules:
1. Data goes to `<out>/data/<id>/` (use `core.package.data_dir(out, "<id>")`). Manual bundles go to
   `<out>/manual_download/<id>/`.
2. Keys, environment, redaction and the restricted switch come from `core.keys` and `core.env`
   (`find_key`, `redact`, `restricted`, `detect_environment`, `setup_command`, `default_out`). Delete the
   pack's own copies.
3. `--check` prints one JSON object with `route`, `message`, `action`, `next_command` and `source: "<id>"`,
   using only the shared route names: `reuse_existing`, `api`, `api_keyless`, `bulk`, `run_setup`,
   `cloud_secret`, `cloud_allowlist`, `manual_download`, `install_deps`, `fix_spec`, `report_error`.
   Put a bundle's folder in `manual_bundle`.
4. `next_command` strings call `python "<skill>/scripts/fetch.py" --source <id> ...`, never the pack's
   file directly.
5. Exit codes 0 / 2 / 3 / 4 as in `core/routes.py`.
6. `--ingest` with no paths also looks in `core.env.upload_dirs()` (the DATA_ANALYST_UPLOADS folders).
7. `request.json` (or the pack's existing provenance file) records the route, retrieval time and
   credential-free URLs; keep the pack's file names stable so analysis code keeps working.
8. Its `lib/` holds the pack's statistics module and takes `Facts` from `scripts/analysis/facts.py`.
9. Manual bundles lead with one copy-paste command per system (`core.manual.run_lines`) that runs a
   self-contained `download_<id>.sh` / `.ps1` with the request built in: curl or Windows PowerShell only, no
   Python; the key comes from its environment variable or a hidden prompt and never reaches argv or a saved
   file. The bundle also holds the request (`spec.json`). The route JSON names them in `download_scripts`,
   `request_spec` and `user_commands`; INSTRUCTIONS.md never shows analyst commands (`--ingest` stays in
   `next_command`). A `definition_only` deep pack also implements `--reproduce-bundle DIR`: it writes the
   scripts (always a fresh request) and the request file into DIR and prints JSON with `scripts`
   (`{"bash", "powershell"}` file names), `request` and `doc`.

## Tests
Tests live outside the skill: `tests/sources/<id>/test_<id>.py` (module name with `_` for `-`) and
`tests/sources/<id>/fixtures/`. A test subclasses `testkit.PackTest` (`tests/testkit.py`) with `source = "<id>"`
and a `sample_spec`; it runs offline with `DATA_ANALYST_FIXTURES=tests/sources/<id>/fixtures`. Each key in
`fixtures/index.json` is `"<METHOD> <credential-free URL>"` (POST: append a space and the first 12 hex digits of
sha256 of the JSON body with sorted keys) mapped to a file or `{"file", "status", "headers"}`. Fixtures are
small (a few rows, under 200 KB) and come from real responses (record with
`DATA_ANALYST_RECORD=tests/sources/<id>/fixtures` and a live fetch) or the API's documented examples, named
`doc_*.json`. Never put a key in a fixture. A test must at least: validate policy.json, run `--check` (route
`api`, or the expected keyed route), fetch one spec into a temp `--out`, and assert the CSV columns and a value;
the restricted-sandbox route test is inherited. `testkit.pack_dir(id)` and `testkit.load_pack_module(id, file)`
reach the pack's files.

Run all tests from the repo root: `python -I tests/run_all.py` (one pack: `python -I tests/run_all.py <id>`).

# Department of Labor Open Data API (OSHA, WHD, ETA unemployment claims, MSHA)
Depth: standard. Key: required for data (DOL_API_KEY); the dataset catalog is keyless. OSHA inspections,
violations and accident investigations; Wage and Hour Division enforcement cases (back wages, penalties);
weekly unemployment insurance claims (national and state); MSHA mines, inspections and violations; and
the other 45 datasets of the DOL API v4.

## When to use
- Workplace safety enforcement: OSHA inspections by state, industry (NAICS/SIC), establishment, type;
  violations and penalties; fatality and injury investigations. Mine safety (MSHA).
- Wage theft enforcement: WHD cases with back wages, employees owed, civil money penalties by law
  (FLSA minimum wage and overtime, child labor, FMLA, Davis-Bacon, SCA, H-1B, H-2A).
- Weekly initial and continued UI claims, national (seasonally adjusted and not) and by state.
- Not here: unemployment rate, payroll jobs, wages, CPI, injury and illness rates (bls); the
  official monthly employment situation (bls); federal workforce (opm); federal contracts to the
  employers (usaspending).

## Endpoints and datasets
| Endpoint | Key | Returns | Key parameters |
|---|---|---|---|
| `query` | yes | rows of one dataset | `dataset`, `filters`, `search`, `state`, `filter_object`, `fields`, `sort_by`, `sort` |
| `datasets` | no | the 45-dataset catalog: agency, api_url, name, frequency, tags | none |
| `dataset_info` | no | the field dictionary: column_name, data_type, column_desc | `dataset_id` (number or alias) |

Main aliases (`--search "words"` or `--endpoints` for all 45): `osha_inspection` (OSHA/inspection,
daily; state field site_state; default sort open_date), `osha_violation` (issuance_date),
`osha_accident` (event_date), `osha_accident_injury`, `osha_accident_abstract`, `whd_enforcement`
(WHD/enforcement, quarterly; st_cd; findings_end_date), `eta_ui_national_weekly_claims`
(ETA/ui_national_weekly_claims, rptdate), `eta_ui_state_weekly_claims` (st; rptdate),
`msha_mines`, `msha_inspection`, `msha_violation`, `msha_accident`, `eta_apprenticeship_data`,
`eta_otaa_petition` (trade adjustment petitions), `wb_ndcp` (childcare prices), `vets_4212`.
Codes (DOL's field dictionary, `dataset_info` osha_inspection, verified 2026-10): OSHA insp_type
A accident, B complaint, C referral, D monitoring, E variance, F follow-up, G unprogrammed related,
H planned, I programmed related, J unprogrammed other, K programmed other, L other, M fatality/catastrophe,
N unprogrammed emphasis; viol_type S serious, W willful, R repeat, O other-than-serious, U
unclassified; degree_of_inj 1 fatality, 2 hospitalized, 3 not hospitalized.

## Spec examples
```json
{"description": "WHD enforcement cases in Texas, newest first",
 "requests": [{"name": "whd_tx", "endpoint": "query", "params": {"dataset": "whd_enforcement", "state": "TX"},
   "max_rows": 5000}]}
```
```json
{"description": "OSHA accident and complaint inspections of Amazon establishments in California since 2024",
 "requests": [{"name": "amazon_ca", "endpoint": "query",
   "params": {"dataset": "osha_inspection", "state": "CA", "search": {"estab_name": "amazon"},
              "filters": {"insp_type": ["A", "B"], "open_date": {"gt": "2024-01-01"}}}}]}
```
```json
{"description": "Weekly national UI claims since 2020 with the column dictionary",
 "requests": [{"name": "ui_national", "endpoint": "query",
   "params": {"dataset": "eta_ui_national_weekly_claims", "filters": {"rptdate": {"gt": "2019-12-31"}}, "sort": "asc"}},
  {"name": "ui_fields", "endpoint": "dataset_info", "params": {"dataset_id": "eta_ui_national_weekly_claims"}}]}
```

## Data format and traps
- Field names are DOL's, not friendly ones. The UI claims tables use coded columns: national `c1`/`c2`
  seasonal factors (initial, continued), `c3` covered employment, `c4` initial claims NSA, `c5` initial
  claims SA, `c6` continued claims NSA, `c7` continued claims SA, `c8`/`c9` 4-week averages (SA);
  state `st`, `c3` state UI initial claims, `c8` state UI continued weeks claimed, `c18` covered
  employment (12-month average), `c19` 13-week insured unemployment rate (extended-benefit trigger), and more (the fetch notes list every column; or use
  `dataset_info`). `rptdate` is the week-ending date. State tables are not seasonally adjusted.
- Filters: `filters` {field: value} is an exact, case-sensitive equals; a list means `in`; {"gt": x,
  "lt": y, "neq": z, "like": "%x%"} adds those operators (DOL has only eq, neq, gt, lt, in, not_in,
  like). `search` {field: text} matches a substring in several spellings (as typed, UPPER, lower,
  Title), because `like` is case-sensitive and has no implicit wildcards: OSHA estab_name is upper case,
  WHD trade_nm mixed. Mixed-case names in other spellings ("McDonald's" vs "MCDONALDS") still differ:
  try variants. All conditions are combined with AND; a raw `filter_object` is ANDed too.
- Date fields: comparisons on dates with gt/lt are as the API evaluates them (unverified without a key:
  check the first rows). Dates come as text, some with a time (`2005-12-20T00:00:00`).
- Sorting defaults to the dataset's date field, newest first; `"sort_by": ""` keeps the API's own order.
  Paging is limit/offset, 1,000 rows per call (the API allows up to 10,000 rows or 5 MB).
- Field names are checked by `--check` against the keyless field dictionary (`dataset_info`), so a wrong
  name is `fix_spec` even without a key (when the dictionary cannot be read, the check is skipped; a field
  the API has but the dictionary lacks: `"check_fields": false` on that request). `osha_inspection` has no county field: filter on
  `site_city` or `site_zip` (or fetch the state and select in analysis.py).
- An unmatched filter returns no rows (the fetch then fails with a hint); an unknown field or operator
  is an HTTP 400 with the API's message.
- Counts of inspections, violations or cases measure enforcement activity, which depends on staffing and
  targeting; they are not rates of violations. OSHA state-plan states run their own inspections
  (reporting_id tells the office). Penalties: initial vs current (after settlement) differ.
- WHD: a change in data collection on 2025-10-01 changed how violations are counted (case_violtn_cnt and
  related columns per DOL's field notes); back wages are amounts agreed to be paid, not collected.
- Names that do not exist: ETA/ui_claims_national and ETA/ui_claims_state (the live names are
  ui_national_weekly_claims and ui_state_weekly_claims); OSHA accident has no state field.

## Access
Key: free; register at https://dataportal.dol.gov/registration (login.gov), then copy a key from
https://dataportal.dol.gov/api-keys (up to 5 keys; sharing keys is prohibited). The key travels as the
`X-API-KEY` query parameter (DOL API User Guide; the pack never logs it). Without a key only `datasets`
and `dataset_info` run (route `api`); data requests route to `run_setup` (local) or `manual_download`
(sandbox). Manual route: `manual_download/INSTRUCTIONS.md` has one link per 1,000 rows with
`{API_KEY}` to replace (or one command runs download_data.sh / .ps1, which asks for the key hidden); upload, then `--ingest`. Other
sources of the same data: the API Query Builder on each dataset page, https://www.osha.gov/data,
https://enforcedata.dol.gov, https://oui.doleta.gov/unemploy/claims.asp (weekly claims).

## Citation and terms
"U.S. Department of Labor, <agency> <dataset name>, DOL Open Data API, retrieved <date>." Public domain;
redistribution allowed. Enforcement records name employers: present them as DOL records (findings and
citations can be contested). Not verified live in this pack's tests (no key): data columns and the
filter syntax follow the DOL API User Guide and the field dictionaries.

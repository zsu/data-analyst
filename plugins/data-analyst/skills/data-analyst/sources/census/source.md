# Census Bureau Data API (ACS, decennial census, population estimates, SIPP, economic indicators, trade, government finances)
Depth: standard. Key: required (CENSUS_API_KEY). American Community Survey 1-year and 5-year estimates,
2020 and 2010 decennial census counts, population estimates, County Business Patterns and the Economic
Census, for the nation, states, counties, places, tracts and ZIP code areas; SIPP person-month microdata;
time series of monthly economic indicators, imports and exports by commodity, and state and local government
taxes, finances and pensions.

## When to use
- Population, age, race and Hispanic origin, household income, per capita income, poverty, home values,
  rents, tenure, education, health insurance, commuting, language, for any U.S. geography; state or
  county profiles; population denominators for per-capita figures (federal spending, deaths, energy use).
- Month-to-month income, program participation (SNAP, SSI, Medicaid, unemployment benefits), health
  coverage spells, assets and debts of the same people over time: SIPP microdata (`sipp`), national
  estimates weighted by the pack.
- Monthly retail sales, housing starts, construction spending; quarterly state and local tax revenue
  (QTAX) and public pension assets; annual state and local government revenue, spending, debt, state tax
  collections and pension systems; monthly imports and exports by HS commodity and country, with the duty
  calculated on imports (tariff revenue by product or country).
- Not here: monthly unemployment and jobs (bls), GDP and personal income by state (bea), CPS, ACS and
  NHIS microdata for custom tabulations (ipums), international comparisons (world-bank), federal spending
  by state (usaspending, which ships stale population figures: use this pack for the denominator).

## Endpoints and datasets
| Endpoint | Returns | Key parameters |
|---|---|---|
| `query` | one row per geography; ACS estimates with their margins of error | `dataset`, `get`, `for`, `in`, `ucgid`, or `state` / `county` / `place` / `zcta` names; predicates such as `YEAR`, `HISP` |
| `group_variables` | every variable of a table: code, label, concept | `dataset`, `group` (B19013, S1701, DP03, P1) |
| `groups` | every table of a dataset | `dataset` |

Datasets (catalog aliases; `latest/...` resolves to the newest vintage the API serves, checked each run):
`acs5` latest/acs/acs5 (2024 = 2020-2024, all geographies), `acs1` latest/acs/acs1 (2024; areas of
65,000+), `acs5_subject` / `acs1_subject` (S tables: rates and medians), `acs5_profile` /
`acs1_profile` (DP02-DP05; percent estimates end in PE), `acs1_supplemental` (acsse, areas 20,000+),
`dec_pl_2020` 2020/dec/pl, `dec_dhc_2020`, `dec_dp_2020`, `dec_sf1_2010`, `pep_charv` latest/pep/charv
(vintage 2023 is the newest on the API in October 2026; for the latest vintage without a Census key use FRED
`<ST>POP`, e.g. `MDPOP`, resident population in thousands as of July 1, or Census's NST-EST xlsx; for
every county, a fred `regional` request with any county population series and `region_type: county`
returns the national cross-section for one date, to filter by state FIPS),
`pep_population_2021` 2021/pep/population (POP_2020, POP_2021; states and above only),
`pep_population_2019` (2010-2019, down to places), `cbp` 2023/cbp,
`ecnbasic_2022`, `sipp` latest/sipp (SIPP microdata; 2022-2025 on the API, one per calendar year).
Time series (no vintage in the path; select periods with `time`): `eits_retail` timeseries/eits/marts,
`eits_housing_starts` timeseries/eits/resconst, `eits_construction` timeseries/eits/vip, `eits_qtax`
timeseries/eits/qtax, `eits_pensions` timeseries/eits/qpr (other indicators: timeseries/eits/m3, mtis,
mrts, ftd, bfs, qfr, qss), `trade_imports_hs` / `trade_exports_hs` timeseries/intltrade/imports|exports/hs
(also .../naics, .../statehs, .../porths, .../enduse), `govs_finances` timeseries/govslocalfin (2017-2024),
`govs_state_finances` timeseries/govsstatefin (1967-2024), `govs_state_tax` timeseries/govsstatetax
(2016-2025), `govs_pensions` timeseries/govspension (2012-2025); every dataset: https://api.census.gov/data/timeseries.json.
Any path works (`2019/acs/acs1`, `2023/acs/acs5/subject`).
Common variables: B01003_001E population, B01002_001E median age, B19013_001E median household income,
B19301_001E per capita income, B17001_002E below poverty (universe B17001_001E), S1701_C03_001E poverty
rate, B25077_001E median home value, B25064_001E median gross rent, B15003_022E bachelor's degree
(universe B15003_001E), B23025_005E unemployed (labor force B23025_003E), P1_001N 2020 count.
Find others: `fetch.py --source census --search "median household income"` (latest ACS 5-year) or
`--search "2020/dec/pl: total population"`; then `group_variables` for the whole table.

## Spec examples
```json
{"description": "Median household income, Maryland counties, latest ACS 5-year",
 "requests": [{"name": "md_county_income", "endpoint": "query",
   "params": {"dataset": "acs5", "get": "NAME,B19013_001E", "for": "county:*", "state": "Maryland"}}]}
```
```json
{"description": "Home value and rent in Montgomery County MD; population of Baltimore city",
 "requests": [{"name": "montgomery_housing", "endpoint": "query",
   "params": {"dataset": "acs5", "get": ["NAME", "B25077_001E", "B25064_001E"], "state": "MD", "county": "Montgomery"}},
  {"name": "baltimore_pop", "endpoint": "query",
   "params": {"dataset": "acs5", "get": "NAME,B01003_001E", "state": "MD", "place": "Baltimore"}}]}
```
```json
{"description": "Poverty rate by state, ACS 1-year 2024, and the 2020 Census count",
 "requests": [{"name": "poverty_2024", "endpoint": "query",
   "params": {"dataset": "2024/acs/acs1/subject", "get": "NAME,S1701_C03_001E", "for": "state:*"}},
  {"name": "pop_2020", "endpoint": "query",
   "params": {"dataset": "dec_pl_2020", "get": "NAME,P1_001N", "for": "state:*"}}]}
```

```json
{"description": "Share of people without health coverage in December, SIPP 2024 (weighted by WPFINWGT)",
 "requests": [{"name": "sipp_coverage_dec", "endpoint": "query",
   "params": {"dataset": "2024/sipp", "get": "TAGE,RHLTHMTH,TPTOTINC", "MONTHCODE": "12"}}]}
```

```json
{"description": "Retail sales, 2026 to date; customs duty on imports from China, August 2026, by HS chapter",
 "requests": [{"name": "retail_2026", "endpoint": "query",
   "params": {"dataset": "eits_retail", "get": "cell_value,data_type_code,category_code,seasonally_adj,time_slot_id,error_data",
              "for": "us:*", "time": "2026"}},
  {"name": "china_duty_2026_08", "endpoint": "query",
   "params": {"dataset": "trade_imports_hs", "get": "CTY_NAME,I_COMMODITY,GEN_VAL_MO,CON_VAL_MO,DUT_VAL_MO,CAL_DUT_MO",
              "CTY_CODE": "5700", "COMM_LVL": "HS2", "time": "2026-08"}}]}
```
```json
{"description": "State and local government finances by state, 2024",
 "requests": [{"name": "govfin_2024", "endpoint": "query",
   "params": {"dataset": "govs_finances", "get": "AGG_DESC,GOVTYPE,AMOUNT,AMOUNT_CV", "for": "state:*", "time": "2024"}}]}
```

## Data format and traps
- Margins of error: every ACS `...E` estimate is fetched with its `...M` margin of error (`...PE` with
  `...PM`), each in its own column, at the 90% confidence level. They are never dropped (`moe: false`
  is refused). Standard error = MOE / 1.645. Two estimates differ significantly only if
  |a - b| > 1.645 * sqrt(SE_a^2 + SE_b^2); say so before ranking small areas.
- Special values (any number <= -1,000,000) are not data: -666666666 (estimate not computable),
  -999999999 (N, too few cases), -888888888 (X, not applicable), -222222222 and -333333333 (MOE not
  computable), -555555555 (controlled estimate: treat the MOE as zero). The pack writes an empty cell
  and a `<column>_flag` column with the meaning; JSON nulls (no data for the geography) are flagged too.
- Top-coding: median home value 2000001 means $2,000,000 or more; median gross rent 2001, 3501 or 4001
  means $2,000, $3,500 or $4,000 or more (by year). The value stays, with a flag. Medians in an open
  interval are not exact; the group() call returns the EA annotation (median+, median-).
- Values arrive as text, exactly as published; NAME reads "Montgomery County, Maryland". Geography
  code columns (state, county, place, tract) are strings with leading zeros: keep them as text.
- 1-year vs 5-year: 1-year (normally released each September for the prior year; the 2025 release was not on the API on 2026-10-20) covers only areas of 65,000+
  and has no tracts, block groups or ZCTAs; 5-year (normally each December) covers all areas but pools
  five years. Never compare a 1-year with a 5-year figure, and do not compare overlapping 5-year
  periods (2019-2023 vs 2020-2024). No standard 2020 ACS 1-year release exists (experimental only).
- Dollars are inflation-adjusted to the last year of the period (2024 dollars for 2020-2024); adjust
  before comparing releases. Universes differ by table (households vs population for whom poverty is
  determined): use the table's total as the denominator.
- Geography vintage: codes and boundaries change. Connecticut uses nine planning regions (county codes
  110-190) instead of its eight counties from ACS 2022 on. Name resolution uses the dataset's own NAME
  list when a key is set, else the Census 2020 reference files (noted in the output; they lack the CT
  planning regions). Ambiguous names (Baltimore County vs Baltimore city) are refused with choices; a
  place name prefers the incorporated place over a CDP of the same name.
- A query whose geography matches nothing returns HTTP 204 (reported as no rows). At most 50 variables
  per call, margins of error included: split larger requests. ZCTAs (`zcta`) do not nest in states.
- 2020 Census counts below the state level carry differential-privacy noise; decennial variables end
  in N and have no MOE. Population estimates (pep) are revised every vintage: name the vintage.
- PEP coverage on the API (checked 2026-10-22): pep/charv exists only as vintage 2023 (nation, state,
  county, metro areas; no places); 2021/pep/population has states and above only; 2019/pep/population is
  the last API vintage with place and county subdivision estimates. Vintages 2022 and 2024-2025 (county
  and city/town totals for 2022-2025) are not on the API: use the Census estimates files at
  https://www.census.gov/programs-surveys/popest/data/tables.html (manual download) and name the vintage.
- SIPP (`sipp`, `20XX/sipp`): public-use microdata, one row per person per reference month (MONTHCODE
  1-12). The reference year is the calendar year before the file's label: `2024/sipp` was collected in
  2024 about January-December 2023; say "2023" for its figures. Filter MONTHCODE (12 = December) for a
  point in time, or average the months for the year. The pack always adds SSUID, PNUM (person = SSUID + PNUM), MONTHCODE and
  WPFINWGT, the person weight: every estimate is weighted by WPFINWGT, never a row count. No geography but
  `state`, which filters TST_INTV (state of the interview address); SIPP is designed for national estimates.
  -9 means NA or missing in most variables: drop it before summing. Standard errors need the replicate
  weights, which are not on the API (bulk file `rw<YYYY>_csv.zip`, REPWGT1-REPWGT240, at
  https://www2.census.gov/programs-surveys/sipp/data/datasets/<YYYY>/; method in the year's user guide); without them
  give the point estimate and say no standard error was computed. Variable names and codes:
  `--search "<YYYY>/sipp: health insurance"` or the dataset's variables.json. The bulk primary file
  (`pu<YYYY>_csv.zip`, about 100 MB) has every variable, for a manual download.
- Time series (`timeseries/...`): give `time` (`2026`, `2026-08`, `2026-Q2`, or `from 2024-01 to 2026-08`).
  Economic indicators (eits) return one row per code combination: `category_code` (industry or tax type),
  `data_type_code` (level, percent change, ...), `seasonally_adj` (yes/no), `time_slot_id`, `error_data` (yes =
  the row is a margin or standard error, not an estimate). The codes are not in the API's metadata: run one
  narrow query and read the codes that come back, or the program's definitions page (census.gov/econ/currentdata),
  then filter on them; never sum across data types or adjustment flags. Monthly indicators are revised (advance,
  preliminary, revised): say which release. Units: retail and construction $ millions, housing units in
  thousands, QTAX `CELL_VALUE` $ millions (variables.json labels say so); government finances `AMOUNT` $ thousands
  with a coefficient of variation `AMOUNT_CV` (percent, sample-based local figures). Government finance years are
  each government's fiscal year, not the calendar year. International trade: values in dollars; `GEN_VAL_MO`
  general imports vs `CON_VAL_MO` imports for consumption; `CAL_DUT_MO` is the duty calculated on imports for
  consumption (an estimate of duty owed, not Treasury's customs receipts); `COMM_LVL` HS2/HS4/HS6/HS10 must be
  fixed or totals double-count; `CTY_CODE` country codes (5700 China, 2010 Mexico, 1220 Canada); it takes no
  for= geography; `SUMMARY_LVL` CGP rows are country groups, not countries (drop them before summing).
- Key: since 2026-05-12 every data query needs a key; the API redirects keyless queries to a "Missing
  Key" page, which the pack reports as a key error. Metadata (variables, discovery) still answer
  without a key.

## Access
Key required: free at https://api.census.gov/data/key_signup.html (an e-mail with an activation link).
Store it with the setup script (route `run_setup`); in a sandbox the route is `manual_download`. Limits
are not published; the pack sends at most 3 requests a second. Manual route: `manual_download/
INSTRUCTIONS.md` lists one JSON link per request with `{API_KEY}` to replace in the browser (or one
command runs download_data.sh / .ps1, which asks for the key with hidden input); upload the files, then `--ingest`. Without the bundle:
https://data.census.gov, search the table (B19013), set geography and year, Download Table > CSV, and
upload it saying it is a data.census.gov export (one estimate and one MOE column per geography; `--ingest`
reads it long: label, geography, measure, value). Its table JSON (data.census.gov/api/access/data/table, no
key) is read too, keeping the spec's `get` variables and their MOEs: one table per file, so give each table its
own request and save its JSON as `<request name>.json` (e.g. `...table?id=ACSDT1Y2024.B25031&g=310XX00US<CBSA code>`
for a metro area; worked keyless on 2026-10-22).
Keyless, for a published ACS detailed table (a benchmark): the table-based summary file, one pipe-delimited
file per table, e.g. https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/1YRData/acsdt1y2024-b08013.dat
(5-year: 5YRData/acsdt5y...); geography ids are in the same folder's documentation. Subject and profile
tables (S2801, DP02) without a key: https://data.census.gov/api/access/data/table?id=ACSST1Y2024.S2801&g=010XX00US$0400000
returns the table JSON (all states); `--ingest` reads it (it is a benchmark: file it under `published/`
when no census fetch was made).
Keyless, for state population totals (PEP): the fred pack's `<ST>POP` series (thousands, July 1) come with
no key at all, through its keyless graph route, with the usual data/fred/ dictionary and provenance. They
are annual and can lag a new Census vintage by weeks: compare the latest year with the Census release
before using them, and name the vintage. (A cross-section of every county through fred `regional` needs a
FRED key.) A Census estimates file (popest tables) that is still needed is a published table: save it under
`<out>/published/` with `source_url` and `accessed` on every row, never straight into `data/census/`.

## Citation and terms
"U.S. Census Bureau, <dataset, e.g. 2020-2024 American Community Survey 5-year estimates, table
B19013; or 2024 Survey of Income and Program Participation, public-use file>, Census Data API, retrieved <date>." Public domain. Required notice (Terms of Service): "This
product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau."
Redistribution allowed. Metadata (variables lists) is cached on disk for 30 days; data are not.

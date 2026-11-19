# HHS Poverty Guidelines API (federal poverty level for program eligibility, ASPE)
Depth: standard. Key: none. The HHS poverty guidelines (often called the federal poverty level, FPL) from 1983 to
the current year, for the 48 contiguous states and DC, Alaska and Hawaii, by household size, in dollars per year.

## When to use
- The income limit behind an eligibility rule stated as a percent of the poverty guidelines: Medicaid expansion
  (138 percent), CHIP, ACA premium tax credits, LIHEAP, Head Start, school meals (130 and 185 percent), SNAP's
  gross and net income tests (130 and 100 percent); the guideline for a household size and year; how the
  guidelines grew over time. Multiply the guideline by the percent yourself and say so.
- The guidelines are HHS's simplified version of the Census poverty thresholds, updated each year by the CPI-U
  under 42 U.S.C. 9902(2) and published in the Federal Register, usually in January.
- Not here: poverty rates, counts of people in poverty and the Census poverty thresholds, which are a different
  (statistical) measure with separate figures by age and number of children (census); SNAP participation and
  benefits, and the fiscal-year SNAP income standards USDA derives from the guidelines (usda-fna has the program
  data; the SNAP tables themselves are not covered); Medicaid and CHIP enrollment and spending (medicaid).
  Guidelines for Puerto Rico and the other territories do not exist.

## Endpoints and datasets
Base `https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines/api`, keyless, JSON, one answer per
year, state group and size (verified live 2026-11-18). The pack makes one call per combination and returns one CSV
per request.

| Endpoint | Returns (one row per) | Key parameters | Units, frequency, coverage |
|---|---|---|---|
| `guidelines` | year x state group x household size | `year` (1983 to the current year), `state` (`us`, `ak`, `hi` or `all`), `household_size` (1 to 20); each a value, a list, or a range string such as `"2020-2026"` or `"1-8"` | dollars of annual income, `guideline_usd`; annual (issued in January); 1983 on |

Columns: `year`, `state` (`us`, `ak`, `hi`), `area` (its name), `household_size`, `guideline_usd`, `value_note`
(filled for sizes above 8). At most 600 combinations per spec (one call each, two per second).

## Spec examples
```json
{"description": "Poverty guideline for a family of four, 48 contiguous states and DC, 2025 and 2026",
 "requests": [
  {"name": "fpl_family4", "endpoint": "guidelines", "params": {"year": [2025, 2026], "state": "us", "household_size": 4}}]}
```
```json
{"description": "2026 poverty guidelines, every schedule, household sizes 1 to 8",
 "requests": [
  {"name": "fpl_2026", "endpoint": "guidelines", "params": {"year": 2026, "state": "all", "household_size": "1-8"}}]}
```
```json
{"description": "Poverty guideline for a household of three, 2000 to 2026, and a single adult in Alaska",
 "requests": [
  {"name": "fpl_3_trend", "endpoint": "guidelines", "params": {"year": "2000-2026", "state": "us", "household_size": 3}},
  {"name": "fpl_ak_1", "endpoint": "guidelines", "params": {"year": "2024-2026", "state": "ak", "household_size": 1}}]}
```

## Data format and traps
- The API silently answers something else when it does not accept a value (verified 2026-11-18): an unknown year
  (2027, 1982) answers the latest year (2026), an unknown state (`ny`) answers `us`, a non-numeric size answers
  size 1, and size 0 answers the per-person increment for each person above 8 (5380 in 2024), not a guideline.
  The pack refuses such specs (`fix_spec`) and, after each call, fails when the echoed year, state or size differs
  from the request; that is how a year not yet published (early January) shows up. Never work around it.
- `state` is a schedule, not a state: `us` is the 48 contiguous states and the District of Columbia (use it for
  New York, Texas, ...); Alaska (`ak`) and Hawaii (`hi`) have higher guidelines (2026, family of four: 33,000 us,
  41,250 ak, 37,950 hi). The API echoes the state in either case (`us` or `US`); the pack writes lower case.
- Household sizes above 8: the published tables stop at 8 and say "add $X for each additional person"; the API
  answers sizes 9 and up with that sum (2024: size 8 52,720, size 9 58,100, size 20 117,280; increment 5,380). The
  pack allows sizes up to 20 and marks them in `value_note`; say they are computed by the published rule. The
  documentation lists sizes 1 to 8 only.
- The value field is `income` (a string, or a number for sizes above 8), not `poverty_threshold` as the API
  documentation's example shows. The pack writes it unchanged as `guideline_usd` (whole dollars per year).
- Guidelines vs thresholds: the guidelines (HHS, administrative, program eligibility) are not the Census poverty
  thresholds (statistical, used for poverty rates). Do not compute a poverty rate with the guidelines; say
  "poverty guidelines", and "federal poverty level" only once defined as the guidelines.
- Year: guidelines are named for the year they are issued (the 2026 guidelines, January 2026) and reflect price
  changes through the previous calendar year, so they roughly equal the Census thresholds of the year before.
  Programs set their own effective dates. ACA premium tax credits for coverage year Y use the guidelines of year
  Y-1 (2026 coverage uses the 2025 guidelines); SNAP uses USDA's fiscal-year income standards (from October 1),
  derived from the guidelines; say which convention you used.
- The 2026 guidelines compare 11 months of 2025 CPI-U with 12 months of 2024 (no October 2025 CPI was published
  during the shutdown), per ASPE's note on the guidelines page.
- Percent-of-FPL limits (138, 200, 400 percent) are not in the API: multiply `guideline_usd` and round as the
  program does (unverified per program; check its rule).

## Access
No key; the host answers scripts (verified 2026-11-18). Limit used: two calls per second; one call per year x
state x size. Route `api`; `manual`: the links in `manual_download/INSTRUCTIONS.md` open each JSON answer in a
browser (save each page under the name shown and upload them with --ingest). Without the links, the current
year's tables are at https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines and every earlier
year's, with its Federal Register citation, at
https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines/prior-hhs-poverty-guidelines-federal-register-references.
API documentation: https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines/poverty-guidelines-api.

## Citation and terms
U.S. Department of Health and Human Services, Office of the Assistant Secretary for Planning and Evaluation, HHS
Poverty Guidelines (<dataset>), Poverty Guidelines API, aspe.hhs.gov, retrieved <date>. For a formal reference,
ASPE suggests "the poverty guidelines updated periodically in the Federal Register by the U.S. Department of
Health and Human Services under the authority of 42 U.S.C. 9902(2)". U.S. government work, public domain;
redistribution allowed.

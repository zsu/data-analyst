# USDA Food and Nutrition Administration: SNAP State Options Report and SNAP State Activity Reports
Depth: standard. Key: none. Two SNAP reports by state agency. The State Options Report gives each agency's choices
on SNAP policy options, waivers and demonstration projects (1st to 17th edition, 2002-2025). The State Activity
Reports give participation, issuance, administrative costs, fair hearings, fraud and recipient claims (FY 2004-2023).

## When to use
- Which states use an option or waiver (BBCE, ABAWD time limit waiver, simplified reporting, drug felony ban,
  combined application projects ...). One state's choices compared with another's. A state's SNAP administrative
  cost, federal share, cost per case, fair hearings, fraud investigations, disqualifications or claims for
  FY 2019-2023 (tidy rows), or for FY 2004-2018 (page text).
- Not here:
  - monthly or more recent SNAP participation and benefits: the State Activity Reports end with FY 2023;
  - WIC, school meals (NSLP/SBP), SNAP Quality Control microdata and payment error rates: not covered;
  - poverty and SNAP receipt in household surveys: census (ACS), ipums;
  - federal spending on SNAP by recipient: usaspending.
- The agency was renamed: the Food and Nutrition Service (FNS) is now the Food and Nutrition Administration (FNA).
  fns.usda.gov redirects to www.fna.usda.gov. The reports up to 2025 still say FNS.

## Endpoints and datasets
Verified live 2026-10-30. One CSV per request.

| Endpoint | Returns (one row per) | Key parameters | Coverage |
|---|---|---|---|
| `options_by_state` | agency × policy option: `edition, state, state_abbr, policy_option, option_selection, agencies_with_selection, share_of_agencies` | `state` (names or USPS codes, default all 53), `edition` (`16`, `17`, default 17) | policy options only (13-16 per agency); no waivers or projects |
| `option_states` | agency with a selection: `edition, policy_option, option_selection, state, state_abbr` | `policy` (catalog alias or name, required), `selection` (default: every selection, so each agency once), `edition` | every policy, waiver and project of editions 16-17 |
| `options_report` | PDF page: `edition, edition_label, page, state, text` | `edition` (1-17), `state` (keeps that agency's profile pages; editions 16-17), `pages` | all 17 editions; needs pypdf |
| `activity_tables` | value: `report_fy, table, table_number, table_title, page, state, state_abbr, row_type, measure, data_fy, value, value_num, value_flag` | `fiscal_year` (2019-2023, one or a list), `tables` (keys below, default all), `state` | FY 2019-2023; needs pypdf |
| `activity_report` | PDF page: `report_fy, page, table_title, text` | `fiscal_year` (2004-2023), `pages` | every year; national summary and rankings; needs pypdf |

- `--endpoints` lists the policy catalog. Each entry has an alias, the exact dashboard name and its selections.
  Examples:
  - options: `bbce`, `reporting_requirements`, `certification_period_lengths`, `utility_expenses`,
    `employment_training`, `drug_felony`, `tba`, `abawd_exemptions`;
  - waivers: `abawd_waiver`, `telephonic_signature_waiver`, `on_demand_interview_waiver`, `early_denial_waiver`;
  - projects: `cap`, `esap`, `smd`.
- `activity_tables` table keys:
  - participation: `participation_issuance` (FY 2019 only), `average_benefit`, `emergency_allotments`
    (FY 2021-2023), `households_by_benefit_type`, `persons_by_benefit_type`, `issuance_by_benefit_type`;
  - costs: `admin_costs`, `covid_admin_funding` (FY 2021), `admin_costs_per_case`,
    `federal_admin_costs_per_case` (FY 2019-2020), `federal_selected_costs`, `federal_adp_costs`,
    `federal_cert_costs_per_case`;
  - fair hearings and fraud: `fair_hearings`, `adh`, `fraud_investigations`, `prosecutions`, `disqualifications`;
  - claims: `claims_established_dollars`, `claims_established_count`, `claims_collected_by_type`,
    `claims_collected_by_method`;
  - multi-year comparisons, each value with its own `data_fy`: `persons_multiyear`, `households_multiyear`,
    `issuance_multiyear` (three years), `federal_costs_multiyear`, `federal_costs_per_case_multiyear`,
    `federal_cert_costs_multiyear`, `federal_cert_costs_per_case_multiyear`, `fair_hearings_multiyear`,
    `disqualifications_multiyear`, `claims_established_multiyear`, `claims_collected_multiyear` (two years).
- State-level persons, households and total issuance for FY 2020-2023 are in the `*_multiyear` tables. The
  single-year participation table exists only in FY 2019.

## Spec examples
```json
{"description": "Which SNAP agencies use BBCE and have an ABAWD time limit waiver (17th edition)",
 "requests": [{"name": "bbce", "endpoint": "option_states", "params": {"policy": "bbce"}},
              {"name": "abawd_waiver", "endpoint": "option_states", "params": {"policy": "abawd_waiver"}}]}
```
```json
{"description": "Texas and California SNAP policy options, and Texas's full profile with waivers (PDF)",
 "requests": [{"name": "tx_ca", "endpoint": "options_by_state", "params": {"state": ["TX", "CA"]}},
              {"name": "tx_profile", "endpoint": "options_report", "params": {"edition": 17, "state": "TX"}}]}
```
```json
{"description": "SNAP administrative costs and households by state, FY 2022-2023",
 "requests": [{"name": "sar", "endpoint": "activity_tables",
               "params": {"fiscal_year": [2022, 2023], "tables": ["admin_costs", "admin_costs_per_case"]}}]}
```

## Data format and traps
- **Editions are not fiscal years.** The 17th edition (Aug 2025) gives options in place on Oct 1, 2024, from the
  FFY 2025 State Plans. The 16th edition (Jun 2024) is from the FFY 2024 plans. Name the edition with every count.
- **Names change between editions.** Option and selection wording changes from one edition to the next (16th
  "Child support income expense deduction", 17th "Deducted from income"; "entire" vs "whole" household), and
  options come and go in the dashboard: self-employment income and the two noncitizen-income options are 16th
  only; Pledge States, SNAP E&T and nine waivers are 17th only (verified 2026-10-30; the catalog descriptions
  say which). For an option missing from an edition's dashboard, read that edition's PDF. Match by meaning, say so, and never splice editions silently.
- **53 state agencies, not 50 states**: the 50 states, DC, Guam (GU) and the Virgin Islands (VI). The reports' "all
  States" means 53. `agencies_with_selection` and `share_of_agencies` are national counts across the 53
  (the share is a fraction), repeated on each agency's row, not a value for that agency.
- **Several selections per agency**: Certification Period Lengths gives one row per length offered
  ("4,6,12" in `options_by_state`), so states can add up to more than 53.
- **The dashboard is not an API.** The pack checks every exported row against the policy, selection and edition
  asked for: an unknown value makes the dashboard fall back to its default view. If FNA republishes the
  dashboard, the view path changes. The error says so; use `options_report` (the PDF) instead.
- **activity_tables values** are as printed. `value_num` drops `$ , %`. `value_flag` keeps codes: `N/A` (not
  applicable, e.g. no cash issuance), `-`/`--` (none or not reported), `blank` (not printed; a footnote may say
  the data are missing, e.g. DC claims FY 2021). A flag is never a zero.
  - Units: dollars, monthly-average persons or households, counts, and percent (`percent_upheld`,
    `*_percent_change`).
  - Each table also has `row_type = us_total` rows, the printed U.S. line. Drop them before summing agencies.
- **The pack checks each table against the PDF.**
  - A table is kept only if its values line up with its columns and the column labels appear in the printed
    header.
  - A table that fails is dropped, and the fetch reports it as incomplete with the page to read with
    `activity_report`.
  - Agency sums are compared with the printed U.S. total, and the fetch notes any difference. Known published
    differences: FY 2020 emergency-allotment persons and households (the U.S. figure is about 60% of the agency
    sum), FY 2021 cash participation, and the FY 2022 state and federal shares of admin costs (about 0.6%).
  - Report such a difference; do not fix it.
- **Emergency allotments** (pandemic supplements, ended after Feb 2023) are in issuance for FY 2020-2023. The
  reports exclude P-EBT, Disaster SNAP and replacements of stolen benefits.
- **100% federal funding.** ARPA (FY 2021-2023) and CAA 2021 paid 100% federal administrative funding on top of
  the usual 50% share: `arpa_funding` (FY 2022-2023 admin_costs) and `covid_admin_funding` (FY 2021).
- **Mislabeled headers in the source.** The FY 2019-2021 persons multi-year tables print "Households
  Participating" in their column headers. The table title (persons) is right; the pack labels by title.
- **Single-year vs multi-year.** A value can appear in a single-year table and again in a later report's
  multi-year table. They agree where checked (persons, households, issuance, federal costs, FY 2019-2023). Use one
  source per figure.

## Access
- No key. Hosts:
  - www.fna.usda.gov: the PDFs, which redirect to fna-bwbufwdzbabpezgc.z01.azurefd.us;
  - publicdashboards.dl.usda.gov: the Tableau dashboard's CSV exports.
- Plain scripted requests are answered (verified 2026-10-30). The pack sends one request a second.
  `options_by_state` makes one call per two agencies (27 calls for all 53); `option_states` makes one per selection.
- The PDF endpoints need the Python package pypdf. Without it the fetch reports `install_deps`; the dashboard
  endpoints do not need it.
- Manual route: `manual_download/INSTRUCTIONS.md` lists every file. Each PDF is a direct download; each dashboard
  `.csv` link downloads a small CSV. Save under the name shown and upload; `--ingest` parses them the same way.
  Without the bundle:
  - State Options Report PDFs: https://www.fna.usda.gov/snap/waivers/state-options-report
  - State Activity Reports: https://www.fna.usda.gov/pd/snap-state-activity-reports
  - The dashboard (https://www.fna.usda.gov/data-research/data-visualization/snap/state-options) has no download
    button.

## Citation and terms
U.S. Department of Agriculture, Food and Nutrition Administration, SNAP State Options Report, 17th edition (or SNAP
State Activity Report, FY <year>), fna.usda.gov, retrieved <date>. Name the edition (and its as-of date) or the
report year with every number. Public domain (works of a federal agency, 17 U.S.C. 105); redistribution allowed.

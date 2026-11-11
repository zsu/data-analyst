# SBA 7(a) and 504 loan-level data (FOIA files, data.sba.gov)
Depth: standard. Key: none. Every SBA 7(a) and 504 loan approved since FY1991, one row per loan, with its amount,
SBA guaranty, lender, industry, place and outcome (paid in full, charged off with the amount, cancelled), as
published quarterly under FOIA.

## When to use
- Loan performance of SBA's guaranteed lending by approval fiscal year (cohort): how many loans and dollars were
  charged off, paid in full or cancelled, as the default and recovery inputs to a credit subsidy estimate or
  re-estimate (cohorts endpoint), and how that differs by processing method (SBA Express, Preferred Lenders),
  industry, state, loan size or term.
- Program volume: 7(a) gross approvals and guaranteed dollars, 504 debentures, number of loans, by fiscal year,
  state, NAICS industry, lender or CDC; average loan size and guaranty share.
- Loan-level detail for a state, industry or lender (loans endpoint).
- Pair with omb `credit_supplement` (subsidy rates, default assumptions and reestimates by cohort for the SBA
  business loan accounts) to set realized charge-offs against the budget's assumptions.
- Not here: subsidy rates, default assumptions and reestimates (omb, credit_supplement); loan guarantee face value
  and subsidy cost by award (usaspending); disaster loans, PPP and EIDL (separate SBA datasets, not this pack); bank lending in general
  (fdic, fed-board).

## Endpoints and datasets
No key. Files verified live 2026-11-11 (as of 2026-09-30). Each request names `program` (`7a` or `504`) and
approval fiscal years (`approval_fy`, or `start_fy`/`end_fy`).

| Endpoint | Returns (one row per) | Key parameters | Units, frequency, coverage |
|---|---|---|---|
| `loans` | loan: the published columns (7(a): 42, 504: 39) plus `loan_status_code` | `program`, `approval_fy` or `start_fy`/`end_fy`; filters `state` (BorrState), `project_state`, `naics` (prefix), `loan_status` (code), `processing_method`; `columns` | dollars, nominal; FY1991 on; updated quarterly (about a month after the quarter) |
| `cohorts` | ApprovalFY x `group_by` values: `program`, `ApprovalFY`, groups, `as_of`, `loans`, `gross_approval`, `sba_guaranteed` (7(a) only), `loans_disbursed`, `disbursed_gross_approval`, `disbursed_sba_guaranteed` (7(a) only), `loans_pif`, `loans_chgoff`, `loans_cancld`, `loans_undisbursed`, `loans_exempt`, `loans_closed`, `loans_other_status`, `gross_chargeoff_amount` | the same filters, plus `group_by` (BorrState, ProjectState, ProcessingMethod, naics2, NaicsCode, BusinessType, BusinessAge, FixedorVariableInterestInd, TermInMonths, RevolverStatus, SoldSecMrktInd, loan_status_code) | dollars, nominal; computed from every matching loan |

Files (one program, a fiscal-year range each): 7(a) FY1991-1999 (147 MB), FY2000-2009 (319 MB), FY2010-2019
(255 MB), FY2020-present (188 MB); 504 FY1991-2009 (54 MB), FY2010-present (60 MB). A request downloads only the
files its years need, once per run; FY2020 on is one 188 MB download. Verified totals (cohorts, 7(a), as of
2026-09-30): FY2024 70,242 loans, gross approval $31.12 billion, guaranteed $22.84 billion; FY2020 42,298
loans, 36,464 disbursed, 1,590 charged off, $233.4 million gross charged off.

## Spec examples
```json
{"description": "SBA 7(a) loan outcomes by approval fiscal year FY2020-latest",
 "requests": [{"name": "sba7a_cohorts", "endpoint": "cohorts",
   "params": {"program": "7a", "start_fy": 2020}}]}
```

```json
{"description": "504 loans in Colorado approved FY2015, with outcome",
 "requests": [{"name": "co504", "endpoint": "loans",
   "params": {"program": "504", "approval_fy": 2015, "state": "CO",
              "columns": ["ApprovalFY", "GrossApproval", "NaicsCode", "LoanStatus", "ChargeOffDate", "GrossChargeOffAmount"]}}]}
```

```json
{"description": "7(a) charge-offs by processing method, FY2010-2019 cohorts",
 "requests": [{"name": "by_method", "endpoint": "cohorts",
   "params": {"program": "7a", "start_fy": 2010, "end_fy": 2019, "group_by": ["ProcessingMethod"]}}]}
```

## Data format and traps
- **Status is withheld for active loans.** `EXEMPT` (FOIA Exemption 4) means disbursed and not cancelled, paid in
  full or charged off: still outstanding, current or delinquent, unknown which. A cohort's charge-off share is
  cumulative to the as-of date: young cohorts show few charge-offs because few have had time to fail (FY2025
  and FY2026 cohorts are almost all EXEMPT). Compare cohorts at the same age, or say the rate is to date.
- **Base of a rate.** Cancelled (`CANCLD`) and undisbursed (`UNDISBURSED`: 7(a) COMMIT, 504 NOT FUNDED) loans
  never went out; the cohorts endpoint gives `loans_disbursed` and `disbursed_gross_approval` (every status but
  those two) for a charge-off rate on disbursed loans. Say which base you used.
- **GrossChargeOffAmount** is the whole loan balance charged off, guaranteed and unguaranteed parts together, before
  recoveries: it is not SBA's loss. SBA's share is roughly the guaranty percentage (SBAGuaranteedApproval /
  GrossApproval) of it; recoveries are not in the files.
- **GrossApproval** is the approved amount, not the disbursed amount; it counts loans later cancelled (use
  `disbursed_gross_approval` for loans that went out). In 504 files GrossApproval is the SBA/CDC debenture only;
  the bank's first lien is ThirdPartyDollars. 504 files have no SBAGuaranteedApproval.
- **Fiscal years** run October to September (FY2026 = Oct 2025 to Sep 2026); `ApprovalFY` is SBA's own field.
- **Status codes as published differ**: the 7(a) files write paid in full as `P I F`, the 504 files as `PIF`;
  `loan_status_code` unifies them (`loan_status` filters on it). CLOSED (504, no longer used) is kept apart.
- Values are published as text with padding (Program ` 7A`); the pack strips leading and trailing spaces only.
  Dollar amounts are published with one decimal (`1188000.0`) and left as published in `loans`.
- **Snapshot, not history**: each quarter's files replace the last (the as-of date is in the file name and the
  AsOfDate column). Outcomes of old loans change between snapshots; the old snapshot is not kept online.
- Borrower names and addresses are in the files as SBA publishes them; leave them out of answers unless asked.
- Unverified: whether SBA's published annual approval totals (press releases, budget justification) count loans
  later cancelled the way these files do; compare like with like before benchmarking.

## Access
No key or limits are documented; the pack sends one request a second. The dataset page lists the files, and the
pack reads their current names from it. Manual route: open https://data.sba.gov/dataset/7a-504-foia, download the
CSV for the program and years (named in INSTRUCTIONS.md) and upload it unchanged; `--ingest` applies the spec's
filters. The data dictionary is the .xlsx on the same page.

## Citation and terms
"U.S. Small Business Administration, 7(a) and 504 FOIA loan data (as of <date>), data.sba.gov/dataset/7a-504-foia,
retrieved <date>." U.S. government work in the public domain (17 U.S.C. 105); redistribution allowed.

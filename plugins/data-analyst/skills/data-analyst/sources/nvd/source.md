# NIST National Vulnerability Database (NVD CVE API 2.0)
Depth: standard. Key: none. Every published CVE (software vulnerability, CVE-1999 on) with NIST's analysis:
CVSS severity scores, CWE weakness types, affected products (CPE) and CISA Known Exploited Vulnerabilities dates.

## When to use
- Counts of vulnerabilities by period, severity, weakness type (CWE), product or reporting organization;
  one CVE's scores and dates; CVEs that CISA lists as exploited in the wild (KEV) and when they were added.
- Not here: cyber incidents, breaches or crime counts (fbi covers reported crime), federal IT spending
  (usaspending), patent or product data (uspto, sec). The KEV catalog's own fields beyond what NVD copies
  (ransomware use, notes) are in CISA's catalog file, not this pack.

## Endpoints and datasets
| Endpoint | Returns | Parameters |
|---|---|---|
| `cves` | one row per CVE (2,000 a page, up to `max_rows`, default 20,000) | filters below |
| `cve_count` | one row, `total_results`: how many CVEs match (one request, no download) | same filters |

Filters: `cveId`, `keywordSearch` (every word), `keywordExactMatch` (flag: exact phrase), `cpeName` (CPE 2.3
name), `virtualMatchString` (CPE prefix, e.g. `cpe:2.3:o:microsoft:windows_10`), `cweId` (`CWE-79`),
`cvssV3Severity` / `cvssV4Severity` (LOW, MEDIUM, HIGH, CRITICAL), `cvssV2Severity` (LOW, MEDIUM, HIGH),
date pairs `pubStartDate`+`pubEndDate`, `lastModStartDate`+`lastModEndDate`, `kevStartDate`+`kevEndDate`
(YYYY-MM-DD or ISO 8601), `hasKev` (flag: in the KEV catalog), `noRejected` (default true),
`isVulnerable` (flag, with `cpeName`), `sourceIdentifier` (the CNA, e.g. `secure@microsoft.com`).
Flags are given as `true`.

`cves` columns: `cve_id`, `published`, `last_modified`, `vuln_status`, `source_identifier`, `cvss_v3_score`,
`cvss_v3_severity`, `cvss_v3_version`, `cvss_v3_vector`, `cvss_v3_source`, `cvss_v3_type` (Primary = NVD's
own score, Secondary = the CNA's), `cvss_v4_score`, `cvss_v4_severity`, `cvss_v4_source`, `cvss_v2_score`,
`cvss_v2_severity`, `cwe_ids` ("; "-joined), `kev_date_added`, `kev_action_due`, `kev_name`,
`reference_count`, `description` (English).

## Spec examples
```json
{"description": "Log4Shell (CVE-2021-44228): scores, weakness and KEV dates",
 "requests": [{"name": "log4shell", "endpoint": "cves", "params": {"cveId": "CVE-2021-44228"}}]}
```
```json
{"description": "CVEs added to CISA's Known Exploited Vulnerabilities catalog, January-April 2026",
 "requests": [{"name": "kev_2026_jan_apr", "endpoint": "cves",
   "params": {"kevStartDate": "2026-01-01", "kevEndDate": "2026-04-30"}}]}
```
```json
{"description": "Critical (CVSS v3) CVEs published in the first two quarters of 2025",
 "requests": [
  {"name": "crit_2025_q1", "endpoint": "cve_count",
   "params": {"cvssV3Severity": "CRITICAL", "pubStartDate": "2025-01-01", "pubEndDate": "2025-03-31"}},
  {"name": "crit_2025_q2", "endpoint": "cve_count",
   "params": {"cvssV3Severity": "CRITICAL", "pubStartDate": "2025-04-01", "pubEndDate": "2025-06-30"}}]}
```

## Data format and traps
- **120-day windows.** Each date pair covers at most 120 days and needs both ends (the check refuses
  others). A year is four requests (quarters); add the `cve_count` results. Dates are UTC.
- **Flags take no value**: the pack sends a bare `hasKev`; the API refuses `hasKev=true` (HTTP 404).
- **Errors are HTTP 404** with the reason in a response header, so a fetch error that says "not found" on a
  filter query usually means a bad parameter, not a missing CVE.
- **Whose score.** A CVE can carry NVD's score (Primary) and the CNA's (Secondary); they differ. The
  `cvss_v3_*` columns hold the Primary one when NVD scored it, else the first CNA score; `cvss_v3_source`
  says whose. Say which you used.
- **Analysis backlog.** Since 2024 (NVD's own notices) many new CVEs wait in `vuln_status` "Awaiting Analysis" or "Received"
  with no NVD score, so severity counts for recent months undercount and grow as NVD catches up. Report
  the retrieval date and the share without a score.
- **`cvssV4Severity`** matched 0 CVEs for January-April 2025 when checked (2026-10-30) although CNAs publish
  v4 scores in records; do not count by it. Filter on v3, or fetch `cves` and count `cvss_v4_severity`.
- `cvssV3Severity` matches NVD's own (Primary) v3 score only: for each quarter of 2025 its count equalled the
  rows with `cvss_v3_type` Primary and CRITICAL, while counting CNA scores too gave about 1.5-2 times as many
  (checked 2026-10-30). To count any v3 score, fetch `cves` and count `cvss_v3_severity`.
- REJECTED CVEs are left out by default (`noRejected`); set it to false to count them.
- `vuln_status` values (NVD's documentation): Received, Awaiting Analysis, Undergoing Analysis, Analyzed,
  Modified, Deferred, Rejected.
  Records change daily (`last_modified`); counts for a past window drift slightly.
- `kev_date_added` is CISA's date; CVEs with it are in the KEV catalog (1,765 when checked on 2026-10-30).
- Rate limit without a key: 5 requests in 30 seconds. The pack waits about 6 seconds between requests, so a
  20,000-row fetch (10 pages) takes about a minute; many `cve_count` requests take 6 seconds each.

## Access
No key needed (NVD offers an optional key for higher limits; this pack does not use one). Route `api`
when services.nvd.nist.gov answers. Manual route: INSTRUCTIONS.md lists one API link per page (JSON,
2,000 CVEs each); open each in a browser about 6 seconds apart, save it under the given name and upload
the files. Yearly CVE JSON 2.0 feeds (https://nvd.nist.gov/vuln/data-feeds) are large and need filtering by hand.

## Citation and terms
"National Institute of Standards and Technology, National Vulnerability Database, <search description>,
NVD CVE API 2.0 (services.nvd.nist.gov), retrieved <date>." Required notice (NVD terms of use,
https://nvd.nist.gov/developers/terms-of-use): "This product uses data from the NVD API but is not endorsed
or certified by the NVD." NVD data are a U.S. government work; redistribution allowed.

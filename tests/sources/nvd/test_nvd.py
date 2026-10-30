"""Offline tests for the nvd pack. Fixtures were recorded live from services.nvd.nist.gov (CVE API 2.0) on
2026-10-30 with DATA_ANALYST_RECORD."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Log4Shell; KEV total; CWE-79 CVEs published 2025 Q1",
        "requests": [
            {"name": "log4shell", "endpoint": "cves", "params": {"cveId": "CVE-2021-44228"}},
            {"name": "kev_total", "endpoint": "cve_count", "params": {"hasKev": True}},
            {"name": "xss_2025_q1", "endpoint": "cve_count",
             "params": {"cweId": "CWE-79", "pubStartDate": "2025-01-01", "pubEndDate": "2025-03-31"}}]}


class TestNvd(PackTest):
    source = "nvd"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        row = self.csv(out, "log4shell")[0]
        self.assertEqual(row["cve_id"], "CVE-2021-44228")
        self.assertEqual(row["cvss_v3_score"], "10.0")
        self.assertEqual(row["cvss_v3_severity"], "CRITICAL")
        self.assertEqual(row["cvss_v3_type"], "Primary")
        self.assertEqual(row["kev_date_added"], "2021-12-10")
        self.assertIn("CWE-502", row["cwe_ids"])
        self.assertEqual(self.csv(out, "kev_total")[0]["total_results"], "1765")
        self.assertEqual(self.csv(out, "xss_2025_q1")[0]["total_results"], "319")

    def test_date_ranges_are_checked(self):
        for prm in ({"pubStartDate": "2025-01-01", "pubEndDate": "2025-06-30"},      # over 120 days
                    {"pubStartDate": "2025-01-01"},                                    # one end only
                    {"kevStartDate": "2025-03-01", "kevEndDate": "2025-02-01"},        # reversed
                    {"cvssV3Severity": "SEVERE", "cweId": "CWE-79"}):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "cve_count", "params": prm}]})
            self.assertEqual(r["route"], "fix_spec", (prm, r))

    def test_unfiltered_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "cves", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_flags_are_bare_in_manual_links(self):
        sp, out = self.write_spec({"requests": [{"name": "k", "endpoint": "cves", "params": {"hasKev": True}}]})
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        url = r["items"][0]["url"]
        self.assertIn("hasKev", url)
        self.assertNotIn("hasKev=", url)
        self.assertNotIn("true", url.lower().split("?", 1)[1])


if __name__ == "__main__":
    unittest.main()

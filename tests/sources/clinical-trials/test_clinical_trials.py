"""Offline tests for the clinical-trials pack. Fixtures were recorded live from clinicaltrials.gov/api/v2 on
2026-10-20 with DATA_ANALYST_RECORD."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Recruiting phase 3 sickle cell trials; registry-wide studies by phase",
        "requests": [
            {"name": "scd_phase3_recruiting", "endpoint": "studies",
             "params": {"query.cond": "sickle cell", "filter.overallStatus": ["RECRUITING"], "phase": "PHASE3"}},
            {"name": "phase_counts", "endpoint": "field_values", "params": {"fields": "Phase"}}]}


class TestClinicalTrials(PackTest):
    source = "clinical-trials"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "scd_phase3_recruiting")
        self.assertEqual(len(rows), 13)
        first = rows[0]
        self.assertEqual(first["nctId"], "NCT06609226")
        self.assertEqual(first["leadSponsor.name"], "Novo Nordisk A/S")
        self.assertEqual(first["enrollmentInfo.count"], "480")
        self.assertTrue(all(x["phases"].startswith("PHASE3") or "PHASE3" in x["phases"] for x in rows))
        ph = {x["value"]: x["studiesCount"] for x in self.csv(out, "phase_counts")}
        self.assertEqual(ph["EARLY_PHASE1"], "6510")

    def test_bad_phase_and_status_are_fix_spec(self):
        for prm in ({"query.cond": "asthma", "phase": "PHASE5"},
                    {"query.cond": "asthma", "filter.overallStatus": "recruiting"}):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "studies", "params": prm}]})
            self.assertEqual(r["route"], "fix_spec", r)

    def test_unfiltered_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "studies", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_link_has_aggfilters(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        urls = [i["url"] for i in r["items"]]
        self.assertTrue(any("aggFilters=phase%3A3" in u for u in urls), urls)


if __name__ == "__main__":
    unittest.main()

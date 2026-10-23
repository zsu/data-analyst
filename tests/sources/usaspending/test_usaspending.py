"""Offline tests for the usaspending pack. Fixtures were recorded live from api.usaspending.gov on
2026-10-20: r001 Maryland obligations by awarding agency FY2024 (POST, body paging), r002 NASA monthly
obligations FY2024, r003 HHS budgetary resources, r004 broadband grants (5 fields), r005 the HTTP 404
for a toptier code that does not exist."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Federal obligations in Maryland by awarding agency, FY2024",
        "requests": [{"name": "md_by_agency_fy2024", "endpoint": "by_category",
                      "params": {"category": "awarding_agency", "fiscal_year": 2024, "state": "Maryland"},
                      "max_rows": 10},
                     {"name": "nasa_monthly_fy2024", "endpoint": "over_time",
                      "params": {"group": "month", "fiscal_year": 2024, "agency": "nasa"}},
                     {"name": "hhs_budget", "endpoint": "agency_budgetary_resources", "params": {"toptier_code": "hhs"}},
                     {"name": "broadband_grants", "endpoint": "awards",
                      "params": {"award_type": "grants", "fiscal_year": 2024, "keyword": "broadband"}, "max_rows": 5}]}
AWARDS = {"description": "Broadband grants active in FY2024, largest first",
          "requests": [{"name": "broadband_grants", "endpoint": "awards",
                        "params": {"award_type": "grants", "fiscal_year": 2024, "keyword": "broadband",
                                   "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
                                              "Start Date", "generated_internal_id"]},
                        "max_rows": 5}]}


class TestUsaspending(PackTest):
    source = "usaspending"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_category_time_and_budget(self):
        spec = {"description": SPEC["description"], "requests": SPEC["requests"][:3]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "md_by_agency_fy2024")
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["code"], "SSA")
        self.assertEqual(rows[0]["amount"], "26899943426.5")
        months = self.csv(out, "nasa_monthly_fy2024")
        self.assertEqual(len(months), 12)
        self.assertEqual((months[0]["fiscal_period"], months[0]["period_start"]), ("FY2024 M01", "2023-10"))
        budget = {b["fiscal_year"]: b for b in self.csv(out, "hhs_budget")}
        self.assertEqual(budget["2026"]["agency_budgetary_resources"], "3709407922072.44")

    def test_fetch_awards(self):
        code, r, out = self.fetch(AWARDS)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "broadband_grants")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["generated_internal_id"], "ASST_NON_4820B328_013")
        self.assertTrue(rows[0]["award_url"].endswith("ASST_NON_4820B328_013"))

    def test_errors_are_not_swallowed(self):
        bad = {"requests": [{"name": "bad", "endpoint": "agency_budgetary_resources", "params": {"toptier_code": "999"}}]}
        code, r, out = self.fetch(bad)
        self.assertEqual(code, 2, r)
        self.assertEqual(r["kind"], "not_found")

    def test_fiscal_year_floor_and_mixed_award_types(self):
        old = {"requests": [{"name": "old", "endpoint": "by_category",
                             "params": {"category": "recipient", "fiscal_year": 2007}}]}
        code, r = self.check(old)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("FY2008", r["message"])
        mixed = {"requests": [{"name": "mixed", "endpoint": "awards",
                               "params": {"award_type": ["A", "02"], "fiscal_year": 2024}}]}
        code, r = self.check(mixed)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("one group", r["message"])

    def test_unknown_agency_did_you_mean(self):
        bad = {"requests": [{"name": "x", "endpoint": "by_category",
                             "params": {"category": "recipient", "fiscal_year": 2024, "agency": "Department"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("Did you mean", r["message"])

    def test_manual_bundle_posts_with_body_paging(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(r["dir"], "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        first = items[0]
        self.assertEqual(first["method"], "POST")
        self.assertEqual((first["body"]["page"], first["body"]["limit"]), (1, 100))
        self.assertEqual(first["body"]["filters"]["time_period"], [{"start_date": "2023-10-01", "end_date": "2024-09-30"}])
        self.assertEqual(first["body"]["filters"]["place_of_performance_locations"], [{"country": "USA", "state": "MD"}])
        budget = [i for i in items if i["name"].startswith("hhs_budget#")][0]
        self.assertTrue(budget["url"].endswith("/agency/075/budgetary_resources/"))


if __name__ == "__main__":
    unittest.main()

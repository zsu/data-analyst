import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Leading causes of death, New York, 2017",
        "requests": [{"name": "ny_causes_2017", "endpoint": "query",
                      "params": {"dataset": "leading_death", "$where": "state = 'New York' AND year = '2017'",
                                 "$order": "deaths DESC"}}]}


class TestCdc(PackTest):
    source = "cdc"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "ny_causes_2017")
        self.assertEqual(rows[0]["cause_name"], "All causes")
        self.assertEqual(rows[0]["deaths"], "155358")
        self.assertTrue(os.path.exists(os.path.join(out, "data", "cdc", "README.md")))

    def test_places_city_is_current_places_long_format(self):
        # Fixtures recorded live from data.cdc.gov on 2026-10-22: places_city must point at the current PLACES
        # place file (long format), not the 2019 500 Cities file (wide <measure>_crudeprev columns).
        spec = {"requests": [{"name": "places_place_obesity", "endpoint": "query",
                              "params": {"dataset": "places_city", "$where": "stateabbr = 'VT' AND locationname = "
                                         "'Burlington' AND measureid = 'OBESITY'"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "places_place_obesity")
        self.assertIn("measureid", rows[0])
        self.assertIn("data_value", rows[0])
        self.assertNotIn("obesity_crudeprev", rows[0])
        self.assertEqual({x["data_value_type"] for x in rows}, {"Crude prevalence", "Age-adjusted prevalence"})

    def test_weekly_deaths_group_filter_with_backticks(self):
        # `group` is a SoQL reserved word; filtered with backticks it returns one granularity only.
        spec = {"requests": [{"name": "vt_deaths_by_year", "endpoint": "query",
                              "params": {"dataset": "weekly_deaths", "$select": "`group`, year, state, total_deaths",
                                         "$where": "`group` = 'By Year' AND state = 'Vermont'", "$order": "year"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "vt_deaths_by_year")
        self.assertEqual({x["group"] for x in rows}, {"By Year"})
        self.assertEqual(rows[0]["year"], "2020")
        self.assertEqual(rows[0]["total_deaths"], "5983")

    def test_reuse_existing(self):
        code, r, out = self.fetch(SPEC)
        code, r = self.cli(os.path.join(out, "spec.json"), "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing", r)

    def test_unknown_alias_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "nope", "params": {}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        self.assertTrue(os.path.exists(r["instructions"]))


if __name__ == "__main__":
    unittest.main()

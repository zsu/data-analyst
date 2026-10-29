"""Offline tests for the dol pack. No DOL_API_KEY was available, so data queries are NOT verified live.

Fixtures (fixtures/index.json):
- recorded live on 2026-10-20 (keyless endpoints): the Datasets API catalog pages 1-5 (r001-r005), the
  dataset descriptions of ETA ui_national_weekly_claims (10305, r006) and OSHA inspection (10334, r007),
  and the HTTP 401 the data API returns without a key (r008).
- doc_ui_national_preview.json and doc_osha_inspection_preview.json: NOT recorded data responses. They are
  the `dataset_preview` that the DOL Datasets API publishes for each dataset (the first rows of the
  table in the API's {"data": [...]} envelope), copied from r006/r007 and served for the matching
  unfiltered query.
"""
import json
import os
import sys
import unittest
import urllib.parse

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

KEY = {"DOL_API_KEY": "test-key-not-real-0000"}
UI = {"description": "Weekly UI claims, national, first 10 weeks on file (documented preview)",
      "requests": [{"name": "ui_national", "endpoint": "query",
                    "params": {"dataset": "eta_ui_national_weekly_claims", "sort": "asc"}, "max_rows": 10}]}
WHD = {"description": "WHD enforcement cases in Texas, newest first",
       "requests": [{"name": "whd_tx", "endpoint": "query", "params": {"dataset": "whd_enforcement", "state": "tx"}}]}


class TestDol(PackTest):
    source = "dol"
    sample_spec = WHD

    def test_check_data_query_needs_key(self):
        code, r = self.check(WHD)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "run_setup", r)
        self.assertTrue(r["key_only_needs"])

    def test_check_catalog_is_keyless_api(self):
        code, r = self.check({"requests": [{"name": "cat", "endpoint": "datasets", "params": {}}]})
        self.assertEqual(r["route"], "api", r)                    # the catalog is keyless

    def test_keyless_data_query_is_blocked(self):
        code, r, out = self.fetch(WHD)
        self.assertEqual(code, 4, r)
        self.assertEqual(r["kind"], "key_missing")

    def test_catalog_and_field_dictionary_live(self):
        spec = {"requests": [{"name": "catalog", "endpoint": "datasets", "params": {}},
                             {"name": "ui_fields", "endpoint": "dataset_info",
                              "params": {"dataset_id": "eta_ui_national_weekly_claims"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        cat = self.csv(out, "catalog")
        self.assertEqual(len(cat), 45)
        self.assertIn("ui_state_weekly_claims", [c["api_url"] for c in cat])
        fields = {f["column_name"]: f["column_desc"] for f in self.csv(out, "ui_fields")}
        self.assertIn("Seasonally adjusted count of total initial claims", fields["c5"])

    def test_fetch_documented_preview(self):
        code, r, out = self.fetch(UI, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "ui_national")
        self.assertEqual(len(rows), 10)
        self.assertEqual((rows[0]["rptdate"], rows[0]["c4"]), ("1967-01-07", "346000"))
        self.assertTrue(any("c5 = Seasonally adjusted count of total initial claims" in n for n in r["notes"]), r)

    def test_osha_preview_in_api_order(self):
        spec = {"requests": [{"name": "osha", "endpoint": "query",
                              "params": {"dataset": "OSHA/inspection", "sort_by": ""}, "max_rows": 10}]}
        code, r, out = self.fetch(spec, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "osha")
        self.assertEqual(rows[0]["estab_name"], "CHRISTMAS TREE SHOPS")
        self.assertEqual(rows[0]["site_state"], "NH")

    def test_filter_object_and_case_tolerant_search(self):
        spec = {"requests": [{"name": "amazon", "endpoint": "query",
                              "params": {"dataset": "osha_inspection", "state": "ca", "search": {"estab_name": "Amazon"},
                                         "filters": {"insp_type": ["A", "B"], "open_date": {"gt": "2024-01-01"}}},
                              "max_rows": 2000}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        items = r["manual_items"]
        self.assertEqual(len(items), 2)                            # two pages of 1,000
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(items[1]["url"]).query)
        self.assertEqual((q["limit"], q["offset"], q["sort_by"], q["sort"]), (["1000"], ["1000"], ["open_date"], ["desc"]))
        fo = json.loads(q["filter_object"][0])
        conds = fo["and"]
        self.assertIn({"field": "insp_type", "operator": "in", "value": ["A", "B"]}, conds)
        self.assertIn({"field": "open_date", "operator": "gt", "value": "2024-01-01"}, conds)
        self.assertIn({"field": "site_state", "operator": "eq", "value": "CA"}, conds)
        like = [c for c in conds if "or" in c][0]["or"]
        self.assertEqual([c["value"] for c in like], ["%Amazon%", "%AMAZON%", "%amazon%"])
        self.assertTrue(items[0]["url"].startswith("https://apiprod.dol.gov/v4/get/OSHA/inspection/json?"))
        self.assertTrue(items[0]["needs_key"])

    def test_bad_specs(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "query", "params": {"dataset": "nope"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        code, r = self.check({"requests": [{"name": "x", "endpoint": "query",
                                            "params": {"dataset": "osha_violation", "state": "TX"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("state", r["message"])

    def test_unknown_field_names_are_fix_spec_without_a_key(self):
        # the field dictionary (r007, dataset 10334) is keyless: a filter, search or sort on a field the dataset lacks
        # is caught by --check (fix_spec), not after the user downloads a bundle of failing links
        bad = {"requests": [{"name": "x", "endpoint": "query",
                             "params": {"dataset": "osha_inspection", "state": "NH",
                                        "filters": {"site_district": "NORTH", "insp_type": "B"},
                                        "search": {"establishment": "acme"}}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("'site_district'", r["message"])
        self.assertIn("'establishment'", r["message"])
        self.assertNotIn("'insp_type'", r["message"].split("its fields")[0])
        self.assertIn("site_city", r["message"])                    # the real fields are listed
        bad["requests"][0]["check_fields"] = False                  # the escape when the dictionary is incomplete
        code, r = self.check(bad)
        self.assertEqual(r["route"], "run_setup", r)
        good = {"requests": [{"name": "x", "endpoint": "query",
                              "params": {"dataset": "osha_inspection", "state": "NH", "filters": {"site_city": "KEENE"}}}]}
        code, r = self.check(good)
        self.assertEqual(r["route"], "run_setup", r)
        # run_setup names the pack's keyless way to the same data and the command that packages such a file
        self.assertIn("https://", r["keyless_alternative"])
        self.assertIn("--ingest", r["ingest_command"])
        self.assertIn("ingest_command", r["action"])

    def test_search_catalog(self):
        code, r = self.cli("--search", "unemployment insurance weekly claims state")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["results"][0]["id"], "ETA/ui_state_weekly_claims")


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the cms pack. Fixtures r001-r003 were recorded live on 2026-10-20 with
DATA_ANALYST_RECORD (provider-data datastore query; data-api Geographic Variation); r004-r005 on 2026-10-22
(monthly enrollment and Part D prescribers, suppressed "*" cells)."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Vermont hospitals: overall star rating; Medicare FFS standardized spending per capita by state, 2024",
        "requests": [
            {"name": "vt_hospitals", "endpoint": "provider",
             "params": {"dataset": "hospital_info", "where": {"state": "VT"},
                        "columns": ["facility_id", "facility_name", "citytown", "hospital_type", "hospital_overall_rating"],
                        "sort": "facility_id"}},
            {"name": "ffs_spending_2024", "endpoint": "medicare",
             "params": {"dataset": "geo_variation", "where": {"YEAR": "2024", "BENE_GEO_LVL": "State", "BENE_AGE_LVL": "All"},
                        "columns": ["YEAR", "BENE_GEO_LVL", "BENE_GEO_DESC", "BENES_OM_CNT", "TOT_MDCR_STDZD_PYMT_PC",
                                    "TOT_MDCR_PYMT_PC"]}}]}

BAD = {"description": "unknown filter column", "requests": [
    {"name": "bad_filter", "endpoint": "medicare", "max_rows": 3,
     "params": {"dataset": "geo_variation", "where": {"STATE": "VT"}, "columns": ["YEAR", "BENE_GEO_DESC"]}}]}


class TestCms(PackTest):
    source = "cms"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "vt_hospitals")
        self.assertEqual(len(rows), 17)
        self.assertEqual(rows[0]["facility_id"], "470001")
        self.assertEqual(rows[0]["hospital_overall_rating"], "3")
        geo = {g["BENE_GEO_DESC"]: g for g in self.csv(out, "ffs_spending_2024")}
        self.assertEqual(geo["AK"]["TOT_MDCR_STDZD_PYMT_PC"], "9209.46")
        self.assertEqual(geo["AK"]["BENE_AGE_LVL"], "All")         # filter columns are always returned

    def test_unknown_filter_column_fails(self):
        code, r, out = self.fetch(BAD)
        self.assertEqual(code, 2, r)
        self.assertIn("STATE", r["message"])

    def test_suppressed_star_cells_are_flagged(self):
        # r004-r005 recorded live on 2026-10-22: data-api publishes suppressed cells as "*"; the pack empties them
        # and adds <column>_flag, while columns that are flags themselves (GE65_Sprsn_Flag) keep their "*".
        spec = {"requests": [
            {"name": "wy_county_enroll_2022", "endpoint": "medicare",
             "params": {"dataset": "monthly_enrollment",
                        "where": {"BENE_GEO_LVL": "County", "BENE_STATE_ABRVTN": "WY", "YEAR": "2022", "MONTH": "Year"},
                        "columns": ["BENE_COUNTY_DESC", "TOT_BENES", "NATIND_TOT_BENES", "AGE_LT_25_BENES"]}},
            {"name": "partd_flagged", "endpoint": "medicare", "max_rows": 2,
             "params": {"dataset": "partd_prescribers_geo_2024",
                        "where": {"Prscrbr_Geo_Lvl": "State", "GE65_Sprsn_Flag": "*"},
                        "columns": ["Prscrbr_Geo_Desc", "Brnd_Name", "Tot_Clms", "GE65_Tot_Clms"]}}]}
        code, r, out = self.fetch(spec)
        self.assertIn(code, (0, 3), r)
        rows = self.csv(out, "wy_county_enroll_2022")
        self.assertFalse(any(v == "*" for x in rows for v in x.values()))
        unknown = next(x for x in rows if x["BENE_COUNTY_DESC"] == "Unknown")
        self.assertEqual(unknown["TOT_BENES"], "")
        self.assertEqual(unknown["TOT_BENES_flag"], "suppressed (*)")
        albany = next(x for x in rows if x["BENE_COUNTY_DESC"] == "Albany County")
        self.assertEqual((albany["TOT_BENES"], albany["TOT_BENES_flag"]), ("5784", ""))
        flagged = self.csv(out, "partd_flagged")
        self.assertEqual({x["GE65_Sprsn_Flag"] for x in flagged}, {"*"})
        self.assertNotIn("GE65_Sprsn_Flag_flag", flagged[0])

    def test_unknown_sort_column_is_named(self):
        # Provider Data answers HTTP 400 "Column not found." without saying which (r007, recorded 2026-10-28); the
        # pack reads the dataset's columns (r008) and names the unknown one
        spec = {"requests": [{"name": "vt_hosp_badsort", "endpoint": "provider",
                              "params": {"dataset": "hospital_info", "where": {"state": "VT"}, "sort": "ccn",
                                         "columns": ["facility_id", "facility_name"]}}]}
        code, r, _ = self.fetch(spec)
        self.assertEqual((code, r["kind"]), (2, "bad_request"), r)
        self.assertIn("['ccn']", r["message"])
        self.assertIn("facility_id", r["message"])

    def test_bad_operator_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "provider",
                             "params": {"dataset": "hospital_info", "conditions": [{"property": "state", "value": "VT",
                                                                                     "operator": "~"}]}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_numeric_text_comparison_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "provider",
                             "params": {"dataset": "hospital_info", "conditions": [
                                 {"property": "mort_group_measure_count", "value": "5", "operator": ">"}]}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("TEXT", r["message"])

    def test_medicare_list_filter_is_one_in_group(self):
        # r006 recorded live on 2026-10-28: repeated filter[COL]= parameters keep only the last value, so a list
        # in `where` must reach the data-api as one IN condition group and return a row for every value
        spec = {"requests": [{"name": "three_states", "endpoint": "medicare",
                              "params": {"dataset": "geo_variation",
                                         "where": {"YEAR": "2022", "BENE_GEO_LVL": "State", "BENE_AGE_LVL": "All",
                                                   "BENE_GEO_DESC": ["VT", "NH", "ME"]},
                                         "columns": ["YEAR", "BENE_GEO_DESC", "BENES_TOTAL_CNT"]}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        self.assertEqual(sorted(x["BENE_GEO_DESC"] for x in self.csv(out, "three_states")), ["ME", "NH", "VT"])
        sp, out = self.write_spec(spec, "manual.json")
        code, r = self.cli(sp, "--out", out, "--dry-run")
        url = r["manual_items"][0]["url"]
        self.assertIn("filter%5Bin3%5D%5Bcondition%5D%5Boperator%5D=IN", url)
        self.assertNotIn("filter%5BBENE_GEO_DESC%5D", url)

    def test_medicare_rows_outside_the_filter_fail(self):
        from testkit import load_pack_module
        ad = load_pack_module("cms")
        ep = {"path": "/data-api/v1/dataset/{dataset}/data"}
        req = {"name": "x", "params": {"where": {"STATE": ["AK", "HI"]}}}
        self.assertEqual(len(ad.normalize(req, ep, [{"STATE": "ak"}, {"STATE": "HI"}])), 2)   # API matches any case
        with self.assertRaises(Exception) as cm:
            ad.normalize(req, ep, [{"STATE": "AK"}, {"STATE": "WA"}])
        self.assertIn("did not apply the filter", str(cm.exception))
        self.assertTrue(ad.check_spec({"requests": [{"name": "e", "params": {"where": {"STATE": []}}}]}))

    def test_manual_links_carry_filters(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(r["dir"], "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertTrue(any("conditions%5B0%5D%5Bproperty%5D=state" in u for u in urls), urls)
        self.assertTrue(any("filter%5BYEAR%5D=2024" in u for u in urls), urls)


if __name__ == "__main__":
    unittest.main()

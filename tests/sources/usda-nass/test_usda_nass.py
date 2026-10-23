"""Offline tests for the usda-nass pack.

No USDA_NASS_API_KEY was available, so the fixtures are the example outputs printed on the Quick Stats
API page (https://quickstats.nass.usda.gov/api, read 2026-10-20), saved as doc_*.json. The api_GET
example there writes {data:[...]} with an unquoted key; it is stored as valid JSON. They are not live
recordings. A placeholder key is passed only so the client takes the keyed route; clean URLs drop it.
"""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

KEY = {"USDA_NASS_API_KEY": "placeholder-not-a-real-key"}
SPEC = {"description": "Corn in Virginia since 2012 (the documented example), its row count, and the sector names",
        "requests": [
            {"name": "corn_va", "endpoint": "query", "params": {"commodity_desc": "corn", "year__GE": 2012, "state_alpha": "va"}},
            {"name": "corn_va_count", "endpoint": "count", "params": {"commodity_desc": "CORN", "year__GE": 2012, "state_alpha": "VA"}},
            {"name": "sectors", "endpoint": "param_values", "params": {"param": "sector_desc"}}]}


class TestUsdaNass(PackTest):
    source = "usda-nass"
    sample_spec = SPEC

    def test_check_without_key_asks_for_setup(self):
        code, r = self.check(SPEC, env={"DATA_ANALYST_ENV": "local"})
        self.assertEqual(r["route"], "run_setup", r)
        self.assertEqual(r["key_env"], "USDA_NASS_API_KEY")

    def test_check_with_key_is_api(self):
        code, r = self.check(SPEC, env=KEY)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch_doc_examples(self):
        code, r, out = self.fetch(SPEC, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "corn_va")                     # lower-case filters were upper-cased
        self.assertEqual(rows[0]["short_desc"], "CORN - ACRES PLANTED")
        self.assertEqual(rows[0]["Value"], "510,000")
        self.assertEqual(rows[0]["value_num"], "510000")
        self.assertEqual(rows[0]["value_flag"], "")
        self.assertEqual(self.csv(out, "corn_va_count")[0]["count"], "27")
        sec = self.csv(out, "sectors")
        self.assertEqual([x["value"] for x in sec][:2], ["ANIMALS & PRODUCTS", "CROPS"])
        self.assertEqual(sec[0]["param"], "sector_desc")
        with open(os.path.join(out, "data", "usda-nass", "README.md"), encoding="utf-8") as f:
            self.assertNotIn("placeholder-not-a-real-key", f.read())

    def test_suppression_codes(self):
        from core import packs
        ad = packs.load("usda-nass").adapter
        rows = ad.normalize({"endpoint": "query"}, {}, [{"Value": "(D)"}, {"Value": "1,234.5"}, {"Value": "(Z)", "CV (%)": "12.3"}])
        self.assertEqual([(r["value_num"], r["value_flag"]) for r in rows], [("", "D"), ("1234.5", ""), ("", "Z")])
        self.assertEqual(rows[2]["cv_num"], "12.3")
        self.assertIn("exceeds limit", ad.check_error(200, b'{"error":["exceeds limit = 50000"]}'))

    def test_unfiltered_query_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "query", "params": {"state_alpha": "IA"}}]}
        code, r = self.check(bad, env=KEY)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_links_csv_with_key_placeholder(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        q = items[0]
        self.assertIn("format=CSV", q["url"])
        self.assertIn("key={API_KEY}", q["url"])
        self.assertIn("commodity_desc=CORN", q["url"])
        self.assertTrue(q["filename"].endswith(".csv"))


if __name__ == "__main__":
    unittest.main()

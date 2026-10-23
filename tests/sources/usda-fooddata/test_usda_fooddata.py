"""Offline tests for the usda-fooddata pack. Fixtures were recorded live from api.nal.usda.gov on 2026-10-20
with DATA_ANALYST_RECORD (the key was dropped from the recorded URLs; responses hold no key)."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Cheddar cheese (Foundation) nutrients; energy, protein and fat of two cheeses",
        "requests": [
            {"name": "cheddar_foundation", "endpoint": "search", "params": {"query": "cheddar", "dataType": ["Foundation"]}},
            {"name": "two_cheeses", "endpoint": "foods", "params": {"fdcIds": [328637, 169901], "nutrients": "203,204,208"}}]}


class TestUsdaFooddata(PackTest):
    source = "usda-fooddata"
    sample_spec = SPEC

    def test_check_api_keyless_demo(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        self.assertEqual(r["key_required"], "optional")

    def test_fetch_long_layout(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "cheddar_foundation")
        self.assertEqual(len(rows), 105)
        energy = [x for x in rows if x["nutrient_number"] == "208"]
        self.assertEqual(energy[0]["value"], "408")
        self.assertEqual(energy[0]["unit"], "KCAL")
        kj = [x for x in rows if x["nutrient_number"] == "268"]
        self.assertEqual(kj[0]["unit"], "kJ")                      # Energy appears twice, in two units
        two = self.csv(out, "two_cheeses")
        prot = {x["fdcId"]: x["value"] for x in two if x["nutrient_number"] == "203"}
        self.assertEqual(prot["328637"], "23.3")
        self.assertEqual(prot["169901"], "16.7")

    def test_foods_layout(self):
        spec = {**SPEC, "requests": [{**SPEC["requests"][1], "layout": "foods"}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "two_cheeses")
        self.assertEqual(len(rows), 2)
        self.assertIn("foodNutrients", rows[0])

    def test_too_many_ids_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "foods", "params": {"fdcIds": list(range(21))}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        # the API silently drops ids past 50 (live 2026-10-22), so a long comma-separated list must fail too
        code, r = self.check({"requests": [{"name": "y", "endpoint": "foods",
                                            "params": {"fdcIds": ",".join(str(1000 + i) for i in range(55))}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("split", str(r))

    def test_manual_links_carry_demo_key(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(r["dir"], "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertTrue(all(u.count("api_key=DEMO_KEY") == 1 for u in urls), urls)


if __name__ == "__main__":
    unittest.main()

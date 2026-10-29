import json
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from api.gsa.gov (CALC+ ceiling rates v3, keyless) on 2026-10-20 (r006, r007 on 2026-10-22).
SPEC = {"description": "Data scientist ceiling rates, master's degree, small business",
        "requests": [{"name": "ds_ma_small", "endpoint": "rates",
                      "params": {"keyword": "data scientist", "education_level": "MA", "business_size": "S"},
                      "max_rows": 30},
                     {"name": "ds_stats", "endpoint": "rate_stats", "params": {"keyword": "data scientist"}},
                     {"name": "ds_facets", "endpoint": "rate_facets",
                      "params": {"keyword": "data scientist", "worksite": "customer"}},
                     {"name": "ds_titles", "endpoint": "suggest_labor_category", "params": {"prefix": "data sci"}},
                     {"name": "ds_phd_export", "endpoint": "rates_export",
                      "params": {"keyword": "data scientist", "education_level": "PHD", "price_range": "150,200"}}]}


class TestGsaCalc(PackTest):
    source = "gsa-calc"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rates = self.csv(out, "ds_ma_small")
        self.assertEqual(len(rates), 30)
        self.assertEqual(rates[0]["labor_category"], "Data Scientist")
        self.assertEqual(rates[0]["current_price"], "90.0")
        self.assertNotIn("_index", rates[0])
        self.assertTrue(all(x["education_level"] == "Masters" and x["business_size"] == "S" for x in rates))
        self.assertTrue(any("10,000" in n for n in r["notes"]))
        st = self.csv(out, "ds_stats")[0]
        self.assertEqual(st["count"], "1470")
        self.assertTrue(st["mean"].startswith("175.20"))
        facets = self.csv(out, "ds_facets")
        ws = {x["key"]: x["doc_count"] for x in facets if x["facet"] == "worksite"}
        self.assertEqual(ws, {"Customer_Facility": "708"})            # worksite filter took effect
        self.assertEqual(self.csv(out, "ds_titles")[0]["key"], "Data Scientist")
        ex = self.csv(out, "ds_phd_export")
        self.assertEqual(len(ex), 3)
        self.assertEqual(ex[0]["idv_piid"], "47QRAA25D0009")          # export columns renamed
        self.assertEqual(ex[0]["business_size"], "S")                 # "small business" recoded
        self.assertEqual(ex[0]["current_price"], "176.75")

    def test_bad_filter_value_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "rates",
                             "params": {"keyword": "analyst", "worksite": "Both"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_empty_export_is_no_rows(self):
        # r006 (recorded 2026-10-22): an export with no matches is only the SEARCH/FILTER VALUES preamble.
        spec = {"requests": [{"name": "none_export", "endpoint": "rates_export",
                              "params": {"keyword": "qzxv nonexistent title", "worksite": "virtual"}}]}
        code, r, out = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertIn("returned no rows", r["message"])
        from core import packs
        parse = packs.load("gsa-calc").adapter.parse_export
        self.assertEqual(parse("SEARCH VALUES\r\nsearch\r\nvendor_name:NOBODY\r\n\r\n"), [])
        self.assertEqual(parse(""), [])

    def test_export_shortfall_is_noted(self):
        # the export writes rates identical in every exported column once (live 2026-10-28: 1,504 lines for 1,516
        # matches): one count request says by how many
        from core import packs
        pack = packs.load("gsa-calc")
        asked = []

        class Resp:
            def json(self):
                return {"aggregations": {"wage_stats": {"count": 52}}}

        class Client:
            def get(self, path, query):
                asked.append(query)
                return Resp()
        note = pack.adapter.export_shortfall(Client(), pack, {"export": "y", "keyword": "analyst"}, 50)
        self.assertIn("the export has 50 rows but the search matches 52", note)
        self.assertNotIn("export", asked[0])
        self.assertEqual((asked[0]["page_size"], asked[0]["keyword"]), (0, "analyst"))
        self.assertIsNone(pack.adapter.export_shortfall(Client(), pack, {"keyword": "analyst"}, 52))

    def test_406_is_explained(self):
        # r007 (recorded 2026-10-22): ordering by a field the API cannot sort on answers 406, empty body.
        spec = {"requests": [{"name": "bad_order", "endpoint": "rates", "max_rows": 5,
                              "params": {"keyword": "economist", "ordering": "sin"}}]}
        code, r, out = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertEqual(r["kind"], "bad_request", r)
        self.assertIn("page past row 10,000", r["message"])

    def test_ignored_filter_or_search_field_is_fix_spec(self):
        for params in ({"keyword": "analyst", "filter": ["schedule:MAS"]},
                       {"keyword": "analyst", "filter": "vendor_name:ACME"},
                       {"keyword": "analyst", "filter": ["sin:541611", "nonsense"]},
                       {"search": "category:Professional Services"}):
            bad = {"requests": [{"name": "x", "endpoint": "rates", "params": params}]}
            code, r = self.check(bad)
            self.assertEqual(r["route"], "fix_spec", (params, r))
        ok = {"requests": [{"name": "x", "endpoint": "rates",
                            "params": {"search": "vendor_name:ACME", "filter": ["subcategory:IT Services"]}}]}
        code, r = self.check(ok)
        self.assertEqual(r["route"], "api", r)

    def test_manual_export_and_ingest(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        first = items[0]
        self.assertEqual(first["filename"], "ds_ma_small.csv")
        self.assertIn("export=y", first["url"])
        self.assertIn("filter=education_level%3AMA", first["url"])
        # ingest the recorded export CSV as if the user had downloaded it
        one = {"requests": [SPEC["requests"][4]]}
        sp, out = self.write_spec(one, "one.json")
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "r005.csv"), os.path.join(up, "ds_phd_export.csv"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual(self.csv(out, "ds_phd_export")[0]["vendor_name"], "RANCHO BIOSCIENCES, LLC")


if __name__ == "__main__":
    unittest.main()

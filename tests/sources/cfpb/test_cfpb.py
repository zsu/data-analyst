"""Offline tests for the cfpb pack. Fixtures r001-r007 were recorded from the CFPB search API on 2026-10-20, r008 on 2026-10-22."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", ".."),                                      # tests/ (testkit)
                os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

WY = {"state": "WY", "date_received_min": "2025-01-01", "date_received_max": "2025-12-31"}
SPEC = {"description": "Wyoming student loan complaints, 2025: latest complaints, counts by product, quarterly trend",
        "requests": [
            {"name": "wy_student_latest", "endpoint": "complaints",
             "params": {"product": "Student loan", **WY, "page_size": 2}, "max_rows": 4},
            {"name": "wy_by_product", "endpoint": "aggregations", "params": {"agg": "product", **WY}},
            {"name": "wy_student_quarterly", "endpoint": "trends",
             "params": {"interval": "quarter", "product": "Student loan", **WY}}]}


class TestCfpb(PackTest):
    source = "cfpb"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "wy_student_latest")          # two pages of two, joined by search_after
        self.assertEqual([x["complaint_id"] for x in rows], ["17993241", "17892924", "16527282", "16469628"])
        self.assertEqual(rows[0]["company"], "MOHELA")
        agg = self.csv(out, "wy_by_product")
        self.assertEqual(agg[0]["key"], "Credit reporting or other personal consumer reports")
        self.assertEqual(agg[0]["complaints"], "769")
        self.assertEqual(agg[0]["total_matching"], "1201")
        q = self.csv(out, "wy_student_quarterly")
        self.assertEqual([x["period"] for x in q], ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"])
        self.assertEqual(sum(int(x["complaints"]) for x in q), 19)
        self.assertTrue(any("of 19 matching" in n for n in r["notes"]), r["notes"])

    def test_periods(self):
        from core import packs
        adapter = packs.load("cfpb").adapter
        self.assertEqual(adapter.periods("2024-11-15", "2025-02-10", "month")[0], ("2024-11", "2024-11-15", "2024-11-30"))
        self.assertEqual(adapter.periods("2024-11-15", "2025-02-10", "month")[-1], ("2025-02", "2025-02-01", "2025-02-10"))
        self.assertEqual(len(adapter.periods("2020-01-01", "2025-12-31", "year")), 6)
        self.assertEqual(adapter.periods("2025-05-01", "2025-12-31", "quarter")[0], ("2025-Q2", "2025-05-01", "2025-06-30"))

    def test_ingest_aggregation_upload(self):
        import shutil
        spec = {"requests": [SPEC["requests"][1]]}
        sp, out = self.write_spec(spec)
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "r003.json"), os.path.join(up, "wy_by_product.json"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "wy_by_product")
        self.assertEqual(rows[1]["key"], "Debt collection")
        self.assertEqual(rows[1]["complaints"], "152")

    def test_trends_need_dates(self):
        bad = {"requests": [{"name": "t", "endpoint": "trends", "params": {"interval": "month"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_bad_agg_is_fix_spec(self):
        bad = {"requests": [{"name": "a", "endpoint": "aggregations", "params": {"agg": "sub_product"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_sub_filters_sent_as_bullet_values(self):
        # The API ignores sub_product / sub_issue parameters; the pack sends product='<p>•<sp>' and
        # issue='<i>•<si>' (a list of sub-issues stays an OR). r008 recorded live on 2026-10-22.
        spec = {"requests": [{"name": "wy_medical_not_mine", "endpoint": "complaints", "max_rows": 3,
                              "params": {"product": "Debt collection", "sub_product": "Medical debt",
                                         "issue": "Attempts to collect debt not owed",
                                         "sub_issue": ["Debt is not yours", "Debt was paid"], **WY}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "wy_medical_not_mine")
        self.assertEqual(len(rows), 3)
        self.assertEqual({x["sub_product"] for x in rows}, {"Medical debt"})
        self.assertTrue({x["sub_issue"] for x in rows} <= {"Debt is not yours", "Debt was paid"})
        self.assertTrue(any("of 14 matching" in n for n in r["notes"]), r["notes"])
        from core import packs
        q = packs.load("cfpb").adapter._q(spec["requests"][0]["params"])
        self.assertEqual(q["product"], ["Debt collection•Medical debt"])
        self.assertNotIn("sub_issue", q)

    def test_ignored_or_invalid_params_are_fix_spec(self):
        base = {"agg": "issue", **WY}
        for extra in ({"has_narrative": True}, {"consumer_disputed": "No"}, {"zipcode": "82001"},
                      {"sub_issue": "Debt was paid"},                                  # no issue
                      {"product": ["Mortgage", "Credit card"], "sub_product": "Store credit card"},
                      {"search_term": "fee", "field": "complaint_what_happened"}):
            bad = {"requests": [{"name": "a", "endpoint": "aggregations", "params": {**base, **extra}}]}
            code, r = self.check(bad)
            self.assertEqual(r["route"], "fix_spec", (extra, r))

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertIn("format=csv", items[0]["url"])
        self.assertEqual(len([i for i in items if i["name"].startswith("wy_student_quarterly#")]), 4)


if __name__ == "__main__":
    unittest.main()

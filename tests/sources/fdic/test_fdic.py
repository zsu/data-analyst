"""Offline tests for the fdic pack. Fixtures r001-r003 were recorded from api.fdic.gov on 2026-10-20, r004-r005 on 2026-10-22."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Bank failures in 2023; Wells Fargo Bank quarterly financials, 2024; Wyoming summary, 2025",
        "requests": [
            {"name": "failures_2023", "endpoint": "failures",
             "params": {"filters": "FAILYR:2023 AND RESTYPE:\"FAILURE\"",
                        "fields": "CERT,NAME,CITY,PSTALP,FAILDATE,QBFASSET,QBFDEP,COST,RESTYPE1", "sort_order": "ASC"}},
            {"name": "wells_2024", "endpoint": "financials",
             "params": {"filters": "CERT:3511 AND REPDTE:[20240101 TO 20241231]",
                        "fields": "CERT,REPDTE,ASSET,DEP,NETINC,ROA", "sort_order": "ASC"}},
            {"name": "wy_summary_2025", "endpoint": "summary",
             "params": {"filters": "STNAME:\"Wyoming\" AND YEAR:2025", "fields": "STNAME,YEAR,CB_SI,BANKS,ASSET,DEP,NETINC"}}]}


class TestFdic(PackTest):
    source = "fdic"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_unwraps_and_adds_iso_dates(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        fails = self.csv(out, "failures_2023")
        self.assertEqual(len(fails), 5)
        svb = fails[0]
        self.assertEqual(svb["NAME"], "SILICON VALLEY BANK")
        self.assertEqual(svb["FAILDATE"], "3/10/2023")
        self.assertEqual(svb["FAILDATE_iso"], "2023-03-10")
        self.assertNotIn("data.NAME", svb)
        self.assertNotIn("score", svb)
        q = self.csv(out, "wells_2024")
        self.assertEqual([x["REPDTE_iso"] for x in q], ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"])
        self.assertEqual(q[0]["NETINC"], "5363000")          # thousands of dollars, year-to-date
        s = self.csv(out, "wy_summary_2025")
        self.assertEqual(sorted(x["CB_SI"] for x in s), ["CB", "SI"])

    def test_financials_without_fields_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "financials", "params": {"filters": "CERT:3511"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_links_are_csv(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(all("format=csv" in i["url"] and i["filename"].endswith(".csv") for i in items))

    def test_search_fields(self):
        code, r = self.cli("--search", "charter class")
        self.assertIn("BKCLASS", [h.get("field") for h in r["results"]])

    def test_aggregations_on_failures_and_history(self):
        # agg_* are declared on failures and history too (FDIC swagger); one bucket per value comes back.
        spec = {"requests": [
            {"name": "ga_failures_by_year", "endpoint": "failures",
             "params": {"filters": "PSTALP:GA AND FAILYR:[2008 TO 2013]", "agg_by": "FAILYR",
                        "agg_sum_fields": "QBFASSET,COST", "agg_term_fields": "RESTYPE1", "agg_limit": 20}},
            {"name": "vt_history_by_code", "endpoint": "history",
             "params": {"filters": "PSTALP:VT", "agg_by": "CHANGECODE", "agg_term_fields": "CLASS", "agg_limit": 10}}]}
        code, r = self.check(spec)
        self.assertEqual(r["route"], "api", r)
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        fy = self.csv(out, "ga_failures_by_year")
        self.assertEqual(sorted(x["FAILYR"] for x in fy), [str(y) for y in range(2008, 2014)])
        self.assertTrue({"count", "sum_QBFASSET", "sum_COST", "count_RESTYPE1_PA"} <= set(fy[0]))
        self.assertEqual(sum(int(x["count"]) for x in fy), 87)
        hist = self.csv(out, "vt_history_by_code")
        self.assertEqual(len(hist), 10)
        self.assertIn("count_CLASS_N", hist[0])
        from core import packs
        eps = packs.load("fdic").endpoints["endpoints"]
        for name in ("failures", "history", "financials", "sod", "summary"):
            self.assertTrue({"agg_by", "agg_sum_fields", "agg_term_fields", "agg_limit"} <= set(eps[name]["params"]), name)


if __name__ == "__main__":
    unittest.main()

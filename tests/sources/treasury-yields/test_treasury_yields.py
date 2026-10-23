"""Offline tests for the treasury-yields pack. Fixtures r001-r003.csv were recorded live on 2026-10-20 with
DATA_ANALYST_RECORD (par yield curve 2024 and 2025, long-term rates 2025), full yearly files as served."""
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SPEC = {"description": "Par yield curve and long-term rates, 2024-2025",
        "requests": [{"name": "par", "endpoint": "par_yield_curve", "params": {"start_year": 2024, "end_year": 2025}},
                     {"name": "lt", "endpoint": "long_term_rates", "params": {"year": 2025}}]}


class TestTreasuryYields(PackTest):
    source = "treasury-yields"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_stacks_years_oldest_first(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        par = self.csv(out, "par")
        self.assertEqual(list(par[0])[:3], ["date", "1 Mo", "1.5 Month"])
        self.assertEqual(par[0]["date"], "2024-01-02")
        self.assertEqual(par[0]["10 Yr"], "3.95")
        self.assertEqual(par[0]["1.5 Month"], "")              # not published before 2025-02-18: empty, not zero
        self.assertEqual(par[-1]["date"], "2025-12-31")
        self.assertEqual(par[-1]["10 Yr"], "4.18")
        self.assertEqual(len(par), 499)
        lt = self.csv(out, "lt")
        self.assertEqual(list(lt[0]), ["date", "LT COMPOSITE (>10 Yrs)", "TREASURY 20-Yr CMT"])
        self.assertEqual((lt[0]["date"], lt[0]["LT COMPOSITE (>10 Yrs)"]), ("2025-01-02", "4.85"))

    def test_year_before_feed_start_is_fix_spec(self):
        bad = {"requests": [{"name": "r", "endpoint": "real_yield_curve", "params": {"start_year": 1999, "end_year": 2004}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("2003", r["message"])

    def test_alias_from_catalog(self):
        code, r = self.cli("--search", "breakeven")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["results"][0]["alias"], "real")

    def test_manual_bundle_one_link_per_year(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        names = [i["filename"] for i in r["items"]]
        self.assertEqual(names, ["par_2024.csv", "par_2025.csv", "lt_2025.csv"])
        self.assertIn("daily-treasury-rates.csv/2024/all?type=daily_treasury_yield_curve", r["items"][0]["url"])

    def test_ingest(self):
        sp, out = self.write_spec(SPEC)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        for src, dst in (("r001.csv", "par_2024.csv"), ("r002.csv", "par_2025.csv"), ("r003.csv", "lt_2025.csv")):
            shutil.copy(os.path.join(FX, src), os.path.join(up, dst))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(len(self.csv(out, "par")), 499)


if __name__ == "__main__":
    unittest.main()

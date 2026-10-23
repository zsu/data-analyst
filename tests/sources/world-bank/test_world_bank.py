"""Offline tests for the world-bank pack. Fixtures were recorded live from api.worldbank.org on 2026-10-20
(r001: G7 GDP per capita PPP 2020-2023; r002: the HTTP 200 error the API returns for the archived
indicator EN.ATM.CO2E.PC)."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "GDP per capita, PPP, G7 countries, 2020-2023",
        "requests": [{"name": "g7_gdp_pc_ppp", "endpoint": "indicator",
                      "params": {"indicator": "gdp_per_capita_ppp", "country": "g7", "date": "2020:2023"}}]}


class TestWorldBank(PackTest):
    source = "world-bank"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "g7_gdp_pc_ppp")
        self.assertEqual(len(rows), 28)
        us = [x for x in rows if x["countryiso3code"] == "USA" and x["date"] == "2023"]
        self.assertEqual(len(us), 1)
        self.assertTrue(us[0]["value"].startswith("8"), us[0])          # about 80,000 international $
        self.assertEqual(us[0]["indicator.id"], "NY.GDP.PCAP.PP.CD")
        self.assertEqual(us[0]["is_aggregate"], "N")

    def test_archived_indicator_is_an_error(self):
        spec = {"requests": [{"name": "co2_old", "endpoint": "indicator",
                              "params": {"indicator": "EN.ATM.CO2E.PC", "country": "US", "date": "2019:2020"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 2, r)
        self.assertIn("archived", r["message"])

    def test_manual_bundle_resolves_groups(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        url = r["items"][0]["url"]
        self.assertIn("CA%3BFR%3BDE", url)
        self.assertIn("NY.GDP.PCAP.PP.CD", url)

    def test_search_catalog_offline(self):
        code, r = self.cli("--search", "health expenditure per capita")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["results"][0]["id"], "SH.XPD.CHEX.PC.CD")

    def test_indicator_list(self):
        """A list of indicators (codes or aliases) is accepted: sent ';'-joined with source=2 (the API refuses
        several indicators without a source id), and a non-text item is a spec problem, not a crash."""
        spec = {"requests": [{"name": "macro", "endpoint": "indicator",
                              "params": {"indicator": ["gdp_growth", "FP.CPI.TOTL.ZG"], "country": "brics",
                                         "mrnev": 3}}]}
        code, r = self.check(spec)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        url = r["items"][0]["url"]
        self.assertIn("NY.GDP.MKTP.KD.ZG%3BFP.CPI.TOTL.ZG", url)
        self.assertIn("source=2", url)
        from testkit import load_pack_module
        ad = load_pack_module("world-bank")
        ep = {"path": "/country/{country}/indicator/{indicator}"}
        p = ad.before_request({}, ep, {"indicator": ["SE.TER.ENRR", "unemployment"], "country": "MX", "source": "11"})
        self.assertEqual((p["indicator"], p["source"]), ("SE.TER.ENRR;SL.UEM.TOTL.ZS", "11"))
        self.assertNotIn("source", ad.before_request({}, ep, {"indicator": "fertility", "country": "MX"}))
        bad = {"requests": [{"name": "b", "endpoint": "indicator", "params": {"indicator": [["x"]], "country": "MX"}}]}
        self.assertEqual(self.check(bad)[1]["route"], "fix_spec")
        none = {"requests": [{"name": "n", "endpoint": "indicator", "params": {"country": "MX"}}]}
        self.assertEqual(self.check(none)[1]["route"], "fix_spec")

    def test_unknown_endpoint_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "nope", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the eia pack. Fixtures were recorded live from api.eia.gov on 2026-10-20 (with an
api.data.gov key passed as EIA_API_KEY; URLs and bodies carry no key): retail electricity price MD/US
2020-2024 (r001), SEDS TETPB per capita (r002), WTI monthly 2025 (r003), weekly U.S. regular gasoline
(r004), retail-sales sector facet values (r005), and the HTTP 400 for an invalid facet (r006)."""
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

SPEC = {"description": "Residential electricity price, Maryland and U.S., annual 2020-2024",
        "requests": [{"name": "md_res_price", "endpoint": "data",
                      "params": {"route": "retail_sales", "state": ["MD", "US"], "facets": {"sectorid": ["RES"]},
                                 "frequency": "annual", "start": "2020", "end": "2024"}}]}
MORE = {"description": "SEDS per capita, WTI, gasoline, sectors",
        "requests": [{"name": "seds_per_capita", "endpoint": "data",
                      "params": {"route": "seds", "msn": "TETPB", "state": ["Maryland", "US"], "start": "2020", "end": "2023"}},
                     {"name": "wti_monthly", "endpoint": "data",
                      "params": {"route": "spot_prices", "facets": {"series": ["RWTC"]}, "frequency": "monthly",
                                 "start": "2025-01", "end": "2025-06"}},
                     {"name": "gasoline_weekly", "endpoint": "data",
                      "params": {"route": "gasoline_diesel_retail", "facets": {"series": ["EMM_EPMR_PTE_NUS_DPG"]},
                                 "frequency": "weekly", "start": "2026-09-01", "end": "2026-10-19"}},
                     {"name": "sectors", "endpoint": "facet_values", "params": {"route": "retail_sales", "facet": "sectorid"}}]}


class TestEia(PackTest):
    source = "eia"
    sample_spec = SPEC

    def test_check_keyless_is_api_with_demo_key(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch_price(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "md_res_price")
        self.assertEqual(len(rows), 10)
        self.assertEqual((rows[0]["period"], rows[0]["stateid"], rows[0]["price"], rows[0]["price-units"]),
                         ("2024", "MD", "17.86", "cents per kilowatt-hour"))

    def test_fetch_seds_spot_gasoline_facets(self):
        code, r, out = self.fetch(MORE)
        self.assertEqual(code, 0, r)
        seds = self.csv(out, "seds_per_capita")
        self.assertEqual((seds[0]["seriesId"], seds[0]["stateId"], seds[0]["value"], seds[0]["unit"]),
                         ("TETPB", "MD", "191.3", "Million Btu"))
        wti = self.csv(out, "wti_monthly")
        self.assertEqual((wti[0]["period"], wti[0]["value"], wti[0]["units"]), ("2025-06", "68.17", "$/BBL"))
        self.assertEqual(len(self.csv(out, "gasoline_weekly")), 7)
        self.assertIn("RES", [s["id"] for s in self.csv(out, "sectors")])

    def test_invalid_facet_error(self):
        bad = {"requests": [{"name": "bad", "endpoint": "data",
                             "params": {"route": "retail_sales", "facets": {"bogus": ["X"]}, "frequency": "annual",
                                        "start": "2024"}}]}
        code, r, out = self.fetch(bad)
        self.assertEqual(code, 2, r)
        self.assertIn("Invalid facet 'bogus'", r["message"])

    def test_spec_checks(self):
        bad = {"requests": [{"name": "x", "endpoint": "data",
                             "params": {"route": "spot_prices", "state": "MD", "frequency": "hourly"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("state", r["message"])
        self.assertIn("frequency 'hourly'", r["message"])

    def test_manual_links_use_demo_key_and_brackets(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(r["dir"], "manifest.json"), encoding="utf-8") as f:
            url = json.load(f)["items"][0]["url"]
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(url.startswith("https://api.eia.gov/v2/electricity/retail-sales/data/?"))
        self.assertEqual(q["facets[stateid][]"], ["MD", "US"])
        self.assertEqual(q["data[0]"], ["price"])
        self.assertEqual(q["api_key"], ["DEMO_KEY"])

    def test_search_catalog(self):
        code, r = self.cli("--search", "electricity price state")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["results"][0]["id"], "electricity/retail-sales")


    def test_request_building_follows_the_route(self):
        """Live 2026-10: total-energy's MSN facet is `msn` (seriesId is a 400 there), and a bare year as the end
        of a monthly series returns January only."""
        from testkit import load_pack_module
        ad = load_pack_module("eia")
        _, q = ad.prepare({"endpoint": "data"}, {"path": "/{route}/data/"},
                          {"route": "total-energy", "msn": "nggcbus", "start": "2019", "end": "2020"})
        self.assertEqual(q["facets[msn][]"], ["NGGCBUS"])
        self.assertNotIn("facets[seriesId][]", q)
        self.assertEqual((q["start"], q["end"]), ("2019-01", "2020-12"))       # monthly is the route default
        _, q = ad.prepare({"endpoint": "data"}, {"path": "/{route}/data/"},
                          {"route": "seds", "msn": "PATCB", "start": "2010", "end": "2012"})
        self.assertEqual(q["facets[seriesId][]"], ["PATCB"])
        self.assertEqual((q["start"], q["end"]), ("2010", "2012"))             # annual: years stay years
        self.assertEqual(ad.whole_year("2021", "quarterly", "end"), "2021-Q4")
        self.assertEqual(ad.whole_year("2021-06", "monthly", "start"), "2021-06")

if __name__ == "__main__":
    unittest.main()

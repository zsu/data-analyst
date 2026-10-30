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
GAS_MONTHLY = {"description": "U.S. regular gasoline, monthly 2024",
               "requests": [{"name": "gas_2024", "endpoint": "data",
                             "params": {"route": "gasoline_diesel_retail", "facets": {"series": ["EMM_EPMR_PTE_NUS_DPG"]},
                                        "frequency": "monthly", "start": "2024", "end": "2024"}}]}
XLS = "EMM_EPMR_PTE_NUS_DPGm.xls"


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

    # ------------------------------------------------------------ keyless dnav history files
    # EMM_EPMR_PTE_NUS_DPGm.xls was downloaded live from www.eia.gov/dnav/pet/hist_xls/ on 2026-10-30 (no key);
    # its 2024 values equal the API's (3.075, 3.212, 3.426 for 2024-01..03, checked the same day). doc_429.json is
    # api.data.gov's documented OVER_RATE_LIMIT body, served with HTTP 429 for the monthly gasoline request.

    def test_check_without_key_lists_the_keyless_file(self):
        code, r = self.check(GAS_MONTHLY)
        self.assertEqual((code, r["route"]), (0, "api"), r)
        self.assertEqual(r["keyless_files"], [{"request": "gas_2024", "frequency": "monthly", "filename": XLS,
                                               "url": "https://www.eia.gov/dnav/pet/hist_xls/" + XLS}])
        self.assertIn("hist_xls", r["keyless_file"])
        self.assertIn("--ingest", r["ingest_command"])
        self.assertIn("429", r["action"])
        # a request no history file holds (SEDS, electricity) gets none
        code, r = self.check(SPEC)
        self.assertNotIn("keyless_file", r)

    def test_demo_key_429_offers_the_keyless_file(self):
        """Eval gas-tax-elasticity-choice: DEMO_KEY was spent (HTTP 429) and the run stopped with no data."""
        sp, out = self.write_spec(GAS_MONTHLY)
        code, r = self.cli(sp, "--out", out, "--env", "local")
        self.assertEqual(code, 4, r)
        self.assertEqual((r["status"], r["kind"], r["route"]), ("blocked", "rate", "run_setup"), r)
        self.assertEqual([f["filename"] for f in r["keyless_files"]], [XLS])
        self.assertIn("DEMO_KEY", r["message"])
        # with a key of the user's, a 429 stays an error (no keyless detour)
        code, r = self.cli(sp, "--out", out, "--env", "local", env={"EIA_API_KEY": "x" * 40})
        self.assertEqual((code, r["status"]), (2, "error"), r)

    def test_api_fetch_cites_the_api(self):
        code, r, out = self.fetch(SPEC)
        self.assertIn("EIA Open Data API v2 (api.eia.gov)", r["citation"])

    def test_ingest_keyless_file(self):
        import shutil
        sp, out = self.write_spec(GAS_MONTHLY)
        up = os.path.join(out, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, XLS), up)
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual((code, r.get("route")), (0, "keyless_file"), r)
        rows = self.csv(out, "gas_2024")
        self.assertEqual(len(rows), 12)
        self.assertEqual([(x["period"], x["value"]) for x in rows[-3:]],
                         [("2024-03", "3.426"), ("2024-02", "3.212"), ("2024-01", "3.075")])     # newest first, as the API
        self.assertEqual((rows[0]["series"], rows[0]["units"]), ("EMM_EPMR_PTE_NUS_DPG", "Dollars per Gallon"))
        with open(os.path.join(out, "data", "eia", "request.json"), encoding="utf-8") as f:
            req = json.load(f)
        self.assertEqual((req["route"], req["acquired"]), ("keyless_file", "keyless_file"))
        self.assertIn(XLS, req["notes"][0])
        with open(os.path.join(out, "data", "eia", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("www.eia.gov/dnav/pet", readme)          # cites the file, not the API
        self.assertNotIn("api.eia.gov", readme)
        # the weekly file is not the monthly request's
        os.rename(os.path.join(up, XLS), os.path.join(up, XLS.replace("m.xls", "w.xls")))
        code, r = self.cli(sp, "--out", out, "--ingest", os.path.join(up, XLS.replace("m.xls", "w.xls")), "--force")
        self.assertEqual(code, 2, r)
        self.assertIn(XLS, r["message"])

    def test_keyless_file_coverage_and_missing_xlrd(self):
        from testkit import load_pack_module
        from core.http import DataError
        ad = load_pack_module("eia")
        one = lambda **p: ad.dnav_file({"name": "x", "endpoint": "data"}, p)
        self.assertEqual(one(route="petroleum/cons/psup", facets={"series": ["MGFUPUS2"]}, frequency="monthly")
                         ["filename"], "MGFUPUS2m.xls")
        self.assertEqual(one(route="petroleum/pri/spt", facets={"series": "RWTC"}, frequency="daily")["filename"],
                         "RWTCd.xls")
        self.assertEqual(one(route="petroleum/pri/gnd", facets={"series": ["EMM_EPMR_PTE_NUS_DPG"]})["filename"],
                         XLS.replace("m.xls", "w.xls"))                      # the route's default frequency
        self.assertIsNone(one(route="petroleum/pri/spt", facets={"series": ["RWTC", "RBRTE"]}, frequency="daily"))
        self.assertIsNone(one(route="petroleum/pri/gnd", facets={"series": ["EMM_EPMR_PTE_NUS_DPG"],
                                                                 "duoarea": ["NUS"]}, frequency="weekly"))
        self.assertIsNone(one(route="seds", msn="TETPB"))
        self.assertIsNone(one(route="natural-gas/pri/sum", facets={"series": ["N3010US3"]}, frequency="monthly"))
        # without xlrd: no file is offered and --ingest says what to install
        saved = sys.modules.get("xlrd")
        sys.modules["xlrd"] = None
        try:
            from core import packs
            self.assertEqual(ad.keyless_files(GAS_MONTHLY, packs.load("eia")), [])
            with self.assertRaises(DataError) as e:
                ad._dnav_rows(os.path.join(self.fixtures, XLS), GAS_MONTHLY["requests"][0],
                              {**GAS_MONTHLY["requests"][0]["params"], "route": "petroleum/pri/gnd"})
            self.assertEqual(e.exception.kind, "install_deps")
        finally:
            if saved is None:
                sys.modules.pop("xlrd", None)
            else:
                sys.modules["xlrd"] = saved

if __name__ == "__main__":
    unittest.main()

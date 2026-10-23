"""Offline tests for the bts pack. Fixtures r001/r002 were recorded from data.bts.gov on 2026-10-20."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "TSI freight, gasoline price and airline passengers, first half of 2024; Texas truck crossings, January 2025",
        "requests": [
            {"name": "tsi_2024h1", "endpoint": "query",
             "params": {"dataset": "monthly_stats",
                        "$select": "date, transportation_services_index_freight, highway_fuel_prices_regular, "
                                   "u_s_airline_traffic_total_non_seasonally_adjusted",
                        "$where": "date >= '2024-01-01' AND date <= '2024-06-01'", "$order": "date"}},
            {"name": "tx_trucks_2025_01", "endpoint": "query",
             "params": {"dataset": "border_crossings", "$select": "port_name, sum(value) AS trucks",
                        "$where": "state = 'Texas' AND measure = 'Trucks' AND date = '2025-01-01T00:00:00'",
                        "$group": "port_name", "$order": "trucks DESC"}}]}


class TestBts(PackTest):
    source = "bts"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "tsi_2024h1")
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["date"], "2024-01-01T00:00:00.000")
        self.assertEqual(rows[0]["highway_fuel_prices_regular"], "3.075")
        ports = self.csv(out, "tx_trucks_2025_01")
        self.assertEqual(ports[0]["port_name"], "Laredo")
        self.assertEqual(ports[0]["trucks"], "239825")

    def test_search_finds_columns(self):
        code, r = self.cli("--search", "transit ridership bus")
        self.assertEqual(code, 0, r)
        cols = [h.get("column") for h in r["results"]]
        self.assertIn("system_use_transit_ridership_1", cols)

    def test_unknown_endpoint_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "nope", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle_csv_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(items[0]["url"].startswith("https://data.bts.gov/resource/crem-w557.json?"))


if __name__ == "__main__":
    unittest.main()

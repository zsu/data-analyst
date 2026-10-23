import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from api.weather.gov on 2026-10-20 (forecast = point lookup + gridpoint forecast).
SPEC = {"description": "Washington, DC forecast; active alerts in New Mexico; observations at Reagan National",
        "requests": [
            {"name": "dc_forecast", "endpoint": "forecast", "params": {"lat": 38.8894, "lon": -77.0352}},
            {"name": "nm_alerts", "endpoint": "alerts_active", "params": {"area": "NM"}},
            {"name": "dca_obs", "endpoint": "observations", "params": {"station": "KDCA", "limit": 3}, "max_rows": 3},
            {"name": "dc_stations", "endpoint": "stations_near", "params": {"lat": 38.8894, "lon": -77.0352}, "max_rows": 3}]}


class TestNws(PackTest):
    source = "nws"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        fc = self.csv(out, "dc_forecast")
        self.assertEqual(len(fc), 14)
        self.assertEqual(fc[0]["name"], "This Afternoon")
        self.assertEqual(fc[0]["temperature"], "71")
        self.assertEqual(fc[0]["office"], "LWX")
        self.assertEqual(fc[0]["grid_x"], "97")
        al = self.csv(out, "nm_alerts")
        self.assertTrue(al[0]["id"].startswith("urn:oid:"))
        self.assertNotIn("geometry.coordinates", al[0])
        obs = self.csv(out, "dca_obs")
        self.assertEqual(obs[0]["stationId"], "KDCA")
        self.assertEqual(obs[0]["temperature.unitCode"], "wmoUnit:degC")
        st = self.csv(out, "dc_stations")
        self.assertEqual(st[0]["stationIdentifier"], "KDCA")
        self.assertEqual(st[0]["latitude"], "38.84833")

    def test_missing_lat_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "forecast", "params": {"lat": 38.9}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle_forecast_note(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(items[0]["url"], "https://api.weather.gov/points/38.8894,-77.0352")
        self.assertIn("properties.forecast", items[0]["note"])


if __name__ == "__main__":
    unittest.main()

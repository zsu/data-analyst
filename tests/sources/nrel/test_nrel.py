import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from developer.nlr.gov on 2026-10-20 with a key (URLs are recorded without it).
SPEC = {"description": "Vermont DC fast EV stations; Montpelier VT rates, solar resource and a 4 kW PV system",
        "requests": [
            {"name": "vt_dcfast", "endpoint": "alt_fuel_stations",
             "params": {"state": "VT", "fuel_type": "ELEC", "ev_charging_level": "dc_fast"}, "max_rows": 3},
            {"name": "montpelier_rates", "endpoint": "utility_rates", "params": {"lat": 44.26, "lon": -72.58}},
            {"name": "montpelier_solar", "endpoint": "solar_resource", "params": {"lat": 44.26, "lon": -72.58}},
            {"name": "montpelier_pv", "endpoint": "pvwatts",
             "params": {"lat": 44.26, "lon": -72.58, "system_capacity": 4, "module_type": 0, "losses": 14,
                        "array_type": 1, "tilt": 30, "azimuth": 180}}]}


class TestNrel(PackTest):
    source = "nrel"
    sample_spec = SPEC

    def test_check_api_keyless(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")          # optional key: DEMO_KEY works

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        st = self.csv(out, "vt_dcfast")
        self.assertEqual(st[1]["station_name"], "Brattleboro, VT - Tesla Supercharger")
        self.assertEqual(st[1]["ev_dc_fast_num"], "16")
        self.assertTrue(any("113 in total" in n for n in r["notes"]), r["notes"])
        self.assertEqual(self.csv(out, "montpelier_rates")[0]["residential"], "0.1688")
        solar = self.csv(out, "montpelier_solar")
        self.assertEqual(len(solar), 13)
        self.assertEqual(solar[0]["avg_ghi_kwh_m2_day"], "1.67")
        self.assertEqual(solar[12]["period"], "annual")
        pv = self.csv(out, "montpelier_pv")
        self.assertEqual(len(pv), 12)
        self.assertEqual(pv[0]["ac_kwh"], "319.5231449149002")

    def test_pvwatts_requires_inputs(self):
        code, r = self.check({"requests": [{"name": "pv", "endpoint": "pvwatts", "params": {"lat": 40, "lon": -105}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_check_error(self):
        sys.path.insert(0, self.pack_dir)
        import importlib.util
        spec = importlib.util.spec_from_file_location("nrel_adapter_t", os.path.join(self.pack_dir, "adapter.py"))
        ad = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ad)
        self.assertIn("lat", ad.check_error(200, b'{"errors": ["lat must be a number"]}'))
        self.assertIsNone(ad.check_error(200, b'{"errors": [], "outputs": {}}'))

    def test_pvwatts_hourly_one_row_per_hour(self):
        """timeframe=hourly: the 8,760-value arrays become one row per hour (not the 12 monthly rows).
        In-process row shaped like generic_rest.flatten output; the numbers are arbitrary, not source data."""
        from testkit import load_pack_module
        from core.http import DataError
        ad = load_pack_module("nrel")
        n = 8760
        row = {"ac_monthly": "; ".join(str(float(m)) for m in range(1, 13)),
               "dc_monthly": "; ".join("1.5" for _ in range(12)),
               "ac": "; ".join(str(float(i % 24) * 7.5) for i in range(n)),
               "tamb": "; ".join(str(-3.0 + (i % 50)) for i in range(n)),
               "ac_annual": 1234.5, "capacity_factor": 11.1}
        out = ad.normalize({"name": "pv"}, {"path": "/pvwatts/v8.json"}, [row])
        self.assertEqual(len(out), n)
        self.assertEqual((out[0]["month"], out[0]["day"], out[0]["hour"]), (1, 1, 0))
        self.assertEqual((out[25]["month"], out[25]["day"], out[25]["hour"]), (1, 2, 1))
        self.assertEqual((out[-1]["month"], out[-1]["day"], out[-1]["hour"]), (12, 31, 23))
        self.assertEqual(out[25]["ac_w"], "7.5")
        self.assertEqual(out[49]["tamb_c"], "46.0")
        self.assertEqual(out[0]["capacity_factor"], 11.1)
        self.assertNotIn("ac_kwh", out[0])
        bad = dict(row, ac="; ".join("1" for _ in range(100)), tamb="; ".join("1" for _ in range(100)))
        with self.assertRaises(DataError):
            ad.normalize({"name": "pv"}, {"path": "/pvwatts/v8.json"}, [bad])
        monthly = ad.normalize({"name": "pv"}, {"path": "/pvwatts/v8.json"},
                               [{k: v for k, v in row.items() if k not in ("ac", "tamb")}])
        self.assertEqual([m["month"] for m in monthly], list(range(1, 13)))

    def test_nearest_needs_coordinates(self):
        """The API removed 'location' (HTTP 422): the nearest endpoint needs latitude and longitude."""
        code, r = self.check({"requests": [{"name": "near", "endpoint": "alt_fuel_nearest",
                                             "params": {"fuel_type": "E85", "radius": 10}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_city_is_matched_on_the_rows_not_sent(self):
        """The station API has no city parameter (sent, it answers 0 rows, live 2026-10-28): the pack fetches the
        state or ZIP and keeps the rows of that city, ignoring case and extra blanks (r005, recorded 2026-10-28:
        the ZIP read in full, 4 stations)."""
        req = {"name": "dcfast_town", "endpoint": "alt_fuel_stations",
               "params": {"zip": "05301", "fuel_type": "ELEC", "ev_charging_level": "dc_fast", "city": "  brattleboro"}}
        code, r, out = self.fetch({"requests": [req]})
        self.assertEqual((code, r["status"]), (0, "ok"), r)
        self.assertEqual([x["city"] for x in self.csv(out, "dcfast_town")], ["Brattleboro"] * 4)
        none = {"requests": [{**req, "params": {**req["params"], "city": "Nowhere"}}]}
        code, r, out = self.fetch(none)
        self.assertEqual(code, 2, r)                                 # every station read, none matched: fails loudly
        self.assertIn("returned no rows", r["message"])
        # a state fetch cut at max_rows (r001: 3 of 113) leaves the city's rows incomplete: partial, and said so
        cut = {"requests": [{"name": "dcfast_town", "endpoint": "alt_fuel_stations", "max_rows": 3,
                             "params": {"state": "VT", "fuel_type": "ELEC", "ev_charging_level": "dc_fast",
                                        "city": "Nowhere"}}]}
        code, r, out = self.fetch(cut)
        self.assertEqual((code, r["status"]), (0, "partial"), r)
        self.assertTrue(any("may be incomplete" in n for n in r["notes"]), r["notes"])
        code, r = self.check({"requests": [{"name": "c", "endpoint": "alt_fuel_stations", "params": {"city": "Barre"}}]})
        self.assertEqual(r["route"], "fix_spec", r)                  # city alone would page through the country


if __name__ == "__main__":
    unittest.main()

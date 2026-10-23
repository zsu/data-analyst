import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from api.nhtsa.gov and vpic.nhtsa.dot.gov on 2026-10-20.
SPEC = {"description": "2019 Honda Civic: recalls, NCAP rating, VIN decode",
        "requests": [{"name": "civic_2019_recalls", "endpoint": "recalls_by_vehicle",
                      "params": {"make": "honda", "model": "civic", "modelYear": 2019}},
                     {"name": "civic_2019_sedan_rating", "endpoint": "ratings_detail", "params": {"vehicleId": 14009}},
                     {"name": "vin", "endpoint": "vin_decode", "params": {"vin": "1HGCM82633A123456"}},
                     {"name": "odi", "endpoint": "complaint_odi", "params": {"odinumber": 11675912}}]}


class TestNhtsa(PackTest):
    source = "nhtsa"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rec = self.csv(out, "civic_2019_recalls")
        self.assertEqual(len(rec), 4)
        first = rec[0]
        self.assertEqual(first["NHTSACampaignNumber"], "20V314000")
        self.assertEqual(first["ReportReceivedDate"], "28/05/2020")       # published DD/MM/YYYY kept
        self.assertEqual(first["ReportReceivedDate_iso"], "2020-05-28")   # added column
        rating = self.csv(out, "civic_2019_sedan_rating")[0]
        self.assertEqual(rating["OverallRating"], "5")
        self.assertEqual(rating["RolloverPossibility"], "0.095")
        vin = self.csv(out, "vin")[0]
        self.assertEqual(vin["Make"], "HONDA")
        self.assertEqual(vin["ModelYear"], "2003")
        odi = self.csv(out, "odi")[0]
        self.assertEqual(odi["dateComplaintFiled_iso"], "2025-07-24")     # complaints are MM/DD/YYYY

    def test_missing_param_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "recalls_by_vehicle", "params": {"make": "honda", "model": "civic"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_year_range_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "recalls_by_vehicle",
                             "params": {"make": "honda", "model": "civic", "modelYear": "2018-2020"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle_vpic_host(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertIn("https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/1HGCM82633A123456?format=json", urls)
        self.assertTrue(any(u.startswith("https://api.nhtsa.gov/recalls/recallsByVehicle?") for u in urls))


if __name__ == "__main__":
    unittest.main()

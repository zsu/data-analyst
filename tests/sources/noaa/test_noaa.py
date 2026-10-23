import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from the keyless NCEI Access Data Service on 2026-10-20.
SPEC = {"description": "Central Park, NYC: daily and monthly weather, monthly normals; stations near Manhattan",
        "requests": [
            {"name": "cp_daily", "endpoint": "data",
             "params": {"dataset": "daily", "stations": "GHCND:USW00094728", "startDate": "2024-01-01",
                        "endDate": "2024-01-03", "dataTypes": "TMAX,TMIN,PRCP,SNOW"}},
            {"name": "cp_monthly", "endpoint": "data",
             "params": {"dataset": "monthly", "stations": "USW00094728", "startDate": "2024-01-01",
                        "endDate": "2024-12-31", "dataTypes": "TAVG,PRCP"}},
            {"name": "cp_normals", "endpoint": "data",
             "params": {"dataset": "normals_monthly", "stations": "USW00094728", "startDate": "0001-01-01",
                        "endDate": "9996-12-31", "dataTypes": "MLY-TAVG-NORMAL,MLY-PRCP-NORMAL"}},
            {"name": "near_manhattan", "endpoint": "station_search",
             "params": {"dataset": "daily", "bbox": "40.9,-74.1,40.6,-73.8", "startDate": "2024-01-01T00:00:00",
                        "endDate": "2024-01-31T23:59:59"}, "max_rows": 2}]}

CDO = {"requests": [{"name": "ny_stations", "endpoint": "cdo_stations", "params": {"datasetid": "GSOM", "locationid": "FIPS:36"}}]}


class TestNoaa(PackTest):
    source = "noaa"
    sample_spec = SPEC

    def test_check_api_keyless(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        d = self.csv(out, "cp_daily")
        self.assertEqual(d[0]["DATE"], "2024-01-01")
        self.assertEqual(d[0]["TMAX"], "47")                 # degrees F (units=standard)
        self.assertEqual(d[0]["PRCP"], "0.03")
        m = self.csv(out, "cp_monthly")
        self.assertEqual(len(m), 12)
        self.assertEqual(m[0]["TAVG"], "37.0")
        n = self.csv(out, "cp_normals")
        self.assertEqual(n[0]["MLY-TAVG-NORMAL"], "33.7")    # padding trimmed
        s = self.csv(out, "near_manhattan")
        self.assertEqual(s[0]["station_id"], "USW00094728")
        self.assertEqual(s[0]["latitude"], "40.77898")
        self.assertIn("TMAX", s[0]["data_types"].split())

    def test_cdo_needs_key(self):
        code, r = self.check(CDO, env={"DATA_ANALYST_ENV": "local"})
        self.assertIn(r["route"], ("run_setup", "manual_download", "cloud_secret"), r)
        self.assertTrue(r["key_only_needs"], r)

    def test_cdo_fetch_without_key_is_key_missing(self):
        """CDO answers a bare HTTP 400 without a token (live 2026-10); the fetch stops as key_missing first."""
        spec = {"requests": [{"name": "tx_types", "endpoint": "cdo_datatypes", "params": {"locationid": "FIPS:48"}}]}
        code, r, out = self.fetch(spec, env={"DATA_ANALYST_ENV": "local"})
        self.assertEqual(code, 4, r)
        self.assertEqual(r.get("kind"), "key_missing", r)
        self.assertIn("NOAA_API_KEY", r.get("error", ""))

    def test_ping_without_key_reports_key_absence(self):
        ad = __import__("testkit").load_pack_module("noaa")

        class NoKey:
            key = None

            def get(self, *a, **k):
                raise AssertionError("CDO must not be called without a token")
        p = ad.ping(NoKey())
        self.assertEqual(p["kind"], "key_missing")
        self.assertFalse(p["auth_ok"])

    def test_partial_dates_padded_for_ads(self):
        """ADS refuses YYYY-MM even for monthly datasets ("Invalid ISO Date Time", live 2026-10)."""
        ad = __import__("testkit").load_pack_module("noaa")
        ep = {"path": "/access/services/data/v1"}
        p = ad.before_request({}, ep, {"startDate": "2019-02", "endDate": "2020-02", "stations": "GHCND:USC00041614"})
        self.assertEqual((p["startDate"], p["endDate"]), ("2019-02-01", "2020-02-29"))
        self.assertEqual(p["stations"], "USC00041614")
        p = ad.before_request({}, ep, {"startDate": "1998", "endDate": "2001"})
        self.assertEqual((p["startDate"], p["endDate"]), ("1998-01-01", "2001-12-31"))
        p = ad.before_request({}, ep, {"startDate": "0001-01-01", "endDate": "2003-06-30T23:59:59"})
        self.assertEqual((p["startDate"], p["endDate"]), ("0001-01-01", "2003-06-30T23:59:59"))
        s = ad.before_request({}, {"path": "/access/services/search/v1/data"}, {"stations": "GHCND:USW00023174"})
        self.assertEqual(s["stations"], "USW00023174")

    def test_no_requested_type_came_back_fails(self):
        """A misspelled or lower-case data type gives HTTP 200 rows with only DATE and STATION (live 2026-10)."""
        from core.http import DataError
        ad = __import__("testkit").load_pack_module("noaa")
        ep = {"path": "/access/services/data/v1"}
        bare = [{"DATE": "2015-07-01", "STATION": "USW00023174"}]
        with self.assertRaises(DataError) as cm:
            ad.normalize({"name": "x", "params": {"dataTypes": "snwd,AWNDX"}}, ep, bare)
        self.assertIn("case-sensitive", str(cm.exception))
        ok = ad.normalize({"name": "x", "params": {"dataTypes": "SNWD,AWNDX"}}, ep,
                          [{"DATE": "2015-07-01", "STATION": "USW00023174", "SNWD": " 0"}])
        self.assertEqual(ok[0]["SNWD"], "0")

    def test_cdo_range_too_long(self):
        bad = {"requests": [{"name": "x", "endpoint": "cdo_data", "params": {
            "datasetid": "GHCND", "stationid": "GHCND:USW00094728", "startdate": "2020-01-01", "enddate": "2024-12-31"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)


if __name__ == "__main__":
    unittest.main()

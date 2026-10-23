import importlib.util
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# No AQS key was available when this pack was built, so the fixtures are NOT live recordings: they are the
# example responses printed in the AQS API documentation (https://aqs.epa.gov/aqsweb/documents/data_api.html),
# saved as doc_*.json (a sample-data record, the "No data matched your selection" reply, the "Failed" reply).
# The documentation's examples call the row list "Body"; the live API calls it "Data" (the adapter reads both).
# Credentials below are the documentation's public test account (email test@aqs.api, key test).
DOC_CREDS = {"AQS_API_KEY": "test", "AQS_EMAIL": "test@aqs.api"}

SPEC = {"description": "PM2.5 sample at site 01-073-0023 (Jefferson County, AL) on 2017-04-01 (documented example)",
        "requests": [{"name": "al_pm25_sample", "endpoint": "data",
                      "params": {"service": "sampleData", "geo": "bySite", "param": "pm25", "bdate": "20170401",
                                 "edate": "20170401", "state": "AL", "county": "73", "site": "23"}}]}

TWO_YEARS = {"description": "Vermont annual ozone 2022-2023 (split into two calls)",
             "requests": [{"name": "vt_ozone", "endpoint": "data", "allow_empty": True,
                           "params": {"service": "annualData", "geo": "byState", "param": "ozone",
                                      "bdate": "20220101", "edate": "20231231", "state": "Vermont"}}]}


def adapter():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst",
                     "sources", "epa-aqs")
    spec = importlib.util.spec_from_file_location("epa_aqs_adapter_t", os.path.join(d, "adapter.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEpaAqs(PackTest):
    source = "epa-aqs"
    sample_spec = SPEC

    def test_check_without_key(self):
        code, r = self.check(SPEC)
        self.assertIn(r["route"], ("run_setup", "manual_download", "cloud_secret"), r)
        self.assertEqual(r["key_env"], "AQS_API_KEY")

    def test_fetch_documented_example(self):
        code, r, out = self.fetch(SPEC, env=DOC_CREDS)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "al_pm25_sample")
        self.assertEqual(rows[0]["sample_measurement"], "6.2")
        self.assertEqual(rows[0]["county_name"], "Jefferson")
        self.assertEqual(rows[0]["unit_of_measure"], "Micrograms/cubic meter (LC)")
        with open(os.path.join(out, "data", "epa-aqs", "request.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("test@aqs.api", text)            # email and key never recorded
        self.assertNotIn("key=test", text)

    def test_multi_year_split(self):
        code, r, out = self.fetch(TWO_YEARS, env=DOC_CREDS)
        self.assertEqual(code, 0, r)
        self.assertTrue(any("2 calls, one per year" in n for n in r["notes"]), r["notes"])

    def test_bad_dates_and_geography(self):
        bad = {"requests": [{"name": "x", "endpoint": "data",
                             "params": {"service": "dailyData", "geo": "byCounty", "param": "88101",
                                        "bdate": "2024-06-01", "edate": "20240630", "state": "06"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("YYYYMMDD", r["message"])
        self.assertIn("county", r["message"])

    def test_failed_header_is_error(self):
        with open(os.path.join(self.fixtures, "doc_failed.json"), "rb") as f:
            msg = adapter().check_error(200, f.read())
        self.assertIn("value is missing", msg)

    def test_year_windows(self):
        w = adapter().year_windows({"service": "dailyData", "bdate": "20220615", "edate": "20240110"})
        self.assertEqual(w, [("20220615", "20221231"), ("20230101", "20231231"), ("20240101", "20240110")])

    def test_manual_bundle_placeholders(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            it = json.load(f)["items"][0]
        self.assertIn("state=01&county=073&site=0023", it["url"])
        self.assertTrue(it["url"].endswith("&email={AQS_EMAIL}&key={API_KEY}"), it["url"])
        self.assertTrue(it["needs_key"])


if __name__ == "__main__":
    unittest.main()

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

    def test_airdata_zip_is_ingested_and_filtered(self):
        # The keyless route: EPA's AirData yearly file goes through --ingest as downloaded. The file covers every
        # monitor; ingest keeps the request's rows and uses the API's column names. (Rows made up for the test,
        # in the layout of daily_<code>_<year>.csv, checked 2026-10-28.)
        import zipfile
        head = ("State Code,County Code,Site Num,Parameter Code,POC,Latitude,Longitude,Datum,Parameter Name,"
                "Sample Duration,Pollutant Standard,Date Local,Units of Measure,Event Type,Observation Count,"
                "Observation Percent,Arithmetic Mean,1st Max Value,1st Max Hour,AQI,Method Code,Method Name,"
                "Local Site Name,Address,State Name,County Name,City Name,CBSA Name,Date of Last Change")
        def line(st, co, day, mean):
            return (f'{st},{co},0007,42602,1,40.1,-80.2,WGS84,Nitrogen dioxide (NO2),1 HOUR,NO2 1-hour 2010,'
                    f'2021-{day},Parts per billion,None,24,100.0,{mean},20.0,7,18,099,Method,Site,Addr,Somestate,'
                    f'Somecounty,Town,"Town, ST",2022-05-01')
        rows = [line("42", "003", "03-01", "9.5"), line("42", "003", "03-02", "11.25"),
                line("42", "005", "03-01", "4.0"), line("39", "003", "03-01", "7.0"), line("42", "003", "04-01", "8.0")]
        sp, out = self.write_spec({"requests": [{"name": "no2_march", "endpoint": "data",
                                                  "params": {"service": "dailyData", "geo": "byCounty", "param": "no2",
                                                             "bdate": "20210301", "edate": "20210331",
                                                             "state": "PA", "county": "3"}}]})
        up = os.path.join(out, "up")
        os.makedirs(up)
        with zipfile.ZipFile(os.path.join(up, "daily_42602_2021.zip"), "w") as z:
            z.writestr("daily_42602_2021.csv", "\n".join([head, *rows]) + "\n")
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        got = self.csv(out, "no2_march")
        self.assertEqual([(x["county_code"], x["date_local"], x["arithmetic_mean"]) for x in got],
                         [("003", "2021-03-01", "9.5"), ("003", "2021-03-02", "11.25")])
        self.assertIn("site_number", got[0])
        self.assertEqual(got[0]["first_max_value"], "20.0")
        # the AirData file is the pack's keyless_file: recorded and cited as such, not as the API
        self.assertEqual(r["route"], "keyless_file")
        with open(os.path.join(out, "data", "epa-aqs", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("aqs.epa.gov/aqsweb/airdata", readme)
        self.assertNotIn("aqs.epa.gov/data/api", readme)
        # an annual file for a daily request is refused, naming the file wanted
        os.remove(os.path.join(up, "daily_42602_2021.zip"))
        with zipfile.ZipFile(os.path.join(up, "daily_42602_2021.zip"), "w") as z:
            z.writestr("x.csv", "State Code,Year,Metric Used\n42,2021,m\n")
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 2, r)
        self.assertIn("not an AirData dailyData file", r["message"])

    def test_run_setup_offers_the_keyless_file(self):
        code, r = self.check(TWO_YEARS)
        if r["route"] == "run_setup":
            self.assertIn("annual_conc_by_monitor_<year>.zip", r["keyless_file"])
        names = adapter().airdata_names({"service": "dailyData", "param": "88101"}, "2023")
        self.assertEqual(names, ["daily_88101_2023.zip", "daily_88101_2023.csv"])
        self.assertEqual(adapter().airdata_names({"service": "dailyData", "param": "88101,44201"}, "2023"), [])
        self.assertEqual(adapter().airdata_names({"service": "monitors", "param": "88101"}, "2023"), [])

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

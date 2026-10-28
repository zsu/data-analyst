"""Offline tests for the census pack.

Fixtures (fixtures/index.json):
- recorded live on 2026-10-20 without a key: the Census 2020 county and place reference files for
  Maryland (www2.census.gov), the dataset discovery documents for 2025/acs/acs5 (HTTP 404) and
  2024/acs/acs5, variables.json of 2020/dec/pl, and the missing_key.html page the API serves (after a
  302 redirect) for a keyless data query since 2026-05-12.
- doc_acs1_2019_hmong.json: NOT a recorded response. It is the documented example response of the Census
  Data API User Guide ("Example API Queries": 2019/acs/acs1?get=NAME,B02015_009E,B02015_009M&for=state:*),
  the rows the guide shows; no CENSUS_API_KEY was available to record a live data response.
- normalize() is unit-tested on hand-written rows that carry the documented special values; those rows
  are test inputs, not fixtures.
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", ".."),                                      # tests/ (testkit)
                os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "scripts")]
from testkit import PackTest, pack_dir# noqa: E402

KEY = {"CENSUS_API_KEY": "test-key-not-real-0000"}
SPEC = {"description": "Median household income, Maryland counties, latest ACS 5-year",
        "requests": [{"name": "md_county_income", "endpoint": "query",
                      "params": {"dataset": "acs5", "get": "NAME,B19013_001E", "for": "county:*", "state": "Maryland"}}]}
DOC = {"description": "Hmong population by state, ACS 1-year 2019 (documented example)",
       "requests": [{"name": "hmong_2019", "endpoint": "query",
                     "params": {"dataset": "2019/acs/acs1", "get": "NAME,B02015_009E", "for": "state:*"}}]}
GEO = {"description": "names to codes",
       "requests": [{"name": "montgomery", "endpoint": "query",
                     "params": {"dataset": "acs5", "get": ["NAME", "B25077_001E", "B25064_001E"], "state": "MD",
                                "county": "Montgomery"}},
                    {"name": "baltimore_city", "endpoint": "query",
                     "params": {"dataset": "acs5", "get": "NAME,B01003_001E", "state": "24", "place": "Baltimore"}}]}


def adapter():
    spec = importlib.util.spec_from_file_location("census_adapter_t", os.path.join(pack_dir("census"), "adapter.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCensus(PackTest):
    source = "census"
    sample_spec = SPEC

    def test_check_without_key_is_run_setup(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "run_setup", r)
        self.assertEqual(r["key_env"], "CENSUS_API_KEY")

    def test_keyless_query_is_blocked_as_missing_key(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 4, r)
        self.assertEqual(r["kind"], "key_missing", r)
        self.assertEqual(r["route"], "run_setup")

    def test_fetch_documented_example_with_moe(self):
        code, r, out = self.fetch(DOC, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "hmong_2019")
        self.assertEqual(list(rows[0]), ["NAME", "B02015_009E", "B02015_009E_flag", "B02015_009M",
                                         "B02015_009M_flag", "state"])   # MOE added, its own column
        ga = [x for x in rows if x["NAME"] == "Georgia"][0]
        self.assertEqual((ga["B02015_009E"], ga["B02015_009M"]), ("3162", "1336"))
        idaho = [x for x in rows if x["NAME"] == "Idaho"][0]
        self.assertEqual(idaho["B02015_009E"], "")
        self.assertIn("null", idaho["B02015_009E_flag"])
        self.assertTrue(any("margins of error added" in n for n in r["notes"]), r)
        with open(os.path.join(out, "data", "census", "README.md"), encoding="utf-8") as f:
            self.assertIn("not endorsed or certified by the Census Bureau", f.read())

    def test_latest_and_names_resolve_in_links(self):
        sp, out = self.write_spec(GEO)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        urls = {i["request"]: i["url"] for i in r["manual_items"]}
        self.assertIn("/2024/acs/acs5?", urls["montgomery"])                  # newest vintage, not hard-coded
        self.assertIn("for=county:031&in=state:24", urls["montgomery"])
        self.assertIn("B25077_001E,B25077_001M,B25064_001E,B25064_001M", urls["montgomery"])
        self.assertIn("for=place:04000&in=state:24", urls["baltimore_city"])   # Baltimore city, not a CDP

    def test_did_you_mean(self):
        bad = {"requests": [{"name": "x", "endpoint": "query",
                             "params": {"dataset": "acs5", "get": "NAME,B01003_001E", "state": "Maryland",
                                        "county": "Montgomry"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("Did you mean: Montgomery County", r["message"])
        st = {"requests": [{"name": "x", "endpoint": "query",
                            "params": {"dataset": "acs5", "get": "NAME", "state": "Marylnd"}}]}
        code, r = self.check(st)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("Did you mean: Maryland", r["message"])
        amb = {"requests": [{"name": "x", "endpoint": "query",
                             "params": {"dataset": "acs5", "get": "NAME", "state": "MD", "county": "Baltimore"}}]}
        code, r = self.check(amb)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("ambiguous", r["message"])

    def test_moe_cannot_be_dropped_and_acs1_has_no_tracts(self):
        bad = {"requests": [{"name": "a", "endpoint": "query",
                             "params": {"dataset": "acs1", "get": "NAME,B19013_001E", "for": "tract:*",
                                        "state": "MD", "moe": False}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("never dropped", r["message"])
        self.assertIn("ACS 1-year has no tracts", r["message"])

    def test_search_variables(self):
        code, r = self.cli("--search", "2020/dec/pl: total population")
        self.assertEqual(code, 0, r)
        ids = [x.get("variable") or x.get("id") for x in r["results"]]
        self.assertIn("P1_001N", ids)
        self.assertTrue(any(x.get("dataset") == "2020/dec/pl" for x in r["results"]))

    def test_normalize_sentinels_and_topcodes(self):
        a = adapter()
        rows = [{"NAME": "A", "B25077_001E": "2000001", "B25077_001M": "-555555555", "B19013_001E": "-666666666",
                 "B19013_001M": "-222222222", "state": "24"},
                {"NAME": "B", "B25077_001E": "350000", "B25077_001M": "4100", "B19013_001E": "90000",
                 "B19013_001M": "-999999999", "state": "11"}]
        out = a.normalize({}, {}, rows)
        self.assertEqual(out[0]["B25077_001E"], "2000001")                   # top-code kept, flagged
        self.assertIn("$2,000,000 or more", out[0]["B25077_001E_flag"])
        self.assertEqual(out[0]["B25077_001M"], "")
        self.assertIn("treat the margin of error as zero", out[0]["B25077_001M_flag"])
        self.assertEqual(out[0]["B19013_001E"], "")
        self.assertIn("could not be computed", out[0]["B19013_001E_flag"])
        self.assertEqual(out[1]["B19013_001M"], "")
        self.assertIn("(N)", out[1]["B19013_001M_flag"])
        self.assertEqual(out[1]["B19013_001E_flag"], "")
        self.assertEqual(list(out[0])[:4], ["NAME", "B25077_001E", "B25077_001E_flag", "B25077_001M"])

    def test_uploads_from_data_census_gov(self):
        import tempfile
        from core import packs
        from core.http import DataError
        a, pack = adapter(), packs.load("census")
        req = {"name": "s1501", "params": {"get": ["NAME", "S1501_C02_015E"]}}
        with tempfile.TemporaryDirectory() as t:
            j = os.path.join(t, "s1501.json")             # data.census.gov table JSON: every column of the table
            json.dump({"response": {"data": [["GEO_ID", "NAME", "S1501_C01_006E", "S1501_C02_015E", "S1501_C02_015M"],
                                             ["0400000US25", "Massachusetts", "1", "48.3", "0.4"]]}}, open(j, "w"))
            rows = a.parse_upload(pack, req, [j])
            self.assertEqual({k: rows[0][k] for k in ("geo_fips", "S1501_C02_015E", "S1501_C02_015M")},
                             {"geo_fips": "25", "S1501_C02_015E": "48.3", "S1501_C02_015M": "0.4"})
            self.assertNotIn("S1501_C01_006E", rows[0])
            c = os.path.join(t, "s1501.csv")              # its "Download Table" CSV, read long
            with open(c, "w", encoding="utf-8") as f:
                f.write('Label (Grouping),Alabama!!Estimate,Alabama!!Margin of Error\n'
                        '"Bachelor\'s degree or higher","29.9%","±0.4"\n')
            rows = a.parse_upload(pack, req, [c])
            self.assertEqual([(r["geography"], r["measure"], r["value"]) for r in rows],
                             [("Alabama", "Estimate", 29.9), ("Alabama", "Margin of Error", 0.4)])
            # the table JSON names its year and product: kept on each row, and a 5-year file is no 1-year request
            v = os.path.join(t, "b19013.json")
            json.dump({"response": {"dataAPIURI": "https://api.census.gov/data/2024/acs/acs5?get=group(B19013)",
                                    "objectId": "ACSDT5Y2024.B19013",
                                    "data": [["GEO_ID", "NAME", "B19013_001E"], ["0400000US51", "Virginia", "1"]]}},
                      open(v, "w"))
            r5 = {"name": "inc", "params": {"dataset": "acs5", "get": ["NAME", "B19013_001E"]}}
            self.assertEqual(a.parse_upload(pack, r5, [v])[0]["vintage"], "2024")
            with self.assertRaises(DataError):
                a.parse_upload(pack, dict(r5, params={**r5["params"], "dataset": "acs1"}), [v])
            b = os.path.join(t, "bad.json")
            json.dump({"x": 1}, open(b, "w"))
            with self.assertRaises(DataError):          # never an empty table that looks fine
                a.parse_upload(pack, req, [b])

    def test_moe_names(self):
        a = adapter()
        self.assertEqual(a.with_moe(["NAME", "B19013_001E", "S1701_C03_001E", "DP03_0062PE", "P1_001N"]),
                         ["NAME", "B19013_001E", "B19013_001M", "S1701_C03_001E", "S1701_C03_001M",
                          "DP03_0062PE", "DP03_0062PM", "P1_001N"])


if __name__ == "__main__":
    unittest.main()

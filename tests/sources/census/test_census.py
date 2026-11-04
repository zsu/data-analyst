"""Offline tests for the census pack.

Fixtures (fixtures/index.json):
- recorded live on 2026-10-20 without a key: the Census 2020 county and place reference files for
  Maryland (www2.census.gov), the dataset discovery documents for 2025/acs/acs5 (HTTP 404) and
  2024/acs/acs5, variables.json of 2020/dec/pl, and the missing_key.html page the API serves (after a
  302 redirect) for a keyless data query since 2026-05-12.
- doc_acs1_2019_hmong.json: NOT a recorded response. It is the documented example response of the Census
  Data API User Guide ("Example API Queries": 2019/acs/acs1?get=NAME,B02015_009E,B02015_009M&for=state:*),
  the rows the guide shows; no CENSUS_API_KEY was available to record a live data response.
- handmade_sipp_2024.json: NOT a recorded response either. Hand-written rows in the API's table shape (one
  header row, text values) for the SIPP request the pack builds; it tests the query (keys and weight added,
  no geography) and that SIPP values skip the ACS special-value handling. The SIPP API needs a key.
- pulse_expense_2024.json, pulse_covid_expense_2023.json, hv_rates_2025.json: recorded live on 2026-11-03
  with a key (DATA_ANALYST_RECORD; the key is not in the recorded URLs) from timeseries/hhpulse, timeseries/hps
  and timeseries/eits/hv. The HVS rows match Table 4 of the Census release (2025 homeownership rates).
- wy_pums2024_call1/2.json, wy_asec2026_call1.json, wy_cpsbasic2026sep_call1.json and vars_*.json: recorded live
  on 2026-11-04 with a key (DATA_ANALYST_RECORD) for the MICRO spec: ACS 2024 1-year PUMS for Wyoming ages 90-99
  with the 80 replicate weights (two calls joined on SERIALNO, SPORDER), CPS ASEC 2026 and CPS basic September 2026
  for Wyoming ages 80-85, and the per-variable metadata (/variables/<VAR>.json) behind the codebooks.
- normalize() is unit-tested on hand-written rows that carry the documented special values; those rows
  are test inputs, not fixtures.
"""
import csv
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", ".."),                                      # tests/ (testkit)
                os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module, pack_dir  # noqa: E402

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
MICRO = {"description": "fixtures: ACS PUMS Wyoming ages 90+, replicates; ASEC and CPS basic Wyoming ages 80-85",
         "requests": [
             {"name": "wy_pums", "endpoint": "query",
              "params": {"dataset": "2024/acs/acs1/pums", "get": "AGEP,HICOV,PINCP", "state": "WY", "AGEP": "90:99",
                         "replicates": True}},
             {"name": "wy_asec", "endpoint": "query",
              "params": {"dataset": "2026/cps/asec/mar", "get": "A_AGE,NOW_COV", "state": "WY", "A_AGE": "80:85"}},
             {"name": "wy_basic", "endpoint": "query",
              "params": {"dataset": "2026/cps/basic/sep", "get": "PRTAGE,PEMLR", "state": "WY", "PRTAGE": "80:85"}}]}


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

    def test_sipp_adds_keys_and_weight_and_needs_no_geography(self):
        spec = {"requests": [{"name": "sipp_dec", "endpoint": "query",
                              "params": {"dataset": "2024/sipp", "get": "TAGE,RHLTHMTH", "MONTHCODE": "12"}}]}
        code, r, out = self.fetch(spec, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "sipp_dec")
        self.assertEqual(list(rows[0]), ["TAGE", "RHLTHMTH", "SSUID", "PNUM", "MONTHCODE", "WPFINWGT"])
        self.assertEqual(rows[0]["SSUID"], "000114552134")             # leading zeros kept
        self.assertEqual(rows[2]["WPFINWGT"], "-9")                     # no ACS sentinel handling, no _flag column
        self.assertTrue(any("WPFINWGT" in n and "keys and weight added" in n for n in r["notes"]), r)

    def test_sipp_state_is_the_interview_state_and_nothing_below(self):
        spec = {"requests": [{"name": "md", "endpoint": "query",
                              "params": {"dataset": "2024/sipp", "get": "TPTOTINC", "state": "Maryland"}}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        url = r["manual_items"][0]["url"]
        self.assertIn("/2024/sipp?get=TPTOTINC,SSUID,PNUM,MONTHCODE,WPFINWGT", url)
        self.assertIn("TST_INTV=24", url)
        self.assertNotIn("for=", url)
        bad = {"requests": [{"name": "x", "endpoint": "query",
                             "params": {"dataset": "2024/sipp", "get": "TPTOTINC", "state": "MD", "county": "031"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("SIPP has no geography below the state", r["message"])

    def test_time_series_links_and_checks(self):
        spec = {"requests": [
            {"name": "duty", "endpoint": "query",
             "params": {"dataset": "trade_imports_hs", "get": "CTY_NAME,CAL_DUT_MO", "CTY_CODE": "5700",
                        "COMM_LVL": "HS2", "time": "2026-08"}},
            {"name": "qtax", "endpoint": "query",
             "params": {"dataset": "eits_qtax", "get": "CELL_VALUE,CATEGORY_CODE", "state": "MD", "time": "2026-Q1"}}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        urls = {i["request"]: i["url"] for i in r["manual_items"]}
        self.assertIn("/timeseries/intltrade/imports/hs?get=CTY_NAME,CAL_DUT_MO", urls["duty"])
        self.assertNotIn("for=", urls["duty"])
        self.assertIn("for=state:24", urls["qtax"])
        bad = {"requests": [{"name": "a", "endpoint": "query",
                             "params": {"dataset": "trade_exports_hs", "get": "ALL_VAL_MO"}},
                            {"name": "b", "endpoint": "query",
                             "params": {"dataset": "eits_retail", "get": "cell_value", "for": "us:*"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("international trade needs a time", r["message"])
        self.assertIn("a time series needs time", r["message"])

    def test_pulse_and_homeownership_aliases(self):
        """Household Pulse (2024 long, 2020-2023 wide) and HVS rates through their aliases, from recorded rows."""
        spec = {"requests": [
            {"name": "pulse_expense_2024", "endpoint": "query",
             "params": {"dataset": "pulse", "get": "MEASURE_NAME,MEASURE_LABEL,RATE,RATE_MOE,TOTAL,UNIVERSE,CYCLE,PHASE,"
                                                  "COL_START_DATE,COL_END_DATE",
                        "for": "us:*", "time": "2024", "MEASURE_NAME": "EXPENSE"}},
            {"name": "pulse_covid_expense_2023", "endpoint": "query",
             "params": {"dataset": "pulse_covid", "get": "EXPENSE_RATE,EXPENSE_RT_MOE,EXPENSE_TOTAL,EXPENSE_UNIV,WEEK,"
                                                        "COL_START_DATE,COL_END_DATE", "for": "us:*", "time": "2023"}},
            {"name": "homeownership_2025", "endpoint": "query",
             "params": {"dataset": "eits_homeownership", "get": "cell_value,data_type_code,category_code,seasonally_adj,"
                                                               "time_slot_id,geo_level_code,error_data",
                        "for": "us:*", "time": "2025", "category_code": "RATE"}}]}
        code, r, out = self.fetch(spec, env=KEY)
        self.assertEqual(code, 0, r)
        p = {x["CYCLE"]: x for x in self.csv(out, "pulse_expense_2024")}
        self.assertEqual(sorted(p), [f"{i:02d}" for i in range(1, 10)])          # Phase 4.0-4.2, cycles 01-09
        c9 = p["09"]
        self.assertEqual((c9["PHASE"], c9["RATE"], c9["RATE_MOE"]), ("4.2", "37.1", "0.7"))
        self.assertAlmostEqual(100 * int(c9["TOTAL"]) / int(c9["UNIVERSE"]), float(c9["RATE"]), places=1)
        w = {x["WEEK"]: x for x in self.csv(out, "pulse_covid_expense_2023")}
        self.assertEqual((w["63"]["EXPENSE_RATE"], w["63"]["EXPENSE_RT_MOE"]), ("40.8", "0.6"))
        hv = self.csv(out, "homeownership_2025")
        hor = {x["time"]: x["cell_value"] for x in hv if x["data_type_code"] == "HOR"}       # NSA, release Table 4
        self.assertEqual(hor, {"2025-Q1": "65.1", "2025-Q2": "65.0", "2025-Q3": "65.3", "2025-Q4": "65.7"})
        moe = {x["cell_value"] for x in hv if x["data_type_code"] == "E_HOR"}
        self.assertEqual((moe, {x["error_data"] for x in hv if x["data_type_code"] == "E_HOR"}), ({"0.5"}, {"yes"}))
        self.assertEqual({x["seasonally_adj"] for x in hv if x["data_type_code"] == "SAHOR"}, {"yes"})

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

    # ------------------------------------------------------------------ ACS PUMS and CPS microdata
    def test_pums_replicates_split_joined_with_codebook(self):
        code, r, out = self.fetch(MICRO, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "wy_pums")
        self.assertEqual(len(rows), 53)
        reps = [f"PWGTP{i}" for i in range(1, 81)]
        self.assertEqual(list(rows[0]), ["AGEP", "HICOV", "PINCP", "SERIALNO", "SPORDER", "PWGTP", "WGTP", "ADJINC"]
                         + reps + ["state"])      # asked order; the AGEP the predicate echoes is not repeated
        self.assertTrue(all(90 <= int(x["AGEP"]) <= 99 for x in rows))
        # the second call's replicates landed on the same persons: compare with the recorded call itself
        with open(os.path.join(self.fixtures, "wy_pums2024_call2.json"), encoding="utf-8") as f:
            head, *call2 = json.load(f)
        want = {(x[head.index("SERIALNO")], x[head.index("SPORDER")]): x[head.index("PWGTP80")] for x in call2}
        self.assertEqual({(x["SERIALNO"], x["SPORDER"]): x["PWGTP80"] for x in rows}, want)
        notes = " | ".join(r["notes"])
        self.assertIn("added ADJINC", notes)                    # PINCP's label says to use ADJINC
        self.assertIn("2 calls of at most 50 variables, joined on SERIALNO, SPORDER", notes)
        book = self.csv(out, "wy_pums_codebook")
        lab = {(x["variable"], x["value"]): x["value_label"] for x in book}
        self.assertEqual(lab[("HICOV", "2")], "No health insurance coverage")
        self.assertIn("N/A (less than 15", lab[("PINCP", "-19999")])
        with open(os.path.join(out, "data", "census", "data_dictionary.csv"), encoding="utf-8") as f:
            dd = {(x["table"], x["column"]): x for x in csv.DictReader(f)}
        self.assertEqual(dd[("wy_pums", "PWGTP80")]["non_missing"], "53")
        self.assertEqual(dd[("wy_pums", "SERIALNO")]["type"], "text")
        # the analysis helper: replicate SE of the weighted share uninsured
        pums = load_pack_module("census", "lib/pums.py")
        import pandas as pd
        df = pums.load(os.path.join(out, "data", "census", "wy_pums.csv"))
        self.assertTrue(df["SERIALNO"].str.startswith("2024").all())         # keys stay text
        self.assertTrue(pd.api.types.is_numeric_dtype(df["PWGTP"]))
        est, se = pums.rep_se(df, lambda d, w: pums.wshare(pums.uninsured(d, "acs"), d[w]))
        self.assertTrue(0 <= est < 1 and se >= 0)
        with self.assertRaises(ValueError):                     # no replicates in the CPS files
            pums.rep_se(pd.DataFrame({"PWSSWGT": [1.0]}), lambda d, w: 0.0, weight="PWSSWGT")

    def test_cps_asec_and_basic_keys_and_weights(self):
        code, r, out = self.fetch(MICRO, env=KEY)
        self.assertEqual(code, 0, r)
        a = self.csv(out, "wy_asec")
        self.assertEqual(list(a[0]), ["A_AGE", "NOW_COV", "H_SEQ", "A_LINENO", "PERIDNUM", "MARSUPWT", "HSUP_WGT",
                                      "state"])
        self.assertEqual(len({(x["H_SEQ"], x["A_LINENO"]) for x in a}), len(a))   # the person key is unique
        b = self.csv(out, "wy_basic")
        self.assertEqual(list(b[0])[:7], ["PRTAGE", "PEMLR", "HRHHID", "HRHHID2", "PULINENO", "PWSSWGT", "PWCMPWGT"])
        self.assertTrue(b[0]["HRHHID"].startswith("0") or len(b[0]["HRHHID"]) == 15)   # text, zeros kept
        pums = load_pack_module("census", "lib/pums.py")
        import pandas as pd
        df = pums.load(os.path.join(out, "data", "census", "wy_basic.csv"))
        self.assertEqual(set(pums.emp_status(df, "basic").dropna()) <= {"employed", "unemployed", "nilf"}, True)
        with self.assertRaises(ValueError):
            pums.uninsured(df, "basic")

    def test_micro_spec_checks(self):
        bad = {"requests": [
            {"name": "a", "endpoint": "query",
             "params": {"dataset": "cps_asec", "get": "PTOTVAL", "replicates": True}},
            {"name": "b", "endpoint": "query",
             "params": {"dataset": "acs1_pums", "get": "AGEP", "state": "MD", "county": "Montgomery"}},
            {"name": "c", "endpoint": "query",
             "params": {"dataset": "acs1_pums", "get": "AGEP", "agep": "25:54"}},
            {"name": "d", "endpoint": "query",
             "params": {"dataset": "2026/cps/basic/sep", "get": "PEMLR", "replicates": True}},
            {"name": "e", "endpoint": "query",
             "params": {"dataset": "1998/cps/asec/mar", "get": "A_AGE", "state": "MD"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        m = r["message"]
        self.assertIn("ASEC replicate weights are not on the Census API", m)
        self.assertIn("ipums", m)
        self.assertIn("smallest area is the PUMA", m)
        self.assertIn("filters are variable names in upper case", m)
        self.assertIn("basic monthly files have no replicate weights", m)
        self.assertIn("ASEC files before 2001 take no state geography", m)

    def test_micro_links_national_by_state_and_chunks(self):
        spec = {"requests": [{"name": "us", "endpoint": "query",
                              "params": {"dataset": "2024/acs/acs1/pums", "get": "HICOV,AGEP", "AGEP": "0:18",
                                         "replicates": True}},
                             {"name": "cps_us", "endpoint": "query",
                              "params": {"dataset": "2026/cps/basic/sep", "get": "PEMLR"}},
                             {"name": "puma", "endpoint": "query",
                              "params": {"dataset": "2024/acs/acs1/pums", "get": "HICOV", "state": "MD",
                                         "for": "public use microdata area:*"}}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        us = [i for i in r["manual_items"] if i["request"] == "us"]
        self.assertEqual(len(us), 51 * 2)                       # 50 states and DC, two calls each
        self.assertIn("for=state:01", us[0]["url"])
        self.assertIn("AGEP=0:18", us[1]["url"])                # every call repeats the filter
        self.assertIn("SERIALNO,SPORDER", us[1]["url"])         # and carries the join keys
        self.assertIn("for=state:56", us[-1]["url"])
        cps = [i for i in r["manual_items"] if i["request"] == "cps_us"]
        self.assertEqual(len(cps), 1)
        self.assertNotIn("for=", cps[0]["url"])                 # the nation: no geography (old ASEC fails with one)
        puma = [i for i in r["manual_items"] if i["request"] == "puma"]
        self.assertEqual(len(puma), 1)
        self.assertIn("for=public use microdata area:*&in=state:24".replace(" ", "+"), puma[0]["url"])

    def test_merge_chunks_out_of_order_and_missing(self):
        from core.http import DataError
        a = adapter()
        p1 = (["X", "K", "state"], iter([["1", "a", "56"], ["2", "b", "56"], ["3", "c", "56"]]))
        p2 = (["R1", "K"], iter([["r-a", "a"], ["r-c", "c"], ["r-b", "b"]]))
        cols, rows = a._merge_chunks([p1, p2], ["K"])
        self.assertEqual(cols, ["X", "K", "state", "R1"])
        self.assertEqual(list(rows), [["1", "a", "56", "r-a"], ["2", "b", "56", "r-b"], ["3", "c", "56", "r-c"]])
        p1 = (["X", "K"], iter([["1", "a"], ["2", "b"]]))
        p2 = (["R1", "K"], iter([["r-b", "b"], ["r-z", "z"]]))
        cols, rows = a._merge_chunks([p1, p2], ["K"])
        with self.assertRaises(DataError):                      # never a blank replicate for a missing person
            list(rows)
        body = b'[["A","B"],\n["1",null],\n["2","x"]]'
        self.assertEqual(list(a._iter_rows(body)), [["A", "B"], ["1", None], ["2", "x"]])

    def test_micro_upload_joins_the_manual_files(self):
        import tempfile
        from core import packs
        a, pack = adapter(), packs.load("census")
        req = {"name": "wy_pums", "params": MICRO["requests"][0]["params"]}
        with tempfile.TemporaryDirectory() as t:
            for i, f in enumerate(("wy_pums2024_call1.json", "wy_pums2024_call2.json"), start=1):
                with open(os.path.join(self.fixtures, f), "rb") as src, \
                        open(os.path.join(t, f"wy_pums_{i:03d}.json"), "wb") as dst:
                    dst.write(src.read())
            rows = a.parse_upload(pack, req, [os.path.join(t, f) for f in sorted(os.listdir(t))])
        self.assertEqual(len(rows), 53)
        self.assertIn("PWGTP80", rows[0])
        self.assertIn("PWGTP1", rows[0])

    def test_moe_names(self):
        a = adapter()
        self.assertEqual(a.with_moe(["NAME", "B19013_001E", "S1701_C03_001E", "DP03_0062PE", "P1_001N"]),
                         ["NAME", "B19013_001E", "B19013_001M", "S1701_C03_001E", "S1701_C03_001M",
                          "DP03_0062PE", "DP03_0062PM", "P1_001N"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the dol pack. No DOL_API_KEY was available, so data queries are NOT verified live.

Fixtures (fixtures/index.json):
- recorded live on 2026-10-20 (keyless endpoints): the Datasets API catalog pages 1-5 (r001-r005), the
  dataset descriptions of ETA ui_national_weekly_claims (10305, r006) and OSHA inspection (10334, r007),
  and the HTTP 401 the data API returns without a key (r008).
- doc_ui_national_preview.json and doc_osha_inspection_preview.json: NOT recorded data responses. They are
  the `dataset_preview` that the DOL Datasets API publishes for each dataset (the first rows of the
  table in the API's {"data": [...]} envelope), copied from r006/r007 and served for the matching
  unfiltered query.
- Unemployment insurance trust fund files (oui.doleta.gov, keyless), recorded live on 2026-10-30:
  SummaryTables.xlsx is the real UI Data Summary workbook (2026 Q2), unchanged. trimmed_ar2112.csv is a TRIMMED
  real copy of https://oui.doleta.gov/unemploy/csv/ar2112.csv (23 MB, Last-Modified 2026-10-30): the header and,
  unchanged, the rows of WY, CA and TX from 2025-01, VT and PR from 2026-05, and AK 1979-01 to 1979-03.
"""
import json
import os
import sys
import unittest
import urllib.parse

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

KEY = {"DOL_API_KEY": "test-key-not-real-0000"}
UTF = {"description": "UI trust fund: Wyoming latest month, CA and TX in 2026, each state's latest month",
       "requests": [{"name": "wy", "endpoint": "ui_trust_fund", "params": {"state": "WY", "latest": True}},
                    {"name": "ca_tx", "endpoint": "ui_trust_fund",
                     "params": {"state": ["CA", "Texas"], "start": "2026-01", "end": "2026-09",
                                "fields": ["dep_ui_contributions_total", "c14", "dis_ui_benefits_total",
                                           "balance_close_utf"]}},
                    {"name": "latest", "endpoint": "ui_trust_fund", "params": {"latest": True,
                                                                               "fields": ["balance_close_utf"]}}]}
UI = {"description": "Weekly UI claims, national, first 10 weeks on file (documented preview)",
      "requests": [{"name": "ui_national", "endpoint": "query",
                    "params": {"dataset": "eta_ui_national_weekly_claims", "sort": "asc"}, "max_rows": 10}]}
WHD = {"description": "WHD enforcement cases in Texas, newest first",
       "requests": [{"name": "whd_tx", "endpoint": "query", "params": {"dataset": "whd_enforcement", "state": "tx"}}]}


class TestDol(PackTest):
    source = "dol"
    sample_spec = WHD

    def test_check_data_query_needs_key(self):
        code, r = self.check(WHD)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "run_setup", r)
        self.assertTrue(r["key_only_needs"])

    def test_check_catalog_is_keyless_api(self):
        code, r = self.check({"requests": [{"name": "cat", "endpoint": "datasets", "params": {}}]})
        self.assertEqual(r["route"], "api", r)                    # the catalog is keyless

    def test_keyless_data_query_is_blocked(self):
        code, r, out = self.fetch(WHD)
        self.assertEqual(code, 4, r)
        self.assertEqual(r["kind"], "key_missing")

    def test_catalog_and_field_dictionary_live(self):
        spec = {"requests": [{"name": "catalog", "endpoint": "datasets", "params": {}},
                             {"name": "ui_fields", "endpoint": "dataset_info",
                              "params": {"dataset_id": "eta_ui_national_weekly_claims"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        cat = self.csv(out, "catalog")
        self.assertEqual(len(cat), 45)
        self.assertIn("ui_state_weekly_claims", [c["api_url"] for c in cat])
        fields = {f["column_name"]: f["column_desc"] for f in self.csv(out, "ui_fields")}
        self.assertIn("Seasonally adjusted count of total initial claims", fields["c5"])

    def test_fetch_documented_preview(self):
        code, r, out = self.fetch(UI, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "ui_national")
        self.assertEqual(len(rows), 10)
        self.assertEqual((rows[0]["rptdate"], rows[0]["c4"]), ("1967-01-07", "346000"))
        self.assertTrue(any("c5 = Seasonally adjusted count of total initial claims" in n for n in r["notes"]), r)

    def test_osha_preview_in_api_order(self):
        spec = {"requests": [{"name": "osha", "endpoint": "query",
                              "params": {"dataset": "OSHA/inspection", "sort_by": ""}, "max_rows": 10}]}
        code, r, out = self.fetch(spec, env=KEY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "osha")
        self.assertEqual(rows[0]["estab_name"], "CHRISTMAS TREE SHOPS")
        self.assertEqual(rows[0]["site_state"], "NH")

    def test_filter_object_and_case_tolerant_search(self):
        spec = {"requests": [{"name": "amazon", "endpoint": "query",
                              "params": {"dataset": "osha_inspection", "state": "ca", "search": {"estab_name": "Amazon"},
                                         "filters": {"insp_type": ["A", "B"], "open_date": {"gt": "2024-01-01"}}},
                              "max_rows": 2000}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        items = r["manual_items"]
        self.assertEqual(len(items), 2)                            # two pages of 1,000
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(items[1]["url"]).query)
        self.assertEqual((q["limit"], q["offset"], q["sort_by"], q["sort"]), (["1000"], ["1000"], ["open_date"], ["desc"]))
        fo = json.loads(q["filter_object"][0])
        conds = fo["and"]
        self.assertIn({"field": "insp_type", "operator": "in", "value": ["A", "B"]}, conds)
        self.assertIn({"field": "open_date", "operator": "gt", "value": "2024-01-01"}, conds)
        self.assertIn({"field": "site_state", "operator": "eq", "value": "CA"}, conds)
        like = [c for c in conds if "or" in c][0]["or"]
        self.assertEqual([c["value"] for c in like], ["%Amazon%", "%AMAZON%", "%amazon%"])
        self.assertTrue(items[0]["url"].startswith("https://apiprod.dol.gov/v4/get/OSHA/inspection/json?"))
        self.assertTrue(items[0]["needs_key"])

    def test_bad_specs(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "query", "params": {"dataset": "nope"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        code, r = self.check({"requests": [{"name": "x", "endpoint": "query",
                                            "params": {"dataset": "osha_violation", "state": "TX"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("state", r["message"])

    def test_unknown_field_names_are_fix_spec_without_a_key(self):
        # the field dictionary (r007, dataset 10334) is keyless: a filter, search or sort on a field the dataset lacks
        # is caught by --check (fix_spec), not after the user downloads a bundle of failing links
        bad = {"requests": [{"name": "x", "endpoint": "query",
                             "params": {"dataset": "osha_inspection", "state": "NH",
                                        "filters": {"site_district": "NORTH", "insp_type": "B"},
                                        "search": {"establishment": "acme"}}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("'site_district'", r["message"])
        self.assertIn("'establishment'", r["message"])
        self.assertNotIn("'insp_type'", r["message"].split("its fields")[0])
        self.assertIn("site_city", r["message"])                    # the real fields are listed
        bad["requests"][0]["check_fields"] = False                  # the escape when the dictionary is incomplete
        code, r = self.check(bad)
        self.assertEqual(r["route"], "run_setup", r)
        good = {"requests": [{"name": "x", "endpoint": "query",
                              "params": {"dataset": "osha_inspection", "state": "NH", "filters": {"site_city": "KEENE"}}}]}
        code, r = self.check(good)
        self.assertEqual(r["route"], "run_setup", r)
        # run_setup names the pack's keyless way to the same data and the command that packages such a file
        self.assertIn("https://", r["keyless_alternative"])
        self.assertIn("--ingest", r["ingest_command"])
        self.assertIn("ingest_command", r["action"])

    # ------------------------------------------------------------------ OUI unemployment insurance files (keyless)
    def test_trust_fund_check_is_keyless_api(self):
        code, r = self.check(UTF)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)
        self.assertEqual(r["key_only_needs"], [])
        # a spec mixing a keyed API query and a trust fund file still needs the key, for the query only
        code, r = self.check({"requests": UTF["requests"] + WHD["requests"]})
        self.assertEqual(r["route"], "run_setup", r)
        self.assertIn("whd_tx", r["key_only_needs"][0])
        self.assertNotIn("wy", r["key_only_needs"][0])

    def test_trust_fund_fetch(self):
        code, r, out = self.fetch(UTF)
        self.assertEqual(code, 0, r)
        wy = self.csv(out, "wy")
        self.assertEqual(len(wy), 1)
        self.assertEqual(len(wy[0]), 136)                         # st, rptdate and the 134 coded columns, renamed
        self.assertEqual((wy[0]["st"], wy[0]["rptdate"], wy[0]["balance_close_utf"], wy[0]["balance_close_total"],
                          wy[0]["dep_ui_contributions_total"], wy[0]["dis_ui_benefits_total"]),
                         ("WY", "2026-09-30", "586148120.75", "589297877.79", "871553.05", "3111454.15"))
        self.assertFalse([c for c in wy[0] if c.startswith("c") and c[1:].isdigit()])
        ca = self.csv(out, "ca_tx")
        self.assertEqual(list(ca[0]), ["st", "rptdate", "dep_ui_contributions_total", "dep_treasury_interest_utf",
                                       "dis_ui_benefits_total", "balance_close_utf"])
        self.assertEqual({x["st"] for x in ca}, {"CA", "TX"})        # "Texas" resolved to TX
        self.assertEqual(min(x["rptdate"] for x in ca), "2026-01-31")
        self.assertEqual([x["balance_close_utf"] for x in ca if x["st"] == "CA" and x["rptdate"] == "2026-06-30"],
                         ["18308724.16"])
        latest = self.csv(out, "latest")
        self.assertEqual({x["st"]: x["rptdate"] for x in latest},
                         {"AK": "1979-03-31", "CA": "2026-08-31", "TX": "2026-08-31", "WY": "2026-09-30",
                          "VT": "2026-06-30", "PR": "2026-06-30"})
        self.assertTrue(any("have not reported it yet" in n and "VT 2026-06" in n for n in r["notes"]), r)
        self.assertTrue(any("balance_close_utf (c87)" in n for n in r["notes"]), r)

    def test_identities_hold_on_the_renamed_columns(self):
        code, r, out = self.fetch({"requests": [{"name": "tx", "endpoint": "ui_trust_fund", "params": {"state": "TX"}}]})
        self.assertEqual(code, 0, r)
        for x in self.csv(out, "tx"):
            f = lambda k: float(x[k] or 0)
            for a in ("total", "clearing", "utf", "bpa"):
                self.assertAlmostEqual(f(f"balance_close_{a}"),
                                       f(f"balance_forward_{a}") + f(f"deposits_{a}") - f(f"disbursements_{a}"), places=1)

    def test_data_summary(self):
        spec = {"requests": [{"name": "fin", "endpoint": "ui_data_summary", "params": {}},
                             {"name": "solv", "endpoint": "ui_data_summary", "params": {"sheet": "Wage and Tax Rate"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        fin = {x["st"]: x for x in self.csv(out, "fin")}
        self.assertEqual(len(fin), 54)                              # 53 jurisdictions and the United States
        ca = fin["CA"]
        self.assertEqual((ca["cyq"], ca["quarter_end"], ca["tf_balance_thousands"], ca["loan_balance_thousands"],
                          ca["net_trust_fund_balance_thousands"]), ("2026.2", "2026-06-30", "18309", "18722060", "-18703751"))
        self.assertEqual((fin["AZ"]["state"], fin["AZ"]["note"]), ("Arizona", "Indicates states missing some monthly revenue reports."))
        self.assertEqual(fin["AZ"]["revenues_thousands"], "319139.6")       # stored 319139.59999999998
        self.assertEqual(fin["US"]["state"], "United States")
        solv = {x["st"]: x for x in self.csv(out, "solv")}
        self.assertEqual(solv["WY"]["cyq"], "2025.4")                       # this sheet lags two quarters
        self.assertEqual((solv["WY"]["ahcm"], solv["WY"]["hcm"], solv["AL"]["tax_rate_total_wages"]), ("2.43", "2.13", "0.07"))
        self.assertEqual(solv["CA"]["ahcm"], "")                            # NA for a borrowing state
        self.assertTrue(any("non-Title XII" in n for n in r["notes"]), r)

    def test_trust_fund_manual_and_ingest(self):
        sp, out = self.write_spec(UTF)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        items = r["manual_items"]
        self.assertEqual({i["url"] for i in items}, {"https://oui.doleta.gov/unemploy/csv/ar2112.csv"})
        self.assertFalse(any(i["needs_key"] for i in items))
        import shutil
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        spec = {"requests": [{"name": "wy", "endpoint": "ui_trust_fund", "params": {"state": "WY", "latest": True}}]}
        sp, out = self.write_spec(spec)
        shutil.copy(os.path.join(self.fixtures, "trimmed_ar2112.csv"), os.path.join(up, "ar2112.csv"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual(self.csv(out, "wy")[0]["balance_close_utf"], "586148120.75")

    def test_trust_fund_bad_specs(self):
        for params, word in (({"state": "Atlantis"}, "unknown state"), ({"start": "June 2026"}, "YYYY-MM"),
                             ({"fields": ["trust_fund_balance"]}, "balance_close_utf")):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "ui_trust_fund", "params": params}]})
            self.assertEqual(r["route"], "fix_spec", (params, r))
            self.assertIn(word, r["message"])
        code, r = self.check({"requests": [{"name": "x", "endpoint": "ui_data_summary", "params": {"sheet": "Loans"}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_trust_fund_client_sends_no_key_and_rejects_html(self):
        a = load_pack_module("dol")
        from core.http import DataError
        with self.assertRaises(DataError) as e:
            a._tidy_oui({"name": "x", "endpoint": "ui_trust_fund"}, {"columns": {}}, {}, b"<html>Error</html>", "ar2112.csv")
        self.assertIn("expected the ETA 2112 CSV", str(e.exception))
        seen = {}

        class FakeClient:
            def __init__(self, base, **kw):
                seen.update(base=base, **kw)

            def request(self, method, path):
                seen["path"] = path
                return type("R", (), {"body": b'"st","rptdate","c87"\n"WY","2026-09-30",5\n'})()
        import core.http
        real = core.http.Client
        core.http.Client = FakeClient
        try:
            pack = type("P", (), {"policy": {"limits": {"rps": 3, "burst": 6}}})()
            rows, _ = a.fetch_oui(pack, {"name": "x", "endpoint": "ui_trust_fund"},
                                  {"path": "https://oui.doleta.gov/unemploy/csv/ar2112.csv", "columns": {"c87": "balance_close_utf"}},
                                  {}, {})
        finally:
            core.http.Client = real
        self.assertEqual(seen["auth"], {})                       # no credentials on the oui.doleta.gov client
        self.assertEqual(rows, [{"st": "WY", "rptdate": "2026-09-30", "balance_close_utf": "5"}])

    def test_search_catalog(self):
        code, r = self.cli("--search", "unemployment insurance weekly claims state")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["results"][0]["id"], "ETA/ui_state_weekly_claims")


if __name__ == "__main__":
    unittest.main()

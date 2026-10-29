"""Offline tests for the treasury deep pack.

Fixtures (fixtures): r00N.json were recorded live on 2026-10-20 with DATA_ANALYST_RECORD (the --check
ping and row count, the download, and --fields for Debt to the Penny, September 2026); upload_debt.csv is the
same query's CSV link (format=csv) as a user would download it from the manual bundle.
"""
import importlib.util
import json
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.abspath(os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "sources", "treasury"))
SKILL = os.path.dirname(os.path.dirname(PACK))
SCRIPTS = os.path.join(SKILL, "scripts")
for p in (SCRIPTS, PACK):
    if p not in sys.path:
        sys.path.insert(0, p)
from testkit import PackTest  # noqa: E402

FX = os.path.join(HERE, "fixtures")
SPEC = {"description": "Debt to the penny, September 2026", "as_of": "2026-09-30",
        "datasets": [{"name": "debt", "endpoint": "v2/accounting/od/debt_to_penny",
                      "filter": {"record_date": {"gte": "2026-09-01"}}}]}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestTreasury(PackTest):
    source = "treasury"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)
        self.assertEqual(r["source"], "treasury")
        self.assertEqual(r["dataset_counts"], {"debt": 21})
        self.assertIn("--source treasury", r["next_command"])
        self.assertIn(os.path.join("scripts", "fetch.py"), r["next_command"])

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        rows = self.csv(out, "debt")
        self.assertEqual(len(rows), 21)
        self.assertEqual(rows[0]["record_date"], "2026-09-01")
        self.assertEqual(rows[0]["tot_pub_debt_out_amt"], "40112464593206.30")
        d = os.path.join(out, "data", "treasury")
        prov = json.load(open(os.path.join(d, "fetch_spec.json"), encoding="utf-8"))
        self.assertEqual(prov["source"], "treasury")
        self.assertEqual(prov["datasets"]["debt"]["rows"], 21)
        self.assertEqual(prov["tables"][0]["file"], "debt.csv")
        self.assertIn("Debt to the penny", open(os.path.join(d, "README.md"), encoding="utf-8").read())

    def test_reuse_existing_and_force(self):
        code, r, out = self.fetch(SPEC)
        sp = os.path.join(out, "spec.json")
        code, r = self.cli(sp, "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing", r)
        code, r = self.cli(sp, "--out", out, "--check", "--force")
        self.assertEqual(r["route"], "api", r)

    def test_unknown_endpoint_is_fix_spec(self):
        bad = {"datasets": [{"name": "x", "endpoint": "v1/accounting/od/no_such_table"}]}
        code, r = self.check(bad)
        self.assertEqual(code, 2, r)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_local_unreachable_is_report_error_with_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--check", "--env", "local", env={"DATA_ACCESS_FORCE": "restricted"},
                           fixtures=False)
        self.assertEqual(r["route"], "report_error", r)
        self.assertIn("HTTPS_PROXY", r["message"])
        self.assertTrue(os.path.exists(os.path.join(out, "manual_download", "treasury", "INSTRUCTIONS.md")))

    def test_fetch_blocked_exit_4(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--env", "sandbox", env={"DATA_ACCESS_FORCE": "restricted"},
                           fixtures=False)
        self.assertEqual(code, 4, r)
        self.assertEqual(r["route"], "manual_download", r)
        b = r["manual_bundle"]
        for f in ("INSTRUCTIONS.md", "download_treasury.sh", "download_treasury.ps1", "spec.json", "download_urls.txt"):
            self.assertTrue(os.path.exists(os.path.join(b, f)), f)
        sh = open(r["download_scripts"]["bash"], encoding="ascii").read()
        self.assertIn("api.fiscaldata.treasury.gov", sh)                # the requests are built in
        text = open(r["instructions"], encoding="utf-8").read()
        self.assertIn("bash ~/Downloads/download_treasury.sh", text)
        self.assertNotIn("python", text.lower())
        self.assertEqual(r["user_commands"]["bash"], "bash ~/Downloads/download_treasury.sh")
        self.assertIn("--ingest", r["next_command"])

    def test_search_falls_back_to_bundled_catalog(self):
        code, r = self.cli("--search", "interest expense")
        self.assertEqual(code, 0, r)
        self.assertIn("bundled catalog.json", r["searched"])
        eps = [h["endpoint"] for h in r["results"]]
        self.assertIn("v2/accounting/od/interest_expense", eps)
        hit = next(h for h in r["results"] if h["endpoint"] == "v2/accounting/od/interest_expense")
        self.assertIn("guidance", hit)                      # curated datasets.md row merged in

    def test_endpoints_lists_curated(self):
        code, r = self.cli("--endpoints")
        self.assertEqual(code, 0, r)
        self.assertGreater(r["all_endpoints"], 180)
        self.assertTrue(any(e["endpoint"] == "v1/accounting/mts/mts_table_9" for e in r["curated"]))

    def test_fields(self):
        code, r = self.cli("--fields", "v2/accounting/od/debt_to_penny")
        self.assertEqual(code, 0, r)
        self.assertIn("record_date", [f["field"] for f in r["fields"]])
        self.assertTrue(r["last_row"]["record_date"] >= "2026-10-01")

    def test_ingest_uploaded_csv(self):
        sp, out = self.write_spec(SPEC)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(FX, "upload_debt.csv"), os.path.join(up, "debt.csv"))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        self.assertEqual(self.csv(out, "debt")[0]["tot_pub_debt_out_amt"], "40112464593206.30")

    def test_catalog_marks_stale_mcp_entries(self):
        cat = json.load(open(os.path.join(PACK, "catalog.json"), encoding="utf-8"))["endpoints"]
        stale = [e["endpoint"] for e in cat if e.get("status")]
        self.assertIn("v2/debt/tror/delinquent_debt", stale)
        self.assertTrue(all(not e["in_live_catalog"] for e in cat if e.get("status")))

    def test_tstats_reads_new_layout(self):
        code, r, out = self.fetch(SPEC)
        ts = load("treasury_pack_tstats", os.path.join(PACK, "lib", "tstats.py"))
        df = ts.load(out, "debt")
        self.assertEqual(len(df), 21)
        # the helpers travel with the data, so analysis.py reruns without the skill folder (SKILL.md section 4)
        self.assertTrue(os.path.exists(os.path.join(out, "lib", "treasury", "tstats.py")))
        self.assertTrue(os.path.exists(os.path.join(out, "lib", "facts.py")))
        self.assertEqual(str(df["record_calendar_month"].dtype), "Int64")
        self.assertAlmostEqual(float(df["tot_pub_debt_out_amt"].iloc[0]), 40112464593206.30, places=2)

    def test_fy_gdp_reads_the_bea_pack(self):
        # GDP fetched with the bea pack (routing's treasury + bea pairing), no --context gdp file
        import tempfile
        ts = load("treasury_pack_tstats", os.path.join(PACK, "lib", "tstats.py"))
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "data", "bea"))
            rows = ["series_code,frequency,date,value,unit_mult"] + [
                f"A191RC,Q,{d},{v},6" for d, v in (("2023-10-01", 28000000), ("2024-01-01", 28400000),
                                                   ("2024-04-01", 28800000), ("2024-07-01", 29200000))]
            open(os.path.join(out, "data", "bea", "bea_data.csv"), "w").write("\n".join(rows) + "\n")
            r = ts.fy_gdp(out).set_index("fiscal_year")
        self.assertAlmostEqual(r.loc[2024, "gdp_billions"], 28600.0)      # Oct 2023-Sep 2024, billions
        self.assertTrue(r.loc[2024, "complete"])


class TestTreasuryUnits(unittest.TestCase):
    def test_split_filter_moves_unsafe_values_local(self):
        f = load("treasury_pack_fetch", os.path.join(PACK, "fetch.py"))
        api, local = f.split_filter({"record_date": {"gte": "2020-01-01"},
                                     "security_desc": ["Foreign Series Notes & Bonds", "Treasury Bills"]})
        self.assertIn("record_date", api)
        self.assertIn("security_desc", local)
        self.assertEqual(f.filter_string({"a": "x", "b": ["1", "2"], "c": {"lte": "3"}}), "a:eq:x,b:in:(1,2),c:lte:3")

    def test_unit_hint_reads_data_formats(self):
        f = load("treasury_pack_fetch", os.path.join(PACK, "fetch.py"))
        t = {"issued_amt": "CURRENCY0", "outstanding_amt": "CURRENCY0", "yield_pct": "PERCENTAGE"}
        mil = {"issued_amt": "$1,000,000", "outstanding_amt": "$1,000,000"}    # MSPD table 3 [live 2026-10-21]
        self.assertIn("issued_amt, outstanding_amt are in MILLIONS", f.unit_hint("v1/debt/mspd/mspd_table_3_market", t, t, mil))
        self.assertIn("not confirmed", f.unit_hint("v1/debt/mspd/mspd_table_3_market", t, t, {}))   # an upload
        d = {"tot_pub_debt_out_amt": "CURRENCY"}
        self.assertEqual(f.unit_hint("v2/accounting/od/debt_to_penny", d, d, {"tot_pub_debt_out_amt": "$10.20"}),
                         "Amounts are in dollars and cents.")
        r = {"avg_interest_rate_amt": "PERCENTAGE"}         # an _amt field that is a rate, not money
        self.assertNotIn("dollars", f.unit_hint("v2/accounting/od/avg_interest_rates", r, r, {"avg_interest_rate_amt": "10.2%"}))
        m = {"mil_amt": "CURRENCY"}                         # millions by name though its format says $10.20
        self.assertIn("mil_amt are in MILLIONS", f.unit_hint("v1/accounting/mts/x", m, m, {"mil_amt": "$10.20"}))

    def test_values_lists_every_value(self):
        # --values is asked for a category list: it must list them all, not a few examples (the README keeps 40)
        f = load("treasury_pack_fetch", os.path.join(PACK, "fetch.py"))
        rows = [{"transaction_catg": f"Line {i:03d}", "record_date": "2026-01-02"} for i in range(120)]
        cols = ["transaction_catg", "record_date"]
        short = f.value_summary(rows, cols)["columns"]["transaction_catg"]
        self.assertEqual(short["distinct"], 120)
        self.assertIn("--values", short["all_values"])
        full = f.value_summary(rows, cols, max_values=f.MAX_VALUES_CLI)["columns"]["transaction_catg"]
        self.assertEqual(len(full), 120)
        self.assertIn("Line 119", full)
        many = [{"transaction_catg": f"Line {i:03d}"} for i in range(250)]      # capped, with how to see the rest
        top = f.value_summary(many, ["transaction_catg"], max_values=f.MAX_VALUES_CLI)["columns"]["transaction_catg"]
        self.assertEqual((top["distinct"], len(top["values"])), (250, 200))
        self.assertIn("+50 more", top["more"])

    def test_string_filter_applied_on_ingest(self):
        f = load("treasury_pack_fetch", os.path.join(PACK, "fetch.py"))
        self.assertEqual(f.parse_filter_string("record_date:gte:2020-01-01,type:in:(Bills,Notes),record_date:lte:2025-09-30"),
                         {"record_date": {"gte": "2020-01-01", "lte": "2025-09-30"}, "type": {"in": ["Bills", "Notes"]}})
        rows = [{"record_date": "2019-12-31", "type": "Bills"}, {"record_date": "2021-01-31", "type": "Notes"},
                {"record_date": "2021-01-31", "type": "Bonds"}, {"record_date": "2026-01-31", "type": "Bills"}]
        kept = f._apply_filter(rows, "record_date:gte:2020-01-01,type:in:(Bills,Notes),record_date:lte:2025-09-30")
        self.assertEqual(kept, [rows[1]])

    def test_period_sum_refuses_cumulative_columns(self):
        import pandas as pd
        ts = load("treasury_pack_tstats", os.path.join(PACK, "lib", "tstats.py"))
        df = pd.DataFrame({"record_date": pd.to_datetime(["2024-10-31", "2024-11-30"]), "month_expense_amt": [1.0, 2.0],
                           "fytd_expense_amt": [1.0, 3.0]})
        with self.assertRaises(ValueError) as e:            # summing running totals counts months over and over
            ts.period_sum(df, "FY", ["fytd_expense_amt"])
        self.assertIn("period_end", str(e.exception))
        self.assertEqual(float(ts.period_sum(df, "FY", ["month_expense_amt"])["month_expense_amt"].iloc[0]), 3.0)


if __name__ == "__main__":
    unittest.main()

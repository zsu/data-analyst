"""Offline tests for the omb pack (OMB budget files on GovInfo).

Fixtures were recorded live from www.govinfo.gov on 2026-10-30 (keyless). Whole real files: Historical Tables
1.1, 3.1 and 7.1 of the FY2027 edition (BUDGET-2027-TAB-2-1/4-1/8-1.xlsx), the FY2027 Public Budget Database
receipts file (BUDGET-2027-DB-3.xlsx), and GovInfo's error page (govinfo_error.html), which GovInfo serves (after
a 302) for every missing file. Trimmed real copies (cells unchanged, other parts removed):
  - tab2026_package_trimmed.zip: BUDGET-2026-TAB.zip (3.1 MB) with only its workbook BUDGET-2026-TAB.xlsx, cut to
    the sheets "Table of Contents" (first 12 rows) and hist01z1 (Table 1.1, whole);
  - tab2027_package_trimmed.zip: BUDGET-2027-TAB.zip (1.3 MB) with only the members for Tables 1.1 and 7.1;
  - fcs2027_trimmed.xlsx: BUDGET-2027-FCS.xlsx (294 KB) with sheets Introduction, Table1 (whole) and Table2
    (first 30 rows).
"""
import json
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

SPEC = {"description": "Historical Tables 1.1, 3.1 and 7.1 (FY2027), receipts by account, direct loan subsidy rates",
        "requests": [
            {"name": "summary", "endpoint": "historical_table", "params": {"fy": 2027, "table": "1.1"}},
            {"name": "functions", "endpoint": "historical_table", "params": {"fy": 2027, "table": "3.1"}},
            {"name": "debt", "endpoint": "historical_table", "params": {"fy": 2027, "table": "table_7_1"}},
            {"name": "receipts", "endpoint": "budget_database", "params": {"fy": 2027, "kind": "receipts"}},
            {"name": "direct_loans", "endpoint": "credit_supplement", "params": {"fy": 2027, "table": 1}}]}


class TestOmb(PackTest):
    source = "omb"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        s = self.csv(out, "summary")
        self.assertEqual(list(s[0])[:4], ["Year", "Total Receipts", "Total Outlays", "Total Surplus or Deficit (-)"])
        self.assertIn("Off-Budget Outlays", s[0])
        y24 = next(x for x in s if x["Year"] == "2024")
        self.assertEqual((y24["Total Outlays"], y24["Total Receipts"], y24["sheet_row"]), ("6735261", "4919884", "131"))
        self.assertEqual(s[-1]["Year"], "2025")                         # the footnotes are not rows
        self.assertEqual(s[0]["Off-Budget Outlays"], "..........")      # published marker kept
        notes = " ".join(r["notes"])
        self.assertIn("(in millions of dollars)", notes)
        self.assertIn("$500 thousand or less", notes)
        self.assertIn("no estimate years", notes)
        # Table 3.1: three unit blocks; the total appears once per block, told apart by units_block
        f = self.csv(out, "functions")
        tot = [x for x in f if x["Superfunction and Function"] == "Total, Federal outlays"]
        self.assertEqual([x["units_block"] for x in tot],
                         ["In millions of dollars", "As percentages of outlays", "As percentages of GDP"])
        self.assertEqual([x["1940"] for x in tot], ["9468", "100", "9.6"])
        self.assertIn("2031 estimate", f[0])
        self.assertIn("6 columns marked estimate", notes)
        # Table 7.1: a three-row header joined top-down
        d = self.csv(out, "debt")
        col = "In Millions of Dollars Equals: Held by the Public Total"
        self.assertEqual(next(x for x in d if x["End of Fiscal Year"] == "2024")[col], "28193866")
        self.assertEqual(next(x for x in d if x["End of Fiscal Year"] == "2019")["As Percentages of GDP Equals: "
                                                                               "Held by the Public Total"], "78.9")
        # Public Budget Database receipts: thousands of dollars; FY2024 adds up to Table 1.1's total receipts
        rc = self.csv(out, "receipts")
        self.assertEqual(len(rc), 259)
        self.assertEqual(list(rc[0])[:3], ["Source Category Code", "Source Category Name", "Source Subcategory"])
        self.assertEqual(rc[0]["Agency Code"], "009")                   # codes keep their leading zeros
        self.assertEqual(sum(int(x["2024"]) for x in rc), 4919884 * 1000)
        self.assertIn("thousands of dollars", notes)
        # Federal Credit Supplement Table 1: leader dots removed, label rows kept
        dl = self.csv(out, "direct_loans")
        self.assertEqual(dl[0]["Agency, Bureau, Program and Risk Category"], "Department of Agriculture")
        self.assertEqual(dl[0]["2027 Subsidy rate (percent)"], "")
        fo = next(x for x in dl if x["Agency, Bureau, Program and Risk Category"] == "Farm Ownership")
        self.assertEqual((fo["BEA Category"], fo["2026 Subsidy rate (percent)"], fo["2027 Subsidy rate (percent)"],
                          fo["2027 Obligations"]), ("Discretionary", "1.27", "2.47", "1241992"))

    def test_edition_without_granules_reads_the_package_zip(self):
        """FY2026's granule URLs redirect to GovInfo's error page; Table 1.1 comes from the package's workbook."""
        spec = {"description": "Table 1.1, FY2026 edition",
                "requests": [{"name": "t11", "endpoint": "historical_table", "params": {"fy": 2026, "table": "1.1"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "t11")
        self.assertEqual(next(x for x in rows if x["Year"] == "2024")["Total Outlays"], "6750856")   # FY2026 edition
        self.assertEqual(rows[-1]["Year"], "2024")
        notes = " ".join(r["notes"])
        self.assertIn("BUDGET-2026-TAB.zip", notes)
        self.assertIn("hist01z1", notes)

    def test_missing_table_is_a_clear_error_not_an_empty_table(self):
        spec = {"description": "a table the edition does not have",
                "requests": [{"name": "t19", "endpoint": "historical_table", "params": {"fy": 2027, "table": "1.9"}}]}
        code, r, out = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertEqual(r.get("kind"), "not_found", r)
        self.assertIn("Table 1.9 is not in the FY2027 Historical Tables", r["message"])
        self.assertIn("1.1, 7.1", r["message"])                         # what the package holds
        self.assertFalse(os.path.exists(os.path.join(out, "data", "omb", "t19.csv")))

    def test_missing_edition_names_the_urls(self):
        spec = {"description": "FY2026 has no Public Budget Database on GovInfo",
                "requests": [{"name": "db", "endpoint": "budget_database", "params": {"fy": 2026, "kind": "outlays"}}]}
        code, r, out = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertIn("no Public Budget Database outlays file for the FY2026 budget", r["message"])
        self.assertIn("BUDGET-2026-DB/csv/BUDGET-2026-DB-2.csv", r["message"])

    def test_bad_specs_are_fix_spec(self):
        for params, ep in (({"fy": 2027, "table": "debt"}, "historical_table"),
                           ({"fy": 1900, "table": "1.1"}, "historical_table"),
                           ({"fy": 2027}, "historical_table"),
                           ({"fy": 2027, "kind": "spending"}, "budget_database"),
                           ({"fy": 2027, "table": "1.1"}, "credit_supplement")):
            code, r = self.check({"requests": [{"name": "x", "endpoint": ep, "params": params}]})
            self.assertEqual(r["route"], "fix_spec", (params, r))

    def test_search_catalog(self):
        code, r = self.cli("--search", "federal debt end of year")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["results"][0]["alias"], "table_7_1", r)
        code, r = self.cli("--search", "credit subsidy rate direct loan")
        self.assertIn("fcs_table1", [x.get("alias") for x in r["results"]], r)

    def test_manual_bundle_and_ingest(self):
        spec = {"description": "Table 1.1 FY2027 and FY2026 by hand",
                "requests": [{"name": "t11", "endpoint": "historical_table", "params": {"fy": 2027, "table": "1.1"}},
                             {"name": "t11_26", "endpoint": "historical_table", "params": {"fy": 2026, "table": "1.1"}},
                             {"name": "rc", "endpoint": "budget_database", "params": {"fy": 2027, "kind": "receipts"}}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(items[0]["url"],
                         "https://www.govinfo.gov/content/pkg/BUDGET-2027-TAB/xls/BUDGET-2027-TAB-2-1.xlsx")
        self.assertIn("BUDGET-2026-TAB.zip", items[1]["note"])
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "tab2027_2_1.xlsx"), os.path.join(up, "t11.xlsx"))
        shutil.copy(os.path.join(self.fixtures, "tab2026_package_trimmed.zip"), os.path.join(up, "t11_26.zip"))
        shutil.copy(os.path.join(self.fixtures, "db2027_3_receipts.xlsx"), os.path.join(up, "rc.xlsx"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual(next(x for x in self.csv(out, "t11") if x["Year"] == "2024")["Total Outlays"], "6735261")
        self.assertEqual(next(x for x in self.csv(out, "t11_26") if x["Year"] == "2024")["Total Outlays"], "6750856")
        self.assertEqual(len(self.csv(out, "rc")), 259)

    def test_ingest_wrong_table_is_refused(self):
        spec = {"description": "Table 3.2 asked, Table 1.1 uploaded",
                "requests": [{"name": "t32", "endpoint": "historical_table", "params": {"fy": 2027, "table": "3.2"}}]}
        sp, out = self.write_spec(spec)
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "tab2027_2_1.xlsx"), os.path.join(up, "t32.xlsx"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertNotEqual(code, 0, r)
        self.assertIn("Table 3.2", r.get("message", ""))

    def test_parse_grid_rules(self):
        """Header joining, footer cut and number cleanup on the real Table 1.1 grid."""
        a = load_pack_module("omb")
        from core.generic_rest import xlsx_sheets
        grid = next(iter(xlsx_sheets(os.path.join(self.fixtures, "tab2027_2_1.xlsx")).values()))
        rows, meta = a.parse_grid(grid)
        self.assertEqual(meta["header"], (3, 4))
        self.assertEqual(meta["top"][1], "(in millions of dollars)")
        self.assertEqual(len(meta["footer"]), 2)
        self.assertEqual(rows[0]["sheet_row"], 5)
        self.assertEqual(a._num_clean("78.900000000000006"), "78.9")
        self.assertEqual(a._num_clean("6.3600000000000004E-2"), "0.0636")
        self.assertEqual(a._num_clean("1160.0"), "1160.0")
        self.assertEqual(a._norm_table("Table 8.5"), "8.5")


if __name__ == "__main__":
    unittest.main()

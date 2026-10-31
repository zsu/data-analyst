"""acf pack: manual route only (acf.gov answers every script with an AWS WAF JavaScript challenge, HTTP 202).

Fixtures are archived copies of real ACF workbooks (acf.gov refuses scripts, so they come from the Internet
Archive's captures of the acf.gov file URLs, downloaded 2026-10-30). Each stands in for the file a user saves from
the browser and uploads:
- fy-2024-tanf-moe-financial-data.xlsx: https://acf.gov/sites/default/files/documents/ofa/fy-2024-tanf-moe-financial-data.xlsx
  (capture 2026-05-03), trimmed to the sheets "A.1 Fed & State by Category", "A.5 FY24 Federal TANF Funds",
  "B. Total Expenditures", "DC" and "F. Appendix" (other sheets, defined names, calcChain and the doc-properties
  sheet list removed; cells unchanged).
- fy2025-tanf-caseload.xlsx: .../ofa/fy2025-tanf-caseload.xlsx (capture 2026-08-07), trimmed to TFam, TRec, Mar25
  and FYCY2025-Families.
- fy2021_tanf_caseload.xlsx: .../ofa/fy2021_tanf_caseload.xlsx (capture 2026-03-29), trimmed to "Total Number of
  Families" (month headers stored as text "Oct-20") and "FY2021-Recipients".
- ccdf-expenditures-for-fy-2022-all-appropriation-years.xlsx: .../occ/ccdf-expenditures-for-fy-2022-all-appropriation-years.xlsx
  (capture 2026-03-28), the whole file (85 KB).
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

FIN = {"description": "FY 2024 TANF and MOE spending",
       "requests": [{"name": "categories", "endpoint": "tanf_financial", "params": {"fiscal_year": 2024}}]}
FIN_FILE = "fy-2024-tanf-moe-financial-data.xlsx"
CASE_FILE = "fy2025-tanf-caseload.xlsx"
CCDF_FILE = "ccdf-expenditures-for-fy-2022-all-appropriation-years.xlsx"


class TestAcf(PackTest):
    source = "acf"
    sample_spec = FIN

    def ingest(self, spec, files):
        up = os.path.join(self.tmp.name, "upload")
        os.makedirs(up, exist_ok=True)
        for f in files:
            shutil.copy(os.path.join(self.fixtures, f), up)
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        return code, r, out

    def test_check_gives_manual_download(self):
        code, r = self.check(FIN)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download", r)
        self.assertIn("AWS WAF", r["message"])
        self.assertEqual(r["probed"], {})                       # nothing is sent to acf.gov
        with open(os.path.join(r["manual_bundle"], "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual([(i["url"], i["filename"]) for i in man["items"]],
                         [("https://acf.gov/sites/default/files/documents/ofa/" + FIN_FILE, FIN_FILE)])
        self.assertIn("web browser", man["items"][0]["note"])
        self.assertIn("--ingest", r["next_command"])

    def test_fetch_is_blocked(self):
        code, r, _ = self.fetch(FIN)
        self.assertEqual(code, 4, r)
        self.assertEqual((r["status"], r["kind"], r["route"]), ("blocked", "blocked", "manual_download"))

    def test_manual_links(self):
        a = load_pack_module("acf")
        spec = {"requests": [
            {"name": "ssp", "endpoint": "tanf_caseload", "params": {"fiscal_year": 2024, "program": "ssp"}},
            {"name": "both", "endpoint": "tanf_caseload", "params": {"fiscal_year": 2023, "program": "tanf_ssp"}},
            {"name": "ccdf19", "endpoint": "ccdf_expenditures", "params": {"fiscal_year": 2019}},
            {"name": "gy21", "endpoint": "ccdf_expenditures", "params": {"fiscal_year": 2021, "file": "grant_year"}},
            {"name": "fin19", "endpoint": "tanf_financial", "params": {"fiscal_year": 2019}},
            {"name": "ccdf16", "endpoint": "ccdf_expenditures", "params": {"fiscal_year": 2016}}]}
        items = {i["request"]: i for i in a.manual_items(spec, None)}
        b = "https://acf.gov/sites/default/files/documents/"
        self.assertEqual(items["ssp"]["url"], b + "ofa/fy2024-15months-ssp-caseload.xlsx")
        self.assertEqual(items["both"]["url"], b + "ofa/fy2023_tanssp_caseload.xlsx")
        self.assertEqual((items["ccdf19"]["url"], items["ccdf19"]["filename"]),
                         (b + "occ/ALL%20YEARS%20FY%202019%20Expenditures.xlsx", "ALL YEARS FY 2019 Expenditures.xlsx"))
        self.assertEqual(items["gy21"]["url"], b + "occ/ccdf-expenditures-for-gy-2021-as-of-9-30-2022-tables-1b-12b.xlsx")
        # a year without a verified file name: the year's page, and a file name for the upload
        self.assertEqual(items["fin19"]["url"], "https://acf.gov/ofa/data/tanf-financial-data-fy-2019")
        self.assertEqual(items["fin19"]["filename"], "fin19_tanf_financial_2019.xlsx")
        self.assertIn("no verified file link", items["fin19"]["note"])
        self.assertEqual(items["ccdf16"]["url"], "https://acf.gov/occ/resource/fy-2016-ccdf-state-expenditure-data")

    def test_bad_specs_refused(self):
        for params, words in [({"fiscal_year": "last year"}, "four-digit year"),
                              ({"fiscal_year": 1990}, "1997 on"),
                              ({}, "'fiscal_year' is required"),
                              ({"fiscal_year": 2024, "program": "snap"}, "program is a tanf_caseload parameter"),
                              ({"fiscal_year": 2024, "header_row": "two"}, "header_row")]:
            code, r = self.check({"requests": [{"name": "x", "endpoint": "tanf_financial", "params": params}]})
            self.assertEqual((code, r["route"]), (2, "fix_spec"), r)
            self.assertIn(words, r["message"])
        code, r = self.check({"requests": [{"name": "x", "endpoint": "tanf_caseload",
                                            "params": {"fiscal_year": 2024, "program": "medicaid"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        code, r = self.check({"requests": [{"name": "x", "endpoint": "ccdf_expenditures",
                                            "params": {"fiscal_year": 2016, "file": "grant_year"}}]})
        self.assertIn("GY 2018", r["message"])

    def test_tanf_financial_upload(self):
        spec = {"description": "FY 2024 TANF financial data",
                "requests": [{"name": "a1", "endpoint": "tanf_financial", "params": {"fiscal_year": 2024}},
                             {"name": "funds", "endpoint": "tanf_financial", "params": {"fiscal_year": 2024, "table": "A.5"}},
                             {"name": "b", "endpoint": "tanf_financial", "params": {"fiscal_year": 2024, "table": "B"}},
                             {"name": "dc", "endpoint": "tanf_financial",
                              "params": {"fiscal_year": 2024, "table": "District of Columbia"}},
                             {"name": "f", "endpoint": "tanf_financial", "params": {"fiscal_year": 2024, "table": "F"}}]}
        code, r, out = self.ingest(spec, [FIN_FILE])
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        a1 = {x["Spending Category"]: x for x in self.csv(out, "a1")}
        self.assertEqual(list(self.csv(out, "a1")[0]),
                         ["row", "Spending Category", "Federal Funds", "State MOE in TANF and Separate State Programs",
                          "All Funds", "Percent of Total Funds Used", "sheet"])
        ba = a1["Basic Assistance"]
        self.assertEqual((ba["Federal Funds"], ba["State MOE in TANF and Separate State Programs"], ba["All Funds"]),
                         ("3162142819.87", "5023870603.12", "8186013422.99"))
        self.assertEqual(ba["Percent of Total Funds Used"], "0.218202265449694")     # a fraction: 21.8%
        self.assertEqual(a1["TOTAL FUNDS USED"]["All Funds"], "37515712342.03")
        self.assertEqual(a1["Transferred to CCDF Discretionary"]["State MOE in TANF and Separate State Programs"], "")
        self.assertEqual(ba["sheet"], "A.1 Fed & State by Category")
        funds = self.csv(out, "funds")
        self.assertEqual([(x["section"], x["label"], x["value"]) for x in funds][-2:],
                         [("Carryover from Prior Years", "State Family Assistance Grant", "9783404395.36"),
                          ("Carryover from Prior Years", "Total Funds Available", "26603502932.57")])
        b = {x["state_code"]: x for x in self.csv(out, "b")}
        self.assertEqual(len(b), 52)                            # U.S. total, 50 states, DC
        self.assertEqual((b["US"]["STATE"], b["DC"]["STATE"]), ("U.S. TOTAL", "DIST.OF COLUMBIA"))
        self.assertEqual(b["AL"]["6. Basic Assistance"], "32124953.24")
        self.assertEqual(b["US"]["24. Total Expenditures"], "35000349713.11")
        self.assertIn("6.a. Basic Assistance - (excluding Relative Foster Care Maintenance Payments and Adoption and "
                      "Guardianship Subsidies)", b["AL"])
        dc = {x["Spending Category"]: x for x in self.csv(out, "dc")}
        self.assertEqual(dc["Basic Assistance"]["All Funds"], "51920224.78")
        f = self.csv(out, "f")
        self.assertEqual({x["block"] for x in f}, {"Transitional Services for Employed", "Job Access"})
        ja = [x for x in f if x["block"] == "Job Access" and x["state_code"] == "US"][0]
        self.assertEqual((ja["Total"], ja["Federal"], ja["State"]), ("3345456.19", "2100000", "1245456.19"))

    def test_tanf_caseload_upload(self):
        spec = {"description": "FY 2025 TANF caseload",
                "requests": [{"name": "fam", "endpoint": "tanf_caseload", "params": {"fiscal_year": 2025}},
                             {"name": "rec", "endpoint": "tanf_caseload",
                              "params": {"fiscal_year": 2025, "table": "recipients"}},
                             {"name": "avg", "endpoint": "tanf_caseload",
                              "params": {"fiscal_year": 2025, "table": "avg_families"}},
                             {"name": "mar", "endpoint": "tanf_caseload", "params": {"fiscal_year": 2025, "table": "2025-03"}}]}
        code, r, out = self.ingest(spec, [CASE_FILE])
        self.assertEqual(code, 0, r)
        fam = self.csv(out, "fam")
        self.assertEqual(list(fam[0])[:4], ["row", "State", "state_code", "2024-10"])
        self.assertIn("2025-12", fam[0])
        self.assertIn("Average CY 2025", fam[0])
        us = fam[0]
        self.assertEqual((us["state_code"], us["2024-10"], us["2025-09"]), ("US", "873701", "832409"))
        self.assertEqual(us["Average FY 2025"], "849457.833333333")
        self.assertEqual(len(fam), 55)                          # U.S., 50 states, DC, Guam, Puerto Rico, Virgin Islands
        self.assertEqual({x["state_code"] for x in fam if x["State"] in ("Guam", "Virgin Islands")}, {"GU", "VI"})
        rec = {x["state_code"]: x for x in self.csv(out, "rec")}
        self.assertEqual(rec["US"]["2024-10"], "2154454")
        avg = self.csv(out, "avg")
        self.assertEqual({x["block"] for x in avg}, {"Fiscal Year 2025 (October - September)",
                                                      "Calendar Year 2025 (January - December)"})
        cy = [x for x in avg if x["block"].startswith("Calendar") and x["state_code"] == "US"][0]
        self.assertEqual((cy["Total Families"], cy["No Parent Families"]), ("836788.333333333", "326938.833333333"))
        mar = {x["state_code"]: x for x in self.csv(out, "mar")}
        self.assertEqual((mar["US"]["Total Families"], mar["US"]["Adults"], mar["US"]["Children"]),
                         ("848807", "577110", "1514959"))
        self.assertEqual(mar["US"]["Total Families"], fam[0]["2025-03"])

    def test_caseload_older_layout(self):
        """FY 2021: other sheet names, month headers stored as text ("Oct-20"), FY-only averages."""
        spec = {"requests": [{"name": "fam", "endpoint": "tanf_caseload", "params": {"fiscal_year": 2021}},
                             {"name": "avg", "endpoint": "tanf_caseload",
                              "params": {"fiscal_year": 2021, "table": "avg_recipients"}}]}
        code, r, out = self.ingest(spec, ["fy2021_tanf_caseload.xlsx"])
        self.assertEqual(code, 0, r)
        fam = self.csv(out, "fam")
        self.assertEqual((fam[0]["2020-10"], fam[0]["sheet"]), ("865205", "Total Number of Families"))
        self.assertIn("Average FY 2021", fam[0])
        avg = self.csv(out, "avg")
        self.assertNotIn("block", avg[0])
        self.assertEqual((avg[0]["state_code"], avg[0]["Total Recipients"], avg[0]["Adults"]),
                         ("US", "1847643.75", "422884.833333333"))

    def test_ccdf_upload(self):
        spec = {"requests": [{"name": "funds", "endpoint": "ccdf_expenditures", "params": {"fiscal_year": 2022}},
                             {"name": "cats", "endpoint": "ccdf_expenditures", "params": {"fiscal_year": 2022, "table": "3a"}},
                             {"name": "by_gy", "endpoint": "ccdf_expenditures",
                              "params": {"fiscal_year": 2022, "table": "Table 1a"}}]}
        code, r, out = self.ingest(spec, [CCDF_FILE])
        self.assertEqual(code, 0, r)
        funds = {x["state_code"]: x for x in self.csv(out, "funds")}
        self.assertEqual(funds["AL"]["Mandatory"], "16441707")
        self.assertEqual(funds["US"]["State"], "Total")
        self.assertEqual(funds["US"]["Total"], "29097749868.57")
        self.assertEqual(funds["AS"]["Total"], "-5965217.77")          # negative adjustments as published
        cats = {x["state_code"]: x for x in self.csv(out, "cats")}
        self.assertEqual(cats["AL"]["Direct Services"], "233813020.42")
        gy = {x["label"]: x for x in self.csv(out, "by_gy")}
        self.assertEqual((gy["Mandatory"]["GY 2022"], gy["Total"]["Total by Fund"]), ("913695732", "29097749869"))

    def test_wrong_table_or_file(self):
        code, r, _ = self.ingest({"requests": [{"name": "x", "endpoint": "tanf_financial",
                                                "params": {"fiscal_year": 2024, "table": "Z.9"}}]}, [FIN_FILE])
        self.assertEqual(code, 2, r)
        self.assertIn("no table 'Z.9'", r["message"])
        self.assertIn("A.1 Fed & State by Category", r["message"])
        code, r, _ = self.ingest({"requests": [{"name": "x", "endpoint": "tanf_caseload",
                                                "params": {"fiscal_year": 2025, "table": "2023-01"}}]}, [CASE_FILE])
        self.assertEqual(code, 2, r)
        self.assertIn("no sheet for 2023-01", r["message"])
        # the empty WAF challenge page saved instead of the workbook
        up = os.path.join(self.tmp.name, "upload2")
        os.makedirs(up)
        with open(os.path.join(up, FIN_FILE), "wb") as f:
            f.write(b"")
        sp, out = self.write_spec(FIN)
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 2, r)
        self.assertIn("not an Excel workbook", r["message"])

    def test_tidy_units(self):
        a = load_pack_module("acf")
        self.assertEqual(a._month_header("45566"), "2024-10")
        self.assertEqual(a._month_header("Oct-20"), "2020-10")
        self.assertEqual(a._month_header("Average FY 2025"), "Average FY 2025")
        self.assertEqual(a._cell("3162142819.8699999"), "3162142819.87")
        self.assertEqual(a.state_code("DIST. OF COLUMBIA"), "DC")
        self.assertEqual(a.state_code("U.S. Totals"), "US")
        self.assertEqual(a.state_code("Spending Category"), "")
        rows = a.tidy([["Title"], ["State", "Total", "Adults"], ["Alabama", "5", "-"], ["Alaska", "3", "1"],
                       ["Arizona", "2", "1"], ["Notes:"], ['"-" - data inapplicable']])
        self.assertEqual(rows[0], {"row": 3, "State": "Alabama", "state_code": "AL", "Total": "5", "Adults": ""})
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()

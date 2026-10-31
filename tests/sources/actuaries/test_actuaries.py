import json
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

# Fixtures, all recorded or downloaded from the live sites on 2026-10-30:
# - cms_*_page.html: the three CMS pages the pack reads links from (Trustees Report & Trust Funds, NHE historical,
#   NHE projected), trimmed to their <main> element (head, navigation menu and footer dropped; every link the
#   pack reads is inside <main>).
# - the NHE summary, type-of-service-by-source-of-funds and historical-and-projections data ZIPs: as recorded.
# - medicare_tr_2026_trimmed.zip, nhe_tables_trimmed.zip, nhe_projections_tables_trimmed.zip: real copies of
#   2026-expanded-supplementary-tables-figures.zip (687 KB), nhe-tables.zip (520 KB) and nhe-projections-tables.zip
#   (259 KB) keeping only the members the tests read (each member byte for byte), so each fixture stays < 200 KB.
# - SingleYearTRTables_TR2026.xlsx: the 2026 OASDI Trustees Report single-year tables workbook. www.ssa.gov refuses
#   scripts (HTTP 403), so this copy comes from the Internet Archive's capture of
#   https://www.ssa.gov/oact/tr/2026/SingleYearTRTables_TR2026.xlsx (snapshot 2026-10-02), trimmed to the sheets
#   IV.B1, V.A3 and VI.G2 (other sheets, print settings and defined names removed; cells unchanged). It stands in
#   for the file a user saves from the browser and uploads.
SPEC = {"description": "2026 Medicare Trustees Report HI trust fund operations",
        "requests": [{"name": "hi_ops", "endpoint": "medicare_tr_table",
                      "params": {"table": "Operations of the Hospital Insurance (HI) Trust Fund"}}]}
SSA = {"description": "2026 OASDI Trustees Report: combined trust fund operations",
       "requests": [{"name": "oasdi_ops", "endpoint": "ssa_tr_table", "params": {"report_year": 2026, "table": "VI.G2"}}]}


class TestActuaries(PackTest):
    source = "actuaries"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_medicare_table(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "hi_ops")
        self.assertEqual(list(rows[0])[:3], ["row", "Calendar Year", "Payroll Taxes (Income)"])
        self.assertIn("Balance at end of year (see note 21) (Trust Fund)", rows[0])
        by = {x["Calendar Year"]: x for x in rows}
        self.assertEqual((rows[0]["Calendar Year"], rows[-1]["Calendar Year"], len(rows)), ("1966", "2035", 70))
        self.assertEqual(by["1966"]["Payroll Taxes (Income)"], "1858")        # published "$1,858" in the CSV twin
        self.assertEqual(by["1966"]["Income from taxation of benefits (Income)"], "")     # published as a dash
        bal = "Balance at end of year (see note 21) (Trust Fund)"
        self.assertEqual((by["2032"][bal], by["2033"][bal]), ("28111", "-56909"))  # HI reserves run out in 2033
        self.assertTrue(any("2026 Medicare Trustees Report" in n and "sheet HI" in n for n in r["notes"]), r["notes"])

    def test_medicare_files_and_other_tables(self):
        spec = {"description": "Medicare TR tables",
                "requests": [{"name": "files", "endpoint": "medicare_tr_files", "params": {"report_year": 2026}},
                             {"name": "rates", "endpoint": "medicare_tr_table", "params": {"table": "HI Cost and Income Rates"}},
                             {"name": "tp_gdp", "endpoint": "medicare_tr_table", "params": {"table": "Ratio of HI Taxable Payroll"}},
                             {"name": "partd", "endpoint": "medicare_tr_table", "params": {"table": "Part D"}},
                             {"name": "notes", "endpoint": "medicare_tr_table",
                              "params": {"table": "HI Cost and Income Rates", "sheet": "Notes"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        files = self.csv(out, "files")
        self.assertEqual({x["table"] for x in files},       # the CSV/ twin of an .xlsx table is not listed
                         {"hi cost and income rates", "ratio of hi taxable payroll to gdp",
                          "operations of the hospital insurance (hi) trust fund for calendar years",
                          "operations of the part d account of the supplementary medical insurance (smi) trust fund "
                          "for calendar years"})
        self.assertIn(("hi cost and income rates", "HI Income & Cost Rates", "xlsx"),
                      {(x["table"], x["sheet"], x["format"]) for x in files})
        rates = {x["Calendar Year"]: x for x in self.csv(out, "rates")}
        self.assertEqual((rates["2026"]["Cost rates (%)"], rates["2026"]["Income rates (%)"]), ("3.49", "3.49"))
        # Excel percent-format cells are written as displayed: 0.371 shown as 37.1%
        tp = {x["CY"]: x for x in self.csv(out, "tp_gdp")}
        self.assertEqual(tp["1966"]["Ratio"], "37.1")
        self.assertTrue(any("percent-format" in n for n in r["notes"]), r["notes"])
        # years whose values are all dashes (Part D before 2004) stay rows with empty values
        pd_ = {x["Calendar year"]: x for x in self.csv(out, "partd")}
        self.assertEqual(pd_["1967"]["Government contributions (see note 2) (Income)"], "")
        self.assertEqual(pd_["2004"]["Government contributions (see note 2) (Income)"], "440")
        notes = self.csv(out, "notes")
        self.assertEqual(list(notes[0]), ["row", "text"])
        self.assertTrue(notes[-1]["text"].startswith("Note: Totals do not necessarily"))

    def test_wrong_sheet_lists_sheets(self):
        spec = {"requests": [{"name": "x", "endpoint": "medicare_tr_table",
                              "params": {"table": "HI Cost and Income Rates", "sheet": "Table 9"}}]}
        code, r, _ = self.fetch(spec)
        self.assertEqual(code, 2, r)
        self.assertIn("no sheet 'Table 9'", r["message"])
        self.assertIn("HI Income & Cost Rates, Notes", r["message"])

    def test_ambiguous_or_unknown_table(self):
        code, r, _ = self.fetch({"requests": [{"name": "x", "endpoint": "medicare_tr_table",
                                               "params": {"table": "Operations"}}]})
        self.assertEqual(code, 2, r)
        self.assertIn("matches 2 files", r["message"])
        code, r, _ = self.fetch({"requests": [{"name": "x", "endpoint": "medicare_tr_table",
                                               "params": {"table": "Part E"}}]})
        self.assertEqual(code, 2, r)
        self.assertIn("no table 'Part E'", r["message"])

    def test_report_year_checks(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "medicare_tr_table",
                                            "params": {"report_year": 2019, "table": "HI Cost"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("2022 on", r["message"])
        code, r = self.check({"requests": [{"name": "x", "endpoint": "medicare_tr_table", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_nhe_historical(self):
        spec = {"description": "NHE 2024",
                "requests": [{"name": "summary", "endpoint": "nhe_table", "params": {"file": "summary"}},
                             {"name": "summary_long", "endpoint": "nhe_table", "params": {"file": "summary", "layout": "long"}},
                             {"name": "funds", "endpoint": "nhe_table", "params": {"file": "service_funds"}},
                             {"name": "t3", "endpoint": "nhe_table", "params": {"file": "tables", "table": "Table 03"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        s = self.csv(out, "summary")
        self.assertEqual(list(s[0])[:6], ["row", "Expenditure Amount", "indent", "group", "parent", "1960"])
        nhe = s[0]
        self.assertEqual((nhe["Expenditure Amount"], nhe["1960"], nhe["2024"]),
                         ("National Health Expenditures (Amount in Billions)", "27.1", "5278.60"))  # "5,278.60"
        gdp = [x for x in s if x["Expenditure Amount"].startswith("National Health Expenditures as a Percent of Gross")]
        self.assertEqual(gdp[0]["2024"], "18")
        long_ = self.csv(out, "summary_long")
        self.assertEqual(list(long_[0])[-2:], ["year", "value"])
        self.assertIn({"year": "2024", "value": "18"},
                      [{"year": x["year"], "value": x["value"]} for x in long_ if x["row"] == gdp[0]["row"]])
        f = self.csv(out, "funds")
        lab = "Expenditure Amount (Millions)"
        self.assertEqual((f[0][lab], f[0]["2024"], f[0]["1960"]), ("Total National Health Expenditures", "5278588", "27122"))
        fed = [x for x in f if x["group"] == "Total National Health Expenditures" and x[lab] == "Federal"]
        self.assertEqual((fed[0]["parent"], fed[0]["2024"], fed[0]["1960"]), ("Medicaid (Title XIX)", "596694", ""))
        self.assertEqual(len(f), 539)
        hosp = [x for x in f if x["group"] == "Total Hospital Expenditures" and x[lab] == "Total Hospital Expenditures"]
        self.assertEqual(hosp[0]["2024"], "1634738")
        t3 = self.csv(out, "t3")
        self.assertEqual(t3[0]["section"], "Amount in Billions")
        self.assertEqual((t3[0]["Item"], t3[0]["2024"]), ("National Health Expenditures", "5278.6"))

    def test_nhe_projections(self):
        spec = {"description": "NHE projections",
                "requests": [{"name": "t1", "endpoint": "nhe_table", "params": {"file": "projections_tables", "table": "Table01"}},
                             {"name": "t2long", "endpoint": "nhe_table",
                              "params": {"file": "projections_tables", "table": "Table02", "layout": "long"}},
                             {"name": "data", "endpoint": "nhe_table", "params": {"file": "projections_data"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        t1 = self.csv(out, "t1")
        self.assertIn("Projected 2025", t1[0])                  # the 'Projected' label covers 2025-2034
        self.assertIn("Projected 2034", t1[0])
        self.assertIn("2024", t1[0])
        t2 = [x for x in self.csv(out, "t2long") if x["Type of Expenditure (billions $)"] == "National Health Expenditures"]
        v = {(x["year"], x["year_label"]): x["value"] for x in t2}
        self.assertEqual((v[("2024", "")], v[("2025", "Projected")]), ("5278.6", "5661.7"))
        d = self.csv(out, "data")
        self.assertIn("2024 Historical", d[0])
        self.assertIn("2034 Projected", d[0])
        # the units line above "Source of Funds" is part of its header
        self.assertEqual((d[0]["Type of Service"], d[0]["Expenditure Amount (Millions) Source of Funds"],
                          d[0]["2024 Historical"]),
                         ("National Health Expenditures", "Total Expenditures", "5278588"))

    def test_ssa_goes_to_manual_route(self):
        code, r = self.check(SSA)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download", r)
        self.assertIn("www.ssa.gov", r["message"])
        with open(os.path.join(r["manual_bundle"], "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        urls = [i["url"] for i in man["items"]]
        self.assertEqual(urls, ["https://www.ssa.gov/oact/TR/2026/SingleYearTRTables_TR2026.xlsx"])
        # a fetch never calls www.ssa.gov: it stops as blocked and returns the same route
        code, r, _ = self.fetch(SSA)
        self.assertEqual(code, 4, r)
        self.assertEqual((r["status"], r["kind"], r["route"]), ("blocked", "blocked", "manual_download"))
        bad = {"requests": [{"name": "x", "endpoint": "ssa_tr_table", "params": {"table": "Table 5"}}]}
        self.assertEqual(self.check(bad)[1]["route"], "fix_spec")

    def test_ssa_upload(self):
        spec = {"description": "2026 OASDI Trustees Report tables",
                "requests": [{"name": "oasdi_ops", "endpoint": "ssa_tr_table", "params": {"report_year": 2026, "table": "VI.G2"}},
                             {"name": "pop", "endpoint": "ssa_tr_table", "params": {"report_year": 2026, "table": "V.A3"}},
                             {"name": "rates", "endpoint": "ssa_tr_table", "params": {"report_year": 2026, "table": "IV.B1"}}]}
        up = os.path.join(self.tmp.name, "upload")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "SingleYearTRTables_TR2026.xlsx"), up)
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        ops = self.csv(out, "oasdi_ops")
        self.assertEqual(list(ops[0])[:4], ["row", "section", "Calendar year", "year"])
        self.assertEqual({x["section"] for x in ops}, {"Historical data", "Intermediate", "Low-cost", "High-cost"})
        inter = [x for x in ops if x["section"] == "Intermediate"]
        last = inter[-1]       # the last year with reserves under the intermediate assumptions (footnote b)
        self.assertEqual((last["Calendar year"], last["year"], last["Reserves at end of yeara"]), ("2033b", "2033", "182.9"))
        pop = self.csv(out, "pop")
        self.assertEqual((pop[0]["Calendar year"], pop[0]["Population (in thousands) Under 20"]), ("1941", "48419"))
        self.assertEqual(pop[0]["Dependency ratio Ageda"], "0.118")
        rates = self.csv(out, "rates")
        self.assertIn("OASDI Cost ratec", rates[0])
        self.assertEqual((rates[0]["Calendar year"], rates[0]["OASI Income rateb"]), ("1970", "7.6"))

    def test_link_scraping(self):
        a = load_pack_module("actuaries")
        with open(os.path.join(self.fixtures, "cms_trustees_page.html"), encoding="utf-8") as f:
            links = a.page_links(f.read())
        import re
        pat = re.compile(a._eps()["medicare_link"], re.I)
        years = {int(pat.match(t).group(1)): h for h, t in links if pat.match(t)}
        self.assertEqual(sorted(years), [2022, 2023, 2024, 2025, 2026])
        self.assertEqual(years[2026], "/files/zip/2026-expanded-supplementary-tables-figures.zip")
        self.assertEqual({str(k): v for k, v in years.items()}, a._eps()["medicare_known"])


if __name__ == "__main__":
    unittest.main()

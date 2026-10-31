"""Offline tests for the usda-outlook pack.

Fixtures are real files fetched live (keyless) on 2026-10-30; several are TRIMMED real copies because the
originals are larger than the 200 KB fixture limit:
  - wasde_2026-10_trimmed.csv: the October 2026 WASDE data CSV (999 KB, 5,054 rows) cut to the rows of
    "U.S. Feed Grain and Corn Supply and Use", "World Corn Supply and Use", "U.S. Wheat Supply and Use" and the
    corn rows of "Reliability of United States October Projections" (lines kept byte for byte);
  - wasde_2026-07/08/09_trimmed.csv: those months cut to "U.S. Feed Grain and Corn Supply and Use";
  - the 2026-06 entry is the real HTTP 404 status with the 143 KB HTML error page cut to one line;
  - september-3-2026-release_trimmed.zip: the release ZIP (6.2 MB; one 111 MB cp1252 CSV) rebuilt with the same
    member name holding the header, the first 200 rows and every "Net farm income" / "Net cash income" row of
    US, IA and CA, raw lines kept byte for byte;
  - baseline_2035_full_trimmed.zip (media 5014, 2 MB) keeps only Projections2026.csv, the README and
    "U.S. Farm receipts expenses and income projections to 2035.xlsx"; baseline_2035_early_trimmed.zip
    (media 5013) keeps only Projections2026.csv.
Full real copies: esmis.html, fiw.html, baseline.html (the three index pages),
us-farm-sector-financial-indicators-2019-2026f.xlsx and ers-farm-income-and-wealth-statistics-data-archive.zip.
"""
import json
import os
import shutil
import sys
import unittest
from datetime import date

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

CORN_US = "U.S. Feed Grain and Corn Supply and Use"
SPEC = {"description": "Latest WASDE U.S. corn, its 2026/27 ending stocks path, 2026 net farm income, baseline corn",
        "requests": [
            {"name": "corn_us", "endpoint": "wasde", "params": {"report_title": CORN_US, "commodity": "Corn"}},
            {"name": "corn_path", "endpoint": "wasde",
             "params": {"start_report": "2026-06", "end_report": "2026-10", "report_title": CORN_US,
                        "commodity": "Corn", "attribute": "Ending Stocks", "market_year": "2026/27"}},
            {"name": "nfi", "endpoint": "farm_income",
             "params": {"variable": ["net farm income", "Net cash income"], "state": ["US", "ia"], "start_year": 2020}},
            {"name": "summary", "endpoint": "farm_income_summary", "params": {}},
            {"name": "vintages", "endpoint": "farm_income_archive", "params": {"item": "nfi", "year": 2025}},
            {"name": "base_corn", "endpoint": "baseline", "params": {"commodity": "Corn", "attribute": "Ending stocks"}},
            {"name": "base_income", "endpoint": "baseline", "params": {"member": "*Farm receipts*"}}]}


class TestUsdaOutlook(PackTest):
    source = "usda-outlook"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        notes = " ".join(r["notes"])
        # WASDE latest = newest ESMIS release (2026-10-09) -> the 2026-10 CSV
        self.assertIn("corn_us: report = 2026-10 (WASDE 676, released 2026-10-09)", notes)
        corn = self.csv(out, "corn_us")
        self.assertEqual(set(corn[0]), {"WasdeNumber", "ReportDate", "ReportTitle", "Attribute", "ReliabilityProjection",
                                        "Commodity", "Region", "MarketYear", "ProjEstFlag", "AnnualQuarterFlag", "Value",
                                        "Unit", "ReleaseDate", "ReleaseTime", "ForecastYear", "ForecastMonth"})
        es = {x["MarketYear"]: (x["ProjEstFlag"], x["Value"], x["Unit"]) for x in corn if x["Attribute"] == "Ending Stocks"}
        self.assertEqual(es, {"2024/25": ("", "1551.00", "Million Bushels"), "2025/26": ("Est.", "2095.00", "Million Bushels"),
                              "2026/27": ("Proj.", "1849.00", "Million Bushels")})
        self.assertEqual({x["ReportTitle"] for x in corn}, {CORN_US})
        # month-to-month path; 2026-06 is a real 404 (reissued v2 report) and is skipped with its reason
        path = self.csv(out, "corn_path")
        self.assertEqual([(x["ReportDate"], x["Value"]) for x in path],
                         [("July 2026", "1790.00"), ("August 2026", "1653.00"), ("September 2026", "1567.00"),
                          ("October 2026", "1849.00")])
        self.assertIn("2026-06: the June 2026 report was reissued", notes)
        # farm income: streamed from the release ZIP, filtered, unit padding stripped, estimate_type added
        nfi = self.csv(out, "nfi")
        us = {(x["Year"], x["VariableDescriptionTotal"]): x for x in nfi if x["State"] == "US"}
        x = us[("2026", "Net farm income")]
        self.assertEqual((x["Amount"], x["unit_desc"], x["ChainType_GDP_Deflator"], x["release_date"], x["estimate_type"],
                          x["artificialKey"]), ("158353790", "$1,000", "100.000", "2026-09-03", "forecast", "FIAUSNTFI--P"))
        self.assertEqual((us[("2025", "Net farm income")]["Amount"], us[("2025", "Net farm income")]["estimate_type"]),
                         ("162614960", "estimate"))
        self.assertEqual(us[("2026", "Net cash income")]["Amount"], "176430198")
        ia = [x for x in nfi if x["State"] == "IA" and x["VariableDescriptionTotal"] == "Net farm income"]
        self.assertEqual(max(int(x["Year"]) for x in ia), 2025)          # no state rows for a forecast year
        self.assertEqual({x["Year"] for x in nfi} - {str(y) for y in range(2020, 2027)}, set())
        self.assertIn("forecast year(s) 2026", notes)
        # summary workbook: forecast column 2026F, billion dollars
        s = {x["item"]: x for x in self.csv(out, "summary")}
        nf = s["l. Net farm income (j-k)"]
        self.assertEqual((nf["section"], nf["unit"], nf["2026F"], nf["2025"]),
                         ("Farm income statement", "Billion Dollars", "158.35379", "162.61496"))
        self.assertEqual(s["Debt-to-equity"]["unit"], "Percent")
        self.assertIn("data as of 2026-09-03", notes)
        # archive of past forecasts: 2025 net farm income, release by release
        v = [(x["release_year"], x["release_month"], x["release_number"], x["amount"]) for x in self.csv(out, "vintages")]
        self.assertEqual(v, [("2025", "feb", "1", "180114083"), ("2025", "sep", "2", "179846374"),
                             ("2026", "feb", "4", "154537944"), ("2026", "sep", "5", "162614960")])
        # baseline: the full release (media 5014), projections CSV and Table 26 workbook
        b = {x["year"]: x["value"] for x in self.csv(out, "base_corn")}
        self.assertEqual((b["2025/26"], b["2026/27"], b["2035/36"]), ("2154", "2019", "2259"))
        self.assertIn("Projections to 2035 (full release, last updated 2/13/2026)", notes)
        inc = {x["col1"]: x for x in self.csv(out, "base_income")}
        self.assertEqual(inc["Cash receipts"]["2026"], "540.875330651853")      # .xlsx float in shortest form

    def test_multi_table_warning_and_reliability(self):
        spec = {"requests": [
            {"name": "es", "endpoint": "wasde",
             "params": {"report": "2026-10", "commodity": "corn", "region": "United States", "attribute": "ending stocks"}},
            {"name": "rel", "endpoint": "wasde",
             "params": {"report": "2026-10", "commodity": "Corn", "attribute": "Ending Stocks", "include_reliability": True,
                        "report_title": "Reliability of United States October Projections"}},
            {"name": "proj", "endpoint": "wasde",
             "params": {"report": "2026-10", "commodity": "Corn", "region": "United States", "proj_est": "proj"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        es = self.csv(out, "es")
        self.assertEqual({x["Unit"] for x in es}, {"Million Bushels", "Million Metric Tons"})
        self.assertFalse(any(x["ReportTitle"].startswith("Reliability") for x in es))
        self.assertIn("the same item comes from 2 report tables", " ".join(r["notes"]))
        rel = self.csv(out, "rel")
        self.assertEqual(len(rel), 7)
        self.assertIn(("root_mean_square_error", "26.80", "Percent"),
                      [(x["ReliabilityProjection"], x["Value"], x["Unit"]) for x in rel])
        self.assertEqual({x["MarketYear"] for x in self.csv(out, "proj")}, {"2026/27"})

    def test_bad_specs_are_fix_spec(self):
        for params, ep in (({"variable": []}, "farm_income"),                           # no filter on 470k rows
                           ({"variable": "Net farm income", "state": "Iowa"}, "farm_income"),
                           ({"report": "2019-05"}, "wasde"),                            # before the CSV series
                           ({"report": "Oct 2026"}, "wasde"),
                           ({"report": "2026-10", "start_report": "2026-01"}, "wasde"),
                           ({"proj_est": "forecast"}, "wasde"),
                           ({"edition": "newest"}, "baseline")):
            code, r = self.check({"requests": [{"name": "x", "endpoint": ep, "params": params}]})
            self.assertEqual(r["route"], "fix_spec", (params, r))

    def test_missing_month_alone_is_an_error(self):
        code, r, out = self.fetch({"requests": [{"name": "june", "endpoint": "wasde",
                                                 "params": {"report": "2026-06", "commodity": "Corn"}}]})
        self.assertNotEqual(code, 0, r)
        self.assertIn("reissued as a corrected v2 report", json.dumps(r))

    def test_manual_bundle_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = {i["name"]: i for i in json.load(f)["items"]}
        w = items["corn_us#2026-10"]
        self.assertEqual(w["url"], "https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-2026-10.csv")
        self.assertEqual(items["nfi#1"]["url"], "https://www.ers.usda.gov/media/29517/september-3-2026-release.zip")
        self.assertTrue(items["base_corn#1"]["url"].startswith("https://www.ers.usda.gov/media/5014/"))
        self.assertFalse(any(i["needs_key"] for i in items.values()))

    def test_ingest_agency_files(self):
        """The user's browser downloads, under the agency's own names, are filtered like a fetch."""
        fx = self.fixtures
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(fx, "wasde_2026-10_trimmed.csv"), os.path.join(up, "oce-wasde-report-data-2026-10.csv"))
        shutil.copy(os.path.join(fx, "september-3-2026-release_trimmed.zip"), os.path.join(up, "september-3-2026-release.zip"))
        shutil.copy(os.path.join(fx, "baseline_2035_full_trimmed.zip"),
                    os.path.join(up, "usda-agricultural-baseline-projections-to-2035.zip"))
        spec = {"requests": [
            {"name": "corn", "endpoint": "wasde", "params": {"report": "2026-10", "report_title": CORN_US,
                                                             "attribute": "Ending Stocks", "commodity": "Corn"}},
            {"name": "nfi", "endpoint": "farm_income", "params": {"variable": "Net farm income", "state": "US", "year": 2026}},
            {"name": "base", "endpoint": "baseline", "params": {"commodity": "Wheat", "attribute": "Ending stocks",
                                                                "year": "2030/31"}}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "keyless_file", r)        # matched by the agency's own file names
        self.assertEqual([x["Value"] for x in self.csv(out, "corn")], ["1551.00", "2095.00", "1849.00"])
        nfi = self.csv(out, "nfi")
        self.assertEqual([(x["Amount"], x["estimate_type"]) for x in nfi], [("158353790", "forecast")])
        base = self.csv(out, "base")
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0]["commodity"], "Wheat")

    def test_rules(self):
        a = load_pack_module("usda-outlook")
        sept26, feb26 = date(2026, 9, 3), date(2026, 2, 5)
        self.assertEqual([a.estimate_type(y, sept26) for y in (2026, 2025, 2024)], ["forecast", "estimate", "estimate"])
        self.assertEqual([a.estimate_type(y, feb26) for y in (2026, 2025, 2024)], ["forecast", "forecast", "estimate"])
        self.assertEqual([a._flag(f) for f in ("Proj.", "(Proj.)", "Est.", "(Est.)", "")],
                         ["proj", "proj", "est", "est", "none"])
        with open(os.path.join(self.fixtures, "baseline.html"), encoding="utf-8") as f:
            eds = a.baseline_editions(f.read())
        self.assertEqual([(e["to_year"], e["kind"]) for e in eds[:3]], [(2035, "early"), (2035, "full"), (2034, "full")])
        self.assertEqual(a.pick_edition(eds, "latest")["url"].split("?")[0],
                         "https://www.ers.usda.gov/media/5014/usda-agricultural-baseline-projections-to-2035.zip")
        self.assertEqual(a.pick_edition(eds, 2034)["to_year"], 2034)
        with open(os.path.join(self.fixtures, "fiw.html"), encoding="utf-8") as f:
            rels = a.fiw_releases(f.read())
        self.assertEqual((rels[0]["date"], rels[1]["date"]), (sept26, feb26))
        self.assertEqual(a.pick_release(rels, "2024-12")["url"], "https://www.ers.usda.gov/media/29593/december-3-2024-release.zip")
        self.assertTrue(rels[-1]["url"].endswith("august-26-2014-release.csv"))


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the pbgc pack (PBGC Pension Insurance Data Tables).

index_page.html is the index page https://www.pbgc.gov/about/reports/pension-insurance-data recorded live on
2026-10-30. The workbooks are trimmed real copies (cells unchanged; other sheets, drawings and unused shared
strings removed), downloaded the same day:
  - pbgc2024_trimmed.xlsx: 2024-pension-data-tables.xlsx (1.28 MB) with DATA BOOK LISTING, S-1, S-29, M-1, M-3, M-4;
  - pbgc2023_trimmed.xlsx: 2023-pension-data-tables.xlsx (2.4 MB) with DATA BOOK LISTING and S-22;
  - pbgc2018_trimmed.xlsx: 2018_pension_data_tables.xlsx with DATA BOOK LISTING, LISTING (Continued) and S-1.
Because the 2024 copy is trimmed, a table missing from it is "listed but not in this workbook", as S-22 really is
in the 2024 1st installment.
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

SPEC = {"description": "PBGC net position of both programs, premium rates, multiemployer assistance, 2024 edition",
        "requests": [
            {"name": "se", "endpoint": "data_table", "params": {"year": 2024, "table": "S-1"}},
            {"name": "me", "endpoint": "data_table", "params": {"year": 2024, "table": "m1"}},
            {"name": "rates", "endpoint": "data_table", "params": {"year": 2024, "table": "s_29"}},
            {"name": "assist", "endpoint": "data_table", "params": {"year": 2024, "table": "M-3"}},
            {"name": "participants", "endpoint": "data_table", "params": {"year": 2024, "table": "M-4"}}]}


class TestPbgc(PackTest):
    source = "pbgc"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        se = self.csv(out, "se")
        self.assertEqual(list(se[0]), ["Fiscal Year", "Assets ($ millions)", "Liabilities ($ millions)",
                                       "Net Position ($ millions)", "sheet_row"])
        y25 = next(x for x in se if x["Fiscal Year"] == "2025")
        # PBGC FY2025 Annual Report: single-employer assets $152.3 billion, liabilities $90.0 billion, net $62.2 billion
        self.assertEqual((y25["Assets ($ millions)"], y25["Liabilities ($ millions)"], y25["Net Position ($ millions)"]),
                         ("152262", "90049", "62213"))
        self.assertEqual(se[1]["Liabilities ($ millions)"], "2480.3")          # 2480.3000000000002 as stored
        me = self.csv(out, "me")
        self.assertEqual(next(x for x in me if x["Fiscal Year"] == "2025")["Net Positions ($ millions)"], "2648")
        rates = self.csv(out, "rates")
        self.assertEqual(next(x for x in rates if x["Plan Year(s) Beginning"] == "2026")
                         ["Flat-Rate Premium Rate per Participant"], "111")
        a = self.csv(out, "assist")
        self.assertIn("Special Financial Assistance (SFA) Total Amount of SFA ($ millions)", a[0])
        self.assertEqual(a[-1]["Fiscal Year"], "Total 10")
        p = self.csv(out, "participants")
        self.assertEqual(next(x for x in p if x["Year"] == "2025")["Total Insured Participants"], "11112")
        notes = " ".join(r["notes"])
        self.assertIn("Table S-1: Net Financial Position (1980-2025), single-employer program", notes)
        self.assertIn("2024 Pension Insurance Data Tables (1st Installment)", notes)
        self.assertIn("Source: PBGC Annual Reports", notes)
        self.assertIn("Plan Size (in thousands)", notes)                       # M-4's units row

    def test_editions_and_listing(self):
        spec = {"description": "editions, the 2024 listing, an older edition's table",
                "requests": [{"name": "eds", "endpoint": "editions", "params": {}},
                             {"name": "tabs", "endpoint": "table_list", "params": {"year": 2024}},
                             {"name": "s1_2018", "endpoint": "data_table", "params": {"year": 2018, "table": "S-1"}},
                             {"name": "tabs18", "endpoint": "table_list", "params": {"year": 2018}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        eds = self.csv(out, "eds")
        self.assertEqual([e["year"] for e in eds][:3], ["2024", "2023", "2022"])
        self.assertEqual(eds[0]["url"], "https://www.pbgc.gov/sites/default/files/documents/2024-pension-data-tables.xlsx")
        self.assertEqual(next(e for e in eds if e["year"] == "2018")["url"],
                         "https://www.pbgc.gov/sites/default/files/2018_pension_data_tables.xlsx")
        self.assertEqual(next(e for e in eds if e["year"] == "2011")["format"], "xls")
        tabs = {t["table"]: t for t in self.csv(out, "tabs")}
        self.assertEqual(len(tabs), 70)
        self.assertEqual((tabs["S-22"]["in_workbook"], tabs["S-1"]["in_workbook"]), ("no", "yes"))
        self.assertEqual(tabs["M-20"]["program"], "multiemployer")
        # numbering changes between editions: premium rate history is S-39 in 2018, S-29 in 2024
        self.assertIn("Premium Rates", tabs["S-29"]["title"])
        t18 = {t["table"]: t for t in self.csv(out, "tabs18")}
        self.assertIn("Premium Rates", t18["S-39"]["title"])
        s18 = self.csv(out, "s1_2018")
        self.assertEqual(next(x for x in s18 if x["Fiscal Year"] == "2019")["Net Position (in millions)"], "8656")

    def test_table_not_in_installment_is_a_clear_error(self):
        spec = {"description": "S-22 is listed but not in the 2024 1st installment",
                "requests": [{"name": "s22", "endpoint": "data_table", "params": {"year": 2024, "table": "S-22"}}]}
        code, r, out = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertEqual(r.get("kind"), "not_found", r)
        self.assertIn("is listed", r["message"])
        self.assertIn("previous year's edition", r["message"])
        ok = {"description": "S-22 from the 2023 edition",
              "requests": [{"name": "s22", "endpoint": "data_table", "params": {"year": 2023, "table": "S-22"}}]}
        code, r, out = self.fetch(ok)
        self.assertEqual(code, 0, r)
        row = next(x for x in self.csv(out, "s22") if x["Plan Year"] == "1980")
        self.assertEqual((row["Active Participants"], row["Separated Vested Participants"]), ("0.7762", "0.0636"))

    def test_unknown_edition_is_not_found(self):
        spec = {"description": "2025 is not posted",
                "requests": [{"name": "x", "endpoint": "data_table", "params": {"year": 2025, "table": "S-1"}}]}
        code, r, out = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertIn("no 2025 edition", r["message"])

    def test_bad_specs_are_fix_spec(self):
        for params in ({"year": 2024, "table": "Table 9"}, {"year": "latest", "table": "S-1"}, {"year": 2024}):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "data_table", "params": params}]})
            self.assertEqual(r["route"], "fix_spec", (params, r))

    def test_search_catalog(self):
        code, r = self.cli("--search", "premium rates")
        self.assertEqual(code, 0, r)
        self.assertIn("s_29", [x.get("alias") for x in r["results"]][:3], r)

    def test_manual_bundle_and_ingest(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(items[0]["url"], "https://www.pbgc.gov/about/reports/pension-insurance-data")
        self.assertIn("2024", items[0]["note"])
        one = {"description": "S-1 from an uploaded workbook",
               "requests": [{"name": "se", "endpoint": "data_table", "params": {"year": 2024, "table": "S-1"}}]}
        sp, out = self.write_spec(one, "one.json")
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "pbgc2024_trimmed.xlsx"), os.path.join(up, "se.xlsx"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual(next(x for x in self.csv(out, "se") if x["Fiscal Year"] == "2025")["Net Position ($ millions)"],
                         "62213")

    def test_norm_table(self):
        a = load_pack_module("pbgc")
        self.assertEqual([a.norm_table(x) for x in ("s1", "S-1", "s_1", "Table S-1", "m 20", "GLANCE")],
                         ["S-1", "S-1", "S-1", "S-1", "M-20", "GLANCE"])


if __name__ == "__main__":
    unittest.main()

"""dhs-ohss pack tests (offline).

Fixtures are real ohss.dhs.gov responses read on 2026-10-30, trimmed to stay small:
- yearbook_index.html, yearbook_2024.html, lir.html: the <main> element of the yearbook index, the FY2024 yearbook
  page and the Legal Immigration and Adjustment of Status Report page, with attributes other than href/id removed and
  some non-link content left out (each file's header comment says what). Every data-file link is as published.
- lpr_fy2024_trimmed.xlsx: the published 2026_0604_ohss_yearbook_lawful_permanent_residents_fy2024.xlsx (252 KB,
  over the 200 KB fixture limit) rewritten to keep the cells of sheets TOC, Table 6 and Table 7; every other sheet
  keeps its name with an empty grid, and printer settings / customXml parts are dropped. Cell values are untouched.
- lir_fy2025.xlsx: the published FY2025 quarterly report workbook, unchanged (74 KB).
"""
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")]      # tests/ (testkit)
from testkit import PackTest, run  # noqa: E402

LPR_URL = "https://ohss.dhs.gov/system/files/2026-06/2026_0604_ohss_yearbook_lawful_permanent_residents_fy2024.xlsx"
SPEC = {"description": "Persons obtaining LPR status by type and class of admission, FY2024 yearbook",
        "requests": [{"name": "lpr_class", "endpoint": "yearbook_table",
                      "params": {"topic": "lpr", "fiscal_year": 2024, "table": "Table 6"}}]}


class TestDhsOhss(PackTest):
    source = "dhs-ohss"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_lpr_table6(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "lpr_class")
        self.assertEqual(list(rows[0])[:3], ["section", "Type and class of admission", "2015"])
        self.assertEqual(list(rows[0])[-1], "2024")
        self.assertEqual(len(rows), 78)                      # 3 sections x 26 rows; footnotes and notes cut
        by = {(x["section"], x["Type and class of admission"]): x for x in rows}
        self.assertEqual(by[("TOTAL", "Total")]["2024"], "1364090")
        self.assertEqual(by[("ADJUSTMENTS OF STATUS", "Total")]["2024"], "782800")
        self.assertEqual(by[("NEW ARRIVALS", "Total")]["2024"], "581290")
        self.assertEqual(by[("TOTAL", "Children1")]["2015"], "66740")    # footnote marker kept in the label
        notes = " ".join(r["notes"])
        self.assertIn("1 Includes orphans.", notes)
        self.assertIn("round table cells to the nearest 10", notes)
        self.assertIn(LPR_URL, notes)

    def test_default_year_and_detailed_class(self):
        """No fiscal_year: the newest year page with the topic (FY2024); Table 7 by a bare number."""
        spec = {"requests": [{"name": "t7", "endpoint": "yearbook_table", "params": {"topic": "lpr", "table": "7"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "t7")
        self.assertEqual(list(rows[0]), ["Type and class of admission", "Total", "Adjustments of status",
                                         "New arrivals"])
        self.assertEqual(rows[0], {"Type and class of admission": "Total, all immigrants", "Total": "1364090",
                                   "Adjustments of status": "782800", "New arrivals": "581290"})
        self.assertIn("FY2024 yearbook", " ".join(r["notes"]))

    def test_wrong_sheet_lists_sheets(self):
        spec = {"requests": [{"name": "x", "endpoint": "yearbook_table",
                              "params": {"topic": "lpr", "fiscal_year": 2024, "table": "Table 99"}}]}
        code, r, _ = self.fetch(spec)
        self.assertEqual(code, 2, r)
        self.assertIn("no sheet 'Table 99'", r["message"])
        self.assertIn("'LPRSuppTable 4'", r["message"])
        self.assertIn("Type and Major Class of Admission", r["message"])
        spec["requests"][0]["params"].pop("table")
        code, r, _ = self.fetch(spec)
        self.assertEqual(code, 2, r)
        self.assertIn("no table given", r["message"])

    def test_topic_not_posted(self):
        """FY2024 enforcement tables are 'coming soon' on the year page: a clear not-found, not another year."""
        spec = {"requests": [{"name": "enf", "endpoint": "yearbook_table",
                              "params": {"topic": "enforcement", "fiscal_year": 2024, "table": "Table 39"}}]}
        code, r, _ = self.fetch(spec)
        self.assertNotEqual(code, 0, r)
        self.assertIn("posts no enforcement file", r["message"])

    def test_yearbook_files(self):
        spec = {"requests": [{"name": "files", "endpoint": "yearbook_files", "params": {"fiscal_year": 2024}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "files")
        self.assertEqual(len(rows), 6)                       # each file once, though the page links most twice
        by = {x["topic"]: x for x in rows}
        self.assertEqual(by["lpr"]["url"], LPR_URL)
        self.assertIn("lpr_expanded", by)
        self.assertEqual(by["refugees, refugees_asylees"]["fiscal_year"], "2024")

    def test_lir(self):
        spec = {"requests": [{"name": "lir_class", "endpoint": "lir_table", "params": {"table": "Table 1B"}},
                             {"name": "lir_files", "endpoint": "lir_files", "params": {}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "lir_class")
        # two header rows joined: "Total | ... | Adjustments of status | ..." over "Total | Quarter 1 | ..."
        self.assertEqual(list(rows[0])[:3], ["Type and class of admission", "Total", "Total Quarter 1"])
        self.assertEqual(rows[0]["Total"], "1320080")
        self.assertEqual(rows[0]["Adjustments of status Total"], "746830")
        self.assertIn("preliminary", " ".join(r["notes"]))
        files = self.csv(out, "lir_files")
        self.assertEqual((files[0]["fiscal_year"], files[0]["through_quarter"]), ("2025", "4"))
        self.assertEqual(len(files), 10)

    def test_check_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "yearbook_table",
                             "params": {"topic": "green cards", "fiscal_year": 2024, "table": "Table 6"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        old = {"requests": [{"name": "x", "endpoint": "yearbook_table",
                             "params": {"topic": "lpr", "fiscal_year": 2010, "table": "Table 6"}}]}
        code, r = self.check(old)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("PDF only", r["message"] + str(r.get("problems")))

    def test_manual_items(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["manual_items"][0]["url"], LPR_URL)
        self.assertEqual(r["manual_items"][0]["filename"], "lpr_class.xlsx")
        # the page cannot be read (sandbox): the item is the year page, with what to download from it
        code, r = run(["--source", self.source, sp, "--out", out, "--dry-run"], None,
                      {"DATA_ACCESS_FORCE": "restricted"}, self.home)
        self.assertEqual(code, 0, r)
        it = r["manual_items"][0]
        self.assertEqual(it["url"], "https://ohss.dhs.gov/topics/immigration/yearbook/2024")
        self.assertIn("lpr data tables workbook", it["note"])

    def test_parse_upload(self):
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "lpr_fy2024_trimmed.xlsx"), os.path.join(up, "lpr_class.xlsx"))
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "lpr_class")
        self.assertEqual(len(rows), 78)
        self.assertEqual(rows[0]["2024"], "1364090")
        # the year page saved under the workbook's name (what a script gets for the page link) is refused clearly
        bad = os.path.join(self.tmp.name, "bad")
        os.makedirs(bad)
        shutil.copy(os.path.join(self.fixtures, "yearbook_2024.html"), os.path.join(bad, "lpr_class.xlsx"))
        code, r = self.cli(sp, "--out", os.path.join(self.tmp.name, "out2"), "--ingest", bad)
        self.assertEqual(code, 2, r)
        self.assertIn("is a web page, not the workbook", r["message"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the fed-board pack (Federal Reserve Board DDP, Z.1 bulk files, SCF).

Fixtures recorded live from www.federalreserve.gov (keyless) on 2026-10-30:
- ddp_choose_G19.html (the G.19 Choose page with its package ids), ddp_G19_sa.csv (package "Consumer Credit
  Outstanding (S.A.)", complete), ddp_SLOOS_all.csv (SLOOS "All chart data", complete), z1_table_mapping.csv.
- ddp_empty_bad_package.csv: the empty HTTP 200 body the DDP serves for a package id that does not exist.
- ddp_choose_NOPE_400.html: the HTTP 400 HTML page for an unknown release code.
Trimmed real copies (originals over the 200 KB fixture limit):
- z1_csv_files_trim.zip: z1_csv_files.zip (8.3 MB, 2026:Q2 release) with only csv/S1M_b.csv and
  data_dictionary/S1M_b.txt, members copied byte for byte.
- scf_chart_trim.zip: scf.zip (457 KB) with only the all_median, racecl4_median, all_have CSVs and
  scf-data-definitions.txt, members copied byte for byte.
- scf2025_tables_real_trim.xlsx: scf2025_tables_public_real_historical.xlsx (2.5 MB), sheet "Table 4" only, cell
  values copied exactly as stored, written with a minimal workbook skeleton.
"""
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SPEC = {"description": "G.19 consumer credit (SA), SLOOS, G.19 packages, B.101 from the Z.1 zip, SCF chart data and "
                       "Table 4",
        "requests": [
            {"name": "g19_sa", "endpoint": "ddp",
             "params": {"rel": "G19", "package": "Consumer Credit Outstanding (S.A.)", "from": "2025-01"}},
            {"name": "sloos", "endpoint": "ddp", "params": {"package": "sloos", "lastobs": 4}},
            {"name": "g19_pkgs", "endpoint": "ddp_packages", "params": {"rel": "G19"}},
            {"name": "b101", "endpoint": "z1", "params": {"table": "B.101", "start": "2025",
                                                          "series_ids": ["FL152090005", "FL152000005.Q"]}},
            {"name": "z1_tables", "endpoint": "z1_tables", "params": {}},
            {"name": "race_median", "endpoint": "scf_chart", "params": {"group": "racecl4", "stat": "median"}},
            {"name": "table4", "endpoint": "scf_table",
             "params": {"file": "scf2025_tables_public_real_historical", "sheet": "Table 4"}}]}


class TestFedBoard(PackTest):
    source = "fed-board"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        g = self.csv(out, "g19_sa")
        self.assertEqual(list(g[0]), ["date", "series", "value", "value_flag", "description", "unit", "multiplier",
                                      "currency", "unique_id"])
        self.assertEqual(min(x["date"] for x in g), "2025-01")
        tot = [x for x in g if x["series"] == "DTCTL.M"]
        self.assertEqual((tot[-1]["date"], tot[-1]["value"], tot[-1]["unit"], tot[-1]["multiplier"]),
                         ("2026-08", "5196778.44", "Currency", "1000000"))     # G.19 release: $5,196.8 billion
        self.assertEqual(tot[-1]["unique_id"], "G19/CCOUT/DTCTL.M")
        self.assertIn("seasonally adjusted level", tot[-1]["description"])
        sl = self.csv(out, "sloos")
        self.assertEqual(sorted({x["date"] for x in sl})[-4:], ["2025Q4", "2026Q1", "2026Q2", "2026Q3"])
        self.assertEqual(len({x["date"] for x in sl}), 4)                         # lastobs applied per series
        pk = self.csv(out, "g19_pkgs")
        self.assertEqual(len(pk), 7)
        self.assertEqual((pk[0]["package_title"], pk[0]["package"]),
                         ("Consumer Credit Outstanding - All", "56a37b655eef16e2ede83467282a6e95"))
        b = self.csv(out, "b101")
        nw = [x for x in b if x["series"] == "FL152090005.Q"]
        self.assertEqual((nw[-1]["date"], nw[-1]["value"], nw[-1]["line"], nw[-1]["table"], nw[-1]["table_published"]),
                         ("2026:Q2", "195870496", "42", "S1M.b", "B.101"))
        self.assertEqual({x["series"] for x in b}, {"FL152090005.Q", "FL152000005.Q"})
        self.assertEqual(min(x["date"] for x in b), "2025:Q1")
        self.assertIn("Millions of dollars", nw[0]["units"])
        t = self.csv(out, "z1_tables")
        self.assertIn({"new": "S1M.b", "old": "B.101", "title": "Households and nonprofit organizations"}, t)
        rm = self.csv(out, "race_median")
        last = [x for x in rm if x["year"] == "2025"]
        self.assertEqual([x["Category"] for x in last], ["White, non-Hispanic", "Black, non-Hispanic", "Hispanic", "Other"])
        t4 = self.csv(out, "table4")
        allf = [x for x in t4 if x["c1"] == "All families"][0]
        self.assertEqual((allf["row"], allf["c26"], allf["c27"]), ("6", "216.1", "1232"))   # 2025 median, mean

    def test_catalog_alias_carries_release_and_manual_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        urls = {i["name"].split("#")[0]: i for i in r["items"]}
        self.assertEqual(urls["sloos"]["url"], "https://www.federalreserve.gov/datadownload/Output.aspx?rel=SLOOS&series="
                         "e64f07187815f0e7ed89237d8cb91eeb&filetype=csv&label=include&layout=seriescolumn&type=package")
        self.assertIn("Choose.aspx?rel=G19", urls["g19_sa"]["url"])
        self.assertIn("Consumer Credit Outstanding (S.A.)", urls["g19_sa"]["note"])
        self.assertTrue(urls["b101"]["url"].endswith("/releases/z1/current/z1_csv_files.zip"))
        code, r = self.cli("--search", "consumer credit")
        self.assertIn("g19_sa", [h.get("alias") for h in r["results"]])

    def test_ambiguous_title_and_bad_ids(self):
        amb = {"requests": [{"name": "x", "endpoint": "ddp", "params": {"rel": "G19", "package": "Credit"}}]}
        code, r, out = self.fetch(amb)
        self.assertNotEqual(code, 0, r)
        self.assertIn("matches", r["message"])
        bad = {"requests": [{"name": "x", "endpoint": "ddp",
                             "params": {"rel": "G19", "package": "00000000000000000000000000000000"}}]}
        code, r, out = self.fetch(bad)
        self.assertNotEqual(code, 0, r)
        self.assertIn("empty file", r["message"])
        rel = {"requests": [{"name": "x", "endpoint": "ddp_packages", "params": {"rel": "NOPE"}}]}
        code, r, out = self.fetch(rel)
        self.assertIn("not a Data Download Program release", r["message"])

    def test_bad_spec_is_fix_spec(self):
        for req in ({"endpoint": "ddp", "params": {"package": "Consumer Credit Outstanding (S.A.)"}},      # no rel
                    {"endpoint": "ddp", "params": {"package": "g19_sa", "from": "Jan 2025"}},
                    {"endpoint": "z1", "params": {"table": "B.101", "start": "2025Q1"}},
                    {"endpoint": "scf_chart", "params": {"group": "race", "stat": "median"}}):
            code, r = self.check({"requests": [{"name": "x", **req}]})
            self.assertEqual(r["route"], "fix_spec", (req, r))

    def test_date_filters(self):
        ad = load_pack_module("fed-board")
        rows = [{"date": d, "series": "S"} for d in ("2024Q4", "2025Q1", "2025Q4", "2026Q1")]
        self.assertEqual([x["date"] for x in ad.filter_rows(rows, "2025", "2025")], ["2025Q1", "2025Q4"])
        self.assertEqual([x["date"] for x in ad.filter_rows(rows, lastobs=2)], ["2025Q4", "2026Q1"])
        self.assertEqual(ad.date_key("2026-08"), "2026-08-01")
        self.assertEqual(ad.date_key("2026:Q3", end=True), "2026-09-31")

    def test_ingest_uploaded_files(self):
        spec = {"requests": [
            {"name": "g19_sa", "endpoint": "ddp", "params": {"package": "g19_sa", "series_ids": "DTCTL.M"}},
            {"name": "b101", "endpoint": "z1", "params": {"table": "S1M.b", "series_ids": ["FL152090005"]}},
            {"name": "race_median", "endpoint": "scf_chart", "params": {"group": "racecl4", "stat": "median"}}]}
        sp, out = self.write_spec(spec)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        for src, dst in (("ddp_G19_sa.csv", "g19_sa.csv"), ("z1_csv_files_trim.zip", "b101.zip"),
                         ("scf_chart_trim.zip", "race_median.zip")):
            shutil.copy(os.path.join(FX, src), os.path.join(up, dst))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        g = self.csv(out, "g19_sa")
        self.assertEqual({x["series"] for x in g}, {"DTCTL.M"})
        self.assertEqual(g[-1]["value"], "5196778.44")
        b = self.csv(out, "b101")
        self.assertEqual(b[-1]["value"], "195870496")
        self.assertEqual(len(self.csv(out, "race_median")), 52)


if __name__ == "__main__":
    unittest.main()

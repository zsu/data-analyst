"""Offline tests for the philfed pack (Philadelphia Fed Real-Time Data Research Center workbooks).

Fixtures recorded live from www.philadelphiafed.org (keyless) on 2026-10-30:
- spf_medianGrowth.xlsx, spf_prob.xlsx, liv_medians.xlsx: the complete workbooks as served.
- html_404_ROUTPUTQvQx.html: the HTML "Error - 404" page the site serves with HTTP 200 for a missing file.
Trimmed real copies (the originals are over the 200 KB fixture limit; cell values copied exactly as stored, only
some sheets/rows kept, written with a minimal workbook skeleton):
- spf_medianLevel_trim.xlsx: medianLevel.xlsx (554 KB), sheets RGDP and CPI only, all rows.
- spf_Dispersion_2_trim.xlsx: Dispersion_2.xlsx (285 KB), sheet RGDP only, all rows.
- gb_row_format_trim.xlsx: GBweb_Row_Format.xlsx (598 KB), sheet gRGDP, header and the last 40 Greenbooks.
- rtdsm_ROUTPUTQvQd_trim.xlsx: ROUTPUTQvQd.xlsx (243 KB), header and the last 12 rows (2023:Q3 on), all vintages.
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
SPEC = {"description": "SPF real GDP outlook (2026:Q3 survey), CPI, dispersion, Anxious Index, Greenbook, Livingston, "
                       "real GDP vintages",
        "requests": [
            {"name": "rgdp_level", "endpoint": "spf", "params": {"file": "medianLevel", "sheet": "RGDP"}},
            {"name": "rgdp_growth", "endpoint": "spf", "params": {"file": "medianGrowth", "sheet": "RGDP"}},
            {"name": "cpi", "endpoint": "spf", "params": {"file": "medianLevel", "sheet": "CPI"}},
            {"name": "rgdp_disp", "endpoint": "spf_dispersion", "params": {"measure": 2, "sheet": "RGDP"}},
            {"name": "recess", "endpoint": "spf_prob", "params": {"sheet": "RECESS"}},
            {"name": "gb_rgdp", "endpoint": "greenbook", "params": {"sheet": "gRGDP"}},
            {"name": "liv_cpi", "endpoint": "livingston", "params": {"file": "medians", "sheet": "CPI"}},
            {"name": "rgdp_vintages", "endpoint": "rtdsm", "params": {"file": "real_gdp"}}]}


class TestPhilfed(PackTest):
    source = "philfed"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        lv = self.csv(out, "rgdp_level")
        last = lv[-1]
        self.assertEqual((last["survey"], last["RGDP1"], last["RGDPA"], last["RGDPB"]),
                         ("2026Q3", "24270.6000", "24359.0924", "24886.3000"))
        self.assertEqual(lv[0]["survey"], "1968Q4")
        g = self.csv(out, "rgdp_growth")
        self.assertEqual(list(g[0])[:4], ["survey", "YEAR", "QUARTER", "drgdp2"])
        self.assertEqual((g[-1]["survey"], g[-1]["drgdp2"]), ("2026Q3", "2.4624"))      # release: 2.5 percent
        self.assertEqual(g[1]["drgdp6"], "")                                              # "#N/A" -> empty
        self.assertEqual(self.csv(out, "cpi")[-1]["CPIA"], "3.5610")                      # Q4/Q4 2026: 3.6
        d = self.csv(out, "rgdp_disp")
        self.assertEqual((d[-1]["Survey_Date(T)"], d[-1]["RGDP_D2(T)"]), ("2026Q3", "1.2909"))
        rc = self.csv(out, "recess")[-1]
        self.assertEqual((rc["survey"], rc["RECESS2"]), ("2026Q3", "19.9667"))
        gb = self.csv(out, "gb_rgdp")
        self.assertEqual((gb[-1]["greenbook_date"], gb[-1]["DATE"], gb[-1]["gRGDPF0"]), ("2020-12-04", "2020.4", "5"))
        liv = self.csv(out, "liv_cpi")
        self.assertEqual((liv[-1]["survey_date"], liv[-1]["Date"], liv[-1]["CPI_10Y"]), ("2026-06-01", "46174", "2.33"))
        v = self.csv(out, "rgdp_vintages")
        q = {x["DATE"]: x for x in v}
        base = [float(q[f"2025:Q{i}"]["ROUTPUT26Q3"]) for i in range(1, 5)]
        self.assertEqual(q["2026:Q2"]["ROUTPUT26Q3"], last["RGDP1"])                     # the SPF's information set
        growth = (float(last["RGDPA"]) / (sum(base) / 4) - 1) * 100
        self.assertEqual(round(growth, 1), 2.1)                                           # SPF release 2026:Q3: 2.1

    def test_html_404_page_is_a_clear_error(self):
        bad = {"requests": [{"name": "bad", "endpoint": "rtdsm", "params": {"file": "ROUTPUTQvQx"}}]}
        code, r, out = self.fetch(bad)
        self.assertNotEqual(code, 0, r)
        self.assertIn("Error - 404", r["message"])
        self.assertIn("file name", r["message"])
        ad = load_pack_module("philfed")
        self.assertIsNone(ad.check_error(200, b"PK\x03\x04 workbook bytes"))

    def test_wrong_sheet_lists_sheets(self):
        bad = {"requests": [{"name": "x", "endpoint": "spf", "params": {"file": "medianGrowth", "sheet": "rgdp"}}]}
        code, r, out = self.fetch(bad)
        self.assertNotEqual(code, 0, r)
        self.assertIn("RGDP", r["message"])

    def test_bad_spec_is_fix_spec(self):
        for p in ({"file": "medianlevels", "sheet": "RGDP"}, {"file": "medianLevel"}):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "spf", "params": p}]})
            self.assertEqual(r["route"], "fix_spec", r)
        code, r = self.check({"requests": [{"name": "x", "endpoint": "spf_dispersion",
                                            "params": {"measure": 4, "sheet": "RGDP"}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_catalog_alias_and_manual_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        urls = {i["name"].split("#")[0]: i["url"] for i in r["items"]}
        self.assertTrue(urls["rgdp_vintages"].endswith("/real-time-data/data-files/xlsx/ROUTPUTQvQd.xlsx"))
        self.assertTrue(urls["rgdp_disp"].endswith("/historical-data/Dispersion_2.xlsx"))
        self.assertLessEqual({"rgdp_level.xlsx", "gb_rgdp.xlsx"}, {i["filename"] for i in r["items"]})
        code, r = self.cli("--search", "unemployment")
        self.assertIn("unemployment", [h.get("alias") for h in r["results"]])

    def test_ingest_uploaded_workbook(self):
        spec = {"requests": [{"name": "rgdp_growth", "endpoint": "spf", "params": {"file": "medianGrowth", "sheet": "RGDP"}}]}
        sp, out = self.write_spec(spec)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(FX, "spf_medianGrowth.xlsx"), os.path.join(up, "rgdp_growth.xlsx"))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "rgdp_growth")
        self.assertEqual((rows[-1]["survey"], rows[-1]["drgdp2"]), ("2026Q3", "2.4624"))


if __name__ == "__main__":
    unittest.main()

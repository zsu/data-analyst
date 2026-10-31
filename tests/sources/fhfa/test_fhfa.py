"""fhfa pack tests (offline).

Fixtures from www.fhfa.gov, 2026-10-30. Recorded live with DATA_ANALYST_RECORD: r001.csv (hpi_at_state.csv, whole)
and r002.xml (annual hpi_at_state.xlsx, whole). Trimmed REAL copies (rows kept unchanged, in file order; the full
files are over the 200 KB fixture limit):
  hpi_master_trimmed.csv  hpi_master.csv: header, the first 40 rows, every U.S. purchase-only monthly row, Vermont
                          purchase-only quarterly rows and Vermont all-transactions rows from 2024
  cll2026_trimmed.csv     2026 loan limits: header and the AL, AK, CA, HI rows
  cll2025_trimmed.csv     2025 loan limits (other layout): header and the AL, CA rows
  pudb2025_trimmed.csv    2025 FHLBank PUDB: header, the first 150 rows and every Vermont row
  notfound_2027.html      the first 1,500 bytes of FHFA's real HTTP 404 page for the unpublished 2027 limits file
"""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

SPEC = {"description": "FHFA HPI (U.S. purchase-only monthly, Vermont), annual state HPI, 2026 loan limits, FHLBank PUDB",
        "requests": [
            {"name": "us_po_monthly", "endpoint": "hpi_master",
             "params": {"hpi_type": "traditional", "hpi_flavor": "purchase-only", "frequency": "monthly", "place_id": "USA"}},
            {"name": "vt_po_quarterly", "endpoint": "hpi_master",
             "params": {"hpi_flavor": "purchase-only", "level": "State", "place_id": "VT", "min_year": 2024}},
            {"name": "vt_at", "endpoint": "hpi_state", "params": {"state": "vt", "min_year": 2025}},
            {"name": "annual_state", "endpoint": "hpi_annual",
             "params": {"geography": "state", "where": {"Abbreviation": "VT"}, "min_year": 2020}},
            {"name": "limits_2026", "endpoint": "loan_limits", "params": {"year": 2026, "state": ["CA", "HI"]}},
            {"name": "pudb_vt", "endpoint": "fhlb_pudb", "params": {"year": "2025", "where": {"FIPSStateNumericCode": "50"}}}]}


class TestFhfa(PackTest):
    source = "fhfa"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        us = self.csv(out, "us_po_monthly")
        self.assertEqual(list(us[0])[:10], ["hpi_type", "hpi_flavor", "frequency", "level", "place_name", "place_id",
                                            "yr", "period", "index_nsa", "index_sa"])
        self.assertEqual(len(us), 428)                     # 1991-01 .. 2026-08
        self.assertEqual({x["place_id"] for x in us}, {"USA"})
        last = us[-1]
        # the latest purchase-only U.S. SA index (August 2026; +0.3 % m/m, +2.4 % y/y in FHFA's Oct 27, 2026 release)
        self.assertEqual((last["yr"], last["period"], last["index_sa"], last["index_nsa"]), ("2026", "8", "444.61", "450.45"))
        aug25 = next(x for x in us if (x["yr"], x["period"]) == ("2025", "8"))
        self.assertEqual(round(float(last["index_sa"]) / float(aug25["index_sa"]) * 100 - 100, 1), 2.4)
        vt = self.csv(out, "vt_po_quarterly")
        self.assertEqual({(x["place_id"], x["hpi_flavor"], x["level"]) for x in vt}, {("VT", "purchase-only", "State")})
        self.assertEqual(min(int(x["yr"]) for x in vt), 2024)
        at = self.csv(out, "vt_at")                        # headerless file: the pack names the columns
        self.assertEqual(list(at[0]), ["state", "yr", "qtr", "index_nsa"])
        self.assertEqual([(x["yr"], x["qtr"], x["index_nsa"]) for x in at][-1], ("2026", "2", "902.53"))
        self.assertEqual(len(at), 6)
        an = self.csv(out, "annual_state")
        self.assertEqual(list(an[0]), ["State", "Abbreviation", "FIPS", "Year", "Annual Change (%)", "HPI",
                                       "HPI with 1990 base", "HPI with 2000 base"])
        self.assertEqual((an[-1]["Year"], an[-1]["HPI"], an[-1]["Annual Change (%)"]), ("2025", "1036.55", "3.61"))
        lim = self.csv(out, "limits_2026")
        self.assertEqual(list(lim[0]), ["fips_state_code", "fips_county_code", "county_name", "state", "cbsa_number",
                                        "one_unit_limit", "two_unit_limit", "three_unit_limit", "four_unit_limit"])
        self.assertEqual({x["state"] for x in lim}, {"CA", "HI"})
        alpine = next(x for x in lim if x["county_name"] == "ALPINE COUNTY")
        self.assertEqual((alpine["fips_state_code"], alpine["fips_county_code"], alpine["one_unit_limit"]),
                         ("06", "003", "832750"))                       # the 2026 baseline (FHFA, Nov 25, 2025)
        self.assertEqual(max(int(x["one_unit_limit"]) for x in lim if x["state"] == "CA"), 1249125)   # ceiling
        self.assertEqual(max(int(x["one_unit_limit"]) for x in lim), 1299500)                         # Maui, HI
        self.assertTrue(any("snake_case" in n for n in r["notes"]), r["notes"])
        pudb = self.csv(out, "pudb_vt")
        self.assertTrue(pudb and {x["FIPSStateNumericCode"] for x in pudb} == {"50"})
        self.assertEqual(pudb[0]["Bank"], "Boston")

    def test_loan_limit_layouts_match(self):
        spec = {"requests": [{"name": "y2025", "endpoint": "loan_limits", "params": {"year": 2025, "state": "AL"}},
                             {"name": "y2026", "endpoint": "loan_limits", "params": {"year": 2026, "state": "AL"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        a, b = self.csv(out, "y2025"), self.csv(out, "y2026")
        self.assertEqual(list(a[0]), list(b[0]))
        self.assertEqual((a[0]["fips_state_code"], a[0]["fips_county_code"], a[0]["one_unit_limit"]), ("01", "001", "806500"))
        self.assertEqual(b[0]["one_unit_limit"], "832750")

    def test_max_rows_after_filter(self):
        spec = {"requests": [{"name": "us", "endpoint": "hpi_master", "max_rows": 12,
                              "params": {"hpi_flavor": "purchase-only", "frequency": "monthly", "place_id": "USA"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(r["status"], "partial", r)
        self.assertEqual(len(self.csv(out, "us")), 12)
        self.assertTrue(any("stopped at 12 rows" in n and "428 rows matched" in n for n in r["notes"]), r["notes"])

    def test_unpublished_year_not_found(self):
        spec = {"requests": [{"name": "y2027", "endpoint": "loan_limits", "params": {"year": 2027}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(r.get("status"), "error", r)
        self.assertIn("not found", r.get("message", ""))

    def test_unknown_filter_column_and_empty_filter(self):
        spec = {"requests": [{"name": "x", "endpoint": "hpi_state", "params": {"where": {"State Name": "Vermont"}}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(r.get("status"), "error", r)
        self.assertIn("not in this file", r.get("message", ""))
        spec = {"requests": [{"name": "x", "endpoint": "hpi_state", "params": {"state": "Vermont"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(r.get("status"), "error", r)
        self.assertIn("returned no rows", r.get("message", ""))

    def test_bad_spec_refused(self):
        for bad in ({"requests": [{"name": "x", "endpoint": "hpi_master", "params": {"hpi_flavor": "sales-only"}}]},
                    {"requests": [{"name": "x", "endpoint": "hpi_annual", "params": {"geography": "zip5"}}]},
                    {"requests": [{"name": "x", "endpoint": "loan_limits", "params": {"year": 2023}}]},
                    {"requests": [{"name": "x", "endpoint": "loan_limits", "params": {"year": 2026, "min_year": 2020}}]},
                    {"requests": [{"name": "x", "endpoint": "hpi_state", "params": {"where": ["VT"]}}]}):
            code, r = self.check(bad)
            self.assertEqual(r["route"], "fix_spec", (bad, r))

    def test_manual_bundle_and_upload(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(r["instructions"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv", text)
        self.assertIn("https://www.fhfa.gov/document/d/cll/fullcountyloanlimitlist2026_hera-based_final_flat.csv", text)
        # an uploaded copy of the headerless state file is named and filtered like a fetch
        from core import packs, generic_rest as gr
        pack = packs.load("fhfa")
        rows = gr.parse_upload(pack, SPEC["requests"][2], [os.path.join(self.fixtures, "r001.csv")])
        self.assertEqual(rows[0], {"state": "VT", "yr": "2025", "qtr": "1", "index_nsa": "852.68"})

    def test_html_page_refused(self):
        ad = load_pack_module("fhfa")
        self.assertIsNotNone(ad.check_error(200, b"<!DOCTYPE html><html><body>Page not found</body></html>"))
        self.assertIsNone(ad.check_error(200, b"hpi_type,hpi_flavor\n"))


if __name__ == "__main__":
    unittest.main()

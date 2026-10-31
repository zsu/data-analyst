"""Offline tests for the irs-soi pack.

Fixtures (all real irs.gov files, downloaded 2026-10-30):
- r001.csv (stateinflow2223.csv, the whole file) and r002.xml (the HTTP 404 page for stateinflow2324.csv) were
  recorded live with DATA_ANALYST_RECORD.
- The all-states files are too big to record (core.http skips bodies over 200 KB, and Client.download, used for the
  216 MB ZIP code file, is never recorded), so these are trimmed real files: the header line, the first data lines
  and the lines of a few places, copied byte for byte, nothing edited or added:
    22incyallagi_subset.csv       header + first 16 lines (Alabama total, Autauga) + Maryland COUNTYFIPS 000/001/031
    22zpallagi_subset.csv         header + first 12 lines (Alabama 00000, 35004) + Maryland ZIP 00000/20601/99999
    22in55cmcsv_subset.csv        header + the US and MD lines
    countyoutflow2223_subset.csv  header + first 20 lines (Autauga) + the lines of 01005 (Barbour) and 24031 (Montgomery)
"""
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SPEC = {"description": "Maryland income tax statistics, tax year 2022, and migration into Maryland 2022-2023",
        "requests": [
            {"name": "md_county", "endpoint": "county_agi", "params": {"tax_year": 2022, "state": "MD"}},
            {"name": "md_in", "endpoint": "migration",
             "params": {"level": "state", "direction": "inflow", "y1": 2022, "state": "Maryland"}}]}


class TestIrsSoi(PackTest):
    source = "irs-soi"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_county_and_state_migration(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        md = self.csv(out, "md_county")
        self.assertEqual(len(md), 24)                                  # 3 places x 8 AGI classes, Alabama dropped
        self.assertEqual(list(md[0])[:7], ["TAX_YEAR", "STATEFIPS", "STATE", "COUNTYFIPS", "COUNTYNAME", "AGI_STUB",
                                           "N1"])
        self.assertIn("MARS1", md[0])                                  # published as mars1: upper-cased
        self.assertEqual({x["STATE"] for x in md}, {"MD"})
        tot = [x for x in md if x["COUNTYFIPS"] == "000"]
        self.assertEqual((tot[0]["COUNTYNAME"], tot[0]["AGI_STUB"], tot[0]["N1"], tot[0]["A00100"]),
                         ("Maryland", "1", "40530", "-2088844"))       # published 40530.0000, -2088844.000
        mont = {x["AGI_STUB"]: x for x in md if x["COUNTYFIPS"] == "031"}
        self.assertEqual((mont["8"]["N1"], mont["8"]["A00100"]), ("89920", "44953260"))
        self.assertEqual(mont["8"]["TAX_YEAR"], "2022")
        mig = self.csv(out, "md_in")
        self.assertEqual(len(mig), 56)
        self.assertEqual(list(mig[0])[:3], ["Y1_YEAR", "Y2_YEAR", "Y2_STATEFIPS"])
        types = [x["ROW_TYPE"] for x in mig[:5]]
        self.assertEqual(types, ["total_us_and_foreign", "total_us", "total_foreign", "total_same_state",
                                 "non_migrants"])                      # both 97 rows told apart by name
        va = next(x for x in mig if x["Y1_STATE"] == "VA")
        self.assertEqual((va["N1"], va["N2"], va["AGI"], va["ROW_TYPE"]), ("13353", "21646", "1173876", "flow"))

    def test_zip_and_state_tables(self):
        spec = {"requests": [
            {"name": "zips", "endpoint": "zip_agi",
             "params": {"tax_year": 2022, "state": "24", "zipcode": ["20601", "99999"], "columns": ["N1", "A00100"]}},
            {"name": "states", "endpoint": "state_agi", "params": {"tax_year": 2022, "state": ["MD", "US"],
                                                                    "agi_stub": [0, 10]}}]}
        code, r = self.check(spec)
        self.assertEqual(r["route"], "api", r)
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        z = self.csv(out, "zips")
        self.assertEqual(list(z[0]), ["TAX_YEAR", "STATEFIPS", "STATE", "ZIPCODE", "AGI_STUB", "N1", "A00100"])
        self.assertEqual(len(z), 12)
        top = next(x for x in z if x["ZIPCODE"] == "20601" and x["AGI_STUB"] == "6")
        self.assertEqual((top["N1"], top["A00100"]), ("780", "217760"))
        s = {(x["STATE"], x["AGI_STUB"]): x for x in self.csv(out, "states")}
        self.assertEqual(len(s), 4)
        self.assertEqual((s[("MD", "0")]["N1"], s[("MD", "0")]["A00100"]), ("3095140", "295351996"))   # "3,095,140"
        self.assertEqual(s[("US", "10")]["A00100"], "2716781710")

    def test_county_migration_suppressed_and_other_flows(self):
        spec = {"requests": [{"name": "out", "endpoint": "migration",
                              "params": {"level": "county", "direction": "outflow", "y1": 2022,
                                         "county": ["01005", "24031"]}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "out")
        self.assertEqual({(x["Y1_STATEFIPS"], x["Y1_COUNTYFIPS"]) for x in rows}, {("01", "005"), ("24", "031")})
        ne = next(x for x in rows if x["Y1_COUNTYFIPS"] == "005" and x["Y2_STATEFIPS"] == "59"
                  and x["Y2_COUNTYFIPS"] == "001")
        self.assertEqual((ne["N1"], ne["AGI"], ne["SUPPRESSED"], ne["ROW_TYPE"]), ("", "", "N1;N2;AGI", "other_flows"))
        m = {x["ROW_TYPE"]: x for x in rows if x["Y1_COUNTYFIPS"] == "031" and x["ROW_TYPE"] != "flow"}
        self.assertEqual(m["total_different_state"]["N1"], "20473")
        self.assertEqual(m["non_migrants"]["N1"], "429090")

    def test_bad_specs_are_fix_spec(self):
        for bad, word in (
                ({"name": "z", "endpoint": "zip_agi", "params": {"tax_year": 2022}}, "needs state"),
                ({"name": "c", "endpoint": "county_agi", "params": {"tax_year": 2008, "state": "MD"}}, "2011"),
                ({"name": "s", "endpoint": "state_agi", "params": {"tax_year": 2013}}, "xlsx"),
                ({"name": "m", "endpoint": "migration", "params": {"level": "state", "direction": "inflow",
                                                                   "tax_year": 2022}}, "y1"),
                ({"name": "m2", "endpoint": "migration", "params": {"level": "metro", "direction": "inflow"}}, "level"),
                ({"name": "k", "endpoint": "county_agi", "params": {"tax_year": 2022, "county": "031"}}, "needs state"),
                ({"name": "x", "endpoint": "county_agi", "params": {"tax_year": 2022, "state": "Marylnd"}},
                 "unknown state")):
            code, r = self.check({"requests": [bad]})
            self.assertEqual(r["route"], "fix_spec", (bad, r))
            self.assertIn(word, r["message"], (bad, r))

    def test_year_not_posted_names_the_file(self):
        spec = {"requests": [{"name": "next", "endpoint": "migration",
                              "params": {"level": "state", "direction": "inflow", "y1": 2023}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 2, r)
        self.assertEqual(r["kind"], "not_found", r)
        self.assertIn("stateinflow2324.csv", r["message"])

    def test_manual_bundle_links_published_files(self):
        sp, out = self.write_spec({"requests": [
            {"name": "zips", "endpoint": "zip_agi", "params": {"start_year": 2021, "end_year": 2022, "state": "MD"}},
            {"name": "cty", "endpoint": "county_agi", "params": {"tax_year": 2022, "agi_classes": False}},
            {"name": "st", "endpoint": "state_agi", "params": {"tax_year": 2018}},
            {"name": "mig", "endpoint": "migration", "params": {"level": "county", "direction": "outflow",
                                                                "y1": 2021}}]})
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        names = [i["filename"] for i in r["items"]]
        self.assertEqual(names, ["21zpallagi.csv", "22zpallagi.csv", "22incyallnoagi.csv", "18in55cmagi.csv",
                                 "countyoutflow2122.csv"])
        self.assertEqual(r["items"][0]["url"], "https://www.irs.gov/pub/irs-soi/21zpallagi.csv")

    def test_ingest_filters_upload(self):
        sp, out = self.write_spec(SPEC)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(FX, "22incyallagi_subset.csv"), os.path.join(up, "22incyallagi.csv"))
        shutil.copy(os.path.join(FX, "r001.csv"), os.path.join(up, "stateinflow2223.csv"))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        md = self.csv(out, "md_county")
        self.assertEqual(len(md), 24)
        self.assertEqual({x["STATE"] for x in md}, {"MD"})
        self.assertEqual(len(self.csv(out, "md_in")), 56)

    def test_parse_upload_direct(self):
        import testkit
        ad = testkit.load_pack_module("irs-soi")
        req = {"name": "z", "endpoint": "zip_agi", "params": {"tax_year": 2022, "zipcode": "20601", "agi_stub": 6}}
        rows = ad.parse_upload(None, req, [os.path.join(FX, "22zpallagi_subset.csv")])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ZIPCODE"], rows[0]["N1"], rows[0]["TAX_YEAR"]), ("20601", "780", 2022))
        req = {"name": "c", "endpoint": "county_agi", "params": {"tax_year": 2022, "state": "MD", "county": "Montgomery"}}
        rows = ad.parse_upload(None, req, [os.path.join(FX, "22incyallagi_subset.csv")])
        self.assertEqual({(x["COUNTYNAME"], x["COUNTYFIPS"]) for x in rows}, {("Montgomery County", "031")})
        self.assertEqual(len(rows), 8)
        # TY2022 publishes "Prince George's Coun" (cut to 20 characters): the full name still matches
        self.assertTrue(ad._name_hit("Prince George's Coun", {"prince george's county"}))
        self.assertFalse(ad._name_hit("Allegany County", {"montgomery county"}))


if __name__ == "__main__":
    unittest.main()

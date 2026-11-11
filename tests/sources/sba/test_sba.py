"""Offline tests for the sba pack.

Fixtures (all real data.sba.gov files, downloaded 2026-11-11):
- dataset_page.html is the whole https://data.sba.gov/dataset/7a-504-foia page (it lists the FOIA CSVs).
- The loan files are too big to record (Client.download is never recorded), so these are trimmed real files: the
  header line and whole records copied byte for byte, nothing edited or added:
    FOIA_7a_FY2020_Present_subset.csv    header + first 30 records + the first 8 FY2020 CHGOFF records after them
                                         + the first 10 FY2025 records (FOIA_7a_FY2020_Present_asof_260930.csv)
    FOIA_504_FY2010_Present_subset.csv   header + first 10 records + the first 2 Colorado FY2015 CHGOFF records +
                                         the first 12 other Colorado FY2015 records (FOIA_504_FY2010_Present_asof_260930.csv)
"""
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SPEC = {"description": "SBA 7(a) cohorts FY2020-latest and Colorado 504 loans of FY2015",
        "requests": [
            {"name": "c7a", "endpoint": "cohorts", "params": {"program": "7a", "start_fy": 2020}},
            {"name": "co504", "endpoint": "loans",
             "params": {"program": "504", "approval_fy": 2015, "state": "Colorado",
                        "columns": ["ApprovalFY", "GrossApproval", "LoanStatus", "GrossChargeOffAmount"]}}]}


class TestSba(PackTest):
    source = "sba"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_cohorts_and_loans(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        c = {x["ApprovalFY"]: x for x in self.csv(out, "c7a")}
        self.assertEqual(sorted(c), ["2020", "2025"])
        y = c["2020"]
        self.assertEqual((y["program"], y["as_of"], y["loans"], y["loans_disbursed"], y["loans_pif"],
                          y["loans_chgoff"], y["loans_cancld"], y["loans_exempt"]),
                         ("7a", "2026-09-30", "38", "32", "13", "9", "6", "10"))      # "P I F" counted as PIF
        self.assertEqual((y["gross_approval"], y["sba_guaranteed"], y["disbursed_gross_approval"],
                          y["gross_chargeoff_amount"]), ("12103200", "8854300", "9273200", "589148.12"))
        self.assertEqual((c["2025"]["loans"], c["2025"]["loans_disbursed"]), ("10", "9"))
        co = self.csv(out, "co504")
        self.assertEqual(len(co), 14)                                   # FY2010 and other states dropped
        self.assertEqual(list(co[0]), ["ApprovalFY", "GrossApproval", "LoanStatus", "GrossChargeOffAmount",
                                       "loan_status_code"])
        self.assertEqual({x["ApprovalFY"] for x in co}, {"2015"})
        ch = sorted((x["GrossApproval"], x["GrossChargeOffAmount"]) for x in co if x["loan_status_code"] == "CHGOFF")
        self.assertEqual(ch, [("1051000.0", "265673.22"), ("667000.0", "629924.92")])

    def test_status_filter_and_group_by(self):
        spec = {"requests": [
            {"name": "pif", "endpoint": "loans", "params": {"program": "7a", "approval_fy": 2020, "loan_status": "PIF"}},
            {"name": "g", "endpoint": "cohorts", "params": {"program": "504", "start_fy": 2010, "end_fy": 2015,
                                                            "group_by": ["BorrState"]}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        pif = self.csv(out, "pif")
        self.assertEqual(len(pif), 13)
        self.assertEqual({x["LoanStatus"] for x in pif}, {"P I F"})    # published value kept
        self.assertEqual({x["Program"] for x in pif}, {"7A"})          # padding " 7A" stripped
        g = {(x["ApprovalFY"], x["BorrState"]): x for x in self.csv(out, "g")}
        self.assertEqual(g[("2015", "CO")]["loans"], "14")
        self.assertNotIn("sba_guaranteed", g[("2015", "CO")])          # 504 files have no guaranty column

    def test_bad_specs_are_fix_spec(self):
        for p in ({"program": "8a", "approval_fy": 2020}, {"program": "7a"},
                  {"program": "7a", "approval_fy": 2020, "start_fy": 2019},
                  {"program": "7a", "approval_fy": 2020, "loan_status": "DEFAULT"},
                  {"program": "7a", "approval_fy": 1980}):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "loans", "params": p}]})
            self.assertEqual(r["route"], "fix_spec", (p, r))
        code, r = self.check({"requests": [{"name": "x", "endpoint": "cohorts",
                                            "params": {"program": "7a", "approval_fy": 2020, "group_by": "lender"}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_file_list_from_page(self):
        import testkit
        ad = testkit.load_pack_module("sba")
        with open(os.path.join(FX, "dataset_page.html"), encoding="utf-8") as f:
            html = f.read()
        names = sorted({m.group(0) for m in ad.FILE_RE.finditer(html)})
        self.assertEqual(len(names), 6)
        self.assertEqual(ad._pick(names, "7a", 2005, 2012), ["FOIA_7a_FY2000_FY2009_asof_260930.csv",
                                                             "FOIA_7a_FY2010_FY2019_asof_260930.csv"])
        self.assertEqual(ad._pick(names, "504", 2024, None), ["FOIA_504_FY2010_Present_asof_260930.csv"])

    def test_manual_bundle_and_ingest(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        self.assertEqual([i["filename"] for i in r["items"]], ["FOIA_7a_FY2020_Present_asof_260930.csv",
                                                               "FOIA_504_FY2010_Present_asof_260930.csv"])
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(FX, "FOIA_7a_FY2020_Present_subset.csv"),
                    os.path.join(up, "FOIA_7a_FY2020_Present_asof_260930.csv"))
        shutil.copy(os.path.join(FX, "FOIA_504_FY2010_Present_subset.csv"),
                    os.path.join(up, "FOIA_504_FY2010_Present_asof_260930.csv"))
        sp, out = self.write_spec(SPEC, "spec2.json")
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(len(self.csv(out, "co504")), 14)
        self.assertEqual({x["ApprovalFY"]: x["loans"] for x in self.csv(out, "c7a")}, {"2020": "38", "2025": "10"})


if __name__ == "__main__":
    unittest.main()

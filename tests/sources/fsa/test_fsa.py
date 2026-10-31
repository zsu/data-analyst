"""Offline tests for the fsa pack (Federal Student Aid Data Center library workbooks).

Fixtures (fixtures/index.json), recorded live from studentaid.gov on 2026-10-30:
- PortfolioSummary.xls, PortfoliobyLoanStatus.xls, Portfolio-by-Location.xls, DLPortfoliobyRepaymentPlan.xls: the
  real files, unchanged (recorded with DATA_ANALYST_RECORD, then renamed).
- trimmed_dl_dashboard_ay2025-2026_q4.xlsx and trimmed_grants_ay24-25_q4.xlsx: TRIMMED real copies of
  dl-dashboard-ay2025-2026-q4.xls (3.2 MB) and grants-ay24-25-q4.xls (1.6 MB): both data sheets with their title
  and header rows, the first three schools and every Wyoming school, cell values copied as read by xlrd, saved as
  .xlsx (no .xls writer is available) and served for the .xls URL (the reader accepts an .xlsx under an .xls name).
- not_found.html: the 404 for an award year not yet published (dl-dashboard-ay2026-2027-q1.xls was 404 live).
Reading the real .xls fixtures needs xlrd (an optional package); those tests are skipped without it.
"""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

try:
    import xlrd  # noqa: F401
    HAVE_XLRD = True
except ImportError:
    HAVE_XLRD = False

SUMMARY = {"description": "Total federal student loan portfolio by program, every quarter",
           "requests": [{"name": "portfolio", "endpoint": "portfolio_summary", "params": {}}]}
VOLUME = {"description": "Direct Loans and Pell Grants at Wyoming schools",
          "requests": [{"name": "dl_wy", "endpoint": "dl_volume_by_school",
                        "params": {"award_year": "2025-26", "quarter": 4, "state": "wy"}},
                       {"name": "pell_q4", "endpoint": "grant_volume_by_school",
                        "params": {"award_year": "2024-2025", "quarter": "Q4", "sheet": "Quarterly Activity",
                                   "school": "casper"}}]}


class TestFsa(PackTest):
    source = "fsa"
    sample_spec = SUMMARY

    def test_check_route_api(self):
        code, r = self.check(SUMMARY)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)
        self.assertEqual(r["key_only_needs"], [])

    @unittest.skipUnless(HAVE_XLRD, "xlrd not installed")
    def test_portfolio_summary(self):
        code, r, out = self.fetch(SUMMARY)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "portfolio")
        self.assertEqual(list(rows[0]), ["report", "fiscal_year", "quarter", "period_end", "category",
                                         "dollars_outstanding_billions", "recipients_millions",
                                         "unduplicated_recipients_millions"])
        # FY2007 is an annual row; Total has unduplicated recipients only (footnote mark "Total1" dropped)
        self.assertEqual((rows[0]["fiscal_year"], rows[0]["quarter"], rows[0]["period_end"], rows[0]["category"],
                          rows[0]["dollars_outstanding_billions"]), ("2007", "", "2007-09-30", "Direct Loans", "106.8"))
        last = [x for x in rows if x["period_end"] == max(y["period_end"] for y in rows)]
        total = [x for x in last if x["category"] == "Total"][0]
        # the latest quarter in the 2026-10 file: FY2026 Q3 (ends 2026-06-30), $1,724.5 billion, 42.3 million people
        self.assertEqual((total["fiscal_year"], total["quarter"], total["period_end"]), ("2026", "Q3", "2026-06-30"))
        self.assertEqual((total["dollars_outstanding_billions"], total["unduplicated_recipients_millions"],
                          total["recipients_millions"]), ("1724.5", "42.3", ""))
        self.assertEqual({x["category"] for x in last},
                         {"Direct Loans", "Federal Family Education Loans (FFEL)", "Perkins Loans", "Total"})
        # FY2025 Q1 is published with full precision: kept as published
        q1 = [x for x in rows if x["period_end"] == "2024-12-31" and x["category"] == "Direct Loans"][0]
        self.assertEqual(q1["dollars_outstanding_billions"], "1472.58466176543")
        self.assertTrue(any("fiscal-year quarters" in n for n in r["notes"]), r)

    @unittest.skipUnless(HAVE_XLRD, "xlrd not installed")
    def test_status_sheets_location_and_plans(self):
        spec = {"requests": [{"name": "status", "endpoint": "portfolio_by_loan_status", "params": {"sheet": "Federally Managed"}},
                             {"name": "dl", "endpoint": "portfolio_by_loan_status", "params": {}},
                             {"name": "loc", "endpoint": "portfolio_by_location", "params": {}},
                             {"name": "plans", "endpoint": "portfolio_by_repayment_plan", "params": {}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        st = self.csv(out, "status")
        self.assertEqual(st[0]["report"], "Federally Managed Portfolio by Loan Status")
        dflt = [x for x in st if x["period_end"] == "2026-06-30" and x["category"] == "Cumulative in Default"][0]
        self.assertEqual((dflt["dollars_outstanding_billions"], dflt["recipients_millions"]), ("234.1", "9.3"))
        # the year cell is blank under the first quarter of each year: filled down
        self.assertEqual([x["fiscal_year"] for x in st if x["period_end"] == "2017-03-31"][0], "2017")
        dl = self.csv(out, "dl")
        self.assertEqual(dl[0]["report"], "Direct Loan Portfolio by Loan Status")
        self.assertEqual(len({x["category"] for x in dl}), 7)
        loc = self.csv(out, "loc")
        self.assertEqual(list(loc[0]), ["report", "as_of", "location", "balance_billions", "borrowers_thousands"])
        wy = [x for x in loc if x["location"] == "Wyoming"][0]
        self.assertEqual((wy["as_of"], wy["balance_billions"], wy["borrowers_thousands"]), ("2026-03-31", "1.8", "54.8"))
        self.assertIn("Not Reported", {x["location"] for x in loc})
        self.assertFalse([x for x in loc if x["location"].startswith("*")])     # footnote row dropped
        plans = self.csv(out, "plans")
        save = [x for x in plans if x["category"] == "SAVE"]
        # "-" before REPAYE (which SAVE replaced) began: those quarters have no SAVE row
        self.assertEqual((save[0]["period_end"], save[0]["dollars_outstanding_billions"]), ("2015-12-31", "0.1"))
        other = [x for x in plans if x["category"] == "Other" and x["period_end"] == "2026-06-30"][0]
        self.assertEqual((other["dollars_outstanding_billions"], other["recipients_millions"]), ("9", ""))  # NA***
        self.assertIn("Pay As You Earn", {x["category"] for x in plans})          # "Pay As You Earn*" mark dropped

    def test_volume_by_school(self):
        code, r, out = self.fetch(VOLUME)
        self.assertEqual(code, 0, r)
        dl = self.csv(out, "dl_wy")
        self.assertEqual(list(dl[0])[:11], ["award_year", "activity", "period_start", "period_end", "as_of", "ope_id",
                                            "school", "state", "zip_code", "school_type", "program"])
        self.assertEqual(set(list(dl[0])[11:]), {"recipients", "loans_originated_count", "loans_originated_dollars",
                                                 "disbursements_count", "disbursements_dollars"})
        self.assertEqual({x["state"] for x in dl}, {"WY"})
        wyo = [x for x in dl if x["school"] == "WYOTECH" and x["program"] == "DL PARENT PLUS"][0]
        self.assertEqual((wyo["award_year"], wyo["activity"], wyo["period_start"], wyo["period_end"], wyo["ope_id"]),
                         ("2025-26", "award year to date", "2025-07-01", "2026-06-30", "00915700"))
        self.assertEqual((wyo["recipients"], wyo["loans_originated_dollars"]), ("644", "15454940"))
        # programs a school had no activity in ("-") are left out
        self.assertNotIn("DL GRAD PLUS", {x["program"] for x in dl if x["school"] == "WYOTECH"})
        pell = self.csv(out, "pell_q4")
        self.assertEqual({x["school"] for x in pell}, {"CASPER COLLEGE"})
        self.assertEqual((pell[0]["activity"], pell[0]["period_start"], pell[0]["period_end"], pell[0]["program"],
                          pell[0]["recipients"], pell[0]["disbursements_dollars"]),
                         ("quarter", "2025-04-01", "2025-06-30", "FEDERAL PELL GRANT PROGRAM", "218", "317984.05"))
        self.assertTrue(any("award-year quarter 4" in n for n in r["notes"]), r)

    def test_manual_links_build_file_names(self):
        sp, out = self.write_spec(VOLUME)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        urls = [i["url"] for i in r["manual_items"]]
        base = "https://studentaid.gov/sites/default/files/fsawg/datacenter/library/"
        self.assertEqual(urls, [base + "dl-dashboard-ay2025-2026-q4.xls", base + "grants-ay24-25-q4.xls"])
        self.assertFalse(any(i["needs_key"] for i in r["manual_items"]))

    def test_unpublished_year_is_not_found(self):
        code, r, out = self.fetch({"requests": [{"name": "next", "endpoint": "dl_volume_by_school",
                                                 "params": {"award_year": "2026-27", "quarter": 1}}]})
        self.assertNotEqual(code, 0, r)
        self.assertIn("not found", r.get("message", "") + r.get("error", ""), r)

    def test_bad_specs(self):
        for params, word in (({"award_year": "2025", "quarter": 4}, "ambiguous"),
                             ({"award_year": "2016-17", "quarter": 4}, "2018-19"),
                             ({"award_year": "2025-27", "quarter": 4}, "2025-26"),
                             ({"award_year": "2025-26", "quarter": 5}, "1-4"),
                             ({"quarter": 4}, "award_year")):
            code, r = self.check({"requests": [{"name": "x", "endpoint": "dl_volume_by_school", "params": params}]})
            self.assertEqual(r["route"], "fix_spec", (params, r))
            self.assertIn(word, r["message"], (params, r))
        code, r = self.check({"requests": [{"name": "x", "endpoint": "portfolio_by_loan_status",
                                            "params": {"sheet": "Perkins"}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    @unittest.skipUnless(HAVE_XLRD, "xlrd not installed")
    def test_parse_upload_of_the_workbook(self):
        import shutil
        sp, out = self.write_spec(SUMMARY)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "PortfolioSummary.xls"), os.path.join(up, "portfolio.xls"))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "portfolio")
        self.assertEqual([x["dollars_outstanding_billions"] for x in rows if x["category"] == "Total"][-1], "1724.5")

    def test_layout_change_fails_loudly(self):
        a = load_pack_module("fsa")
        from core.http import DataError
        rows = [{"Some Title": "Federal Fiscal Year", "col2": "", "col3": "Dollars"},
                {"Some Title": "2026", "col2": "Q1", "col3": "1"}]
        with self.assertRaises(DataError) as e:
            a.normalize({"name": "x"}, {"layout": "portfolio", "path": "/x.xls"}, rows)
        self.assertIn("layout changed", str(e.exception))
        self.assertEqual(a._measure("Dollars Outstanding           (in billions)"), "dollars_outstanding_billions")
        self.assertEqual(a._measure("Recipients3     (in millions)"), "recipients_millions")
        self.assertEqual(a._measure("$ of Loans Originated"), "loans_originated_dollars")
        self.assertEqual(a._measure("YTD Disbursements"), "disbursements_dollars")
        self.assertEqual(a._label("Total1"), "Total")


if __name__ == "__main__":
    unittest.main()

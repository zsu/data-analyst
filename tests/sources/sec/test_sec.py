"""Offline tests for the sec pack. Fixtures r001-r007 were recorded from data.sec.gov and efts.sec.gov on
2026-10-20, r008-r009 on 2026-10-22 (Margaree Acquisition Corp., CIK 2129664, was chosen because its companyfacts file is small)."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Apple annual net income; Q4 2023 goodwill impairments; a small company's facts and filings; "
                       "8-K documents mentioning goodwill impairment; Apple Form 4s",
        "requests": [
            {"name": "aapl_net_income", "endpoint": "company_concept",
             "params": {"ticker": "AAPL", "tag": "NetIncomeLoss", "forms": "10-K", "frames_only": True, "duration": "annual"}},
            {"name": "goodwill_impairment_2023q4", "endpoint": "frames",
             "params": {"tag": "GoodwillImpairmentLoss", "period": "CY2023Q4"}},
            {"name": "margaree_facts", "endpoint": "company_facts",
             "params": {"cik": "2129664", "tags": "Assets,NetIncomeLoss,StockholdersEquity"}},
            {"name": "margaree_filings", "endpoint": "submissions", "params": {"cik": "2129664"}},
            {"name": "goodwill_8k", "endpoint": "full_text_search",
             "params": {"q": "\"goodwill impairment\"", "forms": "8-K", "startdt": "2024-01-01", "enddt": "2024-01-05"}},
            {"name": "aapl_form4", "endpoint": "insider_filings",
             "params": {"cik": "320193", "startdt": "2025-01-01", "enddt": "2025-03-31"}}]}


class TestSec(PackTest):
    source = "sec"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        ni = self.csv(out, "aapl_net_income")
        self.assertEqual(ni[0]["cik"], "0000320193")              # ticker resolved through efts
        last = ni[-1]
        self.assertEqual((last["end"], last["val"], last["frame"], last["period_days"]),
                         ("2025-09-27", "112010000000", "CY2025", "364"))
        self.assertTrue(all(350 <= int(x["period_days"]) <= 380 for x in ni))
        self.assertEqual(len({x["frame"] for x in ni}), len(ni))   # one value per calendar year
        fr = self.csv(out, "goodwill_impairment_2023q4")
        self.assertEqual(len(fr), 125)
        self.assertEqual(fr[0]["entityName"], "CONSTELLATION BRANDS, INC.")
        facts = self.csv(out, "margaree_facts")
        self.assertEqual({x["tag"] for x in facts}, {"Assets", "NetIncomeLoss", "StockholdersEquity"})
        self.assertIn("3293", [x["val"] for x in facts if x["tag"] == "Assets"])
        fl = self.csv(out, "margaree_filings")
        self.assertEqual(fl[0]["form"], "10-Q")
        self.assertTrue(fl[0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/2129664/"))
        docs = self.csv(out, "goodwill_8k")
        self.assertEqual(len(docs), 11)
        self.assertEqual(docs[0]["adsh"], "0001437749-24-000383")
        f4 = self.csv(out, "aapl_form4")
        self.assertEqual(f4[0]["insider"], "WAGNER SUSAN")
        self.assertTrue(all(x["form"] == "4" for x in f4))

    def test_user_agent_never_github(self):
        from core import packs
        ad = packs.load("sec").adapter
        old = os.environ.get("SEC_USER_AGENT")
        try:
            os.environ["SEC_USER_AGENT"] = "tool (+https://github.com/x/y)"
            self.assertEqual(ad.user_agent(), ad.DEFAULT_UA)
            os.environ["SEC_USER_AGENT"] = "Jane Analyst jane.analyst@example.org"
            self.assertEqual(ad.user_agent(), "Jane Analyst jane.analyst@example.org")
        finally:
            if old is None:
                os.environ.pop("SEC_USER_AGENT", None)
            else:
                os.environ["SEC_USER_AGENT"] = old
        self.assertNotIn("github", ad.DEFAULT_UA.lower())

    def test_bad_specs(self):
        for bad in ({"requests": [{"name": "a", "endpoint": "company_concept", "params": {"tag": "Assets"}}]},
                    {"requests": [{"name": "b", "endpoint": "frames", "params": {"tag": "Assets", "period": "2023"}}]}):
            code, r = self.check(bad)
            self.assertEqual(r["route"], "fix_spec", r)

    def test_ratio_unit_spelling_per_endpoint(self):
        # company_concept keys units "USD/shares"; the frames URL needs "USD-per-shares". Each request below
        # uses the other endpoint's spelling (r008, r009 recorded 2026-10-22).
        spec = {"requests": [
            {"name": "msft_dividends_ps", "endpoint": "company_concept",
             "params": {"cik": "789019", "tag": "CommonStockDividendsPerShareDeclared", "unit": "USD-per-shares",
                        "forms": "10-K", "frames_only": True, "duration": "annual"}},
            {"name": "proforma_eps_2022", "endpoint": "frames",
             "params": {"tag": "BusinessAcquisitionProFormaEarningsPerShareBasic", "unit": "USD/shares",
                        "period": "CY2022"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        dv = self.csv(out, "msft_dividends_ps")
        self.assertEqual(len(dv), 16)
        self.assertEqual((dv[0]["end"], dv[0]["val"], dv[0]["unit"]), ("2008-06-30", "0.44", "USD/shares"))
        fr = self.csv(out, "proforma_eps_2022")
        self.assertEqual(len(fr), 111)
        self.assertEqual({x["unit"] for x in fr}, {"USD/shares"})
        from core import packs
        ad = packs.load("sec").adapter
        self.assertEqual(ad.unit_for("frames", "shares/USD"), "shares-per-USD")
        self.assertEqual(ad.unit_for("company_facts", "USD-per-shares"), "USD/shares")
        self.assertEqual(ad.unit_for("company_concept", "USD"), "USD")

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = {i["name"]: i for i in json.load(f)["items"]}
        self.assertEqual(items["margaree_facts#1"]["url"], "https://data.sec.gov/api/xbrl/companyfacts/CIK0002129664.json")
        self.assertIn("efts.sec.gov", items["goodwill_8k#1"]["url"])


if __name__ == "__main__":
    unittest.main()

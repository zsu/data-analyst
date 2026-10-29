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
        self.assertEqual(len({x["end"] for x in ni}), len(ni))     # one value per period
        # SEC framed no fact for fiscal 2010 or 2012: frames_only keeps their latest-filed facts, with a note
        fy10 = [x for x in ni if x["end"] == "2010-09-25"]
        self.assertEqual((fy10[0]["val"], fy10[0]["frame"]), ("14013000000", ""))
        self.assertEqual(len(ni), 19)
        self.assertTrue(any("aapl_net_income" in n and "2 period(s) with no SEC frame were restored" in n
                            for n in r["notes"]), r["notes"])
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

    def test_unknown_tag_names_the_request_and_spares_the_rest(self):
        # r010 recorded live on 2026-10-28: an unknown company_concept tag answers 404. Like the standard packs, every
        # request runs and the missing ones are named together; with allow_empty the others are written.
        known = {"name": "known", "endpoint": "company_concept",
                 "params": {"cik": "320193", "tag": "NetIncomeLoss", "forms": "10-K", "duration": "annual"}}
        unknown = {"name": "unknown_tag", "endpoint": "company_concept",
                   "params": {"cik": "320193", "tag": "RevenueFromLemonadeStands"}}
        code, r, out = self.fetch({"requests": [unknown, known]})
        self.assertEqual((code, r["kind"]), (2, "not_found"), r)
        self.assertIn("'unknown_tag'", r["message"])
        self.assertIn("company_facts", r["message"])
        code, r, out = self.fetch({"requests": [{**unknown, "allow_empty": True}, known]})
        self.assertEqual(code, 0, r)
        self.assertTrue(self.csv(out, "known"))
        self.assertTrue(any("unknown_tag" in n and "404" in n for n in r["notes"]), r["notes"])

    def test_company_search_without_match_is_an_empty_table(self):
        # r011 recorded live 2026-10-28: a name no filer has. No match is an answer: the other requests are
        # written and a note names the search.
        none = {"name": "nomatch", "endpoint": "company_search", "params": {"text": "Qzxvy Kwrrt Holdings"}}
        found = {"name": "found", "endpoint": "company_search", "params": {"text": "AAPL"}}
        code, r, out = self.fetch({"requests": [none, found]})
        self.assertEqual(code, 0, r)
        self.assertEqual(self.csv(out, "nomatch"), [])
        self.assertTrue(self.csv(out, "found"))
        self.assertTrue(any(n.startswith("nomatch: no SEC filer matched") for n in r["notes"]), r["notes"])

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
        self.assertEqual(len(dv), 19)                 # 16 framed years and 3 that SEC left without a frame
        self.assertEqual((dv[0]["end"], dv[0]["val"], dv[0]["unit"]), ("2008-06-30", "0.44", "USD/shares"))
        fr = self.csv(out, "proforma_eps_2022")
        self.assertEqual(len(fr), 111)
        self.assertEqual({x["unit"] for x in fr}, {"USD/shares"})
        from core import packs
        ad = packs.load("sec").adapter
        self.assertEqual(ad.unit_for("frames", "shares/USD"), "shares-per-USD")
        self.assertEqual(ad.unit_for("company_facts", "USD-per-shares"), "USD/shares")
        self.assertEqual(ad.unit_for("company_concept", "USD"), "USD")

    def test_one_value_per_period(self):
        from core import packs
        ad = packs.load("sec").adapter

        def fact(start, end, val, filed, frame=""):
            return {"tag": "T", "unit": "USD", "start": start, "end": end, "val": val, "filed": filed,
                    "accn": filed, "frame": frame, "period_days": ad._days(start, end)}
        rows = [fact("2019-01-01", "2019-12-31", 1, "2020-02-01", "CY2019"),
                fact("2019-01-01", "2019-12-31", 1, "2021-02-01"),            # repeated in a later filing
                fact("2020-01-01", "2020-12-31", 2, "2021-02-01"),            # no framed fact for 2020
                fact("2020-01-01", "2020-12-31", 3, "2022-02-01"),            # restated later: the latest wins
                fact("2021-01-03", "2022-01-01", 4, "2022-02-01"),
                fact("2021-01-01", "2021-12-31", 4, "2023-02-01", "CY2021"),  # 52/53-week twin of a framed year
                fact("2022-01-01", "2022-06-30", 5, "2022-08-01"),            # 6-month YTD: overlaps, skipped
                fact("2022-04-01", "2022-06-30", 6, "2022-08-01")]            # a quarter: no framed quarter here
        out, restored, skipped = ad.one_per_period(rows)
        self.assertEqual([(x["end"], x["val"]) for x in out], [("2019-12-31", 1), ("2020-12-31", 3), ("2021-12-31", 4)])
        self.assertEqual((restored, skipped), (1, 2))
        out, restored, _ = ad.one_per_period(rows, "quarter")          # a quarterly series takes the quarter
        self.assertIn(("2022-06-30", 6), [(x["end"], x["val"]) for x in out])

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

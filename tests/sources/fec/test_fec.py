"""Offline tests for the fec pack. Fixtures were recorded live from api.open.fec.gov (October 2026) with
DATA_ANALYST_RECORD; the client drops api_key from recorded URLs. The Schedule A sample uses committee
(PAC) receipts only, so no individual contributor's details are stored in the repository."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "2024 Pennsylvania Senate race: candidates' money, small-donor share and outside spending for one "
                       "candidate, and a sample of committee (PAC) contributions",
        "requests": [
            {"name": "pa_senate_2024", "endpoint": "elections", "params": {"office": "senate", "state": "PA", "cycle": 2024}},
            {"name": "casey_by_size", "endpoint": "contributions_by_size", "params": {"candidate_id": "S6PA00217", "cycle": 2024}},
            {"name": "casey_outside", "endpoint": "independent_expenditures_by_candidate",
             "params": {"candidate_id": "S6PA00217", "cycle": 2024}},
            {"name": "casey_pac_receipts_sample", "endpoint": "schedule_a",
             "params": {"committee_id": "C00431056", "two_year_transaction_period": 2024, "is_individual": "false",
                        "per_page": 5}, "max_rows": 8}]}


class TestFec(PackTest):
    source = "fec"
    sample_spec = SPEC

    def test_check_api_keyless(self):
        small = {"requests": SPEC["requests"][:3]}
        code, r = self.check(small)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        race = self.csv(out, "pa_senate_2024")
        self.assertEqual(len(race), 11)
        self.assertEqual(race[0]["candidate_id"], "S6PA00217")
        self.assertEqual(race[0]["total_receipts"], "64886501.07")
        size = {x["size"]: x["total"] for x in self.csv(out, "casey_by_size")}
        self.assertEqual(size["200"], "4035976.17")
        ie = {x["support_oppose_indicator"]: x["total"] for x in self.csv(out, "casey_outside")}
        self.assertEqual(ie["O"], "103689638.03")
        rows = self.csv(out, "casey_pac_receipts_sample")      # two keyset pages of 5, cut at 8
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(x["committee_id"] == "C00431056" for x in rows))
        self.assertTrue(any("stopped at 8 rows" in n for n in r["notes"]))

    def test_unfiltered_schedule_is_fix_spec(self):
        bad = {"requests": [{"name": "all", "endpoint": "schedule_a", "params": {"two_year_transaction_period": 2024}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_big_itemized_request_needs_key(self):
        big = {"requests": [{"name": "pac", "endpoint": "schedule_a", "params": {"committee_id": "C00431056"}}]}
        code, r = self.check(big)
        self.assertTrue(r.get("key_only_needs"), r)

    def test_manual_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(len(items), 4)
        self.assertTrue(all("api_key=DEMO_KEY" in i["url"] for i in items))


if __name__ == "__main__":
    unittest.main()

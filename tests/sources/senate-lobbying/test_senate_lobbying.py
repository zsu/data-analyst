"""Offline tests for the senate-lobbying pack. Fixtures were recorded live from lda.gov (October 2026)
with DATA_ANALYST_RECORD (anonymous access, no key)."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

BASE = {"registrant_name": "Pfizer", "client_name": "Pfizer", "filing_year": 2024, "filing_period": "first_quarter"}
SPEC = {"description": "Pfizer's own lobbying reports for Q1 2024 (original and amendment), by filing and by activity; "
                       "LD-203 contributions of one lobbyist",
        "requests": [
            {"name": "pfizer_q1_2024", "endpoint": "filings", "params": BASE},
            {"name": "pfizer_q1_2024_tax", "endpoint": "filing_activities", "params": {**BASE, "issue_code": "TAX"}},
            {"name": "pomper_2024", "endpoint": "contributions", "params": {"lobbyist_name": "Pomper", "filing_year": 2024}}]}


class TestSenateLobbying(PackTest):
    source = "senate-lobbying"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        f = {x["filing_type"]: x for x in self.csv(out, "pfizer_q1_2024")}
        self.assertEqual(f["Q1"]["expenses"], "3550000.00")
        self.assertEqual(f["1A"]["expenses"], "3740000.00")
        self.assertEqual(f["1A"]["is_amendment"], "True")
        self.assertEqual(f["Q1"]["is_amendment"], "False")
        acts = self.csv(out, "pfizer_q1_2024_tax")
        self.assertTrue(acts)
        self.assertEqual({a["general_issue_code"] for a in acts}, {"TAX"})
        self.assertIn("lobbyists", acts[0])
        items = self.csv(out, "pomper_2024")
        self.assertEqual(len(items), 49)
        self.assertEqual(items[0]["payee_name"], "DEMOCRATIC SENATORIAL CAMPAIGN COMMITTEE")

    def test_ignored_entity_filters_are_renamed(self):
        """/clients/ ignores state and client_id, /registrants/ ignores registrant_id (every row comes back,
        live 2026-10-22); they filter on client_state and id."""
        import urllib.parse
        from core import generic_rest as gr
        from core.packfiles import read_json
        from testkit import load_pack_module, pack_dir
        ad = load_pack_module("senate-lobbying")
        eps = read_json(pack_dir("senate-lobbying"), "endpoints.json")["endpoints"]
        p = ad.before_request({}, eps["clients"], {"state": "OR", "client_id": "9876", "client_name": "port"})
        self.assertEqual(p, {"client_state": "OR", "id": "9876", "client_name": "port"})
        p = ad.before_request({}, eps["registrants"], {"registrant_id": "321", "state": "OR"})
        self.assertEqual(p, {"id": "321", "state": "OR"})                       # registrants do filter on state
        p = ad.before_request({}, eps["filings"], {"registrant_id": "321", "client_state": "OR"})
        self.assertEqual(p, {"registrant_id": "321", "client_state": "OR"})     # filings take these names
        sp, out = self.write_spec({"requests": [{"name": "c", "endpoint": "clients", "params": {"state": "OR"}}]})
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertIn("client_state=OR", r["manual_items"][0]["url"])
        self.assertNotIn("&state=", r["manual_items"][0]["url"])

    def test_unfiltered_is_fix_spec(self):
        bad = {"requests": [{"name": "all", "endpoint": "filings", "params": {"ordering": "-dt_posted"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        self.assertTrue(os.path.exists(r["instructions"]))


if __name__ == "__main__":
    unittest.main()

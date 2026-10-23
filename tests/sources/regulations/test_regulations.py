"""Offline tests for the regulations pack. Fixtures were recorded live from api.regulations.gov (October
2026) with DATA_ANALYST_RECORD; the key is never part of a fixture (the client drops api_key from URLs)."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Power-plant greenhouse gas rule docket: proposed rule documents and posted comment count",
        "requests": [
            {"name": "docket_rules", "endpoint": "documents",
             "params": {"filter[docketId]": "EPA-HQ-OAR-2023-0072", "filter[documentType]": "Proposed Rule"}},
            {"name": "docket_comments", "endpoint": "comment_count",
             "params": {"filter[docketId]": "EPA-HQ-OAR-2023-0072"}}]}


class TestRegulations(PackTest):
    source = "regulations"
    sample_spec = SPEC

    def test_check_api_keyless(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        docs = self.csv(out, "docket_rules")
        self.assertEqual(len(docs), 3)
        self.assertIn("documentType", docs[0])          # "attributes." prefix dropped
        self.assertEqual({d["documentType"] for d in docs}, {"Proposed Rule"})
        self.assertEqual(docs[0]["frDocNum"], "2023-25580")
        cnt = self.csv(out, "docket_comments")
        self.assertEqual(cnt[0]["totalElements"], "8202")
        self.assertEqual(cnt[0]["filter[docketId]"], "EPA-HQ-OAR-2023-0072")

    def test_big_request_needs_key(self):
        big = {"requests": [{"name": "all_epa", "endpoint": "documents", "params": {"filter[agencyId]": "EPA"},
                             "max_rows": 10000}]}
        code, r = self.check(big)
        self.assertTrue(r.get("key_only_needs"), r)
        self.assertNotEqual(r["route"], "api")

    def test_manual_links_carry_demo_key(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(all("api_key=DEMO_KEY" in i["url"] for i in items))


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the USPTO Open Data Portal pack.

No USPTO_API_KEY was available when the pack was built, so:
- doc_applications_search.json is the JSON RESPONSE SAMPLE that USPTO publishes for the Patent File Wrapper
  search endpoint (https://data.uspto.gov/apis/patent-file-wrapper/search, file
  /documents/api/search/sample-response-200.md), copied unchanged. It is mapped to the query that would
  return it (applicationNumberText:18045436).
- live_probe_401.json is a real response recorded on 2026-10-20 from the probe URL without a key.
"""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Application 18045436 (documented example)",
        "requests": [{"name": "app_18045436", "endpoint": "applications_search",
                      "params": {"q": "applicationNumberText:18045436"}}]}
FAKE = {"USPTO_API_KEY": "test-placeholder-not-a-key"}


class TestUspto(PackTest):
    source = "uspto"
    sample_spec = SPEC

    def test_check_without_key_gives_setup(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--check", "--env", "local")
        self.assertEqual(r["route"], "run_setup", r)
        self.assertIn("USPTO_API_KEY", r["message"])

    def test_rejected_key_is_reported(self):
        # the recorded probe answers 401, so a key that is present but wrong must not route to api
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--check", "--env", "local", env=FAKE)
        self.assertEqual(r["key_ok"], False, r)
        self.assertEqual(r["route"], "run_setup", r)
        self.assertNotIn("test-placeholder-not-a-key", json.dumps(r))

    def test_fetch_documented_example(self):
        code, r, out = self.fetch(SPEC, env=FAKE)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "app_18045436")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["applicationNumberText"], "18045436")
        self.assertEqual(row["applicationMetaData.patentNumber"], "12000000")
        self.assertEqual(row["applicationMetaData.grantDate"], "2024-06-04")
        self.assertEqual(row["applicationMetaData.applicationStatusDescriptionText"], "Patented Case")
        self.assertIn("C12Q1/6869", row["applicationMetaData.cpcClassificationBag"])

    def test_application_number_format_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "application", "params": {"applicationNumberText": "14/412,875"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle_uses_header_key(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            it = json.load(f)["items"][0]
        self.assertEqual(it["key_header"], "X-API-KEY")
        self.assertEqual(it["key_env"], "USPTO_API_KEY")
        self.assertNotIn("{API_KEY}", it["url"])


if __name__ == "__main__":
    unittest.main()

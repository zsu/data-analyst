"""Offline tests for the nih pack. Fixtures were recorded live from api.reporter.nih.gov on 2026-10-20 with
DATA_ANALYST_RECORD (POST bodies are keyed by the sha256 prefix of the JSON body)."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "NCI breast cancer projects at Vermont organizations, FY2024; opioid (RCDC) projects in Vermont, FY2024; publications of one project",
        "requests": [
            {"name": "vt_nci_breast_2024", "endpoint": "projects",
             "params": {"fiscal_years": [2024], "org_states": ["VT"], "agencies": ["NCI"], "text": "breast cancer"}},
            {"name": "vt_opioids_2024", "endpoint": "projects",
             "params": {"fiscal_years": [2024], "org_states": ["VT"], "spending_categories": ["Opioids"],
                        "include_fields": ["ApplId", "CoreProjectNum", "FiscalYear", "ProjectTitle", "AwardAmount",
                                           "AgencyIcAdmin"]}},
            {"name": "pubs_r01ca282725", "endpoint": "publications",
             "params": {"core_project_nums": ["R01CA282725"]}}]}


class TestNih(PackTest):
    source = "nih"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "vt_nci_breast_2024")
        self.assertEqual(len(rows), 7)
        first = [x for x in rows if x["appl_id"] == "10931667"][0]
        self.assertEqual(first["award_amount"], "686647")
        self.assertEqual(first["organization.org_state"], "VT")
        self.assertEqual(first["subproject_id"], "")             # exclude_subprojects defaults to true
        op = self.csv(out, "vt_opioids_2024")
        self.assertEqual(len(op), 3)
        self.assertIn("R01DA057308", [x["core_project_num"] for x in op])
        pubs = self.csv(out, "pubs_r01ca282725")
        self.assertIn("39270543", [p["pmid"] for p in pubs])

    def test_no_criteria_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "all", "endpoint": "projects", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_unknown_rcdc_category_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "projects",
                                            "params": {"fiscal_years": [2024], "spending_categories": ["Cancr"]}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("Cancer", r["message"])

    def test_search_rcdc(self):
        code, r = self.cli("--search", "alzheimer")
        self.assertEqual(code, 0, r)
        self.assertTrue(any(h.get("id") == 40 for h in r["results"]), r)

    def test_publications_paging_never_asks_past_offset_9999(self):
        # Live 2026-10-22: /publications/search answers HTTP 400 for offset > 9,999 (projects allow 15,000).
        # A fake API with 25,000 publications checks that the pack's paging stops at 10,000 rows.
        import json as _json
        from core import generic_rest as gr
        from core import packfiles
        ep = packfiles.read_json(self.pack_dir, "endpoints.json")["endpoints"]["publications"]
        asked = []

        class Resp:
            def __init__(self, d):
                self.body = _json.dumps(d).encode()
            def json(self):
                return _json.loads(self.body)

        class FakeClient:
            def request(self, method, path, q, json_body=None):
                off, lim = json_body["offset"], json_body["limit"]
                asked.append((off, lim))
                if off > 9999 or lim > 500:
                    raise AssertionError(f"asked offset={off} limit={lim}")
                n = max(0, min(lim, 25000 - off))
                return Resp({"meta": {"total": 25000},
                             "results": [{"coreproject": "X", "pmid": off + i, "applid": 1} for i in range(n)]})

        rows, meta = gr.fetch_request(FakeClient(), ep, {"criteria": {"core_project_nums": ["X"]}}, max_rows=None)
        self.assertEqual(len(rows), 10000)
        self.assertTrue(meta["truncated"])
        self.assertEqual(max(o for o, _ in asked), 9500)
        code, r = self.check({"requests": [{"name": "p", "endpoint": "publications", "max_rows": 12000,
                                            "params": {"pmids": [1]}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("10,000", r["message"])

    def test_manual_bundle_is_post(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        self.assertTrue(all(i["method"] == "POST" and "criteria" in i["body"] for i in r["items"]), r["items"])


if __name__ == "__main__":
    unittest.main()

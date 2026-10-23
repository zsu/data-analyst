"""Offline tests for the federal-register pack. Fixtures were recorded live from www.federalregister.gov
(October 2026) with DATA_ANALYST_RECORD."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Executive orders published in 2024 (Biden) and EPA final rules per year",
        "requests": [
            {"name": "eo_2024", "endpoint": "executive_orders",
             "params": {"conditions[president][]": "joe-biden", "conditions[publication_date][year]": "2024"}},
            {"name": "epa_rules_by_year", "endpoint": "document_counts",
             "params": {"facet": "yearly", "conditions[type][]": "RULE",
                        "conditions[agencies][]": "environmental-protection-agency"}}]}


class TestFederalRegister(PackTest):
    source = "federal-register"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        eos = self.csv(out, "eo_2024")
        self.assertEqual(len(eos), 18)
        self.assertIn("executive_order_number", eos[0])
        self.assertEqual(eos[0]["executive_order_number"], "14132")
        self.assertEqual(eos[0]["president.identifier"], "joe-biden")
        counts = self.csv(out, "epa_rules_by_year")
        self.assertEqual(list(counts[0]), ["key", "name", "count"])
        by = {c["name"]: c["count"] for c in counts}
        self.assertEqual(by["1995"], "729")

    def test_repeated_documents_across_pages_dropped(self):
        """Page-number paging over order=oldest can repeat a document on two pages (live 2026-10-22)."""
        from testkit import load_pack_module
        ad = load_pack_module("federal-register")
        ep = {"path": "/documents.json"}
        rows = [{"document_number": "2019-00011", "type": "Notice"}, {"document_number": "2019-00012", "type": "Notice"},
                {"document_number": "2019-00011", "type": "Notice"}, {"document_number": "", "type": "Notice"}]
        got = ad.normalize({}, ep, rows)
        self.assertEqual([r["document_number"] for r in got], ["2019-00011", "2019-00012", ""])
        facet = ad.normalize({}, {"path": "/documents/facets/yearly"}, [{"2019-01-01.count": 7, "2019-01-01.name": "2019"}])
        self.assertEqual(facet, [{"key": "2019-01-01", "name": "2019", "count": 7}])

    def test_bad_facet_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "document_counts", "params": {"facet": "decade"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(r["instructions"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("federalregister.gov/api/v1/documents.json", text)


if __name__ == "__main__":
    unittest.main()

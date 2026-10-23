"""Offline tests for the open-payments pack. Fixtures were recorded live on 2026-10-20 with
DATA_ANALYST_RECORD (state totals summary; 2024 general payments, Vermont, Pfizer)."""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Industry payments to Vermont recipients, program year 2024: state totals and Pfizer general payments",
        "requests": [
            {"name": "vt_totals_2024", "endpoint": "query",
             "params": {"dataset": "state_totals", "where": {"state_code": "VT", "program_year": "2024"},
                        "columns": ["state_code", "program_year", "payment_type", "recipient_type",
                                    "total_number_of_physicians", "total_payment_amount_physician",
                                    "total_payment_count_physician"]}},
            {"name": "vt_pfizer_2024", "endpoint": "query",
             "params": {"dataset": "general_2024", "where": {"recipient_state": "VT"},
                        "conditions": [{"property": "applicable_manufacturer_or_applicable_gpo_making_payment_name",
                                        "value": "%pfizer%", "operator": "LIKE"}],
                        "columns": ["record_id", "covered_recipient_type", "recipient_city",
                                    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
                                    "total_amount_of_payment_usdollars", "date_of_payment",
                                    "nature_of_payment_or_transfer_of_value"]}}]}


class TestOpenPayments(PackTest):
    source = "open-payments"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        tot = self.csv(out, "vt_totals_2024")
        gen = [t for t in tot if t["payment_type"] == "General" and t["recipient_type"] == "Covered Recipient Physician"]
        self.assertEqual(gen[0]["total_payment_amount_physician"], "1324575.46")
        pf = self.csv(out, "vt_pfizer_2024")
        self.assertEqual(len(pf), 26)
        self.assertTrue(all(p["applicable_manufacturer_or_applicable_gpo_making_payment_name"].upper().startswith("PFIZER")
                            for p in pf))

    def test_unfiltered_payment_file_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "all", "endpoint": "query", "params": {"dataset": "general_2024"}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_amount_comparison_is_fix_spec(self):
        bad = {"requests": [{"name": "big", "endpoint": "query", "params": {
            "dataset": "general_2024", "where": {"recipient_state": "VT"},
            "conditions": [{"property": "total_amount_of_payment_usdollars", "value": "900", "operator": ">"}]}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("TEXT", r["message"])

    def test_manual_links_carry_filters(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(r["dir"], "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertTrue(all("/datastore/query/" in u and "conditions%5B0%5D" in u for u in urls), urls)


if __name__ == "__main__":
    unittest.main()

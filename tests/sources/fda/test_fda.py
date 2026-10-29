"""Offline tests for the fda pack. Fixtures r001-r003 were recorded live from api.fda.gov on 2026-10-20 (r004-r006
keyless on 2026-10-22: counts with limit 999, a 404 "Nothing to count") with
DATA_ANALYST_RECORD (a count query, a search page, and a 404 "No matches found" answer)."""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

SPEC = {"description": "Metformin adverse-event reports received in 2024: top reactions; Class I food recalls reported in January 2024",
        "requests": [
            {"name": "metformin_reactions_2024", "endpoint": "count",
             "params": {"dataset": "drug_event",
                        "search": "patient.drug.openfda.generic_name.exact:\"METFORMIN HYDROCHLORIDE\" AND receivedate:[20240101 TO 20241231]",
                        "count": "patient.reaction.reactionmeddrapt.exact", "limit": 10}},
            {"name": "food_recalls_class1", "endpoint": "search", "max_rows": 5,
             "params": {"dataset": "food_enforcement",
                        "search": "classification:\"Class I\" AND report_date:[20240101 TO 20240131]",
                        "sort": "report_date:asc"}}]}

NONE = {"description": "no match", "requests": [{"name": "none", "endpoint": "search", "max_rows": 5,
        "params": {"dataset": "drug_enforcement", "search": "recalling_firm:\"ZZQXNOFIRM\""}}]}


class TestFda(PackTest):
    source = "fda"
    sample_spec = SPEC

    def test_check_api_without_key(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        self.assertEqual(r["key_required"], "optional")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rx = self.csv(out, "metformin_reactions_2024")
        self.assertEqual(len(rx), 10)
        self.assertEqual(rx[0]["term"], "DIARRHOEA")
        self.assertEqual(rx[0]["count"], "502")
        fr = self.csv(out, "food_recalls_class1")
        self.assertEqual(len(fr), 5)
        self.assertEqual(fr[0]["classification"], "Class I")
        self.assertEqual(fr[0]["recall_number"], "F-0690-2024")
        self.assertTrue(any("66 in total" in n for n in r["notes"]), r["notes"])

    def test_no_match_is_clear_error(self):
        code, r, out = self.fetch(NONE)
        self.assertEqual(code, 2, r)
        self.assertIn("no records matching", r["message"])

    def test_no_match_allowed_empty(self):
        spec = {**NONE, "requests": [{**NONE["requests"][0], "allow_empty": True}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)

    def test_keyless_count_asks_999_and_says_when_cut(self):
        # r004-r005 recorded keyless on 2026-10-22. Without a key openFDA refuses limit=1000 on device/event and
        # drug/event (HTTP 403 API_KEY_MISSING), so a count with no limit is sent with limit=999 (the fixture key);
        # a term list as long as the limit is reported as cut off (status partial).
        spec = {"requests": [
            {"name": "device_generic_jan2024", "endpoint": "count",
             "params": {"dataset": "device_event", "search": "date_received:[20240101 TO 20240131]",
                        "count": "device.generic_name.exact"}},
            {"name": "shortage_categories", "endpoint": "count",
             "params": {"dataset": "drug_shortages", "count": "therapeutic_category"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "partial", r)
        self.assertTrue(any("device_generic_jan2024: stopped at 999 terms" in n for n in r["truncated"]), r)
        self.assertEqual(len(self.csv(out, "device_generic_jan2024")), 999)
        cats = self.csv(out, "shortage_categories")
        self.assertEqual((cats[0]["term"], cats[0]["count"]), ("Anesthesia", "370"))
        self.assertFalse(any("shortage_categories" in n for n in r.get("truncated", [])))
        sp, out2 = self.write_spec(spec)
        code, m = self.cli(sp, "--out", out2, "--manual-bundle")
        self.assertEqual(code, 0, m)
        text = open(m["instructions"], encoding="utf-8").read()
        self.assertIn("limit=999", text)
        self.assertNotIn("limit=1000", text)

    def test_date_count_is_not_cut_at_1000(self):
        # r007 recorded live 2026-10-28 (a date count ignores limit): six years of daily counts are 2,123 rows,
        # all kept; only term counts are capped by openFDA's limit.
        spec = {"requests": [{"name": "daily", "endpoint": "count",
                               "params": {"dataset": "drug_event", "count": "receivedate",
                                          "search": "receivedate:[20200101 TO 20251231] AND "
                                                    "patient.drug.openfda.generic_name:\"metformin hydrochloride\""}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(len(self.csv(out, "daily")), 2123)

    def test_count_404_reports_openfda_message(self):
        # r006 (2026-10-22): drug/shortages cannot count therapeutic_category.exact; openFDA answers HTTP 404
        # "Nothing to count", which is not a "no records match" case.
        spec = {"requests": [{"name": "shortage_categories_exact", "endpoint": "count",
                              "params": {"dataset": "drug_shortages", "count": "therapeutic_category.exact"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 2, r)
        self.assertIn("Nothing to count", r["message"])
        self.assertNotIn("no records matching", r["message"])

    def test_unknown_dataset_and_skip_are_fix_spec(self):
        for bad in ({"requests": [{"name": "x", "endpoint": "search", "params": {"dataset": "drug/events"}}]},
                    {"requests": [{"name": "x", "endpoint": "search", "params": {"dataset": "drug_event", "skip": 30000}}]},
                    {"requests": [{"name": "x", "endpoint": "count", "params": {"dataset": "drug_event", "count": "serious",
                                                                                  "limit": 5000}}]}):
            code, r = self.check(bad)
            self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_links_use_plain_paths(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        text = open(r["instructions"], encoding="utf-8").read()
        self.assertIn("https://api.fda.gov/drug/event.json?", text)
        self.assertNotIn("%2F", text)


if __name__ == "__main__":
    unittest.main()

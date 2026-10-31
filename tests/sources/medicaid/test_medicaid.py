"""medicaid pack tests (offline).

Fixtures recorded live from data.medicaid.gov (DKAN, keyless) on 2026-10-30 with DATA_ANALYST_RECORD, except
catalog_trimmed.json: a trimmed REAL copy of /api/1/metastore/schemas/dataset/items (5 of the 561 datasets kept,
unchanged; the full body is 0.8 MB, over the fixture limit).
"""
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

ENROLL_COLS = ["state_abbreviation", "reporting_period", "preliminary_or_updated", "final_report",
               "total_medicaid_and_chip_enrollment", "total_medicaid_enrollment", "total_chip_enrollment",
               "total_medicaid_and_chip_enrollment__footnotes"]
SPEC = {"description": "Medicaid and CHIP enrollment July 2026, VT/NH 2026, Trulicity national Q4 2025, VT expansion spending",
        "requests": [
            {"name": "enroll_latest", "endpoint": "query",
             "params": {"dataset": "enrollment", "conditions[0][property]": "reporting_period",
                        "conditions[0][value]": "202607", **{f"properties[{i}]": c for i, c in enumerate(ENROLL_COLS)}}},
            {"name": "vt_nh_2026", "endpoint": "query",
             "params": {"dataset": "enrollment", "conditions[0][property]": "state_abbreviation",
                        "conditions[0][operator]": "in", "conditions[0][value][0]": "VT", "conditions[0][value][1]": "NH",
                        "conditions[1][property]": "reporting_period", "conditions[1][operator]": ">=",
                        "conditions[1][value]": "202601",
                        "conditions[2][property]": "preliminary_or_updated", "conditions[2][value]": "U",
                        "sorts[0][property]": "reporting_period", "sorts[0][order]": "desc",
                        "properties[0]": "state_abbreviation", "properties[1]": "reporting_period",
                        "properties[2]": "preliminary_or_updated", "properties[3]": "total_medicaid_and_chip_enrollment"}},
            {"name": "trulicity", "endpoint": "query",
             "params": {"dataset": "sdud_2025", "conditions[0][property]": "state", "conditions[0][value]": "XX",
                        "conditions[1][property]": "product_name", "conditions[1][operator]": "like",
                        "conditions[1][value]": "TRULICITY%",
                        "conditions[2][property]": "quarter", "conditions[2][value]": "4"}},
            {"name": "vt_expansion", "endpoint": "query",
             "params": {"dataset": "cms64_new_adult", "conditions[0][property]": "state", "conditions[0][value]": "Vermont"}},
            {"name": "info", "endpoint": "dataset_info", "params": {"dataset": "enrollment"}}]}


class TestMedicaid(PackTest):
    source = "medicaid"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r.get("notes"), [], r)        # every bracketed param is declared: no "not declared" note
        en = self.csv(out, "enroll_latest")
        self.assertEqual(list(en[0]), ENROLL_COLS)
        self.assertEqual(len(en), 51)
        self.assertEqual({x["preliminary_or_updated"] for x in en}, {"P"})
        # the national total of the July 2026 monthly enrollment report (Medicaid.gov highlights)
        self.assertEqual(sum(int(x["total_medicaid_and_chip_enrollment"]) for x in en), 72946510)
        self.assertEqual(sum(int(x["total_medicaid_enrollment"]) for x in en), 65742057)
        self.assertEqual(sum(int(x["total_chip_enrollment"]) for x in en), 7204453)
        vt = self.csv(out, "vt_nh_2026")
        self.assertEqual(len(vt), 12)                 # 2026-01..06 updated rows; 2026-07 has only a preliminary row
        self.assertEqual((vt[1]["state_abbreviation"], vt[1]["reporting_period"],
                          vt[1]["total_medicaid_and_chip_enrollment"]), ("VT", "202606", "146227"))
        tr = self.csv(out, "trulicity")
        self.assertEqual({x["state"] for x in tr}, {"XX"})
        self.assertEqual(tr[0]["product_name"], "TRULICITY ")          # padded, cut to 10 characters
        self.assertEqual((tr[0]["utilization_type"], tr[0]["number_of_prescriptions"]), ("FFSU", "47"))
        ex = self.csv(out, "vt_expansion")
        self.assertEqual(ex[0]["total_computable_all_medical_assistance_expenditures"], "374,032,607")
        self.assertEqual(ex[0]["quarter_end_date"], "2014-03-31")
        info = self.csv(out, "info")
        self.assertEqual(info[0]["identifier"], "6165f45b-ca93-5bb5-9d06-db29c692a360")

    def test_catalog_list(self):
        spec = {"requests": [{"name": "cat", "endpoint": "datasets", "params": {}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "cat")
        self.assertIn("State Drug Utilization Data 2026", {x["title"] for x in rows})
        self.assertIn("modified", rows[0])

    def test_catalog_alias_resolves(self):
        from core import packs, generic_rest as gr
        pack = packs.load("medicaid")
        ep, params, probs = gr.resolve(pack, SPEC["requests"][2])
        self.assertEqual(probs, [])
        self.assertEqual(params["dataset"], "158a1baa-5506-400a-8ec3-97756f0b0536")
        path, query, _ = gr.split(ep, params)
        self.assertEqual(path, "/api/1/datastore/query/158a1baa-5506-400a-8ec3-97756f0b0536/0")
        self.assertEqual(query["conditions[1][operator]"], "like")

    def test_search(self):
        code, r = self.cli("--search", "drug utilization 2025")
        self.assertEqual(code, 0, r)
        self.assertIn("sdud_2025", str(r))

    def test_manual_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(r["instructions"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("https://data.medicaid.gov/api/1/datastore/query/6165f45b-ca93-5bb5-9d06-db29c692a360/0?", text)
        self.assertIn("limit=8000", text)

    def test_parse_upload_csv_export(self):
        """A dataset page's CSV export (display-style names) uploaded for a query request is read as CSV."""
        from core import packs, generic_rest as gr
        pack = packs.load("medicaid")
        p = os.path.join(self.tmp.name, "enroll_latest.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write('"State Abbreviation","Reporting Period","Total Medicaid and CHIP Enrollment"\n"VT","202607","144684"\n')
        rows = gr.parse_upload(pack, SPEC["requests"][0], [p])
        self.assertEqual(rows, [{"State Abbreviation": "VT", "Reporting Period": "202607",
                                 "Total Medicaid and CHIP Enrollment": "144684"}])

    def test_bad_column_is_reported(self):
        bad = {"requests": [{"name": "bad", "endpoint": "query",
                             "params": {"dataset": "enrollment", "conditions[0][property]": "state",
                                        "conditions[0][value]": "VT"}}]}
        code, r, out = self.fetch(bad)
        self.assertEqual(r.get("status"), "error", r)
        self.assertIn("Column not found", r.get("message", ""))

    def test_bad_spec_refused(self):
        bad = {"requests": [{"name": "x", "endpoint": "query", "params": {"sorts[0][order]": "down"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("dataset", r.get("message", "") + str(r))


if __name__ == "__main__":
    unittest.main()

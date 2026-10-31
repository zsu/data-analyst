"""Offline tests for the usda-fna pack.

Fixtures r001-r005.csv were recorded live on 2026-10-30 with DATA_ANALYST_RECORD (State Options dashboard CSV
exports: Texas and California policy options; BBCE and No BBCE, 17th edition; BBCE, 16th edition; the telephonic
signature waiver). The PDFs are 1.4-5.9 MB, so the PDF fixtures are real pages copied out of them with pypdf:
  sar23_p9_p14_p30.pdf  SNAP State Activity Report FY 2023, pages 9 (Table 5, printed column by column),
                        14 (Table 8) and 30 (Table 18, printed row by row)
  sor17_p132_p134.pdf   State Options Report 17th edition, pages 132-134 (Texas's two profile pages, Utah's first)
Two index entries are not responses to their own URL, to test the gates: the TBA request replays the BBCE export
(the dashboard ignoring a parameter), and the ESAP request answers HTTP 404 (a republished dashboard).
"""
import importlib.util
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module, run  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
SPEC = {"description": "SNAP State Options: Texas and California policy options; BBCE and telephonic signature waiver states",
        "requests": [
            {"name": "tx_ca", "endpoint": "options_by_state", "params": {"state": ["TX", "CA"], "edition": "17"}},
            {"name": "bbce", "endpoint": "option_states", "params": {"policy": "bbce"}},
            {"name": "bbce16", "endpoint": "option_states", "params": {"policy": "bbce", "selection": "BBCE", "edition": "16"}},
            {"name": "tel", "endpoint": "option_states",
             "params": {"policy": "telephonic_signature_waiver", "selection": "Telephonic signature waiver"}}]}
SAR = {"description": "SNAP State Activity Report FY 2023: households by benefit type, admin costs, claim dollars",
       "requests": [{"name": "sar", "endpoint": "activity_tables",
                     "params": {"fiscal_year": 2023,
                                "tables": ["households_by_benefit_type", "admin_costs", "claims_established_dollars"]}}]}


class TestUsdaFna(PackTest):
    source = "usda-fna"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_dashboard(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        tx_ca = self.csv(out, "tx_ca")
        self.assertEqual(list(tx_ca[0]), ["edition", "state", "state_abbr", "policy_option", "option_selection",
                                          "agencies_with_selection", "share_of_agencies"])
        self.assertEqual(len(tx_ca), 30)
        tx = {x["policy_option"]: x for x in tx_ca if x["state_abbr"] == "TX"}
        self.assertEqual(tx["Broad-Based Categorical Eligibility (BBCE)"]["option_selection"], "BBCE")
        self.assertEqual(tx["Broad-Based Categorical Eligibility (BBCE)"]["agencies_with_selection"], "44")
        self.assertEqual(tx["Certification Period Lengths Months"]["option_selection"], "4,6,12")
        bbce = self.csv(out, "bbce")
        self.assertEqual(len(bbce), 53)                       # every agency once: 44 BBCE + 9 No BBCE
        self.assertEqual(sum(x["option_selection"] == "BBCE" for x in bbce), 44)
        self.assertEqual({x["edition"] for x in self.csv(out, "bbce16")}, {"16th Edition (FY 2024)"})
        self.assertEqual(len(self.csv(out, "tel")), 12)

    def test_ignored_parameter_is_an_error(self):
        """The dashboard falls back to its default view for a value it does not know: never take those rows."""
        code, r, out = self.fetch({"requests": [{"name": "tba", "endpoint": "option_states",
                                                 "params": {"policy": "tba", "selection": "TBA"}}]})
        self.assertEqual(code, 2, r)
        self.assertIn("no longer matches the dashboard", r["message"])

    def test_missing_view_names_the_pdf_fallback(self):
        code, r, out = self.fetch({"requests": [{"name": "esap", "endpoint": "option_states",
                                                 "params": {"policy": "esap", "selection": "ESAP"}}]})
        self.assertEqual(code, 2, r)
        self.assertIn("republished", r["message"])
        self.assertIn("options_report", r["message"])

    def test_bad_selection_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "option_states",
                                            "params": {"policy": "bbce", "selection": "Maybe"}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("No BBCE", r["message"])

    def test_unparsed_year_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "activity_tables", "params": {"fiscal_year": 2015}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("activity_report", r["message"])

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_activity_tables(self):
        code, r, out = self.fetch(SAR)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "ok", r)
        rows = self.csv(out, "sar")
        self.assertEqual(list(rows[0]), ["report_fy", "table", "table_number", "table_title", "page", "state",
                                         "state_abbr", "row_type", "measure", "data_fy", "value", "value_num",
                                         "value_flag"])
        val = {(x["table"], x["state_abbr"], x["measure"]): x for x in rows}
        al = val[("households_by_benefit_type", "AL", "households_ebt")]      # printed column by column
        self.assertEqual((al["value"], al["value_num"], al["data_fy"]), ("391,662", "391662", "2023"))
        cash = val[("households_by_benefit_type", "AL", "households_cash")]
        self.assertEqual((cash["value_num"], cash["value_flag"]), ("", "N/A"))   # a code, never a zero
        fraud = val[("claims_established_dollars", "AL", "fraud_claim_dollars_established")]   # printed row by row
        self.assertEqual(fraud["value_num"], "318378")
        dc = val[("claims_established_dollars", "DC", "total_claim_dollars_established")]
        self.assertEqual((dc["value_num"], dc["value_flag"]), ("", "--"))
        self.assertEqual({x["state_abbr"] for x in rows if x["row_type"] == "state"} >= {"GU", "VI", "DC"}, True)
        self.assertEqual(len({x["state_abbr"] for x in rows if x["row_type"] == "state"}), 53)
        us = [x for x in rows if x["row_type"] == "us_total" and x["measure"] == "households_ebt"]
        self.assertEqual(us[0]["value_num"], "22267689")
        self.assertEqual({x["measure"] for x in rows if x["table"] == "admin_costs"},
                         {"state_share_admin_costs", "federal_share_admin_costs", "arpa_funding", "total_admin_costs"})

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_misaligned_table_is_dropped(self):
        """A value that pypdf loses must drop the table, never shift the values onto the wrong agencies."""
        ad = load_pack_module("usda-fna")
        with open(os.path.join(FX, "sar23_p9_p14_p30.pdf"), "rb") as f:
            page = ad.pdf_pages(f.read())[0]
        key, rows, prob = ad.parse_table_page(page, 9, 2023)
        self.assertEqual((key, len(rows) > 0, prob), ("households_by_benefit_type", True, None))
        lines = page.splitlines()
        i = next(j for j, ln in enumerate(lines) if ln.strip() == "391,662")
        key, rows, prob = ad.parse_table_page("\n".join(lines[:i] + lines[i + 1:]), 9, 2023)
        self.assertEqual((key, rows), ("households_by_benefit_type", []))
        self.assertIn("values for 53 agencies", prob)

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_options_report_state_pages(self):
        code, r, out = self.fetch({"requests": [{"name": "tx", "endpoint": "options_report",
                                                 "params": {"edition": 17, "state": "TX"}}]})
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "tx")
        self.assertEqual([x["page"] for x in rows], ["1", "2"])             # pages of the trimmed fixture
        self.assertEqual({x["state"] for x in rows}, {"Texas"})
        self.assertIn("\nBBCE BBCE", rows[0]["text"])
        self.assertIn("Pre-Release Applications from Institutions Waiver", rows[1]["text"])

    def test_manual_bundle(self):
        spec = {"requests": SPEC["requests"][:2] + SAR["requests"]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        names = [i["filename"] for i in r["items"]]
        self.assertEqual(names, ["tx_ca_01.csv", "bbce_01.csv", "bbce_02.csv", "sar_snap-sar-fy23.pdf"])
        self.assertIn("StateAgencyComparison.csv?State%201%20Param=Texas&State%202%20Param%20=California",
                      r["items"][0]["url"])
        self.assertTrue(r["items"][3]["url"].startswith("https://www.fna.usda.gov/sites/default/files/"))

    def test_ingest(self):
        spec = {"requests": SPEC["requests"][:2] + (SAR["requests"] if HAS_PYPDF else [])}
        sp, out = self.write_spec(spec)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        files = [("r001.csv", "tx_ca_01.csv"), ("r002.csv", "bbce_01.csv"), ("r003.csv", "bbce_02.csv")]
        if HAS_PYPDF:
            files.append(("sar23_p9_p14_p30.pdf", "sar_snap-sar-fy23.pdf"))
        for src, dst in files:
            shutil.copy(os.path.join(FX, src), os.path.join(up, dst))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        self.assertEqual(len(self.csv(out, "tx_ca")), 30)
        self.assertEqual(len(self.csv(out, "bbce")), 53)
        if HAS_PYPDF:
            self.assertEqual(len({x["state_abbr"] for x in self.csv(out, "sar") if x["row_type"] == "state"}), 53)

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_ingest_reads_the_year_from_the_pdf(self):
        """Uploaded report PDFs are matched to fiscal years by their content: the agency's file names do not sort by
        year, and a renamed file must not be read as another year."""
        req = {"name": "sar", "endpoint": "activity_tables", "params": {"fiscal_year": 2023, "tables": "all"}}
        sp, out = self.write_spec({"requests": [req]})
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(FX, "sar23_p9_p14_p30.pdf"), os.path.join(up, "sar_FY19-state-activity-report.pdf"))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual({x["report_fy"] for x in self.csv(out, "sar")}, {"2023"})
        sp, out = self.write_spec({"requests": [{**req, "params": {"fiscal_year": 2022}}]}, "spec22.json")
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 2, r)
        self.assertIn("FY [2022]", r["message"])

    def test_blocked_network_hands_off(self):
        """A host the sandbox cannot reach mid-fetch returns the standard handoff route, not an error."""
        sp, out = self.write_spec(SPEC)
        code, r = run(["--source", self.source, sp, "--out", out, "--env", "sandbox"], None,
                      {"DATA_ACCESS_FORCE": "restricted"}, self.home)
        self.assertEqual(r.get("route"), "manual_download", r)


if __name__ == "__main__":
    unittest.main()

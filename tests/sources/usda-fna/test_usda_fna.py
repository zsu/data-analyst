"""Offline tests for the usda-fna pack.

Fixtures r001-r005.csv were recorded live on 2026-10-30 with DATA_ANALYST_RECORD (State Options dashboard CSV
exports: Texas and California policy options; BBCE and No BBCE, 17th edition; BBCE, 16th edition; the telephonic
signature waiver). The PDFs are 1.4-5.9 MB, so the PDF fixtures are real pages copied out of them with pypdf:
  sar23_p9_p14_p30.pdf  SNAP State Activity Report FY 2023, pages 9 (Table 5, printed column by column),
                        14 (Table 8) and 30 (Table 18, printed row by row)
  sor17_p132_p134.pdf   State Options Report 17th edition, pages 132-134 (Texas's two profile pages, Utah's first)
Two index entries are not responses to their own URL, to test the gates: the TBA request replays the BBCE export
(the dashboard ignoring a parameter), and the ESAP request answers HTTP 404 (a republished dashboard).
Program Data (recorded live 2026-10-30/31): the three listing pages (pd_*.html) and the workbooks snap-persons-10,
26wifypart-10, 27wilatest-10 (its "June 2025" column holds June 2026), 01slfypart-10, cncost-10 and 36slmonthly-10
(hidden columns 9-10 hold numbers), all unchanged; 27wilatest-9 (the September 2026 release, downloaded 2026-10-31,
not in index.json) prints "Data as of September 11, 2025" over June 2026 data. Two are trimmed copies of larger real files:
  snap-zip-fy69tocurrent-10_FY25only.zip  the SNAP data zip (1.6 MB) with only its FY25.xlsx member
  wicagencies2025ytd_3sheets.xlsx         wicagencies2025ytd.xlsx (225 KB) with three of its sixteen sheets (Children,
                                          Total Number of Participants, NSA costs), the sheet XML unchanged
"""
import importlib.util
import io
import os
import re
import shutil
import sys
import unittest
import zipfile

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


PD = {"description": "FNA Program Data: SNAP persons (latest months), WIC and school lunch participation by state, "
                     "school meal costs, NSLP national monthly",
      "requests": [
          {"name": "snap", "endpoint": "program_table", "params": {"table": "snap_persons_latest", "state": ["TX", "CA"]}},
          {"name": "wic", "endpoint": "program_table", "params": {"table": "wic_participation_state", "state": "AZ"}},
          {"name": "nslp", "endpoint": "program_table",
           "params": {"table": "nslp_participation_state", "state": "TX", "fiscal_year": [2024, 2025]}},
          {"name": "costs", "endpoint": "program_table", "params": {"table": "school_meals_costs", "fiscal_year": 2025}},
          {"name": "nslp_monthly", "endpoint": "program_table", "params": {"table": "nslp_monthly", "fiscal_year": 2025}}]}
PD_COLS = ["table", "table_title", "data_as_of", "state", "state_abbr", "agency", "row_type", "period", "period_type",
           "fiscal_year", "measure", "unit", "value", "value_num", "value_flag", "status", "footnote"]


def _rezip(path: str, edit) -> bytes:
    """The fixture workbook with edit(member name, bytes) -> bytes applied to each member."""
    out = io.BytesIO()
    with zipfile.ZipFile(path) as z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
        for n in z.namelist():
            o.writestr(n, edit(n, z.read(n)))
    return out.getvalue()


class TestUsdaFnaProgramData(PackTest):
    source = "usda-fna"
    sample_spec = PD

    def test_check_api(self):
        code, r = self.check(PD)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_program_tables(self):
        code, r, out = self.fetch(PD)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "ok", r)
        snap = self.csv(out, "snap")
        self.assertEqual(list(snap[0]), PD_COLS)
        v = {(x["state_abbr"], x["period"]): x for x in snap}
        self.assertEqual(len(snap), 6)                                    # 2 states x 3 months
        self.assertEqual((v[("CA", "2025-07")]["value_num"], v[("CA", "2025-07")]["data_as_of"]),
                         ("5449432", "2026-10-09"))
        self.assertEqual(v[("CA", "2026-06")]["status"], "preliminary")
        self.assertEqual(v[("TX", "2026-07")]["status"], "initial")
        wic = {(x["agency"], x["period"]): x for x in self.csv(out, "wic")}
        self.assertEqual(wic[("", "FY 2025")]["value_num"], "157532.9167")         # the state total, ITOs included
        self.assertEqual(wic[("", "FY 2025")]["row_type"], "state")
        self.assertEqual(wic[("Navajo Nation", "FY 2025")]["row_type"], "sub_agency")
        self.assertEqual({x["fiscal_year"] for x in self.csv(out, "nslp")}, {"2024", "2025"})
        costs = {x["measure"]: x for x in self.csv(out, "costs")}
        self.assertEqual(costs["cash_nslp"]["value_num"], "17236.12265209")
        self.assertEqual(costs["cash_nslp"]["unit"], "millions of dollars")
        self.assertEqual((costs["commodities_bonus"]["value_num"], costs["commodities_bonus"]["value_flag"]),
                         ("", "blank"))
        mon = self.csv(out, "nslp_monthly")
        self.assertEqual({x["measure"] for x in mon}, {"participation", "lunches_served", "share_free",
                                                       "share_reduced_price", "snacks_served", "cash_payments",
                                                       "commodity_costs"})              # hidden columns 9-10 skipped
        self.assertEqual(len([x for x in mon if x["period_type"] == "month"]), 12 * 7)
        oct_ = next(x for x in mon if x["period"] == "2024-10" and x["measure"] == "share_free")
        self.assertEqual(oct_["unit"], "fraction of lunches (0-1)")

    def test_mislabeled_month_column_is_dropped(self):
        """27wilatest labels June 2026 "June 2025": the column is dropped and named, never relabeled."""
        code, r, out = self.fetch({"requests": [{"name": "wl", "endpoint": "program_table",
                                                 "params": {"table": "wic_participation_latest", "state": "AL"}}]})
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "partial", r)
        self.assertIn("June 2025", " ".join(r["truncated"]))
        self.assertEqual(sorted(x["period"] for x in self.csv(out, "wl")), ["2025-07", "2026-07"])
        self.assertFalse([n for n in r["notes"] if "Data as of" in n], r["notes"])     # -10: October 9, 2026

    def test_asof_before_latest_data_is_noted(self):
        """27wilatest-9 says "Data as of September 11, 2025" over June 2026: noted, kept as printed, never fixed."""
        ad = load_pack_module("usda-fna")
        k, c = ad._table({"table": "wic_participation_latest"})
        with open(os.path.join(FX, "27wilatest-9.xlsx"), "rb") as f:
            rows, notes = ad.program_rows(k, c, f.read(), {})
        self.assertEqual({x["data_as_of"] for x in rows}, {"2025-09-11"})
        hit = [n for n in notes if "Data as of" in n]
        self.assertEqual(len(hit), 1, notes)
        self.assertIn("2025-09-11, before its own latest data (2026-06)", hit[0])
        # the same date a year later (as FNA meant it) passes
        fixed = _rezip(os.path.join(FX, "27wilatest-9.xlsx"),
                       lambda n, b: b.replace(b"September 11, 2025", b"September 11, 2026"))
        self.assertFalse([n for n in ad.program_rows(k, c, fixed, {})[1] if "Data as of" in n])

    def test_layout_gates(self):
        ad = load_pack_module("usda-fna")
        k, c = ad._table({"table": "nslp_participation_state"})
        with open(os.path.join(FX, "01slfypart-10.xlsx"), "rb") as f:
            good = f.read()
        self.assertEqual(len(ad.program_rows(k, c, good, {})[0]), 305)
        bad = _rezip(os.path.join(FX, "01slfypart-10.xlsx"),
                     lambda n, b: b.replace(b"FY 2023", b"FY 2033") if n == "xl/sharedStrings.xml" else b)
        with self.assertRaises(ad.DataError) as e:
            ad.program_rows(k, c, bad, {})
        self.assertIn("expected layout", str(e.exception))
        # the hidden columns of 36slmonthly made visible: values under no label fail the table
        k, c = ad._table({"table": "nslp_monthly"})
        shown = _rezip(os.path.join(FX, "36slmonthly-10.xlsx"),
                       lambda n, b: re.sub(rb' hidden="1"', b"", b) if n.startswith("xl/worksheets/") else b)
        with self.assertRaises(ad.DataError) as e:
            ad.program_rows(k, c, shown, {})
        self.assertIn("unlabeled column", str(e.exception))
        # a workbook that is another table is refused by its title
        with self.assertRaises(ad.DataError):
            ad.program_rows(*ad._table({"table": "wic_participation_state"}), good, {})

    def test_snap_state_monthly(self):
        code, r, out = self.fetch({"requests": [{"name": "sm", "endpoint": "snap_state_monthly",
                                                 "params": {"fiscal_year": 2025}}]})
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "ok", r)
        self.assertFalse([n for n in r["notes"] if "check:" in n], r["notes"])          # states = regions = U.S.
        rows = self.csv(out, "sm")
        tx = {(x["period"], x["measure"]): x for x in rows if x["state_abbr"] == "TX"}
        self.assertEqual(tx[("FY 2025", "persons")]["value_num"], "3472443.0833")
        self.assertEqual(tx[("2025-07", "benefits")]["value_num"], "625221101")
        self.assertEqual(len([k for k in tx if k[1] == "persons"]), 13)                 # 12 months + the FY
        self.assertEqual({x["row_type"] for x in rows}, {"state", "region", "national"})
        code, r = self.check({"requests": [{"name": "x", "endpoint": "snap_state_monthly",
                                            "params": {"fiscal_year": 2019}}]})
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn(".xls", r["message"])

    def test_wic_agency_monthly(self):
        code, r, out = self.fetch({"requests": [{"name": "wa", "endpoint": "wic_agency_monthly",
                                                 "params": {"fiscal_year": 2025, "state": "AZ",
                                                            "measures": ["participants_total", "nsa_costs"]}}]})
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "ok", r)
        rows = self.csv(out, "wa")
        tot = {(x["agency"], x["row_type"]): x for x in rows if x["measure"] == "participants_total"
               and x["period"] == "FY 2025"}
        self.assertEqual(round(float(tot[("", "state_agency")]["value_num"])), 146899)   # the state agency only
        self.assertIn(("Navajo Nation", "tribal_org"), tot)
        self.assertEqual({x["period_type"] for x in rows if x["measure"] == "nsa_costs"}, {"fiscal_year"})
        # a sheet missing from the workbook makes the table incomplete
        code, r, out = self.fetch({"requests": [{"name": "wf", "endpoint": "wic_agency_monthly",
                                                 "params": {"fiscal_year": 2025, "measures": ["food_costs"]}}]})
        self.assertEqual(r["status"], "partial", r)
        self.assertIn("food_costs", " ".join(r["truncated"]))

    def test_program_spec_checks(self):
        for params, words in [({"table": "nslp_annual", "state": "TX"}, "national table"),
                              ({"table": "no_such_table"}, "not a Program Data table")]:
            code, r = self.check({"requests": [{"name": "x", "endpoint": "program_table", "params": params}]})
            self.assertEqual(r["route"], "fix_spec", r)
            self.assertIn(words, r["message"])
        code, r = self.check({"requests": [{"name": "x", "endpoint": "option_states",
                                            "params": {"policy": "nslp_annual"}}]})
        self.assertEqual(r["route"], "fix_spec", r)                    # a table key is not a State Options policy

    def test_manual_bundle_and_ingest(self):
        spec = {"requests": PD["requests"][:2] + [
            {"name": "sm", "endpoint": "snap_state_monthly", "params": {"fiscal_year": 2025, "state": "TX"}},
            {"name": "wa", "endpoint": "wic_agency_monthly", "params": {"fiscal_year": 2025, "state": "AZ"}}]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        names = [i["filename"] for i in r["items"]]
        self.assertEqual(names, ["snap_snap-persons.xlsx", "wic_26wifypart.xlsx", "sm_snap-zip-fy69tocurrent.zip",
                                 "wa_wicagencies2025ytd.xlsx"])
        self.assertTrue(r["items"][0]["url"].endswith("/resource-files/snap-persons-10.xlsx"))
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        # saved under the bundle's names; parse_upload still checks each file's content (title, fiscal year)
        for src, dst in zip(["snap-persons-10.xlsx", "26wifypart-10.xlsx", "snap-zip-fy69tocurrent-10_FY25only.zip",
                             "wicagencies2025ytd_3sheets.xlsx"], names):
            shutil.copy(os.path.join(FX, src), os.path.join(up, dst))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(len(self.csv(out, "snap")), 6)
        self.assertEqual(len(self.csv(out, "wic")), 20)
        self.assertEqual({x["state_abbr"] for x in self.csv(out, "sm")}, {"TX"})
        self.assertIn("Navajo Nation", {x["agency"] for x in self.csv(out, "wa")})

    def test_incomplete_marker_matches_core(self):
        from core import generic_rest as gr
        self.assertEqual(load_pack_module("usda-fna").pdx.INCOMPLETE, gr.INCOMPLETE)


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from data.epa.gov/dmapservice and echodata.epa.gov on 2026-10-20
# (ECHO: get_facilities / get_case_info, then get_qid with the QueryID they returned).
SPEC = {"description": "Vermont: GHGRP emitters 2022, NPL Superfund sites, major air facilities, CAA cases",
        "requests": [
            {"name": "vt_ghg_2022", "endpoint": "envirofacts",
             "params": {"table": "ghg_emitters", "filters": [["state", "equals", "VT"], ["year", "equals", 2022]]},
             "max_rows": 3},
            {"name": "vt_npl", "endpoint": "envirofacts",
             "params": {"table": "superfund_sites", "filters": {"fk_ref_state_code": "VT", "npl_status_code": "F"}},
             "max_rows": 3},
            {"name": "vt_major_air", "endpoint": "echo_facilities", "params": {"program": "air", "p_st": "VT", "p_maj": "Y"}},
            {"name": "vt_caa_cases", "endpoint": "echo_cases", "params": {"p_state": "VT", "p_law": "CAA"}, "max_rows": 3}]}


class TestEpa(PackTest):
    source = "epa"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        ghg = self.csv(out, "vt_ghg_2022")
        self.assertEqual(ghg[0]["facility_name"], "Omya Inc. (Verpol)")
        self.assertEqual(ghg[0]["co2e_emission"], "16.688")
        npl = self.csv(out, "vt_npl")
        self.assertEqual(npl[0]["name"], "PINE STREET CANAL")
        self.assertEqual(npl[0]["npl_status_code"], "F")
        air = self.csv(out, "vt_major_air")
        self.assertEqual(len(air), 17)
        self.assertTrue(all(row["AIRState"] == "VT" for row in air))
        cases = self.csv(out, "vt_caa_cases")
        self.assertEqual(cases[0]["CaseNumber"], "01-1991-0008")
        self.assertEqual(cases[0]["FedPenalty"], "$40,000.00")        # published text kept
        self.assertEqual(cases[0]["FedPenalty_usd"], "40000.00")      # added column
        self.assertEqual(cases[0]["DateFiled_iso"], "1991-05-01")
        self.assertTrue(any("ECHO matched 99 rows" in n for n in r["notes"]), r["notes"])

    def test_cases_reject_p_st(self):
        bad = {"requests": [{"name": "x", "endpoint": "echo_cases", "params": {"p_st": "VT"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("p_state", r["message"])

    def test_params_echo_ignores_are_refused(self):
        """ECHO accepts these without filtering (live 2026-10): each would return unfiltered rows."""
        cases = [("echo_cases", {"p_state": "KS", "p_fy": "2019"}, "p_milestone_fy"),
                 ("echo_cases", {"p_state": "KS", "p_crim": "Y"}, "p_civil_criminal_indicator"),
                 ("echo_cases", {"p_state": "KS", "p_name": "ACME"}, "CaseName"),
                 ("echo_facilities", {"program": "rcra", "p_st": "KS", "p_naics": "3251"}, "p_ncs"),
                 ("echo_facilities", {"program": "air", "p_st": "KS", "p_co": "Sedgwick"}, "p_fips")]
        for ep, params, hint in cases:
            code, r = self.check({"requests": [{"name": "x", "endpoint": ep, "params": params}]})
            self.assertEqual(r["route"], "fix_spec", (ep, params, r))
            self.assertIn(hint, r["message"])
        ok = {"requests": [{"name": "x", "endpoint": "echo_facilities",
                            "params": {"program": "cwa", "p_st": "KS", "p_co": "Sedgwick", "p_ncs": "3251"}}]}
        self.assertEqual(self.check(ok)[1]["route"], "api")
        bad_enum = {"requests": [{"name": "x", "endpoint": "echo_cases", "params": {"p_state": "KS", "p_fed_penalty": "100000"}}]}
        self.assertEqual(self.check(bad_enum)[1]["route"], "fix_spec")

    def test_bad_operator_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "envirofacts",
                             "params": {"table": "ghg_emitters", "filters": [["state", "eq", "VT"]]}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(items[0]["url"],
                         "https://data.epa.gov/dmapservice/ghg.ghg_emitter_sector/state/equals/VT/year/equals/2022/1:5000/CSV")
        self.assertTrue(items[2]["url"].startswith("https://echodata.epa.gov/echo/air_rest_services.get_facilities?"))
        self.assertIn("get_qid", items[2]["note"])

    def test_echo_error_in_200(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("epa_adapter_t", os.path.join(self.pack_dir, "adapter.py"))
        ad = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ad)
        body = b'{"Results":{"Error":{"ErrorMessage":"Queryset Limit would be exceeded"}}}'
        self.assertIn("Queryset Limit", ad.check_error(200, body))


if __name__ == "__main__":
    unittest.main()

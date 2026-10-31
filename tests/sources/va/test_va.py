import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from www.data.va.gov on 2026-10-30.
SPEC = {"description": "Texas veterans by sex, VetPop2023, FY2023-FY2026",
        "requests": [{"name": "tx_all_veterans", "endpoint": "query",
                      "params": {"dataset": "vetpop_state_pos",
                                 "$where": "state = 'Texas' AND pos = '(a) All Veterans (b+c)' "
                                           "AND date <= '2026-09-30'",
                                 "$order": "date, sex"}}]}

STATE_TOTALS = {"description": "Veterans by state, VetPop2023 projection for 2025-09-30",
                "requests": [{"name": "veterans_by_state_2025", "endpoint": "query",
                              "params": {"dataset": "vetpop_state", "$select": "state, sum(veterans) AS veterans",
                                         "$where": "date = '2025-09-30'", "$group": "state", "$order": "state"}}]}

BENEFITS = {"description": "Pension recipients FY2025 and compensation recipients FY2025 in Vermont",
            "requests": [{"name": "pension_fy2025", "endpoint": "query",
                          "params": {"dataset": "pension_recipients_state_fy2025", "$order": "state"}},
                         {"name": "vt_comp_counties", "endpoint": "query",
                          "params": {"dataset": "comp_recipients_county_fy2025", "$where": "state = 'Vermont'",
                                     "$order": "fips_code"}}]}

GDX = {"description": "VA expenditures by state, GDX FY2025",
       "requests": [{"name": "gdx_direct_fy25", "endpoint": "gdx_state_direct", "params": {"fy": "25"}},
                    {"name": "gdx_cd_fy25", "endpoint": "gdx_cd_direct"}]}


class TestVa(PackTest):
    source = "va"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch_state_period_of_service(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "tx_all_veterans")
        self.assertEqual(list(rows[0]), ["date", "sex", "state", "pos", "veterans"])
        self.assertEqual(len(rows), 8)                      # 4 fiscal years x 2 sexes, no total rows
        self.assertEqual({x["sex"] for x in rows}, {"Female", "Male"})
        m25 = [x for x in rows if x["date"].startswith("2025-09-30") and x["sex"] == "Male"]
        self.assertEqual(m25[0]["veterans"], "1308094")
        self.assertTrue(os.path.exists(os.path.join(out, "data", "va", "README.md")))

    def test_soql_group_state_totals(self):
        # $group without an explicit default order; 53 geographies (50 states, DC, PR, Island Areas & Foreign)
        code, r, out = self.fetch(STATE_TOTALS)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "veterans_by_state_2025")
        self.assertEqual(len(rows), 53)
        by = {x["state"]: x["veterans"] for x in rows}
        self.assertEqual(by["Texas"], "1518969")
        self.assertIn("Island Areas & Foreign", by)

    def test_benefit_tables_keep_suppressed_blanks(self):
        code, r, out = self.fetch(BENEFITS)
        self.assertEqual(code, 0, r)
        pen = {x["state"]: x for x in self.csv(out, "pension_fy2025")}
        self.assertEqual(pen["Alabama"]["total_pension_recipients"], "3229")
        self.assertEqual(pen["Alaska"]["male"], "")              # suppressed, not zero
        self.assertIn("Other Foreign Countries", pen)
        vt = self.csv(out, "vt_comp_counties")
        self.assertEqual(vt[0]["fips_code"], "50001")
        self.assertEqual(vt[0]["total_disability_compensation"], "423")

    def test_catalog_aliases_resolve(self):
        sys.path.insert(0, os.path.join(self.pack_dir, "..", "..", "scripts"))
        from core import packfiles
        cat = packfiles.read_json(self.pack_dir, "endpoints.json")["catalog"]
        self.assertEqual(cat["vetpop_state_pos"]["id"], "6kr5-skza")
        self.assertEqual(cat["vetpop_state"]["id"], "f43c-vb9n")
        self.assertEqual(cat["comp_recipients_county_fy2025"]["id"], "95an-zhhy")
        # the alias and the 4x4 id fetch the same recorded request
        spec = json.loads(json.dumps(SPEC))
        spec["requests"][0]["params"]["dataset"] = "6kr5-skza"
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        self.assertEqual(len(self.csv(out, "tx_all_veterans")), 8)
        code, r = self.cli("--search", "pension recipients")
        self.assertIn("pension_recipients_state_fy2025", json.dumps(r))

    def test_gdx_check_and_manual_link(self):
        code, r = self.check(GDX)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        sp, out = self.write_spec(GDX)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(r["instructions"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("https://www.va.gov/vetdata/docs/GDX/GDX_FY25.xlsx", text)

    def test_manual_bundle_csv_links(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(r["instructions"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("https://www.data.va.gov/resource/6kr5-skza.json", text)

    def test_reuse_existing(self):
        code, r, out = self.fetch(SPEC)
        code, r = self.cli(os.path.join(out, "spec.json"), "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing", r)

    def test_unknown_endpoint_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "nope", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)


if __name__ == "__main__":
    unittest.main()

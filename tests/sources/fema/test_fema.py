import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from www.fema.gov/api/open on 2026-10-20 (max_rows 3 keeps them small).
SPEC = {"description": "Vermont FY2023 major disaster declarations; Vermont NFIP claims from the July 2023 flood",
        "requests": [
            {"name": "vt_dr_fy2023", "endpoint": "disaster_declarations",
             "params": {"$filter": "state eq 'VT' and fyDeclared eq 2023 and declarationType eq 'DR'", "$orderby": "id"},
             "max_rows": 3},
            {"name": "vt_claims_2023", "endpoint": "nfip_claims",
             "params": {"$filter": "state eq 'VT' and yearOfLoss eq 2023",
                        "$select": "id,dateOfLoss,yearOfLoss,floodEvent,state,countyCode,ratedFloodZone,"
                                   "netBuildingPaymentAmount,netContentsPaymentAmount"},
             "max_rows": 3},
            {"name": "regions_csv", "endpoint": "query",
             "params": {"version": "v2", "entity": "FemaRegions", "$select": "region,name,city,state"}, "max_rows": 3}]}


class TestFema(PackTest):
    source = "fema"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        dr = self.csv(out, "vt_dr_fy2023")
        self.assertEqual(len(dr), 3)
        self.assertEqual(dr[0]["femaDeclarationString"], "DR-4720-VT")
        self.assertEqual(dr[0]["designatedArea"], "Addison (County)")
        claims = self.csv(out, "vt_claims_2023")
        self.assertEqual(claims[1]["floodEvent"], "Vermont/New York Flooding")
        self.assertEqual(claims[1]["netBuildingPaymentAmount"], "95493.9")
        regions = self.csv(out, "regions_csv")
        self.assertEqual(regions[0]["city"], "Boston")
        self.assertIn("not endorsed by FEMA", open(os.path.join(out, "data", "fema", "README.md"), encoding="utf-8").read())

    def test_query_needs_entity(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "query", "params": {"version": "v2"}}]})
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle_pages(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(items[0]["url"].startswith("https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries?"))
        self.assertIn("%24top=10000", items[0]["url"])


if __name__ == "__main__":
    unittest.main()

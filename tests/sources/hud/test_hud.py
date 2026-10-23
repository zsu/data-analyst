"""Offline tests for the hud pack.

No HUD_USER_TOKEN was available, so the fixtures are HUD's documented example responses
(https://www.huduser.gov/portal/dataset/fmr-api.html, read 2026-10-20), saved as doc_*.json with the
elided items left out. They are not live recordings. A placeholder token is passed only so the client
takes the keyed route; fixtures are matched by URL and the token never reaches a file.
"""
import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

TOKEN = {"HUD_USER_TOKEN": "placeholder-not-a-real-token"}
SPEC = {"description": "FMRs for Wallingford town VT (FY2017) and the Washington DC metro; Autauga County AL income limits; Massachusetts FMRs; Virginia counties",
        "requests": [
            {"name": "wallingford_2017", "endpoint": "fmr", "params": {"entityid": "5002175925", "year": 2017}},
            {"name": "dc_metro", "endpoint": "fmr", "params": {"entityid": "METRO47900M47900"}},
            {"name": "autauga_il", "endpoint": "income_limits", "params": {"entityid": "0100199999"}},
            {"name": "ma_fmr", "endpoint": "fmr_state", "params": {"statecode": "ma"}},
            {"name": "va_counties", "endpoint": "list_counties", "params": {"statecode": "VA"}}]}


class TestHud(PackTest):
    source = "hud"
    sample_spec = SPEC

    def test_check_without_token_asks_for_setup(self):
        code, r = self.check(SPEC, env={"DATA_ANALYST_ENV": "local"})
        self.assertEqual(r["route"], "run_setup", r)
        self.assertEqual(r["key_env"], "HUD_USER_TOKEN")

    def test_check_with_token_is_api(self):
        code, r = self.check(SPEC, env=TOKEN)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)

    def test_fetch_doc_examples(self):
        code, r, out = self.fetch(SPEC, env=TOKEN)
        self.assertEqual(code, 0, r)
        w = self.csv(out, "wallingford_2017")
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["town_name"], w[0]["year"], w[0]["Two-Bedroom"]), ("Wallingford town", "2017", "948.0"))
        dc = self.csv(out, "dc_metro")
        self.assertEqual([x["zip_code"] for x in dc], ["MSA level", "20001", "20002", "20003"])
        self.assertEqual(dc[0]["Two-Bedroom"], "2045")
        il = self.csv(out, "autauga_il")
        self.assertEqual((il[0]["median_income"], il[0]["il50_p4"], il[0]["il30_p1"], il[0]["il80_p8"]),
                         ("65900", "32950", "13850", "69600"))
        ma = self.csv(out, "ma_fmr")
        self.assertEqual([x["area_type"] for x in ma], ["metro", "metro", "county", "county"])
        self.assertEqual(ma[2]["fips_code"], "2502300170")
        va = self.csv(out, "va_counties")
        self.assertEqual(va[0]["fips_code"], "5100199999")
        with open(os.path.join(out, "data", "hud", "README.md"), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("placeholder-not-a-real-token", raw)

    def test_five_digit_fips_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "fmr", "params": {"entityid": "51001"}}]}
        code, r = self.check(bad, env=TOKEN)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_items_need_token_header(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(all(i["needs_key"] and i["key_header"] == "Authorization" and i["key_prefix"] == "Bearer "
                            for i in items))
        self.assertIn("/fmr/statedata/MA", [i["url"].split("public")[1] for i in items][3])

    def test_ingest_published_fmr_workbooks(self):
        """Without a token the user uploads HUD's published FMR workbooks (source.md, Access). Columns follow
        FY22_FMRs_revised.xlsx and FY27_FMRs.xlsx (read 2026-10-22);
        FY26_FMRs_revised.xlsx carries a malformed docProps/core.xml date that openpyxl refuses."""
        import tempfile
        import zipfile
        import pandas as pd
        spec = {"description": "One metro's FMRs from the published workbooks",
                "requests": [{"name": "metro_fy2022", "endpoint": "fmr", "params": {"entityid": "METRO19740M19740", "year": 2022}},
                             {"name": "metro_fy2027", "endpoint": "fmr", "params": {"entityid": "METRO19740M19740", "year": 2027}}]}
        fy22 = pd.DataFrame({"fips2010": ["0800199999", "0800599999", "0100199999"], "fmr_2": ["1530", "1530", "786"],
                             "metro_code": ["METRO19740M19740", "METRO19740M19740", "METRO33860M33860"],
                             "areaname": ["Denver-Aurora-Lakewood, CO HUD Metro FMR Area"] * 2 + ["Montgomery, AL MSA"]})
        fy27 = pd.DataFrame({"stusps": ["CO", "CO", "AL"], "hud_area_code": fy22["metro_code"], "fips": fy22["fips2010"],
                             "fmr_2": ["1915", "1915", "1021"]})
        with tempfile.TemporaryDirectory() as up:
            fy22.to_excel(os.path.join(up, "metro_fy2022.xlsx"), index=False)
            src = os.path.join(up, "tmp.xlsx")
            fy27.to_excel(src, index=False)
            with zipfile.ZipFile(src) as zin, zipfile.ZipFile(os.path.join(up, "metro_fy2027.xlsx"), "w") as zout:
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    if info.filename == "docProps/core.xml":       # HUD's malformed date, as in FY26_FMRs_revised.xlsx
                        head = data.split(b"<dcterms:created")[0]
                        data = head + (b'<dcterms:modified xsi:type="dcterms:W3CDTF">2026- 2-19T18:17:31Z'
                                       b"</dcterms:modified></cp:coreProperties>")
                    zout.writestr(info, data)
            os.remove(src)
            with self.assertRaises(ValueError):                    # what openpyxl does with it
                pd.read_excel(os.path.join(up, "metro_fy2027.xlsx"))
            sp, out = self.write_spec(spec)
            code, r = self.cli(sp, "--out", out, "--ingest", up)
            self.assertEqual(code, 0, r)
            a22, a27 = self.csv(out, "metro_fy2022"), self.csv(out, "metro_fy2027")
            self.assertEqual([(x["fips2010"], x["fmr_2"]) for x in a22], [("0800199999", "1530"), ("0800599999", "1530")])
            self.assertEqual({x["fmr_2"] for x in a27}, {"1915"})
            # a workbook with no row for the requested entity is refused, not passed through
            bad = {**spec, "requests": [{**spec["requests"][0], "params": {"entityid": "METRO99999M99999", "year": 2022}}]}
            sp2, out2 = self.write_spec(bad, "spec2.json")
            code, r = self.cli(sp2, "--out", out2, "--ingest", os.path.join(up, "metro_fy2022.xlsx"))
            self.assertNotEqual(code, 0, r)
            self.assertIn("no row for entity id METRO99999M99999", json.dumps(r))


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from www.nationsreportcard.gov (NAEP Data Service 6.4) on 2026-10-20.
SPEC = {"description": "NAEP grade 4 reading: national 2024 vs 2019 with significance test; grade 4 reading by race in Wyoming 2024; math grade 8 states",
        "requests": [{"name": "read4_np_2019_2024", "endpoint": "scores",
                      "params": {"subject": "reading", "grade": 4, "jurisdiction": "NP", "Year": "2024,2019"}},
                     {"name": "read4_change", "endpoint": "compare_years",
                      "params": {"subject": "reading", "grade": 4, "Year": "2024,2019"}},
                     {"name": "read4_wy_race", "endpoint": "scores",
                      "params": {"subject": "reading", "grade": 4, "jurisdiction": "WY", "variable": "SDRACE", "Year": "2024"}},
                     {"name": "math8_states", "endpoint": "compare_jurisdictions",
                      "params": {"subject": "math", "grade": 8, "jurisdiction": "NP,MA,MS", "Year": "2024"}},
                     {"name": "read4_levels", "endpoint": "achievement_levels",
                      "params": {"subject": "reading", "grade": 4, "Year": "2024"}}]}


class TestNaep(PackTest):
    source = "naep"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = {x["year"]: x for x in self.csv(out, "read4_np_2019_2024")}
        self.assertEqual(rows["2024"]["value"], "214.274883563924")
        self.assertEqual(rows["2024"]["stdError"], "0.364153420531045")      # QCData=true gives SEs
        chg = self.csv(out, "read4_change")
        self.assertEqual({x["sig"] for x in chg}, {"HIGHER", "LOWER"})
        wy = {x["varValueLabel"]: x for x in self.csv(out, "read4_wy_race")}
        self.assertEqual(wy["Black"]["value"], "999")                          # published sentinel kept
        self.assertEqual(wy["Black"]["value_flag"], "suppressed")
        self.assertIn("Reporting standard not met", wy["Black"]["errorFlag_notes"])
        self.assertEqual(wy["White"]["value_flag"], "")
        st = self.csv(out, "math8_states")
        self.assertEqual(st[0]["subject"], "MAT")                              # alias math -> mathematics
        self.assertEqual(len(self.csv(out, "read4_levels")), 4)

    def test_compare_years_needs_two_years(self):
        bad = {"requests": [{"name": "x", "endpoint": "compare_years",
                             "params": {"subject": "reading", "grade": 4, "Year": "2024"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_bad_grade_for_subject(self):
        bad = {"requests": [{"name": "x", "endpoint": "scores", "params": {"subject": "tel", "grade": 4}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_variables_malformed_json_is_repaired(self):
        # type=independentvariables for 2024 sends unescaped quotes inside longLabel. Fixture r006 recorded
        # live 2026-10-22 (grade 8 reading; the raw malformed body is kept in the fixture).
        spec = {"description": "Independent variables for grade 8 reading 2024",
                "requests": [{"name": "read8_vars_2024", "endpoint": "variables",
                              "params": {"subject": "reading", "cohort": 2, "Year": "2024"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "read8_vars_2024")
        self.assertEqual(len(rows), 407)
        self.assertTrue(any(x["varName"] == "SCHTYPE" for x in rows))
        self.assertTrue(any('"' in x["longLabel"] for x in rows))           # the quoted survey item is kept
        self.assertTrue(any("malformed JSON" in n for n in r["notes"]), r["notes"])
        code, r, out = self.fetch(SPEC)                                    # valid bodies: no note
        self.assertFalse(any("malformed" in n for n in r["notes"]), r["notes"])

    def test_all_withheld_year_is_noted(self):
        # live 2026-10-28: a variable NAEP no longer reports returns status ok with every 2024 cell 999 and
        # error flag 288 (non-qualified independent variable + SE not applicable)
        from testkit import load_pack_module
        ad = load_pack_module("naep")

        def row(year, value, flag):
            return {"year": year, "value": value, "errorFlag": flag}
        rows = ad.normalize({}, {}, [row(2022, 210.5, 0), row(2022, 230.1, 0), row(2024, 999, 288), row(2024, 999, 288)])
        note = ad.withheld_note({"name": "g", "params": {"variable": "SLUNCH3"}}, rows)
        self.assertIn("every estimate for 2024 is withheld", note)
        self.assertIn("Non-qualified independent variable", note)
        self.assertIn("use ECONDIS", note)
        self.assertIsNone(ad.withheld_note({"name": "g", "params": {}}, rows[:2]))
        some = ad.normalize({}, {}, [row(2024, 999, 1), row(2024, 215.0, 0)])     # one small cell: no note
        self.assertIsNone(ad.withheld_note({"name": "g", "params": {}}, some))

    def test_repair_quotes_unit(self):
        import tempfile
        from testkit import load_pack_module
        from core import packs
        ad = load_pack_module("naep")
        bad = ('{"result": [{"variables": [{"varName": "Q7", "shortLabel": "Agree: "Science is useful" (student)", '
               '"longLabel": "Rate: "very" or "not at all"."}, {"varName": "Q8", "shortLabel": "n", '
               '"longLabel": "Has a \\"proper\\" escape"}]}], "n": [1, -2]}').encode()
        data, fixed = ad.loads_tolerant(bad)
        self.assertTrue(fixed)
        v = data["result"][0]["variables"]
        self.assertEqual(v[0]["shortLabel"], 'Agree: "Science is useful" (student)')
        self.assertEqual(v[0]["longLabel"], 'Rate: "very" or "not at all".')
        self.assertEqual(v[1]["longLabel"], 'Has a "proper" escape')
        self.assertEqual(data["n"], [1, -2])
        self.assertEqual(ad.loads_tolerant(b'{"a": "b \\"c\\""}'), ({"a": 'b "c"'}, False))   # valid: untouched
        with self.assertRaises(ValueError):
            ad.loads_tolerant(b'{"a": [1, 2}')                             # other faults still fail
        with tempfile.TemporaryDirectory() as d:                           # manual route: same repair
            p = os.path.join(d, "vars.json")
            with open(p, "wb") as f:
                f.write(bad)
            req = {"name": "vars", "endpoint": "variables", "params": {"subject": "science", "cohort": 1, "Year": "2024"}}
            rows = ad.parse_upload(packs.load("naep"), req, [p])
        self.assertEqual([x["varName"] for x in rows], ["Q7", "Q8"])

    def test_manual_links_carry_subscale(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertTrue(all("subscale=" in u and "QCData=true" in u for u in urls), urls)
        self.assertTrue(any("subject=mathematics" in u and "subscale=MRPCM" in u for u in urls))


if __name__ == "__main__":
    unittest.main()

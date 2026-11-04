import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
import testkit  # noqa: E402
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from hts.usitc.gov/reststop (keyless) on 2026-11-04, HTS 2026 Revision 22.
SPEC = {"description": "HTS revision, steel flat-rolled heading 7208, lithium-ion lines, dairy heading 0401",
        "requests": [
            {"name": "release", "endpoint": "current_release", "params": {}},
            {"name": "steel", "endpoint": "schedule", "params": {"from": "7208", "to": "7208"}},
            {"name": "li_ion", "endpoint": "search", "params": {"keyword": "lithium-ion"}},
            {"name": "dairy", "endpoint": "schedule", "params": {"from": "0401", "to": "0401"}}]}


class TestUsitc(PackTest):
    source = "usitc"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(self.csv(out, "release")[0]["name"], "2026HTSRev22")
        steel = self.csv(out, "steel")
        # to=7208 is the whole heading: the pack asks up to 7209 and drops the 7209 heading row
        self.assertEqual(len(steel), 54)
        self.assertEqual(steel[0]["htsno"], "7208")
        self.assertTrue(all(not x["htsno"].startswith("7209") for x in steel))
        self.assertEqual(steel[-1]["htsno"], "7208.90.00.00")
        line = next(x for x in steel if x["htsno"] == "7208.10.15.00")
        self.assertEqual((line["general"], line["general_rate_type"], line["general_ad_valorem_pct"]),
                         ("Free", "free", "0.0"))
        for col in ("special", "other", "rate_line", "general_effective", "chapter99_refs", "footnotes", "units"):
            self.assertIn(col, steel[0])
        dairy = self.csv(out, "dairy")
        cream = next(x for x in dairy if x["htsno"] == "0401.50.75.00")
        self.assertEqual((cream["general"], cream["general_rate_type"], cream["general_ad_valorem_pct"]),
                         ("$1.646/kg", "specific", ""))
        self.assertEqual(cream["chapter99_refs"], "9904.04.09 9904.04.21")
        li = self.csv(out, "li_ion")
        self.assertEqual(len(li), 31)
        # a search hit whose rate line is not in the reply gets no rate from the previous (unrelated) hit
        stat = next(x for x in li if x["htsno"] == "2826.90.90.10")
        self.assertEqual((stat["rate_line"], stat["general_rate_type"]), ("", "none"))

    def test_rate_carry_down_and_types(self):
        ad = testkit.load_pack_module("usitc", "adapter.py")
        rows = [
            {"htsno": "8703", "indent": "0", "general": "", "footnotes": [], "units": []},
            {"htsno": "8703.10.50", "indent": "2", "general": "2.5%", "units": [],
             "footnotes": [{"columns": ["other"], "value": "See 9903.90.08. ", "type": "endnote"}]},
            {"htsno": "8703.10.50.30", "indent": "3", "general": "", "footnotes": [], "units": ["No."]},
            {"htsno": "", "indent": "1", "general": "", "footnotes": [], "units": []},
            {"htsno": "8703.21.01.10", "indent": "4", "general": "", "footnotes": [], "units": ["No."]},
        ]
        out = ad.normalize({}, {"path": "/exportList"}, rows)
        self.assertEqual((out[2]["rate_line"], out[2]["general_effective"], out[2]["general_ad_valorem_pct"]),
                         ("8703.10.50", "2.5%", 2.5))
        self.assertEqual(out[2]["units"], "No.")
        self.assertEqual(out[1]["chapter99_refs"], "9903.90.08")
        self.assertEqual(out[1]["footnotes"], "[other] See 9903.90.08.")
        self.assertEqual((out[3]["rate_line"], out[3]["general_rate_type"]), ("", "none"))
        self.assertEqual(out[4]["rate_line"], "")   # 8703.21.01.10 does not extend 8703.10.50
        self.assertEqual(ad.rate_type("Free (A,AU,BH)"), "free")
        self.assertEqual(ad.rate_type("1.7¢/kg on lead content"), "specific")
        self.assertEqual(ad.rate_type("0.9¢/kg + 2.2%"), "compound")
        self.assertEqual(ad.rate_type("The duty provided in the applicable subheading + 25%"), "chapter99")
        self.assertEqual(ad.rate_type("12%"), "ad_valorem")

    def test_range_bounds(self):
        ad = testkit.load_pack_module("usitc", "adapter.py")
        self.assertEqual(ad.dotted("870380"), "8703.80")
        self.assertEqual(ad.dotted("8703.80.00.20"), "8703.80.00.20")
        self.assertEqual(ad.dotted("870"), "")
        sent = ad.before_request({}, {"path": "/exportList"}, {"from": "870380", "to": "8703.80"})
        self.assertEqual((sent["from"], sent["to"]), ("8703.80", "8703.81"))
        self.assertEqual(ad.before_request({}, {"path": "/exportList"}, {"from": "8703", "to": "8799"})["to"], "8800")
        self.assertEqual(ad.before_request({}, {"path": "/exportList"}, {"from": "9903", "to": "9999"})["to"],
                         "9999.99.99.99")
        bad = {"requests": [{"name": "x", "endpoint": "schedule", "params": {"from": "87a", "to": "8703"}}]}
        self.assertEqual(self.check(bad)[1]["route"], "fix_spec")
        rev = {"requests": [{"name": "x", "endpoint": "schedule", "params": {"from": "8704", "to": "8703"}}]}
        self.assertEqual(self.check(rev)[1]["route"], "fix_spec")

    def test_missing_range_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "schedule", "params": {"from": "7208"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)


if __name__ == "__main__":
    unittest.main()

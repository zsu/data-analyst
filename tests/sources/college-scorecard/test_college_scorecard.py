import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from api.data.gov/ed/collegescorecard/v1 on 2026-10-20 (key dropped from the URLs).
SPEC = {"description": "Delaware and Maryland public bachelor's colleges by 10-year earnings; MIT selected measures",
        "requests": [{"name": "de_md_public", "endpoint": "schools",
                      "params": {"state": "DE,MD", "ownership": 1, "predominant_degree": 3,
                                 "sort": "latest.earnings.10_yrs_after_entry.median:desc"}},
                     {"name": "mit", "endpoint": "schools",
                      "params": {"id": 166683, "fields": ["name", "admission_rate", "net_price", "earnings_10yr", "completion_rate"]}}]}


class TestCollegeScorecard(PackTest):
    source = "college-scorecard"
    sample_spec = SPEC

    def test_check_api_without_key(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "de_md_public")
        self.assertEqual(len(rows), 16)
        ud = [x for x in rows if x["id"] == "130943"][0]
        self.assertEqual(ud["school.name"], "University of Delaware")
        self.assertEqual(ud["latest.earnings.10_yrs_after_entry.median"], "83800")
        self.assertEqual(ud["school.ownership_label"], "Public")
        self.assertEqual(rows[-1]["latest.earnings.10_yrs_after_entry.median"], "")   # nulls sort last
        mit = self.csv(out, "mit")[0]
        self.assertEqual(mit["latest.admissions.admission_rate.overall"], "0.0455")  # alias resolved

    def test_seven_year_earnings_alias_and_nonzero_selectivity(self):
        # earnings_7yr alias (live from October 2026) and a selectivity range that leaves out admission_rate 0.
        # Fixtures r003/r004 recorded live 2026-10-22.
        spec = {"description": "Two Big Ten universities: 6-, 7- and 10-year earnings; schools admitting under 8% excluding zeros",
                "requests": [{"name": "earn_678", "endpoint": "schools",
                              "params": {"id": "170976,145637",
                                         "fields": ["name", "earnings_6yr", "earnings_7yr", "earnings_10yr"]}},
                             {"name": "selective_nonzero", "endpoint": "schools",
                              "params": {"latest.admissions.admission_rate.overall__range": "0.001..0.08",
                                         "fields": ["name", "admission_rate"],
                                         "sort": "latest.admissions.admission_rate.overall:asc"},
                              "max_rows": 100}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        earn = self.csv(out, "earn_678")
        self.assertEqual(len(earn), 2)
        self.assertTrue(all(x["latest.earnings.7_yrs_after_entry.median"].isdigit() for x in earn))
        sel = [float(x["latest.admissions.admission_rate.overall"]) for x in self.csv(out, "selective_nonzero")]
        self.assertTrue(sel and all(0 < v <= 0.08 for v in sel))
        self.assertEqual(sel, sorted(sel))

    def test_sort_takes_the_short_names(self):
        # the API answers HTTP 400 to sort=name:asc; short names map in sort as they do in filters and fields
        from testkit import load_pack_module
        a = load_pack_module("college-scorecard")
        p = a.before_request({}, {}, {"sort": "name:asc,latest.student.size:desc"})
        self.assertEqual(p["sort"], "school.name:asc,latest.student.size:desc")
        self.assertEqual(a.before_request({}, {}, {"sort": "state"})["sort"], "school.state")

    def test_paging_params_are_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "schools", "params": {"state": "DE", "per_page": 500}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_links_use_demo_key(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(all("api_key=DEMO_KEY" in i["url"] and "fields=id%2C" in i["url"] for i in items))
        self.assertTrue(any("page=0&per_page=100" in i["url"] for i in items))


if __name__ == "__main__":
    unittest.main()

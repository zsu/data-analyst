import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from api.usa.gov/crime/fbi/cde on 2026-10-20 (key dropped from the URLs).
SPEC = {"description": "Violent crime, United States, 2020-2022; Delaware agencies; hate crimes; police employees",
        "requests": [{"name": "violent_us", "endpoint": "summarized_national", "params": {"offense": "V", "from": "2020", "to": "2022"}},
                     {"name": "de_agencies", "endpoint": "agencies_by_state", "params": {"state": "de"}},
                     {"name": "hate_us", "endpoint": "hate_crime_national", "params": {"from": "01-2022", "to": "12-2023"}},
                     {"name": "employees_us", "endpoint": "pe_national", "params": {"from": 2021, "to": 2023}}]}


class TestFbi(PackTest):
    source = "fbi"
    sample_spec = SPEC

    def test_check_api_without_key(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")          # key optional: DEMO_KEY is used

    def test_fetch_long_table(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "violent_us")
        self.assertEqual(list(rows[0])[:7], ["section", "series", "key", "period", "year", "month", "value"])
        jan = [x for x in rows if x["section"] == "offenses/actuals" and x["series"] == "United States Offenses"
               and x["period"] == "2020-01"]
        self.assertEqual(jan[0]["value"], "93464")
        cov = {x["period"]: x["value"] for x in rows if x["section"] == "tooltips/Percent of Population Coverage"}
        self.assertEqual(cov["2021-06"], "77.06")       # NIBRS transition: coverage dip
        rate = [x for x in rows if x["section"] == "offenses/rates" and x["period"] == "2020-01"
                and x["series"] == "United States Offenses"][0]
        self.assertEqual(rate["unit"], "Offenses per 100,000 people")
        ag = self.csv(out, "de_agencies")
        self.assertIn("DE0010100", [a["ori"] for a in ag])
        hate = self.csv(out, "hate_us")
        self.assertTrue(any(h["series"] == "bias_category" and h["key"] == "Religion" for h in hate))
        # totals have no period of their own: each row names the span it was summed over
        self.assertEqual({(h["period"], h["span_from"], h["span_to"]) for h in hate}, {("", "2022-01", "2023-12")})
        self.assertEqual({(x["span_from"], x["span_to"]) for x in rows}, {("2020-01", "2022-12")})
        pe = self.csv(out, "employees_us")
        self.assertIn("651534", [p["value"] for p in pe if p["series"] == "Male Officers"])
        self.assertEqual({(p["span_from"], p["span_to"]) for p in pe}, {("2021", "2023")})

    def test_hate_crime_counts_carry_coverage(self):
        # source.md: type=totals has no participation figure; type=counts returns monthly coverage.
        # Fixture r005 recorded live (keyless, DEMO_KEY dropped from the URL) on 2026-10-22.
        spec = {"description": "Anti-Asian hate crime incidents, monthly with coverage, 2022",
                "requests": [{"name": "anti_asian_2022", "endpoint": "hate_crime_bias_national",
                              "params": {"bias": "14", "from": "01-2022", "to": "12-2022", "type": "counts"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "anti_asian_2022")
        cov = [x for x in rows if x["section"] == "tooltips/Percent of Population Coverage"]
        self.assertEqual(sorted(x["period"] for x in cov), [f"2022-{m:02d}" for m in range(1, 13)])
        self.assertTrue(all(0 < float(x["value"]) <= 100 for x in cov))
        self.assertTrue(any(x["section"] == "populations/participated_population" for x in rows))
        hate = self.csv(self.fetch(SPEC)[2], "hate_us")                 # totals: no coverage section
        self.assertFalse(any("Coverage" in h["section"] for h in hate))

    def test_bad_date_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "summarized_national", "params": {"offense": "V", "from": "2020-01", "to": "2022"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_bad_offense_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "summarized_national", "params": {"offense": "MURDER", "from": "2020", "to": "2022"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_links_use_demo_key_and_converted_dates(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertIn("https://api.usa.gov/crime/fbi/cde/summarized/national/V?from=01-2020&to=12-2022&API_KEY=DEMO_KEY", urls)
        self.assertTrue(any("/agency/byStateAbbr/DE?" in u for u in urls))


if __name__ == "__main__":
    unittest.main()

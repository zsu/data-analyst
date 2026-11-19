import json
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402
from core.http import DataError  # noqa: E402

# Fixtures recorded live from aspe.hhs.gov (keyless) on 2026-11-18: the SPEC fetch (r001-r008), and two requests
# the pack refuses, recorded to show the API's silent defaults: 2027/us/4 answers year "2026" with state "US"
# (r009), 2024/ny/4 answers state "us" (r010).
SPEC = {"description": "Poverty guidelines for four- and nine-person households, 48 states and Alaska, 2024 and 2026",
        "requests": [{"name": "fpl", "endpoint": "guidelines",
                      "params": {"year": [2024, 2026], "state": ["us", "AK"], "household_size": [4, 9]}}]}
BASE = "https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines/api"


def one(year, state, size):
    return {"requests": [{"name": "x", "endpoint": "guidelines",
                          "params": {"year": year, "state": state, "household_size": size}}]}


class TestAspe(PackTest):
    source = "aspe"
    sample_spec = SPEC

    def recorded(self, path):
        with open(os.path.join(self.fixtures, "index.json"), encoding="utf-8") as f:
            name = json.load(f)[f"GET {BASE}{path}"]
        with open(os.path.join(self.fixtures, name), encoding="utf-8") as f:
            return json.load(f)

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "fpl")
        self.assertEqual(list(rows[0]), ["year", "state", "area", "household_size", "guideline_usd", "value_note"])
        self.assertEqual(len(rows), 8)
        got = {(x["year"], x["state"], x["household_size"]): x for x in rows}
        self.assertEqual(got[("2024", "us", "4")]["guideline_usd"], "31200")
        self.assertEqual(got[("2026", "us", "4")]["guideline_usd"], "33000")
        self.assertEqual(got[("2026", "ak", "4")]["guideline_usd"], "41250")      # "AK" in the spec, ak in the table
        self.assertEqual(got[("2026", "ak", "4")]["area"], "Alaska")
        self.assertEqual(got[("2024", "us", "4")]["value_note"], "")
        nine = got[("2024", "us", "9")]                     # the API gives this one as a number
        self.assertEqual(nine["guideline_usd"], "58100")
        self.assertIn("size-8 guideline plus the per-person increment", nine["value_note"])
        self.assertTrue(any("above 8" in n for n in r["notes"]), r["notes"])

    def test_other_state_is_fix_spec(self):
        code, r = self.check(one(2024, "ny", 4))
        self.assertEqual((code, r["route"]), (2, "fix_spec"), r)
        self.assertIn("48 contiguous states and the District of Columbia", json.dumps(r))

    def test_year_out_of_range_is_fix_spec(self):
        for y in (1982, 3000, "2020-3000", "abc"):
            with self.subTest(year=y):
                code, r = self.check(one(y, "us", 4))
                self.assertEqual(r["route"], "fix_spec", r)

    def test_bad_size_is_fix_spec(self):
        for n in (0, "abc", 21, "0-4"):
            with self.subTest(size=n):
                code, r = self.check(one(2024, "us", n))
                self.assertEqual(r["route"], "fix_spec", r)

    def test_too_many_calls_is_fix_spec(self):
        code, r = self.check(one("1983-2026", "all", "1-8"))       # 44 x 3 x 8 = 1,056 calls
        self.assertEqual(r["route"], "fix_spec", r)

    def test_echo_mismatch_fails(self):
        ad = load_pack_module("aspe")
        # the real answer to 2027/us/4: the 2026 guideline, silently
        with self.assertRaises(DataError) as e:
            ad.row(2027, "us", 4, self.recorded("/2027/us/4"))
        self.assertIn("2026 guidelines, not 2027", str(e.exception))
        # the real answer to 2024/ny/4: the "us" schedule, silently
        with self.assertRaises(DataError):
            ad.row(2024, "ny", 4, self.recorded("/2024/ny/4"))
        # the same 2027 answer is right for the request it matches (state echoed as "US")
        self.assertEqual(ad.row(2026, "us", 4, self.recorded("/2027/us/4"))["guideline_usd"], "33000")
        with self.assertRaises(DataError):                  # an answer without the value
            ad.row(2024, "us", 4, {"data": {"year": "2024", "state": "us", "household_size": "4"}})

    def test_manual_ingest(self):
        """The manual bundle's files (the JSON pages saved from a browser) ingest to the same table."""
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, r)
        items = r["manual_items"]
        self.assertEqual(len(items), 8)
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        with open(os.path.join(self.fixtures, "index.json"), encoding="utf-8") as f:
            idx = json.load(f)
        for it in items:
            shutil.copy(os.path.join(self.fixtures, idx["GET " + it["url"]]), os.path.join(up, it["filename"]))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "fpl")
        self.assertEqual(len(rows), 8)
        self.assertEqual((rows[0]["year"], rows[0]["state"], rows[0]["household_size"], rows[0]["guideline_usd"]),
                         ("2024", "us", "4", "31200"))


if __name__ == "__main__":
    unittest.main()

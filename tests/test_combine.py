"""Analysis helpers: fiscal-year completeness, join checks, pack helpers with the same file name, education groups."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
sys.path.insert(0, os.path.join(SKILL, "scripts", "analysis"))
from combine import align_check, pack_lib, to_fiscal  # noqa: E402


def quiet(f, *a, **k):
    with redirect_stdout(io.StringIO()):
        return f(*a, **k)


class TestToFiscal(unittest.TestCase):
    def test_twelve_rows_of_one_month_is_not_a_full_year(self):
        df = pd.DataFrame({"date": ["2023-10-01"] * 12, "v": range(12)})
        r = quiet(to_fiscal, df, "date", "v").iloc[0]
        self.assertEqual(r["periods"], 1)
        self.assertFalse(r["complete"])

    def test_full_year_and_duplicated_month(self):
        months = pd.date_range("2022-10-01", periods=12, freq="MS")
        df = pd.DataFrame({"date": months, "v": 1.0})
        self.assertTrue(quiet(to_fiscal, df, "date", "v").iloc[0]["complete"])
        dup = pd.concat([df, df.iloc[[0]]])          # a second vintage of October
        self.assertFalse(quiet(to_fiscal, dup, "date", "v").iloc[0]["complete"])

    def test_quarterly(self):
        q = pd.date_range("2022-10-01", periods=4, freq="QS-OCT")
        r = quiet(to_fiscal, pd.DataFrame({"date": q, "v": 1.0}), "date", "v", freq="Q").iloc[0]
        self.assertTrue(r["complete"])


class TestAlignCheck(unittest.TestCase):
    def test_repeated_keys_refused(self):
        a = pd.DataFrame({"year": [2020, 2020], "x": [1, 2]})
        b = pd.DataFrame({"year": [2020], "y": [3]})
        with self.assertRaises(ValueError):
            quiet(align_check, {"a": a, "b": b}, on=["year"], units={"a": "dollars", "b": "dollars"})

    def test_partial_overlap_reported_and_units_required(self):
        a = pd.DataFrame({"year": [2020, 2021], "x": [1, 2]})
        b = pd.DataFrame({"year": [2021, 2022], "y": [3, 4]})
        out = io.StringIO()
        with redirect_stdout(out):
            align_check({"a": a, "b": b}, on=["year"], units={"a": "billions", "b": "thousands of persons"})
        self.assertIn("drop out", out.getvalue())
        with self.assertRaises(ValueError):
            quiet(align_check, {"a": a, "b": b}, on=["year"], units={"a": "billions"})


class TestPackLib(unittest.TestCase):
    def test_same_named_helpers_stay_apart(self):
        bls = pack_lib("bls", "tsstats", tempfile.gettempdir(), SKILL)
        fred = pack_lib("fred", "tsstats", tempfile.gettempdir(), SKILL)
        self.assertIsNot(bls, fred)
        self.assertTrue(bls.__file__.replace("\\", "/").endswith("bls/lib/tsstats.py"))
        self.assertTrue(fred.__file__.replace("\\", "/").endswith("fred/lib/tsstats.py"))

    def test_replication_copy_wins(self):
        with tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(out, "lib", "opm"))
            with open(os.path.join(out, "lib", "opm", "stats.py"), "w") as f:
                f.write("WHERE = 'copy'\n")
            self.assertEqual(pack_lib("opm", "stats", out, SKILL).WHERE, "copy")
            sys.modules.pop("opm_stats", None)


class TestEducGroup(unittest.TestCase):
    def test_usa_12th_grade_no_diploma_is_less_than_high_school(self):
        stats = pack_lib("ipums", "stats", tempfile.gettempdir(), SKILL)
        df = pd.DataFrame({"EDUCD": [61, 62, 63, 64, 65, 101, 114, 116]})
        g = list(stats.educ_group(df, "usa", "educ4"))
        self.assertEqual(g[0], "Less than high school")
        self.assertEqual(g[1:4], ["High school diploma"] * 3)
        self.assertEqual(g[4], "Some college")
        self.assertEqual(g[5:], ["Bachelor's degree or more"] * 3)

    def test_cps_codes(self):
        stats = pack_lib("ipums", "stats", tempfile.gettempdir(), SKILL)
        df = pd.DataFrame({"EDUC": [71, 73, 81, 92, 111, 125]})
        g = list(stats.educ_group(df, "cps", "educ5"))
        self.assertEqual(g, ["High school diploma or less"] * 2 + ["Some college"] * 2
                         + ["Bachelor's degree", "Professional degree or doctorate"])


if __name__ == "__main__":
    unittest.main()

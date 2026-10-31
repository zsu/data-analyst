"""Offline tests for the snapqc pack.

Fixtures (downloaded from snapqcdata.net on 2026-10-31, unchanged unless said):
  datafiles_p0.html, datafiles_p1.html, datafiles_p2.html  the /datafiles listing, pages 0-2 (FY 2024 to FY 2014;
                                                           FY 2018 lists two CSV zips, FY 2020 three databases)
  qcfy2024_csv_GU_VI.zip   qcfy2024_csv.zip (5.6 MB) with only the Guam and Virgin Islands rows (475 of 44,891),
                           header and values unchanged
  qcfy2018_csv_0_GU.zip    the reweighted FY 2018 file (2020-08 upload) with only the Guam rows
"""
import csv
import io
import os
import shutil
import sys
import unittest
import zipfile

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills", "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SPEC = {"description": "SNAP QC FY 2024: weighted totals; Virgin Islands units and participants",
        "requests": [
            {"name": "totals", "endpoint": "summary", "params": {"fiscal_year": 2024}},
            {"name": "vi_units", "endpoint": "units",
             "params": {"fiscal_year": 2024, "state": "VI", "variables": ["FSUSIZE", "FSBEN", "AGE*"]}},
            {"name": "vi_people", "endpoint": "persons",
             "params": {"fiscal_year": 2024, "state": "Virgin Islands", "person_variables": ["AGE", "SEX"],
                        "participants_only": True}}]}


def fixture_rows(name: str) -> list[dict]:
    with zipfile.ZipFile(os.path.join(FX, name)) as z:
        return list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode("latin-1"))))


def rezip(name: str, edit) -> bytes:
    """The fixture zip with edit(csv text) -> csv text applied to its CSV."""
    out = io.BytesIO()
    with zipfile.ZipFile(os.path.join(FX, name)) as z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
        for n in z.namelist():
            o.writestr(n, edit(z.read(n).decode("latin-1")).encode("latin-1"))
    return out.getvalue()


class TestSnapqc(PackTest):
    source = "snapqc"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["status"], "ok", r)
        raw = fixture_rows("qcfy2024_csv_GU_VI.zip")
        tot = {(x["state_abbr"], x["measure"]): x for x in self.csv(out, "totals")}
        # weighted from the file itself: units = sum FYWGT, persons = sum FYWGT x FSUSIZE, benefits = sum FYWGT x FSBEN
        for abbr, fips, n in (("GU", "66", 197), ("VI", "78", 278)):
            rs = [x for x in raw if x["STATE"] == fips]
            self.assertEqual(int(tot[(abbr, "units")]["sample_units"]), n)
            self.assertAlmostEqual(float(tot[(abbr, "units")]["value"]), sum(float(x["FYWGT"]) for x in rs), places=2)
            self.assertAlmostEqual(float(tot[(abbr, "persons")]["value"]),
                                   sum(float(x["FYWGT"]) * float(x["FSUSIZE"]) for x in rs), places=2)
            self.assertAlmostEqual(float(tot[(abbr, "benefits")]["value"]),
                                   sum(float(x["FYWGT"]) * float(x["FSBEN"]) for x in rs), places=1)
        self.assertEqual(int(tot[("US", "units")]["sample_units"]), 475)
        notes = " ".join(r["notes"])
        # a trimmed file cannot match the tech doc's national totals: the note says so instead of passing silently
        self.assertIn("U.S. totals differ from FY 2024 Technical Documentation", notes)
        units = self.csv(out, "vi_units")
        self.assertEqual(len(units), 278)
        self.assertEqual(list(units[0])[:9], ["fiscal_year", "database", "HHLDNO", "STATE", "STATENAME", "YRMONTH",
                                              "HWGT", "FYWGT", "state_abbr"])
        self.assertIn("AGE18", units[0])                                  # FY 2024 has 18 person slots
        self.assertEqual({x["state_abbr"] for x in units}, {"VI"})
        people = self.csv(out, "vi_people")
        self.assertEqual(len(people), sum(int(x["FSUSIZE"]) for x in units))   # FSAFIL = 1 rows = FSUSIZE
        self.assertEqual(list(people[0])[-5:], ["FSBEN", "person", "FSAFIL", "AGE", "SEX"])

    def test_reissued_year_takes_latest_upload(self):
        code, r, out = self.fetch({"requests": [{"name": "s18", "endpoint": "summary",
                                                 "params": {"fiscal_year": 2018, "state": "GU"}}]})
        self.assertEqual(code, 0, r)
        notes = " ".join(r["notes"])
        self.assertIn("2020-07/qcfy2018_csv.zip", notes)
        self.assertIn("used the latest upload, /sites/default/files/2020-08/qcfy2018_csv_0.zip", notes)
        gu = {x["measure"]: x for x in self.csv(out, "s18")}
        rs = fixture_rows("qcfy2018_csv_0_GU.zip")
        self.assertAlmostEqual(float(gu["units"]["value"]), sum(float(x["FYWGT"]) for x in rs), places=2)

    def test_listing(self):
        ad = load_pack_module("snapqc")
        rows = []
        for i in (0, 1, 2):
            with open(os.path.join(FX, f"datafiles_p{i}.html"), encoding="utf-8") as f:
                rows += ad.listing_files(f.read())
        self.assertEqual(sorted({r["year"] for r in rows}, reverse=True)[:11], list(range(2024, 2013, -1)))
        for db, path in (("qcfy2020", "/2022-12/qcfy2020_csv.zip"), ("qcfy2020_per1", "/2022-12/qcfy2020_per1_csv.zip"),
                         ("qcfy2020_per2", "/2022-12/qcfy2020_per2_csv.zip")):
            hit, notes = ad.pick_csv(ad.csv_candidates(rows, 2020, db), db)
            self.assertTrue(hit["path"].endswith(path))
            self.assertEqual(notes, [])
        self.assertEqual(ad.csv_candidates(rows, 2013, "qcfy2013"), [])     # no CSV before FY 2014
        two = [dict(r, path=r["path"].replace("2020-07", "2020-08")) for r in ad.csv_candidates(rows, 2018, "qcfy2018")]
        with self.assertRaisesRegex(Exception, "not guessing"):
            ad.pick_csv(two, "qcfy2018")

    def test_gates(self):
        ad = load_pack_module("snapqc")
        keep = lambda h: ad._ids(h) + ["FSUSIZE", "FSBEN"]
        ok = rezip("qcfy2024_csv_GU_VI.zip", lambda t: t)
        self.assertEqual(len(ad.Database(ok, 2024, "qcfy2024", keep).rows), 475)
        cases = [
            (lambda t: t.replace("\n", "\n1,", 2).replace("\n1,", "\n", 1), "fields, the header"),   # one row too long
            (lambda t: t.replace(",202401,", ",202501,", 1), "outside FY 2024"),
            (lambda t: t.replace(",FYWGT,", ",FYWGTX,"), "lacks"),
        ]
        for edit, words in cases:
            with self.assertRaisesRegex(Exception, words):
                ad.Database(rezip("qcfy2024_csv_GU_VI.zip", edit), 2024, "qcfy2024", keep)
        with self.assertRaisesRegex(Exception, "should hold one qc_pub_fy2023.csv"):
            ad.Database(ok, 2023, "qcfy2023", keep)                          # the wrong year's zip
        with self.assertRaisesRegex(Exception, "not in the FY 2024 file: NOPE"):
            ad.resolve_vars(["FSBEN", "NOPE"], ["FSBEN"], [], 2024)
        # summary: HWGT / the state's sample months must be FYWGT in a full-year file
        bad = rezip("qcfy2024_csv_GU_VI.zip", lambda t: _scale_first_fywgt(t))
        with self.assertRaisesRegex(Exception, "differs from FYWGT on 1 rows"):
            ad.summary_rows(ad.Database(bad, 2024, "qcfy2024", keep), None, None)

    def test_spec_checks(self):
        for params, words in [({"fiscal_year": 2012}, "CSV files exist from FY 2014"),
                              ({"fiscal_year": 2024, "period": "per1"}, "FY 2020 only"),
                              ({"fiscal_year": 2024, "state": "PR"}, "not one of the 53"),
                              ({"fiscal_year": 2024, "month": 202312 + 100}, "inside FY 2024"),
                              ({"fiscal_year": 2024, "variables": ["FS-BEN"]}, "not a variable name")]:
            code, r = self.check({"requests": [{"name": "x", "endpoint": "units", "params": params}]})
            self.assertEqual(r["route"], "fix_spec", r)
            self.assertIn(words, r["message"])
        code, r, out = self.fetch({"requests": [{"name": "x", "endpoint": "units",
                                                 "params": {"fiscal_year": 2024, "variables": ["FSBEN", "EITC*"]}}]})
        self.assertNotEqual(code, 0)
        self.assertIn("not in the FY 2024 file: EITC*", r["message"])     # EITCi was dropped in FY 2024

    def test_manual_bundle_and_ingest(self):
        spec = {"requests": SPEC["requests"][1:2]}
        sp, out = self.write_spec(spec)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        self.assertEqual([i["filename"] for i in r["items"]], ["vi_units_qcfy2024_csv.zip"])
        self.assertEqual(r["items"][0]["url"], "https://snapqcdata.net/sites/default/files/2026-08/qcfy2024_csv.zip")
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(FX, "qcfy2024_csv_GU_VI.zip"), os.path.join(up, "vi_units_qcfy2024_csv.zip"))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(len(self.csv(out, "vi_units")), 278)


def _scale_first_fywgt(text: str) -> str:
    lines = text.split("\n")
    head = lines[0].split(",")
    j = head.index("FYWGT")
    cells = lines[1].split(",")
    cells[j] = str(float(cells[j]) * 1.01)
    lines[1] = ",".join(cells)
    return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()

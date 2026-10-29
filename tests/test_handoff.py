"""The data hand-off (fetch.py --handoff) and the --check records behind check_reminder."""
import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
FETCH = os.path.join(SKILL, "scripts", "fetch.py")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from core import handoff  # noqa: E402


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def fetch_cli():
    spec = importlib.util.spec_from_file_location("fetch_cli_h", FETCH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestHandoff(unittest.TestCase):
    def test_summary_reads_the_provenance(self):
        with tempfile.TemporaryDirectory() as out:
            rows = [{"region": "North", "year": y, "units": "tons", "amount": 10 + y % 7} for y in range(2001, 2011)]
            write_csv(os.path.join(out, "data", "noaa", "storms.csv"), rows)
            with open(os.path.join(out, "data", "noaa", "request.json"), "w", encoding="utf-8") as f:
                json.dump({"route": "api", "retrieved": "2026-03-04T15:00:00+00:00",
                           "tables": [{"name": "storms", "file": "storms.csv", "rows": 10}],
                           "notes": ["storms: stopped at 10 rows"], "problems": []}, f)
            write_csv(os.path.join(out, "data", "loose", "x.csv"), [{"a": "1"}])          # copied in by hand
            write_csv(os.path.join(out, "published", "table.csv"),
                      [{"measure": "m", "value": "3", "source_url": "https://agency.example/t", "accessed": "2026-03-01"}])
            p = subprocess.run([sys.executable, "-I", "-B", FETCH, "--handoff", "--out", out], capture_output=True,
                               text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            r = json.loads(p.stdout)
        noaa = next(s for s in r["sources"] if s["source"] == "noaa")
        self.assertEqual((noaa["route"], noaa["retrieved"]), ("api", "2026-03-04"))
        self.assertEqual(noaa["files"][0]["rows"], 10)
        pv = noaa["files"][0]["preview"]
        self.assertEqual(pv["header"], ["year", "amount"])               # the columns that vary come first
        self.assertEqual(len(pv["rows"]), handoff.PREVIEW_ROWS)
        self.assertIn(("region", "North"), [tuple(x) for x in pv["same"]])
        self.assertIn("### noaa", r["text"])
        self.assertIn("Route: api; retrieved 2026-03-04", r["text"])
        self.assertIn("stopped at 10 rows", r["text"])
        self.assertEqual(r["unpackaged"], ["loose"])                     # no request.json: said, not hidden
        self.assertIn("https://agency.example/t", r["text"])

    def test_notes_are_never_dropped_silently(self):
        with tempfile.TemporaryDirectory() as out:
            write_csv(os.path.join(out, "data", "cdc", "t.csv"), [{"a": "1"}])
            notes = [f"t: plain note {i}" for i in range(10)] + ["t: 12 rows may be missing across pages"]
            with open(os.path.join(out, "data", "cdc", "request.json"), "w", encoding="utf-8") as f:
                json.dump({"route": "api", "retrieved": "2026-01-02", "tables": [{"file": "t.csv"}], "notes": notes}, f)
            r = handoff.summarize(out)
        self.assertIn("rows may be missing", r["text"])                  # a caveat is always shown
        self.assertIn("plain note 5", r["text"])
        self.assertNotIn("plain note 6", r["text"])
        self.assertIn("(+4 more notes in data/cdc/request.json)", r["text"])

    def test_more_notes_pointer_names_the_file_read(self):
        with tempfile.TemporaryDirectory() as out:
            write_csv(os.path.join(out, "data", "usps", "t.csv"), [{"a": "1"}])
            with open(os.path.join(out, "data", "usps", "fetch_spec.json"), "w", encoding="utf-8") as f:
                json.dump({"route": "api", "retrieved": "2026-01-02", "tables": [{"file": "t.csv"}],
                           "notes": [f"t: plain note {i}" for i in range(9)]}, f)
            r = handoff.summarize(out)
        self.assertIn("(+3 more notes in data/usps/fetch_spec.json)", r["text"])

    def test_microdata_gets_no_preview(self):
        with tempfile.TemporaryDirectory() as out:
            write_csv(os.path.join(out, "data", "ipums", "extract.csv"), [{"SERIAL": "1", "AGE": "40"}])
            with open(os.path.join(out, "data", "ipums", "request.json"), "w", encoding="utf-8") as f:
                json.dump({"route": "api", "retrieved": "2026-01-02", "tables": [{"file": "extract.csv"}]}, f)
            r = handoff.summarize(out)
        self.assertIsNone(r["sources"][0]["files"][0]["preview"])
        self.assertIn("Microdata: no preview", r["text"])


class TestCheckRecords(unittest.TestCase):
    def test_a_fetch_of_an_unchecked_spec_is_named(self):
        cli = fetch_cli()
        with tempfile.TemporaryDirectory() as out:
            sp = os.path.join(out, "spec.json")
            with open(sp, "w", encoding="utf-8") as f:
                json.dump({"requests": [{"name": "a", "endpoint": "q"}]}, f)
            self.assertIn("without --check", cli.check_reminder(out, "cdc", sp))
            cli.record_check(out, "cdc", sp)
            self.assertIsNone(cli.check_reminder(out, "cdc", sp))
            self.assertIsNotNone(cli.check_reminder(out, "fda", sp))         # per source
            with open(sp, "w", encoding="utf-8") as f:                       # an edited spec needs a new check
                json.dump({"requests": [{"name": "a", "endpoint": "q", "max_rows": 5}]}, f)
            self.assertIsNotNone(cli.check_reminder(out, "cdc", sp))

    def test_a_failed_check_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as out:
            sp = os.path.join(out, "spec.json")
            with open(sp, "w", encoding="utf-8") as f:
                json.dump({"requests": [{"name": "a", "endpoint": "no_such_endpoint"}]}, f)
            env = dict(os.environ, DATA_ANALYST_FIXTURES=out)
            p = subprocess.run([sys.executable, "-I", "-B", FETCH, "--source", "cdc", sp, "--out", out, "--check"],
                               capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(p.stdout)["route"], "fix_spec", p.stdout)
            self.assertIsNotNone(fetch_cli().check_reminder(out, "cdc", sp))

    def test_a_deep_check_is_recorded_by_its_outcome(self):
        from unittest import mock
        cli = fetch_cli()
        with tempfile.TemporaryDirectory() as out:
            sp = os.path.join(out, "spec.json")
            with open(sp, "w", encoding="utf-8") as f:
                json.dump({"requests": [{"name": "a"}]}, f)
            argv = ["fetch.py", "--source", "bea", sp, "--out", out, "--check"]

            def fails(pack, rest):
                raise SystemExit(2)
            with mock.patch.object(sys, "argv", list(argv)), mock.patch.object(cli, "run_deep", fails):
                with self.assertRaises(SystemExit):
                    cli.main()
            self.assertIsNotNone(cli.check_reminder(out, "bea", sp))          # a failed check: not recorded
            with mock.patch.object(sys, "argv", list(argv)), \
                    mock.patch.object(cli, "run_deep", lambda pack, rest: None):
                cli.main()                                                    # returns without exiting
            self.assertIsNone(cli.check_reminder(out, "bea", sp))


if __name__ == "__main__":
    unittest.main()

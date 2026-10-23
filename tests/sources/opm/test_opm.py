"""Offline tests for the opm deep pack.

Fixtures (fixtures), all recorded from data.opm.gov in October 2026 (public data, no key):
- ping_accessions_202501.json: GET /accessions?current=true&year=2025&month=1 (the ping call).
- list_separations.json: GET /separations (the full file listing).
- separations_202608_1_head.parquet: the first six rows (all 69 columns) of the real
  separations_202608_1 file, trimmed with pyarrow to stay small; served for its download URL.
- separations_202608_1_head.csv: the same six rows as CSV (the TXT/CSV ingest route, no Parquet needed).
Every test points OPM_CACHE_DIR at a temp folder so the user's raw-file cache is never touched.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest, run  # noqa: E402

PACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst",
                    "sources", "opm")


def opm_fetch():
    """The pack's fetch.py as a module (for its bundle helpers)."""
    import importlib.util
    sys.path[:0] = [os.path.abspath(PACK)]
    spec = importlib.util.spec_from_file_location("opm_pack_fetch", os.path.join(PACK, "fetch.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

SPEC = {"description": "Separations by category, August 2026 file",
        "datasets": {"separations": {"periods": ["2026-08"]}},
        "columns": ["department_code", "department", "agency_code", "agency", "separation_category_code",
                    "separation_category", "annualized_adjusted_basic_pay"]}


def has_parquet() -> bool:
    for m in ("pyarrow", "fastparquet", "duckdb", "polars"):
        try:
            __import__(m)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


class TestOpm(PackTest):
    source = "opm"
    sample_spec = SPEC

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp.name, "cache")
        self.env = {"OPM_CACHE_DIR": self.cache, "DATA_ANALYST_CACHE": self.cache}

    def opm_csv(self, out, name="opm_separations"):
        with open(os.path.join(out, "data", "opm", f"{name}.csv"), encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_check_api(self):
        code, r = self.check(SPEC, env=self.env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)
        self.assertEqual(r["source"], "opm")
        self.assertIn("--source opm", r["next_command"])
        self.assertEqual(r["plan"]["first"], "separations_202608_1")
        self.assertLess(len(json.dumps(r)), 2500)

    def test_dry_run(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--dry-run", env=self.env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["files"][0]["url"], "https://data.opm.gov/api/v1/files/separations/2026/08/1/download")
        self.assertTrue(r["files"][0]["current"])

    @unittest.skipUnless(has_parquet(), "no Parquet engine installed")
    def test_fetch(self):
        code, r, out = self.fetch(SPEC, env=self.env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        rows = self.opm_csv(out)
        self.assertEqual(len(rows), 6)
        self.assertEqual(list(rows[0])[:3], ["count", "personnel_action_effective_date_yyyymm", "department_code"])
        self.assertEqual(rows[0]["department_code"], "SZ")
        self.assertEqual(rows[0]["separation_category"], "QUIT")
        self.assertEqual(rows[2]["annualized_adjusted_basic_pay"], "REDACTED")
        self.assertEqual(rows[0]["source_file"], "separations_202608_1")
        d = os.path.join(out, "data", "opm")
        for f in ("manifest.json", "request.json", "README.md", "data_dictionary.csv", "spec_used.json",
                  "opm_separations.parquet"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
            M = json.load(f)
        self.assertEqual(M["files"][0]["version"], 1)
        self.assertEqual(M["route"], "api")
        self.assertTrue(os.path.exists(os.path.join(self.cache, "separations_202608_1.parquet")))
        # the same request is reused; with the raw file cached a refetch needs no download
        code, r = self.cli(os.path.join(out, "spec.json"), "--out", out, "--check", env=self.env)
        self.assertEqual(r["route"], "reuse_existing", r)

    @unittest.skipUnless(has_parquet(), "no Parquet engine installed")
    def test_filters_and_aggregate(self):
        spec = dict(SPEC, filters={"department_code": ["SZ", "TR"]}, columns=["department_code", "separation_category"],
                    aggregate={"by": ["department_code", "separation_category"]})
        code, r, out = self.fetch(spec, env=self.env)
        self.assertEqual(code, 0, r)
        rows = self.opm_csv(out)
        self.assertEqual({x["department_code"] for x in rows}, {"SZ", "TR"})
        sz = [x for x in rows if x["department_code"] == "SZ"][0]
        self.assertEqual(float(sz["headcount"]), 2.0)
        self.assertEqual(float(sz["pay_sum"]), 72738 + 63034)

    def test_ingest_csv_form(self):
        sp, out = self.write_spec(SPEC)
        up = os.path.join(self.tmp.name, "up")
        os.makedirs(up)
        shutil.copy(os.path.join(self.fixtures, "separations_202608_1_head.csv"),
                    os.path.join(up, "separations_202608_1.csv"))
        code, r = self.cli(sp, "--out", out, "--ingest", up, env=self.env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        rows = self.opm_csv(out)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[3]["separation_category"], "RETIREMENT - VOLUNTARY")
        self.assertEqual(rows[5]["annualized_adjusted_basic_pay"], "")      # null in the Parquet original

    @unittest.skipUnless(has_parquet(), "no Parquet engine installed")
    def test_ingest_prebuilt_folder(self):
        code, r, out = self.fetch(SPEC, env=self.env)
        self.assertEqual(code, 0, r)
        out2 = os.path.join(self.tmp.name, "out2")
        os.makedirs(out2)
        sp2 = os.path.join(out2, "spec.json")
        shutil.copy(os.path.join(out, "spec.json"), sp2)
        code, r = self.cli(sp2, "--out", out2, "--ingest", os.path.join(out, "data"), env=self.env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["acquired"], "manual_prebuilt")
        self.assertEqual(r["warnings"], [])
        self.assertEqual(len(self.opm_csv(out2)), 6)

    def test_fix_spec(self):
        code, r = self.check({"datasets": {"payroll": {"periods": ["2026-08"]}}}, env=self.env)
        self.assertEqual((code, r["route"]), (2, "fix_spec"), r)
        code, r = self.check(dict(SPEC, filters={"grade": [8]}), env=self.env)
        self.assertEqual((code, r["route"]), (2, "fix_spec"), r)
        code, r = self.check({"datasets": {"separations": {"from": "2031-01", "to": "2031-03"}}}, env=self.env)
        self.assertEqual((code, r["route"]), (2, "fix_spec"), r)

    def test_restricted_sandbox_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = run(["--source", "opm", sp, "--out", out, "--check", "--env", "sandbox"], None,
                      {"DATA_ACCESS_FORCE": "restricted", **self.env}, self.home)
        self.assertEqual(r["route"], "manual_download", r)
        b = r["manual_bundle"]
        self.assertTrue(b.endswith(os.path.join("manual_download", "opm")), b)
        for f in ("INSTRUCTIONS.md", "spec.json", "fetch.py", "opm_api.py", "opm_io.py", "data_dictionary.csv",
                  os.path.join("core", "http.py")):
            self.assertTrue(os.path.exists(os.path.join(b, f)), f)
        self.assertIn("--ingest", r["next_command"])
        for f in ("download_opm.sh", "download_opm.ps1"):              # the request is built in
            with open(os.path.join(b, f), encoding="ascii") as fh:
                self.assertIn("separations\tperiods\t202608", fh.read())
        with open(r["instructions"], encoding="utf-8") as fh:
            text = fh.read()
        self.assertLess(text.index("bash ~/Downloads/download_opm.sh"), text.index("python fetch.py"))
        self.assertEqual(r["user_commands"]["bash"], "bash ~/Downloads/download_opm.sh")
        self.assertTrue(r["request_spec"].endswith("spec.json"))
        code, r = run(["--source", "opm", sp, "--out", out, "--check", "--env", "cloud"], None,
                      {"DATA_ACCESS_FORCE": "restricted", **self.env}, self.home)
        self.assertEqual(r["route"], "cloud_allowlist", r)

    RANGE_SPEC = {"datasets": {"separations": {"from": "2025-06", "to": "latest", "months": [8]},
                               "accessions": {"periods": ["latest"]}},
                  "pin": {"separations_202508": 3}}

    def test_script_rules(self):
        self.assertEqual(opm_fetch().script_rules(self.RANGE_SPEC),
                         ["separations\trange\t202506\tlatest\t8", "accessions\tperiods\tlatest",
                          "separations\tpin\t202508\t3"])

    def run_download_script(self, cmd_for):
        """List and download against a local stand-in for data.opm.gov; check which files were picked."""
        import http.server
        import threading
        listing = {"separations": ["separations_202508_4", "separations_202509_4", "separations_202608_1"],
                   "accessions": ["accessions_202607_2", "accessions_202608_1"]}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = self.path.split("?")[0].strip("/").split("/")     # api/v1/files/<ds>[/y/m/v/download]
                if len(parts) == 4 and "current=true" in self.path:
                    body = json.dumps([{"filename": n, "version": int(n.rsplit("_", 1)[1]), "current": True}
                                       for n in listing[parts[3]]]).encode()
                elif len(parts) == 8 and parts[-1] == "download":
                    body = ("/".join(parts[3:7])).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            d = os.path.join(self.tmp.name, "opm_script_" + cmd_for.__name__)
            os.makedirs(d)
            paths = opm_fetch().write_download_scripts(d, self.RANGE_SPEC)
            env = {**os.environ, "OPM_API_BASE": f"http://127.0.0.1:{srv.server_port}/api/v1/files",
                   "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
            p = subprocess.run(cmd_for(paths), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            got = sorted(f for f in os.listdir(d) if f.endswith(".parquet"))
            self.assertEqual(got, ["accessions_202608_1.parquet", "separations_202508_3.parquet",
                                   "separations_202608_1.parquet"])
            with open(os.path.join(d, "separations_202508_3.parquet"), encoding="ascii") as f:
                self.assertEqual(f.read(), "separations/2025/08/3")         # the pinned version
        finally:
            srv.shutdown()

    def test_download_script_bash(self):
        bash = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
        if not bash or not os.path.exists(bash) or subprocess.run([bash, "-c", "command -v curl"],
                                                                  capture_output=True).returncode:
            self.skipTest("bash with curl not available")

        def bash_cmd(p):
            return [bash, p["bash"].replace("\\", "/")]
        self.run_download_script(bash_cmd)

    def test_download_script_powershell(self):
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            self.skipTest("PowerShell not available")

        def ps_cmd(p):
            return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p["powershell"], "-NoPause"]
        self.run_download_script(ps_cmd)

    @unittest.skipUnless(has_parquet(), "no Parquet engine installed")
    def test_bundle_option_a_runs_standalone(self):
        """The bundle's fetch.py runs without the skill (Option A), here against the fixtures."""
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle", env=self.env)
        self.assertEqual(code, 0, r)
        b = r["manual_bundle"]
        with open(os.path.join(b, "urls.txt"), encoding="utf-8") as f:
            self.assertIn("/separations/2026/08/1/download", f.read())
        e = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        e.update(DATA_ANALYST_FIXTURES=self.fixtures, **self.env)
        p = subprocess.run([sys.executable, os.path.join(b, "fetch.py"), "spec.json", "--out", "."], cwd=b,
                           capture_output=True, text=True, env=e)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.exists(os.path.join(b, "data", "opm", "opm_separations.csv")))

    def test_list(self):
        code, r = self.cli("--list", "separations", env=self.env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["first"], "2005-01")
        self.assertIn("latest_files", r)

    @unittest.skipUnless(has_parquet(), "no Parquet engine installed")
    def test_stats_lib(self):
        code, r, out = self.fetch(SPEC, env=self.env)
        import importlib.util          # by path: ipums and opm both have a lib/stats.py
        sys.path.insert(0, os.path.join(self.pack_dir, "lib"))
        spec = importlib.util.spec_from_file_location("opm_stats_t", os.path.join(self.pack_dir, "lib", "stats.py"))
        stats = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stats)
        sep = stats.clean(stats.load(os.path.join(out, "data"), "separations"))     # data/ -> data/opm/
        self.assertEqual(len(sep), 6)
        self.assertEqual(int(sep["pay_redacted"].sum()), 2)
        cov = stats.redaction_coverage(sep)
        self.assertEqual(len(cov), 1)
        # aggregated rows stand for n actions: late shares are weighted by n, not by rows
        import pandas as pd
        agg = pd.DataFrame({"period": [202509] * 3, "file_period": [202509, 202510, 202511], "n": [90, 8, 2]})
        t = stats.late_reporting(agg)
        self.assertEqual((int(t["same_month"][0]), int(t["later"][0])), (90, 10))
        self.assertAlmostEqual(float(t["share_late"][0]), 0.1)
        # aggregated rows: a redaction flag counts the row's n employees; an aggregated pay_redacted is a count
        g = pd.DataFrame({"dept": ["DD", "VA"], "n": [680072, 400], "loc_redacted": [True, False],
                          "pay_redacted": [680072, 3]})
        cov = stats.redaction_coverage(g, ["dept"]).set_index("dept")
        self.assertEqual((int(cov["location_redacted"]["DD"]), float(cov["location_redacted_share"]["DD"])), (680072, 1.0))
        self.assertEqual(int(cov["pay_redacted"]["VA"]), 3)
        # missing categories: left out of the shares by default; OPM's dashboards keep them in the denominator
        ed = pd.DataFrame({"dept": ["A"] * 3, "educ": ["BA", "HS", None], "n": [6, 3, 1]})
        self.assertAlmostEqual(float(stats.shares(ed, ["dept"], "educ").set_index("educ")["share"]["BA"]), 6 / 9)
        kept = stats.shares(ed, ["dept"], "educ", keep_missing=True).set_index("educ")
        self.assertEqual((round(float(kept["share"]["BA"]), 6), int(kept["n_missing"]["BA"])), (0.6, 1))
        self.assertAlmostEqual(float(stats.shares(ed, [], "educ", keep_missing=True).set_index("educ")["share"]["HS"]), 0.3)

    def test_aggregate_refuses_columns_it_would_drop(self):
        spec = dict(SPEC, aggregate={"by": ["department_code"]})        # SPEC lists more columns than "by"
        code, r, out = self.fetch(spec, env=self.env)
        self.assertNotEqual(code, 0)
        self.assertIn("would be dropped", r.get("message", ""))


if __name__ == "__main__":
    unittest.main()

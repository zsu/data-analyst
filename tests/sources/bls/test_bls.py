"""Offline tests for the bls deep pack.

Fixtures (fixtures/) were recorded from live, keyless BLS API v1 responses on 2026-10-20
(DATA_ANALYST_RECORD + a normal fetch): v1_cpi_2017_2026.json, v1_jobs_2017_2026.json, v1_build_md.json
(LAUS state and county, state CES, OEWS national and state, U-6 and LNS13023621), and the data.bls.gov
series page describe_CUUR0000SA0.html. No BLS key was available, so the keyed paths (v2, a rejected key)
are tested in-process with a stubbed ping instead of invented responses.
"""
import csv
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest

PACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "sources", "bls")
PACK = os.path.abspath(PACK)
SCRIPTS = os.path.join(PACK, "..", "..", "scripts")
sys.path.insert(0, SCRIPTS)
from testkit import PackTest  # noqa: E402

CPI = {"description": "CPI-U all items, U.S. city average, 2017-2026",
       "series": [{"id": "CUUR0000SA0", "label": "CPI-U all items (NSA)"}], "start_year": 2017, "end_year": 2026}
JOBS = {"description": "Unemployment rate and nonfarm payrolls, 2017-2026",
        "series": [{"id": "LNS14000000", "label": "Unemployment rate"},
                   {"id": "CES0000000001", "label": "Total nonfarm payrolls"}], "start_year": 2017, "end_year": 2026}
BUILD = {"description": "Builder verification: LAUS state and county, CES state, OEWS national and state, "
                        "U-6 vs the job-losers level",
         "series": ["LNS13327709", "LNS13023621"],
         "build": [{"kind": "state", "state": "MD"}, {"kind": "laus", "county": "24005"},
                   {"kind": "oews", "occupation": "15-1252", "measure": "annual_median_wage"},
                   {"kind": "oews", "occupation": "15-1252", "measure": "annual_median_wage", "state": "MD"}],
         "start_year": 2025, "end_year": 2026}


def load(name, fname):
    """Import a pack module by path under a unique name (other packs have a fetch.py and keycheck.py too)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(PACK, fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def rows_of(out, name="bls_series"):
    with open(os.path.join(out, "data", "bls", f"{name}.csv"), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pick(rows, sid, year, period):
    return next(r for r in rows if r["series_id"] == sid and r["year"] == str(year) and r["period"] == period)


class TestBls(PackTest):
    source = "bls"
    sample_spec = CPI

    # ---------------------------------------------------------------- route check
    def test_check_keyless_route(self):
        code, r = self.check(CPI)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api_keyless", r)
        self.assertEqual(r["source"], "bls")
        self.assertIn("scripts", r["next_command"])
        self.assertIn("--source bls", r["next_command"])
        self.assertEqual(r["queries_needed"], {"v1_keyless": 1, "v2_with_key": 1})
        self.assertLess(len(json.dumps(r)), 2600)

    def test_check_fix_spec(self):
        code, r = self.check({"series": ["not-an-id"], "start_year": 2020, "end_year": 2021})
        self.assertEqual(code, 2, r)
        self.assertEqual(r["route"], "fix_spec")

    def test_check_large_request_needs_key(self):
        big = {"series": [f"CES{i:010d}" for i in range(200)], "start_year": 1990, "end_year": 2026}
        code, r = self.check(big, env={"DATA_ANALYST_ENV": "local"})
        self.assertEqual(r["route"], "run_setup", r)
        self.assertIn("bls", r["run_command"])
        self.assertIn("setup", r["run_command"])
        code, r = self.check(big, env={"DATA_ANALYST_ENV": "sandbox"})
        self.assertEqual(r["route"], "manual_download", r)
        self.assertTrue(r["manual_bundle"].replace("\\", "/").endswith("manual_download/bls"))
        with open(os.path.join(r["manual_bundle"], "manifest.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["api_version"], "v2")
        with open(os.path.join(r["manual_bundle"], "query_01.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["registrationkey"], "__BLS_KEY__")
        for name in ("download_bls.sh", "download_bls.ps1"):     # the key placeholder, never a key
            with open(os.path.join(r["manual_bundle"], name), encoding="ascii") as f:
                self.assertIn('"registrationkey": "__BLS_KEY__"', f.read())

    # ---------------------------------------------------------------- fetch
    def test_fetch_cpi(self):
        code, r, out = self.fetch(CPI)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api_keyless")
        rows = rows_of(out)
        self.assertEqual(list(rows[0])[:8], ["series_id", "label", "year", "period", "period_name", "freq", "date",
                                             "value"])
        self.assertEqual(pick(rows, "CUUR0000SA0", 2026, "M09")["value"], "336.94")
        gap = pick(rows, "CUUR0000SA0", 2025, "M10")
        self.assertEqual(gap["value"], "")
        self.assertEqual(gap["value_raw"], "-")
        self.assertIn("lapse in appropriations", gap["footnotes"])
        meta = rows_of(out, "series")[0]
        self.assertTrue(meta["title"].startswith("All items in U.S. city average"), meta)
        self.assertEqual(meta["title_source"], "data.bls.gov")
        self.assertEqual(meta["n_missing"], "1")
        d = os.path.join(out, "data", "bls")
        with open(os.path.join(d, "README.md"), encoding="utf-8") as f:
            self.assertIn("cannot vouch for the data", f.read())
        with open(os.path.join(d, "request.json"), encoding="utf-8") as f:
            req = json.load(f)
        self.assertEqual((req["route"], req["api_version"], req["source"]), ("api_keyless", "v1", "bls"))
        self.assertTrue(any(q["url"].startswith("https://api.bls.gov/publicAPI/v1/") for q in req["requests"]))
        self.assertTrue(os.path.exists(os.path.join(d, "raw", "response_01.json")))
        self.assertTrue(os.path.exists(os.path.join(out, "lib", "bls", "tsstats.py")))
        self.assertFalse(os.path.exists(os.path.join(out, "data", "bls_series.csv")))

    def test_fetch_jobs_preliminary(self):
        sp, out = self.write_spec(JOBS)
        code, r = self.cli(sp, "--out", out, "--no-describe")
        self.assertEqual(code, 0, r)
        rows = rows_of(out)
        self.assertEqual(pick(rows, "CES0000000001", 2026, "M09")["value"], "159044.0")
        self.assertEqual(pick(rows, "CES0000000001", 2026, "M09")["preliminary"], "1")
        self.assertEqual(pick(rows, "CES0000000001", 2026, "M08")["preliminary"], "1")
        self.assertEqual(pick(rows, "CES0000000001", 2026, "M07")["preliminary"], "0")
        self.assertEqual(pick(rows, "LNS14000000", 2026, "M09")["value"], "4.2")
        self.assertEqual(pick(rows, "LNS14000000", 2025, "M10")["value"], "")

    def test_fetch_built_ids(self):
        sp, out = self.write_spec(BUILD)
        code, r = self.cli(sp, "--out", out, "--no-describe")
        self.assertEqual(code, 0, r)
        rows = rows_of(out)
        self.assertEqual(pick(rows, "LASST240000000000003", 2026, "M09")["value"], "3.9")
        self.assertEqual(pick(rows, "LNS13327709", 2026, "M09")["value"], "7.6")        # U-6
        self.assertEqual(pick(rows, "LNS13023621", 2026, "M09")["value"], "3200.0")     # job losers, thousands
        self.assertEqual(pick(rows, "OEUN000000000000015125213", 2025, "A01")["value"], "135980.0")
        self.assertEqual(pick(rows, "OEUS240000000000015125213", 2025, "A01")["freq"], "A")
        self.assertTrue(any(r["series_id"] == "SMS24000000000000001" for r in rows))
        self.assertTrue(any(r["series_id"] == "LAUCN240050000000003" for r in rows))
        labels = {m["series_id"]: m["label"] for m in rows_of(out, "series")}
        self.assertEqual(labels["LASST240000000000003"], "Maryland unemployment rate (seasonally adjusted)")

    def test_reuse_existing(self):
        code, r, out = self.fetch(CPI)
        code, r = self.cli(os.path.join(out, "spec.json"), "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing", r)
        code, r = self.cli(os.path.join(out, "spec.json"), "--out", out)
        self.assertEqual(r["status"], "reused_existing", r)

    # ---------------------------------------------------------------- manual route
    def test_manual_bundle_and_ingest(self):
        code, r, out = self.fetch(CPI)
        raw = os.path.join(out, "data", "bls", "raw")
        out2 = os.path.join(self.tmp.name, "out2")
        os.makedirs(out2)
        sp2 = os.path.join(out2, "spec.json")
        with open(sp2, "w", encoding="utf-8") as f:
            json.dump(CPI, f)
        code, b = self.cli(sp2, "--out", out2, "--manual-bundle")
        self.assertEqual(code, 0, b)
        bd = b["manual_bundle"]
        for name in ("INSTRUCTIONS.md", "spec.json", "download_bls.sh", "download_bls.ps1"):
            self.assertTrue(os.path.exists(os.path.join(bd, name)), name)
        self.assertEqual(sorted(os.path.basename(p) for p in b["send_files"]),
                         ["INSTRUCTIONS.md", "download_bls.ps1", "download_bls.sh", "spec.json"])
        self.assertFalse(os.path.exists(os.path.join(bd, "download_bls.py")))
        self.assertNotIn("python", b["download_scripts"])
        self.assertIn("--source bls", b["next_command"])
        self.assertIn("--ingest", b["next_command"])
        with open(os.path.join(bd, "spec.json"), encoding="utf-8") as f:
            self.assertEqual([x["id"] for x in json.load(f)["series"]], ["CUUR0000SA0"])
        with open(os.path.join(bd, "INSTRUCTIONS.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("bash ~/Downloads/download_bls.sh", text)
        self.assertIn('powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\\Downloads\\download_bls.ps1"', text)
        self.assertIn("the folder you saved it in", text)
        self.assertNotIn("--ingest", text)
        self.assertNotIn("python", text.lower())
        # each script carries the requests (a quoted heredoc / a single-quoted here-string) and is ASCII
        for name, eol, block in (("download_bls.sh", b"\n", "<<'BLS_REQUEST_EOF'"),
                                 ("download_bls.ps1", b"\r\n", "$Embedded['query_01.json'] = @'")):
            with open(os.path.join(bd, name), "rb") as f:
                data = f.read()
            text = data.decode("ascii")
            self.assertNotIn("@QUERIES@", text, name)
            self.assertIn("CUUR0000SA0", text, name)
            self.assertIn(block, text, name)
            self.assertIn("manifest.json", text, name)
            self.assertEqual(data.count(b"\n"), data.count(eol), name)
        code, r = self.cli(sp2, "--out", out2, "--ingest", raw, "--no-describe")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        self.assertEqual(rows_of(out2), rows_of(out))

    def test_ingest_without_paths_or_uploads(self):
        sp, out = self.write_spec(CPI)
        code, r = self.cli(sp, "--out", out, "--ingest")
        self.assertEqual(code, 2, r)

    def test_restricted_fetch_blocks(self):
        sp, out = self.write_spec(CPI)
        code, r = self.cli(sp, "--out", out, "--env", "sandbox", env={"DATA_ACCESS_FORCE": "restricted"},
                           fixtures=False)
        self.assertEqual(code, 4, r)
        self.assertEqual(r["route"], "manual_download", r)
        self.assertIn("--ingest", r["next_command"])

    # ---------------------------------------------------------------- offline commands
    def test_build_search_decode(self):
        code, r = self.cli("--build", "laus", "--county", "24005", "--measure", "labor_force")
        self.assertEqual(r["series"][0]["id"], "LAUCN240050000000006")
        code, r = self.cli("--build", "oews", "--occupation", "29-1141", "--measure", "hourly_mean_wage")
        self.assertEqual(r["series"][0]["id"], "OEUN000000000000029114103")
        code, r = self.cli("--build", "ces_state", "--state", "California", "--seasonal", "U")
        self.assertEqual(r["series"][0]["id"], "SMU06000000000000001")
        code, r = self.cli("--build", "jolts", "--industry", "all", "--measure", "quits")
        ids = [x["id"] for x in r["series"]]
        self.assertEqual((len(ids), ids[2], ids[-1]), (28, "JTS110099000000000QUR", "JTS929000000000000QUR"))
        code, r = self.cli("--build", "jolts", "--industry", "Retail trade", "--measure", "hires", "--seasonal", "U")
        self.assertEqual(r["series"][0]["id"], "JTU440000000000000HIR")
        code, r = self.cli("--build", "jolts", "--industry", "999999")
        self.assertEqual(code, 2)
        code, r = self.cli("--build", "laus", "--state", "Atlantis")
        self.assertEqual(code, 2)
        code, r = self.cli("--search", "u-6")
        self.assertEqual(r["results"][0]["id"], "LNS13327709")
        for q, sid in (("import prices", "EIUIR"), ("education unemployment", "LNS14027659"),
                       ("union membership", "LUU0204899600")):
            code, r = self.cli("--search", q)
            self.assertIn(sid, [x["id"] for x in r["results"]], q)
        code, r = self.cli("--decode", "CUUR0000SA0")
        self.assertEqual(r["series"][0]["seasonal"], "not seasonally adjusted")


class TestBlsInProcess(unittest.TestCase):
    """Route logic with a key, redaction and the usage counter, without network or invented responses."""

    def setUp(self):
        sys.path.insert(0, PACK)
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("BLS_API_KEY", "DATA_ANALYST_FIXTURES", "HOME", "USERPROFILE",
                                                     "CLAUDE_CONFIG_DIR", "DATA_ACCESS_FORCE")}
        os.environ["DATA_ANALYST_FIXTURES"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        os.environ.update(HOME=self.tmp.name, USERPROFILE=self.tmp.name,
                          CLAUDE_CONFIG_DIR=os.path.join(self.tmp.name, ".claude"))
        os.environ.pop("DATA_ACCESS_FORCE", None)
        self.fake = "0123456789abcdef0123456789abcdef"            # a made-up key, never sent anywhere
        os.environ["BLS_API_KEY"] = self.fake
        self.api = load("bls_api", "bls_api.py")
        self.f = load("bls_pack_fetch", "fetch.py")
        self.s = self.f.norm_spec(CPI)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _check(self, ping, env="local"):
        orig = self.api.BlsClient.ping
        self.f.BlsClient.ping = lambda self_: ping
        try:
            return self.f.route_check(self.s, os.path.join(self.tmp.name, "out"), env)
        finally:
            self.f.BlsClient.ping = orig

    def test_key_accepted_is_api(self):
        r = self._check({"reachable": True, "auth_ok": True, "kind": "ok", "detail": ""})
        self.assertEqual(r["route"], "api")
        self.assertNotIn("--no-key", r["next_command"])

    def test_key_rejected_falls_back_keyless(self):
        r = self._check({"reachable": True, "auth_ok": False, "kind": "auth",
                         "detail": f"The key: {self.fake} provided by the User is invalid."})
        self.assertEqual(r["route"], "api_keyless")
        self.assertIn("--no-key", r["next_command"])
        self.assertIn("setup", r["run_command"])
        self.assertNotIn(self.fake, self.api.redact(json.dumps(r)))

    def test_local_tally_warns_but_does_not_block(self):
        # the tally counts this machine's queries; BLS counts its own (it served a query at tally 36 [2026-10-22])
        from datetime import date
        os.environ.pop("BLS_API_KEY", None)
        with open(self.api._usage_file(), "w", encoding="utf-8") as f:
            json.dump({date.today().isoformat(): {"v1": 36}}, f)
        orig = self.f.http_reachable
        self.f.http_reachable = lambda url: (True, "")
        try:
            r = self.f.route_check(self.s, os.path.join(self.tmp.name, "out"), "local")
        finally:
            self.f.http_reachable = orig
        self.assertEqual(r["route"], "api_keyless", r)
        self.assertIn("tally already shows 36", r["message"])

    def test_busy_site_is_an_outage(self):
        # BLS answers 503 "The BLS website is busy" (an HTML page) [live 2026-10-22]: an outage, not a spec error
        from core.http import DataError
        c = self.api.BlsClient(use_key=False)
        def busy(*a, **k):
            raise DataError("bls: HTTP 503: <html>The BLS website is busy</html>", kind="http", status=503)
        c.http.request = busy
        with self.assertRaises(self.api.BlsError) as cm:
            c._post({"seriesid": ["LNS14000000"]})
        self.assertEqual(cm.exception.kind, "unavailable")
        self.assertNotIn("<html>", str(cm.exception))

    def test_describe_decodes_oews_without_catalog(self):
        class Page:                         # data.bls.gov pages for OEWS IDs carry no catalog table [live 2026-10-21]
            def text(self):
                return "<html><body><h1>Databases, Tables &amp; Calculators</h1></body></html>"
        orig = self.api._DESCRIBE.get
        self.api._DESCRIBE.get = lambda url: Page()
        try:
            oe = self.api.describe("OEUS060000000000029114113")
            other = self.api.describe("LNS14000000")
        finally:
            self.api._DESCRIBE.get = orig
        self.assertNotIn("error", oe)
        self.assertEqual((oe["fields"]["Occupation code (SOC)"], oe["fields"]["Data type"]), ("29-1141", "annual median wage"))
        self.assertIn("not verified", oe["note"])
        self.assertIn("error", other)        # any other survey without a catalog is still "not found"

    def test_describe_points_fred_metro_cpi_ids_to_bls_codes(self):
        class Page:                         # data.bls.gov: "Invalid Series ID: <id>" [live 2026-10-22]
            def text(self):
                return "<html><body>ERROR: Invalid Series ID: CUURA210SA0</body></html>"
        orig = self.api._DESCRIBE.get
        self.api._DESCRIBE.get = lambda url: Page()
        try:
            fred_id = self.api.describe("CUURA210SA0")
            other = self.api.describe("CUUR0000SEHA")
        finally:
            self.api._DESCRIBE.get = orig
        self.assertIn("cu.area", fred_id["error"])
        self.assertIn("A210", fred_id["error"])         # echoes the old area code to look up
        self.assertNotIn("cu.area", other["error"])

    def test_quota_is_report_error(self):
        r = self._check({"reachable": True, "auth_ok": None, "kind": "quota", "detail": "daily threshold"})
        self.assertEqual(r["route"], "report_error")

    def test_redacts_echoed_32_hex(self):
        other = "fedcba9876543210fedcba9876543210"                # not a known key: caught by the pattern
        msg = self.api.redact(f"The key: {other} provided by the User is invalid. sha {'ab' * 32}")
        self.assertNotIn(other, msg)
        self.assertIn("ab" * 32, msg)                              # a 64-hex digest is left alone
        self.assertNotIn(self.fake, self.api.redact(f"key={self.fake}"))

    def test_usage_counter_in_home(self):
        p = self.api._usage_file()
        self.assertTrue(p.startswith(self.tmp.name), p)
        self.api.usage_today("v1")

    def test_keycheck(self):
        keycheck = load("bls_pack_keycheck", "keycheck.py")
        orig = keycheck.BlsClient.ping
        try:
            keycheck.BlsClient.ping = lambda self_: {"reachable": True, "auth_ok": False, "kind": "auth",
                                                     "detail": f"key {self.fake} invalid"}
            ok, detail = keycheck.verify(self.fake)
            self.assertIs(ok, False)
            self.assertNotIn(self.fake, detail)
            keycheck.BlsClient.ping = lambda self_: {"reachable": True, "auth_ok": True, "kind": "ok", "detail": ""}
            self.assertIs(keycheck.verify(self.fake)[0], True)
        finally:
            keycheck.BlsClient.ping = orig

    def test_tsstats_reads_pack_layout(self):
        sys.path.insert(0, os.path.join(PACK, "lib"))
        import tsstats
        out = os.path.join(self.tmp.name, "o")
        os.makedirs(os.path.join(out, "data", "bls"))
        with open(os.path.join(out, "data", "bls", "bls_series.csv"), "w", encoding="utf-8") as f:
            f.write("series_id,label,year,period,period_name,freq,date,value,value_raw,preliminary,footnote_codes,"
                    "footnotes,latest\nX,,2026,M01,January,M,2026-01-01,1.0,1.0,0,,,0\n"
                    "X,,2026,M03,March,M,2026-03-01,2.0,2.0,0,,,1\n")
        m = tsstats.wide(tsstats.load(out), "M")
        self.assertEqual(len(m), 3)                                # February kept as a gap
        self.assertTrue(m["X"].isna().iloc[1])

    def test_tsstats_compare_published_scale(self):
        sys.path.insert(0, os.path.join(PACK, "lib"))
        import tsstats
        pd = tsstats.pd
        idx = pd.date_range("2026-01-01", periods=3, freq="MS")
        with self.assertRaises(ValueError):                    # share (0.029) vs BLS percent (2.9)
            tsstats.compare_published(pd.Series([0.029, 0.03, 0.031], idx), pd.Series([2.9, 3.0, 3.1], idx), 0.05)
        ok = tsstats.compare_published(pd.Series([2.91, 3.0, 3.1], idx), pd.Series([2.9, 3.0, 3.1], idx), 0.05)
        self.assertTrue(ok["ok"].all())
        # a metro CPI priced every other month: 12-month change compares the same month a year earlier
        bi = pd.Series([100.0 + i for i in range(13)], pd.date_range("2024-02-01", periods=13, freq="2MS"))
        self.assertEqual(tsstats.infer_freq(bi), "B")
        self.assertAlmostEqual(float(tsstats.yoy(bi).iloc[-1]), 112 / 106 - 1)

    def test_catalog_youth_ids(self):
        # LNS14000036 is the 20-24 rate (data.bls.gov title, checked 2026-10-21); 16-24 is LNS14024887
        sys.path.insert(0, os.path.join(PACK, "lib"))
        import series_ids
        cat = {sid: label for sid, label, *_ in series_ids.CATALOG}
        self.assertIn("16 to 24", cat["LNS14024887"])
        self.assertIn("20 to 24", cat["LNS14000036"])


if __name__ == "__main__":
    unittest.main()

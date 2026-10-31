"""Offline tests for the ipums deep pack.

Fixtures (fixtures):
- ping_*.json, samples_cps_p*.json: real IPUMS API v2 metadata responses (recorded October 2026).
- extract_cps_11.json: a real GET /extracts/11 response, with two personal fields scrubbed: the account
  e-mail ("[REDACTED]") and the account number in the download URLs ("0000000").
- ddi_cps_00011.xml: the real DDI codebook of that extract, trimmed to six variables (documentation,
  not microdata).
- synthetic_cps_00011.csv.gz: SYNTHETIC rows in the IPUMS CSV layout (see synthetic_README.txt).
  The IPUMS terms forbid redistributing extracts, so no real microdata are stored here; because its
  checksum differs from the one IPUMS reported, it also tests the checksum guard.
- samples_nhis_p1.json, samples_meps_p1.json: real sample listings (October 2026).
- ddi_nhis_00001.xml: the real DDI of an NHIS 2024 extract, trimmed to its eleven variables;
  synthetic_nhis_00001.csv.gz: SYNTHETIC rows in that layout.
"""
import gzip
import json
import os
import shutil
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest, load_pack_module  # noqa: E402

SPEC = {"collection": "cps",
        "description": "Employment and wages of people ages 25-54 by sex and education, CPS ASEC 2023 (smoke test)",
        "samples": ["cps2023_03s"],
        "variables": ["AGE", "SEX", "EDUC", "EMPSTAT", "CLASSWLY", "INCWAGE", "WKSWORK1", "UHRSWORKLY"],
        "case_selections": {"AGE": {"min": 25, "max": 54}}}
FAKE_KEY = {"IPUMS_API_KEY": "offline-test-key-not-real"}
SPEC_NHIS = {"collection": "nhis", "description": "Uninsured adults, NHIS 2024 (smoke test)", "samples": ["ih2024"],
             "variables": ["AGE", "EDUC", "EMPSTAT", "USBORN", "HINOTCOVE"]}


def _stats(pack_dir):
    import importlib.util          # by path: ipums and opm both have a lib/stats.py
    sys.path.insert(0, os.path.join(pack_dir, "lib"))
    spec = importlib.util.spec_from_file_location("ipums_stats_t", os.path.join(pack_dir, "lib", "stats.py"))
    stats = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stats)
    return stats


class TestIpums(PackTest):
    source = "ipums"
    sample_spec = SPEC

    def _uploads(self):
        """The fixture files under the names IPUMS gives them (cps_00011.csv.gz, cps_00011.xml)."""
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up, exist_ok=True)
        pairs = (("synthetic_cps_00011.csv.gz", "cps_00011.csv.gz"), ("ddi_cps_00011.xml", "cps_00011.xml"))
        for src, dst in pairs:
            shutil.copy(os.path.join(self.fixtures, src), os.path.join(up, dst))
        return [os.path.join(up, dst) for _, dst in pairs]

    def test_dry_run_body(self):
        sp, out = self.write_spec(SPEC)
        code, body = self.cli(sp, "--out", out, "--dry-run")
        self.assertEqual(code, 0, body)
        self.assertEqual(body["samples"], {"cps2023_03s": {}})
        self.assertEqual(body["variables"]["AGE"]["caseSelections"]["general"][0], "25")
        self.assertEqual(len(body["variables"]["AGE"]["caseSelections"]["general"]), 30)
        self.assertEqual(body["dataStructure"], {"rectangular": {"on": "P"}})

    def test_check_api(self):
        code, r = self.check(SPEC, env=FAKE_KEY)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api", r)
        self.assertEqual(r["source"], "ipums")
        self.assertIn("scripts", r["next_command"])
        self.assertIn("--source ipums", r["next_command"])
        self.assertNotIn("offline-test-key", json.dumps(r))

    def test_check_no_key_local_is_run_setup(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--check", "--env", "local")
        self.assertEqual(r["route"], "run_setup", r)
        self.assertIn("-Source ipums" if sys.platform == "win32" else "setup.sh\" ipums", r["next_command"])
        self.assertTrue(os.path.exists(os.path.join(out, "manual_download", "ipums", "INSTRUCTIONS.md")))

    def test_check_cloud_no_key(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--check", "--env", "cloud")
        self.assertEqual(r["route"], "cloud_secret", r)
        self.assertIn("--ingest", r["next_command"])

    def test_restricted_sandbox_bundle(self):
        from testkit import run
        sp, out = self.write_spec(SPEC)
        code, r = run(["--source", "ipums", sp, "--out", out, "--check", "--env", "sandbox"], None,
                      {"DATA_ACCESS_FORCE": "restricted", **FAKE_KEY}, self.home)
        self.assertEqual(r["route"], "manual_download", r)
        b = r["manual_bundle"]
        self.assertTrue(b.endswith(os.path.join("manual_download", "ipums")), b)
        for f in ("INSTRUCTIONS.md", "download_ipums.sh", "download_ipums.ps1", "extract_spec.json", "manifest.json"):
            self.assertTrue(os.path.exists(os.path.join(b, f)), f)
        with open(os.path.join(b, "download_ipums.sh"), encoding="utf-8") as f:
            sh = f.read()
        self.assertIn('"cps2023_03s"', sh)                 # the definition is built into the script
        self.assertNotIn("@SPEC_JSON@", sh)
        with open(os.path.join(b, "INSTRUCTIONS.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Option A: the IPUMS website", text)      # no key: the website comes first
        self.assertIn("codes 25 through 54", text)
        self.assertIn("bash ~/Downloads/download_ipums.sh", text)     # one command per system
        self.assertIn(r'-File "$HOME\Downloads\download_ipums.ps1"', text)
        self.assertNotIn("python", text.lower())                      # no analyst commands for the user
        self.assertEqual(r["user_commands"]["bash"], "bash ~/Downloads/download_ipums.sh")
        self.assertIn("extract_spec.json", r["action"])
        self.assertIn("--ingest", r["next_command"])
        self.assertLess(len(json.dumps(r)), 3500)

    def test_fix_spec(self):
        code, r = self.check({"collection": "cps", "samples": [], "variables": ["AGE"]})
        self.assertEqual(code, 2, r)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_ingest_supersedes_earlier_codebook(self):
        # a second extract's DDI replaces the first's: both would ship with *.xml
        sp, out = self.write_spec(SPEC)
        d = os.path.join(out, "data", "ipums")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cps_00009.xml"), "w", encoding="utf-8") as f:
            f.write("<codeBook/>")
        code, r = self.cli(sp, "--out", out, "--ingest", *self._uploads())
        self.assertEqual(code, 0, r)
        xmls = [x for x in os.listdir(d) if x.endswith(".xml")]
        self.assertNotIn("cps_00009.xml", xmls)
        self.assertTrue(os.path.exists(os.path.join(d, "cps_00009.xml.superseded")))

    def test_ingest_package(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--ingest", *self._uploads())
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        self.assertEqual(r["rows"], 3)
        d = os.path.join(out, "data", "ipums")
        rows = self.csv(out, "ipums_extract")
        self.assertEqual(list(rows[0]), ["YEAR", "SERIAL", "ASECWT", "AGE", "SEX", "INCWAGE"])
        self.assertEqual(rows[0]["INCWAGE"], "52000")
        code_rows = self.csv(out, "codebook")
        sex = {c["value"]: c["value_label"] for c in code_rows if c["variable"] == "SEX"}
        self.assertEqual(sex.get("2"), "Female")              # from the real DDI
        with open(os.path.join(d, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("DOI 10.18128/D030.V13.0", readme)
        self.assertIn("definition_only", readme)
        with open(os.path.join(d, "request.json"), encoding="utf-8") as f:
            req = json.load(f)
        self.assertEqual(req["route"], "manual_download")
        self.assertEqual(req["tables"][0]["file"], "ipums_extract.csv")
        with open(os.path.join(d, "extract_spec.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["extract_number"], 11)
        # the same request is now reused, not downloaded again
        code, r = self.cli(sp, "--out", out, "--check", env=FAKE_KEY)
        self.assertEqual(r["route"], "reuse_existing", r)

    def test_ingest_from_folder(self):
        sp, out = self.write_spec(SPEC)
        up = os.path.dirname(self._uploads()[0])
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["extract"], "cps:11")

    def test_resume_detected_and_checksum_guard(self):
        sp, out = self.write_spec(SPEC)
        d = os.path.join(out, "data", "ipums")
        os.makedirs(d)
        sys.path.insert(0, self.pack_dir)
        import importlib.util
        spec = importlib.util.spec_from_file_location("ipums_fetch_t", os.path.join(self.pack_dir, "fetch.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with open(os.path.join(d, "extract_spec.json"), "w", encoding="utf-8") as f:
            json.dump({"collection": "cps", "extract_number": 11, "spec": SPEC, "api_body": mod.build_body(SPEC),
                       "status": "submitted"}, f)
        code, r = self.cli(sp, "--out", out, "--check", env=FAKE_KEY)
        self.assertEqual(r["route"], "api", r)
        self.assertEqual(r["resume"], "cps:11")
        self.assertIn("--resume cps:11", r["next_command"])
        # collecting extract 11: the synthetic file's checksum differs from the one IPUMS reported: refused
        code, r = self.cli("--resume", "cps:11", "--out", out, env=FAKE_KEY)
        self.assertEqual(code, 2, r)
        self.assertIn("Checksum mismatch", r["message"])
        self.assertFalse(os.path.exists(os.path.join(d, "ipums_extract.csv")))

    def test_list_samples(self):
        from testkit import run
        import subprocess
        e = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        e.update(DATA_ANALYST_FIXTURES=self.fixtures, **FAKE_KEY)
        from testkit import FETCH
        p = subprocess.run([sys.executable, FETCH, "--source", "ipums", "--list-samples", "cps", "--match", "ASEC 2023"],
                           capture_output=True, text=True, env=e)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("cps2023_03s\tIPUMS-CPS, ASEC 2023", p.stdout)

    def test_manual_bundle_and_reproduce(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        self.assertTrue(os.path.exists(r["instructions"]))
        rep = os.path.join(out, "reproduce")
        code, r = self.cli(sp, "--out", out, "--reproduce-bundle", rep)
        self.assertEqual(code, 0, r)
        self.assertEqual(sorted(r["files"]), ["DOWNLOAD.md", "download_ipums.ps1", "download_ipums.sh",
                                              "extract_spec.json"])
        self.assertEqual(r["scripts"], {"bash": "download_ipums.sh", "powershell": "download_ipums.ps1"})
        for name in ("download_ipums.sh", "download_ipums.ps1"):      # each works alone: the definition is built in
            with open(os.path.join(rep, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn('"cps2023_03s"', text)
            self.assertNotIn("SPEC_FILE", text)
            self.assertNotIn("SpecFile", text)
            self.assertIn("BAKED_NUMBER=\"\"" if name.endswith(".sh") else "$BakedNumber = ''", text)
        with open(os.path.join(rep, "DOWNLOAD.md"), encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("bash download_ipums.sh", doc)
        self.assertIn("built in", doc)

    def test_policy_microdata_rules(self):
        from core import policy
        p = policy.load(self.pack_dir)
        for f in ("data/ipums/ipums_extract.csv", "data/ipums/cps_00011.csv.gz", "usa_00003.dat.gz"):
            self.assertTrue(policy.is_microdata(p, f), f)
        for f in ("data/ipums/codebook.csv", "data/ipums/extract_spec.json", "data/ipums/README.md"):
            self.assertFalse(policy.is_microdata(p, f), f)

    def test_keycheck_offline(self):
        os.environ["DATA_ANALYST_FIXTURES"] = self.fixtures
        try:
            sys.path.insert(0, self.pack_dir)
            import importlib.util
            spec = importlib.util.spec_from_file_location("ipums_keycheck_t", os.path.join(self.pack_dir, "ipums_api.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            ok, detail = mod.verify("offline-test-key-not-real")
            self.assertTrue(ok, detail)
            self.assertNotIn("offline-test-key", detail)
        finally:
            os.environ.pop("DATA_ANALYST_FIXTURES", None)

    def test_stats_lib(self):
        stats = _stats(self.pack_dir)
        import pandas as pd
        with gzip.open(self._uploads()[0], "rt") as f:
            df = pd.read_csv(f)
        df = stats.clean_missing(df, "cps")
        self.assertTrue(pd.isna(df.loc[2, "INCWAGE"]))          # 99999999 = NIU in CPS
        # a value label of exactly "N/A" (ACS VEHICLES 0: group quarters, vacant) is a missing code, not NaN
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cb = os.path.join(d, "codebook.csv")
            with open(cb, "w", encoding="utf-8", newline="") as f:
                f.write("variable,label,value,value_label\nVEHICLES,Vehicles available,0,N/A\n"
                        "VEHICLES,Vehicles available,1,1 available\nVEHICLES,Vehicles available,9,No vehicles available\n")
            self.assertEqual(stats.missing_codes(cb).get("VEHICLES"), [0.0])
        self.assertAlmostEqual(stats.wmean(df["AGE"], df["ASECWT"]),
                               (30 * 1500.25 + 28 * 1500.25 + 45 * 980.5) / (2 * 1500.25 + 980.5))
        # heaped dollars: the plain median sits on the heap; Census interpolates within $2,500 intervals
        x, w = pd.Series([50000, 60000, 60000, 60000, 70000, 80000.0]), pd.Series([1.0] * 6)
        self.assertEqual(stats.wmedian(x, w), 60000.0)
        self.assertAlmostEqual(stats.wmedian_binned(x, w), 60000 + 2500 * 2 / 3)
        # BLS weekly earnings: $50 intervals centred on multiples of $50 ([1,225, 1,275) holds 1,250)
        e = pd.Series([1200, 1250, 1268, 1300, 1330.0])
        self.assertAlmostEqual(stats.wmedian_binned(e, pd.Series([1.0] * 5), 50, 25), 1225 + 50 * 1.5 / 2)
        self.assertTrue(pd.isna(stats.clean_missing(pd.DataFrame({"EARNWEEK2": [999999.99]}), "cps").loc[0, "EARNWEEK2"]))
        # NIU rows (NaN after clean_missing) leave the denominator when share() names the variable
        u = pd.DataFrame({"ANYCOVLY": [1, 2, 2, None], "w": [1.0, 1.0, 1.0, 1.0]})
        self.assertAlmostEqual(stats.share(lambda d: d.ANYCOVLY == 1, col="ANYCOVLY")(u, "w"), 1 / 3)
        self.assertAlmostEqual(stats.share(lambda d: d.ANYCOVLY == 1)(u, "w"), 1 / 4)     # the trap, kept as is
        # unused category levels would make the dummies collinear
        d = pd.DataFrame({"g": pd.Categorical(["a", "b", "a"], categories=["a", "b", "c"])})
        self.assertEqual(list(stats.design_matrix(d, categorical=["g"]).columns), ["const", "g[b]"])
        # CPS basic monthly records have no ASECWT: rep_se falls back to WTFINL
        m = pd.DataFrame({"y": [1.0, 2.0], "WTFINL": [1.0, 1.0], "ASECWT": [None, None],
                          "REPWTP1": [1.0, 1.0], "REPWTP2": [1.0, 1.0]})
        est, se = stats.rep_se(m, lambda d, wt: stats.wmean(d["y"], d[wt]), "cps")
        self.assertEqual((est, se), (1.5, 0.0))

    def test_health_survey_recodes(self):
        stats = _stats(self.pack_dir)
        import pandas as pd
        with gzip.open(os.path.join(self.fixtures, "synthetic_nhis_00001.csv.gz"), "rt") as f:
            df = stats.clean_missing(pd.read_csv(f), "nhis")
        self.assertTrue(pd.isna(df.loc[2, "AGE"]))                      # 997 unknown (no DDI labels for AGE)
        self.assertEqual(list(stats.emp_status(df, "nhis")), ["employed", "not employed", None, None])
        labels = lambda c: [x if isinstance(x, str) else None for x in c]   # noqa: E731 (unassigned: NaN)
        self.assertEqual(labels(stats.educ_group(df, "nhis"))[:3], ["Bachelor's degree or more", "High school diploma", None])
        self.assertEqual(list(stats.foreign_born(df, "nhis")), [False, True, None, False])
        # CPS: foreign born is CITIZEN 4/5 (BLS), not NATIVITY 5, which also holds CITIZEN 2 (outlying areas)
        # and 3 (born abroad of American parents)
        cps = pd.DataFrame({"NATIVITY": [5, 5, 5, 5, 1, 0], "CITIZEN": [2, 3, 4, 5, 1, 4], "WTFINL": 1.0})
        self.assertEqual(list(stats.foreign_born(cps)), [False, False, True, True, False, True])
        m = pd.DataFrame({"EDUC": [116, 500, 510, 530, 522], "AGE": [8, 50, 50, 50, 70]})
        self.assertEqual(labels(stats.educ_group(m, "meps", "educ5")),
                         ["High school diploma or less", None, "Master's degree", None, "Professional degree or doctorate"])
        self.assertEqual(list(stats.educ_group(m, "meps"))[1], "Bachelor's degree or more")   # 500: graduate, no detail
        self.assertTrue(pd.isna(stats.clean_missing(pd.DataFrame({"AGE": [996]}), "meps").loc[0, "AGE"]))
        # no codes for a collection: refuse rather than borrow another collection's codes
        for call in (lambda: stats.emp_status(df, "meps"), lambda: stats.educ_group(df, "atus"),
                     lambda: stats.foreign_born(df), lambda: stats.rep_se(df, lambda d, w: 0.0, "nhis")):
            with self.assertRaises(ValueError):
                call()
        # design-based SE with NHIS STRATA/PSU on the whole file, the sub-population by mask
        d = df.assign(y=[1, 0, 1, 0])
        r, se = stats.se_estimate(d, stats.share_z(d, d.y == 1, d.ASTATFLG == 1, "SAMPWEIGHT"), strata="STRATA",
                                  cluster="PSU")
        self.assertAlmostEqual(r, 8000 / 15000)
        self.assertGreater(se, 0)
        # survey_design: same SE for one year; strata kept apart across NHIS design periods
        z = stats.share_z(d, d.y == 1, stats.sample_adults(d), "SAMPWEIGHT")
        self.assertAlmostEqual(stats.se_estimate(d, z, **stats.survey_design(d, "nhis"))[1], se)
        p = pd.DataFrame({"YEAR": [2014, 2017], "STRATA": [5, 5], "PSU": [1, 1]})
        stats.survey_design(p, "nhis")
        self.assertEqual(p.DSTRATA.nunique(), 2)
        self.assertEqual(p.DPSU.nunique(), 2)
        with self.assertWarns(UserWarning):
            stats.survey_design(p.assign(YEAR=[2018, 2019]), "nhis")
        mp = pd.DataFrame({"YEAR": [2021, 2022], "STRATANN": [1, 1], "PSUANN": [1, 2]})
        with self.assertRaises(ValueError):
            stats.survey_design(mp, "meps")                              # pooled MEPS needs STRATAPLD/PSUPLD
        self.assertEqual(stats.survey_design(mp.iloc[:1].copy(), "meps"), {"strata": "DSTRATA", "cluster": "DPSU"})
        # uninsured: 1 / 0 / NaN; HINOTCOVE survives clean_missing even when the codebook labels 0 NIU
        self.assertEqual(list(stats.uninsured(df, "nhis").fillna(-1)), [0.0, 1.0, -1, 0.0])
        self.assertEqual(list(stats.uninsured(pd.DataFrame({"HCOVANY": [1, 2]}), "usa")), [1.0, 0.0])
        cb = pd.DataFrame({"variable": ["HINOTCOVE"], "value": ["0"], "value_label": ["NIU"]})
        self.assertEqual(stats.clean_missing(pd.DataFrame({"HINOTCOVE": [0]}), "nhis", codebook=cb).loc[0, "HINOTCOVE"], 0)
        self.assertTrue(pd.isna(stats.clean_missing(pd.DataFrame({"INCTOT": [9999999.98]}), "meps").loc[0, "INCTOT"]))
        self.assertEqual(stats.default_weight("nhis", [2018, 2024]), "SAMPWEIGHT")
        self.assertEqual(stats.default_weight("meps", universe="saq"), "SAQWEIGHT")
        with self.assertRaises(ValueError):
            stats.uninsured(df, "cps")

    def test_health_survey_spec_and_ingest(self):
        bad = dict(SPEC_NHIS, case_selections={"AGE": {"min": 18, "max": 64}}, adjust_monetary_values=["X"])
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertEqual(sum("available only for usa, cps, ipumsi" in p for p in json.dumps(r).split("\\n")), 1)
        self.assertIn("case_selections", json.dumps(r))
        self.assertIn("domain mask", json.dumps(r))
        self.assertIn("CPI-U", json.dumps(r))
        # options the API refuses for one health survey but not the other
        for spec, word in ((dict(SPEC_NHIS, data_quality_flags=True), "data_quality_flags"),
                           (dict(SPEC_NHIS, collection="meps", samples=["mp2022"],
                                 attached_characteristics={"EDUC": ["spouse"]}), "attached_characteristics"),
                           (dict(SPEC_NHIS, data_structure={"rectangular": {"on": "R"}}), "rectangular on 'R'")):
            code, r = self.check(spec)
            self.assertEqual(r["route"], "fix_spec", r)
            self.assertIn(word, json.dumps(r))
        code, r = self.check(dict(SPEC_NHIS, data_structure={"rectangular": {"on": "I"}}), env=FAKE_KEY)
        self.assertNotEqual(r["route"], "fix_spec", r)
        sp, out = self.write_spec(SPEC_NHIS)
        up = os.path.join(self.tmp.name, "uploads_nhis")
        os.makedirs(up)
        for src, dst in (("synthetic_nhis_00001.csv.gz", "nhis_00001.csv.gz"), ("ddi_nhis_00001.xml", "nhis_00001.xml")):
            shutil.copy(os.path.join(self.fixtures, src), os.path.join(up, dst))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        self.assertEqual((r["extract"], r["rows"]), ("nhis:1", 4))
        cov = {c["value"]: c["value_label"] for c in self.csv(out, "codebook") if c["variable"] == "HINOTCOVE"}
        self.assertEqual(cov.get("2"), "Yes, has no coverage")           # from the real DDI
        with open(os.path.join(out, "data", "ipums", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("IPUMS NHIS", readme)
        self.assertIn("SAMPWEIGHT", readme)
        f = load_pack_module("ipums", "fetch.py")
        self.assertEqual((f.sample_label("ih2024"), f.sample_label("mp2022")), ("2024 NHIS", "2022 MEPS"))
        self.assertEqual(f.approx_rows(["ih2024", "mp2022"]), 45_000 + 25_000)
        self.assertIn("0 = NIU", readme)                                  # NHIS missing-code hint, not USA's
        rounds = f.build_body(dict(SPEC_NHIS, collection="meps", samples=["mp2022"],
                                   data_structure={"rectangular": {"on": "R"}}))
        self.assertIn("**Rectangular (round)**", f.web_section("meps", rounds))

    def test_list_samples_health(self):
        import subprocess
        from testkit import FETCH
        e = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        e.update(DATA_ANALYST_FIXTURES=self.fixtures, **FAKE_KEY)
        for c, line in (("nhis", "ih2024\t2024 NHIS"), ("meps", "mp2022\t2022 MEPS")):
            p = subprocess.run([sys.executable, FETCH, "--source", "ipums", "--list-samples", c],
                               capture_output=True, text=True, env=e)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(line, p.stdout)

    def test_ddi_special_codes_of_continuous_variables(self):
        # INCBUS has no category rows; its NIU code is only in the coding notes [ASEC 2026 DDI, 2026-10-21]
        f = load_pack_module("ipums", "fetch.py")
        xml = os.path.join(self.tmp.name, "x.xml")
        with open(xml, "w", encoding="utf-8") as fh:
            fh.write('<codeBook><dataDscr><var name="INCBUS"><labl>Non-farm business income</labl>'
                     '<txt>INCBUS indicates net business income.</txt>'
                     '<codInstr>Codes99999999 = N.I.U. (Not in Universe). 99999998 = Missing. (1962-1964 only)'
                     '</codInstr></var><var name="SEX"><labl>Sex</labl><catgry><catValu>1</catValu><labl>Male</labl>'
                     '</catgry></var></dataDscr></codeBook>')
        _, codes, _ = f.parse_ddi(xml)
        got = {(c["variable"], c["value"]) for c in codes}
        self.assertEqual(got, {("INCBUS", "99999999"), ("INCBUS", "99999998"), ("SEX", "1")})
        with open(xml, "w", encoding="utf-8") as fh:          # a decimal code is one code, not "99" [EARNWEEK2]
            fh.write('<codeBook><dataDscr><var name="EARNWEEK2"><labl>Weekly earnings</labl>'
                     '<codInstr>999999.99 = N.I.U. (Not in Universe).</codInstr></var></dataDscr></codeBook>')
        _, codes, _ = f.parse_ddi(xml)
        self.assertEqual({(c["variable"], c["value"]) for c in codes}, {("EARNWEEK2", "999999.99")})


if __name__ == "__main__":
    unittest.main()

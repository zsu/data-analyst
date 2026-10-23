"""The claude.ai upload zip (tools/package_skill.py) stays under claude.ai's file limit and works as built:
policy.json and endpoints.json come from sources/packs.json there, not from each pack's folder."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import testkit

ROOT = testkit.ROOT
MAX_FILES = 200                     # claude.ai: "Zip contains too many files (maximum 200)"


def _load_packager():
    spec = importlib.util.spec_from_file_location("package_skill_t", os.path.join(ROOT, "tools", "package_skill.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="da_pkg_")
        cls.zip = os.path.join(cls.tmp, "data-analyst.zip")
        cls.n = _load_packager().build(cls.zip)
        with zipfile.ZipFile(cls.zip) as z:
            cls.names = z.namelist()
            z.extractall(os.path.join(cls.tmp, "x"))
        cls.skill = os.path.join(cls.tmp, "x", "data-analyst")
        cls.fetch = os.path.join(cls.skill, "scripts", "fetch.py")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_file_limit(self):
        self.assertEqual(self.n, len(self.names))
        self.assertLessEqual(len(self.names), MAX_FILES, "the upload zip has too many files for claude.ai")
        self.assertFalse([x for x in self.names if x.endswith("/")], "no folder entries")
        self.assertIn("data-analyst/sources/packs.json", self.names)
        self.assertFalse([x for x in self.names if x.endswith(("/policy.json", "/endpoints.json"))])

    def test_bundle_matches_repo(self):
        with open(os.path.join(self.skill, "sources", "packs.json"), encoding="utf-8") as f:
            b = json.load(f)
        ids = sorted(os.listdir(os.path.join(testkit.SKILL, "sources")))
        self.assertEqual(sorted(b), ids)
        for pid in ids:
            for name in ("policy.json", "endpoints.json"):
                p = os.path.join(testkit.pack_dir(pid), name)
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        self.assertEqual(b[pid][name], json.load(f), f"{pid}/{name}")

    def test_every_pack_loads_from_bundle(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from core import packs, policy\n"
                "ps = packs.all_packs()\n"
                "bad = {p.id: policy.validate(p.policy, p.dir) for p in ps if policy.validate(p.policy, p.dir)}\n"
                "print(len(ps), sum(p.endpoints is not None for p in ps), bad)")
        p = subprocess.run([sys.executable, "-I", "-c", code, os.path.join(self.skill, "scripts")],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        n_packs = len(os.listdir(os.path.join(testkit.SKILL, "sources")))
        n_eps = len([d for d in os.listdir(os.path.join(testkit.SKILL, "sources"))
                     if os.path.exists(os.path.join(testkit.pack_dir(d), "endpoints.json"))])
        self.assertEqual(p.stdout.split(), [str(n_packs), str(n_eps), "{}"])

    def test_policy_command(self):
        for pid in ("eia", "opm"):                  # a standard and a deep pack
            code, r = testkit.run(["--source", pid, "--policy"], fetch=self.fetch)
            self.assertEqual(code, 0, r)
            self.assertEqual(r["policy"]["id"], pid)

    def test_fetch_from_package(self):
        """A standard pack whose adapter reads endpoints.json itself, end to end from the extracted zip."""
        sys.path.insert(0, os.path.join(ROOT, "tests", "sources", "eia"))
        from test_eia import SPEC
        out = os.path.join(self.tmp, "out_eia")
        os.makedirs(out)
        sp = os.path.join(out, "spec.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(SPEC, f)
        fx = os.path.join(ROOT, "tests", "sources", "eia", "fixtures")
        code, r = testkit.run(["--source", "eia", sp, "--out", out, "--check"], fixtures=fx, fetch=self.fetch)
        self.assertEqual((code, r.get("route")), (0, "api"), r)
        code, r = testkit.run(["--source", "eia", sp, "--out", out], fixtures=fx, fetch=self.fetch)
        self.assertEqual(code, 0, r)
        self.assertTrue(os.path.exists(os.path.join(out, "data", "eia", "md_res_price.csv")))

    def test_opm_bundle_carries_policy(self):
        """OPM's manual bundle runs without the skill, so it still gets a policy.json from the bundle."""
        sys.path.insert(0, os.path.join(ROOT, "tests", "sources", "opm"))
        from test_opm import SPEC
        out = os.path.join(self.tmp, "out_opm")
        os.makedirs(out)
        sp = os.path.join(out, "spec.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(SPEC, f)
        cache = os.path.join(self.tmp, "opm_cache")
        code, r = testkit.run(["--source", "opm", sp, "--out", out, "--manual-bundle"],
                              fixtures=os.path.join(ROOT, "tests", "sources", "opm", "fixtures"),
                              env={"OPM_CACHE_DIR": cache, "DATA_ANALYST_CACHE": cache}, fetch=self.fetch)
        self.assertEqual(code, 0, r)
        with open(os.path.join(r["manual_bundle"], "policy.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["id"], "opm")


if __name__ == "__main__":
    unittest.main()

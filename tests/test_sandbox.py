"""A locked-down sandbox (claude.ai code execution and the like): read-only skill copy, a home folder of its
own, no keys, network off. Detected from the environment (IS_SANDBOX), never from fixed paths. Nothing may be
written in the skill folder, the bundle must land in the analysis folder, and ingest must find the uploads
from explicit paths and from DATA_ANALYST_UPLOADS."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
FIXTURE = os.path.join(ROOT, "tests", "sources", "cdc", "fixtures", "r001.json")
SPEC = {"requests": [{"name": "ny", "endpoint": "query",
                      "params": {"dataset": "leading_death", "$where": "state = 'New York' AND year = '2017'",
                                 "$order": "deaths DESC"}}]}


def snapshot(d):
    out = {}
    for dp, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x != "__pycache__"]     # bytecode writes fail harmlessly on a read-only mount
        for f in fns:
            p = os.path.join(dp, f)
            out[p] = os.path.getmtime(p)
    return out


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.skill = os.path.join(self.t, "skills", "data-analyst")
        shutil.copytree(SKILL, self.skill, ignore=shutil.ignore_patterns("__pycache__"))
        self.work = os.path.join(self.t, "outputs")             # wherever the environment keeps deliverables
        self.uploads = os.path.join(self.t, "uploads")
        self.home = os.path.join(self.t, "home")
        for d in (self.work, self.uploads, self.home):
            os.makedirs(d)
        self.before = snapshot(self.skill)

    def tearDown(self):
        for dp, dns, fns in os.walk(self.t):
            for n in dns + fns:
                try:
                    os.chmod(os.path.join(dp, n), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                except OSError:
                    pass
        shutil.rmtree(self.t, ignore_errors=True)

    def run_fetch(self, *args, uploads_env=False):
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.startswith("DATA_ANALYST")}
        env.update(HOME=self.home, USERPROFILE=self.home, CLAUDE_CONFIG_DIR=os.path.join(self.home, ".claude"),
                   IS_SANDBOX="1", DATA_ACCESS_FORCE="restricted", PYTHONDONTWRITEBYTECODE="1")
        env.pop("SSH_CONNECTION", None)
        env.pop("CLAUDE_CODE_REMOTE", None)
        if uploads_env:
            env["DATA_ANALYST_UPLOADS"] = self.uploads
        p = subprocess.run([sys.executable, os.path.join(self.skill, "scripts", "fetch.py"), *args],
                           capture_output=True, text=True, env=env)
        out = p.stdout
        return p.returncode, (json.loads(out[out.find("{"):]) if "{" in out else {"_raw": out, "_err": p.stderr})

    def prepare(self):
        out = os.path.join(self.work, "cdc_test")
        os.makedirs(out)
        sp = os.path.join(out, "spec.json")
        with open(sp, "w") as f:
            json.dump(SPEC, f)
        return out, sp

    def test_detected_from_environment(self):
        out, sp = self.prepare()
        code, r = self.run_fetch("--source", "cdc", sp, "--out", out, "--check")
        self.assertEqual(code, 0, r)
        self.assertEqual(r.get("environment"), "sandbox", r)
        self.assertEqual(r.get("route"), "manual_download", r)
        self.assertNotIn("setup", r.get("next_command", "").lower())
        self.assertTrue(r["instructions"].startswith(out), r["instructions"])

    def test_manual_round_trip(self):
        out, sp = self.prepare()
        self.run_fetch("--source", "cdc", sp, "--out", out, "--check")
        shutil.copy(FIXTURE, os.path.join(self.uploads, "ny.json"))     # stands in for the browser download
        code, r = self.run_fetch("--source", "cdc", sp, "--out", out, "--ingest", self.uploads)
        self.assertEqual(code, 0, r)
        self.assertTrue(os.path.exists(os.path.join(out, "data", "cdc", "ny.csv")))
        self.assertEqual(snapshot(self.skill), self.before, "the skill folder was modified")

    def test_ingest_uses_uploads_env_when_no_paths(self):
        out, sp = self.prepare()
        shutil.copy(FIXTURE, os.path.join(self.uploads, "ny.json"))
        code, r = self.run_fetch("--source", "cdc", sp, "--out", out, "--ingest", uploads_env=True)
        self.assertEqual(code, 0, r)
        code, r = self.run_fetch("--source", "cdc", sp, "--out", out, "--ingest", "--force")
        self.assertTrue(os.path.exists(os.path.join(out, "data", "cdc", "ny.csv")))

    def test_no_fixed_paths_in_skill(self):
        """Environment detection and paths come from variables and arguments, not hard-coded mount points."""
        for dp, dns, fns in os.walk(SKILL):
            dns[:] = [x for x in dns if x != "__pycache__"]
            for f in fns:
                if f.endswith((".py", ".md", ".json", ".sh", ".ps1")):
                    text = open(os.path.join(dp, f), encoding="utf-8", errors="replace").read()
                    self.assertNotIn("/mnt/", text, os.path.join(dp, f))


if __name__ == "__main__":
    unittest.main()

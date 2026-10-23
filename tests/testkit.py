"""Helpers for offline pack tests (standard library only).

    from testkit import PackTest
    class TestCdc(PackTest):
        source = "cdc"
        def test_fetch(self):
            code, res, out = self.fetch({"requests": [...]})
            self.assertEqual(code, 0); rows = self.csv(out, "name")

Lives outside the skill folder (tests/), so the uploaded skill carries no test code. Pack tests are in
tests/sources/<id>/ with their fixtures in tests/sources/<id>/fixtures/. Every call runs the skill's
scripts/fetch.py in a subprocess with DATA_ANALYST_FIXTURES pointing there, so no network is touched and no
key is read (keys in the environment are removed). Record fixtures from real responses with
DATA_ANALYST_RECORD=tests/sources/<id>/fixtures and a normal live fetch.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
SCRIPTS = os.path.join(SKILL, "scripts")
FETCH = os.path.join(SCRIPTS, "fetch.py")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
KEY_VARS = ("FRED_API_KEY", "BEA_API_KEY", "BLS_API_KEY", "IPUMS_API_KEY", "CENSUS_API_KEY", "DATA_GOV_API_KEY",
            "EIA_API_KEY", "DOL_API_KEY", "AQS_API_KEY", "AQS_EMAIL", "NREL_API_KEY", "HUD_USER_TOKEN",
            "NOAA_API_KEY", "USDA_NASS_API_KEY", "USPTO_API_KEY", "OPEN_FEC_API_KEY")


def pack_dir(pid: str) -> str:
    """The pack's folder inside the skill (sources/<id>)."""
    return os.path.join(SKILL, "sources", pid)


def load_pack_module(pid: str, filename: str = "adapter.py"):
    """Import a file of a pack (adapter.py, keycheck.py, lib/x.py) by path, with the pack on sys.path."""
    import importlib.util
    d = pack_dir(pid)
    if d not in sys.path:
        sys.path.insert(0, d)
    name = f"{pid.replace('-', '_')}_{os.path.splitext(os.path.basename(filename))[0]}_t"
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(args: list[str], fixtures: str | None = None, env: dict | None = None, home: str | None = None,
        fetch: str = FETCH):
    e = {k: v for k, v in os.environ.items() if k not in KEY_VARS and not k.endswith("_API_KEY")}
    e.pop("DATA_ANALYST_RECORD", None)
    if fixtures:
        e["DATA_ANALYST_FIXTURES"] = fixtures
    if home:                                     # isolate settings.json and ~/.<x>/api_key lookups
        e.update(HOME=home, USERPROFILE=home, CLAUDE_CONFIG_DIR=os.path.join(home, ".claude"))
    e.update(env or {})
    p = subprocess.run([sys.executable, fetch, *args], capture_output=True, text=True, env=e)
    out = p.stdout.strip()
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except ValueError:
        data = {"_raw": out, "_stderr": p.stderr}
    return p.returncode, data


class PackTest(unittest.TestCase):
    source = ""

    @property
    def pack_dir(self) -> str:
        return os.path.join(SKILL, "sources", self.source)

    @property
    def test_dir(self) -> str:
        return os.path.join(TESTS, "sources", self.source)

    @property
    def fixtures(self) -> str:
        # DATA_ANALYST_FIXTURES_DIR: replay another recording (rsi-data-analyst runs the tests on live responses)
        return os.environ.get("DATA_ANALYST_FIXTURES_DIR") or os.path.join(self.test_dir, "fixtures")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def write_spec(self, spec: dict, name: str = "spec.json") -> tuple[str, str]:
        out = os.path.join(self.tmp.name, "out")
        os.makedirs(out, exist_ok=True)
        sp = os.path.join(out, name)
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        return sp, out

    def cli(self, *args, env=None, fixtures=True):
        return run(["--source", self.source, *args], self.fixtures if fixtures else None, env, self.home)

    def check(self, spec: dict, env=None):
        sp, out = self.write_spec(spec)
        return self.cli(sp, "--out", out, "--check", env=env)

    def fetch(self, spec: dict, env=None):
        sp, out = self.write_spec(spec)
        code, res = self.cli(sp, "--out", out, env=env)
        return code, res, out

    def csv(self, out: str, name: str) -> list[dict]:
        with open(os.path.join(out, "data", self.source, f"{name}.csv"), encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_policy_valid(self):
        if not self.source:
            self.skipTest("base class")
        sys.path.insert(0, SCRIPTS)
        from core import policy
        p = policy.load(self.pack_dir)
        self.assertEqual(policy.validate(p, self.pack_dir), [])

    def test_restricted_routes_to_manual(self):
        """With the network forced off in a sandbox, --check must offer the manual route, never a setup script."""
        if not self.source or not getattr(self, "sample_spec", None):
            self.skipTest("no sample_spec")
        sp, out = self.write_spec(self.sample_spec)
        code, r = run(["--source", self.source, sp, "--out", out, "--check", "--env", "sandbox"], None,
                      {"DATA_ACCESS_FORCE": "restricted"}, self.home)
        self.assertIn(r.get("route"), ("manual_download", "fix_spec"), r)
        # --check reports a route; it only fails (exit 2) when the spec is wrong or the source is unreachable
        # from the user's own machine
        self.assertEqual(code, 2 if r.get("route") in ("fix_spec", "report_error") else 0, r)
        self.assertNotIn("setup", (r.get("next_command") or "").lower())

"""Every pack follows the contract, and the skill stays inside its size budget."""
import glob
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from core import packs, policy  # noqa: E402

HEADINGS = ["## When to use", "## Endpoints and datasets", "## Spec examples", "## Data format and traps",
            "## Access", "## Citation and terms"]
# Hard limits only where something real breaks: claude.ai rejects a description over 1024 characters, and
# Anthropic's skill guidance keeps the SKILL.md body under 500 lines. source.md has no hard limit (cutting a
# trap to meet a number would cost accuracy); a long one is only reported, so it can be split into docs/.
MAX_SKILL_BODY = 500
MAX_DESC = 1024
NOTE_SOURCE_MD = 400


class TestSkillBudget(unittest.TestCase):
    def test_no_test_code_in_skill(self):
        """The skill folder is what gets zipped and uploaded: no tests, fixtures or test helpers in it."""
        for dp, dns, fns in os.walk(SKILL):
            dns[:] = [x for x in dns if x != "__pycache__"]
            for n in dns + fns:
                self.assertFalse(n in ("tests", "fixtures") or n.startswith("test_") or n == "testkit.py",
                                 f"test material inside the skill: {os.path.join(dp, n)}")

    def test_skill_md_limits(self):
        s = open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8").read()
        m = re.match(r"---\n(.*?)\n---\n(.*)", s, re.S)
        self.assertIsNotNone(m, "frontmatter")
        name = re.search(r"^name:\s*(.+)$", m.group(1), re.M).group(1).strip()
        desc = re.search(r'^description:\s*"(.*)"$', m.group(1), re.M).group(1)
        self.assertRegex(name, r"^[a-z0-9-]{1,64}$")
        self.assertLessEqual(len(desc), MAX_DESC)
        self.assertLessEqual(len(m.group(2).strip().splitlines()), MAX_SKILL_BODY)

    def test_generic_voice(self):
        """No organization-specific voice anywhere in the skill text the model reads."""
        bad = re.compile(r"Congressional Budget Office|\bCBO by name\b|\bCBO voice\b")
        for f in glob.glob(os.path.join(SKILL, "**", "*.md"), recursive=True) + [os.path.join(SKILL, "assets", "disclaimer.txt")]:
            if os.path.exists(f):
                text = open(f, encoding="utf-8").read()
                for line in text.splitlines():
                    if bad.search(line) and "publication" not in line.lower() and "report" not in line.lower():
                        self.fail(f"{f}: organization voice: {line[:120]}")


class TestPacks(unittest.TestCase):
    def test_every_pack(self):
        ids = packs.pack_ids()
        self.assertTrue(ids)
        for pid in ids:
            with self.subTest(pack=pid):
                d = os.path.join(SKILL, "sources", pid)
                p = policy.load(d)
                self.assertEqual(policy.validate(p, d), [])
                md = os.path.join(d, "source.md")
                self.assertTrue(os.path.exists(md), "source.md")
                text = open(md, encoding="utf-8").read()
                if len(text.splitlines()) > NOTE_SOURCE_MD:
                    print(f"note: {pid}/source.md has {len(text.splitlines())} lines; consider moving detail to docs/")
                pos = [text.find(h) for h in HEADINGS]
                self.assertNotIn(-1, pos, f"source.md headings missing: {[h for h, i in zip(HEADINGS, pos) if i < 0]}")
                self.assertEqual(pos, sorted(pos), "source.md headings out of order")
                self.assertTrue(re.search(r"^Depth: (deep|standard)\.", text, re.M), "Depth line")
                t = os.path.join(ROOT, "tests", "sources", pid)
                self.assertTrue(glob.glob(os.path.join(t, "test_*.py")), "tests/sources/<id>/test_*.py")
                self.assertFalse(os.path.exists(os.path.join(d, "tests")), "tests belong in tests/sources/<id>, not the skill")
                self.assertTrue(os.path.exists(os.path.join(d, "fetch.py")) or os.path.exists(os.path.join(d, "endpoints.json"))
                                or os.path.exists(os.path.join(d, "adapter.py")), "fetch.py, endpoints.json or adapter.py")
                self.assertEqual(p["depth"] == "deep", os.path.exists(os.path.join(d, "fetch.py")),
                                 "deep packs (and only they) have their own fetch.py")
                if os.path.exists(os.path.join(d, "endpoints.json")):
                    eps = json.load(open(os.path.join(d, "endpoints.json"), encoding="utf-8"))
                    # A catalog entry that names its API version must point at an endpoint path with that
                    # version: a stale path keeps replaying its old fixture while the live API answers 404.
                    for alias, c in (eps.get("catalog") or {}).items():
                        path = ((eps.get("endpoints") or {}).get(c.get("endpoint")) or {}).get("path", "")
                        if c.get("version") and path and "{" not in path:
                            self.assertIn(f"/{c['version']}/", path + "/",
                                          f"catalog {alias!r} says {c['version']} but its endpoint path is {path!r}")
                for fx in glob.glob(os.path.join(t, "fixtures", "*")):
                    self.assertLess(os.path.getsize(fx), 200_000, f"fixture too large: {fx}")

    def test_no_key_in_fixtures(self):
        pat = re.compile(r"(?i)(api_key|registrationkey|UserID)=(?!\[REDACTED\]|DEMO_KEY)[A-Za-z0-9]{16,}")
        for f in glob.glob(os.path.join(ROOT, "tests", "sources", "*", "fixtures", "*")):
            text = open(f, encoding="utf-8", errors="replace").read()
            self.assertIsNone(pat.search(text), f"key-like value in {f}")

    def test_no_stdlib_shadowing(self):
        """Folders that go on sys.path must not hold a module named like a standard-library one
        (scripts/core/setup_key.py once made core/http.py shadow the stdlib http package)."""
        dirs = [os.path.join(SKILL, "scripts"), os.path.join(SKILL, "scripts", "core"),
                os.path.join(SKILL, "scripts", "analysis"), os.path.join(SKILL, "scripts", "report")]
        for pid in packs.pack_ids():
            dirs += [os.path.join(SKILL, "sources", pid), os.path.join(SKILL, "sources", pid, "lib")]
        allowed = {os.path.join(SKILL, "scripts", "core"): {"http"}}    # a package member, never on sys.path
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                name = f[:-3]
                if f.endswith(".py") and name in sys.stdlib_module_names and name not in allowed.get(d, set()):
                    self.fail(f"{os.path.join(d, f)} shadows the standard-library module {name!r}")
        for f in glob.glob(os.path.join(SKILL, "scripts", "core", "*.py")):
            text = open(f, encoding="utf-8").read()
            self.assertNotIn('if __name__ == "__main__"', text, f"{f}: core modules must not be run as scripts")

    def test_routing_up_to_date(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_routing.py"), "--check"])
        self.assertEqual(r.returncode, 0, "references/routing.md is stale: run python tools/gen_routing.py")


if __name__ == "__main__":
    unittest.main()

"""The repo installs as a Claude plugin from GitHub: marketplace.json -> plugins/data-analyst/ (plugin.json) ->
skills/data-analyst/SKILL.md. Also guards against the old project-skill path creeping back in."""
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
import zipfile

import testkit

ROOT = testkit.ROOT
KEBAB = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
RESERVED_PREFIXES = ("claude-", "anthropic-")
RESERVED_MARKETPLACES = {"claude-plugins-official", "claude-code-plugins", "anthropic-plugins", "anthropic-marketplace"}
OLD_PATH = ".claude/skills/data-analyst"


def _json(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return json.load(f)


class TestManifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market = _json(".claude-plugin", "marketplace.json")

    def test_marketplace_identity(self):
        m = self.market
        self.assertRegex(m["name"], KEBAB)
        self.assertNotIn(m["name"], RESERVED_MARKETPLACES)
        self.assertTrue(m["owner"]["name"])
        self.assertTrue(m["plugins"])

    def test_every_listed_plugin_resolves(self):
        names = [p["name"] for p in self.market["plugins"]]
        self.assertEqual(len(names), len(set(names)), "duplicate plugin names")
        for entry in self.market["plugins"]:
            self.assertRegex(entry["name"], KEBAB)
            self.assertFalse(entry["name"].startswith(RESERVED_PREFIXES), entry["name"])
            self.assertTrue(entry["source"].startswith("./"), "relative sources start with ./")
            pdir = os.path.join(ROOT, entry["source"])
            plugin = _json(entry["source"], ".claude-plugin", "plugin.json")
            self.assertEqual(plugin["name"], entry["name"])
            self.assertRegex(plugin["version"], r"\d+\.\d+\.\d+")
            self.assertNotIn("version", entry, "keep the version in plugin.json only, so the two cannot drift")
            self.assertTrue(plugin["description"])
            self.assertTrue(os.path.isdir(os.path.join(pdir, "skills")))

    def test_skill_matches_directory_and_no_claude_md(self):
        pdir = os.path.join(ROOT, "plugins", "data-analyst")
        skill = os.path.join(pdir, "skills", "data-analyst", "SKILL.md")
        self.assertTrue(os.path.isfile(skill))
        with open(skill, encoding="utf-8") as f:
            self.assertRegex(f.read(), r"(?m)^name: data-analyst$")
        for stray in ("CLAUDE.md", "AGENTS.md"):
            self.assertFalse(os.path.exists(os.path.join(pdir, stray)), f"{stray} is not loaded from a plugin root")

    def test_instructions_file_is_agents_md(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "AGENTS.md")))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "CLAUDE.md")), "AGENTS.md replaces CLAUDE.md")


class TestPluginZip(unittest.TestCase):
    def test_plugin_zip_layout(self):
        spec = importlib.util.spec_from_file_location("package_skill_p", os.path.join(ROOT, "tools", "package_skill.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tmp = tempfile.mkdtemp(prefix="da_plug_")
        try:
            out = os.path.join(tmp, "p.zip")
            n = mod.build(out, plugin=True)
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
            self.assertEqual(n, len(names))
            self.assertIn(".claude-plugin/plugin.json", names)
            self.assertIn("skills/data-analyst/SKILL.md", names)
            self.assertIn("skills/data-analyst/sources/packs.json", names)
            self.assertFalse([x for x in names if "\\" in x or "/fixtures/" in x or x.startswith("tests/")])
            self.assertEqual(sum(1 for x in names if x == ".claude-plugin/plugin.json"), 1,
                             "claude.ai needs exactly one plugin.json")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNoStalePaths(unittest.TestCase):
    def test_old_project_skill_path_is_gone(self):
        skip = {".git", "out", "dist", "__pycache__", "node_modules"}
        me = os.path.abspath(__file__)
        hits = []
        for dp, dns, fns in os.walk(ROOT):
            dns[:] = [d for d in dns if d not in skip]
            for f in fns:
                p = os.path.join(dp, f)
                if os.path.abspath(p) == me or not f.endswith((".py", ".md", ".sh", ".ps1", ".json", ".txt")):
                    continue
                with open(p, encoding="utf-8", errors="replace") as fh:
                    if OLD_PATH in fh.read():
                        hits.append(os.path.relpath(p, ROOT))
        self.assertEqual(hits, [], f"still mention {OLD_PATH}")


if __name__ == "__main__":
    unittest.main()

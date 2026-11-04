"""The key-guard hook (plugins/data-analyst/hooks/): it denies tool calls that would show a key and allows the
skill's and the maintainers' normal work. Runs guard_keys.py as the app would (JSON on stdin), with a temp home,
a fake key in keys.json and no real key in the environment."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import testkit

ROOT = testkit.ROOT
HOOKS = os.path.join(ROOT, "plugins", "data-analyst", "hooks")
GUARD = os.path.join(HOOKS, "guard_keys.py")
FAKE = "fakefredkey0123456789abcdef0123"
FAKE_BEA = "BEAFAKE0-1234-5678-9ABC-DEF012345678"


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="da_hook_")
        self.home = os.path.join(self.tmp, "home")
        self.cfg = os.path.join(self.home, ".config", "data-analyst")
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(self.cfg)
        os.makedirs(self.claude)
        with open(os.path.join(self.cfg, "keys.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"FRED_API_KEY": FAKE}}, f)
        os.makedirs(os.path.join(self.home, ".fred"))
        with open(os.path.join(self.home, ".fred", "api_key"), "w", encoding="utf-8") as f:
            f.write(FAKE + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, env: dict):
        with open(os.path.join(self.claude, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": env, "model": "x"}, f)

    def run_hook(self, tool, tool_input, env=None, cwd=ROOT):
        e = {k: v for k, v in os.environ.items() if k not in testkit.KEY_VARS and not k.endswith(("_API_KEY", "_TOKEN"))
             and k not in ("DATA_ANALYST_CONFIG_DIR", "CLAUDE_CONFIG_DIR")}
        e.update(HOME=self.home, USERPROFILE=self.home, CLAUDE_CONFIG_DIR=self.claude)
        e.update(env or {})
        event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": cwd}
        p = subprocess.run([sys.executable, "-I", GUARD], input=json.dumps(event).encode(), capture_output=True,
                           env=e, cwd=cwd, timeout=60)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        self.assertNotIn(FAKE, out, "the hook printed a key")
        self.assertNotIn(FAKE_BEA, out, "the hook printed a key")
        return p.returncode, out

    def deny(self, tool, tool_input, **kw):
        code, out = self.run_hook(tool, tool_input, **kw)
        self.assertEqual(code, 2, f"not denied: {tool} {tool_input}")
        self.assertIn("data-analyst blocked this tool call", out)
        self.assertIn("--check", out)

    def allow(self, tool, tool_input, **kw):
        code, out = self.run_hook(tool, tool_input, **kw)
        self.assertEqual(code, 0, f"denied: {tool} {tool_input}: {out}")
        self.assertEqual(out.strip(), "")


class TestDenies(HookTest):
    def test_key_files(self):
        keys = os.path.join(self.cfg, "keys.json")
        self.deny("Bash", {"command": "cat ~/.config/data-analyst/keys.json"})
        self.deny("Bash", {"command": 'type "%USERPROFILE%\\.config\\data-analyst\\keys.json"'})
        self.deny("Bash", {"command": "cat $DATA_ANALYST_CONFIG_DIR/keys.json"})
        self.deny("PowerShell", {"command": "Get-Content $env:USERPROFILE\\.config\\data-analyst\\keys.json"})
        self.deny("Bash", {"command": "head -1 ~/.fred/api_key"})
        self.deny("Read", {"file_path": keys})
        self.deny("Read", {"file_path": keys + ".bak"})
        self.deny("Read", {"file_path": os.path.join(self.home, ".fred", "api_key")})
        self.deny("Grep", {"pattern": "API_KEY", "path": os.path.join(self.home, ".config")})
        self.deny("Grep", {"pattern": "x", "path": self.cfg})

    def test_custom_config_dir_and_key_file_variable(self):
        alt = os.path.join(self.tmp, "vault")
        os.makedirs(alt)
        kf = os.path.join(self.tmp, "bea.txt")
        env = {"DATA_ANALYST_CONFIG_DIR": alt, "BEA_API_KEY_FILE": kf}
        self.deny("Read", {"file_path": os.path.join(alt, "keys.json")}, env=env)
        self.deny("Bash", {"command": f"cat {alt}/keys.json"}, env=env)
        self.deny("Read", {"file_path": kf}, env=env)
        self.deny("Bash", {"command": "cat $BEA_API_KEY_FILE"}, env=env)

    def test_key_variables(self):
        self.deny("Bash", {"command": "echo $FRED_API_KEY"})
        self.deny("Bash", {"command": 'curl "https://api.example.gov/x?api_key=${FRED_API_KEY}"'})
        self.deny("Bash", {"command": "printenv HUD_USER_TOKEN"})
        self.deny("PowerShell", {"command": "Write-Output $env:BEA_API_KEY"})
        self.deny("PowerShell", {"command": "Get-Item Env:\\CENSUS_API_KEY"})
        self.deny("Bash", {"command": "cmd /c echo %EIA_API_KEY%"})
        self.deny("Bash", {"command": "python -c \"import os; print(os.environ['FRED_API_KEY'])\""})
        self.deny("Bash", {"command": "python -c 'import os; print(os.environ.get(\"DATA_GOV_API_KEY\"))'"})

    def test_environment_dumps_while_a_key_is_set(self):
        env = {"BEA_API_KEY": FAKE_BEA}
        for cmd in ("env", "printenv", "env | sort", "set", "export -p", "declare -x", "cd /tmp && printenv > e.txt",
                    "cmd /c set"):
            self.deny("Bash", {"command": cmd}, env=env)
        for cmd in ("Get-ChildItem Env:", "gci env:", "dir Env:\\", "[Environment]::GetEnvironmentVariables()"):
            self.deny("PowerShell", {"command": cmd}, env=env)
        self.deny("Bash", {"command": "python -c \"import os; print(dict(os.environ))\""}, env=env)
        self.deny("Bash", {"command": "cat /proc/self/environ"}, env=env)

    def test_settings_json_only_while_it_holds_a_key(self):
        path = os.path.join(self.claude, "settings.json")
        self.settings({"FRED_API_KEY": FAKE})
        self.deny("Read", {"file_path": path})
        self.deny("Bash", {"command": "cat ~/.claude/settings.json"})
        self.deny("Grep", {"pattern": "FRED", "path": self.claude})
        self.deny("Bash", {"command": "env"})            # settings env reaches every shell the assistant starts

    def test_key_lookup_code(self):
        code = "import sys; sys.path.insert(0, 'scripts'); from core.keys import find_key; print(find_key('FRED_API_KEY'))"
        self.deny("Bash", {"command": f'python -c "{code}"'})
        self.deny("Bash", {"command": "python - <<'EOF'\nfrom core import keys\nprint(keys.secret_values())\nEOF"})
        outside = os.path.join(self.tmp, "out", "analysis.py")
        self.deny("Write", {"file_path": outside, "content": "from core.keys import find_auth_key\nprint(find_auth_key({}))\n"})
        self.deny("Write", {"file_path": outside, "content": "import os\nk = os.environ.get('FRED_API_KEY')\n"})
        self.deny("Edit", {"file_path": outside, "old_string": "a", "new_string": "open(os.path.expanduser('~/.config/data-analyst/keys.json'))"})
        self.deny("MultiEdit", {"file_path": outside, "edits": [{"old_string": "a", "new_string": "find_key('X')"}]})
        self.deny("Write", {"file_path": os.path.join(self.cfg, "keys.json"), "content": "{}"})
        patch = f"*** Begin Patch\n*** Add File: {outside}\n+from core.keys import secret_values\n+print(secret_values())\n*** End Patch\n"
        self.deny("apply_patch", {"command": patch})


class TestAllows(HookTest):
    def test_normal_skill_work(self):
        env = {"BEA_API_KEY": FAKE_BEA}
        self.allow("Bash", {"command": "python plugins/data-analyst/skills/data-analyst/scripts/fetch.py --source fred out/spec.json --out out --check"}, env=env)
        self.allow("Bash", {"command": "set -euo pipefail; echo $PATH"}, env=env)
        self.allow("Bash", {"command": "env PYTHONUTF8=1 python analysis.py"}, env=env)
        self.allow("PowerShell", {"command": "$env:PATH; Get-ChildItem out"}, env=env)
        self.allow("Bash", {"command": "python -c \"import os; os.environ['DATA_ANALYST_FIXTURES'] = 'x'\""}, env=env)
        self.allow("Read", {"file_path": os.path.join(ROOT, "README.md")})
        self.allow("Grep", {"pattern": "API_KEY", "path": ROOT})
        self.allow("Grep", {"pattern": "API_KEY", "path": self.home})  # whole home folder: hidden folders skipped
        self.allow("Write", {"file_path": os.path.join(self.tmp, "out", "analysis.py"), "content": "import pandas\n"})

    def test_environment_without_keys(self):
        self.allow("Bash", {"command": "env | sort"})
        self.allow("PowerShell", {"command": "Get-ChildItem Env:"})

    def test_settings_without_keys(self):
        self.settings({"SOME_FLAG": "1"})
        self.allow("Read", {"file_path": os.path.join(self.claude, "settings.json")})
        self.allow("Bash", {"command": "cat ~/.claude/settings.json"})

    def test_maintainer_work_in_this_repo(self):
        self.allow("Bash", {"command": "grep -rn FRED_API_KEY plugins/data-analyst/skills/data-analyst/sources/fred"})
        self.allow("Bash", {"command": "grep -rn 'find_key(' plugins/data-analyst/skills/data-analyst/scripts"})
        self.allow("Bash", {"command": "python -I tests/run_all.py fred"})
        keys_py = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst", "scripts", "core", "keys.py")
        self.allow("Edit", {"file_path": keys_py, "old_string": "a", "new_string": "def find_key(env):\n    return os.environ.get(env)"})
        self.allow("Write", {"file_path": os.path.join(ROOT, "tests", "test_x.py"),
                             "content": "from core.keys import find_key\nfind_key('FRED_API_KEY')\n"})
        self.allow("Grep", {"pattern": "keys.json", "path": os.path.join(ROOT, "plugins")})

    def test_fails_open(self):
        p = subprocess.run([sys.executable, "-I", GUARD], input=b"not json", capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.allow("SomeNewTool", {"anything": 1})


class TestManifest(unittest.TestCase):
    def test_hooks_json_points_at_the_guard(self):
        with open(os.path.join(HOOKS, "hooks.json"), encoding="utf-8") as f:
            data = json.load(f)
        (entry,) = data["hooks"]["PreToolUse"]
        for tool in ("Bash", "PowerShell", "Read", "Grep", "Write", "Edit", "apply_patch"):
            self.assertIn(tool, entry["matcher"].split("|"))
        (h,) = entry["hooks"]
        self.assertEqual(h["type"], "command")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/guard_keys.sh", h["command"])
        ps = base64.b64decode(h["commandWindows"].split()[-1]).decode("utf-16-le")
        self.assertIn("guard_keys.ps1", ps)
        for f in ("guard_keys.sh", "guard_keys.ps1", "guard_keys.py"):
            self.assertTrue(os.path.isfile(os.path.join(HOOKS, f)), f)
            with open(os.path.join(HOOKS, f), encoding="utf-8") as fh:
                if f != "guard_keys.py":
                    self.assertIn("guard_keys.py", fh.read())

    def test_guard_knows_every_pack_key(self):
        sys.path.insert(0, HOOKS)
        try:
            import guard_keys
        finally:
            sys.path.remove(HOOKS)
        names, homes = guard_keys.key_names()
        src = os.path.join(testkit.SKILL, "sources")
        for pid in os.listdir(src):
            p = os.path.join(src, pid, "policy.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    a = json.load(f).get("auth") or {}
                for e in [a.get("env")] + list(a.get("alt_env") or []):
                    if e:
                        self.assertIn(e, names, pid)
                if a.get("home"):
                    self.assertIn(a["home"], homes, pid)

    def test_plugin_zip_carries_the_hook(self):
        import importlib.util
        import zipfile
        spec = importlib.util.spec_from_file_location("package_skill_h", os.path.join(ROOT, "tools", "package_skill.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tmp = tempfile.mkdtemp(prefix="da_hookzip_")
        try:
            out = os.path.join(tmp, "p.zip")
            mod.build(out, plugin=True)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
            for f in ("hooks/hooks.json", "hooks/guard_keys.py", "hooks/guard_keys.sh", "hooks/guard_keys.ps1"):
                self.assertIn(f, names)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

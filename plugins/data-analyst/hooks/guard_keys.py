#!/usr/bin/env python
"""PreToolUse hook (Claude Code and Codex): refuse tool calls that would show a data-analyst API key.

The skill's rule (SKILL.md: keys are never printed or looked for) as a check the app runs before each tool
call. It denies:
  - Read / Grep / NotebookRead of a key file: keys.json (and its folder), ~/.<home>/api_key, the file a
    <ENV>_FILE variable names, and Claude Code's settings.json while its "env" block holds a pack key
  - shell commands (Bash, PowerShell) that name such a file, expand a key variable ($FRED_API_KEY,
    $env:FRED_API_KEY, %FRED_API_KEY%, os.environ["FRED_API_KEY"], ...), dump the whole environment while a
    key is set (env, printenv, export -p, gci env:, os.environ, ...), or run code that calls the skill's
    key helpers (find_key, secret_values, ...)
  - writes (Write, Edit, MultiEdit, NotebookEdit, apply_patch) of code that calls those helpers or names a
    key file, outside this plugin's own source (its install folder or a checkout of its repository)
A guardrail, not a sandbox: a path assembled at run time still gets through. It never prints a key, and it
fails open (any internal error allows the call) so a bug here never blocks unrelated work.

Input: the hook JSON on stdin. Deny: exit 2 with the reason on stderr (on stdout with --stdout-reason, for
the PowerShell wrapper). Allow: exit 0.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SKILL = os.path.join(PLUGIN, "skills", "data-analyst")
WIN = os.name == "nt"
HELPERS = ("find_key", "find_auth_key", "secret_values", "known_keys", "_read_env_json", "_read_key_file")
HELPER_CALL = re.compile(r"\b(?:%s)\s*\(" % "|".join(HELPERS))
HINT = ("To see whether a key is set, run the skill's fetch.py with --source <id> --check "
        "(it prints key_source and key_ok, never the key).")


# ---------------------------------------------------------------- what is secret on this machine

def _policies() -> list[dict]:
    """Every pack's auth block: sources/*/policy.json, else the bundled sources/packs.json (plugin zip)."""
    out = []
    for p in sorted(glob.glob(os.path.join(SKILL, "sources", "*", "policy.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f).get("auth") or {})
        except (OSError, ValueError, AttributeError):
            pass
    if not out:
        try:
            with open(os.path.join(SKILL, "sources", "packs.json"), encoding="utf-8") as f:
                out = [(v.get("policy.json") or {}).get("auth") or {} for v in json.load(f).values()]
        except (OSError, ValueError, AttributeError):
            pass
    return out


def _home_of(env: str) -> str:
    return env.lower().replace("_api_key", "").replace("_", "-")   # core.keys default


def key_names() -> tuple[list[str], list[str]]:
    """(key variable names, ~/.<home> folder names) the packs use (core.keys lookup)."""
    names, homes = set(), set()
    for a in _policies():
        envs = ([a["env"]] if isinstance(a.get("env"), str) else []) + [x for x in a.get("alt_env") or []
                                                                           if isinstance(x, str)]
        for i, e in enumerate(envs):
            names.add(e)
            homes.add(a["home"] if i == 0 and isinstance(a.get("home"), str) else _home_of(e))
        for v in (a.get("extra") or {}).values():
            if isinstance(v, str) and v.isupper():
                names.add(v)
    return sorted(names), sorted(h for h in homes if re.fullmatch(r"[A-Za-z0-9._-]+", h))


def config_dir() -> str:
    return os.environ.get("DATA_ANALYST_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "data-analyst")


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _env_block(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        env = data.get("env") if isinstance(data, dict) else None
        return env if isinstance(env, dict) else {}
    except (OSError, ValueError):
        return {}


class Secrets:
    def __init__(self):
        self.names, self.homes = key_names()
        self.settings = [os.path.join(claude_dir(), n) for n in ("settings.json", "settings.json.bak")]
        self.settings_keys = any(str(_env_block(p).get(n) or "").strip() for p in self.settings for n in self.names)
        self.key_set = self.settings_keys or any((os.environ.get(n) or "").strip() for n in self.names)
        self.dirs = [norm(config_dir())]                                   # keys.json, its .bak, temp files
        self.files = [norm(os.path.join(os.path.expanduser("~"), f".{h}", "api_key")) for h in self.homes]
        self.files += [norm(os.environ[n + "_FILE"]) for n in self.names if os.environ.get(n + "_FILE")]
        if self.settings_keys:
            self.files += [norm(p) for p in self.settings]

    def secret_path(self, path: str) -> str | None:
        """The secret this path is (a key file or inside the key folder), as a label; None if not one."""
        p = norm(path)
        for d in self.dirs:
            if p == d or p.startswith(d + os.sep):
                return "the data-analyst keys folder"
        for f in self.files:
            if p == f:
                return os.path.basename(f) if not f.endswith("api_key") else "an api_key file"
        return None

    def holds_secret(self, path: str) -> str | None:
        """For a recursive search: the path is a secret, or a folder below the home folder that holds one."""
        hit = self.secret_path(path)
        if hit:
            return hit
        p, home = norm(path), norm(os.path.expanduser("~"))
        if p == home or not p.startswith(home + os.sep):
            return None                       # searching the whole home folder is left alone (hidden folders)
        for s in self.dirs + self.files:
            if s.startswith(p + os.sep):
                return "a folder that holds an API key file"
        return None


def norm(path: str, cwd: str | None = None) -> str:
    p = os.path.expandvars(os.path.expanduser(str(path).strip().strip("'\"")))
    if WIN:
        m = re.match(r"^/([A-Za-z])(/.*)?$", p)                       # Git Bash /c/Users/...
        if m:
            p = m.group(1) + ":" + (m.group(2) or "/")
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.normcase(os.path.normpath(p))


# ---------------------------------------------------------------- shell commands

def _unquoted(cmd: str) -> str:
    """The command with single-quoted text removed (no expansion happens there in sh or PowerShell)."""
    return re.sub(r"'[^']*'", "''", cmd)


def _path_refs(text: str, s: Secrets) -> str | None:
    """Does the text name a key file (literally or through a variable)?"""
    if re.search(r"(?i)\.config[\\/]+data-analyst\b", text) or \
            re.search(r"(?i)(?:\$\{?|\$env:|%)DATA_ANALYST_CONFIG_DIR\b", text):
        return "the data-analyst keys file"
    for h in s.homes:
        if re.search(r"(?i)\.%s[\\/]+(?:api_key|\*)" % re.escape(h), text):
            return "an api_key file"
    lits = [config_dir()] + [os.environ[n + "_FILE"] for n in s.names if os.environ.get(n + "_FILE")]
    for lit in lits:
        for form in {lit, lit.replace("\\", "/")}:
            if len(form) > 3 and form.lower() in text.lower():
                return "an API key file"
    if s.settings_keys:
        if re.search(r"(?i)\.claude[\\/]+settings\.json", text) or \
                re.search(r"(?i)(?:\$\{?|\$env:|%)CLAUDE_CONFIG_DIR\b", text) or \
                any(len(d) > 3 and d.lower() in text.lower() for d in {claude_dir(), claude_dir().replace("\\", "/")}):
            return "Claude Code's settings.json (it holds an API key)"
    return None


def _expands_key(cmd: str, s: Secrets) -> str | None:
    bare = _unquoted(cmd)
    for n in s.names:
        for v in (n, n + "_FILE"):
            e = re.escape(v)
            if re.search(r"(?<!\\)\$\{?%s\b|(?i:\$env:)%s\b|%%%s%%|(?i:\benv:\\?)%s\b" % (e, e, e, e), bare) or \
                    re.search(r"\bprintenv\s+%s\b" % e, cmd) or \
                    re.search(r"(?:environ|getenv|GetEnvironmentVariable|process\.env)\b[^;\n]{0,24}?\b%s\b" % e, cmd):
                return f"the {n} variable"
    return None


SEP = r"(?:^|[;&|(`\n]|\$\(|\bcmd(?:\.exe)?\s+/[ck])\s*"
END = r"\s*(?:$|[;&|)>`\n])"
DUMPS = [
    re.compile(SEP + r"(?:env|printenv|set|export|typeset|declare)" + END),
    re.compile(SEP + r"(?:export\s+-p|declare\s+-[a-zA-Z]*[xp][a-zA-Z]*|typeset\s+-[a-zA-Z]*x[a-zA-Z]*|env\s+-0)" + END),
    re.compile(r"(?i)(?:^|[\s(;|&'\"])env:\\?\*?(?=$|[\s'\";|)])"),                  # Get-ChildItem Env:
    re.compile(r"(?i)\bGetEnvironmentVariables\b"),
    re.compile(r"\bos\.environ\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b|\.__contains__))"),
    re.compile(r"\bprocess\.env\b(?!\s*(?:\.\w|\[))"),
    re.compile(r"/proc/[^\s/]+/environ\b"),
]
SEARCH_ONLY = re.compile(r"(?i)^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?(?:grep|egrep|rg|findstr|Select-String|sls|"
                         r"git\s+(?:grep|log|diff|show|blame))\b")


def _runs_code(cmd: str) -> bool:
    return not SEARCH_ONLY.search(cmd) or bool(re.search(r"<<|\bpython|\bpy\b|\bnode\b|>", cmd))


def check_command(cmd: str, s: Secrets) -> str | None:
    hit = _path_refs(cmd, s)
    if hit:
        return f"it names {hit} (searching source code for this text? use the search tool, not a shell command)"
    hit = _expands_key(cmd, s)
    if hit:
        return f"it reads {hit}"
    if s.key_set and any(p.search(cmd) for p in DUMPS):
        return "it lists the environment, which holds an API key"
    if HELPER_CALL.search(cmd) and _runs_code(cmd):
        return "it calls the skill's key lookup (core.keys)"
    return None


# ---------------------------------------------------------------- writes

def own_source(path: str) -> bool:
    """Is the file part of this plugin (its install folder, or a checkout of its repository)?"""
    p = norm(path)
    if p.startswith(norm(PLUGIN) + os.sep):
        return True
    d = os.path.dirname(p)
    for _ in range(40):
        if os.path.isfile(os.path.join(d, "plugins", "data-analyst", ".claude-plugin", "plugin.json")):
            return True
        up = os.path.dirname(d)
        if up == d:
            return False
        d = up
    return False


def check_write(paths: list[str], content: str, s: Secrets) -> str | None:
    if paths and all(own_source(p) for p in paths):
        return None
    if HELPER_CALL.search(content):
        return "the code calls the skill's key lookup (core.keys); fetch.py already uses the key without showing it"
    hit = _path_refs(content, s) or _expands_key(content, s)
    if hit:
        return f"the code reads {hit}"
    return None


# ---------------------------------------------------------------- dispatch

def decide(event: dict) -> str | None:
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input") or {}
    if not isinstance(ti, dict):
        return None
    cwd = event.get("cwd") or os.getcwd()
    s = Secrets()
    if tool in ("Read", "NotebookRead"):
        p = ti.get("file_path") or ti.get("notebook_path")
        hit = p and s.secret_path(norm(p, cwd))
        return f"it reads {hit}" if hit else None
    if tool == "Grep":
        hit = s.holds_secret(norm(ti.get("path") or cwd, cwd))
        return f"it searches {hit}" if hit else None
    if tool in ("Bash", "PowerShell", "shell", "local_shell", "exec_command"):
        cmd = ti.get("command")
        cmd = " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd or "")
        return check_command(cmd, s)
    if tool == "apply_patch":
        text = str(ti.get("command") or ti.get("patch") or ti.get("input") or "")
        paths = [norm(m, cwd) for m in re.findall(r"(?m)^\*\*\* (?:Add|Update) File:\s*(.+?)\s*$", text)]
        added = "\n".join(l[1:] for l in text.splitlines() if l.startswith("+"))
        return check_write(paths, added, s)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = ti.get("file_path") or ti.get("notebook_path")
        content = "\n".join(str(x) for x in [ti.get("content"), ti.get("new_string"), ti.get("new_source")]
                            + [e.get("new_string") for e in ti.get("edits") or [] if isinstance(e, dict)] if x)
        hit = p and s.secret_path(norm(p, cwd))
        if hit:
            return f"it writes {hit}"
        return check_write([norm(p, cwd)] if p else [], content, s)
    return None


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        why = decide(json.loads(raw) if raw.strip() else {})
    except Exception:                                       # fail open: never block work over a hook bug
        return 0
    if not why:
        return 0
    msg = f"data-analyst blocked this tool call: {why}. Never print or look up an API key. {HINT}"
    print(msg, file=sys.stdout if "--stdout-reason" in sys.argv else sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

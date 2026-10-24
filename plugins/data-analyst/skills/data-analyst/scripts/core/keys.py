"""API key lookup and redaction for every source pack (standard library only).

A pack names its key variable in policy.json (auth.env, e.g. "FRED_API_KEY"). find_key() looks, in order:
  1. the <ENV> environment variable
  2. the file named by the <ENV>_FILE environment variable
  3. "env": {"<ENV>": "..."} in the skill's own keys.json (~/.config/data-analyst or $DATA_ANALYST_CONFIG_DIR),
     which setup.ps1 / setup.sh write; the same file serves every assistant (Claude, Codex, a plain terminal)
  4. "env": {"<ENV>": "..."} in Claude Code's settings.json (~/.claude or $CLAUDE_CONFIG_DIR; setup --claude)
  5. ~/.<home>/api_key (a file holding only the key); <home> is auth.home in policy.json
Files 3 and 4 are read directly on every lookup, not loaded into the environment, so a key stored a moment
ago works without a restart.
Returns (key, source label). The label may be printed; the key never is. Every message that leaves a
pack goes through redact(), which removes every key this process has seen plus the usual key-shaped
query parameters, headers and JSON fields.

DATA_ACCESS_FORCE=restricted (the shared data-access kit's test switch) hides every key and makes every
request fail as a network error, to replay a locked-down sandbox on any machine.
"""
from __future__ import annotations

import json
import os
import re

_KNOWN: set[str] = set()
# Query parameters, headers and JSON fields that carry keys in the APIs the packs use.
KEY_FIELDS = ("api_key", "apikey", "api-key", "key", "registrationkey", "userid", "token", "access_token",
              "x-api-key", "authorization", "subscription-key", "user_token")
EXTRA_PATTERNS: list[re.Pattern] = []


def restricted() -> bool:
    return (os.environ.get("DATA_ACCESS_FORCE") or "").strip().lower() == "restricted"


def config_path() -> str:
    cfg = os.environ.get("DATA_ANALYST_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "data-analyst")
    return os.path.join(cfg, "keys.json")


def settings_path() -> str:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(cfg, "settings.json")


def _read_env_json(path: str, env: str) -> str:
    """The "env" block of a JSON settings file (keys.json, Claude Code's settings.json)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    return str(((data.get("env") if isinstance(data, dict) else None) or {}).get(env) or "").strip()


def _read_key_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.readline().strip()
    except OSError:
        return ""


def key_locations(env: str, home: str | None = None) -> list[str]:
    home = home or env.lower().replace("_api_key", "").replace("_", "-")
    return [f"the {env} environment variable",
            f"the file named by the {env}_FILE environment variable",
            f'"env": {{"{env}": "..."}} in {config_path()} (setup.ps1 / setup.sh write it)',
            f'"env": {{"{env}": "..."}} in Claude Code\'s {settings_path()}',
            f"~/.{home}/api_key (a file holding only the key)"]


def find_key(env: str | None, home: str | None = None) -> tuple[str | None, str | None]:
    """Locate the key named env. Returns (key, source); (None, None) when absent or restricted."""
    if not env or restricted():
        return None, None
    home = home or env.lower().replace("_api_key", "").replace("_", "-")
    found: tuple[str | None, str | None] = (None, None)
    key = (os.environ.get(env) or "").strip()
    if key:
        found = key, f"{env} environment variable"
    if not found[0] and os.environ.get(f"{env}_FILE"):
        key = _read_key_file(os.environ[f"{env}_FILE"])
        if key:
            found = key, f"file named by {env}_FILE"
    for label, path in (("keys.json", config_path()), ("settings.json", settings_path())):
        if not found[0]:
            key = _read_env_json(path, env)
            if key:
                found = key, f"{label} ({path})"
    if not found[0]:
        kf = os.path.join(os.path.expanduser("~"), f".{home}", "api_key")
        key = _read_key_file(kf)
        if key:
            found = key, kf
    if found[0]:
        remember(found[0])
    return found


def remember(secret: str) -> None:
    """Add a secret to the redaction set (keys passed in as arguments, tokens returned by an API)."""
    if secret and len(secret) >= 6:
        _KNOWN.add(secret)


def add_pattern(regex: str) -> None:
    """A pack whose API echoes keys in odd shapes adds a pattern (BLS: 32-hex tokens in error text)."""
    EXTRA_PATTERNS.append(re.compile(regex))


def redact(text) -> str:
    text = str(text)
    for k in sorted(_KNOWN, key=len, reverse=True):
        text = text.replace(k, "[REDACTED]")
    names = "|".join(re.escape(f) for f in KEY_FIELDS)
    text = re.sub(rf"(?i)([?&](?:{names})=)[^&\s'\"#]+", r"\1[REDACTED]", text)
    text = re.sub(rf"(?i)([\"'](?:{names})[\"']\s*:\s*[\"'])[^\"']+", r"\1[REDACTED]", text)
    # a credential is a run of token characters (RFC 6750 b64token); prose such as "'Authorization: Bearer' header"
    # or "Bearer <token>" is left alone
    text = re.sub(r"(?i)(authorization:\s*(?:(?:bearer|basic|token)\s+)?)(?!(?:bearer|basic|token)\b)"
                  r"[A-Za-z0-9._~+/=-]{6,}", r"\1[REDACTED]", text)
    for p in EXTRA_PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


def secret_values() -> list[str]:
    """Every secret this process knows, for scan_secrets (never printed)."""
    return sorted(_KNOWN)


def find_auth_key(auth: dict) -> tuple[str | None, str | None]:
    """The key for a pack's auth block: auth.env first, then each auth.alt_env (e.g. EIA accepts an
    api.data.gov key, so ["DATA_GOV_API_KEY"])."""
    key, src = find_key(auth.get("env"), auth.get("home"))
    for alt in ([] if key else auth.get("alt_env") or []):
        key, src = find_key(alt)
        if key:
            break
    return key, src

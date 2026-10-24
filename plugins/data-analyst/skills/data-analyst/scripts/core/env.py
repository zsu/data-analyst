"""Where this runs, what it can write, and how the user can supply a key (standard library only).

Environments (only decide HOW the user can supply a key or a file; the route itself is chosen from what
was actually reached, never from the environment's name):
  local    the user's own computer: they can run the setup script in their terminal
  ssh      a machine the user reaches over SSH: same, in that terminal
  cloud    a Claude Code cloud session: keys go into the environment's secrets; hosts may need allowlisting
  sandbox  claude.ai code execution, Cowork, ChatGPT, Codex cloud or any other container with no terminal of
           the user's: no setup script, no key file; keyless hosts only if the network allows them, otherwise the user
           downloads the files and uploads them
Detected from environment variables and container markers (no fixed paths); override with --env or
DATA_ANALYST_ENV when the guess is wrong.

Paths come from the caller, not from guesses: the model passes --out (the deliverables folder its
environment names, or out/<name>) and the uploaded files to --ingest. Optional overrides:
  DATA_ANALYST_OUT      root for default_out(name)
  DATA_ANALYST_UPLOADS  folders (os.pathsep-separated) searched when --ingest is given no paths
  DATA_ANALYST_CACHE    persistent cache root
Nothing is ever written inside the skill folder (it is read-only in some environments).
"""
from __future__ import annotations

import os
import sys
import tempfile

ENVIRONMENTS = ("local", "ssh", "cloud", "sandbox")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRUTHY = ("1", "true", "yes")


def detect_environment(override: str | None = None) -> str:
    forced = (override or os.environ.get("DATA_ANALYST_ENV") or "").strip().lower()
    if forced in ENVIRONMENTS:
        return forced
    if (os.environ.get("CLAUDE_CODE_REMOTE") or "").lower() in TRUTHY:
        return "cloud"
    if (os.environ.get("IS_SANDBOX") or "").lower() in TRUTHY:
        return "sandbox"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "ssh"
    if sys.platform in ("win32", "darwin"):
        return "local"
    markers = [os.environ.get("CODESPACES"), os.environ.get("KUBERNETES_SERVICE_HOST"),
               "x" if os.path.exists("/.dockerenv") else "", "x" if os.path.exists("/run/.containerenv") else ""]
    return "sandbox" if any(markers) or not writable(os.path.expanduser("~")) else "local"


def network_note() -> str:
    """A hint for a local run whose network is cut by the assistant's own sandbox (Codex sets
    CODEX_SANDBOX_NETWORK_DISABLED=1 there); empty otherwise."""
    if (os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") or "").lower() in TRUTHY:
        return (" Codex's sandbox has network access turned off: approve running the command with network access, "
                "or turn it on in Codex's sandbox settings (sandbox_workspace_write.network_access = true).")
    return ""


def writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".write_test_{os.getpid()}")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def default_out(name: str) -> str:
    """Analysis folder for a run when the caller gives none: $DATA_ANALYST_OUT/<name>, else out/<name>."""
    root = os.environ.get("DATA_ANALYST_OUT") or os.path.join(os.getcwd(), "out")
    return os.path.join(root, name)


def cache_root() -> str:
    """Persistent cache root (DATA_ANALYST_CACHE, else ~/.cache/data-analyst); a temp folder when that is not
    writable (sandboxes), where it lasts only for the session."""
    want = os.environ.get("DATA_ANALYST_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "data-analyst")
    if writable(want):
        return want
    return os.path.join(tempfile.gettempdir(), "data-analyst-cache")


def upload_dirs() -> list[str]:
    """Folders searched for uploaded files when --ingest gets no paths: DATA_ANALYST_UPLOADS only."""
    return [d for d in (os.environ.get("DATA_ANALYST_UPLOADS") or "").split(os.pathsep) if d and os.path.isdir(d)]


def setup_command(source: str) -> tuple[str, str]:
    """(script path, command) that stores a key for source with hidden input (local and ssh only)."""
    if sys.platform == "win32":
        path = os.path.join(SKILL_DIR, "setup.ps1")
        return path, f'powershell -NoProfile -ExecutionPolicy Bypass -File "{path}" -Source {source}'
    path = os.path.join(SKILL_DIR, "setup.sh")
    return path, f'bash "{path}" {source}'

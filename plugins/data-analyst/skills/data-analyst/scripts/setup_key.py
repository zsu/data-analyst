"""Back end of setup.ps1 / setup.sh: describe, verify and store one source's API key (standard library only).

    python setup_key.py info <source>              # key variable, signup URL (JSON)
    python setup_key.py verify <source>            # key from DATA_ANALYST_SETUP_KEY; exit 0 ok, 4 rejected,
                                                   # 3 unclear, 5 network
    python setup_key.py save <source> <settings>   # writes env.<VAR> into settings.json (backup kept)

The key travels only through the DATA_ANALYST_SETUP_KEY environment variable (never argv, never printed).
A pack can supply sources/<id>/keycheck.py with verify(key) -> (True|False|None, detail); otherwise the
pack's API probe is called with the key and an authentication error counts as a rejection.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import packs  # noqa: E402
from core.http import Client, DataError  # noqa: E402
from core.keys import redact, remember  # noqa: E402


def info(pid: str) -> int:
    p = packs.load(pid).policy
    a = p["auth"]
    print(json.dumps({"source": pid, "title": p["title"], "env": a.get("env"), "required": a["required"],
                      "signup_url": a.get("signup_url"), "extra": a.get("extra") or {}}))
    return 0 if a.get("env") else 2


def _extras() -> dict:
    """Paired, non-secret credentials (AQS_EMAIL) the setup scripts collected: DATA_ANALYST_SETUP_EXTRA (JSON)."""
    try:
        return {k: str(v).strip() for k, v in json.loads(os.environ.get("DATA_ANALYST_SETUP_EXTRA") or "{}").items() if str(v).strip()}
    except ValueError:
        return {}


def verify(pid: str) -> int:
    key = os.environ.get("DATA_ANALYST_SETUP_KEY", "").strip()
    if not key:
        return 3
    os.environ.update(_extras())
    remember(key)
    pack = packs.load(pid)
    kc = os.path.join(pack.dir, "keycheck.py")
    try:
        if os.path.exists(kc):
            spec = importlib.util.spec_from_file_location(f"keycheck_{pid}", kc)
            mod = importlib.util.module_from_spec(spec)
            sys.path.insert(0, pack.dir)
            spec.loader.exec_module(mod)
            ok, detail = mod.verify(key)
        else:
            api = pack.policy["routes"].get("api") or {}
            base_host = urllib.parse.urlsplit(api.get("base", "")).netloc.lower()
            probe_host = urllib.parse.urlsplit(api.get("probe", "")).netloc.lower() or base_host
            if not base_host or probe_host != base_host:
                # a probe on another host never sees the key: it would answer "OK" for any string, and
                # the key would go to a host that does not need it
                ok, detail = None, (f"not checked: the probe ({probe_host or 'none'}) is not the keyed API host "
                                    f"({base_host or 'none'}); add sources/{pid}/keycheck.py to check this key")
            else:
                c = Client(api.get("base", ""), name=pid, auth=pack.policy["auth"], key=key, retries=1)
                c.get(api["probe"])
                ok, detail = True, "the API answered with the key"
    except DataError as e:
        if e.kind == "network":
            print(redact(f"network error: {e}"))
            return 5
        ok, detail = (False if e.kind in ("auth", "key_missing") else None), str(e)
    except Exception as e:                       # a pack's keycheck failing must not leak the key
        ok, detail = None, redact(f"{type(e).__name__}: {e}")
    print(redact(detail)[:300])
    return 0 if ok else 4 if ok is False else 3


def save(pid: str, path: str) -> int:
    env_name = packs.load(pid).policy["auth"].get("env")
    key = os.environ.get("DATA_ANALYST_SETUP_KEY", "").strip()
    if not env_name or not key:
        print("ERROR: nothing to save")
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = {}
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: {path} is not valid JSON ({e.msg}, line {e.lineno}). Fix it first; nothing was changed.")
            return 2
        if not isinstance(data, dict):
            print(f"ERROR: {path} does not contain a JSON object; nothing was changed.")
            return 2
        shutil.copy2(path, path + ".bak")
        try:
            os.chmod(path + ".bak", stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    env = data.get("env")
    if env is None:
        env = {}
    elif not isinstance(env, dict):
        print(f'ERROR: "env" in {path} is not an object; nothing was changed.')
        return 2
    action = "Replaced the existing" if env_name in env else "Added"
    env[env_name] = key
    env.update(_extras())
    data["env"] = env
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".settings.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, path)
    print(f"{action} {env_name} in {path} (env). Other settings were kept."
          + (f" Backup: {path}.bak" if os.path.exists(path + ".bak") else ""))
    return 0


if __name__ == "__main__":
    cmd, pid = sys.argv[1], sys.argv[2]
    if cmd == "info":
        sys.exit(info(pid))
    if cmd == "verify":
        sys.exit(verify(pid))
    if cmd == "save":
        sys.exit(save(pid, sys.argv[3]))
    sys.exit(2)

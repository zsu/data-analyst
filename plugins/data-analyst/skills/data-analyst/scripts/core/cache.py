"""Disk cache for raw downloads, used only when a pack's policy allows it (standard library only).

- <cache_root>/<namespace>/<sha256(key)>.bin with a .meta.json sidecar {key, fetched_at, expires, meta}
- the key is a credential-free URL (plus a body hash for POST), so no secret is ever written
- atomic writes (temp file + os.replace), 0600 files and 0700 folders where the OS supports it
- size cap per entry and in total, swept by least recent use; entries untouched for 30 days are dropped
- cache_root() is ~/.cache/data-analyst (DATA_ANALYST_CACHE overrides), or a temp folder in sandboxes

policy cache.mode decides: "none" (FRED's terms: no persistent copies) refuses every put and get;
"run_only" also refuses (the run's own data/ folder is the only reuse); "persistent" caches for ttl_hours.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

from core.env import cache_root

MAX_ENTRY = 512 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024 * 1024
STALE_DAYS = 30


class Cache:
    def __init__(self, namespace: str, policy: dict, enabled: bool = True):
        self.mode = policy["cache"]["mode"]
        self.ttl = float(policy["cache"].get("ttl_hours") or 24) * 3600
        self.on = enabled and self.mode == "persistent"
        self.dir = os.path.join(cache_root(), namespace)
        if self.on:
            os.makedirs(self.dir, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass

    def _paths(self, key: str):
        h = hashlib.sha256(key.encode()).hexdigest()
        return os.path.join(self.dir, h + ".bin"), os.path.join(self.dir, h + ".meta.json")

    def get(self, key: str, max_age_hours: float | None = None) -> tuple[bytes, dict] | None:
        if not self.on:
            return None
        data_p, meta_p = self._paths(key)
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
            ttl = max_age_hours * 3600 if max_age_hours is not None else self.ttl
            if time.time() - meta["stored"] > ttl:
                return None
            with open(data_p, "rb") as f:
                data = f.read()
            os.utime(meta_p)
            return data, meta
        except (OSError, ValueError, KeyError):
            return None

    def path(self, key: str) -> str | None:
        """Path of a cached file (for large bulk files read in place)."""
        if self.get(key) is None:
            return None
        return self._paths(key)[0]

    def put(self, key: str, data: bytes, meta: dict | None = None) -> str | None:
        if not self.on or len(data) > MAX_ENTRY:
            return None
        data_p, meta_p = self._paths(key)
        for target, payload in ((data_p, data),
                                (meta_p, json.dumps({"key": key, "stored": time.time(), **(meta or {})}).encode())):
            fd, tmp = tempfile.mkstemp(dir=self.dir)
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, target)
        self.sweep()
        return data_p

    def sweep(self):
        try:
            entries = []
            for n in os.listdir(self.dir):
                p = os.path.join(self.dir, n)
                st = os.stat(p)
                if time.time() - st.st_mtime > STALE_DAYS * 86400:
                    os.remove(p)
                    continue
                entries.append((st.st_mtime, st.st_size, p))
            total = sum(s for _, s, _ in entries)
            for _, size, p in sorted(entries):
                if total <= MAX_TOTAL * 0.8:
                    break
                os.remove(p)
                total -= size
        except OSError:
            pass

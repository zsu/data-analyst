#!/usr/bin/env python
"""Check that no API key or credential appears in any output file. Never prints a key.

Usage: python scan_secrets.py <output_dir or file> [more ...] [--sources bls,fred]

Scans every file (large files in streaming chunks, and every member of a .zip such as
replication_package.zip, decompressed) for:
  - the literal value of every key core.keys can find for any source pack (each policy's auth.env and
    auth.extra variables: environment, *_FILE, settings.json, ~/.<home>/api_key), in any letter case
  - key-shaped assignments and URL parameters: <PACK>_API_KEY=..., "api_key": "...", ?api_key=<20+>,
    registrationkey=<hex>, UserID=<GUID> (BEA), BEA's echoed "ParameterName": "USERID", Authorization: Bearer ...
    (the placeholders {API_KEY}, [REDACTED] and __NAME__ are fine)
  - for sources whose API echoes keys in error text (policy auth.echoes_key: BLS, BEA), any 32-hex token
    (a SHA-256 checksum is 64 hex digits and does not match), except inside data/<id>/ of a source that does
    not echo keys: a 32-hex value there is that source's own identifier (AFDC network ids, ...), and a real key
    is still caught by the literal-value check
Sources: --sources, else the data/<id>/ folders of the first directory argument.
Exit code 0 = clean, 1 = something found (file names and line numbers are listed, values are not).
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import keys as K  # noqa: E402
from core import packfiles  # noqa: E402
from core import policy as pol  # noqa: E402

CHUNK = 1 << 22
GUID = rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
PLACE = rb"(?!\{API_KEY\}|\[REDACTED\]|__[A-Z_]+__)"
BASE_PATTERNS = [
    re.compile(rb"\b[A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET)['\"]?\s*[=:]\s*['\"]?" + PLACE + rb"[A-Za-z0-9-]{16,}"),
    re.compile(rb"(?i)(?:api[_-]?key|registrationkey|subscription-key|user_token|access_token)['\"]?\s*[=:]\s*['\"]"
               + PLACE + rb"(?:[A-Za-z0-9-]{16,}|" + GUID + rb")['\"]"),
    re.compile(rb"(?i)registrationkey['\"]?\s*[=:]\s*['\"]?" + PLACE + rb"[0-9a-f]{20,}"),
    re.compile(rb"(?i)[?&](?:api_key|apikey|api-key|registrationkey|token|access_token|user_token|subscription-key)="
               + PLACE + rb"[A-Za-z0-9-]{16,}"),
    re.compile(rb"(?i)[?&]userid=" + GUID),
    re.compile(rb'(?i)"ParameterName"\s*:\s*"USERID"\s*,\s*"ParameterValue"\s*:\s*"' + GUID),
    re.compile(rb"(?i)authorization['\"]?\s*[:=]\s*['\"]?(?:bearer\s+|token\s+|basic\s+)?" + PLACE + rb"[A-Za-z0-9._-]{30,}"),
]
HEX32 = re.compile(rb"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def all_policies() -> dict[str, dict]:
    import policyset as PS
    out = {}
    d = PS.sources_dir()
    for sid in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if packfiles.is_pack(os.path.join(d, sid)):
            try:
                out[sid] = pol.load(os.path.join(d, sid))
            except (OSError, ValueError):
                pass
    return out


def known_keys(policies: dict | None = None) -> list[bytes]:
    """Every key the packs can find on this machine (looked up through core.keys, never printed)."""
    policies = all_policies() if policies is None else policies
    for P in policies.values():
        a = P.get("auth") or {}
        if a.get("env"):
            K.find_key(a["env"], a.get("home"))
        for var in (a.get("extra") or {}).values():
            if isinstance(var, str) and var.isupper():
                K.find_key(var, a.get("home"))
    return [s.encode().lower() for s in K.secret_values() if s]


def echo_sources(policies: dict) -> list[str]:
    return [sid for sid, P in policies.items() if (P.get("auth") or {}).get("echoes_key")]


def _url_id(buf: bytes, pos: int) -> bool:
    """Is the hex at pos the value of a URL query parameter whose name is not a credential (series=, id=)?"""
    before = buf[max(0, pos - 200):pos]
    m = re.search(rb"[?&]([A-Za-z_][\w.-]{0,30})=$", before)
    return bool(m and b"http" in before and not re.search(rb"(?i)key|token|auth|secret|pass|user|reg|sig", m.group(1)))


def scan_stream(f, name: str, keys: list[bytes], hex32: bool = False) -> list[str]:
    hits = []
    overlap = max([len(k) for k in keys] + [200])
    prev, offset_line = b"", 1
    pats = BASE_PATTERNS + ([HEX32] if hex32 else [])
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            break
        buf = prev + chunk
        low = buf.lower() if keys else b""

        def line_of(pos):
            return offset_line + buf[:pos].count(b"\n") - prev.count(b"\n")
        for k in keys:
            if k in low:
                hits.append(f"{name}: line ~{line_of(low.index(k))}: contains an API key value")
        for p in pats:
            m = next((x for x in p.finditer(buf) if not (p is HEX32 and _url_id(buf, x.start()))), None)
            if m:
                what = "a 32-hex token (a key this source echoes)" if p is HEX32 else "a hard-coded credential"
                hits.append(f"{name}: line ~{line_of(m.start())}: looks like {what}")
        offset_line += chunk.count(b"\n")
        prev = buf[-overlap:]
    return hits


def other_source_data(rel: str, quiet: set[str]) -> bool:
    """Is rel (a path or zip member name) inside data/<id>/ of a source in quiet?"""
    parts = [x for x in re.split(r"[\\/]", rel) if x and x != "."]
    return any(parts[i] == "data" and i + 1 < len(parts) and parts[i + 1] in quiet for i in range(len(parts) - 1))


def scan_file(path: str, keys=None, hex32: bool = False, quiet: set[str] | None = None, rel: str = "") -> list[str]:
    keys = [keys] if isinstance(keys, bytes) else [k.lower() for k in (keys or []) if k]
    quiet = quiet or set()
    with open(path, "rb") as f:
        hits = scan_stream(f, path, keys, hex32 and not other_source_data(rel or path, quiet))
    if zipfile.is_zipfile(path):                   # members are compressed: scan them decompressed too
        try:
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if not info.is_dir():
                        with z.open(info) as m:
                            h = hex32 and not other_source_data(info.filename, quiet)
                            hits += scan_stream(io.BufferedReader(m), f"{path}!{info.filename}", keys, h)
        except (zipfile.BadZipFile, OSError):
            pass
    return sorted(set(hits))


def scan_text(text: str, keys=None, hex32: bool = False, name: str = "text") -> list[str]:
    keys = [k.lower() for k in (keys or []) if k]
    return scan_stream(io.BytesIO(text.encode("utf-8", "replace")), name, keys, hex32)


def scan_tree(paths: list[str], keys: list[bytes], hex32: bool = False) -> tuple[list[str], int]:
    # data/<id>/ of sources that never echo keys skip the 32-hex rule (their ids look like keys)
    quiet = {sid for sid, P in all_policies().items() if not (P.get("auth") or {}).get("echoes_key")} if hex32 else set()
    found, n = [], 0
    for root in paths:
        files = [root] if os.path.isfile(root) else [os.path.join(r, f) for r, _, fs in os.walk(root) for f in fs]
        for p in files:
            n += 1
            try:
                rel = os.path.relpath(p, root) if os.path.isdir(root) else p
                found += scan_file(p, keys, hex32, quiet, rel)
            except OSError as e:
                print(f"skip {p}: {e}", file=sys.stderr)
    return found, n


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sources = None
    if "--sources" in argv:
        i = argv.index("--sources")
        sources = [s for s in argv[i + 1].split(",") if s] if i + 1 < len(argv) else []
        del argv[i:i + 2]
    if not argv:
        sys.exit(__doc__)
    policies = all_policies()
    if sources is None and os.path.isdir(argv[0]):
        d = os.path.join(argv[0], "data")
        sources = sorted(os.listdir(d)) if os.path.isdir(d) else []
    used = {s: policies[s] for s in sources or [] if s in policies}
    found, n = scan_tree(argv, known_keys(policies), hex32=bool(echo_sources(used)))
    if found:
        print("SECRET FOUND - remove before sharing:")
        print("\n".join(found))
        sys.exit(1)
    print(f"OK: scanned {n} files; no API key or credential found.")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()

"""Build the upload zip for claude.ai (Settings > Capabilities > Skills > Upload) or for sharing: the skill zip
is a plain Agent Skills folder, so any assistant that reads SKILL.md can use it unzipped.

    python tools/package_skill.py [--out dist/data-analyst.zip] [--max-mb 30] [--max-files 200]
    python tools/package_skill.py --plugin [--out dist/data-analyst-plugin.zip]

The zip holds the skill folder `data-analyst/` without tests, fixtures, caches, out/ folders or
bytecode. Every pack's policy.json and endpoints.json go into one sources/packs.json (read by
core.packfiles), since claude.ai rejects a zip of more than 200 files. It prints the file count and size and
fails when the count passes --max-files, when the size passes --max-mb (set it to the limit your claude.ai
plan shows; the default is a conservative guess, not a documented limit) or when SKILL.md's frontmatter
breaks the skill rules (name <= 64 chars of a-z, 0-9 and hyphens; description <= 1024 chars).

With --plugin the zip is the plugin for claude.ai / Cowork (Customize > Plugins > Upload plugin): the same
skill files under `skills/data-analyst/` plus `.claude-plugin/plugin.json` at the zip root.
"""
import argparse
import json
import os
import re
import sys
import zipfile

sys.dont_write_bytecode = True                   # never write __pycache__ into the skill folder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "data-analyst")
SKILL = os.path.join(PLUGIN, "skills", "data-analyst")
SKIP_DIRS = {"tests", "__pycache__", "out", ".pytest_cache", "fixtures"}
SKIP_EXT = {".pyc", ".pyo"}
BUNDLED = ("policy.json", "endpoints.json")       # sources/<id>/<name> -> sources/packs.json


def frontmatter_problems() -> list[str]:
    s = open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---\n", s, re.S)
    if not m:
        return ["SKILL.md has no frontmatter"]
    fm = m.group(1)
    name = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc = re.search(r'^description:\s*"(.*)"$', fm, re.M)
    probs = []
    if not name or not re.fullmatch(r"[a-z0-9-]{1,64}", name.group(1).strip()):
        probs.append("name must be 1-64 characters of a-z, 0-9 and hyphens")
    if not desc or not (0 < len(desc.group(1)) <= 1024):
        probs.append(f"description must be 1-1024 characters (is {len(desc.group(1)) if desc else 0})")
    return probs


def _bundled(path: str) -> bool:
    rel = os.path.relpath(path, SKILL).split(os.sep)
    return len(rel) == 3 and rel[0] == "sources" and rel[2] in BUNDLED


def files():
    for dp, dns, fns in os.walk(SKILL):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for f in fns:
            p = os.path.join(dp, f)
            if os.path.splitext(f)[1] not in SKIP_EXT and not _bundled(p):
                yield p


def bundle() -> dict:
    """{"<id>": {"policy.json": {...}, "endpoints.json": {...}}} for every pack (core/packfiles.py reads it)."""
    out = {}
    src = os.path.join(SKILL, "sources")
    for pid in sorted(os.listdir(src)):
        for name in BUNDLED:
            p = os.path.join(src, pid, name)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    out.setdefault(pid, {})[name] = json.load(f)
    return out


def build(out: str, plugin: bool = False) -> int:
    """Write the zip; returns the number of files in it. plugin=True nests the skill in a plugin layout."""
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    top = "skills/data-analyst" if plugin else "data-analyst"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if plugin:
            z.write(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), ".claude-plugin/plugin.json")
            n += 1
        for f in files():
            z.write(f, top + "/" + os.path.relpath(f, SKILL).replace(os.sep, "/"))
            n += 1
        z.writestr(top + "/sources/packs.json", json.dumps(bundle(), sort_keys=True, separators=(",", ":")))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", action="store_true", help="build the claude.ai plugin zip instead of the skill zip")
    ap.add_argument("--out")
    ap.add_argument("--max-mb", type=float, default=30)
    ap.add_argument("--max-files", type=int, default=200)
    a = ap.parse_args()
    a.out = a.out or os.path.join(ROOT, "dist", "data-analyst-plugin.zip" if a.plugin else "data-analyst.zip")
    probs = frontmatter_problems()
    if probs:
        sys.exit("SKILL.md: " + "; ".join(probs))
    n = build(a.out, a.plugin)
    mb = os.path.getsize(a.out) / 1e6
    print(f"{a.out}: {n} files, {mb:.2f} MB")
    if n > a.max_files:
        sys.exit(f"the zip has more than --max-files {a.max_files} files (claude.ai rejects it)")
    if mb > a.max_mb:
        sys.exit(f"the zip is larger than --max-mb {a.max_mb}")


if __name__ == "__main__":
    main()

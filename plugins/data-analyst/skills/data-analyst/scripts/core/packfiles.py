"""Read a pack's JSON files (policy.json, endpoints.json) from its folder or from the upload bundle.

In the repo each pack keeps sources/<id>/policy.json and endpoints.json. The claude.ai upload zip
(tools/package_skill.py) replaces them with one sources/packs.json, {"<id>": {"policy.json": {...},
"endpoints.json": {...}}}, to stay under claude.ai's file limit. Runtime code reads them only through
read_json, so both layouts work. Standard library only.
"""
from __future__ import annotations

import json
import os

BUNDLE = "packs.json"
_bundles: dict[str, dict] = {}


def _bundle(sources_dir: str) -> dict:
    if sources_dir not in _bundles:
        path = os.path.join(sources_dir, BUNDLE)
        b = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                b = json.load(f)
        _bundles[sources_dir] = b
    return _bundles[sources_dir]


def read_json(pack_dir: str, name: str) -> dict | None:
    """<pack_dir>/<name> parsed, else that pack's entry in the bundle next to it, else None."""
    path = os.path.join(pack_dir, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    d = os.path.normpath(os.path.abspath(pack_dir))
    return (_bundle(os.path.dirname(d)).get(os.path.basename(d)) or {}).get(name)


def has(pack_dir: str, name: str) -> bool:
    if os.path.exists(os.path.join(pack_dir, name)):
        return True
    d = os.path.normpath(os.path.abspath(pack_dir))
    return name in (_bundle(os.path.dirname(d)).get(os.path.basename(d)) or {})


def is_pack(pack_dir: str) -> bool:
    return has(pack_dir, "policy.json")

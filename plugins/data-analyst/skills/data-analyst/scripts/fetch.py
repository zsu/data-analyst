#!/usr/bin/env python
"""data-analyst: one command line for every source pack.

Find sources
  python fetch.py --sources [--json]                  # every pack: id, title, key, depth, domains
  python fetch.py --route "real wages of federal workers since 2015"   # which packs answer this?

One source (spec.json format: the pack's source.md)
  python fetch.py --source <id> <out>/spec.json --out <out> --check          # pre-flight: which route works here?
  python fetch.py --source <id> <out>/spec.json --out <out>                  # download + package into <out>/data/<id>/
  python fetch.py --source <id> <out>/spec.json --out <out> --manual-bundle  # links + scripts for another computer
  python fetch.py --source <id> <out>/spec.json --out <out> --ingest [FILES] # package files the user downloaded
                                                                             # FILES: the uploaded files or their folder
  python fetch.py --source <id> --endpoints | --search "words" | --ping
  python fetch.py --source <id> --policy           # that pack's policy.json (clarify, notices, limits); any pack
  Deep packs (ipums, bls, fred, bea, treasury, opm) take their own options: --source <id> --help.

Several sources for one question (plan.json: {"description", "sources": [{"source": id, "spec": {...} | "file"}]})
  python fetch.py --plan <out>/plan.json --out <out> --check     # every source's route, one combined answer
  python fetch.py --plan <out>/plan.json --out <out>             # fetch every source that has a working route
  python fetch.py --plan <out>/plan.json --out <out> --ingest [FILES]
When some sources need manual download, --check writes ONE bundle covering all of them, so the user is
asked once.

Common options: --env local|ssh|cloud|sandbox (override the guess), --force (download again), --no-cache.
Exit codes: 0 ok; 2 source or spec error; 3 unexpected error or missing dependency; 4 blocked (follow the
printed route). Never invent data when a source cannot be reached: follow the route.
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import generic_rest as gr  # noqa: E402
from core import manual, package, packfiles, packs, routes  # noqa: E402
from core import policy as pol  # noqa: E402
from core.env import detect_environment  # noqa: E402
from core.http import SOURCES, DataError  # noqa: E402
from core.keys import redact  # noqa: E402

HERE = os.path.abspath(__file__)
PARALLEL = 4                                    # sources checked or fetched at once in a plan
READY_ROUTES = ("api", "api_keyless", "bulk", "reuse_existing")


def emit(obj, code: int = 0):
    print(redact(json.dumps(obj, indent=2, default=str)))
    sys.exit(code)


def load_spec(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        emit({"status": "error", "kind": "spec", "message": f"cannot read {path}: {e}"}, routes.EXIT_SPEC)


def expand_spec(spec: dict, path: str) -> dict:
    """for_each requests (standard packs) as one request per value; see core.generic_rest.expand_for_each."""
    if not any(r.get("for_each") for r in spec.get("requests") or [] if isinstance(r, dict)):
        return spec
    try:
        return gr.expand_for_each(spec, os.path.dirname(os.path.abspath(path)))
    except DataError as e:
        emit({"status": "error", "kind": "spec", "message": str(e)}, routes.EXIT_SPEC)


# ---------------------------------------------------------------------- listing and routing
def list_sources(as_json: bool):
    rows = []
    for p in packs.all_packs():
        P = p.policy
        probs = pol.validate(P, p.dir)
        rows.append({"id": p.id, "title": P["title"], "agency": P["agency"], "depth": P["depth"],
                     "key": P["auth"]["required"], "key_env": P["auth"].get("env"), "domains": P["domains"],
                     "cli": "own (deep pack)" if p.deep_cli else "generic", "policy_ok": not probs})
    if as_json:
        emit(rows)
    for r in rows:
        key = f"key {r['key']}" + (f" ({r['key_env']})" if r["key_env"] else "")
        print(f"{r['id']:<20} {r['depth']:<8} {key:<38} {r['title']}")
    sys.exit(0)


# ---------------------------------------------------------------------- standard packs
def std_ping(pack):
    def ping(key):
        if pack.has("ping"):
            return pack.adapter.ping(gr.client_for(pack, key=key))
        api = pack.policy["routes"].get("api", {})
        probe = api.get("probe")
        base = (pack.endpoints or {}).get("base_url") or api.get("base", "")
        host = lambda u: urllib.parse.urlsplit(u or "").netloc
        if host(probe) != host(base):                # never send a key to another host
            return {"auth_ok": True, "kind": "", "detail": "key not checked (the probe is on another host)"}
        try:
            gr.client_for(pack, key=key).get(probe)
            return {"auth_ok": True, "kind": "", "detail": "probe answered"}
        except DataError as e:
            return {"auth_ok": e.kind not in ("auth", "key_missing"), "kind": e.kind, "detail": str(e)}
    return ping


def items_for(pack, spec):
    if pack.has("manual_items"):
        return pack.adapter.manual_items(spec, pack)
    return gr.manual_items(pack, spec) if pack.endpoints else []


def key_only(pack, spec) -> list[str]:
    return pack.adapter.key_only_needs(spec) if pack.has("key_only_needs") else []


def std_check(pack, spec, spec_path, out, env, force, failed=None) -> dict:
    if failed is None and pack.has("refused_hosts"):    # a second host the spec needs refuses scripts
        why = pack.adapter.refused_hosts(spec)
        failed = {"route": "api", "kind": "blocked", "error": why} if why else None
    return routes.route_check(pack, spec, spec_path, out, env, force=force, key_only_needs=key_only(pack, spec),
                              ping=std_ping(pack), spec_problems=gr.check_spec(pack, spec) if pack.endpoints else
                              (gr.call_check_spec(pack, spec) if pack.has("check_spec") else []),
                              manual_items=lambda: items_for(pack, spec), failed=failed)


def std_fetch(pack, spec, spec_path, out, env, force, route_name="api") -> dict:
    ex = package.existing(out, pack.id, gr.collapse_for_each(spec))
    if ex and ex["matches"] and ex["complete"] and not ex["problems"] and not force:
        return {"status": "ok", "route": "reuse_existing", "data_dir": ex["path"], "retrieved": ex["retrieved"]}
    n0 = len(SOURCES)
    try:
        if pack.has("fetch"):
            tables, notes = pack.adapter.fetch(spec, pack, route_name)
        else:
            tables, notes = gr.fetch(pack, spec)
    except DataError as e:
        if e.kind in ("network", "key_missing", "auth", "blocked"):
            r = std_check(pack, spec, spec_path, out, env, force,
                          failed={"route": route_name, "kind": e.kind, "error": str(e)})
            r.update(status="blocked", error=str(e), kind=e.kind)
            emit(r, routes.EXIT_BLOCKED)
        emit({"status": "error", "source": pack.id, "kind": e.kind, "message": str(e)}, routes.EXIT_SPEC)
    truncated = [n for n in notes if "stopped at" in n]       # a row cap was hit: the tables are incomplete
    tables, spec = gr.merge_for_each(spec, tables), gr.collapse_for_each(spec)
    res = package.write(out, pack, tables, spec, route=route_name, acquired=route_name,
                        requests=SOURCES[n0:], notes=[n for n in notes if n not in truncated], problems=truncated)
    out_ = {"status": "partial" if truncated else "ok", "source": pack.id, "route": route_name,
            "data_dir": res["data_dir"], "tables": [{k: t[k] for k in ("file", "rows")} for t in res["tables"]],
            "notes": notes, "citation": res["citation"], "read_next": pack.source_md}
    if truncated:
        out_.update(truncated=truncated, action="These tables are incomplete. Narrow the filter or raise max_rows and "
                    "fetch again (--force); if the cap is the API's own limit, tell the user the result is partial. "
                    "Never present a truncated table as complete.")
    return out_


def std_ingest(pack, spec, out, paths) -> dict:
    items = items_for(pack, spec)
    if not items:
        emit({"status": "error", "source": pack.id, "message": "this pack has no manual items; see source.md"}, 2)
    found, missing = manual.match_uploads(paths, items)
    if missing:
        emit({"status": "incomplete", "source": pack.id, "missing": missing,
              "message": "Some expected files were not found in the upload; ask the user for them."}, routes.EXIT_BLOCKED)
    local = manual.copy_in(found, os.path.join(package.data_dir(out, pack.id), "raw"))
    tables = {}
    for r in spec["requests"]:
        files = [p for n, p in local.items() if n.split("#")[0] == r["name"]]
        if pack.has("parse_upload"):
            tables[r["name"]] = pack.adapter.parse_upload(pack, r, files)
        else:
            tables[r["name"]] = gr.parse_upload(pack, r, files)
    tables, spec = gr.merge_for_each(spec, tables), gr.collapse_for_each(spec)
    res = package.write(out, pack, tables, spec, route="manual_download", acquired="manual_download",
                        notes=["Files downloaded outside the API route (by the user, or saved from the agency's own "
                               "site) and ingested: say where they came from in the methods; an API-only notice "
                               "applies only if they came from the API."])
    return {"status": "ok", "source": pack.id, "route": "manual_download", "data_dir": res["data_dir"],
            "tables": [{k: t[k] for k in ("file", "rows")} for t in res["tables"]]}


def run_standard(pack, argv: list[str]):
    ap = argparse.ArgumentParser(prog=f"fetch.py --source {pack.id}")
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--manual-bundle", action="store_true")
    ap.add_argument("--ingest", nargs="*")
    ap.add_argument("--endpoints", action="store_true")
    ap.add_argument("--search")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--env")
    ap.add_argument("--route-override", default="api")
    a = ap.parse_args(argv)
    env = detect_environment(a.env)
    if a.endpoints:
        emit({"source": pack.id, "endpoints": gr.describe_endpoints(pack),
              "catalog": (pack.endpoints or {}).get("catalog", {}), "read": pack.source_md})
    if a.search:
        hits = pack.adapter.search(a.search, pack) if pack.has("search") else gr.search_catalog(pack, a.search)
        emit({"source": pack.id, "query": a.search, "results": hits})
    if a.ping:
        p = std_ping(pack)(None)
        emit({"source": pack.id, **p}, 0 if p["auth_ok"] else routes.EXIT_BLOCKED)
    if not a.spec or not a.out:
        ap.error("spec and --out are required")
    spec = expand_spec(load_spec(a.spec), a.spec)
    if a.check:
        r = std_check(pack, spec, os.path.abspath(a.spec), a.out, env, a.force)
        emit(r, 0 if r["route"] not in ("report_error", "fix_spec") else routes.EXIT_SPEC)
    if a.dry_run:
        probs = gr.check_spec(pack, spec) if pack.endpoints else []
        emit({"source": pack.id, "problems": probs, "manual_items": items_for(pack, spec)},
             routes.EXIT_SPEC if probs else 0)
    if a.manual_bundle:
        b = manual.write_bundle(a.out, [(pack, items_for(pack, spec))], "requested by the user", specs={pack.id: spec})
        emit({"status": "ok", **b})
    if a.ingest is not None:
        emit(std_ingest(pack, spec, a.out, a.ingest))
    emit(std_fetch(pack, spec, os.path.abspath(a.spec), a.out, env, a.force, a.route_override))


# ---------------------------------------------------------------------- deep packs
def run_deep(pack, argv: list[str]):
    """A deep pack keeps its own command line; it is run in this process with its folder on sys.path."""
    sys.argv = [pack.deep_cli, *argv]
    sys.path.insert(0, pack.dir)
    runpy.run_path(pack.deep_cli, run_name="__main__")


def deep_json(pack, argv: list[str]) -> tuple[int, dict]:
    """Run one source (deep or standard) in a subprocess and parse its JSON."""
    p = subprocess.run([sys.executable, HERE, "--source", pack.id, *argv], capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout[p.stdout.find("{"):])
    except ValueError:
        return p.returncode, {"status": "error", "source": pack.id, "message": redact((p.stderr or p.stdout)[-800:])}


# ---------------------------------------------------------------------- plans (several sources)
def plan_specs(plan_path: str, out: str) -> list[tuple]:
    plan = load_spec(plan_path)
    os.makedirs(os.path.join(out, "specs"), exist_ok=True)
    res = []
    ids = [s.get("source") for s in plan.get("sources") or []]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:                     # one source = one spec file and one data/<id>/ folder: two entries would overwrite
        emit({"status": "error", "kind": "spec", "message": f"plan.json lists {', '.join(dup)} more than once: merge "
              "those entries into one spec (one spec can hold several requests or series)"}, routes.EXIT_SPEC)
    for s in plan.get("sources") or []:
        pack = packs.load(s["source"])
        spec = s.get("spec")
        if isinstance(spec, str):
            sp = spec if os.path.isabs(spec) else os.path.join(os.path.dirname(os.path.abspath(plan_path)), spec)
            spec = expand_spec(load_spec(sp), sp)
        else:
            sp = os.path.join(out, "specs", f"{pack.id}.json")
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(spec, f, indent=2)
            spec = expand_spec(spec, os.path.join(out, "plan.json"))     # for_each paths: relative to <out>
        res.append((pack, spec, os.path.abspath(sp)))
    if not res:
        emit({"status": "error", "message": "plan.json needs a non-empty \"sources\" list"}, routes.EXIT_SPEC)
    return res


def plan_action(ready: list, blocked: list, bundles: bool) -> str:
    """What to do after a plan check, naming only what this check produced (a bundle only when one was written)."""
    if not blocked:
        return "Run without --check."
    by = {}
    for r in blocked:
        by.setdefault(r.get("route"), []).append(r.get("source"))
    asks = []
    if bundles:
        asks.append("send every bundle together (each one's INSTRUCTIONS.md, .sh and .ps1 download scripts and "
                    "spec.json / plan.json, via your file-sending tool) and give the one command per script for "
                    "their system (user_commands; each script asks for its key with hidden input)")
    if by.get("run_setup"):
        asks.append(f"give the setup command of {', '.join(by['run_setup'])} (need_user next_command) to run in their "
                    "own terminal (never ask for a key in chat), and offer each one's keyless alternative from its "
                    "source.md (Access), e.g. the agency's published file to upload")
    if by.get("install_deps"):
        asks.append(f"install the missing package for {', '.join(by['install_deps'])} if the network allows")
    rest = [s for k, v in by.items() if k not in ("run_setup", "install_deps", "fix_spec", "report_error",
                                                    *(("manual_download", "cloud_secret", "cloud_allowlist") if bundles else ()))
            for s in v]
    if rest:
        asks.append(f"for {', '.join(rest)}, follow each one's message in sources")
    text = "Fetch every ready source now (run without --check). " if ready else ""
    if asks:
        text += ("Then ask the user ONCE for everything in need_user: " + "; ".join(asks) + ". After uploads run "
                 "--plan ... --ingest (or --source <id> <spec> --ingest FILES); after setup, rerun --plan ... --check. ")
    if by.get("fix_spec"):
        text += f"Fix the spec of {', '.join(by['fix_spec'])} (its message says what), then rerun --check. "
    if by.get("report_error"):
        text += f"{', '.join(by['report_error'])}: tell the user what the message says and stop for that source. "
    return text.strip()


def run_plan(a):
    out, env = os.path.abspath(a.out), detect_environment(a.env)
    items = plan_specs(a.plan, out)
    extra = (["--force"] if a.force else []) + (["--env", a.env] if a.env else [])
    if a.check:
        def one(item):
            pack, spec, sp = item
            if pack.deep_cli:
                return deep_json(pack, [sp, "--out", out, "--check", *extra])[1]
            # no per-source bundle here: one combined bundle is written below
            return routes.route_check(pack, spec, sp, out, env, force=a.force, key_only_needs=key_only(pack, spec),
                                      ping=std_ping(pack), spec_problems=gr.check_spec(pack, spec) if pack.endpoints else [])
        with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            results = list(ex.map(one, items))
        manual_groups = [(pack, items_for(pack, spec)) for (pack, spec, _), r in zip(items, results)
                         if not pack.deep_cli and r.get("route") in ("manual_download", "cloud_secret", "cloud_allowlist")]
        if manual_groups:
            b = manual.write_bundle(out, manual_groups, "some sources cannot be downloaded from here",
                                   specs={pack.id: spec for (pack, spec, _), r in zip(items, results)
                                          if any(g[0] is pack for g in manual_groups)})
        deep_bundles = [r.get("manual_bundle") for r in results
                        if r.get("route") in ("manual_download", "cloud_secret", "cloud_allowlist") and r.get("manual_bundle")]
        routes_seen = {r.get("route") for r in results}
        ready = [r["source"] for r in results if r.get("route") in READY_ROUTES]
        blocked = [r for r in results if r.get("source") not in ready]
        summary = {"status": "plan_check", "environment": env, "sources": results, "ready": ready,
                   "need_user": [{"source": r.get("source"), "route": r.get("route"), "message": r.get("message"),
                                  "next_command": r.get("next_command")} for r in blocked]}
        if manual_groups:
            summary["manual_bundle"] = b["dir"]
            summary["instructions"] = b["instructions"]
            summary.update(download_scripts=b["scripts"], request_spec=b["spec"], user_commands=b["commands"])
        if deep_bundles:
            summary["other_bundles"] = deep_bundles
        summary["action"] = plan_action(ready, blocked, bool(manual_groups or deep_bundles))
        emit(summary, 0 if not routes_seen & {"fix_spec", "report_error"} else routes.EXIT_SPEC)
    def fetch_one(item):
        pack, spec, sp = item
        args = [sp, "--out", out, "--ingest", *a.ingest] if a.ingest is not None else [sp, "--out", out, *extra]
        c, r = deep_json(pack, args)              # every source in its own process: no shared state
        r.setdefault("source", pack.id)
        return c, r
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        done = list(ex.map(fetch_one, items))
    results = [r for _, r in done]
    code = max(c for c, _ in done)
    emit({"status": "plan_fetch", "results": results,
          "next": "Read each source's source.md caveats, then align units, periods (fiscal vs calendar), "
                  "geography and vintages before joining (see SKILL.md, Combining sources)."}, code)


def main():
    argv = sys.argv[1:]
    if "--source" in argv:
        i = argv.index("--source")
        if i + 1 >= len(argv):
            emit({"status": "error", "message": "--source needs a pack id (see --sources)"}, routes.EXIT_SPEC)
        pack = packs.load(argv[i + 1])
        probs = pol.validate(pack.policy, pack.dir)
        if probs:
            emit({"status": "error", "source": pack.id, "message": "policy.json is invalid: " + "; ".join(probs)}, 3)
        rest = argv[:i] + argv[i + 2:]
        if "--clarify" in rest:       # just the open choices and the statements an answer must carry
            P = packfiles.read_json(pack.dir, "policy.json")
            emit({"source": pack.id, "clarify": P.get("clarify") or [], "notices": P.get("notices") or [],
                  "audit_hooks": [{k: v for k, v in h.items() if k in ("id", "kind", "any", "message")
                                   or k.startswith("when")}      # when_route, when_data: when it applies
                                  for h in P.get("audit_hooks") or []]})
        if "--policy" in rest:        # the upload zip bundles every policy.json: read one here, never the bundle
            emit({"source": pack.id, "policy": packfiles.read_json(pack.dir, "policy.json")})
        return run_deep(pack, rest) if pack.deep_cli else run_standard(pack, rest)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--route")
    ap.add_argument("--plan")
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ingest", nargs="*")
    ap.add_argument("--env")
    a = ap.parse_args(argv)
    if a.sources:
        list_sources(a.json)
    if a.route:
        emit({"question": a.route, "candidates": packs.route(a.route),
              "next": "Split the question into measures; give each one a source (references/routing.md lists the "
                      "usual pairings); read each chosen pack's source.md; write plan.json for more than one source."})
    if a.plan:
        if not a.out:
            ap.error("--plan needs --out")
        return run_plan(a)
    ap.print_help()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:                      # never leak a key in a traceback
        print(json.dumps({"status": "error", "kind": "unexpected", "message": redact(f"{type(e).__name__}: {e}")}))
        sys.exit(routes.EXIT_UNEXPECTED)

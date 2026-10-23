"""The pre-flight route check: which way gets this source's data here? (standard library only)

Every pack (standard packs through this module, deep packs through their own fetch.py) answers --check
with one JSON object: route, message (what to tell the user), action (what the model does next),
next_command, plus what was probed. Routes, first match wins:

  reuse_existing   <out>/data/<source>/ already holds exactly this request
  api              the API answers and the key (if any is needed) works
  api_keyless      no working key, but a keyless route (graph CSVs, bulk files, v1 API) answers and the
                   request needs no key-only feature; the message says what a key would add
  bulk             the bulk-file host answers (no key), the API does not or is not needed
  run_setup        a key is needed; the user has a terminal (local, ssh): give them the setup command
  cloud_secret     a key is needed in a Claude Code cloud session: add it to the environment's secrets
                   (a manual bundle is written as the other option)
  cloud_allowlist  a cloud session cannot reach the host: allow it in the network settings, or use the bundle
  manual_download  the analysis environment cannot get the data (sandbox, claude.ai with network off or the
                   host not on the organization's allow-list, or a key that cannot be stored here): the user
                   downloads the files with the bundle and uploads them; then --ingest
  fix_spec         the request names an endpoint, parameter or value the source does not have
  report_error     the source cannot be reached from the user's own machine
Routes are chosen from what was actually reached and which key works, never from the environment's name;
the environment only decides how a key or a file can be supplied.

Exit codes for every command: 0 ok, 2 source or spec error, 3 unexpected error or missing dependency,
4 blocked (the printed JSON has the route to follow).
"""
from __future__ import annotations

import os
import sys

from core import keys, package
from core.env import setup_command
from core.http import reachable

EXIT_OK, EXIT_SPEC, EXIT_UNEXPECTED, EXIT_BLOCKED = 0, 2, 3, 4
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fetch.py")


# What the model does with a manual-download bundle (every pack says the same).
SEND_BUNDLE = ("Send INSTRUCTIONS.md, both download scripts (download_scripts: .sh for macOS/Linux, .ps1 for Windows) and "
               "the request (request_spec) with present_files / SendUserFile when available, and print their full "
               "paths. Give the user the one command for their system (user_commands; both when unsure): it runs the "
               "script, which asks for any key with hidden input and downloads every file. Never ask for a key in the "
               "chat. Wait for the upload, then run next_command.")


def cmd(*parts: str) -> str:
    # the interpreter running now: "python" may not exist (only python3, or py on Windows)
    return " ".join([f'"{sys.executable}" -B "{HERE}"' if sys.executable else f'python -B "{HERE}"', *parts])


def probe(pack) -> dict:
    """Reachability of each network route the pack declares (one cheap request each)."""
    res = {}
    for name in ("api", "api_keyless", "bulk"):
        r = pack.policy["routes"].get(name)
        if r and r.get("probe"):
            ok, why = reachable(r["probe"])
            res[name] = {"reachable": ok, "detail": why}
    return res


def route_check(pack, spec: dict, spec_path: str, out: str, env: str, force: bool = False,
                key_only_needs: list[str] | None = None, ping=None, spec_problems: list[str] | None = None,
                manual_items=None, failed: dict | None = None) -> dict:
    """ping(key) -> {"auth_ok": bool, "kind": str, "detail": str}; manual_items() -> [item, ...].
    failed = {"route", "kind", "error"} after a fetch by that route failed: the route is not offered again
    (a probe can answer while the real request is blocked, e.g. HTTP 403 from a bot filter, or a key the
    probe never checks)."""
    from core import manual
    P, a = pack.policy, pack.policy["auth"]
    out = os.path.abspath(out)
    base = f'--source {pack.id} "{spec_path}" --out "{out}"'
    run = cmd(base)
    ingest = cmd(base, "--ingest", "<the uploaded files or their folder>")
    key, key_src = keys.find_auth_key(a)
    needs = list(key_only_needs or [])
    r = {"status": "route_check", "source": pack.id, "environment": env, "os": sys.platform,
         "restricted": keys.restricted(), "key_env": a.get("env"), "key_source": key_src,
         "key_required": a["required"], "key_ok": None, "key_only_needs": needs, "probed": {},
         "existing_data": None, "route": None, "message": "", "action": "", "next_command": ""}

    def done(route, message, action, next_command=""):
        r.update(route=route, message=message, action=action, next_command=next_command)
        return r

    def bundle(reason):
        items = manual_items() if manual_items else []
        if not items:
            r["manual_how"] = P["routes"]["manual"]["how"]
            return None
        b = manual.write_bundle(out, [(pack, items)], reason, specs={pack.id: spec})
        r.update(manual_bundle=b["dir"], instructions=b["instructions"], download_scripts=b["scripts"],
                 request_spec=b["spec"], user_commands=b["commands"])
        return b["dir"]

    if spec_problems:
        return done("fix_spec", "The request has problems: " + "; ".join(spec_problems),
                    "Fix spec.json (see the pack's source.md and --endpoints), then run --check again.")
    ex = package.existing(out, pack.id, spec)
    r["existing_data"] = ex
    if ex and ex["matches"] and ex["complete"] and not ex["problems"] and not force:
        stale = ex["age_days"] is not None and ex["age_days"] >= 7
        return done("reuse_existing",
                    f"The data for this request are already in {ex['path']} (retrieved {ex['retrieved'][:10]} by the "
                    f"{ex['route']} route)." + (" The source has probably published newer data since." if stale else ""),
                    "Skip the download and continue with the analysis." +
                    (" Ask whether to refresh (--force) if the latest value matters." if stale else ""))

    pr = probe(pack)
    if failed and failed.get("kind") == "network" and failed.get("route") in pr:
        pr[failed["route"]] = {**pr[failed["route"]], "reachable": False,
                               "detail": f"the request failed: {keys.redact(str(failed.get('error', '')))[:200]}"}
    if failed and failed.get("kind") in ("auth", "key_missing") and failed.get("route") == "api" and key:
        ping = lambda _k: {"auth_ok": False, "kind": "auth", "detail": keys.redact(str(failed.get("error", "")))}
    r["probed"] = pr
    api_ok = pr.get("api", {}).get("reachable", False)
    kl_ok = pr.get("api_keyless", {}).get("reachable", False)
    bulk_ok = pr.get("bulk", {}).get("reachable", False)
    why = "; ".join(f"{k}: {v['detail']}" for k, v in pr.items() if not v["reachable"])
    key_needed = a["required"] == "required" or bool(needs)
    signup = a.get("signup_url") or ""

    if api_ok and a["required"] == "none":
        return done("api", f"{P['title']} answers from here; no key is needed.", "Run next_command.", run)
    if api_ok and key:
        p = ping(key) if ping else {"auth_ok": True, "kind": "", "detail": "not checked"}
        r.update(key_ok=p["auth_ok"], key_detail=p.get("detail", ""))
        if p["auth_ok"]:
            return done("api", f"A {a['env']} key was found ({key_src}) and {P['title']} answers.", "Run next_command.", run)
        if p.get("kind") not in ("auth", "key_missing"):
            return done("report_error", f"The key check failed: {p.get('detail', '')}", "Tell the user and stop.")
    if api_ok and a["required"] == "optional" and not needs and not key:
        return done("api", f"{P['title']} answers from here without a key (a free key at {signup} raises the "
                    "rate limits).", "Run next_command. Mention once that a key raises the limits.", run)
    rejected = r["key_ok"] is False
    lead = (f"{P['title']} rejected the {a['env']} key found in {key_src}. " if rejected else
            f"No {a['env']} key is set up. " if key_needed and not key else "")

    if (kl_ok or bulk_ok) and not needs:
        route = "api_keyless" if kl_ok else "bulk"
        what = ", ".join(a.get("key_only") or []) or "higher limits"
        return done(route, lead + f"The {route.replace('_', ' ')} route answers, so the data can come without a key."
                    + (f" A free key ({signup}) adds: {what}." if signup else ""),
                    "Run next_command now. Tell the user once what a key adds; do not wait for it.",
                    cmd(base, "--route-override", route))

    if api_ok or kl_ok or bulk_ok:                   # reachable, but a key is needed
        need = (f"This request needs a {a['env']} key" + (f" for: {'; '.join(needs)}" if needs else "") +
                f". A free key is at {signup}.")
        if env in ("local", "ssh"):
            path, setup = setup_command(pack.id)
            r.update(setup_script=path, run_command=setup)
            return done("run_setup", lead + need + " The setup script asks for it with hidden input, checks it and "
                        "stores it in Claude Code's settings.json.",
                        "Give the user run_command to run in their own terminal (never run it for them, never ask for "
                        "the key in chat). Offer any keyless alternative. When they say it is stored, run --check again.",
                        setup)
        b = bundle(f"the request needs a {a['env']} key")
        if env == "cloud":
            return done("cloud_secret", lead + need + f" Add {a['env']} to the cloud environment's secrets and restart "
                        "the session" + (f", or download on your own computer with the files in {b}." if b else "."),
                        "Explain both options. If the user downloads by hand: " + SEND_BUNDLE, ingest)
        return done("manual_download", lead + need + " A key cannot be stored in this environment (claude.ai or another "
                    "sandbox), so the files must be downloaded where the key is: " +
                    (f"the links and scripts are in {b}." if b else P["routes"]["manual"]["how"]) +
                    " Alternatively run the request in Claude Code on your own computer.",
                    SEND_BUNDLE, ingest)

    if env in ("local", "ssh") and not keys.restricted():
        return done("report_error", f"{P['title']} could not be reached from this computer ({why}). Check the "
                    "internet connection, VPN and proxy settings (HTTPS_PROXY), then try again.",
                    "Tell the user and stop. Do not substitute data from memory.")
    b = bundle(f"{P['title']} cannot be reached from the analysis environment")
    hosts = sorted({h for v in P["routes"].values() if isinstance(v, dict) for h in v.get("hosts", [])})
    if env == "cloud":
        return done("cloud_allowlist", f"This cloud session cannot reach {P['title']} ({why}). Allow "
                    f"{', '.join(hosts) or 'its hosts'} in the environment's network settings and restart, or download "
                    "by hand" + (f" with {b}." if b else f": {P['routes']['manual']['how']}"),
                    "Offer both options. For the manual one: " + SEND_BUNDLE,
                    ingest)
    return done("manual_download", f"This {env} environment cannot reach {P['title']} ({why}). On claude.ai an "
                f"organization admin can allow {', '.join(hosts) or 'the host'} in the network settings; otherwise "
                + (f"the download links and scripts are in {b}: run one on your own computer and upload the files."
                   if b else P["routes"]["manual"]["how"]),
                SEND_BUNDLE, ingest)

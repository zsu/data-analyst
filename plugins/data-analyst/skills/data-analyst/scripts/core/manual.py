"""Manual-download bundles and ingest, for when the analysis environment cannot reach a source
(sandbox without network, an Enterprise allow-list without the host, a cloud session without the key).
Standard library only.

    b = manual.write_bundle(out, [(pack, items)], reason, specs={pack.id: spec})   # one bundle for every source
    found = manual.match_uploads(paths, items)                                      # after the user uploads

An item is {"name", "url", "filename", "needs_key": bool, "method": "GET"|"POST", "body": {...},
"note": str}. A URL that needs a key carries the placeholder {API_KEY}; the scripts fill it on the user's own
computer (from the pack's key variable, or a hidden prompt), so the key never reaches the analysis
environment or the chat. The bundle (<out>/manual_download/) holds:
  INSTRUCTIONS.md                       one command per system, the links, what to upload
  download_data.sh / download_data.ps1  self-contained scripts with every request built in (curl / Windows
                                        PowerShell, no Python); each asks for a key with hidden input and saves
                                        every file next to itself
  spec.json (one source) or plan.json   the request, for reference
  manifest.json                         the items, machine-readable (for --ingest and the report builder)
The bundle lands in the analysis folder (--out), which the model hands over with its file-sending tool;
after the upload the model passes the uploaded files (or their folder) to --ingest. With no paths, ingest
searches DATA_ANALYST_UPLOADS (core.env.upload_dirs).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil

from core.env import upload_dirs

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
SCRIPT_KINDS = {"bash": ".sh", "powershell": ".ps1"}


def bundle_dir(out: str) -> str:
    return os.path.join(os.path.abspath(out), "manual_download")


def safe_name(name: str) -> str:
    """A file name the scripts can write on any system (ASCII, no path separators or tabs)."""
    return re.sub(r"[^A-Za-z0-9._()+ -]", "_", str(name)).strip() or "file"


def _item(pack, it: dict) -> dict:
    a = pack.policy["auth"]
    needs = bool(it.get("needs_key"))
    x = {"source": pack.id, "name": it["name"], "url": it["url"], "filename": safe_name(it["filename"]),
         "method": it.get("method", "GET"), "note": it.get("note", ""), "needs_key": needs,
         "optional": bool(it.get("optional")),
         "key_env": a.get("env") if needs else None, "signup_url": a.get("signup_url") if needs else None}
    if it.get("body") is not None:
        x["body"] = it["body"]
    if a.get("extra"):
        x["extra_env"] = sorted(a["extra"].values())
    if needs and a.get("placement") == "body":
        x.update(body_has_key=True, key_param=a.get("param"))
    if needs and a.get("placement") == "header":
        x.update(key_header=a.get("param"), key_prefix=a.get("prefix") or "")
    return x


def _ascii(s) -> str:
    """Percent-encode what would break a script line or a Windows PowerShell 5.1 file (non-ASCII, blanks)."""
    out = []
    for c in str(s):
        if 32 < ord(c) < 127:
            out.append(c)
        else:
            out.append("".join(f"%{b:02X}" for b in c.encode("utf-8")))
    return "".join(out)


def script_rows(items: list[dict]) -> tuple[list[str], list[str]]:
    """(values, requests): the tab-separated blocks the script templates embed. "-" marks an empty field."""
    values, seen, rows = [], set(), []
    for it in items:
        needs = bool(it.get("needs_key")) and bool(it.get("key_env"))
        if needs and it["key_env"] not in seen:
            seen.add(it["key_env"])
            values.append("\t".join([it["key_env"], "secret", "free key: " + _ascii(it.get("signup_url") or "")]))
        extras = [v for v in it.get("extra_env") or [] if "{" + v + "}" in it["url"]]
        for var in extras:
            if var not in seen:
                seen.add(var)
                values.append("\t".join([var, "plain", "the value registered with your key"]))
        place = ("body" if it.get("body_has_key") else "header" if it.get("key_header") else "query") if needs else "none"
        body = it.get("body")
        if body is not None and place == "body":
            body = {**body, it["key_param"]: "{API_KEY}"}
        fields = [safe_name(it["filename"]), str(it.get("method") or "GET").upper(), "1" if it.get("optional") else "0",
                  it["key_env"] if needs else "", place,
                  (it.get("key_header") or it.get("key_param") or "") if needs else "",
                  (it.get("key_prefix") or "") if place == "header" else "",
                  ",".join(extras), _ascii(it["url"]),
                  json.dumps(body, ensure_ascii=True, separators=(",", ":")) if body is not None else ""]
        rows.append("\t".join(f.replace("\t", " ").replace("\r", " ").replace("\n", " ") or "-" for f in fields))
    return values, rows


def write_scripts(d: str, items: list[dict], stem: str = "download_data") -> dict:
    """Write <stem>.sh and <stem>.ps1 into d with every item built in. Returns {"bash": path, "powershell": path}.

    items are in the manifest shape (_item). The scripts need only curl (bash) or Windows PowerShell 5.1, ask
    for each key with hidden input unless its environment variable is set, and save the files next to
    themselves."""
    os.makedirs(d, exist_ok=True)
    values, rows = script_rows(items)
    paths = {}
    for kind, ext in SCRIPT_KINDS.items():
        with open(os.path.join(ASSETS, "download_data" + ext), encoding="ascii") as f:
            text = f.read()
        text = (text.replace("@STEM@", stem).replace("@VALUES@", "\n".join(values))
                .replace("@ITEMS@", "\n".join(rows)))           # last, so the requests are never rewritten
        path = os.path.join(d, stem + ext)
        # bash needs LF line ends even when the bundle is written on Windows; PowerShell 5.1 prefers CRLF
        with open(path, "w", encoding="ascii", newline="\n" if kind == "bash" else "\r\n") as f:
            f.write(text)
        if kind == "bash":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        paths[kind] = path
    return paths


def run_lines(stem: str, folder: str = "") -> dict:
    """The one command per system that runs <stem>.sh / <stem>.ps1: from the Downloads folder, or from `folder`
    (relative to where the command is run, e.g. "reproduce/ipums")."""
    if folder:
        f = folder.strip("/\\").replace("\\", "/")
        win = f.replace("/", "\\")
        return {"bash": f"bash {f}/{stem}.sh",
                "powershell": f'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\{win}\\{stem}.ps1"'}
    return {"bash": f"bash ~/Downloads/{stem}.sh",
            "powershell": f'powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\\Downloads\\{stem}.ps1"'}


def run_section(stem: str, keys: list | None = None, folder: str = "", spec_name: str = "") -> list[str]:
    """Markdown lines: the one-command instructions for <stem>.sh / <stem>.ps1. keys: [(env var, signup url)]."""
    c = run_lines(stem, folder)
    if folder:
        lead = f"From the folder that holds `{folder}/`, copy and paste the command for your system:"
        d = run_lines(stem)
        tail = (f"If you downloaded only the script (for example from the report page), run it where you saved it "
                f"instead: `{d['bash']}` or `{d['powershell']}`.")
    else:
        lead = (f"Save `{stem}.sh` (macOS, Linux) or `{stem}.ps1` (Windows) in your Downloads folder, then copy and "
                "paste the command for your system into a terminal:")
        tail = "If you saved the script in another folder, put that folder in the command instead of Downloads."
    lines = [lead, "", "macOS, Linux, Git Bash or WSL (needs only curl):", "", "```bash", c["bash"], "```", "",
             "Windows PowerShell:", "", "```powershell", c["powershell"], "```", "", tail, ""]
    ref = f" (`{spec_name}` holds the same request, for reference)" if spec_name else ""
    lines += [f"The requests are built into the script{ref}. It saves every file next to itself and prints their "
              "full paths when it finishes.", ""]
    if keys:
        lines += ["It needs your own free API key: the script asks for it with hidden input (or uses the environment "
                  "variable when it is set in that terminal), and the key goes only to the source. Never paste a "
                  "key into the chat.", ""]
        lines += [f"- `{env}`: get one at {url}" for env, url in keys] + [""]
    return lines


def write_spec(d: str, specs: dict | None) -> str | None:
    """spec.json (one source) or plan.json (several) in d; returns its file name."""
    if not specs:
        return None
    if len(specs) == 1:
        name, data = "spec.json", next(iter(specs.values()))
    else:
        name, data = "plan.json", {"sources": [{"source": k, "spec": v} for k, v in specs.items()]}
    with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return name


def write_bundle(out: str, groups: list[tuple], reason: str, specs: dict | None = None) -> dict:
    """groups: [(pack, [item, ...]), ...]; specs: {pack id: spec} for spec.json / plan.json.
    Returns {"dir", "instructions", "scripts", "spec", "commands", "items"}."""
    d = bundle_dir(out)
    os.makedirs(d, exist_ok=True)
    items = [_item(p, it) for p, its in groups for it in its]
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"reason": reason, "items": items}, f, indent=2)
    if os.path.exists(os.path.join(d, "download_data.py")):      # written by older versions
        os.remove(os.path.join(d, "download_data.py"))
    scripts = write_scripts(d, items)
    spec_name = write_spec(d, specs)
    keyed = sorted({(i["key_env"], i["signup_url"]) for i in items if i["needs_key"]})
    lines = ["# Download the data on your own computer", "",
             f"The analysis environment cannot download these files itself ({reason}).",
             "Get them with one command on your own computer, then upload every downloaded file to the chat (or "
             "zip the folder and upload the zip).", "", "## Option A: one command (recommended)", ""]
    lines += run_section("download_data", keyed, spec_name=spec_name or "")
    lines += ["## Option B: open each link in a browser", "",
              "Save each file under the name shown. Links that need a key are marked; replace `{API_KEY}` with yours "
              "in the browser only.", ""]
    for i in items:
        mark = " (needs key)" if i["needs_key"] else ""
        how = "" if i["method"] == "GET" else f" (a {i['method']} request: use Option A)"
        mark += " (optional)" if i.get("optional") else ""
        lines.append(f"- **{i['filename']}** [{i['source']}]{mark}{how}: {i['url']}" + (f"  \n  {i['note']}" if i["note"] else ""))
    lines += ["", "## Upload", "",
              "Upload every downloaded file (do not rename or edit them). The assistant then reads them and continues."]
    with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"dir": d, "instructions": os.path.join(d, "INSTRUCTIONS.md"), "scripts": scripts,
            "spec": os.path.join(d, spec_name) if spec_name else None, "commands": run_lines("download_data"),
            "items": items}


def candidate_files(paths: list[str] | None) -> list[str]:
    """Files the user uploaded: the given files or folders, else the DATA_ANALYST_UPLOADS folders."""
    roots = paths or upload_dirs()
    out = []
    for p in roots:
        if os.path.isdir(p):
            out += [f for f in glob.glob(os.path.join(p, "**", "*"), recursive=True) if os.path.isfile(f)]
        elif os.path.isfile(p):
            out.append(p)
    return out


def match_uploads(paths: list[str] | None, items: list[dict]) -> tuple[dict, list[str]]:
    """Map each expected item to an uploaded file: exact filename, then the same name with a browser
    suffix ("file (1).csv"), then a unique file with the same extension. Returns (found, missing names)."""
    files = candidate_files(paths)
    by_name = {os.path.basename(f).lower(): f for f in files}
    found, missing = {}, []
    for it in items:
        want = safe_name(it["filename"]).lower()
        stem, ext = os.path.splitext(want)
        exts = (ext, *[e.lower() for e in it.get("also_ext") or ()])    # another format the pack can read
        hit = by_name.get(want) or next((f for n, f in by_name.items() if n.startswith(stem) and n.endswith(exts)),
                                        None)
        if hit:
            found[it["name"]] = hit
        elif not it.get("optional"):
            missing.append(it["filename"])
    if missing and len(items) == 1 and len(files) == 1:
        found[items[0]["name"]], missing = files[0], []
    return found, missing


def copy_in(found: dict, dest: str) -> dict:
    os.makedirs(dest, exist_ok=True)
    res = {}
    for name, src in found.items():
        tgt = os.path.join(dest, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(tgt):
            shutil.copy2(src, tgt)
        res[name] = tgt
    return res

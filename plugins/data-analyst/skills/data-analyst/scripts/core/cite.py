"""Snapshots of the pages a method record cites, so "opened": true is a fact a tool wrote, not a claim.

    python fetch.py --cite <url> --id <ref id> --out <out>                    # download the page
    python fetch.py --cite <url> --id <ref id> --out <out> --from-file <path> # the text your fetch tool returned

Each call saves <out>/references/<id>.<ext> and records it in <out>/references/opened.json:
{"<id>": {"url", "file", "sha256", "bytes", "opened" (UTC time), "via": "download" | "file", "content_type"}}.
The audit (scripts/report/method_audit.py) accepts "opened": true on a reference only with such a record
whose file still has that hash, and looks for the reference's "quote" in it. The snapshots stay in the
analysis folder: the replication package does not carry them (third-party pages).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.http import UA, open_url

ID_RE = re.compile(r"[A-Za-z0-9_-]+")
MAX_BYTES = 30 * 1024 * 1024
EXT = (("html", ".html"), ("pdf", ".pdf"), ("json", ".json"), ("xml", ".xml"), ("text/plain", ".txt"),
       ("markdown", ".md"))


def _ext(content_type: str, body: bytes) -> str:
    ct = (content_type or "").lower()
    for k, e in EXT:
        if k in ct:
            return e
    if body[:5] == b"%PDF-":
        return ".pdf"
    head = body[:300].lstrip().lower()
    return ".html" if head.startswith((b"<!doctype html", b"<html")) else ".txt"


def _record(out: str, ref_id: str, url: str, data: bytes, ext: str, via: str, content_type: str) -> dict:
    d = os.path.join(out, "references")
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):                      # one snapshot per reference: drop an earlier one in another format
        if os.path.splitext(f)[0] == ref_id and f != ref_id + ext:
            os.remove(os.path.join(d, f))
    rel = f"references/{ref_id}{ext}"
    with open(os.path.join(out, rel), "wb") as f:
        f.write(data)
    log_path = os.path.join(d, "opened.json")
    log = {}
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            log = json.load(f)
    rec = {"url": url, "file": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
           "opened": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "via": via,
           "content_type": content_type}
    log[ref_id] = rec
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
    return rec


def cite(url: str, ref_id: str, out: str, from_file: str | None = None, timeout: float = 30) -> dict:
    """Save the page and record it; returns the JSON fetch.py prints (status ok or error)."""
    if not ID_RE.fullmatch(ref_id or ""):
        return {"status": "error", "message": f"--id {ref_id!r}: letters, digits, _ and - only (the [@id] you cite)"}
    if not re.match(r"(?i)^https?://", url or ""):
        return {"status": "error", "message": "--cite needs the page's http(s) URL (the url in references)"}
    if from_file:
        if not os.path.isfile(from_file):
            return {"status": "error", "message": f"--from-file {from_file}: no such file"}
        with open(from_file, "rb") as f:
            data = f.read(MAX_BYTES + 1)
        if not data.strip():
            return {"status": "error", "message": f"--from-file {from_file} is empty: save the text you read"}
        ext = os.path.splitext(from_file)[1].lower() or ".txt"
        ext = ext if ext in {e for _, e in EXT} else ".txt"
        rec = _record(out, ref_id, url, data[:MAX_BYTES], ext, "file", "")
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with open_url(req, timeout) as r:
                data = r.read(MAX_BYTES + 1)
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return {"status": "error", "url": url, "message": f"HTTP {e.code} opening {url}: check the URL; never "
                    "cite a page you could not open"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"status": "error", "url": url, "message": f"could not open {url} from here ({e}). If your own "
                    "fetch tool can read the page, save the text it returned to a file and rerun with --from-file "
                    "<path>; otherwise do not mark the reference opened"}
        rec = _record(out, ref_id, url, data[:MAX_BYTES], _ext(ctype, data), "download", ctype)
    return {"status": "ok", "id": ref_id, **rec,
            "next": f"in decisions.json references: {{\"id\": \"{ref_id}\", \"citation\": ..., \"url\": \"{url}\", "
                    f"\"accessed\": \"{rec['opened'][:10]}\", \"opened\": true, \"used_for\": ..., \"quote\": "
                    "\"<a sentence from the page that supports the use>\"}"}

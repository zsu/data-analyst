#!/usr/bin/env python
"""Build a single self-contained HTML report or paper, and replication_package.zip, from report.json.

Usage:  python build_report.py <out>/report.json [-o report.html]

The HTML has no external dependencies (no CDN, fonts, or images). Charts are drawn by an inline script at
the reader's screen width, with hover tooltips, a legend, a data table and a CSV download for every
exhibit. Light and dark mode follow the OS setting. The AI-assistance disclaimer (assets/disclaimer.txt)
is always printed at the top of the page. Nothing here speaks for an organization: "organization" is
optional and only shown when given.

One builder for every source pack. What differs by source comes from each source's policy.json
(scripts/core/policy.py), for every source the report uses: report.json "sources" (ids), else every
data/<id>/ folder under <out>:
  notices         every used source's notices go into Data and Methods and the page footer (FRED and BEA API
                  notices, the BLS statement)
  redistribution  data/<id>/ files go into the ZIP by the source's rule (scripts/report/policyset.py):
                  allowed -> all; aggregates_only -> not the microdata_globs files; definition_only (IPUMS)
                  -> only package_include files, never microdata, plus reproduce/<id>/ (download_<id>.sh/.ps1:
                  one command downloads the data under the reader's own account, with the request built in;
                  the request as spec.json; REPRODUCE.md), all attached to the page with Download buttons
  withhold        redistribution.withhold drops the rows of withheld keys from shared CSVs (FRED copyright);
                  a report.json "clearance" entry {"source", "key", "basis", "redistribute": true} (or the
                  FRED-style "copyright_clearance" {"series_id", ...}) lifts it for one key
  vintage         the Data Vintage block (retrieval date, route, vintages, latest period, preliminary values,
                  gaps, series table and citations) from data/<id>/request.json or the pack's provenance file
  size            data files over 50 MB stay out of the ZIP with a note
  lib             scripts/analysis/*.py -> <out>/lib/, each used pack's sources/<id>/lib/*.py -> <out>/lib/<id>/,
                  and lib/run_analysis.py, so `python lib/run_analysis.py` reruns analysis.py from the package
Nothing is ever written inside the skill folder.

Both modes follow references/style_guide.md and references/report_patterns.md:
  "mode": "report"  (default) exhibit chartbook: cover (optional hero chart), At a Glance, Contents, List of
                    Exhibits, Notes About This Report, Introduction, sections that each open with a summary
                    and then show one exhibit per page with narrative beside the chart, Appendix A: Data and
                    Methods (+ optional appendixes), References, About This Document. Replication materials
                    and the audit trail follow as supplementary material.
  "mode": "paper"   prose-led publication: summary paragraphs with bold lead-ins, sections with the exhibits
                    in the text, footnotes, a data-and-methods appendix and "About This Document".

report.json schema (paths are relative to report.json):
{
  "mode": "report",                                  # or "paper"
  "title": "Unemployment and Output Since 2019",
  "subtitle": "One-sentence framing of the question",
  "organization": "Example Research Institute",      # optional; omit for a neutral product
  "status": "Draft",                                 # badge (optional)
  "date": "2026-10-14",                              # shown as "October 2026" (default: today)
  "sources": ["bls", "bea"],                         # optional; default: the data/<id>/ folders
  "request": "the user's original request, verbatim (shown in Data and Methods)",
  "summary": [{"lead": "Unemployment.", "text": "..."}],     # paper: lead-in + paragraph
  "cover": {"exhibit": 1, "title": "optional hero-chart title"},
  "at_a_glance": {"text": "Framing paragraph.", "findings": [{"lead": "Unemployment.", "text": "2-4 sentences."}]},
  "notes_page": ["Unless otherwise indicated, all years are calendar years.", "..."],
  "introduction": {"title": "Introduction", "text": "Lead paragraph.[^1]",
                   "subsections": [{"heading": "How Has Unemployment Changed?", "body": "..."}],
                   "footnotes": ["Footnotes of the introduction (numbering restarts in each part)."]},
  "appendixes": [{"title": "Definitions", "body": "..."}],         # after Appendix A: Data and Methods
  "sections": [{"heading": "Unemployment", "body": "Paragraph.[^1]", "exhibits": [1, 2], "footnotes": ["..."]}],
  "exhibits": [{
      "title": "Unemployment Rate, January 2019 to September 2026", "subtitle": "Percent, seasonally adjusted",
      "text": "Report mode: 2-4 paragraphs of narrative shown beside the chart.",
      "data": "exhibits/exhibit_1.csv",            # or "rows": [{...}, ...]
      "chart": {"type": "line",                    # line | column | hbar | stacked | stacked_h | table
                "x": "period", "y": "rate", "series": "group",   # or wide: "y": ["A", "B"]
                "date": "date",                    # line charts: ISO date column -> a real date axis
                "recessions": true,                # shade exhibits/recessions.csv (columns start, end) or a path
                "recession_note": "...",           # optional; default names NBER dating
                "filter": {"measure": "U-3"}, "x_order": [], "series_order": [],
                "columns": ["..."],                # table view and download: only these columns
                "format": "pct100", "decimals": 1, "y_min": 0, "y_max": 10},
      "notes": "...", "source": "Analyst calculations using data from the Bureau of Labor Statistics ..."}],
  "methods": ["Paragraph about samples, universe, definitions, vintage and revisions.", "Limitations ..."],
  "footnotes": ["Text of note 1."], "about": "About This Document text (optional).",
  "data_files": [{"label": "BLS series (CSV)", "path": "data/bls/cpi.csv"}],
  "citation": "one line per source (copy them from data/<id>/README.md)",
  "references": [{"id": "empsit", "label": "BLS 2026", "citation": "...", "url": "https://...",
                  "accessed": "2026-10-19", "used_for": "definitions, benchmark"}],    # cite as [@empsit] or [@a; @b]
  "assumptions": [{"parameter": "...", "value": "...", "basis": "precedent", "evidence": "... [@empsit]"}],
                                                   # references, assumptions, questions and literature_search
                                                   # usually live in <out>/decisions.json (method_audit.py),
                                                   # merged in here; report.json wins on the same id
  "clearance": [{"source": "fred", "key": "SP500", "basis": "user confirmed internal use", "redistribute": false}],
  "provenance": {"bls": "data/bls/request.json"},  # optional; default: data/<id>/request.json and the like
  "replication": {"files": ["spec.json", "analysis.py", "facts.json"], "extra": [], "include_data": true}
}
Paper mode: exhibits not listed in any section's "exhibits" are shown after the sections, in order.
Report mode: every exhibit must be placed, and the sections must place them in order (1, 2, 3, ...).
[^n] in a section's body, its exhibits' text and notes resolves to that section's "footnotes" when it has
them (likewise for the introduction); otherwise to the global "footnotes". {{fact}} / {{fact|fmt}} take
their values from facts.json (formats: scripts/analysis/factfmt.py).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the skill folder (read-only in some environments)
import zipfile
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
SKILL = os.path.dirname(SCRIPTS)
ANALYSIS = os.path.join(SCRIPTS, "analysis")
for _p in (ANALYSIS, SCRIPTS, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from core import manual, policy as pol  # noqa: E402
from factfmt import FMT_PATTERN, format_value  # noqa: E402
import method_audit as MA  # noqa: E402
import policyset as PS  # noqa: E402
import scan_secrets  # noqa: E402

TEMPLATE = os.path.join(SKILL, "assets", "report_template.html")
DISCLAIMER = os.path.join(SKILL, "assets", "disclaimer.txt")
EMBED_MAX = 400_000
LIB_MARKER = ".data-analyst-lib"


class BuildError(SystemExit):
    pass


def fail(msg: str):
    raise BuildError(f"ERROR: {msg}")


# ----------------------------------------------------------------------------- text helpers
def fn_id(n, scope: str | None = None) -> str:
    """Footnote anchor id; a scope (e.g. "s2", "intro") restarts numbering in that part."""
    return f"{scope}-{n}" if scope else str(n)


CITE_RE = r"\[@[A-Za-z0-9_-]+(?:\s*[;,]\s*@[A-Za-z0-9_-]+)*\]"
REF_LABELS: dict = {}


def cited_ids(text: str) -> set:
    return {i for g in re.findall(CITE_RE, text) for i in re.findall(r"@([A-Za-z0-9_-]+)", g)}


def inline_md(s: str, scope: str | None = None) -> str:
    s = html.escape(str(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\[\^(\d+)\]", lambda m: f'<sup class="fn"><a href="#fn-{fn_id(m.group(1), scope)}" '
               f'id="fnref-{fn_id(m.group(1), scope)}">{m.group(1)}</a></sup>', s)

    def cites(m):                                   # [@a] or a group: [@a; @b] / [@a, @b]
        ids = re.findall(r"@([A-Za-z0-9_-]+)", m.group(0))
        return "(" + "; ".join(f'<a class="cite" href="#ref-{i}">{html.escape(REF_LABELS.get(i, i))}</a>'
                               for i in ids) + ")"
    return re.sub(CITE_RE, cites, s)


def blocks_md(text, scope: str | None = None) -> str:
    """Paragraphs separated by blank lines; lines starting with '- ' become a list."""
    if isinstance(text, list):
        return "".join(blocks_md(t, scope) for t in text)
    out = []
    for block in re.split(r"\n\s*\n", str(text or "").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if lines and all(l.lstrip().startswith(("- ", "* ")) for l in lines):
            out.append("<ul>" + "".join(f"<li>{inline_md(l.lstrip()[2:], scope)}</li>" for l in lines) + "</ul>")
        elif lines:
            out.append(f"<p>{inline_md(' '.join(lines), scope)}</p>")
    return "".join(out)


def footnotes_html(notes: list, scope: str | None = None, heading: str | None = "Notes") -> str:
    """Numbered notes with back-links; scoped notes restart at 1 in each part."""
    if not notes:
        return ""
    items = "".join(f"<li id='fn-{fn_id(n, scope)}'>{inline_md(t, scope)} "
                    f"<a href='#fnref-{fn_id(n, scope)}' aria-label='Back to text'>↩</a></li>"
                    for n, t in enumerate(notes, start=1))
    h = f"<h2>{esc(heading)}</h2>" if heading else ""
    cls = "footnotes" if heading else "footnotes part-notes"
    return f"<section class='prose {cls}'>{h}<ol>{items}</ol></section>"


# ----------------------------------------------------------------------------- data helpers
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else v
    s = str(v).strip().replace(",", "")
    if s == "" or s.lower() in ("nan", "na", "none", "null"):
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in s else f
    except ValueError:
        return None


def _key(v) -> str:
    n = _num(v)
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return str(n) if n is not None else str(v)


def load_rows(ex: dict, base: str) -> tuple[list[dict], list[str], str]:
    if "rows" in ex:
        rows = ex["rows"]
        cols = list(rows[0].keys()) if rows else []
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        return rows, cols, buf.getvalue()
    with open(os.path.join(base, ex["data"]), encoding="utf-8-sig", newline="") as f:
        text = f.read()
    reader = csv.DictReader(io.StringIO(text))
    return list(reader), reader.fieldnames or [], text


REC_NOTE = "Shaded areas indicate recessions as dated by the National Bureau of Economic Research."


def load_recessions(base: str, rel: str) -> list[list[str]]:
    """[[start, end], ...] from a CSV with columns start, end (e.g. fred's tsstats.save_recessions)."""
    path = os.path.join(base, rel)
    if not os.path.exists(path):
        fail(f"a chart asks for recession shading but {rel} does not exist: write it in analysis.py (columns "
             "start, end; for example the FRED pack's save_recessions(OUT, recession_periods(<USREC>)))")
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [[r["start"], r["end"]] for r in csv.DictReader(f)]


def chart_spec(ex: dict, rows: list[dict]) -> dict | None:
    c = dict(ex.get("chart") or {})
    if not c or c.get("type") == "table":
        return None
    for k, v in (c.get("filter") or {}).items():
        vals = {_key(x) for x in (v if isinstance(v, list) else [v])}
        rows = [r for r in rows if _key(r.get(k)) in vals]
    x = c["x"]
    cats = []
    for r in rows:
        k = _key(r[x])
        if k not in cats:
            cats.append(k)
    if c.get("x_order"):
        order = [_key(v) for v in c["x_order"]]
        cats = [v for v in order if v in cats] + [v for v in cats if v not in order]
    series: dict[str, dict] = {}
    scol = c.get("series") if not isinstance(c.get("y"), list) else None
    seen: dict[tuple, int] = {}
    for r in rows:                                        # one value per (series, x), or the chart silently
        k = (_key(r[scol]) if scol else "", _key(r[x]))   # shows whichever row came last
        seen[k] = seen.get(k, 0) + 1
    dup = [k for k, n in seen.items() if n > 1]
    if dup:
        fail(f"exhibit {ex.get('title', '')!r}: {len(dup)} (series, {x}) pairs have several rows, e.g. {dup[0]}; "
             "add a chart 'filter' (or 'series') so each point is one row")
    if isinstance(c.get("y"), list):                      # wide format
        for col in c["y"]:
            series[col] = {_key(r[x]): _num(r.get(col)) for r in rows}
    else:                                                 # long format
        for r in rows:
            name = _key(r[scol]) if scol else (c.get("series_name") or ex.get("subtitle") or c["y"])
            series.setdefault(name, {})[_key(r[x])] = _num(r.get(c["y"]))
    names = list(series)
    if c.get("series_order"):
        order = [_key(v) for v in c["series_order"]]
        names = [n for n in order if n in series] + [n for n in names if n not in order]
    if len(names) > 8:
        print(f"WARNING: exhibit '{ex.get('title')}' has {len(names)} series; more than 8 is unreadable. Fold into "
              "'Other' or split into small multiples.", file=sys.stderr)
    dates = None
    dcol = c.get("date", "date")
    if c.get("type", "line") == "line" and rows and dcol in rows[0]:
        first = {}
        for r in rows:
            first.setdefault(_key(r[x]), str(r.get(dcol) or ""))
        dates = [first.get(k, "") for k in cats]
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates):
            dates = None
    return {"type": c.get("type", "line"), "categories": cats, "dates": dates, "recessions": c.get("_recessions"),
            "series": [{"name": n, "values": [series[n].get(k) for k in cats]} for n in names],
            "format": c.get("format", "number"), "decimals": c.get("decimals"),
            "y_min": c.get("y_min"), "y_max": c.get("y_max"), "x_label": c.get("x_label", "")}


# ----------------------------------------------------------------------------- render helpers
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def long_date(s: str) -> str:
    if not s:
        return ""
    try:
        return date.fromisoformat(str(s)[:10]).strftime("%B %Y")
    except ValueError:
        return str(s)


def day_date(d) -> str:
    try:
        x = datetime.fromisoformat(str(d)[:10])
        return f"{x.strftime('%B')} {x.day}, {x.year}"
    except ValueError:
        return str(d)


def disclaimer_html() -> str:
    with open(DISCLAIMER, encoding="utf-8") as f:
        paras = [p.strip() for p in re.split(r"\n\s*\n", f.read()) if p.strip()]
    return ('<aside class="disclaimer" role="note" aria-label="AI-generated content disclaimer">'
            + "".join(f"<p>{esc(p)}</p>" for p in paras) + "</aside>")


def exhibit_html(i: int, ex: dict, spec, fname: str, mode: str = "report", scope: str | None = None) -> str:
    # Style guide: the data source note comes first, labeled "Data source:", then the notes.
    source = f'<p class="source"><span>Data source:</span> {inline_md(ex["source"], scope)}</p>' if ex.get("source") else ""
    notes = f'<p class="notes"><span>Notes:</span> {inline_md(ex["notes"], scope)}</p>' if ex.get("notes") else ""
    inner = f"""
  <p class="ex-num">Exhibit {i}.</p>
  <h3>{esc(ex.get('title'))}</h3>
  {f'<p class="ex-sub">{esc(ex.get("subtitle"))}</p>' if ex.get('subtitle') else ''}
  <div class="legend" data-legend="{i}"></div>
  <div class="chart" data-chart="{i}" role="img" aria-label="{esc(ex.get('title'))}"></div>
  <details class="table-view"{' open' if spec is None else ''}>
    <summary>Data table</summary>
    <div class="table-wrap" data-table="{i}"></div>
    <button type="button" class="dl" data-download="{i}">Download {esc(fname)}</button>
  </details>
  {source}{notes}"""
    if mode == "paper":
        return f'\n<section class="exhibit" id="exhibit-{i}">{inner}\n</section>'
    text = f'<div class="ex-text prose">{blocks_md(ex["text"], scope)}</div>' if ex.get("text") else ""
    return (f'\n<section class="exhibit-page{"" if text else " no-text"}" id="exhibit-{i}">{text}'
            f'<div class="exhibit">{inner}\n</div></section>')


# ----------------------------------------------------------------------------- facts
FACT_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*(" + FMT_PATTERN + r"))?\s*\}\}")
ANY_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")       # anything FACT_RE did not replace is malformed
SENTENCE_START = re.compile(r"(?:^|[.!?][*)\"”’]*\s+|\n\s*(?:[-*]\s+)?|\*\*\s*)$")


def fill_facts(R: dict, facts: dict, mode: str):
    """Replace {{name}} / {{name|fmt}} in every prose field with the registered fact value."""
    used, missing, bad = [], [], []

    def rep(m):
        name, fmt = m.group(1), m.group(2)
        if name not in facts:
            missing.append(name)
            return m.group(0)
        f = facts[name]
        fmt = fmt or f.get("fmt", "raw")
        try:
            s = format_value(f["value"], fmt, mode)
        except (ValueError, TypeError) as e:
            bad.append(f"{name}|{fmt}: {e}")
            return m.group(0)
        used.append((name, fmt))                          # the format as cited, which may override the default
        if s[:1].islower() and SENTENCE_START.search(m.string[:m.start()]):
            s = s[0].upper() + s[1:]                      # "Seven departments ..." starting a sentence
        return s

    def walk(x):
        if isinstance(x, str):
            return FACT_RE.sub(rep, x)
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, dict):
            return {k: (v if k in ("data", "chart", "path") else walk(v)) for k, v in x.items()}
        return x
    keep = {k: R[k] for k in ("data_files", "facts", "provenance", "request", "sources", "replication") if k in R}
    out = walk({k: v for k, v in R.items() if k not in keep})
    out.update(keep)
    if missing:
        fail("report.json cites facts that are not in facts.json: " + ", ".join(sorted(set(missing))))
    if bad:
        fail("fact formats that cannot be applied: " + "; ".join(bad))
    return out, used


def file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------------- sources and package
class Ctx:
    """Everything the build knows about the sources a report uses."""

    def __init__(self, R: dict, base: str):
        self.R, self.base = R, base
        self.ids = PS.used_sources(base, R)
        try:
            self.policies = PS.load_policies(self.ids)
        except PS.SourceError as e:
            fail(str(e))
        self.prov = {sid: PS.provenance(base, sid, R) for sid in self.ids}
        self.withheld = {sid: PS.withheld_keys(base, sid, P, R) for sid, P in self.policies.items()}
        self.notices = pol.notices(list(self.policies.values()))

    def title(self, sid):
        return self.policies[sid]["title"]


def replication_files(R: dict, base: str) -> list[str]:
    """Top-level files of the package: the request, code, facts, report and every exhibit CSV."""
    repl = R.get("replication") or {}
    want = list(repl.get("files") or ["spec.json", "plan.json", "analysis.py", "facts.json", "decisions.json",
                                      "report.json"])
    want += [e["data"] for e in R.get("exhibits", []) if e.get("data")]
    exdir = os.path.join(base, "exhibits")             # benchmark.csv, sensitivity.csv, recessions.csv ...
    if os.path.isdir(exdir):
        want += sorted(f"exhibits/{f}" for f in os.listdir(exdir) if f.endswith((".csv", ".json")))
    want += list(repl.get("extra") or [])
    return [w.replace("\\", "/") for w in dict.fromkeys(want) if os.path.isfile(os.path.join(base, w))]


def tree(base: str, top: str) -> list[str]:
    d = os.path.join(base, top)
    out = []
    for r, _, fs in os.walk(d):
        for f in sorted(fs):
            out.append(os.path.relpath(os.path.join(r, f), base).replace("\\", "/"))
    return sorted(out)


def plan_package(ctx: Ctx) -> tuple[list[dict], list[dict]]:
    """(members [{"name", "path", "filter"}], excluded [{"name", "reason", "policy"}])."""
    base, R = ctx.base, ctx.R
    names = replication_files(R, base) + tree(base, "data") + tree(base, "reproduce") + tree(base, "lib")
    members, excluded = [], []
    for n in dict.fromkeys(names):
        if n.endswith(".pyc") or "/__pycache__/" in n:
            continue
        d = PS.decide(n, base, ctx.policies, R, ctx.withheld)
        if d["ok"]:
            members.append({"name": n, "path": os.path.join(base, n), "filter": d["filter"]})
        else:
            excluded.append({"name": n, "reason": d["reason"], "policy": d["policy"]})
    return members, excluded


def write_lib(ctx: Ctx) -> list[str]:
    """Copy the analysis helpers into <out>/lib/ (and each used pack's lib/ into <out>/lib/<id>/)."""
    lib = os.path.join(ctx.base, "lib")
    if os.path.isdir(lib) and os.path.exists(os.path.join(lib, LIB_MARKER)):
        shutil.rmtree(lib)                            # our own copy from an earlier build
    os.makedirs(lib, exist_ok=True)
    with open(os.path.join(lib, LIB_MARKER), "w", encoding="utf-8") as f:
        f.write("Written by build_report.py; rebuilt on every build.\n")
    copied = []
    for f in sorted(os.listdir(ANALYSIS)):
        if f.endswith(".py"):
            shutil.copy2(os.path.join(ANALYSIS, f), os.path.join(lib, f))
            copied.append(f"lib/{f}")
    packs_with_lib = []
    for sid in ctx.ids:
        src = PS.pack_lib(sid)
        if os.path.isdir(src):
            pyfiles = [f for f in sorted(os.listdir(src)) if f.endswith(".py")]
            if pyfiles:
                os.makedirs(os.path.join(lib, sid), exist_ok=True)
                packs_with_lib.append(sid)
            for f in pyfiles:
                shutil.copy2(os.path.join(src, f), os.path.join(lib, sid, f))
                copied.append(f"lib/{sid}/{f}")
    with open(os.path.join(lib, "run_analysis.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(RUN_ANALYSIS.replace("@PACKS@", json.dumps(packs_with_lib)))
    copied.append("lib/run_analysis.py")
    return copied


RUN_ANALYSIS = '''"""Run analysis.py with the copied helpers first on the import path (written by build_report.py).

    python lib/run_analysis.py

lib/ holds the data-analyst skill's analysis helpers (facts.py, factfmt.py, combine.py) and lib/<id>/ each
source pack's own helpers, as they were when the report was built.
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKS = @PACKS@
sys.path[:0] = [HERE] + [os.path.join(HERE, p) for p in PACKS]
os.environ["PYTHONPATH"] = os.pathsep.join([HERE] + [os.path.join(HERE, p) for p in PACKS]
                                           + [os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
os.chdir(ROOT)
sys.argv = [os.path.join(ROOT, "analysis.py")] + sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def source_spec(ctx: Ctx, sid: str):
    """The request to rebuild one source's data: its provenance "spec", report.json sources[].spec, or
    <out>/spec.json when the report has one source."""
    P = ctx.prov[sid][1]
    if isinstance(P.get("spec"), dict) and P["spec"]:
        return P["spec"]
    for s in ctx.R.get("sources") or []:
        if isinstance(s, dict) and (s.get("source") or s.get("id")) == sid and isinstance(s.get("spec"), dict):
            return s["spec"]
    sp = os.path.join(ctx.base, "spec.json")
    if len(ctx.ids) == 1 and os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            return json.load(f)
    return None


def reproduce_scripts(base: str, sid: str) -> dict:
    """{"bash": rel path, "powershell": rel path} of the download scripts in reproduce/<id>/ (empty when none)."""
    d = os.path.join(base, "reproduce", sid)
    out = {}
    for kind, ext in manual.SCRIPT_KINDS.items():
        hits = sorted(f for f in os.listdir(d) if f.endswith(ext)) if os.path.isdir(d) else []
        if hits:
            out[kind] = f"reproduce/{sid}/{hits[0]}"
    return out if len(out) == len(manual.SCRIPT_KINDS) else {}


def pack_reproduce_bundle(sid: str, spec_path: str, base: str, d: str) -> dict:
    """Ask a deep pack for its reproduce bundle (its fetch.py --reproduce-bundle DIR, see docs/pack_contract.md):
    download scripts with the request built in, the request file and a how-to. {} when the pack has none."""
    cli = os.path.join(PS.sources_dir(), sid, "fetch.py")
    if not os.path.isfile(cli):
        return {}
    p = subprocess.run([sys.executable, "-I", cli, spec_path, "--out", base, "--reproduce-bundle", d],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        r = json.loads(p.stdout[p.stdout.find("{"):])
    except ValueError:
        return {}
    return r if p.returncode == 0 and r.get("scripts") else {}


def manual_items_for(ctx: Ctx, sid: str) -> list[dict]:
    """The source's items from the analysis's manual-download manifest (standard packs), if there is one."""
    mf = os.path.join(ctx.base, "manual_download", "manifest.json")
    try:
        with open(mf, encoding="utf-8") as f:
            return [it for it in json.load(f).get("items", []) if it.get("source") == sid]
    except (OSError, ValueError):
        return []


def write_reproduce(ctx: Ctx, sid: str) -> list[str]:
    """reproduce/<id>/ for a definition_only source: download scripts (bash and PowerShell) with the request built
    in, so one command downloads the data again under the reader's own account; the request (spec.json, and the
    pack's own request file); REPRODUCE.md. No data files."""
    P = ctx.policies[sid]
    d = os.path.join(ctx.base, "reproduce", sid)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    spec = source_spec(ctx, sid)
    if spec is not None:
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2)
            f.write("\n")
    rel = f"reproduce/{sid}"
    got = pack_reproduce_bundle(sid, os.path.join(d, "spec.json"), ctx.base, d) if spec is not None else {}
    request, doc = got.get("request") or "spec.json", got.get("doc")
    if not got:
        items = manual_items_for(ctx, sid)
        if items:
            manual.write_scripts(d, items, f"download_{sid}")
    copied = []
    if not reproduce_scripts(ctx.base, sid):          # no scripts from the pack: the analysis's own bundle, as is
        mdir = os.path.join(ctx.base, "manual_download", sid)
        if os.path.isdir(mdir):
            for r, _, fs in os.walk(mdir):
                for f in fs:
                    src = os.path.join(r, f)
                    rel_f = os.path.relpath(src, mdir).replace("\\", "/")
                    if (pol.is_microdata(P, rel_f) or os.path.getsize(src) > PS.MICRO_MAX
                            or f.startswith("extract_number_")):     # a saved number belongs to the analyst
                        continue
                    os.makedirs(os.path.join(d, "manual", os.path.dirname(rel_f)), exist_ok=True)
                    shutil.copy2(src, os.path.join(d, "manual", rel_f))
                    copied.append(rel_f)
    scripts = reproduce_scripts(ctx.base, sid)
    inc = [f for f in tree(ctx.base, f"data/{sid}") if PS.decide(f, ctx.base, ctx.policies, ctx.R, ctx.withheld)["ok"]]
    spec_arg = f"{rel}/spec.json" if spec is not None else f"<spec.json for {sid}>"
    a = P.get("auth") or {}
    keys = ([(a["env"], a["signup_url"])] if a.get("required") in ("required", "optional") and a.get("env")
            and a.get("signup_url") else [])
    lines = [f"# Reproduce the {P['title']} data", "",
             f"The terms of this source do not allow its data files to be shared ({P['redistribution'].get('why', '')}). "
             "This folder holds what is needed to download the same data under your own account; the analysis "
             "then runs unchanged.", ""]
    if scripts:
        stem = os.path.splitext(os.path.basename(scripts["bash"]))[0]
        script_dir = os.path.dirname(scripts["bash"])
        lines += ["## Download: one command", ""]
        lines += manual.run_section(stem, keys, folder=script_dir, spec_name=request)
        lines += ["## Then: package the files and run the analysis", "",
                  "With the data-analyst skill (`<skill>` is its folder, for example `plugins/data-analyst/skills/data-analyst`), "
                  "from the folder that holds this package:", "", "```",
                  f'python "<skill>/scripts/fetch.py" --source {sid} {spec_arg} --out . --ingest {script_dir}',
                  "python lib/run_analysis.py", "```", ""]
    else:
        lines += ["## With the data-analyst skill", "",
                  "`<skill>` is the folder of the data-analyst skill (for example `plugins/data-analyst/skills/data-analyst`).", "",
                  "```",
                  f'python "<skill>/scripts/fetch.py" --source {sid} {spec_arg} --out . --check',
                  f'python "<skill>/scripts/fetch.py" --source {sid} {spec_arg} --out .',
                  "```", "",
                  "The first command says which route works on your computer (a key, a keyless route, or a manual "
                  "download); the second downloads the data into `data/" + sid + "/`. "
                  f"If you download the files by hand, package them with "
                  f'`python "<skill>/scripts/fetch.py" --source {sid} {spec_arg} --out . --ingest <files>`.', ""]
    lines += ["## By hand", "", (f"`{doc}` has the other ways: the source's website and the same steps one by one."
                                  if doc else P["routes"]["manual"]["how"]), ""]
    if copied:
        lines += ["The manual-download bundle of the original analysis is in `manual/`.", ""]
    if inc:
        lines += ["## The original request", "", "Shared in the package (no data):", ""] + [f"- `{f}`" for f in inc] + [""]
    if keys and not scripts:
        lines += [f"A free key ({keys[0][0]}) is available at {keys[0][1]}.", ""]
    with open(os.path.join(d, "REPRODUCE.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return [f"{rel}/{x}" for x in ("REPRODUCE.md", "spec.json") if os.path.exists(os.path.join(d, x))]


REPRODUCE_ORDER = ("REPRODUCE.md", ".sh", ".ps1", "spec.json", ".json", ".md")


def reproduce_shown(base: str, sid: str) -> list[str]:
    """Files of reproduce/<id>/ to attach to the page: REPRODUCE.md, the download scripts, the request, the how-to."""
    def rank(f):
        return next((i for i, k in enumerate(REPRODUCE_ORDER) if f.endswith(k)), len(REPRODUCE_ORDER)), f
    files = [f for f in tree(base, f"reproduce/{sid}") if not f.endswith(".txt")]
    return sorted(files, key=lambda f: rank(os.path.basename(f)))


def replication_readme(ctx: Ctx, excluded: list[dict]) -> str:
    R, lines = ctx.R, []
    lines += [f"# Replication materials: {R.get('title', '')}", "",
              "This package holds what is needed to reproduce every number, exhibit and chart in the report, within "
              "the terms of each data source.", "",
              "1. Install Python 3.10 or later with pandas and numpy.",
              "2. Get the data (by source, below).",
              "3. Run the analysis: `python lib/run_analysis.py` (it puts the copied helpers in `lib/` on the import "
              "path and runs `analysis.py`). This writes `exhibits/*.csv` and `facts.json`.",
              "4. Compare the new `facts.json` with the original. The values must be identical when the data are the "
              "ones used (see the retrieval dates below).",
              "5. With the data-analyst skill, rebuild and audit the report: "
              "`python <skill>/scripts/report/build_report.py report.json`, then "
              "`python <skill>/scripts/report/audit_report.py report.json --rerun`.", "",
              "## Data by source", ""]
    for sid in ctx.ids:
        P = ctx.policies[sid]
        mode = P["redistribution"]["mode"]
        prov_rel, prov = ctx.prov[sid]
        when = prov.get("retrieved") or prov.get("downloaded") or ""
        head = f"- **{P['title']}** (`data/{sid}/`)" + (f", retrieved {day_date(when)}" if when else "") + ": "
        if mode == "allowed":
            head += "the data are included as retrieved (the vintage used)."
        elif mode == "aggregates_only":
            head += "tables of estimates are included; row-level files stay with the analyst."
        else:
            head += "no data files are included; the terms forbid sharing them. "
            sc = reproduce_scripts(ctx.base, sid)
            if sc:
                stem = os.path.splitext(os.path.basename(sc["bash"]))[0]
                c = manual.run_lines(stem, os.path.dirname(sc["bash"]))
                head += (f"Download the same data under your own account with one command: `{c['bash']}` (macOS, "
                         f"Linux) or `{c['powershell']}` (Windows); the request is built into the scripts. Then follow "
                         f"`reproduce/{sid}/REPRODUCE.md`.")
            else:
                head += f"Download the same data under your own account with `reproduce/{sid}/REPRODUCE.md`."
        lines.append(head + f" Rule: {mode}. {P['redistribution'].get('why', '')}")
        if ctx.withheld.get(sid):
            w = P["redistribution"]["withhold"]
            lines.append(f"  - Rows of {', '.join(sorted(ctx.withheld[sid]))} ({w.get('key')}) are left out of the shared "
                         "files: their terms do not allow redistribution. Download them again with your own access.")
        if mode != "definition_only":
            lines.append(f"  - Current data: `python <skill>/scripts/fetch.py --source {sid} <spec> --out . --force` "
                         "(the request is in " + (f"`{prov_rel}`" if prov_rel else "spec.json") + "). Sources revise "
                         "published data, so new numbers can differ from the report.")
    out_ = [e for e in excluded if not e["name"].startswith(("lib/", "reproduce/"))]
    if out_:
        lines += ["", "## Not included", ""] + [f"- `{e['name']}`: {e['reason']}" for e in out_[:60]]
        if len(out_) > 60:
            lines.append(f"- and {len(out_) - 60} more files")
    lines += ["", "## Contents", "",
              "- `spec.json` / `plan.json`: the data request; `analysis.py`: all computations; `facts.json`: every "
              "number in the text, with its source cell or derivation.",
              "- `data/<id>/`: each source's files as its terms allow, with `request.json` (exact requests without "
              "keys, retrieval time) and `README.md` (citation, terms).",
              "- `exhibits/`: the data for each exhibit; `lib/`: the helper scripts `analysis.py` uses; "
              "`reproduce/`: download instructions for sources whose data may not be shared.", ""]
    cites = [R.get("citation")] if R.get("citation") else []
    if not cites:
        for s in ctx.ids:
            P = ctx.prov[s][1]
            v = P.get("vintage")
            cites.append(pol.citation(ctx.policies[s], retrieved=str(P.get("retrieved") or P.get("downloaded") or "")[:10],
                                      dataset=str((P.get("spec") or {}).get("description") or ctx.title(s)),
                                      vintage="" if v in (None, "latest") else str(v)))
    if cites:
        lines += ["## Data citations", ""] + [str(c) for c in cites] + [""]
    if ctx.notices:
        lines += ["## Notices", ""] + [f"- {n}" for n in ctx.notices] + [""]
    return "\n".join(lines)


def make_package(ctx: Ctx) -> tuple[str, list[dict], list[dict]]:
    members, excluded = plan_package(ctx)
    zp = os.path.join(ctx.base, "replication_package.zip")
    with zipfile.ZipFile(zp + ".part", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("REPLICATION.md", replication_readme(ctx, excluded))
        for m in members:
            if m["filter"]:
                text, _ = PS.filtered_csv(m["path"], *m["filter"])
                z.writestr(m["name"], text)
            else:
                z.write(m["path"], m["name"])
    os.replace(zp + ".part", zp)
    # a last guard: the policy check of core on everything that went in
    names = [m["name"] for m in members]
    bad = set()
    for sid, P in ctx.policies.items():
        scope = [n for n in names if n.startswith(f"data/{sid}/") or not n.startswith(("data/", "exhibits/", "lib/"))]
        bad.update(pol.check_package(P, scope))
    bad = sorted(bad)
    if bad:
        os.remove(zp)
        fail(f"the package would hold files the source terms forbid: {bad[:5]}")
    return "replication_package.zip", members, excluded


# ----------------------------------------------------------------------------- data and methods: vintage, notices
SERIES_COLS = ("series_id", "title", "units", "frequency", "seasonal_adjustment", "first_date", "last_date",
               "vintage", "last_updated", "n_future", "n_missing", "converted_to", "transformation", "copyright")
SKIP_SCAN = ("series.csv", "data_dictionary.csv", "codebook.csv", "variables.csv", "notes.csv")


def _period_text(lp: str) -> str:
    mm = re.fullmatch(r"(\d{4}) ?([MQS])(\d\d)", str(lp))
    if not mm:
        return str(lp)
    y, k, n = mm.group(1), mm.group(2), int(mm.group(3))
    if k == "M" and 1 <= n <= 12:
        return f"{datetime(int(y), n, 1).strftime('%B')} {y}"
    if k == "Q" and 1 <= n <= 4:
        return f"the {['first', 'second', 'third', 'fourth'][n - 1]} quarter of {y}"
    if k == "S" and n in (1, 2):
        return f"the {'first' if n == 1 else 'second'} half of {y}"
    return str(lp)


def _vint_text(v: str) -> str:
    v = str(v)
    named = {"latest": "latest available", "first_release": "first release", "all": "full revision history"}
    if v in named:
        return named[v]
    if ":" in v or re.match(r"\d{4}-\d\d-\d\d", v):
        return "as published on " + ", ".join(day_date(x) for x in v.split(":", 1)[-1].split(","))
    return v


def _flags_and_gaps(base: str, sid: str, P: dict) -> tuple[list[str], list[str]]:
    """Values marked preliminary and periods without a value, in the source's shared CSVs (BLS, FRED style)."""
    used = set(P.get("used_series") or [])
    prelim, gaps = set(), set()
    for rel in tree(base, f"data/{sid}"):
        full = os.path.join(base, rel)
        if not rel.endswith(".csv") or os.path.basename(rel) in SKIP_SCAN or "/raw/" in rel:
            continue
        if os.path.getsize(full) > PS.MICRO_MAX:
            continue
        cols = PS.csv_header(full)
        if "preliminary" not in cols and not ("value" in cols and "series_id" in cols):
            continue
        with open(full, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                sidv = r.get("series_id", "")
                if used and sidv and sidv not in used:
                    continue
                per = (f"{r.get('period_name')} {r.get('year')}" if r.get("period_name") else
                       r.get("date") or r.get("period") or r.get("time_period") or "")
                if str(r.get("preliminary", "")).strip().lower() in ("1", "true", "yes", "p"):
                    prelim.add(f"{sidv} {per}".strip())
                if "value" in cols and "series_id" in cols and str(r.get("value", "")).strip() == "":
                    gaps.add(per)
    return sorted(prelim), sorted(gaps)


def vintage_html(ctx: Ctx) -> str:
    """Data Vintage: per source the retrieval date, route, vintage, latest period, preliminary values and
    gaps; a series table and series citations when data/<id>/series.csv exists."""
    parts, tables = [], []
    for sid in ctx.ids:
        prov_rel, P = ctx.prov[sid]
        T = ctx.title(sid)
        if not prov_rel:
            parts.append(f"<p class='vintage'>{esc(T)}: no provenance file was found in <code>data/{esc(sid)}/</code>, "
                         "so the retrieval date is not recorded.</p>")
            continue
        when = P.get("retrieved") or P.get("downloaded") or ""
        route = P.get("route") or ""
        acq = P.get("acquired") or ""
        txt = f"{T}: retrieved on {day_date(when) if when else 'an unrecorded date'}"
        if route or acq:
            txt += " by the " + " / ".join(x for x in dict.fromkeys([str(route), str(acq)]) if x) + " route"
        if P.get("api_version"):
            txt += f" ({P['api_version']})"
        vf = PS.vintage_fields(P)
        v = vf.get("vintage")
        if v and v != "latest":
            txt += "; vintage " + ("; ".join(map(str, v[:4])) if isinstance(v, list) else _vint_text(v))
        if vf.get("series_vintages"):
            txt += "; series vintages " + ", ".join(map(str, vf["series_vintages"]))
        lp = P.get("latest_period") or P.get("latest")
        if isinstance(lp, dict):
            txt += "; latest observation " + "; ".join(f"{k} {x}" for k, x in lp.items())
        elif lp:
            txt += f"; the latest period in the data is {_period_text(lp)}"
        if (P.get("spec") or {}).get("as_of"):
            txt += f"; data cut-off {P['spec']['as_of']}"
        parts.append(f"<p class='vintage'>{esc(txt)}.</p>")
        prelim, gaps = _flags_and_gaps(ctx.base, sid, P)
        if prelim:
            parts.append("<p class='vintage'>Values marked preliminary at retrieval: " + esc("; ".join(prelim[:12]))
                         + ("; and others" if len(prelim) > 12 else "") + ".</p>")
        if gaps:
            parts.append("<p class='vintage'>Periods with no published value in the series used: "
                         + esc(", ".join(gaps[:12])) + (" and others" if len(gaps) > 12 else "")
                         + ". These gaps were left empty, not interpolated.</p>")
        dropped = P.get("options_dropped") or P.get("v2_options_dropped")
        if dropped:
            parts.append("<p class='vintage'>Requested but unavailable on the route used: " + esc(", ".join(map(str, dropped)))
                         + ".</p>")
        for pr in (P.get("problems") or [])[:5]:
            parts.append(f"<p class='vintage'>{esc(T)}: {esc(pr)}</p>")
        sp = os.path.join(ctx.base, "data", sid, "series.csv")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8-sig", newline="") as f:
                meta = list(csv.DictReader(f))
            cols = [c for c in SERIES_COLS if meta and c in meta[0]]
            if meta and "series_id" in cols:
                head = "".join(f"<th>{esc(c.replace('_', ' ').capitalize())}</th>" for c in cols)
                body = "".join("<tr>" + "".join(f"<td>{esc(_vint_text(m.get(c, '')) if c == 'vintage' and m.get(c) else m.get(c, ''))}</td>"
                                                for c in cols) + "</tr>" for m in meta)
                tables.append(f"<div class='table-wrap'><table class='audit'><thead><tr>{head}</tr></thead>"
                              f"<tbody>{body}</tbody></table></div>")
            cites = [m["citation"] for m in meta if m.get("citation")]
            if cites:
                tables.append("<h3>Series Citations</h3><ul class='files'>" + "".join(f"<li>{esc(c)}</li>" for c in cites)
                              + "</ul>")
    if not parts:
        return ""
    parts.append("<p class='vintage'>Sources revise their published data (benchmark and seasonal revisions, later "
                 "releases of preliminary values), so a later retrieval can differ from the data used here.</p>")
    return "<h3>Data Vintage</h3>" + "".join(parts) + "".join(tables)


def notices_html(ctx: Ctx) -> str:
    if not ctx.notices:
        return ""
    return ("<h3>Required Notices</h3><ul class='notices'>" + "".join(f"<li>{esc(n)}</li>" for n in ctx.notices)
            + "</ul>")


def footer_html(ctx: Ctx) -> str:
    srcs = "; ".join(ctx.title(s) for s in ctx.ids)
    out = [f"<p>Data: {esc(srcs)}. Sources revise published data; later retrievals can differ.</p>"] if srcs else []
    out += [f"<p>{esc(n)}</p>" for n in ctx.notices]
    return "".join(out)


# ----------------------------------------------------------------------------- appendix blocks
def references_html(R: dict) -> str:
    refs = R.get("references") or []
    if not refs:
        return ""
    items = []
    for r in refs:
        url = f" <a href='{esc(r['url'])}'>{esc(r['url'])}</a>" if r.get("url") else ""
        acc = f" (accessed {esc(r['accessed'])})" if r.get("accessed") else ""
        use = f"<br><span class='muted'>Used for: {esc(r['used_for'])}</span>" if r.get("used_for") else ""
        items.append(f"<li id='ref-{esc(r['id'])}'>{inline_md(r.get('citation', ''))}.{url}{acc}{use}</li>")
    return f"<section class='prose references' id='references'><h2>References</h2><ol>{''.join(items)}</ol></section>"


def assumptions_html(R: dict) -> str:
    rows = R.get("assumptions") or []
    if not rows:
        return ""
    judged = any((a.get("basis") or "").lower() == "judgment" for a in rows)

    def why(a):
        if (a.get("basis") or "").lower() != "judgment":
            return ""
        sens = f" Sensitivity: <code>exhibits/sensitivity.csv</code>, {esc(a.get('sensitivity') or a.get('parameter', ''))}."
        return f"<td>{inline_md(a.get('why_not_empirical', ''))}{sens}</td>"
    trs = "".join(
        f"<tr><td>{inline_md(a.get('parameter', ''))}</td><td>{inline_md(a.get('value', ''))}</td>"
        f"<td>{esc(a.get('basis', ''))}</td><td>{inline_md(a.get('evidence', ''))}</td>"
        f"{(why(a) or '<td></td>') if judged else ''}</tr>" for a in rows)
    extra = "<th>Why Not Empirical</th>" if judged else ""
    return ("<h3>Key Assumptions and Parameters</h3><div class='table-wrap'><table class='audit'>"
            f"<thead><tr><th>Parameter</th><th>Value</th><th>Basis</th><th>Evidence</th>{extra}</tr></thead>"
            f"<tbody>{trs}</tbody></table></div>")


def decisions_html(R: dict) -> str:
    """The critical questions with how each was answered, and the literature searched (decisions.json)."""
    out = []
    qs = R.get("questions") or []
    if qs:
        how = {"dont_know": "Don't know", "skipped": "Skipped (treated as Don't know)",
               "not_asked": "Not asked"}
        trs = []
        for q in qs:
            no = q.get("answer") in (None, "", *MA.NO_ANSWER)
            ans = how.get(q.get("how") or q.get("answer") or "not_asked", "Don't know") if no else str(q["answer"])
            if not q.get("asked", True) and q.get("reason_code") == "settled_by_request" and q.get("request"):
                ans = f"Settled by the request: “{q['request']}”"
            elif not q.get("asked", True) and (q.get("reason_code") or q.get("reason")):
                ans += f" ({MA.NOT_ASKED_CODES.get(q.get('reason_code'), q.get('reason') or q.get('reason_code'))})"
            dec = q.get("resolution") or ("" if no else "As answered")
            if q.get("blocking") and q.get("rationale"):
                dec += f"; {q['rationale']}"
            trs.append(f"<tr><td>{inline_md(q.get('question', q.get('id', '')))}</td><td>{inline_md(ans)}</td>"
                       f"<td>{inline_md(dec)}</td></tr>")
        out.append("<h3>Questions and Decisions</h3><div class='table-wrap'><table class='audit'><thead><tr>"
                   "<th>Question</th><th>Answer</th><th>How It Was Decided</th></tr></thead>"
                   f"<tbody>{''.join(trs)}</tbody></table></div>")
    lit = R.get("literature_search") or []
    if lit:
        items = []
        for s_ in lit:
            if s_.get("unavailable"):
                why = MA.UNAVAILABLE_CODES.get(s_.get("reason_code"), s_.get("reason_code") or "")
                detail = f" ({s_['reason']})" if s_.get("reason") else ""
                items.append(f"<li>Literature search was not available: {inline_md(why + detail)}</li>")
                continue
            found = ", ".join(f"[@{i}]" for i in s_.get("found") or []) or "nothing relevant"
            note = f" {inline_md(s_['note'])}" if s_.get("note") else ""
            tool = f", {esc(s_['tool'])}" if s_.get("tool") else ""
            items.append(f"<li>&ldquo;{esc(s_.get('query', ''))}&rdquo; ({esc(s_.get('date', ''))}{tool}): "
                         f"{inline_md(found)}.{note}</li>")
        out.append(f"<h3>Literature Searched</h3><ul>{''.join(items)}</ul>")
    disc = [(k, (R.get("disclosures") or {}).get(k)) for k, _ in MA.escapes(R)]
    disc = [(k, t) for k, t in disc if t]
    if disc:
        out.append("<h3>Not Verified by the Audit</h3><ul>" + "".join(f"<li>{inline_md(t)}</li>" for _, t in disc)
                   + "</ul>")
    return "".join(out)


def data_needs_html(R: dict) -> str:
    """The data that would make the estimates more accurate, those only the user can supply first."""
    needs = sorted(R.get("data_needs") or [], key=lambda d: not d.get("from_user"))
    if not needs:
        note = R.get("data_needs_note")
        return (f"<h3 id='data-needs'>Data That Would Improve These Estimates</h3><p>{inline_md(note)}</p>"
                if note else "")
    trs = "".join(
        f"<tr><td>{inline_md(d.get('data', ''))}</td><td>{inline_md(d.get('effect', ''))}</td>"
        f"<td>{inline_md(d.get('how_to_get', ''))}{' (from you)' if d.get('from_user') else ''}</td></tr>"
        for d in needs)
    return ("<h3 id='data-needs'>Data That Would Improve These Estimates</h3><div class='table-wrap'>"
            "<table class='audit'><thead><tr><th>Data</th><th>What It Would Improve</th><th>How to Get It</th></tr>"
            f"</thead><tbody>{trs}</tbody></table></div>")


def replication_html(ctx: Ctx, out_dir: str, package: str | None, members: list[dict], excluded: list[dict],
                     mode: str) -> tuple[str, list]:
    base = ctx.base

    def rel(p):
        return os.path.relpath(os.path.join(base, p), out_dir).replace("\\", "/")
    shown = replication_files(ctx.R, base)
    for sid in ctx.ids:
        prov_rel = ctx.prov[sid][0]
        shown += [x for x in (prov_rel, f"data/{sid}/README.md") if x]
    defonly = [sid for sid in ctx.ids if ctx.policies[sid]["redistribution"]["mode"] == "definition_only"]
    for sid in defonly:
        shown += reproduce_shown(base, sid)
    in_pkg = {m["name"]: m for m in members}
    embeds, blocks = [], []
    for k, f in enumerate(dict.fromkeys(x for x in shown if not x.startswith("exhibits/"))):
        fp = os.path.join(base, f)
        if f not in in_pkg or not os.path.isfile(fp):
            continue
        if in_pkg[f]["filter"]:
            blocks.append(f"<li><code>{esc(f)}</code> (in the package, without the rows of "
                          f"{esc(', '.join(sorted(in_pkg[f]['filter'][1])))}: not cleared for redistribution)</li>")
            continue
        if os.path.getsize(fp) > EMBED_MAX:
            blocks.append(f"<li><a href='{esc(rel(f))}'>{esc(f)}</a> (large file; it is in the package)</li>")
            continue
        text = open(fp, encoding="utf-8", errors="replace").read()
        embeds.append({"id": f"att-{k}", "name": os.path.basename(f), "text": text})
        blocks.append(f"<li><details><summary><code>{esc(f)}</code></summary>"
                      f"<button type='button' class='dl' data-attach='att-{k}'>Download {esc(os.path.basename(f))}</button>"
                      f"<pre class='code'>{esc(text)}</pre></details></li>")
    held = [e for e in excluded if e["policy"] and not e["name"].startswith(("lib/", "reproduce/"))]
    held_html = ""
    if held:
        held_html = ("<p>Not in the package because of the source terms: "
                     + "; ".join(f"<code>{esc(e['name'])}</code> ({esc(e['reason'])})" for e in held[:8])
                     + (f"; and {len(held) - 8} more" if len(held) > 8 else "") + ".</p>")
    pkg = (f"<p><a class='pkg' href='{esc(rel(package))}'>Download the replication package (ZIP)</a>: the data each "
           "source's terms allow to share, download instructions for the rest, the code, the exhibit data, the facts "
           "file, and step-by-step instructions (REPLICATION.md).</p>" if package else "")
    title = "Appendix: Replication Materials" if mode == "paper" else "Replication Materials"
    get = []
    for sid in defonly:
        sc = reproduce_scripts(base, sid)
        if sc:
            get.append(f"{esc(ctx.title(sid))}: run <code>{esc(sc['bash'])}</code> (macOS, Linux) or "
                       f"<code>{esc(sc['powershell'])}</code> (Windows), attached below. One command downloads the "
                       "data under your own account (the request is built in; the script asks for your key with "
                       f"hidden input). Then follow <code>reproduce/{esc(sid)}/REPRODUCE.md</code>.")
        else:
            get.append(f"{esc(ctx.title(sid))}: follow <code>reproduce/{esc(sid)}/REPRODUCE.md</code>.")
    get_html = ("<ul>" + "".join(f"<li>{g}</li>" for g in get) + "</ul>") if get else ""
    steps = ("<ol><li>Get the data: use the included files in <code>data/</code> where the terms allow"
             + (", and for sources whose terms forbid sharing:" + get_html if get else ".") +
             "</li><li>Run <code>python lib/run_analysis.py</code>. It writes the exhibit data and "
             "<code>facts.json</code>.</li><li>Compare the new <code>facts.json</code> with the values in the audit "
             "trail.</li></ol>")
    return (f"<section class='prose replication' id='replication'><h2>{title}</h2>{pkg}{held_html}{steps}"
            f"<ul class='files code-list'>{''.join(blocks)}</ul></section>"), embeds


PROV_ROWS = (("retrieved", "Retrieved"), ("downloaded", "Downloaded"), ("route", "Route"), ("acquired", "How acquired"),
             ("api_version", "API version"), ("api_base", "API"), ("collection", "Collection"),
             ("extract_number", "Extract number"), ("latest_period", "Latest period"), ("n_requests", "Requests"),
             ("n_queries", "Queries"), ("rows", "Rows"), ("fingerprint", "Request fingerprint"),
             ("csv_sha256", "Data checksum (SHA-256)"))


def provenance_html(ctx: Ctx) -> str:
    parts = []
    for sid in ctx.ids:
        prov_rel, P = ctx.prov[sid]
        pol_ = ctx.policies[sid]
        rows = [("Source", f"{pol_['title']} ({pol_['agency']})"), ("Redistribution rule", pol_["redistribution"]["mode"])]
        for k, label in PROV_ROWS:
            v = P.get(k)
            if v not in (None, "", [], {}):
                rows.append((label, f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else
                             json.dumps(v) if isinstance(v, (dict, list)) else str(v)))
        if isinstance(P.get("requests"), list) and "n_requests" not in P:
            rows.append(("Requests", str(len(P["requests"]))))
        if (P.get("spec") or {}).get("as_of"):
            rows.append(("Data cut-off (as_of)", str(P["spec"]["as_of"])))
        rows.append(("Provenance file", prov_rel or "none found"))
        tbl = ("<div class='table-wrap'><table class='audit'><tbody>" + "".join(
            f"<tr><th scope='row'>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows) + "</tbody></table></div>")
        trs = ""
        if isinstance(P.get("tables"), list):                     # core.package request.json
            trs = "".join(f"<tr><td><code>{esc(t.get('file', ''))}</code></td><td>{t.get('rows', 0):,}</td>"
                          f"<td><code>{esc(str(t.get('sha256', ''))[:16])}</code></td></tr>" for t in P["tables"])
        elif isinstance(P.get("datasets"), dict):                 # treasury fetch_spec.json
            trs = "".join(f"<tr><td><code>{esc(n)}.csv</code> ({esc(d.get('endpoint', ''))})</td><td>{d.get('rows', 0):,}</td>"
                          f"<td>{esc(d.get('first_date') or '')} to {esc(d.get('last_date') or '')}</td></tr>"
                          for n, d in P["datasets"].items())
        elif isinstance(P.get("files"), list):                    # opm manifest.json
            trs = "".join(f"<tr><td><code>{esc(f.get('dataset', ''))} {esc(f.get('year', ''))}-{esc(f.get('month', ''))}</code>"
                          f"</td><td>v{esc(f.get('version', ''))}</td><td><code>{esc(str(f.get('sha256', ''))[:16])}</code></td></tr>"
                          for f in P["files"][:40])
        if trs:
            tbl += f"<div class='table-wrap'><table class='audit'><tbody>{trs}</tbody></table></div>"
        ctxp = os.path.join(ctx.base, "data", sid, "context_sources.json")
        if os.path.exists(ctxp):
            C = json.load(open(ctxp, encoding="utf-8"))
            tbl += ("<div class='table-wrap'><table class='audit'><thead><tr><th>Context series</th><th>Source</th>"
                    "<th>Units</th><th>Range</th><th>Retrieved</th></tr></thead><tbody>" + "".join(
                        f"<tr><td>{esc(k)} (<code>{esc(v.get('series_id', ''))}</code>)</td><td>{esc(v.get('source', ''))}"
                        f"</td><td>{esc(v.get('units', ''))}</td><td>{esc(v.get('first', ''))} to {esc(v.get('last', ''))}</td>"
                        f"<td>{esc(v.get('retrieved', ''))}</td></tr>" for k, v in C.items()) + "</tbody></table></div>")
        parts.append(f"<h4>{esc(pol_['title'])}</h4>{tbl}")
    if not parts:
        return ""
    return ("<h3>Data provenance</h3>" + "".join(parts) + "<p class='audit-note'>The exact requests (without keys) are "
            "in each source's provenance file; <code>fetch.py --source &lt;id&gt;</code> downloads the data again.</p>")


def audit_html(ctx: Ctx, out_dir: str, facts: dict, used: list, mode: str) -> str:
    R, base = ctx.R, ctx.base

    def rel(p):
        return os.path.relpath(os.path.join(base, p), out_dir).replace("\\", "/")
    parts = [provenance_html(ctx)]
    files = list(R.get("data_files", []))
    for extra in ("analysis.py", "facts.json", "report.json", "spec.json", "plan.json"):
        if os.path.exists(os.path.join(base, extra)) and extra not in [d["path"] for d in files]:
            files.append({"label": extra, "path": extra})
    trs = []
    for d in files:
        fp = os.path.join(base, d["path"])
        trs.append(f"<tr><td><a href='{esc(rel(d['path']))}'>{esc(d['path'])}</a></td>"
                   f"<td>{os.path.getsize(fp):,}</td><td><code>{file_sha(fp)}</code></td></tr>")
    for sid, P in ctx.policies.items():                    # definition_only data: checksum only, never linked
        if P["redistribution"]["mode"] != "definition_only":
            continue
        for f in tree(base, f"data/{sid}"):
            if pol.is_microdata(P, f[len(f"data/{sid}/"):]) and os.path.getsize(os.path.join(base, f)) <= 4 * PS.MICRO_MAX:
                fp = os.path.join(base, f)
                trs.append(f"<tr><td><code>{esc(f)}</code> (local only, not distributed)</td>"
                           f"<td>{os.path.getsize(fp):,}</td><td><code>{file_sha(fp)}</code></td></tr>")
    if trs:
        parts.append("<h3>Files and checksums (SHA-256)</h3><div class='table-wrap'><table class='audit'>"
                     "<thead><tr><th>File</th><th>Bytes</th><th>SHA-256</th></tr></thead><tbody>"
                     + "".join(trs) + "</tbody></table></div>")
    if used:
        any_se = any(isinstance(facts[n].get("se"), (int, float)) for n, _ in used)
        trs = []
        for name, cited_fmt in dict.fromkeys(used):
            f = facts[name]
            pk = f.get("pack")
            pk_txt = (", ".join(pk) if isinstance(pk, list) else pk) if pk else ""
            if f.get("source"):
                where = ", ".join(f"{k} = {v}" for k, v in f.get("where", {}).items())
                src = (f"<a href='{esc(rel(f['source']))}'>{esc(f['source'])}</a><br>"
                       f"<span class='muted'>row: {esc(where)}; column: {esc(f.get('column'))}</span>")
            else:
                src = f"<span class='muted'>derived: {esc(f.get('derivation', ''))}</span>"
            se = f"{f['se']:.6g}" if isinstance(f.get("se"), (int, float)) else ""
            se_td = f"<td>{esc(se)}</td>" if any_se else ""
            trs.append(f"<tr><td>{esc(f.get('label') or name)}<br><code>{esc(name)}</code></td>"
                       f"<td>{esc(format_value(f['value'], cited_fmt, mode))}</td>"
                       f"<td><code>{esc(f['value'])}</code></td>{se_td}<td>{src}</td><td>{esc(pk_txt)}</td></tr>")
        se_th = "<th>Standard error</th>" if any_se else ""
        parts.append("<h3>Numbers cited in the text</h3><div class='table-wrap'><table class='audit'>"
                     f"<thead><tr><th>Fact</th><th>As cited</th><th>Unrounded</th>{se_th}<th>Source cell</th>"
                     "<th>Source pack</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>")
    # balanced parentheses stay in a URL ("...RA%20(GW).aspx"), as in audit_report.find_urls
    urls = sorted({u.rstrip(".,;:") for u in re.findall(r"https?://(?:[^\s\"'<>()\[\]\\]|\([^\s\"'<>()]*\))+",
                                                         json.dumps(R))})
    if urls:
        parts.append("<h3>External references</h3><ul class='files'>" + "".join(
            f"<li><a href='{esc(u)}'>{esc(u)}</a></li>" for u in urls) + "</ul>")
    parts.append(f"<p class='audit-note'>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from "
                 "<code>report.json</code>. The data for every exhibit are embedded in this file and are also saved "
                 "in the CSV files linked above.</p>")
    title = "Appendix: Audit Trail" if mode == "paper" else "Audit Trail"
    return (f"<section class='prose audit-trail' id='audit'><h2>{title}</h2>"
            f"<details><summary>Data provenance, file checksums, and the source of every number</summary>"
            f"{''.join(parts)}</details></section>")


def finding_html(s) -> str:
    if isinstance(s, dict):
        lead = f"<strong>{inline_md(s['lead'])}</strong> " if s.get("lead") else ""
        return lead + inline_md(s.get("text", ""))
    return inline_md(s)


def tiles_html(R: dict) -> str:
    def tile(h):
        note = f"<p class='t-note'>{esc(h['note'])}</p>" if h.get("note") else ""
        return (f"<div class='tile'><p class='t-label'>{esc(h.get('label'))}</p>"
                f"<p class='t-value'>{esc(h.get('value'))}</p>{note}</div>")
    hl = R.get("highlights") or []
    return f"<div class='tiles'>{''.join(tile(h) for h in hl)}</div>" if hl else ""


def ex_range(nums: list[int]) -> str:
    if not nums:
        return ""
    return f"Exhibit {nums[0]}" if len(nums) == 1 else f"Exhibits {nums[0]}–{nums[-1]}"


def validate_report(R: dict):
    """Hard requirements of the report (chartbook) layout; audit_report.py checks the rest."""
    n = len(R.get("exhibits", []))
    errs = []
    placed = [int(x) for s in R.get("sections", []) for x in s.get("exhibits", [])]
    if placed != list(range(1, n + 1)):
        errs.append(f"the sections must show every exhibit exactly once, in order 1..{n} "
                    f"(exhibits are numbered in reading order); got {placed}")
    for k, s in enumerate(R.get("sections", []), start=1):
        if not str(s.get("body", "")).strip():
            errs.append(f"section {k} ({s.get('heading')!r}) has no body: it opens with a summary of its findings")
    cov = (R.get("cover") or {}).get("exhibit")
    if cov and not 1 <= int(cov) <= n:
        errs.append(f"cover.exhibit {cov} is not an exhibit number")
    if errs:
        fail("report structure:\n  - " + "\n  - ".join(errs))


def paper_body(R: dict, ex_blocks: dict, meta, badge, methods, refs, repl, audit) -> str:
    summary_html = "".join(f"<p>{finding_html(s)}</p>" for s in R.get("summary") or [])
    placed, sec_html, toc = set(), [], []
    for k, s in enumerate(R.get("sections", []), start=1):
        nums = [int(n) for n in s.get("exhibits", []) if int(n) in ex_blocks and int(n) not in placed]
        placed.update(nums)
        sid = f"section-{k}"
        toc.append(f"<li><a href='#{sid}'>{esc(s.get('heading'))}</a></li>")
        sec_html.append(f"<section class='prose' id='{sid}'><h2>{esc(s.get('heading'))}</h2>"
                        f"{blocks_md(s.get('body', ''))}{''.join(ex_blocks[n] for n in nums)}</section>")
    rest = [n for n in ex_blocks if n not in placed]
    rest_html = (f"<section class='exhibits'>{'<h2>Exhibits</h2>' if sec_html else ''}"
                 f"{''.join(ex_blocks[n] for n in rest)}</section>") if rest else ""
    toc_items = toc + ["<li><a href='#methods'>Appendix: Data and Methods</a></li>",
                       "<li><a href='#replication'>Appendix: Replication Materials</a></li>",
                       "<li><a href='#audit'>Appendix: Audit Trail</a></li>"]
    nav = f"<nav class='toc'><h2>Contents</h2><ol>{''.join(toc_items)}</ol></nav>" if toc else ""
    about = (f"<section class='prose about' id='about'><h2>About This Document</h2>{blocks_md(R['about'])}</section>"
             if R.get("about") else "")
    return f"""
{disclaimer_html()}
<header class="masthead">
  <p class="kicker">{meta}{badge}</p>
  <h1>{esc(R.get('title'))}</h1>
  {f'<p class="dek">{inline_md(R["subtitle"])}</p>' if R.get('subtitle') else ''}
</header>
<section class="summary">
  <h2>Summary</h2>
  {tiles_html(R)}
  {summary_html}
</section>
{nav}
{''.join(sec_html)}
{rest_html}
{methods}
{refs}
{repl}
{audit}
{footnotes_html(R.get('footnotes') or [])}
{about}
"""


def report_body(R: dict, ex_blocks: dict, meta, badge, methods, refs, repl, audit) -> str:
    """Exhibit chartbook: cover, At a Glance, Contents, List of Exhibits, Notes, Introduction,
    sections (opener + one exhibit page each), appendixes, About This Document, supplementary."""
    sections, exhibits = R.get("sections", []), R.get("exhibits", [])
    sec_nums = [[int(n) for n in s.get("exhibits", [])] for s in sections]
    cov = R.get("cover") or {}
    hero = ""
    if cov.get("exhibit"):
        ex = exhibits[int(cov["exhibit"]) - 1]
        t = cov.get("title") or ex.get("title")
        sub = f"<p class='ex-sub'>{esc(ex.get('subtitle'))}</p>" if ex.get("subtitle") else ""
        hero = (f"<figure class='cover-chart'><p class='cover-chart-title'>{esc(t)}</p>{sub}"
                f"<div class='legend' data-legend='cover'></div>"
                f"<div class='chart' data-chart='cover' role='img' aria-label='{esc(t)}'></div></figure>")
    dek = f'<p class="dek">{inline_md(R["subtitle"])}</p>' if R.get("subtitle") else ""
    cover = (f'<header class="masthead cover" id="cover"><p class="kicker">{meta}{badge}</p>'
             f'<h1>{esc(R.get("title"))}</h1>{dek}{hero}</header>')
    g = R.get("at_a_glance") or {}
    findings = g.get("findings") or R.get("summary") or []
    glance = (f"<section class='glance' id='at-a-glance'><h2>At a Glance</h2>{blocks_md(g.get('text', ''))}"
              + ("<ul class='glance-findings'>" + "".join(f"<li>{finding_html(s)}</li>" for s in findings)
                 + "</ul>" if findings else "") + f"{tiles_html(R)}</section>")
    intro = R.get("introduction") or {}
    intro_title = intro.get("title", "Introduction")
    subs = intro.get("subsections") or []
    items = []
    if intro:
        sub_list = ("<ul>" + "".join(f"<li><a href='#intro-{j}'>{esc(x.get('heading'))}</a></li>"
                                     for j, x in enumerate(subs, start=1)) + "</ul>") if subs else ""
        items.append(f"<li><a href='#introduction'>{esc(intro_title)}</a>{sub_list}</li>")
    for k, (s, nums) in enumerate(zip(sections, sec_nums), start=1):
        rng = f"<span class='ex-range'>{ex_range(nums)}</span>" if nums else ""
        items.append(f"<li><a href='#section-{k}'>{esc(s.get('heading'))}</a>{rng}</li>")
    if R.get("footnotes"):
        items.append("<li><a href='#footnotes'>Notes</a></li>")
    items.append("<li><a href='#methods'>Appendix A: Data and Methods</a></li>")
    appx = R.get("appendixes") or []
    letters = [chr(ord("B") + j) for j in range(len(appx))]
    items += [f"<li><a href='#appendix-{L}'>Appendix {L}: {esc(a.get('title'))}</a></li>" for L, a in zip(letters, appx)]
    if refs:
        items.append("<li><a href='#references'>References</a></li>")
    items.append("<li><a href='#about'>About This Document</a></li>")
    items.append("<li class='supp'>Supplementary materials: <a href='#replication'>Replication Materials</a>"
                 " · <a href='#audit'>Audit Trail</a></li>")
    contents = f"<nav class='contents toc' id='contents'><h2>Contents</h2><ol class='contents-list'>{''.join(items)}</ol></nav>"
    groups = "".join(
        f"<h3>{esc(s.get('heading'))}</h3><ol class='ex-list'>" + "".join(
            f"<li><a href='#exhibit-{n}'><span class='ex-n'>{n}.</span> {esc(exhibits[n - 1].get('title'))}</a></li>"
            for n in nums) + "</ol>"
        for s, nums in zip(sections, sec_nums) if nums)
    ex_list = f"<nav class='exhibit-list' id='list-of-exhibits'><h2>List of Exhibits</h2>{groups}</nav>" if groups else ""
    notes = R.get("notes_page")
    if notes and R.get("data_needs"):
        notes = ([notes] if isinstance(notes, str) else list(notes)) + [
            "Appendix A lists the data that would make these estimates more accurate."]
    notes_html = (f"<section class='prose notes-page' id='notes'><h2>Notes About This Report</h2>{blocks_md(notes)}</section>"
                  if notes else "")
    intro_html = ""
    if intro:
        sc = "intro" if intro.get("footnotes") else None
        sub_html = "".join(f"<h3 id='intro-{j}'>{esc(x.get('heading'))}</h3>{blocks_md(x.get('body', ''), sc)}"
                           for j, x in enumerate(subs, start=1))
        intro_html = (f"<section class='prose intro' id='introduction'><h2>{esc(intro_title)}</h2>"
                      f"{blocks_md(intro.get('text', ''), sc)}{sub_html}"
                      f"{footnotes_html(intro.get('footnotes') or [], sc, None)}</section>")
    sec_html = []
    for k, (s, nums) in enumerate(zip(sections, sec_nums), start=1):
        sc = f"s{k}" if s.get("footnotes") else None
        sec_html.append(f"<section class='chapter' id='section-{k}'><div class='section-opener prose'>"
                        f"<h2>{esc(s.get('heading'))}</h2>{blocks_md(s.get('body', ''), sc)}</div>"
                        f"{''.join(ex_blocks[n] for n in nums)}"
                        f"{footnotes_html(s.get('footnotes') or [], sc, None)}</section>")
    global_fn = footnotes_html(R.get("footnotes") or []).replace("<section class='prose footnotes'>",
                                                                 "<section class='prose footnotes' id='footnotes'>", 1)
    extra = "".join(f"<section class='prose appendix' id='appendix-{L}'><h2>Appendix {L}: {esc(a.get('title'))}</h2>"
                    f"{blocks_md(a.get('body', ''))}</section>" for L, a in zip(letters, appx))
    about = f"<section class='prose about' id='about'><h2>About This Document</h2>{blocks_md(R.get('about', ''))}</section>"
    supp = (f"<div class='supplementary'><p class='supp-label'>Supplementary materials (not part of the publication)</p>"
            f"{repl}{audit}</div>")
    return "\n".join([disclaimer_html(), cover, glance, contents, ex_list, notes_html, intro_html, *sec_html,
                      global_fn, methods, extra, refs, about, supp])


# ----------------------------------------------------------------------------- build
def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
    except ValueError:
        return False


def build(report_path: str, out_path: str | None = None) -> dict:
    base = os.path.dirname(os.path.abspath(report_path))
    if _inside(base, SKILL):
        fail(f"{base} is inside the skill folder; build reports under the analysis folder (--out), never in the skill")
    with open(report_path, encoding="utf-8") as f:
        R = MA.merged(base, json.load(f))
    mode = R.get("mode", "report")
    if mode not in ("report", "paper"):
        fail(f"mode must be 'report' or 'paper', not {mode!r}")
    out_path = out_path or os.path.join(base, "report.html")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    ctx = Ctx(R, base)

    REF_LABELS.clear()
    REF_LABELS.update({r["id"]: r.get("label", r["id"]) for r in R.get("references", []) if r.get("id")})
    unknown = cited_ids(json.dumps(R)) - set(REF_LABELS)
    if unknown:
        fail(f"[@citations] without a matching entry in references: {sorted(unknown)}")
    facts_path = os.path.join(base, R.get("facts", "facts.json"))
    facts = json.load(open(facts_path, encoding="utf-8")) if os.path.exists(facts_path) else {}
    R, used = fill_facts(R, facts, mode)
    ctx.R = R
    left = ANY_PLACEHOLDER.findall(json.dumps({k: v for k, v in R.items() if k != "request"}, ensure_ascii=False))
    if left:
        fail(f"unresolved or malformed fact placeholders (use {{{{name}}}} or {{{{name|fmt}}}}): {left}")
    if not R.get("date"):
        R["date"] = date.today().isoformat()
        print(f"[build] report.json has no \"date\"; using today ({R['date']})", file=sys.stderr)
    bad = [d["path"] for d in R.get("data_files", []) if not os.path.exists(os.path.join(base, d["path"]))]
    bad += [e["data"] for e in R.get("exhibits", []) if e.get("data") and not os.path.exists(os.path.join(base, e["data"]))]
    if bad:
        fail(f"linked files do not exist (every link must resolve): {bad}")
    # files the source terms keep local are never linked from the page (it travels with the package)
    decisions = {d["path"]: PS.decide(d["path"], base, ctx.policies, R, ctx.withheld) for d in R.get("data_files", [])}
    keep_local = [p for p, d in decisions.items() if not d["ok"] and d["policy"]]
    if keep_local:
        print(f"[build] not linked: {keep_local} (the source terms keep these files local)", file=sys.stderr)
        R["data_files"] = [d for d in R["data_files"] if d["path"] not in keep_local]
    if (R.get("replication") or {}).get("include_microdata"):
        print("[build] replication.include_microdata is ignored: microdata of a definition_only source never "
              "goes into a package", file=sys.stderr)

    sections = R.get("sections", [])
    ex_scope: dict[int, str | None] = {}
    if mode == "report":
        validate_report(R)
        for k, s in enumerate(sections, start=1):
            for n in s.get("exhibits", []):
                ex_scope[int(n)] = f"s{k}" if s.get("footnotes") else None
    cover_ex = int((R.get("cover") or {}).get("exhibit") or 0) if mode == "report" else 0

    payload, ex_blocks = [], {}
    for i, ex in enumerate(R.get("exhibits", []), start=1):
        ch = ex.get("chart") or {}
        rec = ch.get("recessions")
        if rec:
            ch["_recessions"] = load_recessions(base, rec if isinstance(rec, str) else "exhibits/recessions.csv")
            note = ch.get("recession_note") or REC_NOTE
            if note not in str(ex.get("notes", "")):
                ex["notes"] = (str(ex.get("notes", "")).rstrip() + " " + note).strip()
        rows, cols, csv_text = load_rows(ex, base)
        show = ch.get("columns")                         # display/download only these columns (table exhibits)
        if show:
            miss = [c for c in show if c not in cols]
            if miss:
                fail(f"exhibit {i}: chart.columns {miss} are not in {ex.get('data')}")
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=show, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
            rows, cols, csv_text = [{k: r[k] for k in show} for r in rows], list(show), buf.getvalue()
        spec = chart_spec(ex, rows)
        fname = os.path.basename(ex.get("data", f"exhibit_{i}.csv"))
        fmt = ch.get("format", "number")
        payload.append({"id": i, "chart": spec, "csv": csv_text, "csv_name": fname, "columns": cols, "format": fmt})
        if i == cover_ex and spec:
            payload.append({"id": "cover", "chart": spec, "csv": "", "csv_name": "", "columns": [], "format": fmt})
        ex_blocks[i] = exhibit_html(i, ex, spec, fname, mode, ex_scope.get(i))

    files = []
    for d in R.get("data_files", []):
        p = d["path"]
        rel = os.path.relpath(os.path.join(base, p), out_dir).replace("\\", "/")
        files.append(f"<li><a href='{esc(rel)}'>{esc(d.get('label') or p)}</a> <code>{esc(p)}</code></li>")
    files_html = f"<h3>Data Underlying the Exhibits</h3><ul class='files'>{''.join(files)}</ul>" if files else ""
    request_html = f"<h3>Request</h3><blockquote>{inline_md(R['request'])}</blockquote>" if R.get("request") else ""
    cite_html = f"<h3>Data Citation</h3><p class='cite'>{inline_md(R['citation'])}</p>" if R.get("citation") else ""
    methods_title = "Appendix A: Data and Methods" if mode == "report" else "Appendix: Data and Methods"
    methods_html = (f"<section class='prose methods appendix' id='methods'><h2>{methods_title}</h2>{request_html}"
                    f"{blocks_md(R.get('methods', ''))}{vintage_html(ctx)}{decisions_html(R)}{assumptions_html(R)}{data_needs_html(R)}{files_html}"
                    f"{cite_html}{notices_html(ctx)}</section>")

    write_lib(ctx)
    for sid, P in ctx.policies.items():
        if P["redistribution"]["mode"] == "definition_only":
            write_reproduce(ctx, sid)
        elif os.path.isdir(os.path.join(base, "reproduce", sid)):
            shutil.rmtree(os.path.join(base, "reproduce", sid))
    package, members, excluded = make_package(ctx)
    R.setdefault("data_files", [])
    if package not in [d["path"] for d in R["data_files"]]:
        R["data_files"].append({"label": "Replication package (ZIP)", "path": package})
    repl_html, attachments = replication_html(ctx, out_dir, package, members, excluded, mode)
    audit = audit_html(ctx, out_dir, facts, used, mode)

    meta = " · ".join(esc(x) for x in [R.get("organization"), long_date(R.get("date"))] if x)
    badge = f' <span class="badge">{esc(R.get("status"))}</span>' if R.get("status") else ""
    parts = dict(meta=meta, badge=badge, methods=methods_html, refs=references_html(R), repl=repl_html, audit=audit)
    body = report_body(R, ex_blocks, **parts) if mode == "report" else paper_body(R, ex_blocks, **parts)

    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    attach_json = json.dumps(attachments, ensure_ascii=False).replace("</", "<\\/")
    page = (tpl.replace("__TITLE__", esc(R.get("title", "Report")))
               .replace("__MODE__", mode)
               .replace("<!--__BODY__-->", body)
               .replace("<!--__FOOTER__-->", footer_html(ctx))
               .replace("/*__DATA__*/null", data_json)
               .replace("/*__ATTACH__*/null", attach_json))
    leaks = scan_secrets.scan_text(page, scan_secrets.known_keys(), name="report.html")
    if leaks:
        if os.path.exists(out_path):
            os.remove(out_path)
        fail("an API key or credential appeared in the report content; nothing was written ("
             + "; ".join(leaks[:3]) + ")")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    ext = re.findall(r'(?:\bsrc\s*=\s*["\']|<link[^>]+href\s*=\s*["\']|@import\s+|url\(\s*["\']?)'
                     r'(?:https?:)?//', page, re.I)
    res = {"report": os.path.abspath(out_path), "mode": mode, "sources": ctx.ids, "exhibits": len(ex_blocks),
           "facts_cited": len({n for n, _ in used}), "bytes": len(page.encode()), "external_refs": len(ext),
           "package": {"file": os.path.join(base, package), "members": len(members) + 1,
                       "left_out": [f"{e['name']}: {e['reason']}" for e in excluded
                                    if not e["name"].startswith(("lib/", "reproduce/"))][:20]},
           "notices": len(ctx.notices)}
    print(json.dumps(res))
    return res


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):       # Windows pipes default to cp1252; prose may hold "−" or "®"
        if (getattr(_s, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    build(a.report, a.out)


if __name__ == "__main__":
    main()

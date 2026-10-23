"""Offline tests of the shared analysis and report chain: factfmt, facts, build_report, audit_report,
scan_secrets and style_check.

The report tests build a small two-source report in a temporary folder. The two sources are FAKE packs
("alpha", allowed with a FRED-style withhold rule, and "beta", definition_only like IPUMS) whose policies
are written into a temporary sources folder (policyset.SOURCES_DIR is pointed there). Their data are
synthetic numbers written by the test itself; they stand for no real source and no real response.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
SCRIPTS = os.path.join(SKILL, "scripts")
for _p in (os.path.join(SCRIPTS, "analysis"), os.path.join(SCRIPTS, "report"), SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

from core import policy as pol  # noqa: E402
import audit_report  # noqa: E402
import build_report  # noqa: E402
import factfmt  # noqa: E402
import facts as factsmod  # noqa: E402
import policyset  # noqa: E402
import scan_secrets  # noqa: E402
import style_check  # noqa: E402
from factfmt import format_value as fv  # noqa: E402

ALPHA_NOTICE = "This product uses the Alpha Test API but is not endorsed or certified by the Alpha Test Agency."
BETA_NOTICE = "Beta Test Survey data are used under terms that forbid redistributing the extract."

POLICIES = {
    "alpha": {
        "id": "alpha", "title": "Alpha Test Statistics", "agency": "Alpha Test Agency", "depth": "standard",
        "domains": ["test"], "homepage": "https://example.org/alpha",
        "auth": {"env": "ALPHA_TEST_API_KEY", "required": "optional", "placement": "query", "param": "api_key",
                 "signup_url": "https://example.org/alpha/key", "echoes_key": False},
        "limits": {"rps": 1},
        "routes": {"api": {"base": "https://example.org/alpha/api", "probe": "https://example.org/alpha/api",
                           "hosts": ["example.org"]},
                   "manual": {"how": "Download the CSV from the test page and upload it."}},
        "cache": {"mode": "run_only", "why": "test"},
        "redistribution": {"mode": "allowed", "why": "Synthetic test source (public domain).",
                           "withhold": {"meta": "series.csv", "key": "series_id", "column": "copyright",
                                        "values": ["copyrighted"]}},
        "notices": [ALPHA_NOTICE],
        "citation": "Alpha Test Agency, {dataset}, retrieved {retrieved_long}.",
    },
    "beta": {
        "id": "beta", "title": "Beta Test Survey", "agency": "Beta Test Center", "depth": "deep",
        "domains": ["test"], "homepage": "https://example.org/beta",
        "auth": {"env": "BETA_TEST_API_KEY", "required": "required", "placement": "header", "param": "Authorization",
                 "signup_url": "https://example.org/beta/key", "echoes_key": True},
        "limits": {"rps": 1},
        "routes": {"api": {"base": "https://example.org/beta/api", "probe": "https://example.org/beta/api",
                           "hosts": ["example.org"]},
                   "manual": {"how": "Build the same extract on the test website and download the CSV."}},
        "cache": {"mode": "run_only", "why": "test"},
        "redistribution": {"mode": "definition_only", "why": "Synthetic stand-in for terms that forbid sharing extracts.",
                           "microdata_globs": ["*_extract.csv", "*.dat"],
                           "package_include": ["request.json", "README.md", "codebook.csv"]},
        "notices": [BETA_NOTICE],
        "audit_hooks": [{"id": "beta_codebook", "kind": "require_file", "glob": "data/beta/codebook.csv",
                         "why": "the codebook documents the extract"}],
        "citation": "Beta Test Center, {dataset}, retrieved {retrieved_long}.",
    },
}

ANALYSIS = r'''"""Synthetic analysis for the report-chain test."""
import os
import pandas as pd
from facts import Facts, save_exhibit
import alphastats

OUT = os.path.dirname(os.path.abspath(__file__))
obs = pd.read_csv(os.path.join(OUT, "data", "alpha", "obs.csv"), dtype={"series_id": str, "date": str})
a = obs[obs.series_id == "A1"].copy()
a["year"] = a["date"].str[:4]
ex1 = a[["year", "date", "value"]].reset_index(drop=True)
save_exhibit(ex1, OUT, "exhibits/exhibit_1.csv", packs=["alpha"])
micro = pd.read_csv(os.path.join(OUT, "data", "beta", "person_extract.csv"))
ex2 = micro.groupby("group")["employed"].mean().reset_index(name="rate")
save_exhibit(ex2, OUT, "exhibits/exhibit_2.csv", packs=["beta"])
F = Facts(OUT)
F.add("alpha_first", ex1, where={"year": "2022"}, column="value", source="exhibits/exhibit_1.csv", fmt="pct100:1",
      label="Alpha rate, 2022")
F.add("alpha_last", ex1, where={"year": "2024"}, column="value", source="exhibits/exhibit_1.csv", fmt="pct100:1",
      label="Alpha rate, 2024", preliminary=True)
F.add("alpha_change", value=alphastats.change(ex1["value"]), derivation="alpha_last - alpha_first", fmt="pp100:1",
      pack="alpha", label="Change in the alpha rate")
F.add("beta_rate_x", ex2, where={"group": "X"}, column="rate", source="exhibits/exhibit_2.csv", fmt="pct:0",
      label="Employment rate, group X")
F.add("beta_rate_y", ex2, where={"group": "Y"}, column="rate", source="exhibits/exhibit_2.csv", fmt="pct:0",
      label="Employment rate, group Y")
F.save()
'''

REPORT = {
    "mode": "report",
    "title": "Alpha Rates and Beta Employment, 2022 to 2024",
    "subtitle": "A synthetic two-source report used to test the report chain",
    "date": "2026-10-20",
    "sources": ["alpha", "beta"],
    "request": "Build a test report from two fake sources.",
    "at_a_glance": {
        "text": "This test report examines a synthetic alpha rate from 2022 to 2024 and synthetic employment by group.",
        "findings": [
            {"lead": "Alpha Rates.", "text": "The alpha rate reached {{alpha_last}} in 2024, a preliminary value."},
            {"lead": "Change Over Time.", "text": "The rate rose by {{alpha_change}} from 2022 to 2024."},
            {"lead": "Beta Employment.", "text": "The employment rate was {{beta_rate_x}} in group X and "
                                                 "{{beta_rate_y}} in group Y."}]},
    "notes_page": ["Unless otherwise indicated, all years are calendar years.",
                   "Numbers in the text and exhibits may not add up to totals because of rounding.",
                   "The data are the vintage retrieved in October 2026; both test sources revise their data."],
    "introduction": {"text": "The alpha rate was {{alpha_first}} in 2022 and {{alpha_last}} in 2024, a preliminary "
                             "value that the agency revises.",
                     "subsections": [{"heading": "How Did the Alpha Rate Change?",
                                      "body": "The rate rose by {{alpha_change}} over the period."},
                                     {"heading": "What Data Were Used?",
                                      "body": "The report uses two synthetic sources built for this test."}]},
    "sections": [
        {"heading": "Alpha Rates", "body": "The alpha rate rose from {{alpha_first}} in 2022 to {{alpha_last}} "
                                           "in 2024, a preliminary value (see Exhibit 1).", "exhibits": [1]},
        {"heading": "Beta Employment", "body": "Employment differed by group (see Exhibit 2).", "exhibits": [2]}],
    "exhibits": [
        {"title": "Alpha Rate, 2022 to 2024", "subtitle": "Percent",
         "text": "The alpha rate climbed in each year. The latest value, {{alpha_last}}, is preliminary.",
         "data": "exhibits/exhibit_1.csv",
         "chart": {"type": "line", "x": "year", "y": "value", "format": "pct100", "decimals": 1},
         "notes": "The latest value is preliminary.",
         "source": "Analyst calculations using data from the Alpha Test Agency."},
        {"title": "Employment Rate by Group, 2024", "subtitle": "Percent",
         "text": "Group X had a higher employment rate ({{beta_rate_x}}) than group Y ({{beta_rate_y}}).",
         "data": "exhibits/exhibit_2.csv",
         "chart": {"type": "column", "x": "group", "y": "rate", "format": "pct", "decimals": 0},
         "notes": "Rates are unweighted shares of the synthetic extract.",
         "source": "Analyst calculations using data from the Beta Test Center."}],
    "methods": ["The alpha series is the vintage retrieved from the test API; the agency revises it [@alpha_doc].",
                "The beta extract stays in the analysis folder; only its definition is shared."],
    "assumptions": [{"parameter": "Year basis", "value": "Calendar years", "basis": "precedent",
                     "evidence": "The test documentation reports calendar years [@alpha_doc]."}],
    "references": [{"id": "alpha_doc", "label": "Alpha 2026", "citation": "Alpha Test Agency, *Test Documentation* (2026)",
                    "url": "https://example.org/alpha/doc", "accessed": "2026-10-20", "opened": True}],
    "about": "This document was prepared to test the report chain of the data-analyst skill.",
    "data_files": [{"label": "Alpha observations (CSV)", "path": "data/alpha/obs.csv"},
                   {"label": "Beta codebook (CSV)", "path": "data/beta/codebook.csv"}],
    "clearance": [{"source": "alpha", "key": "C9", "basis": "synthetic test series, not cleared", "redistribute": False}],
}

DECISIONS = {
    "questions": [{"id": "year_basis", "question": "Calendar or fiscal years?",
                   "options": ["Calendar years (recommended)", "Fiscal years", "Don't know — decide for me"],
                   "asked": True, "answer": "dont_know", "how": "skipped", "resolution": "Calendar years",
                   "assumption": "Year basis"}],
    "literature_search": [{"query": "alpha rate methodology", "date": "2026-10-20", "tool": "WebSearch",
                           "found": ["alpha_doc"]}],
    "data_needs": [{"id": "alpha_pub", "data": "The agency's published alpha table", "improves": ["Alpha rate"],
                    "effect": "a benchmark for the headline rate", "how_to_get": "the Alpha Test Agency release page",
                    "from_user": False}],
}
BENCHMARK = ("measure,period,computed,published,source,url,accessed\n"
             "Alpha rate,2024,5.25,not checked,Alpha Test Agency,,\n")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if isinstance(text, str) else json.dumps(text, indent=2))


# A deep pack's --reproduce-bundle (docs/pack_contract.md): download scripts with the request built in.
BETA_FETCH = r"""import json, os, sys
a = sys.argv
d = a[a.index("--reproduce-bundle") + 1]
spec = json.load(open(a[1], encoding="utf-8"))
body = json.dumps({"samples": spec["samples"]})
with open(os.path.join(d, "download_beta.sh"), "w", newline="\n") as f:
    f.write("#!/usr/bin/env bash\n# request: " + body + "\ncurl -K - https://example.org/beta/api\n")
with open(os.path.join(d, "download_beta.ps1"), "w") as f:
    f.write("# request: " + body + "\nInvoke-WebRequest https://example.org/beta/api\n")
with open(os.path.join(d, "beta_request.json"), "w") as f:
    f.write(body)
with open(os.path.join(d, "DOWNLOAD.md"), "w") as f:
    f.write("# Other ways to get the beta extract\n")
print(json.dumps({"status": "ok", "scripts": {"bash": "download_beta.sh", "powershell": "download_beta.ps1"},
                  "doc": "DOWNLOAD.md", "request": "beta_request.json", "files": sorted(os.listdir(d))}))
"""


def make_sources(d):
    for sid, P in POLICIES.items():
        write(os.path.join(d, sid, "policy.json"), P)
    write(os.path.join(d, "beta", "fetch.py"), BETA_FETCH)
    write(os.path.join(d, "alpha", "lib", "alphastats.py"),
          'def change(s):\n    """Last value minus first value."""\n    return float(s.iloc[-1]) - float(s.iloc[0])\n')


def make_out(out):
    write(os.path.join(out, "data", "alpha", "obs.csv"),
          "series_id,date,value\nA1,2022-01-01,4.0\nA1,2023-01-01,4.5\nA1,2024-01-01,5.25\nC9,2024-01-01,99\n")
    write(os.path.join(out, "data", "alpha", "series.csv"),
          "series_id,title,units,frequency,copyright,citation\n"
          "A1,Alpha rate,Percent,Annual,public_domain,\"Alpha Test Agency, Alpha rate [A1].\"\n"
          "C9,Copyrighted index,Index,Annual,copyrighted,\"Owner, Copyrighted index [C9].\"\n")
    write(os.path.join(out, "data", "alpha", "raw", "A1_resp.json"), {"note": "synthetic"})
    write(os.path.join(out, "data", "alpha", "raw", "C9_resp.json"), {"note": "synthetic"})
    write(os.path.join(out, "data", "alpha", "README.md"), "# Alpha Test Statistics: downloaded data (synthetic)\n")
    write(os.path.join(out, "data", "alpha", "request.json"),
          {"source": "alpha", "spec": {"description": "alpha test", "requests": [{"name": "obs", "endpoint": "obs"}]},
           "route": "api", "acquired": "api", "retrieved": "2026-10-01T12:00:00+00:00", "vintage": "latest",
           "latest_period": "2024 M01", "requests": [{"url": "https://example.org/alpha/api/obs"}],
           "tables": [{"name": "obs", "file": "obs.csv", "rows": 4, "columns": ["series_id", "date", "value"]}]})
    write(os.path.join(out, "data", "beta", "person_extract.csv"),
          "id,group,employed\n1,X,1\n2,X,1\n3,X,0\n4,X,1\n5,Y,1\n6,Y,0\n7,Y,0\n8,Y,1\n")
    write(os.path.join(out, "data", "beta", "codebook.csv"), "variable,label\ngroup,Synthetic group\nemployed,Employed\n")
    write(os.path.join(out, "data", "beta", "README.md"), "# Beta Test Survey extract (synthetic)\n")
    write(os.path.join(out, "data", "beta", "request.json"),
          {"source": "beta", "spec": {"description": "beta extract", "samples": ["s1"], "variables": ["group"]},
           "route": "manual_download", "retrieved": "2026-10-02T00:00:00+00:00"})
    write(os.path.join(out, "manual_download", "beta", "INSTRUCTIONS.md"), "# How to download the beta extract\n")
    write(os.path.join(out, "analysis.py"), ANALYSIS)
    write(os.path.join(out, "report.json"), REPORT)
    write(os.path.join(out, "decisions.json"), DECISIONS)
    write(os.path.join(out, "exhibits", "benchmark.csv"), BENCHMARK)


def run_analysis(out):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([os.path.join(SCRIPTS, "analysis"),
                                                         os.path.join(policyset.SOURCES_DIR, "alpha", "lib")])}
    p = subprocess.run([sys.executable, os.path.join(out, "analysis.py")], cwd=out, capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        r = fn(*a, **k)
    return r, buf.getvalue()


def fails(results):
    return [r for r in results if not r[1]]


class ReportChainBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="da-report-")
        cls.src = os.path.join(cls.tmp, "sources")
        make_sources(cls.src)
        cls._old = policyset.SOURCES_DIR
        policyset.SOURCES_DIR = cls.src
        cls.out = os.path.join(cls.tmp, "out")
        make_out(cls.out)
        run_analysis(cls.out)
        cls.res, _ = quiet(build_report.build, os.path.join(cls.out, "report.json"))
        cls.zip = zipfile.ZipFile(os.path.join(cls.out, "replication_package.zip"))
        cls.names = set(cls.zip.namelist())
        with open(os.path.join(cls.out, "report.html"), encoding="utf-8") as f:
            cls.page = f.read()

    @classmethod
    def tearDownClass(cls):
        cls.zip.close()
        policyset.SOURCES_DIR = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy_out(self, name):
        dst = os.path.join(self.tmp, name)
        shutil.copytree(self.out, dst)
        return dst


class TestBuild(ReportChainBase):
    def test_policies_valid(self):
        for sid in POLICIES:
            self.assertEqual(pol.validate(pol.load(os.path.join(self.src, sid)), os.path.join(self.src, sid)), [])

    def test_html_builds(self):
        self.assertEqual(self.res["sources"], ["alpha", "beta"])
        self.assertEqual(self.res["external_refs"], 0)
        self.assertIn('class="disclaimer"', self.page)
        self.assertIn("Data Vintage", self.page)
        self.assertIn("5.3 percent", self.page)                        # alpha_last 5.25 as pct100:1 (half up)
        self.assertIn("1.3 percentage points", self.page)              # alpha_change 1.25 as pp100:1
        visible = audit_report.re.sub(r"<blockquote>.*?</blockquote>|<pre\b.*?</pre>|<script\b.*?</script>", "",
                                      self.page, flags=audit_report.re.S)
        self.assertNotIn("{{", visible)

    def test_notices_present(self):
        text = audit_report.htmlmod.unescape(self.page)
        self.assertIn(ALPHA_NOTICE, text)
        self.assertIn(BETA_NOTICE, text)
        self.assertIn("Required Notices", self.page)

    def test_method_record_rendered(self):
        for h in ("Questions and Decisions", "Literature Searched", "Data That Would Improve These Estimates",
                  "Skipped (treated as Don", "Appendix A lists the data", "the Alpha Test Agency release page"):
            self.assertIn(h, self.page)
        self.assertIn("decisions.json", self.names)

    def test_zip_allowed_files_only(self):
        n = self.names
        for want in ("REPLICATION.md", "analysis.py", "facts.json", "report.json", "exhibits/exhibit_1.csv",
                     "exhibits/exhibit_2.csv", "data/alpha/obs.csv", "data/alpha/series.csv",
                     "data/alpha/request.json", "data/alpha/raw/A1_resp.json", "data/beta/request.json",
                     "data/beta/README.md", "data/beta/codebook.csv", "lib/facts.py", "lib/factfmt.py",
                     "lib/combine.py", "lib/run_analysis.py", "lib/alpha/alphastats.py"):
            self.assertIn(want, n)
        self.assertNotIn("data/alpha/raw/C9_resp.json", n)              # withheld key: raw file left out
        obs = self.zip.read("data/alpha/obs.csv").decode()
        self.assertIn("A1,2024-01-01,5.25", obs)
        self.assertNotIn("C9", obs)                                     # withheld rows dropped
        self.assertIn("C9", open(os.path.join(self.out, "data", "alpha", "obs.csv")).read())   # local file intact
        self.assertNotIn("manual_download/beta/INSTRUCTIONS.md", n)

    def test_definition_only_microdata_refused_and_reproduce(self):
        self.assertNotIn("data/beta/person_extract.csv", self.names)
        self.assertIn("reproduce/beta/REPRODUCE.md", self.names)
        self.assertIn("reproduce/beta/spec.json", self.names)
        for f in ("download_beta.sh", "download_beta.ps1", "beta_request.json", "DOWNLOAD.md"):
            self.assertIn(f"reproduce/beta/{f}", self.names)
        self.assertNotIn("reproduce/beta/manual/INSTRUCTIONS.md", self.names)    # the pack's scripts replace it
        self.assertIn('"s1"', self.zip.read("reproduce/beta/download_beta.sh").decode())   # request built in
        txt = self.zip.read("reproduce/beta/REPRODUCE.md").decode()
        self.assertIn("bash reproduce/beta/download_beta.sh", txt)
        self.assertIn(r'-File ".\reproduce\beta\download_beta.ps1"', txt)
        self.assertLess(txt.index("bash reproduce/beta/download_beta.sh"), txt.index("python"))   # script first
        self.assertIn("--source beta reproduce/beta/spec.json --out . --ingest reproduce/beta", txt)
        self.assertIn("bash reproduce/beta/download_beta.sh", self.zip.read("REPLICATION.md").decode())
        # attached to the page with Download buttons, the scripts before the request
        for f in ("REPRODUCE.md", "download_beta.sh", "download_beta.ps1", "beta_request.json"):
            self.assertIn(f"Download {f}</button>", self.page)
        self.assertLess(self.page.index("Download download_beta.sh"), self.page.index("Download beta_request.json"))
        self.assertIn("<code>reproduce/beta/download_beta.sh</code> (macOS, Linux)", self.page)
        self.assertEqual(json.loads(self.zip.read("reproduce/beta/spec.json"))["samples"], ["s1"])
        self.assertIn("data/beta/person_extract.csv", self.zip.read("REPLICATION.md").decode())   # listed as left out
        self.assertNotIn("href='../data/beta/person_extract.csv'", self.page)
        self.assertIn("local only, not distributed", self.page)

    def test_microdata_refused_even_when_listed(self):
        out = self.copy_out("extra_listed")
        R = json.load(open(os.path.join(out, "report.json")))
        R["replication"] = {"extra": ["data/beta/person_extract.csv"], "include_microdata": True}
        R["data_files"].append({"label": "Extract", "path": "data/beta/person_extract.csv"})
        write(os.path.join(out, "report.json"), R)
        quiet(build_report.build, os.path.join(out, "report.json"))
        with zipfile.ZipFile(os.path.join(out, "replication_package.zip")) as z:
            self.assertNotIn("data/beta/person_extract.csv", z.namelist())
        page = open(os.path.join(out, "report.html"), encoding="utf-8").read()
        self.assertNotIn("person_extract.csv'>Extract", page)
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        self.assertTrue(any(c == "links" and "person_extract" in m for c, ok, m in fails(r)), fails(r))

    def test_lib_written_outside_skill(self):
        self.assertTrue(os.path.exists(os.path.join(self.out, "lib", "facts.py")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "lib", "alpha", "alphastats.py")))
        self.assertFalse(os.path.exists(os.path.join(SKILL, "lib")))
        self.assertFalse(os.path.exists(os.path.join(SCRIPTS, "analysis", "facts.json")))

    def test_build_refuses_inside_skill(self):
        with self.assertRaises(SystemExit):
            quiet(build_report.build, os.path.join(SKILL, "report.json"))

    def test_facts_carry_pack(self):
        F = json.load(open(os.path.join(self.out, "facts.json")))
        self.assertEqual(F["alpha_last"]["pack"], "alpha")
        self.assertEqual(F["beta_rate_x"]["pack"], "beta")
        self.assertEqual(F["alpha_change"]["pack"], "alpha")
        self.assertEqual(F["alpha_last"]["source"], "exhibits/exhibit_1.csv")
        self.assertTrue(F["alpha_last"]["preliminary"])
        self.assertEqual(F["alpha_last"]["text"], "5.3 percent")


class TestAudit(ReportChainBase):
    def test_audit_passes_good_report(self):
        r, printed = quiet(audit_report.audit, os.path.join(self.out, "report.json"), rerun=True)
        self.assertEqual(fails(r), [], printed)
        self.assertTrue(any(c == "policy" and "beta hook beta_codebook" in m for c, ok, m in r))
        self.assertTrue(any(c == "replication" and "rerun reproduces" in m for c, ok, m in r))

    def test_audit_main_exit_code(self):
        code, _ = quiet(audit_report.main, [os.path.join(self.out, "report.json")])
        self.assertEqual(code, 0)

    def test_audit_fails_on_forced_microdata(self):
        out = self.copy_out("forced")
        with zipfile.ZipFile(os.path.join(out, "replication_package.zip"), "a") as z:
            z.write(os.path.join(out, "data", "beta", "person_extract.csv"), "data/beta/person_extract.csv")
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        bad = fails(r)
        self.assertTrue(any(c == "replication" and "person_extract" in m for c, ok, m in bad), bad)

    def test_audit_fails_on_missing_notice(self):
        out = self.copy_out("no_notice")
        p = os.path.join(out, "report.html")
        page = open(p, encoding="utf-8").read()
        write(p, page.replace(build_report.esc(BETA_NOTICE), "").replace(BETA_NOTICE, ""))
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        self.assertTrue(any(c == "policy" and "notice missing" in m for c, ok, m in fails(r)), fails(r))

    def test_audit_fails_on_missing_hook_file(self):
        out = self.copy_out("no_codebook")
        os.remove(os.path.join(out, "data", "beta", "codebook.csv"))
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        self.assertTrue(any(c == "policy" and "beta_codebook" in m for c, ok, m in fails(r)), fails(r))

    def test_audit_fails_without_clearance(self):
        out = self.copy_out("no_clearance")
        R = json.load(open(os.path.join(out, "report.json")))
        R.pop("clearance")
        write(os.path.join(out, "report.json"), R)
        quiet(build_report.build, os.path.join(out, "report.json"))
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        self.assertTrue(any(c == "policy" and "alpha C9" in m for c, ok, m in fails(r)), fails(r))

    def test_audit_fails_without_download_scripts(self):
        out = self.copy_out("no_scripts")
        zp = os.path.join(out, "replication_package.zip")
        with zipfile.ZipFile(zp) as z:
            keep = {n: z.read(n) for n in z.namelist() if not n.endswith((".sh", ".ps1"))}
        with zipfile.ZipFile(zp, "w") as z:
            for n, b in keep.items():
                z.writestr(n, b)
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        self.assertTrue(any(c == "replication" and "download_*.sh" in m for c, ok, m in fails(r)), fails(r))

    def test_audit_fails_on_withheld_rows(self):
        out = self.copy_out("withheld_rows")
        with zipfile.ZipFile(os.path.join(out, "replication_package.zip"), "a") as z:
            z.writestr("data/alpha/extra.csv", "series_id,value\nC9,1\n")
        r, _ = quiet(audit_report.audit, os.path.join(out, "report.json"))
        self.assertTrue(any(c == "replication" and "withheld" in m for c, ok, m in fails(r)), fails(r))

    def test_hand_typed_numbers(self):
        self.assertEqual(audit_report.hard_numbers("In 2024, Exhibits 2, 3, and 4 show U-6 and 2026Q2.", set()), [])
        self.assertEqual(audit_report.hard_numbers("The rate was 4.2 percent on March 31, 2026.", set()), ["4.2"])
        self.assertEqual(audit_report.hard_numbers("It cost $2,000 in fiscal year 2025.", set()), ["$2,000"])


class TestPolicySet(unittest.TestCase):
    def test_aggregates_only(self):
        P = {"id": "g", "redistribution": {"mode": "aggregates_only", "microdata_globs": ["records_*.csv"],
                                           "package_include": [], "withhold": None}, "audit_hooks": []}
        tmp = tempfile.mkdtemp()
        try:
            self.assertFalse(policyset.decide("data/g/records_2024.csv", tmp, {"g": P}, {})["ok"])
            self.assertTrue(policyset.decide("data/g/table1.csv", tmp, {"g": P}, {})["ok"])
            self.assertFalse(policyset.decide("data/loose.csv", tmp, {"g": P}, {})["ok"])   # strictest rule
            self.assertTrue(policyset.decide("exhibits/records_x.csv", tmp, {"g": P}, {})["ok"])
        finally:
            shutil.rmtree(tmp)

    def test_size_limit(self):
        P = {"id": "g", "redistribution": {"mode": "allowed", "microdata_globs": [], "package_include": [],
                                           "withhold": None}, "audit_hooks": []}
        tmp = tempfile.mkdtemp()
        try:
            write(os.path.join(tmp, "data", "g", "big.csv"), "x\n1\n")
            old = policyset.MICRO_MAX
            policyset.MICRO_MAX = 2
            try:
                d = policyset.decide("data/g/big.csv", tmp, {"g": P}, {})
            finally:
                policyset.MICRO_MAX = old
            self.assertFalse(d["ok"])
            self.assertFalse(d["policy"])
            self.assertIn("MB", d["reason"])
        finally:
            shutil.rmtree(tmp)

    def test_forbid_hook(self):
        P = {"id": "g", "redistribution": {"mode": "allowed", "microdata_globs": [], "package_include": [],
                                           "withhold": None},
             "audit_hooks": [{"id": "h", "kind": "forbid_in_package", "globs": ["exhibits/private_*"], "why": "t"}]}
        self.assertFalse(policyset.decide("exhibits/private_a.csv", ".", {"g": P}, {})["ok"])


# ---------------------------------------------------------------------- factfmt: every code
class TestFactFmt(unittest.TestCase):
    def test_raw(self):
        self.assertEqual(fv(2015.0), "2015")
        self.assertEqual(fv(0.1 + 0.2), "0.3")
        self.assertEqual(fv(1234.5), "1234.5")                 # raw never groups digits
        self.assertEqual(fv("text"), "text")
        self.assertEqual(fv(-3.0, "raw:abs"), "3")

    def test_percent_family(self):
        self.assertEqual(fv(0.8734, "pct:0"), "87 percent")
        self.assertEqual(fv(0.145, "pct:0"), "15 percent")     # half up on the decimal value
        self.assertEqual(fv(87.34, "pct100:1"), "87.3 percent")
        self.assertEqual(fv(2.25, "pct100:1"), "2.3 percent")  # not half-even
        self.assertEqual(fv(-0.04, "pct100:1"), "0.0 percent")  # no negative zero

    def test_points(self):
        self.assertEqual(fv(0.038, "pp:1"), "3.8 percentage points")
        self.assertEqual(fv(-3.8, "pp100:1"), "3.8 percentage points")
        self.assertEqual(fv(1, "pp100:0"), "1 percentage point")
        self.assertEqual(fv(1.0, "pp100:1"), "1.0 percentage point")
        self.assertEqual(fv(0.1, "pp100:1"), "0.1 percentage point")
        self.assertEqual(fv(0.2, "pp100:1"), "0.2 percentage points")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            fv(3.8, "pp:1")
        self.assertIn("pp100", err.getvalue())

    def test_basis_points(self):
        self.assertEqual(fv(0.0025, "bp:0"), "25 basis points")
        self.assertEqual(fv(-0.25, "bp100:0"), "25 basis points")
        self.assertEqual(fv(0.01, "bp100:0"), "1 basis point")

    def test_dollars(self):
        self.assertEqual(fv(80612, "dollars:0"), "$80,612")
        self.assertEqual(fv(80650, "dollars:-2"), "$80,700")
        self.assertEqual(fv(34.3, "dollars:2"), "$34.30")
        self.assertEqual(fv(-12.5, "dollars:0"), "-$13")
        self.assertEqual(fv(-0.004, "dollars:2"), "$0.00")

    def test_num(self):
        self.assertEqual(fv(25812, "num:0"), "25,812")
        self.assertEqual(fv(7, "num:0"), "seven")
        self.assertEqual(fv(0, "num"), "zero")
        self.assertEqual(fv(7.25, "num:1"), "7.3")
        self.assertEqual(fv(-3, "num:0"), "-3")

    def test_scales(self):
        self.assertEqual(fv(254, "thousands:-3"), "254,000")
        self.assertEqual(fv(-1.5, "thousands:0"), "-1,500")
        self.assertEqual(fv(25.8e6, "millions:1"), "25.8 million")
        self.assertEqual(fv(25.8e9, "billions:1"), "25.8 billion")
        self.assertEqual(fv(29.4e12, "trillions:1"), "29.4 trillion")
        self.assertEqual(fv(159044, "kmillions:1"), "159.0 million")

    def test_usd(self):
        self.assertEqual(fv(1.832e9, "usd_m:0"), "$1,832 million")
        self.assertEqual(fv(1.8328e12, "usd_b:1"), "$1,832.8 billion")
        self.assertEqual(fv(3.621e13, "usd_t:2"), "$36.21 trillion")
        self.assertEqual(fv(25.8e9, "dollars_b:1"), "$25.8 billion")       # alias of usd_b
        self.assertEqual(fv(29.4e12, "dollars_t:1"), "$29.4 trillion")
        self.assertEqual(fv(5e6, "dollars_m:0"), "$5 million")
        self.assertEqual(fv(-1.8e12, "usd_t:1"), "-$1.8 trillion")
        self.assertEqual(fv(1832.8, "usd_b_scaled:1"), "$1,832.8 billion")
        self.assertEqual(fv(1832, "usd_m_scaled:0"), "$1,832 million")
        self.assertEqual(fv(36.21, "usd_t_scaled:2"), "$36.21 trillion")

    def test_ratio_index_year(self):
        self.assertEqual(fv(0.991, "ratio:2"), "0.99")
        self.assertEqual(fv(123.456, "index:1"), "123.5")
        self.assertEqual(fv(2015.0, "year"), "2015")

    def test_prefixes_and_abs(self):
        self.assertEqual(fv(0.4, "signed:pct100:1"), "+0.4 percent")
        self.assertEqual(fv(-0.4, "signed:pct100:1"), "-0.4 percent")
        self.assertEqual(fv(0.01, "signed:pct100:1"), "0.0 percent")       # rounds to zero: no sign
        self.assertEqual(fv(-0.123, "pct:1:abs"), "12.3 percent")           # opm's suffix
        self.assertEqual(fv(-0.123, "abs:pct:1"), "12.3 percent")
        self.assertEqual(fv(970065, "from_millions:dollars_b:1"), "$970.1 billion")
        self.assertEqual(fv(31422.5, "from_billions:dollars_t:1"), "$31.4 trillion")
        self.assertEqual(fv(159044, "from_thousands:millions:1"), "159.0 million")
        self.assertEqual(fv(-970065, "signed:from_millions:dollars_b:1"), "-$970.1 billion")

    def test_unknown_formats_raise(self):
        for bad in ("pcnt:1", "pct:1:xyz", "signed", "pct:a"):
            with self.assertRaises(ValueError):
                fv(1.0, bad)

    def test_every_kind_is_tested(self):
        src = open(__file__, encoding="utf-8").read()
        for k in factfmt.KINDS:
            self.assertRegex(src, rf'"(?:[a-z_]+:)*{k}(?::-?\d+)?(?::abs)?"', k)

    def test_placeholder_grammar(self):
        self.assertTrue(build_report.FACT_RE.fullmatch("{{x|signed:from_millions:dollars_b:1}}"))
        self.assertTrue(build_report.FACT_RE.fullmatch("{{x|pct:1:abs}}"))
        self.assertTrue(build_report.FACT_RE.fullmatch("{{ x }}"))


# ---------------------------------------------------------------------- facts registry
class TestFacts(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.out, "data", "bls"))

    def tearDown(self):
        shutil.rmtree(self.out)

    def table(self, rel, df):
        p = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        df.to_csv(p, index=False)
        return df

    def test_reads_cell_back_and_raises_on_mismatch(self):
        F = factsmod.Facts(self.out)
        df = self.table("exhibits/e.csv", pd.DataFrame({"year": [2024], "v": [1992.47]}))
        rec = F.add("ok", df, where={"year": 2024}, column="v", source="exhibits/e.csv")
        self.assertEqual(rec["value"], 1992.47)
        self.assertEqual(rec["pack"], "bls")                       # the only data/<id>/ folder
        df2 = pd.DataFrame({"year": [2024], "v": [2000.0]})
        with self.assertRaises(ValueError):
            F.add("bad", df2, where={"year": 2024}, column="v", source="exhibits/e.csv")

    def test_nan_and_formats_refused(self):
        F = factsmod.Facts(self.out)
        with self.assertRaises(ValueError):
            F.add("n", value=float("nan"), derivation="x")
        with self.assertRaises(ValueError):
            F.add("s", value=1832.8, derivation="x", fmt="usd_b:1")    # already scaled
        F.add("s2", value=1832.8, derivation="x", fmt="usd_b_scaled:1")
        with self.assertRaises(ValueError):
            F.add("m", value=5403326.76, derivation="x", fmt="usd_b:1")    # millions: would print $0.0 billion
        with self.assertRaises(ValueError):
            F.add("d", value=5403326758960.8, derivation="x", fmt="usd_b_scaled:1")   # dollars given to *_scaled
        F.add("z", value=-0.027, derivation="x", fmt="pct100:1")       # a rate may round to 0.0 percent
        with self.assertRaises(ValueError):
            F.add("g", value=-2.6, derivation="x", fmt="pp:1:abs")     # already in points: pp100
        F.add("g2", value=-0.026, derivation="x", fmt="pp:1:abs")
        self.assertEqual(factsmod.format_value(6, "signed:num:0"), "+6")   # a signed count stays in digits
        self.assertEqual(factsmod.format_value(6, "num:0"), "six")                  # reports spell it out
        self.assertEqual(factsmod.format_value(6, "num:0", mode="answer"), "6")     # answers keep digits the gate checks
        with self.assertRaises(ValueError):
            F.add("f", value=1.0, derivation="x", fmt="nope:1")
        with self.assertRaises(ValueError):
            F.add("w", value=1.0)                                      # no derivation

    def test_pack_inference(self):
        os.makedirs(os.path.join(self.out, "data", "bea"))
        F = factsmod.Facts(self.out)
        with self.assertRaises(ValueError):                            # two sources, nothing says which
            F.add("x", value=1.0, derivation="d")
        self.assertEqual(F.add("e", value=1.0, derivation="d", pack="bea")["pack"], "bea")
        self.assertEqual(F.add("l", value=1.0, derivation="d", pack=["bls", "bea"])["pack"], ["bls", "bea"])
        df = self.table("data/bea/t.csv", pd.DataFrame({"k": ["a"], "v": [2.0]}))
        self.assertEqual(F.add("d", df, where={"k": "a"}, column="v", source="data/bea/t.csv")["pack"], "bea")
        ex = pd.DataFrame({"k": ["a"], "v": [3.0]})
        factsmod.save_exhibit(ex, self.out, "exhibits/x.csv", packs=["bls"])
        self.assertEqual(F.add("x2", ex, where={"k": "a"}, column="v", source="exhibits/x.csv")["pack"], "bls")
        ex.attrs["packs"] = ["bea", "bls"]
        ex.to_csv(os.path.join(self.out, "exhibits", "y.csv"), index=False)
        self.assertEqual(F.add("y", ex, where={"k": "a"}, column="v", source="exhibits/y.csv")["pack"], ["bea", "bls"])
        self.assertEqual(factsmod.Facts(self.out, pack="bls").add("z", value=1.0, derivation="d")["pack"], "bls")
        pub = self.table("published/umich.csv", pd.DataFrame({"k": ["a"], "v": [46.3]}))
        F1 = factsmod.Facts(self.out)                                  # an agency page's figure: no pack
        self.assertNotIn("pack", F1.add("p", pub, where={"k": "a"}, column="v", source="published/umich.csv"))
        F2 = factsmod.Facts(self.out, pack="bls")                      # a default pack does not claim a page's figure
        self.assertNotIn("pack", F2.add("p2", pub, where={"k": "a"}, column="v", source="published/umich.csv"))
        self.assertNotIn("pack", F2.add("d2", value=1.0, derivation="d", pack=[]))   # pack=[]: none
        # a CSV value on a rounding tie for its format ("21.55" printed as "21.6") gets a note
        tie = self.table("exhibits/tie.csv", pd.DataFrame({"k": ["a"], "v": [21.55]}))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            F2.add("t", tie, where={"k": "a"}, column="v", source="exhibits/tie.csv", fmt="num:1", pack="bls")
        self.assertIn("rounding tie", err.getvalue())

    def test_save_writes_text_and_flags(self):
        F = factsmod.Facts(self.out)
        F.add("p", value=0.25, derivation="d", fmt="pct:0", preliminary=True, vintage="2020-07-30", projection=True)
        F.save()
        rec = json.load(open(os.path.join(self.out, "facts.json")))["p"]
        self.assertEqual(rec["text"], "25 percent")
        self.assertTrue(rec["preliminary"] and rec["projection"])
        self.assertEqual(rec["vintage"], "2020-07-30")


# ---------------------------------------------------------------------- secrets and style
class TestSecrets(unittest.TestCase):
    def test_scan(self):
        tmp = tempfile.mkdtemp()
        try:
            key = "zq" + "9" * 30
            write(os.path.join(tmp, "a.txt"), f"url https://x.org/api?series=1&api_key={key}\n")
            write(os.path.join(tmp, "ok.txt"), "url https://x.org/api?api_key=[REDACTED] and {API_KEY}\n"
                                               "sha " + "ab" * 32 + "\n")
            with zipfile.ZipFile(os.path.join(tmp, "p.zip"), "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("inner.txt", f"the key is {key.upper()}")
            self.assertTrue(scan_secrets.scan_file(os.path.join(tmp, "a.txt"), []))
            self.assertEqual(scan_secrets.scan_file(os.path.join(tmp, "ok.txt"), [], hex32=True), [])
            hits = scan_secrets.scan_file(os.path.join(tmp, "p.zip"), [key.encode()])
            self.assertTrue(any("p.zip!inner.txt" in h for h in hits), hits)
            self.assertTrue(all(key not in h and key.upper() not in h for h in hits))
            write(os.path.join(tmp, "h.txt"), "error: invalid key 0123456789abcdef0123456789abcdef\n")
            self.assertEqual(scan_secrets.scan_file(os.path.join(tmp, "h.txt"), []), [])
            self.assertTrue(scan_secrets.scan_file(os.path.join(tmp, "h.txt"), [], hex32=True))
        finally:
            shutil.rmtree(tmp)


class TestStyle(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(style_check.style_issues("We find that the rate rose."))
        self.assertEqual(style_check.style_issues("Title I funds rose in October 1 and under 5 U.S.C. 5332."), [])
        self.assertTrue(any("percent" in m for m in style_check.style_issues("The rate rose 5%.")))


if __name__ == "__main__":
    unittest.main()

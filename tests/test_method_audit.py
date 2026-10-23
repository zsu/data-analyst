"""Offline tests of the method record (scripts/report/method_audit.py) and the quick-answer gate
(scripts/report/audit_answer.py): questions with one "Don't know" option, skips treated as "Don't know",
blocking questions, the literature search, references opened, the evidence basis of each assumption,
the data that would improve the estimates, and benchmarks.

The quick-answer folder uses the real bls pack's policy but synthetic numbers written by the test itself;
they stand for no real BLS series or response.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
SCRIPTS = os.path.join(SKILL, "scripts")
for _p in (os.path.join(SCRIPTS, "analysis"), os.path.join(SCRIPTS, "report"), SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audit_answer  # noqa: E402
import method_audit as MA  # noqa: E402

DK = "Don't know — decide for me"
GOOD = {
    "questions": [
        {"id": "dollar_basis", "question": "Nominal or real dollars?", "asked": True,
         "options": ["Real, CPI-U (recommended)", "Nominal", DK], "answer": "dont_know", "how": "dont_know",
         "resolution": "Real dollars, CPI-U", "assumption": "deflator"},
        {"id": "period", "question": "Which years?", "asked": True,
         "options": ["Last 10 years (recommended)", "Since 2000", DK], "answer": "Since 2000",
         "assumption": "span"}],
    "literature_search": [{"query": "CPI-U deflating wages methodology", "date": "2026-10-20", "tool": "WebSearch",
                           "found": ["cpi_hb"]},
                          {"query": "regional wage deflators", "date": "2026-10-20", "found": [],
                           "note": "nothing beyond the handbook"}],
    "references": [{"id": "cpi_hb", "citation": "Test Agency, *Handbook of Methods*", "url": "https://example.org/hb",
                    "accessed": "2026-10-20", "opened": True, "used_for": "deflator"}],
    "assumptions": [
        {"parameter": "deflator", "value": "CPI-U", "basis": "precedent", "evidence": "The agency deflates wages "
         "with the CPI-U [@cpi_hb]."},
        {"parameter": "span", "value": "Since 2000", "basis": "user", "evidence": "The user chose it.",
         "question": "period"},
        {"parameter": "trend", "value": "Linear", "basis": "empirical",
         "evidence": "Fitted in exhibits/trend.csv; slope {{slope}}."},
        {"parameter": "top_code", "value": "Exclude top-coded rows", "basis": "judgment",
         "evidence": "No source settles it.", "why_not_empirical": "The extract has no uncapped values.",
         "sensitivity": "top_code"}],
    "data_needs": [{"id": "uncapped", "data": "Uncapped earnings records", "improves": ["top_code"],
                    "effect": "an empirical top-code adjustment", "how_to_get": "your payroll file", "from_user": True}],
}
SENS = ("parameter,base,alternative,headline_base,headline_alt,conclusion_changes\n"
        "top_code,exclude,include,1.0,1.1,no\n")
BENCH = "measure,period,computed,published,source\nslope,2024,0.5,0.5,Test Agency\n"
FACTS = {"slope": {"value": 0.5, "source": "exhibits/trend.csv", "where": {"year": "2024"}, "column": "slope",
                   "pack": "bls"}}

ANALYSIS = r'''import json, os
OUT = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(OUT, "exhibits"), exist_ok=True)
with open(os.path.join(OUT, "exhibits", "trend.csv"), "w", newline="\n") as f:
    f.write("year,slope\n2024,0.5\n")
with open(os.path.join(OUT, "facts.json"), "w") as f:
    json.dump({"slope": {"value": 0.5, "source": "exhibits/trend.csv", "where": {"year": "2024"},
               "column": "slope", "pack": "bls"}}, f)
'''


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if isinstance(text, str) else json.dumps(text, indent=2))


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
        r = fn(*a, **k)
    return r, buf.getvalue()


def fails(results, check=None):
    return [r for r in results if not r[1] and (check is None or r[0] == check)]


class MethodBase(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="da-method-")
        write(os.path.join(self.out, "exhibits", "trend.csv"), "year,slope\n2024,0.5\n")
        write(os.path.join(self.out, "exhibits", "sensitivity.csv"), SENS)
        write(os.path.join(self.out, "exhibits", "benchmark.csv"), BENCH)

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def check(self, D):
        r, _ = quiet(MA.check_method, D, self.out, FACTS)
        return r


class TestMethodRules(MethodBase):
    def test_good_record_passes(self):
        self.assertEqual(fails(self.check(GOOD)), [])

    def test_dont_know_option_exactly_once(self):
        D = copy.deepcopy(GOOD)
        D["questions"][0]["options"] = ["Real", "Nominal"]
        self.assertTrue(fails(self.check(D), "questions"))
        D["questions"][0]["options"] = ["Real", "Skip", DK]
        self.assertTrue(any("Skip" in m for _, _, m in fails(self.check(D), "questions")))
        D["questions"][0]["options"] = ["Real", DK, "I don't know"]
        self.assertTrue(fails(self.check(D), "questions"))

    def test_skip_and_not_asked_count_as_dont_know(self):
        for how in ("dont_know", "skipped", "not_asked"):
            D = copy.deepcopy(GOOD)
            q = D["questions"][0]
            q.update(how=how, answer=how)
            if how == "not_asked":
                q.update(asked=False, reason_code="no_user")
                D["disclosures"] = {"dollar_basis": "Nobody could be asked about dollars; real CPI-U was used."}
            self.assertEqual(fails(self.check(D)), [], how)
            q.pop("resolution")
            self.assertTrue(fails(self.check(D), "questions"), how)

    def test_not_asked_needs_a_reason_code_and_a_disclosure(self):
        D = copy.deepcopy(GOOD)
        q = D["questions"][0]
        q.update(asked=False, how="not_asked", answer="not_asked", reason="non-interactive run")
        self.assertTrue(fails(self.check(D), "questions"))               # free text is not enough
        q["reason_code"] = "no_user"
        self.assertTrue(any("dollar_basis" in m for _, _, m in fails(self.check(D), "disclosures")))
        D["disclosures"] = {"dollar_basis": "Not asked: no user could answer; real CPI-U dollars were used."}
        r = self.check(D)
        self.assertEqual(fails(r), [])
        self.assertTrue(any(c == "disclosures" and "disclose" in m for c, ok, m in r if ok))

    def test_settled_by_request_needs_the_quote_and_counts_as_answered(self):
        D = copy.deepcopy(GOOD)
        q = D["questions"][1]                                             # backs the user-basis "span"
        q.update(asked=False, answer=None, reason_code="settled_by_request")
        self.assertTrue(fails(self.check(D)))
        q["request"] = "since 2000"
        self.assertEqual(fails(self.check(D)), [])

    def test_user_basis_can_quote_the_request(self):
        D = copy.deepcopy(GOOD)
        D["questions"] = D["questions"][:1]
        D["assumptions"][1].pop("question")
        self.assertTrue(fails(self.check(D), "assumptions"))
        D["assumptions"][1]["request"] = "how have wages changed since 2000"
        self.assertEqual(fails(self.check(D), "assumptions"), [])

    def test_judgment_needs_no_evidence_field(self):
        D = copy.deepcopy(GOOD)
        D["assumptions"][3].pop("evidence")
        self.assertEqual(fails(self.check(D), "assumptions"), [])
        D["assumptions"][0].pop("evidence")                               # precedent still needs it
        self.assertTrue(any("evidence" in m for _, _, m in fails(self.check(D), "assumptions")))

    def test_dont_know_cannot_rest_on_user(self):
        D = copy.deepcopy(GOOD)
        D["questions"][0]["assumption"] = "span"
        self.assertTrue(fails(self.check(D), "questions"))

    def test_blocking_question(self):
        D = copy.deepcopy(GOOD)
        D["questions"][0].update(blocking=True, how="skipped")
        self.assertTrue(fails(self.check(D), "questions"))                  # no rationale, no follow-up
        D["questions"][0].update(rationale="literal reading: all states")
        self.assertTrue(fails(self.check(D), "questions"))                  # still no follow-up
        D["questions"][0].update(followed_up=True)
        r = self.check(D)
        self.assertEqual(fails(r), [])
        self.assertTrue(any("first line of the answer" in m for _, ok, m in r if ok))

    def test_literature_required(self):
        D = copy.deepcopy(GOOD)
        D["literature_search"] = []
        self.assertTrue(fails(self.check(D), "literature"))
        D["literature_search"] = [{"unavailable": True}]
        self.assertTrue(fails(self.check(D), "literature"))
        D["literature_search"] = [{"unavailable": True, "reason": "no search tool here"}]
        self.assertTrue(fails(self.check(D), "literature"))              # free text is not enough
        D["literature_search"] = [{"unavailable": True, "reason_code": "no_search_tool"}]
        self.assertEqual(fails(self.check(D), "literature"), [])
        self.assertTrue(fails(self.check(D), "disclosures"))             # and the answer must say so
        D["disclosures"] = {"literature": "No literature search was run: this environment has no search tool."}
        self.assertEqual(fails(self.check(D)), [])
        D["literature_search"] = [{"query": "x", "date": "2026-10-20", "found": ["nope"]}]
        self.assertTrue(fails(self.check(D), "literature"))

    def test_reference_must_be_opened(self):
        D = copy.deepcopy(GOOD)
        D["references"][0]["opened"] = False
        bad = fails(self.check(D))
        self.assertTrue(any(c == "references" for c, _, _ in bad))
        self.assertTrue(any(c == "assumptions" and "deflator" in m for c, _, m in bad))
        D = copy.deepcopy(GOOD)
        D["assumptions"][0]["evidence"] = "See [@missing]."
        self.assertTrue(any("[@missing]" in m for _, _, m in fails(self.check(D), "references")))

    def test_empirical_needs_existing_csv_or_fact(self):
        D = copy.deepcopy(GOOD)
        D["assumptions"][2]["evidence"] = "Fitted in exhibits/missing.csv."
        self.assertTrue(fails(self.check(D), "assumptions"))
        D["assumptions"][2]["evidence"] = "Slope {{slope}}."
        self.assertEqual(fails(self.check(D), "assumptions"), [])

    def test_user_basis_needs_a_user_answer(self):
        D = copy.deepcopy(GOOD)
        D["questions"][1].update(answer="dont_know", resolution="Since 2000")
        self.assertTrue(fails(self.check(D), "assumptions"))

    def test_judgment_needs_why_and_sensitivity_row(self):
        D = copy.deepcopy(GOOD)
        D["assumptions"][3].pop("why_not_empirical")
        self.assertTrue(fails(self.check(D), "assumptions"))
        D = copy.deepcopy(GOOD)
        D["assumptions"][3]["sensitivity"] = "other"
        self.assertTrue(fails(self.check(D), "assumptions"))
        os.remove(os.path.join(self.out, "exhibits", "sensitivity.csv"))
        self.assertTrue(fails(self.check(GOOD), "assumptions"))

    def test_unknown_basis(self):
        D = copy.deepcopy(GOOD)
        D["assumptions"][0]["basis"] = "expert opinion"
        self.assertTrue(fails(self.check(D), "assumptions"))

    def test_data_needs(self):
        D = copy.deepcopy(GOOD)
        D["data_needs"] = []
        self.assertTrue(any("top_code" in m for _, _, m in fails(self.check(D), "data_needs")))
        D.pop("data_needs")
        self.assertTrue(fails(self.check(D), "data_needs"))
        D = copy.deepcopy(GOOD)
        D["data_needs"][0].pop("how_to_get")
        self.assertTrue(fails(self.check(D), "data_needs"))
        D = copy.deepcopy(GOOD)                                    # an unchecked benchmark needs an entry too
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              BENCH + "wage,2024,30,not checked,Test Agency\n")
        self.assertTrue(any("wage" in m for _, _, m in fails(self.check(D), "data_needs")))
        D["data_needs"][0]["improves"].append("wage")
        self.assertEqual(fails(self.check(D), "data_needs"), [])

    def test_no_data_needs_with_note(self):
        D = copy.deepcopy(GOOD)
        D["assumptions"] = D["assumptions"][:3]
        D["data_needs"] = []
        self.assertTrue(fails(self.check(D), "data_needs"))
        D["data_needs_note"] = "The agency publishes every value used."
        self.assertEqual(fails(self.check(D)), [])

    def test_benchmark_compares_values(self):
        bench = os.path.join(self.out, "exhibits", "benchmark.csv")
        head = "measure,period,computed,published,source,url,accessed,match,note\n"
        ok = [("slope", "2024", "0.5", "0.5", "", ""), ("rate", "2026", "3.419", "3.4", "yes", ""),
              ("earnings", "2025", "72376", "72380", "yes", "Census rounds medians to $10"), ("level", "2025", "23943307", "23943305", "", ""),
              ("votes", "2022", "219 D / 24 R", "219 D / 24 R", "yes", ""),
              ("laws", "2024", "5", "not checked", "", "")]
        rows = lambda rs: head + "".join(f"{m},{p},{c},{pb},src,https://x.gov,2026-10-21,{y},{n}\n"
                                         for m, p, c, pb, y, n in rs)
        write(bench, rows(ok))
        self.assertEqual(fails(MA.check_benchmark(self.out, FACTS)), [])
        write(bench, rows([("slope", "2024", "0.5", "0.6", "yes", "")]))              # says yes, differs
        self.assertTrue(any("says it matches" in m for _, _, m in fails(MA.check_benchmark(self.out))))
        write(bench, rows([("slope", "2024", "0.5", "0.6", "no", "")]))               # differs, no cause given
        self.assertTrue(any("differ by more than rounding" in m for _, _, m in fails(MA.check_benchmark(self.out))))
        write(bench, rows([("slope", "2024", "0.5", "0.6", "no", "BLS excludes self-employed")]))
        self.assertEqual(fails(MA.check_benchmark(self.out)), [])
        self.assertEqual(MA.bench_number("$72,380"), (72380.0, 0.5))       # exact unless a note says rounded
        self.assertEqual(MA.bench_number("140,000"), (140000.0, 140.0))      # three zeros: rounding, capped
        write(bench, rows([("deficit", "2025", "-931964", "-931960", "yes", "")]))   # an exact count, off by 4
        self.assertTrue(fails(MA.check_benchmark(self.out)))
        self.assertEqual(MA.bench_number("1992473.0"), (1992473.0, 0.5))
        self.assertIsNone(MA.bench_number("219 D / 24 R"))
        self.assertTrue(MA._is_fact(2209848, 0.5, [-2209848.0]))      # a decline written without its sign
        write(bench, rows([("m2", "2026", "5.66", "5.65891", "yes", "")]))       # computed shown rounded: agrees
        self.assertEqual(fails(MA.check_benchmark(self.out)), [])
        write(bench, rows([("m2", "2026", "6", "5.65891", "yes", "")]))          # but not to any coarse rounding
        self.assertTrue(fails(MA.check_benchmark(self.out)))
        write(bench, rows([("dsr", "2026", "11.111", "11.31", "yes", "within published rounding")]))
        self.assertTrue(fails(MA.check_benchmark(self.out)))             # a "rounding" note cannot excuse a 0.2 gap
        write(bench, rows([("earnings", "2025", "72376", "72380", "yes", "Census rounds medians to $10")]))
        self.assertEqual(fails(MA.check_benchmark(self.out)), [])
        write(bench, rows([("debt", "2026", "32425013", "32525013", "yes", "rounded to $1 million")]))
        self.assertTrue(fails(MA.check_benchmark(self.out)))             # a $100 billion gap is no rounding
        write(bench, rows([("income", "2025", "72376", "72400", "yes", "published rounded to $100")]))
        self.assertEqual(fails(MA.check_benchmark(self.out)), [])
        write(bench, rows([("pages", "2025", "16494", "16336", "yes", "computed is around the published value")]))
        self.assertTrue(fails(MA.check_benchmark(self.out)))             # "around" is not "rounding"
        write(bench, head + "Median days, 119th Congress,2026,194,999,src,https://x.gov,2026-10-21,,x\n")
        self.assertTrue(any("quote fields" in m for _, _, m in fails(MA.check_benchmark(self.out))))   # shifted row

    def test_benchmark(self):
        self.assertEqual(fails(MA.check_benchmark(self.out)), [])
        os.remove(os.path.join(self.out, "exhibits", "benchmark.csv"))
        self.assertTrue(fails(MA.check_benchmark(self.out)))

    def test_merged_prefers_report(self):
        write(os.path.join(self.out, "decisions.json"), GOOD)
        R = MA.merged(self.out, {"assumptions": [{"parameter": "deflator", "value": "PCE"}]})
        self.assertEqual({a["parameter"]: a for a in R["assumptions"]}["deflator"]["value"], "PCE")
        self.assertEqual(len(R["assumptions"]), 4)
        self.assertEqual(R["references"][0]["id"], "cpi_hb")
        self.assertEqual(len(R["questions"]), 2)


class TestAnswerGate(MethodBase):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.out, "decisions.json"), GOOD)
        write(os.path.join(self.out, "facts.json"), FACTS)
        write(os.path.join(self.out, "analysis.py"), ANALYSIS)
        write(os.path.join(self.out, "data", "bls", "request.json"),
              {"source": "bls", "route": "api_keyless", "retrieved": "2026-10-20T00:00:00+00:00"})
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5 (BLS, retrieved 2026-10-20; subject to "
                                                   "revision), the same as the published figure. BLS.gov cannot vouch "
                                                   "for the data or analyses derived from these data after the data "
                                                   "have been retrieved from BLS.gov.\n")

    def test_passes_with_rerun(self):
        r, _ = quiet(audit_answer.audit, self.out, rerun=True)
        self.assertEqual(fails(r), [])
        self.assertTrue(any(c == "replication" and "reproduces" in m for c, _, m in r))
        code, printed = quiet(audit_answer.main, [self.out])
        self.assertEqual(code, 0, printed)
        self.assertIn("AUDIT PASSED", printed)

    def test_fails_without_decisions(self):
        os.remove(os.path.join(self.out, "decisions.json"))
        code, printed = quiet(audit_answer.main, [self.out])
        self.assertEqual(code, 1)
        self.assertIn("decisions.json missing", printed)

    def test_fails_on_mismatched_fact(self):
        F = copy.deepcopy(FACTS)
        F["slope"]["value"] = 0.6
        write(os.path.join(self.out, "facts.json"), F)
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(fails(r, "facts"))

    def test_rerun_restores_a_changed_exhibit(self):
        trend = os.path.join(self.out, "exhibits", "trend.csv")
        write(trend, "year,slope\n2024,0.5\n# hand edit\n")                # analysis.py will rewrite it
        r, _ = quiet(audit_answer.audit, self.out, rerun=True)
        self.assertTrue(any("changed exhibits" in m.replace(os.sep, "/") for _, _, m in fails(r, "replication")))
        with open(trend, encoding="utf-8") as f:
            self.assertIn("# hand edit", f.read())                         # the original is back

    def test_answer_numbers_must_be_facts(self):
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.6.\n")       # a typo
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(any("0.6" in m for _, _, m in fails(r, "answer")))
        write(os.path.join(self.out, "answer.md"), "The slope was 50 percent in 2024 (0.50), as published.\n")  # share as percent
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertEqual(fails(r, "answer"), [])
        os.remove(os.path.join(self.out, "answer.md"))
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(any("answer.md missing" in m for _, _, m in fails(r, "answer")))

    def test_answer_numbers_skip_ids_years_and_allowed(self):
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "H.R. 1-10,564 and H.R. 4346 passed 243-187 (roll 404) in the 117th Congress on 2022-07-28 and on "
            "October 21, 2026; P.L. 118-31; Tables 1.1.1, 1.1.2 and 2.6; line 32 of chapter 5, session 2; HTTP 403; "
            "LNS14024887 in 2026 Q3: 9.9% (90 percent margin of error) and $1.99 trillion, 3 of 12 states\n"
            "| 2 (tie) | House Natural Resources | 162 |")]
        self.assertEqual(toks, ["9.9%", "$1.99 trillion", "3", "12", "162", "243", "187"])   # a vote tally is two facts
        # en-dash ranges, plural and volume identifiers, and a share table's 100 percent total are not statistics
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "Volumes 18 and 37 (parts 72–78, titles 28–41) were missing; vol. 17 has 382 pages.\n"
            "| Notes | 37.3% |\n| **Total** | 100.0% |\n\nAll 100 percent of 9 lines.")]
        self.assertEqual(toks, ["382", "37.3%", "100 percent", "9"])
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "EO 14296 and Executive Orders 14134 to 14371, E.O. 13810 and Proclamation 10886 were 238 orders.")]
        self.assertEqual(toks, ["238"])
        self.assertEqual([n["tok"] for n in audit_answer.answer_numbers("the per-1,000 ratio is 19.4")], ["19.4"])
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "IPUMS CPS extract 30: workers 16 and over, ages 25 to 64, children under 15 who live alone, "
            "aged 65 were 9.7 percent; under 15 percent; under 50 bills")]
        self.assertEqual(toks, ["9.7 percent", "15 percent", "50"])
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "Adults 25+; NIPA Handbook ch. 13 and sec. 4.2; beyond 1.645 standard errors; 48.3 percent")]
        self.assertEqual(toks, ["48.3 percent"])
        # a year is a count only after a count word and before a plural noun; in a table, a lone cell under a value column
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "The budget had 2017 pages; in 2026 refunds rose; their 2022 peaks; September 2025 is shown.\n\n"
            "| Volume | FY2017 pages | Year |\n|---|---|---|\n| Total | 2018 | 2019 |\n")]
        self.assertEqual(toks, ["2017", "2018"])
        # first-column labels, survey codes and replicate ranges are not statistics
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "RELATE 1, OWNERSHP in (1, 2), GQ = 2, REPWT1-80, FY2024-25; US 5 states\n| 35 to 44 | 61.1 |\n| 70.4 |")]
        self.assertEqual(toks, ["5", "61.1", "70.4"])
        # a percent with fewer decimals than its fact is not that fact, unless hedged ("about 22 percent")
        f224 = audit_answer.fact_display({"value": 22.4, "fmt": "pct100:1"})
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("22 percent")[0], *f224))
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("about 22 percent")[0], *f224))
        self.assertEqual([n["tok"] for n in audit_answer.answer_numbers(
            "above the 15 to 20 percent range; about 45 to 48 percent; rose from 3 to 5 percent")], ["3", "5 percent"])
        self.assertEqual(audit_answer._at("against 30.6 percent and 0.6 percent", "0.6 percent"), 25)
        # a bare count is not a percent fact; a bare decimal in prose ("from 11.7 to 6.5") still is
        m13 = audit_answer.fact_display({"value": -13.0, "fmt": "pct100:0"})
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("13 of the 15 departments")[0], *m13))
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("fell from 11.7 to 6.5 hires")[0],
                                              *audit_answer.fact_display({"value": 11.7, "fmt": "pct100:1"})))
        f019 = audit_answer.fact_display({"value": 0.19, "fmt": "num:2"})
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("a margin of 0.2")[0], *f019))
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("about 0.2")[0], *f019))
        self.assertEqual([n["tok"] for n in audit_answer.answer_numbers(
            "version 1 for August; RPP (U.S. = 100); months in sample 4 and 8; rose 4.5 percent")], ["4.5 percent"])
        self.assertEqual([n["tok"] for n in audit_answer.answer_numbers(
            "(collection BILLS, congress 118); Table 2.1, footnote 5; 274 laws")], ["274"])
        # a number dated in the text must be a fact of that year
        f24 = {"value": 6.3, "fmt": "pct100:1", "where": {"year": "2024"}}
        self.assertTrue(audit_answer._wrong_year(" percent in 2019, ...", [f24]))
        self.assertFalse(audit_answer._wrong_year(" percent in 2024", [f24]))
        self.assertFalse(audit_answer._wrong_year(" percent in 2019", [{"value": 6.3, "where": {"group": "all"}}]))
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "GAO-26-900644, p. 9 and pp. 12-14, page 31 of USDL-26-0229: 671 products; BLS table 37b; "
            "ages 55-59, 60-64 and 65 or more; 65 or more workers")]
        self.assertEqual(toks, ["671", "65"])
        from audit_report import find_urls
        self.assertEqual(find_urls("see https://x.gov/RA%20(GW).aspx) and (https://y.gov/a)."),
                         ["https://x.gov/RA%20(GW).aspx", "https://y.gov/a"])
        # a published percent is not a percentage-point difference
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("3.0 percentage points")[0], 3.0, "pubpct", 1))
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("3.0 percent")[0], 3.0, "pubpct", 1))
        import scan_secrets
        hexid = "d7e27b7b09a3a7feae95b9c61781fcd8"           # a made-up 32-hex value
        self.assertEqual(scan_secrets.scan_text(f"https://www.federalreserve.gov/x.aspx?rel=H15&series={hexid}", hex32=True), [])
        self.assertTrue(scan_secrets.scan_text(f"https://api.bls.gov/x?registrationkey={hexid}", hex32=True))
        # a table read by hand from an agency page names its page and date on every row
        pub = os.path.join(self.out, "published")
        os.makedirs(pub, exist_ok=True)
        write(os.path.join(pub, "dash.csv"), "state,employees,source_url,accessed\n"
              "MD,121612,https://data.opm.gov/x,2026-10-21\nDC,114531,,2026-10-21\n")
        r = audit_answer.check_published(self.out)
        self.assertFalse(r[0][1])
        self.assertIn("1 row(s)", r[0][2])
        write(os.path.join(pub, "dash.csv"), "state,employees,source_url,accessed\nMD,121612,https://data.opm.gov/x,2026-10-21\n")
        self.assertEqual([(x[1], x[3]) for x in audit_answer.check_published(self.out)], [(True, ["https://data.opm.gov/x"])])
        shutil.rmtree(pub)
        # a published benchmark figure is quoted at its published precision, never a coarser rounding
        pub = (2.35, "pub", 2)
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("2.35 points")[0], *pub))
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("2.4 percent")[0], *pub))
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("in 1 year")[0], 1.149, "pubpct", 3))
        n = audit_answer.answer_numbers("$1.99 trillion")[0]
        self.assertEqual((n["num"] * n["scale"], n["tol"] * n["scale"]), (1.99e12, 0.005e12))
        # a fact shows in its format's unit: a pct100 fact of 0.3 is "0.3 percent", never "30"
        f = {"value": 0.3, "fmt": "pct100:1"}
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("0.3 percent")[0], *audit_answer.fact_display(f)))
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("30 states")[0], *audit_answer.fact_display(f)))
        b = {"value": 1992473224352.42, "fmt": "usd_b:1"}
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("| 1,992.5 |")[0], *audit_answer.fact_display(b)))
        neg = {"value": -2.2, "fmt": "pct100:1"}          # "fell 2.2 percent" may drop the sign; "-2.2" never adds one
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("fell 2.2 percent")[0], *audit_answer.fact_display(neg)))
        pos = {"value": 2.2, "fmt": "pct100:1"}
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("-2.2 percent")[0], *audit_answer.fact_display(pos)))
        idx = {"value": 110.4, "fmt": "index:1"}           # a dollar amount is never an index value
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("$110")[0], *audit_answer.fact_display(idx)))
        toks = [n["tok"] for n in audit_answer.answer_numbers(
            "267,442 for FY2024 and 146,482 for FY2025; 4.1 in FY2024 and 77 in FY2025; FY2024-25; Note 12; "
            "the 10 Yr rate; CPI-W (1982-84=100) and 1982–84")]
        self.assertEqual(toks, ["267,442", "146,482", "4.1", "77"])
        rate = {"value": 3.6, "fmt": "pct100:1"}          # a rate in percent is not a gap in percentage points
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("3.6 percentage points")[0],
                                               *audit_answer.fact_display(rate)))
        gap = {"value": 3.4, "fmt": "pp100:1"}
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("3.4 percentage points")[0],
                                              *audit_answer.fact_display(gap)))
        drag = {"value": -0.12, "fmt": "pp100:2"}          # a table cell must carry the sign of a negative fact
        cell = dict(audit_answer.answer_numbers("| Retail | 0.12 |")[0], table=True)
        self.assertFalse(audit_answer._matches(cell, *audit_answer.fact_display(drag)))
        c = {"value": 52123, "fmt": "num:0"}             # "52 offices" must not match 52,123 read in thousands
        self.assertFalse(audit_answer._matches(audit_answer.answer_numbers("52 offices")[0], *audit_answer.fact_display(c)))
        self.assertTrue(audit_answer._matches(audit_answer.answer_numbers("52.1 thousand")[0], *audit_answer.fact_display(c)))
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published; the survey has 60,000 households.\n")
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(fails(r, "answer"))
        D = copy.deepcopy(GOOD)
        D["audit_allow"] = [{"value": "60,000", "reason": "sample size from the survey documentation"}]
        write(os.path.join(self.out, "decisions.json"), D)
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertEqual(fails(r, "answer"), [])

    def test_answer_carries_disclosures_and_flags(self):
        D = copy.deepcopy(GOOD)
        D["disclosures"] = {"q1": "Not asked (no user): the measure is the official rate."}
        write(os.path.join(self.out, "decisions.json"), D)
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(any("word for word" in m for _, _, m in fails(r, "disclosures")))
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5.\n\n**Not asked (no user):** the measure "
                                                   "is the official rate\n")       # markdown and the final period
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertEqual(fails(r, "disclosures"), [])
        F = copy.deepcopy(FACTS)
        F["slope"]["preliminary"] = True
        write(os.path.join(self.out, "facts.json"), F)
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(any("preliminary" in m for _, _, m in fails(r, "answer")))

    def test_answer_labels_near_numbers_and_table_words(self):
        toks = [n["tok"] for n in audit_answer.answer_numbers("A 9.9-point drop, 3 of 4 quarters, (2) the 10-yr "
                                                               "rate and the 50-states list")]
        self.assertEqual(toks, ["9.9"])
        self.assertEqual([n["num"] for n in audit_answer._table_words("| Senate | four |\n| House | 162 |")], [4.0])
        F = copy.deepcopy(FACTS)
        F["slope"]["preliminary"] = True
        write(os.path.join(self.out, "facts.json"), F)
        # the label only in a later disclosure does not cover the headline number
        write(os.path.join(self.out, "answer.md"), "**Slope**\n\nThe 2024 slope was 0.5, as published.\n\nOther "
                                                   "notes.\n\nThe data are subject to revision.\n")
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(any("preliminary" in m for _, _, m in fails(r, "answer")))
        write(os.path.join(self.out, "answer.md"), "**Slope (preliminary)**\n\nThe 2024 slope was 0.5, as published."
                                                   "\n\nOther notes.\n")
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertFalse(any("preliminary" in m for _, _, m in fails(r, "answer")))
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5.\n")      # no benchmark reported
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(any("benchmark check" in m for _, _, m in fails(r, "answer")))

    def test_answer_round6_rules(self):
        A = audit_answer
        # a statistic in backticks is checked; a label in backticks is not
        self.assertEqual([n["tok"] for n in A.answer_numbers("`31.7 percent` and the `65 or more` band")],
                         ["31.7 percent"])
        # a bare integer is not a percent fact shown with one decimal
        f = {"value": 70.36, "fmt": "pct100:1"}
        self.assertFalse(A._matches(A.answer_numbers("70 quorum calls")[0], *A.fact_display(f)))
        self.assertTrue(A._matches(A.answer_numbers("| 70.4 |")[0], *A.fact_display(f)))
        # a published percent is not a dollar amount, and is never rescaled
        self.assertFalse(A._matches(A.answer_numbers("$4.7 trillion")[0], 4.7, "pubpct"))
        self.assertFalse(A._matches(A.answer_numbers("0.01")[0], 7.40, "pub"))
        self.assertTrue(A._matches(A.answer_numbers("4.7 percent")[0], 4.7, "pubpct"))
        # an unsigned number that can only be a negative fact needs a falling word in its sentence
        F = copy.deepcopy(FACTS)
        F["drop"] = {"value": -4.2, "fmt": "pct100:1", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. Income rose 4.2 percent.\n")
        r, _ = quiet(A.audit, self.out)
        self.assertTrue(any("direction words disagree" in m for _, _, m in fails(r, "answer")))
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. Income fell 4.2 percent.\n")
        r, _ = quiet(A.audit, self.out)
        self.assertFalse(any("direction words disagree" in m for _, _, m in fails(r, "answer")))
        # the direction word sits by the number (before, just after, or "by" a change), not anywhere in the sentence
        for txt, flagged in (("The 2010-2019 average real rate was 4.2 percent; it has since turned lower.", True),
                             ("Pages came to 4.2 percent fewer than in 2017.", False),
                             ("The yield was below the rate in FY2020 (by 3.1 percent) and in FY2021 (by 4.2 "
                              "percent), then above it.", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("direction words disagree" in m for _, _, m in fails(r, "answer")), flagged, txt)

    def test_answer_rows_and_opening(self):
        A = audit_answer
        facts = {"ag": {"value": 93119, "fmt": "dollars:0", "where": {"department": "Agriculture"}},
                 "tr": {"value": 87250, "fmt": "dollars:0", "where": {"department": "Treasury"}},
                 "y26": {"value": 22.4, "fmt": "pct100:1", "where": {"year": "2026"}},
                 "y23": {"value": 20.4, "fmt": "pct100:1", "where": {"year": "2023"}}}
        D = {"questions": [{"id": "scope", "blocking": True, "asked": False}],
             "disclosures": {"scope": "No one was available to ask, so the scope is the literal reading."}}
        table = "| Department | Average |\n|---|---|\n| Agriculture | {a} |\n| Treasury | {t} |\n"
        for a, t, ok in (("$93,119", "$87,250", True), ("$87,250", "$93,119", False)):   # swapped rows fail
            write(os.path.join(self.out, "answer.md"), D["disclosures"]["scope"] + "\n\n" + table.format(a=a, t=t))
            r, _ = A.check_answer(self.out, facts, D)
            self.assertEqual(not any("match facts of other rows" in m for _, ok_, m in r if not ok_), ok, (a, t))
        # a ranked table: the row is named by the cell after the rank
        ranked = "| Rank | Department | Average |\n|---|---|---|\n| 1 | Agriculture | {a} |\n| 2 | Treasury | {t} |\n"
        for a, t, ok in (("$93,119", "$87,250", True), ("$87,250", "$93,119", False)):
            write(os.path.join(self.out, "answer.md"), D["disclosures"]["scope"] + "\n\n" + ranked.format(a=a, t=t))
            r, _ = A.check_answer(self.out, facts, D)
            self.assertEqual(not any("match facts of other rows" in m for _, ok_, m in r if not ok_), ok, (a, t))
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "status code 2 (supervisor), 4 (CSRA supervisor) or 5 (manager); codes 6 and 7; the 15 departments")],
            ["15"])
        # period rows: each column keeps one lag to its rows (a COLA year's row holds the prior year's average);
        # a value moved from another period's row breaks it
        per = {f"g{y}": {"value": v, "fmt": "pct100:1", "where": {"year": str(y)}}
               for y, v in ((2021, 6.1), (2022, 1.9), (2023, 2.4), (2024, 2.0))}
        per.update({f"q{y}": {"value": v, "fmt": "num:3", "where": {"year": str(y)}}
                    for y, v in ((2020, 291.901), (2021, 301.236), (2022, 308.729), (2023, 317.265))})
        tbl = "| Year | Growth | Prior-year average |\n|---|---|---|\n" + "".join(
            f"| {y} | {g} percent | {q} |\n" for y, g, q in ((2021, "{a}", "291.901"), (2022, "1.9", "301.236"),
                                                            (2023, "2.4", "308.729"), (2024, "2.0", "317.265")))
        for a, ok in (("6.1", True), ("1.9", False)):
            write(os.path.join(self.out, "answer.md"), D["disclosures"]["scope"] + "\n\n" + tbl.replace("{a}", a))
            r, _ = A.check_answer(self.out, per, D)
            self.assertEqual(not any("match facts of other rows" in m for _, ok_, m in r if not ok_), ok, a)
        # a period row is not judged by its label alone: a COLA year's row holds the prior year's data
        write(os.path.join(self.out, "answer.md"), D["disclosures"]["scope"] + "\n\n| Year | Share |\n|---|---|\n"
                                                   "| 2026 | 20.4 percent |\n")
        r, _ = A.check_answer(self.out, facts, D)
        self.assertFalse(any("match facts of other rows" in m for _, ok_, m in r if not ok_))
        # a blocking question decided without the user: its disclosure opens the answer
        write(os.path.join(self.out, "answer.md"), "The average was $93,119.\n\nMore text.\n\nAnother part.\n\n"
                                                   + D["disclosures"]["scope"] + "\n")
        r, _ = A.check_answer(self.out, facts, D)
        self.assertTrue(any("opening" in m for c, ok_, m in r if c == "disclosures" and not ok_))

    def test_answer_round7_8_rules(self):
        A = audit_answer
        toks = [n["tok"] for n in A.answer_numbers(
            "Statements of January 17 and 21, 2025; 2 U.S.C. 653; 40 CFR 60.5; Montgomery (24031); usa:20; SuDoc "
            "Y 4.J 89/2:; budget function 900; Somerset's 10.7 percent; U.S. 4.1 percent; S. 5 passed; "
            "standard error 9.9 percentage points; HTTP 403; On March 3, 12 states")]
        self.assertEqual(toks, ["10.7 percent", "4.1 percent", "9.9 percentage points", "12"])
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "| 0301 | Misc | 24,530 |\n\nSeries 0343 and code 1802; `2210` but `22.3%`")], ["24,530", "22.3%"])
        self.assertEqual(A._allow_key("90 percent"), A._allow_key("90-percent"))
        self.assertNotEqual(A._allow_key("90"), A._allow_key("90 percent"))
        F = copy.deepcopy(FACTS)
        F["va"] = {"value": -25600, "fmt": "num:0", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. Virginia rose 25,600 jobs, "
                                                   "while the declines elsewhere were small.\n")
        r, _ = quiet(A.audit, self.out)
        self.assertTrue(any("direction words disagree" in m for _, _, m in fails(r, "answer")))
        # a rise written as a fall; a decline stored as a positive size (its label says so) stays fine
        F["va"] = {"value": 0.8, "fmt": "pp100:1", "derivation": "x", "label": "HHS change, pp"}
        F["drop_size"] = {"value": 130326, "fmt": "num:0", "derivation": "x", "label": "Decline in accessions"}
        write(os.path.join(self.out, "facts.json"), F)
        for txt, flagged in (("The share is down 0.8 percentage points.", True),
                             ("The share rose 0.8 percentage points.", False),
                             ("Accessions fell 130,326.", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("direction words disagree" in m for _, _, m in fails(r, "answer")), flagged, txt)
        # a number that is only a published benchmark figure needs its publisher named
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              "measure,period,computed,published,source,url,accessed,match,note\n"
              "slope,2024,0.5,0.5,BLS,https://x.gov,2026-10-21,yes,\nmedian,2025,769,770,BLS,https://x.gov,2026-10-21,"
              "no,rounded public file\n")
        for txt, ok in (("The median was $770.", False), ("BLS publishes $770.", True),
                        ("IPUMS gives $770.", False)):           # naming the estimate's own source is no credit
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("match only a published figure" in m for _, _, m in fails(r, "answer")), not ok, txt)
        # decisions.json in a Windows code page is read (with a note), not a crash
        with open(os.path.join(self.out, "decisions.json"), "wb") as f:
            f.write(json.dumps(GOOD).replace("}", ', "note_x": "caf\\u00e9"}', 1).encode("utf-8")
                    .decode("unicode_escape").encode("cp1252"))
        self.assertIsInstance(MA.load(self.out), dict)

    def test_answer_round20_rules(self):
        A = audit_answer
        F = copy.deepcopy(FACTS)
        F["chg"] = {"value": -0.011, "fmt": "signed:pp:1", "derivation": "x", "label": "change, less than HS"}
        write(os.path.join(self.out, "facts.json"), F)
        bad = lambda: [m for _, _, m in fails(quiet(A.audit, self.out)[0], "answer")]      # noqa: E731
        # an explicit plus sign cannot show a fall, even with "lower" nearby
        for txt, ok in (("The rate is lower than in 2019 (+1.1 percentage points).", False),
                        ("The rate is lower than in 2019 (-1.1 percentage points).", True)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            self.assertEqual(any("1.1" in m for m in bad()), not ok, txt)
        # audit_allow entries may carry a unit word ("60 days"); a list of resolution numbers is identifiers
        D = copy.deepcopy(GOOD)
        D["audit_allow"] = [{"value": "60 days", "reason": "the window, a choice"}]
        write(os.path.join(self.out, "decisions.json"), D)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. Laws signed 60 days "
                                                   "before; S.Res. 377, 412 and 532 passed.\n")
        self.assertEqual(bad(), [])
        # a number dated "in <Month> <year>" must be a fact of that year
        f25 = {"value": 53.6, "where": {"snapshot": "September 2025"}}
        self.assertTrue(A._wrong_year(" percent in September 2024 ", [f25]))
        self.assertFalse(A._wrong_year(" percent in Sept. 2025", [f25]))
        self.assertTrue(A._wrong_year(" percent in 2026", [{"where": {"period": 202508}}]))     # OPM's yyyymm
        self.assertFalse(A._wrong_year(" percent in 2025", [{"where": {"period": "202508"}}]))
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "Employees under 30 dropped; workers younger than 30 fell 19.5 percent; under 30 percent")],
            ["19.5 percent", "30 percent"])
        # a benchmark row matched within the publisher's stated rounding needs no explanation in the answer
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              "measure,period,computed,published,source,url,accessed,match,note\n"
              "slope,2024,0.5,0.5,BLS,https://x.gov,2026-10-21,yes,\n"
              "households,2025,137056,137100,Census,https://x.gov,2026-10-21,yes,Census rounds to 4 significant digits\n")
        F["hh"] = {"value": 137056, "fmt": "num:0", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, matching the published figure.\n")
        self.assertFalse(any("households" in m for m in bad()))

    def test_answer_round21_rules(self):
        A = audit_answer
        # in a table keyed by years, a cell that matches only an undated fact (a ratio) fits no row
        facts = {f"s{y}": {"value": v, "fmt": "num:2", "where": {"year": str(y)}}
                 for y, v in ((2014, 1.62), (2015, 1.71), (2016, 1.66), (2017, 1.69))}
        facts["ratio"] = {"value": 1.17, "fmt": "num:2", "derivation": "x"}
        shown = [(k, A.fact_display(f), f) for k, f in facts.items()]
        table = "| Year | Spread |\n|---|---|\n| 2014 | 1.62 |\n| 2015 | {} |\n| 2016 | 1.66 |\n| 2017 | 1.69 |\n"
        self.assertEqual(A._period_misfits(table.format("1.17"), shown, facts), ["1.17"])
        self.assertEqual(A._period_misfits(table.format("1.71"), shown, facts), [])
        # a benchmark figure moved into another row of a table is not excused by being published
        F = copy.deepcopy(FACTS)
        F.update({"re": {"value": 0.54, "fmt": "num:2", "where": {"industry": "Real estate"}},
                  "rt": {"value": -0.12, "fmt": "num:2", "where": {"industry": "Retail trade"}},
                  "fed": {"value": -0.11, "fmt": "num:2", "where": {"industry": "Federal"}}})
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              "measure,period,computed,published,source,url,accessed,match,note\n"
              "slope,2024,0.5,0.5,BLS,https://x.gov,2026-10-21,yes,\n"
              "Federal contribution,2025,-0.11,-0.11,BEA Real GDP by Industry,https://x.gov/?id=1,2026-10-22,yes,\n")
        for cell, ok in (("-0.12", True), ("-0.11", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, matching BEA's published figure.\n\n"
                  "| Industry | Contribution to real GDP growth (BEA) |\n|---|---|\n| Real estate | 0.54 |\n"
                  f"| Retail trade | {cell} |\n| Federal | -0.11 |\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("other rows only" in m for _, _, m in fails(r, "answer")), not ok, cell)
        # a published figure in basis points counts as given ("170 basis points")
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              "measure,period,computed,published,source,url,accessed,match,note\n"
              "slope,2024,0.5,0.5,BLS,https://x.gov,2026-10-21,yes,\n"
              "spread (bp),2010-2019,169,170,MBA,https://x.gov,2026-10-22,no,MBA averages 1990-2021\n")
        F["sp"] = {"value": 0.0169, "fmt": "bp100:0", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. The spread averaged 169 "
              "basis points; MBA reports roughly 170 basis points for 1990-2021, a longer period.\n")
        r, _ = quiet(A.audit, self.out)
        self.assertFalse(any("must give the published figure" in m for _, _, m in fails(r, "answer")))
        # a fall stated by a comparison after the number reads right even after a rise word
        F2 = copy.deepcopy(FACTS)
        F2["gap"] = {"value": -0.034, "fmt": "pp:1", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F2)
        for txt, ok in (("Federal pay grew 3.4 percentage points less than private wages.", True),
                        ("Federal pay grew 3.4 percentage points faster.", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("direction words disagree" in m for _, _, m in fails(r, "answer")), not ok, txt)
        write(os.path.join(self.out, "facts.json"), F)
        # an odd duration is a statistic; a standard term or window is a name
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "a 43-day shutdown; the 10-year note, a 4-week bill and the 30-day average")], ["43"])
        # Congressional Record identifiers and a day range are not statistics
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "On February 12 to 13, 2024, issues 204 to 206 and issue 45 of volume 171; 219 issues")], ["219"])
        # prose: a number right after a name the facts are keyed by is that name's figure
        F.update({"co": {"value": 19.8, "fmt": "num:1", "where": {"state": "Colorado"}},
                  "or_": {"value": 16.9, "fmt": "num:1", "where": {"state": "Oregon"}},
                  "tot": {"value": 18.1, "fmt": "num:1", "derivation": "x"}})
        write(os.path.join(self.out, "facts.json"), F)
        for txt, ok in (("Colorado (19.8) and Oregon (16.9) lead.", True),
                        ("Colorado (16.9) and Oregon (19.8) lead.", False),
                        ("Colorado and Oregon together reach 18.1.", True),       # a total of a list
                        ("Colorado and Oregon: 19.8 and 16.9.", True),             # a list, then its numbers
                        ("Spending was 16.9 on Oregon and 19.8 on Colorado.", True),   # the name follows its number
                        ("Colorado (16.9), Oregon (19.8).", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("other entries of the same kind" in m for _, _, m in fails(r, "answer")), not ok, txt)

    def test_answer_round22_rules(self):
        A = audit_answer
        # quarters are periods of their own, December is not next year, a fiscal year and month are one month
        self.assertNotEqual(A._period_key("2025 Q1"), A._period_key("2025 Q3"))
        self.assertEqual(A._period_key("Q3 2025"), A._period_key("2025-07"))
        self.assertNotEqual(A._period_key("Dec 2025"), A._period_key("2026"))
        self.assertTrue(A._wrong_year(" percent in 2026", [{"where": {"month": "Dec 2025"}}]))
        self.assertEqual(A._fact_periods({"where": {"fiscal_year": "2026", "month": "12"}}), [A._period_key("2025-12")])
        self.assertEqual(A._fact_periods({"where": {"fiscal_year": "2026", "month": "2026-02"}}), [A._period_key("2026-02")])
        facts = {f"q{i}": {"value": v, "fmt": "num:1", "where": {"quarter": f"2025 Q{i}"}}
                 for i, v in ((1, -1.8), (2, 4.2), (3, 5.2), (4, 1.6))}
        shown = [(k, A.fact_display(f), f) for k, f in facts.items()]
        table = "| Quarter | Growth |\n|---|---|\n| 2025 Q1 | -1.8 |\n| 2025 Q2 | 4.2 |\n| 2025 Q3 | {} |\n| 2025 Q4 | 1.6 |\n"
        self.assertEqual(A._period_misfits(table.format("4.2"), shown, facts), ["4.2"])
        self.assertEqual(A._period_misfits(table.format("5.2"), shown, facts), [])
        # a direction word against its own from-to numbers
        self.assertTrue(A._from_to_misreads("The balance fell from $890.8 billion to $984.0 billion."))
        self.assertFalse(A._from_to_misreads("The balance rose from $890.8 billion to $984.0 billion."))
        self.assertFalse(A._from_to_misreads("Spending rose from 2019 to 2024 by 3 percent."))
        self.assertFalse(A._from_to_misreads("The rate fell from 5.4 percent in 2019 to 4.3 percent."))
        # a year just before the number dates it too, unless a comparison word stands between
        f26 = [{"where": {"year": "2026"}}]
        self.assertTrue(A._wrong_year(A._year_before("for 2025 (men "), f26))
        self.assertFalse(A._wrong_year(A._year_before("rose from 13.1 percent in 2019 to "), f26))
        self.assertFalse(A._wrong_year(A._year_before("146 issues in 2025 (", " in 2024) had both"), f26))
        self.assertFalse(A._wrong_year(A._year_before("$1,177.1 billion for 2025, $"), f26))
        self.assertFalse(A._wrong_year(A._year_before("from $885.5 billion in 2024: a goods deficit of $"), f26))
        # a name's short form pairs too ("Natural Resources" for "Natural Resources Committee")
        F = copy.deepcopy(FACTS)
        F.update({"nr": {"value": 66, "fmt": "num:0", "where": {"committee": "Natural Resources Committee"}},
                  "ec": {"value": 63, "fmt": "num:0", "where": {"committee": "Energy and Commerce Committee"}}})
        write(os.path.join(self.out, "facts.json"), F)
        for txt, ok in (("Natural Resources (66) and Energy and Commerce (63) came next.", True),
                        ("Natural Resources (63) and Energy and Commerce (63) came next.", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("other entries of the same kind" in m for _, _, m in fails(r, "answer")), not ok, txt)
        # a dropped connection is throttling, not a dead link; an HTTP error still fails
        import urllib.request
        import audit_report as AR
        from unittest import mock
        with mock.patch.object(urllib.request, "urlopen", side_effect=ConnectionResetError(10054, "reset by peer")):
            self.assertTrue(AR.check_url("https://fred.stlouisfed.org/series/X")[0])
        err = urllib.error.HTTPError("https://x.gov/a", 404, "Not Found", {}, None)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            self.assertFalse(AR.check_url("https://x.gov/a")[0])

    def test_answer_round23_rules(self):
        A = audit_answer
        # a month in the text must match month-level facts; "percent of GDP in 2019" is dated too
        f = [{"where": {"snapshot": "September 2024"}}]
        self.assertTrue(A._wrong_year(" percent in October 2024", f))
        self.assertFalse(A._wrong_year(" percent in September 2024", f))
        self.assertTrue(A._wrong_year(" percent of GDP in 2019", [{"where": {"year": "2025"}}]))
        # a wide table's column carries the year
        self.assertEqual(A._fact_periods({"where": {"component": "Defense"}, "column": "share_2019"}), [201900])
        # ordinal rows are row labels; occupation series codes are identifiers
        self.assertEqual(A._row_label(A.answer_numbers("| 117th | 362 |")[0] | {"line": "| 117th | 362 |"}), "117th")
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "IT management (series 2210) and series 2210 grew 12 percent")], ["12 percent"])
        # a name right after a number is that number's ("616 Senate, 254 House, 85.3 percent of all")
        ents = {"house": ({"chamber"}, "house"), "senate": ({"chamber"}, "senate")}
        n = A.answer_numbers("616 Senate, 254 House, 85.3 percent of all roll calls")[2]
        self.assertIsNone(A._misnamed(["616 Senate, 254 House, 85.3 percent of all roll calls"], n,
                                      [{"where": {"chamber": "Both"}}], ents))
        # a sentence does not end at "U.S."
        self.assertEqual(len(A._sentences("The U.S. city average (3.7 percent) rose. Houston fell.")), 2)
        # a benchmark row with an empty published cell
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              "measure,period,computed,published,source,url,accessed,match,note\n"
              "slope,2024,0.5,,BLS,https://x.gov,2026-10-21,not checked,\n")
        self.assertTrue(any("published column is empty" in m for _, _, m in fails(MA.check_benchmark(self.out))))
        # a published figure in a table's own "published" column is that row's, named by its lead word
        F = copy.deepcopy(FACTS)
        F.update({"la": {"value": 8.0, "fmt": "num:1", "where": {"metro": "Los Angeles-Long Beach-Anaheim"}},
                  "mia": {"value": 7.9, "fmt": "num:1", "where": {"metro": "Miami-Fort Lauderdale-West Palm Beach"}}})
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "exhibits", "benchmark.csv"),
              "measure,period,computed,published,source,url,accessed,match,note\n"
              "slope,2024,0.5,0.5,BLS,https://x.gov,2026-10-21,yes,\n"
              "No-vehicle share Miami,2024,7.9,8.0,Census DP04,https://x.gov,2026-10-22,no,sampling error\n")
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published.\n\n"
              "| Metro | Computed | Census DP04 published |\n|---|---|---|\n"
              "| Los Angeles-Long Beach-Anaheim | 8.0 | 8.0 |\n| Miami-Fort Lauderdale-West Palm Beach | 7.9 | 8.0 |\n")
        r, _ = quiet(A.audit, self.out)
        self.assertFalse(any("other rows only" in m for _, _, m in fails(r, "answer")))

    def test_answer_round24_rules(self):
        A = audit_answer
        # a fall registered with fmt ...:abs prints unsigned, but a rise word next to it is still wrong
        F = copy.deepcopy(FACTS)
        F["cit"] = {"value": -70.675, "fmt": "usd_b_scaled:1:abs", "derivation": "x", "label": "corporate change"}
        write(os.path.join(self.out, "facts.json"), F)
        for txt, ok in (("Corporate receipts fell $70.7 billion.", True),
                        ("Corporate receipts were down $70.7 billion.", True),
                        ("Corporate receipts were up $70.7 billion.", False)):
            write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. " + txt + "\n")
            r, _ = quiet(A.audit, self.out)
            self.assertEqual(any("direction words disagree" in m for _, _, m in fails(r, "answer")), not ok, txt)
        # "in the March 2019 ASEC" is dated; "in October 2023–August 2024" is a range; FY columns are not years
        f20 = [{"where": {"year": "2020"}}]
        self.assertTrue(A._wrong_year(" percent in the March 2019 ASEC", f20))
        self.assertFalse(A._wrong_year(" in October 2023–August 2024", [{"where": {"year": "2024"}}]))
        self.assertEqual(A._fact_periods({"where": {"type": "Retirement"}, "column": "FY2024_oct_aug"}), [])
        # a row label with a note in parentheses is still bound to its facts
        facts = {"ret": {"value": 76830, "fmt": "num:0", "where": {"type": "Retirement"}},
                 "quit": {"value": 73491, "fmt": "num:0", "where": {"type": "Quit"}}}
        n = A.answer_numbers("| Retirement (voluntary, early out) | 73,491 |")[0]
        self.assertFalse(A._row_ok(n, [facts["quit"]], facts))
        # the ages that define a group are not statistics; "all 79 succeeded" is
        self.assertEqual([n["tok"] for n in A.answer_numbers(
            "Mothers whose youngest child is 6 to 17; a child under 18 BLS reports; all 79 succeeded; all 50 states")],
            ["79"])

    def test_answer_round25_rules(self):
        A = audit_answer
        # two-row then-vs-now tables are checked; a period label with a note is still a period
        facts = {"a": {"value": 2.51, "fmt": "num:2", "where": {"fiscal_year": "FY2026"}},
                 "b": {"value": 2.56, "fmt": "num:2", "where": {"fiscal_year": "FY2025"}}}
        shown = [(k, A.fact_display(f), f) for k, f in facts.items()]
        t = "| Year | Bid-to-cover |\n|---|---|\n| FY2025 | 2.56 |\n| FY2026 (preliminary) | {} |\n"
        self.assertEqual(A._period_misfits(t.format("2.56"), shown, facts), ["2.56"])
        self.assertEqual(A._period_misfits(t.format("2.51"), shown, facts), [])
        # a sibling row's fact is not this row's ("Nominal (NSA)" vs "Nominal (SA)"); "n/a" is no row name;
        # a bill number names its row
        facts = {"sa": {"value": 55.8, "fmt": "num:1", "where": {"series": "Nominal (SA)"}},
                 "nsa": {"value": 59.0, "fmt": "num:1", "where": {"series": "Nominal (NSA)"}}}
        n = A.answer_numbers("| Nominal (NSA) | 55.8 |")[0]
        self.assertFalse(A._row_ok(n, [facts["sa"]], facts))
        self.assertEqual(A._row_label(A.answer_numbers("| Appendix | n/a | 1,340 |")[0]), "appendix")
        self.assertEqual(A._row_label(A.answer_numbers("| H.R. 131 | 248 | 177 |")[0]), "h.r. 131")
        # every place the number stands in a sentence is paired with its name; "improved" is a rise
        ents = {"labor": ({"department"}, "labor"), "energy": ({"department"}, "energy")}
        s = "Labor is first at 16.3 years on average, with Energy (16.3) next."
        n = [x for x in A.answer_numbers(s) if x["tok"] == "16.3"][0]
        self.assertEqual(A._misnamed([s], n, [{"where": {"department": "Labor"}}], ents), "energy")
        self.assertTrue(A.UP_WORDS.search("the position improved"))
        self.assertEqual([n["tok"] for n in A.answer_numbers("BUDGET-2026-PER returns 404; it returned 503 rows")],
                         ["503"])
        # a range of vote numbers is not a tally; a fact's period label may carry a note
        self.assertEqual([n["tok"] for n in A.answer_numbers("2025 equals the session's votes 1-659 in all")], [])
        self.assertEqual(A._fact_periods({"where": {"year": "2026 (to Oct 15)"}}), [202600])
        # a size without a direction needs no fall word
        F = copy.deepcopy(FACTS)
        F["btc"] = {"value": -0.01, "fmt": "num:2", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. Bid-to-cover is flat "
                                                   "(within 0.01).\n")
        r, _ = quiet(A.audit, self.out)
        self.assertFalse(any("direction words disagree" in m for _, _, m in fails(r, "answer")))

    def test_answer_round26_rules(self):
        A = audit_answer
        # "X, down from Y" is checked against its own numbers; a longer "of ..." phrase still dates the number
        self.assertTrue(A._from_to_misreads("The share was 18.3 percent in 2025, down from 16.8 percent in 2019."))
        self.assertFalse(A._from_to_misreads("Unemployment was 4.2 percent, down from 4.3 percent."))
        self.assertTrue(A._wrong_year(" percent of U.S. personal income in 2024", [{"where": {"year": "2025"}}]))
        # a name after a comma belongs to the next number ("Native Americans with 9, Energy with 9")
        ents = {"native americans": ({"area"}, "native americans"), "energy": ({"area"}, "energy")}
        s = "Then Native Americans with 9, Energy with 9."
        self.assertEqual(A._misnamed([s], A.answer_numbers(s)[0], [{"where": {"area": "Energy"}}], ents),
                         "native americans")
        # a header with its period inside ("Self-employed, 2026 (%)") dates its column; age spans are not statistics
        facts = {f"{g}_{y}": {"value": v, "fmt": "num:1", "where": {"group": g}, "column": f"share_{y}"}
                 for y, rows in ((2019, (("65+", 22.6), ("25-64", 9.8), ("16+", 10.2))),
                                 (2026, (("65+", 21.5), ("25-64", 10.0), ("16+", 10.6)))) for g, v in rows}
        shown = [(k, A.fact_display(f), f) for k, f in facts.items()]
        t = ("| Group | Self-employed, 2019 (%) | Self-employed, 2026 (%) |\n|---|---|---|\n"
             "| 65 and over | 22.6 | {} |\n| 25 to 64 | 9.8 | 10.0 |\n| 16 and over | 10.2 | 10.6 |\n")
        self.assertEqual(A._period_misfits(t.format("22.6"), shown, facts), ["22.6"])
        self.assertEqual(A._period_misfits(t.format("21.5"), shown, facts), [])
        self.assertEqual([n["tok"] for n in A.answer_numbers("5.7 percent at 25 to 64; the 25-to-64 share")],
                         ["5.7 percent"])
        # citations use the user's calendar date, not UTC's
        from core import policy
        self.assertEqual(policy.local_day("2026-10-22"), "2026-10-22")
        from datetime import datetime, timezone
        stamp = datetime(2026, 10, 23, 0, 2, 50, tzinfo=timezone.utc)
        self.assertEqual(policy.local_day(stamp.isoformat()), stamp.astimezone().date().isoformat())

    def test_answer_round27_rules(self):
        A = audit_answer
        ents = {"louisiana": ({"state"}, "louisiana"), "south carolina": ({"state"}, "south carolina"),
                "montana": ({"state"}, "montana"), "virginia": ({"state"}, "virginia"),
                "bills": ({"security"}, "bills"), "bonds": ({"security"}, "bonds")}
        # "(+2.3 percent)" pairs with its name; every occurrence is judged; lowercase names next to their number
        s = "South Carolina (+2.3 percent) and Louisiana (+2.3 percent) led."
        n = A.answer_numbers(s)[0]
        self.assertEqual(A._misnamed([s], n, [{"where": {"state": "South Carolina"}}], ents), "louisiana")
        s = "The mix: bills 17.4 percent, bonds 22.4 percent."
        n = A.answer_numbers(s)[0]
        self.assertEqual(A._misnamed([s], n, [{"where": {"security": "Bonds"}}], ents), "bills")
        # an explicit plus never shows a negative published figure
        n = A.answer_numbers("BEA publishes +1.1 percentage points")[0]
        self.assertFalse(A._matches(n, -1.1, "pubpp", 1))
        # "U.S." stands for the United States; a code in parentheses before the value keeps the pairing
        ents2 = {"united states": ({"geo"}, "united states"), "u.s.": ({"geo"}, "united states"),
                 "florida": ({"geo"}, "florida"), "food inspection": ({"series"}, "food inspection"),
                 "compliance inspection": ({"series"}, "compliance inspection")}
        s = "About 4.26 percent of U.S. households."
        self.assertIn("u.s.", A._forms("United States"))
        s = "Food Inspection (1863, down 47.5 percent) led."
        n = A.answer_numbers(s)[0]
        self.assertEqual(A._misnamed([s], n, [{"where": {"series": "Compliance Inspection"}}], ents2),
                         "food inspection")
        # a list of roll-call numbers is identifiers; a tally still counts
        self.assertEqual([x["tok"] for x in A.answer_numbers("Senate roll call votes 225 and 600; it passed 51-47")],
                         ["51", "47"])
        # "32,089-page" is one number, not "32"
        self.assertEqual([x["tok"] for x in A.answer_numbers("The 33,999-page figure")], ["33,999"])
        # one fall word carries a list
        F = copy.deepcopy(FACTS)
        F.update({"n": {"value": -0.086, "fmt": "pp:1", "derivation": "x"},
                  "t": {"value": -0.021, "fmt": "pp:1", "derivation": "x"},
                  "f": {"value": -0.004, "fmt": "pp:1", "derivation": "x"}})
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "The 2024 slope was 0.5, as published. Notes fell 8.6 percentage "
              "points, TIPS 2.1 percentage points and FRNs 0.4 percentage points.\n")
        r, _ = quiet(A.audit, self.out)
        self.assertEqual(fails(r, "answer"), [])                     # found, and the shared "fell" carries all three
        # but a rise comparison right after the number, or a fall word in an earlier bold sentence, does not pass
        F["m"] = {"value": -0.002, "fmt": "pp:1", "derivation": "x"}
        write(os.path.join(self.out, "facts.json"), F)
        write(os.path.join(self.out, "answer.md"), "**Men are slightly below.** The 2024 slope was 0.5, as published, "
              "and men were 0.2 percentage points above their 2019 average.\n")
        r, _ = quiet(A.audit, self.out)
        self.assertTrue(any("direction words disagree" in m for _, _, m in fails(r, "answer")))

    def test_fails_without_provenance(self):
        os.remove(os.path.join(self.out, "data", "bls", "request.json"))
        r, _ = quiet(audit_answer.audit, self.out)
        self.assertTrue(fails(r, "vintage"))


if __name__ == "__main__":
    unittest.main()

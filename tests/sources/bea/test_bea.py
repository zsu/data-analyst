"""Offline tests for the bea deep pack.

Fixtures (fixtures, index.json):
  bulk_*.txt, bulk_SAGDP.zip  BEA's real bulk files (downloaded 2026-10-19/20), trimmed to a few series, areas
                              and years: same columns and rows as published (a one-off trimming script, not part of the pack).
  doc_*.json                  no BEA key was available, so the API replies are BEA's documented examples (user
                              guide, April 2026: the GetDataSetList dataset names; the NIPA GetData sample row for
                              T20305 2015Q1; the guide's example UserID 11111111-2222-3333-4444-555555555555,
                              which BEA echoes in Request). Not live responses.
Regression checks (test_sNN_*); 15-18 test the shared report tools (scripts/report) and skip when a
function is not there.
"""
import importlib.util
import io
import json
import math
import os
import sys
import tempfile
import unittest
import warnings
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.abspath(os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "sources", "bea"))
SKILL = os.path.dirname(os.path.dirname(PACK))
SCRIPTS = os.path.join(SKILL, "scripts")
for p in (SCRIPTS, PACK):
    if p not in sys.path:
        sys.path.insert(0, p)
from testkit import PackTest  # noqa: E402

warnings.simplefilter("ignore")
FX = os.path.join(HERE, "fixtures")
DOC_KEY = "11111111-2222-3333-4444-555555555555"      # the user guide's example key, not a real one


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fetch = load("bea_pack_fetch", os.path.join(PACK, "fetch.py"))
stats = load("bea_pack_stats", os.path.join(PACK, "lib", "stats.py"))
factfmt = load("bea_pack_factfmt", os.path.join(PACK, "..", "..", "scripts", "analysis", "factfmt.py"))
import bea_api  # noqa: E402

pd = stats.pd
FILES = {"SeriesRegister.txt": os.path.join(FX, "bulk_SeriesRegister.txt"),
         "NipaDataA.txt": os.path.join(FX, "bulk_NipaDataA.txt"),
         "NipaDataQ.txt": os.path.join(FX, "bulk_NipaDataQ.txt"),
         "TablesRegister.txt": os.path.join(FX, "bulk_TablesRegister.txt"),
         "SAGDP.zip": os.path.join(FX, "bulk_SAGDP.zip")}
SPEC = {"description": "Real GDP, percent change and state real GDP",
        "requests": [{"id": "pct", "dataset": "NIPA", "table": "T10101", "line_code": 1, "frequency": "Q",
                      "years": "2025-2026"},
                     {"id": "st", "dataset": "Regional", "table": "SAGDP9", "line_code": 1,
                      "geo_fips": ["06000", "48000"], "years": "2024-2025"}]}
API_SPEC = {"description": "PCE by type of product (documented example)",
            "requests": [{"id": "pce", "dataset": "NIPA", "table": "T20305", "frequency": "Q", "years": "2015"}]}
ITA_SPEC = {"description": "trade", "requests": [{"id": "ita", "dataset": "ITA",
                                                  "params": {"Indicator": "BalGds", "AreaOrCountry": "China"}}]}


def nipa_req(table, years, freq="A"):
    return {"id": "t", "dataset": "NIPA", "table": table, "frequency": freq, "years": years,
            "line_codes": [], "geo": None}


def regional_req(geo, years="2024"):
    return fetch.normalize_requests({"requests": [{"id": "s", "dataset": "Regional", "table": "SAGDP9",
                                                   "line_code": 1, "geo_fips": geo, "years": years}]})[0]


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def report_module(name):
    p = os.path.join(SCRIPTS, "report", f"{name}.py")
    if not os.path.exists(p):
        raise unittest.SkipTest(f"scripts/report/{name}.py not present")
    for d in (os.path.join(SCRIPTS, "report"), os.path.join(SCRIPTS, "analysis")):
        if d not in sys.path:
            sys.path.append(d)
    return load(f"bea_test_{name}", p)


class TestBeaCli(PackTest):
    source = "bea"
    sample_spec = SPEC

    def test_check_bulk_without_key(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "bulk", r)
        self.assertEqual(r["source"], "bea")
        self.assertIn("scripts", r["next_command"])
        self.assertIn("--source bea", r["next_command"])
        self.assertIn("--prefer bulk", r["next_command"])

    def test_fetch_bulk(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "bulk")
        rows = self.csv(out, "bea_data")
        self.assertEqual(list(rows[0])[:4], ["request_id", "dataset", "table", "line"])
        q = {(x["request_id"], x["time_period"]): x for x in rows}
        self.assertEqual(q[("pct", "2025Q1")]["series_code"], "A191RL")
        self.assertEqual(q[("pct", "2025Q1")]["value"], "0.1")
        st = {(x["geo_fips"], x["time_period"]): x for x in rows if x["request_id"] == "st"}
        self.assertEqual(st[("06000", "2024")]["value"], "3312182.2")
        self.assertEqual(st[("06000", "2024")]["unit"], "Millions of chained 2017 dollars")
        self.assertEqual(st[("06000", "2024")]["unit_mult"], "6")
        d = os.path.join(out, "data", "bea")
        for f in ("README.md", "notes.csv", "request.json"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        req = json.load(open(os.path.join(d, "request.json"), encoding="utf-8"))
        self.assertEqual(req["route"], "bulk")
        self.assertEqual(req["source"], "bea")

    def test_reuse_existing(self):
        code, r, out = self.fetch(SPEC)
        code, r = self.cli(os.path.join(out, "spec.json"), "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing", r)

    def test_api_needs_key_local_is_run_setup(self):
        code, r = self.check(ITA_SPEC, env={"DATA_ANALYST_ENV": "local"})
        self.assertEqual(r["route"], "run_setup", r)
        self.assertIn("-Source bea", r["run_command"]) if sys.platform == "win32" else self.assertIn("bea", r["run_command"])

    def test_api_needs_key_sandbox_is_manual(self):
        sp, out = self.write_spec(ITA_SPEC)
        code, r = self.cli(sp, "--out", out, "--check", "--env", "sandbox")
        self.assertEqual(r["route"], "manual_download", r)
        self.assertTrue(r["manual_bundle"].replace("\\", "/").endswith("manual_download/bea"), r["manual_bundle"])
        for f in ("download_bea.sh", "download_bea.ps1", "INSTRUCTIONS.md", "requests.json", "spec.json"):
            self.assertTrue(os.path.exists(os.path.join(r["manual_bundle"], f)), f)
        text = open(os.path.join(r["manual_bundle"], "INSTRUCTIONS.md"), encoding="utf-8").read()
        self.assertIn("bash ~/Downloads/download_bea.sh", text)          # one command per system
        self.assertIn(r'-File "$HOME\Downloads\download_bea.ps1"', text)
        self.assertNotIn("python", text.lower())                         # no analyst command for the user
        self.assertNotIn("paste-your-key-here", text)                    # curl steps ask with hidden input too
        self.assertEqual(r["user_commands"]["bash"], "bash ~/Downloads/download_bea.sh")
        self.assertTrue(r["request_spec"].endswith("spec.json"))
        self.assertIn("--source bea", r["next_command"])

    def test_api_route_with_doc_fixture(self):
        env = {"BEA_API_KEY": DOC_KEY}
        code, r = self.check(API_SPEC, env=env)
        self.assertEqual(r["route"], "api", r)
        code, r, out = self.fetch(API_SPEC, env=env)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        rows = self.csv(out, "bea_data")
        self.assertEqual(rows[0]["series_code"], "DPCERC")
        self.assertEqual(rows[0]["value"], "12119763.0")         # "12,119,763" with the commas removed
        self.assertEqual(rows[0]["unit_mult"], "6")
        d = os.path.join(out, "data", "bea")
        for root, _, fs in os.walk(d):
            for f in fs:
                self.assertNotIn(DOC_KEY, open(os.path.join(root, f), encoding="utf-8", errors="replace").read(), f)
        self.assertIn("not endorsed or certified", open(os.path.join(d, "README.md"), encoding="utf-8").read())

    def test_dataset_rules_are_fix_spec(self):
        bad = {"requests": [{"id": "x", "dataset": "IntlServTrade",
                             "params": {"TypeOfService": "Telecom,Travel", "AreaOrCountry": "All"}}]}
        code, r = self.check(bad)
        self.assertEqual(code, 2, r)
        self.assertEqual(r["route"], "fix_spec", r)
        bad = {"requests": [{"id": "m", "dataset": "MNE", "params": {"Year": "2023"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("DirectionOfInvestment", r["message"])

    def test_endpoints_and_dataset_info_offline(self):
        code, r = self.cli("--endpoints")
        self.assertEqual(code, 0, r)
        self.assertIn("ITA", r["datasets"])
        code, r = self.cli("--dataset-info", "mne")
        self.assertEqual(r["dataset"], "MNE")

    def test_ingest_bulk_files(self):
        sp, out = self.write_spec(SPEC)
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        import shutil
        for name, src in FILES.items():
            shutil.copy(src, os.path.join(up, name))
        code, r = self.cli(sp, "--out", out, "--ingest", up, fixtures=False)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")


class TestBeaUnits(unittest.TestCase):
    """Units of the client and the parser (no subprocess)."""

    def test_special_values_go_to_flag(self):
        q = {"id": "r", "dataset": "Regional", "table": "CAINC5N", "frequency": "", "geo": "COUNTY"}
        rows = fetch.tidy_api_rows(q, {"LineCode": "10"}, [
            {"Code": "CAINC5N-10", "GeoFips": "01001", "GeoName": "Autauga, AL", "TimePeriod": "2024",
             "DataValue": "(D)", "CL_UNIT": "Thousands of dollars", "UNIT_MULT": "3"},
            {"Code": "CAINC5N-10", "GeoFips": "01003", "GeoName": "Baldwin, AL *", "TimePeriod": "2024",
             "DataValue": "(NA)"},
            {"Code": "CAINC5N-10", "GeoFips": "01005", "GeoName": "Barbour, AL", "TimePeriod": "2024",
             "DataValue": "(NM)"},
            {"Code": "CAINC5N-10", "GeoFips": "01007", "GeoName": "Bibb, AL", "TimePeriod": "2024",
             "DataValue": "1,337"}])
        self.assertEqual([(r["value"], r["flag"]) for r in rows],
                         [("", "(D)"), ("", "(NA)"), ("", "(NM)"), ("1337.0", "")])
        self.assertEqual(rows[1]["geo_name"], "Baldwin, AL")

    def test_error_shapes_and_auth_codes(self):
        e = bea_api.first_error({"BEAAPI": {"Results": [{"Error": {"APIErrorCode": "40", "APIErrorDescription": "x",
                                                                    "ErrorDetail": [{"Description": "bad"}]}}]}})
        self.assertEqual(bea_api.error_detail(e), "x (bad)")
        self.assertTrue(bea_api.is_auth_error("4", "This UserId is not active."))
        self.assertFalse(bea_api.is_auth_error("40", "missing parameters"))

    def test_dataset_problems(self):
        self.assertEqual(bea_api.dataset_problems("ITA", {"Indicator": "BalGds", "AreaOrCountry": "China"}), [])
        self.assertTrue(bea_api.dataset_problems("ITA", {"Indicator": "BalGds,BalServ", "AreaOrCountry": "All"}))
        self.assertTrue(bea_api.dataset_problems("UnderlyingGDPbyIndustry",
                                                 {"TableID": "210", "Frequency": "Q", "Year": "2024", "Industry": "ALL"}))
        self.assertTrue(bea_api.dataset_problems("Nope", {}))

    def test_byte_budget_waits(self):
        c = bea_api.BeaClient.__new__(bea_api.BeaClient)
        import time
        now = time.monotonic()
        c._bytes = [(now - 59.6, 60_000_000), (now - 1, 30_000_000)]
        slept = []
        orig = time.sleep
        bea_api.time.sleep = lambda s: (slept.append(s), c._bytes.pop(0))
        try:
            c._wait_for_bytes()
        finally:
            bea_api.time.sleep = orig
        self.assertTrue(slept and slept[0] < 2, slept)

    def test_keycheck_verify(self):
        kc = load("bea_pack_keycheck", os.path.join(PACK, "keycheck.py"))
        os.environ["DATA_ANALYST_FIXTURES"] = FX
        try:
            ok, detail = kc.verify(DOC_KEY)
        finally:
            del os.environ["DATA_ANALYST_FIXTURES"]
        self.assertIs(ok, True, detail)
        self.assertNotIn(DOC_KEY, detail)

    def test_cache_dir_override(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["DATA_ANALYST_CACHE"] = d               # core.env.cache_root() + bea/bulk
            try:
                self.assertEqual(os.path.normpath(fetch.cache_dir()), os.path.join(d, "bea", "bulk"))
            finally:
                del os.environ["DATA_ANALYST_CACHE"]


class TestRegressions(unittest.TestCase):
    """Regression checks (bulk-file checks read the trimmed real fixtures)."""

    # ------------------------------------------------------------------ fetch.py
    def test_s00_missing_requested_years_are_named(self):
        # a request that gets some of its years succeeds; the years it asked for and did not get must be said
        reqs = [{"id": "inc", "years": "2016,2017,2018"}, {"id": "all", "years": "X"}, {"id": "gone", "years": "2016"}]
        rows = [{"request_id": "inc", "year": 2016}, {"request_id": "inc", "year": 2017},
                {"request_id": "all", "year": 2010}]
        self.assertEqual(fetch.missing_years(reqs, rows), {"inc": [2018]})

    def test_s01_nipa_bulk_keeps_every_line(self):
        rows, _ = fetch.nipa_from_flat(nipa_req("T10205", "2023"), FX, {}, FILES)
        lines = {int(r["line"]) for r in rows}
        self.assertTrue({3, 6} <= lines and lines == set(range(1, max(lines) + 1)), sorted(lines))

    def test_s02_nipa_bulk_raises_on_empty(self):
        self.assertTrue(raises(lambda: fetch.nipa_from_flat(nipa_req("T10101", "1900,1901"), FX, {}, FILES),
                               bea_api.BeaError))

    def test_s02b_nipa_bulk_refuses_monthly_for_a_table_not_published_monthly(self):
        # NipaDataM holds series shared with A/Q tables: the bulk route must not label monthly values with a table
        # whose title does not say it is monthly (the API refuses frequency M for such tables)
        with self.assertRaises(bea_api.BeaError) as cm:
            fetch.nipa_from_flat(nipa_req("T10106", "2024", "M"), FX, {}, FILES)
        self.assertIn("not monthly", str(cm.exception))
        self.assertTrue(fetch.MONTHLY_TITLE.search("Table 2.6. Personal Income and Its Disposition, Monthly"))
        self.assertTrue(fetch.MONTHLY_TITLE.search("Table 2.8.11. ... Percent Change from Month One Year Ago"))
        self.assertFalse(fetch.MONTHLY_TITLE.search("Table 2.3.11. ... Percent Change from Quarter One Year Ago"))

    def test_s03_nipa_line_code_filters(self):
        q = fetch.normalize_requests({"requests": [{"id": "g", "dataset": "NIPA", "table": "T10106", "line_code": 1,
                                                    "frequency": "A", "years": "2024-2025"}]})[0]
        self.assertEqual(q["line_codes"], ["1"])
        rows = [{"request_id": "g", "line": str(ln), "year": y} for ln in (1, 2) for y in (2024, 2025)]
        self.assertEqual({r["line"] for r in fetch.finalize_rows([q], rows)}, {"1"})
        q["line_codes"] = ["999"]
        self.assertTrue(raises(lambda: fetch.finalize_rows([q], rows), bea_api.BeaError))

    def test_s04_fingerprint_unchanged_without_line_filters(self):
        spec = {"requests": [{"id": "a", "dataset": "NIPA", "table": "T10101", "frequency": "Q", "years": "2024"}]}
        reqs = fetch.normalize_requests(spec)
        import hashlib
        old = hashlib.sha256(json.dumps([r["calls"] for r in reqs] + [[r["dataset"] for r in reqs]],
                                        sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(fetch.fingerprint(reqs), old)

    def test_s05_lastn_not_sent_to_nipa(self):
        q = fetch.normalize_requests({"requests": [{"id": "n", "dataset": "NIPA", "table": "T10101", "years": "LAST2"}]})[0]
        self.assertTrue(q["calls"][0]["Year"] == "X" and q["years"] == "LAST2")
        rows = [{"request_id": "n", "line": "1", "year": y} for y in (2022, 2023, 2024)]
        self.assertEqual({r["year"] for r in fetch.finalize_rows([q], rows)}, {2023, 2024})

    def test_s06_two_digit_state_fips_padded(self):
        q = regional_req(["06", "48000"])
        self.assertTrue(q["geo"] == "06000,48000" and q["calls"][0]["GeoFips"] == "06000,48000")
        rows, _ = fetch.regional_from_zip(q, FX, {}, FILES)
        self.assertEqual({r["geo_fips"] for r in rows}, {"06000", "48000"})

    def test_s07_bulk_refuses_unselectable_geographies(self):
        self.assertFalse(fetch.bulk_coverage(regional_req("TERR"))[0])
        self.assertFalse(fetch.bulk_coverage(regional_req("NY"))[0])     # postal code = counties: not in SA tables
        self.assertTrue(fetch.bulk_geo_ok("NY", "CAINC1"))
        self.assertTrue(raises(lambda: fetch.regional_from_zip(regional_req("TERR"), FX, {}, FILES), bea_api.BeaError))

    def test_s08_regional_notes_carry_footnotes(self):
        _, notes = fetch.regional_from_zip(regional_req("STATE"), FX, {}, FILES)
        refs = {n["note_ref"] for n in notes}
        self.assertTrue({"1", "(D)", "(NA)"} <= refs, refs)
        self.assertFalse(any("included footnote file" in n["note_text"] for n in notes))
        self.assertTrue(any("chained 2017 dollars" in n["note_text"] for n in notes))

    def test_s09_gdpbyindustry_api_periods(self):
        rows = fetch.tidy_api_rows({"id": "g", "dataset": "GDPbyIndustry", "table": "1", "frequency": "", "geo": None},
                                   {}, [{"Year": "2024", "Quarter": "2024", "DataValue": "1"},
                                        {"Year": "2024", "Quarter": "II", "DataValue": "2"}])
        self.assertEqual([(r["time_period"], r["year"], r["frequency"]) for r in rows],
                         [("2024", 2024, "A"), ("2024Q2", 2024, "Q")])

    def test_s10_geo_level_classifies_codes(self):
        self.assertEqual(fetch.geo_level("06037", "06037,06059"), "county")
        self.assertEqual(fetch.geo_level("06000", "06000,48000"), "state")
        self.assertEqual(fetch.geo_level("31080", "MSA"), "msa")

    def test_s11_workbook_unit_text(self):
        self.assertEqual(fetch.workbook_unit("[Millions of dollars] Seasonally adjusted at annual rates"),
                         "Millions of dollars; seasonally adjusted at annual rates")
        self.assertEqual(fetch.workbook_unit("[2017=100]"), "2017=100")

    # ------------------------------------------------------------------ stats.py
    def test_s12_growth_nan_across_gaps(self):
        a = pd.Series([100.0, 110.0, 121.0], index=["2019", "2021", "2022"])
        a.attrs["frequency"] = "A"
        g = stats.growth(a)
        self.assertTrue(math.isnan(g["2021"]) and abs(g["2022"] - 10) < 1e-9)
        q = pd.Series([100.0, 101.0, 103.0], index=["2024Q1", "2024Q2", "2024Q4"])
        q.attrs["frequency"] = "Q"
        self.assertTrue(math.isnan(stats.growth(q)["2024Q4"]))
        idx = [f"{y}Q{k}" for y in range(2020, 2026) for k in range(1, 5)]
        s = pd.Series([100 + i * 0.7 + (i % 3) for i in range(len(idx))], index=idx, dtype=float)
        s.attrs["frequency"] = "Q"
        self.assertTrue(stats.growth(s).equals(((s / s.shift(1)) ** 4 - 1) * 100))
        self.assertTrue(stats.yoy(s).equals((s / s.shift(4) - 1) * 100))

    def test_s12b_monthly_growth_not_annualized(self):
        # BEA's monthly percent-change tables (T20807) are plain monthly changes; quarterly ones are annualized
        m = pd.Series([100.0, 100.3, 100.5], index=["2026M06", "2026M07", "2026M08"])
        m.attrs["frequency"] = "M"
        self.assertAlmostEqual(stats.growth(m)["2026M07"], 0.3)
        self.assertAlmostEqual(stats.growth(m, annualize=True)["2026M07"], (1.003 ** 12 - 1) * 100)

    def test_s12d_industry_section_units(self):
        f = load("bea_pack_fetch_t", os.path.join(PACK, "fetch.py"))
        self.assertEqual(f.section_unit("Percent and percentage points", "Percent change"), "Percent")
        self.assertEqual(f.section_unit("Percent and percentage points", "Percentage points"), "Percentage points")
        self.assertEqual(f.section_unit("Millions of dollars", "Addenda"), "Millions of dollars")
        self.assertEqual(f.plain_unit("Level", "Quantity Contributions", "0"),
                         "percentage points (contribution to the percent change; quarterly at an annual rate)")
        self.assertIn("dollars per person", f.plain_unit("Level", "Ratio", "0"))

    def test_s12c_guid_redaction_spares_paths(self):
        api = load("bea_api_t", os.path.join(PACK, "bea_api.py"))
        p = r"C:\tmp\f2b4c907-70d5-4426-8f30-5c3698def66e\scratchpad\bea_data.csv"
        self.assertEqual(api.redact(p), p)
        self.assertEqual(api.redact("/tmp/f2b4c907-70d5-4426-8f30-5c3698def66e/x"), "/tmp/f2b4c907-70d5-4426-8f30-5c3698def66e/x")
        self.assertNotIn("12345678-ABCD", api.redact("my key 12345678-ABCD-1234-ABCD-1234567890AB"))

    def test_s13_series_mixed_frequency_asks(self):
        base = {"request_id": "x", "dataset": "NIPA", "table": "T10106", "line": "1", "series_code": "A191RX",
                "description": "GDP", "geo_fips": "", "geo_name": "United States", "geo_level": "nation", "industry": "",
                "date": "", "flag": "", "unit": "Level", "metric": "Chained Dollars", "unit_mult": 6, "note_ref": "",
                "dims": ""}
        rows = [dict(base, time_period=tp, frequency=f, year=2024, subperiod=sub, value=1.0)
                for tp, f, sub in (("2024", "A", ""), ("2024Q1", "Q", "Q1"))]
        buf = io.StringIO()
        pd.DataFrame(rows).to_csv(buf, index=False)
        buf.seek(0)
        df = stats.load(buf)
        with self.assertRaises(ValueError) as cm:
            stats.series(df, table="T10106", line=1)
        self.assertIn("frequency", str(cm.exception))

    def test_s14_change_between_and_near_ties(self):
        self.assertLess(abs(stats.change_between(pd.Series([100.0, 129.2289], index=["2019", "2025"]), "2019", "2025")
                            - 29.2289), 1e-9)
        ties = stats.near_ties(pd.Series({"A": 21.61, "B": 21.58, "C": 10.44, "D": 10.43, "E": 5.0}))
        self.assertEqual(ties, [(1, "A", "B"), (3, "C", "D")])

    # ------------------------------------------------------------------ report tooling (shared scripts/report)
    def test_s15_fact_format_override_with_underscore(self):
        br = report_module("build_report")
        m = br.FACT_RE.search("GDP was {{gdp|dollars_t:1}}")
        self.assertTrue(m and m.group(2) == "dollars_t:1")

    def test_s16_hand_typed_number_gate(self):
        ar = report_module("audit_report")
        hn = getattr(ar, "hard_numbers", None)
        if hn is None:
            self.skipTest("audit_report.hard_numbers not present")
        self.assertTrue(hn("Spending reached $2,000 per person.", set()))
        self.assertEqual(hn("In Exhibit 2, 45 percent of states grew.", set()), ["45"])
        self.assertEqual(hn("Exhibits 2, 3 and 4 show it.", set()), [])
        self.assertEqual(hn("Table 1.1.6 shows growth since 2019.", set()), [])
        self.assertEqual(hn("Growth was 45 percent.", set()), ["45"])

    def test_s17_rerun_diff(self):
        ar = report_module("audit_report")
        d = getattr(ar, "fact_diff", None)
        if d is None:
            self.skipTest("audit_report.fact_diff not present")
        self.assertEqual(d({"a": {"value": 1.0}}, {"a": {"value": float("nan")}}), ["a"])
        self.assertEqual(d({"a": {"value": "x"}}, {"a": {"value": "y"}}), ["a"])
        self.assertEqual(d({"a": {"value": 1.0}}, {"a": {"value": 1.0}, "b": {"value": 2.0}}), ["b"])
        self.assertEqual(d({"a": {"value": 1.0}}, {"a": {"value": 1.0 + 1e-12}}), [])

    def test_s18_key_redaction_and_secret_scan(self):
        key = "1A2B3C4D-1A2B-1A2B-1A2B-1A2B3C4D5E6F"
        samples = ['{"env": {"BEA_API_KEY": "%s"}}' % key,
                   '{"ParameterName": "USERID", "ParameterValue": "%s"}' % key,
                   "https://apps.bea.gov/api/data?UserID=%s&method=GetData" % key]
        for s in samples:                                  # the pack never lets one out (core.keys.redact path)
            self.assertNotIn(key, bea_api.redact(s), s)
        scrubbed = bea_api.scrub_response({"BEAAPI": {"Request": {"RequestParam": [
            {"ParameterName": "USERID", "ParameterValue": "not-a-guid-key"}]}}})
        self.assertEqual(scrubbed["BEAAPI"]["Request"]["RequestParam"][0]["ParameterValue"], "[REDACTED]")
        try:
            ss = report_module("scan_secrets")
        except unittest.SkipTest:
            return
        for s in samples:                                  # and the shared scanner finds each shape
            self.assertTrue(ss.scan_text(s), s)
        self.assertFalse(ss.scan_text('{"ParameterName": "USERID", "ParameterValue": "[REDACTED]"}'))
        with tempfile.TemporaryDirectory() as d:
            zp = os.path.join(d, "pkg.zip")
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("lib/x.txt", "key=" + key.lower())
            self.assertTrue(ss.scan_file(zp, [key.encode()]), "key inside zip not found")

    def test_s19_number_formats(self):
        f = factfmt.format_value
        self.assertEqual(f(-0.04, "pct100:1"), "0.0 percent")
        self.assertEqual(f(-4e6, "dollars_b:1"), "$0.0 billion")
        self.assertEqual((f(2.25, "pct100:1"), f(2.35, "pct100:1")), ("2.3 percent", "2.4 percent"))
        self.assertEqual(f(-2.25, "pct100:1"), "-2.3 percent")
        self.assertEqual(f(80612, "dollars:-2"), "$80,600")
        self.assertEqual(f(-1.5, "dollars:2"), "-$1.50")
        self.assertEqual(f(7, "num:0"), "seven")


if __name__ == "__main__":
    unittest.main()

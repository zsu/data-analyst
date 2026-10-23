"""Offline tests for the fred deep pack: 38 checks.

Every behaviour of fetch.py / fred_api.py / tsstats.py / factfmt.py that a regression would silently break,
plus the release calendar, v2 release observations and the shared-contract rules (routes, data/fred/,
restricted).

Fixtures (fixtures/) were recorded live on 2026-10-20 (the FULL spec's API requests re-recorded on 2026-10-22,
adding the series/tags and release requests the code now makes) with DATA_ANALYST_RECORD and normal fetches:
graph_* / alfred_* from the keyless route; api_* / geofred_* / v2_* from the API with a real key (the key is
not in the recorded URLs or bodies). Keyed tests set FRED_API_KEY to a made-up value: fixture URLs carry no
key, so the made-up key never matters and is never sent anywhere.
"""
import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

PACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "sources", "fred")
PACK = os.path.abspath(PACK)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SCRIPTS = os.path.join(PACK, "..", "..", "scripts")
sys.path.insert(0, SCRIPTS)
from testkit import PackTest  # noqa: E402

FAKE = "abcdefghijklmnopqrstuvwxyz012345"          # made up; fixtures answer without any key
PIPE = {"description": "Output gap since 2019",
        "series": [{"id": "GDPC1", "label": "Real GDP", "source": "U.S. Bureau of Economic Analysis",
                    "copyright": "public_domain",
                    "copyright_basis": "series page tag Public Domain: Citation Requested, read 2026-10-20"},
                   {"id": "GDPPOT", "label": "Real potential GDP (CBO)", "source": "U.S. Congressional Budget Office",
                    "copyright": "public_domain", "copyright_basis": "series page tag, read 2026-10-20"},
                   {"id": "USREC", "label": "NBER recession indicator", "source": "Federal Reserve Bank of St. Louis",
                    "copyright": "citation_required",
                    "copyright_basis": "series page tag Copyrighted: Citation Required, read 2026-10-20"}],
        "start": "2007-01-01"}
FULL = {"description": "API coverage: latest, first release, as-of vintages, GeoFRED, release calendar and release "
                       "observations",
        "series": [{"id": "GDPC1", "label": "Real GDP", "start": "2024-01-01", "end": "2025-12-31"},
                   {"id": "GDPC1", "label": "Real GDP, first release", "start": "2020-04-01", "end": "2020-04-01",
                    "vintage": "first_release"},
                   {"id": "GDPC1", "label": "Real GDP, as of", "start": "2020-04-01", "end": "2020-07-01",
                    "vintage": {"as_of": ["2020-08-27", "2021-07-29"]}}],
        "regional": [{"series_id": "CAUR", "region_type": "state", "date": "2025-01-01",
                      "label": "State unemployment rates"}],
        "release_dates": {"release_id": 50, "start": "2026-10-01", "end": "2026-12-31", "limit": 5},
        "release_observations": [{"release_id": 53, "limit": 5, "max_pages": 2}]}
ASOF = {"description": "Keyless as-of vintages",
        "series": [{"id": "GDPC1", "start": "2020-04-01", "end": "2020-07-01",
                    "vintage": {"as_of": ["2020-08-27", "2021-07-29"]}}]}
KEY = {"FRED_API_KEY": FAKE}


def load(name, rel):
    """Import a pack module by path under a unique name (other packs have a fetch.py and keycheck.py too)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(PACK, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def table(out, name):
    with open(os.path.join(out, "data", "fred", f"{name}.csv"), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fixture(name) -> bytes:
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class TestFredCli(PackTest):
    source = "fred"
    sample_spec = PIPE

    # ---------------------------------------------------------------- routes
    def test_01_check_keyless(self):
        code, r = self.check(PIPE)
        self.assertEqual(code, 0, r)
        self.assertEqual((r["route"], r["source"]), ("api_keyless", "fred"))
        self.assertIn("--source fred", r["next_command"])
        self.assertTrue(r["next_command"].endswith("--no-key"))
        self.assertLess(len(json.dumps(r)), 2600)

    def test_02_check_api_with_key(self):
        code, r = self.check(PIPE, env=KEY)
        self.assertEqual(r["route"], "api", r)
        self.assertTrue(r["key_ok"])
        self.assertNotIn(FAKE, json.dumps(r))

    def test_03_key_only_needs(self):
        code, r = self.check(FULL, env={"DATA_ANALYST_ENV": "local"})
        self.assertEqual(r["route"], "run_setup", r)
        self.assertIn("fred", r["run_command"])
        code, r = self.check(FULL, env={"DATA_ANALYST_ENV": "sandbox"})
        self.assertEqual(r["route"], "manual_download", r)
        self.assertTrue(r["manual_bundle"].replace("\\", "/").endswith("manual_download/fred"))
        with open(os.path.join(r["manual_bundle"], "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertTrue(any(i.get("bearer") and i["file"] == "release_53.json" for i in items))
        self.assertTrue(all("__FRED_KEY__" in i["url"] or not i["keyed"] or i.get("bearer") for i in items))
        with open(r["download_scripts"]["bash"], "rb") as f:
            sh = f.read().decode("ascii")
        with open(r["download_scripts"]["powershell"], "rb") as f:
            ps = f.read().decode("ascii")
        self.assertNotIn("\r", sh)                                      # bash LF, PowerShell CRLF
        self.assertEqual(ps.count("\n"), ps.count("\r\n"))
        for script in (sh, ps):                                         # the v2 pages and GeoFRED steps built in
            self.assertNotIn("@MANIFEST@", script)
            self.assertIn("release_53.json\tbearer\t2\thttps://api.stlouisfed.org/fred/v2/release/observations?", script)
            self.assertIn("regional_01__group.json\tgroup\t0\t", script)
            self.assertIn("__GROUP_SERIES_GROUP__", script)
            self.assertNotIn(FAKE, script)
        self.assertIn("Authorization: Bearer", sh)
        self.assertIn("curl -sS -K -", sh)

    def test_04_fix_spec(self):
        code, r = self.check({"series": [{"id": "GDPC1", "vintage": "yesterday"}]})
        self.assertEqual((code, r["route"]), (2, "fix_spec"), r)

    # ---------------------------------------------------------------- fetch
    def test_05_fetch_keyless(self):
        sp, out = self.write_spec(PIPE)
        code, r = self.cli(sp, "--out", out, "--no-key")
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api_keyless")
        rows = table(out, "fred_observations")
        self.assertEqual(list(rows[0]), ["series_id", "label", "variant", "vintage", "date", "value", "realtime_start",
                                         "realtime_end"])
        hit = next(x for x in rows if x["series_id"] == "GDPC1" and x["date"] == "2020-04-01")
        self.assertEqual(hit["value"], "19077.992")
        meta = {m["series_id"]: m for m in table(out, "series")}
        self.assertEqual(meta["USREC"]["copyright"], "citation_required")
        self.assertEqual(meta["GDPC1"]["metadata_source"], "fredgraph.xlsx")
        self.assertGreater(int(meta["GDPPOT"]["n_future"]), 0)            # CBO projections flagged
        with open(os.path.join(out, "data", "fred", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("downloaded without the API", readme)
        with open(os.path.join(out, "data", "fred", "request.json"), encoding="utf-8") as f:
            req = json.load(f)
        self.assertEqual((req["source"], req["route"], req["keyless"]), ("fred", "api_keyless", True))
        self.assertTrue(os.path.exists(os.path.join(out, "lib", "fred", "tsstats.py")))
        self.assertTrue(os.path.exists(os.path.join(out, "lib", "factfmt.py")))

    def test_06_fetch_api_full(self):
        code, r, out = self.fetch(FULL, env=KEY)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")
        rows = table(out, "fred_observations")
        fr = next(x for x in rows if x["variant"] == "GDPC1_first_release")
        self.assertEqual((fr["value"], fr["realtime_start"]), ("17205.822", "2020-07-30"))
        asof = {(x["vintage"], x["date"]): x["value"] for x in rows if x["variant"] == "GDPC1_asof"}
        self.assertEqual(asof[("2020-08-27", "2020-04-01")], "17282.188")
        self.assertNotIn(("2020-08-27", "2020-07-01"), asof)                 # not yet published then
        meta = table(out, "series")
        self.assertEqual(meta[0]["source"], "U.S. Bureau of Economic Analysis")
        self.assertEqual(meta[0]["copyright"], "public_domain")             # from the series/tags record
        self.assertEqual(len(table(out, "regional")), 51)
        with open(os.path.join(out, "data", "fred", "README.md"), encoding="utf-8") as f:
            self.assertIn("This product uses the FRED", f.read())
        with open(os.path.join(out, "data", "fred", "request.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(FAKE, text)
        self.assertNotIn("api_key=", text.replace("api_key=[REDACTED]", ""))

    def test_07_release_calendar_and_observations(self):
        code, r, out = self.fetch(FULL, env=KEY)
        self.assertEqual([d["date"] for d in table(out, "release_dates")], ["2026-10-02", "2026-11-06", "2026-12-04"])
        rs = table(out, "release_series")
        self.assertEqual(len(rs), 1)                                     # pages merged by series
        self.assertEqual((rs[0]["series_id"], rs[0]["copyright"], rs[0]["n_obs"]),
                         ("A001RD3A086NBEA", "public_domain", "10"))
        ro = table(out, "release_observations")
        self.assertEqual(len(ro), 10)
        self.assertEqual((ro[0]["date"], ro[0]["value"]), ("1929-01-01", "8.757"))
        self.assertTrue(any("max_pages" in n for n in r["notes"]))

    def test_08_release_dates_command(self):
        code, r = self.cli("--release-dates", "50", "--start", "2026-10-01", "--end", "2026-12-31", "--limit", "5",
                           env=KEY)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["count"], 3)
        code, r = self.cli("--release-dates", "50")
        self.assertEqual((code, r["status"]), (4, "needs_key"))

    def test_09_keyless_asof(self):
        sp, out = self.write_spec(ASOF)
        code, r = self.cli(sp, "--out", out, "--no-key")
        self.assertEqual(code, 0, r)
        rows = table(out, "fred_observations")
        self.assertEqual([(x["vintage"], x["date"]) for x in rows],
                         [("2020-08-27", "2020-04-01"), ("2021-07-29", "2020-04-01"), ("2021-07-29", "2020-07-01")])

    def test_10_unknown_id(self):
        sp, out = self.write_spec({"description": "Unknown ID", "series": ["NOSUCHSERIESXYZ"], "start": "2024-01-01"})
        code, r = self.cli(sp, "--out", out, "--no-key")
        self.assertEqual(code, 2, r)
        self.assertTrue(any("no such series" in p for p in r["problems"]))

    def test_11_reuse_and_repackage(self):
        sp, out = self.write_spec(PIPE)
        self.cli(sp, "--out", out, "--no-key")
        code, r = self.cli(sp, "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing", r)
        spec2 = json.loads(json.dumps(PIPE))
        spec2["series"][0]["label"] = "Real gross domestic product"
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(spec2, f)
        code, r = self.cli(sp, "--out", out, "--check")
        self.assertEqual(r["route"], "reuse_existing")
        self.assertIn("--source fred", r["next_command"])
        code, r = self.cli(sp, "--out", out)
        self.assertEqual(r["status"], "repackaged", r)
        self.assertEqual(table(out, "series")[0]["label"], "Real gross domestic product")

    def test_12_manual_bundle_and_ingest(self):
        sp, out = self.write_spec(PIPE)
        self.cli(sp, "--out", out, "--no-key")
        code, b = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, b)
        names = sorted(os.listdir(b["manual_bundle"]))
        self.assertEqual(names, ["INSTRUCTIONS.md", "download_fred.ps1", "download_fred.sh", "manifest.json",
                                 "spec.json"])
        self.assertEqual(sorted(b["download_scripts"]), ["bash", "powershell"])
        with open(os.path.join(b["manual_bundle"], "spec.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), PIPE)
        with open(os.path.join(b["manual_bundle"], "INSTRUCTIONS.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("bash ~/Downloads/download_fred.sh", text)
        self.assertIn('powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\\Downloads\\download_fred.ps1"', text)
        self.assertNotIn("python", text.lower())
        self.assertNotIn("--ingest", text)
        for name in ("download_fred.sh", "download_fred.ps1"):         # the series URLs are in the scripts
            with open(os.path.join(b["manual_bundle"], name), encoding="ascii") as f:
                script = f.read()
            for sid in ("GDPC1", "GDPPOT", "USREC"):
                self.assertIn(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&", script)
        out2 = os.path.join(self.tmp.name, "out2")
        os.makedirs(out2)
        sp2 = os.path.join(out2, "spec.json")
        with open(sp2, "w", encoding="utf-8") as f:
            json.dump(PIPE, f)
        code, r = self.cli(sp2, "--out", out2, "--ingest", os.path.join(out, "data", "fred", "raw"))
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "manual_download")
        self.assertEqual(table(out2, "fred_observations"), table(out, "fred_observations"))

    def test_12b_ingest_release_pages(self):
        """The scripts save each v2 page as its own wrapped file; --ingest merges them like the API route."""
        spec = {"description": "Release 53 by hand", "release_observations": [{"release_id": 53, "limit": 5,
                                                                                "max_pages": 2}]}
        sp, out = self.write_spec(spec)
        up = os.path.join(self.tmp.name, "uploads")
        os.makedirs(up)
        url = "https://api.stlouisfed.org/fred/v2/release/observations?release_id=53&format=json&limit=5"
        for name, fx, u in (("release_53.json", "v2_release_obs_53_p1.json", url),
                            ("release_53__p02.json", "v2_release_obs_53_p2.json",
                             url + "&next_cursor=A001RD3A086NBEA%2C1934-01-01")):
            with open(os.path.join(up, name), "w", encoding="utf-8") as f:
                f.write('{"request": "%s", "response": %s}' % (u, fixture(fx).decode("utf-8")))
        code, r = self.cli(sp, "--out", out, "--ingest", up)
        self.assertEqual(code, 0, r)
        rs = table(out, "release_series")
        self.assertEqual([(x["series_id"], x["n_obs"]) for x in rs], [("A001RD3A086NBEA", "10")])
        ro = table(out, "release_observations")
        self.assertEqual([x["date"][:4] for x in ro], [str(y) for y in range(1929, 1939)])
        with open(os.path.join(out, "data", "fred", "request.json"), encoding="utf-8") as f:
            self.assertTrue(any("max_pages" in n for n in json.load(f).get("notes") or []))

    def test_13_ingest_without_paths(self):
        sp, out = self.write_spec(PIPE)
        code, r = self.cli(sp, "--out", out, "--ingest")
        self.assertEqual(code, 2, r)

    def test_14_restricted_fetch_blocks(self):
        sp, out = self.write_spec(PIPE)
        code, r = self.cli(sp, "--out", out, "--no-key", "--env", "sandbox", env={"DATA_ACCESS_FORCE": "restricted"},
                           fixtures=False)
        self.assertEqual((code, r["route"]), (4, "manual_download"), r)
        self.assertIn("--ingest", r["next_command"])

    def test_15_search_without_key(self):
        code, r = self.cli("--search", "potential gdp")
        self.assertEqual((code, r["status"]), (4, "needs_key"))

    def test_16_describe(self):
        code, r = self.cli("--describe", "GDPC1")
        self.assertEqual(r["series"][0]["title"], "Real Gross Domestic Product")
        self.assertEqual(r["series"][0]["copyright"], "unknown")
        code, r = self.cli("--describe", "GDPC1", env=KEY)
        self.assertEqual(r["series"][0]["source"], "U.S. Bureau of Economic Analysis")
        self.assertEqual(r["series"][0]["release"], "Gross Domestic Product")


class TestFredUnits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load("fred_api", "fred_api.py")
        cls.f = load("fred_pack_fetch", "fetch.py")
        sys.path.insert(0, os.path.join(PACK, "lib"))
        cls.ts = load("fred_tsstats", os.path.join("lib", "tsstats.py"))
        cls.fmt = load("factfmt", os.path.join("..", "..", "scripts", "analysis", "factfmt.py"))

    def test_citation_uses_local_day(self):
        # an evening retrieval in the U.S. is already tomorrow in UTC [live 2026-10-22]
        from datetime import datetime, timezone
        stamp = datetime(2026, 10, 23, 0, 2, tzinfo=timezone.utc)
        local = stamp.astimezone()
        want = f"{local.strftime('%B')} {local.day}, {local.year}"
        self.assertIn(want, self.api.citation("T5YIE", "5-Year Breakeven", "FRBSL", stamp.isoformat()))
        self.assertIn("October 22, 2026", self.api.citation("T5YIE", "x", "", "2026-10-22"))

    def test_readme_flags_delayed_series(self):
        # UMCSENT's FRED notes [live 2026-10-22]: the series reaches FRED a month after UMich publishes it
        keys = ("series_id", "label", "title", "units", "frequency", "fiscal_year", "seasonal_adjustment", "vintage",
                "first_date", "last_date", "n_obs", "n_missing", "n_future", "last_updated", "copyright", "citation")
        m = dict.fromkeys(keys, "")
        m.update(series_id="UMCSENT", notes="At the request of the source, the data is delayed by 1 month. Copyright.")
        req = {"keyless": False, "retrieved": "2026-10-22", "acquired": "api", "route": "api", "regional_rows": 0}
        with tempfile.TemporaryDirectory() as d:
            self.f.write_readme(d, {"description": "x"}, [m, dict(m, series_id="GDPC1", notes="BEA")], [], req)
            with open(os.path.join(d, "README.md"), encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("`UMCSENT` reaches FRED late", text)
        self.assertIn("delayed by 1 month", text)
        self.assertNotIn("`GDPC1` reaches FRED late", text)

    def test_maintenance_page_is_an_outage(self):
        # FRED's maintenance page comes back as HTML with HTTP 200 [live 2026-10-21]: not "no such series"
        from core.http import Response

        class Stub:
            def get(self, url, params=None, ok_statuses=()):
                return Response(200, {}, b"<!DOCTYPE html><html><title>Site Maintenance</title></html>", url)
        c = self.api.FredClient(api_key="0" * 32)
        with self.assertRaises(self.api.FredError) as e:
            c._json(Stub(), "https://api.stlouisfed.org/fred/series", {}, "series")
        self.assertEqual(e.exception.kind, "unavailable")
        self.assertEqual(c.ping()["auth_ok"], c.ping()["auth_ok"])      # ping never raises

    def test_17_norm_vintage(self):
        nv = self.f.norm_vintage
        self.assertEqual(nv(None)["kind"], "latest")
        self.assertEqual(nv("initial")["kind"], "first_release")
        self.assertEqual(nv({"as_of": ["2021-07-29", "2020-08-27"]})["dates"], ["2020-08-27", "2021-07-29"])
        with self.assertRaises(ValueError):
            nv({"as_of": ["July 2020"]})

    def test_18_norm_spec(self):
        s = self.f.norm_spec({"series": ["gdpc1"], "recessions": True})
        self.assertEqual([x["id"] for x in s["series"]], ["GDPC1", "USREC"])
        with self.assertRaises(ValueError):
            self.f.norm_spec({"series": ["GDPC1", "GDPC1"]})
        with self.assertRaises(ValueError):
            self.f.norm_spec({"series": [{"id": "GDPC1", "frequency": "x"}]})
        with self.assertRaises(ValueError):
            self.f.norm_spec({"release_observations": [{"release_id": "gdp"}]})

    def test_19_fingerprints(self):
        a = self.f.norm_spec({"series": [{"id": "GDPC1", "label": "a"}]})
        b = self.f.norm_spec({"series": [{"id": "GDPC1", "label": "b"}]})
        self.assertEqual(self.f.fingerprint(a), self.f.fingerprint(b))
        self.assertNotEqual(self.f.meta_fingerprint(a), self.f.meta_fingerprint(b))
        c = self.f.norm_spec({"series": [{"id": "GDPC1"}], "release_dates": {"release_id": 50}})
        self.assertNotEqual(self.f.fingerprint(a), self.f.fingerprint(c))

    def test_20_tags(self):
        s = self.f.norm_spec({"series": [{"id": "CPIAUCSL", "units": "pc1"}, {"id": "GDP", "frequency": "a"},
                                         {"id": "GDPC1", "vintage": {"as_of": "2020-08-27"}}]})
        self.assertEqual([self.f.tag(x) for x in s["series"]], ["CPIAUCSL_pc1", "GDP_aavg", "GDPC1_asof"])

    def test_21_key_only_needs(self):
        s = self.f.norm_spec(FULL)
        needs = self.f.key_only_needs(s)
        self.assertEqual(len(needs), 4)
        self.assertEqual(self.f.key_only_needs(self.f.norm_spec(ASOF)), [])

    def test_22_graph_url(self):
        u = self.api.graph_url("GDPC1", start="2020-04-01", vintage_dates=["2020-08-27", "2021-07-29"])
        self.assertTrue(u.startswith("https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=GDPC1,GDPC1"))
        self.assertIn("cosd=2020-04-01,2020-04-01", u)
        self.assertIn("fq=Quarterly&fam=eop", self.api.graph_url("X", frequency="q", aggregation_method="eop"))

    def test_23_parse_graph_csv_and_zip(self):
        cols = self.api.parse_graph_csv(fixture("graph_GDPC1_2007.csv"))
        self.assertEqual(cols["GDPC1"][0]["date"], "2007-01-01")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:                 # FRED zips mixed frequencies: same files inside
            z.writestr("quarterly.csv", fixture("graph_GDPC1_2007.csv"))
            z.writestr("monthly.csv", fixture("graph_USREC_2007.csv"))
        self.assertEqual(sorted(self.api.parse_graph_csv(buf.getvalue())), ["GDPC1", "USREC"])

    def test_24_parse_graph_html_is_not_found(self):
        with self.assertRaises(self.api.FredError) as cm:
            self.api.parse_graph_csv(fixture("graph_404_page.html"))
        self.assertEqual(cm.exception.kind, "not_found")

    def test_25_parse_graph_xlsx(self):
        meta, obs = self.api.parse_graph_xlsx(fixture("graph_meta_GDPC1_GDPPOT_USREC.xlsx"))
        self.assertEqual(meta["GDPC1"]["units"], "Billions of Chained 2017 Dollars")
        self.assertEqual(meta["GDPC1"]["seasonal_adjustment"], "Seasonally Adjusted Annual Rate")
        self.assertEqual(meta["USREC"]["frequency"], "Monthly")
        self.assertRegex(meta["GDPPOT"]["last_updated"], r"^\d{4}-\d{2}-\d{2}$")

    def test_26_split_description_fiscal(self):
        # the README line FRED's fredgraph.xlsx gave for FYOINT on 2026-10-20; the fiscal year shows in the sheet name
        d = self.api._split_description("Federal Outlays: Interest, Millions of Dollars, Annual, Not Seasonally "
                                        "Adjusted", "Data Updated: 2026-04-03")
        self.assertEqual((d["title"], d["units"], d["frequency"], d["last_updated"]),
                         ("Federal Outlays: Interest", "Millions of Dollars", "Annual", "2026-04-03"))
        self.assertTrue(self.f.is_fiscal({**d, "sheet": "Annual, Fiscal Year"}, "FYOINT"))

    def test_27_observation_rows(self):
        rows = self.api.observation_rows("GDPC1", [{"date": "2020-04-01", "value": "."},
                                                   {"date": "2020-04-01", "GDPC1_20200827": "17282.188"}])
        self.assertIsNone(rows[0]["value"])
        self.assertEqual((rows[1]["realtime_start"], rows[1]["value"]), ("2020-08-27", 17282.188))

    def test_28_regional_rows(self):
        payload = json.loads(fixture("geofred_regional_1224_2025-01.json"))
        rows = self.api.regional_rows(payload)
        self.assertEqual(len(rows), 51)
        self.assertEqual(rows[0]["date"], "2025-01-01")

    def test_29_copyright(self):
        self.assertEqual(self.api.copyright_status("Copyright, 2016, Standard & Poor's", True), "copyrighted")
        self.assertEqual(self.api.copyright_status("BEA Account Code", True), "no_copyright_notice")
        self.assertEqual(self.api.copyright_status("", False), "unknown")
        self.assertEqual(self.api.copyright_from_tag("copyrighted: pre-approval required"), "preapproval_required")
        self.assertEqual(self.api.copyright_from_tag("public domain: citation requested"), "public_domain")
        tags = {"tags": [{"name": "nation", "group_id": "geot"},          # series/tags of T10Y2Y [live 2026-10-21]
                         {"name": "copyrighted: citation required", "group_id": "cc"}]}
        self.assertEqual(self.api.copyright_tag_of(tags), "copyrighted: citation required")
        self.assertEqual(self.api.copyright_tag_of({"tags": []}), "")

    def test_29b_keyless_vintages_chunked_and_geo_freq(self):
        # one graph download holds 12 columns and FRED drops the rest silently [live 2026-10-21: 14 asked, 12 back]
        dates = [f"2024-{m:02d}-15" for m in range(1, 13)] + ["2025-01-15", "2025-02-15"]
        s = self.f.norm_spec({"series": [{"id": "GDPC1", "vintage": {"as_of": dates}}]})
        x = s["series"][0]
        self.assertEqual([len(c) for c in self.f.vintage_chunks(x)], [12, 2])
        items = self.f.manual_items(s)
        self.assertEqual([i["file"] for i in items[:2]], ["GDPC1_asof_p1.csv", "GDPC1_asof_p2.csv"])
        self.assertEqual(self.f.vintage_chunks(self.f.norm_spec({"series": ["GDPC1"]})["series"][0]), [None])
        self.assertEqual([self.f.geo_freq(v) for v in ("a", "Annual", "sa", "Semiannual", "bw", "Biweekly", "q")],
                         ["a", "a", "sa", "sa", "bw", "bw", "q"])

    def test_30_citation(self):
        c = self.api.citation("GDPC1", "Real Gross Domestic Product", "U.S. Bureau of Economic Analysis",
                              "2026-10-20T19:45:12+00:00")
        self.assertEqual(c, "U.S. Bureau of Economic Analysis, Real Gross Domestic Product [GDPC1], retrieved from "
                            "FRED, Federal Reserve Bank of St. Louis; https://fred.stlouisfed.org/series/GDPC1, "
                            "October 20, 2026.")

    def test_31_period_end_and_fiscal(self):
        self.assertEqual(self.f.period_end_iso("2026-10-01", ["2026-07-01", "2026-10-01"]), "2026-12-31")
        self.assertEqual(self.f.period_end_iso("2025-09-30", ["2024-09-30", "2025-09-30"]), "2025-09-30")
        # daily and weekly dates are the observation itself, not a month that has not ended
        self.assertEqual(self.f.period_end_iso("2026-10-01", ["2026-09-30", "2026-10-01"]), "2026-10-01")
        self.assertEqual(self.f.period_end_iso("2026-10-01", ["2026-09-24", "2026-10-01"]), "2026-10-01")
        self.assertTrue(self.f.is_fiscal({"frequency": "Annual, Fiscal Year"}))
        self.assertTrue(self.f.is_fiscal({"frequency": "Annual"}, "FYOIGDA188S"))
        self.assertFalse(self.f.is_fiscal({"frequency": "Annual"}, "GDPA"))

    def test_32_tsstats_wide_and_future(self):
        pd = self.ts.pd
        df = pd.DataFrame({"series_id": "X", "label": "", "variant": "X", "vintage": "latest",
                           "date": pd.to_datetime(["2025-01-01", "2025-04-01", "2025-10-01", "2026-01-01",
                                                   "2026-04-01", "2099-01-01"]),
                           "value": [1.0, 2.0, 4.0, 5.0, 6.0, 9.0], "realtime_start": "", "realtime_end": ""})
        w = self.ts.wide(df, "X")
        self.assertEqual(len(w), 6)                         # 2025 Q3 kept as a gap, 2099 (a projection) dropped
        self.assertTrue(w["X"].isna().iloc[2])
        self.assertEqual(len(self.ts.wide(df, "X", include_future=True)), 297)

    def test_33_tsstats_to_frequency_and_fy(self):
        pd = self.ts.pd
        s = pd.Series([1.0] * 11 + [None] + [2.0] * 12,
                      index=pd.date_range("2024-01-01", periods=24, freq="MS"))
        a = self.ts.to_frequency(s, "A")
        self.assertTrue(a["value"].isna().iloc[0])          # 2024 has a missing month
        self.assertEqual(a["value"].iloc[1], 2.0)
        fy = self.ts.fiscal_year_label(pd.Series([1.0, 2.0], index=pd.to_datetime(["2024-09-30", "2025-09-30"])))
        self.assertEqual(list(fy.index), [2024, 2025])
        d = pd.Series(range(60), index=pd.bdate_range("2026-07-01", periods=60), dtype=float)
        m = self.ts.to_frequency(d, "M")                     # daily -> monthly averages
        self.assertEqual(m.index[0], pd.Timestamp("2026-07-01"))
        self.assertAlmostEqual(m["value"].iloc[0], sum(range(23)) / 23)
        with self.assertRaises(ValueError):
            self.ts.to_frequency(s, "M")                     # already monthly
        # weekly data through mid-October: 42 weeks of 2026 are not its annual average
        w = pd.Series(1.0, index=pd.date_range("2024-01-06", "2026-10-17", freq="W-SAT"))
        a = self.ts.to_frequency(w, "A")
        self.assertTrue(a["value"].isna().iloc[-1])
        self.assertEqual(a["value"].iloc[-2], 1.0)

    def test_34_factfmt(self):
        fv = self.fmt.format_value
        self.assertEqual(fv(970065, "from_millions:dollars_b:1"), "$970.1 billion")
        self.assertEqual(fv(0.1, "pp100:1"), "0.1 percentage point")
        self.assertEqual(fv(0.4, "signed:pct100:1"), "+0.4 percent")
        self.assertEqual(fv(7, "num:0"), "seven")

    def test_35_redaction(self):
        self.api.keys.remember(FAKE)
        msg = self.api.redact(f"https://api.stlouisfed.org/fred/series?series_id=X&api_key={FAKE}&file_type=json")
        self.assertNotIn(FAKE, msg)
        self.assertIn("api_key=[REDACTED]", msg)
        self.assertEqual(repr(self.api.FredClient(api_key=FAKE)), "FredClient(key=[REDACTED])")

    def test_36_policy(self):
        from core import policy
        p = policy.load(PACK)
        self.assertEqual(p["cache"]["mode"], "none")
        self.assertEqual(p["redistribution"]["withhold"]["shareable"],
                         ["no_copyright_notice", "public_domain", "citation_required"])
        with self.assertRaises(policy.PolicyError):
            policy.check_cache(p, "run_only")
        self.assertIn("FRED", p["notices"][0])

    def test_37_keycheck(self):
        kc = load("fred_pack_keycheck", "keycheck.py")

        class R:
            def __init__(self, status, body):
                self.status, self.body = status, body.encode()

        class C:
            reply = None

            def __init__(self, *a, **k):
                pass

            def get(self, *a, **k):
                return C.reply
        orig = kc.Client
        kc.Client = C
        try:
            C.reply = R(400, '{"error_code":400,"error_message":"Bad Request.  The value for variable api_key is not '
                             'registered."}')                # the documented rejection
            self.assertIs(kc.verify(FAKE)[0], False)
            C.reply = R(200, '{"seriess": []}')
            self.assertIs(kc.verify(FAKE)[0], True)
            C.reply = R(429, "")
            self.assertIsNone(kc.verify(FAKE)[0])
        finally:
            kc.Client = orig


if __name__ == "__main__":
    unittest.main()

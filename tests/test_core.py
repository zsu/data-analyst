"""Core behaviour: key lookup and redaction, policy enforcement, cache rules, budgets, paging, manual bundles."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from core import generic_rest as gr  # noqa: E402
from core import keys, manual, package, policy  # noqa: E402
from core.cache import Cache  # noqa: E402
from core.http import Budget, Client, DataError, clean_url  # noqa: E402
from core.packs import Pack  # noqa: E402

FAKE = "abcdef0123456789abcdef0123456789"


def isolated_home(tmp):
    return mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp, "CLAUDE_CONFIG_DIR": os.path.join(tmp, ".claude"),
                                        "DATA_ANALYST_CONFIG_DIR": os.path.join(tmp, ".config", "data-analyst")})


class TestKeys(unittest.TestCase):
    def test_lookup_order(self):
        with tempfile.TemporaryDirectory() as t, isolated_home(t):
            os.environ.pop("X_TEST_KEY", None)
            self.assertEqual(keys.find_key("X_TEST_KEY", "xtest"), (None, None))
            os.makedirs(os.path.join(t, ".xtest"))
            open(os.path.join(t, ".xtest", "api_key"), "w").write("from-home-file\n")
            self.assertEqual(keys.find_key("X_TEST_KEY", "xtest")[0], "from-home-file")
            os.makedirs(os.path.join(t, ".claude"))
            json.dump({"env": {"X_TEST_KEY": "from-settings"}}, open(os.path.join(t, ".claude", "settings.json"), "w"))
            self.assertEqual(keys.find_key("X_TEST_KEY", "xtest")[0], "from-settings")
            # the skill's own keys.json (any assistant) comes before Claude Code's settings.json
            os.makedirs(os.path.join(t, ".config", "data-analyst"))
            json.dump({"env": {"X_TEST_KEY": "from-keys-json"}}, open(keys.config_path(), "w"))
            self.assertEqual(keys.find_key("X_TEST_KEY", "xtest"), ("from-keys-json", f"keys.json ({keys.config_path()})"))
            kf = os.path.join(t, "k.txt")
            open(kf, "w").write("from-file-var")
            with mock.patch.dict(os.environ, {"X_TEST_KEY_FILE": kf}):
                self.assertEqual(keys.find_key("X_TEST_KEY", "xtest")[0], "from-file-var")
                with mock.patch.dict(os.environ, {"X_TEST_KEY": "from-env"}):
                    self.assertEqual(keys.find_key("X_TEST_KEY", "xtest"), ("from-env", "X_TEST_KEY environment variable"))
                    with mock.patch.dict(os.environ, {"DATA_ACCESS_FORCE": "restricted"}):
                        self.assertEqual(keys.find_key("X_TEST_KEY", "xtest"), (None, None))

    def test_redact(self):
        keys.remember(FAKE)
        s = f"https://x.gov/a?api_key={FAKE}&q=1 and {{'registrationkey': '{FAKE}'}} Authorization: Bearer tok123456"
        r = keys.redact(s)
        self.assertNotIn(FAKE, r)
        self.assertNotIn("tok123456", r)
        self.assertIn("q=1", r)
        self.assertNotIn("secret", clean_url("https://x.gov/a?key=secret&b=2", ("key",)))

    def test_redact_leaves_header_prose_alone(self):
        # a manual item's note said "'Authorization: Bearer[REDACTED] header" (the quote was taken for a token)
        prose = "needs your token in an 'Authorization: Bearer' header; send Authorization: Bearer <token>"
        self.assertEqual(keys.redact(prose), prose)
        self.assertEqual(keys.redact("Authorization: Basic dXNlcjpwYXNz"), "Authorization: Basic [REDACTED]")
        self.assertEqual(keys.redact("authorization: abcdef123456"), "authorization: [REDACTED]")


class TestPolicy(unittest.TestCase):
    def pol(self, **over):
        p = {"id": "t", "title": "T", "agency": "A", "depth": "standard", "domains": ["x"], "homepage": "https://x",
             "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"manual": {"how": "download"}}, "cache": {"mode": "none", "why": "terms"},
             "redistribution": {"mode": "definition_only", "microdata_globs": ["*.dat", "extract.csv"], "why": "terms"},
             "citation": "T, retrieved {retrieved}."}
        p.update(over)
        with tempfile.TemporaryDirectory() as t:
            d = os.path.join(t, "t")
            os.makedirs(d)
            json.dump(p, open(os.path.join(d, "policy.json"), "w"))
            return policy.load(d), d

    def test_validate_and_microdata(self):
        p, d = self.pol()
        self.assertEqual(policy.validate(p), [])
        self.assertTrue(policy.is_microdata(p, "data/t/extract.csv"))
        self.assertFalse(policy.is_microdata(p, "data/t/codebook.csv"))
        self.assertEqual(policy.check_package(p, ["data/t/x.dat", "data/t/README.md"]), ["data/t/x.dat"])

    def test_validate_catches_problems(self):
        p, _ = self.pol(routes={}, citation="no date")
        probs = " ".join(policy.validate(p))
        self.assertIn("manual", probs)
        self.assertIn("retrieved", probs)

    def test_cache_refused(self):
        p, _ = self.pol()
        with self.assertRaises(policy.PolicyError):
            policy.check_cache(p, "persistent")
        self.assertIsNone(Cache("t", p).put("k", b"x"))
        self.assertIsNone(Cache("t", p).get("k"))

    def test_strictest_and_notices(self):
        a, _ = self.pol(notices=["N1"])
        b, _ = self.pol(redistribution={"mode": "allowed", "why": "pd"}, notices=["N1", "N2"])
        self.assertEqual(policy.strictest([a, b]), "definition_only")
        self.assertEqual(policy.notices([a, b]), ["N1", "N2"])


class TestHttp(unittest.TestCase):
    def test_budget(self):
        b = Budget(2, "errors")
        b.spend()
        b.spend()
        with self.assertRaises(DataError) as e:
            b.spend()
        self.assertEqual(e.exception.kind, "rate_limit")

    def test_restricted(self):
        with mock.patch.dict(os.environ, {"DATA_ACCESS_FORCE": "restricted"}):
            os.environ.pop("DATA_ANALYST_FIXTURES", None)
            with self.assertRaises(DataError) as e:
                Client("https://example.gov", name="t").get("/x")
            self.assertEqual(e.exception.kind, "network")

    def test_html_error_page_reduced_to_title(self):
        # BLS answered POSTs with this page (HTTP 503) on 2026-10-22; the message carried 400 characters of markup
        page = (b'<!DOCTYPE HTML>\r\n<html lang="en-us">\t\r\n<head>\r\n<title>The BLS website is busy</title>\r\n'
                b'<script>(window.BOOMR_mq=window.BOOMR_mq||[]).push(["addVar",{}])</script></head><body></body></html>')
        with tempfile.TemporaryDirectory() as t:
            json.dump({"GET https://example.gov/x": {"file": "r.html", "status": 503}}, open(os.path.join(t, "index.json"), "w"))
            open(os.path.join(t, "r.html"), "wb").write(page)
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                with self.assertRaises(DataError) as e:
                    Client("https://example.gov", name="t", retries=0).get("/x")
        msg = str(e.exception)
        self.assertIn("HTML page: The BLS website is busy", msg)
        self.assertIn("run again", msg)
        self.assertNotIn("<script", msg)
        from core.http import error_snippet
        self.assertEqual(error_snippet(b'{"error": "bad param"}'), '{"error": "bad param"}')

    def test_403_from_a_keyless_host_is_a_block_not_a_key_problem(self):
        # senate.gov answered scripts with an HTML 403 on 2026-10-27; it was reported as a rejected api.data.gov key
        with tempfile.TemporaryDirectory() as t:
            json.dump({"GET https://example.gov/x": {"file": "r.html", "status": 403}}, open(os.path.join(t, "index.json"), "w"))
            open(os.path.join(t, "r.html"), "wb").write(b"<html><title>Access Denied</title></html>")
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                with self.assertRaises(DataError) as e:
                    Client("https://example.gov", name="t", retries=0).get("/x")
                self.assertEqual(e.exception.kind, "blocked")
                self.assertNotIn("key rejected", str(e.exception))
                with self.assertRaises(DataError) as e:      # a host that gets a key: still a key problem
                    Client("https://example.gov", name="t", retries=0, key=FAKE,
                           auth={"env": "T_KEY", "placement": "query", "param": "api_key"}).get("/x")
                self.assertEqual(e.exception.kind, "auth")

    def test_key_placement_never_in_recorded_url(self):
        with tempfile.TemporaryDirectory() as t:
            json.dump({"GET https://example.gov/x?a=1": "r.json"}, open(os.path.join(t, "index.json"), "w"))
            open(os.path.join(t, "r.json"), "w").write("[1]")
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                c = Client("https://example.gov", name="t", auth={"placement": "query", "param": "api_key"}, key=FAKE)
                r = c.get("/x", {"a": 1})
                self.assertEqual(r.json(), [1])
                self.assertNotIn(FAKE, r.url)

    def test_redirect_to_another_host_drops_the_key(self):
        import http.server
        import threading
        seen = {}

        class Target(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen[self.path.split("?")[0]] = (self.headers.get("X-Api-Key"), self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        tgt = http.server.HTTPServer(("127.0.0.1", 0), Target)
        port = tgt.server_port

        class Origin(Target):
            def do_GET(self):
                if self.path.startswith("/same"):
                    return super().do_GET()
                self.send_response(302)
                if self.path.startswith("/away"):         # another host (localhost, not 127.0.0.1)
                    self.send_header("Location", f"http://localhost:{port}/landed{self.path}?api_key=echoed")
                else:                                     # the same host and port
                    self.send_header("Location", f"/same{self.path}")
                self.end_headers()

        org = http.server.HTTPServer(("127.0.0.1", 0), Origin)
        for s in (tgt, org):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        env = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        try:
            with mock.patch.dict(os.environ, env):
                os.environ.pop("DATA_ANALYST_FIXTURES", None)
                os.environ.pop("DATA_ACCESS_FORCE", None)
                hdr = Client(f"http://127.0.0.1:{org.server_port}", name="t", key=FAKE, retries=0,
                             auth={"placement": "header", "param": "X-Api-Key"})
                hdr.get("/away")                          # 127.0.0.1 -> localhost: another host
                self.assertIsNone(seen["/landed/away"][0])
                qry = Client(f"http://127.0.0.1:{org.server_port}", name="t", key=FAKE, retries=0,
                             auth={"placement": "query", "param": "api_key"})
                qry.get("/away2")
                self.assertNotIn("api_key", seen["/landed/away2"][1])
                hdr.get("/stay")                          # same host and port: the key may follow
                self.assertEqual(seen["/same/stay"][0], FAKE)
        finally:
            for s in (tgt, org):
                s.shutdown()
                s.server_close()


class TestRouteAfterFailure(unittest.TestCase):
    """A probe can answer (any HTTP status counts) while the real request is blocked: the route that just
    failed must not come back as next_command."""

    def check(self, pid, env, failed):
        from core import packs, routes
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
            return routes.route_check(packs.load(pid), {"requests": []}, os.path.join(t, "spec.json"), t, env,
                                      failed=failed)

    def test_network_failure_is_not_offered_again(self):
        r = self.check("cdc", "local", None)
        self.assertEqual(r["route"], "api")              # fixtures: every probe answers
        r = self.check("cdc", "local", {"route": "api", "kind": "network", "error": "HTTP 403 bot filter"})
        self.assertNotEqual(r["route"], "api")
        self.assertIn("403", json.dumps(r["probed"]))

    def test_key_rejected_by_the_request_is_not_ok(self):
        with mock.patch.dict(os.environ, {"DATA_GOV_API_KEY": FAKE}):
            r = self.check("fbi", "local", {"route": "api", "kind": "auth", "error": "HTTP 403 API_KEY_INVALID"})
        self.assertIs(r["key_ok"], False)
        self.assertNotEqual(r["route"], "api")


    def test_blocked_host_goes_to_a_browser_download(self):
        with mock.patch.dict(os.environ, {"DATA_GOV_API_KEY": FAKE}):
            r = self.check("congress", "local", {"route": "api", "kind": "blocked",
                                                 "error": "senate-lis: HTTP 403 from www.senate.gov: the host refused"})
        self.assertEqual(r["route"], "manual_download")
        self.assertIsNone(r["key_ok"])                    # no key was blamed
        self.assertIn("web browser", r["message"])
        self.assertNotIn("rejected", r["message"])


class TestPlan(unittest.TestCase):
    def test_a_source_listed_twice_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            plan = os.path.join(t, "plan.json")
            with open(plan, "w", encoding="utf-8") as f:
                json.dump({"sources": [{"source": "cdc", "spec": {"requests": []}},
                                       {"source": "cdc", "spec": {"requests": []}}]}, f)
            p = subprocess.run([sys.executable, "-I", "-B", os.path.join(SKILL, "scripts", "fetch.py"), "--plan", plan,
                                "--out", os.path.join(t, "out"), "--check"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("more than once", p.stdout)


    def test_plan_action_names_only_what_the_check_produced(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fetch_cli", os.path.join(SKILL, "scripts", "fetch.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        # keys missing, no bundle written: no talk of bundles; the setup command and the keyless alternative
        a = cli.plan_action(["fred"], [{"source": "hud", "route": "run_setup"}, {"source": "census", "route": "run_setup"}],
                            False)
        self.assertNotIn("bundle", a)
        self.assertIn("setup command of hud, census", a)
        self.assertIn("keyless alternative", a)
        self.assertTrue(a.startswith("Fetch every ready source now"))
        a = cli.plan_action([], [{"source": "cdc", "route": "manual_download"}], True)
        self.assertIn("send every bundle together", a)
        self.assertEqual(cli.plan_action(["cdc"], [], False), "Run without --check.")
        a = cli.plan_action([], [{"source": "cdc", "route": "fix_spec"}], False)
        self.assertIn("Fix the spec of cdc", a)
        self.assertNotIn("ask the user", a)


class TestSetupKey(unittest.TestCase):
    def helper(self, *args, **env):
        e = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        e.update(env)
        return subprocess.run([sys.executable, "-I", "-B", os.path.join(SKILL, "scripts", "setup_key.py"), *args],
                              capture_output=True, text=True, env=e)

    def test_save_defaults_to_keys_json(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = os.path.join(t, "cfg")
            p = self.helper("save", "fred", DATA_ANALYST_SETUP_KEY=FAKE, DATA_ANALYST_CONFIG_DIR=cfg)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertNotIn(FAKE, p.stdout + p.stderr)
            with open(os.path.join(cfg, "keys.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"env": {"FRED_API_KEY": FAKE}})

    def test_info_reports_a_key_already_in_the_environment(self):
        self.assertFalse(json.loads(self.helper("info", "fred").stdout)["env_set"])
        p = self.helper("info", "fred", FRED_API_KEY=FAKE)
        self.assertTrue(json.loads(p.stdout)["env_set"])
        self.assertNotIn(FAKE, p.stdout)


    def test_probe_on_another_host_is_not_a_key_check(self):
        """usgs probes earthquake.usgs.gov, which ignores keys: verify must say "not checked", not "OK"."""
        env = {**os.environ, "DATA_ANALYST_SETUP_KEY": FAKE, "DATA_ACCESS_FORCE": "restricted"}
        p = subprocess.run([sys.executable, "-I", "-B", os.path.join(SKILL, "scripts", "setup_key.py"), "verify", "usgs"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("not checked", p.stdout)
        self.assertNotIn(FAKE, p.stdout + p.stderr)


class TestPatientFetch(unittest.TestCase):
    def test_a_busy_minute_does_not_lose_the_run(self):
        # Congress.gov answered 503 for a minute inside a 1,244-call for_each (Oct 2026)
        calls = []

        def flaky(c, ep, params, max_rows):
            calls.append(1)
            if len(calls) < 3:
                raise DataError("x: HTTP 503 after 2 retries", kind="http", status=503)
            return [{"a": 1}], {"truncated": False, "total": 1}
        c = Client("https://example.gov", name="t")
        env = {k: v for k, v in os.environ.items() if k != "DATA_ANALYST_FIXTURES"}
        with mock.patch.object(gr, "fetch_request", flaky), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("time.sleep"):
            rows, _ = gr._fetch_patiently(c, {}, {}, None)
            self.assertEqual((rows, len(calls)), ([{"a": 1}], 3))

        def missing(c, ep, params, max_rows):
            calls.append(1)
            raise DataError("x: not found (HTTP 404)", kind="not_found", status=404)
        calls.clear()
        with mock.patch.object(gr, "fetch_request", missing), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("time.sleep") as slept:
            with self.assertRaises(DataError):                # a 404 is not waited out
                gr._fetch_patiently(c, {}, {}, None)
            self.assertEqual((len(calls), slept.call_count), (1, 0))


class TestGenericRest(unittest.TestCase):
    def test_offset_paging_and_manual_items(self):
        eps = {"base_url": "https://example.gov", "endpoints": {"q": {
            "path": "/d/{ds}.json", "params": {"ds": {"in": "path", "required": True}},
            "records": "", "paging": {"type": "offset", "limit_param": "limit", "offset_param": "offset", "size": 2},
            "missing": ["", "(NA)"]}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        with tempfile.TemporaryDirectory() as t:
            idx = {"GET https://example.gov/d/a.json?limit=2&offset=0": "p1.json",
                   "GET https://example.gov/d/a.json?limit=2&offset=2": "p2.json"}
            json.dump(idx, open(os.path.join(t, "index.json"), "w"))
            json.dump([{"v": "1"}, {"v": "(NA)", "n": {"x": 1}}], open(os.path.join(t, "p1.json"), "w"))
            json.dump([{"v": "3"}], open(os.path.join(t, "p2.json"), "w"))
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                spec = {"requests": [{"name": "a", "endpoint": "q", "params": {"ds": "a"}}]}
                tables, notes = gr.fetch(pack, spec)
        self.assertEqual([r["v"] for r in tables["a"]], ["1", "", "3"])
        self.assertEqual(tables["a"][1]["n.x"], 1)
        items = gr.manual_items(pack, {"requests": [{"name": "a", "endpoint": "q", "params": {"ds": "a"}, "max_rows": 5}]})
        self.assertEqual(len(items), 3)

    def test_csv_cells_over_128_kb_are_read(self):
        # Python's csv default stops at 131072 chars per field; agency CSVs carry geometries and full texts.
        eps = {"base_url": "https://example.gov", "endpoints": {"c": {
            "path": "/docs.csv", "records": "$csv", "paging": {"type": "none"}}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        big = "word " * 60000                                   # 300,000 characters in one quoted cell
        with tempfile.TemporaryDirectory() as t:
            json.dump({"GET https://example.gov/docs.csv": "d.csv"}, open(os.path.join(t, "index.json"), "w"))
            with open(os.path.join(t, "d.csv"), "w", encoding="utf-8", newline="") as f:
                f.write(f'id,text\n1,"{big}"\n2,short\n')
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                tables, _ = gr.fetch(pack, {"requests": [{"name": "d", "endpoint": "c"}]})
        self.assertEqual([r["id"] for r in tables["d"]], ["1", "2"])
        self.assertEqual(len(tables["d"][0]["text"]), len(big))

    def test_empty_requests_named_together_and_allowed(self):
        eps = {"base_url": "https://example.gov", "endpoints": {"q": {
            "path": "/d/{ds}.json", "params": {"ds": {"in": "path", "required": True}}, "records": "",
            "paging": {"type": "none"}}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        with tempfile.TemporaryDirectory() as t:
            idx = {f"GET https://example.gov/d/{k}.json": f"{k}.json" for k in "abc"}
            json.dump(idx, open(os.path.join(t, "index.json"), "w"))
            json.dump([{"v": "1", "w": "2"}], open(os.path.join(t, "a.json"), "w"))
            for k in "bc":
                json.dump([], open(os.path.join(t, f"{k}.json"), "w"))
            reqs = [{"name": k, "endpoint": "q", "params": {"ds": k}} for k in "abc"]
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                with self.assertRaises(DataError) as e:
                    gr.fetch(pack, {"requests": reqs})
                self.assertIn("'b', 'c'", str(e.exception))
                self.assertIn("allow_empty", str(e.exception))
                for r in reqs[1:]:
                    r["allow_empty"] = True
                tables, _ = gr.fetch(pack, {"requests": reqs})
        self.assertEqual(tables["b"], [])

    def test_for_each_expands_over_a_column_and_merges(self):
        eps = {"base_url": "https://example.gov", "endpoints": {"q": {
            "path": "/d/{ds}.json", "params": {"ds": {"in": "path", "required": True}}, "records": "",
            "paging": {"type": "none"}}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "list.csv"), "w", encoding="utf-8", newline="") as f:
                f.write("packageId,title\na,A\nb,B\na,A again\n")
            json.dump({"GET https://example.gov/d/a.json": "a.json", "GET https://example.gov/d/b.json": "b.json"},
                      open(os.path.join(t, "index.json"), "w"))
            json.dump([{"pages": "3"}], open(os.path.join(t, "a.json"), "w"))
            json.dump([{"pages": "5"}], open(os.path.join(t, "b.json"), "w"))
            spec = {"requests": [{"name": "pages", "endpoint": "q", "for_each": {"csv": "list.csv", "column": "packageId",
                                                                                "param": "ds"}}]}
            ex = gr.expand_for_each(spec, t)
            self.assertEqual([r["params"]["ds"] for r in ex["requests"]], ["a", "b"])     # distinct values
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                tables, _ = gr.fetch(pack, ex)
            merged = gr.merge_for_each(ex, tables)
            self.assertEqual(merged, {"pages": [{"ds": "a", "pages": "3"}, {"ds": "b", "pages": "5"}]})
            self.assertEqual(gr.collapse_for_each(ex), spec)
            # several parameters from several columns (a bill: type and number), values lowercased
            with open(os.path.join(t, "laws.csv"), "w", encoding="utf-8", newline="") as f:
                f.write("type,number\nHR,1\nS,1\nHR,1\n")
            ex2 = gr.expand_for_each({"requests": [{"name": "b", "endpoint": "q", "for_each": {
                "csv": "laws.csv", "map": {"billType": "type", "billNumber": "number"}, "lower": True}}]}, t)
            self.assertEqual([(r["params"]["billType"], r["params"]["billNumber"]) for r in ex2["requests"]],
                             [("hr", "1"), ("s", "1")])
            self.assertEqual(gr.merge_for_each(ex2, {"b__1": [{"x": 1}], "b__2": [{"x": 2}]})["b"],
                             [{"billType": "hr", "billNumber": "1", "x": 1}, {"billType": "s", "billNumber": "1", "x": 2}])
            # a prefix dropped from each value (a split nomination "PN1255-17" -> "1255-17")
            with open(os.path.join(t, "noms.csv"), "w", encoding="utf-8", newline="") as f:
                f.write("citation\nPN1255-17\nPN12\n")
            ex3 = gr.expand_for_each({"requests": [{"name": "a", "endpoint": "q", "for_each": {
                "csv": "noms.csv", "column": "citation", "param": "nominationNumber", "strip": "PN"}}]}, t)
            self.assertEqual([r["params"]["nominationNumber"] for r in ex3["requests"]], ["1255-17", "12"])
            with self.assertRaises(DataError):
                gr.expand_for_each({"requests": [{"name": "x", "endpoint": "q",
                                                  "for_each": {"csv": "list.csv", "column": "nope"}}]}, t)

    def test_undeclared_param_is_noted(self):
        # congress laws_congress has no lawType: the API ignored it and private laws came back too [live 2026-10-22]
        eps = {"base_url": "https://example.gov", "endpoints": {"q": {
            "path": "/law/{congress}", "params": {"congress": {"in": "path", "required": True}}, "records": "",
            "paging": {"type": "none"}}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        with mock.patch.object(gr, "fetch_request", return_value=([{"v": 1}], {"truncated": False, "total": 1})):
            _, notes = gr.fetch(pack, {"requests": [{"name": "laws", "endpoint": "q",
                                                     "params": {"congress": 119, "lawType": "pub"}}]})
        self.assertTrue(any("lawType" in n and "not declared" in n for n in notes), notes)

    def test_not_found_requests_run_the_rest_and_are_named_together(self):
        eps = {"base_url": "https://example.gov", "endpoints": {"q": {
            "path": "/d/{ds}.json", "params": {"ds": {"in": "path", "required": True}}, "records": "",
            "paging": {"type": "none"}}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        with tempfile.TemporaryDirectory() as t:
            idx = {"GET https://example.gov/d/a.json": "a.json", "GET https://example.gov/d/c.json": "a.json",
                   "GET https://example.gov/d/b.json": {"status": 404, "body": ""},
                   "GET https://example.gov/d/d.json": {"status": 404, "body": ""}}
            json.dump(idx, open(os.path.join(t, "index.json"), "w"))
            json.dump([{"v": "1"}], open(os.path.join(t, "a.json"), "w"))
            reqs = [{"name": k, "endpoint": "q", "params": {"ds": k}} for k in "abcd"]
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                with self.assertRaises(DataError) as e:
                    gr.fetch(pack, {"requests": reqs})
                self.assertIn("'b', 'd'", str(e.exception))
                self.assertEqual(e.exception.kind, "not_found")
                for r in (reqs[1], reqs[3]):
                    r["allow_empty"] = True
                tables, notes = gr.fetch(pack, {"requests": reqs})
        self.assertEqual((tables["b"], len(tables["c"])), ([], 1))
        self.assertTrue(any("404" in n and n.startswith("b:") for n in notes))

    def test_manual_bundle_and_match(self):
        P = {"id": "t", "title": "T", "auth": {"required": "required", "placement": "query", "param": "api_key",
                                               "env": "T_KEY", "signup_url": "https://x"}}
        pack = Pack("t", "", P, None, None)
        with tempfile.TemporaryDirectory() as t:
            b = manual.write_bundle(t, [(pack, [{"name": "a", "url": "https://x/a?api_key={API_KEY}", "filename": "a.json",
                                                  "needs_key": True}])], "test")
            self.assertTrue(os.path.exists(b["instructions"]))
            self.assertIn("T_KEY", open(b["instructions"]).read())
            up = os.path.join(t, "up")
            os.makedirs(up)
            open(os.path.join(up, "a (1).json"), "w").write("[]")
            found, missing = manual.match_uploads([up], b["items"])
            self.assertEqual(missing, [])

    def bundle_packs(self):
        q = Pack("q", "", {"id": "q", "title": "Q", "auth": {"required": "required", "placement": "query",
                                                              "param": "api_key", "env": "Q_KEY",
                                                              "signup_url": "https://q.example/key"}}, None, None)
        h = Pack("h", "", {"id": "h", "title": "H", "auth": {"required": "required", "placement": "header",
                                                              "param": "Authorization", "prefix": "Bearer ",
                                                              "env": "H_KEY", "signup_url": "https://h.example/key"}},
                 None, None)
        return q, h

    def groups(self, base):
        q, h = self.bundle_packs()
        return [(q, [{"name": "a", "url": base + "/a?x=caf\u00e9&api_key={API_KEY}", "filename": "a.json",
                      "needs_key": True},
                     {"name": "gone", "url": base + "/missing", "filename": "gone.json", "optional": True}]),
                (h, [{"name": "b", "url": base + "/b", "filename": "b.json", "needs_key": True, "method": "POST",
                      "body": {"q": "x"}}])]

    def test_bundle_scripts_have_requests_built_in(self):
        with tempfile.TemporaryDirectory() as t:
            b = manual.write_bundle(t, self.groups("https://api.example"), "test", specs={"q": {"requests": []}})
            names = sorted(os.listdir(b["dir"]))
            self.assertEqual(names, ["INSTRUCTIONS.md", "download_data.ps1", "download_data.sh", "manifest.json",
                                     "spec.json"])
            sh = open(b["scripts"]["bash"], "rb").read()
            ps = open(b["scripts"]["powershell"], "rb").read()
            for raw in (sh, ps):
                raw.decode("ascii")                                  # PowerShell 5.1 reads BOM-less files as ANSI
                self.assertIn(b"https://api.example/a?x=caf%C3%A9&api_key={API_KEY}", raw)
                self.assertIn(b'{"q":"x"}', raw)
                self.assertNotIn(b"@ITEMS@", raw)
                self.assertNotIn(b"python", raw.lower())
            self.assertNotIn(b"\r\n", sh)
            self.assertIn(b"\r\n", ps)
            self.assertIn(b"curl", sh)
            self.assertIn(b"-K -", sh)                                  # the key goes to curl on stdin, not argv
            text = open(b["instructions"], encoding="utf-8").read()
            self.assertIn("bash ~/Downloads/download_data.sh", text)
            self.assertIn('-File "$HOME\\Downloads\\download_data.ps1"', text)
            self.assertIn("hidden input", text)
            self.assertNotIn("python", text.lower())
            self.assertEqual(b["commands"]["bash"], "bash ~/Downloads/download_data.sh")

    def test_bundle_two_sources_writes_plan(self):
        with tempfile.TemporaryDirectory() as t:
            b = manual.write_bundle(t, self.groups("https://api.example"), "test",
                                    specs={"q": {"requests": []}, "h": {"requests": []}})
            self.assertTrue(b["spec"].endswith("plan.json"))
            self.assertEqual([s["source"] for s in json.load(open(b["spec"]))["sources"]], ["q", "h"])

    def run_bundle_script(self, cmd_for):
        """Serve the three requests locally, run the generated script, check what it saved."""
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path.startswith("/a?") and "api_key=QK" in self.path:
                    return self.reply(200, b'{"a": 1}')
                self.reply(404, b'{"error": "not found"}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(("POST", self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") == "Bearer HK" and json.loads(body) == {"q": "x"}:
                    return self.reply(200, b'{"b": 2}')
                self.reply(401, b'{"error": "no"}')

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as t:
                b = manual.write_bundle(t, self.groups(f"http://127.0.0.1:{srv.server_port}"), "test")
                env = {**os.environ, "Q_KEY": "QK", "H_KEY": "HK", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
                p = subprocess.run(cmd_for(b["scripts"]), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=120)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(json.load(open(os.path.join(b["dir"], "a.json"))), {"a": 1})
                self.assertEqual(json.load(open(os.path.join(b["dir"], "b.json"))), {"b": 2})
                self.assertFalse(os.path.exists(os.path.join(b["dir"], "gone.json")))
                self.assertNotIn("QK", p.stdout + p.stderr)
                self.assertIn("Upload these files", p.stdout)
        finally:
            srv.shutdown()
        self.assertIn(("POST", "/b", "Bearer HK"), seen)

    def test_bundle_bash_script_downloads(self):
        bash = (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash"))
        if not bash or not os.path.exists(bash) or subprocess.run([bash, "-c", "command -v curl"],
                                                                  capture_output=True).returncode:
            self.skipTest("bash with curl not available")
        self.run_bundle_script(lambda s: [bash, s["bash"].replace("\\", "/")])

    def test_bundle_powershell_script_downloads(self):
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            self.skipTest("PowerShell not available")
        self.run_bundle_script(lambda s: [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", s["powershell"],
                                          "-NoPause"])


class TestLongCsvCells(unittest.TestCase):
    def test_facts_reads_a_cell_over_128_kib_without_core(self):
        big = "x" * 300_000
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "x.csv")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write('a,b\r\n1,"' + big + '"\r\n')
            # facts.py is used by analysis.py without core: it must raise the limit itself (own process)
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import facts; "
                    "print(facts._csv_cell(sys.argv[2], {'a': '1'}, 'a', 't')[0])")
            r = subprocess.run([sys.executable, "-I", "-B", "-c", code, os.path.join(SKILL, "scripts", "analysis"), p],
                               capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "1.0", r.stderr)


class TestUploadFormats(unittest.TestCase):
    def test_csv_upload_matches_a_json_link(self):
        # generic parse_upload reads a CSV saved for a JSON link; the matcher must accept it with two requests
        eps = {"base_url": "https://example.gov", "endpoints": {"q": {
            "path": "/d/{ds}.json", "params": {"ds": {"in": "path", "required": True}}, "records": "",
            "paging": {"type": "none"}}}}
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        pack = Pack("t", "", P, eps, None)
        spec = {"requests": [{"name": "a", "endpoint": "q", "params": {"ds": "a"}},
                             {"name": "b", "endpoint": "q", "params": {"ds": "b"}}]}
        items = gr.manual_items(pack, spec)
        self.assertEqual([i["also_ext"] for i in items], [[".csv"], [".csv"]])
        with tempfile.TemporaryDirectory() as t:
            for n in ("a", "b"):
                with open(os.path.join(t, f"{n}.csv"), "w", encoding="utf-8") as f:
                    f.write("v\n" + n + "1\n")
            found, missing = manual.match_uploads([t], items)
            self.assertEqual(missing, [])
            self.assertEqual(gr.parse_upload(pack, spec["requests"][1], [found["b#1"]]), [{"v": "b1"}])


class TestPackage(unittest.TestCase):
    def test_empty_table_gets_a_header(self):
        from core import packs
        pack = packs.load("congress")
        spec = {"requests": [{"name": "pub", "endpoint": "laws"}, {"name": "priv", "endpoint": "laws", "allow_empty": True},
                             {"name": "other", "endpoint": "bills", "allow_empty": True}]}
        with tempfile.TemporaryDirectory() as t:
            package.write(t, pack, {"pub": [{"number": "1", "type": "Public Law"}], "priv": [], "other": []}, spec, "api")
            d = os.path.join(t, "data", "congress")
            self.assertEqual(open(os.path.join(d, "priv.csv"), encoding="utf-8").read().strip(), "number,type")
            self.assertEqual(open(os.path.join(d, "other.csv"), encoding="utf-8").read().strip(), "no_rows")

    def test_second_fetch_keeps_earlier_provenance(self):
        from core import packs
        pack = packs.load("congress")
        with tempfile.TemporaryDirectory() as t:
            package.write(t, pack, {"laws": [{"number": "1"}]}, {"requests": [{"name": "laws", "endpoint": "laws"}]}, "api")
            package.write(t, pack, {"bill_1": [{"sponsor": "A"}]}, {"requests": [{"name": "bill_1", "endpoint": "bill"}]},
                          "api")
            d = os.path.join(t, "data", "congress")
            req = json.load(open(os.path.join(d, "request.json"), encoding="utf-8"))
            self.assertEqual([x["file"] for x in req["tables"]], ["bill_1.csv"])
            self.assertEqual([x["file"] for e in req["earlier_fetches"] for x in e["tables"]], ["laws.csv"])
            self.assertEqual(req["earlier_fetches"][0]["spec"]["requests"][0]["endpoint"], "laws")
            self.assertIn("laws.csv", open(os.path.join(d, "README.md"), encoding="utf-8").read())
            dd = open(os.path.join(d, "data_dictionary.csv"), encoding="utf-8").read()
            self.assertIn("laws,number", dd)
            self.assertIn("bill_1,sponsor", dd)

    def test_hook_conditions(self):
        from core import policy as pol
        P = {"id": "x", "audit_hooks": [
            {"id": "api_notice", "kind": "require_text", "any": ["not endorsed"], "when_route": ["api"], "why": "w"},
            {"id": "prelim", "kind": "require_text", "any": ["preliminary"], "why": "w",
             "when_data": {"glob": "data/x/s.csv", "column": "preliminary"}}]}
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "data", "x"))
            json.dump({"route": "bulk"}, open(os.path.join(t, "data", "x", "request.json"), "w"))
            open(os.path.join(t, "data", "x", "s.csv"), "w").write("v,preliminary\n1,0\n")
            self.assertEqual([s for s, _, _ in pol.run_audit_hooks(P, t, None, {}, html="")], ["PASS", "PASS"])
            json.dump({"route": "api"}, open(os.path.join(t, "data", "x", "request.json"), "w"))
            open(os.path.join(t, "data", "x", "s.csv"), "w").write("v,preliminary\n1,1\n")
            self.assertEqual([s for s, _, _ in pol.run_audit_hooks(P, t, None, {}, html="")], ["FAIL", "FAIL"])
            # when_columns: only when a matching column was fetched (or all columns); an empty file is no file
            Q = {"id": "x", "audit_hooks": [{"id": "cov", "kind": "require_file", "glob": "coverage*.csv",
                                             "when_columns": ["pay"], "why": "w"}]}
            json.dump({"route": "api", "spec": {"columns": ["occupational_series_code"]}},
                      open(os.path.join(t, "data", "x", "request.json"), "w"))
            self.assertEqual(pol.run_audit_hooks(Q, t, None, {}, html="")[0][0], "PASS")
            json.dump({"route": "api", "spec": {"columns": ["annualized_adjusted_basic_pay"]}},
                      open(os.path.join(t, "data", "x", "request.json"), "w"))
            open(os.path.join(t, "coverage.csv"), "w").write("")
            self.assertEqual(pol.run_audit_hooks(Q, t, None, {}, html="")[0][0], "FAIL")
            open(os.path.join(t, "coverage.csv"), "w").write("group,share\nall,0.47\n")
            self.assertEqual(pol.run_audit_hooks(Q, t, None, {}, html="")[0][0], "PASS")
            # the FRED citation: FRED and "retrieved" in either order, not one fixed phrase
            fred = pol.load(os.path.join(SKILL, "sources", "fred"))
            hook = [h for h in fred["audit_hooks"] if h["id"] == "fred_retrieved"]
            R = {"id": "fred", "audit_hooks": hook}
            for txt, want in (("Retrieved October 21, 2026 from FRED.", "PASS"), ("FRED (retrieved 2026-10-21)", "PASS"),
                              ("Source: FRED.", "FAIL")):
                self.assertEqual(pol.run_audit_hooks(R, t, None, {}, html=txt)[0][0], want, txt)

    def test_hook_condition_on_footnote_text(self):
        # a hook can fire on a footnote's text (BLS 2025 annual averages that exclude October)
        from core import policy
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data", "bls"))
            with open(os.path.join(d, "data", "bls", "bls_series.csv"), "w", encoding="utf-8", newline="") as f:
                f.write("series_id,footnotes\nLUU0204899600,Annual estimates for 2025 are 11-month averages that "
                        "exclude October.\nLNS14000000,\n")
            cond = {"glob": "data/bls/bls_series.csv", "column": "footnotes", "pattern": r"\d+-month average"}
            self.assertTrue(policy._data_flagged(d, cond))
            self.assertFalse(policy._data_flagged(d, dict(cond, pattern="preliminary")))
            # BLS's October 2025 gap matters only to an answer whose BLS facts reach 2025 or FY2026
            from core import packs
            P = packs.load("bls").policy
            with open(os.path.join(d, "data", "bls", "bls_series.csv"), "w", encoding="utf-8", newline="") as f:
                f.write("series_id,footnotes\nLNS14000000,Data unavailable due to the 2025 lapse in appropriations.\n")
            hook = lambda facts, text: [r for r in policy.run_audit_hooks(P, d, None, facts, html=text)  # noqa: E731
                                        if r[1] == "bls_partial_year"][0][0]
            fy26 = {"u": {"value": 4.3, "where": {"fiscal_year": "2026"}, "pack": "bls"}}
            y24 = {"u": {"value": 4.0, "where": {"year": "2024"}, "pack": "bls"}}
            self.assertEqual(hook(fy26, "unemployment averaged 4.3"), "FAIL")
            self.assertEqual(hook(fy26, "an 11-month average"), "PASS")
            self.assertEqual(hook(y24, "unemployment averaged 4.0"), "PASS")

    def test_route_ranks_the_named_source(self):
        from core import packs
        self.assertIn("u-6", packs._tokens("the U-6 rate"))
        top = [r["source"] for r in packs.route("What is the latest U-6 unemployment rate?", 3)]
        self.assertEqual(top[0], "bls")
        top = [r["source"] for r in packs.route("Personal saving rate trend", 3)]
        self.assertEqual(top[0], "bea")                      # "rate" alone must not pull gsa-calc's "... rate" keywords
        # questions that missed their pack in live runs (round 20-24 QA)
        for q, sid in (("Where is the University of Michigan consumer sentiment index now?", "fred"),
                       ("How much has the broad trade-weighted dollar index changed in 2026?", "fred"),
                       ("How much have import prices changed over 12 months, fuel and nonfuel?", "bls")):
            self.assertEqual(packs.route(q, 3)[0]["source"], sid, q)
        # word forms ("earn per week" / "weekly earnings"), a bare number of a phrase ("10" of "10-k") scores nothing
        self.assertEqual(packs._tokens("earn weekly"), packs._tokens("earnings week"))
        self.assertEqual(packs.route("What do women earn per week compared with men?", 3)[0]["source"], "bls")
        why = [m for r in packs.route("the 10-year yield as a share of GDP", 8) for m in r["matched"]]
        self.assertFalse([m for m in why if "one word of '10-k'" in m], why)
        self.assertIn("bea", [r["source"] for r in packs.route("Federal debt as a percent of GDP since 2000", 5)])

    def test_fingerprint_ignores_labels(self):
        a = package.fingerprint({"requests": [{"name": "x", "params": {"a": 1}}], "description": "one"})
        b = package.fingerprint({"requests": [{"name": "x", "params": {"a": 1}}], "description": "two"})
        self.assertEqual(a, b)


class TestTruncation(unittest.TestCase):
    """A row cap must never pass silently: every way of hitting max_rows is flagged."""

    def run_rows(self, n_rows, size, max_rows, kind="offset"):
        P = {"id": "t", "title": "T", "auth": {"required": "none", "placement": "none"}, "limits": {},
             "routes": {"api": {"base": "https://example.gov"}}}
        paging = ({"type": "offset", "limit_param": "limit", "offset_param": "offset", "size": size}
                  if kind == "offset" else {"type": "none"})
        eps = {"base_url": "https://example.gov",
               "endpoints": {"q": {"path": "/d.json", "records": "", "paging": paging}}}
        pack = Pack("t", "", P, eps, None)
        data = [{"i": str(i)} for i in range(n_rows)]
        with tempfile.TemporaryDirectory() as t:
            idx = {}
            if kind == "none":
                idx["GET https://example.gov/d.json"] = "all.json"
                with open(os.path.join(t, "all.json"), "w") as f:
                    json.dump(data, f)
            else:                              # answer any limit/offset the client asks for
                for off in range(0, n_rows + size + 1):
                    for lim in range(1, size + 1):
                        name = f"p_{off}_{lim}.json"
                        idx[f"GET https://example.gov/d.json?limit={lim}&offset={off}"] = name
                        with open(os.path.join(t, name), "w") as f:
                            json.dump(data[off:off + lim], f)
            with open(os.path.join(t, "index.json"), "w") as f:
                json.dump(idx, f)
            with mock.patch.dict(os.environ, {"DATA_ANALYST_FIXTURES": t}):
                c = Client("https://example.gov", name="t")
                return gr.fetch_request(c, eps["endpoints"]["q"], {}, max_rows)

    def test_cap_across_pages(self):
        rows, meta = self.run_rows(7, 3, 5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(meta["truncated"])

    def test_cap_smaller_than_page(self):
        """max_rows below the page size: the client asks for max_rows rows, gets them all, and must still
        flag that more may exist (this was missed before)."""
        rows, meta = self.run_rows(7, 6, 5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(meta["truncated"])

    def test_fewer_rows_than_cap(self):
        rows, meta = self.run_rows(3, 6, 5)
        self.assertEqual(len(rows), 3)
        self.assertFalse(meta["truncated"])

    def test_unpaged_endpoint_cut(self):
        rows, meta = self.run_rows(7, 0, 5, kind="none")
        self.assertEqual(len(rows), 5)
        self.assertTrue(meta["truncated"])
        rows, meta = self.run_rows(5, 0, 5, kind="none")
        self.assertFalse(meta["truncated"])

    def test_cli_reports_partial(self):
        """Through fetch.py: a capped request comes back as status partial with the reason, and its data folder
        is not reused as if complete."""
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        skill_fx = os.path.join(ROOT, "tests", "sources", "cdc", "fixtures", "r001.json")
        if not os.path.exists(skill_fx):
            skill_fx = os.path.join(SKILL, "sources", "cdc", "tests", "fixtures", "r001.json")
        rows = json.load(open(skill_fx, encoding="utf-8"))
        where = "state = 'New York' AND year = '2017'"
        with tempfile.TemporaryDirectory() as t:
            fx = os.path.join(t, "fx")
            os.makedirs(fx)
            import urllib.parse
            q = urllib.parse.urlencode({"$order": "deaths DESC", "$where": where, "$limit": 5, "$offset": 0})
            with open(os.path.join(fx, "index.json"), "w") as f:
                json.dump({f"GET https://data.cdc.gov/resource/bi63-dtpu.json?{q}": "p.json"}, f)
            with open(os.path.join(fx, "p.json"), "w") as f:
                json.dump(rows[:5], f)
            out = os.path.join(t, "out")
            os.makedirs(out)
            sp = os.path.join(out, "spec.json")
            with open(sp, "w") as f:
                json.dump({"requests": [{"name": "ny", "endpoint": "query", "max_rows": 5,
                                         "params": {"dataset": "leading_death", "$where": where, "$order": "deaths DESC"}}]}, f)
            env = {**os.environ, "DATA_ANALYST_FIXTURES": fx}
            p = subprocess.run([sys.executable, os.path.join(SKILL, "scripts", "fetch.py"), "--source", "cdc", sp,
                                "--out", out], capture_output=True, text=True, env=env)
            r = json.loads(p.stdout)
            self.assertEqual(r["status"], "partial", r)
            self.assertIn("stopped at 5 rows", r["truncated"][0])
            p = subprocess.run([sys.executable, os.path.join(SKILL, "scripts", "fetch.py"), "--source", "cdc", sp,
                                "--out", out, "--check"], capture_output=True, text=True, env=env)
            self.assertNotEqual(json.loads(p.stdout)["route"], "reuse_existing")


if __name__ == "__main__":
    unittest.main()


class TestGroupedQueryOrder(unittest.TestCase):
    def test_default_row_order_is_dropped_for_an_aggregate(self):
        """SoQL rejects ORDER BY :id on a grouped query (HTTP 400, live 2026-10); a spec's own $order stays."""
        from types import SimpleNamespace
        from core import generic_rest as gr
        pack = SimpleNamespace(endpoints={"endpoints": {"q": {"path": "/{dataset}.json", "defaults": {"$order": ":id", "$limit": 10},
                                                              "params": {"dataset": {"in": "path", "required": True}}}}})
        _, p, probs = gr.resolve(pack, {"name": "a", "endpoint": "q", "params": {"dataset": "x", "$group": "region"}})
        self.assertNotIn("$order", p)
        self.assertEqual(p["$limit"], 10)                     # other defaults stay
        _, p, _ = gr.resolve(pack, {"name": "b", "endpoint": "q", "params": {"dataset": "x", "$group": "region", "$order": "region"}})
        self.assertEqual(p["$order"], "region")
        _, p, _ = gr.resolve(pack, {"name": "c", "endpoint": "q", "params": {"dataset": "x"}})
        self.assertEqual(p["$order"], ":id")                  # ungrouped queries keep stable paging


class TestMinPageSize(unittest.TestCase):
    def test_page_size_never_below_the_api_minimum(self):
        """regulations.gov refuses page[size] < 5 (HTTP 400, live 2026-10); ask 5, keep only max_rows."""
        from core import generic_rest as gr

        class Resp:
            def __init__(self, n):
                self.n = n

            def json(self):
                return {"data": [{"i": i} for i in range(self.n)]}

        class Client:
            def __init__(self):
                self.sent = []

            def request(self, method, path, q, json_body=None):
                self.sent.append(dict(q))
                return Resp(int(q["per_page"]))
        ep = {"path": "/x", "records": "data",
              "paging": {"type": "page", "page_param": "page", "size_param": "per_page", "size": 100, "min_size": 7}}
        c = Client()
        rows, meta = gr.fetch_request(c, ep, {}, max_rows=3)
        self.assertEqual(c.sent[0]["per_page"], 7)
        self.assertEqual(len(rows), 3)


class TestCatalogParamList(unittest.TestCase):
    def test_a_list_for_a_catalog_param_does_not_crash(self):
        from types import SimpleNamespace
        from core import generic_rest as gr
        pack = SimpleNamespace(endpoints={"endpoints": {"q": {"path": "/x", "params": {"series": {"catalog": True}}}},
                                          "catalog": {"pop": {"id": "POP.TOTAL"}}})
        _, p, _ = gr.resolve(pack, {"name": "a", "endpoint": "q", "params": {"series": ["A.1", "B.2"]}})
        self.assertEqual(p["series"], ["A.1", "B.2"])
        _, p, _ = gr.resolve(pack, {"name": "b", "endpoint": "q", "params": {"series": "pop"}})
        self.assertEqual(p["series"], "POP.TOTAL")

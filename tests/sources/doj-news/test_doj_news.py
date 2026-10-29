import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from www.justice.gov/api/v1 on 2026-10-20 (re-recorded 2026-10-28: pages by sort=created).
SPEC = {"description": "DOJ press releases with fentanyl in the title on 2 June 2025; one release in full; blog entries since 2025",
        "requests": [{"name": "fentanyl_june_2025", "endpoint": "press_releases",
                      "params": {"parameters[title]": "\"fentanyl\"", "parameters[date]": "2025-06-02"}},
                     {"name": "one_release", "endpoint": "press_release",
                      "params": {"uuid": "2715a413-eb10-4d71-b4fd-9fd3e3eb0c0a",
                                 "fields": "uuid,title,date,component,topic,url,body"}},
                     {"name": "blog_since", "endpoint": "blog_entries", "params": {"since": "2025-01-01"}, "max_rows": 3}]}


class TestDojNews(PackTest):
    source = "doj-news"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rel = self.csv(out, "fentanyl_june_2025")
        self.assertEqual(len(rel), 7)
        self.assertTrue(all(x["date_iso"] == "2025-06-02" for x in rel))
        self.assertTrue(all("fentanyl" in x["title"].lower() for x in rel))
        self.assertEqual(rel[0]["date"], "1748865600")                 # published Unix timestamp kept
        self.assertTrue(any("USAO - Florida, Middle" in x["component_names"] for x in rel))
        self.assertNotIn("created", rel[0])                            # added only to page by: not kept
        one = self.csv(out, "one_release")[0]
        self.assertEqual(one["title"], "Bluefield Woman Pleads Guilty to COVID-19 Relief Fraud Scheme")
        self.assertNotIn("<p>", one["body_text"])
        self.assertIn("April Elick", one["body_text"])
        blog = self.csv(out, "blog_since")
        self.assertEqual(len(blog), 3)
        self.assertTrue(all(b["date_iso"] >= "2025-01-01" for b in blog))
        self.assertTrue(any("max_rows" in n and "posting time" in n for n in r["notes"]), r["notes"])

    def test_pre_2013_rows_get_coverage_note(self):
        # USAO releases are archived only from 2013: a fetch with older rows says counts across 2013 differ.
        # Fixture r004 recorded live 2026-10-22 (a 2012 day; Main Justice components only).
        old = {"description": "Releases with 'sentenced' in the title on 26 November 2012",
               "requests": [{"name": "sentenced_2012_11_26", "endpoint": "press_releases",
                             "params": {"parameters[title]": "sentenced", "parameters[date]": "2012-11-26"}}]}
        code, r, out = self.fetch(old)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "sentenced_2012_11_26")
        self.assertTrue(rows and all(x["date_iso"] < "2013-01-01" for x in rows))
        self.assertFalse(any("USAO" in x["component_names"] for x in rows))
        self.assertTrue(any("2013-01-01" in n and "not comparable" in n for n in r["notes"]), r["notes"])
        code, r, out = self.fetch(SPEC)                               # 2025 rows: no coverage note
        self.assertFalse(any("2013-01-01" in n for n in r["notes"]), r["notes"])

    def test_created_changed_time_tags_and_entities(self):
        # created/changed arrive as <time datetime="...">unix</time> HTML; titles carry entities (&#039;).
        # Fixture r005 recorded live 2026-10-22 (another day and title filter than the one that found it).
        spec = {"description": "Releases with 'Office' in the title on 14 March 2024, with web-publishing times",
                "requests": [{"name": "office_2024_03_14", "endpoint": "press_releases",
                              "params": {"parameters[title]": "Office", "parameters[date]": "2024-03-14",
                                         "fields": "uuid,title,date,created,changed,component,topic,url"}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "office_2024_03_14")
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(x["created"].startswith("<time") for x in rows))      # published value kept
        self.assertTrue(all(x["created_iso"] and x["changed_iso"] for x in rows))
        self.assertTrue(all(x["changed_iso"] >= x["created_iso"] >= "2024-03-14" for x in rows))
        self.assertTrue(any(x["changed_iso"] > "2024-03-14" for x in rows))       # later edits keep their day
        self.assertFalse(any("&#" in x["title"] for x in rows))
        self.assertTrue(any("Attorney's Office" in x["title"] for x in rows))

    def test_normalize_time_formats_unit(self):
        from testkit import load_pack_module
        ad = load_pack_module("doj-news")
        rows = [{"title": "Smith &amp; Sons&#039; Owner Sentenced", "date": "1504152000",
                 "created": '<time datetime="2017-08-31T22:15:00-04:00">1504232100</time>\n',
                 "changed": "1504232100",
                 "component": [{"name": "Office of Justice Programs &amp; Grants"}], "topic": ""}]
        out = ad.normalize({"params": {}}, {}, rows)[0]
        self.assertEqual(out["title"], "Smith & Sons' Owner Sentenced")
        self.assertEqual(out["date_iso"], "2017-08-31")
        self.assertEqual(out["created_iso"], "2017-08-31")    # the attribute's (Eastern) day, not UTC 09-01
        self.assertEqual(out["changed_iso"], "2017-09-01")    # bare digits: UTC day
        self.assertEqual(out["component_names"], "Office of Justice Programs & Grants")
        self.assertEqual(ad._iso("<time>1504232100</time>"), "2017-09-01")       # no attribute: the digits
        self.assertEqual(ad._iso("<time datetime=\"\">n/a</time>"), "")

    def test_date_order_pages_by_created_and_drops_repeats(self):
        # sort=date ties (one day stamp per day) shift between pages; the pack pages by created, puts rows in
        # date order, reads a margin past `since`, and drops a release that comes back twice, with a note.
        import json as _json
        from unittest import mock
        from testkit import load_pack_module
        from core import packs
        ad = load_pack_module("doj-news")
        pack = packs.load("doj-news")
        day, base = 86400, 1700000000                     # base: 2023-11-14
        # 160 releases, two a day over 80 days, each posted an hour or two after its date stamp
        recs = [{"uuid": f"u{i}", "title": f"t{i}", "date": str(base - (i // 2) * day),
                 "created": f'<time datetime="x">{base - (i // 2) * day + 3600 * (1 + i % 2)}</time>'}
                for i in range(160)]
        recs.sort(key=lambda x: ad._ts(x["created"]), reverse=True)
        sent = []

        class Resp:
            def __init__(self, d):
                self.d = d
            def json(self):
                return _json.loads(_json.dumps(self.d))

        class Client:
            def request(self, method, path, q):
                sent.append(dict(q))
                p, n = q["page"], q["pagesize"]
                page = recs[p * n:(p + 1) * n]
                if p == 1:
                    page = [recs[n - 1]] + page[:-1]          # one release repeated at a page boundary
                return Resp({"results": page, "metadata": {"resultset": {"count": len(recs)}}})

        spec = {"requests": [{"name": "r", "endpoint": "press_releases",
                              "params": {"since": "2023-10-05", "fields": "uuid,title,date"}}]}
        with mock.patch.object(ad.gr, "client_for", lambda pack: Client()):
            tables, notes = ad.fetch(spec, pack, "api")
        self.assertTrue(all(q["sort"] == "created" and "created" in q["fields"] for q in sent), sent[0])
        rows = tables["r"]
        self.assertEqual(len({x["uuid"] for x in rows}), len(rows))
        self.assertTrue(rows and all(x["date_iso"] >= "2023-10-05" for x in rows))
        self.assertEqual([x["date"] for x in rows], sorted((x["date"] for x in rows), reverse=True))
        self.assertNotIn("created", rows[0])
        self.assertTrue(any("came back twice" in n for n in notes), notes)
        self.assertEqual(len(sent), 3)        # page 2 lies past `since`, within the margin: read for early postings

    def test_bad_since_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "press_releases", "params": {"since": "June 2025"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_since_needs_newest_first(self):
        bad = {"requests": [{"name": "x", "endpoint": "press_releases",
                             "params": {"since": "2025-01-01", "direction": "ASC"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_manual_bundle_drops_pack_options(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = json.load(f)["items"]
        blog = [i for i in items if i["name"].startswith("blog_since")]
        self.assertTrue(blog)
        self.assertNotIn("since=", blog[0]["url"])
        self.assertIn("page=0", blog[0]["url"])
        self.assertIn("ingested", blog[0]["note"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the govinfo pack. Fixtures were recorded live from api.govinfo.gov (October 2026)
with DATA_ANALYST_RECORD; the client drops api_key from every recorded URL."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", ".."),                                      # tests/ (testkit)
                os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "scripts")]
from testkit import PackTest, pack_dir# noqa: E402

SPEC = {"description": "Stablecoin bills in the 118th Congress, recent budget office publications, and the text of one law",
        "requests": [
            {"name": "stablecoin_bills", "endpoint": "search",
             "params": {"collection": "BILLS", "congress": 118, "phrase": "payment stablecoin", "sort": "newest"}},
            {"name": "cbo_recent", "endpoint": "search",
             "params": {"collection": "GOVPUB", "publisher": "Congressional Budget Office", "date_from": "2026-06-01",
                        "sort": "newest"}},
            {"name": "law_text", "endpoint": "text", "params": {"packageId": "PLAW-118publ201"}}]}
SECTIONS_SPEC = {"description": "Sections of two 2024 laws and the U.S. Code sections the first amends",
                 "requests": [
                     {"name": "performance_act", "endpoint": "sections", "params": {"packageId": "PLAW-118publ190"}},
                     {"name": "clinic_naming", "endpoint": "sections",
                      "params": {"packageId": "PLAW-118publ200", "max_chars": 200}}]}


def adapter():
    sys.path.insert(0, pack_dir("govinfo"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("govinfo_adapter_t", os.path.join(pack_dir("govinfo"), "adapter.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestGovinfo(PackTest):
    source = "govinfo"
    sample_spec = SPEC

    def test_check_api_keyless(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        bills = self.csv(out, "stablecoin_bills")
        self.assertEqual(len(bills), 9)
        # the all-words count (r010, 2026-10-28) is 10: not well above 9, so no note
        self.assertFalse(any("quoted search returned" in n for n in r["notes"]), r["notes"])
        self.assertEqual(bills[0]["packageId"], "BILLS-118hr10561ih")
        self.assertTrue(all(b["collectionCode"] == "BILLS" for b in bills))
        pubs = self.csv(out, "cbo_recent")
        self.assertEqual(pubs[0]["governmentAuthor"], "Congressional Budget Office")
        text = self.csv(out, "law_text")[0]
        self.assertEqual(text["date_issued"], "2024-12-23")
        self.assertIn("Public Law 118-201", text["text"])
        self.assertNotIn("<pre>", text["text"])
        self.assertEqual(text["text_status"], "full")
        self.assertEqual(text["pdf_url"], "https://www.govinfo.gov/content/pkg/PLAW-118publ201/pdf/PLAW-118publ201.pdf")
        self.assertFalse(any("no usable text" in n for n in r["notes"]), r["notes"])

    def test_query_built_and_escaped(self):
        a = adapter()
        q = a.build_query({"collection": "BILLS", "congress": 118, "text": 'tax:credit (child) "AND" \\ OR'})
        self.assertEqual(q, "collection:BILLS AND congress:118 AND (tax credit child)")
        q = a.build_query({"collection": ["PLAW", "STATUTE"], "phrase": 'say "hi"', "date_from": "2024-01-01"})
        self.assertEqual(q, '(collection:PLAW OR collection:STATUTE) AND publishdate:range(2024-01-01,) AND ("say hi")')
        q = a.build_query({"all_words": 'say "hi"'})
        self.assertEqual(q, '(say hi)')
        # the title search (not a full-text phrase search): escaped, quoted for several words
        q = a.build_query({"collection": "BILLS", "congress": 118, "title": 'harbor: "dredging"'})
        self.assertEqual(q, 'collection:BILLS AND congress:118 AND title:"harbor dredging"')
        self.assertEqual(a.build_query({"title": "harbor"}), "title:harbor")
        self.assertEqual(a.check_spec({"requests": [{"name": "t", "endpoint": "search", "params": {"title": "harbor"}}]}), [])
        self.assertEqual(a.bill_package_id({"congress": 117, "billtype": "HR", "number": 5376}), "BILLS-117hr5376enr")

    def test_word_forms_and_one_word_phrases(self):
        # live 2026-10-28 (CHRG, 2024): quoted "tariff" matched 44 hearings, unquoted tariff 92: quoting one word only
        # loses documents, so a one-word phrase goes unquoted, and `any` ORs every form of a term
        a = adapter()
        self.assertEqual(a.build_query({"collection": "CRPT", "phrase": " levy "}), "collection:CRPT AND (levy)")
        self.assertEqual(a.build_query({"collection": "CRPT", "any": ["levy", "levies", "flat tax:rate"]}),
                         'collection:CRPT AND (levy OR levies OR "flat tax rate")')
        self.assertEqual(a.build_query({"any": "levy; levies"}), "(levy OR levies)")
        self.assertEqual(a.check_spec({"requests": [{"name": "s", "endpoint": "search",
                                                      "params": {"collection": "CRPT", "any": ["levy", "levies"]}}]}), [])
        self.assertTrue(a.check_spec({"requests": [{"name": "s", "endpoint": "search", "params": {"any": ["::"]}}]}))

    def test_quoted_phrase_far_below_all_words_is_noted(self):
        # live 2026-10-28: a two-word phrase quoted matched 58 bills of one Congress, title:"..." 110: the quoted
        # search can miss documents, so one all-words count is compared with what the phrase returned
        from unittest import mock
        from core import packs
        a, pack, sent = adapter(), packs.load("govinfo"), []

        class Resp:
            def __init__(self, n):
                self.n = n
            def json(self):
                return {"count": self.n}

        class Client:
            def __init__(self, n):
                self.n = n
            def request(self, method, path, params=None, json_body=None):
                sent.append(json_body)
                return Resp(self.n)
        req = {"name": "s", "endpoint": "search", "params": {"collection": "BILLS", "phrase": "flat tax"}}
        with mock.patch.object(a.gr, "client_for", lambda pack: Client(140)):
            note = a.phrase_count_note(pack, req, 40)
        self.assertIn("quoted search returned 40 documents, under half of the 140", note)
        self.assertIn("may be missing documents", note)
        self.assertIn("all_words", note)
        self.assertEqual((sent[0]["query"], sent[0]["pageSize"]), ("collection:BILLS AND (flat tax)", 1))
        with mock.patch.object(a.gr, "client_for", lambda pack: Client(70)):
            self.assertIsNone(a.phrase_count_note(pack, req, 40))          # a phrase is rarer than its words: no note
        with mock.patch.object(a.gr, "client_for", lambda pack: Client(14)):
            self.assertIsNone(a.phrase_count_note(pack, req, 4))           # under half, but only 10 short: no note
        self.assertIsNone(a.phrase_count_note(pack, {**req, "params": {"phrase": "levy"}}, 3))   # one word: no call

    def test_dateissued_in_query_is_fix_spec(self):
        # GovInfo answers HTTP 500 to dateIssued: inside the query every time (live 2026-10-28)
        bad = {"requests": [{"name": "x", "endpoint": "search",
                             "params": {"collection": "CRPT", "query": "levy AND dateIssued:range(2023-01-01,2023-12-31)"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        self.assertIn("date_from", r["message"])

    def test_unknown_collection_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "search", "params": {"collection": "NOPE", "text": "tax"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_wildcard_search_is_refused(self):
        # GovInfo answers "title:tariff*" with HTTP 500 [live 2026-10-22]; list the word forms instead
        a = adapter()
        p = a.check_spec({"requests": [{"name": "s", "endpoint": "search", "params": {
            "query": "title:tariff*", "collection": "BILLS"}}]})
        self.assertTrue(any("wildcard" in x for x in p), p)
        self.assertEqual(a.check_spec({"requests": [{"name": "s", "endpoint": "search", "params": {
            "query": "title:tariff OR title:tariffs", "collection": "BILLS"}}]}), [])

    def test_dates_outside_a_closed_collection(self):
        a = adapter()                                     # GAOREPORTS ends in 2008: a 2025 window finds nothing
        p = a.check_spec({"requests": [{"name": "g", "endpoint": "published", "params": {
            "collection": "GAOREPORTS", "start_date": "2025-01-01", "end_date": "2025-12-31"}}]})
        self.assertTrue(any("covers 1989-2008" in x for x in p), p)   # 1989-06-22 to 2008-09-18 (live 2026-10-22)
        self.assertEqual(a.check_spec({"requests": [{"name": "g", "endpoint": "published", "params": {
            "collection": "GAOREPORTS", "start_date": "2005-01-01", "end_date": "2005-12-31"}}]}), [])
        self.assertEqual(a.check_spec({"requests": [{"name": "g", "endpoint": "published", "params": {
            "collection": "GAOREPORTS", "start_date": "1990-01-01", "end_date": "1992-12-31"}}]}), [])

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = {i["filename"]: i for i in json.load(f)["items"]}
        self.assertEqual(items["law_text.htm"]["url"],
                         "https://www.govinfo.gov/content/pkg/PLAW-118publ201/html/PLAW-118publ201.htm")
        self.assertIn("collection:BILLS", items["stablecoin_bills.json"]["body"]["query"])

    def test_sections(self):
        code, r, out = self.fetch(SECTIONS_SPEC)
        self.assertEqual(code, 0, r)
        secs = self.csv(out, "performance_act")
        self.assertEqual(len(secs), 8)
        self.assertEqual(secs[0]["identifier"], "/us/pl/118/190/s1")
        self.assertEqual(secs[0]["usc_classified"], "31 U.S.C. 1101")       # the "31 USC 1101 note" margin note
        self.assertEqual(secs[3]["usc_amended"], "31 U.S.C. 1120(a)(2)")
        self.assertEqual(secs[2]["usc_amended"], "31 U.S.C. 1122")
        self.assertTrue(all(s["num"] == str(i + 1) for i, s in enumerate(secs)))   # nothing from <quotedContent>
        self.assertIn("REPORTING. (a)", secs[1]["text"])
        clinic = self.csv(out, "clinic_naming")
        self.assertEqual(len(clinic), 1)
        self.assertEqual((clinic[0]["usc_amended"], clinic[0]["usc_cited"]), ("", ""))
        self.assertEqual(len(clinic[0]["text"]), 200)

    def test_unmarked_amending_action_counts_as_amended(self):
        # PL 119-1 sec. 3(b): "Section 236 of the INA (8 U.S.C. 1226), as amended by this Act, is further amended"
        # carries no <amendingAction> tag in GPO's USLM
        import importlib.util
        spec = importlib.util.spec_from_file_location("govinfo_adapter_t", os.path.join(pack_dir("govinfo"), "adapter.py"))
        a = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(a)
        xml = ('<lawDoc xmlns="http://schemas.gpo.gov/xml/uslm"><main><section identifier="/us/pl/119/1/s3">'
               '<num value="3">SEC. 3.</num><subsection><chapeau>Section 236 of the Act (<ref href="/us/usc/t8/s1226">'
               '8 U.S.C. 1226</ref>), as amended by this Act, is further amended</chapeau></subsection>'
               '<subsection><chapeau>See also <ref href="/us/usc/t8/s1101">8 U.S.C. 1101</ref>.</chapeau></subsection>'
               '</section></main></lawDoc>')
        row = a.uslm_sections("PLAW-119publ1", xml)[0]
        self.assertEqual((row["usc_amended"], row["usc_cited"]), ("8 U.S.C. 1226", "8 U.S.C. 1101"))
        # Daily Digest renditions carried NUL characters in 2025
        self.assertEqual(a.html_to_text("<pre>(Vote No.\x00 7)</pre>"), "(Vote No. 7)")
        self.assertEqual(a._calls({"requests": [{"endpoint": "text", "params": {"packageId": "C", "granuleId": "G"}}]}), 2)

    def test_pdf_only_placeholder_is_flagged(self):
        # GPO serves a cover page plus "[TEXT NOT AVAILABLE REFER TO PDF]" as the text of a document it did not
        # convert [live 2026-10-28]: the row must say so, and the fetch must say which rows were not read
        a = adapter()
        cover = "[House Hearing]\n\nA SAMPLE HEARING TITLE\n\nHEARING BEFORE THE COMMITTEE\n\n"
        stub = cover + "[TEXT NOT AVAILABLE REFER TO PDF]"
        body = "Witness testimony on the sample topic. " * 2000
        self.assertEqual(a.text_status(stub, 120), "pdf_only")
        self.assertEqual(a.text_status(stub, ""), "pdf_only")
        self.assertEqual(a.text_status(body + "[TEXT NOT AVAILABLE REFER TO PDF]" + body, 40), "partial_pdf")
        self.assertEqual(a.text_status(cover, 200), "near_empty")      # no marker, but a stub for 200 pages
        self.assertEqual(a.text_status(cover, 1), "full")              # a short one-page document is fine
        self.assertEqual(a.text_status(body + "[GRAPHIC NOT AVAILABLE IN TIFF FORMAT]", 30), "full")
        rows = {"HX-1": stub, "HX-2": body, "HX-3": cover}

        def fake_text(pack, req):
            pid = req["params"]["packageId"]
            return [a._text_row(pid, {"pages": 150}, rows[pid], "link", None)]
        a._fetch_text = fake_text
        spec = {"requests": [{"name": f"hearing_text#{i}", "endpoint": "text", "_group": "hearing_text",
                              "params": {"packageId": pid}} for i, pid in enumerate(rows)]}
        tables, notes = a.fetch(spec, None, "api")
        self.assertEqual([t[0]["text_status"] for t in tables.values()], ["pdf_only", "full", "near_empty"])
        bad = [n for n in notes if "no usable text" in n]
        self.assertEqual(len(bad), 1, notes)                             # one note for the for_each group
        self.assertTrue(bad[0].startswith("hearing_text: incomplete: 2 of 3 rows"), bad)
        self.assertIn("HX-1, HX-3", bad[0])
        self.assertIn("pdf_url", bad[0])
        self.assertEqual(tables["hearing_text#0"][0]["pdf_url"],
                         "https://www.govinfo.gov/content/pkg/HX-1/pdf/HX-1.pdf")

    def test_sections_without_uslm(self):
        a = adapter()                                   # bills carry the older bill XML, not USLM [live 2026-10-25]
        p = a.check_spec({"requests": [{"name": "b", "endpoint": "sections", "params": {"packageId": "BILLS-118hr4763eh"}}]})
        self.assertTrue(any("use endpoint text" in x for x in p), p)
        old = {"requests": [{"name": "old_law", "endpoint": "sections", "params": {"packageId": "PLAW-112publ1"}}]}
        code, r, _ = self.fetch(old)                    # USLM starts with the 113th Congress: GovInfo answers HTTP 400
        self.assertNotEqual(code, 0)
        self.assertIn("113th Congress", r["message"])

    def test_sections_upload(self):
        a = adapter()
        req = {"name": "u", "endpoint": "sections", "params": {"packageId": "PLAW-118publ200"}}
        rows = a.parse_upload(None, req, [os.path.join(HERE, "fixtures", "r008.xml")])
        self.assertEqual(rows[0]["identifier"], "/us/pl/118/200/s1")
        self.assertEqual(a.usc_cite("/us/usc/t16/s666c–1/p"), "16 U.S.C. 666c–1(p)")
        self.assertEqual(a.usc_cite("/us/usc/t31/ch11"), "31 U.S.C. ch. 11")

    def test_published_end_date_inclusive(self):
        # GovInfo's /published end date is exclusive [live 2026-10-21]; the spec's is inclusive
        from testkit import load_pack_module
        ad = load_pack_module("govinfo")
        ep = {"path": "/published/{start_date}/{end_date}"}
        p = ad.before_request({}, ep, {"start_date": "2025-01-01", "end_date": "2025-12-31", "collection": "FR"})
        self.assertEqual(p["end_date"], "2026-01-01")
        self.assertEqual(ad.before_request({}, {"path": "/packages/{packageId}/summary"}, {"x": 1}), {"x": 1})


if __name__ == "__main__":
    unittest.main()

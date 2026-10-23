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
        self.assertEqual(bills[0]["packageId"], "BILLS-118hr10561ih")
        self.assertTrue(all(b["collectionCode"] == "BILLS" for b in bills))
        pubs = self.csv(out, "cbo_recent")
        self.assertEqual(pubs[0]["governmentAuthor"], "Congressional Budget Office")
        text = self.csv(out, "law_text")[0]
        self.assertEqual(text["date_issued"], "2024-12-23")
        self.assertIn("Public Law 118-201", text["text"])
        self.assertNotIn("<pre>", text["text"])

    def test_query_built_and_escaped(self):
        a = adapter()
        q = a.build_query({"collection": "BILLS", "congress": 118, "text": 'tax:credit (child) "AND" \\ OR'})
        self.assertEqual(q, "collection:BILLS AND congress:118 AND (tax credit child)")
        q = a.build_query({"collection": ["PLAW", "STATUTE"], "phrase": 'say "hi"', "date_from": "2024-01-01"})
        self.assertEqual(q, '(collection:PLAW OR collection:STATUTE) AND publishdate:range(2024-01-01,) AND ("say hi")')
        self.assertEqual(a.bill_package_id({"congress": 117, "billtype": "HR", "number": 5376}), "BILLS-117hr5376enr")

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

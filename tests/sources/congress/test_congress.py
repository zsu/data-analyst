"""Offline tests for the congress pack. Fixtures were recorded live (October 2026) from api.congress.gov,
clerk.house.gov and www.senate.gov with DATA_ANALYST_RECORD; the client drops api_key from recorded URLs.
The vote XML files are whole official files (a House roll call lists every member, so they are ~95 KB)."""
import importlib.util
import json
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", ".."),                                      # tests/ (testkit)
                os.path.join(HERE, "..", "..", "..", "plugins", "data-analyst", "skills", "data-analyst", "scripts")]
from testkit import PackTest, pack_dir# noqa: E402

B = {"congress": 119, "billType": "s", "billNumber": 5}
SPEC = {"description": "Laken Riley Act (119th S. 5): bill, cosponsors and every recorded vote; one House and one "
                       "Senate roll call; a 1989 Senate vote list; cost-estimate links for H.R. 5376 (117th); recent "
                       "CRS reports; Maryland members of the 118th Congress",
        "requests": [
            {"name": "s5_bill", "endpoint": "bill", "params": B},
            {"name": "s5_cosponsors", "endpoint": "bill_cosponsors", "params": B},
            {"name": "s5_votes", "endpoint": "bill_votes", "params": B},
            {"name": "ira_senate_passage", "endpoint": "roll_call", "params": {"chamber": "senate", "congress": 117, "session": 2, "roll": 325}},
            {"name": "ira_house_passage", "endpoint": "roll_call", "params": {"chamber": "house", "year": 2022, "roll": 420}},
            {"name": "senate_votes_1989", "endpoint": "senate_votes", "params": {"congress": 101, "session": 1}},
            {"name": "hr5376_cost_estimates", "endpoint": "bill_cbo_estimates", "params": {"congress": 117, "billType": "hr", "billNumber": 5376}},
            {"name": "crs_recent", "endpoint": "crs_reports", "max_rows": 5},
            {"name": "md_members_118", "endpoint": "members_state", "params": {"congress": 118, "stateCode": "MD", "currentMember": "false"}}]}


def adapter():
    spec = importlib.util.spec_from_file_location("congress_adapter_t", os.path.join(pack_dir("congress"), "adapter.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestCongress(PackTest):
    source = "congress"
    sample_spec = SPEC

    def test_check_api_keyless(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        bill = self.csv(out, "s5_bill")[0]
        self.assertEqual(bill["latestAction.text"], "Became Public Law No: 119-1.")
        cos = self.csv(out, "s5_cosponsors")
        self.assertEqual(len(cos), 53)
        self.assertIn("party", cos[0])
        votes = {(v["chamber"], v["roll"]): v for v in self.csv(out, "s5_votes")}
        self.assertEqual(len(votes), 5)
        house = votes[("house", "23")]
        self.assertEqual((house["yea"], house["nay"], house["yea_D"], house["yea_R"]), ("263", "156", "46", "217"))
        passage = votes[("senate", "7")]
        self.assertEqual((passage["yea"], passage["nay"], passage["yea_D"], passage["nay_R"]), ("64", "35", "12", "0"))
        sen = self.csv(out, "ira_senate_passage")
        self.assertEqual(len(sen), 100)
        self.assertEqual(sum(1 for m in sen if m["position"] == "yea"), 50)
        self.assertEqual(sen[0]["date"], "2022-08-07")
        hou = self.csv(out, "ira_house_passage")
        self.assertEqual(sum(1 for m in hou if m["position"] == "yea" and m["party"] == "D"), 220)
        self.assertEqual(hou[0]["member_id"], "A000370")
        menu = self.csv(out, "senate_votes_1989")
        self.assertEqual(len(menu), 312)
        self.assertEqual(menu[0]["date"], "1989-11-21")
        cbo = self.csv(out, "hr5376_cost_estimates")
        self.assertTrue(all(c["url"].startswith("https://www.cbo.gov/") for c in cbo))
        self.assertEqual(len(self.csv(out, "crs_recent")), 5)
        md = self.csv(out, "md_members_118")
        self.assertEqual({m["state"] for m in md}, {"Maryland"})

    def test_current_congress(self):
        a = adapter()
        self.assertEqual(a.current_congress(date(2025, 1, 2)), 118)
        self.assertEqual(a.current_congress(date(2025, 1, 3)), 119)
        self.assertEqual(a.current_congress(date(2026, 12, 31)), 119)
        self.assertEqual(a.current_congress(date(2027, 1, 3)), 120)
        self.assertEqual(a.current_congress(date(1933, 3, 3)), 72)
        self.assertEqual(a.ordinal(121), "121st")
        self.assertEqual(a.ordinal(112), "112th")
        self.assertEqual(a.session_year(117, 2), 2022)

    def test_sort_plus_is_sent_as_space(self):
        """Congress.gov ignores sort=introducedDate%2Basc silently; a literal + (space) sorts (live 2026-10)."""
        import urllib.parse
        from core import generic_rest as gr
        from testkit import load_pack_module
        from core.packfiles import read_json
        from testkit import pack_dir
        eps = read_json(pack_dir("congress"), "endpoints.json")["endpoints"]
        a = load_pack_module("congress")
        p = a.before_request({}, eps["bills_congress"], {"congress": 116, "sort": "updateDate+asc"})
        path, query, _ = gr.split(eps["bills_congress"], p)
        self.assertEqual(path, "/bill/116")
        self.assertIn("sort=updateDate+asc", urllib.parse.urlencode(query))
        self.assertIn("introducedDate+desc", eps["bills"]["params"]["sort"]["enum"])
        cb = eps["committee_bills_congress"]
        path, _, _ = gr.split(cb, a.before_request({}, cb, {"congress": 116, "chamber": "senate",
                                                            "committeeCode": "ssfi00"}))
        self.assertEqual(path, "/committee/116/senate/ssfi00/bills")

    def test_title_contains_matches_the_phrase_as_given(self):
        a = adapter()
        m = a.title_matcher("water rights")
        self.assertTrue(m("Tribal Water Rights Settlement Act"))
        self.assertTrue(m("A bill on water-rights adjudication"))          # punctuation between words is fine
        self.assertFalse(m("Rights of Water Users Act"))                   # word order matters
        self.assertFalse(m("Freshwater Rights Act"))                       # each word matches at a word start
        self.assertTrue(m("Waters Rightsholders Act"))                     # plurals and longer forms still match
        one = a.title_matcher("port")
        self.assertFalse(one("Rural Support Act"))
        self.assertTrue(a.title_matcher("veteran")("Veterans Housing Act"))
        many = a.title_matcher(["levy", "levies"])
        self.assertTrue(many("Special Levies Act") and many("Levy Relief Act"))
        self.assertIsNone(a.title_matcher("  "))
        rows = [{"title": "Port Infrastructure Act"}, {"title": "Passport Fee Act"}]
        self.assertEqual(a.normalize({"params": {"title_contains": "port"}}, {}, rows), rows[:1])

    def test_multi_word_title_phrase_that_keeps_nothing_explains_word_order(self):
        a = adapter()
        self.assertIsNone(a.phrase_order_hint({"params": {"title_contains": "harbor"}}))
        h = a.phrase_order_hint({"params": {"title_contains": ["harbor", "dredging permits"]}})
        self.assertIn("'dredging permits'", h)
        self.assertIn("in that order", h)
        self.assertIn("list each form", h)
        from unittest import mock
        from core import packs
        from core.http import DataError
        pack = packs.load("congress")
        r = {"name": "b", "endpoint": "bills", "params": {"title_contains": "dredging permits"}}

        def empty(pack, spec, client=None):
            raise DataError("request(s) 'b' returned no rows: check the filter values", kind="bad_request")
        with mock.patch.object(a.gr, "fetch", empty):
            with self.assertRaises(DataError) as cm:
                a.fetch({"requests": [r]}, pack, "api")
        self.assertIn("in that order", str(cm.exception))
        with mock.patch.object(a.gr, "fetch", lambda pack, spec, client=None: ({"b": []}, [])):
            _, notes = a.fetch({"requests": [{**r, "allow_empty": True}]}, pack, "api")
        self.assertTrue(any("no titles matched" in n and "in that order" in n for n in notes), notes)

    def test_title_contains_says_display_titles_only_and_gives_the_title_search(self):
        # the list's title is the display title: a bill whose official title alone holds the phrase is missed, so
        # every such fetch says so and names the GovInfo title search plus bill_titles as the follow-up
        from unittest import mock
        from core import packs
        a = adapter()
        pack = packs.load("congress")
        rows = [{"title": "Harbor Dredging Act", "number": "12", "type": "HR"}]
        spec = {"requests": [{"name": "b", "endpoint": "bills_congress",
                              "params": {"congress": 118, "title_contains": "harbor dredging"}},
                             {"name": "plain", "endpoint": "bills_congress", "params": {"congress": 118}}]}
        with mock.patch.object(a.gr, "fetch", lambda pack, spec, client=None: ({spec["requests"][0]["name"]: rows}, [])):
            _, notes = a.fetch(spec, pack, "api")
        self.assertEqual(len(notes), 1, notes)                            # none for the list without the filter
        n = notes[0]
        self.assertTrue(n.startswith("b: title_contains matched the list's display title only"), n)
        self.assertIn('{"collection": "BILLS", "congress": 118, "title": "harbor dredging"}', n)
        self.assertIn("bill_titles", n)
        self.assertIn("missing", n)                                       # a caveat word: the hand-off always shows it
        law = a.display_title_note({"name": "l", "endpoint": "laws", "params": {"congress": "current",
                                                                                "title_contains": ["levy", "levies"]}})
        self.assertIn('"collection": "PLAW", "congress": <the Congress number>, "title": "levies"', law)
        self.assertIsNone(a.display_title_note({"name": "c", "endpoint": "crs_reports",
                                                "params": {"title_contains": "harbor"}}))

    def test_senate_roll_call_needs_session(self):
        bad = {"requests": [{"name": "x", "endpoint": "roll_call", "params": {"chamber": "senate", "roll": 5}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_refused_senate_host_is_found_at_check(self):
        # senate.gov answered scripts with HTTP 403 on 2026-10-27 while api.congress.gov answered
        from unittest import mock
        from core.http import DataError
        a = adapter()

        class Refuses:
            def get(self, *_a, **_k):
                raise DataError("senate-lis: HTTP 403 from www.senate.gov: the host refused", kind="blocked")
        senate = {"requests": [{"name": "v", "endpoint": "bill_votes",
                                "params": {"congress": 119, "billType": "hr", "billNumber": 1}}]}
        house = {"requests": [{"name": "v", "endpoint": "bill_votes",
                               "params": {"congress": 119, "billType": "hr", "billNumber": 1, "chamber": "house"}}]}
        with mock.patch.object(a, "_client", lambda *_a: Refuses()):
            self.assertIn("chamber", a.refused_hosts(senate))
            self.assertEqual(a.refused_hosts(house), "")

    def test_manual_bundle(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            items = {i["filename"]: i for i in json.load(f)["items"]}
        self.assertEqual(items["ira_house_passage.xml"]["url"], "https://clerk.house.gov/evs/2022/roll420.xml")
        self.assertFalse(items["ira_house_passage.xml"]["needs_key"])
        self.assertIn("api_key=DEMO_KEY", items["s5_bill.json"]["url"])
        self.assertTrue(all(i["url"].count("api_key=") <= 1 for i in items.values()))   # once per link

    def test_ingest_roll_call_xml(self):
        sp, out = self.write_spec({"requests": [SPEC["requests"][3]]})
        code, r = self.cli(sp, "--out", out, "--ingest", os.path.join(self.fixtures, "r009.xml"))
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "ira_senate_passage")
        self.assertEqual(len(rows), 100)

    def test_roll_call_summary_row(self):
        # summary=true: one row for the vote (question, result, totals), e.g. to classify a session's House votes
        req = {"name": "h", "endpoint": "roll_call", "params": {"chamber": "house", "year": 2022, "roll": 420,
                                                                "summary": "true"}}
        sp, out = self.write_spec({"requests": [req]})
        code, r = self.cli(sp, "--out", out, "--ingest", os.path.join(self.fixtures, "r008.xml"))
        self.assertEqual(code, 0, r)
        rows = self.csv(out, "h")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["question"], rows[0]["chamber"]), ("On Passage", "house"))

    def test_senate_menu_en_bloc(self):
        # an en bloc vote nests issue, question and result under <en_bloc><matter> (as in vote_menu_119_1.xml)
        import xml.etree.ElementTree as ET
        spec = importlib.util.spec_from_file_location("congress_adapter", os.path.join(pack_dir("congress"), "adapter.py"))
        ad = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ad)
        xml = ("<vote_summary><congress>119</congress><session>1</session><congress_year>2025</congress_year><votes>"
               "<vote><vote_number>00655</vote_number><vote_date>18-Dec</vote_date><en_bloc>"
               "<matter><issue>PN416-9</issue><question>On the Nomination</question><result>Confirmed</result></matter>"
               "<matter><issue>PN141-12</issue><question>On the Nomination</question><result>Confirmed</result></matter>"
               "</en_bloc><vote_tally><yeas>53</yeas><nays>43</nays></vote_tally><title>En bloc</title></vote>"
               "<vote><vote_number>00656</vote_number><vote_date>18-Dec</vote_date><issue>PN499-11</issue>"
               "<question>On the Cloture Motion\n </question><result>Agreed to</result>"
               "<vote_tally><yeas>53</yeas><nays>43</nays></vote_tally><title>Cloture</title></vote></votes></vote_summary>")
        rows = ad.parse_senate_menu(ET.fromstring(xml), "menu.xml")
        self.assertEqual((rows[0]["issue"], rows[0]["question"], rows[0]["result"], rows[0]["en_bloc"]),
                         ("PN416-9; PN141-12", "On the Nomination", "Confirmed", "2"))
        self.assertEqual((rows[1]["question"], rows[1]["en_bloc"]), ("On the Cloture Motion", ""))


if __name__ == "__main__":
    unittest.main()

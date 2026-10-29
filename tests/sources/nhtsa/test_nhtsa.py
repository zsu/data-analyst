import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from api.nhtsa.gov and vpic.nhtsa.dot.gov on 2026-10-20.
SPEC = {"description": "2019 Honda Civic: recalls, NCAP rating, VIN decode",
        "requests": [{"name": "civic_2019_recalls", "endpoint": "recalls_by_vehicle",
                      "params": {"make": "honda", "model": "civic", "modelYear": 2019}},
                     {"name": "civic_2019_sedan_rating", "endpoint": "ratings_detail", "params": {"vehicleId": 14009}},
                     {"name": "vin", "endpoint": "vin_decode", "params": {"vin": "1HGCM82633A123456"}},
                     {"name": "odi", "endpoint": "complaint_odi", "params": {"odinumber": 11675912}}]}


class TestNhtsa(PackTest):
    source = "nhtsa"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        rec = self.csv(out, "civic_2019_recalls")
        self.assertEqual(len(rec), 4)
        first = rec[0]
        self.assertEqual(first["NHTSACampaignNumber"], "20V314000")
        self.assertEqual(first["ReportReceivedDate"], "28/05/2020")       # published DD/MM/YYYY kept
        self.assertEqual(first["ReportReceivedDate_iso"], "2020-05-28")   # added column
        rating = self.csv(out, "civic_2019_sedan_rating")[0]
        self.assertEqual(rating["OverallRating"], "5")
        self.assertEqual(rating["RolloverPossibility"], "0.095")
        vin = self.csv(out, "vin")[0]
        self.assertEqual(vin["Make"], "HONDA")
        self.assertEqual(vin["ModelYear"], "2003")
        odi = self.csv(out, "odi")[0]
        self.assertEqual(odi["dateComplaintFiled_iso"], "2025-07-24")     # complaints are MM/DD/YYYY

    def test_missing_param_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "recalls_by_vehicle", "params": {"make": "honda", "model": "civic"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_year_range_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "recalls_by_vehicle",
                             "params": {"make": "honda", "model": "civic", "modelYear": "2018-2020"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)

    def test_variants_fetched_and_unmatched_name_is_a_note(self):
        # Fixtures r005-r013 recorded live 2026-10-28 (r005/r009: the full model lists, max=10000): by default only the
        # name asked for is fetched and the other listed names are named in a note; "variants": true adds only
        # the powertrain/body versions. A name NHTSA does not know (HTTP 400) is an empty table with a note.
        base = {"make": "honda", "model": "accord", "modelYear": 2023}
        spec = {"description": "Recalls for a 2023 sedan; a misspelled model",
                "requests": [{"name": "sedan_2023", "endpoint": "recalls_by_vehicle", "params": base},
                             {"name": "typo", "endpoint": "recalls_by_vehicle",
                              "params": {"make": "honda", "model": "zzqx", "modelYear": 2023}},
                             {"name": "odi", "endpoint": "complaint_odi", "params": {"odinumber": 11675912}}]}
        code, r, out = self.fetch(spec)
        self.assertEqual(code, 0, r)
        self.assertEqual({x["model_requested"] for x in self.csv(out, "sedan_2023")}, {"accord"})
        self.assertTrue(any(n.startswith("sedan_2023: NHTSA also lists ACCORD HYBRID, ACCORD SEDAN") for n in r["notes"]),
                        r["notes"])
        self.assertEqual(self.csv(out, "typo"), [])
        self.assertTrue(any(n.startswith("typo: no vehicle matched") for n in r["notes"]), r["notes"])
        self.assertTrue(self.csv(out, "odi"))
        spec["requests"][0]["params"] = {**base, "variants": True}
        code, r, out = self.fetch(spec)
        rows = self.csv(out, "sedan_2023")
        camp = [x["NHTSACampaignNumber"] for x in rows]
        self.assertEqual(len(camp), len(set(camp)))                       # a shared campaign is kept once
        self.assertIn("ACCORD HYBRID", {x["model_requested"] for x in rows})
        self.assertTrue(any("ACCORD HYBRID (4)" in n and "no record under: ACCORD SEDAN" in n for n in r["notes"]),
                        r["notes"])

    def test_model_list_is_asked_in_full(self):
        # products/vehicle/models and modelYears send only their first ~20 rows unless max is given [live
        # 2026-10-28: 13 rows by default, 57 with max]: the variant lookup and the endpoints must ask for all
        import json as _json
        from unittest import mock
        from core import generic_rest as gr
        from core import packs
        pack = packs.load("nhtsa")
        ad = pack.adapter
        sent = []

        class Resp:
            def __init__(self, d):
                self.body = _json.dumps(d).encode()
            def json(self):
                return _json.loads(self.body)

        class Client:
            def request(self, method, path, q, json_body=None):
                if "products" in path:
                    sent.append(dict(q))
                    names = [f"OTHER {i}" for i in range(19)] + ["WREN"] + ([] if not q.get("max") else
                                                                         ["SPARROW", "WREN HYBRID"])
                    return Resp({"results": [{"model": n} for n in names]})
                return Resp({"Count": 1, "results": [{"NHTSACampaignNumber": "99V00%d000" % len(q["model"])}]})

        spec = {"requests": [{"name": "w", "endpoint": "recalls_by_vehicle",
                              "params": {"make": "x", "model": "wren", "modelYear": 2023, "variants": True}}]}
        with mock.patch.object(ad.gr, "client_for", lambda pack: Client()):
            tables, notes = ad.fetch(spec, pack, "api")
        self.assertEqual(sent[0].get("max"), 10000)
        self.assertEqual({r["model_requested"] for r in tables["w"]}, {"wren", "WREN HYBRID"})
        for name in ("products_models", "products_model_years"):
            _, params, _ = gr.resolve(pack, {"name": "m", "endpoint": name,
                                             "params": {"modelYear": 2023, "make": "x", "issueType": "c"}})
            self.assertEqual(params.get("max"), 10000, name)

    def test_variant_5xx_marks_the_table_incomplete(self):
        # a 5xx is not "no record": the table is marked incomplete (status partial) while other lookups answered;
        # when no vehicle lookup answers at all, the source is down and the fetch fails
        import json as _json
        from unittest import mock
        from testkit import load_pack_module
        from core import generic_rest as gr
        from core import packs
        from core.http import DataError
        ad = load_pack_module("nhtsa")
        pack = packs.load("nhtsa")

        class Resp:
            def __init__(self, d):
                self.body = _json.dumps(d).encode()
            def json(self):
                return _json.loads(self.body)

        class Client:
            def __init__(self, up):
                self.up = up
            def request(self, method, path, q, json_body=None):
                if "products" in path:
                    return Resp({"results": [{"model": "WREN"}, {"model": "WREN HYBRID"}, {"model": "WREN SPORT"}]})
                if q["model"] == "wren" and self.up:
                    return Resp({"Count": 1, "results": [{"NHTSACampaignNumber": "99V001000"}]})
                raise DataError("nhtsa: HTTP 503", kind="http", status=503)

        spec = {"requests": [{"name": "w", "endpoint": "recalls_by_vehicle",
                              "params": {"make": "x", "model": "wren", "modelYear": 2023, "variants": True}}]}
        with mock.patch.object(ad.gr, "client_for", lambda pack: Client(True)):
            tables, notes = ad.fetch(spec, pack, "api")
        self.assertEqual(len(tables["w"]), 1)
        self.assertTrue(any(n.startswith("w" + gr.INCOMPLETE) and "WREN HYBRID (HTTP 503)" in n for n in notes), notes)
        self.assertFalse(any("no vehicle matched" in n for n in notes), notes)
        self.assertTrue(any("NHTSA also lists WREN SPORT" in n for n in notes), notes)   # a distinct model: not merged
        with mock.patch.object(ad.gr, "client_for", lambda pack: Client(False)):
            with self.assertRaises(DataError):
                ad.fetch(spec, pack, "api")

    def test_prefixed_names_unit(self):
        from testkit import load_pack_module
        ad = load_pack_module("nhtsa")
        listed = ["SPARROW", "SPARROW HYBRID", "SPARROWHAWK", "SPARROW (CREW CAB) EV", "sparrow hybrid", "SPARROW SPORT",
                  "SPARROW 4DR AWD", "WREN"]
        self.assertEqual(ad.prefixed("sparrow", listed),
                         (["SPARROW HYBRID", "SPARROW (CREW CAB) EV", "SPARROW 4DR AWD"], ["SPARROW SPORT"]))
        self.assertEqual(ad._forms("SPARROW (CREW CAB) EV", True), ["SPARROW (CREW CAB) EV", "SPARROW EV"])
        self.assertEqual(ad._forms("SPARROW (CREW CAB) EV", False), ["SPARROW (CREW CAB) EV"])

    def test_car_seat_stations_follow_offset_paging(self):
        # the station API returns 100 a page and pages with ?offset=; a state with more must not stop at 100
        import json as _json
        from core import generic_rest as gr
        from core import packs
        ep = packs.load("nhtsa").endpoints["endpoints"]["car_seat_stations_state"]
        sent = []

        class Resp:
            def __init__(self, n, start=0):
                self.body = _json.dumps({"Count": n, "Results": [{"Organization": f"o{start + i}"} for i in range(n)]}).encode()
            def json(self):
                return _json.loads(self.body)

        class Client:
            def request(self, method, path, q, json_body=None):
                sent.append(q.get("offset"))
                off = int(q.get("offset") or 0)
                return Resp(max(0, min(100, 230 - off)), off)

        rows, meta = gr.fetch_request(Client(), ep, {"state": "ZZ"})
        self.assertEqual(len(rows), 230)
        self.assertEqual(sent, [0, 100, 200])
        self.assertFalse(meta["truncated"])

    def test_paging_stops_when_a_page_repeats(self):
        # an endpoint that ignores offset returns the same page again: the fetch stops and marks the table
        # incomplete instead of reading that page up to max_rows
        import json as _json
        from core import generic_rest as gr
        from core import packs
        ep = packs.load("nhtsa").endpoints["endpoints"]["car_seat_stations_zip"]
        sent = []

        class Resp:
            body = _json.dumps({"Count": 100, "Results": [{"Organization": f"o{i}"} for i in range(100)]}).encode()
            def json(self):
                return _json.loads(self.body)

        class Client:
            def request(self, method, path, q, json_body=None):
                sent.append(q.get("offset"))
                return Resp()

        rows, meta = gr.fetch_request(Client(), ep, {"zip": "00000"})
        self.assertEqual((len(rows), sent), (100, [0, 100]))
        self.assertTrue(meta["truncated"] and meta["repeated_page"])

    def test_manual_bundle_vpic_host(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertIn("https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/1HGCM82633A123456?format=json", urls)
        self.assertTrue(any(u.startswith("https://api.nhtsa.gov/recalls/recallsByVehicle?") for u in urls))


if __name__ == "__main__":
    unittest.main()

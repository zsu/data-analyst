import json
import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from earthquake.usgs.gov and api.waterdata.usgs.gov (ogcapi/v1) on 2026-10-20.
SPEC = {"description": "M6+ earthquakes, January 2024; 2024 count; Potomac daily discharge; Vermont stream sites",
        "requests": [
            {"name": "m6_jan2024", "endpoint": "earthquakes",
             "params": {"starttime": "2024-01-01", "endtime": "2024-01-31", "minmagnitude": 6}, "max_rows": 3},
            {"name": "m6_2024_count", "endpoint": "earthquake_count",
             "params": {"starttime": "2024-01-01", "endtime": "2024-12-31", "minmagnitude": 6}},
            {"name": "potomac_daily", "endpoint": "water",
             "params": {"collection": "streamflow_daily", "monitoring_location_id": "01646500", "parameter_code": "00060",
                        "statistic_id": "00003", "time": "2024-01-01/2024-01-05"}, "max_rows": 3},
            {"name": "vt_sites", "endpoint": "water",
             "params": {"collection": "monitoring-locations", "state_code": "Vermont", "site_type_code": "ST",
                        "properties": "monitoring_location_number,monitoring_location_name,county_name,drainage_area"},
             "max_rows": 2}]}


class TestUsgs(PackTest):
    source = "usgs"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        eq = self.csv(out, "m6_jan2024")
        self.assertEqual(eq[0]["id"], "us7000lui3")
        self.assertEqual(eq[0]["mag"], "6.5")
        self.assertEqual(eq[0]["time_utc"], "2024-01-28T09:38:56.538Z")
        self.assertEqual(eq[0]["depth_km"], "621.081")
        self.assertEqual(self.csv(out, "m6_2024_count")[0]["count"], "99")
        q = {row["time"]: row["value"] for row in self.csv(out, "potomac_daily")}
        self.assertEqual(q["2024-01-03"], "6860")
        sites = self.csv(out, "vt_sites")
        self.assertEqual(sites[0]["monitoring_location_number"], "01129400")
        self.assertEqual(sites[0]["latitude"], "45.0039350382815")

    def test_manual_bundle_hosts(self):
        sp, out = self.write_spec(SPEC)
        code, r = self.cli(sp, "--out", out, "--manual-bundle")
        self.assertEqual(code, 0, r)
        with open(os.path.join(out, "manual_download", "manifest.json"), encoding="utf-8") as f:
            urls = [i["url"] for i in json.load(f)["items"]]
        self.assertTrue(urls[0].startswith("https://earthquake.usgs.gov/fdsnws/event/1/query?"), urls[0])
        self.assertIn("monitoring_location_id=USGS-01646500", urls[2])
        self.assertIn("/collections/daily/items?", urls[2])

    def test_site_filter_on_the_sites_collection_is_id(self):
        """monitoring-locations answers HTTP 400 to monitoring_location_id; its site key is `id` (live 2026-10)."""
        from testkit import load_pack_module
        ad = load_pack_module("usgs")
        ep = {"path": "/collections/{collection}/items"}
        sites = ad.before_request({}, ep, {"collection": "monitoring-locations", "monitoring_location_id": "04086600,usgs-05420500"})
        self.assertEqual(sites["id"], "USGS-04086600,USGS-05420500")
        self.assertNotIn("monitoring_location_id", sites)
        data = ad.before_request({}, ep, {"collection": "peaks", "monitoring_location_id": "04086600"})
        self.assertEqual(data["monitoring_location_id"], "USGS-04086600")       # data collections keep it

    def test_eventid_single_feature_is_one_row(self):
        """With `eventid` the event service answers one GeoJSON Feature, not a FeatureCollection (live 2026-10)."""
        from testkit import load_pack_module, pack_dir
        from core.packfiles import read_json
        ad = load_pack_module("usgs")
        ep = read_json(pack_dir("usgs"), "endpoints.json")["endpoints"]["earthquakes"]
        body = {"type": "Feature", "id": "zz00000001",       # synthetic event
                "properties": {"mag": 4.2, "place": "test place", "time": 1700000000000, "updated": 1700000100000,
                               "products": {"origin": [{"id": "p1", "contents": {"a": {"url": "x"}}}]}},
                "geometry": {"type": "Point", "coordinates": [-120.5, 36.25, 8.1]}}

        class Resp:
            def json(self):
                return body

        class Fake:
            calls = []

            def get(self, path, query=None):
                self.calls.append(query)
                return Resp()

        c = Fake()
        rows, more = ad._quake(c, ep, {**ep["defaults"], "eventid": "zz00000001"}, None)
        self.assertFalse(more)
        self.assertEqual(c.calls[0]["eventid"], "zz00000001")
        rows = ad.normalize({}, ep, rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "zz00000001")
        self.assertEqual(rows[0]["depth_km"], "8.1")
        self.assertEqual(rows[0]["time_utc"], "2023-11-14T22:13:20.000Z")
        self.assertFalse([k for k in rows[0] if k.startswith("products")])
        coll = ad._features({"type": "FeatureCollection", "features": [body, body]})
        self.assertEqual(len(coll), 2)

    def test_unknown_endpoint_is_fix_spec(self):
        code, r = self.check({"requests": [{"name": "x", "endpoint": "nwis_iv", "params": {}}]})
        self.assertEqual(r["route"], "fix_spec", r)


if __name__ == "__main__":
    unittest.main()

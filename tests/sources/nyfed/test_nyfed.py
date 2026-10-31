import os
import sys
import unittest

sys.path[:0] = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),      # tests/ (testkit)
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plugins", "data-analyst",
                             "skills",
                             "data-analyst", "scripts")]
from testkit import PackTest  # noqa: E402

# Fixtures recorded live from markets.newyorkfed.org (keyless) on 2026-10-30. The soma_summary endpoint (the whole
# weekly history, 320 KB) has no fixture; it was fetched live the same day (1,2xx weekly rows from 2003-07-09).
SPEC = {"description": "SOFR and EFFR in October 2026, Fed Treasury holdings and purchases",
        "requests": [
            {"name": "sofr", "endpoint": "rates",
             "params": {"family": "secured", "ratetype": "sofr", "startDate": "2026-10-01", "endDate": "2026-10-29"}},
            {"name": "effr", "endpoint": "rates",
             "params": {"family": "unsecured", "ratetype": "effr", "startDate": "2026-10-01", "endDate": "2026-10-29"}},
            {"name": "wam", "endpoint": "soma_tsy_wam", "params": {"holdingtype": "all", "date": "2026-10-28"}},
            {"name": "dates", "endpoint": "soma_asofdates", "params": {}},
            {"name": "ops", "endpoint": "tsy_operations",
             "params": {"operation": "purchases", "include": "summary", "startDate": "2026-10-01",
                        "endDate": "2026-10-29"}},
            {"name": "pd", "endpoint": "pd_series", "params": {"timeseries": "PDPOSGST-TOT"}},
            {"name": "hold", "endpoint": "soma_tsy_holdings", "params": {"date": "2026-10-28"}}]}


class TestNyfed(PackTest):
    source = "nyfed"
    sample_spec = SPEC

    def test_check_api(self):
        code, r = self.check(SPEC)
        self.assertEqual(code, 0, r)
        self.assertEqual(r["route"], "api")

    def test_fetch(self):
        code, r, out = self.fetch(SPEC)
        self.assertEqual(code, 0, r)
        sofr = self.csv(out, "sofr")
        self.assertEqual(len(sofr), 20)
        self.assertEqual((sofr[0]["effectiveDate"], sofr[0]["type"], sofr[0]["percentRate"]), ("2026-10-29", "SOFR", "3.94"))
        effr = self.csv(out, "effr")
        self.assertEqual(effr[0]["type"], "EFFR")
        self.assertEqual((effr[0]["targetRateFrom"], effr[0]["targetRateTo"]), ("3.75", "4.0"))
        self.assertEqual(self.csv(out, "wam")[0]["wam"], "8.13")
        dates = self.csv(out, "dates")
        self.assertEqual((len(dates), dates[0]["value"]), (1217, "2026-10-28"))
        ops = self.csv(out, "ops")
        self.assertEqual(len(ops), 5)
        self.assertEqual(ops[0]["totalParAmtAccepted"], "1827000000")
        pd = self.csv(out, "pd")
        self.assertEqual((pd[0]["asofdate"], pd[0]["value"]), ("2013-04-03", "108014"))
        hold = self.csv(out, "hold")
        self.assertEqual(len(hold), 433)
        self.assertEqual({x["securityType"] for x in hold} - {"Bills", "NotesBonds", "TIPS", "FRNs"}, set())

    def test_bad_rate_family_is_fix_spec(self):
        bad = {"requests": [{"name": "x", "endpoint": "rates",
                             "params": {"family": "overnight", "ratetype": "sofr"}}]}
        code, r = self.check(bad)
        self.assertEqual(r["route"], "fix_spec", r)
        missing = {"requests": [{"name": "x", "endpoint": "soma_tsy_holdings", "params": {}}]}
        code, r = self.check(missing)
        self.assertEqual(r["route"], "fix_spec", r)


if __name__ == "__main__":
    unittest.main()

"""Sweep 2026-09-12: the passive-obs summary must accept a JSON body.

The UI sent ~450 pubkey prefixes as a query string (~5 KB) every few seconds, which
flooded the access log (and risks URL limits). POST is now preferred; the legacy GET
form must keep working for any page that is still cached.
"""
import unittest
from unittest import mock

import app as app_module


class PassiveObsSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.calls = []

        def fake_summary(radio_id, prefixes):
            self.calls.append((radio_id, list(prefixes)))
            return {p: {"count": 1} for p in prefixes}

        self.patcher = mock.patch("routes.mc.load_passive_obs_summary", side_effect=fake_summary)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_post_body_is_accepted(self):
        r = self.client.post("/api/mc/r1/passive_obs/summary", json={"prefixes": ["aa", "bb"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(sorted(r.get_json()), ["aa", "bb"])
        self.assertEqual(self.calls[-1], ("r1", ["aa", "bb"]))

    def test_legacy_query_string_still_works(self):
        r = self.client.get("/api/mc/r1/passive_obs/summary?prefixes=aa,bb")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(sorted(r.get_json()), ["aa", "bb"])

    def test_empty_request_returns_empty_without_touching_the_db(self):
        r = self.client.post("/api/mc/r1/passive_obs/summary", json={"prefixes": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

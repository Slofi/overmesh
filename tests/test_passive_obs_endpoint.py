"""Sweep 2026-09-12: the passive-obs summary must accept a JSON body.

The UI sent ~450 pubkey prefixes as a query string (~5 KB) every few seconds, which
flooded the access log (and risks URL limits). POST is now preferred; the legacy GET
form must keep working for any page that is still cached.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import app as app_module


class PassiveObsSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.calls = []

        def fake_summary(radio_id, prefixes, **kwargs):
            self.calls.append((radio_id, list(prefixes), kwargs))
            return {p: {"count": 1} for p in prefixes}

        self.patcher = mock.patch("routes.mc.load_passive_obs_summary", side_effect=fake_summary)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_post_body_is_accepted(self):
        r = self.client.post("/api/mc/r1/passive_obs/summary", json={"prefixes": ["aa", "bb"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(sorted(r.get_json()), ["aa", "bb"])
        self.assertEqual(self.calls[-1][:2], ("r1", ["aa", "bb"]))

    def test_legacy_query_string_still_works(self):
        r = self.client.get("/api/mc/r1/passive_obs/summary?prefixes=aa,bb")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(sorted(r.get_json()), ["aa", "bb"])

    def test_overheard_packets_are_excluded_by_default(self):
        self.client.post("/api/mc/r1/passive_obs/summary", json={"prefixes": ["aa"]})
        self.assertEqual(self.calls[-1][2].get("exclude_obs_types"), ("rx",))

    def test_include_rx_opt_in_counts_overheard_packets(self):
        # A local MC node produces no identity observations, so the eye badge had
        # nothing to count until the client opted overheard packets in.
        self.client.post("/api/mc/r1/passive_obs/summary", json={"prefixes": ["aa"], "include_rx": True})
        self.assertEqual(self.calls[-1][2].get("exclude_obs_types"), ())

    def test_include_rx_accepts_the_query_string_form_too(self):
        self.client.get("/api/mc/r1/passive_obs/summary?prefixes=aa&include_rx=1")
        self.assertEqual(self.calls[-1][2].get("exclude_obs_types"), ())

    def test_empty_request_returns_empty_without_touching_the_db(self):
        r = self.client.post("/api/mc/r1/passive_obs/summary", json={"prefixes": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {})
        self.assertEqual(self.calls, [])



class PassiveObsSummaryStoreTests(unittest.TestCase):
    """The summary is what the eye badge and the per-contact view are built on.

    A local MC node stores no identity observations, only overheard packets, so
    until 2026-11-02 the summary could only ever return zeros for it. These tests
    run the real query against a real (temporary) store.
    """

    RADIO = "r_summary_test"

    def setUp(self):
        import db
        self.db = db
        self.tmp = tempfile.mkdtemp(prefix="om-passive-")
        path = os.path.join(self.tmp, "passive.db")
        with db._mc_passive_db_registry_lock:
            db._mc_passive_db_registry[self.RADIO] = path
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with self.db._mc_passive_db_registry_lock:
            self.db._mc_passive_db_registry.pop(self.RADIO, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        # one overheard packet filed under a 2-byte hop hash, one under an
        # ambiguous 1-byte hash, and one identity observation keyed by full pubkey
        self.db.save_passive_obs(self.RADIO, "86a8", "rx", rssi=-61, snr=9)
        self.db.save_passive_obs(self.RADIO, "86", "rx", rssi=-100, snr=1)
        self.db.save_passive_obs(self.RADIO, "86a8eedb744b" + "0" * 52, "rc_adv", rssi=-70, snr=6)

    def test_overheard_packets_are_not_credited_by_default(self):
        self._seed()
        summary = self.db.load_passive_obs_summary(self.RADIO, ["86a8eedb744b"])
        self.assertEqual(summary["86a8eedb744b"]["obs_count"], 1,
                         "only the identity observation counts by default")

    def test_opting_rx_in_credits_the_relay_key_to_the_contact(self):
        self._seed()
        summary = self.db.load_passive_obs_summary(self.RADIO, ["86a8eedb744b"], exclude_obs_types=())
        self.assertEqual(summary["86a8eedb744b"]["obs_count"], 2,
                         "the 2-byte hop hash belongs to this contact and is now counted")
        self.assertEqual(summary["86a8eedb744b"]["best_rssi"], -61,
                         "the best signal across both sources is reported")

    def test_a_one_byte_hop_hash_is_never_credited(self):
        self._seed()
        summary = self.db.load_passive_obs_summary(self.RADIO, ["86a8eedb744b"], exclude_obs_types=())
        self.assertEqual(summary["86a8eedb744b"]["obs_count"], 2,
                         "the ambiguous '86' row must not be added to the contact")

if __name__ == "__main__":
    unittest.main()

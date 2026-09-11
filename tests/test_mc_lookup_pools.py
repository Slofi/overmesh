"""Sweep #7: _mc_lookup_full_contact must search the LIVE pool too.

Its docstring promised "live + merged contacts first, then OM's archive", but the
live pool was never added to the searched pools — a contact present on the radio
but absent from the merged/archive views was unresolvable (KeyError), which broke
store-to-radio / scoped delete for exactly the contacts that exist only on the
radio. Introduced in 2ca3385 (manual contact approval).
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "/home/slofi/overmesh")

import routes.mc as mc  # noqa: E402

LIVE_KEY = "aa11" * 16
MERGED_KEY = "bb22" * 16
ARCH_KEY = "cc33" * 16


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "rc1": {
                "contacts": {},                     # merged not populated yet
                "live_contacts": {LIVE_KEY: {"long_name": "Live Only"}},
            },
        }
        self.archive = {ARCH_KEY: {"long_name": "Archived Only"}}
        self.patches = [
            mock.patch.object(mc, "mc_connections", self.state),
            mock.patch.object(mc, "get_mc_contact_archive", lambda rid: self.archive),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_live_only_contact_resolves(self):
        """The regression: live_contacts was searched nowhere."""
        full, rec = mc._mc_lookup_full_contact("rc1", LIVE_KEY[:8])
        self.assertEqual(full, LIVE_KEY)
        self.assertEqual(rec.get("long_name"), "Live Only")

    def test_archive_contact_still_resolves(self):
        full, _ = mc._mc_lookup_full_contact("rc1", ARCH_KEY[:8])
        self.assertEqual(full, ARCH_KEY)

    def test_merged_takes_precedence_over_archive(self):
        merged_key = "dd44" * 16
        self.state["rc1"]["contacts"] = {merged_key: {"long_name": "Merged"}}
        full, rec = mc._mc_lookup_full_contact("rc1", merged_key[:8])
        self.assertEqual(full, merged_key)
        self.assertEqual(rec.get("long_name"), "Merged")

    def test_same_contact_in_two_pools_counts_once(self):
        """A contact seen in both live and archive is one match, not ambiguous."""
        same = "ee55" * 16
        self.state["rc1"]["live_contacts"] = {same: {"long_name": "Both"}}
        self.archive = {same: {"long_name": "Both"}}
        full, _ = mc._mc_lookup_full_contact("rc1", same[:8])
        self.assertEqual(full, same)

    def test_ambiguous_prefix_is_refused(self):
        self.state["rc1"]["live_contacts"] = {
            "ab11" + "0" * 60: {"long_name": "One"},
            "ab22" + "0" * 60: {"long_name": "Two"},
        }
        with self.assertRaises(ValueError):
            mc._mc_lookup_full_contact("rc1", "ab")

    def test_short_prefix_rejected_and_missing_raises_keyerror(self):
        with self.assertRaises(ValueError):
            mc._mc_lookup_full_contact("rc1", "abc")
        with self.assertRaises(KeyError):
            mc._mc_lookup_full_contact("rc1", "999999")


class ImportHygieneTests(unittest.TestCase):
    def test_no_unused_auto_add_import(self):
        src = open("/home/slofi/overmesh/routes/mc.py").read()
        self.assertNotIn("apply_mc_auto_add_contacts,", src)
        self.assertIn("get_mc_auto_add_state,", src)


if __name__ == "__main__":
    unittest.main()

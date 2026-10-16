"""Scan / Probe results: collection, the done payload, and the pop-up (S453).

Filip, 2026-10-16: "on Map/Sens, MC side, there are 'Scan' and 'Probe'
buttons/commands.. Make the response to those a new pop-up window with results,
so responded nodes/RPTRs are listed there."

Before this, a Scan or Probe ended with nothing but "Done: N new" in a small
status line: the repeaters that actively answered our DISCOVER_REQ (the only
proof the radio was heard) were never listed anywhere, and the list could not be
looked at again once the moment passed.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-scan-results-")
_CONFIG_PATH = Path(_TEST_DIR.name) / "config.json"
_DATA_DIR = Path(_TEST_DIR.name) / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

import mesh_mc  # noqa: E402
import routes.mc as mc_routes  # noqa: E402

RPTR_KEY = "86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6"
NODE_KEY = "aa11bb22cc33dd44ee55ff6600112233445566778899aabbccddeeff00112233"
ROOM_KEY = "1234abcd5678ef901234abcd5678ef901234abcd5678ef901234abcd5678ef90"


class ScanResultsTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.contacts = {
            "mc1": {"contacts": {
                RPTR_KEY: {"adv_name": "SI-LJ-Trnovo II", "type": 2, "out_path_len": 0,
                           "last_heard_ts": self.now - 10},
                NODE_KEY: {"adv_name": "Filip phone", "type": 0, "out_path_len": 2,
                           "last_heard_ts": self.now - 5},
                "ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000": {
                    "adv_name": "Old node", "type": 0, "last_heard_ts": self.now - 4000},
            }},
        }
        patcher = mock.patch.dict(mesh_mc.mc_connections, self.contacts, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        mesh_mc.mc_scan_begin("mc1")
        self.addCleanup(mesh_mc.mc_scan_end, "mc1")
        self.addCleanup(lambda: mesh_mc._scan_discover_seen.pop("mc1", None))

    def _discover(self, key, rssi=-80, snr=-7.5, snr_in=3.25, node_type=2):
        mesh_mc.mc_scan_note_discover("mc1", {
            "pubkey": key, "RSSI": rssi, "SNR": snr, "SNR_in": snr_in,
            "node_type": node_type, "tag": "0badc0de",
        })

    def test_a_repeater_that_answered_is_listed_with_its_signal(self):
        self._discover(RPTR_KEY)
        results, summary = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=False, discover=True)
        answered = [r for r in results if r["answered"]]
        self.assertEqual(len(answered), 1)
        rptr = answered[0]
        self.assertEqual(rptr["full_key"], RPTR_KEY)
        self.assertEqual(rptr["name"], "SI-LJ-Trnovo II", "the name comes from the contact record")
        self.assertEqual(rptr["kind"], "repeater")
        self.assertEqual(rptr["rssi"], -80)
        self.assertEqual(rptr["snr_in"], 3.25, "how well the repeater heard US is kept")
        self.assertEqual(summary["answered"], 1)
        self.assertEqual(summary["repeaters"], 1)
        self.assertFalse(summary["advert"])

    def test_responders_come_before_nodes_merely_heard(self):
        self._discover(RPTR_KEY, rssi=-95)
        results, _ = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=True, discover=True)
        self.assertTrue(results[0]["answered"], "the active replies are the point of the scan")
        self.assertIn(NODE_KEY, [r["full_key"] for r in results], "adverts heard in the window are listed too")
        self.assertFalse([r for r in results if r["full_key"] == NODE_KEY][0]["answered"])

    def test_replies_outside_the_window_are_not_results(self):
        """A DISCOVER reply from an ordinary Ping must not be attributed to a scan."""
        mesh_mc.mc_scan_end("mc1")
        self._discover(RPTR_KEY)
        results, summary = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=False, discover=True)
        self.assertEqual([r for r in results if r["answered"]], [])
        self.assertEqual(summary["answered"], 0)

    def test_contacts_heard_before_the_window_are_left_out(self):
        results, _ = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=True, discover=True)
        keys = [r["full_key"] for r in results]
        self.assertIn(NODE_KEY, keys)
        self.assertNotIn("ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000", keys,
                         "a contact last heard an hour ago is not a scan result")

    def test_room_servers_and_plain_nodes_get_their_own_kind(self):
        self._discover(ROOM_KEY, node_type=1)
        results, summary = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=False, discover=True)
        self.assertEqual([r for r in results if r["full_key"] == ROOM_KEY][0]["kind"], "room")
        self.assertEqual(summary["rooms"], 1)

    def test_a_reply_without_a_stored_contact_still_lists(self):
        """The name may be unknown at reply time (non-blocking lookup missed it),
        so the result builder resolves it again from the contact list."""
        mesh_mc.mc_scan_note_discover("mc1", {"pubkey": RPTR_KEY, "RSSI": -70, "node_type": 2})
        results, _ = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=False, discover=True)
        self.assertEqual(results[0]["name"], "SI-LJ-Trnovo II")

    def test_summary_counts_what_the_pop_up_shows(self):
        self._discover(RPTR_KEY, rssi=-70)
        self._discover(ROOM_KEY, rssi=-90, node_type=1)
        results, summary = mesh_mc.mc_scan_results("mc1", self.now - 30, advert=True, discover=True)
        self.assertEqual(summary["total"], len(results))
        self.assertEqual(summary["answered"], 2)
        self.assertEqual(summary["repeaters"], 1)
        self.assertEqual(summary["rooms"], 1)
        self.assertTrue(summary["advert"])
        self.assertTrue(summary["discover"])
        self.assertGreaterEqual(summary["window"], 0)


class ScanDonePayloadTests(unittest.TestCase):
    def setUp(self):
        mesh_mc.mc_scan_begin("mc1")
        self.addCleanup(mesh_mc.mc_scan_end, "mc1")

    def test_the_done_event_carries_the_results(self):
        payload = mc_routes._scan_done_payload("mc1", time.time() - 60, advert=False, discover=True)
        self.assertEqual(payload["type"], "mc_scan_done")
        self.assertEqual(payload["radio_id"], "mc1")
        self.assertEqual(payload["window"], mc_routes.MC_SCAN_WINDOW)
        self.assertFalse(payload["advert"])
        self.assertTrue(payload["discover"])
        self.assertIn("results", payload)
        self.assertIn("summary", payload)
        self.assertIsInstance(payload["results"], list)

    def test_the_window_is_closed_once_done_is_sent(self):
        payload = mc_routes._scan_done_payload("mc1", time.time() - 60, advert=True, discover=True)
        self.assertEqual(payload["type"], "mc_scan_done")
        self.assertNotIn("mc1", mesh_mc._scan_active_radios,
                         "replies after the scan must not land in it")

    def test_a_failing_collector_still_emits_scan_done(self):
        """If building the list raises, the pop-up must still be told the scan is
        over — otherwise the buttons stay disabled for a minute."""
        with mock.patch.object(mc_routes, "mc_scan_results", side_effect=RuntimeError("boom")):
            payload = mc_routes._scan_done_payload("mc1", time.time() - 60, advert=True, discover=True)
        self.assertEqual(payload["type"], "mc_scan_done")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["summary"], {})


class ScanResultsPanelWiringTests(unittest.TestCase):
    """The pop-up is static JS plus one button, so the guards read the sources."""

    def setUp(self):
        self.src = Path(_REPO, "static/js/app.js").read_text(encoding="utf-8")
        self.html = Path(_REPO, "templates/index.html").read_text(encoding="utf-8")

    def test_the_scan_done_event_keeps_and_shows_the_results(self):
        self.assertIn("_mcScanLastResults = resultsPayload;", self.src)
        self.assertIn("function showMcScanResults()", self.src)
        self.assertIn("openModal(`${payload.advert === false ? 'Probe' : 'Scan'} results`", self.src)

    def test_a_scan_does_not_clobber_an_open_modal(self):
        """The pop-up shares the app's one action modal; a scan that finishes
        while the user is inside another panel must not overwrite it."""
        self.assertIn("const modalOpen = document.getElementById('action-modal')?.classList.contains('open');", self.src)
        self.assertIn("if (!modalOpen) showMcScanResults();", self.src)

    def test_results_button_exists_and_is_toggled(self):
        self.assertIn('id="mc-scan-results-btn"', self.html)
        self.assertIn('onclick="showMcScanResults()"', self.html)
        self.assertIn("resultsBtn.style.display = (mcConnected && _mcScanLastResults) ? '' : 'none';", self.src)

    def test_rows_label_repeaters_rooms_and_plain_nodes(self):
        for fragment in ("'RPTR'", "'ROOM'", "'node'", "answered our DISCOVER probe",
                         "heard during the scan window", "heard you "):
            self.assertIn(fragment, self.src, f"the results list must show {fragment}")
        self.assertIn("_mcScanSignalText", self.src)
        self.assertIn("first seen in this scan", self.src, "newly seen contacts are flagged")

    def test_an_empty_window_explains_itself(self):
        self.assertIn("Nothing answered and nothing was heard in the", self.src)
        self.assertIn("Probe only reaches repeaters and room servers in direct (0-hop) range.", self.src)

    def test_new_contacts_seen_during_the_window_are_tracked(self):
        self.assertIn("_mcScanNewIds.add(data.id);", self.src)
        self.assertIn("_mcScanNewIds   = new Set();", self.src)


if __name__ == "__main__":
    unittest.main()

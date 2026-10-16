import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-scan-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import mesh_mc  # noqa: E402
import routes.mc as mc_routes  # noqa: E402


class McScanProbeModeTests(unittest.TestCase):
    """Scan must advertise + discover; Probe must send DISCOVER_REQ only.

    The advert is the packet that makes other nodes add this radio to their
    contact tables, so the flag has to be honoured (and honoured exactly).
    """

    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(mc_routes.bp)
        self.client = self.app.test_client()
        self.adverts = []
        self.discovers = []
        with mc_routes.mc_connections_lock:
            mc_routes.mc_connections["mcA"] = {"status": "connected", "config": {"type": "serial"}}
        patcher = mock.patch.object(mc_routes, "send_advert",
                                    side_effect=lambda rid, flood=False: self.adverts.append((rid, flood)))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(mc_routes, "send_discover_req",
                                     side_effect=lambda rid: (self.discovers.append(rid) or "tag"))
        patcher2.start()
        self.addCleanup(patcher2.stop)
        patcher3 = mock.patch.object(mc_routes, "push_to_sse")
        self.push = patcher3.start()
        self.addCleanup(patcher3.stop)
        patcher4 = mock.patch.object(mc_routes, "MC_SCAN_WINDOW", 0)
        patcher4.start()
        self.addCleanup(patcher4.stop)
        self.addCleanup(lambda: mc_routes._scan_timers.pop("mcA", None))

    def _scan(self, body=None):
        return self.client.post("/api/mc/mcA/scan", json=body) if body is not None \
            else self.client.post("/api/mc/mcA/scan")

    def test_default_scan_advertises_then_probes(self):
        r = self._scan()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.adverts, [("mcA", True)])
        self.assertEqual(self.discovers, ["mcA"])
        self.assertTrue(r.get_json()["advert"])

    def test_probe_mode_sends_no_advert(self):
        r = self._scan({"advert": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.adverts, [], "Probe must not transmit an advert")
        self.assertEqual(self.discovers, ["mcA"], "Probe must still send DISCOVER_REQ")
        self.assertFalse(r.get_json()["advert"])

    def test_a_reply_during_the_window_lands_in_the_done_event(self):
        """The route must OPEN the results window (before it transmits), and a
        repeater answering during it must appear in mc_scan_done — that is the
        whole feature: the pop-up lists who responded (S453)."""
        key = "86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6"
        with mock.patch.object(mc_routes, "MC_SCAN_WINDOW", 0.15):
            r = self._scan({"advert": False})
            self.assertEqual(r.status_code, 200)
            # the repeater answers our DISCOVER_REQ while the window is open
            mesh_mc.mc_scan_note_discover("mcA", {"pubkey": key, "RSSI": -80, "node_type": 2})
            mc_routes._scan_timers["mcA"].join(timeout=5)
        done = [json.loads(call.args[0]) for call in self.push.call_args_list
                if json.loads(call.args[0]).get("type") == "mc_scan_done"]
        self.assertEqual(len(done), 1, "the scan must still end with mc_scan_done")
        answered = [x for x in done[0]["results"] if x["answered"]]
        self.assertEqual([x["full_key"] for x in answered], [key],
                         "the reply must be in the done payload (the route must have opened the window)")
        self.assertEqual(done[0]["summary"]["answered"], 1)
        self.assertFalse(done[0]["advert"], "a Probe reports advert=False to the pop-up")

    def test_silent_mode_blocks_scan_before_any_tx(self):
        with mock.patch.object(mc_routes, "CONFIG", {**mc_routes.CONFIG, "silent_mode": True}):
            r = self._scan()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.adverts, [])
        self.assertEqual(self.discovers, [])


class McScanProbeButtonLabelTests(unittest.TestCase):
    """EST-83 Q1: the Probe button must read "Probing…", not "Scanning…".

    Both modes share the mc_scan_started handler, which used to hard-code
    "Scanning…" for the button it happened to be updating. This is client-side
    only (the label is set in app.js), so the guard reads the source: the
    isProbe branch must choose the per-mode label, and startMcScan's immediate
    feedback must do the same instead of a bare '…'.
    """

    def setUp(self):
        self.src = Path(REPO, "static/js/app.js").read_text(encoding="utf-8")

    def test_scan_started_labels_probe_and_scan_distinctly(self):
        m = re.search(
            r"const isProbe = data\.advert === false;[\s\S]{0,500}?btn\.textContent = ([^;]+);",
            self.src,
        )
        self.assertIsNotNone(m, "mc_scan_started must keep its `const isProbe = data.advert === false;` guard")
        expr = m.group(1)
        self.assertIn("Probing", expr, "the probing button must say Probing…")
        self.assertIn("Scanning", expr, "the scanning button must still say Scanning…")

    def test_immediate_feedback_uses_per_mode_label(self):
        m = re.search(
            r"const isProbe = advert === false;[\s\S]{0,320}?btn\.textContent = ([^;]+);",
            self.src,
        )
        self.assertIsNotNone(m, "startMcScan must keep its `const isProbe = advert === false;` guard")
        expr = m.group(1)
        self.assertIn("Probing", expr, "the immediate Probe feedback must say Probing…")
        self.assertIn("Scanning", expr, "the immediate Scan feedback must say Scanning…")


class McRegionsDiscoverSilentTests(unittest.TestCase):
    """Settings → MC radio → *Discover regions* transmits too.

    Its docstring says so itself ("the same RF class as the Scan button"), but
    unlike Scan it had no silent gate of its own: the sweep's entry gate raised
    mid-flight and the UI got a 500 "Region discovery failed: Silent Running
    active…" instead of the clean 409 the Scan button returns. Found 2026-10-16
    (S453) while answering "and Probe?" — the sweep behind this route is also
    the one whose retry transmitted 8 s after its check.
    """

    def setUp(self):
        import routes.settings as settings_routes
        self.settings_routes = settings_routes
        self.app = Flask(__name__)
        self.app.register_blueprint(settings_routes.bp)
        self.client = self.app.test_client()
        self._backup = json.loads(json.dumps(settings_routes.CONFIG))
        settings_routes.CONFIG["mc_nodes"] = [{"id": "mcA"}]
        settings_routes.CONFIG["silent_mode"] = False
        self.calls = []

        def _fake(radio_id, **kw):
            self.calls.append(radio_id)
            return {"regions": [], "perRepeater": [], "noZeroHopRepeaters": True, "warning": None}

        patcher = mock.patch("mesh_mc.discover_mc_regions", side_effect=_fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.settings_routes.CONFIG.clear()
        self.settings_routes.CONFIG.update(self._backup)

    def _post(self):
        return self.client.post("/api/settings/mc_nodes/mcA/regions/discover")

    def test_silent_refuses_with_409_before_any_transmission(self):
        self.settings_routes.CONFIG["silent_mode"] = True
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertIn("Silent Running", r.get_json()["error"])
        self.assertEqual(self.calls, [], "no sweep may be started while silent")

    def test_not_silent_runs_the_sweep(self):
        """Positive control: the 409 above must not be 'this endpoint always fails'."""
        self.settings_routes.CONFIG["silent_mode"] = False
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls, ["mcA"])


if __name__ == "__main__":
    unittest.main()

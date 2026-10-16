"""EST-82 B3: GPS push must respect Silent Running like every other send path.

_gps_push_to_nodes() called iface.localNode._sendAdmin() with no silent_mode
check, so POST /api/gps/push returned 200 while /api/chat/send returned 409,
and the auto-push loop kept transmitting. Both paths now honour the flag; the
auto-push loop logs once per transition instead of every tick.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-gps-silent-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(
    json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}),
    encoding="utf-8",
)
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

import gps as gps_mod  # noqa: E402
from config import CONFIG  # noqa: E402
from routes import gps as gps_routes  # noqa: E402

_SILENT_MSG = "Silent Running active: transmissions are blocked"

# A valid fix for the route + parser (NMEA GPGGA sentence).
_GPGGA = "$GPGGA,123519,4605.123,N,01430.456,E,1,08,0.9,300.0,M,0.0,M,,*47"


class _ConfigStub(unittest.TestCase):
    def setUp(self):
        self._backup = json.loads(json.dumps(CONFIG))
        CONFIG.clear()
        CONFIG.update({
            "nodes": [], "mc_nodes": [],
            "gps": {"enabled": False, "port": "", "auto_push": False},
            "silent_mode": False,
        })
        self._silent_backup = gps_mod._gps_silent_suppressed
        gps_mod._gps_silent_suppressed = False
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        CONFIG.clear()
        CONFIG.update(self._backup)
        gps_mod._gps_silent_suppressed = self._silent_backup
        with gps_mod.gps_lock:
            gps_mod.gps_state.update({"fix": False, "lat": None, "lon": None, "alt": None})
        gps_mod._gps_last_push_ts = 0.0


class GpsPushApiSilentTests(_ConfigStub):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(gps_routes.bp)
        self.client = self.app.test_client()
        with gps_mod.gps_lock:
            gps_mod.gps_state.update({"fix": True, "lat": 46.05, "lon": 14.5, "alt": 300})

    def test_silent_mode_returns_409_and_does_not_push(self):
        CONFIG["silent_mode"] = True
        with mock.patch.object(gps_routes, "_gps_push_to_nodes") as push:
            resp = self.client.post("/api/gps/push", json={})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], _SILENT_MSG)
        push.assert_not_called()

    def test_not_silent_still_pushes(self):
        CONFIG["silent_mode"] = False
        with mock.patch.object(gps_routes, "_gps_push_to_nodes") as push, \
             mock.patch("threading.Thread") as thread_mock:
            resp = self.client.post("/api/gps/push", json={})
        self.assertEqual(resp.status_code, 200)
        thread_mock.assert_called_once()


class GpsAutoPushSilentTests(_ConfigStub):
    def test_auto_push_skipped_while_silent(self):
        CONFIG["silent_mode"] = True
        CONFIG["gps"] = {"auto_push": True, "push_interval": 10, "precision": 32}
        gps_mod._gps_last_push_ts = 0.0
        with mock.patch.object(gps_mod, "_gps_push_to_nodes") as push, \
             mock.patch("threading.Thread") as thread_mock:
            gps_mod._parse_gpgga(_GPGGA)
        thread_mock.assert_not_called()
        push.assert_not_called()

    def test_auto_push_runs_when_not_silent(self):
        CONFIG["silent_mode"] = False
        CONFIG["gps"] = {"auto_push": True, "push_interval": 10, "precision": 32}
        gps_mod._gps_last_push_ts = 0.0
        with mock.patch.object(gps_mod, "_gps_push_to_nodes") as push, \
             mock.patch("threading.Thread") as thread_mock:
            gps_mod._parse_gpgga(_GPGGA)
        thread_mock.assert_called_once()

    def test_push_helper_refuses_while_silent(self):
        """Found in the 2026-10-16 sweep: this test only asserted "does not raise", so it
        passed whether or not the push was actually refused — the failure it exists to
        catch (a silent transmit) would have gone unnoticed. Now it asserts the refusal."""
        CONFIG["silent_mode"] = True
        iface = mock.Mock()
        iface.myInfo.my_node_num = 1
        iface.nodesByNum = {1: {"position": {}}}
        with mock.patch.object(gps_mod, "connections", {"radio1": {"status": "connected", "iface": iface}}), \
             mock.patch.object(gps_mod, "push_to_sse") as sse:
            gps_mod._gps_push_to_nodes(46.05, 14.5, 300, 32)
        iface.localNode._sendAdmin.assert_not_called()
        iface.localNode.ensureSessionKey.assert_not_called()
        sse.assert_not_called()
        self.assertEqual(iface.nodesByNum[1]["position"], {},
                         "the silent path must not even update the position cache")

    def test_suppression_logs_once_per_transition(self):
        CONFIG["silent_mode"] = True
        with self.assertLogs("gps", level="INFO") as captured:
            gps_mod._gps_tx_blocked("GPS auto-push")
            gps_mod._gps_tx_blocked("GPS auto-push")
            gps_mod._gps_tx_blocked("GPS auto-push")
        suppressed = [r for r in captured.records if "suppressed" in r.getMessage()]
        self.assertEqual(len(suppressed), 1, [r.getMessage() for r in captured.records])

        CONFIG["silent_mode"] = False
        with self.assertLogs("gps", level="INFO") as resumed:
            gps_mod._gps_tx_blocked("GPS auto-push")
        self.assertTrue(any("resumed" in r.getMessage() for r in resumed.records))


if __name__ == "__main__":
    unittest.main()

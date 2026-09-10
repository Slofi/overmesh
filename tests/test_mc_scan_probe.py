import json
import os
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
sys.path.insert(0, "/home/slofi/overmesh")

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

    def test_silent_mode_blocks_scan_before_any_tx(self):
        with mock.patch.object(mc_routes, "CONFIG", {**mc_routes.CONFIG, "silent_mode": True}):
            r = self._scan()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.adverts, [])
        self.assertEqual(self.discovers, [])


if __name__ == "__main__":
    unittest.main()

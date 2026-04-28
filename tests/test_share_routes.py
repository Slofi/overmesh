import json
import os
import sys
import tempfile
import unittest
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-share-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

from routes import mc as mc_routes  # noqa: E402
from routes import nodes as node_routes  # noqa: E402
from routes import radio as radio_routes  # noqa: E402
from state import connections, connections_lock, mc_connections, mc_connections_lock  # noqa: E402


class ShareRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(node_routes.bp)
        self.app.register_blueprint(radio_routes.bp)
        self.app.register_blueprint(mc_routes.bp)
        self.client = self.app.test_client()
        with connections_lock:
            connections.clear()
        with mc_connections_lock:
            mc_connections.clear()

    def tearDown(self):
        with connections_lock:
            connections.clear()
        with mc_connections_lock:
            mc_connections.clear()

    def test_mt_node_share_returns_public_key_and_qr(self):
        pubkey_b64 = base64.b64encode(b"\xaa\xbb\xcc\xdd").decode("ascii").rstrip("=")
        iface = SimpleNamespace(nodes={
            "abc-node-key": {
                "num": "abc-node-key",
                "user": {
                    "id": "!12345678",
                    "longName": "Trail Node",
                    "shortName": "TRAL",
                    "publicKey": pubkey_b64,
                },
                "deviceMetrics": {"batteryLevel": 91},
                "position": {"latitude": 46.1, "longitude": 14.5},
                "lastHeard": 123,
            }
        })
        with connections_lock:
            connections["mt1"] = {"status": "connected", "iface": iface, "config": {"name": "MT One"}}

        with mock.patch.object(node_routes, "_qr_svg", return_value="<svg/>"):
            res = self.client.get("/api/nodes/!12345678/share?radio_id=mt1")

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["details"]["public_key_hex"], "aabbccdd")
        self.assertIn("overmesh://mt/contact?", data["uri"])
        self.assertEqual(data["qr_svg"], "<svg/>")

    def test_mt_channel_share_returns_meshtastic_url(self):
        settings = SimpleNamespace(name="LongFast", psk=b"\x01")
        channel = SimpleNamespace(role=1, settings=settings)
        local_node = SimpleNamespace(channels=[channel])
        iface = SimpleNamespace(localNode=local_node)

        with mock.patch.object(radio_routes, "get_iface_by_radio", return_value=iface):
            with mock.patch.object(radio_routes, "_mt_channel_url", return_value="https://meshtastic.org/e/#abc"):
                with mock.patch.object(radio_routes, "_qr_svg", return_value="<svg/>"):
                    res = self.client.get("/api/radio/mt1/channels/0/share")

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["uri"], "https://meshtastic.org/e/#abc")
        self.assertEqual(data["details"]["name"], "LongFast")
        self.assertTrue(data["details"]["psk_set"])

    def test_mc_channel_share_includes_secret_hex(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "status": "connected",
                "config": {"name": "MC One"},
                "node_info": {"max_channels": 8},
            }
        channel = {
            "channel_idx": 2,
            "channel_name": "Ops",
            "channel_hash": "aa",
            "channel_secret": b"\x10" * 16,
        }
        with mock.patch.object(mc_routes, "get_channels", return_value=[channel]):
            with mock.patch.object(mc_routes, "_qr_svg", return_value="<svg/>"):
                res = self.client.get("/api/mc/mc1/channels/2/share")

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["details"]["secret_hex"], "10" * 16)
        self.assertIn("overmesh://mc/channel?", data["uri"])
        self.assertIn("secret=1010", data["uri"])

    def test_mc_channel_share_rejects_missing_secret(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "status": "connected",
                "config": {"name": "MC One"},
                "node_info": {"max_channels": 8},
            }
        channel = {"channel_idx": 2, "channel_name": "Ops", "channel_hash": "aa"}
        with mock.patch.object(mc_routes, "get_channels", return_value=[channel]):
            res = self.client.get("/api/mc/mc1/channels/2/share")

        self.assertEqual(res.status_code, 422)
        self.assertIn("secret unavailable", res.get_json()["error"])

    def test_qr_svg_handles_long_meshcore_contact_payload(self):
        payload = "meshcore://contact?" + ("a" * 720)

        svg = mc_routes._qr_svg(payload)

        self.assertIn("<svg", svg)
        self.assertIn("viewBox", svg)


if __name__ == "__main__":
    unittest.main()

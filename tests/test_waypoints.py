import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import routes.waypoints as waypoints  # noqa: E402


class _DummyIface:
    def __init__(self):
        self.sent = []

    def sendData(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self

    def execute(self, *args, **kwargs):
        return None


class WaypointEditValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(waypoints.bp)
        self.client = self.app.test_client()
        self.iface = _DummyIface()
        self.db = _DummyConn()
        self.base_wp = {
            "id": 42,
            "name": "Old",
            "description": "Old desc\n46.0000,14.0000",
            "lat": 46.0,
            "lon": 14.0,
            "radio_id": "node_1",
            "channel_index": 2,
            "destination_id": "!12345678",
            "destination_ids": ["!12345678"],
            "marker_emoji": "📍",
        }
        self.patches = [
            mock.patch.dict(waypoints.waypoints_cache, {42: dict(self.base_wp)}, clear=True),
            mock.patch.object(waypoints, "get_iface_by_radio", return_value=self.iface),
            mock.patch.object(waypoints, "get_any_iface_with_id", return_value=(self.iface, "node_1")),
            mock.patch.object(waypoints, "get_prefs_db", return_value=self.db),
            mock.patch.object(waypoints, "push_to_sse"),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(self._cleanup_patches)

    def _cleanup_patches(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_edit_rejects_non_list_destination_ids(self):
        resp = self.client.put(
            "/api/waypoints/42",
            json={"name": "Updated", "destination_ids": "!abcdef12"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "destination_ids must be a list")
        self.assertEqual(self.iface.sent, [])

    def test_edit_rejects_empty_destination_ids(self):
        resp = self.client.put(
            "/api/waypoints/42",
            json={"name": "Updated", "destination_ids": []},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "destination_ids must not be empty")
        self.assertEqual(self.iface.sent, [])

    def test_edit_sends_only_to_valid_destination_ids(self):
        resp = self.client.put(
            "/api/waypoints/42",
            json={"name": "Updated", "destination_ids": ["!12345678", "!87654321"]},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["ok"], True)
        self.assertEqual(
            [call[1]["destinationId"] for call in self.iface.sent],
            ["!12345678", "!87654321"],
        )


if __name__ == "__main__":
    unittest.main()

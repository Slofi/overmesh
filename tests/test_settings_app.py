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
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "app": {}, "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import routes.settings as settings_routes  # noqa: E402


class AppSettingsOmPositionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(settings_routes.bp)
        self.client = self.app.test_client()
        self.config_backup = json.loads(json.dumps(settings_routes.CONFIG))
        settings_routes.CONFIG.clear()
        settings_routes.CONFIG.update({
            "nodes": [],
            "mc_nodes": [],
            "app": {},
            "gps": {},
        })
        self.save_mock = mock.patch.object(settings_routes, "save_config").start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        settings_routes.CONFIG.clear()
        settings_routes.CONFIG.update(self.config_backup)

    def test_saves_manual_om_position(self):
        resp = self.client.post("/api/settings/app", json={"om_manual_lat": 46.0569472, "om_manual_lon": 14.5057514})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(settings_routes.CONFIG["app"]["om_manual_lat"], 46.056947)
        self.assertEqual(settings_routes.CONFIG["app"]["om_manual_lon"], 14.505751)
        self.save_mock.assert_called_once()

    def test_clears_manual_om_position(self):
        settings_routes.CONFIG["app"]["om_manual_lat"] = 46.1
        settings_routes.CONFIG["app"]["om_manual_lon"] = 14.5

        resp = self.client.post("/api/settings/app", json={"om_manual_lat": None, "om_manual_lon": None})

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("om_manual_lat", settings_routes.CONFIG["app"])
        self.assertNotIn("om_manual_lon", settings_routes.CONFIG["app"])

    def test_rejects_invalid_manual_om_position(self):
        resp = self.client.post("/api/settings/app", json={"om_manual_lat": 200, "om_manual_lon": 14.5})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "OM coordinates are out of range.")
        self.save_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

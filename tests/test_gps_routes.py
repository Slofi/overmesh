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

import routes.gps as gps_routes  # noqa: E402


class GpsSettingsConflictTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(gps_routes.bp)
        self.client = self.app.test_client()
        self.config_backup = json.loads(json.dumps(gps_routes.CONFIG))
        gps_routes.CONFIG.clear()
        gps_routes.CONFIG.update({
            "nodes": [{"id": "node_1", "name": "EDC-2", "enabled": True, "port": "/dev/ttyACM1"}],
            "mc_nodes": [],
            "gps": {"enabled": False, "port": ""},
        })
        self.start_mock = mock.patch.object(gps_routes, "_gps_start").start()
        self.stop_mock = mock.patch.object(gps_routes, "_gps_stop").start()
        self.save_mock = mock.patch.object(gps_routes, "save_config").start()
        self.conflict_mock = mock.patch.object(
            gps_routes,
            "gps_port_conflict",
            return_value={"network": "MT", "name": "EDC-2"},
        ).start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        gps_routes.CONFIG.clear()
        gps_routes.CONFIG.update(self.config_backup)

    def test_rejects_gps_port_conflict(self):
        resp = self.client.post("/api/settings/gps", json={"enabled": True, "port": "/dev/ttyACM1"})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.get_json()["error"],
            "GPS port /dev/ttyACM1 conflicts with enabled MT radio EDC-2.",
        )
        self.start_mock.assert_not_called()
        self.save_mock.assert_not_called()


class GpsSettingsMissingDongleTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(gps_routes.bp)
        self.client = self.app.test_client()
        self.config_backup = json.loads(json.dumps(gps_routes.CONFIG))
        gps_routes.CONFIG.clear()
        gps_routes.CONFIG.update({
            "nodes": [],
            "mc_nodes": [],
            "gps": {"enabled": False, "port": ""},
        })
        self.start_mock = mock.patch.object(gps_routes, "_gps_start").start()
        self.stop_mock = mock.patch.object(gps_routes, "_gps_stop").start()
        self.save_mock = mock.patch.object(gps_routes, "save_config").start()
        self.conflict_mock = mock.patch.object(gps_routes, "gps_port_conflict", return_value=None).start()
        self.present_mock = mock.patch.object(gps_routes, "gps_port_present", return_value=False).start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        gps_routes.CONFIG.clear()
        gps_routes.CONFIG.update(self.config_backup)

    def test_allows_save_but_reports_missing_dongle(self):
        resp = self.client.post("/api/settings/gps", json={"enabled": True, "port": "/dev/ttyUSB9"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json()["warning"],
            "GPS enabled, but port /dev/ttyUSB9 is not currently connected.",
        )
        self.start_mock.assert_not_called()
        self.stop_mock.assert_called_once()
        self.save_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

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
        self.thread_mock = mock.patch.object(settings_routes.threading, "Thread").start()
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

    def test_saves_sound_notification_preferences(self):
        resp = self.client.post(
            "/api/settings/app",
            json={
                "sound_notify_messages": False,
                "sound_notify_radio_connected": True,
                "sound_notify_nodes": False,
            },
        )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(settings_routes.CONFIG["app"]["sound_notify_messages"])
        self.assertTrue(settings_routes.CONFIG["app"]["sound_notify_radio_connected"])
        self.assertFalse(settings_routes.CONFIG["app"]["sound_notify_nodes"])
        self.save_mock.assert_called_once()

    def test_saves_distance_unit_preference(self):
        resp = self.client.post("/api/settings/app", json={"distance_unit": "mi"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(settings_routes.CONFIG["app"]["distance_unit"], "mi")

        get_resp = self.client.get("/api/settings/app")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.get_json()["distance_unit"], "mi")

    def test_app_settings_include_default_accent_color(self):
        resp = self.client.get("/api/settings/app")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["accent_color"], "#4ade80")

    def test_rejects_invalid_distance_unit_preference(self):
        resp = self.client.post("/api/settings/app", json={"distance_unit": "yards"})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "distance_unit must be 'km' or 'mi'")

    def test_ports_marks_direct_serial_ports_in_use(self):
        settings_routes.CONFIG["nodes"] = [
            {"id": "node_a", "name": "MT", "type": "serial", "port": "/dev/ttyACM0"}
        ]
        settings_routes.CONFIG["mc_nodes"] = [
            {"id": "mc_a", "name": "MC", "type": None, "port": "/dev/ttyACM1"}
        ]
        fake_ports = [
            mock.Mock(device="/dev/ttyACM0", description="MT", serial_number=None, vid=1, pid=2),
            mock.Mock(device="/dev/ttyACM1", description="MC", serial_number=None, vid=1, pid=3),
            mock.Mock(device="/dev/ttyACM2", description="Free", serial_number=None, vid=1, pid=4),
        ]
        with mock.patch("serial.tools.list_ports.comports", return_value=fake_ports):
            resp = self.client.get("/api/settings/ports")

        self.assertEqual(resp.status_code, 200)
        ports = {p["device"]: p for p in resp.get_json()["ports"]}
        self.assertTrue(ports["/dev/ttyACM0"]["in_use"])
        self.assertTrue(ports["/dev/ttyACM1"]["in_use"])
        self.assertFalse(ports["/dev/ttyACM2"]["in_use"])

    def test_adds_mc_tcp_radio(self):
        resp = self.client.post(
            "/api/settings/mc_nodes/add",
            json={
                "name": "MC TCP",
                "type": "tcp",
                "host": "192.168.1.50",
                "tcp_port": 4403,
            },
        )

        self.assertEqual(resp.status_code, 200)
        node = settings_routes.CONFIG["mc_nodes"][0]
        self.assertEqual(node["type"], "tcp")
        self.assertEqual(node["host"], "192.168.1.50")
        self.assertEqual(node["tcp_port"], 4403)
        self.assertEqual(node["port"], "192.168.1.50:4403")
        self.thread_mock.return_value.start.assert_called_once()

    def test_adds_mc_bluetooth_radio(self):
        resp = self.client.post(
            "/api/settings/mc_nodes/add",
            json={
                "name": "MC BT",
                "type": "bluetooth",
                "bt_address": "AA:BB:CC:DD:EE:FF",
                "bt_pin": "123456",
            },
        )

        self.assertEqual(resp.status_code, 200)
        node = settings_routes.CONFIG["mc_nodes"][0]
        self.assertEqual(node["type"], "ble")
        self.assertEqual(node["bt_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(node["bt_pin"], "123456")
        self.assertEqual(node["port"], "AA:BB:CC:DD:EE:FF")

    def test_adds_mc_serial_port_without_usb_serial(self):
        resp = self.client.post(
            "/api/settings/mc_nodes/add",
            json={
                "name": "MC Serial",
                "type": "serial",
                "usb_serial": "/dev/ttyACM9",
                "port": "/dev/ttyACM9",
            },
        )

        self.assertEqual(resp.status_code, 200)
        node = settings_routes.CONFIG["mc_nodes"][0]
        self.assertEqual(node["type"], "serial")
        self.assertNotIn("usb_serial", node)
        self.assertEqual(node["port"], "/dev/ttyACM9")

    def test_rejects_mc_tcp_without_host(self):
        resp = self.client.post(
            "/api/settings/mc_nodes/add",
            json={"name": "MC TCP", "type": "tcp", "tcp_port": 4403},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Enter an IP address or hostname")

    def test_rejects_mc_bluetooth_without_address(self):
        resp = self.client.post(
            "/api/settings/mc_nodes/add",
            json={"name": "MC BT", "type": "ble"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Enter a Bluetooth address")


if __name__ == "__main__":
    unittest.main()

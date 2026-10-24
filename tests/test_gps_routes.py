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
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import gps as gps_mod  # noqa: E402
import routes.gps as gps_routes  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402


class _RouteBase(unittest.TestCase):
    """Settings -> OM Position. The readers themselves are mocked: these tests are about what gets saved."""

    gps_cfg = {}
    nodes = []

    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(gps_routes.bp)
        self.client = self.app.test_client()
        self.config_backup = json.loads(json.dumps(gps_routes.CONFIG))
        gps_routes.CONFIG.clear()
        gps_routes.CONFIG.update({"nodes": json.loads(json.dumps(self.nodes)), "mc_nodes": [],
                                  "gps": json.loads(json.dumps(self.gps_cfg)), "silent_mode": False})
        self.start = mock.patch.object(gps_mod, "_gps_start").start()
        self.proxy = mock.patch.object(gps_mod, "_gps_start_proxy").start()
        mock.patch.object(gps_mod, "_gps_stop").start()
        mock.patch.object(gps_mod, "_gps_thread", None).start()
        self.save_mock = mock.patch.object(gps_routes, "save_config").start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        gps_routes.CONFIG.clear()
        gps_routes.CONFIG.update(self.config_backup)

    def ports(self, *ps):
        return mock.patch("serial.tools.list_ports.comports", return_value=[
            NS(device=d, serial_number=sn, vid=v, pid=p, product=pr, manufacturer="", description=pr)
            for d, sn, v, p, pr in ps])


UBLOX = ("/dev/ttyACM2", None, 0x1546, 0x01A7, "u-blox 7 - GPS/GNSS Receiver")
CP2102 = ("/dev/ttyUSB0", "0001", 0x10C4, 0xEA60, "CP2102")


class GpsSettingsConflictTests(_RouteBase):
    nodes = [{"id": "node_1", "name": "EDC-2", "enabled": True, "port": "/dev/ttyACM1"}]

    def test_rejects_gps_port_conflict(self):
        with mock.patch.object(gps_routes, "gps_port_conflict", return_value={"network": "MT", "name": "EDC-2"}), \
             self.ports():
            resp = self.client.post("/api/settings/gps", json={"source": "usb", "port": "/dev/ttyACM1"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "GPS port /dev/ttyACM1 conflicts with enabled MT radio EDC-2.")
        self.start.assert_not_called()
        self.save_mock.assert_not_called()


class GpsSettingsSourceTests(_RouteBase):
    """ONE source, chosen by hand; hand-picked is the default (Filip, S471)."""

    def test_default_is_the_hand_picked_position(self):
        body = self.client.get("/api/settings/gps").get_json()
        self.assertEqual(body["source"], "manual")
        self.assertFalse(body["auto_push_mt"])
        self.assertFalse(body["auto_push_mc"])

    def test_choosing_manual_reads_nothing(self):
        with self.ports(UBLOX):
            resp = self.client.post("/api/settings/gps", json={"source": "manual"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(gps_routes.CONFIG["gps"]["source"], "manual")
        self.start.assert_not_called()
        self.proxy.assert_not_called()

    def test_choosing_ops_toc_only_polls_ops_toc(self):
        with self.ports(UBLOX):
            self.client.post("/api/settings/gps", json={"source": "proxy"})
        self.proxy.assert_called_once()
        self.start.assert_not_called()

    def test_usb_dongle_missing_is_reported(self):
        with self.ports():
            resp = self.client.post("/api/settings/gps", json={"source": "usb", "port": "/dev/ttyUSB9"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["warning"], "Not reading /dev/ttyUSB9: /dev/ttyUSB9 is not connected.")
        self.start.assert_not_called()
        self.save_mock.assert_called_once()

    def test_internal_refuses_a_usb_port_and_usb_refuses_a_uart(self):
        with self.ports(UBLOX):
            r1 = self.client.post("/api/settings/gps", json={"source": "internal", "port": "/dev/ttyACM2"})
            r2 = self.client.post("/api/settings/gps", json={"source": "usb", "port": "/dev/ttyS3"})
        self.assertEqual((r1.status_code, r2.status_code), (400, 400))
        self.save_mock.assert_not_called()

    def test_old_settings_page_still_saves_sensibly(self):
        # A browser tab opened before the update posts enabled + direct/proxy.
        with self.ports(UBLOX):
            self.client.post("/api/settings/gps", json={"enabled": False, "source": "proxy"})
            self.assertEqual(gps_routes.CONFIG["gps"]["source"], "manual")
            self.client.post("/api/settings/gps", json={"enabled": True, "source": "direct", "port": "/dev/ttyACM2"})
            self.assertEqual(gps_routes.CONFIG["gps"]["source"], "usb")


class GpsSettingsMigrationTests(_RouteBase):
    # CD's config before S471, verbatim in shape.
    gps_cfg = {"enabled": True, "port": "/dev/ttyACM0", "auto_push": True, "push_interval": 30,
               "precision": 32, "precision_meters": 0, "source": "proxy", "proxy_url": "http://localhost:8090"}

    def test_read_as_ops_toc_until_changed(self):
        self.assertEqual(self.client.get("/api/settings/gps").get_json()["source"], "proxy")

    def test_saving_moves_old_fields_to_the_new_shape(self):
        with self.ports():
            self.client.post("/api/settings/gps", json={"source": "manual"})
        cfg = gps_routes.CONFIG["gps"]
        self.assertEqual(cfg["source"], "manual")
        self.assertNotIn("enabled", cfg)
        self.assertNotIn("port", cfg)
        self.assertEqual(cfg["usb_port"], "/dev/ttyACM0", "the old port is kept as the USB port, not lost")
        self.assertNotIn("auto_push", cfg)
        self.assertIs(cfg["auto_push_mt"], True, "the old toggle only ever pushed to MT")
        self.assertNotIn("auto_push_mc", cfg)

    def test_a_remembered_radio_port_does_not_block_switching_source(self):
        # CD after the update: usb_port is the old /dev/ttyACM0, which is now the CD-MT radio.
        gps_routes.CONFIG["nodes"] = [{"id": "n1", "name": "CD-MT", "enabled": True, "usb_serial": "68DF"}]
        with self.ports(("/dev/ttyACM0", "68DF", 0x239A, 0xB3, "ProMicro")):
            resp = self.client.post("/api/settings/gps", json={"source": "usb"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(gps_routes.CONFIG["gps"]["source"], "usb", "the switch must stick")
        self.assertIn("is the MT radio CD-MT", resp.get_json()["warning"])
        self.start.assert_not_called()


class GpsSettingsIdentityTests(_RouteBase):
    """The USB GPS is remembered by WHICH device was picked; a saved radio's board can never be picked."""

    nodes = [{"id": "n1", "name": "CD-MT", "enabled": False, "port": "/dev/ttyACM5", "usb_serial": "68DF"}]

    def test_rejects_a_disabled_radio_board_as_gps(self):
        # The enabled-radio conflict check cannot see it (CD-MT is disabled): the serial match must.
        with self.ports(("/dev/ttyACM0", "68DF", 0x239A, 0xB3, "ProMicro")):
            resp = self.client.post("/api/settings/gps", json={"source": "usb", "port": "/dev/ttyACM0"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "GPS port /dev/ttyACM0 is the MT radio CD-MT.")
        self.save_mock.assert_not_called()

    def test_saves_the_picked_device_identity(self):
        with self.ports(CP2102):
            resp = self.client.post("/api/settings/gps", json={"source": "usb", "port": "/dev/ttyUSB0"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(gps_routes.CONFIG["gps"]["usb_id"], {"serial": "0001", "vid": 0x10C4, "pid": 0xEA60})

    def test_switching_to_internal_keeps_the_usb_choice(self):
        gps_routes.CONFIG["gps"] = {"source": "usb", "usb_port": "/dev/ttyUSB0", "usb_id": {"serial": "0001"}}
        with self.ports(), mock.patch.object(gps_mod.os.path, "exists", return_value=True):
            self.client.post("/api/settings/gps", json={"source": "internal", "port": "/dev/ttyS3"})
        cfg = gps_routes.CONFIG["gps"]
        self.assertEqual((cfg["internal_port"], cfg["usb_port"], cfg["usb_id"]),
                         ("/dev/ttyS3", "/dev/ttyUSB0", {"serial": "0001"}))

    def test_unchanged_port_unplugged_keeps_its_identity(self):
        gps_routes.CONFIG["gps"] = {"source": "usb", "usb_port": "/dev/ttyUSB0", "usb_id": {"serial": "0001"}}
        with self.ports():
            self.client.post("/api/settings/gps", json={"source": "usb", "port": "/dev/ttyUSB0"})
        self.assertEqual(gps_routes.CONFIG["gps"]["usb_id"], {"serial": "0001"})


class GpsSettingsGetTests(_RouteBase):
    def test_get_keeps_the_configured_source_and_reports_the_live_one_apart(self):
        gps_routes.CONFIG["gps"] = {"source": "proxy"}
        with mock.patch.dict(gps_routes._gps_runtime, {"source": "usb", "port": "/dev/ttyACM2"}), self.ports():
            body = self.client.get("/api/settings/gps").get_json()
        self.assertEqual(body["source"], "proxy")
        self.assertEqual(body["active_source"], "usb")

    def test_get_in_proxy_mode_shows_ops_toc_not_a_port(self):
        gps_routes.CONFIG["gps"] = {"source": "proxy", "usb_port": "/dev/ttyUSB7"}
        with mock.patch.dict(gps_routes._gps_runtime, {"source": "proxy", "port": "OPS-TOC (x)", "port_present": True}), \
             self.ports():
            body = self.client.get("/api/settings/gps").get_json()
        self.assertEqual(body["port"], "OPS-TOC (x)")
        self.assertTrue(body["port_present"])


class GpsShareNowTests(_RouteBase):
    """Share with radios now: per network, never the hand-picked position."""

    gps_cfg = {"source": "usb", "usb_port": "/dev/ttyACM2", "precision": 32}

    def setUp(self):
        super().setUp()
        with gps_mod.gps_lock:
            gps_mod.gps_state.update({"fix": True, "lat": 46.05, "lon": 14.5, "alt": 300})
        self.addCleanup(lambda: gps_mod.gps_state.update({"fix": False, "lat": None, "lon": None, "alt": None}))
        self.thread = mock.patch("threading.Thread").start()

    def test_mt_and_mc_go_to_their_own_push(self):
        self.client.post("/api/gps/push", json={"network": "mt"})
        self.client.post("/api/gps/push", json={"network": "mc"})
        targets = [c.kwargs["target"] for c in self.thread.call_args_list]
        self.assertEqual(targets, [gps_routes._gps_push_to_nodes, gps_routes._gps_push_to_mc])

    def test_hand_picked_position_is_never_shared(self):
        gps_routes.CONFIG["gps"] = {"source": "manual"}
        resp = self.client.post("/api/gps/push", json={"network": "mc"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("never sent to radios", resp.get_json()["error"])
        self.thread.assert_not_called()

    def test_unknown_network_refused(self):
        self.assertEqual(self.client.post("/api/gps/push", json={"network": "lora"}).status_code, 400)


class GpsStartupSourceTests(unittest.TestCase):
    def test_startup_uses_the_same_apply_path_as_settings(self):
        src = Path(REPO, "app.py").read_text(encoding="utf-8")
        start = src[src.index("def startup"):]
        start = start[:start.index("pub.subscribe")]
        self.assertIn("gps_apply_config(restart=True)", start)
        self.assertNotIn("_gps_start(", start, "startup must not open a GPS port on its own")


if __name__ == "__main__":
    unittest.main()

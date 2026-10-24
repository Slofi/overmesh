"""OM must only ever open the GPS, never a radio that happens to sit on the GPS's old /dev/ttyACMx name.

Found live on CD 2026-10-24 (S471, board swap): the T114 (CD-MC) was off, the FakeTec, freshly flashed with
Meshtastic and not yet added to OM, came up on /dev/ttyACM0. OPS-TOC was not running, so the GPS proxy fell back
to its saved port /dev/ttyACM0 and OM opened the radio and wrote UBX setup bytes to it. The same swap hid the
FakeTec from Settings -> Add radio as "in use" by CD-MC, because CD-MC's saved path was ttyACM0.

Contract (identity, not path):
  * a board whose USB serial belongs to ANY saved radio (enabled or not) is never the GPS;
  * a saved usb_id must match the device behind the port; without one, the device must identify as a GPS;
  * internal UARTs (ttyS*, ttyAS*) are fixed wiring: their path is their identity;
  * ONE source, chosen by hand: hand-picked (default, reads nothing) / internal UART / picked USB dongle / OPS-TOC,
    no fallback and no auto-detect between them (Filip, S471: "this time I want no automation");
  * sharing with radios is per network (MT, MC), off by default, never the hand-picked position;
  * a radio whose board is unplugged claims no port, in the GPS conflict check as in the Add-radio list.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

import gps  # noqa: E402


def _port(device, serial=None, vid=None, pid=None, product=None, manufacturer=None):
    return SimpleNamespace(device=device, serial_number=serial, vid=vid, pid=pid, product=product,
                           manufacturer=manufacturer, description=product or "n/a")


FAKETEC = _port("/dev/ttyACM0", "68DF6C579D8FC812", 0x239A, 0x00B3, "ProMicro compatible nRF52840", "Nologo")
T114 = _port("/dev/ttyACM1", "4925104047D17CA4", 0x239A, 0x8029, "HT-n5262", "Heltec")
UBLOX = _port("/dev/ttyACM2", None, 0x1546, 0x01A7, "u-blox 7 - GPS/GNSS Receiver", "u-blox AG - www.u-blox.com")
CP2102_GPS = _port("/dev/ttyUSB0", "0001", 0x10C4, 0xEA60, "CP2102 USB to UART Bridge Controller", "Silicon Labs")


class _Base(unittest.TestCase):
    def setUp(self):
        # Every key a test here may touch is restored: a leaked silent_mode=True made later MC/MT tests get 409.
        self._saved = {k: gps.CONFIG.get(k) for k in ("nodes", "mc_nodes", "gps", "silent_mode")}
        gps.CONFIG["nodes"] = []
        gps.CONFIG["mc_nodes"] = [{"id": "mc_1", "name": "CD-MC", "enabled": True, "type": "serial",
                                   "port": "/dev/ttyACM0", "usb_serial": T114.serial_number}]
        gps.CONFIG["gps"] = {"enabled": True, "source": "proxy", "port": "/dev/ttyACM0"}

    def tearDown(self):
        for k, v in self._saved.items():
            gps.CONFIG[k] = v

    def ports(self, *ps):
        return mock.patch("serial.tools.list_ports.comports", return_value=list(ps))


class DeviceCheckTests(_Base):
    def test_the_s471_case_an_unadded_radio_on_the_saved_path_is_refused(self):
        # T114 off, FakeTec on ttyACM0, FakeTec NOT saved as a radio yet: the old code opened it.
        with self.ports(FAKETEC):
            ok, why = gps.gps_device_check("/dev/ttyACM0")
        self.assertFalse(ok)
        self.assertIn("does not identify as a GPS", why)

    def test_a_saved_radio_is_never_the_gps_even_when_disabled(self):
        gps.CONFIG["nodes"] = [{"id": "n1", "name": "CD-MT", "enabled": False, "usb_serial": FAKETEC.serial_number}]
        with self.ports(FAKETEC):
            ok, why = gps.gps_device_check("/dev/ttyACM0", usb_id={"serial": FAKETEC.serial_number})
            radio = gps.gps_port_radio("/dev/ttyACM0")
        self.assertFalse(ok, "a radio must be refused even if a usb_id somehow names it")
        self.assertEqual(radio, "MT radio CD-MT")

    def test_a_gps_dongle_is_recognised_without_a_saved_identity(self):
        with self.ports(UBLOX):
            self.assertEqual(gps.gps_device_check("/dev/ttyACM2"), (True, ""))

    def test_a_saved_usb_id_trusts_a_generic_uart_gps_and_only_that_device(self):
        uid = {"serial": "0001", "vid": 0x10C4, "pid": 0xEA60}
        with self.ports(CP2102_GPS):
            self.assertEqual(gps.gps_device_check("/dev/ttyUSB0", uid), (True, ""))
            self.assertFalse(gps.gps_device_check("/dev/ttyUSB0")[0], "no usb_id: a bare CP2102 is not trusted")
        other = _port("/dev/ttyUSB0", "XYZ", 0x1A86, 0x7523, "USB Serial")
        with self.ports(other):
            ok, why = gps.gps_device_check("/dev/ttyUSB0", uid)
        self.assertFalse(ok)
        self.assertIn("not the GPS picked in Settings", why)

    def test_a_usb_id_without_serial_matches_on_vid_pid(self):
        uid = {"serial": "", "vid": 0x1546, "pid": 0x01A7}
        with self.ports(UBLOX):
            self.assertTrue(gps.gps_device_check("/dev/ttyACM2", uid)[0])
        with self.ports(_port("/dev/ttyACM2", None, 0x239A, 0x00B3, "ProMicro")):
            self.assertFalse(gps.gps_device_check("/dev/ttyACM2", uid)[0])

    def test_internal_uart_path_is_its_identity(self):
        with mock.patch.object(gps.os.path, "exists", return_value=True), self.ports():
            self.assertEqual(gps.gps_device_check("/dev/ttyS3"), (True, ""))
        with mock.patch.object(gps.os.path, "exists", return_value=False), self.ports():
            self.assertFalse(gps.gps_device_check("/dev/ttyS3")[0])


class ConflictCheckTests(_Base):
    def test_an_unplugged_radio_claims_no_port(self):
        # CD-MC (T114) is off; whatever sits on its old ttyACM0 is not "the CD-MC radio".
        with self.ports(_port("/dev/ttyACM0", None, 0x1546, 0x01A7, "u-blox 7")):
            self.assertIsNone(gps.gps_port_conflict("/dev/ttyACM0"))

    def test_a_plugged_radio_still_claims_its_current_port(self):
        with self.ports(T114):
            self.assertEqual(gps.gps_port_conflict("/dev/ttyACM1"), {"network": "MC", "name": "CD-MC"})


class SourceModelTests(_Base):
    """ONE source, chosen by hand (Filip, S471). Old configs keep meaning what they meant."""

    def test_reading_old_and_new_configs(self):
        cases = [
            ({}, "manual"),
            ({"enabled": False, "source": "proxy", "port": "/dev/ttyACM0"}, "manual"),
            ({"enabled": True, "source": "proxy", "port": "/dev/ttyACM0"}, "proxy"),   # CD before S471
            ({"enabled": True, "source": "direct", "port": "/dev/ttyS3"}, "internal"),
            ({"enabled": True, "source": "direct", "port": "/dev/ttyAS2"}, "internal"),  # HD
            ({"enabled": True, "source": "direct", "port": "/dev/ttyACM2"}, "usb"),
            ({"source": "usb"}, "usb"),
            ({"source": "something-else"}, "manual"),
        ]
        for cfg, want in cases:
            with self.subTest(cfg=cfg):
                self.assertEqual(gps.gps_source(cfg), want)

    def test_each_serial_source_keeps_its_own_port(self):
        cfg = {"source": "usb", "internal_port": "/dev/ttyS3", "usb_port": "/dev/ttyACM2"}
        self.assertEqual(gps.gps_source_port(cfg), "/dev/ttyACM2")
        self.assertEqual(gps.gps_source_port(cfg, "internal"), "/dev/ttyS3")
        self.assertEqual(gps.gps_source_port(cfg, "proxy"), "")
        self.assertEqual(gps.gps_source_port({"enabled": True, "port": "/dev/ttyS3"}, "internal"), "/dev/ttyS3")
        self.assertEqual(gps.gps_source_port({"enabled": True, "port": "/dev/ttyS3"}, "usb"), "")


class ApplyConfigTests(_Base):
    """gps_apply_config is the only thing that starts a reader: Settings save, startup and the watchdog."""

    def setUp(self):
        super().setUp()
        self.start = mock.patch.object(gps, "_gps_start").start()
        self.proxy = mock.patch.object(gps, "_gps_start_proxy").start()
        self.stop = mock.patch.object(gps, "_gps_stop").start()
        mock.patch.object(gps, "_gps_thread", None).start()
        self.addCleanup(mock.patch.stopall)

    def test_manual_reads_nothing(self):
        gps.CONFIG["gps"] = {"source": "manual", "usb_port": "/dev/ttyACM2"}
        with self.ports(UBLOX):
            self.assertIsNone(gps.gps_apply_config(restart=True))
        self.start.assert_not_called()
        self.proxy.assert_not_called()

    def test_manual_stops_a_running_reader(self):
        gps.CONFIG["gps"] = {"source": "manual"}
        alive = mock.Mock(is_alive=mock.Mock(return_value=True))
        with mock.patch.object(gps, "_gps_thread", alive):
            gps.gps_apply_config()
        self.stop.assert_called_once()

    def test_proxy_never_touches_a_serial_port(self):
        # CD's config before S471: proxy with a saved /dev/ttyACM0, a GPS dongle plugged in too.
        gps.CONFIG["gps"] = {"enabled": True, "source": "proxy", "port": "/dev/ttyACM2"}
        with self.ports(UBLOX):
            gps.gps_apply_config(restart=True)
        self.proxy.assert_called_once_with("http://localhost:8090")
        self.start.assert_not_called()

    def test_usb_source_refuses_an_unadded_radio_on_its_port(self):
        gps.CONFIG["gps"] = {"source": "usb", "usb_port": "/dev/ttyACM0"}
        with self.ports(FAKETEC):
            warning = gps.gps_apply_config(restart=True)
        self.start.assert_not_called()
        self.assertIn("does not identify as a GPS", warning)

    def test_usb_source_reads_the_picked_dongle(self):
        gps.CONFIG["gps"] = {"source": "usb", "usb_port": "/dev/ttyACM2"}
        with self.ports(UBLOX):
            self.assertIsNone(gps.gps_apply_config(restart=True))
        self.start.assert_called_once_with("/dev/ttyACM2")

    def test_internal_source_refuses_a_usb_port(self):
        gps.CONFIG["gps"] = {"source": "internal", "internal_port": "/dev/ttyACM2"}
        with self.ports(UBLOX):
            warning = gps.gps_apply_config(restart=True)
        self.start.assert_not_called()
        self.assertIn("not a built-in UART", warning)

    def test_internal_source_reads_its_uart(self):
        gps.CONFIG["gps"] = {"source": "internal", "internal_port": "/dev/ttyS3"}
        with self.ports(), mock.patch.object(gps.os.path, "exists", return_value=True):
            self.assertIsNone(gps.gps_apply_config(restart=True))
        self.start.assert_called_once_with("/dev/ttyS3")

    def test_watchdog_only_restarts_a_dead_reader(self):
        gps.CONFIG["gps"] = {"source": "usb", "usb_port": "/dev/ttyACM2"}
        alive = mock.Mock(is_alive=mock.Mock(return_value=True))
        with mock.patch.object(gps, "_gps_thread", alive), self.ports(UBLOX):
            gps.gps_apply_config(restart=False)
        self.start.assert_not_called()


class SharePerNetworkTests(_Base):
    """Two toggles, MT and MC (Filip, S471). Both off by default; the old `auto_push` only ever meant MT."""

    def setUp(self):
        super().setUp()
        gps.CONFIG["silent_mode"] = False
        gps._gps_last_push_ts = gps._gps_last_push_mc_ts = 0.0
        self.threads = mock.patch("threading.Thread").start()
        self.addCleanup(mock.patch.stopall)

    def targets(self):
        return [c.kwargs.get("target") for c in self.threads.call_args_list]

    def test_both_off_by_default(self):
        gps.CONFIG["gps"] = {"source": "usb"}
        gps._gps_auto_push(46.0, 14.5, 300)
        self.threads.assert_not_called()

    def test_mt_only(self):
        gps.CONFIG["gps"] = {"source": "usb", "auto_push_mt": True}
        gps._gps_auto_push(46.0, 14.5, 300)
        self.assertEqual(self.targets(), [gps._gps_push_to_nodes])

    def test_mc_only(self):
        gps.CONFIG["gps"] = {"source": "usb", "auto_push_mc": True}
        gps._gps_auto_push(46.0, 14.5, 300)
        self.assertEqual(self.targets(), [gps._gps_push_to_mc])

    def test_old_auto_push_means_mt(self):
        gps.CONFIG["gps"] = {"enabled": True, "auto_push": True}
        gps._gps_auto_push(46.0, 14.5, 300)
        self.assertEqual(self.targets(), [gps._gps_push_to_nodes])

    def test_each_network_has_its_own_clock(self):
        gps.CONFIG["gps"] = {"source": "usb", "auto_push_mt": True, "auto_push_mc": True, "push_interval": 60}
        gps._gps_auto_push(46.0, 14.5, 300)
        gps._gps_auto_push(46.0, 14.5, 300)            # within the interval: nothing more
        self.assertEqual(sorted(t.__name__ for t in self.targets()), ["_gps_push_to_mc", "_gps_push_to_nodes"])

    def test_silent_running_blocks_both(self):
        gps.CONFIG["silent_mode"] = True
        gps.CONFIG["gps"] = {"source": "usb", "auto_push_mt": True, "auto_push_mc": True}
        gps._gps_auto_push(46.0, 14.5, 300)
        self.threads.assert_not_called()


class McPushTests(_Base):
    def test_mc_push_sets_blurred_coords_on_connected_companions(self):
        gps.CONFIG["silent_mode"] = False
        conns = {"mc_1": {"status": "connected"}, "mc_2": {"status": "disconnected"}}
        with mock.patch.object(gps, "mc_connections", conns), \
             mock.patch("mesh_mc.set_device_coords") as setc:
            done = gps._gps_push_to_mc(46.0391351, 14.4974192, 16)
        self.assertEqual(done, ["mc_1"])
        (rid, lat, lon), _ = setc.call_args
        self.assertEqual(rid, "mc_1")
        self.assertNotEqual((lat, lon), (46.0391351, 14.4974192), "precision 16 must blur")
        self.assertLess(abs(lat - 46.0391351), 0.0066)   # 2**16 * 1e-7 = 0.0066 deg cell
        self.assertLess(abs(lon - 14.4974192), 0.0066)

    def test_mc_push_refused_while_silent(self):
        gps.CONFIG["silent_mode"] = True
        with mock.patch.object(gps, "mc_connections", {"mc_1": {"status": "connected"}}), \
             mock.patch("mesh_mc.set_device_coords") as setc:
            self.assertEqual(gps._gps_push_to_mc(46.0, 14.5, 32), [])
        setc.assert_not_called()

    def test_blur_matches_meshtastic_precision(self):
        self.assertEqual(gps._blur_coord(46.0391351, 32), 46.0391351)
        self.assertEqual(gps._blur_coord(46.0391351, 16), gps._blur_coord(46.0391000, 16), "same cell, same value")
        self.assertLess(gps._blur_coord(-14.5, 16), 0, "negative coordinates stay negative")


if __name__ == "__main__":
    unittest.main()

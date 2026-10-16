"""MT Silent Running must be reconciled whenever a radio appears (S453).

MT is silenced at the FIRMWARE level (lora.tx_enabled = False), so the flag is
state on the device, and a radio that is not connected at that moment simply
does not have it. Until 2026-10-16 _silent_enable() walked only the radios that
were connected right then, and nothing re-applied it later: a radio that was
unplugged or rebooting when Silent Running was switched on came back
transmitting, with no snapshot entry either. The same shape existed in the other
direction — a snapshotted radio that was away when Silent Running was switched
off kept tx_enabled=False for good.

Both directions are now reconciled from the MT connect path, per radio, and a
radio Silent Running never silenced is never written to (switching Silent
Running off must not *enable* transmitting on it).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-silent-mt-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}),
                        encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

import mesh as mesh_mod  # noqa: E402
import routes.silent as silent_routes  # noqa: E402
from config import CONFIG  # noqa: E402


class _Lora:
    def __init__(self, tx):
        self.tx_enabled = tx


class _Device:
    def __init__(self, role):
        self.role = role


class _LocalNode:
    def __init__(self, tx, role=0):
        self.localConfig = type("LC", (), {"lora": _Lora(tx), "device": _Device(role)})()
        self.config_writes = []

    def writeConfig(self, section):
        self.config_writes.append(section)


class _Iface:
    def __init__(self, tx, role=0):
        self.localNode = _LocalNode(tx, role)

    @property
    def tx_enabled(self):
        return self.localNode.localConfig.lora.tx_enabled


class _SilentMtTestCase(unittest.TestCase):
    def setUp(self):
        self._backup = json.loads(json.dumps(CONFIG))
        CONFIG.clear()
        CONFIG.update({"nodes": [], "mc_nodes": [], "silent_mode": False})
        # config.json must never be written by a test
        patcher = mock.patch.object(silent_routes, "save_config")
        self.save_config_silent = patcher.start()
        patcher2 = mock.patch.object(mesh_mod, "save_config")
        self.save_config_mesh = patcher2.start()
        patcher3 = mock.patch.object(silent_routes, "push_to_sse")
        patcher3.start()
        self.ifaces = {}
        patcher4 = mock.patch.object(silent_routes, "get_iface_by_radio",
                                     side_effect=lambda rid: self.ifaces.get(rid))
        patcher4.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        CONFIG.clear()
        CONFIG.update(self._backup)

    def _node(self, node_id, name):
        CONFIG.setdefault("nodes", []).append({"id": node_id, "enabled": True, "name": name})

    def _snapshot(self):
        return CONFIG.get("silent_snapshot")


class EnableDisableTests(_SilentMtTestCase):
    def test_connected_radio_is_silenced_and_snapshotted(self):
        self._node("r1", "ERA-1")
        self.ifaces["r1"] = _Iface(tx=True)
        silent_routes._silent_enable()
        self.assertFalse(self.ifaces["r1"].tx_enabled)
        self.assertEqual(self.ifaces["r1"].localNode.config_writes, ["lora"])
        self.assertEqual(self._snapshot()["r1"]["tx_enabled"], True)

    def test_radio_away_at_enable_is_not_snapshotted_but_is_silenced_on_connect(self):
        self._node("r1", "ERA-1")
        self._node("r2", "ERA-9 (away)")
        self.ifaces["r1"] = _Iface(tx=True)
        silent_routes._silent_enable()
        self.assertNotIn("r2", self._snapshot() or {})

        # it comes back, with the firmware value it always had
        self.ifaces["r2"] = _Iface(tx=True)
        mesh_mod.sync_silent_running_on_mt_connect(self.ifaces["r2"], "r2")
        self.assertFalse(self.ifaces["r2"].tx_enabled, "it must not transmit after reconnect")
        self.assertEqual(self._snapshot()["r2"]["tx_enabled"], True,
                         "its own value must be remembered for the restore")

    def test_radio_away_at_disable_gets_its_own_value_back_on_connect(self):
        self._node("r1", "ERA-1")
        self._node("r2", "ERA-9 (away)")
        self.ifaces["r1"] = _Iface(tx=True)
        silent_routes._silent_enable()
        self.ifaces["r2"] = _Iface(tx=True)
        mesh_mod.sync_silent_running_on_mt_connect(self.ifaces["r2"], "r2")
        del self.ifaces["r2"]                       # away again

        silent_routes._silent_disable()
        self.assertFalse(CONFIG["silent_mode"])
        self.assertIn("r2", self._snapshot() or {},
                      "an away radio keeps its entry until it can be restored")

        self.ifaces["r2"] = _Iface(tx=False)        # still carrying our False
        mesh_mod.sync_silent_running_on_mt_connect(self.ifaces["r2"], "r2")
        self.assertTrue(self.ifaces["r2"].tx_enabled, "its own value is handed back")
        self.assertIsNone(self._snapshot(), "and the entry is gone once used")

    def test_radio_never_silenced_is_not_written_when_disabling(self):
        self._node("r1", "ERA-1")
        self.ifaces["r1"] = _Iface(tx=True)
        silent_routes._silent_enable()
        # a radio that appears AFTER the snapshot and was never silenced
        self._node("r9", "ERA-9")
        self.ifaces["r9"] = _Iface(tx=False)        # user's own choice, not ours
        silent_routes._silent_disable()
        self.assertFalse(self.ifaces["r9"].tx_enabled)
        self.assertEqual(self.ifaces["r9"].localNode.config_writes, [],
                         "Silent Running off must not enable TX on a radio it never silenced")

    def test_first_value_wins_on_reconnect(self):
        """A reconnect sees the False we wrote; the restore value must stay the original."""
        self._node("r1", "ERA-1")
        self.ifaces["r1"] = _Iface(tx=True)
        silent_routes._silent_enable()
        self.ifaces["r1"] = _Iface(tx=False)        # it reconnected, still silenced
        mesh_mod.sync_silent_running_on_mt_connect(self.ifaces["r1"], "r1")
        self.assertEqual(self._snapshot()["r1"]["tx_enabled"], True,
                         "the snapshot must not be overwritten by the value we wrote")
        self.assertEqual(self.ifaces["r1"].localNode.config_writes, [],
                         "an already-silent radio needs no further device write")

    def test_enabling_sets_the_flag_before_it_walks_the_radios(self):
        """A radio connecting during the enable loop must match the new state."""
        self._node("r1", "ERA-1")
        self._node("r2", "ERA-2")
        seen = {}

        class _WatchingIface(_Iface):
            def __init__(self):
                super().__init__(tx=True)

            def _note(self):
                seen["silent_mode"] = CONFIG.get("silent_mode")

        def fake_get(rid):
            seen.setdefault("flag_when_r1_handled", CONFIG.get("silent_mode"))
            return self.ifaces.get(rid)

        self.ifaces["r1"] = _Iface(tx=True)
        with mock.patch.object(silent_routes, "get_iface_by_radio", side_effect=fake_get):
            silent_routes._silent_enable()
        self.assertIs(seen["flag_when_r1_handled"], True,
                      "silent_mode must already be True while the radios are being silenced")


if __name__ == "__main__":
    unittest.main()

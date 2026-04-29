import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import helpers  # noqa: E402
import db  # noqa: E402
import state  # noqa: E402


class MtNodeIdFormattingTests(unittest.TestCase):
    def setUp(self):
        db.init_prefs_db()
        db._last_logged_pos.clear()
        with state.connections_lock:
            state.connections.clear()
        with state.mt_last_heard_lock:
            state.mt_last_heard.clear()
        with db.get_prefs_db() as conn:
            conn.execute("DELETE FROM nodes")
            conn.execute("DELETE FROM node_position_history")

    def test_mt_node_id_from_num_zero_pads_hex(self):
        self.assertEqual(helpers.mt_node_id_from_num(0x1234), "!00001234")

    def test_mt_node_id_from_num_returns_none_for_invalid_input(self):
        self.assertIsNone(helpers.mt_node_id_from_num(None))
        self.assertIsNone(helpers.mt_node_id_from_num("bad"))

    def test_get_node_name_falls_back_to_db_archive(self):
        db.upsert_node({
            "id": "!12345678",
            "long_name": "Archive Node",
            "short_name": "ARCH",
            "snr": None,
            "rssi": None,
            "battery": None,
            "latitude": None,
            "longitude": None,
            "hops_away": 0,
            "radio_id": "mt_test",
            "is_local": False,
            "last_heard_ts": 123,
        })

        self.assertEqual(helpers.get_node_name("!12345678"), "Archive Node")
        self.assertEqual(helpers.get_node_short_name("!12345678"), "ARCH")

    def test_clear_position_history_clears_in_memory_cache_for_node(self):
        node_id = "!12345678"
        radio_id = "mt_test"
        ts = int(time.time())

        db.log_position(node_id, radio_id, 46.0, 14.0, ts)
        db.clear_position_history(node_id)
        db.log_position(node_id, radio_id, 46.0, 14.0, ts + 1)

        rows = db.get_position_history(node_id, hours=24)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], ts + 1)

    def test_get_db_nodes_distance_handles_zero_coordinates(self):
        db.upsert_node({
            "id": "!local",
            "long_name": "Local",
            "short_name": "LOC",
            "snr": None,
            "rssi": None,
            "battery": None,
            "latitude": 0.0,
            "longitude": 0.0,
            "hops_away": 0,
            "radio_id": "mt_test",
            "is_local": True,
            "last_heard_ts": 100,
        })
        db.upsert_node({
            "id": "!remote",
            "long_name": "Remote",
            "short_name": "REM",
            "snr": None,
            "rssi": None,
            "battery": None,
            "latitude": 0.0,
            "longitude": 1.0,
            "hops_away": 1,
            "radio_id": "mt_test",
            "is_local": False,
            "last_heard_ts": 101,
        })

        rows = {row["id"]: row for row in db.get_db_nodes()}
        self.assertAlmostEqual(rows["!remote"]["distance"], 111.19, places=1)

    def test_get_node_data_prefers_observed_last_heard_over_future_nodedb(self):
        now = int(time.time())

        class FakeInfo:
            my_node_num = 1

        class FakeIface:
            myInfo = FakeInfo()
            nodes = {
                "!remote": {
                    "num": 2,
                    "user": {"id": "!remote", "longName": "Remote", "shortName": "REM"},
                    "lastHeard": now + 1000,
                    "deviceMetrics": {},
                    "position": {},
                }
            }

        with state.connections_lock:
            state.connections["mt_test"] = {
                "iface": FakeIface(),
                "status": "connected",
                "config": {"name": "Test Radio"},
            }
        with state.mt_last_heard_lock:
            state.mt_last_heard[("mt_test", "!remote")] = now - 123

        rows = helpers.get_node_data()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_heard_ts"], now - 123)
        self.assertEqual(rows[0]["last_heard"], "2m ago")


if __name__ == "__main__":
    unittest.main()

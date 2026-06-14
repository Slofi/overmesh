import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-bot-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import bot  # noqa: E402
from config import CONFIG  # noqa: E402
from state import mc_connections, mc_connections_lock  # noqa: E402


class McBotHopReplyTests(unittest.TestCase):
    def setUp(self):
        with mc_connections_lock:
            mc_connections.clear()
        self._orig_mc_nodes = list(CONFIG.get("mc_nodes", []))
        CONFIG["mc_nodes"] = []

    def tearDown(self):
        with mc_connections_lock:
            mc_connections.clear()
        CONFIG["mc_nodes"] = list(self._orig_mc_nodes)

    def test_mc_test_hops_reports_direct_path_with_byte_suffix_when_known(self):
        self.assertEqual(
            bot._build_mc_test_hops_info({"path_len": 0, "path_hash_size": 2}, "mc1"),
            " | Hops(0): direct | 2byte",
        )

    def test_mc_test_hops_direct_path_uses_configured_byte_suffix(self):
        CONFIG["mc_nodes"] = [{"id": "mc1", "path_hash_mode": 1}]

        self.assertEqual(
            bot._build_mc_test_hops_info({"path_len": 0}, "mc1"),
            " | Hops(0): direct | 2byte",
        )

    def test_mc_test_hops_resolves_short_hop_ids(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "contacts": {
                    "aa00" + "1" * 60: {"public_key": "aa00" + "1" * 60, "adv_name": "Argus RPTR", "type": 2},
                    "bb00" + "2" * 60: {"public_key": "bb00" + "2" * 60, "adv_name": "Relay-B", "type": 2},
                }
            }

        label = bot._build_mc_test_hops_info({"path": "aa00bb00", "path_len": 2, "path_hash_size": 2}, "mc1")

        self.assertEqual(label, " | Hops(2): AA0, BB0 | 2byte")

    def test_mc_test_hops_prefers_repeater_on_hash_collision(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "contacts": {
                    "aa11" + "1" * 60: {"public_key": "aa11" + "1" * 60, "adv_name": "Alice", "type": 1},
                    "aa22" + "2" * 60: {"public_key": "aa22" + "2" * 60, "adv_name": "Rptr South", "type": 2},
                }
            }

        label = bot._build_mc_test_hops_info({"path": "aa", "path_len": 1, "path_hash_size": 1}, "mc1")

        self.assertEqual(label, " | Hops(1): AA2 | 1byte")

    def test_mc_test_hops_falls_back_to_count_without_path_hashes(self):
        self.assertEqual(bot._build_mc_test_hops_info({"path_len": 2}, "mc1"), " | Hops(2): unknown | 3byte")

    def test_mc_test_hops_uses_hash_prefix_when_contact_unresolved(self):
        label = bot._build_mc_test_hops_info({"path": "aabbccddeeff", "path_len": 2, "path_hash_size": 3}, "mc1")

        self.assertEqual(label, " | Hops(2): AAB, DDE | 3byte")


if __name__ == "__main__":
    unittest.main()

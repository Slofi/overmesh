import json
import os
import sys
import tempfile
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

import mesh_mc  # noqa: E402


class MeshMcPathHelperTests(unittest.TestCase):
    def test_rx_path_hash_mode_is_used_when_explicit_size_missing(self):
        size = mesh_mc._mc_path_hash_size_from_msg({}, {"path_hash_mode": 1})
        self.assertEqual(size, 2)

    def test_rx_path_len_is_inferred_from_hex_when_unknown(self):
        fields = mesh_mc._mc_message_path_fields(
            {},
            {"path": "a1b2c3d4", "path_len": 255, "path_hash_mode": 0},
        )
        self.assertEqual(fields["path"], "a1b2c3d4")
        self.assertEqual(fields["path_hash_size"], 1)
        self.assertEqual(fields["path_hash_mode"], 0)
        self.assertEqual(fields["path_len"], 4)

    def test_message_path_len_is_inferred_from_explicit_hash_size(self):
        fields = mesh_mc._mc_message_path_fields(
            {"path": "a1b2c3d4", "path_len": None, "path_hash_size": 2},
            None,
        )
        self.assertEqual(fields["path_hash_size"], 2)
        self.assertEqual(fields["path_len"], 2)


if __name__ == "__main__":
    unittest.main()

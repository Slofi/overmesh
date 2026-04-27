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

import helpers  # noqa: E402


class MtNodeIdFormattingTests(unittest.TestCase):
    def test_mt_node_id_from_num_zero_pads_hex(self):
        self.assertEqual(helpers.mt_node_id_from_num(0x1234), "!00001234")

    def test_mt_node_id_from_num_returns_none_for_invalid_input(self):
        self.assertIsNone(helpers.mt_node_id_from_num(None))
        self.assertIsNone(helpers.mt_node_id_from_num("bad"))


if __name__ == "__main__":
    unittest.main()

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

import cross  # noqa: E402


class CrossRelayTagTests(unittest.TestCase):
    def test_relay_tagged_matches_prefixed_bridge_header(self):
        self.assertTrue(cross._relay_tagged('[MT->MC Alice / CH2] hello'))
        self.assertTrue(cross._relay_tagged('  [mc->mt Bob / CH0] hi  '))

    def test_relay_tagged_does_not_match_plain_user_text(self):
        self.assertFalse(cross._relay_tagged('hello [MT->MC Alice / CH2] there'))
        self.assertFalse(cross._relay_tagged('MT->MC without brackets'))
        self.assertFalse(cross._relay_tagged('just a normal message'))


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-version-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import routes.chat as chat_routes  # noqa: E402


class SseVersionHandshakeTests(unittest.TestCase):
    """The SSE handshake is what makes an update+restart refresh open pages.

    The shell exposes window.OM_VERSION at load; this event carries the version the
    server is actually running. A mismatch means the page's markup and ?v= asset
    URLs are stale (the Probe button needed a manual hard refresh, 2026-09-10)."""

    def test_event_shape_and_version_source(self):
        ev = chat_routes._sse_version_event()
        self.assertEqual(ev["type"], "server_version")
        # matches the repo VERSION file, which is also what index() renders into the shell
        with open("/home/slofi/overmesh/VERSION", encoding="utf-8") as f:
            self.assertEqual(ev["version"], f.read().strip())

    def test_version_is_never_empty(self):
        self.assertTrue(chat_routes._app_version())
        self.assertNotEqual(chat_routes._app_version(), "")

    def test_missing_version_file_degrades_gracefully(self):
        original = chat_routes.BASE_DIR
        chat_routes.BASE_DIR = "/nonexistent-path-for-test"
        try:
            self.assertEqual(chat_routes._app_version(), "0.0.0")
        finally:
            chat_routes.BASE_DIR = original


if __name__ == "__main__":
    unittest.main()

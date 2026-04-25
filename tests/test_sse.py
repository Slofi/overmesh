import json
import os
import queue
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
import state  # noqa: E402


class SseQueueTests(unittest.TestCase):
    def setUp(self):
        with state.sse_lock:
            state.sse_clients.clear()
            state._sse_queue_last_ok.clear()

    def tearDown(self):
        with state.sse_lock:
            state.sse_clients.clear()
            state._sse_queue_last_ok.clear()

    def test_recently_healthy_full_queue_is_not_evicted(self):
        q = queue.Queue(maxsize=1)
        with state.sse_lock:
            state.sse_clients.append(q)
            state._sse_queue_last_ok[id(q)] = time.time()

        q.put_nowait("existing")
        helpers.push_to_sse("next-event")

        with state.sse_lock:
            self.assertIn(q, state.sse_clients)
            self.assertIn(id(q), state._sse_queue_last_ok)

    def test_stale_full_queue_is_evicted(self):
        q = queue.Queue(maxsize=1)
        with state.sse_lock:
            state.sse_clients.append(q)
            state._sse_queue_last_ok[id(q)] = time.time() - helpers._SSE_QUEUE_TTL - 5

        q.put_nowait("existing")
        helpers.push_to_sse("next-event")

        with state.sse_lock:
            self.assertNotIn(q, state.sse_clients)
            self.assertNotIn(id(q), state._sse_queue_last_ok)


if __name__ == "__main__":
    unittest.main()

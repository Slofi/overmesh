import json
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-keepalive-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import routes.chat as chat_routes  # noqa: E402
from helpers import SSE_CLOSE     # noqa: E402


class SseKeepaliveTests(unittest.TestCase):
    """A dead socket after a laptop sleep still reports OPEN in the browser.

    An SSE comment keepalive is invisible to JavaScript, so the page has no way
    to tell the feed is dead. The server must send a NAMED event instead; named
    events do not reach onmessage, so the existing message consumers are intact.
    """

    def test_generator_yields_the_named_keepalive(self):
        q = queue.Queue()
        gen = chat_routes._chat_stream_events(q, [], keepalive_secs=0.01)
        first = next(gen)
        self.assertIn("server_version", first)
        self.assertEqual(next(gen), "event: keepalive\ndata: 1\n\n")
        gen.close()

    def test_keepalive_is_not_a_comment(self):
        q = queue.Queue()
        gen = chat_routes._chat_stream_events(q, [], keepalive_secs=0.01)
        next(gen)
        chunk = next(gen)
        gen.close()
        self.assertFalse(chunk.lstrip().startswith(":"),
                         "a comment keepalive is invisible to JavaScript")
        self.assertEqual(chunk, chat_routes.SSE_KEEPALIVE_EVENT)
        self.assertEqual(chunk.count("\n\n"), 1)

    def test_history_rows_still_come_first_and_close_sentinel_ends_it(self):
        q = queue.Queue()
        gen = chat_routes._chat_stream_events(
            q, [{"id": "h1", "ts": 1, "text": "stored"}], keepalive_secs=0.01)
        next(gen)  # handshake
        hist = json.loads(next(gen)[len("data: "):])
        self.assertEqual(hist["id"], "h1")
        self.assertTrue(hist["is_history"])
        q.put("live")
        self.assertEqual(next(gen), "data: live\n\n")
        q.put(SSE_CLOSE)
        with self.assertRaises(StopIteration):
            next(gen)

    def test_route_uses_this_exact_generator(self):
        with open(os.path.join(REPO, "routes", "chat.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("yield from _chat_stream_events(q, history)", src)
        self.assertNotIn('": keepalive', src)


class LiteConsumerTests(unittest.TestCase):
    """The named event must not disturb lite.js, which is not being changed."""

    def test_lite_has_only_onmessage(self):
        with open(os.path.join(REPO, "static", "js", "lite.js"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("src.onmessage = e => handleSSE(JSON.parse(e.data));", src)
        self.assertNotIn("addEventListener('message'", src)
        self.assertNotIn('addEventListener("message"', src)
        self.assertNotIn("addEventListener('keepalive'", src)
        self.assertNotIn('addEventListener("keepalive"', src)

    def test_app_js_listens_for_the_named_keepalive(self):
        with open(os.path.join(REPO, "static", "js", "app.js"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("addEventListener('keepalive'", src)


if __name__ == "__main__":
    unittest.main()

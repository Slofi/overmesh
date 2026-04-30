import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-bridge-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(
    json.dumps({"nodes": [], "mc_nodes": [], "app": {}, "gps": {}, "silent_mode": False}),
    encoding="utf-8",
)

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import bridge  # noqa: E402
import routes.settings as settings_routes  # noqa: E402


class BridgeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(settings_routes.bp)
        self.client = self.app.test_client()
        self.config_backup = json.loads(json.dumps(settings_routes.CONFIG))
        settings_routes.CONFIG.clear()
        settings_routes.CONFIG.update({
            "nodes": [],
            "mc_nodes": [],
            "app": {},
            "gps": {},
            "silent_mode": False,
        })
        self.save_mock = mock.patch.object(settings_routes, "save_config").start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        settings_routes.CONFIG.clear()
        settings_routes.CONFIG.update(self.config_backup)

    def test_bridge_settings_round_trip(self):
        resp = self.client.post("/api/settings/app", json={
            "bridge_webhooks_enabled": True,
            "bridge_webhook_urls": "http://ha.local/hook\nt https://bad.example".replace("t ", ""),
            "bridge_webhook_secret": "secret",
            "bridge_ingest_enabled": True,
            "bridge_ingest_token": "token",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(settings_routes.CONFIG["bridge"]["webhooks"]["enabled"])
        self.assertEqual(settings_routes.CONFIG["bridge"]["webhooks"]["urls"], [
            "http://ha.local/hook",
            "https://bad.example",
        ])
        self.assertEqual(settings_routes.CONFIG["bridge"]["webhooks"]["secret"], "secret")
        self.assertTrue(settings_routes.CONFIG["bridge"]["ingest"]["enabled"])
        self.assertEqual(settings_routes.CONFIG["bridge"]["ingest"]["token"], "token")
        self.save_mock.assert_called_once()

        data = self.client.get("/api/settings/app").get_json()
        self.assertTrue(data["bridge_webhooks_enabled"])
        self.assertIn("http://ha.local/hook", data["bridge_webhook_urls"])
        self.assertEqual(data["bridge_ingest_token"], "token")

    def test_bridge_rejects_non_http_webhook_url(self):
        resp = self.client.post("/api/settings/app", json={
            "bridge_webhook_urls": "ftp://example.invalid/hook",
        })

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Webhook URLs", resp.get_json()["error"])

    def test_bridge_ingest_requires_token_when_enabled(self):
        resp = self.client.post("/api/settings/app", json={
            "bridge_ingest_enabled": True,
            "bridge_ingest_token": "",
        })

        self.assertEqual(resp.status_code, 400)
        self.assertIn("requires a bearer token", resp.get_json()["error"])


class BridgePublisherTests(unittest.TestCase):
    def setUp(self):
        self.config_backup = json.loads(json.dumps(bridge.CONFIG))
        bridge.CONFIG.clear()
        bridge.CONFIG.update({"bridge": {"webhooks": {"enabled": False, "urls": []}}})
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        bridge.CONFIG.clear()
        bridge.CONFIG.update(self.config_backup)

    def test_publish_inbound_is_inert_when_disabled(self):
        with mock.patch.object(bridge.threading, "Thread") as thread_mock:
            bridge.publish_inbound_message({"text": "hi", "from_id": "!1", "sent": False})

        thread_mock.assert_not_called()

    def test_publish_inbound_starts_webhook_threads_when_enabled(self):
        bridge.CONFIG["bridge"] = {
            "webhooks": {
                "enabled": True,
                "urls": ["http://ha.local/hook"],
                "secret": "secret",
            }
        }
        with mock.patch.object(bridge.threading, "Thread") as thread_mock:
            bridge.publish_inbound_message({
                "text": "hi",
                "from_id": "!1",
                "from_name": "Node",
                "channel": 0,
                "radio_id": "mt1",
                "sent": False,
            })

        thread_mock.assert_called_once()
        args = thread_mock.call_args.kwargs["args"]
        self.assertEqual(args[0], "http://ha.local/hook")
        self.assertEqual(args[1]["type"], "overmesh.message")
        self.assertEqual(args[1]["network"], "mt")
        self.assertEqual(args[1]["text"], "hi")


if __name__ == "__main__":
    unittest.main()

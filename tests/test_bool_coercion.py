"""EST-82 B1 + B6: request booleans must go through helpers._as_bool.

`bool("false")` is True, so a REST/automation caller (Home Assistant, curl, a
script) sending the JSON string "false" silently turned flags ON — e.g.
`POST /api/silent_mode {"enabled":"false"}` enabled Silent Running. The helper
was written for exactly this class (its docstring cites the 2026-09-12 sweep)
but only routes/radio.py used it. It now lives in helpers.py and every boolean
read of request JSON uses it.

Two layers of guard:
  * _as_bool() matrix — the helper contract.
  * behavioural tests on representative routes in each file, plus a source
    check that no `bool(request_data)` remains in the route files (armed: it
    fails on the pre-fix tree).
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-bool-test-")
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

import helpers  # noqa: E402
from routes import gps as gps_routes  # noqa: E402
from routes import mc as mc_routes  # noqa: E402
from routes import radio as radio_routes  # noqa: E402
from routes import settings as settings_routes  # noqa: E402
from routes import silent as silent_routes  # noqa: E402


class AsBoolContractTests(unittest.TestCase):
    def test_falsey_strings_are_false(self):
        for value in ("false", "0", "no", "off", "FALSE", " False ", ""):
            self.assertFalse(helpers._as_bool(value), value)

    def test_truthy_strings_are_true(self):
        for value in ("true", "1", "yes", "on", "TRUE", " On "):
            self.assertTrue(helpers._as_bool(value), value)

    def test_real_bools_numbers_and_none(self):
        self.assertFalse(helpers._as_bool(False))
        self.assertFalse(helpers._as_bool(0))
        self.assertFalse(helpers._as_bool(None))
        self.assertTrue(helpers._as_bool(True))
        self.assertTrue(helpers._as_bool(1))

    def test_radio_reexports_the_shared_helper(self):
        self.assertIs(radio_routes._as_bool, helpers._as_bool)


class NoRawBoolOnRequestDataTests(unittest.TestCase):
    """The bug class, not one instance: no route may coerce request JSON with
    bool(...) again. routes/radio.py is excluded — its `_bool` nested helper
    reads protobuf objects, not the request body."""

    FILES = [
        "routes/silent.py",
        "routes/settings.py",
        "routes/gps.py",
        "routes/mc.py",
        "routes/nodes.py",
    ]

    def test_no_bool_of_request_data(self):
        bad = []
        for rel in self.FILES:
            with open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    # (?<!\w) so the shared helper itself (_as_bool(...)) is not flagged.
                    if re.search(r"(?<!\w)bool\(\s*(data|request)\b", line):
                        bad.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            bad, [],
            "request booleans must use _as_bool, found: " + "; ".join(bad),
        )


class _ConfigStub(unittest.TestCase):
    """Back up / replace the shared CONFIG for a test, restore afterwards."""

    def setUp(self):
        self._backup = json.loads(json.dumps(settings_routes.CONFIG))
        settings_routes.CONFIG.clear()
        settings_routes.CONFIG.update({
            "nodes": [], "mc_nodes": [], "app": {}, "gps": {}, "silent_mode": False,
        })
        self.addCleanup(self._restore)

    def _restore(self):
        mock.patch.stopall()
        settings_routes.CONFIG.clear()
        settings_routes.CONFIG.update(self._backup)


class SilentModeCoercionTests(_ConfigStub):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(silent_routes.bp)
        self.client = self.app.test_client()
        mock.patch.object(silent_routes, "save_config").start()
        mock.patch.object(silent_routes, "push_to_sse").start()

    def test_string_false_does_not_enable(self):
        resp = self.client.post("/api/silent_mode", json={"enabled": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["silent_mode"])
        self.assertFalse(silent_routes.CONFIG["silent_mode"])

    def test_string_zero_does_not_enable(self):
        resp = self.client.post("/api/silent_mode", json={"enabled": "0"})
        self.assertFalse(resp.get_json()["silent_mode"])

    def test_real_false_does_not_enable(self):
        resp = self.client.post("/api/silent_mode", json={"enabled": False})
        self.assertFalse(resp.get_json()["silent_mode"])

    def test_string_true_enables(self):
        resp = self.client.post("/api/silent_mode", json={"enabled": "true"})
        self.assertTrue(resp.get_json()["silent_mode"])
        self.assertTrue(silent_routes.CONFIG["silent_mode"])

    def test_missing_defaults_to_false(self):
        resp = self.client.post("/api/silent_mode", json={})
        self.assertFalse(resp.get_json()["silent_mode"])


class SettingsBoolCoercionTests(_ConfigStub):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(settings_routes.bp)
        self.client = self.app.test_client()
        mock.patch.object(settings_routes, "save_config").start()
        mock.patch.object(settings_routes, "connect_node").start()

    def test_mt_set_enabled_string_false_disables(self):
        settings_routes.CONFIG["nodes"] = [
            {"id": "node_1", "name": "N", "enabled": True, "port": "/dev/null"}
        ]
        resp = self.client.post("/api/settings/nodes/node_1/set_enabled",
                                json={"enabled": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["nodes"][0]["enabled"], False)

    def test_mt_set_enabled_missing_keeps_enabled(self):
        settings_routes.CONFIG["nodes"] = [
            {"id": "node_1", "name": "N", "enabled": True, "port": "/dev/null"}
        ]
        resp = self.client.post("/api/settings/nodes/node_1/set_enabled", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["nodes"][0]["enabled"], True)

    def test_force_flood_string_false(self):
        settings_routes.CONFIG["mc_nodes"] = [{"id": "mc_1", "name": "M", "enabled": True}]
        resp = self.client.post("/api/settings/mc_nodes/mc_1/force_flood",
                                json={"force_flood": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["mc_nodes"][0]["force_flood"], False)

    def test_auto_add_contacts_string_false(self):
        settings_routes.CONFIG["mc_nodes"] = [{"id": "mc_1", "name": "M", "enabled": True}]
        resp = self.client.post("/api/settings/mc_nodes/mc_1/auto_add_contacts",
                                json={"auto_add_contacts": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["mc_nodes"][0]["auto_add_contacts"], False)

    def test_auto_add_contacts_missing_defaults_true(self):
        settings_routes.CONFIG["mc_nodes"] = [{"id": "mc_1", "name": "M", "enabled": True}]
        resp = self.client.post("/api/settings/mc_nodes/mc_1/auto_add_contacts", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["mc_nodes"][0]["auto_add_contacts"], True)

    def test_passive_collection_string_false(self):
        settings_routes.CONFIG["mc_nodes"] = [{"id": "mc_1", "name": "M", "enabled": True}]
        resp = self.client.post("/api/settings/mc_nodes/mc_1/passive_collection",
                                json={"passive_collection": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["mc_nodes"][0]["passive_collection"], False)

    def test_app_boolean_string_false(self):
        resp = self.client.post("/api/settings/app",
                                json={"sound_notify_messages": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["app"]["sound_notify_messages"], False)

    def test_mt_node_cleanup_enabled_string_false(self):
        settings_routes.CONFIG["nodes"] = [{"id": "node_1", "name": "N", "enabled": True}]
        resp = self.client.post("/api/settings/mt_node_cleanup/node_1",
                                json={"enabled": "false", "days": 30})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(settings_routes.CONFIG["nodes"][0]["auto_cleanup"], False)


class GpsBoolCoercionTests(_ConfigStub):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(gps_routes.bp)
        self.client = self.app.test_client()
        mock.patch.object(gps_routes, "save_config").start()
        import gps as gps_mod
        self.gps_start = mock.patch.object(gps_mod, "_gps_start").start()
        mock.patch.object(gps_mod, "_gps_start_proxy").start()
        mock.patch.object(gps_mod, "_gps_stop").start()
        mock.patch.object(gps_routes, "gps_port_conflict", return_value=None).start()

    def test_auto_push_string_false(self):
        gps_routes.CONFIG["gps"] = {"source": "manual", "auto_push_mt": True, "auto_push_mc": True}
        resp = self.client.post("/api/settings/gps", json={"source": "manual", "auto_push_mt": "false", "auto_push_mc": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(gps_routes.CONFIG["gps"]["auto_push_mt"], False)
        self.assertIs(gps_routes.CONFIG["gps"]["auto_push_mc"], False)

    def test_old_page_enabled_string_false_means_hand_picked(self):
        gps_routes.CONFIG["gps"] = {"enabled": True, "port": ""}
        resp = self.client.post("/api/settings/gps", json={"enabled": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(gps_routes.CONFIG["gps"]["source"], "manual")
        self.gps_start.assert_not_called()


class McBoolCoercionTests(_ConfigStub):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(mc_routes.bp)
        self.client = self.app.test_client()
        mock.patch.object(mc_routes, "save_config").start()
        mock.patch.object(mc_routes, "push_to_sse").start()

    def test_mc_ignore_string_false_unmutes(self):
        with mock.patch.object(mc_routes, "set_mc_ignored") as setter:
            resp = self.client.patch("/api/mc/contacts/abc/ignore",
                                     json={"ignored": "false"})
        self.assertEqual(resp.status_code, 200)
        setter.assert_called_once_with("abc", False)
        self.assertIs(resp.get_json()["ignored"], False)

    def test_contact_route_clear_string_false_does_not_clear(self):
        with mock.patch.object(mc_routes, "set_contact_path",
                               return_value=({"public_key": "k"}, None)) as setter, \
             mock.patch.object(mc_routes, "get_mc_contact_archive", return_value={}), \
             mock.patch.object(mc_routes, "_mc_contact_source_state", return_value={}), \
             mock.patch.object(mc_routes, "_serialize_mc_contact", return_value={}):
            resp = self.client.post("/api/mc/mc_1/contacts/abc/route",
                                    json={"clear": "false", "hops": ["aa"]})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(setter.call_args.kwargs["clear"], False)

    def test_mc_scan_probe_advert_string_false_is_probe_mode(self):
        mc_routes.CONFIG["silent_mode"] = False
        with mc_routes.mc_connections_lock:
            mc_routes.mc_connections["mc_1"] = {"status": "connected", "mc": object()}
        self.addCleanup(lambda: mc_routes.mc_connections.pop("mc_1", None))
        with mock.patch.object(mc_routes, "send_advert") as advert, \
             mock.patch.object(mc_routes, "send_discover_req", return_value="tag"):
            resp = self.client.post("/api/mc/mc_1/scan", json={"advert": "false"})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["advert"], False)
        advert.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""S504: contacts must report what the RADIO actually holds, and the bot's MC
replies must survive a client that was switched off.

Three defects are pinned here:

1. A background poll intentionally keeps the previous live set when firmware
   answers with nothing. An explicit "refresh from radio" must not: it is the
   action that makes OverMesh agree with the radio again.
2. "Delete from this radio" used to fail with ERR_CODE_NOT_FOUND whenever the
   radio had already dropped the contact (NVS purge), because OverMesh still
   listed it as live. That answer now reconciles the state instead of raising.
3. The MC bot reply went out only as an SSE event, so a client that was off,
   or any reload, never saw it. It is stored in the MC message archive now.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-s504-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import bot  # noqa: E402
import mesh_mc  # noqa: E402
from config import CONFIG  # noqa: E402
from routes import mc as mc_routes  # noqa: E402
from state import mc_connections, mc_connections_lock  # noqa: E402

RADIO = "mc_node_test"
KEY_RADIO_ONLY = "aa" * 32
KEY_BOTH = "bb" * 32
KEY_BOT_OLD = "cc" * 32


def _event(type_name, payload=None):
    return SimpleNamespace(type=getattr(mesh_mc.EventType, type_name), payload=payload or {})


class _FakeCommands:
    def __init__(self, contacts_payload=None, remove_result=None, legacy_result=None):
        self._contacts_payload = contacts_payload if contacts_payload is not None else {}
        self._remove_result = remove_result
        self._legacy_result = legacy_result
        self.send_calls = []
        self.remove_calls = []
        self.dispatcher = None

    async def get_contacts(self):
        return _event("CONTACTS", dict(self._contacts_payload))

    async def remove_contact(self, full_key):
        self.remove_calls.append(full_key)
        return self._remove_result

    async def send(self, data, expect):
        self.send_calls.append(data)
        return self._legacy_result


class _FakeMeshCore:
    def __init__(self, **kw):
        self.commands = _FakeCommands(**kw)


class RadioTruthTests(unittest.TestCase):
    def setUp(self):
        mesh_mc.DATA_DIR = str(_DATA_DIR)
        mesh_mc.MC_CONTACT_ARCHIVE_PATH = str(_DATA_DIR / "mc_contacts_archive.json")
        mesh_mc._mc_contact_archive_cache = None
        mesh_mc._mc_legacy_statusreq_unsupported.clear()
        with mc_connections_lock:
            mc_connections.clear()

    def _seed_state(self):
        """Radio holds KEY_BOTH; the archive also holds KEY_RADIO_ONLY (app only)."""
        with mc_connections_lock:
            mc_connections[RADIO] = {
                "contacts": {
                    KEY_BOTH: {"adv_name": "Both", "type": 2, "out_path_len": -1},
                    KEY_RADIO_ONLY: {"adv_name": "AppOnly", "type": 2, "out_path_len": -1},
                },
                "live_contacts": {
                    KEY_BOTH: {"adv_name": "Both", "type": 2, "out_path_len": -1},
                    KEY_RADIO_ONLY: {"adv_name": "AppOnly", "type": 2, "out_path_len": -1},
                },
                "status": "connected",
            }

    # -- 1. poll vs explicit refresh -------------------------------------
    def test_poll_keeps_live_set_when_radio_answers_empty(self):
        self._seed_state()
        fake = _FakeMeshCore(contacts_payload={})
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, {})), \
                mock.patch.object(mesh_mc, "_mc_archive_merge_contacts", lambda *a, **k: None):
            asyncio.run(mesh_mc._get_contacts_async(RADIO))
        with mc_connections_lock:
            live = dict(mc_connections[RADIO]["live_contacts"])
        self.assertIn(KEY_BOTH, live, "a background poll must not drop the live set on an empty read")

    def test_authoritative_refresh_accepts_an_empty_answer(self):
        self._seed_state()
        fake = _FakeMeshCore(contacts_payload={})
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, {})), \
                mock.patch.object(mesh_mc, "_mc_archive_merge_contacts", lambda *a, **k: None):
            asyncio.run(mesh_mc._get_contacts_async(RADIO, authoritative=True))
        with mc_connections_lock:
            live = dict(mc_connections[RADIO]["live_contacts"])
        self.assertEqual(live, {}, "an explicit refresh takes the radio's word for it")

    def test_authoritative_refresh_reports_what_changed(self):
        self._seed_state()
        fake = _FakeMeshCore(contacts_payload={KEY_BOTH: {"adv_name": "Both", "type": 2}})

        def run_now(coro, timeout=10):
            return asyncio.run(coro)

        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, {})), \
                mock.patch.object(mesh_mc, "_mc_archive_merge_contacts", lambda *a, **k: None), \
                mock.patch.object(mesh_mc, "run_mc", side_effect=run_now):
            report = mesh_mc.refresh_contacts_authoritative(RADIO)

        self.assertEqual(report["on_radio"], 1)
        self.assertEqual(report["gone_count"], 1, "the contact the radio dropped must be reported")
        self.assertEqual(report["gone"], [KEY_RADIO_ONLY[:12]])
        self.assertEqual(report["new_count"], 0)
        self.assertGreaterEqual(report["app_only"], 1)

    # -- 2. delete-from-radio reconciliation -----------------------------
    def test_radio_only_removal_reconciles_when_device_says_not_found(self):
        self._seed_state()
        fake = _FakeMeshCore(remove_result=_event("ERROR", {"error_code": 2, "code_string": "ERR_CODE_NOT_FOUND"}))
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, {})):
            result = asyncio.run(mesh_mc._remove_mc_radio_only_async(RADIO, KEY_BOTH))
        self.assertTrue(result.get("reconciled"))
        self.assertEqual(result.get("reason"), "not_on_radio")
        with mc_connections_lock:
            self.assertNotIn(KEY_BOTH, mc_connections[RADIO]["live_contacts"],
                             "the stale live entry must be dropped so the row reads app only")
            self.assertIn(KEY_BOTH, mc_connections[RADIO]["contacts"],
                          "the device-less contact stays in OM")

    def test_radio_only_removal_still_raises_on_a_real_device_error(self):
        self._seed_state()
        fake = _FakeMeshCore(remove_result=_event("ERROR", {"error_code": 4, "code_string": "ERR_CODE_BAD_STATE"}))
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, {})):
            with self.assertRaises(RuntimeError):
                asyncio.run(mesh_mc._remove_mc_radio_only_async(RADIO, KEY_BOTH))
        with mc_connections_lock:
            self.assertIn(KEY_BOTH, mc_connections[RADIO]["live_contacts"],
                          "a real rejection leaves the state alone")

    def test_delete_route_returns_a_plain_note_when_reconciled(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()
        with mock.patch.object(mc_routes, "remove_mc_contact_scoped",
                               return_value={"ok": True, "reconciled": True, "reason": "not_on_radio"}):
            res = client.delete(f"/api/mc/{RADIO}/contacts/aaaa?scope=radio")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload.get("reconciled"))
        self.assertIn("app only", payload.get("note", ""))

    def test_refresh_route_returns_the_counts(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()
        report = {"on_radio": 48, "app_only": 478, "gone_count": 3, "new_count": 1, "gone": ["aa"], "new": ["bb"]}
        with mock.patch.object(mc_routes, "refresh_contacts_authoritative", return_value=dict(report)):
            res = client.post(f"/api/mc/{RADIO}/contacts/refresh")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["on_radio"], 48)
        self.assertEqual(payload["gone_count"], 3)

    # -- legacy status request: stop asking a firmware that said no ------
    def test_legacy_statusreq_is_skipped_after_not_found(self):
        fake = _FakeMeshCore(legacy_result=_event("ERROR", {"error_code": 2, "code_string": "ERR_CODE_NOT_FOUND"}))
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, {})):
            first = asyncio.run(mesh_mc._mc_try_legacy_statusreq(fake, RADIO, KEY_BOTH, {}, KEY_BOTH))
            second = asyncio.run(mesh_mc._mc_try_legacy_statusreq(fake, RADIO, KEY_BOTH, {}, KEY_BOTH))
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIn(RADIO, mesh_mc._mc_legacy_statusreq_unsupported)
        self.assertEqual(len(fake.commands.send_calls), 1,
                         "the second ping must not repeat a request this firmware rejects")


class McBotReplyStorageTests(unittest.TestCase):
    def test_channel_reply_is_stored_as_a_normal_message(self):
        captured = {}
        with mock.patch.object(bot, "save_mc_message", side_effect=lambda m: captured.update(m)):
            bot.store_mc_bot_reply(RADIO, "pong", 2)
        self.assertEqual(captured.get("radio_id"), RADIO)
        self.assertEqual(captured.get("subtype"), "channel")
        self.assertEqual(captured.get("channel"), 2)
        self.assertEqual(captured.get("from_id"), "bot")
        self.assertEqual(captured.get("from_name"), "Bot")
        self.assertTrue(captured.get("sent"))
        self.assertEqual(captured.get("text"), "pong")
        self.assertTrue(captured.get("ts"), "a timestamp is required for history ordering")

    def test_dm_reply_is_stored_as_a_dm_with_its_target(self):
        captured = {}
        with mock.patch.object(bot, "save_mc_message", side_effect=lambda m: captured.update(m)):
            bot.store_mc_bot_reply(RADIO, "answer", 0, dest_pre="abcd1234", to_name="Filip")
        self.assertEqual(captured.get("subtype"), "dm")
        self.assertEqual(captured.get("to_id"), "abcd1234")
        self.assertEqual(captured.get("to_name"), "Filip")

    def test_a_storage_failure_never_breaks_the_reply(self):
        with mock.patch.object(bot, "save_mc_message", side_effect=RuntimeError("disk full")):
            msg = bot.store_mc_bot_reply(RADIO, "still sent", 0)
        self.assertEqual(msg.get("text"), "still sent")


if __name__ == "__main__":
    unittest.main()

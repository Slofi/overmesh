import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import asyncio
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from flask import Flask

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
import db  # noqa: E402
from routes import mc as mc_routes  # noqa: E402
from config import CONFIG  # noqa: E402
from state import mc_connections, mc_connections_lock  # noqa: E402


class MeshMcPathHelperTests(unittest.TestCase):
    def setUp(self):
        mesh_mc.DATA_DIR = str(_DATA_DIR)
        mesh_mc.MC_CONTACT_ARCHIVE_PATH = str(_DATA_DIR / "mc_contacts_archive.json")
        mesh_mc._mc_contact_archive_cache = None
        mesh_mc._advert_tasks.clear()
        archive_path = _DATA_DIR / "mc_contacts_archive.json"
        if archive_path.exists():
            archive_path.unlink()
        with mc_connections_lock:
            mc_connections.clear()
        self._orig_mc_nodes = list(CONFIG.get("mc_nodes", []))
        CONFIG["mc_nodes"] = []

    def tearDown(self):
        CONFIG["mc_nodes"] = list(self._orig_mc_nodes)
        mesh_mc._advert_tasks.clear()

    def test_rx_path_hash_mode_is_used_when_explicit_size_missing(self):
        size = mesh_mc._mc_path_hash_size_from_msg({}, {"path_hash_mode": 1})
        self.assertEqual(size, 2)

    def test_passive_collection_defaults_enabled(self):
        CONFIG["mc_nodes"] = [{"id": "mc1", "name": "MC One"}]

        self.assertTrue(mesh_mc._mc_passive_collection_enabled("mc1"))

    def test_passive_collection_can_be_disabled_from_config(self):
        CONFIG["mc_nodes"] = [{"id": "mc1", "name": "MC One", "passive_collection": False}]

        self.assertFalse(mesh_mc._mc_passive_collection_enabled("mc1"))

    def test_passive_collection_prefers_runtime_config(self):
        CONFIG["mc_nodes"] = [{"id": "mc1", "name": "MC One", "passive_collection": True}]
        with mc_connections_lock:
            mc_connections["mc1"] = {"config": {"passive_collection": False}}

        self.assertFalse(mesh_mc._mc_passive_collection_enabled("mc1"))

    def test_send_advert_async_reports_duplicate_in_progress(self):
        mesh_mc._advert_tasks["mc1"] = SimpleNamespace(done=lambda: False)

        result = asyncio.run(mesh_mc._send_advert_async("mc1", flood=True))

        self.assertEqual(result, {"status": "in_progress", "flood": True})

    def test_send_advert_async_reports_queued(self):
        fake_mc = SimpleNamespace(commands=SimpleNamespace(send_advert=None))

        def fake_bg_task(coro, _name):
            coro.close()
            return SimpleNamespace(done=lambda: False)

        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake_mc, {})):
            with mock.patch.object(mesh_mc, "_mc_bg_task", side_effect=fake_bg_task):
                result = asyncio.run(mesh_mc._send_advert_async("mc1", flood=True))

        self.assertEqual(result, {"status": "queued", "flood": True})
        self.assertIn("mc1", mesh_mc._advert_tasks)

    def test_create_meshcore_closes_transport_when_connect_raises(self):
        class FakeConnection:
            def __init__(self):
                self.closed = 0
                self.transport = None

            async def disconnect(self):
                self.closed += 1

        class FakeMeshCore:
            def __init__(self, connection, **_kwargs):
                self.connection_manager = SimpleNamespace(connection=connection)
                self.disconnect_called = 0

            async def connect(self):
                raise RuntimeError("appstart timeout")

            async def disconnect(self):
                self.disconnect_called += 1

        conn = FakeConnection()
        with mock.patch.object(mesh_mc, "MeshCore", FakeMeshCore):
            with self.assertRaises(RuntimeError):
                asyncio.run(mesh_mc._create_meshcore(conn, default_timeout=0.01))

        self.assertEqual(conn.closed, 1)

    def test_create_meshcore_closes_transport_when_connect_returns_none(self):
        class FakeConnection:
            def __init__(self):
                self.closed = 0
                self.transport = None

            async def disconnect(self):
                self.closed += 1

        class FakeMeshCore:
            def __init__(self, connection, **_kwargs):
                self.connection_manager = SimpleNamespace(connection=connection)

            async def connect(self):
                return None

            async def disconnect(self):
                pass

        conn = FakeConnection()
        with mock.patch.object(mesh_mc, "MeshCore", FakeMeshCore):
            result = asyncio.run(mesh_mc._create_meshcore(conn, default_timeout=0.01))

        self.assertIsNone(result)
        self.assertEqual(conn.closed, 1)

    def test_remote_cli_reply_value_strips_repeater_prompt(self):
        self.assertEqual(
            mesh_mc._remote_cli_reply_value({"text": "> Argus mobile RPTR"}),
            "Argus mobile RPTR",
        )

    def test_remote_contact_updates_parse_name_and_coords(self):
        self.assertEqual(
            mesh_mc._remote_contact_updates_for_command("get name", {"text": "> Argus mobile RPTR"}),
            {"adv_name": "Argus mobile RPTR"},
        )
        self.assertEqual(
            mesh_mc._remote_contact_updates_for_command("set name Argus mobile RPTR", {"text": "OK"}),
            {"adv_name": "Argus mobile RPTR"},
        )
        self.assertEqual(
            mesh_mc._remote_contact_updates_for_command("get lat", {"text": "> 46.123456"}),
            {"adv_lat": 46.123456},
        )
        self.assertEqual(
            mesh_mc._remote_contact_updates_for_command("set lon 14.654321", {"text": "OK"}),
            {"adv_lon": 14.654321},
        )

    def test_force_close_meshcore_closes_raw_serial_transport(self):
        class FakeSerial:
            def __init__(self):
                self.closed = 0

            def close(self):
                self.closed += 1

        class FakeTransport:
            def __init__(self):
                self.closed = 0
                self.serial = FakeSerial()

            def close(self):
                self.closed += 1

        class FakeConnection:
            def __init__(self):
                self.transport = FakeTransport()
                self._background_tasks = set()

            async def disconnect(self):
                self.transport = None

        class FakeMeshCore:
            def __init__(self):
                self.connection_manager = SimpleNamespace(connection=FakeConnection())

            async def disconnect(self):
                pass

        mc = FakeMeshCore()
        transport = mc.connection_manager.connection.transport

        asyncio.run(mesh_mc._force_close_meshcore(mc))

        self.assertEqual(transport.closed, 1)
        self.assertEqual(transport.serial.closed, 1)
        self.assertIsNone(mc.connection_manager.connection.transport)

    def test_om_serial_connection_keeps_connecting_when_rts_release_fails(self):
        async def run_case():
            class FakeSerial:
                @property
                def rts(self):
                    return False

                @rts.setter
                def rts(self, _value):
                    raise TimeoutError("rts stuck")

            transport = SimpleNamespace(serial=FakeSerial())
            conn = mesh_mc.OMSerialConnection("/dev/test", 115200)
            proto = conn.MCSerialClientProtocol(conn)

            proto.connection_made(transport)

            self.assertIs(conn.transport, transport)
            self.assertTrue(conn._connected_event.is_set())

        asyncio.run(run_case())

    def test_mc_contact_seen_ts_does_not_default_to_now(self):
        self.assertEqual(mesh_mc._mc_contact_seen_ts({"adv_name": "Remote"}), 0)

    def test_mc_contact_seen_ts_prefers_real_seen_fields(self):
        self.assertEqual(mesh_mc._mc_contact_seen_ts({"last_advert": 123}), 123)
        self.assertEqual(mesh_mc._mc_contact_seen_ts({"last_seen_ts": 456, "last_advert": 123}), 456)
        self.assertEqual(mesh_mc._mc_contact_seen_ts({"last_heard_ts": 789, "last_seen_ts": 456}), 789)

    def test_mc_contact_seen_ts_ignores_future_advert_and_uses_lastmod(self):
        self.assertEqual(
            mesh_mc._mc_contact_seen_ts({"last_advert": 999999, "lastmod": 456}, now=1000),
            456,
        )

    def test_mc_contact_api_serializer_does_not_clamp_future_seen_to_now(self):
        row = mc_routes._serialize_mc_contact(
            "abcdef1234567890",
            {"adv_name": "Remote", "last_advert": 999999, "lastmod": 456},
            now=1000,
        )

        self.assertEqual(row["last_seen_ts"], 456)
        self.assertEqual(row["last_seen"], "9m ago")

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

    def test_mc_archive_persists_contacts(self):
        radio_id = "mc_test"
        pubkey = "abcdef1234567890"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "Relay", "type": 2, "out_path_len": 1, "out_path": "aa"}
        })

        mesh_mc._mc_contact_archive_cache = None
        archived = mesh_mc.get_mc_contact_archive(radio_id)

        self.assertIn(pubkey, archived)
        self.assertEqual(archived[pubkey]["adv_name"], "Relay")
        self.assertEqual(archived[pubkey]["out_path"], "aa")

    def test_mc_archive_getter_returns_detached_contact_dicts(self):
        radio_id = "mc_test"
        pubkey = "abcdef1234567890"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "Relay", "type": 2}
        })

        archived = mesh_mc.get_mc_contact_archive(radio_id)
        archived[pubkey]["adv_name"] = "Mutated"

        fresh = mesh_mc.get_mc_contact_archive(radio_id)
        self.assertEqual(fresh[pubkey]["adv_name"], "Relay")

    def test_mc_archive_getter_detaches_nested_contact_data(self):
        radio_id = "mc_test"
        pubkey = "abcdef1234567890"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {
                "adv_name": "Relay",
                "type": 2,
                "meta": {"path": ["aa", "bb"]},
            }
        })

        archived = mesh_mc.get_mc_contact_archive(radio_id)
        archived[pubkey]["meta"]["path"].append("cc")

        fresh = mesh_mc.get_mc_contact_archive(radio_id)
        self.assertEqual(fresh[pubkey]["meta"]["path"], ["aa", "bb"])

    def test_mc_archive_merge_detaches_nested_input_contact_data(self):
        radio_id = "mc_test"
        pubkey = "abcdef1234567890"
        source = {
            pubkey: {
                "adv_name": "Relay",
                "type": 2,
                "meta": {"path": ["aa", "bb"]},
            }
        }
        mesh_mc._mc_archive_merge_contacts(radio_id, source)
        source[pubkey]["meta"]["path"].append("cc")

        fresh = mesh_mc.get_mc_contact_archive(radio_id)
        self.assertEqual(fresh[pubkey]["meta"]["path"], ["aa", "bb"])

    def test_archive_merge_exposes_archived_only_contact_to_runtime(self):
        radio_id = "mc_test"
        pubkey = "feedfacecafebeef"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1}
        })

        merged = mesh_mc._merge_mc_contacts_with_archive(radio_id, {})

        self.assertIn(pubkey, merged)
        self.assertEqual(merged[pubkey]["adv_name"], "ArchiveOnly")

    def test_api_contacts_returns_archive_without_runtime_state(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get(f"/api/mc/{radio_id}/contacts")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["radio_id"], radio_id)
        self.assertEqual(len(payload["contacts"]), 1)
        self.assertEqual(payload["contacts"][0]["full_key"], pubkey)
        self.assertEqual(payload["contacts"][0]["source_state"], "archive")
        self.assertTrue(payload["contacts"][0]["archived_only"])

    def test_api_contacts_uses_archive_when_runtime_state_is_empty(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "contacts": {},
                "live_contacts": {},
            }

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get(f"/api/mc/{radio_id}/contacts")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(len(payload["contacts"]), 1)
        self.assertEqual(payload["contacts"][0]["full_key"], pubkey)
        self.assertEqual(payload["contacts"][0]["source_state"], "archive")

    def test_api_contacts_uses_live_contacts_when_merged_contacts_are_empty(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "contacts": {},
                "live_contacts": {
                    pubkey: {"adv_name": "LiveOnly", "type": 1, "last_seen_ts": 123}
                },
            }

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get(f"/api/mc/{radio_id}/contacts")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(len(payload["contacts"]), 1)
        self.assertEqual(payload["contacts"][0]["full_key"], pubkey)
        self.assertEqual(payload["contacts"][0]["source_state"], "live")

    def test_mark_mc_disconnected_clears_live_contacts_but_keeps_archive(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        CONFIG["mc_nodes"] = [{"id": radio_id, "name": "Archive Radio", "enabled": True}]
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "status": "connected",
                "mc": object(),
                "contacts": {
                    pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
                },
                "live_contacts": {
                    pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
                },
            }

        mesh_mc._mark_mc_disconnected(radio_id)

        with mc_connections_lock:
            state = mc_connections[radio_id]
            self.assertEqual(state["status"], "disconnected")
            self.assertIsNone(state["mc"])
            self.assertEqual(state["live_contacts"], {})
            self.assertIn(pubkey, state["contacts"])

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        status_res = client.get("/api/mc/status")
        self.assertEqual(status_res.status_code, 200)
        status_payload = status_res.get_json()
        radio_status = next(node for node in status_payload["mc_nodes"] if node["id"] == radio_id)
        self.assertEqual(radio_status["contacts"], 0)
        self.assertEqual(radio_status["live_contacts"], 0)
        self.assertEqual(radio_status["stored_contacts"], 1)

        contacts_res = client.get(f"/api/mc/{radio_id}/contacts")
        self.assertEqual(contacts_res.status_code, 200)
        contacts_payload = contacts_res.get_json()
        self.assertEqual(contacts_payload["contacts"][0]["source_state"], "archive")

    def test_api_status_includes_configured_archive_only_radio(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        CONFIG["mc_nodes"] = [{"id": radio_id, "name": "Archive Radio", "enabled": False}]
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get("/api/mc/status")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        radio_status = next(node for node in payload["mc_nodes"] if node["id"] == radio_id)
        self.assertEqual(radio_status["status"], "disconnected")
        self.assertEqual(radio_status["contacts"], 0)
        self.assertEqual(radio_status["stored_contacts"], 1)
        self.assertEqual(radio_status["archived_contacts"], 1)
        self.assertEqual(radio_status["enabled"], False)

    def test_api_status_ignores_unconfigured_archive_only_radio(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        CONFIG["mc_nodes"] = []
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get("/api/mc/status")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertFalse(any(node["id"] == radio_id for node in payload["mc_nodes"]))

    def test_api_status_ignores_unconfigured_disconnected_runtime_radio(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        CONFIG["mc_nodes"] = []
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "status": "disconnected",
                "mc": None,
                "contacts": {
                    pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
                },
                "live_contacts": {},
            }

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get("/api/mc/status")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertFalse(any(node["id"] == radio_id for node in payload["mc_nodes"]))

    def test_api_contacts_unknown_radio_without_archive_is_404(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get("/api/mc/no_such_radio/contacts")
        self.assertEqual(res.status_code, 404)

    def test_delete_archive_only_contact_does_not_require_runtime_state(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.delete(f"/api/mc/{radio_id}/contacts/{pubkey[:12]}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mesh_mc.get_mc_contact_archive(radio_id), {})

    def test_delete_archive_contact_cleans_stale_disconnected_runtime_state(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed"
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "mc": None,
                "contacts": {
                    pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
                },
                "live_contacts": {
                    pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
                },
            }

        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.delete(f"/api/mc/{radio_id}/contacts/{pubkey[:12]}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mesh_mc.get_mc_contact_archive(radio_id), {})
        with mc_connections_lock:
            self.assertEqual(mc_connections[radio_id]["contacts"], {})
            self.assertEqual(mc_connections[radio_id]["live_contacts"], {})

    def test_dtr_reset_rejects_non_serial_mc_radio(self):
        radio_id = "mc_tcp"
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "config": {"type": "tcp", "port": "192.168.1.50:4403"},
                "port": "192.168.1.50:4403",
                "mc": None,
            }

        with self.assertRaisesRegex(RuntimeError, "USB serial"):
            mesh_mc.reboot_device_dtr(radio_id)

    def test_stale_connecting_radio_is_marked_for_reconnect(self):
        radio_id = "mc_stale"
        cfg = {"id": radio_id, "name": "Stale", "enabled": True}
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "status": "connecting",
                "status_ts": 100.0,
                "mc": object(),
                "config": cfg,
            }

        items = mesh_mc._mc_reconnect_items(now=230.0, stale_connect_secs=120)

        self.assertEqual(items, [(radio_id, cfg)])
        with mc_connections_lock:
            self.assertEqual(mc_connections[radio_id]["status"], "disconnected")
            self.assertIsNone(mc_connections[radio_id]["mc"])

    def test_recent_connecting_radio_is_not_retried(self):
        radio_id = "mc_recent"
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "status": "connecting",
                "status_ts": 100.0,
                "mc": object(),
                "config": {"id": radio_id, "name": "Recent", "enabled": True},
            }

        items = mesh_mc._mc_reconnect_items(now=150.0, stale_connect_secs=120)

        self.assertEqual(items, [])
        with mc_connections_lock:
            self.assertEqual(mc_connections[radio_id]["status"], "connecting")

    def test_send_dm_forces_flood_when_radio_option_enabled(self):
        radio_id = "mc_flood"
        pubkey = "aabbccddeeff" + "00" * 26

        class FakeCommands:
            def __init__(self):
                self.reset_keys = []
                self.sent_contacts = []

            async def reset_path(self, key):
                self.reset_keys.append(key)
                return SimpleNamespace(type=SimpleNamespace(name="OK"))

            async def send_msg(self, contact, text):
                self.sent_contacts.append(dict(contact))
                return SimpleNamespace(type=SimpleNamespace(name="MSG_SENT"))

        fake_mc = SimpleNamespace(commands=FakeCommands())
        CONFIG["mc_nodes"] = [{"id": radio_id, "name": "Flood", "force_flood": True}]
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "mc": fake_mc,
                "status": "connected",
                "contacts": {
                    pubkey: {"public_key": pubkey, "out_path_len": 2, "out_path": "aabb"}
                },
                "live_contacts": {
                    pubkey: {"public_key": pubkey, "out_path_len": 2, "out_path": "aabb"}
                },
            }

        asyncio.run(mesh_mc._send_dm_async(radio_id, pubkey[:12], "hello"))

        self.assertEqual(fake_mc.commands.reset_keys, [pubkey])
        self.assertEqual(fake_mc.commands.sent_contacts[0]["out_path_len"], -1)
        self.assertEqual(fake_mc.commands.sent_contacts[0]["out_path"], "")
        with mc_connections_lock:
            self.assertEqual(mc_connections[radio_id]["contacts"][pubkey]["out_path_len"], -1)
            self.assertEqual(mc_connections[radio_id]["live_contacts"][pubkey]["out_path"], "")

    def test_status_response_path_fields_fall_back_to_learned_repeater_path(self):
        full_key = "aabbccddeeff" + "00" * 26
        contact = {
            "type": 2,
            "out_path": "1234abcd",
            "out_path_len": 2,
            "out_path_hash_size": 2,
        }

        fields = mesh_mc._mc_status_observed_path_fields(full_key, contact)

        self.assertEqual(fields["observed_path"], "1234abcd")
        self.assertEqual(fields["observed_path_len"], 2)
        self.assertEqual(fields["observed_path_hash_size"], 2)

    def test_status_response_path_fields_prefer_matching_rx_path(self):
        full_key = "aabbccddeeff" + "00" * 26
        contact = {
            "out_path": "1234abcd",
            "out_path_len": 2,
            "out_path_hash_size": 2,
        }
        rx_event = SimpleNamespace(payload={
            "pubkey_pre": "aabbccddeeff",
            "path": "beef",
            "path_len": 1,
            "path_hash_size": 2,
            "rssi": -90,
            "snr": 4.25,
        })

        fields = mesh_mc._mc_status_observed_path_fields(full_key, contact, rx_event)

        self.assertEqual(fields["observed_path"], "beef")
        self.assertEqual(fields["observed_path_len"], 1)
        self.assertEqual(fields["observed_path_hash_size"], 2)
        self.assertEqual(fields["observed_rssi"], -90)
        self.assertEqual(fields["observed_snr"], 4.25)

    def test_api_mc_statusreq_does_not_prime_trace_by_default(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        with mock.patch.object(mc_routes, "req_node_status", return_value={"pubkey_pre": "abcdef123456"}) as status_mock:
            res = client.post("/api/mc/mc1/statusreq/abcdef123456")

        self.assertEqual(res.status_code, 200)
        status_mock.assert_called_once_with("mc1", "abcdef123456", prime_trace=False)

    def test_api_mc_statusreq_can_opt_in_to_trace_prime(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        with mock.patch.object(mc_routes, "req_node_status", return_value={"pubkey_pre": "abcdef123456"}) as status_mock:
            res = client.post("/api/mc/mc1/statusreq/abcdef123456", json={"trace_probe": True})

        self.assertEqual(res.status_code, 200)
        status_mock.assert_called_once_with("mc1", "abcdef123456", prime_trace=True)

    def test_remote_admin_command_allowlist_normalizes_safe_commands(self):
        self.assertEqual(mesh_mc._validate_remote_admin_command("  get   name  "), "get name")
        self.assertEqual(mesh_mc._validate_remote_admin_command("get powersaving"), "get powersaving")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set repeat on"), "set repeat on")
        self.assertEqual(mesh_mc._validate_remote_admin_command("advert.zerohop"), "advert.zerohop")
        self.assertEqual(mesh_mc._validate_remote_admin_command("clock sync"), "clock sync")
        self.assertEqual(mesh_mc._validate_remote_admin_command("discover.neighbors"), "discover.neighbors")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set radio 869.525,250,11,5"), "set radio 869.525,250,11,5")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set advert.interval 60"), "set advert.interval 60")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set flood.advert.interval 12"), "set flood.advert.interval 12")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set loop.detect minimal"), "set loop.detect minimal")
        self.assertEqual(mesh_mc._validate_remote_admin_command("OMCOLLECT"), "OMCOLLECT")

    def test_rc_collector_import_stores_identity_obs_only(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "contacts": {
                    "abc123000000": {"adv_lat": 0.0, "adv_lon": 14.5},
                }
            }

        with mock.patch.object(mesh_mc, "save_passive_obs") as save_mock:
            mesh_mc._handle_rc_collector_line("mc1", "abc123", "OMCOLLECT_START|RC1|2")
            mesh_mc._handle_rc_collector_line("mc1", "abc123", "OBS|ADV|deadbeefcaf0|-72|8.25|1778157541")
            mesh_mc._handle_rc_collector_line("mc1", "abc123", "OBS|RX|3a7f|-85|4.50|1778157602")
            mesh_mc._handle_rc_collector_line("mc1", "abc123", "OMCOLLECT_END")

        save_mock.assert_called_once()
        args, kwargs = save_mock.call_args
        self.assertEqual(args[:3], ("mc1", "deadbeefcaf0", "rc_adv"))
        self.assertEqual(kwargs["rssi"], -72)
        self.assertEqual(kwargs["snr"], 8.25)
        self.assertEqual(kwargs["collector_id"], "RC1")
        self.assertEqual(kwargs["collector_lat"], 0.0)
        self.assertEqual(kwargs["collector_lon"], 14.5)
        self.assertEqual(kwargs["observed_ts"], 1778157541)

    def test_remote_admin_command_allowlist_rejects_unknown_commands(self):
        with self.assertRaises(ValueError):
            mesh_mc._validate_remote_admin_command("erase everything")
        with self.assertRaises(ValueError):
            mesh_mc._validate_remote_admin_command("set prv.key abc")

    def test_remote_cli_reply_error_detection(self):
        self.assertTrue(mesh_mc._remote_cli_reply_is_error({"text": "Error: wrong password"}))
        self.assertTrue(mesh_mc._remote_cli_reply_is_error({"text": "ERR: bad value"}))
        self.assertTrue(mesh_mc._remote_cli_reply_is_error({"text": "can't find custom var"}))
        self.assertFalse(mesh_mc._remote_cli_reply_is_error({"text": "OK"}))
        self.assertFalse(mesh_mc._remote_cli_reply_is_error({"text": "password now: secret"}))
        self.assertTrue(mesh_mc._remote_cli_reply_is_error({"text": "ERR: clock cannot go backwards"}))
        self.assertTrue(mesh_mc._remote_cli_reply_is_benign_for_command("clock sync", {"text": "ERR: clock cannot go backwards"}))
        self.assertFalse(mesh_mc._remote_cli_reply_is_benign_for_command("set name x", {"text": "ERR: clock cannot go backwards"}))

    def test_api_mc_remote_read_calls_backend_helper(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        with mock.patch.object(mc_routes, "remote_repeater_read", return_value={"ok": True, "target": "abcdef"}) as read_mock:
            res = client.post("/api/mc/mc1/remote/abcdef123456/read", json={
                "login": True,
                "password": "secret",
            })

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["target"], "abcdef")
        read_mock.assert_called_once_with("mc1", "abcdef123456", password="secret", login=True)

    def test_api_mc_remote_read_can_request_login_only(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        with mock.patch.object(mc_routes, "remote_repeater_read", return_value={"ok": True, "target": "abcdef"}) as read_mock:
            res = client.post("/api/mc/mc1/remote/abcdef123456/read", json={
                "login": True,
                "login_only": True,
                "password": "secret",
            })

        self.assertEqual(res.status_code, 200)
        read_mock.assert_called_once_with(
            "mc1",
            "abcdef123456",
            password="secret",
            login=True,
            login_only=True,
        )

    def test_api_mc_remote_command_maps_validation_errors_to_400(self):
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        with mock.patch.object(mc_routes, "remote_repeater_command", side_effect=ValueError("Command is not in the safe remote-admin allowlist")):
            res = client.post("/api/mc/mc1/remote/abcdef123456/command", json={
                "command": "erase everything",
            })

        self.assertEqual(res.status_code, 400)
        self.assertIn("allowlist", res.get_json()["error"])

    def test_share_contact_exports_uri_and_qr_for_live_contact(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed" + "00" * 24
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "status": "connected",
                "contacts": {pubkey: {"adv_name": "Remote", "type": 1, "last_seen_ts": 123}},
                "live_contacts": {pubkey: {"adv_name": "Remote", "type": 1, "last_seen_ts": 123}},
            }
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get(f"/api/mc/{radio_id}/contacts/{pubkey[:12]}/share")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertIn("meshcore://contact/add?", payload["uri"])
        self.assertIn("public_key=deadbeef", payload["uri"])
        self.assertIn("type=1", payload["uri"])
        self.assertTrue(payload["official"])
        self.assertIn("<svg", payload["qr_svg"])
        self.assertEqual(payload["details"]["full_key"], pubkey)

    def test_share_contact_builds_qr_for_archive_only_contact(self):
        radio_id = "mc_test"
        pubkey = "deadbeefcafefeed" + "00" * 24
        mesh_mc._mc_archive_merge_contacts(radio_id, {
            pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}
        })
        with mc_connections_lock:
            mc_connections[radio_id] = {
                "status": "disconnected",
                "contacts": {pubkey: {"adv_name": "ArchiveOnly", "type": 1, "last_seen_ts": 123}},
                "live_contacts": {},
            }
        app = Flask(__name__)
        app.register_blueprint(mc_routes.bp)
        client = app.test_client()

        res = client.get(f"/api/mc/{radio_id}/contacts/{pubkey[:12]}/share")

        self.assertEqual(res.status_code, 200)
        self.assertIn("meshcore://contact/add?", res.get_json()["uri"])
        self.assertTrue(res.get_json()["official"])

    def test_sanitize_repairs_duplicated_ff_contact_record(self):
        good = "faff04a08bd6b67e8499b774d60809d23683c0b706e235eac5eae0fcb694cfb6"
        bad = "faffff04a08bd6b67e8499b774d60809d23683c0b706e235eac5eae0fcb694cf"
        key, contact = mesh_mc._mc_sanitize_contact(bad, {
            "public_key": bad,
            "adv_name": "ERA-2 TST RPTR(SI)",
            "type": 0xB6,
            "flags": 2,
            "out_path": "ffff",
            "out_path_len": 0,
            "out_path_hash_mode": 0,
            "adv_lat": -1199.0,
            "adv_lon": 541.0,
        })

        self.assertEqual(key, good)
        self.assertEqual(contact["public_key"], good)
        self.assertEqual(contact["type"], 2)
        self.assertEqual(contact["flags"], 0)
        self.assertEqual(contact["out_path_len"], -1)
        self.assertNotIn("adv_lat", contact)
        self.assertNotIn("adv_lon", contact)


class RxObsIdentityTests(unittest.TestCase):
    """Overheard RX-log packets carry no sender pubkey — see _rx_obs_identity."""

    def test_sender_prefix_wins_when_present(self):
        entry = {"pubkey_pre": "aabbcc", "path": "1122", "path_hash_size": 1}
        self.assertEqual(mesh_mc._rx_obs_identity(entry), "aabbcc")

    def test_falls_back_to_last_hop_when_no_sender(self):
        # flood path [11, 22, 33]: 33 appended last, so 33 is who we heard it from
        entry = {"path": "112233", "path_hash_size": 1, "path_len": 3}
        self.assertEqual(mesh_mc._rx_obs_identity(entry), "33")

    def test_last_hop_respects_multibyte_hash_size(self):
        entry = {"path": "11112222", "path_hash_size": 2, "path_len": 2}
        self.assertEqual(mesh_mc._rx_obs_identity(entry), "2222")

    def test_list_form_path_is_supported(self):
        entry = {"path": [{"hash": "AA"}, {"hash": "BB"}]}
        self.assertEqual(mesh_mc._rx_obs_identity(entry), "bb")

    def test_hop_count_wins_when_declared_hash_size_disagrees(self):
        # The wire format holds exactly path_len * hash_size bytes, so a declared
        # size that contradicts the buffer is the wrong one. Mis-slicing here
        # would file the observation under a node that never saw the packet.
        self.assertEqual(mesh_mc._rx_obs_hops({"path": "aaaabbbb", "path_len": 2}),
                         ["aaaa", "bbbb"])
        self.assertEqual(
            mesh_mc._rx_obs_hops({"path": "aaaabbbb", "path_len": 2, "path_hash_size": 1}),
            ["aaaa", "bbbb"])
        self.assertEqual(mesh_mc._rx_obs_identity({"path": "aaaabbbb", "path_len": 2}), "bbbb")

    def test_hops_fall_back_to_declared_size_when_hop_count_missing(self):
        self.assertEqual(mesh_mc._rx_obs_hops({"path": "aaaabbbb", "path_hash_size": 2}),
                         ["aaaa", "bbbb"])

    def test_empty_path_uses_sentinel_rather_than_being_dropped(self):
        entry = {"path": "", "path_len": 0, "snr": 4.25, "rssi": -99}
        self.assertEqual(mesh_mc._rx_obs_identity(entry), mesh_mc.RX_OBS_SENTINEL)

    def test_identity_never_empty_for_a_bare_payload(self):
        # The old gate required a sender prefix and therefore stored nothing at all.
        self.assertTrue(mesh_mc._rx_obs_identity({}))
        self.assertEqual(mesh_mc._rx_sender_prefix({}), "")


class LatLonForPrefixTests(unittest.TestCase):
    """1-byte hop hashes collide; a wrong coordinate gets plotted, so refuse to guess.

    Exercised through the batch index, which is the path both the rx flusher and
    the trace handler actually take.
    """

    def _archive(self, contacts):
        return mock.patch.object(mesh_mc, "get_mc_contact_archive", return_value=contacts)

    def _resolve(self, key):
        index = mesh_mc._build_prefix_latlon_index("r", {len(key)})
        return mesh_mc._latlon_from_index(index, key)

    def test_unique_prefix_resolves(self):
        with self._archive({"33aabb": {"adv_lat": 46.0, "adv_lon": 14.5}}):
            self.assertEqual(self._resolve("33"), (46.0, 14.5))

    def test_ambiguous_prefix_returns_nothing_rather_than_the_first_match(self):
        with self._archive({
            "33aabb": {"adv_lat": 46.0, "adv_lon": 14.5},
            "33ccdd": {"adv_lat": 47.0, "adv_lon": 15.5},
        }):
            self.assertEqual(self._resolve("33"), (None, None))

    def test_contacts_without_coords_do_not_count_as_collisions(self):
        with self._archive({
            "33aabb": {"adv_lat": 46.0, "adv_lon": 14.5},
            "33ccdd": {"adv_name": "no coords"},
        }):
            self.assertEqual(self._resolve("33"), (46.0, 14.5))

    def test_no_match_returns_nothing(self):
        with self._archive({"99aabb": {"adv_lat": 46.0, "adv_lon": 14.5}}):
            self.assertEqual(self._resolve("33"), (None, None))


class RxObsBufferingTests(unittest.TestCase):
    """on_rx_log_data runs inline on the asyncio loop — it must never write there."""

    def setUp(self):
        from meshcore import EventType
        self.EventType = EventType
        self.radio = "buffer_radio"
        db.delete_passive_obs(self.radio)
        with mesh_mc._rx_obs_buffer_lock:
            mesh_mc._rx_obs_buffer.clear()
            mesh_mc._rx_obs_dropped = 0
        # Never start the real flusher thread in tests — flush explicitly instead.
        patcher = mock.patch.object(mesh_mc, "_ensure_rx_obs_flusher")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        with mesh_mc._rx_obs_buffer_lock:
            mesh_mc._rx_obs_buffer.clear()
        db.delete_passive_obs(self.radio)

    def _fire(self, radio, payload):
        handlers = {}

        class FakeMC:
            def subscribe(self, evt, fn):
                handlers[evt] = fn

        mesh_mc._subscribe_mc_events(FakeMC(), radio, "BUF")
        handlers[self.EventType.RX_LOG_DATA](SimpleNamespace(payload=payload))

    def _packet(self, path="112233", hops=3):
        return {"snr": 4.25, "rssi": -99, "route_typename": "FLOOD",
                "payload_typename": "TXT_MSG", "path_len": hops,
                "path_hash_size": 1, "path": path, "recv_time": int(time.time())}

    def test_handler_buffers_and_does_not_write(self):
        self._fire(self.radio, self._packet())
        self.assertEqual(len(mesh_mc._rx_obs_buffer[self.radio]), 1)
        self.assertEqual(db.load_passive_obs(self.radio, limit=10, obs_types=["rx"]), [])

    def test_flush_writes_buffered_rows(self):
        self._fire(self.radio, self._packet())
        self.assertEqual(mesh_mc.flush_rx_obs(), 1)
        rows = db.load_passive_obs(self.radio, limit=10, obs_types=["rx"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pubkey_pre"], "33")
        self.assertEqual(rows[0]["payload_type"], "TXT_MSG")
        self.assertEqual(rows[0]["route_type"], "FLOOD")
        self.assertAlmostEqual(rows[0]["ts"], int(time.time()), delta=5)

    def test_flush_on_empty_buffer_is_a_noop(self):
        self.assertEqual(mesh_mc.flush_rx_obs(), 0)

    def test_flush_groups_rows_by_radio(self):
        other = "buffer_radio_2"
        db.delete_passive_obs(other)
        self.addCleanup(db.delete_passive_obs, other)
        self._fire(self.radio, self._packet())
        self._fire(other, self._packet(path="4455", hops=2))
        self.assertEqual(mesh_mc.flush_rx_obs(), 2)
        self.assertEqual(len(db.load_passive_obs(self.radio, limit=10, obs_types=["rx"])), 1)
        rows2 = db.load_passive_obs(other, limit=10, obs_types=["rx"])
        self.assertEqual([r["pubkey_pre"] for r in rows2], ["55"])

    def test_buffer_overflow_drops_oldest_and_keeps_newest(self):
        with mock.patch.object(mesh_mc, "RX_OBS_BUFFER_MAX", 3):
            for i in range(5):
                mesh_mc._queue_rx_obs(self.radio, {"pubkey_pre": f"k{i}",
                                                   "obs_type": "rx",
                                                   "ts": int(time.time()) + i})
            self.assertEqual(len(mesh_mc._rx_obs_buffer[self.radio]), 3)
            self.assertEqual(mesh_mc._rx_obs_dropped, 2)
        mesh_mc.flush_rx_obs()
        keys = [r["pubkey_pre"] for r in db.load_passive_obs(self.radio, limit=10, obs_types=["rx"])]
        self.assertEqual(sorted(keys), ["k2", "k3", "k4"])

    def test_one_radio_failure_does_not_discard_another_radios_batch(self):
        other = "buffer_radio_3"
        db.delete_passive_obs(other)
        self.addCleanup(db.delete_passive_obs, other)
        self._fire(self.radio, self._packet())
        self._fire(other, self._packet(path="4455", hops=2))

        real = mesh_mc.save_passive_obs_bulk

        def flaky(radio_id, rows, **kw):
            if radio_id == self.radio:
                raise sqlite3.OperationalError("boom")
            return real(radio_id, rows, **kw)

        with mock.patch.object(mesh_mc, "save_passive_obs_bulk", side_effect=flaky):
            mesh_mc.flush_rx_obs()
        self.assertEqual(len(db.load_passive_obs(other, limit=10, obs_types=["rx"])), 1)

    def test_coords_are_resolved_on_the_flusher_not_the_loop(self):
        archive = {"33" + "a" * 62: {"adv_lat": 46.5, "adv_lon": 14.5}}
        with mock.patch.object(mesh_mc, "get_mc_contact_archive", return_value=archive) as arch:
            self._fire(self.radio, self._packet())
            self.assertEqual(arch.call_count, 0)   # nothing touched the archive on the loop
            mesh_mc.flush_rx_obs()
            self.assertEqual(arch.call_count, 1)   # exactly once for the whole batch
        row = db.load_passive_obs(self.radio, limit=5, obs_types=["rx"])[0]
        self.assertEqual((row["lat"], row["lon"]), (46.5, 14.5))

    def test_archive_is_read_once_per_batch_not_once_per_row(self):
        archive = {"33" + "a" * 62: {"adv_lat": 46.5, "adv_lon": 14.5}}
        with mock.patch.object(mesh_mc, "get_mc_contact_archive", return_value=archive) as arch:
            for _ in range(25):
                self._fire(self.radio, self._packet())
            mesh_mc.flush_rx_obs()
            self.assertEqual(arch.call_count, 1)

    def test_ambiguous_prefix_still_resolves_to_no_coords(self):
        archive = {
            "33" + "a" * 62: {"adv_lat": 46.5, "adv_lon": 14.5},
            "33" + "b" * 62: {"adv_lat": 47.5, "adv_lon": 15.5},
        }
        with mock.patch.object(mesh_mc, "get_mc_contact_archive", return_value=archive):
            self._fire(self.radio, self._packet())
            mesh_mc.flush_rx_obs()
        row = db.load_passive_obs(self.radio, limit=5, obs_types=["rx"])[0]
        self.assertEqual((row["lat"], row["lon"]), (None, None))

    def test_sentinel_key_resolves_to_no_coords(self):
        archive = {"33" + "a" * 62: {"adv_lat": 46.5, "adv_lon": 14.5}}
        with mock.patch.object(mesh_mc, "get_mc_contact_archive", return_value=archive):
            self._fire(self.radio, self._packet(path="", hops=0))
            mesh_mc.flush_rx_obs()
        row = db.load_passive_obs(self.radio, limit=5, obs_types=["rx"])[0]
        self.assertEqual(row["pubkey_pre"], mesh_mc.RX_OBS_SENTINEL)
        self.assertEqual((row["lat"], row["lon"]), (None, None))

    def test_stale_device_timestamp_falls_back_to_now(self):
        now = int(time.time())
        # A ts older than the retention window would be pruned on insert, so the
        # observation would vanish with no trace.
        self.assertEqual(mesh_mc._rx_obs_timestamp({"recv_time": 5000}, now=now), now)
        self.assertEqual(mesh_mc._rx_obs_timestamp({"recv_time": 0}, now=now), now)
        self.assertEqual(mesh_mc._rx_obs_timestamp({"recv_time": "junk"}, now=now), now)
        self.assertEqual(mesh_mc._rx_obs_timestamp({}, now=now), now)
        # A plausible device time is kept.
        self.assertEqual(mesh_mc._rx_obs_timestamp({"recv_time": now - 30}, now=now), now - 30)

    def test_stale_timestamp_row_survives_the_round_trip(self):
        self._fire(self.radio, dict(self._packet(), recv_time=5000))
        mesh_mc.flush_rx_obs()
        rows = db.load_passive_obs(self.radio, limit=5, obs_types=["rx"])
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["ts"], int(time.time()), delta=5)

    def test_failed_batch_is_requeued_then_dropped_after_max_attempts(self):
        self._fire(self.radio, self._packet())

        def boom(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(mesh_mc, "save_passive_obs_bulk", side_effect=boom):
            for expected in range(1, mesh_mc.RX_OBS_MAX_FLUSH_ATTEMPTS + 1):
                mesh_mc.flush_rx_obs()
                self.assertEqual(len(mesh_mc._rx_obs_buffer[self.radio]), 1)
                self.assertEqual(mesh_mc._rx_obs_buffer[self.radio][0]["_attempts"], expected)
            # One attempt past the cap and the row is finally discarded.
            mesh_mc.flush_rx_obs()
            self.assertEqual(sum(len(q) for q in mesh_mc._rx_obs_buffer.values()), 0)

    def test_requeued_batch_is_written_when_the_db_recovers(self):
        self._fire(self.radio, self._packet())
        with mock.patch.object(mesh_mc, "save_passive_obs_bulk",
                               side_effect=sqlite3.OperationalError("locked")):
            mesh_mc.flush_rx_obs()
        self.assertEqual(len(mesh_mc._rx_obs_buffer[self.radio]), 1)
        mesh_mc.flush_rx_obs()          # DB healthy again
        self.assertEqual(len(db.load_passive_obs(self.radio, limit=5, obs_types=["rx"])), 1)

    def test_chatty_radio_does_not_evict_a_quiet_radios_buffered_rows(self):
        with mock.patch.object(mesh_mc, "RX_OBS_BUFFER_MAX", 3):
            mesh_mc._queue_rx_obs("quiet_radio", {"pubkey_pre": "q", "obs_type": "rx"})
            for i in range(10):
                mesh_mc._queue_rx_obs("chatty_radio", {"pubkey_pre": f"c{i}", "obs_type": "rx"})
            buffered = {cid: len(q) for cid, q in mesh_mc._rx_obs_buffer.items()}
        self.assertEqual(buffered.get("quiet_radio"), 1)   # never evicted
        self.assertEqual(buffered.get("chatty_radio"), 3)  # capped per radio

    def test_atexit_flush_writes_and_returns(self):
        self._fire(self.radio, self._packet())
        mesh_mc._atexit_flush_rx_obs()
        self.assertEqual(len(db.load_passive_obs(self.radio, limit=5, obs_types=["rx"])), 1)

    def test_bulk_write_applies_retention_once(self):
        rows = [{"pubkey_pre": f"h{i}", "obs_type": "rx", "ts": int(time.time()) + i} for i in range(6)]
        db.save_passive_obs_bulk(self.radio, rows, max_rows_for_type=2)
        kept = db.load_passive_obs(self.radio, limit=10, obs_types=["rx"])
        self.assertEqual(sorted(r["pubkey_pre"] for r in kept), ["h4", "h5"])

    def test_bulk_write_prunes_by_age_within_its_type_only(self):
        db.save_passive_obs(self.radio, "nodeD", "trace", snr=1.0, observed_ts=1)
        db.save_passive_obs_bulk(
            self.radio,
            [{"pubkey_pre": "old", "obs_type": "rx", "ts": 1},
             {"pubkey_pre": "new", "obs_type": "rx", "ts": int(time.time())}],
            max_age_s=3600,
        )
        rows = db.load_passive_obs(self.radio, limit=10)
        self.assertEqual(sorted(r["obs_type"] for r in rows), ["rx", "trace"])
        self.assertEqual([r["pubkey_pre"] for r in rows if r["obs_type"] == "rx"], ["new"])


class PacketTypeFilterTests(unittest.TestCase):
    """Filter in SQL, and take the type strings from the library, not the docs."""

    def setUp(self):
        self.radio = "filter_radio"
        db.delete_passive_obs(self.radio)
        now = int(time.time())
        rows = []
        for i, (payload, route) in enumerate([
            ("TEXT_MSG", "FLOOD"), ("ADVERT", "FLOOD"), ("ACK", "DIRECT"),
            ("TEXT_MSG", "TC_FLOOD"), ("UNK", "FLOOD"),
        ] * 3):
            rows.append({"pubkey_pre": f"h{i}", "obs_type": "rx", "ts": now + i,
                         "payload_type": payload, "route_type": route})
        db.save_passive_obs_bulk(self.radio, rows, max_age_s=86400)
        self.app = Flask(__name__)
        self.app.register_blueprint(mc_routes.bp)
        self.client = self.app.test_client()

    def tearDown(self):
        db.delete_passive_obs(self.radio)

    def test_library_is_the_source_of_the_type_names(self):
        names = self.client.get("/api/mc/packet_types").get_json()
        # The strings that are actually stored — not the protocol doc's names.
        self.assertIn("TEXT_MSG", names["payload_types"])
        self.assertNotIn("TXT_MSG", names["payload_types"])
        self.assertIn("TC_FLOOD", names["route_types"])
        self.assertNotIn("TRANSPORT_FLOOD", names["route_types"])
        # "UNK" is a real stored value for types outside the library's table.
        self.assertIn("UNK", names["payload_types"])

    def test_payload_type_filter(self):
        rows = self.client.get(
            f"/api/mc/{self.radio}/passive_obs?obs_types=rx&payload_types=ADVERT&limit=500"
        ).get_json()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["payload_type"] == "ADVERT" for r in rows))

    def test_multiple_payload_types(self):
        rows = self.client.get(
            f"/api/mc/{self.radio}/passive_obs?obs_types=rx&payload_types=ADVERT,ACK&limit=500"
        ).get_json()
        self.assertEqual(sorted({r["payload_type"] for r in rows}), ["ACK", "ADVERT"])

    def test_route_type_filter(self):
        rows = self.client.get(
            f"/api/mc/{self.radio}/passive_obs?obs_types=rx&route_types=DIRECT&limit=500"
        ).get_json()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["route_type"] == "DIRECT" for r in rows))

    def test_filter_applies_before_limit_not_after(self):
        # The point of filtering in SQL: limit=2 must return the 2 newest ADVERTs,
        # not "the adverts that happen to be inside the newest 2 rows" (zero).
        rows = self.client.get(
            f"/api/mc/{self.radio}/passive_obs?obs_types=rx&payload_types=ADVERT&limit=2"
        ).get_json()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["payload_type"] == "ADVERT" for r in rows))

    def test_unknown_type_is_selectable(self):
        rows = self.client.get(
            f"/api/mc/{self.radio}/passive_obs?obs_types=rx&payload_types=UNK&limit=500"
        ).get_json()
        self.assertEqual(len(rows), 3)


class PassiveDbInitTests(unittest.TestCase):
    """The DDL used to run on every single passive-DB access."""

    def setUp(self):
        self.radio = "init_memo_radio"
        db.delete_passive_obs(self.radio)
        db._mc_passive_db_initialized.clear()

    def tearDown(self):
        db.delete_passive_obs(self.radio)

    def test_ddl_runs_once_across_many_accesses(self):
        real = db._init_mc_passive_db
        with mock.patch.object(db, "_init_mc_passive_db", side_effect=real) as init:
            for i in range(10):
                db.save_passive_obs(self.radio, f"n{i}", "trace", snr=1.0)
            db.load_passive_obs(self.radio, limit=5)
            self.assertEqual(init.call_count, 1)

    def test_deleted_db_file_is_reinitialised(self):
        db.save_passive_obs(self.radio, "n1", "trace", snr=1.0)
        path = db._resolved_mc_passive_db_path(self.radio)
        os.remove(path)
        real = db._init_mc_passive_db
        with mock.patch.object(db, "_init_mc_passive_db", side_effect=real) as init:
            db.save_passive_obs(self.radio, "n2", "trace", snr=2.0)
            self.assertEqual(init.call_count, 1)
        rows = db.load_passive_obs(self.radio, limit=5)
        self.assertEqual([r["pubkey_pre"] for r in rows], ["n2"])


class PassiveObsRetentionTests(unittest.TestCase):
    def setUp(self):
        self.radio = "test_retention_radio"
        db.delete_passive_obs(self.radio)

    def tearDown(self):
        db.delete_passive_obs(self.radio)

    def test_per_contact_cap_still_applies_by_default(self):
        for i in range(5):
            db.save_passive_obs(self.radio, "nodeA", "trace", snr=float(i),
                                max_per_contact=3, observed_ts=1000 + i)
        rows = db.load_passive_obs(self.radio, pubkey_pre="nodeA", limit=50)
        self.assertEqual(len(rows), 3)

    def test_age_retention_is_scoped_to_its_obs_type(self):
        now = int(time.time())
        # An old row of a DIFFERENT type must survive rx pruning.
        db.save_passive_obs(self.radio, "nodeB", "trace", snr=1.0, observed_ts=now - 99999)
        db.save_passive_obs_bulk(
            self.radio,
            [{"pubkey_pre": "hop1", "obs_type": "rx", "snr": 2.0, "ts": now - 99999},
             {"pubkey_pre": "hop2", "obs_type": "rx", "snr": 3.0, "ts": now}],
            max_age_s=3600,
        )

        rows = db.load_passive_obs(self.radio, limit=50)
        kinds = sorted(r["obs_type"] for r in rows)
        self.assertEqual(kinds, ["rx", "trace"])
        rx_keys = [r["pubkey_pre"] for r in rows if r["obs_type"] == "rx"]
        self.assertEqual(rx_keys, ["hop2"])

    def test_row_cap_is_scoped_to_its_obs_type(self):
        now = int(time.time())
        db.save_passive_obs(self.radio, "nodeE", "trace", snr=9.0, observed_ts=now)
        db.save_passive_obs_bulk(
            self.radio,
            [{"pubkey_pre": f"hop{i}", "obs_type": "rx", "snr": float(i), "ts": now + i}
             for i in range(6)],
            max_rows_for_type=2,
        )
        rows = db.load_passive_obs(self.radio, limit=50)
        # The cap trims rx only — the trace row is untouched.
        self.assertEqual(sorted(r["pubkey_pre"] for r in rows if r["obs_type"] == "rx"),
                         ["hop4", "hop5"])
        self.assertEqual(len([r for r in rows if r["obs_type"] == "trace"]), 1)

    def test_obs_type_scoping_keeps_timeline_out_of_per_node_queries(self):
        now = int(time.time())
        db.save_passive_obs(self.radio, "nodeC", "trace", snr=1.0, observed_ts=now)
        db.save_passive_obs_bulk(
            self.radio,
            [{"pubkey_pre": f"hop{i}", "obs_type": "rx", "snr": 2.0, "ts": now + 1 + i}
             for i in range(5)],
            max_age_s=86400,
        )

        # Default endpoint behaviour: newest rows, rx excluded — the trace row
        # must still be visible even though rx traffic is newer and more numerous.
        kept = db.load_passive_obs(self.radio, limit=3, exclude_obs_types=["rx"])
        self.assertEqual([r["obs_type"] for r in kept], ["trace"])

        # The flow view asks for rx explicitly.
        timeline = db.load_passive_obs(self.radio, limit=10, obs_types=["rx"])
        self.assertEqual(len(timeline), 5)
        self.assertTrue(all(r["obs_type"] == "rx" for r in timeline))

        # Unscoped still returns everything, for callers that want it.
        self.assertEqual(len(db.load_passive_obs(self.radio, limit=10)), 6)

    def test_chatty_key_does_not_evict_a_quiet_one_in_timeline_mode(self):
        # The exact failure the per-contact cap would cause on a packet timeline.
        now = int(time.time())
        db.save_passive_obs_bulk(
            self.radio,
            [{"pubkey_pre": "quiet", "obs_type": "rx", "snr": 1.0, "ts": now}]
            + [{"pubkey_pre": "chatty", "obs_type": "rx", "snr": float(i), "ts": now + 1 + i}
               for i in range(10)],
            max_age_s=86400,
        )
        rows = db.load_passive_obs(self.radio, limit=50)
        self.assertIn("quiet", [r["pubkey_pre"] for r in rows])
        self.assertEqual(len([r for r in rows if r["pubkey_pre"] == "chatty"]), 10)


if __name__ == "__main__":
    unittest.main()

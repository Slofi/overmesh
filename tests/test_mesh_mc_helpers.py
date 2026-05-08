import json
import os
import sys
import tempfile
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
from routes import mc as mc_routes  # noqa: E402
from config import CONFIG  # noqa: E402
from state import mc_connections, mc_connections_lock  # noqa: E402


class MeshMcPathHelperTests(unittest.TestCase):
    def setUp(self):
        mesh_mc.DATA_DIR = str(_DATA_DIR)
        mesh_mc.MC_CONTACT_ARCHIVE_PATH = str(_DATA_DIR / "mc_contacts_archive.json")
        mesh_mc._mc_contact_archive_cache = None
        archive_path = _DATA_DIR / "mc_contacts_archive.json"
        if archive_path.exists():
            archive_path.unlink()
        with mc_connections_lock:
            mc_connections.clear()
        self._orig_mc_nodes = list(CONFIG.get("mc_nodes", []))
        CONFIG["mc_nodes"] = []

    def tearDown(self):
        CONFIG["mc_nodes"] = list(self._orig_mc_nodes)

    def test_rx_path_hash_mode_is_used_when_explicit_size_missing(self):
        size = mesh_mc._mc_path_hash_size_from_msg({}, {"path_hash_mode": 1})
        self.assertEqual(size, 2)

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
        self.assertEqual(mesh_mc._validate_remote_admin_command("discover.neighbors"), "discover.neighbors")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set radio 869.525,250,11,5"), "set radio 869.525,250,11,5")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set advert.interval 60"), "set advert.interval 60")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set flood.advert.interval 12"), "set flood.advert.interval 12")
        self.assertEqual(mesh_mc._validate_remote_admin_command("set loop.detect minimal"), "set loop.detect minimal")

    def test_remote_admin_command_allowlist_rejects_unknown_commands(self):
        with self.assertRaises(ValueError):
            mesh_mc._validate_remote_admin_command("erase everything")
        with self.assertRaises(ValueError):
            mesh_mc._validate_remote_admin_command("set prv.key abc")

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


if __name__ == "__main__":
    unittest.main()

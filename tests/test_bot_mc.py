import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-bot-test-")
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
from config import CONFIG  # noqa: E402
from state import mc_connections, mc_connections_lock  # noqa: E402


class McBotHopReplyTests(unittest.TestCase):
    def setUp(self):
        with mc_connections_lock:
            mc_connections.clear()
        self._orig_mc_nodes = list(CONFIG.get("mc_nodes", []))
        CONFIG["mc_nodes"] = []

    def tearDown(self):
        with mc_connections_lock:
            mc_connections.clear()
        CONFIG["mc_nodes"] = list(self._orig_mc_nodes)

    def test_mc_test_hops_reports_direct_path_with_byte_suffix_when_known(self):
        self.assertEqual(
            bot._build_mc_test_hops_info({"path_len": 0, "path_hash_size": 2}, "mc1"),
            " | Hops(0): direct | 2byte",
        )

    def test_mc_test_hops_direct_path_uses_configured_byte_suffix(self):
        CONFIG["mc_nodes"] = [{"id": "mc1", "path_hash_mode": 1}]

        self.assertEqual(
            bot._build_mc_test_hops_info({"path_len": 0}, "mc1"),
            " | Hops(0): direct | 2byte",
        )

    def test_mc_test_hops_resolves_short_hop_ids(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "contacts": {
                    "aa00" + "1" * 60: {"public_key": "aa00" + "1" * 60, "adv_name": "Argus RPTR", "type": 2},
                    "bb00" + "2" * 60: {"public_key": "bb00" + "2" * 60, "adv_name": "Relay-B", "type": 2},
                }
            }

        label = bot._build_mc_test_hops_info({"path": "aa00bb00", "path_len": 2, "path_hash_size": 2}, "mc1")

        self.assertEqual(label, " | Hops(2): AA0, BB0 | 2byte")

    def test_mc_test_hops_prefers_repeater_on_hash_collision(self):
        with mc_connections_lock:
            mc_connections["mc1"] = {
                "contacts": {
                    "aa11" + "1" * 60: {"public_key": "aa11" + "1" * 60, "adv_name": "Alice", "type": 1},
                    "aa22" + "2" * 60: {"public_key": "aa22" + "2" * 60, "adv_name": "Rptr South", "type": 2},
                }
            }

        label = bot._build_mc_test_hops_info({"path": "aa", "path_len": 1, "path_hash_size": 1}, "mc1")

        self.assertEqual(label, " | Hops(1): AA2 | 1byte")

    def test_mc_test_hops_falls_back_to_count_without_path_hashes(self):
        self.assertEqual(bot._build_mc_test_hops_info({"path_len": 2}, "mc1"), " | Hops(2): unknown | 3byte")

    def test_mc_test_hops_uses_hash_prefix_when_contact_unresolved(self):
        label = bot._build_mc_test_hops_info({"path": "aabbccddeeff", "path_len": 2, "path_hash_size": 3}, "mc1")

        self.assertEqual(label, " | Hops(2): AAB, DDE | 3byte")


class McBotReplyDelayTests(unittest.TestCase):
    """Replies to an incoming message wait a random few seconds before sending,
    so they do not land on top of the repeaters rebroadcasting that message (S470)."""

    def setUp(self):
        import mesh_mc
        self.events = []
        self._orig_silent = CONFIG.get("silent_mode")
        CONFIG["silent_mode"] = False
        patches = [
            mock.patch.object(bot, "_mc_bot_reply_wait", side_effect=lambda: self.events.append("wait")),
            mock.patch.object(mesh_mc, "send_chan_msg", side_effect=lambda *a, **k: self.events.append(("chan", a[1], a[2]))),
            mock.patch.object(mesh_mc, "send_dm", side_effect=lambda *a, **k: self.events.append(("dm", a[1], a[2]))),
            mock.patch.object(bot, "push_to_sse", side_effect=lambda m: self.events.append(("sse", m["text"]))),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        CONFIG["silent_mode"] = self._orig_silent

    def test_command_reply_waits_then_sends_then_shows(self):
        bot.send_and_record_mc_bot_response("mc1", "[OM Bot CD] PONG", 2, None, "EDC-3", "ping")
        self.assertEqual(self.events, ["wait", ("chan", 2, "[OM Bot CD] PONG"), ("sse", "[OM Bot CD] PONG")])

    def test_dm_reply_waits_too(self):
        bot.send_and_record_mc_bot_response("mc1", "hi", 0, "abcd", "EDC-3", "ack")
        self.assertEqual(self.events[:2], ["wait", ("dm", "abcd", "hi")])

    def test_silent_running_switched_on_during_the_wait_still_holds(self):
        def wait():
            self.events.append("wait")
            CONFIG["silent_mode"] = True
        bot._mc_bot_reply_wait.side_effect = wait
        self.assertFalse(bot.send_mc_bot_response("mc1", "x", 2, reply_delay=True))
        self.assertEqual(self.events, ["wait"])   # nothing sent

    def test_non_reply_sends_do_not_wait(self):
        # MOTD and the Settings test button are not answers to anything.
        self.assertTrue(bot.send_mc_bot_response("mc1", "motd", 2))
        self.assertEqual(self.events, [("chan", 2, "motd")])

    def test_relay_forward_waits(self):
        import inspect
        src = inspect.getsource(bot.handle_mc_bot_command)
        self.assertIn('args=(config_id, relay_text, relay_ch, dest),\n                            kwargs={"reply_delay": True}', src)

    def test_wait_is_random_within_bounds(self):
        mock.patch.stopall()
        lo, hi = bot.MC_BOT_REPLY_DELAY_S
        self.assertTrue(2.0 <= lo < hi <= 10.0, bot.MC_BOT_REPLY_DELAY_S)
        slept = []
        with mock.patch.object(bot.time, "sleep", side_effect=slept.append):
            for _ in range(50):
                bot._mc_bot_reply_wait()
        self.assertTrue(all(lo <= s <= hi for s in slept))
        self.assertGreater(len(set(slept)), 1)   # random, not a fixed delay


if __name__ == "__main__":
    unittest.main()

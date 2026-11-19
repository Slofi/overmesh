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


def _rptr(prefix, name, lat, lon, seen=1_700_000_000, ctype=2):
    # Distinct public keys that still share the 1/2-byte hash prefix.
    tail = str(sum(ord(ch) for ch in name) % 10**12)
    key = (prefix + tail).ljust(64, "0")
    return {
        "public_key": key,
        "adv_name": name,
        "type": ctype,
        "adv_lat": lat,
        "adv_lon": lon,
        "last_seen_ts": seen,
    }


class McBotWholePathTests(unittest.TestCase):
    """EST-460 / S514: resolve the whole MC path, not each hop on its own.

    The real collision: hash 86a8 is both SI-LJ-Trnovo II (Ljubljana,
    46.042/14.505, older advert) and Dioniso (Pordenone, 46.081/12.522,
    newer advert). Recency alone picks Italy; the path says Ljubljana.
    """

    RADIO = (46.0409, 14.4981)

    def setUp(self):
        with mc_connections_lock:
            mc_connections.clear()
        self._orig_mc_nodes = list(CONFIG.get("mc_nodes", []))
        CONFIG["mc_nodes"] = []

    def tearDown(self):
        with mc_connections_lock:
            mc_connections.clear()
        CONFIG["mc_nodes"] = list(self._orig_mc_nodes)

    def _install(self, contacts, radio_pos=None):
        data = {"contacts": {c["public_key"]: c for c in contacts}}
        if radio_pos:
            data["node_info"] = {"adv_lat": radio_pos[0], "adv_lon": radio_pos[1]}
        with mc_connections_lock:
            mc_connections["mc1"] = data

    def test_five_hop_slovenian_route_is_unchanged(self):
        # cebe -> ea61 -> c2aa -> 66df -> 1cce (sender near Radovljica to our
        # radio near Ljubljana); legs 5.4 / 41.1 / 25.7 / 62.1 km.
        route = [
            _rptr("cebe", "SI-KR-Radolca", 46.3517, 14.1723),
            _rptr("ea61", "SI-Dobrca", 46.3770, 14.2316),
            _rptr("c2aa", "SI-LJ-Dunajska5", 46.0595, 14.5061),
            _rptr("66df", "SI-Jost", 46.2429, 14.3038),
            _rptr("1cce", "SI-Mrzlica", 46.1874, 15.1074),
        ]
        self._install(route, self.RADIO)
        hashes = [c["public_key"][:4] for c in route]

        label = bot._build_mc_test_hops_info(
            {"path": "".join(hashes), "path_len": 5, "path_hash_size": 2}, "mc1"
        )
        self.assertEqual(label, " | Hops(5): CEB, EA6, C2A, 66D, 1CC | 2byte")

        resolutions = bot._mc_resolve_path_contacts("mc1", hashes, None, self.RADIO)
        names = [r["contact"]["adv_name"] for r in resolutions]
        self.assertEqual(names, [c["adv_name"] for c in route])

    def test_three_hop_ending_in_86a8_picks_ljubljana_not_pordenone(self):
        lj = _rptr("86a8", "SI-LJ-Trnovo II", 46.042, 14.505, seen=1_600_000_000)
        it = _rptr("86a8", "Dioniso", 46.081, 12.522, seen=1_710_000_000)
        a = _rptr("aa11", "RPTR-A", 46.30, 14.40)
        b = _rptr("bb22", "RPTR-B", 46.20, 14.45)
        self._install([lj, it, a, b], self.RADIO)

        # Path order: sender -> aa11 -> bb22 -> 86a8 (next to our radio).
        resolutions = bot._mc_resolve_path_contacts("mc1", ["aa11", "bb22", "86a8"], None, self.RADIO)
        self.assertEqual(resolutions[-1]["contact"]["adv_name"], "SI-LJ-Trnovo II")
        self.assertFalse(resolutions[-1]["ambiguous"])

    def test_single_hop_resolver_uses_distance_to_our_radio(self):
        lj = _rptr("86a8", "SI-LJ-Trnovo II", 46.042, 14.505, seen=1_600_000_000)
        it = _rptr("86a8", "Dioniso", 46.081, 12.522, seen=1_710_000_000)
        self._install([lj, it], self.RADIO)

        self.assertEqual(bot._mc_resolve_hop_contact("mc1", "86a8")["adv_name"], "SI-LJ-Trnovo II")

    def test_mid_path_collision_is_settled_by_continuity(self):
        left = _rptr("77cc", "RPTR-Left", 46.20, 14.40, seen=1_700_000_000)
        far = _rptr("77cc", "RPTR-Far", 45.20, 16.90, seen=1_710_000_000)
        a = _rptr("aa11", "RPTR-A", 46.25, 14.35)
        b = _rptr("bb22", "RPTR-B", 46.15, 14.45)
        self._install([left, far, a, b], self.RADIO)

        resolutions = bot._mc_resolve_path_contacts("mc1", ["aa11", "77cc", "bb22"], None, self.RADIO)

        self.assertEqual(resolutions[1]["contact"]["adv_name"], "RPTR-Left")

    def test_no_positions_falls_back_to_todays_recency_choice(self):
        newer = _rptr("99dd", "RPTR-Newer", None, None, seen=1_710_000_000)
        older = _rptr("99dd", "RPTR-Older", None, None, seen=1_600_000_000)
        with mc_connections_lock:
            mc_connections["mc1"] = {"contacts": {newer["public_key"]: newer, older["public_key"]: older}}

        self.assertIsNone(bot._mc_resolve_path_contacts("mc1", ["99dd"], None, self.RADIO))
        self.assertEqual(bot._mc_resolve_hop_contact("mc1", "99dd")["adv_name"], "RPTR-Newer")


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

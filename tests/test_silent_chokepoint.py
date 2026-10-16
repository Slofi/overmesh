"""Silent Running is enforced at the RADIO BOUNDARY, not at the callers (S453).

Until 2026-10-16 the flag was checked at the ~12 API entry points that call a
transmit. That is a convention, not a guarantee, and it leaked — both cases
reproduced against this tree before the fix:

  * the Scan sweep checks once, then transmits a second time after sleep(8);
  * the Ping path schedules a DETACHED task which transmits and re-checks
    nothing (the advert path re-checks itself with a comment saying why).

install_silent_tx_guard() wraps the per-connection commands object, so a
transmitting command is refused at the moment of transmission — whoever asks,
including a call site added later and a method this app has never called.

These tests assert on WHAT REACHES THE RADIO, so the two leak tests fail on the
old behaviour instead of passing for the wrong reason.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-silent-chokepoint-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}),
                        encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

import mesh_mc  # noqa: E402
from config import CONFIG  # noqa: E402

FULL_KEY = "ab" * 32


class _Event:
    def __init__(self, type_):
        self.type = type_


class _Sub:
    def unsubscribe(self):
        pass


class _RecordingCommands:
    """Stands in for meshcore's CommandHandler; records what reached the radio."""

    def __init__(self, silence_after_first=False):
        self.sent = []
        self.silence_after_first = silence_after_first
        self.dispatcher = "the-dispatcher-object"     # non-callable attribute
        self.default_timeout = 75.0                   # set by the library itself
        self.writes = []
        self.reboots = 0

    def _record(self, name):
        self.sent.append(name)
        if self.silence_after_first and len(self.sent) == 1:
            CONFIG["silent_mode"] = True      # the user silences the rig right here

    # --- transmitting (must be refused while silent) ---
    async def send_node_discover_req(self, **kw):
        self._record("send_node_discover_req")
        return _Event(mesh_mc.EventType.OK)

    async def send_binary_req(self, contact, req_type):
        self._record("send_binary_req")
        return _Event(mesh_mc.EventType.MSG_SENT)

    async def send_advert(self, flood=False):
        self._record("send_advert")
        return _Event(mesh_mc.EventType.OK)

    async def send_chan_msg(self, chan_idx, text):
        self._record("send_chan_msg")
        return _Event(mesh_mc.EventType.OK)

    def req_acl_sync(self, contact):              # sync callable, never used by the app
        self._record("req_acl_sync")
        return _Event(mesh_mc.EventType.OK)

    async def run_cli_command(self, cmd):         # could be anything, incl. TX
        self._record("run_cli_command")
        return _Event(mesh_mc.EventType.OK)

    # --- local (must keep working while silent) ---
    async def get_contacts(self):
        return _Event(mesh_mc.EventType.CONTACTS)

    async def set_time(self, ts):
        self.writes.append(("set_time", ts))
        return _Event(mesh_mc.EventType.OK)

    async def set_coords(self, lat, lon):
        self.writes.append(("set_coords", lat, lon))
        return _Event(mesh_mc.EventType.OK)

    async def reboot(self):
        self.reboots += 1
        return _Event(mesh_mc.EventType.OK)

    async def send_appstart(self):
        return _Event(mesh_mc.EventType.OK)


class _FakeMc:
    def __init__(self, commands):
        self.commands = commands

    def subscribe(self, event, handler):
        return _Sub()


class _SilentTestCase(unittest.TestCase):
    def setUp(self):
        self._backup = json.loads(json.dumps(CONFIG))
        CONFIG.clear()
        CONFIG.update({"nodes": [], "mc_nodes": [], "silent_mode": False})
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        mock.patch.stopall()
        mesh_mc._statusreq_tasks.clear()
        mesh_mc.mc_connections.clear()
        CONFIG.clear()
        CONFIG.update(self._backup)


class ChokepointTests(_SilentTestCase):
    def test_transmitting_command_is_refused_while_silent(self):
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        CONFIG["silent_mode"] = True
        with self.assertRaises(mesh_mc.SilentRunningBlocked):
            asyncio.run(mc.commands.send_advert(flood=True))
        self.assertEqual(cmds.sent, [], "nothing may reach the radio")

    def test_local_commands_still_work_while_silent(self):
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        CONFIG["silent_mode"] = True

        async def exercise():
            await mc.commands.get_contacts()
            await mc.commands.set_time(123)
            await mc.commands.set_coords(46.0, 14.0)
            await mc.commands.send_appstart()
            await mc.commands.reboot()

        asyncio.run(exercise())
        self.assertEqual(cmds.writes, [("set_time", 123), ("set_coords", 46.0, 14.0)])
        self.assertEqual(cmds.reboots, 1)
        self.assertEqual(cmds.sent, [])

    def test_nothing_is_blocked_while_silent_is_off(self):
        """With Silent Running off the proxy must be transparent — a library
        update cannot change or break normal behaviour."""
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        CONFIG["silent_mode"] = False
        asyncio.run(mc.commands.send_advert(flood=True))
        asyncio.run(mc.commands.send_chan_msg(0, "hi"))
        cmds.req_acl_sync({"k": "v"})
        self.assertEqual(cmds.sent, ["send_advert", "send_chan_msg", "req_acl_sync"])

    def test_sync_command_the_app_never_calls_is_covered_too(self):
        """A future call site must inherit the guarantee, so the guard cannot be a
        hand-maintained list of the methods this app happens to use."""
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        CONFIG["silent_mode"] = True
        with self.assertRaises(mesh_mc.SilentRunningBlocked):
            mc.commands.req_acl_sync({"k": "v"})
        with self.assertRaises(mesh_mc.SilentRunningBlocked):
            asyncio.run(mc.commands.run_cli_command("advert"))
        self.assertEqual(cmds.sent, [])

    def test_attributes_pass_through_and_writes_are_forwarded(self):
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        self.assertIs(mc.commands.dispatcher, "the-dispatcher-object")
        mc.commands.default_timeout = 8.0
        self.assertEqual(cmds.default_timeout, 8.0)
        CONFIG["silent_mode"] = True
        self.assertIs(mc.commands.dispatcher, "the-dispatcher-object")

    def test_a_stored_callable_is_not_treated_as_a_command(self):
        """The library keeps callbacks as attributes (set_contact_getter_by_prefix
        stores a bound method). Those are not transmissions, they are somebody
        else's object, and their identity must survive — only real command
        methods are guarded."""
        cmds = _RecordingCommands()
        stored = lambda: "not a command"          # noqa: E731
        cmds.contact_getter = stored
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        CONFIG["silent_mode"] = True
        self.assertIs(mc.commands.contact_getter, stored)
        self.assertEqual(mc.commands.contact_getter(), "not a command")

    def test_a_bound_method_stored_on_the_library_object_also_passes_through(self):
        """`set_contact_getter_by_prefix(mc.get_contact_by_key_prefix)` — bound to
        the MeshCore object, not to the command handler, so it must NOT be wrapped."""
        cmds = _RecordingCommands()
        cmds.other_owner_method = self.assertIs      # bound to this TestCase, not to cmds
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        CONFIG["silent_mode"] = True
        self.assertIs(mc.commands.other_owner_method.__self__, self)

    def test_install_is_idempotent(self):
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        first = mc.commands
        mesh_mc.install_silent_tx_guard(mc)
        self.assertIs(mc.commands, first, "a reconnect must not stack wrappers")
        CONFIG["silent_mode"] = True
        with self.assertRaises(mesh_mc.SilentRunningBlocked):
            asyncio.run(mc.commands.send_advert())


class LateTransmissionTests(_SilentTestCase):
    """The two leaks reproduced on 2026-10-16, asserted on what reached the radio."""

    def test_scan_sweep_does_not_transmit_again_after_silent_turns_on(self):
        cmds = _RecordingCommands(silence_after_first=True)
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        asyncio.run(mesh_mc._mc_collect_responders("mc1", mc, sweep_s=0.05))
        self.assertEqual(cmds.sent, ["send_node_discover_req"],
                         "the retry after sleep() must not reach the radio")

    def test_detached_ping_task_does_not_transmit_after_silent_turns_on(self):
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        mesh_mc.mc_connections["mc1"] = {"contacts": {FULL_KEY: {"adv_name": "Tester"}}}
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(mc, None)):
            # the entry-point gate passes (silent is off), then the user silences
            mesh_mc._ensure_mc_tx_allowed("MC status request")
            CONFIG["silent_mode"] = True
            asyncio.run(mesh_mc._send_statusreq_async("mc1", FULL_KEY))
            asyncio.run(asyncio.sleep(0.05))       # let the detached task run
        self.assertEqual(cmds.sent, [], "the detached task must not reach the radio")

    def test_ping_still_transmits_when_not_silent(self):
        """Positive control: the test above must be able to see a transmission."""
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)
        mesh_mc.install_silent_tx_guard(mc)
        mesh_mc.mc_connections["mc1"] = {"contacts": {FULL_KEY: {"adv_name": "Tester"}}}
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(mc, None)):
            asyncio.run(mesh_mc._send_statusreq_async("mc1", FULL_KEY))
            asyncio.run(asyncio.sleep(0.05))
        self.assertEqual(cmds.sent, ["send_binary_req"])

    def test_arming_the_scan_leak_test(self):
        """Without the guard the sweep transmits twice — so the leak test above can
        fail, and is not passing because it is looking at nothing."""
        cmds = _RecordingCommands(silence_after_first=True)
        mc = _FakeMc(cmds)                      # deliberately NOT wrapped
        asyncio.run(mesh_mc._mc_collect_responders("mc1", mc, sweep_s=0.05))
        self.assertEqual(cmds.sent, ["send_node_discover_req", "send_node_discover_req"])

    def test_arming_the_detached_ping_leak_test(self):
        """Without the guard the detached task transmits while silenced."""
        cmds = _RecordingCommands()
        mc = _FakeMc(cmds)                      # deliberately NOT wrapped
        mesh_mc.mc_connections["mc1"] = {"contacts": {FULL_KEY: {"adv_name": "Tester"}}}
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(mc, None)):
            mesh_mc._ensure_mc_tx_allowed("MC status request")
            CONFIG["silent_mode"] = True
            asyncio.run(mesh_mc._send_statusreq_async("mc1", FULL_KEY))
            asyncio.run(asyncio.sleep(0.05))
        self.assertEqual(cmds.sent, ["send_binary_req"])


if __name__ == "__main__":
    unittest.main()

"""Remote RPTR management: the command path and the Regions read (S453).

Two live defects on 2026-10-16, both reported by Filip against his own repeater
(SI-LJ-Trnovo II, `86a8eedb744b…`), both visible in gandalf's journal:

  * every `POST /api/mc/<radio>/remote/<node>/command` -> **500**,
    `WARNING [MC] remote command failed: string indices must be integers, not 'str'`.
    OM called `mc.commands.send_cmd(<hex string>, cmd)`; the library only skips
    `dst_type = dst["type"]` when `dst_type` is passed, so the call died before
    anything was sent. That took the WHOLE Manage panel with it — every Set
    button, every region preset, Home?/Default?. Reads were a different API and
    kept working (200).
  * the read's Regions section was useless: it was backed by the library's
    `req_regions_sync`, which waits on a tag the firmware never matches (the
    module's own docstring for the anon query says so), so the section came back
    empty/failed and there was no way to list a repeater's regions at all.

The fakes here reproduce the library's contract, so the tests fail on the old
code rather than passing for the wrong reason.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-remote-regions-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

import mesh_mc  # noqa: E402
import routes.mc as mc_routes  # noqa: E402
from config import CONFIG  # noqa: E402

FULL_KEY = "86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6"


class _Event:
    def __init__(self, type_, payload=None):
        self.type = type_
        self.payload = payload if payload is not None else {}


class _StrictCommands:
    """Mimics meshcore's MessagingCommands.send_cmd contract.

    `dst_type` is read off the destination when not given — on a hex string that
    is exactly the live TypeError.
    """

    def __init__(self):
        self.calls = []
        self.req_regions_calls = 0

    async def send_cmd(self, dst, cmd, timestamp=None, dst_type=None):
        if dst_type is None:
            _ = dst["type"]          # <- meshcore's line 75; raises on a str
        self.calls.append({"dst": dst, "cmd": cmd, "dst_type": dst_type})
        return _Event(mesh_mc.EventType.MSG_SENT, {"suggested_timeout": 4000})

    # the read's other sections
    async def req_basic_sync(self, contact, min_timeout=0):
        return _Event(mesh_mc.EventType.OK, {})

    async def req_owner_sync(self, contact, min_timeout=0):
        return _Event(mesh_mc.EventType.OK, {})

    async def req_status_sync(self, contact, min_timeout=0):
        return _Event(mesh_mc.EventType.OK, {})

    async def req_telemetry_sync(self, contact, min_timeout=0):
        return _Event(mesh_mc.EventType.OK, {})

    async def fetch_all_neighbours(self, contact, min_timeout=0):
        return _Event(mesh_mc.EventType.OK, {})

    async def req_acl_sync(self, contact, min_timeout=0):
        return _Event(mesh_mc.EventType.OK, {})

    async def req_regions_sync(self, contact, min_timeout=0):
        self.req_regions_calls += 1
        return _Event(mesh_mc.EventType.OK, {})


class _FakeMc:
    def __init__(self, commands):
        self.commands = commands
        self.dispatcher = None


class _RemoteTestCase(unittest.TestCase):
    def setUp(self):
        self._backup = json.loads(json.dumps(CONFIG))
        CONFIG.clear()
        CONFIG.update({"nodes": [], "mc_nodes": [], "silent_mode": False})
        self.commands = _StrictCommands()
        self.mc = _FakeMc(self.commands)
        self._contacts = {"mc1": {"contacts": {FULL_KEY: {"adv_name": "SI-LJ-Trnovo II", "type": 2}}}}
        patcher = mock.patch.object(mesh_mc, "_get_mc", return_value=(self.mc, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: (CONFIG.clear(), CONFIG.update(self._backup)))


class RemoteCommandDestinationTests(_RemoteTestCase):
    def _run_command(self, command):
        with mock.patch.dict(mesh_mc.mc_connections, self._contacts, clear=True):
            return asyncio.run(mesh_mc._remote_command_async("mc1", FULL_KEY[:12], command))

    def test_command_is_sent_with_the_destination_type(self):
        """Without dst_type the library raises TypeError and nothing is sent."""
        result = self._run_command("region home")
        self.assertEqual(len(self.commands.calls), 1, "the command must actually reach send_cmd")
        self.assertEqual(self.commands.calls[0]["cmd"], "region home")
        self.assertEqual(self.commands.calls[0]["dst_type"], 2, "a repeater contact reports type 2")

    def test_room_server_type_is_passed_through_not_forced_to_repeater(self):
        """The firmware parses the text differently per type, so use what the
        radio reported (room server = 3) instead of assuming repeater."""
        self._contacts["mc1"]["contacts"][FULL_KEY]["type"] = 3
        self._run_command("get name")
        self.assertEqual(self.commands.calls[0]["dst_type"], 3)

    def test_contact_without_a_type_falls_back_to_repeater(self):
        del self._contacts["mc1"]["contacts"][FULL_KEY]["type"]
        self._run_command("get name")
        self.assertEqual(self.commands.calls[0]["dst_type"], 2)

    def test_region_writes_are_still_allowlisted_and_reach_the_radio(self):
        for cmd in ("region put si", "region allowf si", "region default si",
                    "region home si", "region save"):
            self.commands.calls.clear()
            self._run_command(cmd)
            self.assertEqual(self.commands.calls[0]["cmd"], cmd, f"{cmd} must be allowed and sent")


class RegionsReadTests(_RemoteTestCase):
    def test_dedicated_read_returns_the_region_names(self):
        with mock.patch.dict(mesh_mc.mc_connections, self._contacts, clear=True), \
             mock.patch.object(mesh_mc, "_mc_query_responder_regions",
                               new=mock.AsyncMock(return_value=["si", "si-dol", "*"])):
            out = asyncio.run(mesh_mc._remote_regions_async("mc1", FULL_KEY[:12]))
        self.assertEqual(out["regions"], ["si", "si-dol", "*"])
        self.assertEqual(out["full_key"], FULL_KEY)

    def test_the_read_uses_the_binary_regions_query_not_req_regions_sync(self):
        """req_regions_sync waits on a tag the firmware never matches — the
        section was empty because of it."""
        with mock.patch.dict(mesh_mc.mc_connections, self._contacts, clear=True), \
             mock.patch.object(mesh_mc, "_mc_query_responder_regions",
                               new=mock.AsyncMock(return_value=["si"])) as query:
            result = asyncio.run(mesh_mc._remote_read_async("mc1", FULL_KEY[:12]))
        self.assertEqual(query.await_count, 1, "the read must use the proven 0-hop query")
        self.assertEqual(self.commands.req_regions_calls, 0, "req_regions_sync must not be used")
        self.assertTrue(result["regions"]["ok"])
        self.assertEqual(result["regions"]["data"], ["si"])

    def test_empty_answer_is_reported_as_no_regions_not_as_a_failure(self):
        with mock.patch.dict(mesh_mc.mc_connections, self._contacts, clear=True), \
             mock.patch.object(mesh_mc, "_mc_query_responder_regions",
                               new=mock.AsyncMock(return_value=[])):
            out = asyncio.run(mesh_mc._remote_regions_async("mc1", FULL_KEY[:12]))
        self.assertEqual(out["regions"], [])


class RegionsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(mc_routes.bp)
        self.client = self.app.test_client()

    def test_endpoint_returns_the_list(self):
        with mock.patch.object(mc_routes, "remote_repeater_regions",
                               return_value={"full_key": FULL_KEY, "regions": ["si", "*"]}):
            r = self.client.post(f"/api/mc/mcA/remote/{FULL_KEY}/regions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["regions"], ["si", "*"])
        self.assertTrue(r.get_json()["ok"])

    def test_unknown_contact_is_a_400(self):
        with mock.patch.object(mc_routes, "remote_repeater_regions",
                               side_effect=ValueError("Contact nope not found")):
            r = self.client.post("/api/mc/mcA/remote/nope/regions")
        self.assertEqual(r.status_code, 400)

    def test_silent_running_is_a_503_like_the_other_remote_endpoints(self):
        with mock.patch.object(mc_routes, "remote_repeater_regions",
                               side_effect=RuntimeError("Silent Running active: MC remote regions read blocked")):
            r = self.client.post(f"/api/mc/mcA/remote/{FULL_KEY}/regions")
        self.assertEqual(r.status_code, 503)


class RegionsPanelWiringTests(unittest.TestCase):
    """The panel is static JS, so the guard reads the source — the same approach
    the Scan/Probe label guards use."""

    def setUp(self):
        self.src = Path(_REPO, "static/js/app.js").read_text(encoding="utf-8")

    def test_regions_section_has_a_read_button(self):
        self.assertIn("onclick=\"mcRemoteReadRegions(this)\"", self.src,
                      "the Regions section needs its own read button")

    def test_read_button_calls_the_dedicated_endpoint(self):
        self.assertIn("/regions`, {", self.src)
        self.assertIn("function mcRemoteReadRegions", self.src)
        self.assertIn("_mcRemoteRenderRegionList(d.regions)", self.src)

    def test_read_result_renders_region_names_as_chips_not_json(self):
        self.assertIn("function _mcRemoteChipsHtml", self.src)
        self.assertIn("Array.isArray(block.data) && block.data.length", self.src,
                      "the read result's Regions block must render the list as chips")

    def test_region_list_has_its_own_output_element(self):
        self.assertIn('id="mc-remote-region-list"', self.src)


if __name__ == "__main__":
    unittest.main()

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
        self.forced_repeater_warning = False

    async def send_cmd(self, dst, cmd, timestamp=None, dst_type=None):
        """meshcore's MessagingCommands.send_cmd, branch for branch.

        dst_type given -> the library WARNS and forces REPEATER, ignoring the
        value (seen live in gandalf's journal when we passed dst_type=). A dict
        destination -> the contact's own type is used, no warning.
        """
        if dst_type is None:
            resolved = dst["type"]           # raises on a hex string: the live bug
        else:
            self.forced_repeater_warning = True
            resolved = 2
        self.calls.append({"dst": dst, "cmd": cmd, "dst_type": resolved,
                           "passed_dst_type": dst_type})
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

    def test_command_is_sent_with_the_contact_dict_destination(self):
        """A hex-string destination makes the library raise TypeError before
        sending; the fix passes the contact dict so dst["type"] resolves."""
        self._run_command("region home")
        self.assertEqual(len(self.commands.calls), 1, "the command must actually reach send_cmd")
        call = self.commands.calls[0]
        self.assertEqual(call["cmd"], "region home")
        self.assertIsInstance(call["dst"], dict, "the destination must be the contact dict")
        self.assertEqual(call["dst"]["public_key"], FULL_KEY)
        self.assertEqual(call["dst_type"], 2, "a repeater contact reports type 2")

    def test_no_dst_type_is_passed_which_would_force_repeater_and_warn(self):
        """The library's `dst_type=` else-branch warns and OVERRIDES the value
        with REPEATER (seen live in gandalf's journal), so we must not use it."""
        self._run_command("get name")
        self.assertIsNone(self.commands.calls[0]["passed_dst_type"])
        self.assertFalse(self.commands.forced_repeater_warning,
                         "passing dst_type= would make the library warn and assume repeater")

    def test_room_server_type_is_honoured_not_forced_to_repeater(self):
        """The firmware parses the text differently per type, so use what the
        radio reported (room server = 3) instead of assuming repeater."""
        self._contacts["mc1"]["contacts"][FULL_KEY]["type"] = 3
        self._run_command("get name")
        self.assertEqual(self.commands.calls[0]["dst_type"], 3)
        self.assertEqual(self.commands.calls[0]["dst"]["type"], 3)

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


class RegionsReadDiagnosticsTests(_RemoteTestCase):
    """An empty region list alone cannot say WHY it is empty.

    On the live repeater Filip pressed Read regions and got "No regions
    reported" while the RPTR demonstrably had regions, so the read now records
    what actually happened and ships it with the answer."""

    def _run_read(self, event_payload=None, timeout=0.3):
        calls = {}

        class _Sub:
            def unsubscribe(self):
                calls["unsubscribed"] = True

        class _Mc:
            def __init__(self):
                self.commands = self

            def subscribe(self, event_type, callback):
                calls["event_type"] = event_type
                if event_payload is not None:
                    callback(_Event(mesh_mc.EventType.BINARY_RESPONSE, event_payload))
                return _Sub()

            async def send_anon_req(self, contact, req_type, min_timeout=0):
                calls["sent"] = True
                return _Event(mesh_mc.EventType.MSG_SENT, {})

        mc = _Mc()
        with mock.patch.dict(mesh_mc.mc_connections, self._contacts, clear=True):
            names = asyncio.run(mesh_mc._mc_query_responder_regions("mc1", mc, FULL_KEY, timeout=timeout))
        self.assertEqual(calls["event_type"], mesh_mc.EventType.BINARY_RESPONSE)
        return names

    def test_a_reply_is_parsed_and_recorded(self):
        names = self._run_read({"data": b"si,si-dol\x00".hex()})
        self.assertEqual(names, ["si", "si-dol"])
        diag = mesh_mc._mc_regions_read_diag["mc1"]
        self.assertTrue(diag["got_reply"])
        self.assertFalse(diag["no_reply"])
        self.assertEqual(diag["names"], ["si", "si-dol"])
        self.assertEqual(diag["reply_bytes"], len(b"si,si-dol\x00"))

    def test_a_silent_repeater_is_recorded_as_no_reply(self):
        names = self._run_read(None)          # nothing ever arrives
        self.assertEqual(names, [])
        diag = mesh_mc._mc_regions_read_diag["mc1"]
        self.assertTrue(diag["no_reply"])
        self.assertFalse(diag["got_reply"])

    def test_an_unparseable_reply_is_distinguishable_from_no_regions(self):
        names = self._run_read({"data": b"\x01\x02\xff\xfe".hex()})
        self.assertEqual(names, [])
        diag = mesh_mc._mc_regions_read_diag["mc1"]
        self.assertTrue(diag["got_reply"], "a reply did arrive")
        self.assertEqual(diag["reply_bytes"], 4, "and its size is reported")
        self.assertEqual(diag["names"], [])

    def test_the_diagnosis_travels_with_the_answer(self):
        with mock.patch.dict(mesh_mc.mc_connections, self._contacts, clear=True), \
             mock.patch.object(mesh_mc, "_mc_query_responder_regions",
                               new=mock.AsyncMock(return_value=["si"])):
            out = asyncio.run(mesh_mc._remote_regions_async("mc1", FULL_KEY[:12]))
        self.assertIn("diag", out, "the panel needs the diagnosis to explain an empty list")
        self.assertIsInstance(out["diag"], dict)


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

    def test_bare_region_is_allowed_as_a_read_only_command(self):
        """`region` with no argument prints the configured regions: read-only, and
        it is the fallback when the fast 0-hop read gets no answer."""
        self.assertEqual(mesh_mc._validate_remote_admin_command("region"), "region")
        self.assertEqual(mesh_mc._validate_remote_admin_command("  REGION  "), "REGION")
        # and the allowlist must still refuse anything dangerous
        with self.assertRaises(ValueError):
            mesh_mc._validate_remote_admin_command("region erase")

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

    def test_section_is_grouped_read_then_preset_then_policy_then_advanced(self):
        """Filip, 2026-10-16: the section was cluttered and not intuitive. The
        layout must keep the questions in order: what the RPTR has, the common
        one-click preset, the network policy, then the rare per-region editing."""
        for label in ("Read regions", "Read home", "Read default",
                      "Slovenia preset", "Apply preset", "Make selected the default",
                      "Unscoped flood traffic", "One region at a time (advanced)",
                      "Add or update", "Save to RPTR"):
            self.assertIn(label, self.src, f"the Regions section must offer '{label}'")

    def test_per_region_editing_is_collapsed_by_default(self):
        self.assertIn('<summary style="cursor:pointer;font-size:12px;color:var(--muted)">One region at a time (advanced)</summary>',
                      self.src, "the advanced row must be a collapsed <details>")
        idx = self.src.index("One region at a time (advanced)")
        tail = self.src[idx:idx + 2000]
        self.assertIn('id="mc-remote-region-custom"', tail, "the region input belongs to the advanced group")
        self.assertIn('id="mc-remote-region-parent"', tail)

    def _regions_block(self):
        """Only the Regions part of the Manage panel — the app has other Save and
        Default buttons, so a whole-file check would be meaningless."""
        start = self.src.index(">Regions</h3>")
        end = self.src.index(">Channels</h3>", start)
        return self.src[start:end]

    def test_the_cryptic_labels_are_gone(self):
        """Put / Save / Home / Default / Allow * / Deny * and the bare ↻ told the
        user nothing. A guard so the section cannot drift back."""
        block = self._regions_block()
        for old in (">Put</button>", ">Save</button>", ">Home?</button>",
                    ">Default?</button>", ">Allow *</button>", ">Deny *</button>",
                    ">Apply SI preset</button>", ">Default selected</button>"):
            self.assertNotIn(old, block, f"{old} must not come back to the Regions section")

    def test_writes_that_only_existed_in_the_allowlist_are_now_reachable(self):
        """`region denyf` and `region remove` were allowed by the backend but had
        no button, so they could not be used at all."""
        self.assertIn("'custom-deny'", self.src)
        self.assertIn("region denyf ${custom}", self.src)
        self.assertIn("'custom-remove'", self.src)
        self.assertIn("region remove ${custom}", self.src)
        self.assertIn("window.confirm", self.src, "removing a region must ask first")

    def test_home_and_default_reads_show_what_they_found(self):
        self.assertIn("function mcRemoteReadRegionFact", self.src)
        self.assertIn("_mcRemoteRegionState.facts[kind]", self.src)
        self.assertIn("Default scope:", self.src)

    def test_reply_strip_is_pinned_to_the_bottom_of_the_panel(self):
        """His question was where it should go. The panel scrolls inside
        #action-modal .modal-body, so a sticky last child stays visible while
        scrolling instead of scrolling away with the section."""
        self.assertIn('id="mc-remote-reply"', self.src)
        self.assertIn("position:sticky;bottom:0", self.src)
        self.assertIn("${_mcRemoteReplyStripHtml()}", self.src)

    def test_command_replies_and_errors_are_reported(self):
        self.assertIn("function mcRemoteReport", self.src)
        self.assertIn("mcRemoteReport(d.command || command, answer", self.src)
        self.assertIn("mcRemoteReport(command, e.message, 'error')", self.src)

    def test_replies_linger_and_can_be_cleared(self):
        self.assertIn("_MC_REMOTE_REPLY_MAX = 4", self.src)
        self.assertIn('onclick="mcRemoteClearReplies()"', self.src)
        self.assertIn("function mcRemoteClearReplies", self.src)

    def test_field_refreshes_stay_out_of_the_reply_strip(self):
        """A value that fills its own field would just be noise here."""
        self.assertIn("{silentError: true, report: false}", self.src)

    def test_regions_read_reports_why_it_is_empty(self):
        for phrase in ("the RPTR did not answer the region request",
                       "no region name could be read from it",
                       "request could not be sent"):
            self.assertIn(phrase, self.src, "the strip must explain an empty region list")
        self.assertIn("mcRemoteReport('regions (read)', answer", self.src)
        self.assertIn("mcRemoteReport('regions (read)', e.message, 'error')", self.src)

    def test_regions_read_falls_back_to_the_rptr_own_region_command(self):
        """The 0-hop request can go unanswered even when the RPTR has regions, so
        the read asks the RPTR's own CLI as well and shows its words verbatim
        (no parser for a format we have not seen yet)."""
        self.assertIn("mcRemoteRunCommand('region', 'mc-remote-region-list'", self.src)
        self.assertIn("no answer to the region command either", self.src)
        self.assertIn("output is in the reply strip at the bottom", self.src)

    def test_region_preset_transcript_no_longer_dumps_into_the_section(self):
        self.assertNotIn("done.join(", self.src, "the transcript belongs in the reply strip now")
        self.assertIn("replies below.", self.src)


if __name__ == "__main__":
    unittest.main()

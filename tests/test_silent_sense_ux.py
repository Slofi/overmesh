"""Silent Running must EXPLAIN itself on the MT side too (S453).

Filip, 2026-10-16, with Silent Running on: *"on MT side, the buttons for 'Sense Mesh' and 'Active' are disabled in
silent mode, but there is no explanation why, when clicked; like on the MC side, where there is a pop-up … so pls
copy the pop-up to MT side too."*

The buttons were not actually disabled — the handlers did `if (!r.ok) throw new Error(r.status)` and the final
`.catch(e => console.error(…))` dropped the server's message into the console. A 409 whose body nobody reads is
indistinguishable from a broken button, which is the same class of defect as the inert test guard: the work was
done (the server refused correctly) and the visible half was missing.

This file pins both halves:
  * the server semantics — the two transmitting paths refuse in Silent Running and say why, the receive-only one
    is never gated (a monitor that goes deaf is a worse failure than one that talks), and switching Active OFF is
    always allowed;
  * the front end — all three handlers surface the server's own wording.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-silent-sense-")
_CONFIG_PATH = Path(_TEST_DIR.name) / "config.json"
_DATA_DIR = Path(_TEST_DIR.name) / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from flask import Flask  # noqa: E402

import routes.sense as sense_routes  # noqa: E402


class SilentSenseServerTests(unittest.TestCase):
    """What the server does while Silent Running is on."""

    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(sense_routes.bp)
        self.client = app.test_client()
        patcher = mock.patch.dict(sense_routes.CONFIG, {"silent_mode": True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._was_active = sense_routes._sense_state["active_auto"]
        self.addCleanup(self._restore)

    def _restore(self):
        sense_routes._sense_state["active_auto"] = self._was_active

    def test_the_sense_broadcast_is_refused_with_a_reason(self):
        r = self.client.post("/api/mesh/sense")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Silent Running active", r.get_json()["error"],
                      "the refusal must carry a message the UI can show")

    def test_active_scanning_cannot_be_switched_on_while_silent(self):
        sense_routes._sense_state["active_auto"] = False
        r = self.client.post("/api/mesh/sense/active_auto")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Silent Running active", r.get_json()["error"])

    def test_active_scanning_can_always_be_switched_off_while_silent(self):
        """Turning it off must never be blocked — otherwise Silent Running would
        trap a radio in the mode that keeps broadcasting."""
        sense_routes._sense_state["active_auto"] = True
        r = self.client.post("/api/mesh/sense/active_auto")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["active_auto"])

    def test_passive_sensing_is_never_gated_by_silent_running(self):
        """Passive capture transmits nothing, so gating it would leave the operator
        in monitor-only mode with nothing to monitor."""
        src = (REPO / "routes/sense.py").read_text(encoding="utf-8")
        body = src[src.index("def api_sense_passive"):]
        body = body[:body.index("@bp.route")]
        self.assertNotIn("silent_mode", body,
                         "passive (receive-only) sensing must stay available while silent")


class MtSenseUxTests(unittest.TestCase):
    """The refusal has to be visible, in the server's own words."""

    def setUp(self):
        self.src = (REPO / "static/js/app.js").read_text(encoding="utf-8")

    def _handler(self, name: str) -> str:
        start = self.src.index("function %s(" % name)
        return self.src[start:start + 2600]

    def test_every_sense_handler_shows_the_servers_message(self):
        for name in ("triggerSense", "toggleSensePassive", "toggleSenseActive"):
            with self.subTest(handler=name):
                body = self._handler(name)
                self.assertIn("showAlert(", body, f"{name} must surface the refusal in a pop-up")
                self.assertIn("d.error", body, f"{name} must use the server's own wording")

    def test_the_pop_up_is_the_same_one_the_mc_side_uses(self):
        """Filip asked for the MC pop-up on the MT side: same helper, so the wording
        comes from the server rather than being re-typed per button."""
        mc = self._handler("startMcScan")
        self.assertIn("showAlert(d.error", mc, "the MC side's pattern is showAlert(d.error || …)")
        for name in ("triggerSense", "toggleSenseActive"):
            self.assertIn("showAlert(d.error", self._handler(name))

    def test_no_sense_handler_throws_the_status_into_the_console(self):
        for name in ("toggleSensePassive", "toggleSenseActive"):
            with self.subTest(handler=name):
                self.assertNotIn(
                    "if (!r.ok) throw new Error(r.status)", self._handler(name),
                    "a thrown status ends up in the console and nowhere else — the button then looks dead")


if __name__ == "__main__":
    unittest.main()

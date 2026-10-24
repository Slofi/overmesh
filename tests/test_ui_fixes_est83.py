"""EST-83 client-side guards.

The app has no browser test harness, so the contract these fixes are about
lives in the source strings/wiring of static/js/app.js and the two templates.
Each guard below reads the *real* source and asserts the exact wiring the fix
introduced. They are deliberately narrow: a guard that does not pin the
failure mode is not a guard (see the EST-83 report / the team's "a check that
can't fail isn't a check" rule).

Red-first: every method here was run against the pre-fix source and failed,
then passed after the fix, in the same commit.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static/js/app.js"
INDEX = ROOT / "templates/index.html"

# The url_map test imports the Flask app, which reads config at import time.
# Use a throwaway config/data dir exactly like the other route tests do.
_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-est83-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
(_TEST_ROOT / "data").mkdir(parents=True, exist_ok=True)
(_TEST_ROOT / "config.json").write_text(
    json.dumps({"nodes": [], "mc_nodes": [], "app": {}, "gps": {}, "silent_mode": False}),
    encoding="utf-8",
)
os.environ.setdefault("OVERMESH_CONFIG", str(_TEST_ROOT / "config.json"))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_TEST_ROOT / "data"))
sys.path.insert(0, str(ROOT))


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    """Return the braces-balanced body of `function name(...)`.

    Only used on small handlers; the corpus is ordinary JS (balanced braces in
    template-literal ${} interpolations included), so a depth scan is enough.
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"function {name}() not found")
    start = src.index("{", m.end())
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


class TestRequestFailureFeedback(unittest.TestCase):
    """B5/B9/B10/B11: a failed request must be visible and must not lose input."""

    def setUp(self):
        self.src = _js()

    def test_b11_chat_send_shows_json_error_not_raw_body(self):
        body = _fn_body(self.src, "sendChat")
        self.assertNotIn("throw new Error(await r.text())", body,
                         "B11: the raw response body must not be shown as the alert")
        self.assertRegex(body, r"\.json\(\)", "B11: the error body must be parsed as JSON")
        self.assertRegex(body, r"\.error\b", "B11: the parsed error text must be used")

    def test_b10_map_dm_restores_input_and_alerts_on_failure(self):
        body = _fn_body(self.src, "sendMapDM")
        self.assertIn("input.value = text", body,
                      "B10: a failed map-DM send must restore the typed text")
        self.assertIn("showAlert", body, "B10: a failed map-DM send must alert")

    def test_b9_mc_ignore_checks_ok_and_alerts(self):
        body = _fn_body(self.src, "ignoreMcContact")
        self.assertRegex(body, r"r\.ok", "B9: ignoreMcContact must check r.ok before mutating")
        self.assertIn("showAlert", body, "B9: a failed ignore must alert")

    def test_b9_mt_ignore_checks_ok_and_alerts(self):
        body = _fn_body(self.src, "ignoreNode")
        self.assertRegex(body, r"r\.ok", "B9: ignoreNode must check r.ok")
        self.assertIn("showAlert", body, "B9: a failed ignore must alert")

    def test_b5_sense_reads_429_json_and_starts_cooldown(self):
        body = _fn_body(self.src, "triggerSense")
        self.assertRegex(body, r"r\.status\s*===\s*429",
                         "B5: a 429 (server-side cooldown) must be read, not thrown")
        self.assertIn("senseCooldownTick(d.cooldown_remaining)", body,
                      "B5: the cooldown message + ticker must run on 429")


class TestLabelFixes(unittest.TestCase):
    """C1/C2/C4/C6/H4: one term, one accurate tooltip, named close controls,
    state in the Silent button, and a visible Debug Events status."""

    def test_c1_zero_precision_is_called_exact_position_everywhere(self):
        src = _js()
        html = _html()
        self.assertEqual(src.count("'Actual position'"), 0,
                         "C1: precMeterLabel must use the single term 'Exact position'")
        self.assertEqual(html.count(">Actual position<"), 0,
                         "C1: the MC precision default must match the other two sliders")
        m = re.search(r"function precMeterLabel\(m\)\s*\{[^}]*\}", src)
        self.assertIsNotNone(m)
        self.assertIn("'Exact position'", m.group(0))

    def test_c2_lora_save_title_names_tx_power(self):
        self.assertIn(
            'title="Save LoRa region, modem preset, TX power, hop limit and the MQTT flags to the node"',
            _html(), "C2: the Save tooltip must name TX power and the MQTT flags, which it also writes")

    def test_c4_four_close_controls_have_accessible_name(self):
        html = _html()
        for call in ("closeNodeCleanupModal()", "closePolarCustomize()",
                     "closeMapDM()", "_closeDetailPanel()"):
            # The tag carrying the close handler must name itself for SR/keyboard users.
            m = re.search(r"<[^<>]*onclick=\"" + re.escape(call) + r"\"[^<>]*>", html)
            self.assertIsNotNone(m, f"C4: {call} control not found")
            tag = m.group(0)
            self.assertIn('title="Close"', tag, f"C4: {call} needs title=\"Close\"")
            self.assertIn('aria-label="Close"', tag, f"C4: {call} needs aria-label=\"Close\"")

    def test_c6_silent_button_text_shows_state(self):
        body = _fn_body(_js(), "_updateSilentUi")
        self.assertIn("btn.textContent", body,
                      "C6: the Silent button must show its state in text")
        self.assertRegex(body, r"Silent ON", "C6: active state must read 'Silent ON'")

    def test_h4_debug_events_shows_status_and_clears(self):
        body = _fn_body(_js(), "startMcDebugEvents")
        self.assertIn("Logging serial events for 60 s… (see the server log)", body,
                      "H4: the MC actions status must explain what is happening")


class TestDeadControlRemoved(unittest.TestCase):
    """C3: the 'RC collector obs count' alert toggle had no producer, so it could
    never light the Alert Panel. Removed from the UI + prefs wiring; a stored
    `alert_log_rc_obs` key in config stays harmless (the loader no longer reads it)."""

    def test_rc_obs_toggle_is_gone_from_ui_and_prefs(self):
        self.assertEqual(_html().count("alert-log-rc-obs"), 0,
                         "C3: the dead RC-obs toggle must be removed from Settings")
        self.assertEqual(_js().count("alert_log_rc_obs"), 0,
                         "C3: saveAlertLogPrefs/loadAlertLogPrefs must not carry the dead key")


class TestCrossSystemRemoved(unittest.TestCase):
    """H1: the parked MT↔MC Cross-system feature is gone, not just hidden.

    It was never enabled (CROSS_SYSTEM_ENABLED = false), so every code path it
    owned was unreachable: the hidden settings tab, the /mt //mc chat command,
    the rule routes, cross.py and its forward hooks. A stored `cross` config key
    is still harmless — nothing reads it any more.
    """

    def test_backend_is_gone(self):
        self.assertFalse((ROOT / "cross.py").exists(), "H1: cross.py must be deleted")
        self.assertFalse((ROOT / "tests/test_cross.py").exists(),
                         "H1: the cross.py-only test must go with it")
        settings = (ROOT / "routes/settings.py").read_text(encoding="utf-8")
        self.assertEqual(settings.count("api_settings_cross"), 0,
                         "H1: /api/settings/cross routes must be removed")
        self.assertEqual(settings.count("from cross import"), 0,
                         "H1: routes/settings must not import cross")
        for rel in ("mesh.py", "mesh_mc.py", "routes/chat.py", "routes/mc.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(text.count("maybe_forward_m"), 0,
                             f"H1: {rel} must not call the cross forward hook")

    def test_client_is_gone(self):
        src = _js()
        html = _html()
        for needle in ("CROSS_SYSTEM_ENABLED", "stab-cross", "st-cross",
                       "_crossSettings", "handleCrossCommand", "/api/settings/cross"):
            self.assertEqual(src.count(needle), 0, f"H1: app.js still mentions {needle}")
        for needle in ("stab-cross", 'id="st-cross"'):
            self.assertEqual(html.count(needle), 0, f"H1: index.html still mentions {needle}")

    def test_url_map_has_no_cross_rule(self):
        import app as app_module
        rules = {r.rule for r in app_module.app.url_map.iter_rules()}
        self.assertNotIn("/api/settings/cross", rules)

    def test_docs_say_it_was_removed(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Cross-system forwarding was removed", readme,
                      "H1: the docs must say the parked feature was removed in 2026-10")
        self.assertEqual(readme.count("cross.py"), 0,
                         "H1: the project-structure table must not list a deleted file")


class TestUnreferencedLibsRemoved(unittest.TestCase):
    """H3: two vendored assets looked load-bearing but were referenced nowhere.

    The offline tile layer uses raw indexedDB (app.js), not idb.min.js, and
    nothing created a Leaflet.Offline layer. A grep across source, templates,
    tests and docs found only the files' own self-references, so they are gone.
    """

    GONE = ("static/idb.min.js", "static/leaflet.offline.min.js")

    def test_files_are_deleted(self):
        for rel in self.GONE:
            self.assertFalse((ROOT / rel).exists(), f"H3: {rel} must be deleted")

    def test_no_source_still_loads_them(self):
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in (".git", "node_modules") for part in path.parts):
                continue
            if path == Path(__file__).resolve():     # this guard names them on purpose
                continue
            if path.suffix not in (".py", ".js", ".html", ".md", ".json", ".css", ".sh", ".mjs"):
                continue
            if path.name == "package-lock.json":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for rel in self.GONE:
                if rel in text or rel.split("/")[-1] in text:
                    offenders.append(f"{path.relative_to(ROOT)} -> {rel}")
        self.assertEqual(offenders, [], "H3: something still references a deleted asset")


class TestWaypointDialog(unittest.TestCase):
    """Q4: the Map marker dialog is network-aware and can also write a Log entry.

    MeshCore has no waypoint packet type, so the Mark (MT waypoint) tab must not
    be offered on an MC-only map; the Self Note tab then carries an optional
    "Also add to Log" that POSTs the note name/description/coordinates to
    /api/toc (the same fields tocSubmit uses).
    """

    def setUp(self):
        self.src = _js()

    def test_mark_tab_is_labelled_as_mt_waypoint(self):
        self.assertTrue("📡 Mark (MT waypoint)" in self.src,
                        "Q4: the Mark tab must be labelled as the MT waypoint tab")
        self.assertTrue("Sent as a Meshtastic waypoint. MeshCore has no waypoint message." in self.src,
                        "Q4: the Mark tab must explain why it is MT-only")

    def test_dialog_detects_the_map_network(self):
        body = _fn_body(self.src, "openWaypointModal")
        self.assertIn("mapShowMt", body, "Q4: dialog must read the map MT/MC toggle")
        self.assertIn("mapShowMc", body, "Q4: dialog must read the map MT/MC toggle")
        self.assertRegex(body, r"showMarkTab", "Q4: the dialog must decide whether to show the Mark tab")
        self.assertRegex(body, r"mcHide", "Q4: an MC map that is force-hidden must count as MT-visible")

    def test_self_note_has_also_add_to_log(self):
        self.assertTrue("Also add to Log" in self.src, "Q4: the Self Note tab needs the checkbox")
        self.assertTrue("note-also-log" in self.src, "Q4: the checkbox must be readable from saveMapNote")
        body = _fn_body(self.src, "saveMapNote")
        self.assertIn("/api/toc", body, "Q4: checking the box must POST a Log (TOC) entry")
        self.assertIn("category", body, "Q4: the TOC entry must set a category like tocSubmit")
        self.assertRegex(body, r"Name:", "Q4: the Log entry must carry the note name")
        self.assertRegex(body, r"Coordinates:", "Q4: the Log entry must carry the coordinates")


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-updcheck-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import routes.settings as settings  # noqa: E402


def _info(available, behind=1, commit="aaaaaaa", remote="bbbbbbb", fetch_ok=True, err=None):
    return {"update_available": available, "behind": behind, "commit": commit,
            "remote_commit": remote, "version": "2026.09.11.5",
            "fetch_ok": fetch_ok, "fetch_error": err}


class UpdateCheckTests(unittest.TestCase):
    """The boot/periodic update check must be quiet, cached, and never fatal."""

    def setUp(self):
        settings._UPDATE_CHECK_STATE.update({
            "checked_at": None, "available": False, "remote_commit": None,
            "local_commit": None, "behind": 0, "version": None, "error": None})
        settings._UPDATE_STATE.update({"running": False})
        self.pushes = []
        p = mock.patch.object(settings, "push_to_sse", side_effect=lambda m: self.pushes.append(json.loads(m)))
        p.start()
        self.addCleanup(p.stop)

    def test_detects_available_update_and_pushes_once(self):
        with mock.patch.object(settings, "_git_info", return_value=_info(True, behind=3)):
            state = settings.check_for_update()
        self.assertTrue(state["available"])
        self.assertEqual(state["behind"], 3)
        self.assertEqual(state["remote_commit"], "bbbbbbb")
        self.assertEqual(len(self.pushes), 1, "must announce the flip")
        self.assertEqual(self.pushes[0]["type"], "update_available")
        self.assertEqual(self.pushes[0]["behind"], 3)
        # a second check with the same answer must NOT spam
        with mock.patch.object(settings, "_git_info", return_value=_info(True, behind=3)):
            settings.check_for_update()
        self.assertEqual(len(self.pushes), 1, "only the transition is pushed")

    def test_up_to_date_reports_not_available(self):
        with mock.patch.object(settings, "_git_info", return_value=_info(False, behind=0)):
            state = settings.check_for_update()
        self.assertFalse(state["available"])
        self.assertEqual(state["behind"], 0)
        self.assertEqual(self.pushes, [])

    def test_skips_while_update_job_running(self):
        settings._UPDATE_STATE["running"] = True
        with mock.patch.object(settings, "_git_info") as git_info:
            state = settings.check_for_update()
        git_info.assert_not_called()
        self.assertFalse(state["available"])

    def test_fetch_failure_is_recorded_not_raised(self):
        with mock.patch.object(settings, "_git_info", return_value=_info(False, fetch_ok=False, err="timed out")):
            state = settings.check_for_update()
        self.assertFalse(state["available"])
        self.assertIn("timed out", state["error"])

    def test_git_info_exception_is_swallowed(self):
        with mock.patch.object(settings, "_git_info", side_effect=RuntimeError("boom")):
            state = settings.check_for_update()
        self.assertFalse(state["available"])
        self.assertIn("boom", state["error"])

    def test_stale_cache_triggers_a_background_refresh(self):
        # A page load must stay instant, but an old answer must not stay stale: the
        # endpoint kicks a thread instead of fetching inline.
        settings._UPDATE_CHECK_STATE["checked_at"] = int(__import__('time').time()) - 3600
        started = []
        with mock.patch.object(settings.threading, "Thread", side_effect=lambda *a, **k: mock.Mock(start=lambda: started.append(k.get('target')))) as T,              mock.patch.object(settings, "check_for_update") as chk:
            app = Flask(__name__); app.register_blueprint(settings.bp)
            r = app.test_client().get("/api/settings/update/available")
        self.assertEqual(r.status_code, 200)
        chk.assert_not_called()                      # never inline
        self.assertEqual(len(started), 1, "must start exactly one refresh thread")
        self.assertIs(started[0], chk)               # the thread runs the check

    def test_fresh_cache_does_not_refresh(self):
        settings._UPDATE_CHECK_STATE["checked_at"] = int(__import__('time').time())
        with mock.patch.object(settings.threading, "Thread") as T:
            app = Flask(__name__); app.register_blueprint(settings.bp)
            r = app.test_client().get("/api/settings/update/available")
        T.assert_not_called()

    def test_endpoint_is_local_only_like_its_siblings(self):
        # update/status and update/run are 403 to non-local callers; this endpoint can
        # trigger a fetch, so it follows the same policy.
        app = Flask(__name__); app.register_blueprint(settings.bp)
        with mock.patch.object(settings, "_settings_local_request", return_value=False),              mock.patch.object(settings, "_refresh_update_check_if_stale") as refresh:
            r = app.test_client().get("/api/settings/update/available")
        self.assertEqual(r.status_code, 403)
        refresh.assert_not_called()

    def test_concurrent_requests_start_only_one_refresh(self):
        # Real threads (patching settings.threading.Thread would patch the shared
        # module and fake the test's own threads too). The fake check blocks, so the
        # refresh thread stays alive for the duration — exactly the window in which a
        # second caller must NOT start another refresh.
        import threading as _t
        settings._UPDATE_CHECK_STATE["checked_at"] = 0        # stale
        calls, entered, release = [], _t.Event(), _t.Event()

        def fake_check(*a, **k):
            calls.append(1)
            entered.set()
            release.wait(3)

        barrier = _t.Barrier(5)
        with mock.patch.object(settings, "check_for_update", side_effect=fake_check):
            def call():
                barrier.wait()
                settings._refresh_update_check_if_stale()
            callers = [_t.Thread(target=call) for _ in range(5)]
            for t in callers: t.start()
            for t in callers: t.join()
            entered.wait(2)
        self.assertEqual(len(calls), 1, f"must start exactly one refresh (got {len(calls)})")
        release.set()

    def test_check_logs_its_outcome(self):
        # The loop is otherwise invisible in the log; 'did it check?' must be answerable.
        with self.assertLogs("routes.settings", level="INFO") as cm:
            with mock.patch.object(settings, "_git_info", return_value=_info(True, behind=2)):
                settings.check_for_update()
        self.assertTrue(any("2 commit(s) behind" in m for m in cm.output), cm.output)
        with self.assertLogs("routes.settings", level="INFO") as cm:
            with mock.patch.object(settings, "_git_info", return_value=_info(False, behind=0)):
                settings.check_for_update()
        self.assertTrue(any("up to date" in m for m in cm.output), cm.output)

    def test_endpoint_serves_cached_state_without_fetching_inline(self):
        # The request itself never fetches (a page load must stay instant). When the
        # cache is stale the endpoint only *starts a thread* — covered separately above.
        settings._UPDATE_CHECK_STATE["checked_at"] = int(__import__('time').time())
        app = Flask(__name__)
        app.register_blueprint(settings.bp)
        with mock.patch.object(settings, "_git_info") as git_info:
            r = app.test_client().get("/api/settings/update/available")
        git_info.assert_not_called()
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("available", body)
        self.assertIn("remote_commit", body)

    def test_completed_update_clears_availability(self):
        settings._UPDATE_CHECK_STATE.update({"available": True, "behind": 2, "remote_commit": "bbbbbbb"})
        src = open("/home/slofi/overmesh/routes/settings.py", encoding="utf-8").read()
        # the success path of the update job must drop the notice
        self.assertIn('_UPDATE_CHECK_STATE.update({\n            "checked_at": int(time.time()),\n            "available": False,', src)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest import mock

import routes.settings as settings


class UpdateDirtySignalTests(unittest.TestCase):
    """'local changes present' must mean tracked edits.

    It used to count untracked files too, so stale config backups
    (config.json.bak-prehost) made the update panel claim local changes on a
    clean checkout (Gandalf, 2026-09-10)."""

    def _run_git_info(self, porcelain_out, rc=0):
        calls = []

        def fake_git_cmd(args, timeout=10):
            calls.append(args)
            if args[:1] == ["status"]:
                return rc, porcelain_out, ""
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "03ed6f6", ""
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, "03ed6f6542d4974eb68ae6033e90a9dd0ae294e0", ""
            if args[:2] == ["config", "--get"]:
                return 0, "git@github.com:Slofi/overmesh.git", ""
            return 0, "03ed6f6", ""

        with mock.patch.object(settings, "_git_cmd", side_effect=fake_git_cmd):
            info = settings._git_info(fetch=False)
        return info, calls

    def test_status_ignores_untracked_files(self):
        _info, calls = self._run_git_info("")
        status_calls = [c for c in calls if c[:1] == ["status"]]
        self.assertEqual(len(status_calls), 1)
        self.assertIn("--untracked-files=no", status_calls[0],
                      "untracked files must not be counted as local changes")

    def test_untracked_only_checkout_is_clean(self):
        # git already honours -uno, so an untracked-only tree yields no lines
        info, _ = self._run_git_info("")
        self.assertFalse(info["dirty"])
        self.assertEqual(info["dirty_summary"], [])

    def test_tracked_modification_is_dirty_and_summarised(self):
        info, _ = self._run_git_info(" M mesh_mc.py\n")
        self.assertTrue(info["dirty"])
        self.assertEqual(info["dirty_summary"], [" M mesh_mc.py"])

    def test_filter_still_hides_secret_key(self):
        self.assertEqual(settings._filter_update_status_lines("?? secret.key"), [])
        self.assertEqual(settings._filter_update_status_lines(" M app.py"), [" M app.py"])

    def test_git_failure_is_reported_dirty(self):
        info, _ = self._run_git_info("", rc=128)
        self.assertTrue(info["dirty"])


if __name__ == "__main__":
    unittest.main()

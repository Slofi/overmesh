import subprocess
import unittest
from unittest import mock

import routes.settings as settings


class GitTimeoutHandlingTests(unittest.TestCase):
    """A stalled git call must not abort the update with a raw exception.

    CD's in-app update failed with:
      Error: Command '['git','fetch','--prune','origin']' timed out after 45 seconds
    because subprocess.TimeoutExpired escaped _git_cmd."""

    def test_git_cmd_converts_timeout_to_failure(self):
        with mock.patch("routes.settings.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=90)):
            rc, out, err = settings._git_cmd(["fetch", "--prune", "origin"], timeout=90)
        self.assertEqual(rc, 124)
        self.assertEqual(out, "")
        self.assertIn("timed out after 90s", err)

    def test_git_cmd_check_still_raises_on_timeout(self):
        with mock.patch("routes.settings.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd=["git", "reset"], timeout=60)):
            with self.assertRaises(RuntimeError):
                settings._git_cmd(["reset", "--hard"], timeout=60, check=True)


class FetchRetryTests(unittest.TestCase):
    """One transient stall used to end the whole update: fetch now gets a longer
    timeout and one retry."""

    def _git_info_with(self, fetch_results):
        calls = []

        def fake_git_cmd(args, timeout=10, check=False):
            if args[:1] == ["fetch"]:
                calls.append(timeout)
                return fetch_results[min(len(calls) - 1, len(fetch_results) - 1)]
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, "abc1234", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "abc1234", ""
            if args[:2] == ["config", "--get"]:
                return 0, "git@github.com:Slofi/overmesh.git", ""
            return 0, "abc1234", ""

        with mock.patch.object(settings, "_git_cmd", side_effect=fake_git_cmd), \
             mock.patch("routes.settings.time.sleep"):
            info = settings._git_info(fetch=True)
        return info, calls

    def test_fetch_failure_then_success(self):
        info, calls = self._git_info_with([(124, "", "timed out after 90s"), (0, "", "")])
        self.assertTrue(info.get("fetch_ok"))
        self.assertEqual(len(calls), 2, "must retry once")
        self.assertTrue(all(t == 90 for t in calls), "retry must use the longer timeout")

    def test_fetch_failing_twice_reports_error(self):
        info, calls = self._git_info_with([(124, "", "timed out after 90s")])
        self.assertFalse(info.get("fetch_ok"))
        self.assertIn("timed out after 90s", info.get("fetch_error", ""))
        self.assertEqual(len(calls), 2)

    def test_fetch_success_first_try_does_not_retry(self):
        info, calls = self._git_info_with([(0, "", "")])
        self.assertTrue(info.get("fetch_ok"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

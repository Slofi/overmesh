"""Integration check for the update notice: real git, real refs, no mocks of git.

Builds a throwaway repo where refs/remotes/origin/main is genuinely ahead of HEAD
and runs the real check_for_update() against it. This is the case the notice exists
for — a mocked test alone would not prove the ref comparison works.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-updint-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import routes.settings as settings  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


class UpdateCheckIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="om-repo-")
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "Test")
        Path(self.repo, "VERSION").write_text("2026.09.11.5\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "local")
        self.local = _git(self.repo, "rev-parse", "--short", "HEAD")

        # A commit that exists only as "upstream"
        Path(self.repo, "newer.txt").write_text("newer\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "upstream work")
        self.remote = _git(self.repo, "rev-parse", "--short", "HEAD")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.remote)
        # Rewind local HEAD: now HEAD is 1 behind origin/main, exactly like a pending release
        _git(self.repo, "reset", "-q", "--hard", "HEAD~1")

        self.pushes = []
        p = mock.patch.object(settings, "push_to_sse", side_effect=lambda m: self.pushes.append(json.loads(m)))
        p.start(); self.addCleanup(p.stop)
        p2 = mock.patch.object(settings, "BASE_DIR", self.repo)
        p2.start(); self.addCleanup(p2.stop)
        settings._UPDATE_CHECK_STATE.update({"checked_at": None, "available": False, "remote_commit": None,
                                             "local_commit": None, "behind": 0, "version": None, "error": None})
        settings._UPDATE_STATE.update({"running": False})

    def test_behind_repo_reports_update_available(self):
        state = settings.check_for_update()
        self.assertTrue(state["available"], f"expected available: {state}")
        self.assertEqual(state["behind"], 1)
        self.assertEqual(state["local_commit"], self.local)
        self.assertEqual(state["remote_commit"], self.remote)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0], {"type": "update_available",
                                          "remote_commit": self.remote, "behind": 1})

    def test_in_sync_repo_reports_nothing(self):
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.local)
        state = settings.check_for_update()
        self.assertFalse(state["available"], f"expected in sync: {state}")
        self.assertEqual(state["behind"], 0)
        self.assertEqual(self.pushes, [])


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-ignored-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import routes.mc as mc_routes  # noqa: E402
import routes.nodes as mt_routes  # noqa: E402
from state import connections, connections_lock  # noqa: E402


class _FakeIface:
    def __init__(self):
        self.nodes = {"!abc": {"num": 111}}
        self.nodesByNum = {111: {"num": 111}}

        class _LN:
            def __init__(self, outer):
                self.outer = outer

            def removeNode(self, nid):
                self.outer.radio_removed.append(nid)
        self.radio_removed = []
        self.localNode = _LN(self)


class MtIgnoredKeepsOmRowTests(unittest.TestCase):
    """Filip 2026-09-11: ignored/muted nodes ARE cleared from the radio, but their
    OM row is kept so they stay ignored."""

    def _setup(self, ignored_ids):
        iface = _FakeIface()
        with connections_lock:
            connections["mt1"] = {"iface": iface, "config": {"name": "EDC-2"}}
        self.addCleanup(lambda: connections.pop("mt1", None))

        executed = []

        class _Conn:
            def execute(self, sql, params=None):
                executed.append((sql.strip(), params))
        class _Ctx:
            def __enter__(self_inner):
                return _Conn()
            def __exit__(self_inner, *a):
                return False

        patchers = [
            mock.patch.object(mt_routes, "get_prefs_db", return_value=_Ctx()),
            mock.patch.object(mt_routes, "get_ignored", return_value=ignored_ids),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return iface, executed

    def test_ignored_node_leaves_radio_but_keeps_om_row(self):
        iface, executed = self._setup({("!abc", "mt1")})
        ok = mt_routes._mt_auto_evict_node("mt1", "!abc", keep_om_row=True)
        self.assertTrue(ok, "the radio removal must still happen")
        self.assertEqual(iface.radio_removed, ["!abc"])
        self.assertNotIn("!abc", iface.nodes)
        self.assertEqual([e for e in executed if e[0].startswith("DELETE")], [],
                         "the OM row must survive so the ignore flag persists")

    def test_non_ignored_node_is_deleted_from_om_too(self):
        iface, executed = self._setup(set())
        mt_routes._mt_auto_evict_node("mt1", "!abc", keep_om_row=False)
        self.assertEqual(iface.radio_removed, ["!abc"])
        deletes = [e for e in executed if e[0].startswith("DELETE")]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1], ("!abc", "mt1"))

    def test_hygiene_pass_sets_keep_row_for_ignored(self):
        iface, _ = self._setup({("!abc", "mt1")})
        captured = []

        def fake_evict(rid, nid, keep_om_row=False):
            captured.append((rid, nid, keep_om_row))
            return True

        with mock.patch.object(mt_routes, "_collect_stale_nodes",
                               return_value=[{"id": "!abc", "radio_id": "mt1"},
                                             {"id": "!zzz", "radio_id": "mt1"}]), \
             mock.patch.object(mt_routes, "_mt_auto_evict_node", side_effect=fake_evict), \
             mock.patch.object(mt_routes, "CONFIG", {**mt_routes.CONFIG, "nodes": [
                 {"id": "mt1", "auto_cleanup": True, "auto_cleanup_days": 30}]}):
            mt_routes.run_mt_auto_cleanup_once()
        self.assertIn(("mt1", "!abc", True), captured, "ignored node must keep its OM row")
        self.assertIn(("mt1", "!zzz", False), captured)


class McCleanupIgnoredTests(unittest.TestCase):
    """MC side: an ignored contact is removed with scope='radio' (device only)."""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(mc_routes.bp)
        self.client = self.app.test_client()
        self.calls = []
        for name, fn in (("remove_mc_contact", lambda rid, cid: self.calls.append(("all", rid, cid))),
                         ("remove_mc_contact_scoped",
                          lambda rid, cid, scope="all": self.calls.append((scope, rid, cid)))):
            p = mock.patch.object(mc_routes, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(mc_routes, "get_mc_ignored", return_value={"ig1"})
        p.start()
        self.addCleanup(p.stop)

    def test_ignored_contact_is_removed_from_radio_only(self):
        r = self.client.post("/api/mc/contacts/cleanup", json={"contacts": [
            {"id": "ig1", "radio_id": "mcA"}, {"id": "ok1", "radio_id": "mcA"}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn(("radio", "mcA", "ig1"), self.calls, "ignored -> radio scope only")
        self.assertIn(("all", "mcA", "ok1"), self.calls, "normal contact -> full removal")
        self.assertEqual(r.get_json()["removed"], 2)


if __name__ == "__main__":
    unittest.main()

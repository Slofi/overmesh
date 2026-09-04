import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
sys.path.insert(0, "/home/slofi/overmesh")

import db  # noqa: E402
from routes import nodes as nodes_routes  # noqa: E402


def _node_row(radio_id, ts):
    return {
        "id": "!abcdef01",
        "long_name": f"Shared via {radio_id}",
        "short_name": "SHR",
        "snr": None,
        "rssi": None,
        "battery": None,
        "latitude": None,
        "longitude": None,
        "hops_away": 1,
        "radio_id": radio_id,
        "is_local": False,
        "last_heard_ts": ts,
    }


class MtDbMultiRadioPersistenceTests(unittest.TestCase):
    def setUp(self):
        db.init_prefs_db()
        with db.get_prefs_db() as conn:
            conn.execute("DELETE FROM nodes")
            # node_notes is a separate radio-agnostic table — clear it too or
            # notes leak between tests (and into other modules sharing the DB).
            conn.execute("DELETE FROM node_notes")
        self.app = Flask(__name__)
        self.app.register_blueprint(nodes_routes.bp)
        self.client = self.app.test_client()

    def _seed_same_node_on_two_radios(self):
        db.upsert_node(_node_row("mt_a", 100))
        db.upsert_node(_node_row("mt_b", 200))

    def test_node_patch_with_radio_id_updates_only_that_radio_row(self):
        self._seed_same_node_on_two_radios()

        resp = self.client.patch(
            "/api/db/node/!abcdef01",
            json={"radio_id": "mt_b", "is_favorite": True, "notes": "heard on B"},
        )

        self.assertEqual(resp.status_code, 200)
        # is_favorite / is_ignored are per-radio (nodes.radio_id row).
        with db.get_prefs_db() as conn:
            favs = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT radio_id, is_favorite FROM nodes WHERE id=?",
                    ("!abcdef01",),
                ).fetchall()
            }
        self.assertEqual(favs["mt_a"], 0)
        self.assertEqual(favs["mt_b"], 1)
        # Notes are deliberately RADIO-AGNOSTIC since 1032c0c (2026-05-13):
        # they live in node_notes keyed by node_id alone, not on nodes.notes.
        with db.get_prefs_db() as conn:
            note_rows = conn.execute(
                "SELECT notes_json FROM node_notes WHERE node_id=?", ("!abcdef01",)
            ).fetchall()
        self.assertEqual([r[0] for r in note_rows], ["heard on B"])

    def test_node_delete_with_radio_id_deletes_only_that_radio_row(self):
        self._seed_same_node_on_two_radios()

        resp = self.client.delete("/api/db/node/!abcdef01?radio_id=mt_b")

        self.assertEqual(resp.status_code, 200)
        with db.get_prefs_db() as conn:
            rows = conn.execute(
                "SELECT radio_id FROM nodes WHERE id=? ORDER BY radio_id",
                ("!abcdef01",),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], ["mt_a"])

    def test_get_db_nodes_keeps_same_node_separate_per_radio(self):
        self._seed_same_node_on_two_radios()
        self.client.patch(
            "/api/db/node/!abcdef01",
            json={"radio_id": "mt_b", "is_favorite": True, "notes": "heard on B"},
        )

        rows = db.get_db_nodes(fav_first=False)

        by_radio = {row["radio_id"]: row for row in rows if row["id"] == "!abcdef01"}
        self.assertEqual(set(by_radio), {"mt_a", "mt_b"})
        # Favourites stay per-radio…
        self.assertEqual(by_radio["mt_a"]["is_favorite"], 0)
        self.assertEqual(by_radio["mt_b"]["is_favorite"], 1)
        # …while the note is shared across every radio row for that node, by
        # design (node_notes is keyed by node_id — see 1032c0c).
        self.assertEqual(by_radio["mt_a"]["notes"], "heard on B")
        self.assertEqual(by_radio["mt_b"]["notes"], "heard on B")


if __name__ == "__main__":
    unittest.main()

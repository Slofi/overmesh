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
        with db.get_prefs_db() as conn:
            rows = {
                row[0]: {"is_favorite": row[1], "notes": row[2]}
                for row in conn.execute(
                    "SELECT radio_id, is_favorite, notes FROM nodes WHERE id=?",
                    ("!abcdef01",),
                ).fetchall()
            }
        self.assertEqual(rows["mt_a"], {"is_favorite": 0, "notes": ""})
        self.assertEqual(rows["mt_b"], {"is_favorite": 1, "notes": "heard on B"})

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
        self.assertEqual(by_radio["mt_a"]["notes"], "")
        self.assertEqual(by_radio["mt_a"]["is_favorite"], 0)
        self.assertEqual(by_radio["mt_b"]["notes"], "heard on B")
        self.assertEqual(by_radio["mt_b"]["is_favorite"], 1)


if __name__ == "__main__":
    unittest.main()

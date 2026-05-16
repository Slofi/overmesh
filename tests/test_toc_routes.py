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
from routes import toc as toc_routes  # noqa: E402


class TocRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(toc_routes.bp)
        self.client = self.app.test_client()
        db.init_prefs_db()
        with db.get_prefs_db() as conn:
            conn.execute("DELETE FROM toc_log")

    def test_add_accepts_custom_timestamp(self):
        resp = self.client.post("/api/toc", json={
            "category": "SITREP",
            "body": "Custom time",
            "ts": 1_777_777_700,
        })

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["category"], "SITREP")
        self.assertEqual(data["ts"], 1_777_777_700)

    def test_update_entry_changes_body_category_and_timestamp(self):
        created = self.client.post("/api/toc", json={"body": "Wrong", "category": "NOTE", "ts": 10}).get_json()

        resp = self.client.put(f"/api/toc/{created['id']}", json={
            "body": "Corrected",
            "category": "ACTION",
            "ts": 20,
        })

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["body"], "Corrected")
        self.assertEqual(data["category"], "ACTION")
        self.assertEqual(data["ts"], 20)

    def test_structured_body_is_saved_as_markdown_fields(self):
        resp = self.client.post("/api/toc", json={
            "category": "COMMS",
            "body": {"From": "Alpha", "To": "Bravo", "Hops": "2", "Distance": "4.2 km"},
            "ts": 30,
        })

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()["body"]
        self.assertIn("**From:** Alpha", body)
        self.assertIn("**Hops:** 2", body)
        self.assertIn("**Distance:** 4.2 km", body)

    def test_intel_category_is_accepted(self):
        resp = self.client.post("/api/toc", json={
            "category": "INTEL",
            "body": {"Who / Source": "Alpha", "Intel Tags": "Personnel, Recon"},
            "ts": 35,
        })

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["category"], "INTEL")
        self.assertIn("**Intel Tags:** Personnel, Recon", data["body"])

    def test_plan_category_is_accepted(self):
        resp = self.client.post("/api/toc", json={
            "category": "PLAN",
            "body": {"Objective": "Mobile CD route", "Mission / Folder": "CD test"},
            "ts": 36,
        })

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["category"], "PLAN")
        self.assertIn("**Objective:** Mobile CD route", data["body"])
        self.assertIn("**Mission / Folder:** CD test", data["body"])

    def test_text_export_formats_legacy_json_body_as_markdown(self):
        created = self.client.post("/api/toc", json={
            "category": "SITREP",
            "body": json.dumps({"Location": "Hill", "Situation": "Clear"}),
            "ts": 40,
        }).get_json()
        self.assertEqual(created["category"], "SITREP")

        resp = self.client.get("/api/toc/export?fmt=text")

        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn("**Location:** Hill", text)
        self.assertIn("**Situation:** Clear", text)

    def test_json_import_round_trips_export_shape(self):
        content = json.dumps([
            {"id": 99, "ts": 111, "category": "COMMS", "body": "Radio check"},
            {"ts": 222, "category": "NOPE", "body": "Bad category falls back"},
        ])

        resp = self.client.post("/api/toc/import", json={"filename": "toc_log.json", "content": content})

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["imported"], 2)
        self.assertEqual(data["entries"][0]["category"], "COMMS")
        self.assertEqual(data["entries"][1]["category"], "NOTE")

    def test_txt_import_accepts_exported_text_format(self):
        content = "[2026-05-09 12:34:00] [ALERT]\nFirst line\n\n[2026-05-09 12:35:00] [NOTE]\nSecond line\n"

        resp = self.client.post("/api/toc/import", json={"filename": "toc_log.txt", "content": content})

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["imported"], 2)
        self.assertEqual(data["entries"][0]["category"], "ALERT")
        self.assertEqual(data["entries"][0]["body"], "First line")


if __name__ == "__main__":
    unittest.main()

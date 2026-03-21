import json
import random
import time

from flask import Blueprint, jsonify, request

from db import get_prefs_db
from helpers import push_to_sse
from state import notes_cache, notes_lock

bp = Blueprint('notes', __name__)


@bp.route("/api/notes")
def api_get_notes():
    with notes_lock:
        return jsonify(list(notes_cache.values()))


@bp.route("/api/notes", methods=["POST"])
def api_save_note():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    desc         = (data.get("description") or "").strip()[:200]
    try:
        lat = float(data.get("lat") or 0)
        lon = float(data.get("lon") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400
    marker_emoji = (data.get("marker_emoji") or "📝").strip() or "📝"
    note_id = int(time.time() * 1000) + random.randint(0, 999)
    note = {"id": note_id, "name": name, "description": desc,
            "lat": lat, "lon": lon, "marker_emoji": marker_emoji, "ts": int(time.time())}
    with notes_lock:
        notes_cache[note_id] = note
    with get_prefs_db() as conn:
        conn.cursor().execute(
            "INSERT INTO self_notes (id,name,description,lat,lon,marker_emoji,ts) VALUES (?,?,?,?,?,?,?)",
            (note_id, name, desc, lat, lon, marker_emoji, int(time.time()))
        )
    push_to_sse(json.dumps({"type": "note", "note": note}))
    return jsonify({"ok": True, "id": note_id})


@bp.route("/api/notes/<int:note_id>", methods=["PUT"])
def api_edit_note(note_id):
    with notes_lock:
        note = notes_cache.get(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    desc         = (data.get("description") or "").strip()[:200]
    marker_emoji = (data.get("marker_emoji") or "📝").strip() or "📝"
    updated = dict(note, name=name, description=desc, marker_emoji=marker_emoji)
    with notes_lock:
        notes_cache[note_id] = updated
    with get_prefs_db() as conn:
        conn.cursor().execute(
            "UPDATE self_notes SET name=?,description=?,marker_emoji=? WHERE id=?",
            (name, desc, marker_emoji, note_id)
        )
    push_to_sse(json.dumps({"type": "note", "note": updated}))
    return jsonify({"ok": True})


@bp.route("/api/notes/<int:note_id>", methods=["DELETE"])
def api_delete_note(note_id):
    with notes_lock:
        if note_id not in notes_cache:
            return jsonify({"error": "Note not found"}), 404
        notes_cache.pop(note_id, None)  # remove under same lock — prevents duplicate deletes
    with get_prefs_db() as conn:
        conn.cursor().execute("DELETE FROM self_notes WHERE id=?", (note_id,))
    push_to_sse(json.dumps({"type": "note_deleted", "id": note_id}))
    return jsonify({"ok": True})

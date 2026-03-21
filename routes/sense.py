import json
import threading
import time

from flask import Blueprint, jsonify, request

import sense as _sense_mod
from config import CONFIG, save_config
from helpers import push_to_sse
from mesh import get_any_iface
from sense import (
    _active_auto_event,
    _active_auto_loop,
    _active_auto_running_lock,
    _run_sense_broadcast,
    _sense_lock,
    _sense_state,
    SENSE_COLLECTION_WINDOW,
)

bp = Blueprint('sense_routes', __name__)


@bp.route("/api/mesh/sense", methods=["POST"])
def api_mesh_sense():
    iface = get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    cooldown = CONFIG.get("app", {}).get("sense_cooldown", 180)
    now = time.time()
    with _sense_lock:
        elapsed = now - _sense_state["last_triggered"]
        if _sense_state["last_triggered"] > 0 and elapsed < cooldown:
            return jsonify({"ok": False, "cooldown_remaining": int(cooldown - elapsed)}), 429
        _sense_state["active"]         = True
        _sense_state["last_triggered"] = now
        _sense_state["window_end"]     = now + SENSE_COLLECTION_WINDOW
        _sense_state["responses"]      = []
    threading.Thread(target=_run_sense_broadcast, args=(iface, cooldown), daemon=True).start()
    push_to_sse(json.dumps({"type": "sense_started", "window": SENSE_COLLECTION_WINDOW,
                            "cooldown": cooldown}))
    return jsonify({"ok": True, "window": SENSE_COLLECTION_WINDOW, "cooldown": cooldown})


@bp.route("/api/mesh/sense/passive", methods=["POST"])
def api_sense_passive():
    with _sense_lock:
        new_val = not _sense_state["passive"]
        _sense_state["passive"] = new_val
    CONFIG["sense_passive"] = new_val
    save_config()
    return jsonify({"passive": new_val})


@bp.route("/api/mesh/sense/active_auto", methods=["POST"])
def api_sense_active_auto():
    with _sense_lock:
        active_auto = not _sense_state["active_auto"]
        _sense_state["active_auto"] = active_auto
    CONFIG["sense_active_auto"] = active_auto
    save_config()
    if active_auto:
        _active_auto_event.clear()
        with _active_auto_running_lock:
            if not _sense_mod._active_auto_running:
                threading.Thread(target=_active_auto_loop, daemon=True).start()
    else:
        _active_auto_event.set()   # wake the waiting thread so it exits cleanly
    return jsonify({"active_auto": active_auto})


@bp.route("/api/mesh/sense/status", methods=["GET"])
def api_mesh_sense_status():
    cooldown = CONFIG.get("app", {}).get("sense_cooldown", 180)
    now = time.time()
    with _sense_lock:
        remaining_window   = max(0, int(_sense_state["window_end"] - now))
        elapsed_since_last = now - _sense_state["last_triggered"]
        cooldown_remaining = max(0, int(cooldown - elapsed_since_last)) if _sense_state["last_triggered"] > 0 else 0
        payload = {
            "active":             _sense_state["active"],
            "passive":            _sense_state["passive"],
            "active_auto":        _sense_state["active_auto"],
            "cooldown_remaining": cooldown_remaining,
            "window_remaining":   remaining_window,
            "response_count":     len(_sense_state["responses"]),
            "responses":          list(_sense_state["responses"]),
            "cooldown":           cooldown,
        }
    return jsonify(payload)  # serialise outside the lock

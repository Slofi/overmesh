import logging

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from helpers import _as_bool, push_to_sse
from mesh import get_iface_by_radio, restore_mt_radio, silence_mt_radio
import sense as _sense_mod
from sense import _active_auto_event, _active_auto_running_lock, _sense_lock, _sense_state

log = logging.getLogger(__name__)
bp = Blueprint("silent", __name__)


def _silent_enable():
    """Silence every MT radio; snapshot the ones that answer for the restore.

    Order matters: the flag is set FIRST, so a radio that connects while this
    loop is still running matches the new state (sync_silent_running_on_mt_connect
    is what handles it), and a radio that is away right now is silenced the moment
    it appears instead of being missed altogether.
    """
    with _sense_lock:
        _sense_state["active_auto"] = False
    _active_auto_event.set()
    with _active_auto_running_lock:
        _sense_mod._active_auto_running = False
    with CONFIG_LOCK:
        CONFIG["silent_mode"] = True
        CONFIG["sense_active_auto"] = False
        save_config()
    silenced, awaiting = [], []
    for node_cfg in CONFIG.get("nodes", []):
        if not node_cfg.get("enabled", True):
            continue
        radio_id = node_cfg["id"]
        iface = get_iface_by_radio(radio_id)
        if not iface:
            awaiting.append(radio_id)      # silenced by the MT connect path
            continue
        if silence_mt_radio(iface, radio_id):
            silenced.append(radio_id)
    # MC companion nodes have no firmware-side auto-advert, and since 2026-10-16 (S453)
    # OM sends an advert only on an explicit request — so the app-layer gate in
    # mesh_mc.py (send_advert / _send_advert_async) covers what remains. Since the
    # same date the MC guarantee lives at the radio boundary, not at the callers:
    # install_silent_tx_guard() refuses any transmitting command while silent.
    push_to_sse({"type": "silent_mode", "active": True})
    log.info(f"[silent] Silent Running enabled — silenced {len(silenced)}, "
             f"awaiting connect {len(awaiting)}: {awaiting}")


def _silent_disable():
    """Restore the MT radios the snapshot holds and clear the silent state.

    A radio that was silenced but is away keeps its snapshot entry: the connect
    path hands its own tx_enabled back when it returns. A radio that is NOT in
    the snapshot is left completely alone — writing tx_enabled=True for it would
    *enable* transmitting on a radio Silent Running never silenced.
    """
    with CONFIG_LOCK:
        CONFIG["silent_mode"] = False      # first, so a connect in flight syncs to OFF
        save_config()
    snapshot = CONFIG.get("silent_snapshot", {}) or {}
    restored, awaiting = [], []
    for node_cfg in CONFIG.get("nodes", []):
        if not node_cfg.get("enabled", True):
            continue
        radio_id = node_cfg["id"]
        saved = snapshot.get(radio_id)
        if not saved:
            continue
        iface = get_iface_by_radio(radio_id)
        if not iface:
            awaiting.append(radio_id)      # restored by the connect path
            continue
        if restore_mt_radio(iface, radio_id, bool(saved.get("tx_enabled", True))):
            restored.append(radio_id)
    with CONFIG_LOCK:
        if not CONFIG.get("silent_snapshot"):
            CONFIG.pop("silent_snapshot", None)
        save_config()
    push_to_sse({"type": "silent_mode", "active": False})
    log.info(f"[silent] Silent Running disabled — restored {len(restored)}, "
             f"awaiting connect {len(awaiting)}: {awaiting}")
    log.info("[silent] Silent Running disabled")


@bp.route("/api/silent_mode")
def api_silent_mode_get():
    return jsonify({"silent_mode": bool(CONFIG.get("silent_mode", False))})


@bp.route("/api/silent_mode", methods=["POST"])
def api_silent_mode_set():
    data = request.get_json(silent=True) or {}
    enable = _as_bool(data.get("enabled", False))
    if enable:
        _silent_enable()
    else:
        _silent_disable()
    return jsonify({"ok": True, "silent_mode": enable})

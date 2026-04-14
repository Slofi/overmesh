import logging

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from helpers import push_to_sse
from mesh import get_iface_by_radio
import sense as _sense_mod
from sense import _active_auto_event, _active_auto_running_lock, _sense_lock, _sense_state

log = logging.getLogger(__name__)
bp = Blueprint("silent", __name__)


def _silent_enable():
    """Push lora.tx_enabled=False to all connected MT radios and save snapshot."""
    with _sense_lock:
        _sense_state["active_auto"] = False
    _active_auto_event.set()
    with _active_auto_running_lock:
        _sense_mod._active_auto_running = False
    snapshot = {}
    for node_cfg in CONFIG.get("nodes", []):
        if not node_cfg.get("enabled", True):
            continue
        radio_id = node_cfg["id"]
        iface = get_iface_by_radio(radio_id)
        if not iface:
            continue
        try:
            lc = iface.localNode.localConfig
            tx_enabled = bool(getattr(lc.lora, "tx_enabled", True))
            role = int(getattr(lc.device, "role", 0))
            snapshot[radio_id] = {"tx_enabled": tx_enabled, "role": role}
            lc.lora.tx_enabled = False
            iface.localNode.writeConfig("lora")
            log.info(f"[silent] MT {radio_id}: tx_enabled → False (was {tx_enabled})")
        except Exception as e:
            log.warning(f"[silent] MT {radio_id} enable failed: {e}")
    # MC companion nodes have no firmware-side auto-advert — app-layer gating
    # in mesh_mc.py (_startup_advert, send_advert) is sufficient.
    with CONFIG_LOCK:
        CONFIG["silent_mode"] = True
        CONFIG["silent_snapshot"] = snapshot
        CONFIG["sense_active_auto"] = False
        save_config()
    push_to_sse({"type": "silent_mode", "active": True})
    log.info("[silent] Silent Running enabled")


def _silent_disable():
    """Restore MT radios from snapshot and clear silent state."""
    snapshot = CONFIG.get("silent_snapshot", {})
    for node_cfg in CONFIG.get("nodes", []):
        if not node_cfg.get("enabled", True):
            continue
        radio_id = node_cfg["id"]
        iface = get_iface_by_radio(radio_id)
        if not iface:
            continue
        try:
            saved = snapshot.get(radio_id, {})
            restored = bool(saved.get("tx_enabled", True))
            lc = iface.localNode.localConfig
            lc.lora.tx_enabled = restored
            iface.localNode.writeConfig("lora")
            log.info(f"[silent] MT {radio_id}: tx_enabled → {restored}")
        except Exception as e:
            log.warning(f"[silent] MT {radio_id} restore failed: {e}")
    with CONFIG_LOCK:
        CONFIG["silent_mode"] = False
        CONFIG.pop("silent_snapshot", None)
        save_config()
    push_to_sse({"type": "silent_mode", "active": False})
    log.info("[silent] Silent Running disabled")


@bp.route("/api/silent_mode")
def api_silent_mode_get():
    return jsonify({"silent_mode": bool(CONFIG.get("silent_mode", False))})


@bp.route("/api/silent_mode", methods=["POST"])
def api_silent_mode_set():
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enabled", False))
    if enable:
        _silent_enable()
    else:
        _silent_disable()
    return jsonify({"ok": True, "silent_mode": enable})

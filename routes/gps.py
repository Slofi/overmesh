from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from gps import (
    _gps_push_to_nodes, _gps_start, _gps_stop,
    gps_lock, gps_port_conflict, gps_port_present, gps_state, _gps_runtime,
)

bp = Blueprint('gps_routes', __name__)


@bp.route("/api/settings/gps", methods=["GET"])
def api_settings_gps_get():
    cfg = CONFIG.get("gps", {"enabled": False, "port": ""})
    with gps_lock:
        pos = {k: gps_state[k] for k in ("lat", "lon", "alt", "sats", "sats_view", "fix", "speed")}
        runtime = dict(_gps_runtime)
    port = str(cfg.get("port") or "").strip()
    if port and runtime.get("port") != port:
        runtime["port"] = port
        runtime["port_present"] = gps_port_present(port)
    return jsonify({**cfg, **pos, **runtime})


@bp.route("/api/settings/gps", methods=["POST"])
def api_settings_gps_set():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    port    = str(data.get("port", "")).strip()
    if enabled and port:
        conflict = gps_port_conflict(port)
        if conflict:
            return jsonify({"error": f"GPS port {port} conflicts with enabled {conflict['network']} radio {conflict['name']}."}), 400
    with CONFIG_LOCK:
        cfg  = CONFIG.setdefault("gps", {"enabled": False, "port": ""})
        was_enabled = cfg.get("enabled", False)
        cfg["enabled"] = enabled
        cfg["port"]    = port
        if "auto_push" in data:
            cfg["auto_push"] = bool(data["auto_push"])
        if "push_interval" in data:
            try:
                cfg["push_interval"] = max(10, min(3600, int(data["push_interval"])))
            except (TypeError, ValueError):
                return jsonify({"error": "push_interval must be a number"}), 400
        if "precision" in data:
            try:
                cfg["precision"] = max(1, min(32, int(data["precision"])))
            except (TypeError, ValueError):
                return jsonify({"error": "precision must be a number"}), 400
        if "precision_meters" in data:
            try:
                cfg["precision_meters"] = max(0, min(1000, int(data["precision_meters"])))
            except (TypeError, ValueError):
                pass
        save_config()
    warning = None
    if enabled and port:
        if gps_port_present(port):
            _gps_start(port)
        else:
            _gps_stop()
            warning = f"GPS enabled, but port {port} is not currently connected."
    elif was_enabled and not enabled:
        _gps_stop()
    return jsonify({"ok": True, "warning": warning})


@bp.route("/api/gps/push", methods=["POST"])
def api_gps_push():
    import threading
    with gps_lock:
        lat  = gps_state.get("lat")
        lon  = gps_state.get("lon")
        alt  = gps_state.get("alt") or 0
        fix  = gps_state.get("fix", False)
    if not fix or lat is None or lon is None:
        return jsonify({"error": "No GPS fix — cannot push position"}), 400
    precision_bits = CONFIG.get("gps", {}).get("precision", 32)
    threading.Thread(target=_gps_push_to_nodes, args=(lat, lon, alt, precision_bits), daemon=True).start()
    return jsonify({"ok": True, "pushed": ["all connected"]})

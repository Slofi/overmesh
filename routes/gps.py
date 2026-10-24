from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from helpers import _as_bool
from gps import (
    GPS_SOURCES, _gps_push_to_mc, _gps_push_to_nodes, _is_hw_uart, gps_apply_config, gps_lock, gps_port_conflict,
    gps_port_present, gps_port_radio, gps_source, gps_source_port, gps_state, gps_usb_id, _gps_runtime,
)

bp = Blueprint('gps_routes', __name__)

_DEFAULT_PROXY_URL = "http://localhost:8090"


def _public_cfg(cfg):
    """The GPS config as Settings sees it: the ONE source, each serial source's own port, two push toggles."""
    source = gps_source(cfg)
    out = {k: v for k, v in cfg.items() if k not in ("enabled", "auto_push")}
    out.update({
        "source": source,
        "port": gps_source_port(cfg, source),
        "internal_port": gps_source_port(cfg, "internal"),
        "usb_port": gps_source_port(cfg, "usb"),
        "auto_push_mt": bool(cfg.get("auto_push_mt", cfg.get("auto_push", False))),
        "auto_push_mc": bool(cfg.get("auto_push_mc", False)),
    })
    return out


@bp.route("/api/settings/gps", methods=["GET"])
def api_settings_gps_get():
    pub = _public_cfg(CONFIG.get("gps") or {})
    with gps_lock:
        pos = {k: gps_state[k] for k in ("lat", "lon", "alt", "sats", "sats_view", "fix", "speed")}
        runtime = dict(_gps_runtime)
    if pub["source"] in ("internal", "usb") and pub["port"] and runtime.get("port") != pub["port"]:
        runtime["port"] = pub["port"]
        runtime["port_present"] = gps_port_present(pub["port"])
    # The live state must not overwrite the CONFIGURED source: the Settings dropdown is filled from `source`.
    runtime["active_source"] = runtime.pop("source", "")
    return jsonify({**pub, **pos, **runtime})


@bp.route("/api/settings/gps", methods=["POST"])
def api_settings_gps_set():
    data = request.get_json(silent=True) or {}
    current = CONFIG.get("gps") or {}
    source = str(data.get("source", "")).strip()
    port = str(data.get("port", "")).strip()
    if "enabled" in data or source not in GPS_SOURCES:
        # A Settings page from before S471, still open in a browser, sends enabled + direct/proxy ("proxy" is a
        # value of both shapes, so `enabled` is what marks the old one; the new page never sends it).
        source = gps_source({"enabled": _as_bool(data.get("enabled", False)), "source": source, "port": port})
    if source in ("internal", "usb"):
        # Only a port the user PICKED is refused outright. A remembered one that is no longer valid (e.g. CD's old
        # /dev/ttyACM0, now a radio) must not block switching the source: gps_apply_config reads nothing from it
        # and the warning tells the user to pick (S471).
        picked = bool(port)
        port = port or gps_source_port(current, source)
        if picked:
            if (source == "internal") != _is_hw_uart(port):
                kind = "a built-in UART" if source == "internal" else "a USB device"
                return jsonify({"error": f"GPS port {port} is not {kind}."}), 400
            radio = gps_port_radio(port)   # a saved radio's board, enabled or not (S471)
            if radio:
                return jsonify({"error": f"GPS port {port} is the {radio}."}), 400
            conflict = gps_port_conflict(port)
            if conflict:
                return jsonify({"error": f"GPS port {port} conflicts with enabled {conflict['network']} radio {conflict['name']}."}), 400
    with CONFIG_LOCK:
        cfg = CONFIG.setdefault("gps", {})
        prev_usb = gps_source_port(cfg, "usb")
        # Pre-S471 shape -> per-source ports + per-network push toggles, so nothing the user set is lost.
        legacy_port = str(cfg.pop("port", "") or "").strip()
        if legacy_port:
            cfg.setdefault("internal_port" if _is_hw_uart(legacy_port) else "usb_port", legacy_port)
        if "auto_push" in cfg:
            cfg.setdefault("auto_push_mt", bool(cfg["auto_push"]))
            cfg.pop("auto_push")
        cfg.pop("enabled", None)
        cfg["source"] = source
        cfg["proxy_url"] = str(data.get("proxy_url") or cfg.get("proxy_url") or _DEFAULT_PROXY_URL).strip()
        if source == "internal" and port:
            cfg["internal_port"] = port
        if source == "usb" and port:
            cfg["usb_port"] = port
            # Remember WHICH device was picked, so another board that later takes the same /dev/ttyACMx name is
            # never opened as the GPS. Kept when the port is unchanged but unplugged right now.
            usb_id = gps_usb_id(port)
            if usb_id:
                cfg["usb_id"] = usb_id
            elif port != prev_usb:
                cfg.pop("usb_id", None)
        if "auto_push_mt" in data or "auto_push" in data:
            cfg["auto_push_mt"] = _as_bool(data.get("auto_push_mt", data.get("auto_push")))
        if "auto_push_mc" in data:
            cfg["auto_push_mc"] = _as_bool(data["auto_push_mc"])
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
    warning = gps_apply_config(restart=True)
    return jsonify({"ok": True, "source": source, "warning": warning})


@bp.route("/api/gps/push", methods=["POST"])
def api_gps_push():
    """Send the current LIVE fix to one network's connected radios (Filip, S471: MT and MC separately)."""
    import threading
    data = request.get_json(silent=True) or {}
    network = str(data.get("network") or "mt").strip().lower()
    if network not in ("mt", "mc"):
        return jsonify({"error": "network must be 'mt' or 'mc'"}), 400
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active: transmissions are blocked"}), 409
    if gps_source() == "manual":
        return jsonify({"error": "OM Position is hand-picked: it is never sent to radios. Choose a GPS source to share a live fix."}), 400
    with gps_lock:
        lat  = gps_state.get("lat")
        lon  = gps_state.get("lon")
        alt  = gps_state.get("alt") or 0
        fix  = gps_state.get("fix", False)
    if not fix or lat is None or lon is None:
        return jsonify({"error": "No GPS fix: cannot push position"}), 400
    precision_bits = (CONFIG.get("gps") or {}).get("precision", 32)
    if network == "mt":
        threading.Thread(target=_gps_push_to_nodes, args=(lat, lon, alt, precision_bits), daemon=True).start()
    else:
        threading.Thread(target=_gps_push_to_mc, args=(lat, lon, precision_bits), daemon=True).start()
    return jsonify({"ok": True, "network": network, "pushed": ["all connected"]})

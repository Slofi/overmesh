import threading

from flask import Blueprint, jsonify, request

from bot import (
    build_motd_text, build_mc_motd_text, load_bot_config, log_bot_activity,
    save_bot_config, send_bot_response, send_mc_bot_response,
)
from mesh import get_any_iface
from state import (
    _motd_event,
    bot_activity, bot_activity_lock,
    connections, connections_lock,
    mc_connections, mc_connections_lock,
)

bp = Blueprint('bot_routes', __name__)


@bp.route("/api/bot/config", methods=["GET"])
def api_bot_config_get():
    radio_id = request.args.get("radio_id") or None
    return jsonify(load_bot_config(radio_id))


@bp.route("/api/bot/config", methods=["POST"])
def api_bot_config_set():
    data = request.get_json(silent=True) or {}
    radio_id = data.pop("radio_id", None) or None
    # Basic structural validation before saving
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid config format"}), 400
    if "commands" in data and not isinstance(data["commands"], dict):
        return jsonify({"error": "commands must be an object"}), 400
    if "motd" in data and not isinstance(data["motd"], dict):
        return jsonify({"error": "motd must be an object"}), 400
    save_bot_config(data, radio_id)
    _motd_event.set()  # wake scheduler to pick up new config
    return jsonify({"ok": True})


@bp.route("/api/bot/activity", methods=["GET"])
def api_bot_activity_get():
    with bot_activity_lock:
        return jsonify(list(reversed(bot_activity)))


@bp.route("/api/bot/motd/test", methods=["POST"])
def api_bot_motd_test():
    data = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or None
    cfg = load_bot_config(radio_id)
    if not cfg.get("enabled"):
        return jsonify({"ok": False, "error": "Bot is disabled"})

    channel = cfg.get("listen_channels", [0])[0]

    # Check if radio_id belongs to an MC radio
    with mc_connections_lock:
        mc_state = mc_connections.get(radio_id, {}) if radio_id else {}
    is_mc = mc_state.get("status") == "connected"

    if is_mc:
        text = f"[{cfg.get('bot_label', 'OM Bot')}] {build_mc_motd_text(cfg, radio_id)}"
        threading.Thread(
            target=send_mc_bot_response, args=(radio_id, text, channel), daemon=True
        ).start()
        log_bot_activity("Bot", "motd_test_mc", text, channel)
        return jsonify({"ok": True})

    # MT radio
    if radio_id:
        with connections_lock:
            state = connections.get(radio_id, {})
            iface = state.get("iface") if state.get("status") == "connected" else None
    else:
        iface = get_any_iface()
    if not iface:
        return jsonify({"ok": False, "error": "No radio connected"})
    text = f"[{cfg.get('bot_label', 'OM Bot')}] {build_motd_text(cfg)}"
    threading.Thread(target=send_bot_response, args=(iface, text, channel, None), daemon=True).start()
    log_bot_activity("Bot", "motd_test", text, channel)
    return jsonify({"ok": True})

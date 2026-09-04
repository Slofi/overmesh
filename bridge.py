import json
import logging
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, _valid_node_id

log = logging.getLogger(__name__)

bp = Blueprint("bridge", __name__)

# Highest valid channel index per network. Meshtastic has 8 slots. MeshCore
# addresses channels with a single byte, so its ceiling is 255 (ERA-3 reports
# max_channels=40) — the device-accurate limit is enforced downstream by
# routes.mc.api_mc_send_chan; this is only a cheap sanity bound.
# Do not collapse these into one value (GH #20).
MT_MAX_CHANNEL_IDX = 7
MC_MAX_CHANNEL_IDX = 255


def _bridge_cfg():
    with CONFIG_LOCK:
        cfg = CONFIG.get("bridge") or CONFIG.get("automation_bridge") or {}
        return json.loads(json.dumps(cfg))


def _webhook_cfg():
    cfg = _bridge_cfg()
    return cfg.get("webhooks") or {}


def _ingest_cfg():
    cfg = _bridge_cfg()
    return cfg.get("ingest") or {}


def _network_for_message(msg):
    network = (msg.get("network") or "").strip().lower()
    if network:
        return network
    if msg.get("type") == "mc_message":
        return "mc"
    return "mt"


def _normalize_inbound_message(msg):
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    if text is None or msg.get("sent"):
        return None
    network = _network_for_message(msg)
    payload = {
        "type": "overmesh.message",
        "direction": "inbound",
        "network": network,
        "radio_id": msg.get("radio_id"),
        "radio_name": msg.get("radio_name"),
        "subtype": msg.get("subtype") or ("dm" if msg.get("is_dm") else "channel"),
        "channel": msg.get("channel"),
        "from_id": msg.get("from_id"),
        "from_name": msg.get("from_name"),
        "to_id": msg.get("to_id"),
        "to_name": msg.get("to_name"),
        "text": str(text),
        "ts": msg.get("ts") or int(time.time()),
        "pkt_id": msg.get("pkt_id"),
        "is_dm": bool(msg.get("is_dm") or msg.get("subtype") == "dm"),
    }
    for key in ("snr", "rx_snr", "rx_rssi", "hops", "path", "path_len", "route_type"):
        if key in msg:
            payload[key] = msg.get(key)
    return payload


def publish_inbound_message(msg):
    """Fan inbound mesh messages out to configured automation webhooks."""
    payload = _normalize_inbound_message(msg)
    if not payload:
        return
    cfg = _webhook_cfg()
    if not cfg.get("enabled"):
        return
    urls = cfg.get("urls") or cfg.get("incoming_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        return
    timeout = cfg.get("timeout", 3)
    try:
        timeout = max(0.2, min(30.0, float(timeout)))
    except (TypeError, ValueError):
        timeout = 3.0
    secret = cfg.get("secret") or ""
    for url in urls:
        threading.Thread(
            target=_post_webhook,
            args=(url, payload, timeout, secret),
            daemon=True,
        ).start()


def _post_webhook(url, payload, timeout, secret):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "OverMesh-Bridge",
    }
    if secret:
        headers["X-OverMesh-Secret"] = str(secret)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                log.warning(f"Bridge webhook {url} returned HTTP {resp.status}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning(f"Bridge webhook {url} failed: {e}")


def _authorized(cfg):
    token = (cfg.get("token") or "").strip()
    if not token:
        return False
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return True
    return request.headers.get("X-OverMesh-Token") == token


def _channel_from_payload(data, max_channel=MT_MAX_CHANNEL_IDX):
    """Validate an inbound channel index.

    max_channel differs per network: Meshtastic has 8 slots (0-7); MeshCore
    addresses channels with a single byte, so its bound is 255. The
    device-accurate MC ceiling is enforced downstream by
    routes.mc.api_mc_send_chan; this is the early, clear rejection. See GH #20.
    """
    try:
        channel = int(data.get("channel", 0))
    except (TypeError, ValueError):
        raise ValueError("channel must be a number")
    if not (0 <= channel <= max_channel):
        raise ValueError(f"channel must be 0-{max_channel}")
    return channel


@bp.route("/api/bridge/send", methods=["POST"])
def api_bridge_send():
    cfg = _ingest_cfg()
    if not cfg.get("enabled"):
        return jsonify({"error": "Bridge ingest is disabled"}), 403
    if not _authorized(cfg):
        return jsonify({"error": "Unauthorized"}), 401
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active - transmissions are blocked"}), 409

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or data.get("message") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    network = (data.get("network") or "mt").strip().lower()
    radio_id = data.get("radio_id")

    try:
        if network == "mt":
            return _send_mt(data, text, radio_id)
        if network == "mc":
            return _send_mc(data, text, radio_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"Bridge send failed: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify({"error": "network must be 'mt' or 'mc'"}), 400


def _send_mt(data, text, radio_id):
    dest_id = (data.get("dest_id") or data.get("to_id") or "").strip()
    channel = _channel_from_payload(data)
    if radio_id:
        from mesh import get_iface_by_radio

        iface = get_iface_by_radio(radio_id)
        if not iface:
            raise RuntimeError("Radio not connected")
    else:
        from mesh import get_any_iface_with_id

        iface, radio_id = get_any_iface_with_id()
    if not iface:
        raise RuntimeError("No radio connected")
    if dest_id:
        if not _valid_node_id(dest_id):
            raise ValueError("Invalid destination node ID")
        iface.sendText(text, destinationId=dest_id, channelIndex=channel, wantAck=True)
    else:
        iface.sendText(text, channelIndex=channel, wantAck=True)
    return jsonify({"ok": True})


def _send_mc(data, text, radio_id):
    from routes.mc import api_mc_send_chan, api_mc_send_dm

    if not radio_id:
        nodes = [n for n in CONFIG.get("mc_nodes", []) if n.get("enabled", True)]
        if len(nodes) == 1:
            radio_id = nodes[0].get("id")
    if not radio_id:
        raise ValueError("radio_id is required for MeshCore sends")
    target = (data.get("target") or data.get("to_id") or "").strip()
    if target:
        return api_mc_send_dm(radio_id)
    _channel_from_payload(data, MC_MAX_CHANNEL_IDX)
    return api_mc_send_chan(radio_id)

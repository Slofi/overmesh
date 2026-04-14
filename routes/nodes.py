import json
import threading
import time

from flask import Blueprint, jsonify, request
from pubsub import pub

from config import CONFIG, _valid_node_id
from db import get_db_nodes, get_position_history, get_prefs_db, get_traceroute_history, save_message, save_traceroute
from helpers import _next_msg_id, get_node_data, get_node_name, push_to_sse
from hw_models import hw_model_name
from mesh import (
    get_any_iface, get_any_iface_with_id, get_iface_by_radio,
    resolve_node_name,
)
from state import (
    chat_lock, chat_messages,
    connections, connections_lock,
    pending_acks, pending_acks_lock,
    traceroute_lock,
)
import logging
log = logging.getLogger(__name__)

bp = Blueprint('nodes', __name__)


@bp.route("/api/nodes")
def api_nodes():
    radio_id = request.args.get("radio_id")
    nodes = get_node_data()
    if radio_id:
        nodes = [n for n in nodes if n.get("radio_id") == radio_id]
    return jsonify(nodes)


@bp.route("/api/debug/patch_pubkey/<node_hex>", methods=["POST"])
def api_debug_patch_pubkey(node_hex):
    """Inject/update a node's publicKey in both iface.nodes and iface.nodesByNum."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Debug endpoints are localhost-only"}), 403
    import base64
    data = request.get_json(silent=True) or {}
    pubkey_b64 = data.get("publicKey", "")
    node_num = data.get("node_num")  # integer node number
    try:
        pubkey_bytes = base64.b64decode(pubkey_b64)
    except Exception:
        return jsonify({"error": "Invalid base64 publicKey"}), 400
    patched = 0
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if not iface:
                continue
            if iface.nodes is not None:
                if node_hex not in iface.nodes:
                    iface.nodes[node_hex] = {"num": node_num, "user": {"id": node_hex}}
                iface.nodes[node_hex].setdefault("user", {})["publicKey"] = pubkey_bytes
            if node_num and hasattr(iface, "nodesByNum") and iface.nodesByNum is not None:
                if node_num not in iface.nodesByNum:
                    iface.nodesByNum[node_num] = iface.nodes.get(node_hex, {"num": node_num, "user": {"id": node_hex}})
                iface.nodesByNum[node_num].setdefault("user", {})["publicKey"] = pubkey_bytes
            patched += 1
    if patched:
        return jsonify({"ok": True, "node": node_hex, "node_num": node_num, "patched_radios": patched})
    return jsonify({"error": "No iface available"}), 503


@bp.route("/api/debug/node_keys")
def api_debug_node_keys():
    """List all keys in iface.nodes — to find the right key format."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Debug endpoints are localhost-only"}), 403
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                return jsonify(list(iface.nodes.keys()))
    return jsonify([])


@bp.route("/api/debug/raw_node/<node_hex>")
def api_debug_raw_node(node_hex):
    """Dump raw iface.nodes entry for a node — for debugging PKI key exchange."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Debug endpoints are localhost-only"}), 403
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                node = iface.nodes.get(node_hex)
                if node:
                    # publicKey is bytes, convert to hex string for display
                    raw = dict(node)
                    user = dict(raw.get("user", {}))
                    if "publicKey" in user and isinstance(user["publicKey"], (bytes, bytearray)):
                        user["publicKey"] = user["publicKey"].hex()
                    raw["user"] = user
                    return jsonify(raw)
    return jsonify({"error": "node not found"}), 404


@bp.route("/api/status")
def api_status():
    with connections_lock:
        status = {
            nid: {"status": s["status"], "name": s["config"]["name"], "enabled": s["config"].get("enabled", True)}
            for nid, s in connections.items()
        }
    return jsonify(status)


@bp.route("/api/db/nodes")
def api_db_nodes():
    sort_by      = request.args.get("sort", "last_seen")
    sort_dir     = request.args.get("dir", "desc")
    fav_first    = request.args.get("fav_first", "1") == "1"
    show_ignored = request.args.get("show_ignored", "0") == "1"
    return jsonify(get_db_nodes(sort_by, sort_dir, fav_first, show_ignored))


@bp.route("/api/db/radio/<radio_id>/nodes/reset", methods=["POST"])
def api_db_radio_nodes_reset(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 404

    removed_db = 0
    removed_live = 0

    with get_prefs_db() as conn:
        cur = conn.execute(
            "DELETE FROM nodes WHERE radio_id=? AND COALESCE(is_local, 0)=0",
            (radio_id,),
        )
        removed_db = cur.rowcount or 0

    local_info = getattr(iface, "myInfo", None)
    local_node_num = getattr(local_info, "my_node_num", None) if local_info else None

    with connections_lock:
        state = connections.get(radio_id, {})
        iface = state.get("iface")
        if iface:
            nodes = getattr(iface, "nodes", None) or {}
            nodes_by_num = getattr(iface, "nodesByNum", None) or {}

            for key, node in list(nodes.items()):
                if (node or {}).get("num") == local_node_num:
                    continue
                nodes.pop(key, None)
                removed_live += 1

            for num, node in list(nodes_by_num.items()):
                if (node or {}).get("num") == local_node_num:
                    continue
                nodes_by_num.pop(num, None)

    return jsonify({"ok": True, "removed_db": removed_db, "removed_live": removed_live})


@bp.route("/api/db/node/<node_id>", methods=["PATCH"])
def api_db_node_update(node_id):
    data = request.get_json(silent=True) or {}
    with get_prefs_db() as conn:
        c = conn.cursor()
        if "is_favorite" in data:
            c.execute("UPDATE nodes SET is_favorite=? WHERE id=?",
                      (1 if data["is_favorite"] else 0, node_id))
        if "is_ignored" in data:
            c.execute("UPDATE nodes SET is_ignored=? WHERE id=?",
                      (1 if data["is_ignored"] else 0, node_id))
        if "notes" in data:
            c.execute("UPDATE nodes SET notes=? WHERE id=?", (data["notes"], node_id))
    return jsonify({"ok": True})


@bp.route("/api/db/node/<node_id>", methods=["DELETE"])
def api_db_node_delete(node_id):
    with get_prefs_db() as conn:
        conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
    radio_errors = []
    with connections_lock:
        items = list(connections.items())
    for _, state in items:
        iface = state.get("iface")
        if not iface:
            continue
        try:
            node = (iface.nodes or {}).get(node_id)
            node_num = node.get("num") if node else None
            # Evict from library in-memory dicts (under lock so helpers.py snapshot loop doesn't race)
            with connections_lock:
                if iface.nodes:
                    iface.nodes.pop(node_id, None)
                if node_num is not None and iface.nodesByNum:
                    iface.nodesByNum.pop(node_num, None)
            # Also purge from radio flash nodeDB so stale PKI keys don't persist across restarts
            iface.localNode.removeNode(node_id)
        except Exception as e:
            radio_errors.append(str(e))
    return jsonify({"ok": True, "radio_errors": radio_errors})


# ---------------------------------------------------------------------------
# Node actions
# ---------------------------------------------------------------------------

@bp.route("/api/node/<node_id>/traceroute", methods=["POST"])
def api_traceroute(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data     = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or request.args.get("radio_id")
    if radio_id:
        iface = get_iface_by_radio(radio_id)
        if not iface:
            return jsonify({"error": "Requested radio not connected"}), 503
    else:
        iface = get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    if not traceroute_lock.acquire(blocking=False):
        return jsonify({"error": "Another traceroute already in progress. Use /api/traceroute/reset to unlock."}), 429

    result = {}
    done = threading.Event()

    def on_receive(packet, interface):
        try:
            if interface is not iface:
                return
            if packet.get("fromId") == node_id:
                decoded = packet.get("decoded", {})
                if decoded.get("portnum") == "TRACEROUTE_APP":
                    tr = decoded.get("traceroute", {})
                    result["route"]       = tr.get("route", [])
                    result["routeBack"]   = tr.get("routeBack", [])
                    result["snrTowards"]  = tr.get("snrTowards", [])
                    result["snrBack"]     = tr.get("snrBack", [])
                    done.set()
        except Exception as e:
            log.warning(f"Traceroute callback: {e}")

    # Read configurable hop limit (default 7 if not set)
    try:
        hop_limit = int(iface.localNode.localConfig.lora.hop_limit) or 7
    except Exception:
        hop_limit = 7
    try:
        if CONFIG.get("silent_mode"):
            return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
        pub.subscribe(on_receive, "meshtastic.receive")
        iface.sendTraceRoute(node_id, hopLimit=hop_limit)
        done.wait(timeout=60)
    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive")
        except Exception:
            pass
        try:
            traceroute_lock.release()
        except RuntimeError:
            pass  # Already force-released by /api/traceroute/reset

    if not done.is_set():
        return jsonify({"error": "Timeout — node did not respond (60s)"}), 504

    my_name    = "You"
    dest_name  = get_node_name(node_id)
    route      = [my_name] + [resolve_node_name(n) for n in result.get("route", [])]     + [dest_name]
    route_back = [dest_name] + [resolve_node_name(n) for n in result.get("routeBack", [])] + [my_name]
    # SNR values are stored as int * 4 in the protobuf
    snr_towards = [round(s / 4, 1) for s in result.get("snrTowards", [])]
    snr_back    = [round(s / 4, 1) for s in result.get("snrBack", [])]
    # Node IDs for map path drawing (hex format matching frontend node IDs)
    local_id = "local"
    try:
        local_num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
        if local_num:
            local_id = f"!{local_num:08x}"
    except Exception:
        pass
    route_ids      = [local_id] + [f"!{int(n):08x}" for n in result.get("route", [])]     + [node_id]
    route_back_ids = [node_id]  + [f"!{int(n):08x}" for n in result.get("routeBack", [])] + [local_id]
    tr = {"route": route, "routeBack": route_back,
          "snrTowards": snr_towards, "snrBack": snr_back,
          "routeIds": route_ids, "routeBackIds": route_back_ids}
    try:
        save_traceroute(node_id, get_node_name(node_id), radio_id or "", tr)
    except Exception as e:
        log.warning(f"Failed to save traceroute history: {e}")
    return jsonify(tr)


@bp.route("/api/traceroute/reset", methods=["POST"])
def api_traceroute_reset():
    """Force-release the traceroute lock if it got stuck (e.g. node disconnected during TR)."""
    if traceroute_lock.locked():
        try:
            traceroute_lock.release()
            return jsonify({"ok": True, "msg": "Lock released"})
        except RuntimeError:
            pass
    return jsonify({"ok": True, "msg": "Lock was not held"})


@bp.route("/api/node/<node_id>/dm", methods=["POST"])
def api_dm(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data         = request.get_json(silent=True) or {}
    msg          = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "Empty message"}), 400
    radio_id_req = data.get("radio_id")
    if radio_id_req:
        iface = get_iface_by_radio(radio_id_req)
        if not iface:
            return jsonify({"error": "Requested radio not connected"}), 503
        radio_id = radio_id_req
    else:
        iface, radio_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        if CONFIG.get("silent_mode"):
            return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
        sent    = iface.sendText(msg, destinationId=node_id, wantAck=True)
        pkt_id  = sent.id if hasattr(sent, "id") else (sent.get("id") if isinstance(sent, dict) else None)
        chat_msg = {
            "id":        _next_msg_id(),
            "from_id":   "local",
            "from_name": "You",
            "to_id":     node_id,
            "to_name":   get_node_name(node_id),
            "channel":   0,
            "text":      msg,
            "ts":        int(time.time()),
            "sent":      True,
            "is_dm":     True,
            "status":    "pending",
            "radio_id":  radio_id,
        }
        with chat_lock:
            chat_messages.append(chat_msg)
        save_message(chat_msg)
        if pkt_id:
            with pending_acks_lock:
                pending_acks[pkt_id] = (chat_msg["id"], radio_id, time.time())
        push_to_sse(json.dumps(chat_msg))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/node/<node_id>/position", methods=["POST"])
def api_request_position(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data     = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or request.args.get("radio_id")
    if radio_id:
        iface = get_iface_by_radio(radio_id)
        if not iface:
            return jsonify({"error": "Requested radio not connected"}), 503
    else:
        iface = get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        if CONFIG.get("silent_mode"):
            return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
        iface.sendPosition(destinationId=node_id, wantResponse=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/node/<node_id>/info", methods=["POST"])
def api_node_info(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data     = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or request.args.get("radio_id")
    if radio_id:
        iface = get_iface_by_radio(radio_id)
        if not iface:
            return jsonify({"error": "Requested radio not connected"}), 503
    else:
        iface = get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503

    # Request fresh telemetry from the node and wait for the response
    done = threading.Event()

    def on_receive(packet, interface):
        try:
            if interface is not iface:
                return  # ignore packets from other radios
            if packet.get("fromId") == node_id:
                decoded = packet.get("decoded", {})
                if decoded.get("portnum") == "TELEMETRY_APP":
                    done.set()
        except Exception:
            pass

    try:
        if CONFIG.get("silent_mode"):
            return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
        pub.subscribe(on_receive, "meshtastic.receive")
        iface.sendTelemetry(destinationId=node_id, wantResponse=True,
                            telemetryType="device_metrics")
        done.wait(timeout=15)
    except Exception as e:
        log.warning(f"Info request error: {e}")
    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive")
        except Exception:
            pass

    # Read the (now updated) node data from iface.nodes
    try:
        nodes = iface.nodes or {}
        for node_num, node in nodes.items():
            user = node.get("user", {})
            if user.get("id") == node_id:
                pos     = node.get("position", {}) or {}
                metrics = node.get("deviceMetrics", {}) or {}
                env     = node.get("environmentMetrics", {}) or {}
                last_heard = node.get("lastHeard")
                last_heard_str = None
                if last_heard:
                    delta = int(time.time()) - last_heard
                    if delta < 60:
                        last_heard_str = f"{delta}s ago"
                    elif delta < 3600:
                        last_heard_str = f"{delta // 60}m ago"
                    elif delta < 86400:
                        last_heard_str = f"{delta // 3600}h ago"
                    else:
                        last_heard_str = f"{delta // 86400}d ago"
                uptime = metrics.get("uptimeSeconds")
                uptime_str = None
                if uptime:
                    h, m = divmod(uptime, 3600)
                    m, s = divmod(m, 60)
                    uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
                fresh = done.is_set()
                return jsonify({
                    "fresh":        fresh,
                    "long_name":    user.get("longName"),
                    "short_name":   user.get("shortName"),
                    "hw_model":     hw_model_name(user.get("hwModel")),
                    "role":         user.get("role"),
                    "snr":          node.get("snr"),
                    "rssi":         node.get("rssi"),
                    "hops_away":    node.get("hopsAway"),
                    "last_heard":   last_heard_str,
                    "battery":      metrics.get("batteryLevel"),
                    "voltage":      metrics.get("voltage"),
                    "channel_util": metrics.get("channelUtilization"),
                    "air_util_tx":  metrics.get("airUtilTx"),
                    "uptime":       uptime_str,
                    "latitude":     pos.get("latitude"),
                    "longitude":    pos.get("longitude"),
                    "altitude":     pos.get("altitude"),
                    "temperature":  env.get("temperature"),
                    "humidity":     env.get("relativeHumidity"),
                    "pressure":     env.get("barometricPressure"),
                })
        return jsonify({"error": "Node not found in radio memory"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/node/<node_id>/gps_history")
def api_node_gps_history(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    hours = request.args.get("hours", 24, type=int)
    hours = max(1, min(hours, 24 * 7))   # clamp 1h – 168h
    return jsonify(get_position_history(node_id, hours))


@bp.route("/api/traceroute/history")
def api_traceroute_history():
    limit = request.args.get("limit", 20, type=int)
    limit = max(1, min(limit, 50))
    return jsonify(get_traceroute_history(limit))


@bp.route("/api/db/gps_history/clear", methods=["POST"])
def api_clear_gps_history():
    from db import clear_position_history
    data    = request.get_json(silent=True) or {}
    node_id = data.get("node_id")   # optional — omit to clear all
    clear_position_history(node_id or None)
    return jsonify({"ok": True, "node_id": node_id or "all"})

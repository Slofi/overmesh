import concurrent.futures
import json
import queue
import time
import threading

from flask import Blueprint, Response, jsonify, request

from config import CONFIG
from cross import maybe_forward_mt_message
from db import save_message
from helpers import _next_msg_id, get_node_name, mt_node_id_from_num, push_to_sse
from mesh import get_any_iface, get_any_iface_with_id, get_iface_by_radio
from state import (
    chat_lock, chat_messages,
    pending_acks, pending_acks_lock,
    sse_clients, sse_lock, _sse_queue_last_ok,
)
import logging
log = logging.getLogger(__name__)

bp = Blueprint('chat', __name__)


@bp.route("/api/chat/channels")
def api_chat_channels():
    radio_id = request.args.get("radio_id")
    iface = get_iface_by_radio(radio_id) if radio_id else get_any_iface()
    if radio_id and not iface:
        return jsonify({"error": "Radio not connected"}), 503
    if not iface:
        return jsonify([{"index": 0, "name": "Primary", "role": 1}])
    try:
        result = []
        ln = getattr(iface, "localNode", None)
        if ln and hasattr(ln, "channels"):
            for ch in ln.channels:
                role = getattr(ch, "role", 0)
                if role == 0:
                    continue
                settings = getattr(ch, "settings", None)
                name     = (getattr(settings, "name", "") or "") if settings else ""
                index    = getattr(ch, "index", 0)
                result.append({
                    "index": index,
                    "name":  name or ("Primary" if index == 0 else f"CH{index}"),
                    "role":  role,
                })
        return jsonify(result or [{"index": 0, "name": "Primary", "role": 1}])
    except Exception as e:
        log.warning(f"Channel fetch: {e}")
        return jsonify([{"index": 0, "name": "Primary", "role": 1}])


@bp.route("/api/chat/messages")
def api_chat_messages():
    radio_id = request.args.get("radio_id")
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    with chat_lock:
        msgs = [
            dict(m) for m in chat_messages
            if not radio_id or m.get("radio_id") == radio_id
        ]
    msgs.sort(key=lambda m: m.get("ts", 0))
    return jsonify({"messages": msgs[-limit:]})


@bp.route("/api/chat/stream")
def api_chat_stream():
    q = queue.Queue(maxsize=100)
    with sse_lock:
        sse_clients.append(q)
        _sse_queue_last_ok[id(q)] = time.time()

    def generate():
        try:
            with chat_lock:
                history = list(chat_messages)
            for msg in history:
                yield f"data: {json.dumps({**msg, 'is_history': True})}\n\n"
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                try:
                    sse_clients.remove(q)
                except ValueError:
                    pass
                _sse_queue_last_ok.pop(id(q), None)

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"]      = "no-cache"
    resp.headers["X-Accel-Buffering"]  = "no"
    return resp


@bp.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    data    = request.get_json(silent=True) or {}
    text    = (data.get("text") or "").strip()
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    try:
        channel = max(0, min(7, int(data.get("channel", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid channel"}), 400
    dest_id = data.get("dest_id")
    if not text:
        return jsonify({"error": "Empty message"}), 400
    radio_id_req = data.get("radio_id")
    if radio_id_req:
        iface = get_iface_by_radio(radio_id_req)
        if not iface:
            return jsonify({"error": "Radio not connected"}), 503
        radio_id = radio_id_req
    else:
        iface, radio_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                iface.sendText, text,
                **({'destinationId': dest_id} if dest_id else {}),
                channelIndex=channel, wantAck=True
            )
            try:
                sent = fut.result(timeout=8)
            except concurrent.futures.TimeoutError:
                return jsonify({"error": "Radio send timed out — node may be unresponsive"}), 504
        pkt_id = sent.id if hasattr(sent, "id") else (sent.get("id") if isinstance(sent, dict) else None)
        # Resolve my own name
        my_name = "You"
        local_node = None
        local_node_id = None
        local_info = getattr(iface, "myInfo", None)
        local_num  = getattr(local_info, "my_node_num", None)
        local_node_key = mt_node_id_from_num(local_num)
        if local_node_key and iface.nodes:
            local_node = iface.nodes.get(local_node_key)
            if local_node:
                u = local_node.get("user", {})
                my_name = u.get("longName") or u.get("shortName") or "You"
                local_node_id = u.get("id")
        msg = {
            "id":        _next_msg_id(),
            "from_id":   "local",
            "from_name": my_name,
            "to_id":     dest_id if dest_id else None,
            "to_name":   get_node_name(dest_id) if dest_id else None,
            "channel":   channel,
            "text":      text,
            "ts":        int(time.time()),
            "sent":      True,
            "is_dm":     bool(dest_id),
            "status":    "pending",
            "radio_id":  radio_id,
        }
        with chat_lock:
            chat_messages.append(msg)
        save_message(msg)
        if pkt_id:
            with pending_acks_lock:
                pending_acks[pkt_id] = (msg["id"], radio_id, time.time())
        push_to_sse(json.dumps(msg))
        if not dest_id:
            forward_msg = {
                "from_id": local_node_id,
                "from_name": my_name,
                "channel": channel,
                "text": text,
                "is_dm": False,
                "radio_id": radio_id,
            }
            threading.Thread(target=maybe_forward_mt_message, args=(forward_msg,), daemon=True).start()
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

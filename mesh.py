"""
Meshtastic interface management — connect, receive, reconnect, helpers.
"""
import json
import logging
import os
import threading
import time

import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic import config_pb2
from pubsub import pub

from bot import handle_bot_command
from config import CONFIG, CONFIG_LOCK, DATA_DIR, save_config
from cross import maybe_forward_mt_message
from db import get_prefs_db, init_msgs_db, load_messages, save_message, update_message_status
from helpers import _next_msg_id, _radio_id_for_iface, get_node_name, push_to_sse
from sense import _sense_lock, _sense_state
from state import (
    chat_lock, chat_messages,
    connections, connections_lock,
    mt_last_heard, mt_last_heard_lock,
    pending_acks, pending_acks_lock,
    waypoints_cache, waypoints_lock,
    _tr_pending, _tr_pending_lock,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Meshtastic enum maps (protobuf int values ↔ display names)
# ---------------------------------------------------------------------------

DEVICE_ROLES = {
    value: name
    for name, value in sorted(
        config_pb2.Config.DeviceConfig.Role.items(),
        key=lambda kv: kv[1],
    )
}

LORA_REGIONS = {
    0: "UNSET", 1: "US", 2: "EU_433", 3: "EU_868", 4: "CN",
    5: "JP", 6: "ANZ", 7: "KR", 8: "TW", 9: "RU", 10: "IN",
    11: "NZ_865", 12: "TH", 13: "LORA_24", 14: "UA_433",
    15: "UA_868", 16: "MY_433", 17: "MY_919", 18: "SG_923",
}

MODEM_PRESETS = {
    0: "LONG_FAST",
    1: "LONG_SLOW",
    2: "VERY_LONG_SLOW",
    3: "MEDIUM_SLOW",
    4: "MEDIUM_FAST",
    5: "SHORT_SLOW",
    6: "SHORT_FAST",
    7: "LONG_MODERATE",
    8: "SHORT_TURBO",
}


def _packet_rx_ts(packet):
    try:
        ts = int(packet.get("rxTime") or 0)
    except (TypeError, ValueError):
        ts = 0
    now = int(time.time())
    return max(0, min(ts or now, now))


# ---------------------------------------------------------------------------
# Packet handler
# ---------------------------------------------------------------------------

def on_text_receive(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum", "")

        # The meshtastic library only updates lastHeard for TEXT + NODEINFO packets.
        # Update it here for ALL packet types so Nodes tab reflects any received response.
        _fid = packet.get("fromId")
        if _fid:
            _new_ts = _packet_rx_ts(packet)
            _rid = _radio_id_for_iface(interface)
            if _rid:
                with mt_last_heard_lock:
                    mt_last_heard[(_rid, _fid)] = _new_ts
            _nodes = interface.nodes or {}
            if _fid in _nodes:
                _nodes[_fid]["lastHeard"] = _new_ts
            else:
                for _node in _nodes.values():
                    if (_node or {}).get("user", {}).get("id") == _fid:
                        _node["lastHeard"] = _new_ts
                        break
            # Always push SSE for non-ACK packets so frontend can refresh map colours.
            # The frontend uses a debounced loadLive() as fallback for nodes without user.id.
            if portnum != "ROUTING_APP":
                push_to_sse(json.dumps({"type": "node_last_heard", "from_id": _fid, "ts": _new_ts}))

        if portnum == "ROUTING_APP":
            request_id = decoded.get("requestId")
            if request_id:
                with pending_acks_lock:
                    ack_entry = pending_acks.pop(request_id, None)
                if ack_entry:
                    msg_id, r_id, _ts = ack_entry
                    error   = decoded.get("routing", {}).get("errorReason", "NONE")
                    success = (error == "NONE")
                    update_message_status(msg_id, "delivered" if success else "failed", r_id)
                    push_to_sse(json.dumps({"type": "ack", "msg_id": msg_id, "success": success}))
            return

        # Capture any packet during active window OR passive listening mode
        with _sense_lock:
            _should_capture = (
                (_sense_state["active"] and time.time() < _sense_state["window_end"])
                or _sense_state["passive"]
            )
        if _should_capture:
            from_id   = packet.get("fromId", "?")
            snr       = packet.get("rxSnr")
            hop_limit = packet.get("hopLimit")
            hop_start = packet.get("hopStart")
            hops      = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
            name     = get_node_name(from_id)
            pos      = decoded.get("position", {}) or {}
            tel      = decoded.get("telemetry", {}) or {}
            dev      = tel.get("deviceMetrics", {}) or {}
            env      = tel.get("environmentMetrics", {}) or {}
            pwr      = tel.get("powerMetrics", {}) or {}
            user     = decoded.get("user", {}) or {}
            tr       = decoded.get("traceroute", {}) or {}
            nb       = decoded.get("neighborinfo", {}) or {}
            tr_route = tr.get("route", [])
            nb_list  = nb.get("neighbors", [])
            entry = {
                "from_id":        from_id,
                "name":           name,
                "portnum":        portnum,
                "snr":            snr,
                "hops":           hops,
                "lat":            pos.get("latitude") or None,
                "lon":            pos.get("longitude") or None,
                "alt":            pos.get("altitude") or None,
                "sats":           pos.get("satsInView") or None,
                "speed":          pos.get("speed") or None,
                "battery":        dev.get("batteryLevel") or None,
                "voltage":        dev.get("voltage") or None,
                "ch_util":        dev.get("channelUtilization") or None,
                "air_util":       dev.get("airUtilTx") or None,
                "hw_model":       user.get("hwModel") or None,
                "role":           user.get("role") or None,
                "short_name":     user.get("shortName") or None,
                "temp":           env.get("temperature") or None,
                "humidity":       env.get("relativeHumidity") or None,
                "pressure":       env.get("barometricPressure") or None,
                "route":          [get_node_name(f"!{r:08x}") or f"!{r:08x}" for r in tr_route] if tr_route else None,
                "neighbor_count": len(nb_list) if nb_list else None,
                "text":           decoded.get("text") if portnum == "TEXT_MESSAGE_APP" else None,
                "pkt_id":         int(packet.get("id", 0) or 0),
                "ts":             int(time.time()),
            }
            with _sense_lock:
                existing = next((r for r in _sense_state["responses"] if r["from_id"] == from_id), None)
                if existing:
                    existing.update({k: v for k, v in entry.items() if v is not None})
                    node_out = dict(existing)
                else:
                    if len(_sense_state["responses"]) >= 500:
                        _sense_state["responses"].sort(key=lambda r: r.get("ts", 0))
                        _sense_state["responses"].pop(0)
                    _sense_state["responses"].append(entry)
                    node_out = dict(entry)
            push_to_sse(json.dumps({"type": "sense_response", "node": node_out,
                                    "radio_id": _radio_id_for_iface(interface)}))

        # Signal pending traceroute if this is the awaited TRACEROUTE_APP response.
        # This is the authoritative callback path — does not rely on per-request
        # pub.subscribe/unsubscribe which can miss packets after the first TR.
        if portnum == "TRACEROUTE_APP":
            with _tr_pending_lock:
                pend = _tr_pending
                if (pend["done"] is not None and
                        not pend["done"].is_set() and
                        packet.get("fromId") == pend["node_id"]):
                    _rid = _radio_id_for_iface(interface)
                    if pend["radio_id"] is None or _rid == pend["radio_id"]:
                        tr_data = decoded.get("traceroute", {}) or {}
                        pend["result"].update({
                            "route":      tr_data.get("route", []),
                            "routeBack":  tr_data.get("routeBack", []),
                            "snrTowards": tr_data.get("snrTowards", []),
                            "snrBack":    tr_data.get("snrBack", []),
                        })
                        pend["done"].set()
                        log.debug(f"[TR] done.set() for {pend['node_id']} via radio {_rid!r}")

        if portnum == "WAYPOINT_APP":
            try:
                wp_data = decoded.get("waypoint", {})
                wp_id   = wp_data.get("id", 0) or 0
                wp_name = wp_data.get("name", "Waypoint") or "Waypoint"
                wp_desc = wp_data.get("description", "") or ""
                lat_i   = wp_data.get("latitudeI") or wp_data.get("latitude_i", 0) or 0
                lon_i   = wp_data.get("longitudeI") or wp_data.get("longitude_i", 0) or 0
                wp_lat  = lat_i * 1e-7
                wp_lon  = lon_i * 1e-7
                wp_exp  = wp_data.get("expire", 0) or 0
                wp_icon = wp_data.get("icon", 0) or 0
                try:
                    marker_emoji = chr(wp_icon) if wp_icon else "📍"
                except (ValueError, OverflowError):
                    marker_emoji = "📍"
                from_id = packet.get("fromId", "")
                r_id    = _radio_id_for_iface(interface)
                if wp_id:
                    if wp_exp == 1:
                        # DB delete first: if we crash between the two operations,
                        # restarting from DB is correct (cache rebuilds from DB).
                        with get_prefs_db() as conn:
                            conn.cursor().execute("DELETE FROM waypoints WHERE id=?", (wp_id,))
                        with waypoints_lock:
                            waypoints_cache.pop(wp_id, None)
                        push_to_sse(json.dumps({"type": "waypoint_deleted", "id": wp_id}))
                    elif wp_lat != 0 or wp_lon != 0:
                        wp_entry = {
                            "id": wp_id, "name": wp_name, "description": wp_desc,
                            "lat": wp_lat, "lon": wp_lon, "expire": wp_exp,
                            "icon": wp_icon, "from_id": from_id, "radio_id": r_id,
                            "ts": int(time.time()), "channel_index": 0, "destination_id": None,
                            "marker_emoji": marker_emoji
                        }
                        with waypoints_lock:
                            waypoints_cache[wp_id] = wp_entry
                        with get_prefs_db() as conn:
                            conn.cursor().execute(
                                "INSERT OR REPLACE INTO waypoints "
                                "(id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,channel_index,destination_id,marker_emoji) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (wp_id, wp_name, wp_desc, wp_lat, wp_lon, wp_exp, wp_icon,
                                 from_id, r_id, int(time.time()), 0, None, marker_emoji)
                            )
                        push_to_sse(json.dumps({"type": "waypoint", "waypoint": wp_entry}))
            except Exception as e:
                log.warning(f"Mark parse: {e}")
            return

        if portnum != "TEXT_MESSAGE_APP":
            return
        from_id  = packet.get("fromId", "?")
        to_id    = packet.get("toId", "^all")
        channel  = packet.get("channel", 0)
        text     = decoded.get("text", "")
        ts       = packet.get("rxTime") or int(time.time())
        snr      = packet.get("rxSnr")
        hop_limit = packet.get("hopLimit")
        hop_start = packet.get("hopStart")
        hops     = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
        is_dm    = to_id != "^all"
        radio_id = _radio_id_for_iface(interface)
        msg = {
            "id":           _next_msg_id(),
            "from_id":      from_id,
            "from_name":    get_node_name(from_id),
            "to_id":        to_id,
            "to_name":      get_node_name(to_id) if is_dm else "All",
            "channel":      channel,
            "text":         text,
            "ts":           ts,
            "snr":          snr,
            "sent":         False,
            "is_dm":        is_dm,
            "radio_id":     radio_id,
            "pkt_id":       int(packet.get("id", 0)),
            "is_emoji":     bool(int(decoded.get("emoji", 0))),
            "reply_pkt_id": int(decoded.get("replyId", 0)),
            "hop_start":    hop_start,
            "hop_limit":    hop_limit,
            "hops":         hops,
        }
        with chat_lock:
            chat_messages.append(msg)
            if len(chat_messages) > 500:
                chat_messages.pop(0)
        save_message(msg)
        push_to_sse(json.dumps(msg))
        threading.Thread(target=maybe_forward_mt_message, args=(dict(msg),), daemon=True).start()
        handle_bot_command(packet, interface)
    except Exception as e:
        log.warning(f"Chat receive error: {e}")


def on_connection_lost(interface, topic=pub.AUTO_TOPIC):
    lost_id = None
    lost_name = None
    with _sense_lock:
        sense_active = _sense_state["active"]
    with connections_lock:
        for node_id, state in connections.items():
            if state.get("iface") is interface:
                if sense_active:
                    log.warning(f"[{node_id}] Connection lost during Sense (USB-CDC reset) — suppressing UI event, reconnecting.")
                else:
                    log.warning(f"[{node_id}] Connection lost.")
                state["status"] = "disconnected"
                state["iface"] = None
                lost_id = node_id
                lost_name = state.get("config", {}).get("name", node_id)
                break
    if lost_id and not sense_active:
        push_to_sse(json.dumps({"type": "node_status", "radio_id": lost_id,
                                "status": "disconnected", "name": lost_name}))
    try:
        interface.close()
    except Exception:
        pass
    # Fallback: explicitly close the underlying serial stream in case close() silently failed.
    # Without this, the FD stays open and the exclusive port lock is never released,
    # causing every subsequent reconnect attempt to fail with EAGAIN.
    try:
        stream = getattr(interface, 'stream', None)
        if stream and hasattr(stream, 'close'):
            stream.close()
    except Exception:
        pass
    if lost_id and sense_active:
        # Reconnect immediately (1s settle time for USB) instead of waiting for reconnect_loop
        threading.Timer(1.0, _reconnect_disconnected).start()


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------

def find_port_by_usb_serial(usb_serial):
    """Find the current tty device path for a given USB hardware serial number."""
    import serial.tools.list_ports
    for p in serial.tools.list_ports.comports():
        if p.serial_number == usb_serial:
            return p.device
    return None


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def connect_node(node_cfg):
    node_id = node_cfg["id"]
    with connections_lock:
        connections[node_id] = {"iface": None, "status": "connecting", "config": node_cfg}
    node_type = node_cfg.get("type", "serial")
    iface = None
    if node_type == "tcp":
        host     = node_cfg.get("host", "").strip()
        tcp_port = int(node_cfg.get("tcp_port") or 4403)
        if not host:
            log.warning(f"[{node_id}] TCP node has no host configured")
            with connections_lock:
                connections[node_id]["status"] = "disconnected"
            return
        log.info(f"[{node_id}] Connecting to {host}:{tcp_port} (TCP)...")
        try:
            iface = meshtastic.tcp_interface.TCPInterface(hostname=host, portNumber=tcp_port)
        except Exception as e:
            log.warning(f"[{node_id}] TCP connection failed: {e}")
            with connections_lock:
                connections[node_id]["status"] = "disconnected"
            return
    else:
        usb_serial = node_cfg.get("usb_serial")
        if usb_serial:
            port = find_port_by_usb_serial(usb_serial)
            if not port:
                log.info(f"[{node_id}] Device (USB serial {usb_serial}) not found, will retry")
                with connections_lock:
                    connections[node_id]["status"] = "disconnected"
                return
        else:
            port = node_cfg.get("port", "")
        log.info(f"[{node_id}] Connecting to {port} (serial)...")
        try:
            iface = meshtastic.serial_interface.SerialInterface(port)
        except Exception as e:
            log.warning(f"[{node_id}] Serial connection failed: {e}")
            with connections_lock:
                connections[node_id]["status"] = "disconnected"
            return
    try:
        msgs_db = None
        try:
            local_num = getattr(iface.myInfo, "my_node_num", None)
            if local_num:
                hex_id  = hex(local_num)[2:]
                msgs_db = os.path.join(DATA_DIR, f"overmesh_msgs_{hex_id}.db")
                init_msgs_db(msgs_db)
        except Exception as e:
            log.warning(f"[{node_id}] Could not set up per-radio DB: {e}")
        with connections_lock:
            connections[node_id]["iface"] = iface
            connections[node_id]["status"] = "connected"
            if msgs_db:
                connections[node_id]["msgs_db"] = msgs_db
        push_to_sse(json.dumps({"type": "node_status", "radio_id": node_id,
                                "status": "connected", "name": node_cfg.get("name", node_id)}))
        if msgs_db:
            node_cfg_live = next((n for n in CONFIG.get("nodes", []) if n["id"] == node_id), None)
            if node_cfg_live and node_cfg_live.get("msgs_db") != msgs_db:
                with CONFIG_LOCK:
                    node_cfg_live["msgs_db"] = msgs_db
                    save_config()
        log.info(f"[{node_id}] Connected.")
        if msgs_db:
            try:
                loaded = load_messages(node_id)
                with chat_lock:
                    chat_messages[:] = [m for m in chat_messages if m.get("radio_id") != node_id]
                    chat_messages.extend(loaded)
                    chat_messages.sort(key=lambda m: m.get("ts", 0))
                    if len(chat_messages) > 500:
                        chat_messages[:] = chat_messages[-500:]
                log.info(f"[{node_id}] msgs_db={msgs_db}, loaded {len(loaded)} messages")
            except Exception as e:
                log.warning(f"[{node_id}] Could not load messages: {e}")
    except Exception as e:
        log.warning(f"[{node_id}] Connection failed: {e}")
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
        with connections_lock:
            connections[node_id]["status"] = "disconnected"


def _is_iface_alive(iface):
    """Check if a serial interface is still open."""
    try:
        if hasattr(iface, 'stream') and iface.stream:
            return iface.stream.is_open
        return iface.myInfo is not None
    except Exception:
        return False


def _reconnect_disconnected():
    """Trigger immediate reconnect for any nodes currently marked disconnected."""
    with connections_lock:
        to_reconnect = [
            state["config"] for state in connections.values()
            if state.get("status") == "disconnected" and state.get("config")
        ]
    for node_cfg in to_reconnect:
        threading.Thread(target=connect_node, args=(node_cfg,), daemon=True).start()


def _check_and_reconnect_iface(iface):
    """Post-Sense health check: handles both clean and silent disconnects for a given iface.
    - If iface is still alive: does nothing.
    - If silent disconnect (status=connected but iface dead): marks disconnected, pushes SSE.
    - Either way, calls _reconnect_disconnected() to start reconnect if needed.
    """
    if _is_iface_alive(iface):
        return
    # Silent disconnect — status is still "connected" but iface is dead
    with connections_lock:
        for node_id, state in connections.items():
            if state.get("iface") is iface and state.get("status") == "connected":
                log.warning(f"[{node_id}] Post-Sense: silent disconnect detected, forcing reconnect.")
                state["status"] = "disconnected"
                state["iface"] = None
                push_to_sse(json.dumps({"type": "node_status", "radio_id": node_id,
                                        "status": "disconnected",
                                        "name": state.get("config", {}).get("name", node_id)}))
                break
    try:
        iface.close()
    except Exception:
        pass
    _reconnect_disconnected()


def health_check_loop():
    """Detect silent disconnects that don't fire the connection.lost event."""
    while True:
        time.sleep(5)
        now = time.time()
        with pending_acks_lock:
            stale = [k for k, v in pending_acks.items() if now - v[2] > 300]
            for k in stale:
                del pending_acks[k]
        with connections_lock:
            to_check = [
                (nid, state.get("iface"))
                for nid, state in connections.items()
                if state.get("status") == "connected" and state.get("iface")
            ]
        reconnect_needed = False
        for node_id, iface in to_check:
            if not iface or _is_iface_alive(iface):
                continue
            log.warning(f"[{node_id}] Health check: silent disconnect detected, forcing reconnect.")
            # Atomically claim ownership before touching the iface so no other
            # thread can use or double-close it.
            node_name = node_id
            with connections_lock:
                if connections.get(node_id, {}).get("iface") is not iface:
                    continue  # another thread already handled this iface
                connections[node_id]["status"] = "disconnected"
                connections[node_id]["iface"] = None
                node_name = connections[node_id].get("config", {}).get("name", node_id)
            reconnect_needed = True
            push_to_sse(json.dumps({
                "type": "node_status",
                "radio_id": node_id,
                "status": "disconnected",
                "name": node_name,
            }))
            try:
                iface.close()
            except Exception:
                pass
            try:
                stream = getattr(iface, 'stream', None)
                if stream and hasattr(stream, 'close'):
                    stream.close()
            except Exception:
                pass
        if reconnect_needed:
            _reconnect_disconnected()


def reconnect_loop():
    while True:
        time.sleep(5)
        with connections_lock:
            to_reconnect = [
                cfg for cfg in CONFIG["nodes"]
                if cfg.get("enabled", True) and connections.get(cfg["id"], {}).get("status") not in ("connected", "connecting")
            ]
        for node_cfg in to_reconnect:
            threading.Thread(target=connect_node, args=(node_cfg,), daemon=True).start()


# ---------------------------------------------------------------------------
# Interface accessors
# ---------------------------------------------------------------------------

def get_any_iface():
    with connections_lock:
        for state in connections.values():
            if state.get("status") == "connected" and state.get("iface"):
                return state["iface"]
    return None


def get_any_iface_with_id():
    """Return (iface, radio_id) for the first connected radio."""
    with connections_lock:
        for rid, state in connections.items():
            if state.get("status") == "connected" and state.get("iface"):
                return state["iface"], rid
    return None, None


def get_iface_by_radio(radio_id):
    with connections_lock:
        state = connections.get(radio_id)
        if state and state.get("status") == "connected":
            return state.get("iface")
    return None


def resolve_node_name(num):
    """Resolve an integer node number to a short name."""
    hex_id = f"!{num:08x}"
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                node = iface.nodes.get(num) or iface.nodes.get(str(num))
                if not node:
                    for n in iface.nodes.values():
                        if n.get("user", {}).get("id") == hex_id:
                            node = n
                            break
                if node:
                    user = node.get("user", {})
                    return user.get("shortName") or user.get("longName") or hex_id
    return hex_id

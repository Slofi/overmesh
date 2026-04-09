import json
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from config import _valid_node_id
from db import get_prefs_db, save_message
from helpers import _next_msg_id, get_node_name, push_to_sse
from mesh import get_any_iface_with_id, get_iface_by_radio
from state import (
    chat_lock, chat_messages,
    waypoints_cache, waypoints_lock,
)
import logging
log = logging.getLogger(__name__)

bp = Blueprint('waypoints', __name__)


@bp.route("/api/waypoints")
def api_get_waypoints():
    with waypoints_lock:
        return jsonify(list(waypoints_cache.values()))


@bp.route("/api/waypoints/send", methods=["POST"])
def api_send_waypoint():
    data = request.get_json(silent=True) or {}
    log.info(f"Mark send received: destination_ids={data.get('destination_ids')!r}")
    name = (data.get("name") or "").strip()[:30]
    try:
        lat = float(data.get("lat", 0))
        lon = float(data.get("lon", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400
    # Append coordinates so they're visible in external apps (e.g. Android MT)
    coord_str = f"\n{lat:.4f},{lon:.4f}"
    raw_desc = (data.get("description") or "").strip()
    desc = raw_desc[:100 - len(coord_str)] + coord_str
    marker_emoji = (data.get("marker_emoji") or "📍").strip() or "📍"
    radio_id     = data.get("radio_id")
    try:
        channel_idx  = max(0, min(7, int(data.get("channel_index", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid channel_index"}), 400
    # Accept either destination_ids (list) or legacy destination_id (single)
    dest_ids_raw = data.get("destination_ids")
    if dest_ids_raw is not None and not isinstance(dest_ids_raw, list):
        return jsonify({"error": "destination_ids must be a list"}), 400
    if isinstance(dest_ids_raw, list):
        if len(dest_ids_raw) == 0:
            return jsonify({"error": "destination_ids must not be empty"}), 400
        dest_ids = [d for d in dest_ids_raw if d and _valid_node_id(d)]
        if not dest_ids:
            return jsonify({"error": "No valid destination IDs in destination_ids"}), 400
    else:
        single = (data.get("destination_id") or "").strip() or None
        dest_ids = [single] if single and _valid_node_id(single) else None
    if not name:
        return jsonify({"error": "Name required"}), 400
    if lat == 0 and lon == 0:
        return jsonify({"error": "Valid coordinates required"}), 400
    if radio_id:
        iface = get_iface_by_radio(radio_id)
        r_id  = radio_id if iface else None
        if not iface:
            iface, r_id = get_any_iface_with_id()
    else:
        iface, r_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        wp_id_req = data.get("wp_id")
        wp_id_val = int(wp_id_req) if wp_id_req else int(time.time() * 1000) % (2 ** 31)
        icon_cp = 0
        try:
            icon_cp = ord(marker_emoji[0])
        except (TypeError, IndexError):
            pass
        # Send one packet per destination (or broadcast if no specific dest)
        targets = dest_ids if dest_ids else [None]
        wp = mesh_pb2.Waypoint()
        wp.id          = wp_id_val
        wp.name        = name
        wp.description = desc
        wp.latitude_i  = int(lat * 1e7)
        wp.longitude_i = int(lon * 1e7)
        wp.expire      = 0
        wp.icon        = icon_cp
        serialized = wp.SerializeToString()
        for dest in targets:
            try:
                iface.sendData(
                    serialized,
                    portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                    destinationId=dest or "^all",
                    wantAck=False,
                    channelIndex=channel_idx
                )
                log.info(f"Mark send OK → dest={dest or '^all'} wp_id={wp_id_val}")
            except Exception as send_err:
                log.warning(f"Mark send FAILED to {dest}: {send_err}")
        dest_ids_json = json.dumps(dest_ids) if dest_ids else None
        primary_dest  = dest_ids[0] if dest_ids else None  # stable legacy field, not loop variable
        wp_entry = {
            "id": wp_id_val, "name": name, "description": desc,
            "lat": lat, "lon": lon, "expire": 0, "icon": icon_cp,
            "from_id": "local", "radio_id": r_id or "", "ts": int(time.time()),
            "channel_index": channel_idx, "destination_id": primary_dest,
            "destination_ids": dest_ids, "marker_emoji": marker_emoji
        }
        with waypoints_lock:
            waypoints_cache[wp_id_val] = wp_entry
        with get_prefs_db() as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO waypoints "
                "(id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,channel_index,destination_id,destination_ids,marker_emoji) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wp_id_val, name, desc, lat, lon, 0, icon_cp, "local", r_id or "", int(time.time()),
                 channel_idx, primary_dest, dest_ids_json, marker_emoji)
            )
        push_to_sse(json.dumps({"type": "waypoint", "waypoint": wp_entry}))
        # Send notification message on same channel/destination
        try:
            local_info = getattr(iface, "myInfo", None)
            local_num  = getattr(local_info, "my_node_num", None)
            my_name    = "?"
            local_key  = ("!" + hex(local_num)[2:]) if local_num else None
            if local_key and iface.nodes:
                local_node = iface.nodes.get(local_key)
                if local_node:
                    u = local_node.get("user", {})
                    my_name = u.get("longName") or u.get("shortName") or "?"
            dt_str     = datetime.now().strftime("%H:%M %d.%m.%Y")
            notif_text = f"📍 Mark \"{name}\" by {my_name} at {lat:.4f},{lon:.4f} — {dt_str}"
            notif_targets = dest_ids if dest_ids else [None]
            for nd in notif_targets:
                if nd:
                    iface.sendText(notif_text, destinationId=nd, channelIndex=channel_idx, wantAck=False)
                else:
                    iface.sendText(notif_text, channelIndex=channel_idx, wantAck=False)
            notif_msg = {
                "id": _next_msg_id(), "from_id": "local", "from_name": my_name,
                "to_id": primary_dest if primary_dest else None,
                "to_name": get_node_name(primary_dest) if primary_dest else None,
                "channel": channel_idx, "text": notif_text,
                "ts": int(time.time()), "sent": True,
                "is_dm": bool(primary_dest), "status": "sent", "radio_id": r_id or "",
            }
            with chat_lock:
                chat_messages.append(notif_msg)
            save_message(notif_msg)
            push_to_sse(json.dumps(notif_msg))
        except Exception as notif_err:
            log.warning(f"Mark notification failed: {notif_err}")
        return jsonify({"ok": True, "id": wp_id_val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/waypoints/<int:wp_id>", methods=["PUT"])
def api_edit_waypoint(wp_id):
    with waypoints_lock:
        wp_data = waypoints_cache.get(wp_id)
    if not wp_data:
        return jsonify({"error": "Mark not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    coord_str = f"\n{wp_data['lat']:.4f},{wp_data['lon']:.4f}"
    raw_desc  = (data.get("description") or "").strip()
    desc      = raw_desc[:100 - len(coord_str)] + coord_str
    marker_emoji = (data.get("marker_emoji") or "📍").strip() or "📍"
    radio_id  = data.get("radio_id") or wp_data.get("radio_id") or ""
    iface     = get_iface_by_radio(radio_id) if radio_id else None
    if not iface:
        iface, radio_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        icon_cp = 0
        try:
            icon_cp = ord(marker_emoji[0])
        except (TypeError, IndexError):
            pass
        new_ch_idx = data.get("channel_index")
        try:
            ch_idx = max(0, min(7, int(new_ch_idx))) if new_ch_idx is not None else (wp_data.get("channel_index") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid channel_index"}), 400
        # Accept destination_ids list or fall back to stored value
        dest_ids_raw = data.get("destination_ids")
        if dest_ids_raw is not None:
            dest_ids = [d for d in dest_ids_raw if d and _valid_node_id(d)] if isinstance(dest_ids_raw, list) else None
        else:
            dest_ids = wp_data.get("destination_ids")
        dest = dest_ids[0] if dest_ids else None
        try:
            wp             = mesh_pb2.Waypoint()
            wp.id          = wp_id
            wp.name        = name
            wp.description = desc
            wp.latitude_i  = int(wp_data["lat"] * 1e7)
            wp.longitude_i = int(wp_data["lon"] * 1e7)
            wp.expire      = 0
            wp.icon        = icon_cp
            serialized = wp.SerializeToString()
            targets = dest_ids if dest_ids else [None]
            for d in targets:
                iface.sendData(
                    serialized,
                    portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                    destinationId=d or "^all",
                    wantAck=False,
                    channelIndex=ch_idx
                )
            log.info(f"Mark edit send OK → targets={targets} wp_id={wp_id}")
        except Exception as send_err:
            log.warning(f"Mark edit send FAILED: {send_err}")
            raise
        dest_ids_json = json.dumps(dest_ids) if dest_ids else None
        updated = dict(wp_data, name=name, description=desc,
                       marker_emoji=marker_emoji, icon=icon_cp,
                       channel_index=ch_idx, destination_id=dest,
                       destination_ids=dest_ids)
        with waypoints_lock:
            waypoints_cache[wp_id] = updated
        with get_prefs_db() as conn:
            conn.cursor().execute(
                "UPDATE waypoints SET name=?,description=?,marker_emoji=?,icon=?,channel_index=?,destination_id=?,destination_ids=? WHERE id=?",
                (name, desc, marker_emoji, icon_cp, ch_idx, dest, dest_ids_json, wp_id)
            )
        push_to_sse(json.dumps({"type": "waypoint", "waypoint": updated}))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/waypoints/<int:wp_id>", methods=["DELETE"])
def api_delete_waypoint(wp_id):
    with waypoints_lock:
        wp_data = waypoints_cache.pop(wp_id, None)
    if not wp_data:
        return jsonify({"error": "Mark not found"}), 404
    with get_prefs_db() as conn:
        conn.cursor().execute("DELETE FROM waypoints WHERE id=?", (wp_id,))
    # Send mesh deletion packet on the same channel/destination it was originally sent to
    try:
        radio_id = (wp_data.get("radio_id") or "").strip() if wp_data else ""
        iface = get_iface_by_radio(radio_id) if radio_id else None
        if not iface:
            iface, _ = get_any_iface_with_id()
        if iface and wp_data:
            from meshtastic.protobuf import mesh_pb2, portnums_pb2
            ch_idx   = wp_data.get("channel_index") or 0
            dest_ids = wp_data.get("destination_ids")
            targets  = dest_ids if dest_ids else [wp_data.get("destination_id") or None]
            wp = mesh_pb2.Waypoint()
            wp.id          = wp_id
            wp.name        = wp_data.get("name", "")
            wp.latitude_i  = int(wp_data.get("lat", 0) * 1e7)
            wp.longitude_i = int(wp_data.get("lon", 0) * 1e7)
            wp.expire      = 1  # Meshtastic protocol: expire=1 means delete
            serialized = wp.SerializeToString()
            for dest in targets:
                iface.sendData(
                    serialized,
                    portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                    destinationId=dest or "^all",
                    wantAck=False,
                    channelIndex=ch_idx
                )
                log.info(f"Mark delete → dest={dest or '^all'} wp_id={wp_id} ch={ch_idx}")
    except Exception as e:
        log.warning(f"Mark mesh delete: {e}")
    push_to_sse(json.dumps({"type": "waypoint_deleted", "id": wp_id}))
    return jsonify({"ok": True})


@bp.route("/api/waypoints/<int:wp_id>/rebroadcast", methods=["POST"])
def api_rebroadcast_waypoint(wp_id):
    with waypoints_lock:
        wp_data = waypoints_cache.get(wp_id)
    if not wp_data:
        return jsonify({"error": "Mark not found"}), 404
    try:
        radio_id = (wp_data.get("radio_id") or "").strip()
        iface = get_iface_by_radio(radio_id) if radio_id else None
        if not iface:
            iface, _ = get_any_iface_with_id()
        if not iface:
            return jsonify({"error": "No radio connected"}), 503
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        marker_emoji = wp_data.get("marker_emoji") or "📍"
        wp             = mesh_pb2.Waypoint()
        wp.id          = wp_id
        wp.name        = wp_data.get("name", "")
        wp.description = wp_data.get("description", "")
        wp.latitude_i  = int(wp_data.get("lat", 0) * 1e7)
        wp.longitude_i = int(wp_data.get("lon", 0) * 1e7)
        wp.expire      = 0
        try:
            wp.icon = ord(marker_emoji[0])
        except (TypeError, IndexError):
            pass
        ch_idx   = wp_data.get("channel_index") or 0
        dest_ids = wp_data.get("destination_ids")
        targets  = dest_ids if dest_ids else [wp_data.get("destination_id") or None]
        for dest in targets:
            iface.sendData(
                wp.SerializeToString(),
                portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                destinationId=dest or "^all",
                wantAck=False,
                channelIndex=ch_idx
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

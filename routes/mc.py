"""
MeshCore API routes.
"""
import io
import hashlib
import os
import threading
import time
import logging
import json
from urllib.parse import urlencode

import qrcode
import qrcode.image.svg

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from cross import maybe_forward_mc_message
from helpers import push_to_sse
from mesh_mc import (send_chan_msg, send_dm, send_advert, refresh_contacts,
                     get_device_info, set_radio_params, set_tx_power,
                     set_device_name, set_device_coords, set_advert_loc_policy,
                     reboot_device, reboot_device_dtr, get_channels, set_channel,
                     req_node_status, get_stats, remove_mc_contact,
                     send_trace_broadcast, import_mc_contact, enable_mc_debug,
                     get_mc_contact_archive,
                     set_contact_path, remote_repeater_read,
                     remote_repeater_command)
from db import (
    delete_mc_channel_messages,
    get_mc_ignored,
    load_mc_messages,
    save_mc_message,
    set_mc_ignored,
)
from state import mc_connections, mc_connections_lock

# Per-radio scan state: radio_id → timer thread
_scan_timers: dict = {}
_scan_lock = threading.Lock()

MC_SCAN_WINDOW = 60  # seconds

log = logging.getLogger(__name__)
bp  = Blueprint("mc", __name__)



def _qr_svg(text):
    """Generate a QR code SVG using the qrcode library."""
    import re
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8").lstrip()
    # Strip XML declaration for inline embedding
    if svg.startswith("<?xml"):
        svg = svg[svg.index("<svg"):]
    # Replace fixed mm dimensions with 100% — viewBox is preserved so it scales correctly
    svg = re.sub(r'width="[\d.]+mm"\s+height="[\d.]+mm"', 'width="100%" height="100%"', svg, count=1)
    # Inject white background rect after the opening <svg ...> tag
    svg = re.sub(r'(<svg[^>]+>)', r'\1<rect width="100%" height="100%" fill="#fff"/>', svg, count=1)
    return svg



def _validate_mc_radio_params(data, include_repeat=False):
    try:
        params = {
            "freq": float(data["freq"]),
            "bw": float(data["bw"]),
            "sf": int(data["sf"]),
            "cr": int(data["cr"]),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Invalid params: {e}")
    if not (7 <= params["sf"] <= 12):
        raise ValueError("sf must be 7-12")
    if not (5 <= params["cr"] <= 8):
        raise ValueError("cr must be 5-8")
    if not (1 <= params["bw"] <= 1000):
        raise ValueError("bw out of range (1-1000 kHz)")
    if not (400 <= params["freq"] <= 950):
        raise ValueError("freq out of range (400-950 MHz)")
    if include_repeat and "repeat" in data:
        repeat = int(data["repeat"])
        if repeat not in (0, 1):
            raise ValueError("repeat must be 0 or 1")
        params["repeat"] = repeat
    return params


def _mc_contact_last_seen_ts(contact, now=None):
    now = int(time.time()) if now is None else int(now)
    max_future = now + 300
    for key in ("last_heard_ts", "last_seen_ts", "last_advert", "lastmod"):
        raw = (contact or {}).get(key)
        if not raw:
            continue
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 < ts <= max_future:
            return ts
    return 0


def _mc_contact_source_state(pubkey, live_contacts, archive_contacts):
    in_live = pubkey in (live_contacts or {})
    in_archive = pubkey in (archive_contacts or {})
    if in_live and in_archive:
        return "both"
    if in_live:
        return "live"
    return "archive"


def _serialize_mc_contact(pubkey, contact, now=None, source_state=None):
    now = int(time.time()) if now is None else int(now)
    last_seen_ts = _mc_contact_last_seen_ts(contact, now=now)
    last_advert = contact.get("last_advert", 0)
    delta = now - last_seen_ts if last_seen_ts else None
    if delta is not None:
        if delta < 60:
            last_seen = f"{delta}s ago"
        elif delta < 3600:
            last_seen = f"{delta // 60}m ago"
        elif delta < 86400:
            last_seen = f"{delta // 3600}h ago"
        else:
            last_seen = f"{delta // 86400}d ago"
    else:
        last_seen = None

    out = {
        "id": pubkey[:12],
        "full_key": pubkey,
        "long_name": contact.get("adv_name", pubkey[:8]),
        "short_name": contact.get("adv_name", "?")[:4].upper(),
        "latitude": contact.get("adv_lat") or None,
        "longitude": contact.get("adv_lon") or None,
        "last_advert": last_advert,
        "last_seen_ts": last_seen_ts,
        "last_seen": last_seen,
        "out_path_len": contact.get("out_path_len", -1),
        "out_path": contact.get("out_path", ""),
        "out_path_hash_mode": contact.get("out_path_hash_mode"),
        "out_path_hash_size": contact.get("out_path_hash_size"),
        "path_manual": bool(contact.get("path_manual", False)),
        "type": contact.get("type", 0),
        "network": "mc",
    }
    if source_state:
        out["source_state"] = source_state
        out["archived_only"] = source_state == "archive"
    return out


# ---------------------------------------------------------------------------
# Status + node info
# ---------------------------------------------------------------------------

@bp.route("/api/mc/status")
def api_mc_status():
    """All MC radio connections and their status."""
    with CONFIG_LOCK:
        configured = {
            str(node.get("id")): dict(node)
            for node in CONFIG.get("mc_nodes", [])
            if node.get("id")
        }
    with mc_connections_lock:
        state_map = {str(cid): dict(v) for cid, v in mc_connections.items()}
    result = []
    runtime_ids = [
        cid for cid, state in state_map.items()
        if cid in configured or state.get("status") in ("connected", "connecting")
    ]
    all_ids = list(dict.fromkeys([
        *configured.keys(),
        *runtime_ids,
    ]))
    for cid in all_ids:
        v = state_map.get(cid, {})
        cfg = dict(v.get("config", {}) or configured.get(cid, {}) or {})
        archive_contacts = get_mc_contact_archive(cid)
        info = v.get("node_info", {})
        live_contacts = v.get("live_contacts", {}) or {}
        merged_contacts = v.get("contacts", {}) or {}
        if not merged_contacts and archive_contacts:
            merged_contacts = archive_contacts
        archived_only_count = len(set(archive_contacts.keys()) - set(live_contacts.keys()))
        result.append({
            "id":         cid,
            "name":       cfg.get("name", cid),
            "status":     v.get("status", "disconnected"),
            "node_id":    v.get("node_id", ""),
            "node_name":  info.get("name", ""),
            "freq":       info.get("radio_freq"),
            "sf":         info.get("radio_sf"),
            "bw":         info.get("radio_bw"),
            "cr":         info.get("radio_cr"),
            "tx_power":   info.get("tx_power"),
            "max_tx_power": info.get("max_tx_power"),
            "max_channels": info.get("max_channels"),
            "lat":        info.get("adv_lat"),
            "lon":        info.get("adv_lon"),
            "adv_loc_policy": info.get("adv_loc_policy"),
            "contacts":   len(live_contacts),
            "stored_contacts": len(merged_contacts),
            "live_contacts": len(live_contacts),
            "archived_contacts": archived_only_count,
            "enabled":    cfg.get("enabled", True),
            "path_hash_mode": cfg.get("path_hash_mode", info.get("path_hash_mode")),
            "force_flood": bool(cfg.get("force_flood", False)),
        })
    return jsonify({"mc_nodes": result})


@bp.route("/api/mc/<radio_id>/contacts")
def api_mc_contacts(radio_id):
    """MC contacts for a given radio. Optionally refresh from device."""
    refresh = request.args.get("refresh") == "1"
    if refresh:
        try:
            refresh_contacts(radio_id)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    with mc_connections_lock:
        state = mc_connections.get(radio_id)
        contacts_raw = dict(state.get("contacts", {})) if state else None
        live_contacts = dict(state.get("live_contacts", {})) if state else None
    archive_contacts = get_mc_contact_archive(radio_id)
    if contacts_raw is None:
        if not archive_contacts:
            return jsonify({"error": "MC radio not found"}), 404
        contacts_raw = dict(archive_contacts)
        live_contacts = {}
    elif not contacts_raw and live_contacts:
        contacts_raw = dict(live_contacts)
    elif not contacts_raw and archive_contacts and not live_contacts:
        contacts_raw = dict(archive_contacts)
    contacts = []
    now = int(time.time())
    for pubkey, c in contacts_raw.items():
        source_state = _mc_contact_source_state(pubkey, live_contacts, archive_contacts)
        contacts.append(_serialize_mc_contact(pubkey, c, now=now, source_state=source_state))

    contacts.sort(key=lambda x: x["last_seen_ts"], reverse=True)
    return jsonify({"contacts": contacts, "radio_id": radio_id})


@bp.route("/api/mc/<radio_id>/contacts/<contact_id>", methods=["DELETE"])
def api_mc_delete_contact(radio_id, contact_id):
    """Remove a contact from the MC device (NVS delete)."""
    try:
        remove_mc_contact(radio_id, contact_id)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] delete contact {contact_id}: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/<radio_id>/contacts/<contact_id>/share")
def api_mc_share_contact(radio_id, contact_id):
    """Export an MC contact as an official meshcore:// share URI plus QR SVG."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
        contacts = dict(state.get("contacts", {}) or {})
        live_contacts = dict(state.get("live_contacts", {}) or {})
    archive_contacts = get_mc_contact_archive(radio_id)
    lookup_contacts = {}
    lookup_contacts.update(archive_contacts)
    lookup_contacts.update(contacts)
    try:
        full_key = next((k for k in lookup_contacts.keys() if str(k).startswith(contact_id)), contact_id)
        contact = lookup_contacts.get(full_key, {})
        if not contact:
            return jsonify({"error": "Contact not found"}), 404
        source_state = _mc_contact_source_state(full_key, live_contacts, archive_contacts)
        uri = _mc_contact_share_uri(full_key, contact)
        return jsonify({
            "ok": True,
            "radio_id": radio_id,
            "contact_id": full_key[:12],
            "uri": uri,
            "qr_svg": _qr_svg(uri),
            "official": True,
            "export_error": None,
            "details": _serialize_mc_contact(full_key, contact, source_state=source_state),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] share contact {contact_id}: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/<radio_id>/contacts/<contact_id>/route", methods=["POST"])
def api_mc_set_contact_route(radio_id, contact_id):
    """Set or clear a stored routing path for one MC contact."""
    data = request.get_json(silent=True) or {}
    clear = bool(data.get("clear"))
    hop_prefixes = data.get("hops") or []
    if not isinstance(hop_prefixes, list):
        return jsonify({"error": "hops must be a list"}), 400

    raw_mode = data.get("path_hash_mode")
    if raw_mode in ("", None):
        path_hash_mode = None
    else:
        try:
            path_hash_mode = int(raw_mode)
        except (TypeError, ValueError):
            return jsonify({"error": "path_hash_mode must be an integer"}), 400
        if path_hash_mode < 0 or path_hash_mode > 2:
            return jsonify({"error": "path_hash_mode must be between 0 and 2"}), 400

    try:
        updated_contact, _result = set_contact_path(
            radio_id,
            contact_id,
            hop_prefixes=hop_prefixes,
            clear=clear,
            path_hash_mode=path_hash_mode,
        )
        full_key = updated_contact.get("public_key", contact_id)
        with mc_connections_lock:
            state = mc_connections.get(radio_id) or {}
            live_contacts = dict(state.get("live_contacts", {}) or {})
        archive_contacts = get_mc_contact_archive(radio_id)
        source_state = _mc_contact_source_state(full_key, live_contacts, archive_contacts)
        return jsonify({
            "ok": True,
            "radio_id": radio_id,
            "contact": _serialize_mc_contact(full_key, updated_contact, source_state=source_state),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] set route failed for {contact_id}: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/ignored")
def api_mc_get_ignored():
    return jsonify({"ignored": list(get_mc_ignored())})


@bp.route("/api/mc/contacts/<contact_id>/ignore", methods=["PATCH"])
def api_mc_ignore_contact(contact_id):
    data = request.get_json(silent=True) or {}
    ignored = bool(data.get("ignored", True))
    set_mc_ignored(contact_id, ignored)
    return jsonify({"ok": True, "ignored": ignored})


@bp.route("/api/mc/<radio_id>/self")
def api_mc_self(radio_id):
    """Self info for a connected MC radio."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id)
    if not state:
        return jsonify({"error": "MC radio not found"}), 404
    return jsonify({
        "node_info": state.get("node_info", {}),
        "node_id":   state.get("node_id", ""),
        "status":    state.get("status", "disconnected"),
    })


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

MC_MAX_DM_MSG_BYTES = 160
MC_CHANNEL_NAME_OVERHEAD_BYTES = 2
MC_CHANNEL_SCOPE_HEADROOM_BYTES = 10


def _mc_text_bytes(text):
    return len((text or "").encode("utf-8"))


def _mc_channel_msg_limit(radio_id):
    """Return the safe user-text byte budget for an MC channel message."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {}) or {}
        info = state.get("node_info", {}) or {}
        advert_name = info.get("name") or state.get("config", {}).get("name") or ""
    name_bytes = _mc_text_bytes(advert_name)
    return max(0, MC_MAX_DM_MSG_BYTES - name_bytes - MC_CHANNEL_NAME_OVERHEAD_BYTES - MC_CHANNEL_SCOPE_HEADROOM_BYTES)


def _mc_sent_message_id(msg):
    parts = [
        msg.get("radio_id", ""),
        msg.get("subtype", ""),
        msg.get("channel", 0),
        msg.get("from_id", ""),
        msg.get("to_id", ""),
        msg.get("text", ""),
        msg.get("ts", 0),
        time.time_ns(),
    ]
    return "mc-" + hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:20]


@bp.route("/api/mc/<radio_id>/messages")
def api_mc_messages(radio_id):
    try:
        limit = int(request.args.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    limit = max(1, min(limit, 2000))
    return jsonify({"messages": load_mc_messages(radio_id, limit=limit)})


@bp.route("/api/mc/<radio_id>/messages/channel/<int:idx>", methods=["DELETE"])
def api_mc_delete_message_channel(radio_id, idx):
    if not (0 <= idx <= 15):
        return jsonify({"error": "channel index must be 0–15"}), 400
    return jsonify({"ok": True, "removed_db": delete_mc_channel_messages(radio_id, idx)})


@bp.route("/api/mc/<radio_id>/send_chan", methods=["POST"])
def api_mc_send_chan(radio_id):
    """Send a channel message."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    try:
        chan = int(data.get("channel", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "channel must be a number"}), 400
    if not text:
        return jsonify({"error": "text is required"}), 400
    if not (0 <= chan <= 7):
        return jsonify({"error": "channel must be 0–7"}), 400
    byte_len = _mc_text_bytes(text)
    max_bytes = _mc_channel_msg_limit(radio_id)
    if byte_len > max_bytes:
        return jsonify({"error": f"Message too long ({byte_len} bytes, max {max_bytes})"}), 400

    try:
        send_chan_msg(radio_id, chan, text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] send_chan failed: {e}")
        return jsonify({"error": str(e)}), 500
    msg = {
        "type": "mc_message",
        "radio_id": radio_id,
        "radio_name": next((n.get("name") for n in CONFIG.get("mc_nodes", []) if n.get("id") == radio_id), radio_id),
        "network": "mc",
        "subtype": "channel",
        "channel": chan,
        "from_id": "me",
        "from_name": "Me",
        "text": text,
        "ts": int(time.time()),
        "sent": True,
        "status": "delivered",
    }
    msg["id"] = _mc_sent_message_id(msg)
    save_mc_message(msg)
    threading.Thread(target=maybe_forward_mc_message, args=(dict(msg),), daemon=True).start()

    return jsonify({"ok": True, "message": msg})


@bp.route("/api/mc/<radio_id>/send_dm", methods=["POST"])
def api_mc_send_dm(radio_id):
    """Send a DM to a contact (identified by pubkey prefix)."""
    data   = request.get_json(silent=True) or {}
    text   = (data.get("text") or "").strip()
    target = (data.get("target") or "").strip()   # pubkey prefix (≥6 chars)
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    if not text:
        return jsonify({"error": "text is required"}), 400
    if not target or len(target) < 6:
        return jsonify({"error": "target pubkey prefix required (≥6 chars)"}), 400
    byte_len = _mc_text_bytes(text)
    if byte_len > MC_MAX_DM_MSG_BYTES:
        return jsonify({"error": f"Message too long ({byte_len} bytes, max {MC_MAX_DM_MSG_BYTES})"}), 400

    try:
        send_dm(radio_id, target, text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] send_dm failed: {e}")
        return jsonify({"error": str(e)}), 500

    msg = {
        "type": "mc_message",
        "radio_id": radio_id,
        "radio_name": next((n.get("name") for n in CONFIG.get("mc_nodes", []) if n.get("id") == radio_id), radio_id),
        "network": "mc",
        "subtype": "dm",
        "from_id": "me",
        "to_id": target,
        "text": text,
        "ts": int(time.time()),
        "sent": True,
        "status": "delivered",
    }
    msg["id"] = _mc_sent_message_id(msg)
    save_mc_message(msg)
    return jsonify({"ok": True, "message": msg})


@bp.route("/api/mc/<radio_id>/statusreq/<node_id>", methods=["POST"])
def api_mc_statusreq(radio_id, node_id):
    """Send a status request to a contact.

    Prefer the library's synchronized req_status_sync path for USB companion
    nodes. It waits for the matching STATUS_RESPONSE tag instead of only
    confirming local TX. If a response arrives, also mirror it into SSE so the
    existing frontend panel continues to work unchanged.
    """
    log.info(f"[MC] statusreq route called: radio={radio_id} node={node_id[:12] if node_id else '?'}")
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    if not node_id or len(node_id) < 6:
        return jsonify({"error": "node_id (pubkey prefix) required"}), 400
    try:
        result = req_node_status(radio_id, node_id)
        if result:
            log.info(
                f"[MC] statusreq sync success: radio={radio_id} node={node_id[:12]} "
                f"pubkey_pre={result.get('pubkey_pre', '?')}"
            )
            push_to_sse({
                "type":         "mc_status_response",
                "radio_id":     radio_id,
                "mode":         result.get("mode", "status"),
                "reachable":    result.get("reachable"),
                "note":         result.get("note"),
                "pubkey_pre":   result.get("pubkey_pre") or node_id[:12],
                "bat":          result.get("bat"),
                "last_rssi":    result.get("last_rssi"),
                "last_snr":     result.get("last_snr"),
                "noise_floor":  result.get("noise_floor"),
                "uptime":       result.get("uptime"),
                "nb_recv":      result.get("nb_recv"),
                "nb_sent":      result.get("nb_sent"),
                "tx_queue_len": result.get("tx_queue_len"),
                "airtime":      result.get("airtime"),
                "rx_airtime":   result.get("rx_airtime"),
                "sent_flood":   result.get("sent_flood"),
                "sent_direct":  result.get("sent_direct"),
                "recv_flood":   result.get("recv_flood"),
                "recv_direct":  result.get("recv_direct"),
                "flood_dups":   result.get("flood_dups"),
                "direct_dups":  result.get("direct_dups"),
                "observed_path":         result.get("observed_path"),
                "observed_path_len":     result.get("observed_path_len"),
                "observed_path_hash_size": result.get("observed_path_hash_size"),
                "observed_rssi":         result.get("observed_rssi"),
                "observed_snr":          result.get("observed_snr"),
                "observed_payload_type": result.get("observed_payload_type"),
                "observed_route_type":   result.get("observed_route_type"),
                "observed_recv_time":    result.get("observed_recv_time"),
            })
            return jsonify({"ok": True, "mode": "req_status_sync", "response": result})
        log.warning(f"[MC] statusreq sync returned no response: radio={radio_id} node={node_id[:12]}")
        return jsonify({"ok": True, "mode": "req_status_sync", "response": None})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] statusreq failed: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/<radio_id>/remote/<node_id>/read", methods=["POST"])
def api_mc_remote_read(radio_id, node_id):
    """Login/read remote repeater or room-server data via the selected MC radio."""
    data = request.get_json(silent=True) or {}
    password = data.get("password")
    login = bool(data.get("login"))
    try:
        return jsonify(remote_repeater_read(radio_id, node_id, password=password, login=login))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] remote read failed: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/<radio_id>/remote/<node_id>/command", methods=["POST"])
def api_mc_remote_command(radio_id, node_id):
    """Send a whitelisted remote admin command to a repeater or room server."""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(remote_repeater_command(radio_id, node_id, data.get("command", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] remote command failed: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/<radio_id>/advert", methods=["POST"])
def api_mc_advert(radio_id):
    """Broadcast an advertisement (announce presence on mesh)."""
    data  = request.get_json(silent=True) or {}
    flood = bool(data.get("flood", False))
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    try:
        send_advert(radio_id, flood=flood)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/device_info")
def api_mc_device_info(radio_id):
    """Fetch firmware info + battery from MC radio (live query, ~1s)."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    try:
        result = get_device_info(radio_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] get_device_info failed: {e}")
        return jsonify({"error": str(e)}), 500
    # Serialise bytes in device payload (channel_secret etc.) to hex
    dev = result.get("device", {})
    for k, v in dev.items():
        if isinstance(v, (bytes, bytearray)):
            dev[k] = v.hex()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Radio settings
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/radio", methods=["POST"])
def api_mc_set_radio(radio_id):
    """Set radio parameters (freq MHz, bw kHz, sf 7-12, cr 5-8)."""
    with mc_connections_lock:
        if mc_connections.get(radio_id, {}).get("status") != "connected":
            return jsonify({"error": "MC radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        freq   = float(data["freq"])
        bw_khz = float(data["bw"])   # received in kHz (e.g. 125)
        sf     = int(data["sf"])
        cr     = int(data["cr"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"Invalid params: {e}"}), 400
    if not (7 <= sf <= 12):
        return jsonify({"error": "sf must be 7–12"}), 400
    if not (5 <= cr <= 8):
        return jsonify({"error": "cr must be 5–8"}), 400
    if not (1 <= bw_khz <= 1000):
        return jsonify({"error": "bw out of range (1–1000 kHz)"}), 400
    if not (400 <= freq <= 950):
        return jsonify({"error": "freq out of range (400–950 MHz)"}), 400
    try:
        set_radio_params(radio_id, freq, bw_khz, sf, cr)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
        info = state.get("node_info")
        if info is not None:
            info.update({"radio_freq": freq, "radio_bw": bw_khz,
                         "radio_sf": sf, "radio_cr": cr})
        cfg = state.get("config")
    with CONFIG_LOCK:
        if cfg is not None:
            cfg["radio_params"] = {"freq": freq, "bw": bw_khz, "sf": sf, "cr": cr}
        save_config()
    return jsonify({"ok": True})


@bp.route("/api/mc/<radio_id>/tx_power", methods=["POST"])
def api_mc_set_tx_power(radio_id):
    """Set TX power in dBm."""
    with mc_connections_lock:
        if mc_connections.get(radio_id, {}).get("status") != "connected":
            return jsonify({"error": "MC radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        val = int(data["tx_power"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "tx_power required (integer dBm)"}), 400
    with mc_connections_lock:
        max_pwr = mc_connections.get(radio_id, {}).get("node_info", {}).get("max_tx_power")
    if max_pwr is not None and val > max_pwr:
        return jsonify({"error": f"tx_power exceeds max ({max_pwr} dBm)"}), 400
    if val < 1:
        return jsonify({"error": "tx_power must be ≥ 1 dBm"}), 400
    try:
        set_tx_power(radio_id, val)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with mc_connections_lock:
        info = mc_connections.get(radio_id, {}).get("node_info")
        if info is not None:
            info["tx_power"] = val
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Device actions
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/device_name", methods=["POST"])
def api_mc_set_device_name(radio_id):
    """Rename the MC device."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(name) > 32:
        return jsonify({"error": "name too long (max 32 chars)"}), 400
    try:
        set_device_name(radio_id, name)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@bp.route("/api/mc/<radio_id>/coords", methods=["POST"])
def api_mc_set_coords(radio_id):
    """Set GPS coordinates on the MC device."""
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon required (floats)"}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400
    try:
        set_device_coords(radio_id, lat, lon)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@bp.route("/api/mc/<radio_id>/loc_policy", methods=["POST"])
def api_mc_set_loc_policy(radio_id):
    """Set advertisement location policy on the MC device (0=disabled, 1=enabled)."""
    data = request.get_json(silent=True) or {}
    try:
        policy = int(data["policy"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "policy required (0 or 1)"}), 400
    if policy not in (0, 1):
        return jsonify({"error": "policy must be 0 or 1"}), 400
    try:
        set_advert_loc_policy(radio_id, policy)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "adv_loc_policy": policy})


@bp.route("/api/mc/<radio_id>/reboot", methods=["POST"])
def api_mc_reboot(radio_id):
    """Reboot the MC device."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    try:
        reboot_device_dtr(radio_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Channel management
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/channels")
def api_mc_channels(radio_id):
    """List all channels for a MC radio (queries device live)."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    # max_channels comes from DEVICE_INFO (send_device_query); default 8
    max_ch = min(int(state.get("node_info", {}).get("max_channels") or 8), 16)
    try:
        channels = get_channels(radio_id, max_ch, timeout=max(30, max_ch * 7))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] get_channels failed: {e}")
        return jsonify({"error": str(e)}), 500
    result = []
    for ch in channels:
        result.append({
            "idx":  ch.get("channel_idx", 0),
            "name": ch.get("channel_name", ""),
            "hash": ch.get("channel_hash", ""),
        })
    return jsonify({"channels": result, "radio_id": radio_id})


def _mc_bytes_hex(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    return ""


def _mc_contact_share_uri(pubkey, contact):
    type_value = int(contact.get("type") or contact.get("contact_type") or 1)
    params = {
        "name": contact.get("adv_name") or contact.get("name") or pubkey[:8],
        "public_key": pubkey,
        "type": str(type_value),
    }
    return "meshcore://contact/add?" + urlencode(params)


def _mc_channel_share_uri(details):
    params = {
        "name": details.get("name") or "",
        "secret": details.get("secret_hex") or "",
    }
    return "meshcore://channel/add?" + urlencode({k: v for k, v in params.items() if v})


@bp.route("/api/mc/<radio_id>/channels/<int:idx>/share")
def api_mc_channel_share(radio_id, idx):
    """Export one MC channel as an OM QR payload with its 16-byte secret."""
    if not (0 <= idx <= 15):
        return jsonify({"error": "channel index must be 0–15"}), 400
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    max_ch = min(int(state.get("node_info", {}).get("max_channels") or 8), 16)
    try:
        channels = get_channels(radio_id, max_ch, timeout=max(30, max_ch * 7))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] get_channel share failed: {e}")
        return jsonify({"error": str(e)}), 500
    channel = next((ch for ch in channels if int(ch.get("channel_idx", -1)) == idx), None)
    if not channel:
        return jsonify({"error": "Channel not found"}), 404
    secret_hex = _mc_bytes_hex(channel.get("channel_secret"))
    if len(secret_hex) != 32 or not all(c in "0123456789abcdefABCDEF" for c in secret_hex):
        return jsonify({"error": "Channel secret unavailable; cannot build a joinable QR"}), 422
    secret_hex = secret_hex.lower()
    details = {
        "network": "MeshCore",
        "idx": idx,
        "name": channel.get("channel_name", ""),
        "hash": channel.get("channel_hash", ""),
        "secret_hex": secret_hex,
        "radio_id": radio_id,
        "radio_name": state.get("config", {}).get("name", radio_id),
    }
    uri = _mc_channel_share_uri(details)
    try:
        qr_svg = _qr_svg(uri)
    except Exception as e:
        return jsonify({"error": f"QR generation failed: {e}", "details": details, "uri": uri}), 500
    return jsonify({
        "ok": True,
        "uri": uri,
        "qr_svg": qr_svg,
        "details": details,
        "json": json.dumps(details, separators=(",", ":"), sort_keys=True),
    })


@bp.route("/api/mc/<radio_id>/channels/<int:idx>", methods=["POST"])
def api_mc_set_channel(radio_id, idx):
    """Set a channel by slot index.
    key_type: 'auto' (derive from name), 'keep' (reuse current), 'random', 'custom' (provide key hex).
    """
    if not (0 <= idx <= 15):
        return jsonify({"error": "channel index must be 0–15"}), 400
    data = request.get_json(silent=True) or {}
    name     = (data.get("name")     or "").strip()
    key_type = (data.get("key_type") or "auto").strip().lower()
    key_hex  = (data.get("key")      or "").strip().lower()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(name) > 32:
        return jsonify({"error": "name too long (max 32 chars)"}), 400

    if key_type == "random":
        key_hex = os.urandom(16).hex()
    elif key_type == "custom":
        if not key_hex:
            return jsonify({"error": "Custom key is empty"}), 400
        if len(key_hex) != 32 or not all(c in "0123456789abcdef" for c in key_hex):
            return jsonify({"error": "key must be exactly 32 hex characters (16 bytes)"}), 400
    elif key_type == "keep":
        with mc_connections_lock:
            state = mc_connections.get(radio_id, {})
        max_ch = min(int(state.get("node_info", {}).get("max_channels") or 8), 16)
        try:
            channels = get_channels(radio_id, max_ch, timeout=max(30, max_ch * 7))
        except Exception as e:
            return jsonify({"error": f"Failed to read current channels: {e}"}), 500
        ch = next((c for c in channels if int(c.get("channel_idx", -1)) == idx), None)
        secret = _mc_bytes_hex(ch.get("channel_secret")) if ch else ""
        key_hex = secret if len(secret) == 32 else ""
    else:  # "auto"
        key_hex = ""

    try:
        set_channel(radio_id, idx, name, key_hex=key_hex or None)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@bp.route("/api/mc/<radio_id>/channels/<int:idx>", methods=["DELETE"])
def api_mc_delete_channel(radio_id, idx):
    """Clear a channel slot by overwriting it with an empty name, and delete its chat history."""
    if not (0 <= idx <= 15):
        return jsonify({"error": "channel index must be 0–15"}), 400
    try:
        set_channel(radio_id, idx, "")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    removed = delete_mc_channel_messages(radio_id, idx)
    return jsonify({"ok": True, "removed_db": removed})


# ---------------------------------------------------------------------------
# Self stats
# ---------------------------------------------------------------------------


@bp.route("/api/mc/<radio_id>/stats")
def api_mc_stats(radio_id):
    """Fetch own stats from the MC radio (core, radio, packets)."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    try:
        result = get_stats(radio_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] get_stats failed: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


# ---------------------------------------------------------------------------
# MC Scan — flood advert + 60s collection window
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/scan", methods=["POST"])
def api_mc_scan(radio_id):
    """Flood-advertise on MC mesh and collect mc_node SSE events for 60s."""
    import json
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503

    with _scan_lock:
        existing = _scan_timers.get(radio_id)
        if existing and existing.is_alive():
            return jsonify({"error": "Scan already in progress"}), 409
        # Send flood advert
        try:
            send_advert(radio_id, flood=True)
        except RuntimeError as e:
            log.warning(f"[MC] scan advert unavailable: {e}")
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            log.warning(f"[MC] scan advert failed: {e}")
            return jsonify({"error": str(e)}), 500
        # Push scan_started SSE
        push_to_sse(json.dumps({"type": "mc_scan_started", "radio_id": radio_id,
                                "window": MC_SCAN_WINDOW}))
        # Schedule scan_done after window
        def _scan_done():
            time.sleep(MC_SCAN_WINDOW)
            push_to_sse(json.dumps({"type": "mc_scan_done", "radio_id": radio_id}))
        t = threading.Thread(target=_scan_done, daemon=True)
        _scan_timers[radio_id] = t
        t.start()

    return jsonify({"ok": True, "window": MC_SCAN_WINDOW})


# ---------------------------------------------------------------------------
# Trace (broadcast trace packet — push notification 0x89, works on older firmware)
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/trace", methods=["POST"])
def api_mc_trace(radio_id):
    """Send a broadcast trace packet. Response arrives via SSE as mc_trace_data.
    TRACE_DATA (0x89) is a push notification so it works even where STATUS_RESPONSE doesn't."""
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    try:
        sent_tag = send_trace_broadcast(radio_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] trace failed: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "tag": sent_tag})


# ---------------------------------------------------------------------------
# Debug event logging
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/debug_events", methods=["POST"])
def api_mc_debug_events(radio_id):
    """Enable catch-all event logging for this MC radio for 60s.
    Every event dispatched from the serial reader is logged at INFO — use to
    diagnose missing STATUS_RESPONSE (ping) issues. Check the active server log."""
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    duration = min(int(data.get("duration", 60)), 300)
    try:
        enable_mc_debug(radio_id, duration=duration)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "duration": duration})


# ---------------------------------------------------------------------------
# Import contact via share link (meshcore://)
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/import_contact", methods=["POST"])
def api_mc_import_contact(radio_id):
    """Import a contact from a meshcore:// share link into the radio's NVS contact list."""
    if CONFIG.get("silent_mode"):
        return jsonify({"error": "Silent Running active — transmissions are blocked"}), 409
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    link = (data.get("link") or "").strip()
    if not link:
        return jsonify({"error": "link is required"}), 400
    try:
        import_mc_contact(radio_id, link)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] import_contact failed: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})

"""
MeshCore API routes.
"""
import io
import hashlib
import os
import re
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
                     send_trace_broadcast, send_discover_req, import_mc_contact, enable_mc_debug,
                     get_mc_contact_archive,
                     set_contact_path, reset_all_paths, remote_repeater_read,
                     remote_repeater_command, clear_mc_all_contacts,
                     get_rc_collect_events, get_local_neighbors)
from db import (
    delete_mc_channel_messages,
    delete_mc_all_messages,
    count_passive_obs_by_collector,
    delete_passive_obs,
    cleanup_passive_obs,
    get_mc_ignored,
    load_mc_messages,
    load_passive_obs,
    load_passive_obs_summary,
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
            "status_ts":  v.get("status_ts"),
            "node_id":    v.get("node_id", ""),
            "node_name":  info.get("name", ""),
            "freq":       info.get("radio_freq"),
            "sf":         info.get("radio_sf"),
            "bw":         info.get("radio_bw"),
            "cr":         info.get("radio_cr"),
            "tx_power":   info.get("tx_power"),
            "max_tx_power": info.get("max_tx_power"),
            "max_channels": info.get("max_channels"),
            "lat":        info.get("adv_lat") or None,
            "lon":        info.get("adv_lon") or None,
            "adv_loc_policy": info.get("adv_loc_policy"),
            "contacts":   len(live_contacts),
            "stored_contacts": len(merged_contacts),
            "live_contacts": len(live_contacts),
            "archived_contacts": archived_only_count,
            "enabled":    cfg.get("enabled", True),
            "path_hash_mode": cfg.get("path_hash_mode", info.get("path_hash_mode")),
            "force_flood": bool(cfg.get("force_flood", False)),
            "passive_collection": cfg.get("passive_collection", True) is not False,
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


@bp.route("/api/mc/<radio_id>/reset_all_paths", methods=["POST"])
def api_mc_reset_all_paths(radio_id):
    """Reset stored routes for every contact on this radio, including manually-set ones."""
    try:
        result = reset_all_paths(radio_id)
        return jsonify({"ok": True, "cleared": result.get("cleared", 0), "errors": result.get("errors", 0)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] reset_all_paths failed: {e}")
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

# MeshCore addresses a channel with a SINGLE BYTE — see the meshcore lib's
# get_channel/set_channel (`channel_idx.to_bytes(1, "little")`) and DEVICE_INFO's
# max_channels, itself read as one byte (reader.py). So the protocol ceiling is
# 255, NOT 16. Real hardware is well above 16: ERA-3 reports max_channels=40.
# (Meshtastic has 8 — do not reuse the MT 0-7 limit on MC paths, see GH #20.)
# Since the device's own max_channels is a byte, this cap can never actually
# bind — the device value always wins. That is deliberate: no artificial ceiling.
MC_MAX_CHANNELS = 255
# Never validate NARROWER than the old hardcoded ceiling — see _mc_max_channels.
MC_MIN_CHANNELS = 8


def _mc_text_bytes(text):
    return len((text or "").encode("utf-8"))


def _mc_max_channels(radio_id):
    """Channel-slot COUNT for this MC radio (valid indices are 0 .. count-1).

    max_channels comes from DEVICE_INFO (send_device_query); default 8 when the
    node has not reported it yet. Same expression the channel-management routes
    already use.
    """
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {}) or {}
        info = state.get("node_info", {}) or {}
    # Floored at 8, the pre-GH#20 hardcoded ceiling: a node that under-reports
    # max_channels (or reports it wrongly) must never LOSE channels that worked
    # before — this limit may only ever widen, never narrow. Also makes a
    # malformed/negative value harmless.
    return max(MC_MIN_CHANNELS, min(int(info.get("max_channels") or 0), MC_MAX_CHANNELS))


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


_CLIENT_MSG_ID_RE = re.compile(r"^mc-[A-Za-z0-9-]{1,60}$")


def _resolve_sent_msg_id(msg, data):
    """Message id for an outgoing MC send.

    A sending client (OM's own UI) creates an optimistic local echo with a
    client-generated id and passes it back as ``client_msg_id``. Reusing it as
    the stored/broadcast id means the SSE echo (push_to_sse) carries the *same*
    id as that local echo, so the frontend merges it in place instead of showing
    a duplicate — regardless of whether the SSE beats the HTTP response. Callers
    that omit it (OPS-TOC comms proxy, other API clients) fall back to a
    server-computed id, unchanged.
    """
    cid = (data.get("client_msg_id") or "").strip()
    if cid and _CLIENT_MSG_ID_RE.match(cid):
        return cid
    return _mc_sent_message_id(msg)


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
    # Protocol bound, NOT the live device value. Validation here must never
    # reject a slot the user legitimately has just because DEVICE_INFO is
    # missing (radio offline, or not yet queried) — the DB-only routes have to
    # work offline at all, and for the routes that do reach the radio the device
    # itself is authoritative and returns the accurate error. Only the SEND path
    # uses _mc_max_channels(), where a connection is required anyway and the
    # exact "0-N" message is the point of GH #20.
    if not (0 <= idx < MC_MAX_CHANNELS):
        return jsonify({"error": f"channel index must be 0–{MC_MAX_CHANNELS - 1}"}), 400
    return jsonify({"ok": True, "removed_db": delete_mc_channel_messages(radio_id, idx)})


@bp.route("/api/mc/<radio_id>/messages", methods=["DELETE"])
def api_mc_delete_all_messages(radio_id):
    removed = delete_mc_all_messages(radio_id)
    return jsonify({"ok": True, "removed_db": removed})

@bp.route("/api/mc/<radio_id>/dm_messages", methods=["DELETE"])
def api_mc_delete_dm_messages(radio_id):
    from db import get_mc_msgs_db
    with get_mc_msgs_db(radio_id) as conn:
        conn.execute("DELETE FROM messages WHERE subtype='dm'")
    return jsonify({"ok": True})

@bp.route("/api/mc/<radio_id>/dm_messages/<node_id>", methods=["DELETE"])
def api_mc_delete_dm_node(radio_id, node_id):
    from db import get_mc_msgs_db
    with get_mc_msgs_db(radio_id) as conn:
        conn.execute("DELETE FROM messages WHERE subtype='dm' AND (from_id=? OR to_id=?)", (node_id, node_id))
    return jsonify({"ok": True})


@bp.route("/api/mc/<radio_id>/contacts/all", methods=["DELETE"])
def api_mc_delete_all_contacts(radio_id):
    try:
        result = clear_mc_all_contacts(radio_id)
        return jsonify({"ok": True, **(result or {})})
    except Exception as e:
        log.warning(f"[MC] clear_all_contacts {radio_id}: {e}")
        return jsonify({"error": str(e)}), 500


def _collect_stale_mc_contacts(cutoff):
    """MC contacts across all connected MC radios not seen since <cutoff> (epoch
    seconds). Skips contacts whose last-seen time is unknown. Returns descriptor
    dicts (oldest first) in the same shape the MT cleanup collector uses, so the
    frontend can render both in one modal."""
    now = int(time.time())
    stale = []
    with mc_connections_lock:
        radio_ids = list(mc_connections.keys())
    for radio_id in radio_ids:
        with mc_connections_lock:
            state = mc_connections.get(radio_id)
            contacts_raw = dict(state.get("contacts", {}) or {}) if state else {}
            live_contacts = dict(state.get("live_contacts", {}) or {}) if state else {}
            radio_name = ((state.get("config") or {}).get("name") if state else None) or radio_id
        archive_contacts = get_mc_contact_archive(radio_id)
        if not contacts_raw:
            contacts_raw = dict(live_contacts or archive_contacts or {})
        for pubkey, c in contacts_raw.items():
            last_seen_ts = _mc_contact_last_seen_ts(c, now=now)
            if not last_seen_ts:
                continue  # unknown last-seen -> leave it, don't delete blindly
            if last_seen_ts < cutoff:
                ser = _serialize_mc_contact(
                    pubkey, c, now=now,
                    source_state=_mc_contact_source_state(pubkey, live_contacts, archive_contacts),
                )
                stale.append({
                    "id": ser["id"],
                    "radio_id": radio_id,
                    "radio_name": radio_name,
                    "long_name": ser["long_name"],
                    "short_name": ser["short_name"],
                    "last_heard_ts": last_seen_ts,
                    "last_heard": ser["last_seen"],
                    "network": "mc",
                })
    stale.sort(key=lambda n: n["last_heard_ts"])  # oldest first
    return stale


@bp.route("/api/mc/contacts/cleanup/preview", methods=["POST"])
def api_mc_contacts_cleanup_preview():
    """List the MC contacts a cleanup would remove for the given <days>, WITHOUT
    deleting anything. Powers the shared cleanup modal (MC side)."""
    data = request.get_json(silent=True) or {}
    try:
        days = float(data.get("days"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a number of days"}), 400
    if days <= 0:
        return jsonify({"error": "Days must be greater than 0"}), 400
    cutoff = int(time.time()) - int(days * 86400)
    return jsonify({"ok": True, "days": days, "nodes": _collect_stale_mc_contacts(cutoff)})


@bp.route("/api/mc/contacts/cleanup", methods=["POST"])
def api_mc_contacts_cleanup():
    """Delete the explicit list of MC contacts chosen in the cleanup modal. Each
    entry is {id, radio_id} where id is the pubkey prefix. Reuses the same
    per-contact removal path as the single delete (device NVS + archive)."""
    data = request.get_json(silent=True) or {}
    requested = data.get("contacts")
    if not isinstance(requested, list) or not requested:
        return jsonify({"error": "No contacts selected"}), 400
    removed = 0
    errors = 0
    for item in requested:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        radio_id = item.get("radio_id")
        if not cid or not radio_id:
            continue
        try:
            remove_mc_contact(radio_id, cid)
            removed += 1
        except Exception as e:
            errors += 1
            log.warning(f"[MC] cleanup remove {cid}: {e}")
    return jsonify({"ok": True, "removed": removed, "errors": errors})


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
    max_ch = _mc_max_channels(radio_id)
    if not (0 <= chan < max_ch):
        return jsonify({"error": f"channel must be 0–{max_ch - 1}"}), 400
    byte_len = _mc_text_bytes(text)
    max_bytes = _mc_channel_msg_limit(radio_id)
    if byte_len > max_bytes:
        return jsonify({"error": f"Message too long ({byte_len} bytes, max {max_bytes})"}), 400

    try:
        result = send_chan_msg(radio_id, chan, text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] send_chan failed: {e}")
        return jsonify({"error": str(e)}), 500
    result_type = getattr(getattr(result, "type", None), "name", None)
    if result_type == "ERROR":
        return jsonify({"error": f"Device rejected channel message: {getattr(result, 'payload', {})}"}), 502
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
        # MeshCore channel sends return OK when the device accepts the message.
        # Unlike DMs, this is not a TX confirmation.
        "status": "queued",
    }
    msg["id"] = _resolve_sent_msg_id(msg, data)
    save_mc_message(msg)
    push_to_sse(msg)  # broadcast to OM's own UI so a send from any client (incl. OPS-TOC) shows live
    threading.Thread(target=maybe_forward_mc_message, args=(dict(msg),), daemon=True).start()

    return jsonify({"ok": True, "queued": True, "tx_event": result_type, "message": msg})


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
        result = send_dm(radio_id, target, text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] send_dm failed: {e}")
        return jsonify({"error": str(e)}), 500
    result_type = getattr(getattr(result, "type", None), "name", None)
    if result_type == "ERROR":
        return jsonify({"error": f"Device rejected DM: {getattr(result, 'payload', {})}"}), 502

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
    msg["id"] = _resolve_sent_msg_id(msg, data)
    save_mc_message(msg)
    push_to_sse(msg)  # broadcast to OM's own UI so a send from any client (incl. OPS-TOC) shows live
    return jsonify({"ok": True, "tx_event": result_type, "message": msg})


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
    data = request.get_json(silent=True) or {}
    prime_trace = bool(data.get("trace_probe"))
    try:
        result = req_node_status(radio_id, node_id, prime_trace=prime_trace)
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
    login_only = bool(data.get("login_only"))
    try:
        kwargs = {"password": password, "login": login}
        if login_only:
            kwargs["login_only"] = True
        return jsonify(remote_repeater_read(radio_id, node_id, **kwargs))
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
        result = send_advert(radio_id, flood=flood) or {}
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, **result})


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
    with mc_connections_lock:
        result["node_info"] = dict((mc_connections.get(radio_id, {}) or {}).get("node_info", {}) or {})
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
        reboot_device(radio_id)
        with mc_connections_lock:
            if radio_id in mc_connections:
                mc_connections[radio_id]["status"] = "disconnected"
                mc_connections[radio_id]["status_ts"] = time.time()
                mc_connections[radio_id]["mc"] = None
    except RuntimeError as e:
        log.warning(f"[MC] firmware reboot failed, falling back to DTR reset: {e}")
        try:
            reboot_device_dtr(radio_id)
        except RuntimeError as e2:
            return jsonify({"error": str(e2)}), 503
        except Exception as e2:
            return jsonify({"error": str(e2)}), 500
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
    max_ch = _mc_max_channels(radio_id)
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
    # Protocol bound, NOT the live device value. Validation here must never
    # reject a slot the user legitimately has just because DEVICE_INFO is
    # missing (radio offline, or not yet queried) — the DB-only routes have to
    # work offline at all, and for the routes that do reach the radio the device
    # itself is authoritative and returns the accurate error. Only the SEND path
    # uses _mc_max_channels(), where a connection is required anyway and the
    # exact "0-N" message is the point of GH #20.
    if not (0 <= idx < MC_MAX_CHANNELS):
        return jsonify({"error": f"channel index must be 0–{MC_MAX_CHANNELS - 1}"}), 400
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    max_ch = _mc_max_channels(radio_id)
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
    # Protocol bound, NOT the live device value. Validation here must never
    # reject a slot the user legitimately has just because DEVICE_INFO is
    # missing (radio offline, or not yet queried) — the DB-only routes have to
    # work offline at all, and for the routes that do reach the radio the device
    # itself is authoritative and returns the accurate error. Only the SEND path
    # uses _mc_max_channels(), where a connection is required anyway and the
    # exact "0-N" message is the point of GH #20.
    if not (0 <= idx < MC_MAX_CHANNELS):
        return jsonify({"error": f"channel index must be 0–{MC_MAX_CHANNELS - 1}"}), 400
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
        max_ch = _mc_max_channels(radio_id)
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
    # Protocol bound, NOT the live device value. Validation here must never
    # reject a slot the user legitimately has just because DEVICE_INFO is
    # missing (radio offline, or not yet queried) — the DB-only routes have to
    # work offline at all, and for the routes that do reach the radio the device
    # itself is authoritative and returns the accurate error. Only the SEND path
    # uses _mc_max_channels(), where a connection is required anyway and the
    # exact "0-N" message is the point of GH #20.
    if not (0 <= idx < MC_MAX_CHANNELS):
        return jsonify({"error": f"channel index must be 0–{MC_MAX_CHANNELS - 1}"}), 400
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
        # Active repeater probe: DISCOVER_REQ makes MC repeaters reply (0x8e) — the
        # only way a scan surfaces coverage without live traffic (channel msgs have
        # no ACK). Non-fatal: if it fails, the flood-advert scan still proceeds.
        discover_tag = None
        try:
            discover_tag = send_discover_req(radio_id)
        except Exception as e:
            log.warning(f"[MC] scan discover_req failed (advert still sent): {e}")
        # Push scan_started SSE
        push_to_sse(json.dumps({"type": "mc_scan_started", "radio_id": radio_id,
                                "window": MC_SCAN_WINDOW,
                                "discover": discover_tag is not None}))
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

@bp.route("/api/mc/<radio_id>/neighbors", methods=["GET"])
def api_mc_neighbors(radio_id):
    """Return contacts from the local MC radio's contact list.

    Query params:
      mode  — neighbors (default, direct only) | nearby (0+1 hop) | recent (24h) | all
    """
    from flask import request as flask_request
    with mc_connections_lock:
        state = mc_connections.get(radio_id, {})
    if state.get("status") != "connected":
        return jsonify({"error": "MC radio not connected"}), 503
    try:
        mode = flask_request.args.get("mode", "neighbors")
        if mode not in ("neighbors", "nearby", "recent", "all"):
            mode = "neighbors"
        result = get_local_neighbors(radio_id, mode=mode)
    except Exception as e:
        log.warning(f"[MC] neighbors failed: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, **result})


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


# ---------------------------------------------------------------------------
# Passive mesh intelligence
# ---------------------------------------------------------------------------

@bp.route("/api/mc/<radio_id>/passive_obs")
def api_mc_passive_obs(radio_id):
    """Return stored passive observations, optionally filtered by pubkey_pre."""
    pubkey_pre = request.args.get("pubkey_pre")
    limit = min(int(request.args.get("limit", 200)), 500)
    try:
        obs = load_passive_obs(radio_id, pubkey_pre=pubkey_pre, limit=limit)
        return jsonify(obs)
    except Exception as e:
        log.warning(f"[MC] passive_obs load error: {e}")
        return jsonify([])


@bp.route("/api/mc/<radio_id>/rc_collect_events")
def api_mc_rc_collect_events(radio_id):
    """Return RC collect summaries newer than ?since=<unix_ts>."""
    try:
        since = float(request.args.get("since", 0))
        return jsonify(get_rc_collect_events(radio_id, since))
    except Exception as e:
        log.warning(f"[MC] rc_collect_events error: {e}")
        return jsonify([]), 500


@bp.route("/api/mc/<radio_id>/passive_obs/collector_stats")
def api_mc_passive_obs_collector_stats(radio_id):
    """Return obs count per collector_id: {collector_id: count}."""
    try:
        return jsonify(count_passive_obs_by_collector(radio_id))
    except Exception as e:
        log.warning(f"[MC] passive_obs collector_stats error: {e}")
        return jsonify({}), 500


@bp.route("/api/mc/<radio_id>/passive_obs/summary")
def api_mc_passive_obs_summary(radio_id):
    """Return per-contact passive obs summary (count, best signal, last seen) for a list of pubkey prefixes."""
    prefixes_raw = request.args.get("prefixes", "")
    prefixes = [p.strip() for p in prefixes_raw.split(",") if p.strip()]
    if not prefixes:
        return jsonify({})
    try:
        summary = load_passive_obs_summary(radio_id, prefixes)
        return jsonify(summary)
    except Exception as e:
        log.warning(f"[MC] passive_obs summary error: {e}")
        return jsonify({})


@bp.route("/api/mc/<radio_id>/passive_obs", methods=["DELETE"])
def api_mc_passive_obs_delete(radio_id):
    """Delete passive observations for a radio, optionally scoped to one contact."""
    data = request.get_json(silent=True) or {}
    pubkey_pre = data.get("pubkey_pre")
    try:
        delete_passive_obs(radio_id, pubkey_pre=pubkey_pre)
        return jsonify({"ok": True})
    except Exception as e:
        log.warning(f"[MC] passive_obs delete error: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mc/<radio_id>/passive_obs/cleanup", methods=["POST"])
def api_mc_passive_obs_cleanup(radio_id):
    """Delete observations older than ttl_days (default 30)."""
    data = request.get_json(silent=True) or {}
    ttl_days = int(data.get("ttl_days", 30))
    try:
        deleted = cleanup_passive_obs(radio_id, ttl_days=ttl_days)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        log.warning(f"[MC] passive_obs cleanup error: {e}")
        return jsonify({"error": str(e)}), 500

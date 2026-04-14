"""
MeshCore API routes.
"""
import threading
import time
import logging

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from cross import maybe_forward_mc_message
from helpers import push_to_sse
from mesh_mc import (send_chan_msg, send_dm, send_advert, refresh_contacts,
                     get_device_info, set_radio_params, set_tx_power,
                     set_device_name, set_device_coords, reboot_device,
                     reboot_device_dtr, get_channels, set_channel,
                     req_node_status, get_stats, remove_mc_contact,
                     send_trace_broadcast, import_mc_contact, enable_mc_debug)
from db import get_mc_ignored, set_mc_ignored
from state import mc_connections, mc_connections_lock

# Per-radio scan state: radio_id → timer thread
_scan_timers: dict = {}
_scan_lock = threading.Lock()

MC_SCAN_WINDOW = 60  # seconds

log = logging.getLogger(__name__)
bp  = Blueprint("mc", __name__)


def _mc_contact_last_seen_ts(contact):
    return int(contact.get("last_seen_ts") or contact.get("last_advert") or 0)


# ---------------------------------------------------------------------------
# Status + node info
# ---------------------------------------------------------------------------

@bp.route("/api/mc/status")
def api_mc_status():
    """All MC radio connections and their status."""
    with mc_connections_lock:
        result = []
        for cid, v in mc_connections.items():
            info = v.get("node_info", {})
            result.append({
                "id":         cid,
                "name":       v.get("config", {}).get("name", cid),
                "status":     v.get("status", "disconnected"),
                "node_id":    v.get("node_id", ""),
                "node_name":  info.get("name", ""),
                "freq":       info.get("radio_freq"),
                "sf":         info.get("radio_sf"),
                "bw":         info.get("radio_bw"),
                "cr":         info.get("radio_cr"),
                "tx_power":   info.get("tx_power"),
                "max_tx_power": info.get("max_tx_power"),
                "lat":        info.get("adv_lat"),
                "lon":        info.get("adv_lon"),
                "contacts":   len(v.get("contacts", {})),
                "enabled":    v.get("config", {}).get("enabled", True),
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
    if contacts_raw is None:
        return jsonify({"error": "MC radio not found"}), 404
    contacts = []
    now = int(time.time())
    for pubkey, c in contacts_raw.items():
        last_seen_ts = _mc_contact_last_seen_ts(c)
        last_advert = c.get("last_advert", 0)
        delta = now - last_seen_ts if last_seen_ts else None
        if delta is not None:
            if delta < 60:       last_seen = f"{delta}s ago"
            elif delta < 3600:   last_seen = f"{delta // 60}m ago"
            elif delta < 86400:  last_seen = f"{delta // 3600}h ago"
            else:                last_seen = f"{delta // 86400}d ago"
        else:
            last_seen = None

        contacts.append({
            "id":          pubkey[:12],
            "full_key":    pubkey,
            "long_name":   c.get("adv_name", pubkey[:8]),
            "short_name":  c.get("adv_name", "?")[:4].upper(),
            "latitude":    c.get("adv_lat") or None,
            "longitude":   c.get("adv_lon") or None,
            "last_advert": last_advert,
            "last_seen_ts": last_seen_ts,
            "last_seen":   last_seen,
            "out_path_len":       c.get("out_path_len", -1),
            "out_path":           c.get("out_path", ""),
            "out_path_hash_mode": c.get("out_path_hash_mode", 0),
            "type":        c.get("type", 0),   # 0=client, 1=room, 2=repeater
            "network":     "mc",
        })

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

    try:
        send_chan_msg(radio_id, chan, text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] send_chan failed: {e}")
        return jsonify({"error": str(e)}), 500
    threading.Thread(target=maybe_forward_mc_message, args=({
        "radio_id": radio_id,
        "subtype": "channel",
        "channel": chan,
        "from_id": "self",
        "from_name": next((n.get("name") for n in CONFIG.get("mc_nodes", []) if n.get("id") == radio_id), radio_id),
        "text": text,
    },), daemon=True).start()

    return jsonify({"ok": True})


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

    try:
        send_dm(radio_id, target, text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC] send_dm failed: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


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
        channels = get_channels(radio_id, max_ch)
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


@bp.route("/api/mc/<radio_id>/channels/<int:idx>", methods=["POST"])
def api_mc_set_channel(radio_id, idx):
    """Set a channel by slot index. Optional key (32 hex chars = 16 bytes); if omitted, auto-derived from name."""
    if not (0 <= idx <= 15):
        return jsonify({"error": "channel index must be 0–15"}), 400
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    key  = (data.get("key")  or "").strip().lower()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(name) > 32:
        return jsonify({"error": "name too long (max 32 chars)"}), 400
    if key:
        if len(key) != 32 or not all(c in "0123456789abcdef" for c in key):
            return jsonify({"error": "key must be exactly 32 hex characters (16 bytes)"}), 400
    try:
        set_channel(radio_id, idx, name, key_hex=key or None)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


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

"""
MeshCore API routes.
"""
import threading
import time
import logging
import json
from urllib.parse import urlencode

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from cross import maybe_forward_mc_message
from helpers import push_to_sse
from mesh_mc import (send_chan_msg, send_dm, send_advert, refresh_contacts,
                     get_device_info, set_radio_params, set_tx_power,
                     set_device_name, set_device_coords, reboot_device,
                     reboot_device_dtr, get_channels, set_channel,
                     req_node_status, get_stats, remove_mc_contact,
                     send_trace_broadcast, import_mc_contact, enable_mc_debug,
                     get_mc_contact_archive,
                     set_contact_path, mc_config_tool, export_mc_contact_uri)
from db import get_mc_ignored, set_mc_ignored
from state import mc_connections, mc_connections_lock

# Per-radio scan state: radio_id → timer thread
_scan_timers: dict = {}
_scan_lock = threading.Lock()

MC_SCAN_WINDOW = 60  # seconds

log = logging.getLogger(__name__)
bp  = Blueprint("mc", __name__)


_QR_TOTAL_CODEWORDS_L = {
    i + 1: v for i, v in enumerate([
        26, 44, 70, 100, 134, 172, 196, 242, 292, 346,
        404, 466, 532, 581, 655, 733, 815, 901, 991, 1085,
        1156, 1258, 1364, 1474, 1588, 1706, 1828, 1921, 2051, 2185,
        2323, 2465, 2611, 2761, 2876, 3034, 3196, 3362, 3532, 3706,
    ])
}
_QR_ECC_CODEWORDS_PER_BLOCK_L = {
    i + 1: v for i, v in enumerate([
        7, 10, 15, 20, 26, 18, 20, 24, 30, 18,
        20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
        28, 28, 30, 30, 26, 28, 30, 30, 30, 30,
        30, 30, 30, 30, 30, 30, 30, 30, 30, 30,
    ])
}
_QR_ECC_BLOCKS_L = {
    i + 1: v for i, v in enumerate([
        1, 1, 1, 1, 1, 2, 2, 2, 2, 4,
        4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
        8, 9, 9, 10, 12, 12, 12, 13, 14, 15,
        16, 17, 18, 19, 19, 20, 21, 22, 24, 25,
    ])
}
_QR_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58],
    13: [6, 34, 62], 14: [6, 26, 46, 66], 15: [6, 26, 48, 70],
    16: [6, 26, 50, 74], 17: [6, 30, 54, 78], 18: [6, 30, 56, 82],
    19: [6, 30, 58, 86], 20: [6, 34, 62, 90], 21: [6, 28, 50, 72, 94],
    22: [6, 26, 50, 74, 98], 23: [6, 30, 54, 78, 102],
    24: [6, 28, 54, 80, 106], 25: [6, 32, 58, 84, 110],
    26: [6, 30, 58, 86, 114], 27: [6, 34, 62, 90, 118],
    28: [6, 26, 50, 74, 98, 122], 29: [6, 30, 54, 78, 102, 126],
    30: [6, 26, 52, 78, 104, 130], 31: [6, 30, 56, 82, 108, 134],
    32: [6, 34, 60, 86, 112, 138], 33: [6, 30, 58, 86, 114, 142],
    34: [6, 34, 62, 90, 118, 146], 35: [6, 30, 54, 78, 102, 126, 150],
    36: [6, 24, 50, 76, 102, 128, 154],
    37: [6, 28, 54, 80, 106, 132, 158],
    38: [6, 32, 58, 84, 110, 136, 162],
    39: [6, 26, 54, 82, 110, 138, 166],
    40: [6, 30, 58, 86, 114, 142, 170],
}


def _qr_blocks_l(version):
    total = _QR_TOTAL_CODEWORDS_L[version]
    ecc_len = _QR_ECC_CODEWORDS_PER_BLOCK_L[version]
    block_count = _QR_ECC_BLOCKS_L[version]
    short_blocks = block_count - (total % block_count)
    short_total_len = total // block_count
    blocks = []
    if short_blocks:
        blocks.append((short_blocks, short_total_len - ecc_len, ecc_len))
    long_blocks = block_count - short_blocks
    if long_blocks:
        blocks.append((long_blocks, short_total_len + 1 - ecc_len, ecc_len))
    return blocks


def _qr_data_capacity_l(version):
    total = _QR_TOTAL_CODEWORDS_L[version]
    return total - _QR_ECC_CODEWORDS_PER_BLOCK_L[version] * _QR_ECC_BLOCKS_L[version]


def _qr_svg(text):
    """Small QR encoder for byte-mode, ECC-L, versions 1-40. Returns SVG markup."""
    data = text.encode("utf-8")
    version = None
    for v in _QR_TOTAL_CODEWORDS_L:
        count_bits = 8 if v <= 9 else 16
        bit_len = 4 + count_bits + len(data) * 8
        if bit_len <= _qr_data_capacity_l(v) * 8:
            version = v
            break
    if version is None:
        raise ValueError("QR payload is too long")
    blocks = _qr_blocks_l(version)
    data_cap = sum(count * data_len for count, data_len, _ecc_len in blocks)
    bits = [0, 1, 0, 0]
    count_bits = 8 if version <= 9 else 16
    bits += [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, data_cap * 8 - len(bits))
    while len(bits) % 8:
        bits.append(0)
    codewords = [sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8)]
    pad = 0
    while len(codewords) < data_cap:
        codewords.append(0xEC if pad % 2 == 0 else 0x11)
        pad += 1

    data_blocks = []
    pos = 0
    for count, data_len, ecc_len in blocks:
        for _ in range(count):
            chunk = codewords[pos:pos + data_len]
            pos += data_len
            data_blocks.append((chunk, _qr_rs_encode(chunk, ecc_len)))
    final = []
    for i in range(max(len(b[0]) for b in data_blocks)):
        for db, _eb in data_blocks:
            if i < len(db):
                final.append(db[i])
    for i in range(max(len(b[1]) for b in data_blocks)):
        for _db, eb in data_blocks:
            if i < len(eb):
                final.append(eb[i])

    size = 21 + (version - 1) * 4
    mat = [[False] * size for _ in range(size)]
    res = [[False] * size for _ in range(size)]

    def set_mod(r, c, val, reserve=True):
        if 0 <= r < size and 0 <= c < size:
            mat[r][c] = bool(val)
            if reserve:
                res[r][c] = True

    def finder(r, c):
        for y in range(-1, 8):
            for x in range(-1, 8):
                rr, cc = r + y, c + x
                if 0 <= rr < size and 0 <= cc < size:
                    on = 0 <= y <= 6 and 0 <= x <= 6 and (y in (0, 6) or x in (0, 6) or (2 <= y <= 4 and 2 <= x <= 4))
                    set_mod(rr, cc, on)

    finder(0, 0); finder(0, size - 7); finder(size - 7, 0)
    for i in range(8, size - 8):
        set_mod(6, i, i % 2 == 0)
        set_mod(i, 6, i % 2 == 0)
    for r in _QR_ALIGN[version]:
        for c in _QR_ALIGN[version]:
            if res[r][c]:
                continue
            for y in range(-2, 3):
                for x in range(-2, 3):
                    set_mod(r + y, c + x, max(abs(x), abs(y)) != 1)
    set_mod(4 * version + 9, 8, True)
    _qr_reserve_format(res, size)
    if version >= 7:
        _qr_add_version(mat, res, version)

    bit_iter = ((cw >> i) & 1 for cw in final for i in range(7, -1, -1))
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not res[r][c]:
                    try:
                        mat[r][c] = bool(next(bit_iter))
                    except StopIteration:
                        mat[r][c] = False
        upward = not upward
        col -= 2

    best = None
    best_mask = 0
    for mask in range(8):
        trial = [row[:] for row in mat]
        for r in range(size):
            for c in range(size):
                if not res[r][c] and _qr_mask(mask, r, c):
                    trial[r][c] = not trial[r][c]
        _qr_add_format(trial, mask)
        score = _qr_penalty(trial)
        if best is None or score < best:
            best = score
            best_mask = mask
            mat = trial
    quiet = 4
    rects = []
    for r, row in enumerate(mat):
        for c, val in enumerate(row):
            if val:
                rects.append(f'<rect x="{c + quiet}" y="{r + quiet}" width="1" height="1"/>')
    dim = size + quiet * 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'shape-rendering="crispEdges" role="img" aria-label="MeshCore contact QR">'
        f'<rect width="{dim}" height="{dim}" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )


def _qr_gf_mul(x, y):
    z = 0
    while y:
        if y & 1:
            z ^= x
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
        y >>= 1
    return z


def _qr_rs_encode(data, ecc_len):
    gen = [1]
    root = 1
    for _ in range(ecc_len):
        gen = [_qr_gf_mul(coef, root) for coef in gen] + [0]
        for i in range(len(gen) - 1):
            gen[i + 1] ^= gen[i]
        root = _qr_gf_mul(root, 2)
    rem = [0] * ecc_len
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        for i, coef in enumerate(gen[:-1]):
            rem[i] ^= _qr_gf_mul(coef, factor)
    return rem


def _qr_reserve_format(res, size):
    for i in range(9):
        res[8][i] = True
        res[i][8] = True
    for i in range(8):
        res[8][size - 1 - i] = True
        res[size - 1 - i][8] = True


def _qr_format_bits(mask):
    data = (1 << 3) | mask  # ECC-L
    bits = data << 10
    gen = 0x537
    for i in range(14, 9, -1):
        if (bits >> i) & 1:
            bits ^= gen << (i - 10)
    return ((data << 10) | bits) ^ 0x5412


def _qr_add_format(mat, mask):
    size = len(mat)
    bits = _qr_format_bits(mask)
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8), (size - 5, 8), (size - 6, 8), (size - 7, 8), (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5), (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
    for i in range(15):
        bit = ((bits >> i) & 1) != 0
        r, c = coords1[i]; mat[r][c] = bit
        r, c = coords2[i]; mat[r][c] = bit


def _qr_add_version(mat, res, version):
    bits = version << 12
    gen = 0x1F25
    for i in range(17, 11, -1):
        if (bits >> i) & 1:
            bits ^= gen << (i - 12)
    bits = (version << 12) | bits
    size = len(mat)
    for i in range(18):
        bit = ((bits >> i) & 1) != 0
        r, c = i // 3, i % 3
        mat[r][size - 11 + c] = bit; res[r][size - 11 + c] = True
        mat[size - 11 + c][r] = bit; res[size - 11 + c][r] = True


def _qr_mask(mask, r, c):
    return [
        (r + c) % 2 == 0,
        r % 2 == 0,
        c % 3 == 0,
        (r + c) % 3 == 0,
        (r // 2 + c // 3) % 2 == 0,
        (r * c) % 2 + (r * c) % 3 == 0,
        ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ][mask]


def _qr_penalty(mat):
    size = len(mat)
    score = 0
    for rows in (mat, list(zip(*mat))):
        for row in rows:
            run = 1
            prev = row[0]
            for val in row[1:]:
                if val == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, val
            if run >= 5:
                score += 3 + run - 5
    for r in range(size - 1):
        for c in range(size - 1):
            if mat[r][c] == mat[r + 1][c] == mat[r][c + 1] == mat[r + 1][c + 1]:
                score += 3
    dark = sum(1 for row in mat for v in row if v)
    score += abs(dark * 20 - size * size * 10) // (size * size) * 10
    return score


def _mc_config_payload(data):
    endpoint = {
        "type": (data.get("type") or "serial").strip().lower(),
        "port": (data.get("port") or "").strip(),
        "usb_serial": (data.get("usb_serial") or "").strip(),
        "host": (data.get("host") or "").strip(),
        "tcp_port": data.get("tcp_port") or 4403,
        "bt_address": (data.get("bt_address") or data.get("address") or "").strip(),
        "bt_pin": (data.get("bt_pin") or data.get("pin") or "").strip(),
    }
    if endpoint["type"] in ("bt", "bluetooth"):
        endpoint["type"] = "ble"
    if endpoint["type"] not in ("serial", "tcp", "ble"):
        raise ValueError("type must be 'serial', 'tcp', or 'ble'")
    if endpoint["type"] == "serial" and not endpoint["port"] and not endpoint["usb_serial"]:
        raise ValueError("Select a serial device")
    if endpoint["type"] == "tcp":
        if not endpoint["host"]:
            raise ValueError("Enter an IP address or hostname")
        try:
            endpoint["tcp_port"] = int(endpoint["tcp_port"])
        except (TypeError, ValueError):
            raise ValueError("tcp_port must be a number")
    if endpoint["type"] == "ble" and not endpoint["bt_address"]:
        raise ValueError("Enter a Bluetooth address")
    return endpoint


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


def _mc_contact_last_seen_ts(contact):
    return int(contact.get("last_seen_ts") or contact.get("last_advert") or 0)


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
    raw_last_seen_ts = _mc_contact_last_seen_ts(contact)
    last_seen_ts = min(raw_last_seen_ts, now) if raw_last_seen_ts else 0
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
    """Export a connected MC contact as an official meshcore:// share URI plus QR SVG."""
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
        if state.get("status") != "connected" or full_key not in live_contacts:
            return jsonify({
                "error": "MC radio must be connected and the contact must be live on the radio to export an official QR.",
                "details": _serialize_mc_contact(full_key, contact, source_state=_mc_contact_source_state(full_key, live_contacts, archive_contacts)) if contact else None,
            }), 503
        uri = export_mc_contact_uri(radio_id, full_key)
        return jsonify({
            "ok": True,
            "radio_id": radio_id,
            "contact_id": full_key[:12],
            "uri": uri,
            "qr_svg": _qr_svg(uri),
            "details": _serialize_mc_contact(full_key, contact, source_state=_mc_contact_source_state(full_key, live_contacts, archive_contacts)),
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
# MC config-only tool (temporary connection, not added to OM runtime)
# ---------------------------------------------------------------------------

@bp.route("/api/mc/config_tool", methods=["POST"])
def api_mc_config_tool():
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "query").strip().lower()
    try:
        endpoint = _mc_config_payload(data)
        params = {}
        if action == "query":
            timeout = 95
        elif action == "set_radio":
            params = _validate_mc_radio_params(data, include_repeat=True)
            timeout = 60
        elif action == "set_tx_power":
            params["tx_power"] = int(data["tx_power"])
            if params["tx_power"] < 1:
                raise ValueError("tx_power must be >= 1 dBm")
            timeout = 45
        elif action == "set_name":
            name = (data.get("name") or "").strip()
            if not name:
                raise ValueError("name is required")
            if len(name) > 32:
                raise ValueError("name too long (max 32 chars)")
            params["name"] = name
            timeout = 45
        elif action == "set_coords":
            params["lat"] = float(data["lat"])
            params["lon"] = float(data["lon"])
            if not (-90 <= params["lat"] <= 90) or not (-180 <= params["lon"] <= 180):
                raise ValueError("Invalid coordinates")
            timeout = 45
        elif action == "set_channel":
            params["idx"] = int(data["idx"])
            params["name"] = (data.get("name") or "").strip()
            params["key"] = (data.get("key") or "").strip().lower()
            if not (0 <= params["idx"] <= 15):
                raise ValueError("channel index must be 0-15")
            if not params["name"]:
                raise ValueError("name is required")
            if len(params["name"]) > 32:
                raise ValueError("name too long (max 32 chars)")
            if params["key"] and (len(params["key"]) != 32 or not all(c in "0123456789abcdef" for c in params["key"])):
                raise ValueError("key must be exactly 32 hex characters (16 bytes)")
            timeout = 45
        elif action == "reboot":
            timeout = 45
        else:
            raise ValueError("Unsupported action")
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    try:
        return jsonify(mc_config_tool(endpoint, action, params=params, timeout=timeout))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        log.warning(f"[MC config] {action} failed: {e}")
        return jsonify({"error": str(e)}), 500


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


def _mc_channel_share_uri(details):
    params = {
        "v": "1",
        "idx": str(details.get("idx", "")),
        "name": details.get("name") or "",
        "secret": details.get("secret_hex") or "",
        "hash": details.get("hash") or "",
    }
    return "overmesh://mc/channel?" + urlencode({k: v for k, v in params.items() if v})


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

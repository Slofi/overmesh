import hashlib
import os

from flask import Blueprint, jsonify, request

from config import CONFIG, CONFIG_LOCK, save_config
from db import delete_channel_messages
from helpers import push_to_sse
from mesh import DEVICE_ROLES, LORA_REGIONS, MODEM_PRESETS, get_iface_by_radio
from state import chat_lock, chat_messages, connections, connections_lock
import logging
log = logging.getLogger(__name__)

bp = Blueprint('radio', __name__)


@bp.route("/api/radio/<radio_id>/config")
def api_radio_config_get(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        # Owner info from live node data
        local_info = getattr(iface, "myInfo", None)
        local_num  = getattr(local_info, "my_node_num", None)
        user = {}
        if local_num and iface.nodes:
            for n in iface.nodes.values():
                if n.get("num") == local_num:
                    user = n.get("user", {})
                    break

        lc = getattr(iface.localNode, "localConfig",  None)
        mc = getattr(iface.localNode, "moduleConfig", None)

        def _int(obj, *attrs):
            try:
                v = obj
                for a in attrs: v = getattr(v, a)
                return int(v)
            except Exception: return 0

        def _bool(obj, *attrs):
            try:
                v = obj
                for a in attrs: v = getattr(v, a)
                return bool(v)
            except Exception: return False

        def _str(obj, *attrs):
            try:
                v = obj
                for a in attrs: v = getattr(v, a)
                return str(v or "")
            except Exception: return ""

        # device + lora
        role      = _int(lc, "device", "role")
        region    = _int(lc, "lora", "region")
        preset    = _int(lc, "lora", "modem_preset")
        tx_power  = _int(lc, "lora", "tx_power")
        hop_limit = _int(lc, "lora", "hop_limit") or 3

        # position — get live position data for local node (used for coords + precisionBits)
        node_hex = "!" + hex(local_num)[2:] if local_num else None
        pos_data = {}
        if node_hex and iface.nodes:
            pos_data = iface.nodes.get(node_hex, {}).get("position", {})
        if not pos_data and local_num and iface.nodesByNum:
            pos_data = iface.nodesByNum.get(local_num, {}).get("position", {})

        # node config entry for persistent fallback values (set by OM when user saves)
        node_cfg_entry = next((n for n in CONFIG.get("nodes", []) if n["id"] == radio_id), {})

        gps_mode             = _int(lc,  "position", "gps_mode")
        pos_broadcast_secs   = _int(lc,  "position", "position_broadcast_secs")
        smart_position       = _bool(lc, "position", "position_broadcast_smart_enabled")
        fixed_position       = _bool(lc, "position", "fixed_position")

        # precision_bits: read from node's position data (live), fallback to our saved config value
        pos_precision = (pos_data.get("precisionBits")
                         or node_cfg_entry.get("precision_bits")
                         or 13)

        # fixed coords: live position data → OM-saved config fallback
        fixed_lat = fixed_lon = fixed_alt = None
        if fixed_position:
            if pos_data.get("latitude") is not None:
                fixed_lat = pos_data["latitude"]
                fixed_lon = pos_data.get("longitude")
                fixed_alt = pos_data.get("altitude", 0)
            if fixed_lat is None:
                fixed_lat = node_cfg_entry.get("fixed_lat")
                fixed_lon = node_cfg_entry.get("fixed_lon")
                fixed_alt = node_cfg_entry.get("fixed_alt", 0)

        # power
        power_saving        = _bool(lc, "power", "is_power_saving")
        shutdown_after_secs = _int(lc,  "power", "on_battery_shutdown_after_secs")

        # display
        screen_on_secs = _int(lc,  "display", "screen_on_secs")
        flip_screen    = _bool(lc, "display", "flip_screen")
        display_units  = _int(lc,  "display", "units")

        # telemetry (module config) — INT32_MAX (2147483647) means "use firmware default", normalize to 0
        tel_device = _int(mc, "telemetry", "device_update_interval")
        tel_env    = _int(mc, "telemetry", "environment_update_interval")
        if tel_device == 2147483647: tel_device = 0
        if tel_env    == 2147483647: tel_env    = 0

        # mqtt (module config)
        mqtt_enabled    = _bool(mc, "mqtt", "enabled")
        mqtt_address    = _str(mc,  "mqtt", "address")
        mqtt_username   = _str(mc,  "mqtt", "username")
        mqtt_pwd_set    = bool(_str(mc, "mqtt", "password"))
        mqtt_encryption = _bool(mc, "mqtt", "encryption_enabled")
        mqtt_json       = _bool(mc, "mqtt", "json_enabled")
        mqtt_tls        = _bool(mc, "mqtt", "tls_enabled")
        mqtt_map        = _bool(mc, "mqtt", "map_reporting_enabled")

        # bluetooth
        bt_enabled   = _bool(lc, "bluetooth", "enabled")
        bt_mode      = _int(lc,  "bluetooth", "mode")
        bt_fixed_pin = _int(lc,  "bluetooth", "fixed_pin")

        # network / wifi
        wifi_enabled  = _bool(lc, "network", "wifi_enabled")
        wifi_ap_mode  = _bool(lc, "network", "wifi_ap_mode")
        wifi_ssid     = _str(lc,  "network", "wifi_ssid")
        wifi_psk_set  = bool(_str(lc, "network", "wifi_psk"))

        return jsonify({
            "long_name":    user.get("longName",  ""),
            "short_name":   user.get("shortName", ""),
            "hw_model":     user.get("hwModel",   ""),
            # device
            "role":          role,
            "device_roles":  DEVICE_ROLES,
            # lora
            "region":        region,
            "modem_preset":  preset,
            "tx_power":      tx_power,
            "hop_limit":     hop_limit,
            "lora_regions":  LORA_REGIONS,
            "modem_presets": MODEM_PRESETS,
            # position
            "gps_mode":            gps_mode,
            "pos_broadcast_secs":  pos_broadcast_secs,
            "smart_position":      smart_position,
            "pos_precision":       pos_precision,
            "fixed_position":      fixed_position,
            "fixed_lat":           fixed_lat,
            "fixed_lon":           fixed_lon,
            "fixed_alt":           fixed_alt or 0,
            # power
            "power_saving":        power_saving,
            "shutdown_after_secs": shutdown_after_secs,
            # display
            "screen_on_secs": screen_on_secs,
            "flip_screen":    flip_screen,
            "display_units":  display_units,
            # telemetry
            "tel_device": tel_device,
            "tel_env":    tel_env,
            # mqtt
            "mqtt_enabled":    mqtt_enabled,
            "mqtt_address":    mqtt_address,
            "mqtt_username":   mqtt_username,
            "mqtt_pwd_set":    mqtt_pwd_set,
            "mqtt_encryption": mqtt_encryption,
            "mqtt_json":       mqtt_json,
            "mqtt_tls":        mqtt_tls,
            "mqtt_map":        mqtt_map,
            # bluetooth
            "bt_enabled":   bt_enabled,
            "bt_mode":      bt_mode,
            "bt_fixed_pin": bt_fixed_pin,
            # network
            "wifi_enabled":  wifi_enabled,
            "wifi_ap_mode":  wifi_ap_mode,
            "wifi_ssid":     wifi_ssid,
            "wifi_psk_set":  wifi_psk_set,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/announce", methods=["POST"])
def api_radio_announce(radio_id):
    """Re-broadcast NodeInfo (publicKey, name) to the mesh."""
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        local_num = getattr(iface.myInfo, "my_node_num", None)
        node_hex = "!" + hex(local_num)[2:] if local_num else None
        u = (iface.nodes or {}).get(node_hex, {}).get("user", {}) if node_hex else {}
        long_name  = u.get("longName",  "")
        short_name = u.get("shortName", "")
        iface.localNode.setOwner(long_name=long_name, short_name=short_name)
        return jsonify({"ok": True, "long_name": long_name, "short_name": short_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/owner", methods=["POST"])
def api_radio_owner_set(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data       = request.get_json(silent=True) or {}
    long_name  = (data.get("long_name")  or "").strip()[:39]
    short_name = (data.get("short_name") or "").strip()[:4]
    if not long_name or not short_name:
        return jsonify({"error": "Long name and short name are required"}), 400
    try:
        iface.localNode.setOwner(long_name=long_name, short_name=short_name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/device", methods=["POST"])
def api_radio_config_device(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        role = int(data.get("role", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "role must be a number"}), 400
    try:
        iface.localNode.localConfig.device.role = role
        iface.localNode.writeConfig("device")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/lora", methods=["POST"])
def api_radio_config_lora(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        int_fields = {k: int(data[k]) for k in ("region","modem_preset","tx_power","hop_limit") if k in data}
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"Invalid numeric value: {e}"}), 400
    try:
        lc = iface.localNode.localConfig
        if "region"       in int_fields: lc.lora.region       = int_fields["region"]
        if "modem_preset" in int_fields: lc.lora.modem_preset = int_fields["modem_preset"]
        if "tx_power"     in int_fields: lc.lora.tx_power     = int_fields["tx_power"]
        if "hop_limit"    in int_fields: lc.lora.hop_limit    = int_fields["hop_limit"]
        iface.localNode.writeConfig("lora")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/channels")
def api_radio_channels_get(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        result = []
        for i, ch in enumerate(iface.localNode.channels):
            role     = int(getattr(ch, "role", 0))
            settings = getattr(ch, "settings", None)
            name     = (getattr(settings, "name", "") or "") if settings else ""
            psk      = bytes(getattr(settings, "psk", b"")) if settings else b""
            result.append({
                "index":   i,
                "name":    name,
                "role":    role,
                "psk_set": len(psk) > 0,
                "psk_len": len(psk),
                "psk_fingerprint": hashlib.sha256(psk).hexdigest()[:16] if psk else None,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/position", methods=["POST"])
def api_radio_config_position(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        from meshtastic.protobuf import mesh_pb2, admin_pb2

        lc  = iface.localNode.localConfig
        pos = lc.position
        if "gps_mode" in data:
            try:    pos.gps_mode = int(data["gps_mode"])
            except (TypeError, ValueError): return jsonify({"error": "gps_mode must be a number"}), 400
        if "pos_broadcast_secs" in data:
            try:    pos.position_broadcast_secs = int(data["pos_broadcast_secs"])
            except (TypeError, ValueError): return jsonify({"error": "pos_broadcast_secs must be a number"}), 400
        if "smart_position"     in data:
            try:    pos.position_broadcast_smart_enabled = bool(data["smart_position"])
            except AttributeError: pass
        # NOTE: position_precision field does not exist in current meshtastic protobuf —
        # precision_bits is set on the Position message (admin), not via PositionConfig
        fixed = data.get("fixed_position")
        if fixed is not None:
            pos.fixed_position = bool(fixed)
        iface.localNode.writeConfig("position")

        local_num = getattr(iface.myInfo, "my_node_num", None)
        node_cfg_entry = next((n for n in CONFIG.get("nodes", []) if n["id"] == radio_id), None)
        try:
            precision = int(data["pos_precision"]) if "pos_precision" in data else None
        except (TypeError, ValueError):
            return jsonify({"error": "pos_precision must be a number"}), 400

        if fixed and data.get("lat") is not None and data.get("lon") is not None:
            try:
                lat = float(data["lat"]); lon = float(data["lon"]); alt = int(data.get("alt", 0))
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid coordinates or altitude"}), 400

            # Build position admin message — includes precision_bits so it's actually sent to node
            p = mesh_pb2.Position()
            if lat != 0.0: p.latitude_i  = int(lat * 1e7)
            if lon != 0.0: p.longitude_i = int(lon * 1e7)
            if alt != 0:   p.altitude    = alt
            if precision is not None: p.precision_bits = precision
            try:
                a = admin_pb2.AdminMessage()
                a.set_fixed_position.CopyFrom(p)
                iface.localNode.ensureSessionKey()
                iface.localNode._sendAdmin(a)
            except Exception:
                pass  # best-effort

            # Update in-memory position immediately
            try:
                if local_num is not None and local_num in iface.nodesByNum:
                    mem_pos = {"latitude": lat, "longitude": lon, "altitude": alt,
                               "latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7),
                               "fixedPosition": True}
                    if precision is not None:
                        mem_pos["precisionBits"] = precision
                    iface.nodesByNum[local_num]["position"] = mem_pos
            except Exception:
                pass

            # Cache in connections (in-memory session)
            with connections_lock:
                entry = connections.get(radio_id)
                if entry is not None:
                    entry["fixed_lat"] = lat
                    entry["fixed_lon"] = lon

            # Persist to config.json — survives OM service restarts
            if node_cfg_entry is not None:
                with CONFIG_LOCK:
                    node_cfg_entry["fixed_lat"] = lat
                    node_cfg_entry["fixed_lon"] = lon
                    node_cfg_entry["fixed_alt"] = alt
                    if precision is not None:
                        node_cfg_entry["precision_bits"] = precision
                    save_config()

        elif fixed is False:
            try:    iface.localNode.removeFixedPosition()
            except Exception: pass
            with connections_lock:
                entry = connections.get(radio_id)
                if entry is not None:
                    entry.pop("fixed_lat", None)
                    entry.pop("fixed_lon", None)
            if node_cfg_entry is not None:
                with CONFIG_LOCK:
                    for k in ("fixed_lat", "fixed_lon", "fixed_alt", "precision_bits"):
                        node_cfg_entry.pop(k, None)
                    save_config()

        elif fixed is None and precision is not None:
            # Precision-only change (fixed_position not being toggled, no coord update)
            if node_cfg_entry is not None:
                with CONFIG_LOCK:
                    node_cfg_entry["precision_bits"] = precision
                    save_config()
            try:
                if local_num is not None and local_num in iface.nodesByNum:
                    iface.nodesByNum[local_num].setdefault("position", {})["precisionBits"] = precision
            except Exception:
                pass

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/power", methods=["POST"])
def api_radio_config_power(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        pwr = iface.localNode.localConfig.power
        if "power_saving"        in data: pwr.is_power_saving              = bool(data["power_saving"])
        if "shutdown_after_secs" in data: pwr.on_battery_shutdown_after_secs = int(data["shutdown_after_secs"])
        iface.localNode.writeConfig("power")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/display", methods=["POST"])
def api_radio_config_display(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        disp = iface.localNode.localConfig.display
        if "screen_on_secs" in data: disp.screen_on_secs = int(data["screen_on_secs"])
        if "flip_screen"    in data: disp.flip_screen    = bool(data["flip_screen"])
        if "display_units"  in data: disp.units          = int(data["display_units"])
        iface.localNode.writeConfig("display")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/telemetry", methods=["POST"])
def api_radio_config_telemetry(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        tel = iface.localNode.moduleConfig.telemetry
        if "tel_device" in data: tel.device_update_interval      = int(data["tel_device"])
        if "tel_env"    in data: tel.environment_update_interval  = int(data["tel_env"])
        iface.localNode.writeModuleConfig("telemetry")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/mqtt", methods=["POST"])
def api_radio_config_mqtt(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        mqtt = iface.localNode.moduleConfig.mqtt
        if "mqtt_enabled"    in data: mqtt.enabled            = bool(data["mqtt_enabled"])
        if "mqtt_address"    in data: mqtt.address            = str(data["mqtt_address"])
        if "mqtt_username"   in data: mqtt.username           = str(data["mqtt_username"])
        if data.get("mqtt_password"):  mqtt.password          = str(data["mqtt_password"])
        if "mqtt_encryption" in data: mqtt.encryption_enabled = bool(data["mqtt_encryption"])
        if "mqtt_json"       in data: mqtt.json_enabled       = bool(data["mqtt_json"])
        if "mqtt_tls"        in data: mqtt.tls_enabled        = bool(data["mqtt_tls"])
        if "mqtt_map"        in data: mqtt.map_reporting_enabled = bool(data["mqtt_map"])
        iface.localNode.writeModuleConfig("mqtt")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/bluetooth", methods=["POST"])
def api_radio_config_bluetooth(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    if "bt_mode" in data:
        try:
            if int(data["bt_mode"]) not in (0, 1, 2):
                return jsonify({"error": "bt_mode must be 0, 1 or 2"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "bt_mode must be a number"}), 400
    if "bt_fixed_pin" in data:
        try:
            pin = int(data["bt_fixed_pin"])
        except (TypeError, ValueError):
            return jsonify({"error": "bt_fixed_pin must be a number"}), 400
        if not (0 <= pin <= 999999):
            return jsonify({"error": "bt_fixed_pin must be 0–999999"}), 400
    try:
        bt = iface.localNode.localConfig.bluetooth
        if "bt_enabled"   in data: bt.enabled   = bool(data["bt_enabled"])
        if "bt_mode"      in data: bt.mode       = int(data["bt_mode"])
        if "bt_fixed_pin" in data: bt.fixed_pin  = int(data["bt_fixed_pin"])
        iface.localNode.writeConfig("bluetooth")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/config/network", methods=["POST"])
def api_radio_config_network(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    if "wifi_ssid" in data and len(str(data["wifi_ssid"])) > 32:
        return jsonify({"error": "SSID too long (max 32 characters)"}), 400
    try:
        net = iface.localNode.localConfig.network
        if "wifi_enabled" in data: net.wifi_enabled  = bool(data["wifi_enabled"])
        if "wifi_ap_mode" in data: net.wifi_ap_mode  = bool(data["wifi_ap_mode"])
        if "wifi_ssid"    in data: net.wifi_ssid      = str(data["wifi_ssid"])
        if data.get("wifi_psk"):    net.wifi_psk       = str(data["wifi_psk"])
        iface.localNode.writeConfig("network")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/channels/<int:ch_index>", methods=["POST"])
def api_radio_channel_set(radio_id, ch_index):
    if ch_index < 0 or ch_index > 7:
        return jsonify({"error": "Channel index must be 0–7"}), 400
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        role = int(data.get("role", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "role must be a number"}), 400
    name = (data.get("name") or "")
    if len(name) > 11:
        return jsonify({"error": f"Channel name too long ({len(name)} chars, max 11)"}), 400
    psk_type = data.get("psk_type", "keep")   # keep / default / random / custom
    psk_hex  = data.get("psk_hex", "").replace(":", "").replace(" ", "")
    try:
        # If promoting to PRIMARY, demote existing PRIMARY to SECONDARY first
        if role == 1:
            for i, c in enumerate(iface.localNode.channels):
                if i != ch_index and int(getattr(c, "role", 0)) == 1:
                    iface.localNode.channels[i].role = 2
                    iface.localNode.writeChannel(i)

        ch = iface.localNode.channels[ch_index]
        ch.role = role

        if role == 0:   # DISABLED — clear settings
            ch.settings.name = ""
            ch.settings.psk  = b""
        else:
            ch.settings.name = name
            if psk_type == "default":
                ch.settings.psk = bytes([1])        # firmware expands to default Meshtastic key
            elif psk_type == "random":
                ch.settings.psk = os.urandom(32)
            elif psk_type == "custom":
                if not psk_hex:
                    return jsonify({"error": "Custom PSK is empty"}), 400
                try:
                    psk_bytes = bytes.fromhex(psk_hex)
                except ValueError:
                    try:
                        import base64
                        psk_bytes = base64.b64decode(psk_hex, validate=True)
                    except Exception:
                        return jsonify({"error": "Invalid key — enter as hex or base64"}), 400
                ch.settings.psk = psk_bytes
            # else "keep" — don't touch psk

        iface.localNode.writeChannel(ch_index)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/channels/<int:ch_index>/history", methods=["POST"])
def api_radio_channel_history_clear(radio_id, ch_index):
    if ch_index < 0 or ch_index > 7:
        return jsonify({"error": "Channel index must be 0–7"}), 400
    if not any(n.get("id") == radio_id for n in CONFIG.get("nodes", [])):
        return jsonify({"error": "Radio not found"}), 404
    try:
        removed_db = delete_channel_messages(radio_id, ch_index)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with chat_lock:
        before = len(chat_messages)
        chat_messages[:] = [
            m for m in chat_messages
            if not (
                m.get("radio_id") == radio_id
                and not m.get("is_dm")
                and int(m.get("channel", 0) or 0) == ch_index
            )
        ]
        removed_mem = before - len(chat_messages)
    push_to_sse({
        "type": "mt_channel_history_cleared",
        "radio_id": radio_id,
        "channel": ch_index,
    })
    return jsonify({"ok": True, "removed_db": removed_db, "removed_mem": removed_mem})


@bp.route("/api/radio/<radio_id>/reboot", methods=["POST"])
def api_radio_reboot(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        iface.localNode.reboot(secs=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/radio/<radio_id>/shutdown", methods=["POST"])
def api_radio_shutdown(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        iface.localNode.shutdown(secs=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

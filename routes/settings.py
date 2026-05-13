import json
import ipaddress
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from config import BASE_DIR, CONFIG, CONFIG_LOCK, DATA_DIR, save_config
from db import get_auth_setting, set_auth_setting
from cross import _normalize_rule, get_cross_config
from helpers import push_to_sse
from mesh import connect_node
from state import chat_lock, chat_messages, connections, connections_lock, mc_connections, mc_connections_lock
import logging
log = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)

_UPDATE_LOCK = threading.Lock()
_UPDATE_STATE = {
    "running": False,
    "ok": None,
    "message": "",
    "log": [],
    "updated_at": None,
}
_UPDATE_STATUS_IGNORED_PATHS = {"secret.key"}


def _app_settings_payload():
    app_cfg = dict(CONFIG.get("app") or {})
    bridge_cfg = CONFIG.get("bridge") or {}
    webhook_cfg = bridge_cfg.get("webhooks") or {}
    ingest_cfg = bridge_cfg.get("ingest") or {}
    urls = webhook_cfg.get("urls") or []
    if isinstance(urls, str):
        urls_text = urls
    else:
        urls_text = "\n".join(str(u) for u in urls if u)
    app_cfg.setdefault("font_size", "medium")
    app_cfg.setdefault("accent_color", "#4ade80")
    app_cfg.setdefault("om_manual_lat", None)
    app_cfg.setdefault("om_manual_lon", None)
    app_cfg.setdefault("sound_notify_messages", True)
    app_cfg.setdefault("sound_notify_radio_connected", True)
    app_cfg.setdefault("sound_notify_nodes", True)
    app_cfg.setdefault("distance_unit", "km")
    app_cfg.setdefault("time_format", "24h")
    app_cfg.setdefault("date_format", "eu")
    app_cfg.setdefault("bridge_webhooks_enabled", bool(webhook_cfg.get("enabled")))
    app_cfg.setdefault("bridge_webhook_urls", urls_text)
    app_cfg.setdefault("bridge_webhook_secret", webhook_cfg.get("secret") or "")
    app_cfg.setdefault("bridge_ingest_enabled", bool(ingest_cfg.get("enabled")))
    app_cfg.setdefault("bridge_ingest_token", ingest_cfg.get("token") or "")
    return app_cfg


def _has_any_mc_nodes():
    return bool(CONFIG.get("mc_nodes", []))


def _node_enabled(node):
    return node.get("enabled", True) is not False


def _active_nodes(nodes):
    return [n for n in nodes if _node_enabled(n)]


def _can_remove_mt_node():
    """Allow removing the last MT radio only if the app still has MC radios configured."""
    return len(CONFIG["nodes"]) > 1 or _has_any_mc_nodes()


def _parse_mc_path_hash_mode(value):
    try:
        mode = int(value)
    except (TypeError, ValueError):
        raise ValueError("path_hash_mode must be an integer")
    if mode < 0 or mode > 2:
        raise ValueError("path_hash_mode must be between 0 and 2")
    return mode


def _settings_local_request():
    addr = request.remote_addr or ""
    if addr == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 4:
        trusted_v4 = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),  # Tailscale/CGNAT admin networks
        )
        return any(ip in net for net in trusted_v4)
    if ip.version == 6:
        return ip.is_private or ip.is_link_local
    return False


def _git_cmd(args, timeout=30, check=False):
    result = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if check and result.returncode != 0:
        msg = err or out or f"git {' '.join(args)} failed"
        raise RuntimeError(msg)
    return result.returncode, out, err


def _git_status_path(line):
    if not line or len(line) < 4:
        return ""
    path = line[3:].strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def _filter_update_status_lines(status):
    lines = status.splitlines() if status else []
    return [
        line for line in lines
        if _git_status_path(line) not in _UPDATE_STATUS_IGNORED_PATHS
    ]


def _app_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _update_append(line):
    with _UPDATE_LOCK:
        _UPDATE_STATE["log"].append(line)
        _UPDATE_STATE["log"] = _UPDATE_STATE["log"][-80:]
        _UPDATE_STATE["updated_at"] = int(time.time())


def _git_info(fetch=False):
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return {"managed": False, "error": "This install is not a Git checkout."}

    info = {"managed": True}
    _, branch, _ = _git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    _, commit, _ = _git_cmd(["rev-parse", "--short", "HEAD"], timeout=10)
    _, full_commit, _ = _git_cmd(["rev-parse", "HEAD"], timeout=10)
    _, remote, _ = _git_cmd(["config", "--get", "remote.origin.url"], timeout=10)
    info.update({
        "version": _app_version(),
        "branch": branch or "unknown",
        "commit": commit or "unknown",
        "full_commit": full_commit or "",
        "remote": remote or "",
    })

    rc, status, _ = _git_cmd(["status", "--porcelain"], timeout=10)
    status_lines = _filter_update_status_lines(status) if rc == 0 else []
    info["dirty"] = bool(status_lines) if rc == 0 else True
    info["dirty_summary"] = status_lines[:12]

    if fetch:
        frc, fout, ferr = _git_cmd(["fetch", "--prune", "origin"], timeout=45)
        info["fetch_ok"] = frc == 0
        if frc != 0:
            info["fetch_error"] = ferr or fout or "Fetch failed."

    upstream = "origin/main"
    rc, remote_commit, _ = _git_cmd(["rev-parse", "--short", upstream], timeout=10)
    if rc == 0 and remote_commit:
        info["remote_commit"] = remote_commit
        rc, counts, _ = _git_cmd(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], timeout=10)
        if rc == 0 and counts:
            parts = counts.split()
            if len(parts) == 2:
                info["ahead"] = int(parts[0])
                info["behind"] = int(parts[1])
                info["update_available"] = info["behind"] > 0
    else:
        info["remote_commit"] = None
        info["update_available"] = False
    return info


def _run_update_job():
    with _UPDATE_LOCK:
        _UPDATE_STATE.update({
            "running": True,
            "ok": None,
            "message": "Updating...",
            "log": [],
            "updated_at": int(time.time()),
        })
    try:
        _update_append("Checking repository state...")
        info = _git_info(fetch=True)
        if not info.get("managed"):
            raise RuntimeError(info.get("error") or "Not a Git checkout.")
        if info.get("ahead", 0) > 0:
            raise RuntimeError("Local commits are ahead of origin. Push or reconcile before updating.")
        if not info.get("update_available"):
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({"running": False, "ok": True, "message": "Already up to date."})
            _update_append("Already up to date.")
            return

        rc, changed, _ = _git_cmd(["diff", "--name-only", "HEAD", "origin/main"], timeout=15)
        changed_files = set(changed.splitlines()) if rc == 0 and changed else set()

        if info.get("dirty"):
            _update_append("Local changes detected — stashing before update...")
            _git_cmd(["stash", "--include-untracked"], timeout=15)

        _update_append("Resetting to origin/main...")
        _git_cmd(["reset", "--hard", "origin/main"], timeout=60, check=True)

        if "requirements.txt" in changed_files:
            _update_append("requirements.txt changed; installing Python dependencies...")
            pip_cmd = [sys.executable or "python3", "-m", "pip", "install", "-r", "requirements.txt"]
            if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
                pip_cmd.append("--user")
            pip = subprocess.run(pip_cmd, cwd=BASE_DIR, capture_output=True, text=True, timeout=180, check=False)
            if pip.returncode != 0 and "externally-managed-environment" in (pip.stderr or ""):
                _update_append("System Python blocks user installs; retrying with --break-system-packages...")
                pip = subprocess.run(
                    pip_cmd + ["--break-system-packages"],
                    cwd=BASE_DIR,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            if pip.returncode != 0:
                raise RuntimeError((pip.stderr or pip.stdout or "pip install failed").strip())
            _update_append("Dependencies updated.")

        final = _git_info(fetch=False)
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                "running": False,
                "ok": True,
                "message": f"Updated to {final.get('commit', 'latest')}. Restart required.",
            })
        _update_append("Update complete. Restart required.")
    except Exception as e:
        log.warning(f"Update failed: {e}")
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                "running": False,
                "ok": False,
                "message": str(e),
                "updated_at": int(time.time()),
            })
        _update_append(f"Error: {e}")


@bp.route("/api/settings/ports")
def api_settings_ports():
    import serial.tools.list_ports
    all_ports = serial.tools.list_ports.comports()
    port_by_serial = {}
    for p in all_ports:
        if p.serial_number and p.serial_number not in port_by_serial:
            port_by_serial[p.serial_number] = p.device

    # Resolve each active node to its actual physical device path. When multiple
    # ports share the same usb_serial (e.g. CP2102 clones all reporting "0001"),
    # prefer the node's configured port so each node claims a distinct device.
    def _resolve_node_device(n):
        serial = (n.get("usb_serial") or "").strip()
        configured = (n.get("port") or "").strip()
        if not serial:
            return configured or None
        matches = [p.device for p in all_ports if p.serial_number == serial]
        if not matches:
            return configured or None
        return configured if configured in matches else matches[0]

    used_devices = set()
    for n in _active_nodes(CONFIG.get("nodes", [])):
        if (n.get("type") or "serial") == "serial":
            d = _resolve_node_device(n)
            if d:
                used_devices.add(d)
    for n in _active_nodes(CONFIG.get("mc_nodes", [])):
        if (n.get("type") or "serial") == "serial":
            d = _resolve_node_device(n)
            if d:
                used_devices.add(d)

    ports = []
    for p in all_ports:
        ports.append({
            "device":      p.device,
            "description": p.description or p.device,
            "usb_serial":  p.serial_number or "",
            "vid":         p.vid,
            "pid":         p.pid,
            "in_use":      p.device in used_devices,
        })
    ports.sort(key=lambda x: x["device"])
    return jsonify({"ports": ports})


@bp.route("/api/settings/update/status")
def api_settings_update_status():
    if not _settings_local_request():
        return jsonify({"error": "Updater is only available from the local machine."}), 403
    fetch = request.args.get("fetch") in ("1", "true", "yes")
    try:
        info = _git_info(fetch=fetch)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with _UPDATE_LOCK:
        state = dict(_UPDATE_STATE)
    return jsonify({"info": info, "state": state})


@bp.route("/api/settings/update/run", methods=["POST"])
def api_settings_update_run():
    if not _settings_local_request():
        return jsonify({"error": "Updater is only available from the local machine."}), 403
    with _UPDATE_LOCK:
        if _UPDATE_STATE.get("running"):
            return jsonify({"error": "Update already running.", "state": dict(_UPDATE_STATE)}), 409
        _UPDATE_STATE.update({
            "running": True,
            "ok": None,
            "message": "Starting update...",
            "log": ["Starting update..."],
            "updated_at": int(time.time()),
        })
    threading.Thread(target=_run_update_job, daemon=True).start()
    return jsonify({"ok": True, "state": dict(_UPDATE_STATE)})


@bp.route("/api/settings/nodes")
def api_settings_nodes():
    with connections_lock:
        statuses = {k: v.get("status", "disconnected") for k, v in connections.items()}
    nodes = [
        {
            "id":         n["id"],
            "name":       n["name"],
            "port":       n.get("port", ""),
            "usb_serial": n.get("usb_serial", ""),
            "enabled":    n.get("enabled", True),
            "status":     statuses.get(n["id"], "disconnected"),
        }
        for n in CONFIG["nodes"]
    ]
    return jsonify({"nodes": nodes})


@bp.route("/api/settings/nodes/add", methods=["POST"])
def api_settings_nodes_add():
    data      = request.get_json(silent=True) or {}
    name      = (data.get("name") or "").strip()
    node_type = (data.get("type") or "serial").strip()
    if node_type not in ("serial", "tcp"):
        return jsonify({"error": "type must be 'serial' or 'tcp'"}), 400
    if not name:
        return jsonify({"error": "Name is required"}), 400
    node_id  = f"node_{uuid.uuid4().hex[:12]}"
    new_node = {"id": node_id, "name": name, "enabled": True, "type": node_type}
    if node_type == "tcp":
        host = (data.get("host") or "").strip()
        try:
            tcp_port = int(data.get("tcp_port") or 4403)
        except (TypeError, ValueError):
            return jsonify({"error": "tcp_port must be a number"}), 400
        if not host:
            return jsonify({"error": "Enter an IP address or hostname"}), 400
        if any(n.get("host") == host and n.get("type") == "tcp" for n in CONFIG["nodes"]):
            return jsonify({"error": f"{host} is already configured"}), 400
        new_node["host"]     = host
        new_node["tcp_port"] = tcp_port
        new_node["port"]     = f"{host}:{tcp_port}"  # display only
    else:
        usb_serial = (data.get("usb_serial") or "").strip()
        port       = (data.get("port") or "").strip()
        if usb_serial and usb_serial == port:
            usb_serial = ""
        if not usb_serial and not port:
            return jsonify({"error": "Select a device"}), 400
        active_mt = _active_nodes(CONFIG.get("nodes", []))
        active_mc = _active_nodes(CONFIG.get("mc_nodes", []))
        if usb_serial and any(n.get("usb_serial") == usb_serial for n in active_mt):
            return jsonify({"error": "This device is already configured"}), 400
        if usb_serial and any(n.get("usb_serial") == usb_serial for n in active_mc):
            return jsonify({"error": "This device is already configured as an MC node"}), 400
        if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in active_mt):
            return jsonify({"error": f"Port {port} is already in use"}), 400
        if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in active_mc):
            return jsonify({"error": f"Port {port} is already configured as an MC node"}), 400
        if usb_serial:
            new_node["usb_serial"] = usb_serial
            new_node["port"]       = port  # display only
        else:
            new_node["port"] = port
    with CONFIG_LOCK:
        CONFIG["nodes"].append(new_node)
        save_config()
    threading.Thread(target=connect_node, args=(new_node,), daemon=True).start()
    return jsonify({"ok": True, "id": node_id})


@bp.route("/api/settings/nodes/<node_id>/remove", methods=["POST"])
def api_settings_nodes_remove(node_id):
    node = next((n for n in CONFIG["nodes"] if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if not _can_remove_mt_node():
        return jsonify({"error": "Cannot remove the last MT radio unless at least one MC radio is configured"}), 400
    with connections_lock:
        state = connections.pop(node_id, None)
    if state and state.get("iface"):
        try:
            state["iface"].close()
        except Exception:
            pass
    with CONFIG_LOCK:
        CONFIG["nodes"] = [n for n in CONFIG["nodes"] if n["id"] != node_id]
        save_config()
    with chat_lock:
        chat_messages[:] = [m for m in chat_messages if m.get("radio_id") != node_id]
    push_to_sse(json.dumps({"type": "radio_removed", "radio_id": node_id}))
    return jsonify({"ok": True})


@bp.route("/api/settings/nodes/<node_id>/delete", methods=["POST"])
def api_settings_nodes_delete(node_id):
    node = next((n for n in CONFIG["nodes"] if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if not _can_remove_mt_node():
        return jsonify({"error": "Cannot delete the last MT radio unless at least one MC radio is configured"}), 400
    # Find db path from connections dict first, then fall back to config
    with connections_lock:
        state = connections.pop(node_id, None)
    msgs_db = None
    if state:
        msgs_db = state.get("msgs_db")
        if state.get("iface"):
            try:
                state["iface"].close()
            except Exception:
                pass
    if not msgs_db:
        msgs_db = node.get("msgs_db")
    # Validate msgs_db before using in a path — extract basename first (stored as full path)
    if msgs_db:
        msgs_db = os.path.basename(msgs_db)
    if msgs_db and not re.match(r'^overmesh_msgs_[a-f0-9]{1,16}\.db$', msgs_db):
        log.warning(f"[{node_id}] Refusing to delete unexpected msgs_db path: {msgs_db}")
        msgs_db = None
    with CONFIG_LOCK:
        CONFIG["nodes"] = [n for n in CONFIG["nodes"] if n["id"] != node_id]
        save_config()
    with chat_lock:
        chat_messages[:] = [m for m in chat_messages if m.get("radio_id") != node_id]
    deleted_db = False
    if msgs_db:
        db_path = os.path.join(DATA_DIR, msgs_db)
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
                deleted_db = True
                log.info(f"[{node_id}] Deleted DB: {msgs_db}")
            except Exception as e:
                log.warning(f"[{node_id}] Could not delete DB {msgs_db}: {e}")
    push_to_sse(json.dumps({"type": "radio_removed", "radio_id": node_id}))
    return jsonify({"ok": True, "deleted_db": deleted_db, "msgs_db": msgs_db})


@bp.route("/api/settings/nodes/<node_id>/set_enabled", methods=["POST"])
def api_settings_nodes_set_enabled(node_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    node = next((n for n in CONFIG["nodes"] if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if enabled:
        usb_serial = (node.get("usb_serial") or "").strip()
        port = (node.get("port") or "").strip()
        active_mt = [n for n in _active_nodes(CONFIG.get("nodes", [])) if n.get("id") != node_id]
        active_mc = _active_nodes(CONFIG.get("mc_nodes", []))
        if usb_serial:
            if any(n.get("usb_serial") == usb_serial for n in active_mt):
                return jsonify({"error": "This device is already configured"}), 400
            if any(n.get("usb_serial") == usb_serial for n in active_mc):
                return jsonify({"error": "This device is already configured as an MC node"}), 400
        elif port:
            if any(n.get("port") == port and not n.get("usb_serial") for n in active_mt):
                return jsonify({"error": f"Port {port} is already in use"}), 400
            if any(n.get("port") == port and not n.get("usb_serial") for n in active_mc):
                return jsonify({"error": f"Port {port} is already configured as an MC node"}), 400
    with CONFIG_LOCK:
        node["enabled"] = enabled
        save_config()
    if not enabled:
        with connections_lock:
            iface_to_close = (connections[node_id].get("iface") if node_id in connections else None)
            if node_id in connections:
                connections[node_id]["status"] = "disconnected"
                connections[node_id]["iface"] = None
        if iface_to_close:
            try:
                iface_to_close.close()
            except Exception:
                pass
    else:
        with connections_lock:
            state = connections.get(node_id)
            already_connected = bool(state and state.get("status") == "connected" and state.get("iface"))
        if not already_connected:
            threading.Thread(target=connect_node, args=(node,), daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/settings/nodes/<node_id>/rename", methods=["POST"])
def api_settings_nodes_rename(node_id):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    for n in CONFIG["nodes"]:
        if n["id"] == node_id:
            n["name"] = name
            with connections_lock:
                if node_id in connections:
                    connections[node_id]["config"]["name"] = name
            with CONFIG_LOCK:
                save_config()
            return jsonify({"ok": True})
    return jsonify({"error": "Node not found"}), 404


@bp.route("/api/settings/app", methods=["GET"])
def api_settings_app_get():
    return jsonify(_app_settings_payload())


@bp.route("/api/settings/app", methods=["POST"])
def api_settings_app_set():
    data = request.get_json(silent=True) or {}
    with CONFIG_LOCK:
        if "app" not in CONFIG:
            CONFIG["app"] = {}
        try:
            if "zoom" in data:
                CONFIG["app"]["zoom"] = max(50, min(200, int(data["zoom"])))
            if "sense_cooldown" in data:
                CONFIG["app"]["sense_cooldown"] = max(1, min(3600, int(data["sense_cooldown"])))
            if "inapp_notify_returned_gap_value" in data:
                CONFIG["app"]["inapp_notify_returned_gap_value"] = max(1, min(9999, int(data["inapp_notify_returned_gap_value"])))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid numeric value"}), 400
        if "accent_color" in data:
            val = str(data["accent_color"])
            if re.match(r'^#[0-9a-fA-F]{6}$', val):
                CONFIG["app"]["accent_color"] = val
        if "inapp_notify_returned_gap_unit" in data:
            unit = str(data["inapp_notify_returned_gap_unit"]).lower()
            if unit in ("hours", "days"):
                CONFIG["app"]["inapp_notify_returned_gap_unit"] = unit
        if "distance_unit" in data:
            unit = str(data["distance_unit"]).lower()
            if unit not in ("km", "mi"):
                return jsonify({"error": "distance_unit must be 'km' or 'mi'"}), 400
            CONFIG["app"]["distance_unit"] = unit
        if "time_format" in data:
            fmt = str(data["time_format"]).lower()
            if fmt not in ("24h", "12h"):
                return jsonify({"error": "time_format must be '24h' or '12h'"}), 400
            CONFIG["app"]["time_format"] = fmt
        if "date_format" in data:
            fmt = str(data["date_format"]).lower()
            if fmt not in ("eu", "us", "iso"):
                return jsonify({"error": "date_format must be 'eu', 'us', or 'iso'"}), 400
            CONFIG["app"]["date_format"] = fmt
        for key in (
            "inapp_notify_messages",
            "inapp_notify_nodes",
            "inapp_notify_returned",
            "sound_notify_messages",
            "sound_notify_radio_connected",
            "sound_notify_nodes",
        ):
            if key in data:
                CONFIG["app"][key] = bool(data[key])
        bridge_keys = {
            "bridge_webhooks_enabled",
            "bridge_webhook_urls",
            "bridge_webhook_secret",
            "bridge_ingest_enabled",
            "bridge_ingest_token",
        }
        if any(k in data for k in bridge_keys):
            bridge_cfg = CONFIG.setdefault("bridge", {})
            webhook_cfg = bridge_cfg.setdefault("webhooks", {})
            ingest_cfg = bridge_cfg.setdefault("ingest", {})
            if "bridge_webhooks_enabled" in data:
                webhook_cfg["enabled"] = bool(data["bridge_webhooks_enabled"])
            if "bridge_webhook_urls" in data:
                raw_urls = data.get("bridge_webhook_urls") or ""
                if isinstance(raw_urls, list):
                    urls = [str(u).strip() for u in raw_urls]
                else:
                    urls = [u.strip() for u in re.split(r'[\n,]+', str(raw_urls))]
                urls = [u for u in urls if u]
                bad = [u for u in urls if not u.startswith(("http://", "https://"))]
                if bad:
                    return jsonify({"error": "Webhook URLs must start with http:// or https://"}), 400
                webhook_cfg["urls"] = urls
            if "bridge_webhook_secret" in data:
                webhook_cfg["secret"] = str(data.get("bridge_webhook_secret") or "").strip()
            if "bridge_ingest_enabled" in data:
                ingest_cfg["enabled"] = bool(data["bridge_ingest_enabled"])
            if "bridge_ingest_token" in data:
                ingest_cfg["token"] = str(data.get("bridge_ingest_token") or "").strip()
            if ingest_cfg.get("enabled") and not ingest_cfg.get("token"):
                return jsonify({"error": "Bridge ingest requires a bearer token"}), 400
        if "om_manual_lat" in data or "om_manual_lon" in data:
            raw_lat = data.get("om_manual_lat", CONFIG["app"].get("om_manual_lat"))
            raw_lon = data.get("om_manual_lon", CONFIG["app"].get("om_manual_lon"))
            lat_blank = raw_lat in (None, "")
            lon_blank = raw_lon in (None, "")
            if lat_blank and lon_blank:
                CONFIG["app"].pop("om_manual_lat", None)
                CONFIG["app"].pop("om_manual_lon", None)
            elif lat_blank or lon_blank:
                return jsonify({"error": "Both OM latitude and longitude are required."}), 400
            else:
                try:
                    lat = float(raw_lat)
                    lon = float(raw_lon)
                except (TypeError, ValueError):
                    return jsonify({"error": "Invalid OM coordinates."}), 400
                if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                    return jsonify({"error": "OM coordinates are out of range."}), 400
                CONFIG["app"]["om_manual_lat"] = round(lat, 6)
                CONFIG["app"]["om_manual_lon"] = round(lon, 6)
        save_config()
    return jsonify({"ok": True})


@bp.route("/api/settings/cross", methods=["GET"])
def api_settings_cross_get():
    return jsonify(get_cross_config())


@bp.route("/api/settings/cross", methods=["POST"])
def api_settings_cross_set():
    data = request.get_json(silent=True) or {}
    rules_in = data.get("rules")
    if rules_in is None:
        rules_in = [data]
    if not isinstance(rules_in, list):
        return jsonify({"error": "rules must be a list"}), 400
    mt_ids = {n.get("id") for n in CONFIG.get("nodes", [])}
    mc_ids = {n.get("id") for n in CONFIG.get("mc_nodes", [])}
    rules = []
    for raw in rules_in:
        rule = _normalize_rule(raw)
        if not rule["source_radio_id"] or not rule["target_radio_id"]:
            return jsonify({"error": "Each rule must have source and target radios"}), 400
        if rule["source_radio_id"] == rule["target_radio_id"]:
            return jsonify({"error": "Source and target radios must be on different systems"}), 400
        source_net = rule["source_network"]
        target_net = "mc" if source_net == "mt" else "mt"
        if source_net == "mt":
            if rule["source_radio_id"] not in mt_ids:
                return jsonify({"error": f'Source radio {rule["source_radio_id"]} is not an MT radio'}), 400
            if rule["target_radio_id"] not in mc_ids:
                return jsonify({"error": f'Target radio {rule["target_radio_id"]} is not an MC radio'}), 400
        else:
            if rule["source_radio_id"] not in mc_ids:
                return jsonify({"error": f'Source radio {rule["source_radio_id"]} is not an MC radio'}), 400
            if rule["target_radio_id"] not in mt_ids:
                return jsonify({"error": f'Target radio {rule["target_radio_id"]} is not an MT radio'}), 400
        rules.append(rule)
    with CONFIG_LOCK:
        CONFIG["cross"] = {"rules": rules}
        save_config()
    return jsonify({"ok": True, "cross": {"rules": rules}})


# ---------------------------------------------------------------------------
# MeshCore node management
# ---------------------------------------------------------------------------

@bp.route("/api/settings/mc_nodes")
def api_settings_mc_nodes():
    with mc_connections_lock:
        statuses = {k: v.get("status", "disconnected") for k, v in mc_connections.items()}
    nodes = [
        {
            "id":         n["id"],
            "name":       n["name"],
            "type":       n.get("type", "serial"),
            "port":       n.get("port", ""),
            "host":       n.get("host", ""),
            "tcp_port":   n.get("tcp_port", 4403),
            "bt_address": n.get("bt_address", ""),
            "bt_pin":     n.get("bt_pin", ""),
            "usb_serial": n.get("usb_serial", ""),
            "enabled":    n.get("enabled", True),
            "status":     statuses.get(n["id"], "disconnected"),
            "path_hash_mode": n.get("path_hash_mode", 2),
            "force_flood": bool(n.get("force_flood", False)),
            "passive_collection": n.get("passive_collection", True) is not False,
        }
        for n in CONFIG.get("mc_nodes", [])
    ]
    return jsonify({"mc_nodes": nodes})


@bp.route("/api/settings/mc_nodes/add", methods=["POST"])
def api_settings_mc_nodes_add():
    from mesh_mc import connect_mc_node
    data       = request.get_json(silent=True) or {}
    name       = (data.get("name") or "").strip()
    node_type  = (data.get("type") or "serial").strip().lower()
    if node_type in ("bt", "bluetooth"):
        node_type = "ble"
    if node_type not in ("serial", "tcp", "ble"):
        return jsonify({"error": "type must be 'serial', 'tcp', or 'ble'"}), 400
    if not name:
        return jsonify({"error": "Name is required"}), 400
    mc_nodes = CONFIG.setdefault("mc_nodes", [])
    node_id  = f"mc_node_{uuid.uuid4().hex[:12]}"
    new_node = {"id": node_id, "name": name, "enabled": True, "path_hash_mode": 2, "force_flood": False, "passive_collection": True, "type": node_type}
    if node_type == "tcp":
        host = (data.get("host") or "").strip()
        try:
            tcp_port = int(data.get("tcp_port") or 4403)
        except (TypeError, ValueError):
            return jsonify({"error": "tcp_port must be a number"}), 400
        if not host:
            return jsonify({"error": "Enter an IP address or hostname"}), 400
        if any(n.get("host") == host and n.get("type") == "tcp" and int(n.get("tcp_port") or 4403) == tcp_port for n in mc_nodes):
            return jsonify({"error": f"{host}:{tcp_port} is already configured as an MC node"}), 400
        if any(n.get("host") == host and n.get("type") == "tcp" and int(n.get("tcp_port") or 4403) == tcp_port for n in CONFIG.get("nodes", [])):
            return jsonify({"error": f"{host}:{tcp_port} is already configured as an MT node"}), 400
        new_node["host"] = host
        new_node["tcp_port"] = tcp_port
        new_node["port"] = f"{host}:{tcp_port}"
    elif node_type == "ble":
        bt_address = (data.get("bt_address") or data.get("address") or "").strip()
        bt_pin = (data.get("bt_pin") or data.get("pin") or "").strip()
        if not bt_address:
            return jsonify({"error": "Enter a Bluetooth address"}), 400
        if any(n.get("bt_address") == bt_address and n.get("type") == "ble" for n in mc_nodes):
            return jsonify({"error": f"{bt_address} is already configured as an MC node"}), 400
        new_node["bt_address"] = bt_address
        if bt_pin:
            new_node["bt_pin"] = bt_pin
        new_node["port"] = bt_address
    else:
        usb_serial = (data.get("usb_serial") or "").strip()
        port       = (data.get("port") or "").strip()
        if usb_serial and usb_serial == port:
            usb_serial = ""
        if not usb_serial and not port:
            return jsonify({"error": "Select a device"}), 400
        active_mc = _active_nodes(mc_nodes)
        active_mt = _active_nodes(CONFIG.get("nodes", []))
        if usb_serial and any(n.get("usb_serial") == usb_serial for n in active_mc):
            return jsonify({"error": "This device is already configured as an MC node"}), 400
        # Also check MT nodes
        if usb_serial and any(n.get("usb_serial") == usb_serial for n in active_mt):
            return jsonify({"error": "This device is already configured as an MT node"}), 400
        if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in active_mc):
            return jsonify({"error": f"Port {port} is already configured as an MC node"}), 400
        if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in active_mt):
            return jsonify({"error": f"Port {port} is already configured as an MT node"}), 400
        if usb_serial:
            new_node["usb_serial"] = usb_serial
            new_node["port"]       = port
        else:
            new_node["port"] = port
    with CONFIG_LOCK:
        mc_nodes.append(new_node)
        save_config()
    threading.Thread(target=connect_mc_node, args=(new_node,), daemon=True).start()
    return jsonify({"ok": True, "id": node_id})


@bp.route("/api/settings/mc_nodes/<node_id>/remove", methods=["POST"])
def api_settings_mc_nodes_remove(node_id):
    mc_nodes = CONFIG.get("mc_nodes", [])
    node = next((n for n in mc_nodes if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "MC node not found"}), 404
    with mc_connections_lock:
        entry = mc_connections.pop(node_id, None)
    mc_obj = (entry or {}).get("mc")
    if mc_obj is not None:
        try:
            from mesh_mc import run_mc
            run_mc(mc_obj.disconnect(), timeout=5)
        except Exception:
            pass
    with CONFIG_LOCK:
        CONFIG["mc_nodes"] = [n for n in mc_nodes if n["id"] != node_id]
        save_config()
    push_to_sse({"type": "mc_radio_removed", "radio_id": node_id})
    return jsonify({"ok": True})


@bp.route("/api/settings/mc_nodes/<node_id>/set_enabled", methods=["POST"])
def api_settings_mc_nodes_set_enabled(node_id):
    data    = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    mc_nodes = CONFIG.get("mc_nodes", [])
    node = next((n for n in mc_nodes if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "MC node not found"}), 404
    if enabled and (node.get("type") or "serial") == "serial":
        usb_serial = (node.get("usb_serial") or "").strip()
        port = (node.get("port") or "").strip()
        active_mc = [n for n in _active_nodes(mc_nodes) if n.get("id") != node_id]
        active_mt = _active_nodes(CONFIG.get("nodes", []))
        if usb_serial:
            if any(n.get("usb_serial") == usb_serial for n in active_mc):
                return jsonify({"error": "This device is already configured as an MC node"}), 400
            if any(n.get("usb_serial") == usb_serial for n in active_mt):
                return jsonify({"error": "This device is already configured as an MT node"}), 400
        elif port:
            if any(n.get("port") == port and not n.get("usb_serial") for n in active_mc):
                return jsonify({"error": f"Port {port} is already configured as an MC node"}), 400
            if any(n.get("port") == port and not n.get("usb_serial") for n in active_mt):
                return jsonify({"error": f"Port {port} is already configured as an MT node"}), 400
    with CONFIG_LOCK:
        node["enabled"] = enabled
        save_config()
    if not enabled:
        mc_obj = None
        with mc_connections_lock:
            if node_id in mc_connections:
                mc_connections[node_id]["status"] = "disconnected"
                mc_obj = mc_connections[node_id].get("mc")
                mc_connections[node_id]["mc"]     = None
        if mc_obj is not None:
            try:
                from mesh_mc import run_mc
                run_mc(mc_obj.disconnect(), timeout=5)
            except Exception:
                pass
    else:
        with mc_connections_lock:
            state = mc_connections.get(node_id)
            already_connected = bool(state and state.get("status") == "connected" and state.get("mc"))
        if not already_connected:
            from mesh_mc import connect_mc_node
            threading.Thread(target=connect_mc_node, args=(node,), daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/settings/mc_nodes/<node_id>/path_hash_mode", methods=["POST"])
def api_settings_mc_nodes_path_hash_mode(node_id):
    data = request.get_json(silent=True) or {}
    try:
        requested_mode = _parse_mc_path_hash_mode(data.get("path_hash_mode"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with CONFIG_LOCK:
        mc_nodes = CONFIG.get("mc_nodes", [])
        node = next((n for n in mc_nodes if n["id"] == node_id), None)
        if not node:
            return jsonify({"error": "MC node not found"}), 404
        node["path_hash_mode"] = requested_mode
        save_config()

    applied_mode = requested_mode
    fallback = False
    warning = None
    connected = False
    with mc_connections_lock:
        connected = bool(
            mc_connections.get(node_id, {}).get("status") == "connected"
            and mc_connections.get(node_id, {}).get("mc")
        )

    if connected:
        try:
            from mesh_mc import set_path_hash_mode
            result = set_path_hash_mode(node_id, requested_mode)
            applied_mode = int(result.get("applied", requested_mode))
            fallback = bool(result.get("fallback")) or applied_mode != requested_mode
        except Exception as e:
            log.warning(f"[MC:{node_id}] path hash mode {requested_mode} failed: {e}")
            applied_mode = 0
            fallback = True
            warning = "Radio did not accept path hash mode command; using 1B/hop fallback."

        if applied_mode != requested_mode:
            with CONFIG_LOCK:
                node = next((n for n in CONFIG.get("mc_nodes", []) if n["id"] == node_id), None)
                if node:
                    node["path_hash_mode"] = applied_mode
                    save_config()
        with mc_connections_lock:
            if node_id in mc_connections:
                mc_connections[node_id].setdefault("config", {})["path_hash_mode"] = applied_mode
                mc_connections[node_id].setdefault("node_info", {})["path_hash_mode"] = applied_mode

    return jsonify({
        "ok": True,
        "requested": requested_mode,
        "applied": applied_mode,
        "fallback": fallback,
        "connected": connected,
        "warning": warning,
    })


@bp.route("/api/settings/mc_nodes/<node_id>/force_flood", methods=["POST"])
def api_settings_mc_nodes_force_flood(node_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("force_flood", False))
    with CONFIG_LOCK:
        mc_nodes = CONFIG.get("mc_nodes", [])
        node = next((n for n in mc_nodes if n["id"] == node_id), None)
        if not node:
            return jsonify({"error": "MC node not found"}), 404
        node["force_flood"] = enabled
        save_config()
    with mc_connections_lock:
        if node_id in mc_connections:
            mc_connections[node_id].setdefault("config", {})["force_flood"] = enabled
    return jsonify({"ok": True, "force_flood": enabled})


@bp.route("/api/settings/mc_nodes/<node_id>/passive_collection", methods=["POST"])
def api_settings_mc_nodes_passive_collection(node_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("passive_collection", True))
    with CONFIG_LOCK:
        mc_nodes = CONFIG.get("mc_nodes", [])
        node = next((n for n in mc_nodes if n["id"] == node_id), None)
        if not node:
            return jsonify({"error": "MC node not found"}), 404
        node["passive_collection"] = enabled
        save_config()
    with mc_connections_lock:
        if node_id in mc_connections:
            mc_connections[node_id].setdefault("config", {})["passive_collection"] = enabled
    return jsonify({"ok": True, "passive_collection": enabled})



@bp.route("/api/settings/auth", methods=["GET"])
def api_settings_auth_get():
    return jsonify({
        "auth_enabled": get_auth_setting("auth_enabled", "0") == "1",
        "auth_username": get_auth_setting("auth_username", ""),
    })


@bp.route("/api/settings/auth", methods=["POST"])
def api_settings_auth_set():
    from werkzeug.security import generate_password_hash
    data = request.get_json(silent=True) or {}

    # Toggle enable/disable
    if "auth_enabled" in data:
        enabled = bool(data["auth_enabled"])
        # Don't allow enabling without credentials
        if enabled:
            username = str(data.get("auth_username") or get_auth_setting("auth_username", "") or "").strip()
            password = str(data.get("auth_password") or "").strip()
            if not username:
                return jsonify({"error": "Username is required to enable authentication."}), 400
            if not password and not get_auth_setting("auth_password_hash", ""):
                return jsonify({"error": "Password is required to enable authentication."}), 400
            if username:
                set_auth_setting("auth_username", username)
            if password:
                set_auth_setting("auth_password_hash", generate_password_hash(password))
        set_auth_setting("auth_enabled", "1" if enabled else "0")
        return jsonify({"ok": True, "auth_enabled": enabled})

    # Update credentials only (without changing enabled state)
    username = str(data.get("auth_username") or "").strip()
    password = str(data.get("auth_password") or "").strip()
    if not username and not password:
        return jsonify({"error": "Provide username and/or password to update."}), 400
    if username:
        set_auth_setting("auth_username", username)
    if password:
        set_auth_setting("auth_password_hash", generate_password_hash(password))
    return jsonify({"ok": True})

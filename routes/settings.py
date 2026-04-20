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


def _has_any_mc_nodes():
    return bool(CONFIG.get("mc_nodes", []))


def _can_remove_mt_node():
    """Allow removing the last MT radio only if the app still has MC radios configured."""
    return len(CONFIG["nodes"]) > 1 or _has_any_mc_nodes()


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
    info["dirty"] = bool(status) if rc == 0 else True
    info["dirty_summary"] = status.splitlines()[:12] if status else []

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
        if info.get("dirty"):
            raise RuntimeError("Local changes detected. Update aborted.")
        if info.get("ahead", 0) > 0:
            raise RuntimeError("Local commits are ahead of origin. Push or reconcile before updating.")
        if not info.get("update_available"):
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({"running": False, "ok": True, "message": "Already up to date."})
            _update_append("Already up to date.")
            return

        rc, changed, _ = _git_cmd(["diff", "--name-only", "HEAD", "origin/main"], timeout=15)
        changed_files = set(changed.splitlines()) if rc == 0 and changed else set()

        _update_append("Pulling latest code...")
        _git_cmd(["pull", "--ff-only", "origin", "main"], timeout=60, check=True)

        if "requirements.txt" in changed_files:
            _update_append("requirements.txt changed; installing Python dependencies...")
            pip = subprocess.run(
                [sys.executable or "python3", "-m", "pip", "install", "-r", "requirements.txt", "--user"],
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
    known_serials = (
        {n.get("usb_serial") for n in CONFIG.get("nodes", []) if n.get("usb_serial")} |
        {n.get("usb_serial") for n in CONFIG.get("mc_nodes", []) if n.get("usb_serial")}
    )
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device":      p.device,
            "description": p.description or p.device,
            "usb_serial":  p.serial_number or "",
            "vid":         p.vid,
            "pid":         p.pid,
            "in_use":      p.serial_number in known_serials if p.serial_number else False,
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
        if not usb_serial and not port:
            return jsonify({"error": "Select a device"}), 400
        if usb_serial and any(n.get("usb_serial") == usb_serial for n in CONFIG["nodes"]):
            return jsonify({"error": "This device is already configured"}), 400
        if usb_serial and any(n.get("usb_serial") == usb_serial for n in CONFIG.get("mc_nodes", [])):
            return jsonify({"error": "This device is already configured as an MC node"}), 400
        if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in CONFIG["nodes"]):
            return jsonify({"error": f"Port {port} is already in use"}), 400
        if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in CONFIG.get("mc_nodes", [])):
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
    return jsonify(CONFIG.get("app", {"font_size": "medium"}))


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
        for key in ("inapp_notify_messages", "inapp_notify_nodes", "inapp_notify_returned"):
            if key in data:
                CONFIG["app"][key] = bool(data[key])
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
            "port":       n.get("port", ""),
            "usb_serial": n.get("usb_serial", ""),
            "enabled":    n.get("enabled", True),
            "status":     statuses.get(n["id"], "disconnected"),
        }
        for n in CONFIG.get("mc_nodes", [])
    ]
    return jsonify({"mc_nodes": nodes})


@bp.route("/api/settings/mc_nodes/add", methods=["POST"])
def api_settings_mc_nodes_add():
    from mesh_mc import connect_mc_node
    data       = request.get_json(silent=True) or {}
    name       = (data.get("name") or "").strip()
    usb_serial = (data.get("usb_serial") or "").strip()
    port       = (data.get("port") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not usb_serial and not port:
        return jsonify({"error": "Select a device"}), 400
    mc_nodes = CONFIG.setdefault("mc_nodes", [])
    if usb_serial and any(n.get("usb_serial") == usb_serial for n in mc_nodes):
        return jsonify({"error": "This device is already configured as an MC node"}), 400
    # Also check MT nodes
    if usb_serial and any(n.get("usb_serial") == usb_serial for n in CONFIG.get("nodes", [])):
        return jsonify({"error": "This device is already configured as an MT node"}), 400
    if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in mc_nodes):
        return jsonify({"error": f"Port {port} is already configured as an MC node"}), 400
    if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in CONFIG.get("nodes", [])):
        return jsonify({"error": f"Port {port} is already configured as an MT node"}), 400
    node_id  = f"mc_node_{uuid.uuid4().hex[:12]}"
    new_node = {"id": node_id, "name": name, "enabled": True}
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

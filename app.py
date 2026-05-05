import logging
import os
import signal
import subprocess
import sys
import threading
import time

def _load_app_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = _load_app_version()

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from pubsub import pub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from auth import check_credentials, is_auth_enabled, load_secret_key
from config import CONFIG, DATA_DIR, save_config, _valid_node_id

app = Flask(__name__)
app.secret_key = load_secret_key(DATA_DIR)

# ---------------------------------------------------------------------------
# Authentication gate (optional, off by default)
# ---------------------------------------------------------------------------

@app.before_request
def _auth_gate():
    path = request.path
    if path == "/login" or path == "/logout" or path.startswith("/static"):
        return
    if not is_auth_enabled():
        return
    if session.get("authenticated"):
        return
    if request.method in ("GET", "HEAD") and not path.startswith("/api/") and not path.startswith("/sse"):
        return redirect(url_for("login"))
    return jsonify({"error": "Unauthorized"}), 401


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if check_credentials(username, password):
            session["authenticated"] = True
            session.permanent = False
            return redirect(url_for("index"))
        error = "Invalid username or password."
    accent = CONFIG.get("app", {}).get("accent_color", "#4ade80")
    return render_template("login.html", error=error, accent_color=accent)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


from bot import motd_scheduler_loop
from db import init_prefs_db, load_notes, load_waypoints
from gps import _gps_start, gps_port_conflict, gps_watchdog_loop
import sense as _sense_mod
from sense import (
    _active_auto_event, _active_auto_loop, _active_auto_running_lock, _sense_state,
)
from mesh import (
    connect_node, health_check_loop,
    on_connection_lost, on_text_receive, reconnect_loop,
)
from mesh_mc import start_mc_loop, connect_mc_node, reconnect_mc_loop, mc_watchdog_loop, disconnect_all_mc

# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------

from routes.nodes     import bp as nodes_bp
from routes.chat      import bp as chat_bp
from routes.settings  import bp as settings_bp
from routes.gps       import bp as gps_bp
from routes.radio     import bp as radio_bp
from routes.bot       import bp as bot_bp
from routes.sense     import bp as sense_bp
from routes.waypoints import bp as waypoints_bp
from routes.notes     import bp as notes_bp
from routes.mc        import bp as mc_bp
from routes.map_layers import bp as map_layers_bp
from routes.silent     import bp as silent_bp
from bridge            import bp as bridge_bp

app.register_blueprint(nodes_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(gps_bp)
app.register_blueprint(radio_bp)
app.register_blueprint(bot_bp)
app.register_blueprint(sense_bp)
app.register_blueprint(waypoints_bp)
app.register_blueprint(notes_bp)
app.register_blueprint(mc_bp)
app.register_blueprint(map_layers_bp)
app.register_blueprint(silent_bp)
app.register_blueprint(bridge_bp)


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.context_processor
def inject_base_path():
    base_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return {"base_path": base_path}


@app.route("/")
def index():
    return render_template("index.html", version=__version__, auth_enabled=is_auth_enabled())


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _kill():
        time.sleep(0.4)
        disconnect_all_mc()
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    def _restart():
        script     = os.path.abspath(__file__)
        script_dir = os.path.dirname(script)
        time.sleep(0.4)
        disconnect_all_mc()
        python_bin = sys.executable or "python3"
        kwargs = {
            "cwd": script_dir,
            "close_fds": True,
        }
        if os.name == "nt":
            detached = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = detached
            kwargs["stdin"] = subprocess.DEVNULL
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        else:
            kwargs["start_new_session"] = True
            kwargs["stdin"] = subprocess.DEVNULL
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        if os.environ.get("INVOCATION_ID"):
            # Running under systemd — exit non-zero so Restart=on-failure triggers
            os._exit(1)
        else:
            helper_code = (
                "import os, sys, time; "
                "time.sleep(1); "
                "os.execv(sys.argv[1], [sys.argv[1], sys.argv[2]])"
            )
            # Spawn a tiny helper that waits briefly, then execs the real app process.
            subprocess.Popen([python_bin, "-c", helper_code, python_bin, script], **kwargs)
            os._exit(0)
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def startup():
    init_prefs_db()
    load_waypoints()
    load_notes()
    gps_cfg = CONFIG.get("gps", {})
    if gps_cfg.get("enabled") and gps_cfg.get("port"):
        gps_conflict = gps_port_conflict(gps_cfg["port"])
        if gps_conflict:
            log.warning(
                f"GPS start skipped: port {gps_cfg['port']} conflicts with enabled "
                f"{gps_conflict['network']} radio {gps_conflict['name']}"
            )
        else:
            _gps_start(gps_cfg["port"])
    # per-radio message DBs are initialized in connect_node() as each radio connects
    pub.subscribe(on_text_receive, "meshtastic.receive")
    try:
        pub.subscribe(on_connection_lost, "meshtastic.connection.lost")
    except Exception as e:
        log.warning(f"Could not subscribe to connection.lost: {e}")
    for node_cfg in CONFIG["nodes"]:
        if node_cfg.get("enabled", True):
            threading.Thread(target=connect_node, args=(node_cfg,), daemon=True).start()
    threading.Thread(target=reconnect_loop,      daemon=True).start()
    threading.Thread(target=health_check_loop,   daemon=True).start()
    threading.Thread(target=gps_watchdog_loop,   daemon=True).start()
    threading.Thread(target=motd_scheduler_loop, daemon=True).start()
    # MeshCore
    start_mc_loop()
    for mc_node_cfg in CONFIG.get("mc_nodes", []):
        if mc_node_cfg.get("enabled", True):
            threading.Thread(target=connect_mc_node, args=(mc_node_cfg,), daemon=True).start()
    threading.Thread(target=reconnect_mc_loop, daemon=True).start()
    threading.Thread(target=mc_watchdog_loop, daemon=True).start()
    if _sense_state["active_auto"]:
        _active_auto_event.clear()
        with _active_auto_running_lock:
            if not _sense_mod._active_auto_running:
                threading.Thread(target=_active_auto_loop, daemon=True).start()


if __name__ == "__main__":
    if hasattr(signal, "SIGHUP"):  # Linux/macOS only — not available on Windows
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def _graceful_shutdown(signum, frame):
        log.info("[app] SIGTERM received — disconnecting MC nodes before exit")
        disconnect_all_mc()
        os._exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)

    startup()
    _host = os.environ.get("OVERMESH_HOST", CONFIG.get("host", "0.0.0.0"))
    try:
        _port = int(os.environ.get("OVERMESH_PORT", CONFIG.get("port", 8082)))
    except (TypeError, ValueError):
        log.error("OVERMESH_PORT is not a valid integer, using default 8082")
        _port = 8082
    app.run(host=_host, port=_port, debug=False)

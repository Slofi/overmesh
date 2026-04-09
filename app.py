import logging
import os
import signal
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, render_template
from pubsub import pub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

from bot import motd_scheduler_loop
from config import CONFIG, save_config, _valid_node_id
from db import init_prefs_db, load_notes, load_waypoints
from gps import _gps_start
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


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


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
        # Spawn a fresh process in a new session (no inherited sockets/fds),
        # wait 1s for current process to fully exit and release port 8082.
        subprocess.Popen(
            ["bash", "-c",
             f"sleep 1 && cd '{script_dir}' && nohup python3 '{script}' >> /tmp/overmesh-mc.log 2>&1"],
            start_new_session=True, close_fds=True
        )
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
        _port = int(os.environ.get("OVERMESH_PORT", CONFIG.get("port", 8081)))
    except (TypeError, ValueError):
        log.error("OVERMESH_PORT is not a valid integer, using default 8081")
        _port = 8081
    app.run(host=_host, port=_port, debug=False)

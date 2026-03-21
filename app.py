import logging
import os
import signal
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

app.register_blueprint(nodes_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(gps_bp)
app.register_blueprint(radio_bp)
app.register_blueprint(bot_bp)
app.register_blueprint(sense_bp)
app.register_blueprint(waypoints_bp)
app.register_blueprint(notes_bp)


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
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    def _kill():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
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
    if _sense_state["active_auto"]:
        _active_auto_event.clear()
        with _active_auto_running_lock:
            if not _sense_mod._active_auto_running:
                threading.Thread(target=_active_auto_loop, daemon=True).start()


if __name__ == "__main__":
    if hasattr(signal, "SIGHUP"):  # Linux/macOS only — not available on Windows
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    startup()
    _host = os.environ.get("OVERMESH_HOST", CONFIG.get("host", "0.0.0.0"))
    _port = int(os.environ.get("OVERMESH_PORT", CONFIG.get("port", 8081)))
    app.run(host=_host, port=_port, debug=False)

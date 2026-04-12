import json
import logging
import os
import re
import tempfile
import threading

log = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))

# Environment variable overrides (for container / non-default deployments).
# If not set, behaviour is identical to a plain `python app.py` run.
CONFIG_PATH = os.environ.get("OVERMESH_CONFIG",    os.path.join(BASE_DIR, "config.json"))
DATA_DIR    = os.environ.get("OVERMESH_DATA_DIR",  BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

CONFIG_LOCK = threading.RLock()

PREFS_DB_PATH = os.path.join(DATA_DIR, "overmesh_prefs.db")

_NODE_ID_RE = re.compile(r'^[!^a-zA-Z0-9_\-]{1,64}$')


def _valid_node_id(node_id):
    """Basic sanity check on a mesh node ID before passing it to the library."""
    return bool(node_id and _NODE_ID_RE.match(str(node_id)))


def save_config():
    tmp = None
    with CONFIG_LOCK:
        try:
            config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
            with tempfile.NamedTemporaryFile("w", dir=config_dir, delete=False, suffix=".tmp") as tf:
                json.dump(CONFIG, tf, indent=2)
                tmp = tf.name
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            log.error(f"save_config failed: {e}")
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

"""
Mesh Sense — broadcast and passive collection.
Owns all Sense state — nothing in state.py.
"""
import json
import logging
import threading
import time

from config import CONFIG
from helpers import push_to_sse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sense state (owned here — not in state.py)
# ---------------------------------------------------------------------------

_sense_state = {
    "active":         False,
    "passive":        bool(CONFIG.get("sense_passive",     True)),
    "active_auto":    bool(CONFIG.get("sense_active_auto", False)),
    "last_triggered": 0,
    "window_end":     0,
    "responses":      [],
}
_sense_lock               = threading.Lock()
_active_auto_event        = threading.Event()
_active_auto_running      = False
_active_auto_running_lock = threading.Lock()

SENSE_COLLECTION_WINDOW = 60    # seconds to collect responses after broadcast


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

def _run_sense_broadcast(iface, cooldown):
    """Broadcast position request, wait for collection window, close. Called in a thread."""
    # Lazy import — mesh.py doesn't exist yet; avoids circular dep at module load time
    from mesh import _reconnect_disconnected
    try:
        iface.sendPosition(wantResponse=True)
    except Exception as e:
        log.warning(f"Sense broadcast error: {e}")
        with _sense_lock:
            _sense_state["active"] = False
            count = len(_sense_state["responses"])
        push_to_sse(json.dumps({"type": "sense_done", "count": count,
                                "error": "Node disconnected during broadcast"}))
        threading.Thread(target=_reconnect_disconnected, daemon=True).start()
        return
    # health_check_loop (5s interval) handles any real silent disconnect after sendPosition.
    # The old 0.5s post-sense check was catching brief USB-CDC glitches on the nRF52840
    # ProMicro during broadcast and triggering an unnecessary full reconnect cycle.
    time.sleep(SENSE_COLLECTION_WINDOW)
    with _sense_lock:
        _sense_state["active"] = False
        count = len(_sense_state["responses"])
    push_to_sse(json.dumps({"type": "sense_done", "count": count}))


def _active_auto_loop():
    """Background thread: re-trigger sense at cooldown intervals while active_auto is on."""
    global _active_auto_running
    # Lazy import — mesh.py doesn't exist yet; avoids circular dep at module load time
    from mesh import get_any_iface
    # Note: _active_auto_running is set True by the caller (routes/sense.py) before start()
    try:
        while True:
            with _sense_lock:
                if not _sense_state["active_auto"]:
                    return
            iface = get_any_iface()
            cooldown = CONFIG.get("app", {}).get("sense_cooldown", 180)
            if iface:
                now = time.time()
                with _sense_lock:
                    _sense_state["active"]         = True
                    _sense_state["last_triggered"] = now
                    _sense_state["window_end"]     = now + SENSE_COLLECTION_WINDOW
                    _sense_state["responses"]      = []
                push_to_sse(json.dumps({"type": "sense_started", "window": SENSE_COLLECTION_WINDOW,
                                        "cooldown": cooldown}))
                _run_sense_broadcast(iface, cooldown)  # blocks for SENSE_COLLECTION_WINDOW
            # Wait cooldown before next scan; wake early if active_auto toggled off
            _active_auto_event.wait(timeout=cooldown)
            _active_auto_event.clear()
    finally:
        with _active_auto_running_lock:
            _active_auto_running = False

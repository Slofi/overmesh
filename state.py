"""
Shared mutable globals — imported by every other module.
This module must NOT import from any other overmesh module (except config)
to keep the dependency graph acyclic.
"""
import queue
import threading

from config import CONFIG

# ---------------------------------------------------------------------------
# Node connections
# ---------------------------------------------------------------------------

connections      = {}
connections_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Chat + SSE
# ---------------------------------------------------------------------------

chat_messages     = []          # rolling buffer, last 500
chat_lock         = threading.Lock()
sse_clients       = []          # queues, evicted when full in push_to_sse()
sse_lock          = threading.Lock()
pending_acks      = {}          # packet_id -> (msg_id, radio_id, ts)
pending_acks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Waypoints + Notes
# ---------------------------------------------------------------------------

waypoints_cache = {}
waypoints_lock  = threading.Lock()

notes_cache = {}
notes_lock  = threading.Lock()

# ---------------------------------------------------------------------------
# GPS receiver
# ---------------------------------------------------------------------------

gps_state = {
    "lat": None, "lon": None, "alt": None,
    "sats": 0, "fix": False, "speed": None,
}
gps_lock              = threading.Lock()
_gps_stop_event       = threading.Event()
_gps_thread           = None
_gps_last_push_ts     = 0.0     # epoch seconds — rate-limits auto-push to nodes
_GPS_AUTO_PUSH_INTERVAL = 30    # seconds between auto-pushes

# ---------------------------------------------------------------------------
# SSE message counter
# ---------------------------------------------------------------------------

_msg_counter      = 0
_msg_counter_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Traceroute
# ---------------------------------------------------------------------------

traceroute_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

bot_activity      = []
bot_activity_lock = threading.Lock()

_bot_config_cache      = {}     # {radio_id_or_None: config_dict}
_bot_config_cache_lock = threading.Lock()

_motd_last_sent_per_radio = {}          # {radio_id: last_fired_timestamp}
_motd_event               = threading.Event()   # wake scheduler early on config change

# ---------------------------------------------------------------------------
# Mesh Sense
# ---------------------------------------------------------------------------

_sense_state = {
    "active":         False,
    "passive":        bool(CONFIG.get("sense_passive",     False)),
    "active_auto":    bool(CONFIG.get("sense_active_auto", False)),
    "last_triggered": 0,
    "window_end":     0,
    "responses":      [],   # cleared on each new active sense run
}
_sense_lock               = threading.Lock()
_active_auto_event        = threading.Event()
_active_auto_running      = False
_active_auto_running_lock = threading.Lock()

SENSE_COLLECTION_WINDOW = 60    # seconds to collect responses after broadcast

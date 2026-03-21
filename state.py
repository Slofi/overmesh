"""
Shared mutable globals — imported by every other module.
This module must NOT import from any other overmesh module (except config)
to keep the dependency graph acyclic.
"""
import threading

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


"""
Shared mutable globals — imported by every other module.
This module must NOT import from any other overmesh module (except config)
to keep the dependency graph acyclic.
"""
import threading

# ---------------------------------------------------------------------------
# Node connections (Meshtastic)
# ---------------------------------------------------------------------------

connections      = {}
connections_lock = threading.Lock()

# ---------------------------------------------------------------------------
# MeshCore connections
# mc_connections[config_id] = {
#   "mc": MeshCore instance (or None),
#   "status": "connected" | "disconnected" | "connecting",
#   "config": node_cfg dict,
#   "node_id": pubkey prefix (12 hex chars) once connected,
#   "node_info": SELF_INFO payload dict,
#   "contacts": {pubkey: contact_dict},
# }
# ---------------------------------------------------------------------------

mc_connections      = {}
mc_connections_lock = threading.Lock()

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
# Traceroute
# ---------------------------------------------------------------------------

traceroute_lock = threading.Lock()

# Pending traceroute response slot.
# api_traceroute sets this before sending the TR request; on_text_receive in
# mesh.py reads it and calls done.set() when the TRACEROUTE_APP packet arrives.
# This avoids per-request pub.subscribe/unsubscribe which can silently fail.
_tr_pending_lock = threading.Lock()
_tr_pending      = {"node_id": None, "radio_id": None, "done": None, "result": None,
                    "started_at": None, "timeout": 30, "token": None,
                    "cancelled": False}

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

bot_activity      = []
bot_activity_lock = threading.Lock()

_bot_config_cache      = {}     # {radio_id_or_None: config_dict}
_bot_config_cache_lock = threading.Lock()

_motd_last_sent_per_radio = {}          # {radio_id: last_fired_timestamp}
_motd_event               = threading.Event()   # wake scheduler early on config change

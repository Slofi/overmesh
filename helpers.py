import json
import logging
import threading
import time

from db import get_favorites, get_ignored, upsert_node
from hw_models import hw_model_name
from state import connections, connections_lock, sse_clients, sse_lock

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE message ID counter (local to this module — only _next_msg_id uses it)
# ---------------------------------------------------------------------------

_msg_counter      = 0
_msg_counter_lock = threading.Lock()


def _next_msg_id():
    global _msg_counter
    with _msg_counter_lock:
        _msg_counter += 1
        return f"{int(time.time())}-{_msg_counter}"


def push_to_sse(data):
    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


# ---------------------------------------------------------------------------
# Node / iface lookup helpers
# ---------------------------------------------------------------------------

def _radio_id_for_iface(interface):
    """Return the config radio_id for a given meshtastic interface object."""
    with connections_lock:
        for rid, state in connections.items():
            if state.get("iface") is interface:
                return rid
    return None


def get_node_name(from_id):
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                for node in iface.nodes.values():
                    if node.get("user", {}).get("id") == from_id:
                        u  = node["user"]
                        ln = u.get("longName")
                        sn = u.get("shortName")
                        return (
                            (ln if ln and ln != "null" else None)
                            or (sn if sn and sn != "null" else None)
                            or from_id
                        )
    return from_id


def get_node_short_name(from_id):
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                for node in iface.nodes.values():
                    if node.get("user", {}).get("id") == from_id:
                        sn = node["user"].get("shortName")
                        return sn if sn and sn != "null" else from_id
    return from_id


def _node_ts(ts):
    if not ts:
        return 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return 0
    return max(0, ts)


def _format_last_heard(ts, now_ts=None):
    ts = _node_ts(ts)
    if not ts:
        return None
    now_ts = int(now_ts if now_ts is not None else time.time())
    delta = max(0, now_ts - ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def get_node_data():
    result    = []
    favorites = get_favorites()
    ignored   = get_ignored()

    with connections_lock:
        items = list(connections.items())

    for node_id, state in items:
        iface      = state.get("iface")
        status     = state.get("status")
        node_name  = state.get("config", {}).get("name", node_id)

        if status != "connected" or iface is None:
            result.append({
                "radio_id": node_id, "radio_name": node_name,
                "radio_status": status, "id": None,
                "long_name": "—", "short_name": "—",
                "snr": None, "rssi": None, "battery": None,
                "last_heard": None, "last_heard_ts": 0,
                "hops_away": None, "latitude": None, "longitude": None,
                "is_local": False, "is_favorite": False,
            })
            continue

        try:
            nodes          = dict(iface.nodes or {})   # snapshot to avoid RuntimeError if deleted concurrently
            local_id       = getattr(iface, "myInfo", None)
            local_node_num = getattr(local_id, "my_node_num", None) if local_id else None
            host_now       = int(time.time())
            newest_node_ts = max((_node_ts(n.get("lastHeard")) for n in nodes.values()), default=0)
            # Some radios keep a nodeDB clock ahead of the host. For nodeDB-derived
            # "last seen", use that radio snapshot's newest timestamp as the clock
            # instead of making every future value look freshly heard on refresh.
            last_heard_now = newest_node_ts if newest_node_ts > host_now + 300 else host_now

            for node_num, node in nodes.items():
                user     = node.get("user", {})
                pos      = node.get("position", {})
                metrics  = node.get("deviceMetrics", {})
                snr      = node.get("snr")
                last_heard = _node_ts(node.get("lastHeard"))
                battery  = metrics.get("batteryLevel")
                lat      = pos.get("latitude")
                lon      = pos.get("longitude")

                is_local    = (node.get("num") == local_node_num)
                node_id_str = user.get("id") or node_num  # fall back to iface.nodes key if user.id is empty
                if is_local:
                    last_heard = host_now

                last_seen_str = _format_last_heard(last_heard, host_now if is_local else last_heard_now)

                # For the local node with fixed position: use cached coords to prevent
                # stale firmware broadcasts from overriding what the user set.
                if is_local:
                    cached_lat = state.get("fixed_lat")
                    cached_lon = state.get("fixed_lon")
                    if cached_lat is not None:
                        lat = cached_lat
                        lon = cached_lon
                    else:
                        try:
                            if iface.localNode.localConfig.position.fixed_position and lat is not None:
                                with connections_lock:
                                    if connections.get(node_id):
                                        connections[node_id]["fixed_lat"] = lat
                                        connections[node_id]["fixed_lon"] = lon
                        except Exception:
                            pass

                node_entry = {
                    "radio_id":     node_id,    "radio_name":   node_name,
                    "radio_status": status,
                    "id":           node_id_str,
                    "long_name":    user.get("longName",  "Unknown"),
                    "short_name":   user.get("shortName", "?"),
                    "hw_model":     hw_model_name(user.get("hwModel")),
                    "snr":          snr,         "rssi":    node.get("rssi"),
                    "battery":      battery,
                    "last_heard":   last_seen_str,
                    "last_heard_ts": last_heard or 0,
                    "hops_away":    node.get("hopsAway", 0),
                    "latitude":     lat,         "longitude": lon,
                    "is_local":     is_local,
                    "is_favorite":  node_id_str in favorites,
                    "is_ignored":   node_id_str in ignored,
                    "air_util":     metrics.get("airUtilTx"),
                    "ch_util":      metrics.get("channelUtilization"),
                }

                if node_id_str:
                    upsert_node(node_entry)

                if node_id_str in ignored:
                    continue

                result.append(node_entry)

        except Exception as e:
            log.warning(f"[{node_id}] Error reading nodes: {e}")
            with connections_lock:
                if connections.get(node_id, {}).get("status") == "connected":
                    log.warning(f"[{node_id}] Marking disconnected due to error.")
                    connections[node_id]["status"] = "disconnected"
                    connections[node_id]["iface"]  = None

    return result

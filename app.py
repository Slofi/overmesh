import glob as _glob
import json
import logging
import math
import os
import queue
import random
import re
import signal
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

from pubsub import pub

import meshtastic
import meshtastic.serial_interface

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_NODE_ID_RE = re.compile(r'^[!^a-zA-Z0-9_\-]{1,64}$')

def _valid_node_id(node_id):
    """Basic sanity check on a mesh node ID before passing it to the library."""
    return bool(node_id and _NODE_ID_RE.match(str(node_id)))

with open(os.path.join(BASE_DIR, "config.json")) as f:
    CONFIG = json.load(f)

PREFS_DB_PATH = os.path.join(BASE_DIR, "overmesh_prefs.db")


@contextmanager
def get_prefs_db():
    conn = sqlite3.connect(PREFS_DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_msgs_db(radio_id):
    with connections_lock:
        db_path = connections.get(radio_id, {}).get("msgs_db")
    if not db_path:
        raise RuntimeError(f"get_msgs_db: msgs_db not yet initialized for radio_id={radio_id!r}")
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _radio_id_for_iface(interface):
    """Find the config radio_id for a given meshtastic interface object."""
    with connections_lock:
        for rid, state in connections.items():
            if state.get("iface") is interface:
                return rid
    return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_prefs_db():
    with get_prefs_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS nodes (
            id          TEXT PRIMARY KEY,
            long_name   TEXT,
            short_name  TEXT,
            first_seen  INTEGER,
            last_seen   INTEGER,
            last_snr    REAL,
            last_rssi   REAL,
            last_battery INTEGER,
            last_lat    REAL,
            last_lon    REAL,
            hops_away   INTEGER,
            radio_id    TEXT,
            is_local    INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0,
            is_ignored  INTEGER DEFAULT 0,
            notes       TEXT DEFAULT ''
        )''')
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN is_ignored INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS waypoints (
            id             INTEGER PRIMARY KEY,
            name           TEXT,
            description    TEXT,
            lat            REAL,
            lon            REAL,
            expire         INTEGER DEFAULT 0,
            icon           INTEGER DEFAULT 0,
            from_id        TEXT,
            radio_id       TEXT,
            ts             INTEGER,
            channel_index  INTEGER DEFAULT 0,
            destination_id  TEXT DEFAULT NULL,
            destination_ids TEXT DEFAULT NULL,
            marker_emoji    TEXT DEFAULT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS self_notes (
            id           INTEGER PRIMARY KEY,
            name         TEXT,
            description  TEXT,
            lat          REAL,
            lon          REAL,
            marker_emoji TEXT DEFAULT '📝',
            ts           INTEGER
        )''')
        # Migration for existing tables
        for col, defn in [("channel_index",  "INTEGER DEFAULT 0"),
                          ("destination_id",  "TEXT DEFAULT NULL"),
                          ("destination_ids", "TEXT DEFAULT NULL"),
                          ("marker_emoji",    "TEXT DEFAULT NULL")]:
            try:
                c.execute(f"ALTER TABLE waypoints ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass


def load_waypoints():
    with get_prefs_db() as conn:
        for row in conn.cursor().execute(
            "SELECT id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,channel_index,destination_id,marker_emoji,destination_ids FROM waypoints"
        ):
            try:
                dest_ids = json.loads(row[13]) if row[13] else None
            except (ValueError, TypeError):
                dest_ids = None
            wp = {"id": row[0], "name": row[1], "description": row[2],
                  "lat": row[3], "lon": row[4], "expire": row[5],
                  "icon": row[6], "from_id": row[7], "radio_id": row[8], "ts": row[9],
                  "channel_index": row[10] or 0, "destination_id": row[11],
                  "marker_emoji": row[12] or "📍", "destination_ids": dest_ids}
            waypoints_cache[row[0]] = wp


def load_notes():
    with get_prefs_db() as conn:
        for row in conn.cursor().execute(
            "SELECT id,name,description,lat,lon,marker_emoji,ts FROM self_notes"
        ):
            notes_cache[row[0]] = {
                "id": row[0], "name": row[1], "description": row[2],
                "lat": row[3], "lon": row[4],
                "marker_emoji": row[5] or "📝", "ts": row[6]
            }


def init_msgs_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS messages (
            id        TEXT PRIMARY KEY,
            from_id   TEXT,
            from_name TEXT,
            to_id     TEXT,
            to_name   TEXT,
            channel   INTEGER DEFAULT 0,
            text      TEXT,
            ts        INTEGER,
            sent      INTEGER DEFAULT 0,
            is_dm     INTEGER DEFAULT 0,
            status    TEXT DEFAULT 'pending',
            radio_id  TEXT
        )''')
        try:
            c.execute("ALTER TABLE messages ADD COLUMN radio_id TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_node(n):
    """Insert or update a node. first_seen is never overwritten after initial insert."""
    ts = n.get("last_heard_ts") or int(time.time())
    with get_prefs_db() as conn:
        conn.execute('''
            INSERT INTO nodes
                (id, long_name, short_name, first_seen, last_seen,
                 last_snr, last_rssi, last_battery,
                 last_lat, last_lon, hops_away, radio_id, is_local)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                long_name    = excluded.long_name,
                short_name   = excluded.short_name,
                last_seen    = excluded.last_seen,
                last_snr     = excluded.last_snr,
                last_rssi    = excluded.last_rssi,
                last_battery = excluded.last_battery,
                last_lat     = COALESCE(excluded.last_lat,  last_lat),
                last_lon     = COALESCE(excluded.last_lon,  last_lon),
                hops_away    = excluded.hops_away,
                radio_id     = excluded.radio_id,
                is_local     = excluded.is_local
        ''', (
            n["id"], n["long_name"], n["short_name"],
            ts, ts,
            n["snr"], n["rssi"], n["battery"],
            n["latitude"], n["longitude"],
            n["hops_away"], n["radio_id"], 1 if n["is_local"] else 0
        ))


def get_favorites():
    with get_prefs_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM nodes WHERE is_favorite=1")
        return {row[0] for row in c.fetchall()}


def get_ignored():
    with get_prefs_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM nodes WHERE is_ignored=1")
        return {row[0] for row in c.fetchall()}


def save_message(msg):
    radio_id = msg.get("radio_id")
    if not radio_id:
        return
    try:
        with get_msgs_db(radio_id) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO messages
                    (id, from_id, from_name, to_id, to_name, channel, text, ts, sent, is_dm, status, radio_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                msg['id'], msg.get('from_id'), msg.get('from_name'),
                msg.get('to_id'), msg.get('to_name'), msg.get('channel', 0),
                msg.get('text', ''), msg.get('ts', 0),
                1 if msg.get('sent') else 0,
                1 if msg.get('is_dm') else 0,
                msg.get('status', 'pending'),
                radio_id,
            ))
            conn.execute('''
                DELETE FROM messages WHERE id NOT IN (
                    SELECT id FROM messages ORDER BY ts DESC LIMIT 1000
                )
            ''')
    except RuntimeError as e:
        log.warning(f"save_message: dropped (radio not ready) — {e}")


def update_message_status(msg_id, status, radio_id):
    if not radio_id:
        return
    try:
        with get_msgs_db(radio_id) as conn:
            conn.execute("UPDATE messages SET status=? WHERE id=?", (status, msg_id))
    except RuntimeError as e:
        log.warning(f"update_message_status: skipped (radio not ready) — {e}")


def load_messages(radio_id):
    with get_msgs_db(radio_id) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM messages ORDER BY ts ASC LIMIT 500")
        rows = [dict(r) for r in c.fetchall()]
    for row in rows:
        row["radio_id"] = radio_id
    return rows


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_ts(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d-%m-%y %H:%M")


def get_db_nodes(sort_by="last_seen", sort_dir="desc", fav_first=True, show_ignored=False):
    valid_cols = {"last_seen", "first_seen", "long_name", "last_snr",
                  "last_rssi", "last_battery", "hops_away"}
    if sort_by not in valid_cols:
        sort_by = "last_seen"
    direction = "DESC" if sort_dir == "desc" else "ASC"
    order = f"is_favorite DESC, {sort_by} {direction}" if fav_first else f"{sort_by} {direction}"
    where = "WHERE is_ignored=1" if show_ignored else "WHERE is_ignored=0"

    with get_prefs_db() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(f"SELECT * FROM nodes {where} ORDER BY {order}")
        rows = [dict(r) for r in c.fetchall()]

    # Find local node position for distance calc
    local = next(
        (r for r in rows if r["is_local"] and r["last_lat"] and r["last_lon"]),
        None
    )
    for row in rows:
        if local and row["last_lat"] and row["last_lon"] and not row["is_local"]:
            row["distance"] = round(haversine(
                local["last_lat"], local["last_lon"],
                row["last_lat"], row["last_lon"]
            ), 2)
        else:
            row["distance"] = None
        row["first_seen_str"] = format_ts(row["first_seen"])
        row["last_seen_str"] = format_ts(row["last_seen"])

    return rows


# ---------------------------------------------------------------------------
# Node connections
# ---------------------------------------------------------------------------

connections = {}
connections_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

chat_messages    = []   # rolling buffer, last 500
chat_lock        = threading.Lock()
sse_clients      = []   # queues evicted when full (maxsize=100) in push_to_sse(); minor slow leak on disconnect without traffic
sse_lock         = threading.Lock()
pending_acks     = {}   # packet_id -> (msg_id, radio_id, ts)
pending_acks_lock = threading.Lock()

waypoints_cache  = {}   # wp_id -> waypoint dict
waypoints_lock   = threading.Lock()

notes_cache = {}  # note_id -> note dict
notes_lock  = threading.Lock()

_msg_counter      = 0
_msg_counter_lock = threading.Lock()

def _next_msg_id():
    global _msg_counter
    with _msg_counter_lock:
        _msg_counter += 1
        return f"{int(time.time())}-{_msg_counter}"


def push_to_sse(data):
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def get_node_name(from_id):
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                for node in iface.nodes.values():
                    if node.get("user", {}).get("id") == from_id:
                        u = node["user"]
                        ln = u.get("longName"); sn = u.get("shortName")
                        return (ln if ln and ln != "null" else None) or (sn if sn and sn != "null" else None) or from_id
    return from_id


def get_node_short_name(from_id):
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                for node in iface.nodes.values():
                    if node.get("user", {}).get("id") == from_id:
                        u = node["user"]
                        sn = u.get("shortName")
                        return sn if sn and sn != "null" else from_id
    return from_id


def on_text_receive(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum", "")

        if portnum == "ROUTING_APP":
            request_id = decoded.get("requestId")
            if request_id:
                with pending_acks_lock:
                    ack_entry = pending_acks.pop(request_id, None)
                if ack_entry:
                    msg_id, r_id, _ts = ack_entry
                    error   = decoded.get("routing", {}).get("errorReason", "NONE")
                    success = (error == "NONE")
                    update_message_status(msg_id, "delivered" if success else "failed", r_id)
                    push_to_sse(json.dumps({"type": "ack", "msg_id": msg_id, "success": success}))
            return

        # Capture any packet during active window OR passive listening mode
        with _sense_lock:
            _should_capture = (
                (_sense_state["active"] and time.time() < _sense_state["window_end"])
                or _sense_state["passive"]
            )
        if _should_capture:
            from_id   = packet.get("fromId", "?")
            snr       = packet.get("rxSnr")
            hop_limit = packet.get("hopLimit")
            hop_start = packet.get("hopStart")
            hops      = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
            name     = get_node_name(from_id)
            pos      = decoded.get("position", {}) or {}
            tel      = decoded.get("telemetry", {}) or {}
            dev      = tel.get("deviceMetrics", {}) or {}
            env      = tel.get("environmentMetrics", {}) or {}
            pwr      = tel.get("powerMetrics", {}) or {}
            user     = decoded.get("user", {}) or {}
            tr       = decoded.get("traceroute", {}) or {}
            nb       = decoded.get("neighborinfo", {}) or {}
            tr_route = tr.get("route", [])
            nb_list  = nb.get("neighbors", [])
            entry = {
                "from_id":       from_id,
                "name":          name,
                "portnum":       portnum,
                "snr":           snr,
                "hops":          hops,
                "lat":           pos.get("latitude") or None,
                "lon":           pos.get("longitude") or None,
                "alt":           pos.get("altitude") or None,
                "sats":          pos.get("satsInView") or None,
                "speed":         pos.get("speed") or None,
                "battery":       dev.get("batteryLevel") or None,
                "voltage":       dev.get("voltage") or None,
                "ch_util":       dev.get("channelUtilization") or None,
                "air_util":      dev.get("airUtilTx") or None,
                "hw_model":      user.get("hwModel") or None,
                "role":          user.get("role") or None,
                "short_name":    user.get("shortName") or None,
                "temp":          env.get("temperature") or None,
                "humidity":      env.get("relativeHumidity") or None,
                "pressure":      env.get("barometricPressure") or None,
                "route":         [get_node_name(f"!{r:08x}") or f"!{r:08x}" for r in tr_route] if tr_route else None,
                "neighbor_count": len(nb_list) if nb_list else None,
                "ts":            int(time.time()),
            }
            with _sense_lock:
                existing = next((r for r in _sense_state["responses"] if r["from_id"] == from_id), None)
                if existing:
                    existing.update({k: v for k, v in entry.items() if v is not None})
                    node_out = dict(existing)
                else:
                    if len(_sense_state["responses"]) >= 500:
                        # Evict oldest entry to keep memory bounded in long passive sessions
                        _sense_state["responses"].sort(key=lambda r: r.get("ts", 0))
                        _sense_state["responses"].pop(0)
                    _sense_state["responses"].append(entry)
                    node_out = dict(entry)
            push_to_sse(json.dumps({"type": "sense_response", "node": node_out, "radio_id": _radio_id_for_iface(interface)}))

        if portnum == "WAYPOINT_APP":
            try:
                wp_data = decoded.get("waypoint", {})
                wp_id   = wp_data.get("id", 0) or 0
                wp_name = wp_data.get("name", "Waypoint") or "Waypoint"
                wp_desc = wp_data.get("description", "") or ""
                lat_i   = wp_data.get("latitudeI") or wp_data.get("latitude_i", 0) or 0
                lon_i   = wp_data.get("longitudeI") or wp_data.get("longitude_i", 0) or 0
                wp_lat  = lat_i * 1e-7
                wp_lon  = lon_i * 1e-7
                wp_exp  = wp_data.get("expire", 0) or 0
                wp_icon = wp_data.get("icon", 0) or 0
                try:
                    marker_emoji = chr(wp_icon) if wp_icon else "📍"
                except (ValueError, OverflowError):
                    marker_emoji = "📍"
                from_id = packet.get("fromId", "")
                r_id    = _radio_id_for_iface(interface)
                if wp_id:
                    if wp_exp == 1:
                        # expire=1 is the Meshtastic deletion signal — no coords needed
                        with waypoints_lock:
                            waypoints_cache.pop(wp_id, None)
                        with get_prefs_db() as conn:
                            conn.cursor().execute("DELETE FROM waypoints WHERE id=?", (wp_id,))
                        push_to_sse(json.dumps({"type": "waypoint_deleted", "id": wp_id}))
                    elif wp_lat != 0 or wp_lon != 0:
                        wp_entry = {
                            "id": wp_id, "name": wp_name, "description": wp_desc,
                            "lat": wp_lat, "lon": wp_lon, "expire": wp_exp,
                            "icon": wp_icon, "from_id": from_id, "radio_id": r_id,
                            "ts": int(time.time()), "channel_index": 0, "destination_id": None,
                            "marker_emoji": marker_emoji
                        }
                        with waypoints_lock:
                            waypoints_cache[wp_id] = wp_entry
                        with get_prefs_db() as conn:
                            conn.cursor().execute(
                                "INSERT OR REPLACE INTO waypoints "
                                "(id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,channel_index,destination_id,marker_emoji) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (wp_id, wp_name, wp_desc, wp_lat, wp_lon, wp_exp, wp_icon,
                                 from_id, r_id, int(time.time()), 0, None, marker_emoji)
                            )
                        push_to_sse(json.dumps({"type": "waypoint", "waypoint": wp_entry}))
            except Exception as e:
                log.warning(f"Mark parse: {e}")
            return

        if portnum != "TEXT_MESSAGE_APP":
            return
        from_id  = packet.get("fromId", "?")
        to_id    = packet.get("toId", "^all")
        channel  = packet.get("channel", 0)
        text     = decoded.get("text", "")
        ts       = packet.get("rxTime") or int(time.time())
        snr      = packet.get("rxSnr")
        is_dm    = to_id != "^all"
        radio_id = _radio_id_for_iface(interface)
        msg = {
            "id":        _next_msg_id(),
            "from_id":   from_id,
            "from_name": get_node_name(from_id),
            "to_id":     to_id,
            "to_name":   get_node_name(to_id) if is_dm else "All",
            "channel":   channel,
            "text":      text,
            "ts":        ts,
            "snr":       snr,
            "sent":      False,
            "is_dm":     is_dm,
            "radio_id":  radio_id,
        }
        with chat_lock:
            chat_messages.append(msg)
            if len(chat_messages) > 500:
                chat_messages.pop(0)
        save_message(msg)
        push_to_sse(json.dumps(msg))
        # Hand off to bot (non-blocking, runs in background)
        handle_bot_command(packet, interface)
    except Exception as e:
        log.warning(f"Chat receive error: {e}")


def on_connection_lost(interface, topic=pub.AUTO_TOPIC):
    with connections_lock:
        for node_id, state in connections.items():
            if state.get("iface") is interface:
                log.warning(f"[{node_id}] Connection lost.")
                state["status"] = "disconnected"
                state["iface"] = None
                break
    # Explicitly release the serial port — without this, the old SerialInterface
    # object holds the lock until GC'd, causing ERRNO 11 on every reconnect attempt.
    try:
        interface.close()
    except Exception:
        pass


def find_port_by_usb_serial(usb_serial):
    """Find the current tty device path for a given USB hardware serial number."""
    import serial.tools.list_ports
    for p in serial.tools.list_ports.comports():
        if p.serial_number == usb_serial:
            return p.device
    return None


def connect_node(node_cfg):
    node_id = node_cfg["id"]
    with connections_lock:
        connections[node_id] = {"iface": None, "status": "connecting", "config": node_cfg}
    usb_serial = node_cfg.get("usb_serial")
    if usb_serial:
        port = find_port_by_usb_serial(usb_serial)
        if not port:
            log.info(f"[{node_id}] Device (USB serial {usb_serial}) not found, will retry")
            with connections_lock:
                connections[node_id]["status"] = "disconnected"
            return
    else:
        port = node_cfg.get("port", "")
    log.info(f"[{node_id}] Connecting to {port}...")
    iface = None
    try:
        iface = meshtastic.serial_interface.SerialInterface(port)
        # Set up msgs_db BEFORE marking connected — prevents save_message() race
        # where a packet arrives between status="connected" and msgs_db being set.
        msgs_db = None
        try:
            local_num = getattr(iface.myInfo, "my_node_num", None)
            if local_num:
                hex_id  = hex(local_num)[2:]
                msgs_db = f"overmesh_msgs_{hex_id}.db"
                init_msgs_db(msgs_db)
        except Exception as e:
            log.warning(f"[{node_id}] Could not set up per-radio DB: {e}")
        with connections_lock:
            connections[node_id]["iface"] = iface
            connections[node_id]["status"] = "connected"
            if msgs_db:
                connections[node_id]["msgs_db"] = msgs_db
        if msgs_db:
            node_cfg_live = next((n for n in CONFIG.get("nodes", []) if n["id"] == node_id), None)
            if node_cfg_live and node_cfg_live.get("msgs_db") != msgs_db:
                node_cfg_live["msgs_db"] = msgs_db
                save_config()
        log.info(f"[{node_id}] Connected.")
        if msgs_db:
            try:
                loaded = load_messages(node_id)
                with chat_lock:
                    chat_messages[:] = [m for m in chat_messages if m.get("radio_id") != node_id]
                    chat_messages.extend(loaded)
                    chat_messages.sort(key=lambda m: m.get("ts", 0))
                    if len(chat_messages) > 500:
                        chat_messages[:] = chat_messages[-500:]
                log.info(f"[{node_id}] msgs_db={msgs_db}, loaded {len(loaded)} messages")
            except Exception as e:
                log.warning(f"[{node_id}] Could not load messages: {e}")
    except Exception as e:
        log.warning(f"[{node_id}] Connection failed: {e}")
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
        with connections_lock:
            connections[node_id]["status"] = "disconnected"


def _is_iface_alive(iface):
    """Check if a serial interface is still open."""
    try:
        if hasattr(iface, 'stream') and iface.stream:
            return iface.stream.is_open
        return iface.myInfo is not None
    except Exception:
        return False


def _reconnect_disconnected():
    """Trigger immediate reconnect for any nodes currently marked disconnected."""
    with connections_lock:
        to_reconnect = [
            state["config"] for state in connections.values()
            if state.get("status") == "disconnected" and state.get("config")
        ]
    for node_cfg in to_reconnect:
        threading.Thread(target=connect_node, args=(node_cfg,), daemon=True).start()


def health_check_loop():
    """Detect silent disconnects that don't fire the connection.lost event."""
    while True:
        time.sleep(5)
        # Evict stale pending ACKs (messages that never got acknowledged)
        now = time.time()
        with pending_acks_lock:
            stale = [k for k, v in pending_acks.items() if now - v[2] > 300]
            for k in stale:
                del pending_acks[k]
        with connections_lock:
            to_check = [
                (nid, state.get("iface"))
                for nid, state in connections.items()
                if state.get("status") == "connected" and state.get("iface")
            ]
        for node_id, iface in to_check:
            if not iface or _is_iface_alive(iface):
                continue
            log.warning(f"[{node_id}] Health check: silent disconnect detected, forcing reconnect.")
            try:
                iface.close()
            except Exception:
                pass
            with connections_lock:
                if connections.get(node_id, {}).get("iface") is iface:
                    connections[node_id]["status"] = "disconnected"
                    connections[node_id]["iface"] = None


def reconnect_loop():
    while True:
        time.sleep(5)
        with connections_lock:
            to_reconnect = [
                cfg for cfg in CONFIG["nodes"]
                if cfg.get("enabled", True) and connections.get(cfg["id"], {}).get("status") not in ("connected", "connecting")
            ]
        for node_cfg in to_reconnect:
            threading.Thread(target=connect_node, args=(node_cfg,), daemon=True).start()


def get_node_data():
    result = []
    favorites = get_favorites()
    ignored   = get_ignored()

    with connections_lock:
        items = list(connections.items())

    for node_id, state in items:
        iface = state.get("iface")
        status = state.get("status")
        node_name = state.get("config", {}).get("name", node_id)

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
            nodes = iface.nodes or {}
            local_id = getattr(iface, "myInfo", None)
            local_node_num = getattr(local_id, "my_node_num", None) if local_id else None

            for node_num, node in nodes.items():
                user = node.get("user", {})
                pos = node.get("position", {})
                metrics = node.get("deviceMetrics", {})
                snr = node.get("snr")
                last_heard = node.get("lastHeard")
                battery = metrics.get("batteryLevel")
                lat = pos.get("latitude")
                lon = pos.get("longitude")

                last_seen_str = None
                if last_heard:
                    delta = int(time.time()) - last_heard
                    if delta < 60:
                        last_seen_str = f"{delta}s ago"
                    elif delta < 3600:
                        last_seen_str = f"{delta // 60}m ago"
                    elif delta < 86400:
                        last_seen_str = f"{delta // 3600}h ago"
                    else:
                        last_seen_str = f"{delta // 86400}d ago"

                is_local  = (node.get("num") == local_node_num)
                node_id_str = user.get("id", "")

                # For the local node with fixed position: use cached coords to prevent
                # stale firmware broadcasts from overriding what the user set.
                if is_local:
                    cached_lat = state.get("fixed_lat")
                    cached_lon = state.get("fixed_lon")
                    if cached_lat is not None:
                        lat = cached_lat
                        lon = cached_lon
                    else:
                        # No cache yet — seed it from the current iface.nodes position
                        # if fixed_position is enabled, so we have a baseline to protect
                        try:
                            if iface.localNode.localConfig.position.fixed_position and lat is not None:
                                with connections_lock:
                                    connections[node_id]["fixed_lat"] = lat
                                    connections[node_id]["fixed_lon"] = lon
                        except Exception:
                            pass

                node_entry = {
                    "radio_id": node_id, "radio_name": node_name,
                    "radio_status": status,
                    "id": node_id_str,
                    "long_name": user.get("longName", "Unknown"),
                    "short_name": user.get("shortName", "?"),
                    "snr": snr, "rssi": node.get("rssi"),
                    "battery": battery,
                    "last_heard": last_seen_str,
                    "last_heard_ts": last_heard or 0,
                    "hops_away": node.get("hopsAway", 0),
                    "latitude": lat, "longitude": lon,
                    "is_local": is_local,
                    "is_favorite": node_id_str in favorites,
                    "is_ignored":  node_id_str in ignored,
                    "air_util": metrics.get("airUtilTx"),
                    "ch_util":  metrics.get("channelUtilization"),
                }

                # Persist to DB always (so ignore persists even as node keeps being heard)
                if node_id_str:
                    upsert_node(node_entry)

                # Skip ignored nodes in live output
                if node_id_str in ignored:
                    continue

                result.append(node_entry)

        except Exception as e:
            log.warning(f"[{node_id}] Error reading nodes: {e}")
            with connections_lock:
                if connections.get(node_id, {}).get("status") == "connected":
                    log.warning(f"[{node_id}] Marking disconnected due to error.")
                    connections[node_id]["status"] = "disconnected"
                    connections[node_id]["iface"] = None

    return result


# ---------------------------------------------------------------------------
# Node action helpers
# ---------------------------------------------------------------------------

traceroute_lock = threading.Lock()


def get_any_iface():
    with connections_lock:
        for state in connections.values():
            if state.get("status") == "connected" and state.get("iface"):
                return state["iface"]
    return None


def get_any_iface_with_id():
    """Return (iface, radio_id) for the first connected radio."""
    with connections_lock:
        for rid, state in connections.items():
            if state.get("status") == "connected" and state.get("iface"):
                return state["iface"], rid
    return None, None


def get_iface_by_radio(radio_id):
    with connections_lock:
        state = connections.get(radio_id)
        if state and state.get("status") == "connected":
            return state.get("iface")
    return None


# Meshtastic enum maps (protobuf int values ↔ display names)
DEVICE_ROLES = {
    0: "CLIENT",
    1: "CLIENT_MUTE",
    2: "ROUTER",
    3: "ROUTER_CLIENT",
    5: "REPEATER",
    6: "TRACKER",
    7: "SENSOR",
    8: "TAK",
    9: "CLIENT_HIDDEN",
    10: "LOST_AND_FOUND",
    11: "TAK_TRACKER",
}

LORA_REGIONS = {
    0: "UNSET", 1: "US", 2: "EU_433", 3: "EU_868", 4: "CN",
    5: "JP", 6: "ANZ", 7: "KR", 8: "TW", 9: "RU", 10: "IN",
    11: "NZ_865", 12: "TH", 13: "LORA_24", 14: "UA_433",
    15: "UA_868", 16: "MY_433", 17: "MY_919", 18: "SG_923",
}

MODEM_PRESETS = {
    0: "LONG_FAST",
    1: "LONG_SLOW",
    2: "VERY_LONG_SLOW",
    3: "MEDIUM_SLOW",
    4: "MEDIUM_FAST",
    5: "SHORT_SLOW",
    6: "SHORT_FAST",
    7: "LONG_MODERATE",
    8: "SHORT_TURBO",
}


def resolve_node_name(num):
    """Resolve an integer node number to a short name."""
    hex_id = f"!{num:08x}"
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                # Try direct key lookup (int or str)
                node = iface.nodes.get(num) or iface.nodes.get(str(num))
                # Fallback: search by hex user ID string
                if not node:
                    for n in iface.nodes.values():
                        if n.get("user", {}).get("id") == hex_id:
                            node = n
                            break
                if node:
                    user = node.get("user", {})
                    return user.get("shortName") or user.get("longName") or hex_id
    return hex_id


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

BOT_CONFIG_PATH = os.path.join(BASE_DIR, "bot_config.json")

BOT_JOKES = [
    "Why do ham radio operators make great detectives? They always find the source.",
    "I tried to make a joke about LoRa... but it got lost in transmission.",
    "What do you call a Meshtastic node with no friends? A standalone router.",
    "Why did the antenna go to therapy? Too many bad connections.",
    "What's a ham operator's favorite exercise? Running a net.",
    "Why don't mesh nodes play poker? They always broadcast their hand.",
    "How do ham radio operators say goodbye? '73' — because 'later' wastes bandwidth.",
    "What do you call a broken SMA connector? An ex-SMA.",
    "Why was the SDR dongle always happy? Outstanding reception.",
    "What did the capacitor say to the battery? 'You charge me up.'",
    "Why did the Meshtastic node cross the road? To extend the mesh coverage.",
    "What do you call a GPS with attitude? A sassy-tellite.",
    "Why did the RF engineer stay calm at the party? Good signal management.",
    "What's a ham operator's least favorite game? Frequency freeze — nobody moves.",
    "What do you call a LoRa module that tells stories? A long-range narrator.",
    "Why do antennas make bad dancers? They always stick to one frequency.",
    "What did the oscilloscope say at the party? 'I see exactly what you're putting out.'",
    "Why did the mesh node apply for a promotion? Wanted to improve its hops.",
    "What do you call a ham operator who can't find a clear frequency? Static Phil.",
    "Why did the T-Beam go to the gym? To build better range.",
    "Why do Meshtastic nodes make bad gossips? They repeat everything to everyone.",
    "What did one resistor say to the other? 'You're too Ohm-work.'",
    "Why did the dipole break up with the Yagi? Too directional.",
    "What do you call a mesh network party? Hop till you drop.",
    "Why did the ham operator fail his driving test? Kept calling CQ at stop signs.",
    "What's an SDR's favorite music genre? Heavy metal — maximum bandwidth.",
    "Why don't LoRa nodes hurry? Built for range, not speed.",
    "What did the power supply say to the MCU? 'I've got you covered.'",
    "Why did the Heltec module overheat? Too many hot takes.",
    "What do you call a Meshtastic node on a mountaintop? A peak performer.",
    "Why did the spectrum analyzer go to the party? To see what was on the air.",
    "What's a ham operator's favorite book? 'War and Frequencies.'",
    "Why did the node change its long name? Too many traceroutes — it needed privacy.",
    "What do you call two ham operators chatting? A QSO. Three? A net. A hundred? A pile-up.",
    "Why was the mesh router always tired? Hopping all night.",
    "What did the soldering iron say? 'Let me make the connection.'",
    "Why do mesh nodes never get lonely? Always surrounded by peers.",
    "What's a Raspberry Pi's biggest fear? SD card corruption at 3 AM.",
    "Why did the GPS fail in the forest? Too many trees — nature's Faraday cage.",
    "What do you call a Meshtastic node in airplane mode? A very sad brick.",
    "Why did the battery only last two days? Too many overhead packets.",
    "Why did the repeater go to school? To improve its output.",
    "What do you call a node that only listens? An SWL with Meshtastic hardware.",
    "What did the multimeter say to the broken circuit? 'You've got no resistance left.'",
    "Why do ham operators love camping? Best antenna heights, no neighbors to complain.",
    "What's a mesh network's worst nightmare? A partition — nobody talks to anyone.",
    "Why did the packet arrive late? TTL issues and construction on the RF path.",
    "What do you call a Meshtastic node in space? Very out of range.",
    "What's a ham operator's favorite chord? The 40-meter dipole.",
    "Why did the node broadcast its GPS? It had nothing to hide — or forgot to check the config.",
    "What's LoRa's dating profile? 'Long range, slow but steady. Low maintenance.'",
    "Why did the Yagi go on a diet? Too much gain.",
    "What do you call a mesh network in a storm? A very committed flood fill.",
    "Why don't ham operators get lost? They always know their heading and their frequency.",
    "What did the inductor say to the capacitor? 'You complete my circuit.'",
    "Why did the Faraday cage get no texts? It blocked everything.",
    "What do you call a node that keeps dropping packets? An influencer.",
    "Why was the battery always the center of attention? Everyone depended on it.",
    "Why did the mesh network win the race? Every node routed for each other.",
    "What do you call a signal that arrives perfectly? A miracle. Probably line of sight.",
    "Why do Meshtastic nodes make bad politicians? They relay every message, never filter.",
    "What's a ham operator's favorite movie? 'The Silence of the Bands.'",
    "Why did the LiPo battery go to court? It swelled up and became a problem.",
    "What do you call a ham operator at the beach? A saltwater antenna tester.",
    "Why did the mesh node get a promotion? Best SNR in the network.",
    "What do you call a node with 0% battery? A doorstop.",
    "Why did the coax break up with the antenna? Bad match — too much standing wave.",
    "What's the difference between a ham operator and a pizza? The pizza can feed a family of four.",
    "Why did the SDR crash? Too many tabs open. Even software has limits.",
    "What do you call a Meshtastic node that's always right? A GPS-enabled contrarian.",
    "Why did the packet take the long route? Hop-timism.",
    "What's an antenna's least favorite weather? Ice storms. Explains itself.",
    "Why did the ham operator stay up all night? DX conditions were perfect and sleep is overrated.",
    "What do you call a radio channel with no traffic? Peaceful. Briefly.",
    "Why was the BMS always cranky? Constantly under pressure.",
    "What do you call a ham station with too many radios? A shack. Obviously.",
    "Why did the Meshtastic firmware update fail? Didn't read the changelog.",
    "What's a node's favorite snack? Packets. Obviously.",
    "Why did the ham operator move to the mountains? Lower noise floor, no HOA antenna restrictions.",
    "What do you call a mesh node that never sleeps? Power-hungry.",
    "Why did the coax go to the gym? To reduce its loss.",
    "What do you call a ham operator who talks too much? An AM station — full carrier, constant output.",
    "Why did the GPS receiver need glasses? Poor satellite geometry.",
    "What's a LoRa module's life motto? 'Slow and steady wins the range.'",
    "Why did the mesh node refuse to be a repeater? Didn't want the extra hops.",
    "What do you call a node with perfect timing? Crystal-controlled.",
    "Why did the ham operator cry? QRM on his favorite frequency. Again.",
    "Why do Meshtastic nodes make the best friends? They relay your message, never judge your SNR, and never drop you — unless the battery dies.",
    "Why did the SMA connector go to therapy? Attachment issues.",
    "What do you call a ham operator who only talks about equipment? An Elmer at full power.",
    "Why did the RF shield win the award? It kept everything in check.",
    "What do you call a band plan meeting? A spectrum summit.",
    "What do you call a node that arrived before anyone else? First-hop advantage.",
    "Why did the mesh node ignore the ping? It was on do-not-disturb.",
    "What do you call a ham operator with no antenna? An SWL waiting to happen.",
    "Why did the APRS beacon always seem happy? It always knew exactly where it stood.",
    "What do you call a ham operator who never logs contacts? A mystery — much like their antenna.",
    "Why did the packet get rejected? Wrong channel — story of its life.",
    "Why don't Faraday cages make good friends? They keep everything to themselves.",
    "What did one mesh node say to another? 'I've got you covered — literally, with RF.'",
    "Why did the LoRa chip win the marathon? Lowest power, longest range — story of its life.",
]

BOT_CONFIG_DEFAULTS = {
    "enabled": False,
    "bot_label": "OM Bot Ljubljana",
    "listen_channels": [0],
    "commands": {
        "ping":   {"enabled": True,  "respond_via": "same"},
        "ack":    {"enabled": True,  "respond_via": "same"},
        "test":   {"enabled": True,  "respond_via": "same"},
        "sitrep": {"enabled": True,  "respond_via": "same"},
        "cmd":    {"enabled": True,  "respond_via": "same"},
        "motd":   {"enabled": False, "respond_via": "same"},
        "joke":   {"enabled": True,  "respond_via": "same"},
        "dot":    {"enabled": True,  "respond_via": "same"},
        "relay":  {"enabled": True,  "respond_via": "dm", "relay_mode": "dm", "relay_channel": 0},
    },
    "motd": {
        "enabled": False,
        "mode": "interval",
        "interval_hours": 4,
        "fixed_time": "08:00",
        "message": "",
    },
}

bot_activity      = []
bot_activity_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Mesh Sense
# ---------------------------------------------------------------------------

_sense_state = {
    "active":         False,
    "passive":        bool(CONFIG.get("sense_passive", False)),   # continuously capture all heard packets
    "active_auto":    bool(CONFIG.get("sense_active_auto", False)),   # periodically broadcast position requests
    "last_triggered": 0,
    "window_end":     0,
    "responses":      [],   # list of response dicts, cleared on each new active sense
}
_sense_lock   = threading.Lock()
_active_auto_event = threading.Event()
_active_auto_running = False
_active_auto_running_lock = threading.Lock()

SENSE_COLLECTION_WINDOW = 60   # seconds to collect responses after broadcast
_motd_last_sent_per_radio = {}          # {radio_id: last_fired_timestamp}
_motd_event       = threading.Event()  # set this to wake scheduler early (e.g. config changed)

_bot_config_cache      = {}             # {radio_id_or_None: config_dict}
_bot_config_cache_lock = threading.Lock()


def _bot_config_path(radio_id):
    if not radio_id:
        return BOT_CONFIG_PATH
    # Reject any radio_id that isn't a simple alphanumeric/underscore/hyphen token
    # to prevent path traversal (e.g. "../../../etc/cron.d/pwn")
    import re as _re
    if not _re.match(r'^[a-zA-Z0-9_\-]{1,64}$', str(radio_id)):
        return BOT_CONFIG_PATH
    return os.path.join(BASE_DIR, f"bot_config_{radio_id}.json")


def load_bot_config(radio_id=None):
    global _bot_config_cache
    with _bot_config_cache_lock:
        if radio_id in _bot_config_cache:
            return json.loads(json.dumps(_bot_config_cache[radio_id]))
        path = _bot_config_path(radio_id)
        try:
            with open(path) as f:
                cfg = json.load(f)
        except FileNotFoundError:
            # Per-radio: migrate settings from legacy global file on first access
            if radio_id and os.path.exists(BOT_CONFIG_PATH):
                try:
                    with open(BOT_CONFIG_PATH) as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
            else:
                cfg = {}
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"bot_config load error ({e}), falling back to defaults")
            cfg = {}
        merged = json.loads(json.dumps(BOT_CONFIG_DEFAULTS))
        merged.update({k: v for k, v in cfg.items() if k != "commands"})
        for cmd, defaults in BOT_CONFIG_DEFAULTS["commands"].items():
            if cmd not in cfg.get("commands", {}):
                merged["commands"][cmd] = json.loads(json.dumps(defaults))
            else:
                d = json.loads(json.dumps(defaults))
                d.update(cfg["commands"][cmd])
                merged["commands"][cmd] = d
        _bot_config_cache[radio_id] = merged
        return json.loads(json.dumps(merged))


def save_bot_config(cfg, radio_id=None):
    global _bot_config_cache
    with _bot_config_cache_lock:
        _bot_config_cache[radio_id] = json.loads(json.dumps(cfg))
    path = _bot_config_path(radio_id)
    with tempfile.NamedTemporaryFile("w", dir=BASE_DIR, delete=False, suffix=".tmp") as tf:
        json.dump(cfg, tf, indent=2)
        tf.flush()
    os.replace(tf.name, path)


def log_bot_activity(from_name, command, response, channel):
    entry = {
        "ts":        int(time.time()),
        "from_name": from_name,
        "command":   command,
        "response":  response,
        "channel":   channel,
    }
    with bot_activity_lock:
        bot_activity.append(entry)
        if len(bot_activity) > 100:
            bot_activity.pop(0)
    push_to_sse(json.dumps({"type": "bot_activity", "entry": entry}))


def build_sitrep():
    nodes  = get_node_data()
    remote = [n for n in nodes if not n.get("is_local") and n.get("id")]
    total  = len(remote)

    def sname(n):
        return n.get("short_name") or n.get("long_name") or n.get("id", "?")

    if not remote:
        return "👀 In Mesh: 0"

    # Last Seen — 3 most recently heard with time string
    recent = sorted(remote, key=lambda n: n.get("last_heard_ts", 0), reverse=True)
    seen_lines = "\n".join(
        f"{sname(n)}, {n['last_heard']}" for n in recent[:3] if n.get("last_heard")
    )

    # Online — heard within last 15 min
    cutoff = int(time.time()) - 900
    online = sum(1 for n in remote if (n.get("last_heard_ts") or 0) > cutoff)

    # Air utilization from local node
    local_node = next((n for n in nodes if n.get("is_local")), None)
    air_util = local_node.get("air_util") if local_node else None

    parts = []
    if seen_lines: parts.append(f"Last Seen:\n{seen_lines}")
    summary = f"👀 In Mesh: {total} | 🟢 Online: {online}"
    if air_util is not None:
        summary += f" | 📶 Air: {air_util:.1f}%"
    parts.append(summary)
    return "\n\n".join(parts)


def build_motd_text(cfg):
    nodes  = get_node_data()
    remote = [n for n in nodes if not n.get("is_local") and n.get("id")]
    cutoff = int(time.time()) - 900
    online = sum(1 for n in remote if (n.get("last_heard_ts") or 0) > cutoff)
    custom = cfg["motd"].get("message", "")
    return f"Bot active | {online} nodes online" + (f" | {custom}" if custom else "")


def send_bot_response(iface, text, channel_index, dest_id=None):
    try:
        if dest_id:
            iface.sendText(text, destinationId=dest_id, channelIndex=channel_index)
        else:
            iface.sendText(text, channelIndex=channel_index)
    except Exception as e:
        log.warning(f"Bot send error: {e}")


def push_bot_chat_message(text, channel, radio_id, dest_id=None, to_name=None):
    """Surface a bot reply in the chat tab (same SSE stream as regular messages)."""
    msg = {
        "id":        _next_msg_id(),
        "from_id":   "bot",
        "from_name": "Bot",
        "to_id":     dest_id,
        "to_name":   to_name,
        "channel":   channel,
        "text":      text,
        "ts":        int(time.time()),
        "sent":      False,
        "is_dm":     bool(dest_id),
        "status":    "delivered",
        "radio_id":  radio_id,
    }
    with chat_lock:
        chat_messages.append(msg)
        if len(chat_messages) > 500:
            chat_messages.pop(0)
    save_message(msg)
    push_to_sse(json.dumps(msg))


def handle_bot_command(packet, interface):
    try:
        radio_id = _radio_id_for_iface(interface)
        cfg = load_bot_config(radio_id)
        if not cfg.get("enabled"):
            return

        prefix = f"[{cfg.get('bot_label', 'OM Bot')}]"

        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        from_id  = packet.get("fromId", "")
        to_id    = packet.get("toId", "^all")
        channel  = packet.get("channel", 0)
        text     = (decoded.get("text") or "").strip()
        is_dm    = (to_id != "^all")

        # Check if listening on this channel (DMs always listened)
        if not is_dm and channel not in cfg.get("listen_channels", [0]):
            return

        if not text:
            return

        from_name = get_node_name(from_id)

        # --- Multi-word: relay <TARGET> <message> ---
        if text.lower().startswith("relay "):
            if not cfg["commands"].get("relay", {}).get("enabled"):
                return
            parts = text.split(" ", 2)
            if len(parts) < 3:
                response = f"{prefix} Usage: relay <SHORT_NAME> <message>"
            else:
                target_name = parts[1]
                relay_msg   = parts[2]
                target_id   = None
                with connections_lock:
                    for state in connections.values():
                        iface_r = state.get("iface")
                        if iface_r and iface_r.nodes:
                            for node in iface_r.nodes.values():
                                user_n = node.get("user", {})
                                if user_n.get("shortName", "").lower() == target_name.lower():
                                    target_id = user_n.get("id")
                                    break
                        if target_id:
                            break
                if not target_id:
                    response = f"{prefix} Node '{target_name}' not found on mesh."
                else:
                    iface = interface
                    relay_cfg  = cfg["commands"].get("relay", {})
                    relay_mode = relay_cfg.get("relay_mode", "dm")
                    relay_ch   = int(relay_cfg.get("relay_channel", 0))
                    from_short = get_node_short_name(from_id)
                    relay_text = f"[{cfg.get('bot_label', 'OM Bot')} relay from {from_short}]: {relay_msg}"
                    log.info(f"Relay: {from_id} → {target_id} ({target_name}) mode={relay_mode} ch={channel}: {relay_msg!r}")
                    try:
                        if relay_mode == "broadcast":
                            iface.sendText(relay_text, channelIndex=relay_ch)
                        else:
                            iface.sendText(relay_text, destinationId=target_id, channelIndex=relay_ch)
                        response = f"{prefix} Relayed to {target_name}."
                        log_bot_activity(from_name, "relay", f"→ {target_name}: {relay_msg}", channel)
                    except Exception as e:
                        response = f"{prefix} Failed to relay to {target_name}: {e}"
                        log_bot_activity(from_name, "relay", f"FAILED → {target_name}: {e}", channel)
                    threading.Thread(target=send_bot_response, args=(iface, response, channel, from_id), daemon=True).start()
                    return
            threading.Thread(target=send_bot_response, args=(interface, response, channel, from_id), daemon=True).start()
            log_bot_activity(from_name, "relay", response, channel)
            return

        # --- Single-word commands (extra text after command is ignored) ---
        cmd_key = "dot" if text == "." else text.lower().split()[0].rstrip("!?.,;")
        if not cmd_key:
            return

        cmd_cfg = cfg["commands"].get(cmd_key)
        if not cmd_cfg or not cmd_cfg.get("enabled"):
            return

        cmd = cmd_key
        snr       = packet.get("rxSnr")
        rssi      = packet.get("rxRssi")
        snr_s     = f"SNR:{snr:.1f}" if snr is not None else "SNR:?"
        rs_s      = f" RSSI:{rssi}" if rssi is not None else ""
        rf_info   = f"[RF] {snr_s}{rs_s}"

        if cmd == "ping":
            witty = random.choice([
                "Still here, unfortunately.",
                "Not dead yet.",
                "Present and accounted for.",
                "You rang?",
                "Did someone say ping?",
                "Responding as trained.",
                "Loud and proud.",
                "Alive and well.",
                "Mesh is alive!",
                "Roger that, I exist.",
                "Beep boop, I'm a bot.",
                "Oh, you noticed me!",
                "Here!",
                "Indeed.",
                "Obviously.",
            ])
            response = f"🏓PONG | {witty} {rf_info}"
        elif cmd == "ack":
            response = random.choice([
                f"✋Ack to you! {rf_info}",
                f"✋Copy that! {rf_info}",
                f"✋Acknowledged {rf_info}",
                f"✋Received! {rf_info}",
                f"✋Loud and clear {rf_info}",
                f"✋Message received, filing it away {rf_info}",
                f"✋Got it, doing nothing about it {rf_info}",
                f"✋Confirmed. Mostly. {rf_info}",
                f"✋10-4, good buddy {rf_info}",
                f"✋Wilco {rf_info}",
            ])
        elif cmd == "test":
            response = random.choice([
                f"🎙Roger that! {rf_info}",
                f"🎙Testing 1,2,3 {rf_info}",
                f"🎙Testing, testing {rf_info}",
                f"🎙Read you loud and clear {rf_info}",
                f"🎙Signal received {rf_info}",
                f"🎙Loud and clear {rf_info}",
                f"🎙You are coming through loud and hot {rf_info}",
                f"🎙Heard you the first time {rf_info}",
                f"🎙Five by five {rf_info}",
                f"🎙Is this thing on? Yes, yes it is {rf_info}",
                f"🎙Transmission received, sanity intact {rf_info}",
                f"🎙Strength 5, readability 5 {rf_info}",
                f"🎙Clear as a bell {rf_info}",
                f"🎙Your signal is better than my day {rf_info}",
                f"🎙Mesh works, miracles do happen {rf_info}",
            ])
        elif cmd == "sitrep":
            response = build_sitrep()
        elif cmd == "cmd":
            enabled = sorted([c for c, v in cfg["commands"].items() if v.get("enabled") and c != "dot"])
            parts = []
            for c in enabled:
                label = c
                if c == "relay":
                    label += ' (relay "NAME" msg)'
                parts.append(label)
            response = "Commands: " + " | ".join(parts)
        elif cmd == "motd":
            response = build_motd_text(cfg)
        elif cmd == "joke":
            response = random.choice(BOT_JOKES)
        elif cmd == "dot":
            response = random.choice([
                "👀 Yes, I can see your . there ;)",
                "👀 Ah, a lone dot. Minimalist.",
                "👀 Received one (1) dot. Processing...",
                "👀 A dot! The mesh carried that with dignity.",
                "👀 Your . arrived safely. Was it worth it? ;)",
                "👀 10-4, dot received.",
                "👀 One dot, no context. Noted.",
                "👀 That's the whole message? Bold choice.",
                "👀 The mesh works! Proven by a dot.",
                "👀 Dot acknowledged. Over and out.",
                "👀 I see your . and raise you a response.",
                "👀 Short, sweet, and 100% LoRa.",
            ])
        else:
            return

        iface = interface

        response = f"{prefix} {response}"

        respond_via = cmd_cfg.get("respond_via", "same")
        if respond_via == "dm" or is_dm:
            dest         = from_id
            resp_channel = 0
        else:
            dest         = None
            resp_channel = channel

        t = threading.Thread(
            target=send_bot_response,
            args=(iface, response, resp_channel, dest),
            daemon=True
        )
        t.start()

        log_bot_activity(from_name, cmd, response, channel)
        push_bot_chat_message(response, resp_channel, radio_id, dest, from_name if dest else None)

    except Exception as e:
        log.warning(f"Bot command error: {e}")


def motd_scheduler_loop():
    """Wakes every 60s (or early on config change) and fires MOTD for each radio that needs it."""
    global _motd_last_sent_per_radio
    while True:
        try:
            _motd_event.wait(timeout=60)
            _motd_event.clear()

            with connections_lock:
                connected_ids = [rid for rid, state in connections.items()
                                 if state.get("status") == "connected" and state.get("iface")]

            now = int(time.time())
            for radio_id in connected_ids:
                with connections_lock:
                    state = connections.get(radio_id, {})
                    if state.get("status") != "connected":
                        continue
                    iface = state.get("iface")
                if not iface:
                    continue
                try:
                    cfg = load_bot_config(radio_id)
                    if not cfg.get("enabled") or not cfg.get("motd", {}).get("enabled"):
                        continue
                    last_sent = _motd_last_sent_per_radio.get(radio_id, 0)
                    mode = cfg["motd"].get("mode", "interval")
                    should_fire = False
                    if mode == "fixed":
                        fixed_time = cfg["motd"].get("fixed_time", "08:00")
                        try:
                            h, m = map(int, fixed_time.split(":"))
                            if not (0 <= h <= 23 and 0 <= m <= 59):
                                raise ValueError("out of range")
                        except ValueError:
                            h, m = 8, 0
                        now_dt  = datetime.now()
                        fire_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
                        # Fire if within 90s of the scheduled time and not already fired since then
                        if abs((now_dt - fire_dt).total_seconds()) < 90 and last_sent < fire_dt.timestamp():
                            should_fire = True
                    else:
                        interval_secs = cfg["motd"].get("interval_hours", 4) * 3600
                        should_fire = (now - last_sent) >= interval_secs
                    if not should_fire:
                        continue
                    text = f"[{cfg.get('bot_label', 'OM Bot')}] {build_motd_text(cfg)}"
                    channels = cfg.get("listen_channels", [0]) or [0]
                    for ch in channels:
                        send_bot_response(iface, text, ch)
                        push_bot_chat_message(text, ch, radio_id)
                    _motd_last_sent_per_radio[radio_id] = now
                    log_bot_activity("Bot", "motd_scheduled", text, channels[0])
                except Exception as e:
                    log.warning(f"MOTD error for {radio_id}: {e}")
        except Exception as e:
            log.warning(f"MOTD scheduler error: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/nodes")
def api_nodes():
    radio_id = request.args.get("radio_id")
    nodes = get_node_data()
    if radio_id:
        nodes = [n for n in nodes if n.get("radio_id") == radio_id]
    return jsonify(nodes)


@app.route("/api/debug/patch_pubkey/<node_hex>", methods=["POST"])
def api_debug_patch_pubkey(node_hex):
    """Inject/update a node's publicKey in both iface.nodes and iface.nodesByNum."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Debug endpoints are localhost-only"}), 403
    import base64
    data = request.get_json(silent=True) or {}
    pubkey_b64 = data.get("publicKey", "")
    node_num = data.get("node_num")  # integer node number
    try:
        pubkey_bytes = base64.b64decode(pubkey_b64)
    except Exception:
        return jsonify({"error": "Invalid base64 publicKey"}), 400
    patched = 0
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if not iface:
                continue
            if iface.nodes is not None:
                if node_hex not in iface.nodes:
                    iface.nodes[node_hex] = {"num": node_num, "user": {"id": node_hex}}
                iface.nodes[node_hex].setdefault("user", {})["publicKey"] = pubkey_bytes
            if node_num and hasattr(iface, "nodesByNum") and iface.nodesByNum is not None:
                if node_num not in iface.nodesByNum:
                    iface.nodesByNum[node_num] = iface.nodes.get(node_hex, {"num": node_num, "user": {"id": node_hex}})
                iface.nodesByNum[node_num].setdefault("user", {})["publicKey"] = pubkey_bytes
            patched += 1
    if patched:
        return jsonify({"ok": True, "node": node_hex, "node_num": node_num, "patched_radios": patched})
    return jsonify({"error": "No iface available"}), 503


@app.route("/api/debug/node_keys")
def api_debug_node_keys():
    """List all keys in iface.nodes — to find the right key format."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Debug endpoints are localhost-only"}), 403
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                return jsonify(list(iface.nodes.keys()))
    return jsonify([])


@app.route("/api/debug/raw_node/<node_hex>")
def api_debug_raw_node(node_hex):
    """Dump raw iface.nodes entry for a node — for debugging PKI key exchange."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Debug endpoints are localhost-only"}), 403
    with connections_lock:
        for state in connections.values():
            iface = state.get("iface")
            if iface and iface.nodes:
                node = iface.nodes.get(node_hex)
                if node:
                    import json
                    # publicKey is bytes, convert to hex string for display
                    raw = dict(node)
                    user = dict(raw.get("user", {}))
                    if "publicKey" in user and isinstance(user["publicKey"], (bytes, bytearray)):
                        user["publicKey"] = user["publicKey"].hex()
                    raw["user"] = user
                    return jsonify(raw)
    return jsonify({"error": "node not found"}), 404


@app.route("/api/status")
def api_status():
    with connections_lock:
        status = {
            nid: {"status": s["status"], "name": s["config"]["name"], "enabled": s["config"].get("enabled", True)}
            for nid, s in connections.items()
        }
    return jsonify(status)


@app.route("/api/db/nodes")
def api_db_nodes():
    sort_by      = request.args.get("sort", "last_seen")
    sort_dir     = request.args.get("dir", "desc")
    fav_first    = request.args.get("fav_first", "1") == "1"
    show_ignored = request.args.get("show_ignored", "0") == "1"
    return jsonify(get_db_nodes(sort_by, sort_dir, fav_first, show_ignored))


@app.route("/api/db/node/<node_id>", methods=["PATCH"])
def api_db_node_update(node_id):
    data = request.get_json(silent=True) or {}
    with get_prefs_db() as conn:
        c = conn.cursor()
        if "is_favorite" in data:
            c.execute("UPDATE nodes SET is_favorite=? WHERE id=?",
                      (1 if data["is_favorite"] else 0, node_id))
        if "is_ignored" in data:
            c.execute("UPDATE nodes SET is_ignored=? WHERE id=?",
                      (1 if data["is_ignored"] else 0, node_id))
        if "notes" in data:
            c.execute("UPDATE nodes SET notes=? WHERE id=?", (data["notes"], node_id))
    return jsonify({"ok": True})


@app.route("/api/db/node/<node_id>", methods=["DELETE"])
def api_db_node_delete(node_id):
    with get_prefs_db() as conn:
        conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
    radio_errors = []
    with connections_lock:
        items = list(connections.items())
    for _, state in items:
        iface = state.get("iface")
        if not iface:
            continue
        try:
            node = (iface.nodes or {}).get(node_id)
            node_num = node.get("num") if node else None
            # Evict from library in-memory dicts
            iface.nodes.pop(node_id, None)
            if node_num is not None:
                iface.nodesByNum.pop(node_num, None)
            # Also purge from radio flash nodeDB so stale PKI keys don't persist across restarts
            iface.localNode.removeNode(node_id)
        except Exception as e:
            radio_errors.append(str(e))
    return jsonify({"ok": True, "radio_errors": radio_errors})


# ---------------------------------------------------------------------------
# Node actions
# ---------------------------------------------------------------------------

@app.route("/api/node/<node_id>/traceroute", methods=["POST"])
def api_traceroute(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data     = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or request.args.get("radio_id")
    iface    = (get_iface_by_radio(radio_id) if radio_id else None) or get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    if not traceroute_lock.acquire(blocking=False):
        return jsonify({"error": "Another traceroute already in progress. Use /api/traceroute/reset to unlock."}), 429

    result = {}
    done = threading.Event()

    def on_receive(packet, interface):
        try:
            if interface is not iface:
                return  # ignore packets from other radios
            if packet.get("fromId") == node_id:
                decoded = packet.get("decoded", {})
                if decoded.get("portnum") == "TRACEROUTE_APP":
                    tr = decoded.get("traceroute", {})
                    result["route"]       = tr.get("route", [])
                    result["routeBack"]   = tr.get("routeBack", [])
                    result["snrTowards"]  = tr.get("snrTowards", [])
                    result["snrBack"]     = tr.get("snrBack", [])
                    done.set()
        except Exception as e:
            log.warning(f"Traceroute callback: {e}")

    # Read configurable hop limit (default 7 if not set)
    try:
        hop_limit = int(iface.localNode.localConfig.lora.hop_limit) or 7
    except Exception:
        hop_limit = 7

    try:
        pub.subscribe(on_receive, "meshtastic.receive")
        iface.sendTraceRoute(node_id, hopLimit=hop_limit)
        done.wait(timeout=30)
    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive")
        except Exception:
            pass
        try:
            traceroute_lock.release()
        except RuntimeError:
            pass  # Already force-released by /api/traceroute/reset

    if not done.is_set():
        return jsonify({"error": "Timeout — node did not respond (30s)"}), 504

    my_name    = "You"
    dest_name  = get_node_name(node_id)
    route      = [my_name] + [resolve_node_name(n) for n in result.get("route", [])]     + [dest_name]
    route_back = [dest_name] + [resolve_node_name(n) for n in result.get("routeBack", [])] + [my_name]
    # SNR values are stored as int * 4 in the protobuf
    snr_towards = [round(s / 4, 1) for s in result.get("snrTowards", [])]
    snr_back    = [round(s / 4, 1) for s in result.get("snrBack", [])]
    return jsonify({"route": route, "routeBack": route_back,
                    "snrTowards": snr_towards, "snrBack": snr_back})


@app.route("/api/traceroute/reset", methods=["POST"])
def api_traceroute_reset():
    """Force-release the traceroute lock if it got stuck (e.g. node disconnected during TR)."""
    if traceroute_lock.locked():
        try:
            traceroute_lock.release()
            return jsonify({"ok": True, "msg": "Lock released"})
        except RuntimeError:
            pass
    return jsonify({"ok": True, "msg": "Lock was not held"})


@app.route("/api/node/<node_id>/dm", methods=["POST"])
def api_dm(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data         = request.get_json(silent=True) or {}
    msg          = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "Empty message"}), 400
    radio_id_req = data.get("radio_id")
    if radio_id_req:
        iface    = get_iface_by_radio(radio_id_req)
        radio_id = radio_id_req if iface else None
        if not iface:
            iface, radio_id = get_any_iface_with_id()
    else:
        iface, radio_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        sent    = iface.sendText(msg, destinationId=node_id, wantAck=True)
        pkt_id  = sent.id if hasattr(sent, "id") else (sent.get("id") if isinstance(sent, dict) else None)
        chat_msg = {
            "id":        _next_msg_id(),
            "from_id":   "local",
            "from_name": "You",
            "to_id":     node_id,
            "to_name":   get_node_name(node_id),
            "channel":   0,
            "text":      msg,
            "ts":        int(time.time()),
            "sent":      True,
            "is_dm":     True,
            "status":    "pending",
            "radio_id":  radio_id,
        }
        with chat_lock:
            chat_messages.append(chat_msg)
        save_message(chat_msg)
        if pkt_id:
            with pending_acks_lock:
                pending_acks[pkt_id] = (chat_msg["id"], radio_id, time.time())
        push_to_sse(json.dumps(chat_msg))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node_id>/position", methods=["POST"])
def api_request_position(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data     = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or request.args.get("radio_id")
    iface    = (get_iface_by_radio(radio_id) if radio_id else None) or get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        iface.sendPosition(destinationId=node_id, wantResponse=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node_id>/info", methods=["POST"])
def api_node_info(node_id):
    if not _valid_node_id(node_id):
        return jsonify({"error": "Invalid node ID"}), 400
    data     = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or request.args.get("radio_id")
    iface    = (get_iface_by_radio(radio_id) if radio_id else None) or get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503

    # Request fresh telemetry from the node and wait for the response
    done = threading.Event()

    def on_receive(packet, interface):
        try:
            if interface is not iface:
                return  # ignore packets from other radios
            if packet.get("fromId") == node_id:
                decoded = packet.get("decoded", {})
                if decoded.get("portnum") == "TELEMETRY_APP":
                    done.set()
        except Exception:
            pass

    try:
        pub.subscribe(on_receive, "meshtastic.receive")
        iface.sendTelemetry(destinationId=node_id, wantResponse=True,
                            telemetryType="device_metrics")
        done.wait(timeout=15)
    except Exception as e:
        log.warning(f"Info request error: {e}")
    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive")
        except Exception:
            pass

    # Read the (now updated) node data from iface.nodes
    try:
        nodes = iface.nodes or {}
        for node_num, node in nodes.items():
            user = node.get("user", {})
            if user.get("id") == node_id:
                pos     = node.get("position", {}) or {}
                metrics = node.get("deviceMetrics", {}) or {}
                env     = node.get("environmentMetrics", {}) or {}
                last_heard = node.get("lastHeard")
                last_heard_str = None
                if last_heard:
                    delta = int(time.time()) - last_heard
                    if delta < 60:
                        last_heard_str = f"{delta}s ago"
                    elif delta < 3600:
                        last_heard_str = f"{delta // 60}m ago"
                    elif delta < 86400:
                        last_heard_str = f"{delta // 3600}h ago"
                    else:
                        last_heard_str = f"{delta // 86400}d ago"
                uptime = metrics.get("uptimeSeconds")
                uptime_str = None
                if uptime:
                    h, m = divmod(uptime, 3600)
                    m, s = divmod(m, 60)
                    uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
                fresh = done.is_set()
                return jsonify({
                    "fresh":        fresh,
                    "long_name":    user.get("longName"),
                    "short_name":   user.get("shortName"),
                    "hw_model":     user.get("hwModel"),
                    "role":         user.get("role"),
                    "snr":          node.get("snr"),
                    "rssi":         node.get("rssi"),
                    "hops_away":    node.get("hopsAway"),
                    "last_heard":   last_heard_str,
                    "battery":      metrics.get("batteryLevel"),
                    "voltage":      metrics.get("voltage"),
                    "channel_util": metrics.get("channelUtilization"),
                    "air_util_tx":  metrics.get("airUtilTx"),
                    "uptime":       uptime_str,
                    "latitude":     pos.get("latitude"),
                    "longitude":    pos.get("longitude"),
                    "altitude":     pos.get("altitude"),
                    "temperature":  env.get("temperature"),
                    "humidity":     env.get("relativeHumidity"),
                    "pressure":     env.get("barometricPressure"),
                })
        return jsonify({"error": "Node not found in radio memory"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Chat routes
# ---------------------------------------------------------------------------

@app.route("/api/chat/channels")
def api_chat_channels():
    radio_id = request.args.get("radio_id")
    iface = get_iface_by_radio(radio_id) if radio_id else get_any_iface()
    if not iface:
        return jsonify([{"index": 0, "name": "Primary", "role": 1}])
    try:
        result = []
        ln = getattr(iface, "localNode", None)
        if ln and hasattr(ln, "channels"):
            for ch in ln.channels:
                role = getattr(ch, "role", 0)
                if role == 0:
                    continue
                settings = getattr(ch, "settings", None)
                name     = (getattr(settings, "name", "") or "") if settings else ""
                index    = getattr(ch, "index", 0)
                result.append({
                    "index": index,
                    "name":  name or ("Primary" if index == 0 else f"CH{index}"),
                    "role":  role,
                })
        return jsonify(result or [{"index": 0, "name": "Primary", "role": 1}])
    except Exception as e:
        log.warning(f"Channel fetch: {e}")
        return jsonify([{"index": 0, "name": "Primary", "role": 1}])


@app.route("/api/chat/stream")
def api_chat_stream():
    q = queue.Queue(maxsize=100)
    with sse_lock:
        sse_clients.append(q)

    def generate():
        try:
            with chat_lock:
                history = list(chat_messages)
            for msg in history:
                yield f"data: {json.dumps({**msg, 'is_history': True})}\n\n"
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                try:
                    sse_clients.remove(q)
                except ValueError:
                    pass

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"]      = "no-cache"
    resp.headers["X-Accel-Buffering"]  = "no"
    return resp


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    data    = request.get_json(silent=True) or {}
    text    = (data.get("text") or "").strip()
    try:
        channel = max(0, min(7, int(data.get("channel", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid channel"}), 400
    dest_id = data.get("dest_id")
    if not text:
        return jsonify({"error": "Empty message"}), 400
    radio_id_req = data.get("radio_id")
    if radio_id_req:
        iface = get_iface_by_radio(radio_id_req)
        if not iface:
            return jsonify({"error": "Radio not connected"}), 503
        radio_id = radio_id_req
    else:
        iface, radio_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        if dest_id:
            sent = iface.sendText(text, destinationId=dest_id, channelIndex=channel, wantAck=True)
        else:
            sent = iface.sendText(text, channelIndex=channel, wantAck=True)
        pkt_id = sent.id if hasattr(sent, "id") else (sent.get("id") if isinstance(sent, dict) else None)
        # Resolve my own name
        my_name = "You"
        local_info = getattr(iface, "myInfo", None)
        local_num  = getattr(local_info, "my_node_num", None)
        if local_num and iface.nodes:
            local_node = iface.nodes.get(local_num)
            if local_node:
                u = local_node.get("user", {})
                my_name = u.get("longName") or u.get("shortName") or "You"
        msg = {
            "id":        _next_msg_id(),
            "from_id":   "local",
            "from_name": my_name,
            "to_id":     dest_id if dest_id else None,
            "to_name":   get_node_name(dest_id) if dest_id else None,
            "channel":   channel,
            "text":      text,
            "ts":        int(time.time()),
            "sent":      True,
            "is_dm":     bool(dest_id),
            "status":    "pending",
            "radio_id":  radio_id,
        }
        with chat_lock:
            chat_messages.append(msg)
        save_message(msg)
        if pkt_id:
            with pending_acks_lock:
                pending_acks[pkt_id] = (msg["id"], radio_id, time.time())
        push_to_sse(json.dumps(msg))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------

def save_config():
    tmp = None
    try:
        config_path = os.path.join(BASE_DIR, "config.json")
        with tempfile.NamedTemporaryFile("w", dir=BASE_DIR, delete=False, suffix=".tmp") as tf:
            json.dump(CONFIG, tf, indent=2)
            tmp = tf.name
        os.replace(tmp, config_path)
    except Exception as e:
        log.error(f"save_config failed: {e}")
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


@app.route("/api/settings/ports")
def api_settings_ports():
    import serial.tools.list_ports
    known_serials = {n.get("usb_serial") for n in CONFIG.get("nodes", []) if n.get("usb_serial")}
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


@app.route("/api/settings/nodes")
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


@app.route("/api/settings/nodes/add", methods=["POST"])
def api_settings_nodes_add():
    data = request.get_json(silent=True) or {}
    usb_serial = (data.get("usb_serial") or "").strip()
    port       = (data.get("port") or "").strip()
    name       = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not usb_serial and not port:
        return jsonify({"error": "Select a device"}), 400
    if usb_serial and any(n.get("usb_serial") == usb_serial for n in CONFIG["nodes"]):
        return jsonify({"error": "This device is already configured"}), 400
    if not usb_serial and any(n.get("port") == port and not n.get("usb_serial") for n in CONFIG["nodes"]):
        return jsonify({"error": f"Port {port} is already in use"}), 400
    node_id  = f"node_{int(time.time())}"
    new_node = {"id": node_id, "name": name, "enabled": True}
    if usb_serial:
        new_node["usb_serial"] = usb_serial
        new_node["port"] = port  # kept for display only
    else:
        new_node["port"] = port
    CONFIG["nodes"].append(new_node)
    save_config()
    threading.Thread(target=connect_node, args=(new_node,), daemon=True).start()
    return jsonify({"ok": True, "id": node_id})


@app.route("/api/settings/nodes/<node_id>/remove", methods=["POST"])
def api_settings_nodes_remove(node_id):
    node = next((n for n in CONFIG["nodes"] if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if len(CONFIG["nodes"]) == 1:
        return jsonify({"error": "Cannot remove the last radio"}), 400
    with connections_lock:
        state = connections.pop(node_id, None)
    if state and state.get("iface"):
        try:
            state["iface"].close()
        except Exception:
            pass
    CONFIG["nodes"] = [n for n in CONFIG["nodes"] if n["id"] != node_id]
    save_config()
    with chat_lock:
        chat_messages[:] = [m for m in chat_messages if m.get("radio_id") != node_id]
    push_to_sse(json.dumps({"type": "radio_removed", "radio_id": node_id}))
    return jsonify({"ok": True})


@app.route("/api/settings/nodes/<node_id>/delete", methods=["POST"])
def api_settings_nodes_delete(node_id):
    node = next((n for n in CONFIG["nodes"] if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if len(CONFIG["nodes"]) == 1:
        return jsonify({"error": "Cannot delete the last radio"}), 400
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
    # Validate msgs_db before using in a path — must match expected pattern
    if msgs_db and not re.match(r'^overmesh_msgs_[a-f0-9]{1,16}\.db$', str(msgs_db)):
        log.warning(f"[{node_id}] Refusing to delete unexpected msgs_db path: {msgs_db}")
        msgs_db = None
    CONFIG["nodes"] = [n for n in CONFIG["nodes"] if n["id"] != node_id]
    save_config()
    with chat_lock:
        chat_messages[:] = [m for m in chat_messages if m.get("radio_id") != node_id]
    deleted_db = False
    if msgs_db:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), msgs_db)
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
                deleted_db = True
                log.info(f"[{node_id}] Deleted DB: {msgs_db}")
            except Exception as e:
                log.warning(f"[{node_id}] Could not delete DB {msgs_db}: {e}")
    push_to_sse(json.dumps({"type": "radio_removed", "radio_id": node_id}))
    return jsonify({"ok": True, "deleted_db": deleted_db, "msgs_db": msgs_db})


@app.route("/api/settings/nodes/<node_id>/set_enabled", methods=["POST"])
def api_settings_nodes_set_enabled(node_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    node = next((n for n in CONFIG["nodes"] if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "Node not found"}), 404
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
    return jsonify({"ok": True})


@app.route("/api/settings/nodes/<node_id>/rename", methods=["POST"])
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
            save_config()
            return jsonify({"ok": True})
    return jsonify({"error": "Node not found"}), 404


@app.route("/api/settings/app", methods=["GET"])
def api_settings_app_get():
    return jsonify(CONFIG.get("app", {"font_size": "medium"}))


@app.route("/api/settings/app", methods=["POST"])
def api_settings_app_set():
    data = request.get_json(silent=True) or {}
    if "app" not in CONFIG:
        CONFIG["app"] = {}
    try:
        if "zoom" in data:
            CONFIG["app"]["zoom"] = max(50, min(200, int(data["zoom"])))
        if "sense_cooldown" in data:
            CONFIG["app"]["sense_cooldown"] = max(1, min(3600, int(data["sense_cooldown"])))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric value"}), 400
    if "accent_color" in data:
        val = str(data["accent_color"])
        if re.match(r'^#[0-9a-fA-F]{6}$', val):
            CONFIG["app"]["accent_color"] = val
    save_config()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Radio (local node) config routes
# ---------------------------------------------------------------------------

@app.route("/api/radio/<radio_id>/config")
def api_radio_config_get(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        # Owner info from live node data
        local_info = getattr(iface, "myInfo", None)
        local_num  = getattr(local_info, "my_node_num", None)
        user = {}
        if local_num and iface.nodes:
            for n in iface.nodes.values():
                if n.get("num") == local_num:
                    user = n.get("user", {})
                    break

        lc = getattr(iface.localNode, "localConfig",  None)
        mc = getattr(iface.localNode, "moduleConfig", None)

        def _int(obj, *attrs):
            try:
                v = obj
                for a in attrs: v = getattr(v, a)
                return int(v)
            except Exception: return 0

        def _bool(obj, *attrs):
            try:
                v = obj
                for a in attrs: v = getattr(v, a)
                return bool(v)
            except Exception: return False

        def _str(obj, *attrs):
            try:
                v = obj
                for a in attrs: v = getattr(v, a)
                return str(v or "")
            except Exception: return ""

        # device + lora
        role      = _int(lc, "device", "role")
        region    = _int(lc, "lora", "region")
        preset    = _int(lc, "lora", "modemPreset")
        tx_power  = _int(lc, "lora", "txPower")
        hop_limit = _int(lc, "lora", "hop_limit") or 3

        # position
        gps_mode             = _int(lc,  "position", "gps_mode")
        pos_broadcast_secs   = _int(lc,  "position", "position_broadcast_secs")
        smart_position       = _bool(lc, "position", "position_broadcast_smart_enabled")
        pos_precision        = _int(lc,  "position", "position_precision") or 13
        fixed_position       = _bool(lc, "position", "fixed_position")
        fixed_lat = fixed_lon = fixed_alt = None
        if fixed_position:
            node_hex = "!" + hex(local_num)[2:] if local_num else None
            if node_hex and iface.nodes:
                pos = iface.nodes.get(node_hex, {}).get("position", {})
                if pos.get("latitude") is not None:
                    fixed_lat = pos["latitude"]
                    fixed_lon = pos.get("longitude")
                    fixed_alt = pos.get("altitude", 0)

        # power
        power_saving        = _bool(lc, "power", "is_power_saving")
        shutdown_after_secs = _int(lc,  "power", "on_battery_shutdown_after_secs")

        # display
        screen_on_secs = _int(lc,  "display", "screen_on_secs")
        flip_screen    = _bool(lc, "display", "flip_screen")
        display_units  = _int(lc,  "display", "units")

        # telemetry (module config) — INT32_MAX (2147483647) means "use firmware default", normalize to 0
        tel_device = _int(mc, "telemetry", "device_update_interval")
        tel_env    = _int(mc, "telemetry", "environment_update_interval")
        if tel_device == 2147483647: tel_device = 0
        if tel_env    == 2147483647: tel_env    = 0

        # mqtt (module config)
        mqtt_enabled    = _bool(mc, "mqtt", "enabled")
        mqtt_address    = _str(mc,  "mqtt", "address")
        mqtt_username   = _str(mc,  "mqtt", "username")
        mqtt_pwd_set    = bool(_str(mc, "mqtt", "password"))
        mqtt_encryption = _bool(mc, "mqtt", "encryption_enabled")
        mqtt_json       = _bool(mc, "mqtt", "json_enabled")
        mqtt_tls        = _bool(mc, "mqtt", "tls_enabled")
        mqtt_map        = _bool(mc, "mqtt", "map_reporting_enabled")

        return jsonify({
            "long_name":    user.get("longName",  ""),
            "short_name":   user.get("shortName", ""),
            "hw_model":     user.get("hwModel",   ""),
            # device
            "role":          role,
            "device_roles":  DEVICE_ROLES,
            # lora
            "region":        region,
            "modem_preset":  preset,
            "tx_power":      tx_power,
            "hop_limit":     hop_limit,
            "lora_regions":  LORA_REGIONS,
            "modem_presets": MODEM_PRESETS,
            # position
            "gps_mode":            gps_mode,
            "pos_broadcast_secs":  pos_broadcast_secs,
            "smart_position":      smart_position,
            "pos_precision":       pos_precision,
            "fixed_position":      fixed_position,
            "fixed_lat":           fixed_lat,
            "fixed_lon":           fixed_lon,
            "fixed_alt":           fixed_alt or 0,
            # power
            "power_saving":        power_saving,
            "shutdown_after_secs": shutdown_after_secs,
            # display
            "screen_on_secs": screen_on_secs,
            "flip_screen":    flip_screen,
            "display_units":  display_units,
            # telemetry
            "tel_device": tel_device,
            "tel_env":    tel_env,
            # mqtt
            "mqtt_enabled":    mqtt_enabled,
            "mqtt_address":    mqtt_address,
            "mqtt_username":   mqtt_username,
            "mqtt_pwd_set":    mqtt_pwd_set,
            "mqtt_encryption": mqtt_encryption,
            "mqtt_json":       mqtt_json,
            "mqtt_tls":        mqtt_tls,
            "mqtt_map":        mqtt_map,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/announce", methods=["POST"])
def api_radio_announce(radio_id):
    """Re-broadcast NodeInfo (publicKey, name) to the mesh."""
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        local_num = getattr(iface.myInfo, "my_node_num", None)
        node_hex = "!" + hex(local_num)[2:] if local_num else None
        u = (iface.nodes or {}).get(node_hex, {}).get("user", {}) if node_hex else {}
        long_name  = u.get("longName",  "")
        short_name = u.get("shortName", "")
        iface.localNode.setOwner(long_name=long_name, short_name=short_name)
        return jsonify({"ok": True, "long_name": long_name, "short_name": short_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/owner", methods=["POST"])
def api_radio_owner_set(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data       = request.get_json(silent=True) or {}
    long_name  = (data.get("long_name")  or "").strip()[:39]
    short_name = (data.get("short_name") or "").strip()[:4]
    if not long_name or not short_name:
        return jsonify({"error": "Long name and short name are required"}), 400
    try:
        iface.localNode.setOwner(long_name=long_name, short_name=short_name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/device", methods=["POST"])
def api_radio_config_device(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        role = int(data.get("role", 0))
        iface.localNode.localConfig.device.role = role
        iface.localNode.writeConfig("device")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/lora", methods=["POST"])
def api_radio_config_lora(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        lc = iface.localNode.localConfig
        if "region" in data:
            lc.lora.region      = int(data["region"])
        if "modem_preset" in data:
            lc.lora.modemPreset = int(data["modem_preset"])
        if "tx_power" in data:
            lc.lora.txPower     = int(data["tx_power"])
        if "hop_limit" in data:
            lc.lora.hop_limit   = int(data["hop_limit"])
        iface.localNode.writeConfig("lora")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/channels")
def api_radio_channels_get(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        result = []
        for i, ch in enumerate(iface.localNode.channels):
            role     = int(getattr(ch, "role", 0))
            settings = getattr(ch, "settings", None)
            name     = (getattr(settings, "name", "") or "") if settings else ""
            psk      = bytes(getattr(settings, "psk", b"")) if settings else b""
            result.append({
                "index":   i,
                "name":    name,
                "role":    role,
                "psk_set": len(psk) > 0,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/position", methods=["POST"])
def api_radio_config_position(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        lc  = iface.localNode.localConfig
        pos = lc.position
        if "gps_mode"           in data: pos.gps_mode                  = int(data["gps_mode"])
        if "pos_broadcast_secs" in data: pos.position_broadcast_secs   = int(data["pos_broadcast_secs"])
        if "smart_position"     in data:
            try:    pos.position_broadcast_smart_enabled = bool(data["smart_position"])
            except AttributeError: pass
        if "pos_precision"      in data:
            try:    pos.position_precision = int(data["pos_precision"])
            except AttributeError: pass  # field not present in all firmware versions
        fixed = data.get("fixed_position")
        if fixed is not None:
            pos.fixed_position = bool(fixed)
        iface.localNode.writeConfig("position")
        # If fixing, push the coordinates to the node
        if fixed and data.get("lat") is not None:
            lat = float(data["lat"]); lon = float(data["lon"]); alt = int(data.get("alt", 0))
            try:
                iface.localNode.setFixedPosition(lat, lon, alt)
            except Exception:
                pass  # admin message sent best-effort
            # Immediately update in-memory position so UI reflects new coords before firmware broadcasts
            try:
                local_num = getattr(iface.myInfo, "my_node_num", None)
                if local_num is not None and local_num in iface.nodesByNum:
                    iface.nodesByNum[local_num]["position"] = {
                        "latitude": lat, "longitude": lon, "altitude": alt,
                        "latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7),
                        "fixedPosition": True,
                    }
            except Exception:
                pass
            # Cache the intended fixed position — prevents stale firmware broadcasts from overriding it
            with connections_lock:
                connections[radio_id]["fixed_lat"] = lat
                connections[radio_id]["fixed_lon"] = lon
        elif fixed is False:
            try:    iface.localNode.removeFixedPosition()
            except Exception: pass
            # Clear the position lock so live broadcasts are used again
            with connections_lock:
                connections[radio_id].pop("fixed_lat", None)
                connections[radio_id].pop("fixed_lon", None)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/power", methods=["POST"])
def api_radio_config_power(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        pwr = iface.localNode.localConfig.power
        if "power_saving"        in data: pwr.is_power_saving              = bool(data["power_saving"])
        if "shutdown_after_secs" in data: pwr.on_battery_shutdown_after_secs = int(data["shutdown_after_secs"])
        iface.localNode.writeConfig("power")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/display", methods=["POST"])
def api_radio_config_display(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        disp = iface.localNode.localConfig.display
        if "screen_on_secs" in data: disp.screen_on_secs = int(data["screen_on_secs"])
        if "flip_screen"    in data: disp.flip_screen    = bool(data["flip_screen"])
        if "display_units"  in data: disp.units          = int(data["display_units"])
        iface.localNode.writeConfig("display")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/telemetry", methods=["POST"])
def api_radio_config_telemetry(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        tel = iface.localNode.moduleConfig.telemetry
        if "tel_device" in data: tel.device_update_interval      = int(data["tel_device"])
        if "tel_env"    in data: tel.environment_update_interval  = int(data["tel_env"])
        iface.localNode.writeModuleConfig("telemetry")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/config/mqtt", methods=["POST"])
def api_radio_config_mqtt(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data = request.get_json(silent=True) or {}
    try:
        mqtt = iface.localNode.moduleConfig.mqtt
        if "mqtt_enabled"    in data: mqtt.enabled            = bool(data["mqtt_enabled"])
        if "mqtt_address"    in data: mqtt.address            = str(data["mqtt_address"])
        if "mqtt_username"   in data: mqtt.username           = str(data["mqtt_username"])
        if data.get("mqtt_password"):  mqtt.password          = str(data["mqtt_password"])
        if "mqtt_encryption" in data: mqtt.encryption_enabled = bool(data["mqtt_encryption"])
        if "mqtt_json"       in data: mqtt.json_enabled       = bool(data["mqtt_json"])
        if "mqtt_tls"        in data: mqtt.tls_enabled        = bool(data["mqtt_tls"])
        if "mqtt_map"        in data: mqtt.map_reporting_enabled = bool(data["mqtt_map"])
        iface.localNode.writeModuleConfig("mqtt")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/channels/<int:ch_index>", methods=["POST"])
def api_radio_channel_set(radio_id, ch_index):
    if ch_index < 0 or ch_index > 7:
        return jsonify({"error": "Channel index must be 0–7"}), 400
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    data     = request.get_json(silent=True) or {}
    role     = int(data.get("role", 0))
    name     = (data.get("name") or "")
    if len(name) > 11:
        return jsonify({"error": f"Channel name too long ({len(name)} chars, max 11)"}), 400
    psk_type = data.get("psk_type", "keep")   # keep / default / random / custom
    psk_hex  = data.get("psk_hex", "").replace(":", "").replace(" ", "")
    try:
        # If promoting to PRIMARY, demote existing PRIMARY to SECONDARY first
        if role == 1:
            for i, c in enumerate(iface.localNode.channels):
                if i != ch_index and int(getattr(c, "role", 0)) == 1:
                    iface.localNode.channels[i].role = 2
                    iface.localNode.writeChannel(i)

        ch = iface.localNode.channels[ch_index]
        ch.role = role

        if role == 0:   # DISABLED — clear settings
            ch.settings.name = ""
            ch.settings.psk  = b""
        else:
            ch.settings.name = name
            if psk_type == "default":
                ch.settings.psk = bytes([1])        # firmware expands to default Meshtastic key
            elif psk_type == "random":
                ch.settings.psk = os.urandom(32)
            elif psk_type == "custom":
                if not psk_hex:
                    return jsonify({"error": "Custom PSK hex is empty"}), 400
                try:
                    ch.settings.psk = bytes.fromhex(psk_hex)
                except ValueError:
                    return jsonify({"error": "Invalid hex string"}), 400
            # else "keep" — don't touch psk

        iface.localNode.writeChannel(ch_index)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Node utility actions (reboot, shutdown)
# ---------------------------------------------------------------------------

@app.route("/api/radio/<radio_id>/reboot", methods=["POST"])
def api_radio_reboot(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        iface.localNode.reboot(secs=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/radio/<radio_id>/shutdown", methods=["POST"])
def api_radio_shutdown(radio_id):
    iface = get_iface_by_radio(radio_id)
    if not iface:
        return jsonify({"error": "Radio not connected"}), 503
    try:
        iface.localNode.shutdown(secs=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Bot routes
# ---------------------------------------------------------------------------

@app.route("/api/bot/config", methods=["GET"])
def api_bot_config_get():
    radio_id = request.args.get("radio_id") or None
    return jsonify(load_bot_config(radio_id))


@app.route("/api/bot/config", methods=["POST"])
def api_bot_config_set():
    data = request.get_json(silent=True) or {}
    radio_id = data.pop("radio_id", None) or None
    # Basic structural validation before saving
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid config format"}), 400
    if "commands" in data and not isinstance(data["commands"], dict):
        return jsonify({"error": "commands must be an object"}), 400
    if "motd" in data and not isinstance(data["motd"], dict):
        return jsonify({"error": "motd must be an object"}), 400
    save_bot_config(data, radio_id)
    _motd_event.set()  # wake scheduler to pick up new config
    return jsonify({"ok": True})


@app.route("/api/bot/activity", methods=["GET"])
def api_bot_activity_get():
    with bot_activity_lock:
        return jsonify(list(reversed(bot_activity)))


@app.route("/api/bot/motd/test", methods=["POST"])
def api_bot_motd_test():
    data = request.get_json(silent=True) or {}
    radio_id = data.get("radio_id") or None
    cfg = load_bot_config(radio_id)
    if not cfg.get("enabled"):
        return jsonify({"ok": False, "error": "Bot is disabled"})
    if radio_id:
        with connections_lock:
            state = connections.get(radio_id, {})
            iface = state.get("iface") if state.get("status") == "connected" else None
    else:
        iface = get_any_iface()
    if not iface:
        return jsonify({"ok": False, "error": "No radio connected"})
    text = f"[{cfg.get('bot_label', 'OM Bot')}] {build_motd_text(cfg)}"
    channel = cfg.get("listen_channels", [0])[0]
    threading.Thread(target=send_bot_response, args=(iface, text, channel, None), daemon=True).start()
    log_bot_activity("Bot", "motd_test", text, channel)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Mesh Sense
# ---------------------------------------------------------------------------

def _run_sense_broadcast(iface, cooldown):
    """Broadcast position request, wait for collection window, close. Called in a thread."""
    try:
        iface.sendPosition(wantResponse=True)
    except Exception as e:
        log.warning(f"Sense broadcast error: {e}")
        with _sense_lock:
            _sense_state["active"] = False
            count = len(_sense_state["responses"])
        push_to_sse(json.dumps({"type": "sense_done", "count": count,
                                "error": "Node disconnected during broadcast"}))
        # sendPosition failed — reconnect immediately instead of waiting for the loop
        threading.Thread(target=_reconnect_disconnected, daemon=True).start()
        return
    # Brief pause then check: sendPosition sometimes silently kills the serial connection
    time.sleep(1)
    threading.Thread(target=_reconnect_disconnected, daemon=True).start()
    time.sleep(SENSE_COLLECTION_WINDOW - 1)
    with _sense_lock:
        _sense_state["active"] = False
        count = len(_sense_state["responses"])
    push_to_sse(json.dumps({"type": "sense_done", "count": count}))


def _active_auto_loop():
    """Background thread: re-trigger sense at cooldown intervals while active_auto is on."""
    global _active_auto_running
    with _active_auto_running_lock:
        if _active_auto_running:
            return
        _active_auto_running = True
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


@app.route("/api/mesh/sense", methods=["POST"])
def api_mesh_sense():
    iface = get_any_iface()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    cooldown = CONFIG.get("app", {}).get("sense_cooldown", 180)
    now = time.time()
    with _sense_lock:
        elapsed = now - _sense_state["last_triggered"]
        if _sense_state["last_triggered"] > 0 and elapsed < cooldown:
            return jsonify({"ok": False, "cooldown_remaining": int(cooldown - elapsed)}), 429
        _sense_state["active"]         = True
        _sense_state["last_triggered"] = now
        _sense_state["window_end"]     = now + SENSE_COLLECTION_WINDOW
        _sense_state["responses"]      = []
    threading.Thread(target=_run_sense_broadcast, args=(iface, cooldown), daemon=True).start()
    push_to_sse(json.dumps({"type": "sense_started", "window": SENSE_COLLECTION_WINDOW,
                            "cooldown": cooldown}))
    return jsonify({"ok": True, "window": SENSE_COLLECTION_WINDOW, "cooldown": cooldown})


@app.route("/api/mesh/sense/passive", methods=["POST"])
def api_sense_passive():
    with _sense_lock:
        new_val = not _sense_state["passive"]
        _sense_state["passive"] = new_val
    CONFIG["sense_passive"] = new_val
    save_config()
    return jsonify({"passive": new_val})


@app.route("/api/mesh/sense/active_auto", methods=["POST"])
def api_sense_active_auto():
    with _sense_lock:
        active_auto = not _sense_state["active_auto"]
        _sense_state["active_auto"] = active_auto
    CONFIG["sense_active_auto"] = active_auto
    save_config()
    if active_auto:
        _active_auto_event.clear()
        with _active_auto_running_lock:
            if not _active_auto_running:
                threading.Thread(target=_active_auto_loop, daemon=True).start()
    else:
        _active_auto_event.set()   # wake the waiting thread so it exits cleanly
    return jsonify({"active_auto": active_auto})


@app.route("/api/mesh/sense/status", methods=["GET"])
def api_mesh_sense_status():
    cooldown = CONFIG.get("app", {}).get("sense_cooldown", 180)
    now = time.time()
    with _sense_lock:
        remaining_window   = max(0, int(_sense_state["window_end"] - now))
        elapsed_since_last = now - _sense_state["last_triggered"]
        cooldown_remaining = max(0, int(cooldown - elapsed_since_last)) if _sense_state["last_triggered"] > 0 else 0
        payload = {
            "active":             _sense_state["active"],
            "passive":            _sense_state["passive"],
            "active_auto":        _sense_state["active_auto"],
            "cooldown_remaining": cooldown_remaining,
            "window_remaining":   remaining_window,
            "response_count":     len(_sense_state["responses"]),
            "responses":          list(_sense_state["responses"]),
            "cooldown":           cooldown,
        }
    return jsonify(payload)  # serialise outside the lock


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _kill():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------

@app.route("/api/waypoints")
def api_get_waypoints():
    with waypoints_lock:
        return jsonify(list(waypoints_cache.values()))


@app.route("/api/waypoints/send", methods=["POST"])
def api_send_waypoint():
    data = request.get_json(silent=True) or {}
    log.info(f"Mark send received: destination_ids={data.get('destination_ids')!r}")
    name = (data.get("name") or "").strip()[:30]
    try:
        lat = float(data.get("lat", 0))
        lon = float(data.get("lon", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400
    # Append coordinates so they're visible in external apps (e.g. Android MT)
    coord_str = f"\n{lat:.4f},{lon:.4f}"
    raw_desc = (data.get("description") or "").strip()
    desc = raw_desc[:100 - len(coord_str)] + coord_str
    marker_emoji = (data.get("marker_emoji") or "📍").strip() or "📍"
    radio_id     = data.get("radio_id")
    try:
        channel_idx  = max(0, min(7, int(data.get("channel_index", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid channel_index"}), 400
    # Accept either destination_ids (list) or legacy destination_id (single)
    dest_ids_raw = data.get("destination_ids")
    if dest_ids_raw and isinstance(dest_ids_raw, list):
        dest_ids = [d for d in dest_ids_raw if d and _valid_node_id(d)] or None
    else:
        single = (data.get("destination_id") or "").strip() or None
        dest_ids = [single] if single and _valid_node_id(single) else None
    if not name:
        return jsonify({"error": "Name required"}), 400
    if lat == 0 and lon == 0:
        return jsonify({"error": "Valid coordinates required"}), 400
    if radio_id:
        iface = get_iface_by_radio(radio_id)
        r_id  = radio_id if iface else None
        if not iface:
            iface, r_id = get_any_iface_with_id()
    else:
        iface, r_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        from meshtastic import mesh_pb2, portnums_pb2
        wp_id_req = data.get("wp_id")
        wp_id_val = int(wp_id_req) if wp_id_req else int(time.time() * 1000) % (2 ** 31)
        icon_cp = 0
        try:
            icon_cp = ord(marker_emoji[0])
        except (TypeError, IndexError):
            pass
        # Send one packet per destination (or broadcast if no specific dest)
        targets = dest_ids if dest_ids else [None]
        wp = mesh_pb2.Waypoint()
        wp.id          = wp_id_val
        wp.name        = name
        wp.description = desc
        wp.latitude_i  = int(lat * 1e7)
        wp.longitude_i = int(lon * 1e7)
        wp.expire      = 0
        wp.icon        = icon_cp
        serialized = wp.SerializeToString()
        for dest in targets:
            try:
                iface.sendData(
                    serialized,
                    portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                    destinationId=dest or "^all",
                    wantAck=False,
                    channelIndex=channel_idx
                )
                log.info(f"Mark send OK → dest={dest or '^all'} wp_id={wp_id_val}")
            except Exception as send_err:
                log.warning(f"Mark send FAILED to {dest}: {send_err}")
        dest_ids_json = json.dumps(dest_ids) if dest_ids else None
        primary_dest  = dest_ids[0] if dest_ids else None  # stable legacy field, not loop variable
        wp_entry = {
            "id": wp_id_val, "name": name, "description": desc,
            "lat": lat, "lon": lon, "expire": 0, "icon": icon_cp,
            "from_id": "local", "radio_id": r_id or "", "ts": int(time.time()),
            "channel_index": channel_idx, "destination_id": primary_dest,
            "destination_ids": dest_ids, "marker_emoji": marker_emoji
        }
        with waypoints_lock:
            waypoints_cache[wp_id_val] = wp_entry
        with get_prefs_db() as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO waypoints "
                "(id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,channel_index,destination_id,destination_ids,marker_emoji) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wp_id_val, name, desc, lat, lon, 0, icon_cp, "local", r_id or "", int(time.time()),
                 channel_idx, primary_dest, dest_ids_json, marker_emoji)
            )
        push_to_sse(json.dumps({"type": "waypoint", "waypoint": wp_entry}))
        # Send notification message on same channel/destination
        try:
            local_info = getattr(iface, "myInfo", None)
            local_num  = getattr(local_info, "my_node_num", None)
            my_name    = "?"
            if local_num and iface.nodes:
                local_node = iface.nodes.get(local_num)
                if local_node:
                    u = local_node.get("user", {})
                    my_name = u.get("longName") or u.get("shortName") or "?"
            dt_str     = datetime.now().strftime("%H:%M %d.%m.%Y")
            notif_text = f"📍 Mark \"{name}\" by {my_name} at {lat:.4f},{lon:.4f} — {dt_str}"
            notif_targets = dest_ids if dest_ids else [None]
            for nd in notif_targets:
                if nd:
                    iface.sendText(notif_text, destinationId=nd, channelIndex=channel_idx, wantAck=False)
                else:
                    iface.sendText(notif_text, channelIndex=channel_idx, wantAck=False)
            notif_msg = {
                "id": _next_msg_id(), "from_id": "local", "from_name": my_name,
                "to_id": primary_dest if primary_dest else None,
                "to_name": get_node_name(primary_dest) if primary_dest else None,
                "channel": channel_idx, "text": notif_text,
                "ts": int(time.time()), "sent": True,
                "is_dm": bool(primary_dest), "status": "sent", "radio_id": r_id or "",
            }
            with chat_lock:
                chat_messages.append(notif_msg)
            save_message(notif_msg)
            push_to_sse(json.dumps(notif_msg))
        except Exception as notif_err:
            log.warning(f"Mark notification failed: {notif_err}")
        return jsonify({"ok": True, "id": wp_id_val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/waypoints/<int:wp_id>", methods=["PUT"])
def api_edit_waypoint(wp_id):
    with waypoints_lock:
        wp_data = waypoints_cache.get(wp_id)
    if not wp_data:
        return jsonify({"error": "Mark not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    coord_str = f"\n{wp_data['lat']:.4f},{wp_data['lon']:.4f}"
    raw_desc  = (data.get("description") or "").strip()
    desc      = raw_desc[:100 - len(coord_str)] + coord_str
    marker_emoji = (data.get("marker_emoji") or "📍").strip() or "📍"
    radio_id  = data.get("radio_id") or wp_data.get("radio_id") or ""
    iface     = get_iface_by_radio(radio_id) if radio_id else None
    if not iface:
        iface, radio_id = get_any_iface_with_id()
    if not iface:
        return jsonify({"error": "No radio connected"}), 503
    try:
        from meshtastic import mesh_pb2, portnums_pb2
        icon_cp = 0
        try:
            icon_cp = ord(marker_emoji[0])
        except (TypeError, IndexError):
            pass
        new_ch_idx = data.get("channel_index")
        try:
            ch_idx = max(0, min(7, int(new_ch_idx))) if new_ch_idx is not None else (wp_data.get("channel_index") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid channel_index"}), 400
        # Accept destination_ids list or fall back to stored value
        dest_ids_raw = data.get("destination_ids")
        if dest_ids_raw is not None:
            dest_ids = [d for d in dest_ids_raw if d] if isinstance(dest_ids_raw, list) else None
        else:
            dest_ids = wp_data.get("destination_ids")
        dest = dest_ids[0] if dest_ids else None
        try:
            wp             = mesh_pb2.Waypoint()
            wp.id          = wp_id
            wp.name        = name
            wp.description = desc
            wp.latitude_i  = int(wp_data["lat"] * 1e7)
            wp.longitude_i = int(wp_data["lon"] * 1e7)
            wp.expire      = 0
            wp.icon        = icon_cp
            serialized = wp.SerializeToString()
            targets = dest_ids if dest_ids else [None]
            for d in targets:
                iface.sendData(
                    serialized,
                    portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                    destinationId=d or "^all",
                    wantAck=False,
                    channelIndex=ch_idx
                )
            log.info(f"Mark edit send OK → targets={targets} wp_id={wp_id}")
        except Exception as send_err:
            log.warning(f"Mark edit send FAILED: {send_err}")
            raise
        dest_ids_json = json.dumps(dest_ids) if dest_ids else None
        updated = dict(wp_data, name=name, description=desc,
                       marker_emoji=marker_emoji, icon=icon_cp,
                       channel_index=ch_idx, destination_id=dest,
                       destination_ids=dest_ids)
        with waypoints_lock:
            waypoints_cache[wp_id] = updated
        with get_prefs_db() as conn:
            conn.cursor().execute(
                "UPDATE waypoints SET name=?,description=?,marker_emoji=?,icon=?,channel_index=?,destination_id=?,destination_ids=? WHERE id=?",
                (name, desc, marker_emoji, icon_cp, ch_idx, dest, dest_ids_json, wp_id)
            )
        push_to_sse(json.dumps({"type": "waypoint", "waypoint": updated}))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/waypoints/<int:wp_id>", methods=["DELETE"])
def api_delete_waypoint(wp_id):
    with waypoints_lock:
        wp_data = waypoints_cache.pop(wp_id, None)
    if not wp_data:
        return jsonify({"error": "Mark not found"}), 404
    with get_prefs_db() as conn:
        conn.cursor().execute("DELETE FROM waypoints WHERE id=?", (wp_id,))
    # Send mesh deletion packet on the same channel/destination it was originally sent to
    try:
        radio_id = (wp_data.get("radio_id") or "").strip() if wp_data else ""
        iface = get_iface_by_radio(radio_id) if radio_id else None
        if not iface:
            iface, _ = get_any_iface_with_id()
        if iface and wp_data:
            from meshtastic import mesh_pb2, portnums_pb2
            ch_idx   = wp_data.get("channel_index") or 0
            dest_ids = wp_data.get("destination_ids")
            targets  = dest_ids if dest_ids else [wp_data.get("destination_id") or None]
            wp = mesh_pb2.Waypoint()
            wp.id          = wp_id
            wp.name        = wp_data.get("name", "")
            wp.latitude_i  = int(wp_data.get("lat", 0) * 1e7)
            wp.longitude_i = int(wp_data.get("lon", 0) * 1e7)
            wp.expire      = 1  # Meshtastic protocol: expire=1 means delete
            serialized = wp.SerializeToString()
            for dest in targets:
                iface.sendData(
                    serialized,
                    portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                    destinationId=dest or "^all",
                    wantAck=False,
                    channelIndex=ch_idx
                )
                log.info(f"Mark delete → dest={dest or '^all'} wp_id={wp_id} ch={ch_idx}")
    except Exception as e:
        log.warning(f"Mark mesh delete: {e}")
    push_to_sse(json.dumps({"type": "waypoint_deleted", "id": wp_id}))
    return jsonify({"ok": True})


@app.route("/api/waypoints/<int:wp_id>/rebroadcast", methods=["POST"])
def api_rebroadcast_waypoint(wp_id):
    with waypoints_lock:
        wp_data = waypoints_cache.get(wp_id)
    if not wp_data:
        return jsonify({"error": "Mark not found"}), 404
    try:
        radio_id = (wp_data.get("radio_id") or "").strip()
        iface = get_iface_by_radio(radio_id) if radio_id else None
        if not iface:
            iface, _ = get_any_iface_with_id()
        if not iface:
            return jsonify({"error": "No radio connected"}), 503
        from meshtastic import mesh_pb2, portnums_pb2
        marker_emoji = wp_data.get("marker_emoji") or "📍"
        wp             = mesh_pb2.Waypoint()
        wp.id          = wp_id
        wp.name        = wp_data.get("name", "")
        wp.description = wp_data.get("description", "")
        wp.latitude_i  = int(wp_data.get("lat", 0) * 1e7)
        wp.longitude_i = int(wp_data.get("lon", 0) * 1e7)
        wp.expire      = 0
        try:
            wp.icon = ord(marker_emoji[0])
        except (TypeError, IndexError):
            pass
        ch_idx   = wp_data.get("channel_index") or 0
        dest_ids = wp_data.get("destination_ids")
        targets  = dest_ids if dest_ids else [wp_data.get("destination_id") or None]
        for dest in targets:
            iface.sendData(
                wp.SerializeToString(),
                portNum=portnums_pb2.PortNum.Value("WAYPOINT_APP"),
                destinationId=dest or "^all",
                wantAck=False,
                channelIndex=ch_idx
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Self Notes API
# ---------------------------------------------------------------------------

@app.route("/api/notes")
def api_get_notes():
    with notes_lock:
        return jsonify(list(notes_cache.values()))


@app.route("/api/notes", methods=["POST"])
def api_save_note():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    desc         = (data.get("description") or "").strip()[:200]
    try:
        lat = float(data.get("lat") or 0)
        lon = float(data.get("lon") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400
    marker_emoji = (data.get("marker_emoji") or "📝").strip() or "📝"
    note_id = int(time.time() * 1000) + random.randint(0, 999)
    note = {"id": note_id, "name": name, "description": desc,
            "lat": lat, "lon": lon, "marker_emoji": marker_emoji, "ts": int(time.time())}
    with notes_lock:
        notes_cache[note_id] = note
    with get_prefs_db() as conn:
        conn.cursor().execute(
            "INSERT INTO self_notes (id,name,description,lat,lon,marker_emoji,ts) VALUES (?,?,?,?,?,?,?)",
            (note_id, name, desc, lat, lon, marker_emoji, int(time.time()))
        )
    push_to_sse(json.dumps({"type": "note", "note": note}))
    return jsonify({"ok": True, "id": note_id})


@app.route("/api/notes/<int:note_id>", methods=["PUT"])
def api_edit_note(note_id):
    with notes_lock:
        note = notes_cache.get(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name required"}), 400
    desc         = (data.get("description") or "").strip()[:200]
    marker_emoji = (data.get("marker_emoji") or "📝").strip() or "📝"
    updated = dict(note, name=name, description=desc, marker_emoji=marker_emoji)
    with notes_lock:
        notes_cache[note_id] = updated
    with get_prefs_db() as conn:
        conn.cursor().execute(
            "UPDATE self_notes SET name=?,description=?,marker_emoji=? WHERE id=?",
            (name, desc, marker_emoji, note_id)
        )
    push_to_sse(json.dumps({"type": "note", "note": updated}))
    return jsonify({"ok": True})


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
def api_delete_note(note_id):
    with notes_lock:
        if note_id not in notes_cache:
            return jsonify({"error": "Note not found"}), 404
        notes_cache.pop(note_id, None)  # remove under same lock — prevents duplicate deletes
    with get_prefs_db() as conn:
        conn.cursor().execute("DELETE FROM self_notes WHERE id=?", (note_id,))
    push_to_sse(json.dumps({"type": "note_deleted", "id": note_id}))
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
    # per-radio message DBs are initialized in connect_node() as each radio connects
    pub.subscribe(on_text_receive, "meshtastic.receive")
    try:
        pub.subscribe(on_connection_lost, "meshtastic.connection.lost")
    except Exception as e:
        log.warning(f"Could not subscribe to connection.lost: {e}")
    for node_cfg in CONFIG["nodes"]:
        if node_cfg.get("enabled", True):
            threading.Thread(target=connect_node, args=(node_cfg,), daemon=True).start()
    threading.Thread(target=reconnect_loop,        daemon=True).start()
    threading.Thread(target=health_check_loop,     daemon=True).start()
    threading.Thread(target=motd_scheduler_loop,   daemon=True).start()
    if _sense_state["active_auto"]:
        _active_auto_event.clear()
        with _active_auto_running_lock:
            if not _active_auto_running:
                threading.Thread(target=_active_auto_loop, daemon=True).start()


if __name__ == "__main__":
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # ignore serial port hangup (USB disconnect)
    startup()
    app.run(host="0.0.0.0", port=CONFIG.get("port", 8081), debug=False)

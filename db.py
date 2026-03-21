import json
import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from config import PREFS_DB_PATH
from state import connections, connections_lock, waypoints_cache, notes_cache

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB connection helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Schema init
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
        # Migrations for existing tables
        for col, defn in [("channel_index",  "INTEGER DEFAULT 0"),
                          ("destination_id",  "TEXT DEFAULT NULL"),
                          ("destination_ids", "TEXT DEFAULT NULL"),
                          ("marker_emoji",    "TEXT DEFAULT NULL")]:
            try:
                c.execute(f"ALTER TABLE waypoints ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass


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


# ---------------------------------------------------------------------------
# Waypoints + Notes cache loaders
# ---------------------------------------------------------------------------

def load_waypoints():
    with get_prefs_db() as conn:
        for row in conn.cursor().execute(
            "SELECT id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,"
            "channel_index,destination_id,marker_emoji,destination_ids FROM waypoints"
        ):
            try:
                dest_ids = json.loads(row[13]) if row[13] else None
            except (ValueError, TypeError):
                dest_ids = None
            wp = {
                "id": row[0], "name": row[1], "description": row[2],
                "lat": row[3], "lon": row[4], "expire": row[5],
                "icon": row[6], "from_id": row[7], "radio_id": row[8], "ts": row[9],
                "channel_index": row[10] or 0, "destination_id": row[11],
                "marker_emoji": row[12] or "📍", "destination_ids": dest_ids,
            }
            waypoints_cache[row[0]] = wp


def load_notes():
    with get_prefs_db() as conn:
        for row in conn.cursor().execute(
            "SELECT id,name,description,lat,lon,marker_emoji,ts FROM self_notes"
        ):
            notes_cache[row[0]] = {
                "id": row[0], "name": row[1], "description": row[2],
                "lat": row[3], "lon": row[4],
                "marker_emoji": row[5] or "📝", "ts": row[6],
            }


# ---------------------------------------------------------------------------
# Node operations
# ---------------------------------------------------------------------------

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
            n["hops_away"], n["radio_id"], 1 if n["is_local"] else 0,
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


def get_db_nodes(sort_by="last_seen", sort_dir="desc", fav_first=True, show_ignored=False):
    valid_cols = {"last_seen", "first_seen", "long_name", "last_snr",
                  "last_rssi", "last_battery", "hops_away"}
    if sort_by not in valid_cols:
        sort_by = "last_seen"
    direction = "DESC" if sort_dir == "desc" else "ASC"
    order     = f"is_favorite DESC, {sort_by} {direction}" if fav_first else f"{sort_by} {direction}"
    where     = "WHERE is_ignored=1" if show_ignored else "WHERE is_ignored=0"

    with get_prefs_db() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(f"SELECT * FROM nodes {where} ORDER BY {order}")
        rows = [dict(r) for r in c.fetchall()]

    local = next(
        (r for r in rows if r["is_local"] and r["last_lat"] and r["last_lon"]),
        None,
    )
    for row in rows:
        if local and row["last_lat"] and row["last_lon"] and not row["is_local"]:
            row["distance"] = round(_haversine(
                local["last_lat"], local["last_lon"],
                row["last_lat"], row["last_lon"],
            ), 2)
        else:
            row["distance"] = None
        row["first_seen_str"] = _format_ts(row["first_seen"])
        row["last_seen_str"]  = _format_ts(row["last_seen"])

    return rows


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _format_ts(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d-%m-%y %H:%M")

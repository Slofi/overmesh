import json
import hashlib
import logging
import math
import os
import uuid
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from config import DATA_DIR, PREFS_DB_PATH
from state import connections, connections_lock, waypoints_cache, waypoints_lock, notes_cache, notes_lock

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


def _safe_radio_id(radio_id):
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(radio_id or "unknown"))


def mc_msgs_db_path(radio_id):
    return os.path.join(DATA_DIR, f"overmesh_mc_msgs_{_safe_radio_id(radio_id)}.db")


# Registry: config_id -> stable db path (populated at connection time by mesh_mc)
_mc_msgs_db_registry: dict = {}
_mc_msgs_db_registry_lock = threading.Lock()


def register_mc_msgs_db(config_id: str, stable_db_path: str) -> None:
    """Register a stable hardware-keyed DB path for a given MC config_id."""
    with _mc_msgs_db_registry_lock:
        _mc_msgs_db_registry[config_id] = stable_db_path


def _resolved_mc_msgs_db_path(radio_id: str) -> str:
    """Return stable path if registered, else fall back to config-id path."""
    with _mc_msgs_db_registry_lock:
        return _mc_msgs_db_registry.get(radio_id) or mc_msgs_db_path(radio_id)


def mc_passive_db_path(radio_id):
    return os.path.join(DATA_DIR, f"overmesh_mc_passive_{_safe_radio_id(radio_id)}.db")


_mc_passive_db_registry: dict = {}
_mc_passive_db_registry_lock = threading.Lock()


def register_mc_passive_db(config_id: str, stable_db_path: str) -> None:
    """Register a stable hardware-keyed passive DB path for a given MC config_id."""
    with _mc_passive_db_registry_lock:
        _mc_passive_db_registry[config_id] = stable_db_path


def _resolved_mc_passive_db_path(radio_id: str) -> str:
    with _mc_passive_db_registry_lock:
        return _mc_passive_db_registry.get(radio_id) or mc_passive_db_path(radio_id)


@contextmanager
def get_mc_passive_db(radio_id):
    db_path = _resolved_mc_passive_db_path(radio_id)
    _init_mc_passive_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_mc_passive_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS passive_obs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pubkey_pre   TEXT NOT NULL,
            obs_type     TEXT NOT NULL,
            ts           INTEGER NOT NULL,
            rssi         REAL,
            snr          REAL,
            path_len     INTEGER,
            path         TEXT,
            path_hash_size INTEGER,
            payload_type TEXT,
            route_type   TEXT,
            lat          REAL,
            lon          REAL,
            collector_id  TEXT,
            collector_lat REAL,
            collector_lon REAL
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_passive_pubkey ON passive_obs (pubkey_pre, ts DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_passive_ts ON passive_obs (ts DESC)')
        for col, defn in [('collector_id', 'TEXT'), ('collector_lat', 'REAL'), ('collector_lon', 'REAL')]:
            try:
                c.execute(f'ALTER TABLE passive_obs ADD COLUMN {col} {defn}')
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def save_passive_obs(radio_id, pubkey_pre, obs_type, rssi=None, snr=None,
                     path_len=None, path=None, path_hash_size=None,
                     payload_type=None, route_type=None, lat=None, lon=None,
                     collector_id=None, collector_lat=None, collector_lon=None,
                     max_per_contact=50, observed_ts=None):
    ts = int(observed_ts) if observed_ts is not None else int(time.time())
    with get_mc_passive_db(radio_id) as conn:
        c = conn.cursor()
        c.execute(
            '''INSERT INTO passive_obs
               (pubkey_pre, obs_type, ts, rssi, snr, path_len, path,
                path_hash_size, payload_type, route_type, lat, lon,
                collector_id, collector_lat, collector_lon)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pubkey_pre, obs_type, ts, rssi, snr, path_len, path,
             path_hash_size, payload_type, route_type, lat, lon,
             collector_id, collector_lat, collector_lon)
        )
        # Enforce per-contact cap: keep only the most recent max_per_contact rows
        c.execute(
            '''DELETE FROM passive_obs WHERE pubkey_pre=? AND id NOT IN (
               SELECT id FROM passive_obs WHERE pubkey_pre=?
               ORDER BY ts DESC LIMIT ?)''',
            (pubkey_pre, pubkey_pre, max_per_contact)
        )


def load_passive_obs(radio_id, pubkey_pre=None, limit=100):
    with get_mc_passive_db(radio_id) as conn:
        c = conn.cursor()
        if pubkey_pre:
            c.execute(
                'SELECT * FROM passive_obs WHERE pubkey_pre=? ORDER BY ts DESC LIMIT ?',
                (pubkey_pre, limit)
            )
        else:
            c.execute(
                'SELECT * FROM passive_obs ORDER BY ts DESC LIMIT ?',
                (limit,)
            )
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in c.fetchall()]


def load_passive_obs_summary(radio_id, pubkey_prefixes):
    """Return best obs (latest RSSI/SNR, obs count) keyed by pubkey_pre."""
    if not pubkey_prefixes:
        return {}
    placeholders = ','.join('?' * len(pubkey_prefixes))
    with get_mc_passive_db(radio_id) as conn:
        c = conn.cursor()
        c.execute(
            f'''SELECT pubkey_pre,
                       COUNT(*) as obs_count,
                       MAX(ts) as last_ts,
                       MAX(rssi) as best_rssi,
                       MAX(snr) as best_snr,
                       MIN(path_len) as min_path_len
                FROM passive_obs
                WHERE pubkey_pre IN ({placeholders})
                GROUP BY pubkey_pre''',
            pubkey_prefixes
        )
        cols = [d[0] for d in c.description]
        return {row[0]: dict(zip(cols, row)) for row in c.fetchall()}


def delete_passive_obs(radio_id, pubkey_pre=None):
    with get_mc_passive_db(radio_id) as conn:
        c = conn.cursor()
        if pubkey_pre:
            c.execute('DELETE FROM passive_obs WHERE pubkey_pre=?', (pubkey_pre,))
        else:
            c.execute('DELETE FROM passive_obs')


def count_passive_obs_by_collector(radio_id):
    """Return {collector_id: count} for all collectors."""
    with get_mc_passive_db(radio_id) as conn:
        c = conn.cursor()
        c.execute(
            'SELECT collector_id, COUNT(*) FROM passive_obs GROUP BY collector_id'
        )
        return {str(row[0] or "unknown"): row[1] for row in c.fetchall()}


def cleanup_passive_obs(radio_id, ttl_days=30):
    cutoff = int(time.time()) - ttl_days * 86400
    with get_mc_passive_db(radio_id) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM passive_obs WHERE ts < ?', (cutoff,))
        return conn.total_changes


@contextmanager
def get_mc_msgs_db(radio_id):
    db_path = _resolved_mc_msgs_db_path(radio_id)
    init_mc_msgs_db(db_path)
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
            id          TEXT,
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
            notes       TEXT DEFAULT '',
            PRIMARY KEY (id, radio_id)
        )''')
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN is_ignored INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Migrate legacy single-row-per-id schema to multi-radio rows keyed by (id, radio_id).
        try:
            info = c.execute("PRAGMA table_info(nodes)").fetchall()
            pk_cols = [row[1] for row in info if row[5] > 0]
            if pk_cols == ["id"]:
                c.execute("ALTER TABLE nodes RENAME TO nodes_legacy")
                c.execute('''CREATE TABLE nodes (
                    id          TEXT,
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
                    notes       TEXT DEFAULT '',
                    PRIMARY KEY (id, radio_id)
                )''')
                c.execute('''
                    INSERT INTO nodes (
                        id, long_name, short_name, first_seen, last_seen,
                        last_snr, last_rssi, last_battery, last_lat, last_lon,
                        hops_away, radio_id, is_local, is_favorite, is_ignored, notes
                    )
                    SELECT
                        id, long_name, short_name, first_seen, last_seen,
                        last_snr, last_rssi, last_battery, last_lat, last_lon,
                        hops_away, COALESCE(radio_id, ''), is_local, is_favorite, is_ignored, COALESCE(notes, '')
                    FROM nodes_legacy
                ''')
                c.execute("DROP TABLE nodes_legacy")
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
        c.execute('''CREATE TABLE IF NOT EXISTS mc_ignored (
            pubkey_id  TEXT PRIMARY KEY
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS mc_contact_notes (
            pubkey_id   TEXT,
            radio_id    TEXT,
            notes_json  TEXT DEFAULT '{}',
            PRIMARY KEY (pubkey_id, radio_id)
        )''')
        # Radio-agnostic notes tables — keyed by node/contact ID only
        c.execute('''CREATE TABLE IF NOT EXISTS node_notes (
            node_id    TEXT PRIMARY KEY,
            notes_json TEXT DEFAULT '{}'
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS mc_notes (
            pubkey_id  TEXT PRIMARY KEY,
            notes_json TEXT DEFAULT '{}'
        )''')
        # One-time migration: copy existing per-radio notes into radio-agnostic tables
        c.execute('''INSERT OR IGNORE INTO node_notes (node_id, notes_json)
            SELECT id, notes FROM nodes WHERE notes IS NOT NULL AND notes != '' AND notes != '{}'
        ''')
        c.execute('''INSERT OR IGNORE INTO mc_notes (pubkey_id, notes_json)
            SELECT pubkey_id, notes_json FROM mc_contact_notes
            WHERE notes_json IS NOT NULL AND notes_json != '' AND notes_json != '{}'
        ''')
        c.execute('''CREATE TABLE IF NOT EXISTS map_layers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            color      TEXT DEFAULT '#f59e0b',
            data_json  TEXT NOT NULL,
            enabled    INTEGER DEFAULT 1,
            is_geofence INTEGER DEFAULT 0,
            geofence_enter INTEGER DEFAULT 1,
            geofence_leave INTEGER DEFAULT 1,
            geofence_notify_app INTEGER DEFAULT 1,
            geofence_notify_browser INTEGER DEFAULT 1,
            geofence_networks TEXT DEFAULT 'both',
            created_ts INTEGER
        )''')
        try:
            c.execute("ALTER TABLE map_layers ADD COLUMN is_geofence INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for col, defn in [
            ("geofence_enter", "INTEGER DEFAULT 1"),
            ("geofence_leave", "INTEGER DEFAULT 1"),
            ("geofence_notify_app", "INTEGER DEFAULT 1"),
            ("geofence_notify_browser", "INTEGER DEFAULT 1"),
            ("geofence_networks", "TEXT DEFAULT 'both'"),
        ]:
            try:
                c.execute(f"ALTER TABLE map_layers ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass
        c.execute('''CREATE TABLE IF NOT EXISTS traceroute_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id          TEXT NOT NULL,
            node_name        TEXT,
            radio_id         TEXT,
            route_json       TEXT,
            route_back_json  TEXT,
            snr_towards_json TEXT,
            snr_back_json    TEXT,
            route_ids_json   TEXT,
            route_back_ids_json TEXT,
            ts               INTEGER NOT NULL
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_tr_hist_ts
                     ON traceroute_history (ts DESC)''')

        c.execute('''CREATE TABLE IF NOT EXISTS node_position_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id  TEXT NOT NULL,
            radio_id TEXT NOT NULL,
            lat      REAL NOT NULL,
            lon      REAL NOT NULL,
            ts       INTEGER NOT NULL
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_pos_hist_node
                     ON node_position_history (node_id, radio_id, ts DESC)''')

        # Migrations for existing tables
        for col, defn in [("channel_index",  "INTEGER DEFAULT 0"),
                          ("destination_id",  "TEXT DEFAULT NULL"),
                          ("destination_ids", "TEXT DEFAULT NULL"),
                          ("marker_emoji",    "TEXT DEFAULT NULL")]:
            try:
                c.execute(f"ALTER TABLE waypoints ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass

        c.execute('''CREATE TABLE IF NOT EXISTS auth_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS toc_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT 'NOTE',
            body     TEXT NOT NULL
        )''')
        try:
            c.execute("ALTER TABLE toc_log ADD COLUMN uuid TEXT")
        except Exception:
            pass
        rows = c.execute("SELECT id FROM toc_log WHERE uuid IS NULL").fetchall()
        for (rid,) in rows:
            c.execute("UPDATE toc_log SET uuid=? WHERE id=?", (str(uuid.uuid4()), rid))


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
        try:
            c.execute("ALTER TABLE messages ADD COLUMN pkt_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE messages ADD COLUMN is_emoji INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE messages ADD COLUMN reply_pkt_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for col in ("hop_start", "hop_limit", "hops"):
            try:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages (channel, is_dm, ts DESC)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_mc_msgs_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS messages (
            id              TEXT PRIMARY KEY,
            radio_id        TEXT,
            radio_name      TEXT,
            subtype         TEXT,
            channel         INTEGER DEFAULT 0,
            from_id         TEXT,
            from_name       TEXT,
            to_id           TEXT,
            to_name         TEXT,
            text            TEXT,
            ts              INTEGER,
            sent            INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'delivered',
            route_type      TEXT,
            path            TEXT,
            path_len        INTEGER,
            path_hash_mode  INTEGER,
            path_hash_size  INTEGER,
            rx_rssi         REAL,
            rx_snr          REAL,
            dedupe_key      TEXT UNIQUE
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_mc_messages_ts ON messages (ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mc_messages_channel ON messages (channel, subtype, ts DESC)")
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
    rows = []
    with get_prefs_db() as conn:
        rows = conn.cursor().execute(
            "SELECT id,name,description,lat,lon,expire,icon,from_id,radio_id,ts,"
            "channel_index,destination_id,marker_emoji,destination_ids FROM waypoints"
        ).fetchall()
    with waypoints_lock:
        for row in rows:
            try:
                dest_ids = json.loads(row[13]) if row[13] else None
            except (ValueError, TypeError):
                dest_ids = None
            waypoints_cache[row[0]] = {
                "id": row[0], "name": row[1], "description": row[2],
                "lat": row[3], "lon": row[4], "expire": row[5],
                "icon": row[6], "from_id": row[7], "radio_id": row[8], "ts": row[9],
                "channel_index": row[10] or 0, "destination_id": row[11],
                "marker_emoji": row[12] or "📍", "destination_ids": dest_ids,
            }


def load_notes():
    rows = []
    with get_prefs_db() as conn:
        rows = conn.cursor().execute(
            "SELECT id,name,description,lat,lon,marker_emoji,ts FROM self_notes"
        ).fetchall()
    with notes_lock:
        for row in rows:
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
    radio_id = n.get("radio_id") or ""
    with get_prefs_db() as conn:
        conn.execute('''
            INSERT INTO nodes
                (id, long_name, short_name, first_seen, last_seen,
                 last_snr, last_rssi, last_battery,
                 last_lat, last_lon, hops_away, radio_id, is_local)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, radio_id) DO UPDATE SET
                long_name    = excluded.long_name,
                short_name   = excluded.short_name,
                last_seen    = excluded.last_seen,
                last_snr     = excluded.last_snr,
                last_rssi    = excluded.last_rssi,
                last_battery = excluded.last_battery,
                last_lat     = COALESCE(excluded.last_lat,  last_lat),
                last_lon     = COALESCE(excluded.last_lon,  last_lon),
                hops_away    = excluded.hops_away,
                is_local     = excluded.is_local
        ''', (
            n["id"], n["long_name"], n["short_name"],
            ts, ts,
            n["snr"], n["rssi"], n["battery"],
            n["latitude"], n["longitude"],
            n["hops_away"], radio_id, 1 if n["is_local"] else 0,
        ))
    log_position(n["id"], radio_id, n.get("latitude"), n.get("longitude"), ts)


def get_db_node(node_id):
    if not node_id:
        return None
    with get_prefs_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM nodes WHERE id=? ORDER BY last_seen DESC LIMIT 1",
            (node_id,),
        ).fetchone()
    return dict(row) if row else None


_last_logged_pos = {}   # (node_id, radio_id) -> (lat, lon)
_MIN_MOVE_M      = 15   # ignore movements smaller than this


def _haversine_m(lat1, lon1, lat2, lon2):
    R   = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dp  = math.radians(lat2 - lat1)
    dl  = math.radians(lon2 - lon1)
    a   = math.sin(dp/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def log_position(node_id, radio_id, lat, lon, ts):
    """Write a GPS history point only if the node moved more than _MIN_MOVE_M."""
    if lat is None or lon is None:
        return
    key  = (node_id, radio_id)
    last = _last_logged_pos.get(key)
    if last is not None and _haversine_m(last[0], last[1], lat, lon) < _MIN_MOVE_M:
        return
    _last_logged_pos[key] = (lat, lon)
    cutoff = int(time.time()) - 365 * 86400  # keep 1 year
    with get_prefs_db() as conn:
        conn.execute(
            "INSERT INTO node_position_history (node_id, radio_id, lat, lon, ts) VALUES (?,?,?,?,?)",
            (node_id, radio_id, lat, lon, ts),
        )
        conn.execute("DELETE FROM node_position_history WHERE ts < ?", (cutoff,))


def clear_position_history(node_id=None):
    """Clear GPS history. If node_id given, clears only that node. Otherwise clears all."""
    with get_prefs_db() as conn:
        if node_id:
            conn.execute("DELETE FROM node_position_history WHERE node_id=?", (node_id,))
        else:
            conn.execute("DELETE FROM node_position_history")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='node_position_history'")
    if not node_id:
        _last_logged_pos.clear()
    else:
        for key in [k for k in _last_logged_pos.keys() if k[0] == node_id]:
            _last_logged_pos.pop(key, None)


def get_position_history(node_id, hours=24):
    """Return list of {lat, lon, ts} dicts for a node, newest first, within the last N hours."""
    since = int(time.time()) - hours * 3600
    with get_prefs_db() as conn:
        rows = conn.execute(
            "SELECT lat, lon, ts FROM node_position_history WHERE node_id=? AND ts>=? ORDER BY ts ASC",
            (node_id, since),
        ).fetchall()
    return [{"lat": r[0], "lon": r[1], "ts": r[2]} for r in rows]


def save_traceroute(node_id, node_name, radio_id, tr):
    """Persist a completed traceroute result. tr is the dict returned by the API."""
    with get_prefs_db() as conn:
        conn.execute(
            """INSERT INTO traceroute_history
               (node_id, node_name, radio_id,
                route_json, route_back_json, snr_towards_json, snr_back_json,
                route_ids_json, route_back_ids_json, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (node_id, node_name, radio_id,
             json.dumps(tr.get("route", [])),
             json.dumps(tr.get("routeBack", [])),
             json.dumps(tr.get("snrTowards", [])),
             json.dumps(tr.get("snrBack", [])),
             json.dumps(tr.get("routeIds", [])),
             json.dumps(tr.get("routeBackIds", [])),
             int(time.time())),
        )
        # Keep only last 50 entries total
        conn.execute(
            "DELETE FROM traceroute_history WHERE id NOT IN "
            "(SELECT id FROM traceroute_history ORDER BY ts DESC LIMIT 50)"
        )


def get_auth_setting(key, default=None):
    with get_prefs_db() as conn:
        row = conn.execute("SELECT value FROM auth_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_auth_setting(key, value):
    with get_prefs_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO auth_settings (key, value) VALUES (?, ?)",
            (key, str(value) if value is not None else ""),
        )


def get_traceroute_history(limit=20):
    """Return last N traceroutes as list of dicts, newest first."""
    with get_prefs_db() as conn:
        rows = conn.execute(
            "SELECT node_id, node_name, radio_id, "
            "route_json, route_back_json, snr_towards_json, snr_back_json, "
            "route_ids_json, route_back_ids_json, ts "
            "FROM traceroute_history ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{
        "node_id":   r[0], "node_name": r[1], "radio_id": r[2],
        "data": {
            "route":         json.loads(r[3]),
            "routeBack":     json.loads(r[4]),
            "snrTowards":    json.loads(r[5]),
            "snrBack":       json.loads(r[6]),
            "routeIds":      json.loads(r[7]),
            "routeBackIds":  json.loads(r[8]),
        },
        "ts": r[9],
    } for r in rows]


def get_favorites():
    with get_prefs_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, COALESCE(radio_id, '') FROM nodes WHERE is_favorite=1")
        return {(row[0], row[1]) for row in c.fetchall()}


def get_ignored():
    with get_prefs_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, COALESCE(radio_id, '') FROM nodes WHERE is_ignored=1")
        return {(row[0], row[1]) for row in c.fetchall()}


def get_mc_ignored():
    with get_prefs_db() as conn:
        c = conn.cursor()
        c.execute("SELECT pubkey_id FROM mc_ignored")
        return {row[0] for row in c.fetchall()}


def set_mc_ignored(pubkey_id, ignored):
    with get_prefs_db() as conn:
        c = conn.cursor()
        if ignored:
            c.execute("INSERT OR IGNORE INTO mc_ignored (pubkey_id) VALUES (?)", (pubkey_id,))
        else:
            c.execute("DELETE FROM mc_ignored WHERE pubkey_id=?", (pubkey_id,))
        conn.commit()


def get_mc_contact_notes(pubkey_id, radio_id):
    with get_prefs_db() as conn:
        row = conn.execute(
            "SELECT notes_json FROM mc_contact_notes WHERE pubkey_id=? AND radio_id=?",
            (pubkey_id, radio_id)
        ).fetchone()
        return row[0] if row else '{}'


def set_mc_contact_notes(pubkey_id, radio_id, notes_json):
    with get_prefs_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mc_contact_notes (pubkey_id, radio_id, notes_json) VALUES (?,?,?)",
            (pubkey_id, radio_id, notes_json)
        )
        conn.commit()


def get_node_note(node_id):
    with get_prefs_db() as conn:
        row = conn.execute(
            "SELECT notes_json FROM node_notes WHERE node_id=?", (node_id,)
        ).fetchone()
        return row[0] if row else '{}'


def set_node_note(node_id, notes_json):
    with get_prefs_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO node_notes (node_id, notes_json) VALUES (?,?)",
            (node_id, notes_json)
        )
        conn.commit()


def get_all_node_notes():
    with get_prefs_db() as conn:
        rows = conn.execute("SELECT node_id, notes_json FROM node_notes").fetchall()
        return {r[0]: r[1] for r in rows}


def get_mc_note(pubkey_id):
    with get_prefs_db() as conn:
        row = conn.execute(
            "SELECT notes_json FROM mc_notes WHERE pubkey_id=?", (pubkey_id,)
        ).fetchone()
        return row[0] if row else '{}'


def set_mc_note(pubkey_id, notes_json):
    with get_prefs_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mc_notes (pubkey_id, notes_json) VALUES (?,?)",
            (pubkey_id, notes_json)
        )
        conn.commit()


def get_all_mc_notes():
    with get_prefs_db() as conn:
        rows = conn.execute("SELECT pubkey_id, notes_json FROM mc_notes").fetchall()
        return {r[0]: r[1] for r in rows}


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
        c.execute(f"""
            SELECT n.*, COALESCE(nn.notes_json, '') AS _nn_notes
            FROM nodes n
            LEFT JOIN node_notes nn ON nn.node_id = n.id
            {where} ORDER BY {order}
        """)
        raw_rows = []
        for r in c.fetchall():
            row = dict(r)
            row['notes'] = row.pop('_nn_notes', '')
            raw_rows.append(row)

    rows = [dict(row) for row in raw_rows]
    local_by_radio = {}
    for row in rows:
        if row["is_local"] and row["last_lat"] is not None and row["last_lon"] is not None:
            rid = row.get("radio_id") or ""
            existing = local_by_radio.get(rid)
            if not existing or (row.get("last_seen") or 0) > (existing.get("last_seen") or 0):
                local_by_radio[rid] = row

    for row in rows:
        local = local_by_radio.get(row.get("radio_id") or "")
        if local and row["last_lat"] is not None and row["last_lon"] is not None and not row["is_local"]:
            row["distance"] = round(_haversine(
                local["last_lat"], local["last_lon"],
                row["last_lat"], row["last_lon"],
            ), 2)
        else:
            row["distance"] = None
        row["first_seen_str"] = _format_ts(row["first_seen"])
        row["last_seen_str"]  = _format_ts(row["last_seen"])

    reverse = direction == "DESC"
    rows.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by)), reverse=reverse)
    if fav_first:
        rows.sort(key=lambda r: r.get("is_favorite", 0), reverse=True)
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
                    (id, from_id, from_name, to_id, to_name, channel, text, ts, sent, is_dm, status, radio_id, pkt_id, is_emoji, reply_pkt_id, hop_start, hop_limit, hops)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                msg['id'], msg.get('from_id'), msg.get('from_name'),
                msg.get('to_id'), msg.get('to_name'), msg.get('channel', 0),
                msg.get('text', ''), msg.get('ts', 0),
                1 if msg.get('sent') else 0,
                1 if msg.get('is_dm') else 0,
                msg.get('status', 'pending'),
                radio_id,
                msg.get('pkt_id', 0),
                1 if msg.get('is_emoji') else 0,
                msg.get('reply_pkt_id', 0),
                msg.get('hop_start'),
                msg.get('hop_limit'),
                msg.get('hops'),
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
        c.execute("""
            SELECT * FROM (
                SELECT * FROM messages ORDER BY ts DESC LIMIT 500
            )
            ORDER BY ts ASC
        """)
        rows = [dict(r) for r in c.fetchall()]
    for row in rows:
        row["radio_id"] = radio_id
    return rows


def delete_channel_messages(radio_id, channel):
    if radio_id is None:
        return 0
    with get_msgs_db(radio_id) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE channel=? AND (is_dm IS NULL OR is_dm=0)",
            (int(channel),),
        )
        removed = int(cur.fetchone()[0] or 0)
        cur.execute(
            "DELETE FROM messages WHERE channel=? AND (is_dm IS NULL OR is_dm=0)",
            (int(channel),),
        )
    return removed


def delete_mt_all_messages(radio_id):
    if radio_id is None:
        return 0
    with get_msgs_db(radio_id) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        removed = int(cur.fetchone()[0] or 0)
        cur.execute("DELETE FROM messages")
    return removed


def delete_mc_all_messages(radio_id):
    with get_mc_msgs_db(radio_id) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        removed = int(cur.fetchone()[0] or 0)
        cur.execute("DELETE FROM messages")
    return removed


def _mc_message_dedupe_key(msg):
    explicit = msg.get("dedupe_key") or msg.get("id")
    if explicit:
        return str(explicit)
    parts = [
        msg.get("radio_id", ""),
        msg.get("subtype", ""),
        msg.get("channel", 0),
        msg.get("from_id", ""),
        msg.get("to_id", ""),
        msg.get("text", ""),
        msg.get("ts", 0),
        msg.get("path", ""),
        msg.get("path_len", ""),
    ]
    return "|".join(str(p) for p in parts)


def save_mc_message(msg):
    radio_id = msg.get("radio_id")
    if not radio_id:
        return
    msg_id = msg.get("id") or "mc-" + hashlib.sha1(_mc_message_dedupe_key(msg).encode("utf-8")).hexdigest()[:20]
    dedupe_key = _mc_message_dedupe_key({**msg, "id": msg_id})
    with get_mc_msgs_db(radio_id) as conn:
        conn.execute('''
            INSERT OR IGNORE INTO messages
                (id, radio_id, radio_name, subtype, channel, from_id, from_name, to_id, to_name,
                 text, ts, sent, status, route_type, path, path_len, path_hash_mode, path_hash_size,
                 rx_rssi, rx_snr, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            msg_id,
            radio_id,
            msg.get("radio_name"),
            msg.get("subtype", "channel"),
            msg.get("channel", 0),
            msg.get("from_id"),
            msg.get("from_name"),
            msg.get("to_id"),
            msg.get("to_name"),
            msg.get("text", ""),
            msg.get("ts", int(time.time())),
            1 if msg.get("sent") else 0,
            msg.get("status", "delivered"),
            msg.get("route_type"),
            msg.get("path"),
            msg.get("path_len"),
            msg.get("path_hash_mode"),
            msg.get("path_hash_size"),
            msg.get("rx_rssi"),
            msg.get("rx_snr"),
            dedupe_key,
        ))
        conn.execute('''
            DELETE FROM messages WHERE id NOT IN (
                SELECT id FROM messages ORDER BY ts DESC LIMIT 2000
            )
        ''')


def load_mc_messages(radio_id, limit=500):
    with get_mc_msgs_db(radio_id) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("""
            SELECT * FROM (
                SELECT * FROM messages ORDER BY ts DESC LIMIT ?
            )
            ORDER BY ts ASC
        """, (int(limit),)).fetchall()]
    for row in rows:
        row["type"] = "mc_message"
        row["network"] = "mc"
        row["sent"] = bool(row.get("sent"))
    return rows


def delete_mc_channel_messages(radio_id, channel):
    with get_mc_msgs_db(radio_id) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE channel=? AND subtype!='dm'",
            (int(channel),),
        )
        removed = int(cur.fetchone()[0] or 0)
        cur.execute(
            "DELETE FROM messages WHERE channel=? AND subtype!='dm'",
            (int(channel),),
        )
    return removed


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

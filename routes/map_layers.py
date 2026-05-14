import json
import re
import time

from flask import Blueprint, jsonify, request

from db import get_prefs_db

bp = Blueprint("map_layers", __name__)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_VALID_GEOJSON_TYPES = {
    "FeatureCollection",
    "Feature",
    "GeometryCollection",
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
}


def _normalize_color(value):
    color = (value or "").strip()
    return color if _HEX_COLOR_RE.match(color) else "#f59e0b"


def _parse_geojson(raw):
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise ValueError("GeoJSON required")
        try:
            geojson = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e.msg}") from e
    elif isinstance(raw, dict):
        geojson = raw
    else:
        raise ValueError("GeoJSON required")

    gtype = geojson.get("type")
    if gtype not in _VALID_GEOJSON_TYPES:
        raise ValueError("Unsupported GeoJSON type")
    return geojson


def _row_to_layer(row):
    try:
        data = json.loads(row[3]) if row[3] else None
    except (TypeError, ValueError):
        data = None
    return {
        "id": row[0],
        "name": row[1],
        "color": row[2] or "#f59e0b",
        "data": data,
        "enabled": bool(row[4]),
        "is_geofence": bool(row[5]),
        "geofence": {
            "enter": bool(row[6]),
            "leave": bool(row[7]),
            "notify_app": bool(row[8]),
            "notify_browser": bool(row[9]),
            "networks": row[10] or "both",
        },
        "created_ts": row[11] or 0,
    }


@bp.route("/api/map_layers")
def api_get_map_layers():
    with get_prefs_db() as conn:
        rows = conn.cursor().execute(
            "SELECT id,name,color,data_json,enabled,is_geofence,geofence_enter,geofence_leave,geofence_notify_app,geofence_notify_browser,geofence_networks,created_ts FROM map_layers ORDER BY created_ts DESC, id DESC"
        ).fetchall()
    return jsonify([_row_to_layer(row) for row in rows])


@bp.route("/api/map_layers", methods=["POST"])
def api_create_map_layer():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()[:60]
    try:
        geojson = _parse_geojson(payload.get("data"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    color = _normalize_color(payload.get("color"))
    enabled = 0 if payload.get("enabled") is False else 1
    is_geofence = 1 if payload.get("is_geofence") else 0
    geofence = payload.get("geofence") if isinstance(payload.get("geofence"), dict) else {}
    geofence_enter = 0 if geofence.get("enter") is False else 1
    geofence_leave = 0 if geofence.get("leave") is False else 1
    geofence_notify_app = 0 if geofence.get("notify_app") is False else 1
    geofence_notify_browser = 0 if geofence.get("notify_browser") is False else 1
    geofence_networks = str(geofence.get("networks") or "both").strip().lower()
    if geofence_networks not in {"both", "mt", "mc"}:
        geofence_networks = "both"
    created_ts = int(time.time())
    raw_json = json.dumps(geojson, separators=(",", ":"))
    with get_prefs_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO map_layers (name,color,data_json,enabled,is_geofence,geofence_enter,geofence_leave,geofence_notify_app,geofence_notify_browser,geofence_networks,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, color, raw_json, enabled, is_geofence, geofence_enter, geofence_leave, geofence_notify_app, geofence_notify_browser, geofence_networks, created_ts),
        )
        layer_id = cur.lastrowid
        row = cur.execute(
            "SELECT id,name,color,data_json,enabled,is_geofence,geofence_enter,geofence_leave,geofence_notify_app,geofence_notify_browser,geofence_networks,created_ts FROM map_layers WHERE id=?",
            (layer_id,),
        ).fetchone()
    return jsonify({"ok": True, "layer": _row_to_layer(row)})


@bp.route("/api/map_layers/<int:layer_id>", methods=["PATCH"])
def api_update_map_layer(layer_id):
    payload = request.get_json(silent=True) or {}
    with get_prefs_db() as conn:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT id,name,color,data_json,enabled,is_geofence,geofence_enter,geofence_leave,geofence_notify_app,geofence_notify_browser,geofence_networks,created_ts FROM map_layers WHERE id=?",
            (layer_id,),
        ).fetchone()
        if not existing:
            return jsonify({"error": "Layer not found"}), 404

        name = (payload.get("name") or existing[1] or "").strip()[:60]
        color = _normalize_color(payload.get("color") if "color" in payload else existing[2])
        enabled = existing[4]
        if "enabled" in payload:
            enabled = 1 if payload.get("enabled") else 0
        is_geofence = existing[5]
        if "is_geofence" in payload:
            is_geofence = 1 if payload.get("is_geofence") else 0
        geofence_enter = existing[6]
        geofence_leave = existing[7]
        geofence_notify_app = existing[8]
        geofence_notify_browser = existing[9]
        geofence_networks = existing[10] or "both"
        geofence = payload.get("geofence") if isinstance(payload.get("geofence"), dict) else None
        if geofence is not None:
            if "enter" in geofence:
                geofence_enter = 1 if geofence.get("enter") else 0
            if "leave" in geofence:
                geofence_leave = 1 if geofence.get("leave") else 0
            if "notify_app" in geofence:
                geofence_notify_app = 1 if geofence.get("notify_app") else 0
            if "notify_browser" in geofence:
                geofence_notify_browser = 1 if geofence.get("notify_browser") else 0
            if "networks" in geofence:
                geofence_networks = str(geofence.get("networks") or "both").strip().lower()
                if geofence_networks not in {"both", "mt", "mc"}:
                    geofence_networks = "both"
        raw_json = existing[3]
        if "data" in payload:
            try:
                raw_json = json.dumps(_parse_geojson(payload.get("data")), separators=(",", ":"))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        old_name = existing[1] or ""
        cur.execute(
            "UPDATE map_layers SET name=?, color=?, data_json=?, enabled=?, is_geofence=?, geofence_enter=?, geofence_leave=?, geofence_notify_app=?, geofence_notify_browser=?, geofence_networks=? WHERE id=?",
            (name, color, raw_json, enabled, is_geofence, geofence_enter, geofence_leave, geofence_notify_app, geofence_notify_browser, geofence_networks, layer_id),
        )
        if name != old_name:
            old_token = f"@[{old_name}](overlay:{layer_id})"
            new_token = f"@[{name}](overlay:{layer_id})"
            toc_rows = cur.execute(
                "SELECT id, body FROM toc_log WHERE body LIKE ?",
                (f"%overlay:{layer_id}%",),
            ).fetchall()
            for row_id, body in toc_rows:
                if body and old_token in body:
                    cur.execute("UPDATE toc_log SET body=? WHERE id=?", (body.replace(old_token, new_token), row_id))
        row = cur.execute(
            "SELECT id,name,color,data_json,enabled,is_geofence,geofence_enter,geofence_leave,geofence_notify_app,geofence_notify_browser,geofence_networks,created_ts FROM map_layers WHERE id=?",
            (layer_id,),
        ).fetchone()
    return jsonify({"ok": True, "layer": _row_to_layer(row)})


@bp.route("/api/map_layers/<int:layer_id>", methods=["DELETE"])
def api_delete_map_layer(layer_id):
    with get_prefs_db() as conn:
        cur = conn.cursor()
        if not cur.execute("SELECT 1 FROM map_layers WHERE id=?", (layer_id,)).fetchone():
            return jsonify({"error": "Layer not found"}), 404
        cur.execute("DELETE FROM map_layers WHERE id=?", (layer_id,))
    return jsonify({"ok": True})

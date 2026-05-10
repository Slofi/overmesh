import datetime
import json
import re
import time

from flask import Blueprint, jsonify, request

from db import get_prefs_db

bp = Blueprint('toc', __name__)

VALID_CATEGORIES = {'NOTE', 'SITREP', 'ALERT', 'ACTION', 'COMMS', 'CONTACT', 'POSITION', 'INTEL'}
_TXT_ENTRY_RE = re.compile(
    r'^\[(?P<dt>\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?Z?)\] \[(?P<cat>[A-Z]+)\]\n(?P<body>.*?)(?=\n\n\[\d{4}-\d{2}-\d{2} |\Z)',
    re.S | re.M,
)


def _normalize_category(value):
    category = (value or 'NOTE').strip().upper()
    return category if category in VALID_CATEGORIES else 'NOTE'


def _normalize_body(value):
    if isinstance(value, (dict, list)):
        value = _structured_body_to_markdown(value)
    return (value or '').strip()


def _structured_body_to_markdown(value):
    if isinstance(value, dict):
        lines = []
        for key, raw in value.items():
            val = '' if raw is None else str(raw).strip()
            if val:
                lines.append(f"**{key}:** {val}")
            else:
                lines.append(f"**{key}:**")
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if str(item).strip())
    return str(value or '').strip()


def _body_for_text_export(body):
    body = (body or '').strip()
    if not body:
        return ''
    try:
        loaded = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return body
    if isinstance(loaded, (dict, list)):
        return _structured_body_to_markdown(loaded)
    return body


def _normalize_ts(value, default=None):
    if value in (None, ''):
        return int(time.time()) if default is None else int(default)
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return int(time.time()) if default is None else int(default)
    if ts > 10_000_000_000:
        ts = int(ts / 1000)
    return max(0, ts)


def _entry_from_row(row):
    return {'id': row[0], 'ts': row[1], 'category': row[2], 'body': row[3]}


def _parse_toc_datetime(value):
    value = (value or '').strip()
    is_utc = value.endswith('Z')
    value = value.rstrip('Z')
    fmt = '%Y-%m-%d %H:%M:%S' if len(value) > 16 else '%Y-%m-%d %H:%M'
    dt = datetime.datetime.strptime(value, fmt)
    if is_utc:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    return int(dt.replace(tzinfo=None).timestamp())


def _parse_import_entries(data):
    if isinstance(data, list):
        raw_entries = data
    elif isinstance(data, dict) and isinstance(data.get('entries'), list):
        raw_entries = data.get('entries')
    else:
        content = data.get('content') if isinstance(data, dict) else None
        if not isinstance(content, str):
            raise ValueError('Upload a TOC JSON or TXT export.')
        stripped = content.strip()
        if not stripped:
            return []
        raw_entries = None
        if stripped.startswith('['):
            try:
                loaded = json.loads(stripped)
                if isinstance(loaded, list):
                    raw_entries = loaded
            except json.JSONDecodeError:
                raw_entries = None
        if raw_entries is None:
            raw_entries = []
            for match in _TXT_ENTRY_RE.finditer(stripped):
                body = match.group('body').strip()
                if body:
                    raw_entries.append({
                        'ts': _parse_toc_datetime(match.group('dt')),
                        'category': match.group('cat'),
                        'body': body,
                    })

    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        body = _normalize_body(raw.get('body'))
        if not body:
            continue
        entries.append({
            'ts': _normalize_ts(raw.get('ts')),
            'category': _normalize_category(raw.get('category')),
            'body': body,
        })
    return entries


@bp.route('/api/toc')
def api_toc_list():
    with get_prefs_db() as conn:
        rows = conn.execute(
            'SELECT id, ts, category, body FROM toc_log ORDER BY ts DESC LIMIT 500'
        ).fetchall()
    return jsonify([_entry_from_row(r) for r in rows])


@bp.route('/api/toc', methods=['POST'])
def api_toc_add():
    data = request.get_json(silent=True) or {}
    body = _normalize_body(data.get('body'))
    if not body:
        return jsonify({'error': 'Body required'}), 400
    category = _normalize_category(data.get('category'))
    ts = _normalize_ts(data.get('ts'))
    with get_prefs_db() as conn:
        cur = conn.execute(
            'INSERT INTO toc_log (ts, category, body) VALUES (?, ?, ?)',
            (ts, category, body)
        )
        entry_id = cur.lastrowid
    return jsonify({'ok': True, 'id': entry_id, 'ts': ts, 'category': category, 'body': body})


@bp.route('/api/toc/<int:entry_id>', methods=['PUT', 'PATCH'])
def api_toc_update(entry_id):
    data = request.get_json(silent=True) or {}
    body = _normalize_body(data.get('body'))
    if not body:
        return jsonify({'error': 'Body required'}), 400
    category = _normalize_category(data.get('category'))
    ts = _normalize_ts(data.get('ts'))
    with get_prefs_db() as conn:
        cur = conn.execute(
            'UPDATE toc_log SET ts=?, category=?, body=? WHERE id=?',
            (ts, category, body, entry_id),
        )
        if cur.rowcount == 0:
            return jsonify({'error': 'Entry not found'}), 404
        row = conn.execute(
            'SELECT id, ts, category, body FROM toc_log WHERE id=?',
            (entry_id,),
        ).fetchone()
    entry = _entry_from_row(row)
    entry['ok'] = True
    return jsonify(entry)


@bp.route('/api/toc/<int:entry_id>', methods=['DELETE'])
def api_toc_delete(entry_id):
    with get_prefs_db() as conn:
        conn.execute('DELETE FROM toc_log WHERE id=?', (entry_id,))
    return jsonify({'ok': True})


@bp.route('/api/toc/import', methods=['POST'])
def api_toc_import():
    data = request.get_json(silent=True) or {}
    try:
        entries = _parse_import_entries(data)
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({'error': str(e)}), 400
    if not entries:
        return jsonify({'error': 'No importable entries found'}), 400
    imported = []
    with get_prefs_db() as conn:
        for entry in entries:
            cur = conn.execute(
                'INSERT INTO toc_log (ts, category, body) VALUES (?, ?, ?)',
                (entry['ts'], entry['category'], entry['body']),
            )
            imported.append({
                'id': cur.lastrowid,
                'ts': entry['ts'],
                'category': entry['category'],
                'body': entry['body'],
            })
    return jsonify({'ok': True, 'imported': len(imported), 'entries': imported})


@bp.route('/api/toc/export')
def api_toc_export():
    fmt = request.args.get('fmt', 'text')
    with get_prefs_db() as conn:
        rows = conn.execute(
            'SELECT id, ts, category, body FROM toc_log ORDER BY ts ASC'
        ).fetchall()
    entries = [_entry_from_row(r) for r in rows]
    if fmt == 'json':
        from flask import Response
        import json
        return Response(
            json.dumps(entries, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename="toc_log.json"'}
        )
    lines = []
    for e in entries:
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['ts']))
        lines.append(f"[{dt}] [{e['category']}]\n{_body_for_text_export(e['body'])}\n")
    from flask import Response
    return Response(
        '\n'.join(lines),
        mimetype='text/plain',
        headers={'Content-Disposition': 'attachment; filename="toc_log.txt"'}
    )

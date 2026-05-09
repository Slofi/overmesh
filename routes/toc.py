import time

from flask import Blueprint, jsonify, request

from db import get_prefs_db

bp = Blueprint('toc', __name__)

VALID_CATEGORIES = {'NOTE', 'SITREP', 'ALERT', 'ACTION', 'COMMS', 'CONTACT', 'POSITION'}


@bp.route('/api/toc')
def api_toc_list():
    with get_prefs_db() as conn:
        rows = conn.execute(
            'SELECT id, ts, category, body FROM toc_log ORDER BY ts DESC LIMIT 500'
        ).fetchall()
    return jsonify([{'id': r[0], 'ts': r[1], 'category': r[2], 'body': r[3]} for r in rows])


@bp.route('/api/toc', methods=['POST'])
def api_toc_add():
    data = request.get_json(silent=True) or {}
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'Body required'}), 400
    category = (data.get('category') or 'NOTE').strip().upper()
    if category not in VALID_CATEGORIES:
        category = 'NOTE'
    ts = int(time.time())
    with get_prefs_db() as conn:
        cur = conn.execute(
            'INSERT INTO toc_log (ts, category, body) VALUES (?, ?, ?)',
            (ts, category, body)
        )
        entry_id = cur.lastrowid
    return jsonify({'ok': True, 'id': entry_id, 'ts': ts, 'category': category, 'body': body})


@bp.route('/api/toc/<int:entry_id>', methods=['DELETE'])
def api_toc_delete(entry_id):
    with get_prefs_db() as conn:
        conn.execute('DELETE FROM toc_log WHERE id=?', (entry_id,))
    return jsonify({'ok': True})


@bp.route('/api/toc/export')
def api_toc_export():
    fmt = request.args.get('fmt', 'text')
    with get_prefs_db() as conn:
        rows = conn.execute(
            'SELECT id, ts, category, body FROM toc_log ORDER BY ts ASC'
        ).fetchall()
    entries = [{'id': r[0], 'ts': r[1], 'category': r[2], 'body': r[3]} for r in rows]
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
        dt = time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(e['ts']))
        lines.append(f"[{dt}] [{e['category']}]\n{e['body']}\n")
    from flask import Response
    return Response(
        '\n'.join(lines),
        mimetype='text/plain',
        headers={'Content-Disposition': 'attachment; filename="toc_log.txt"'}
    )

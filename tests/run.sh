#!/usr/bin/env bash
set -euo pipefail

# Run against THIS checkout, wherever it lives: derive the repo root from the
# script's own location instead of a hard-coded path (that made the runner test
# the live install no matter where it was launched from, and broke on hosts
# whose checkout is elsewhere, e.g. gandalf's /home/gandalf/overmesh).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer the repo's own venv (meshtastic and the app deps live there); fall back
# to system python3 so the runner still works without a venv.
if [ -x ./venv/bin/python ]; then
  PY=./venv/bin/python
else
  PY=python3
fi
"$PY" -m unittest discover -s tests -p 'test_*.py'
node tests/test_mc_path_template.js
node tests/test_mc_cleanup_favourites.js
node tests/test_update_notice.js
node tests/test_waypoint_dialog.js
node tests/test_message_notif_layout.js
node tests/test_mc_scan_results_ui.js
node tests/test_map_keys_ui.js

# JS lint (no-undef) — the only check that sees client-side ReferenceErrors,
# which never reach the server journal. It MUST run with the repo as the base
# path: run from anywhere else and the browser files resolve outside the
# config's tree, eslint answers "File ignored", and a clean result checks
# nothing (that is how a three-month ReferenceError sat in lite.js unnoticed).
# The binary is a dev dependency — restore it with `npm ci` in the repo root.
if [ ! -x ./node_modules/.bin/eslint ]; then
  echo "lint: ./node_modules/.bin/eslint not found — run 'npm ci' in the repo root" >&2
  exit 1
fi
./node_modules/.bin/eslint --config eslint.config.mjs static/js/app.js static/js/lite.js
echo "ok: eslint no-undef clean (static/js/app.js + static/js/lite.js)"

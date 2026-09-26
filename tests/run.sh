#!/usr/bin/env bash
set -euo pipefail
cd /home/slofi/overmesh
python3 -m unittest discover -s tests -p 'test_*.py'
node tests/test_mc_path_template.js
node tests/test_mc_cleanup_favourites.js
node tests/test_update_notice.js
node tests/test_message_notif_layout.js

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

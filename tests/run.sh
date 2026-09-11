#!/usr/bin/env bash
set -euo pipefail
cd /home/slofi/overmesh
python3 -m unittest discover -s tests -p 'test_*.py'
node tests/test_mc_path_template.js
node tests/test_mc_cleanup_favourites.js
node tests/test_update_notice.js
node tests/test_message_notif_layout.js

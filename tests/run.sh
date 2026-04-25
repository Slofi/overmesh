#!/usr/bin/env bash
set -euo pipefail
cd /home/slofi/overmesh
python3 -m unittest discover -s tests -p 'test_*.py'
node tests/test_mc_path_template.js

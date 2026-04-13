#!/bin/bash
# Run OverMesh directly in the background (no service needed).
# Logs go to /tmp/overmesh.log. Use this for testing or non-systemd setups.
# For service-based launch (after running install.sh), use launch-overmesh.sh instead.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" && nohup python3 app.py > /tmp/overmesh.log 2>&1 &
sleep 2
xdg-open http://localhost:8082

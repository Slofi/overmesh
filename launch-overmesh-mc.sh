#!/bin/bash
# Linux helper: run a local background instance from this repo, then open the UI.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" && nohup python3 app.py > /tmp/overmesh-mc.log 2>&1 &
sleep 2
xdg-open http://localhost:8082

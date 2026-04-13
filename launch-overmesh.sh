#!/bin/bash
# Linux helper: start the OverMesh user service, then open the UI in the browser.
# Requires: install.sh to have been run first (sets up the systemd user service).
# For running without a service, use launch-direct.sh instead.
systemctl --user start overmesh
sleep 1
xdg-open http://localhost:8082

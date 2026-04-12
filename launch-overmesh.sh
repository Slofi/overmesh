#!/bin/bash
# Linux helper: start the user service, then open the local UI.
systemctl --user start overmesh
sleep 1
xdg-open http://localhost:8082

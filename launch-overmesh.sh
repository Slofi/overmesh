#!/bin/bash
systemctl --user start overmesh
sleep 1
xdg-open http://localhost:8081

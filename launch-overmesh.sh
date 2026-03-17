#!/bin/bash
systemctl --user start overmesh
sleep 1
vivaldi http://localhost:8081

#!/bin/bash
cd /home/slofi/overmesh-mc && nohup python3 app.py > /tmp/overmesh-mc.log 2>&1 &
sleep 2
xdg-open http://localhost:8082

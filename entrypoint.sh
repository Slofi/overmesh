#!/bin/sh
if [ ! -f /app/data/config.json ]; then
    cp /app/config.example.json /app/data/config.json
fi
exec "$@"

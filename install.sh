#!/bin/bash
# OverMesh install script
# Run as your normal user (not root)

set -e

INSTALL_DIR="$HOME/overmesh"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "=== OverMesh Installer ==="
echo "Installing to: $INSTALL_DIR"

# 1. Install Python dependencies
echo ""
echo "[1/4] Installing Python dependencies..."
pip3 install --user --break-system-packages -r "$INSTALL_DIR/requirements.txt"

# 2. Set up config if not present
echo ""
echo "[2/4] Checking config..."
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
    echo "  Created config.json from template — edit it before starting!"
else
    echo "  config.json already exists, skipping."
fi

# 3. Install systemd user service
echo ""
echo "[3/4] Installing systemd service..."
mkdir -p "$SERVICE_DIR"
sed "s/USER/$USER/g" "$INSTALL_DIR/overmesh.service" > "$SERVICE_DIR/overmesh.service"
systemctl --user daemon-reload
systemctl --user enable overmesh

# 4. Done
echo ""
echo "[4/4] Done!"
echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/config.json — set your node port and name"
echo "  2. Plug in your Meshtastic node"
echo "  3. Start: systemctl --user start overmesh"
echo "  4. Open:  http://localhost:8081"

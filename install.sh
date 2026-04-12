#!/bin/bash
# OverMesh Linux convenience install script
# Run as your normal user (not root)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$SCRIPT_DIR"
SERVICE_DIR="$HOME/.config/systemd/user"
PYTHON_BIN="$(command -v python3)"

echo "=== OverMesh Linux Installer ==="
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

# 3. Install systemd user service (Linux only)
echo ""
echo "[3/4] Installing systemd service..."
mkdir -p "$SERVICE_DIR"
sed \
    -e "s#APP_DIR#$INSTALL_DIR#g" \
    -e "s#PYTHON_BIN#$PYTHON_BIN#g" \
    "$INSTALL_DIR/overmesh.service" > "$SERVICE_DIR/overmesh.service"
systemctl --user daemon-reload
systemctl --user enable overmesh

# 4. Done
echo ""
echo "[4/4] Done!"
echo ""
echo "This installer is for Linux user-service setups."
echo "For Windows or macOS, use the native manual install steps in README.md."
echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/config.json if you want a starting template"
echo "  2. Open OverMesh and add your MT or MC radios from Settings"
echo "  3. Start: systemctl --user start overmesh"
echo "  4. Open:  http://localhost:8082"

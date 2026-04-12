# OverMesh

OverMesh is a self-hosted dashboard for Meshtastic and MeshCore. It runs on your own machine, uses your own radios, and keeps everything local, no cloud, no account, no subscription.

This is now the main active OverMesh app. Meshtastic and MeshCore are treated as two equal parts of one tool, not as separate side projects.

> **Active development** This app is actively used and still changing. Expect a few rough edges, especially in newer cross-system features.

---

## What OverMesh does

OverMesh gives you one place to work with both networks. You can watch nodes on the map, use chat on either system, run Sense, manage radios and channels, use the bot, and bridge traffic between MT and MC when needed.

The app is built around real use, not just setup screens. The idea is simple: one interface where you can actually watch the mesh, talk on it, and manage it.

---

## Screenshots

![Meshtastic Sense and map view](screenshots/mt-sense-map.png)
*Meshtastic Sense with live node response log and map view*

![MeshCore Sense and map view](screenshots/mc-sense-map.png)
*MeshCore Sense with heard-recently contact list, map markers, and activity log*

![Nodes view](screenshots/nodes-live.png)
*Live Nodes view with MT and MC available in one interface*

![MeshCore settings](screenshots/settings-meshcore.png)
*MeshCore radio settings and device controls*

![Cross-system settings](screenshots/settings-cross-system.png)
*Cross-system bridge and mirror rules between MT and MC*

---

## Main features

OverMesh covers the main things you actually need in daily use: maps, chat, direct messages, multi-radio support, Sense, bot tools, settings, notifications, offline map tiles, and MT↔MC forwarding when you want to bridge traffic between the two systems.

### Meshtastic

- live node map with labels, age coloring, and quick actions
- channel chat, direct messages, unread indicators, and per-radio history
- traceroute, node info, position request, and direct-message actions
- radio settings for identity, LoRa, channels, position, power, display, telemetry, MQTT, Bluetooth, and WiFi
- per-channel history cleanup from Settings

### MeshCore

- multi-radio MeshCore support
- MC contacts on the Nodes tab and on the map
- MC chat with channel tabs, DM tabs, delivery state, unread indicators, and local saved history
- MC radio settings, channel management, device info, coordinates, TX power, and reboot tools
- MC bot support, MC Sense activity integration, and per-channel history cleanup from Settings

### Shared / app-wide

- MT and MC visible together in one app
- map and Nodes views designed to stay as similar as possible across both systems
- Mesh Sense
- offline maps
- in-app notifications for messages, new nodes, and nodes seen again after a long gap
- browser notifications
- accent color and zoom settings

### Cross-system

- manual `Mirror` rules for one-way MT↔MC channel forwarding
- manual `Bridge` rules for two-way MT↔MC channel forwarding
- sender filters for cross-system rules
- direct chat commands:
  - `/mt #<channel> message`
  - `/mc #<channel> message`
  - `/mt @<target> message`
  - `/mc @<target> message`
- all forwarded messages are clearly tagged with source system, original sender, and source channel

---

## Requirements

- Python 3.9+
- Linux is the primary tested platform and the strongest target environment
- at least one Meshtastic or MeshCore radio

OverMesh is built first around practical Linux use, including Filip's cyberdeck workflow, but the app should stay reasonably usable on Windows and macOS too where the underlying radio tooling allows it.

## Platform support

- Linux: best-supported path, primary development target, and the most natural fit for attached radios and long-running service use
- Windows: usable for direct app runs, but less tested than Linux
- macOS: possible for direct app runs, but less tested than Linux

For now, native installs are the priority. Docker and similar deployment options can come later if they are useful, but they are not the main install path.

---

## Install

### Linux

```bash
git clone https://github.com/Slofi/overmesh.git
cd overmesh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

> A virtual environment is recommended, modern Linux distros often block system-wide `pip install`, and there is no reason to run this with `sudo`.

Run it with:

```bash
python3 app.py
```

Then open:

- `http://localhost:8082`

You can add radios from the UI after startup in `Settings`.

### Windows

1. Install Python 3.9 or newer
2. Clone or download this repo
3. Open the repo folder in Command Prompt or PowerShell
4. Create a virtual environment:

```powershell
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
py app.py
```

You can also use `start-overmesh.bat` if you want a simple local launch.

### macOS

1. Install Python 3.9 or newer
2. Clone or download this repo
3. Open Terminal in the repo folder
4. Create a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 app.py
```

### First run notes

- The starter config is just a basic template
- You can add radios from the UI after startup in `Settings`
- Serial device names vary by platform, so do not assume Linux-style `/dev/...` paths on Windows or macOS
- If you only use one system, the app still works fine

---

## Quick start

1. Start the app
2. Open `Settings`
3. Add an MT radio, an MC radio, or both
4. Go to `Chat`, `Nodes`, or `Map`
5. Use the radio pills and MT/MC toggles to switch views

If you only use one system, that is fine. If you use both, OverMesh keeps them together in one interface.

---

## Configuration

`config.json` stores app and radio settings. In normal use, most radio setup can be done from the UI, so you usually only touch the file for first setup or custom deployments.

Key values include:

- `nodes` for Meshtastic radios
- `mc_nodes` for MeshCore radios
- `port`, default `8082`
- `host`
- `app.zoom`
- `app.accent_color`
- `sense_passive`
- `gps`
- `cross`

Environment variables:

- `OVERMESH_CONFIG`
- `OVERMESH_DATA_DIR`
- `OVERMESH_HOST`
- `OVERMESH_PORT`

---

## Running as a service

If you want it as a user service on Linux:

```bash
cp overmesh.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now overmesh.service
```

Logs:

```bash
journalctl --user -u overmesh -f
```

This is mainly a Linux convenience option, not the default install method for Windows or macOS.

---

## Known limits

- cross-system forwarding is channel-to-channel only right now
- DM bridging is not implemented yet
- some MC metadata, such as per-message SNR, is best-effort rather than guaranteed
- the app is still evolving, so some UI details will keep changing

---

## Update log

Recent work has moved OverMesh much closer to feeling like one real dual-network app instead of two uneven halves.

The big changes were better MC support across the app, cleaner MT and MC UI parity, better heard-recently logic, in-app notifications, per-channel history deletion on both systems, and much stronger cross-system tools.

OverMesh is now clearly the MT plus MC app.

If you want the short public summary and the current release-prep status, see `RELEASE_NOTES.md` and `RELEASE_CHECKLIST.md` in this repo.

If you are coming from the older OverMesh setup, see `MIGRATION.md`.

---

## Theme customization

OverMesh has an app-wide accent color setting, so you can change the look without changing the layout.

![OverMesh color variants](screenshots/om-colours.gif)
*Example of the accent color applied across the app*

For the background story behind the project, see `ABOUT.md`.

---

## Project structure

| File | Purpose |
|------|---------|
| `app.py` | Flask app startup, routes, shutdown handling |
| `mesh.py` | Meshtastic connect, reconnect, packet handling |
| `mesh_mc.py` | MeshCore async bridge and event handling |
| `bot.py` | Bot logic |
| `cross.py` | Cross-system forwarding logic |
| `db.py` | SQLite storage |
| `helpers.py` | SSE push and helper functions |
| `routes/` | Flask blueprints by feature area |
| `templates/index.html` | Main frontend UI |

---

## License

MIT

---

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/slofi)

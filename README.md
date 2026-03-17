# OverMesh

A self-hosted Meshtastic dashboard. Runs on your machine, no cloud, no account, no subscription.

> **Active development** — OverMesh is being actively developed and tested. Expect rough edges, and feel free to open an issue if you find one.

---

![Mesh Sense and map view](screenshots/sense-map.png)
*Mesh Sense overlay — passive listening, live response log, node map*

![Settings — radios and node config](screenshots/settings-radios.png)
*Multi-radio management and node configuration*

![Settings — app and offline maps](screenshots/settings-app.png)
*App settings, accent theming, and offline map tile caching*

---

## Features

- **Live node map**: GPS markers, cluster grouping for co-located nodes, map position memory
- **Offline maps**: tile caching in browser IndexedDB, downloadable regions, custom zoom levels
- **Chat**: channel tabs, direct messages, unread indicators per radio
- **Mesh Sense**: passive listening and active scan overlay; detects nodes without querying them
- **Marks**: create, send, and delete waypoints over the mesh (syncs to Android MT app)
- **Bot**: automated replies (ping, sitrep, relay, joke...), scheduled MOTD broadcast
- **Traceroute**: hop-by-hop route visualization with SNR per link
- **Multi-radio**: connect several nodes simultaneously, switch active radio from the header
- **Node settings**: configure identity, LoRa, channels, fixed position directly from the UI
- **Browser notifications**: new messages and node online events
- **Theming**: accent color picker, zoom scaling, dark UI throughout

---

## Requirements

- Python 3.9+
- A Meshtastic node connected via USB serial
- **Platform:** Linux ✓ — Windows support coming soon

---

## Install

```bash
git clone https://github.com/Slofi/overmesh.git
cd overmesh
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` and set your node's serial port (`/dev/ttyUSB0`, `/dev/ttyACM0`, or `COM3` on Windows) and a name for it.

```bash
python3 app.py
```

Open [http://localhost:8081](http://localhost:8081).

---

## Configuration

`config.json` reference:

| Key | Description | Default |
|-----|-------------|---------|
| `nodes[].port` | Serial port of the node | `/dev/ttyUSB0` |
| `nodes[].name` | Display name | `MyNode` |
| `nodes[].enabled` | Include this node on startup | `true` |
| `port` | Web server port | `8081` |
| `app.zoom` | UI zoom level (75–125) | `100` |
| `app.accent_color` | Hex accent color | `#4ade80` |
| `sense_passive` | Start passive Sense listening on launch | `false` |

Nodes can also be added and configured through **Settings → Radios** in the UI — no need to edit the file manually after the first run.

---

## Running as a service (Linux)

A `overmesh.service` systemd unit file is included. Copy it and enable it:

```bash
cp overmesh.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now overmesh.service
```

Logs: `journalctl --user -u overmesh -f`

---

## Notes

- Map tiles: [OpenStreetMap](https://www.openstreetmap.org/) — © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright))
- Offline tile caching is browser-side (IndexedDB); tiles stay on your machine

---

## About

See [ABOUT.md](ABOUT.md) for the background story.

---

## License

MIT

---

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/slofi)

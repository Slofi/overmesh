# OverMesh Versions

OverMesh uses date-based release versions:

`YYYY.MM.DD.N`

- `YYYY.MM.DD` is the release date in ISO-style order.
- `N` starts at `1` for the first GitHub-pushed release on that date.
- Increment `N` for additional GitHub pushes on the same date.

The version in `VERSION` must be updated every time an OverMesh update is pushed to GitHub. The Settings updater also shows the Git commit hash for exact build identification.

## 2026.04.20.2

- Hardened MT Last Seen handling:
  - connected local MT radios report current status instead of stale nodeDB age
  - real packet/TR receive updates are preserved across manual refresh for a freshness window
  - MT packet receive handling updates node memory by `user.id` fallback when Meshtastic's internal node key differs from packet `fromId`
  - nodeDB timestamps from future-skewed radio clocks are formatted relative to the radio snapshot instead of the host clock
- Added MT node IDs to the Nodes tab metadata and included MT node IDs in Nodes search.
- Reviewed and preserved the MT node-info pubsub fix.
- Improved Settings -> App updater feedback for Check/Update, including visible checking state, dirty-file listing, and clear disabled-update reasons.

## 2026.04.20.1

- Added Settings -> App updater controls and local-only updater backend.
- Added first-launch intro modal and Settings control to show it again.
- Added a searchable in-app manual covering the main tabs, radio setup, paths, updates, overlays, geofences, troubleshooting, and button/action meanings.
- Based on the MC path/hash hardening work pushed earlier on 2026-04-20.

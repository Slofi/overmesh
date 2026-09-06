# OverMesh Versions

OverMesh uses date-based release versions:

`YYYY.MM.DD.N`

- `YYYY.MM.DD` is the release date in ISO-style order.
- `N` starts at `1` for the first GitHub-pushed release on that date.
- Increment `N` for additional GitHub pushes on the same date.

The version in `VERSION` must be updated every time an OverMesh update is pushed to GitHub. The Settings updater also shows the Git commit hash for exact build identification.

## 2026.09.06.3

- Repurposed the MC Map/Sense **Trace** button. Its old behaviour broadcast a mesh-wide TRACE probe and waited for responses that never arrived; the button is now a **toggle for the overheard-packet view**, which is entirely passive and transmits nothing.
  - draws the path of every MeshCore packet this radio heard, hop by hop, ending at our own radio
  - coloured per payload type, dashed for flood routes and solid for direct, with a tooltip giving type, route, hop count, resolved hop names, RSSI/SNR and time
  - a clickable legend shows per-type counts and switches each type on or off
  - refreshes every 5 s, redrawing only when the data actually changed; hops that cannot be resolved unambiguously are skipped rather than guessed
  - hop resolution runs client-side through the existing geographic path scoring, which resolves far more hops than a per-row coordinate can: measured 908 of 915 hops (99.2%) against real captured traffic
- Removed the broadcast-trace machinery behind the old button (`doMcTrace`, its cooldown, the pending-tag correlation and the mesh-wide warning). The per-contact **Trace Probe** and all TRACE_DATA handling are unchanged — they share the same transport, which is why it was kept.

## 2026.09.06.2

- Fixed the per-contact passive-intel badge, which had never displayed anything on any host: `load_passive_obs_summary` matched the caller's 12-char contact prefixes exactly, but observations are stored under 2-4 char path hop hashes (trace, rx) or 64-char full pubkeys (rc_adv). Matching is now by prefix relationship in either direction, crediting a stored key only when it relates to exactly one requested contact so an ambiguous 1-byte hop hash is never attributed to the wrong node. The packet timeline is excluded by default so it cannot swamp the per-contact signal history.

## 2026.09.06.1

- MeshCore overheard traffic is now actually captured. `on_rx_log_data` gated storage on a sender pubkey that the RX log never carries (an overheard packet's sender sits inside the encrypted payload; only the header and path hashes are in the clear), so every overheard packet was parsed and then discarded. Confirmed on live data: zero `rx` rows across every passive DB on two hosts.
  - observations are filed under the **last path hop** — the node we actually heard the packet from, which is what the rssi/snr measured — falling back to a sentinel for packets that arrive with an empty path
  - writes are **buffered and flushed by a background thread** (`save_passive_obs_bulk`): event handlers run inline on the asyncio loop, and a stalled loop can overflow the serial buffer and drop packets
  - contact-archive lookups moved off the loop thread too, and are built **once per flush batch** instead of once per row
  - timeline retention (24 h / 20 000 rows, scoped to the `rx` type) replaces the per-contact cap, which would let a chatty repeater evict its own recent history
- `passive_obs` API gained `obs_types`, `payload_types` and `route_types` filters. **`rx` is excluded unless requested by name**, so the Passive Intel list and the signal heatmap keep exactly the result set they had before.
- New `GET /api/mc/packet_types` serves the MeshCore packet-type vocabulary from the installed meshcore library. The library and the protocol docs disagree (`TEXT_MSG` vs `TXT_MSG`, `TC_FLOOD` vs `TRANSPORT_FLOOD`) and it is the library's strings that are stored, so a hand-copied list would filter nothing.
- Fixes found while sweeping: hop-prefix coordinate resolution now requires a **unique** match (1-byte hashes have 256 values, so first-match-wins was attaching a different node's coordinates to plotted observations); passive-DB DDL is no longer re-run on every access; a failed flush is retried up to three times instead of discarding the batch; buffer caps are per radio; a stale device `recv_time` no longer makes a row vanish on insert.

## 2026.05.25.1

- Added local Map App marking exchange endpoints for the CD workflow:
  - `GET /api/map_exchange/export` exports OM Marks, Self Notes, and Overlays as GeoJSON features
  - `POST /api/map_exchange/import` imports Map App markers as OM Self Notes and Map App drawings as OM Overlays
  - this exchange is local-only and does not broadcast anything over MT/MC radios

## 2026.05.24.2

- Made shared local tile DB support opt-in for production safety:
  - Settings -> App -> Offline Maps now has a `Use shared local tile DB` toggle, default off
  - when enabled, OM reads MBTiles layers from a local tile server, defaulting to the same host as OM on port `8092`
  - when disabled, OM only uses its built-in online layers and browser IndexedDB cache
  - if a shared layer was selected and the setting is disabled, OM falls back to OpenStreetMap

## 2026.05.24.1

- Added initial CD-local Map App tile sharing, superseded by the opt-in shared tile server setting in `2026.05.24.2`:
  - OM initially fetched the Map App catalog on the same host at port `8090` and added MBTiles downloads as selectable base layers
  - local layers appeared in the map layer menu as `Map App: ...`
  - OM's region downloader and PiP map use the same combined layer catalog
  - this was replaced by the production-safe shared tile server toggle

## 2026.04.27.1

- Added browser-tab unread message badge handling:
  - the OM tab title now shows unread message count for live visible MT/MC chat unread state
  - the tab badge clears when the matching in-app unread indicator clears
- Added app-local notification sounds for both MT and MC:
  - new messages
  - radios connecting to OM
  - new nodes/contacts discovered
- Added Settings -> App notification sound toggles for those sound types.
- Tuned browser audio unlock/recovery handling so notification sounds recover more reliably after reloads and tab/browser suspension.
- Adjusted the message notification tone to cut through more clearly.

## 2026.04.23.1

- Updated the Meshtastic Device Role setting in Settings -> Meshtastic -> Node:
  - OM now reads the role enum directly from the installed Meshtastic library instead of using a stale hard-coded list
  - fixed incorrect role numbering from `REPEATER` onward
  - added the current Meshtastic roles such as `Router Late` and `Client Base`
  - improved the dropdown labels to readable names like `Client Mute`, `Client Hidden`, and `TAK Tracker`

## 2026.04.22.1

- Added MeshCore path-hash mode controls:
  - Settings -> MeshCore now has a per-radio default path hash mode selector for `1B/hop`, `2B/hop`, or `3B/hop`
  - MC Route editor now lets stored contact routes be saved with a selected path hash size
  - OM applies the configured MC path-hash mode on connect when firmware supports it
  - fallback handling tries the selected mode first, then lower supported modes as needed
- Updated the in-app manual for MC path-hash mode and route-editor behavior.

## 2026.04.21.4

- Removed `RELEASE_CHECKLIST.md` from the public repository because it contained local/live release notes that are not useful to users and expose unnecessary environment details.

## 2026.04.21.3

- Bug-sweep cleanup after MT route UI changes:
  - removed stale frontend references to the deleted MT map-layer SNR/TR-history controls
  - removed unused MT trace replay state and no-op update calls
  - fixed manual search highlighting for multi-word searches
  - delayed MC Chat route-badge pinning slightly after opening Map/Sense to avoid map initialization races

## 2026.04.21.2

- Added MC Chat route/hop badges for received MC messages.
- Clicking an MC Chat route badge opens Sense/Map, switches to MC, and pins the matching message path.
- Renamed visible MC message route label from `flood` to `flood mode`.
- Expanded manual explanations for MC route-source badges, flood mode, and 1B/2B/3B per-hop hash metadata.
- Improved manual search with relevance ranking, partial matches, result counts, and highlighted matches.

## 2026.04.21.1

- Fixed bot reply handling for MT DMs:
  - MT bot sends now use `wantAck=True`, matching normal Chat sends
  - MT bot DM replies use the triggering packet's channel index instead of forcing channel `0`
  - MT bot command replies are shown in Chat only after the radio send call succeeds
- Hardened MC bot reply display similarly so MC bot replies are surfaced only after the MC send helper succeeds.

## 2026.04.20.8

- Removed the remaining MT traceroute history list from the map layer menu.
- Removed the old layer-menu trace replay renderer/handler and related styles.
- Kept traceroute history internally for MT Response log cached-route matching.

## 2026.04.20.7

- Removed the old `SNR Paths` toggle from the map layer menu because MT route visibility is now controlled from the Response log.
- Updated the manual so MT Map route visibility points users to Response log hover/click behavior.
- Tidied MT route badges so hop labels stay on one line.
- Improved cached MT Sense route labels for asymmetric traceroutes, such as direct toward the node but multiple hops back.

## 2026.04.20.6

- Refined MT Sense Response log route selection:
  - cached traceroute lookup remains node-based because MT packets do not identify exact per-message repeater chains
  - selected highlighting is now per log entry, so clicking one row no longer highlights every entry from the same node
  - hop-count-only badges are informational and no longer behave like route buttons when no cached traceroute exists
  - updated the manual wording for MT cached-route rows

## 2026.04.20.5

- Allowed the Settings updater from trusted local/private access paths, including Tailscale `100.64.0.0/10`, so a headless G501 install can be updated from the operator browser.
- Updated the manual wording for the updater access restriction.

## 2026.04.20.4

- Added MT message hop metadata persistence and route badges in Chat/Sense.
- Added MT Sense message entries, cached-route row hover previews, and click-to-pin/clear route behavior.
- Added MC message/ping/trace path hardening for short/ambiguous MeshCore hop hashes, including uncertainty labels.
- Added MC message path order scoring so live paths choose the more plausible hop direction.
- Added SNR-colored MC path segments and matching hover SNR labels.
- Improved MT/MC map route arrows, including stable zoom sizing and lower MT arrow density.
- Updated Sense/Map route selection styling with padded left-side indicators and removed the old MC selected-dot marker.
- Expanded the in-app manual with MT/MC-specific button, badge, route-source, and Sense/Map explanations.

## 2026.04.20.3

- Added an update reminder to the first-launch OverMesh intro, pointing users to Settings -> App for update checks.

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

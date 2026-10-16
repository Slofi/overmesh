# OverMesh Release Notes

## 2026.10.16.6

### Added

- Scan and Probe now show what they found. When the 60 second window ends, a window opens listing every repeater and room server that answered the probe, then anything else heard during the scan. Each entry shows the signal we heard it on, how well it heard us, the hop count, and marks contacts that were seen for the first time in that scan.
- A Results button next to Scan and Probe reopens the last set of results, so the list can be read after the moment has passed.

### Changed

- The scan status line now reports the outcome, for example "Done: 3 results, 1 answered, 2 new", instead of only how many contacts were new.

## 2026.10.16.5

### Changed

- The reply strip at the bottom of Remote Manage now has an outline, a lighter background and a shadow, so it stands out from the panel behind it instead of blending into it.

## 2026.10.16.4

### Fixed

- The region list no longer shows a junk first entry. Every reply from the repeater starts with four header bytes, and those were being read as if they were a region name, which on this repeater appeared as ";j*". They are skipped now, and only real region names are kept.
- When the repeater does not answer a region request, the reply says why and what to do. It ignores region requests that do not arrive over a direct path, so trying once more usually works.

## 2026.10.16.3

### Changed

- Remote Manage now keeps the RPTR's answers in one place: a strip pinned to the bottom of the window. It shows the last four exchanges with the command and the time, the newest highlighted, and holds them until you press Clear. Field refreshes stay out of it, because those already fill a field of their own.
- The Regions section no longer prints a transcript of the commands it sent at the bottom. That moved into the reply strip, so the section reads shorter.

### Fixed

- An empty region read now explains itself. If the repeater does not answer the fast region request, the strip says whether it stayed silent, answered something unreadable, or could not be asked at all, and the read also asks the repeater's own region command and shows its answer word for word.

## 2026.10.16.2

### Changed

- Remote Manage, Regions, is rearranged so it reads in the order you ask things. The region list, home region and default scope come first, the Slovenia preset sits under them with a line saying exactly what it does, unscoped flood traffic gets its own allow and deny pair, and editing a single region moved into an advanced group you open only when you need it.
- The buttons say what they do: Put is now Add or update, Save is Save to RPTR, Home and Default are Set as home and Set as default, and the bare refresh arrow is Read regions.
- Reading the home region and the default scope shows the answer (Home: si, Default scope: si) next to the region list, instead of leaving it in the reply box.

### Fixed

- Deny flood and Remove region can now be used from Remote Manage. Both were accepted by the server but had no button anywhere, so they could not be reached at all. Remove asks for confirmation first.
- Remote Manage no longer writes "Can't determine destination type ... Assuming it's a repeater" on every command. The destination is described fully now, so a room server is addressed as a room server rather than as a repeater.

## 2026.10.16.1

### Changed

- OverMesh no longer advertises on its own. Adverts are now manual only: the Advert button, Scan, and a managed repeater's own advert are the only ones that go out. If a ping to a node that has never heard us comes back empty, press Advert once and ping again. Pings are also 1 to 2 seconds faster, because the adverts they used to send first are gone.

### Fixed

- Remote Manage works again. Every command it sent was failing before it left the app, so Set buttons, the region presets and the Home/Default reads all did nothing. Name, position, radio settings, adverts and region commands now reach the repeater.
- Regions can be read again. The Regions part of Remote Manage has a read button that lists the region names the repeater itself has, and it needs no admin password. Region changes (put, allow flood, deny flood, home, default, save) now actually reach the repeater.
- Silent Running really stops transmissions now. It is enforced where the bytes leave, not only when you press the button, so a Scan or a Ping that was already under way can no longer put a packet on the air after you switch it on.
- Silent Running also covers a radio that was away. Turning it on silences a radio that is unplugged or rebooting as soon as it comes back, and turning it off hands such a radio its own setting back instead of switching transmitting on for a radio Silent Running never touched.

## 2026.10.11.2

### Fixed

- Messages heard directly from a nearby radio no longer show a "path likely" badge when OverMesh can confirm the packet.

## 2026.10.11.1

This is a housekeeping release. There is nothing new to learn, but a lot of small things that were wrong, unclear or quietly broken are now fixed.

### Fixed

- On-screen text no longer contains em dashes. The long dashes used in labels, tooltips, pop-ups and error messages are gone, so the interface reads cleanly and the same way everywhere.
- Settings toggles now treat the text "false" as off. A caller sending a boolean as text (curl, Home Assistant, a script) used to switch the flag on no matter what it said.
- Silent Running now also blocks GPS pushes and automatic GPS pushes, matching every other transmit path.
- The Automation Bridge works while a login is enabled. It was answering 401 before its own token was ever checked, which broke the documented Home Assistant setup.
- The Sense button on cooldown now shows the remaining time instead of doing nothing visible.
- Ignore and Mute now report a failure instead of changing the row locally and then snapping back on the next refresh.
- A failed map DM send keeps your typed text and shows what went wrong.
- Chat and map send errors now show the server's own message instead of raw JSON.
- OM Lite Log import works again. It was uploading a file the server could not read.
- The OM Lite Sense toggle no longer starts inverted and now follows the server's actual state.
- Bad input to passive-observation cleanup and contact notes now returns a clear 400 instead of an error page.

### New and improved

- A yellow update banner across the top of the page tells you when a newer OverMesh release is available, showing the available and installed versions, an "Open updates" button and a dismiss X. Dismissal is remembered per release, so silencing one release does not hide the next one. OM Lite shows the same banner.
- The New Mark / Self Note dialog is network-aware. On a MeshCore-only map it offers just the Self Note tab, because MeshCore has no waypoint message; on Meshtastic it shows the "Mark (MT waypoint)" tab. The Self Note tab can also add the note to the Log.
- The MeshCore message badge is now honest about how sure OverMesh is that it matched a received packet to a message. It says "path unconfirmed" when the packet could not be confirmed, instead of showing a confidence that was almost always low.

### Removed

- The parked Cross-system forwarding feature has been removed. It was never enabled, so nothing changes for a normal install, and a stored cross configuration key is simply ignored from now on.
- Three dead API endpoints, a do-nothing "RC collector obs count" alert toggle, and two vendored JavaScript files that nothing referenced were removed.

---

## Earlier notes

This is the point where OverMesh stops being just the older Meshtastic dashboard with some MeshCore work around it, and becomes the real MT plus MC app.

The main change is simple: Meshtastic and MeshCore now live in one interface and are treated as equal parts of the same tool. The app is still local first, still self-hosted, and still built around real radio use.

Linux is still the strongest target and the most natural fit for attached radios and cyberdeck-style use, but the install path is also being cleaned up so OverMesh is easier to run natively on Windows and macOS.

## Highlights

Cross-system work is now real and usable. OverMesh can mirror traffic one way between MT and MC, or bridge it both ways. Rules can be saved, filtered by sender, and clearly tagged so forwarded traffic does not look like a normal local message.

MeshCore is no longer treated like a secondary panel on an MT app. It now has proper chat tabs, DM tabs, delivery state, bot support, Sense activity, map and Nodes parity work, and better heard-recently logic that matches how MC actually behaves.

Meshtastic and MeshCore now feel much closer as one product. A long consistency sweep cleaned up labels, map actions, contact and node handling, unread indicators, notifications, and a lot of the small UI mismatches that made the app feel uneven before.

## New and improved

MC bot support was expanded, including better channel-scoped replies, so a bot response stays where it belongs instead of leaking into every MC channel view.

In-app notifications were added for new messages, new nodes, and nodes seen again after a long gap, with settings toggles and calmer styling so they stay useful instead of noisy.

Per-channel history cleanup is now available in Settings on both sides, so MT and MC channel history can be cleared without wiping unrelated data.

Cross-system forwarding gained recent message dedupe, clearer tagging, saved rule editing, per-rule enable and disable, better target matching for direct commands, and a cleaner rule layout.

MC map and Sense behavior was tightened up, including node list jumps from map popups, more consistent last seen handling, and better live updates from pings and activity.

Message metadata got cleaner too. MT messages now show time without repeating the date on every line, and both systems can show SNR in the message meta when it is available, with MC treated as observed data rather than guaranteed packet metadata.

## Current shape

Mirror means one-way monitoring or copying from one system to the other.

Bridge means a two-way linked path between one MT channel and one MC channel.

Every forwarded message is clearly marked with the source system, original sender, and source channel, so relayed traffic does not pretend to be native local traffic.

## Still not in scope

DM bridging is not implemented yet.

Cross system forwarding is still channel to channel, not arbitrary message routing.

Some MC metadata is still best effort, especially where MeshCore does not expose the same packet detail as Meshtastic.

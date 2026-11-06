# S485 handover for Filip

Verdict first: the branch is ready to ship as one batch. The EST-324 sweep found
four more bugs, and EST-321 adds the map gesture for alternate MC message
paths. Every fix has a test that fails on the code before the fix. They sit on
top of the two earlier items, EST-319 chat catch-up and EST-320 minimap.
Nothing here needs a new setting, a migration or a manual step after the update.

Branch `s485-report`, base `cc948e6`, last commit `a3dac5b`. Not pushed, not
deployed, VERSION untouched.

Gate after the last commit, real exit code: `tests/run.sh` exit 0, 640 Python
tests OK, 25 Node test files OK, eslint no-undef clean. The same full gate ran
before each fix commit.

## What I swept in EST-324

I read `git diff cc948e6..HEAD` against its call sites, not just on its own:
the new SSE resync, the SSE close sentinel, the minimap own-position resolver,
the minimap marker loop, the minimap resize grip, and the new lifecycle
listeners. I looked for the failure modes on your list: duplicates after a
reconnect, a reload storm, a poll that never stops, a listener added on every
redraw, a popup or path left behind, MC and MT parity, no-touch and no-hover
browsers, phone width, and a stale position shown as current.

Found and fixed four:

1. the minimap could not be moved with a finger at all,
2. the minimap Traces poll kept running in a hidden tab,
3. the minimap own marker only moved on the 15 second poll, so it showed an old
   position as current,
4. an MC radio that reconnected kept the position from its previous session.

The same-cause checks elsewhere found nothing more: the other panels I looked at
either rebuild their DOM, so their listeners cannot pile up, or already stop
their timers when the tab or the panel closes.

Not swept, and I am not claiming them: MT send and ACK paths, the bot, RPTR and
remote collector, settings persistence, the server routes other than the chat
stream and MC status, and the live databases. Real radios and a real browser are
also outside what I could test here.

## Item: the minimap cannot be moved with a finger

FIXED. The resize grip had already been moved to pointer events, but the header
bar beside it still listened to `mousedown`. On the CD touchscreen you could
resize the minimap but not move it. The header now uses pointer events with
pointer capture, the same path as the grip, and ends on pointerup, pointercancel
or lost capture. A press that starts on one of the header buttons is still a
click, not a drag.

The three header buttons (Traces, Locate, Collapse) were 13 pixel text with 2
pixels of padding. They are now at least 44 by 44 pixels. The header sets
`touch-action: none` so a finger drag does not scroll the page underneath.

How to see it: open the minimap on Nodes or Chat on the CD, put a finger on the
header bar and drag. The whole minimap moves. Tap Traces, Locate and Collapse:
each is a fingertip target. A mouse still drags it the same way.

Test: `tests/test_pip_resize_touch.js` drives the real `initPipMap()` header
drag, the button exclusion and the CSS sizes. It fails on `cc948e6`, where the
grip has no pointerdown listener and the header has no pointer path.

## Item: the minimap Traces poll kept running in a hidden tab

FIXED. The poll gate checked the tab (Nodes or Chat), whether the minimap was
open, and whether an MC radio was connected, but not whether the browser window
was visible. A phone left on another app, or the CD showing something else,
kept asking for passive observations every few seconds in the background. The
gate now also requires a visible document, and the visibilitychange handler
re-runs the sync on both edges: hiding stops the poll, showing resumes it. The
chat resync on wake is unchanged.

How to see it: open the minimap, switch Traces on, then put the browser in the
background for two minutes. The passive observation requests stop. Bring it
back and they resume. There is nothing to see in the page itself; if you want
proof, the server journal shows the `/passive_obs` requests.

Test: `tests/test_pip_traces.js` runs the real `_pipRxActive` with
`document.hidden` set and checks the timer stops and restarts. The hidden-tab
assertion fails on the branch before this commit, where `_pipRxActive` ignores
`document.hidden`.

## Item: the minimap own marker moved only every 15 seconds

FIXED. The new minimap draws our own position, but the live SSE fast path only
refreshed the main map. A `gps_position` or `local_node_position` event moved the
main map while the minimap kept the previous spot, so for up to 15 seconds (the
`loadLive` interval) it showed an old position as current. Losing the GPS fix was
worse: the stale own marker stayed on the minimap until the next poll.

Both handlers now refresh the minimap with the main map, and `gps_error` removes
the own marker as soon as the fix is gone. `updatePipMarkers` returns immediately
when the minimap is closed, so the cost when it is not on screen is one function
call.

How to see it: open the Map with a GPS source and a live fix, and the minimap
open on Nodes or Chat. Walk or move the GPS antenna: the blue own dot on the
minimap moves within a second, not after 15 seconds. Unplug the GPS: the dot
goes away on the next update instead of lingering.

Test: `tests/test_pip_own_position.js` asserts the three handlers refresh the
minimap. The GPS assertion fails on `6aad9a6`, the branch just before this fix.

## Item: an MC radio showed its previous position after a reconnect

FIXED. `mc_status` carries the name, the status and the status time, not a
latitude or longitude. The page merged that event into the remembered status and
kept the coordinates from the radio's last session. A radio that reconnected
somewhere else was then marked connected at its old spot. Both maps draw a
connected MC radio's own position, so they showed an old position as current.
The status fetch that follows a connect corrected it, but only when that request
answered, and the old spot stayed if it failed.

A connect event that carries no position now drops the remembered coordinates;
the status fetch refills the real ones. A payload that does carry a position is
applied. A disconnect event is left alone, because a disconnected radio is
already filtered out of the map lists.

How to see it: connect an MC radio and note its green diamond. Disconnect it,
change its position (or power cycle it after moving it), and reconnect. The old
spot must not appear as current while the page waits for the fresh status. The
marker appears only when `/api/mc/status` returns the new coordinates.

Test: `tests/test_pip_own_position.js` drives the real `_mcDropStalePosition`
with a connect event without a position, a connect event with one, and a
disconnect event. The helper does not exist on `de0bece`, the branch just before
this fix.

## EST-319: messages only appear while a browser is watching (MC and MT)

FIXED. The radio and the stores were not the problem.

What I checked against the evidence files:

- A message logged but never stored: ruled out for channels. While TestBox slept
  the journal times of MC channel messages and the stored MC row times match
  minute for minute. On 11-04 the journal has 05:18, 05:49, 06:13, 06:18, 06:20,
  06:21, 06:22, 06:28 twice, 06:29, 06:32, 06:52 twice; the stored histogram has
  2 rows in the 05 hour and 11 in the 06 hour. On 11-06 the journal has one
  channel message at 05:48 and the store has one row in that hour. Receiving and
  storing did not need a browser.
- A wrong sender clock: ruled out for this sample. The stored MC `ts` values line
  up with the journal receipt minutes, so they were not filed under an old date.
- The store dedupes, caps or prunes it: not the loss. The live MC table is at its
  2000-row cap, but every channel message in the sampled sleep windows was still
  present. Old rows are dropped by design at the cap; that did not remove these.
- The page live feed with no catch-up: this was real. This is the main cause.
- Something on the server that runs only while a client is connected: this was
  real too, and it made the first cause permanent for a reconnect that never
  happened.

What was wrong, cause 1: the page kept only a live feed. It never refetched what
arrived while it slept or while its socket was down. MT was partly saved by the
server replaying its in-memory chat list on a new SSE connection. MC had no such
replay, and the MC archive was fetched only at page load or when a radio reported
connected. A page that woke up showed its old list, not the server's stored rows.

What was wrong, cause 2: on the server, an SSE client whose queue stayed full for
10 minutes was removed from the broadcast list, but its HTTP response was left
open. The browser saw a healthy connection, never reconnected, and stopped
receiving anything until a manual reload.

What it does now:

- The page pulls stored rows on every SSE open, including an automatic
  reconnect, when the tab becomes visible, on a pageshow from the back/forward
  cache, and when the network comes back online. MC first: each MC radio loads
  its stored rows from `/api/mc/<id>/messages`, channels and DMs together, and
  merges by id. MT then merges `/api/chat/messages` by id. Pulled rows are marked
  history, so they cause no toast, no sound and no unread count, and the same row
  is never added twice.
- An evicted SSE client now gets a close sentinel. Its response ends, the browser
  reconnects, and that reconnect runs the resync above.
- ERA-3 over TCP is just another MC radio id: it uses the same save path and the
  same resync. It is covered by the code, but not tested on the real radio.
- DMs use the same MC save function as channel messages, and the resync fetches
  every subtype. The evidence files contain no DM timestamps, so the DM part is
  covered by code and test, not by a measured DM.

How Filip can see it: leave OM open on Gandalf, put TestBox to sleep, have
someone send an MC channel message and a DM, then wake TestBox. The chat should
show the stored messages without a reload. The same applies to MT. Opening the
page fresh still loads history as before.

What I could not test: a real browser sleep/wake cycle, a real reconnect, real
radios, and the live gandalf databases (ds-ro cannot read the OM service data
there). The page path is covered by `tests/test_chat_resync.js` and the eviction
close by `tests/test_sse.py`; both fail on `cc948e6`. For a real check after the
update, send one message while TestBox is asleep, then confirm on gandalf that
the row exists and on the woken page that it appears without a reload.

Exact next step if a message still seems missing on gandalf: as the OM service
user, run this against the relevant live DB and compare its times with the
journal (Claude has the live path it used for the evidence):

    sqlite3 /path/to/overmesh_mc_msgs_<radio>.db "select datetime(ts,'unixepoch'), subtype, channel, status from messages order by ts desc limit 20;"

## Minimap: my position / CD / node location is not shown

FIXED.

What was wrong: the minimap built its own marker lists. It drew MT nodes and MC
contacts, but never the connected MC radio's own position, and it never drew the
OM Position at all. On the CD with an MC radio that left the minimap with no
marker for us.

What it does now: one shared resolver, `_omOwnPositions()`, returns the OM
Position (hand-picked, or a live GPS fix), each connected MC radio's own position
and the MT local node. The minimap draws that list with the same marker builders
the main map uses, so the MC radio keeps its green diamond and our own point its
blue dot. A source with no position adds no marker: no fallback to an old point,
no guess. The Locate button and the first auto-centre use the same list, so
Locate works on MC too. EST-324 added the live refresh and the MC reconnect reset
above, so the list no longer keeps an old position when a fix arrives or a radio
reconnects.

How Filip can see it: open the minimap on Nodes or Chat. Your own point is on it
on MC, on MT and with a hand-picked OM Position. If there is no real position,
there is no marker.

What I could not test: real radios and a real browser. The resolver and the
marker wiring are covered by `tests/test_pip_own_position.js`, which fails on
`cc948e6` because the resolver did not exist.

## Minimap on CD: cannot resize with a finger

FIXED. The top-left grip listened to `mousedown` only, so a touch never reached
it, and the grip was 18 by 18, too small for a finger. It now uses pointer events
with pointer capture, so finger and mouse take the same path. `pointerdown` calls
preventDefault and stopPropagation, and the CSS sets `touch-action: none` on the
grip, so dragging it does not pan the page or the map. The grip is 44 by 44, the
visible corner mark stays small. The header drag now uses the same pointer path
as well: the earlier note that it was unchanged no longer applies (see the
EST-324 item above).

How Filip can see it: open the minimap on the CD touchscreen and drag the
top-left corner with a finger. It should resize, and the page behind should stay
put. Drag the header bar to move it. A mouse still works the same way.

What I could not test: a real touchscreen. The drag maths, pointer capture,
clamping and the CSS sizes are covered by `tests/test_pip_resize_touch.js`, which
fails on `cc948e6` because the grip has no pointerdown listener. The one thing
left for a real check is the finger feel on the CD itself.

## Item: alternate MC message paths on the map (EST-321)

FIXED. In the MC Activity pop-up, each alternate received copy used to be a
dead block. Only a hop with a position could be clicked, and a finger had no
way to see the whole path.

Each alternate copy is now a control. Hovering it draws that copy's path on the
map; leaving clears it. Clicking or tapping pins it until another copy is
clicked or the panel closes. A tap also fits the whole copy in view. The copy
shows a plain status line: "Shown on map", plus "N hops with no known position
are not drawn" and "1 hop id is ambiguous, so this line is a best guess" when
they apply. Hops with no position say "unknown position" in the list.

The path uses the existing hop resolver, so ambiguous MC hashes keep their
confidence note. A hop that cannot be resolved is not bridged: the line stops at
the last known point and resumes at the next known point, so the map never draws
the packet somewhere no relay is known to be. Alt copy paths now use the same
purple message style as the primary path.

How to see it: open Map, or Sense with MC selected, click an MC message in the
Activity panel that shows "Other copies", expand "Alternate paths", then hover a
copy. The path appears and goes away when you leave. Click or tap it and it
stays. Close the panel and it clears. A second click on another copy replaces
it.

What I could not test: a real radio, a real browser, and a real finger on the CD
touchscreen. The DOM contract, status words and gap indexing are covered by
`tests/test_mc_alt_paths.js`, which fails on the old code; Leaflet drawing was
not run in a browser here.

MT: not applicable. The alternate received copies and this path pop-up exist on
MC only; MT has no `rx_copies` list in the Activity panel.

## What no one could test without real radios or a real browser

- A real sleep and wake on the phone, and a real SSE reconnect. The code paths
  are covered by tests; the actual browser reconnect is not.
- A real touch drag on the CD touchscreen, including the feel of the 44 pixel
  targets. The geometry is tested, the finger is not.
- A real GPS losing and regaining a fix, and a real MC radio reconnecting at a
  new position. Both are simulated in the tests.
- MC and MT radios on the air. No packet was sent in this run.
- The live gandalf databases and the OM service data. Not visible to ds-ro.
- Phone width was checked in the CSS and test geometry, not on a physical phone.

## Proposed release notes (NOT applied)

### Fixed

- **Alternate MeshCore message paths can be seen on the map.** Hover a copy in
  the Activity panel to draw it, click or tap to keep it. Hops with no known
  position are left out instead of being drawn in the wrong place.
- **The minimap can be moved with a finger, and its header buttons are finger
  sized.** The header bar dragged with a mouse only. It now uses the same pointer
  path as the resize grip, and Traces, Locate and Collapse are at least 44 by 44
  pixels.
- **The minimap stops polling when the page is hidden.** The Traces request kept
  running in a background tab or a phone on another app. It now stops when the
  page is hidden and resumes when it is shown.
- **The minimap own position keeps up with a live fix.** A GPS or local node
  position event moved the main map only, so the minimap could show the previous
  spot for up to 15 seconds. It now moves at once, and a lost fix removes the dot
  at once.
- **A MeshCore radio no longer shows its previous position after a reconnect.**
  The status event carries no coordinates, so the old ones stayed and the map
  marked the radio as current at its old spot. They are cleared on connect and
  filled again from the fresh status.
- **Messages that arrived while the page was asleep now appear without a
  reload.** The page pulls stored rows after a reconnect, on wake, on a back and
  forward cache restore and when the network comes back. MeshCore first, then
  Meshtastic. An evicted live feed now closes so the browser reconnects instead
  of staying silent.
- **The minimap shows your own position on MeshCore, Meshtastic and with a
  hand-picked position.** The connected MeshCore radio, the local Meshtastic node
  and the OM Position are drawn with the same markers the main map uses. Locate
  uses the same list.

## Proposed VERSION (NOT applied)

`2026.11.06.1`

Current VERSION on this branch is `2026.11.03.1`. The proposal is not applied; it
is here for you or Claude to set at release time.

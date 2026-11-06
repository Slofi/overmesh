# S485 handover (EST-320: minimap, EST-319: observer effect)

Branch `s485-report`. Commits `af7367e` (EST-319) and `a2500fc` (EST-320), base `cc948e6`.
Gate after the last commit: `tests/run.sh` exit 0: 640 Python tests OK, 25 Node test files OK,
eslint no-undef clean. Not pushed, not deployed, VERSION untouched.

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
blue you-dot. A source with no position adds no marker: no fallback to an old
point, no guess. The Locate button and the first auto-centre use the same list,
so Locate works on MC too.

How Filip can see it: open the minimap on Nodes or Chat. Your own point is on it
on MC, on MT and with a hand-picked OM Position. If there is no real position,
there is no marker.

What I could not test: real radios and a real browser. The resolver and the
marker wiring are covered by `tests/test_pip_own_position.js`, which fails on
`cc948e6` because the resolver did not exist.

## Minimap on CD: cannot resize with a finger

FIXED.

What was wrong: the top-left grip listened to `mousedown` only, so a touch never
reached it, and the grip was 18x18, too small for a finger.

What it does now: the grip uses pointer events with pointer capture, so finger
and mouse take the same path. `pointerdown` calls preventDefault and
stopPropagation, and the CSS sets `touch-action: none` on the grip, so dragging
it does not pan the page or the map. The grip is 44x44. The visible corner mark
stays small. The header mouse drag is unchanged.

How Filip can see it: open the minimap on the CD touchscreen and drag the
top-left corner with a finger. It should resize, and the page behind should stay
put. A mouse still works the same way.

What I could not test: a real touchscreen. The drag maths, pointer capture,
clamping and the CSS size are covered by `tests/test_pip_resize_touch.js`, which
fails on `cc948e6` because the grip has no pointerdown listener. The one thing
left for a real check is the finger feel on the CD itself.

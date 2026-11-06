# S486 handover

**Claude's check, 2026-11-06 12:10. Verdict: ready to ship on Filip's word; only a real laptop sleep can finally prove it.** In a real browser against this branch: with the real server process frozen 85 s and no browser event at all, the page saw the silence and replaced its feed; on thaw the missed MC channel message, DM and MT row appeared with no reload, each unread once, no sound, typed text kept. With the main thread really blocked 36 s (control: timers measured stalled 36.4 s) the feed was replaced, tab showing, no event. A healthy feed is left alone: no reopen in 40 s. **Claude's review found one more defect, fixed:** MT rows replayed by the server arrived before the catch-up fetch, so a missed MT message never counted unread. **Not proven:** a real OS suspend with a real half-dead socket, real radios, gandalf. Lite is unchanged.

One section per item. FIXED means code and tests are in the branch, not that it
was tried on real radios or in a real browser.

## EST-326: a dead live feed after a laptop wakes, missed messages unread

FIXED (code and tests; real-browser sleep not tested yet).

What was wrong:
- The server keepalive was an SSE comment line, which JavaScript never sees, so
  a half-dead socket still looked OPEN. The page showed nothing new until a
  manual reload.
- The catch-up only fired on visibilitychange, pageshow from cache and online.
  A laptop that sleeps with the tab showing fires none of those.
- A catch-up row arrived silently: no unread dot, no count.

What changed:
- The server sends a named keepalive every 25 s: an `event: keepalive` line and
  a `data: 1` line. Named events do not reach onmessage, so message handling in
  app.js and lite.js is unchanged.
- One function in app.js owns the EventSource: onopen, onmessage, the keepalive
  listener and a registry for the two msgstore message handlers, so a replaced
  feed keeps any handler still in flight and removal goes through the same
  registry.
- A watchdog runs every 10 s while the page exists. If the document is visible
  and the timer itself stalled for more than 30 s (the machine slept), or no
  message and no keepalive arrived for more than 70 s, or readyState is CLOSED,
  the feed is closed and a new one opened. Its onopen runs the catch-up. At most
  one reopen per 30 s. A hidden tab does nothing; the visible edge checks at
  once. The page is never reloaded, so typed text stays in the input.
- Catch-up rows that are genuinely new count unread on MC and on MT, once, with
  the same dot and count as a live message, when they are not on the tab being
  looked at. No sound and no toast. Rows outside the window the page already
  held do not count. The first load of the page counts nothing.
- A return from a hidden tab runs the catch-up only after 30 s or more hidden,
  so a phone switching apps no longer reloads status, contacts, channels and
  messages for every radio on every return. pageshow and online still do.

How Filip can see it:
- After his in-app update and the server restart, open Chat and, for example,
  another tab on the OverMesh page. Put the laptop to sleep with the page
  showing, send a message from a radio while it is asleep, wake it. Within at
  most about 80 s (usually the first 10 s watchdog tick after wake) the message
  appears without a reload, and the Chat tab shows the unread dot and count if
  he is on another tab. Nothing should make a sound on wake.
- A phone switching apps for a few seconds must not refetch every radio.

Tests:
- New tests/test_chat_wake_feed.js drives the real feed owner with a fake
  EventSource and fake clock: stalled timer reopens, 70 s silence reopens,
  keepalive prevents it, a hidden tab does nothing and the visible edge checks,
  one reopen per 30 s, CLOSED reopens, a registered msgstore handler survives a
  reopen and is removed from the new object, input text is untouched, and the
  real merge functions count unread once, never on first load, never for sent
  rows, never for the open tab. It also asserts the merge block has no sound
  call.
- New tests/test_chat_keepalive.py drives the real generator: named keepalive,
  history rows first, SSE_CLOSE still ends it, the route uses this generator,
  and lite.js has only onmessage and no keepalive listener.
- Both new tests were run against 4b5ebfa and fail there.

Not tested, and why:
- A real browser after a real laptop sleep: I have no browser here. The
  half-dead TCP case is simulated by the watchdog clock, not by a frozen
  server. Claude planned that browser test with a frozen server process.
- The two msgstore flows with a real radio: the registry is tested with a fake
  EventSource only. There is no MC hardware here.
- One reopen per 30 s is tested, but a real down server reconnecting through
  EventSource's own retry has not been watched live.

lite.js: NOT CHANGED (HD is semi-shelved). It has its own EventSource and the
same weakness. The same fix there would need: a named keepalive listener on its
source, a last-seen timestamp, the same 10 s watchdog with a 30 s stall and 70 s
silence rule, CLOSED handling, a 30 s reopen cap, and a catch-up fetch on
onopen. It has no unread/count UI, so catch-up rows would only refresh the node
and message lists, which is simpler than app.js.

Branch note: the ticket body says branch s486-wake is checked out, so the work
is committed there. The RULES line says s485-report; that looks like a copy of
the previous ticket. Nothing was pushed and VERSION was not bumped.

Proposed release notes (not written to RELEASE_NOTES.md):
- Messages arrive again after a laptop wakes, even when the live connection died
  silently: a watchdog notices the dead feed and pulls the stored rows.
- Messages missed while asleep show the unread dot and count on Chat, without a
  sound or pop-up for the backlog.
- A phone returning from another app no longer reloads every radio unless it
  was away for 30 s or more.

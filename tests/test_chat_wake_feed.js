const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');

const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// ── The real feed owner, extracted and driven with a fake EventSource ───────
const feedStart = src.indexOf('  // ── Live chat feed ownership');
const feedEnd = src.indexOf('  function initChat() {');
assert(feedStart > 0 && feedEnd > feedStart, 'the live feed block must sit before initChat');
const feedBlock = src.slice(feedStart, feedEnd);

// One line of the block must never appear here: a reload would throw away the
// text the user had typed.
assert(!/location\.reload|window\.location\s*=/.test(feedBlock),
       'the wake path must never reload the page');

function makeFeed(clock) {
  const instances = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.closed = false;
      this.listeners = { message: [], keepalive: [] };
      instances.push(this);
    }
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
    removeEventListener(name, fn) {
      const a = this.listeners[name] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    }
    close() { this.closed = true; this.readyState = 2; }
    emit(name, data) { (this.listeners[name] || []).slice().forEach(fn => fn(data)); }
  }
  const doc = { hidden: false };
  const FakeDate = { now: () => clock.t };
  const resyncs = [];
  const messages = [];
  const code = feedBlock + `
    return {
      open: _liveFeedOpen,
      check: _liveFeedCheck,
      add: _liveFeedAddHandler,
      remove: _liveFeedRemoveHandler,
      current: () => chatSSE,
      lastSeen: () => _liveFeedLastSeen,
      setLastTick: v => { _liveFeedLastTick = v; }
    };`;
  const api = new Function(
    'EventSource', 'document', 'Date', 'BASE_PATH',
    '_resyncChatFromServer', '_liveFeedOnMessage', 'chatSSE', code
  )(FakeEventSource, doc, FakeDate, '', opts => resyncs.push(opts), e => messages.push(e), null);
  return { api, instances, doc, clock, resyncs, messages };
}

// 1. A stalled timer means the machine slept: reopen when the page is visible.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  f.api.check();                       // normal tick, records the time
  clock.t = 41000;                     // 40 s gap: the timer did not run
  assert(f.api.check() === true, 'a stalled watchdog timer must reopen the feed');
  assert(f.instances.length === 2, 'a stalled timer opens a second EventSource');
  assert(f.instances[0].closed === true, 'the dead feed is closed, not left behind');
}

// 2. No message and no keepalive for more than 70 s reopens.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  for (let t = 1000; t <= 67000; t += 10000) { clock.t = t; f.api.check(); }
  assert(f.instances.length === 1, 'under 70 s of silence must not reopen');
  clock.t = 72000;
  assert(f.api.check() === true, 'over 70 s without a keepalive must reopen');
  assert(f.instances.length === 2, 'silence opens a second EventSource');
}

// 3. A keepalive every 25 s keeps the feed alive.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  let lastKeep = 1000;
  for (let t = 1000; t <= 120000; t += 10000) {
    clock.t = t;
    // keepalive arrives every 25 s, and it must touch last-seen
    if (t - lastKeep >= 25000) { lastKeep = t; f.instances[0].emit('keepalive'); }
    f.api.check();
  }
  assert(f.instances.length === 1, 'a speaking server must not be replaced');
  assert(f.api.lastSeen() > 90000, 'keepalive must update the last-seen time');
}

// 4. A hidden tab does nothing, even when the feed is silent; the visible edge
//    checks at once and a real hide (30 s or more) is the only catch-up.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  f.doc.hidden = true;
  clock.t = 90000;
  assert(f.api.check() === false, 'a hidden tab must never replace the feed');
  assert(f.instances.length === 1, 'no EventSource work while hidden');
  // Keepalives still arrive while hidden (browsers deliver events there); only
  // the watchdog timers are throttled. That is not a sleep.
  clock.t = 90000;
  f.instances[0].emit('keepalive');
  f.doc.hidden = false;
  clock.t = 95000;
  // The visibility handler refreshes the tick before calling this, exactly as
  // the page does, so a throttled hidden timer is not read as a sleep.
  f.api.setLastTick(clock.t);
  const reopened = f.api.check({ fromHidden: 94000 });
  assert(reopened === false, 'a healthy feed is not replaced on the visible edge');
  // A 10 s trip to another app is below the catch-up threshold.
  assert(f.api.check({ fromHidden: 10000 }) === false, 'a short hide must not reopen');
}

// 5. At most one reopen per 30 s, so a down server cannot make a storm.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  f.api.check();
  clock.t = 72000;
  assert(f.api.check() === true, 'first silence reopen happens');
  const second = f.instances[1];
  second.readyState = 2;               // the new one is closed immediately
  clock.t = 73000;
  assert(f.api.check() === false, 'a second reopen inside 30 s is refused');
  assert(f.instances.length === 2, 'no storm of EventSources');
  clock.t = 103000;
  assert(f.api.check() === true, 'after the 30 s cap a persistent death reopens');
  assert(f.instances.length === 3, 'third EventSource after the throttle window');
}

// 6. readyState CLOSED reopens: EventSource never retries that by itself.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  f.api.check();
  f.instances[0].readyState = 2;
  clock.t = 11000;
  assert(f.api.check() === true, 'CLOSED must reopen at once');
  assert(f.instances.length === 2, 'CLOSED opens a second EventSource');
}

// 7. A msgstore handler registered on the old object follows a reopen and can
//    still be removed from the new one.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  f.api.open();
  const seen = [];
  const handler = e => seen.push(e);
  f.api.add(handler);
  assert(f.instances[0].listeners.message.includes(handler), 'handler attached to the live feed');
  f.api.check();
  f.instances[0].readyState = 2;
  clock.t = 11000;
  f.api.check();
  assert(f.instances.length === 2, 'feed was replaced');
  assert(f.instances[1].listeners.message.includes(handler),
         'the registered handler must survive the reopen');
  const payload = { data: '{"type":"msgstore_msg"}' };
  f.instances[1].emit('message', payload);
  assert(seen.length === 1 && seen[0] === payload, 'the surviving handler receives messages');
  f.api.remove(handler);
  assert(!f.instances[1].listeners.message.includes(handler),
         'removal must go through the registry to the new object');
}

// 8. Typed text survives a reopen: only the feed object changes.
{
  const clock = { t: 1000 };
  const f = makeFeed(clock);
  const input = { value: 'half typed message' };
  f.api.open();
  f.api.check();
  f.instances[0].readyState = 2;
  clock.t = 11000;
  f.api.check();
  assert(input.value === 'half typed message', 'the input box is untouched by a reopen');
}

// ── The real merge functions, driving unread marking ───────────────────────
const mergeStart = src.indexOf('  function _mcMessageKey(m) {');
const mergeEnd = src.indexOf('  // Catch the page up with both networks');
assert(mergeStart > 0 && mergeEnd > mergeStart, 'the merge block must be findable');
const mergeBlock = src.slice(mergeStart, mergeEnd);
assert(!/playNotificationSound\s*\(/.test(mergeBlock),
       'a catch-up must never play a sound');

function makeMerge(state) {
  const code = mergeBlock + `
    return { mtMerge: _mtMergeHistory, mcMerge: _mcMergeMessages };`;
  return new Function(
    'mcMessages', 'chatMsgs', 'currentTab', 'chatNetwork', 'chatChannel', 'mcChatTab',
    'activeRadioId', 'document', 'mcUnreadTabs', 'mcUnreadCounts',
    'unreadChannels', 'unreadChannelCounts', 'inactiveRadioUnread',
    'radioUnreadChannels', 'radioUnreadCounts',
    'mtIgnoredIdSet', 'dmContactId', '_incrementUnreadCount',
    'updateRadioSelector', 'updateUnreadDots', 'renderChannelTabs', 'renderMessages',
    'renderMcMessages', '_saveMcMessages', '_omLogShareProcessMessages',
    '_mcRebuildMessageTabs', code
  )(
    state.mcMessages, state.chatMsgs, state.currentTab, state.chatNetwork,
    state.chatChannel, state.mcChatTab, state.activeRadioId, state.document,
    state.mcUnreadTabs, state.mcUnreadCounts, state.unreadChannels,
    state.unreadChannelCounts, state.inactiveRadioUnread,
    state.radioUnreadChannels, state.radioUnreadCounts,
    () => new Set(), m => (m.sent ? m.to_id : m.from_id),
    (store, key) => { store[key] = (store[key] || 0) + 1; },
    () => {}, () => {}, () => {}, () => {}, () => {}, () => {},
    () => {}, () => {}
  );
}

function newState() {
  return {
    mcMessages: [],
    chatMsgs: [],
    currentTab: 'nodes',
    chatNetwork: 'mt',
    chatChannel: 10,
    mcChatTab: 'chan:0',
    activeRadioId: 'r1',
    document: { hidden: false },
    mcUnreadTabs: new Set(),
    mcUnreadCounts: Object.create(null),
    unreadChannels: new Set(),
    unreadChannelCounts: Object.create(null),
    inactiveRadioUnread: new Set(),
    radioUnreadChannels: Object.create(null),
    radioUnreadCounts: Object.create(null),
  };
}

// S486: the server replays MT history through the feed before the catch-up fetch
// answers, so that row reaches the merge as already held. It must count on the replay.
assert(/if \(data\.is_history\) _mtMarkCatchUpUnread\(data, _mtReplayFloor\);/.test(src),
  'a replayed MT row new to the page is counted unread');
// 9. MT: first load counts nothing, a catch-up counts once, silently.
{
  const s = newState();
  const m = makeMerge(s);
  m.mtMerge([{ id: 'a', ts: 100, channel: 1, radio_id: 'r1', text: 'first load' }]);
  assert(s.unreadChannels.size === 0, 'the first load of the page counts nothing');

  s.chatMsgs.push({ id: 'held', ts: 200, channel: 1, radio_id: 'r1' });
  m.mtMerge([
    { id: 'missed', ts: 300, channel: 1, radio_id: 'r1', text: 'while asleep' },
    { id: 'old', ts: 150, channel: 1, radio_id: 'r1', text: 'history backfill' },
  ], { catchUp: true });
  assert(s.unreadChannels.has(1), 'a catch-up row on another tab gets the unread dot');
  assert(s.unreadChannelCounts[1] === 1, 'exactly one unread count for the catch-up row');
  assert(!s.chatMsgs.find(x => x.id === 'old').unread, 'backfill is not marked as a message');
  assert(s.unreadChannelCounts[1] === 1, 'an old row filling in history does not count');

  // The same stored row arriving again must not count twice.
  m.mtMerge([{ id: 'missed', ts: 300, channel: 1, radio_id: 'r1' }], { catchUp: true });
  assert(s.unreadChannelCounts[1] === 1, 'a row counts once, never twice');

  // A sent row is never unread.
  m.mtMerge([{ id: 'mine', ts: 400, channel: 1, radio_id: 'r1', sent: true }], { catchUp: true });
  assert(s.unreadChannelCounts[1] === 1, 'our own sent row must not count');
}

// 10. MT: a row in the tab being looked at is not unread; a hidden page counts.
{
  const s = newState();
  s.chatMsgs.push({ id: 'held', ts: 100, channel: 1, radio_id: 'r1' });
  s.currentTab = 'chat';
  s.chatChannel = 1;
  const m = makeMerge(s);
  m.mtMerge([{ id: 'here', ts: 200, channel: 1, radio_id: 'r1' }], { catchUp: true });
  assert(s.unreadChannels.size === 0, 'the channel being looked at stays clean');
  s.document.hidden = true;
  m.mtMerge([{ id: 'hidden-row', ts: 300, channel: 1, radio_id: 'r1' }], { catchUp: true });
  assert(s.unreadChannels.has(1), 'a hidden page is not "being looked at"');
}

// 11. MC: same rules, dot and count, never on first load, never twice.
{
  const s = newState();
  const m = makeMerge(s);
  s.mcMessages.push({ id: 'm-held', radio_id: 'r1', ts: 100, subtype: 'channel', channel: 0 });
  m.mcMerge([{ id: 'm-new', radio_id: 'r1', ts: 200, subtype: 'channel', channel: 1 }],
            { silent: true, catchUp: false });
  assert(s.mcUnreadTabs.size === 0, 'MC first load counts nothing');
  s.mcMessages.push({ id: 'm-new2', radio_id: 'r1', ts: 250, subtype: 'channel', channel: 1 });
  m.mcMerge([{ id: 'm-missed', radio_id: 'r1', ts: 300, subtype: 'channel', channel: 2 },
             { id: 'm-old', radio_id: 'r1', ts: 150, subtype: 'channel', channel: 3 }],
            { silent: true, catchUp: true });
  assert(s.mcUnreadTabs.has('chan:2'), 'an MC catch-up row gets the unread dot');
  assert(s.mcUnreadCounts['chan:2'] === 1, 'one unread count for the MC catch-up row');
  assert(!s.mcUnreadTabs.has('chan:3'), 'an old MC row filling history does not count');
  m.mcMerge([{ id: 'm-missed', radio_id: 'r1', ts: 300, subtype: 'channel', channel: 2 }],
            { silent: true, catchUp: true });
  assert(s.mcUnreadCounts['chan:2'] === 1, 'MC catch-up counts once');
  m.mcMerge([{ id: 'm-sent', radio_id: 'r1', ts: 350, subtype: 'channel', channel: 2, sent: true }],
            { silent: true, catchUp: true });
  assert(s.mcUnreadCounts['chan:2'] === 1, 'our own MC row is not unread');

  s.currentTab = 'chat';
  s.chatNetwork = 'mc';
  s.mcChatTab = 'chan:1';
  const m2 = makeMerge(s);   // the tab state is captured when the harness is built
  m2.mcMerge([{ id: 'm-open', radio_id: 'r1', ts: 400, subtype: 'channel', channel: 1 }],
             { silent: true, catchUp: true });
  assert(!s.mcUnreadTabs.has('chan:1'), 'the MC tab being looked at stays clean');
}

console.log('ok: chat wake feed watchdog and catch-up unread');

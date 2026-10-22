// Minimap (PiP) MC Traces (S468) — the real functions extracted from app.js and
// run against fakes, not grepped.
//
// What must hold:
//  - the shared player animates each packet once per view, skips hidden types,
//    caps the seed, launches on the map it was given, and re-checks stillOn
//    before each staggered seed launch;
//  - clearing one map's packets never touches the other map's (Sense switches
//    its Traces off whenever the Map tab is left — that must not wipe the
//    minimap, and the reverse);
//  - Sense's "in flight" counter counts the main map only;
//  - the minimap polls ONLY while it is visible, on Nodes/Chat, with an MC radio
//    connected and not hidden — and stops + clears when any of that changes;
//  - the toggle is remembered, and the button is shown only when MC is connected.
const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');
const html = fs.readFileSync(path.join(REPO, 'templates/index.html'), 'utf8');

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL: ' + msg);
    process.exit(1);
  }
}

function extract(name, endMarker = '\n  }') {
  const start = src.indexOf(`function ${name}(`);
  assert(start >= 0, `${name}() not found in app.js`);
  const end = src.indexOf(endMarker, start);
  assert(end > start, `${name}() has no end marker`);
  return src.slice(start, end + endMarker.length);
}

// ── 1. shared player ────────────────────────────────────────────────────────
{
  const launched = [];
  const timers = [];
  const env = new Function('launched', 'timers', [
    'const MC_RX_SEED_COUNT = 3; const MC_RX_SEED_STAGGER_MS = 10;',
    'let _mcRxHidden = new Set(["ACK"]);',
    'const _mcRxKey = r => `${r.ts}|${r.path}|${r.payload_type}|${r.rssi}`;',
    'function _mcRxPathResult(row) { return row.unresolvable ? null : { row }; }',
    'function _mcRxLaunch(res, map) { launched.push([res.row.ts, map]); }',
    'function setTimeout(fn) { timers.push(fn); }',
    extract('_mcRxPlay'),
    'return _mcRxPlay;',
  ].join('\n'))(launched, timers);
  const _mcRxPlay = env;

  const rows = [5, 4, 3, 2, 1].map(ts => ({ ts, path: 'aa', payload_type: 'ADVERT', rssi: -90 }));
  rows.push({ ts: 9, path: 'bb', payload_type: 'ACK', rssi: -80 });   // hidden type

  // live poll (no seed): every new packet once, oldest first, on the given map
  let seen = _mcRxPlay(rows, new Set(), false, 'MINI', () => true);
  assert(launched.map(l => l[0]).join(',') === '1,2,3,4,5', 'plays oldest-first, all fresh rows: ' + launched.map(l => l[0]));
  assert(launched.every(l => l[1] === 'MINI'), 'launches on the map it was given');
  assert(!launched.some(l => l[0] === 9), 'a hidden payload type is not animated');
  launched.length = 0;
  seen = _mcRxPlay(rows, seen, false, 'MINI', () => true);
  assert(launched.length === 0, 'an unchanged poll animates nothing (already seen)');
  // a separate view has its own seen-set: the same packets animate there too
  _mcRxPlay(rows, new Set(), false, 'MAIN', () => true);
  assert(launched.length === 5 && launched.every(l => l[1] === 'MAIN'), 'each view keeps its own seen-set');

  // seed: capped, staggered, and re-checks stillOn per launch
  launched.length = 0; timers.length = 0;
  let on = true;
  _mcRxPlay(rows, new Set(), true, 'MINI', () => on);
  assert(launched.length === 0 && timers.length === 3, 'seed is capped (3) and staggered via timers');
  timers[0](); on = false; timers[1](); timers[2]();
  assert(launched.length === 1, 'turning the view off mid-seed stops further launches');
}

// ── 2. per-map clearing + counter ───────────────────────────────────────────
{
  const removed = [];
  const env = new Function('removed', [
    'let leafletMap = "MAIN";',
    'let _mcRxRaf = 7; let cancelled = 0;',
    'function cancelAnimationFrame() { cancelled++; }',
    'let _mcRxRows = [1, 2];',
    'const el = { textContent: "" };',
    'const document = { getElementById: () => el };',
    'let _mcRxAnims = [{ map: "MAIN", id: "m1" }, { map: "MINI", id: "p1" }, { map: "MAIN", id: "m2" }];',
    'function _mcRxRemoveAnim(a) { removed.push(a.id); }',
    extract('_clearMcRxLines'),
    extract('_mcRxUpdateCount'),
    'return { clear: _clearMcRxLines, count: _mcRxUpdateCount,',
    '         anims: () => _mcRxAnims, raf: () => _mcRxRaf, cancelled: () => cancelled, el };',
  ].join('\n'))(removed);

  env.count();
  assert(env.el.textContent.startsWith('2 in flight'), 'Sense counter counts the main map only: ' + env.el.textContent);
  env.clear();   // default = main map (what Sense calls when leaving the Map tab)
  assert(removed.join(',') === 'm1,m2', 'clearing the main map removes only its packets: ' + removed);
  assert(env.anims().length === 1 && env.anims()[0].id === 'p1', 'the minimap packet survives');
  assert(env.raf() === 7 && env.cancelled() === 0, 'the animation loop keeps running while the minimap has packets');
  env.clear('MINI');
  assert(env.anims().length === 0 && env.raf() === null && env.cancelled() === 1, 'last map cleared -> loop cancelled');
}

// ── 3. minimap on/off logic ─────────────────────────────────────────────────
{
  const env = new Function([
    'const store = {};',
    'const localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };',
    'const btn = { style: { display: "none" }, textContent: "", cls: new Set(),',
    '              classList: { toggle(c, on) { on ? btn.cls.add(c) : btn.cls.delete(c); } } };',
    'const document = { getElementById: id => (id === "pip-trace-btn" ? btn : null) };',
    'let mcLastStatus = { r1: { status: "connected" } };',
    'let pipOpen = true, pipInited = true, pipMap = "MINI", currentTab = "nodes";',
    'const MC_RX_REFRESH_MS = 5000;',
    'let nextTimer = 1, liveTimers = new Set(), refreshCalls = [], cleared = [];',
    'function setInterval() { const t = nextTimer++; liveTimers.add(t); return t; }',
    'function clearInterval(t) { liveTimers.delete(t); }',
    'function _pipRxRefresh(seed) { refreshCalls.push(seed === true); }',
    'function _clearMcRxLines(map) { cleared.push(map); }',
    'let _pipRxOn = false, _pipRxTimer = null, _pipRxRows = [], _pipRxStamp = null, _pipRxSeen = new Set(), _pipRxGen = 0;',
    extract('_pipMcConnected'),
    extract('_pipRxActive'),
    extract('togglePipTraces'),
    extract('_pipTraceBtnState'),
    extract('_pipTracesSync'),
    'return {',
    '  sync: _pipTracesSync, toggle: togglePipTraces, btn, store,',
    '  set: (k, v) => { ({ pipOpen: () => pipOpen = v, currentTab: () => currentTab = v, mcLastStatus: () => mcLastStatus = v })[k](); },',
    '  timers: () => liveTimers.size, refreshCalls, cleared,',
    '};',
  ].join('\n'))();

  env.sync();
  assert(env.timers() === 0, 'off by default: no polling');
  assert(env.btn.style.display === '' && env.btn.textContent === 'Traces', 'button shown (MC connected), label off');

  env.toggle();
  assert(env.store.pipMcTraces === '1', 'toggle is remembered');
  assert(env.timers() === 1 && env.refreshCalls.join() === 'true', 'on -> one poll started, first refresh is a seed');
  assert(env.btn.textContent === 'Traces ●' && env.btn.cls.has('on'), 'button shows the on state');
  env.sync();
  assert(env.timers() === 1 && env.refreshCalls.length === 1, 'sync with nothing changed starts no second poll');

  env.set('currentTab', 'map'); env.sync();
  assert(env.timers() === 0 && env.cleared.join() === 'MINI', 'leaving Nodes/Chat stops the poll and clears the minimap only');
  env.set('currentTab', 'chat'); env.sync();
  assert(env.timers() === 1, 'back on Chat: polling resumes (toggle still on)');

  env.set('pipOpen', false); env.sync();
  assert(env.timers() === 0, 'collapsed minimap never polls');
  env.set('pipOpen', true); env.sync();

  env.set('mcLastStatus', { r1: { status: 'disconnected' } }); env.sync();
  assert(env.timers() === 0 && env.btn.style.display === 'none', 'radio disconnected: poll stops, button hidden');
  env.set('mcLastStatus', { r1: { status: 'connected' } }); env.store.mcHide = '1'; env.sync();
  assert(env.timers() === 0 && env.btn.style.display === 'none', 'MC hidden in settings: no poll, no button');
  delete env.store.mcHide; env.sync();
  assert(env.timers() === 1, 'MC shown again: polling resumes');

  env.toggle();
  assert(env.store.pipMcTraces === '0' && env.timers() === 0, 'toggle off stops the poll and is remembered');
}

// ── 3b. a poll in flight across a stop+restart is discarded (S468 sweep) ────
(async () => {
  const pending = [];
  const env = new Function('pending', [
    'const BASE_PATH = ""; const MC_RX_FETCH_LIMIT = 200; const activeMcRadioId = "r1";',
    'const pipMap = "MINI"; let plays = 0;',
    'let _pipRxGen = 1, _pipRxRows = [], _pipRxStamp = null, _pipRxSeen = new Set();',
    'function _pipRxActive() { return true; }',
    'function fetch() { return new Promise(res => pending.push(res)); }',
    'function _mcRxPlay(rows, seen) { plays++; return seen; }',
    'const console = { warn() {} };',
    'async ' + extract('_pipRxRefresh'),
    'return { refresh: _pipRxRefresh, bump: () => _pipRxGen++, plays: () => plays };',
  ].join('\n'))(pending);
  const reply = rows => ({ ok: true, json: async () => rows });
  const rows = [{ ts: 1, path: 'aa', payload_type: 'ADVERT' }];

  const old = env.refresh(false);          // run 1: poll goes out
  env.bump(); env.bump();                  // minimap closed + reopened meanwhile
  pending[0](reply(rows)); await old;
  assert(env.plays() === 0, 'a poll from a stopped run plays nothing (no stale burst)');
  const cur = env.refresh(true);           // current run
  pending[1](reply(rows)); await cur;
  assert(env.plays() === 1, 'a poll from the current run still plays');
  section4();
})().catch(e => { console.error('FAIL: ' + e); process.exit(1); });

function section4() {
// ── 4. wiring ────────────────────────────────────────────────────────────────
assert(/id="pip-trace-btn"[^>]*onclick="togglePipTraces\(\)"/.test(html), 'minimap header has the Traces button wired to togglePipTraces()');
assert(/e\.target\.id === 'pip-trace-btn'/.test(src), 'pressing the Traces button does not start a minimap drag');
assert(extract('updatePipVisibility').includes('_pipTracesSync()'), 'every minimap visibility change re-syncs the poll');
assert(extract('_updateScanBtnVisibility').includes('_pipTracesSync()'), 'MC connect/disconnect re-syncs the minimap Traces');
assert(/_mcRxPlay\(_mcRxRows, _mcRxSeen, seed, leafletMap/.test(extract('_mcRxAnimateNew')), 'Sense Traces still play on the main map');

  console.log('ok: minimap MC Traces — own toggle, polls only while visible + connected, per-map clearing, Sense untouched');
}

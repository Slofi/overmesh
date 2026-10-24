// OM Position: ONE source, chosen by hand, hand-picked by default (Filip, S471) — real functions from app.js
// run against fakes.
//
// Filip: "this time I want no automation. Manual select, defaulting to a manually selected position on the map"
// and "If OPS-TOC doesn't have a fix, OM doesn't have a fix".
//
// What must hold:
//  - hand-picked source: the origin is the picked position, even while a GPS fix exists;
//  - a GPS source: the origin is its live fix, and with no fix there is NO origin (no fallback to the pick);
//  - the selector shows only the chosen source's controls, disables "share now" for hand-picked,
//    and saves only when the USER changes it (loading the page must not save).
const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

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

const make = new Function([
  'let _omPosSource = "manual", _omManualPos = null, _localGpsPos = null;',
  'let _gpsPorts = {internal: "/dev/ttyS3", usb: "/dev/ttyACM2"};',
  'const saved = [], populated = [];',
  'function saveGpsSettings() { saved.push(1); }',
  'function _gpsPopulatePortList(kind, port) { populated.push([kind, port]); }',
  'const els = {};',
  'const el = id => (els[id] = els[id] || {id, style: {}, textContent: "", value: ""});',
  'const buttons = [{disabled: false}, {disabled: false}];',
  'const document = { getElementById: el, querySelectorAll: () => buttons };',
  extract('_omLocalOrigin'),
  extract('gpsSourceChanged'),
  'return { _omLocalOrigin, gpsSourceChanged, el, buttons, saved, populated,',
  '  set: (k, v) => { eval(k + " = v"); } };',
].join('\n'));

// --- origin ---------------------------------------------------------------
let t = make();
const PICK = {lat: 46.039135, lon: 14.497419};
const FIX = {lat: 46.1, lon: 14.6};
t.set('_omManualPos', PICK);
t.set('_localGpsPos', FIX);
t.set('_omPosSource', 'manual');
assert(JSON.stringify(t._omLocalOrigin()) === JSON.stringify(PICK), 'hand-picked must win even while a GPS fix exists');
for (const s of ['internal', 'usb', 'proxy']) {
  t.set('_omPosSource', s);
  assert(JSON.stringify(t._omLocalOrigin()) === JSON.stringify(FIX), `${s}: origin must be the live fix`);
  t.set('_localGpsPos', null);
  assert(t._omLocalOrigin() === null, `${s}: no fix must mean NO origin, not the hand-picked one`);
  t.set('_localGpsPos', FIX);
}

// --- selector -------------------------------------------------------------
t = make();
const show = id => t.el(id).style.display;
t.el('gps-source-select').value = 'manual';
t.gpsSourceChanged(false);
assert(show('om-pos-manual-row') === '' && show('gps-port-row') === 'none' && show('gps-live-card') === 'none',
  'manual: only the hand-picked controls');
assert(t.buttons.every(b => b.disabled), 'manual: "share now" disabled');
assert(t.saved.length === 0, 'loading the page (save=false) must not save');

t.el('gps-source-select').value = 'usb';
t.gpsSourceChanged(true);
assert(show('om-pos-manual-row') === 'none' && show('gps-port-row') === 'flex' && show('gps-live-card') === '',
  'usb: port row + live card, no hand-picked controls');
assert(t.buttons.every(b => !b.disabled), 'usb: "share now" enabled');
assert(JSON.stringify(t.populated.at(-1)) === JSON.stringify(['usb', '/dev/ttyACM2']), 'usb: lists USB ports with its own saved port');
assert(t.saved.length === 1, 'a user change of the selector saves once');

t.el('gps-source-select').value = 'internal';
t.gpsSourceChanged(true);
assert(JSON.stringify(t.populated.at(-1)) === JSON.stringify(['internal', '/dev/ttyS3']), 'internal: its own saved UART');

t.el('gps-source-select').value = 'proxy';
t.gpsSourceChanged(true);
assert(show('gps-port-row') === 'none' && show('om-pos-manual-row') === 'none', 'proxy: no port, no pick');
assert(!/fallback/i.test(extract('gpsSourceChanged')), 'no fallback wording or logic left in the selector');

// --- save: never send a port from the OTHER kind's list (it reloads async after a switch) -------------
const makeSave = new Function([
  'let _gpsPortListKind = "internal"; const BASE_PATH = "";',
  'const sent = [];',
  'function fetch(url, opts) { sent.push(JSON.parse(opts.body)); return new Promise(() => {}); }',
  'function metersToBit() { return 32; }',
  'function escHtml(x) { return x; }',
  'function loadGpsSettings() {}',
  'const vals = {"gps-source-select": "usb", "gps-port-select": "/dev/ttyS3", "gps-precision-slider": "0",',
  '  "gps-push-interval-val": "30", "gps-push-interval-unit": "1"};',
  'const document = { getElementById: id => ({ value: vals[id], checked: false, textContent: "" }) };',
  extract('saveGpsSettings'),
  'return { saveGpsSettings, sent, vals, setKind: k => { _gpsPortListKind = k; } };',
].join('\n'));
const sv = makeSave();
sv.saveGpsSettings();
assert(sv.sent[0].source === 'usb' && sv.sent[0].port === '', 'switching to USB while the UART list is on screen must not send /dev/ttyS3');
sv.setKind('usb'); sv.vals['gps-port-select'] = '/dev/ttyACM2';
sv.saveGpsSettings();
assert(sv.sent[1].port === '/dev/ttyACM2', 'a port picked from the USB list is sent');
assert(!('enabled' in sv.sent[1]), 'the new page never sends `enabled` (that marks an old page)');

// --- distances: no GPS fix -> the hand-picked OM Position, before the radio's own position --------------
const makeDist = new Function([
  'let _omPosSource = "usb", _omManualPos = null, _localGpsPos = null;',
  'const allNodes = [{is_local: true, radio_id: "r1", latitude: 45.0, longitude: 13.0}];',
  'const mcLastStatus = {r1: {status: "connected", lat: 45.5, lon: 13.5}}; const activeMcRadioId = "r1";',
  extract('_omLocalOrigin'), extract('_omDistanceOrigin'), extract('_mtLocalPosition'), extract('_mcLocalPosition'),
  'return { _mtLocalPosition, _mcLocalPosition, _omLocalOrigin, set: (k, v) => { eval(k + " = v"); } };',
].join('\n'));
const d = makeDist();
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
assert(eq(d._mtLocalPosition('r1'), {lat: 45.0, lon: 13.0}), 'nothing set: MT falls back to the radio position');
d.set('_omManualPos', PICK);
assert(eq(d._mtLocalPosition('r1'), PICK), 'GPS source, no fix: MT distances from the hand-picked position, not the radio');
assert(eq(d._mcLocalPosition('r1'), PICK), 'GPS source, no fix: MC distances from the hand-picked position, not the radio');
assert(d._omLocalOrigin() === null, '...while the strict origin (centring, +GPS) stays empty');
d.set('_localGpsPos', FIX);
assert(eq(d._mtLocalPosition('r1'), FIX) && eq(d._mcLocalPosition('r1'), FIX), 'with a fix: distances from the fix');

console.log('OK test_om_position_source');

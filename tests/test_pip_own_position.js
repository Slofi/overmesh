// Minimap own position: real functions from app.js run against fakes.
//
// Filip: "on minimap: my/CD/node location is not shown!" The minimap only drew
// MT nodes and MC contacts, never the connected MC radio's own position, and it
// had no OM Position at all. The fix resolves our own positions in ONE shared
// list (_omOwnPositions) and draws it with the icons the main map uses.
//
// What must hold:
//  - the OM Position is listed when it is hand-picked, and when a GPS source
//    has a live fix; a GPS source with no fix lists NOTHING (no fallback, no
//    stale point, no guess);
//  - a connected MC radio contributes its own position; a disconnected one
//    does not, and an empty 0,0 coordinate is not a place;
//  - the MT local node contributes its radio position;
//  - the same spot is listed once;
//  - the minimap draws that list through the shared markers, the main map uses
//    the same MC radio icon, and Locate uses the same list.
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

const env = new Function([
  'let _omPosSource = "manual", _omManualPos = null, _localGpsPos = null;',
  'let mcLastStatus = {}, allNodes = [];',
  extract('_omLocalOrigin'),
  extract('_omOwnPositions'),
  'return { _omOwnPositions, set: (k, v) => { eval(k + " = v"); } };',
].join('\n'))();

const PICK = {lat: 46.039135, lon: 14.497419};
const FIX  = {lat: 46.1, lon: 14.6};

// --- nothing known ---------------------------------------------------------
assert(env._omOwnPositions().length === 0, 'nothing known: no marker');

// --- OM Position, hand-picked ---------------------------------------------
env.set('_omManualPos', PICK);
let own = env._omOwnPositions();
assert(own.length === 1 && own[0].kind === 'om', 'hand-picked: one OM marker');
assert(own[0].lat === PICK.lat && own[0].lon === PICK.lon, 'hand-picked: the exact picked point');
assert(own[0].label === 'OM Position', 'hand-picked: labelled OM Position');

// --- OM Position, GPS source ----------------------------------------------
env.set('_omPosSource', 'usb');
env.set('_localGpsPos', null);
assert(env._omOwnPositions().length === 0,
  'GPS source with no fix: NO marker, never the hand-picked fallback');
env.set('_localGpsPos', FIX);
own = env._omOwnPositions();
assert(own.length === 1 && own[0].lat === FIX.lat && own[0].lon === FIX.lon,
  'GPS source with a fix: the live fix');

// --- MC radio's own position ----------------------------------------------
env.set('_omPosSource', 'manual');
env.set('_omManualPos', null);
env.set('_localGpsPos', null);
env.set('mcLastStatus', {
  r1: {status: 'connected', name: 'CD-MC', lat: 45.5, lon: 13.5},
  r2: {status: 'disconnected', name: 'off', lat: 45.9, lon: 13.9},
  r3: {status: 'connected', name: 'empty', lat: 0, lon: 0},
});
own = env._omOwnPositions();
assert(own.length === 1 && own[0].kind === 'mc-radio', 'only the connected MC radio with a real position');
assert(own[0].label === 'CD-MC' && own[0].lat === 45.5, 'MC marker carries the radio name and position');

// --- MT local node ---------------------------------------------------------
env.set('mcLastStatus', {});
env.set('allNodes', [
  {is_local: true,  id: 'n1', short_name: 'CD-MT', latitude: 45.0, longitude: 13.0},
  {is_local: false, id: 'n2', short_name: 'remote', latitude: 44.0, longitude: 12.0},
  {is_local: true,  id: 'n3', short_name: 'no fix', latitude: null, longitude: null},
]);
own = env._omOwnPositions();
assert(own.length === 1 && own[0].kind === 'mt-local', 'only OUR local MT node, not remote nodes and not a null fix');
assert(own[0].label === 'CD-MT' && own[0].lat === 45.0, 'MT marker carries the local node name and position');

// --- one marker per spot ---------------------------------------------------
env.set('_omManualPos', PICK);
env.set('mcLastStatus', {r1: {status: 'connected', name: 'CD-MC', lat: PICK.lat, lon: PICK.lon}});
env.set('allNodes', [{is_local: true, id: 'n1', short_name: 'CD-MT', latitude: PICK.lat, longitude: PICK.lon}]);
own = env._omOwnPositions();
assert(own.length === 1 && own[0].kind === 'om', 'the same spot is listed once (OM Position wins)');

// --- wiring: minimap uses the shared list and the shared icons -------------
const draw = extract('_pipDrawOwnMarkers');
assert(draw.includes('_omOwnPositions()'), 'the minimap draws the shared own-position list');
assert(draw.includes("p.kind === 'mc-radio' ? _mcRadioIcon() : makeIcon('#3b82f6', true)"),
  'the minimap uses the main map marker builders');
assert(draw.includes('setPointerCapture') === false, 'sanity: marker code is not the resize code');
const update = extract('updatePipMarkers');
assert(update.includes('_pipDrawOwnMarkers()') && update.includes('own.keys.forEach(k => seen.add(k))'),
  'own markers are drawn and kept alive by the same seen-set as every other marker');
assert(update.includes('ownAt(lat, lon)'), 'contact markers skip a second marker on our own spot');
const locate = extract('pipLocateMe');
assert(locate.includes('_omOwnPositions()') && !locate.includes('_mtLocalPosition'),
  'Locate uses the same own-position list, so it works on MC too');
const mainMc = extract('renderMcMapMarkers');
assert(mainMc.includes('_mcRadioIcon()') && !mainMc.includes('transform="rotate(45,15,15)"'),
  'the main MC map uses the shared radio icon, no second copy');

// --- live updates: our own marker must not wait for the 15 s loadLive poll ---
// The SSE fast path moves the main map immediately. The minimap draws the same
// own position from the same list, so it must be refreshed on the same events;
// otherwise it shows the previous spot as current until the next poll.
assert(/data\.type === 'gps_position'[\s\S]{0,260}updatePipMarkers\(allNodes\)/.test(src),
  'a GPS fix updates the minimap own marker immediately');
assert(/data\.type === 'gps_error'[\s\S]{0,300}updatePipMarkers\(allNodes\)/.test(src),
  'losing the GPS fix removes the stale own marker from the minimap');
assert(/data\.type === 'local_node_position'[\s\S]{0,600}updatePipMarkers\(allNodes\)/.test(src),
  'a local node position push updates the minimap own marker immediately');

console.log('OK test_pip_own_position');

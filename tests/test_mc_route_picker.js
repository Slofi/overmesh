// MC route editor: hop matching and duplicate prevention (S479).
//
// The editor is now fed from three places (the map picker, the search list and
// the stored route the radio reports), and those hand it prefixes of different
// lengths: stored hops are 2-byte hashes, map/search adds pass a full key. The
// add path must recognise them as the same repeater, otherwise the same relay
// ends up in the route twice and the saved path is wrong.
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

const RFC1 = {
  id: '86a8eedb744b', full_key: '86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6',
  long_name: 'SI-LJ-Trnovo II', type: 2,
};
const RFC2 = {
  id: 'ead8e0aed065', full_key: 'ead8e0aed065aabbccddeeff00112233445566778899aabbccddeeff00112233',
  long_name: 'SI-LJ-Askerceva', type: 2,
};

const code = [
  extract('_mcRouteSelectedIndex'),
  extract('mcRouteAddHop'),
  extract('mcRouteRemoveHop'),
  extract('mcRoutePickRepeater'),
  extract('mcRouteClearPick'),
  `function findMcContactByKeyPrefix(prefix) {
     const p = String(prefix || '').toLowerCase();
     return [RFC1, RFC2].find(c => c.id.startsWith(p) || c.full_key.startsWith(p)) || null;
   }`,
  'function _mcRouteRefresh() {}',
  'function _mcRouteSyncMapMarkers() {}',
  'function _mcRouteRenderPickInfo() {}',
  'return { index: _mcRouteSelectedIndex, add: mcRouteAddHop, remove: mcRouteRemoveHop, pick: mcRoutePickRepeater, clear: mcRouteClearPick, state: () => _mcRouteEditor, setState: (v) => { _mcRouteEditor = v; } };',
].join('\n');

const api = new Function('RFC1', 'RFC2', 'let _mcRouteEditor = null;' + code + '\n//# sourceURL=route-picker-test.js')(RFC1, RFC2);

// --- matching: short stored hash vs full key, both directions
api.setState({ radioId: 'r1', selected: ['86a8'] });
assert(api.index('86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6') === 0,
  'a full key must match the short stored hash');
assert(api.index('86a8') === 0, 'a short prefix must match itself');
assert(api.index('86a8eedb') === 0, 'a longer prefix must match the short stored hash');

api.setState({ radioId: 'r1', selected: [RFC1.full_key] });
assert(api.index('86a8') === 0, 'a short prefix must match a stored full key');
assert(api.index('ead8') === -1, 'a different repeater must not match');
assert(api.index('') === -1, 'an empty prefix matches nothing');
assert(api.index('ffff') === -1, 'an unknown hash matches nothing');

// --- adding the same relay twice (map click + search click) must not duplicate
api.setState({ radioId: 'r1', selected: [] });
api.add('86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6');
api.add('86a8');           // short form of the same repeater
api.add('86a8eedb');
assert(api.state().selected.length === 1, `the same relay must be added once, got ${api.state().selected.length}`);

// --- a second, different relay is appended in order
api.add('ead8e0aed065');
assert(api.state().selected.length === 2, 'a different relay must be added');
assert(api.state().selected[1] === 'ead8e0aed065', 'hops keep the order they were added in');

// --- and removal takes out the right one, leaving the order of the rest
api.remove(0);
assert(api.state().selected.length === 1, 'remove drops exactly one hop');
assert(api.state().selected[0] === 'ead8e0aed065', 'remove keeps the remaining hop');

// --- clicking a relay on the map toggles it: in the route -> out of it
api.setState({ radioId: 'r1', selected: ['86a8'], pickTarget: '86a8eedb744b' });
api.pick('86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6');
assert(api.state().selected.length === 0, 'clicking a selected repeater must remove it from the path');
assert(api.state().pickTarget === null, 'and it must stop being the previewed repeater');

// --- clicking a relay that is NOT in the route only previews it (Add stays a button)
api.setState({ radioId: 'r1', selected: [], pickTarget: null });
api.pick('86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6');
assert(api.state().selected.length === 0, 'a click on a new repeater must not add it by itself');
assert(api.state().pickTarget === '86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6',
  'a click on a new repeater must preview it');

// --- clicking empty map un-selects what the map selected: a just-added relay comes back out
api.setState({ radioId: 'r1', selected: ['86a8'], pickTarget: '86a8eedb744b', pickSource: 'map' });
api.clear();
assert(api.state().selected.length === 0, 'empty-map click must take a map-picked relay out of the path');
assert(api.state().pickTarget === null, 'empty-map click must clear the preview');

// --- a relay added from the SEARCH LIST is not removed by a map click
api.setState({ radioId: 'r1', selected: ['86a8'], pickTarget: '86a8eedb744b', pickSource: 'list' });
api.clear();
assert(api.state().selected.length === 1, 'a hop added from the search list must survive a map click');
assert(api.state().pickTarget === null, 'the preview is still cleared');

// --- ...nor when the previewed relay is not in the route at all
api.setState({ radioId: 'r1', selected: ['ead8'], pickTarget: '86a8eedb744b', pickSource: 'map' });
api.clear();
assert(api.state().selected.length === 1 && api.state().selected[0] === 'ead8',
  'empty-map click must not remove other hops');

// --- and with nothing previewed it is a no-op
api.setState({ radioId: 'r1', selected: ['ead8'], pickTarget: null, pickSource: null });
api.clear();
assert(api.state().selected.length === 1, 'an empty-map click with no preview changes nothing');

// --- the source is recorded: map pick vs list add
api.setState({ radioId: 'r1', selected: [], pickTarget: null, pickSource: null });
api.pick('86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6');
assert(api.state().pickSource === 'map', 'a marker click marks the pick as a map pick');
api.add('ead8e0aed065');
assert(api.state().pickSource === 'list', 'an add without a source is a list add');

console.log('ok: route hop matching, duplicate prevention and pick toggling');

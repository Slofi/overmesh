// MC activity pop-up: the alternate-copy pin and its status line (EST-325).
//
// Two defects in a3dac5b, both from keeping an index where an identity belongs:
//  1. _mcAltPathPinned held { entryIdx, copyIdx }. Every arrival is unshifted
//     onto the activity log and the open detail panel is not re-rendered, so
//     after one new packet the pin (and every inline handler in the panel)
//     pointed one message too far: a tap drew the wrong message's path.
//  2. _mcShowAltCopyPath only ever set the status line of the copy it drew. A
//     hover left "Shown on map" on a block that was no longer on the map, so a
//     pinned copy and a merely hovered copy both claimed the map.
//
// This test runs the real handlers against a fake document and a fake log.
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

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  assert(start >= 0, `${name}() not found in app.js`);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced ' + name);
}

const code = [
  'let _mcSenseLogEntries = [];',
  'let _mcAltPathPinned = null;',
  'let _mcEntryUidSeq = 0;',
  'let leafletMap = {};',
  'const L = {};',
  'const drawn = [];',
  'function showMcHoverPath(p, on) { drawn.push(p ? (p.tag || "path") : null); }',
  'function _mcAltCopyPathResult(entry, copy) {',
  '  return { points: [[45, 14], [46, 15]], hops: [], unresolvedHops: 0, tag: entry.name + "/" + copy.path };',
  '}',
  extract('_mcRxCopyKey'),
  extract('_mcAltPathCopies'),
  extract('_mcEntryUid'),
  extract('_mcEntryUidFromEvent'),
  extract('_mcAltEntryArg'),
  extract('_mcAltPinIndex'),
  extract('_mcAltCopyStatusText'),
  extract('_mcAltCopyNoPathText'),
  extract('_mcAltCopyStatusEl'),
  extract('_mcSetAltCopyStatus'),
  extract('_mcClearAltCopyStatuses'),
  extract('_mcSetAltCopyActive'),
  extract('_mcShowAltCopyPath'),
  extract('_mcAltCopyHover'),
  extract('_mcAltCopyClick'),
  `return {
     setEntries(list) { _mcSenseLogEntries = list; },
     entries() { return _mcSenseLogEntries; },
     pin() { return _mcAltPathPinned; },
     pinIndex: _mcAltPinIndex,
     uid: _mcEntryUid,
     hover: _mcAltCopyHover,
     click: _mcAltCopyClick,
     drawn,
   };`,
].join('\n');

// Fake DOM: one status span per copy block, looked up by its real id.
const els = {};
function statusEl(id) {
  if (!els[id]) els[id] = { id, cls: 'status', textContent: '' };
  return els[id];
}
const fakeDocument = {
  getElementById: id => els[id] || null,
  querySelectorAll: sel => (sel === '.mc-alt-copy-status'
    ? Object.values(els).filter(el => el.cls === 'status')
    : []),
  querySelector: () => null,
};

const api = new Function('document', code)(fakeDocument);

function evWithUid(uid) {
  return { currentTarget: { dataset: { entryUid: uid } }, stopPropagation() {} };
}

const primaryA = { path: 'aa', path_len: 1, path_hash_size: 1 };
const altA0 = { path: 'bb', path_len: 1, path_hash_size: 1 };
const altA1 = { path: 'cc', path_len: 1, path_hash_size: 1 };
const A = { name: 'msg A', kind: 'message', radioId: 'r1', path: 'aa', path_len: 1, path_hash_size: 1, rx_copies: [primaryA, altA0, altA1] };
const B = { name: 'msg B', kind: 'message', radioId: 'r1' };
const C = { name: 'msg C', kind: 'message', radioId: 'r1' };

const uidA = api.uid(A);
statusEl('mc-alt-copy-status-0-0');
statusEl('mc-alt-copy-status-0-1');
api.setEntries([A, B]);

// --- one block at a time may claim the map
api.hover(evWithUid(uidA), 0, 0, true);
assert(/Shown on map/.test(statusEl('mc-alt-copy-status-0-0').textContent),
  'hovering a copy must say it is shown');
api.hover(evWithUid(uidA), 0, 1, true);
assert(statusEl('mc-alt-copy-status-0-0').textContent === '',
  'the block the pointer left must stop claiming the map: ' + statusEl('mc-alt-copy-status-0-0').textContent);
assert(/Shown on map/.test(statusEl('mc-alt-copy-status-0-1').textContent),
  'the hovered block must say it is shown');

// --- a pin survives the list shifting under it (a new packet is unshifted)
api.click(evWithUid(uidA), 0, 1);
assert(api.pin() && api.pin().entry === A, 'the pin must hold the entry itself, not an index');
api.setEntries([C, A, B]);
assert(api.pinIndex() === 1, `the pin index must follow the entry after a shift, got ${api.pinIndex()}`);
assert(api.entries()[api.pinIndex()] === A, 'the pin must still mean message A');

// --- a control stamped before the shift still means A, not the new neighbour
api.setEntries([C, A, B]);
const drawnBefore = api.drawn.length;
api.hover(evWithUid(uidA), 0, 0, true);  // stale index 0 now points at C
assert(api.drawn.length > drawnBefore, 'the hover must draw something');
assert(api.drawn[api.drawn.length - 1] === 'msg A/bb',
  `a stale index must resolve by uid to message A, drew ${api.drawn[api.drawn.length - 1]}`);
assert(/Shown on map/.test(statusEl('mc-alt-copy-status-0-0').textContent),
  'the status must be written to A copy 0');
assert(! els['mc-alt-copy-status-0-0-c'] , 'no status may be written for a block that does not exist');

// --- leaving a hover with a pin restores the pin and clears the hovered line
assert(api.pin() && api.pin().entry === A, 'A copy 1 is still pinned');
api.hover(evWithUid(uidA), 0, 0, false);
assert(api.drawn[api.drawn.length - 1] === 'msg A/cc',
  `leaving must restore the pinned copy, drew ${api.drawn[api.drawn.length - 1]}`);
assert(statusEl('mc-alt-copy-status-0-0').textContent === '',
  'the block the pointer left must not keep a status line');

console.log('ok: alternate-copy pin follows the message, and only the drawn block claims the map');

// S504: a clicked alternate copy must draw SOMETHING whenever the data allows it.
//
// Filip: "now alternative paths are not drawn on the map at all, when clicked
// on. I am looking at a MSG with 4 alt paths, and the main one is drawn, but the
// alt paths, even when I click on them, are not drawn."
//
// Cause: the main path comes from the entry's stored result and needs no
// endpoints, while a copy refused to draw unless BOTH the sender position and
// our own radio position were known. A copy now falls back to the relay chain
// it does know, labelled as such, and a click that can draw nothing no longer
// leaves a silent pin behind.
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

// Contacts the fake resolver knows, keyed by 4-char hop hash.
const KNOWN = {
  ead8: { id: 'ead8', long_name: 'SI-LJ Askerceva', latitude: 46.05, longitude: 14.51 },
  c2aa: { id: 'c2aa', long_name: 'SI-LJ-Dunajska5-RPT', latitude: 46.06, longitude: 14.52 },
  '9da4': { id: '9da4', long_name: 'SI-Lavrica', latitude: 46.00, longitude: 14.60 },
};

function build(opts) {
  const body = [
    'const KNOWN = ' + JSON.stringify(KNOWN) + ';',
    'function _mcSenseRadioPos() { return ' + JSON.stringify(opts.radioPos || [46.04, 14.50]) + '; }',
    'function decodeMcEventPath() { return ' + JSON.stringify(opts.decode || null) + '; }',
    'function _mcReversePathResult(p) { return p; }',
    'function findMcContactByKeyPrefix(hash) { return KNOWN[hash] || null; }',
    'function _mcPickPathHopResolution(hash) { return { contact: KNOWN[hash] || null }; }',
    extract('_mcAltCopyRelayChain'),
    extract('_mcAltCopyPathResult'),
    'return { copyPath: _mcAltCopyPathResult };',
  ].join('\n');
  return new Function(body)();
}

const COPY = { path: 'ead8c2aa9da4', path_len: 3, path_hash_size: 2, path_hash_mode: 1 };

// 1. Sender position unknown: the relay chain is drawn from the repeaters.
const noSender = build({ radioPos: [46.04, 14.50] });
const chain = noSender.copyPath({ radioId: 'r1', lat: null, lon: null }, COPY);
assert(chain, 'a copy with no sender position must still draw its relay chain');
assert(chain.relayChainOnly === true, 'the relay-chain result must say so');
assert(chain.points.length === 3, `expected 3 relay points, got ${chain.points.length}`);
assert(/repeaters|relay chain/i.test(chain.warningText || ''), 'the chain must be labelled honestly');

// 2. Every endpoint known: the normal decoded path is used, not the chain.
const full = build({
  radioPos: [46.04, 14.50],
  decode: { points: [[46.1, 14.6], [46.05, 14.51], [46.04, 14.50]], hops: [], gapAfterPoints: [] },
});
const decoded = full.copyPath({ radioId: 'r1', lat: 46.1, lon: 14.6 }, COPY);
assert(decoded && decoded.points.length === 3, 'a fully known copy uses the decoded path');
assert(!decoded.relayChainOnly, 'a decoded path must not be marked as a relay chain');

// 3. Nothing positioned at all: still null, and the caller must say why.
const nothing = build({ radioPos: [46.04, 14.50] });
const none = nothing.copyPath({ radioId: 'r1', lat: null, lon: null }, { path: 'ffff', path_len: 1, path_hash_size: 2 });
assert(none === null, 'a copy with no positioned hop and no endpoints stays undrawable');

// 4. A single positioned hop is not a line.
const oneHop = nothing.copyPath({ radioId: 'r1', lat: null, lon: null }, { path: 'ead8', path_len: 1, path_hash_size: 2 });
assert(oneHop === null, 'one point is not a path');

// 5. A click that cannot draw must not leave a silent pin behind.
const click = extract('_mcAltCopyClick');
assert(click.includes('if (!pathResult) {'), 'the click must handle "nothing drawn"');
assert(click.includes('_mcAltPathPinned = null;'), 'a failed draw must clear the pin');
assert(click.includes("_mcSetAltCopyActive(here.domIdx, -1)"),
  'a failed draw must leave the main path marked as the selection');

// 6. The status line says which kind of line is on the map.
const status = extract('_mcAltCopyStatusText');
assert(status.includes('relayChainOnly'), 'the status text must mention the relay-chain case');
const noPath = extract('_mcAltCopyNoPathText');
assert(noPath.includes('repeaters'), 'the no-path text must explain what was missing');

console.log('ok: alternate copies draw what is known, and a failed click says so');

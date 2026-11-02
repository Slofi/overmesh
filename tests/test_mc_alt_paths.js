// MC activity pop-up: the alternate received copies of one message (S479).
//
// A relayed MC packet often reaches us more than once, on different paths. The
// pop-up always showed the best copy and only counted the rest ("3 alternate
// paths") with no way to see them. This test runs the real renderers against a
// fake entry and checks:
//  - the primary copy is never listed as an alternate (it is shown above);
//  - every alternate is rendered as its own block, hops split at the copy's own
//    hash size, with resolved names and a confidence note where there is one;
//  - the copy header carries hops, hash size, RSSI, SNR and time;
//  - a single heard copy produces no block at all (the normal case);
//  - a contact name cannot inject markup;
//  - the old tooltip text on the "Other copies" count is unchanged.
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

// Contact names the fake resolver knows, keyed by hop hash.
const NAMES = {
  '86a8': 'SI-LJ-Trnovo II',
  'ead8': '<img src=x onerror=alert(1)>',
};

const code = [
  extract('_mcRxCopyKey'),
  extract('_mcAltPathCopies'),
  extract('_mcAltPathMeta'),
  extract('_mcAltCopyHopsHtml'),
  extract('_mcAltPathsHtml'),
  extract('mcPathHopLabel'),
  extract('_mcHopConfidenceLabel'),
  extract('escHtml'),
  // helpers mirrored from app.js (the real ones need the live map/contacts)
  'function _mcSenseRadioPos() { return [46.0409, 14.4981]; }',
  `function _mcPickPathHopResolution(hash) {
     if (hash === '86a8') return { contact: { long_name: 'SI-LJ-Trnovo II', id: '86a8eedb' }, confidence: 'ambiguous' };
     return null;
   }`,
  'function findMcContactByKeyPrefix(hash) { return NAMES[hash] ? { long_name: NAMES[hash], id: hash } : null; }',
  'function _formatAppTime(ts) { return "T" + ts; }',
  'return { copies: _mcAltPathCopies, meta: _mcAltPathMeta, html: _mcAltPathsHtml, hops: _mcAltCopyHopsHtml };',
].join('\n');

const api = new Function('NAMES', code)(NAMES);

const primary = {
  path: '86a8ead8', path_len: 2, path_hash_size: 2, rx_rssi: -88, rx_snr: 5.5,
  first_seen: 1700000000, route_type: 'ROUTE_DIRECT',
};
const copyA = {
  path: '86a8ba90', path_len: 2, path_hash_size: 2, rx_rssi: -97, rx_snr: 2.0,
  first_seen: 1700000001, route_type: 'ROUTE_FLOOD',
};
const copyB = {
  path: 'ead8ba90', path_len: 2, path_hash_size: 2, rx_rssi: -101, rx_snr: -1.5,
  first_seen: 1700000005, route_type: 'ROUTE_FLOOD',
};

const entry = {
  kind: 'message', radioId: 'mc1', lat: 46.05, lon: 14.50,
  path: primary.path, path_len: primary.path_len, path_hash_size: primary.path_hash_size,
  rx_rssi: primary.rx_rssi, rx_snr: primary.rx_snr,
  rx_copies: [primary, copyA, copyB],
};

// --- the primary is not an alternate
const alternates = api.copies(entry);
assert(alternates.length === 2, `expected 2 alternates, got ${alternates.length}`);
assert(!alternates.some(c => c.path === primary.path), 'the shown copy must not be listed as an alternate');

// --- one heard copy means no UI at all
assert(api.copies({ rx_copies: [primary], path: primary.path, path_len: 2, path_hash_size: 2, rx_rssi: -88, rx_snr: 5.5 }).length === 0,
  'a single copy must produce no alternates');
assert(api.html({ rx_copies: [primary], kind: 'message' }, 0) === '',
  'a single copy must produce no alternate-path block');
assert(api.meta({ rx_copies: [primary], path: primary.path, path_len: 2, path_hash_size: 2, rx_rssi: -88, rx_snr: 5.5 }) === null,
  'a single copy must produce no "Other copies" row');

// --- a duplicate of the primary in the list is not an alternate either
const withDup = { ...entry, rx_copies: [primary, { ...primary }, copyA] };
assert(api.copies(withDup).length === 1, 'an identical copy of the primary must be filtered out');

// --- the block itself
const html = api.html(entry, 7);
assert(html.includes('id="mc-alt-paths-7"'), 'the block needs a stable id the expand control can open');
assert(html.includes('Alternate paths (2)'), 'the summary must state how many alternates there are');
assert(html.includes('Copy 2 of 3'), 'the first alternate must be labelled copy 2 of 3');
assert(html.includes('Copy 3 of 3'), 'the second alternate must be labelled copy 3 of 3');
assert(html.includes('2 hops'), 'the copy header must state the hop count');
assert(html.includes('2B/hop'), 'the copy header must state the hash size');
assert(html.includes('RSSI -97'), 'the copy header must carry the copy RSSI');
assert(html.includes('SNR 2.0 dB'), 'the copy header must carry the copy SNR');
assert(html.includes('ROUTE_FLOOD'), 'the copy header must carry the route type');
assert(html.includes('T1700000001'), 'the copy header must carry when the copy arrived');
assert(!html.includes('RSSI -88'), 'the primary signal must not appear in the alternate list');

// --- hops are split at the copy's own hash size and named where possible
assert(html.includes('86a8'), 'hop hash must be shown');
assert(html.includes('SI-LJ-Trnovo II'), 'a resolved hop must be named, not just hashed');
assert(html.includes('ambiguous'), 'a non-exact hop match must keep its confidence note');
assert(html.includes('ba90'), 'an unresolved hop must still show its hash');
assert(html.includes('hop 1') && html.includes('hop 2'), 'hops must be numbered');

// --- a contact name cannot inject markup
assert(!html.includes('<img src=x'), 'contact names must be escaped');
assert(html.includes('&lt;img'), 'the escaped form must be shown instead');

// --- 3-byte hashes split every 6 hex chars
const wide = api.hops({ path: 'aabbccddeeff', path_len: 2, path_hash_size: 3 }, { radioId: 'mc1' });
assert(wide.includes('aabbcc') && wide.includes('ddeeff'), '3B hashes must be split every 6 hex chars');

// --- the tooltip contract on the count is unchanged
const meta = api.meta(entry);
assert(meta.count === 2, 'meta.count must stay 2');
assert(/RSSI -97/.test(meta.detail) && /SNR 2/.test(meta.detail) && /2B/.test(meta.detail),
  'the hover text must keep hops, RSSI, SNR and hash size');

console.log('ok: alternate received copies render with their own hops, signals and labels');

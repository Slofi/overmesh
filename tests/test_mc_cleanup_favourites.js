const fs = require('fs');

const src = fs.readFileSync('/home/slofi/overmesh/static/js/app.js', 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// Extract the pure helper from app.js and evaluate it, so the behaviour is
// actually exercised (not just grepped for).
const m = src.match(/function _mcFilterFavourites\(nodes, favs\) \{[\s\S]*?\n  \}/);
assert(m, '_mcFilterFavourites() must exist in app.js');
const _mcFilterFavourites = eval('(' + m[0] + ')');

const favs = { 'aaa111': true, 'bbb222': true };

const nodes = [
  { id: 'aaa111', network: 'mc', long_name: 'EDC-3' },          // favourite  -> kept out
  { id: 'ccc333', network: 'mc', long_name: 'ERA-2' },          // stale MC    -> listed
  { id: 'bbb222', network: 'mc', long_name: 'Krvavec RPTR' },   // favourite  -> kept out
  { id: 'node_1', network: 'mt', long_name: 'EDC-2' },          // MT row      -> listed (filter is MC-only)
];

let r = _mcFilterFavourites(nodes, favs);
assert(r.kept.length === 2, 'both favourites must be kept out of the list (got ' + r.kept.length + ')');
assert(r.nodes.length === 2, 'non-favourites must stay listed (got ' + r.nodes.length + ')');
assert(r.nodes.some(n => n.id === 'ccc333'), 'the non-favourite MC contact must be listed');
assert(r.nodes.some(n => n.network === 'mt'), 'MT rows must pass through untouched');
assert(!r.nodes.some(n => n.id === 'aaa111' || n.id === 'bbb222'), 'no favourite may be listed');

// every candidate a favourite -> nothing to delete
r = _mcFilterFavourites([{ id: 'aaa111', network: 'mc' }], favs);
assert(r.nodes.length === 0 && r.kept.length === 1, 'all-favourites case must list nothing and report 1 kept');

// defensive cases
assert(_mcFilterFavourites([], favs).nodes.length === 0, 'empty input');
assert(_mcFilterFavourites(null, favs).nodes.length === 0, 'null input must not throw');
assert(_mcFilterFavourites([{ id: 'ccc333', network: 'mc' }], null).nodes.length === 1,
       'missing favourite store must not hide contacts');
assert(_mcFilterFavourites([{ id: 'ccc333', network: 'mc' }], {}).nodes.length === 1,
       'empty favourite store must not hide contacts');

// the delete path must gate again, and the modal must report the kept count
assert(/\.filter\(c => !\(c\.network === 'mc' && mcFavs\[c\.id\]\)\)/.test(src),
       'commitNodeCleanup must refuse to delete a favourite');
assert(/openNodeCleanupModal\(filtered\.nodes, days, filtered\.kept\.length\)/.test(src),
       'the cleanup flow must pass the kept-favourite count to the modal');
assert(/favourite\$\{keptFavs === 1 \? '' : 's'\} kept and never listed/.test(src),
       'the modal must tell the user how many favourites were kept');

// Clearing ALL contacts is a different, explicit action — it must say that stars
// are not spared (it cannot know them server-side either).
assert(/starred \(favourite\) contacts are <b>not<\/b> spared/.test(src),
       'Clear all contacts must warn that favourites are not spared by that button');

console.log('ok: MC cleanup leaves favourites alone');

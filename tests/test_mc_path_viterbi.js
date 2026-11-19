// MC path resolution: the whole route decides, not each hop alone (S514, EST-460).
//
// A 2-byte path hash can name two repeaters 150 km apart (86a8: SI-LJ-Trnovo II
// in Ljubljana and Dioniso near Pordenone). Ranking each hop on its own against
// a straight line radio -> sender picks the wrong one when the real route curves,
// and the old code never checked whether two consecutive hops were within radio
// range at all. This test runs the real decoder in app.js and checks:
//  - the measured 5-hop Radovljica route resolves to the same repeaters, in the
//    same order, with its real legs (5.4 / 41.1 / 25.7 / 62.1 km);
//  - a 3-hop path ending in 86a8 next to our radio picks Ljubljana;
//  - a mid-path collision is settled by continuity, not by the straight line;
//  - with no positions anywhere the old per-hop behaviour is untouched;
//  - a 1-byte path with many candidates keeps the ambiguous/estimated label.
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

// The whole geometry/resolution block, so the test exercises the same code the
// page does (old resolver included, for the fallback case).
const chunkStart = src.indexOf('  const MC_PATH_SOFT_DETOUR_KM');
const chunkEnd = src.indexOf('  function _mcPathHopMeta(');
assert(chunkStart > 0 && chunkEnd > chunkStart, 'MC path helper block not found');
const helpers = src.slice(chunkStart, chunkEnd);

const CONTACTS = {};
function add(id, name, lat, lon, seen = 1700000000, type = 2) {
  CONTACTS[id] = { id, long_name: name, latitude: lat, longitude: lon, type, last_seen_ts: seen };
}

// The measured route: every hash matches exactly one archived contact.
add('cebe', 'SI-KR-Radolca', 46.3517, 14.1723);
add('ea61', 'SI-Dobrca', 46.3770, 14.2316);
add('c2aa', 'SI-LJ-Dunajska5', 46.0595, 14.5061);
add('66df', 'SI-Jost', 46.2429, 14.3038);
add('1cce', 'SI-Mrzlica', 46.1874, 15.1074);

// 86a8 collision. Italy advertises more recently on purpose.
add('86a8', 'SI-LJ-Trnovo II', 46.042, 14.505, 1600000000);
add('86a8it', 'Dioniso', 46.081, 12.522, 1710000000);
CONTACTS['86a8it'].id = '86a8it';

// 3-hop route ending at 86a8 next to our radio.
add('aa11', 'RPTR-A', 46.30, 14.40);
add('bb22', 'RPTR-B', 46.20, 14.45);

// Mid-path collision: the real route curves north, the decoy sits on the
// straight radio -> sender line.
add('77cc', 'RPTR-True-North', 46.45, 14.75);
add('77ccd', 'RPTR-Decoy-Chord', 46.05, 14.75);
CONTACTS['77ccd'].id = '77ccd';
add('cc33', 'RPTR-North-West', 46.40, 14.60);
add('dd44', 'RPTR-North-East', 46.40, 14.90);

// 1-byte path with several positioned candidates close together.
add('ee55a', 'RPTR-One', 46.20, 14.45);
add('ee55b', 'RPTR-Two', 46.21, 14.46);

const NOPOS = {
  aa11: { id: 'aa11', long_name: 'RPTR-A', type: 2 },
  bb22: { id: 'bb22', long_name: 'RPTR-B', type: 2 },
};

function contactsFor(hash) {
  const h = String(hash || '').toLowerCase();
  return Object.values(CONTACTS).filter(c => String(c.id).toLowerCase().startsWith(h));
}

const code = [
  // real map maths the helpers expect
  `function _haversineMeters(lat1, lon1, lat2, lon2) {
     const R = 6371000;
     const p1 = lat1 * Math.PI / 180, p2 = lat2 * Math.PI / 180;
     const dp = (lat2 - lat1) * Math.PI / 180, dl = (lon2 - lon1) * Math.PI / 180;
     const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
     return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
   }`,
  extract('_mcPathDistanceScore'),
  extract('_mcPathGeoKm'),
  helpers,
  extract('decodeMcEventPath'),
  extract('_mcExplicitPathHashSize'),
  extract('_mcPathHopMeta'),
  'function _mcContactSeenTs(c) { return c.last_seen_ts || 0; }',
  'function _mcDecodedPathScore(r) { return r.rawPathOrder === "reversed" ? 0 : 100; }',
  'function _mcPathLengthKm() { return 0; }',
  'function _mcPathMonotonicPenalty() { return 0; }',
  'function findMcContactsByKeyPrefix(hash) { return contactsFor(hash); }',
  'return { decode: decodeMcEventPath, resolve: typeof _mcResolveWholePath === "function" ? _mcResolveWholePath : null };',
].join('\n');

const api = new Function('contactsFor', code)(contactsFor);

const RADIO = [46.0409, 14.4981];

// --- (a) the measured 5-hop route is untouched (same repeaters, same order,
// same real legs). Drawn order is radio -> ... -> sender.
const five = api.decode(
  { path: 'cebe' + 'ea61' + 'c2aa' + '66df' + '1cce', path_len: 5, path_hash_size: 2 },
  46.3517, 14.1723, RADIO[0], RADIO[1], 'mc1'
);
assert(five, 'the 5-hop route must decode');
assert(five.hops.length === 5, `the 5-hop route must keep 5 hops, got ${five.hops.length}`);
assert(five.hops.map(h => h.name).join(',') === 'SI-Mrzlica,SI-Jost,SI-LJ-Dunajska5,SI-Dobrca,SI-KR-Radolca',
  `the 5-hop route changed: ${five.hops.map(h => h.name).join(',')}`);
if (api.resolve) {
  const legs = five.hops.map(h => Math.round(h.legKm));
  assert(Math.abs(legs[0] - 50) <= 2 && Math.abs(legs[1] - 62) <= 2 && Math.abs(legs[2] - 26) <= 2
    && Math.abs(legs[3] - 41) <= 2 && Math.abs(legs[4] - 5) <= 2,
    `the measured legs changed: ${legs.join('/')}`);
}

// --- (b) 3-hop path ending in 86a8 next to our radio: Ljubljana, not Italy.
const three = api.decode(
  { path: 'aa11' + 'bb22' + '86a8', path_len: 3, path_hash_size: 2 },
  46.30, 14.40, RADIO[0], RADIO[1], 'mc1'
);
assert(three, 'the 86a8 path must decode');
const ending = three.hops[0];
assert(ending.name === 'SI-LJ-Trnovo II',
  `86a8 next to our radio must be SI-LJ-Trnovo II, got ${ending.name}`);

// --- (c) mid-path collision decided by continuity, not the straight line.
const mid = api.decode(
  { path: 'dd44' + '77cc' + 'cc33', path_len: 3, path_hash_size: 2 },
  46.00, 15.00, 46.00, 14.50, 'mc1'
);
assert(mid, 'the mid-collision path must decode');
const middle = mid.hops.find(h => h.hash === '77cc');
assert(middle, 'the mid-path collision hop must resolve');
assert(middle.name === 'RPTR-True-North',
  `continuity must pick RPTR-True-North, got ${middle.name}`);

// --- (d) no positions anywhere: the whole-path rule declines and the old
// per-hop behaviour (no GPS -> unresolved) is exactly as before.
const saved = {};
Object.keys(CONTACTS).forEach(k => { saved[k] = CONTACTS[k]; delete CONTACTS[k]; });
Object.assign(CONTACTS, NOPOS);
const noPos = api.resolve(['aa11', 'bb22'], 'mc1', RADIO[0], RADIO[1], 46.00, 15.00);
assert(noPos === null, 'with no positioned candidate the whole-path rule must fall back');
const fallback = api.decode(
  { path: 'aa11' + 'bb22', path_len: 2, path_hash_size: 2 },
  46.00, 15.00, RADIO[0], RADIO[1], 'mc1'
);
Object.keys(CONTACTS).forEach(k => delete CONTACTS[k]);
Object.assign(CONTACTS, saved);
assert(fallback, 'the decoder must still return a drawable path without positions');
assert(fallback.hops.length === 0 && fallback.unresolvedHops === 2,
  `without positions both hops must stay unresolved, got ${fallback.hops.length} hops / ${fallback.unresolvedHops}`);

// --- (e) a 1-byte path with many close candidates keeps today's label.
assert(api.resolve, '_mcResolveWholePath() not found in app.js');
const one = api.resolve(['ee55'], 'mc1', RADIO[0], RADIO[1], 46.20, 14.46);
assert(one && one.length === 1, 'the 1-byte hop must resolve');
assert(one[0].ambiguous === true || one[0].confidence === 'estimated',
  `a crowded 1-byte hop must stay ambiguous/estimated, got ${one[0].confidence} ambiguous=${one[0].ambiguous}`);

// The 2-byte collision that the route settles is NOT flagged ambiguous.
const settled = api.resolve(['aa11', 'bb22', '86a8'], 'mc1', RADIO[0], RADIO[1], 46.30, 14.40);
assert(settled[2].contact.id === '86a8' && !settled[2].ambiguous,
  'the 150 km 86a8 collision must resolve unambiguously once the route agrees');

console.log('MC whole-path (Viterbi) checks passed');

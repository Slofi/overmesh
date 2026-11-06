// MC path decoder: an unknown hop before the first known point (EST-325).
//
// The alternate-path commit (a3dac5b) records gapAfterPoints so the map stops
// the line at a relay whose position is unknown instead of bridging across it.
// A gap means "the segment after this point must not be drawn". When our own
// radio has no position yet, points starts empty, and an unknown first hop was
// recorded as gap 0 anyway. That gap then cut the ONLY real segment: the one
// from the first known relay to the endpoint. The map drew nothing and the
// status line still said a hop was left out.
//
// This test runs the real decoder with a fake contact resolver: the unknown hop
// is first in packet order and our radio has no position.
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

const GPS = { bb: [46.06, 14.49] };
const code = [
  extract('decodeMcEventPath'),
  extract('_mcExplicitPathHashSize'),
  extract('_mcPathHopMeta'),
  // The geometry score needs the real map maths. Prefer the as-received build
  // so the test pins the exact ordering: the decoder tries both orders, and
  // build() must be right for whichever one it is asked to build.
  "function _mcDecodedPathScore(r) { return r.rawPathOrder === 'as-received' ? 0 : 100; }",
  'function _mcPathLengthKm() { return 0; }',
  'function _mcPathMonotonicPenalty() { return 0; }',
  `function _mcPickPathHopResolution(hash) {
     const g = GPS[hash];
     return g ? { contact: { id: hash, long_name: 'RPTR ' + hash, latitude: g[0], longitude: g[1] }, confidence: 'unique-1B' } : null;
   }`,
  'return { decode: decodeMcEventPath };',
].join('\n');

const api = new Function('GPS', code)(GPS);

// --- our radio has no position: the first hop is unknown, the second is known.
// points are [known relay, endpoint]; there is no segment to suppress, so no gap.
const noRadio = api.decode(
  { path: 'aabb', path_len: 2, path_hash_size: 1 },
  46.05, 14.50,
  null, null,
  'mc1'
);
assert(noRadio, 'the decoder must still return the path it can draw');
assert(noRadio.points.length === 2, `expected 2 points, got ${noRadio.points.length}`);
assert(noRadio.gapAfterPoints.length === 0,
  `no drawn segment may be dropped when the only unknown hop is before the first known point: ${JSON.stringify(noRadio.gapAfterPoints)}`);
assert(noRadio.unresolvedHops === 1, 'the unknown hop must still be counted so the status line can say so');

// --- with our radio position, the unknown hop still breaks the line: the gap
// must sit between our radio and the known relay, never on the relay->endpoint
// segment.
const withRadio = api.decode(
  { path: 'aabb', path_len: 2, path_hash_size: 1 },
  46.05, 14.50,
  46.04, 14.49,
  'mc1'
);
assert(withRadio && withRadio.points.length === 3, 'radio + known relay + endpoint');
assert(withRadio.gapAfterPoints.length === 1,
  `one unknown hop means one gap: ${JSON.stringify(withRadio.gapAfterPoints)}`);
const endpointSeg = withRadio.points.length - 2;
assert(!withRadio.gapAfterPoints.includes(endpointSeg),
  'the segment from the last known relay to the endpoint must stay drawn');

console.log('ok: an unknown hop before the first known point does not cut a real segment');

// MC signal heatmap: passive observations -> heat points (S479).
//
// Why this test exists: the heat layer used to read only identity observations,
// which a LOCAL MC node never produces (those come from Remote Collectors), so
// switching Heat on with OverMesh's own node always ended in "no source has
// signal and position". Overheard packets are the data that node does have.
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

const CONTACTS = {
  '86a8': { id: '86a8eedb744b', full_key: '86a8eedb744b5828821a2e36cd912f983cfd5555e7dba2ce0ed509893d36dc6', adv_lat: 46.0701, adv_lon: 14.4901 },
  'ba90': { id: 'ba9053aa4a70', full_key: 'ba9053aa4a70aabbccddeeff00112233445566778899aabbccddeeff00112233' },
};

const code = [
  extract('_signalHeatmapIntensity'),
  extract('_mcHeatPointsFromObs'),
  `function findMcContactByKeyPrefix(prefix) {
     const p = String(prefix || '').toLowerCase();
     return Object.entries(CONTACTS).find(([k]) => k.startsWith(p) || CONTACTS[k].id.startsWith(p))?.[1] || null;
   }`,
  'return _mcHeatPointsFromObs;',
].join('\n');

const build = new Function('CONTACTS', code)(CONTACTS);

// --- an overheard packet (rx) is a usable point: filed under the relay it came
// from, with the position the flusher stored
const pts = build([
  { obs_type: 'rx', pubkey_pre: '86a8', rssi: -55, snr: 12, lat: 46.0701, lon: 14.4901 },
  { obs_type: 'rx', pubkey_pre: 'rx:unknown', rssi: -80, snr: 5, lat: 46.1, lon: 14.5 },
  { obs_type: 'rx', pubkey_pre: '', rssi: -80, snr: 5, lat: 46.1, lon: 14.5 },
], 'r1');
assert(pts.length === 1, `only the identified, positioned row may count, got ${pts.length}`);
assert(pts[0][0] === 46.0701 && pts[0][1] === 14.4901, 'the point keeps the stored position');
assert(Math.abs(pts[0][2] - ((-55 + 120) / 70)) < 1e-9, 'RSSI drives intensity');

// --- position fallbacks: contact advertisement first, collector position last
const viaContact = build([{ obs_type: 'rx', pubkey_pre: '86a8', rssi: -70 }], 'r1');
assert(viaContact.length === 1 && viaContact[0][0] === 46.0701, 'position falls back to the contact advertisement');

const viaCollector = build([{ obs_type: 'rc_adv', pubkey_pre: 'ba90', rssi: -90, collector_lat: 46.05, collector_lon: 14.5 }], 'r1');
assert(viaCollector.length === 1 && viaCollector[0][0] === 46.05, 'position falls back to the collector position');

const noPosition = build([{ obs_type: 'rc_adv', pubkey_pre: 'ba90', rssi: -90 }], 'r1');
assert(noPosition.length === 0, 'a row with no resolvable position is skipped, not plotted at 0,0');

// --- a 2-character hop hash is too ambiguous to resolve to a contact, so the
// row falls back to the collector position instead of a stranger's node
const short = build([{ obs_type: 'rx', pubkey_pre: '86', rssi: -70, collector_lat: 46.04, collector_lon: 14.5 }], 'r1');
assert(short.length === 1 && short[0][0] === 46.04, 'a 2-char hop hash must not resolve to a contact position');

// --- SNR fallback when RSSI is missing, and no signal means no point
const snrOnly = build([{ obs_type: 'trace', pubkey_pre: '86a8', snr: 20 }], 'r1');
assert(snrOnly.length === 1 && Math.abs(snrOnly[0][2] - 1) < 1e-9, 'SNR drives intensity when RSSI is absent');
assert(build([{ obs_type: 'rx', pubkey_pre: '86a8' }], 'r1').length === 0, 'a row with no signal is skipped');

// --- intensity is clamped to 0..1
const weak = build([{ obs_type: 'rx', pubkey_pre: '86a8', rssi: -140 }], 'r1');
assert(weak[0][2] === 0, 'a very weak signal clamps to 0');
const strong = build([{ obs_type: 'rx', pubkey_pre: '86a8', rssi: -20 }], 'r1');
assert(strong[0][2] === 1, 'a very strong signal clamps to 1');

// --- junk input never throws
assert(build(null, 'r1').length === 0, 'null input yields no points');
assert(build([null, undefined, 42], 'r1').length === 0, 'junk rows are skipped');

// --- the endpoint is asked for rx explicitly, since it excludes rx by default
assert(/obs_types=\$\{types\}/.test(src) && /'rx,trace,rc_adv,rc_anon,rc_peer'/.test(src),
  'the heat fetch must ask for rx explicitly');

console.log('ok: MC heat points come from overheard packets too');

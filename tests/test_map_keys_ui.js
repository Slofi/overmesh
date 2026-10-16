// Map layer API keys — behaviour, not string matching (S453).
//
// The gap this closes: Stadia Maps layers (Toner Lite/Dark, Stamen Terrain,
// Stadia Outdoors) needed a key but had no placeholder, no settings field, no
// resolve step and no "key missing" guard — so on an instance reached through a
// domain they failed with no explanation and nothing to configure. Rather than
// grep for that, this extracts the real TILE_LAYERS table with the real
// resolver and guard, and asserts the invariants per keyed provider.
const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

function extractFrom(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  assert(start >= 0, `${startMarker} not found in app.js`);
  const end = src.indexOf(endMarker, start);
  assert(end > start, `${startMarker} has no end marker`);
  return src.slice(start, end + endMarker.length);
}

function sandbox(store) {
  const toasts = [];
  const code = [
    'let _mapAppTileLayers = {};',
    extractFrom('const TILE_LAYERS = {', '\n  };'),
    extractFrom('function allTileLayers()', '\n  }'),
    extractFrom('function _layerKeyMissing(layerKey)', '\n  }'),
    extractFrom('function _resolveTileUrl(url)', '\n  }'),
    'return { TILE_LAYERS, allTileLayers, _layerKeyMissing, _resolveTileUrl };',
  ].join('\n');
  const api = new Function('localStorage', 'showToast', code)(store, (t, b, k, tag) => toasts.push(tag));
  return { ...api, toasts };
}

const store = {
  _v: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
  setItem(k, v) { this._v[k] = String(v); },
  removeItem(k) { delete this._v[k]; },
};

// The four providers that need a key, as settings store them.
const KEYED = {
  apikey:     'thunderforestApiKey',
  mtapikey:   'mapTilerApiKey',
  cartokey:   'cartoApiKey',
  stadiakey:  'stadiaApiKey',
};

const { TILE_LAYERS, _layerKeyMissing, _resolveTileUrl } = sandbox(store);
const layers = Object.entries(TILE_LAYERS);

// ── no provider may keep a placeholder nobody resolves ─────────────────────
const KNOWN_LOCAL = ['{s}', '{z}', '{x}', '{y}', '{r}', '{y}{x}'];
for (const [name, def] of layers) {
  const leftover = (def.url.match(/\{[a-z]+\}/g) || [])
    .filter(ph => !KNOWN_LOCAL.includes(ph));
  for (const ph of leftover) {
    assert(Object.prototype.hasOwnProperty.call(KEYED, ph.slice(1, -1)),
      `${name} uses placeholder ${ph} that no key provider resolves`);
  }
  assert(/\{z\}/.test(def.url) && /\{x\}/.test(def.url) && /\{y\}/.test(def.url),
    `${name} is not a tile URL template`);
}

// ── every keyed layer: guarded when keyless, resolved when keyed ───────────
let checked = 0;
for (const [name, def] of layers) {
  const ph = (def.url.match(/\{([a-z]+key)\}/) || [])[1];
  if (!ph) continue;
  checked++;
  store._v = {};
  assert(_layerKeyMissing(name) === true, `${name} must report a missing key when none is set`);
  const keyless = _resolveTileUrl(def.url);
  assert(!keyless.includes(`{${ph}}`), `${name} left its placeholder in the URL with no key`);
  const storageName = KEYED[ph];
  assert(store.getItem(storageName) === null, `${name} must not invent a key`);

  store._v = { [storageName]: 'KEY-123' };
  assert(_layerKeyMissing(name) === false, `${name} must accept the configured key`);
  const withKey = _resolveTileUrl(def.url);
  assert(withKey.includes('KEY-123'), `${name} did not substitute the key`);
  assert(!/\{[a-z]+key\}/.test(withKey), `${name} left a placeholder behind with a key set`);
}
assert(checked >= 17, `expected every keyed layer to be covered, saw ${checked}`);

// ── Stadia specifically: keyless drops the parameter instead of emptying it ─
store._v = {};
const stadia = layers.filter(([k]) => k.startsWith('stamen_') || k === 'stadia_outdoors');
assert(stadia.length === 4, `expected 4 Stadia layers, saw ${stadia.length}`);
for (const [name, def] of stadia) {
  assert(def.url.includes('{stadiakey}'), `${name} must carry the Stadia placeholder`);
  const keyless = _resolveTileUrl(def.url);
  assert(!keyless.includes('api_key='), `${name} must not send an empty api_key parameter`);
  store._v = { stadiaApiKey: 'abc def' };
  const withKey = _resolveTileUrl(def.url);
  assert(withKey.includes('api_key=abc%20def'), `${name} must URL-encode the key`);
  store._v = {};
}

// ── keyless Stadia warns once per layer, with its own toast tag ────────────
const s2 = sandbox(store);
s2._resolveTileUrl(TILE_LAYERS.stadia_outdoors.url);
assert(s2.toasts.includes('stadia-no-key'), 'selecting a keyless Stadia layer must say a key is needed');

// ── the ⚠ cue in the layer menu comes from the same guard ─────────────────
assert(src.includes('if (_layerKeyMissing(key)) {'), 'the layer menu must mark layers that need a key');
assert(src.includes("'\\u00a0key needed'"), 'and say so in words');

// CARTO and Stadia labels carry the ★ convention that Thunderforest/MapTiler use.
for (const name of ['voyager', 'voyager_nolabels', 'positron', 'dark_matter', 'dark_nolabels',
                    'stamen_toner_lite', 'stamen_toner_dark', 'stamen_terrain', 'stadia_outdoors']) {
  assert(TILE_LAYERS[name].label.includes('★'), `${name} needs the ★ key marker like TF/MT layers`);
}

console.log('ok: map layer keys resolve, guard and warn per provider (17 keyed layers)');

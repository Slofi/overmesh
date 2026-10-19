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
    extractFrom('const DEFAULT_TILE_LAYER =', ';'),
    extractFrom('const KEY_PROVIDER_GROUPS = {', '\n  };'),
    extractFrom('const TILE_LAYERS = {', '\n  };'),
    extractFrom('function allTileLayers()', '\n  }'),
    extractFrom('function _layerKeyMissing(layerKey)', '\n  }'),
    extractFrom('function _keyTag(layerKey)', '\n  }'),
    extractFrom('function _resolveTileUrl(url)', '\n  }'),
    'return { DEFAULT_TILE_LAYER, KEY_PROVIDER_GROUPS, TILE_LAYERS, allTileLayers, _layerKeyMissing, _keyTag, _resolveTileUrl };',
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

const { DEFAULT_TILE_LAYER, KEY_PROVIDER_GROUPS, TILE_LAYERS, _layerKeyMissing, _keyTag, _resolveTileUrl } = sandbox(store);
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


// ── the default must be a key-free layer (and not the plain OSM one) ───────
store._v = {};
assert(DEFAULT_TILE_LAYER === 'esri_topo', `Filip picked Esri Topo as the default, saw ${DEFAULT_TILE_LAYER}`);
assert(Object.prototype.hasOwnProperty.call(TILE_LAYERS, DEFAULT_TILE_LAYER), 'the default must exist in the table');
assert(TILE_LAYERS[DEFAULT_TILE_LAYER].url.includes('arcgisonline.com'), 'Esri Topo comes from the public Esri service');
assert(_layerKeyMissing(DEFAULT_TILE_LAYER) === false,
  'the DEFAULT LAYER MUST NEVER NEED A KEY: a keyed default greets a fresh install with watermarks');

// ── menu order: free maps first, then each provider as one contiguous group ─
const order = Object.keys(TILE_LAYERS);
const isKeyed = k => /\{[a-z]+key\}/.test(TILE_LAYERS[k].url);
const firstKeyed = order.findIndex(isKeyed);
assert(firstKeyed > 0, 'there must be free layers, and they must come first');
assert(!order.slice(firstKeyed).some(k => !isKeyed(k)),
  'no free layer may appear after a keyed one — free maps first');
const providerOf = k => {
  const ph = Object.keys(KEY_PROVIDER_GROUPS).find(ph => TILE_LAYERS[k].url.includes(ph));
  return ph || 'free';
};
const seen = [];
for (const k of order) {
  const p = providerOf(k);
  if (seen[seen.length - 1] !== p) {
    assert(!seen.includes(p), `${k} starts a second ${p} block — each provider must be one group`);
    seen.push(p);
  }
}
assert(seen.join(',') === 'free,{apikey},{mtapikey},{cartokey},{stadiakey}',
  `expected free then TF, MT, CARTO, Stadia — saw ${seen.join(',')}`);

// ── the cached-tile identity must include EVERY key, Stadia included ───────
for (const [name, def] of layers) {
  const ph = (def.url.match(/\{([a-z]+key)\}/) || [])[1];
  if (!ph) continue;
  const storage = KEYED[ph];
  store._v = {};
  const tagless = _keyTag(name);
  assert(tagless && tagless !== '', `${name}: a keyed layer must carry a cache tag (else keyless tiles are reused for ever)`);
  assert(tagless === ':nokey', `${name}: no key should tag as :nokey, saw ${tagless}`);
  store._v = { [storage]: 'key-one' };
  const tagOne = _keyTag(name);
  assert(tagOne !== ':nokey' && tagOne !== '', `${name}: the tag must reflect a configured key`);
  store._v = { [storage]: 'key-two' };
  const tagTwo = _keyTag(name);
  assert(tagOne !== tagTwo, `${name}: two different keys must not share a cache identity`);
  assert(!tagOne.includes('key-one') && !tagOne.includes('key-two'),
    `${name}: the tag is a hash — the key itself must never travel in a cache key`);
}
store._v = {};
assert(_keyTag('osm') === '', 'a free layer needs no cache tag');

// ── lite has no settings UI, so its default must be key-free too ──────────
const liteSrc = fs.readFileSync(path.join(REPO, 'static/js/lite.js'), 'utf8');
const liteDefault = (liteSrc.match(/const DEFAULT_LAYER = '([a-z_]+)'/) || [])[1];
assert(liteDefault, 'lite must name a default layer');
const liteBlock = liteSrc.slice(liteSrc.indexOf('const LAYERS = {'), liteSrc.indexOf('\n};', liteSrc.indexOf('const LAYERS = {')));
const liteOrder = [...liteBlock.matchAll(/^  ([a-z_]+):\s*\{/gm)].map(m => m[1]);
assert(/\{[a-z]+key\}/.test(liteBlock.match(new RegExp(`  ${liteDefault}:\\s+\\{.*`))[0]) === false,
  'lite has no key fields, so its default layer must not need a key');
const liteFirstKeyed = liteOrder.findIndex(k => /\{[a-z]+key\}/.test(liteBlock.match(new RegExp(`  ${k}:\\s+\\{.*`))[0]));
assert(liteFirstKeyed > 0 && !liteOrder.slice(liteFirstKeyed).some(k =>
  !/\{[a-z]+key\}/.test(liteBlock.match(new RegExp(`  ${k}:\\s+\\{.*`))[0])),
  'lite: free layers first, then the keyed ones');

// ── credits: required provider text present, Leaflet branding absent (S458) ─
// The Leaflet flag/wordmark is courtesy, not a licence term; provider credits are
// terms. A credit that names a provider without its required link is the failure
// this guards, and so is an abbreviated Esri string.
for (const [name, def] of layers) {
  assert(typeof def.attribution === 'string' && def.attribution.trim(),
    `${name} must carry an attribution`);
  assert(!/leaflet/i.test(def.attribution),
    `${name} must not credit Leaflet on the map`);
}
assert(/openstreetmap\.org\/copyright/.test(TILE_LAYERS.osm.attribution),
  'OSM must link to the copyright page');
for (const k of ['voyager', 'voyager_nolabels', 'positron', 'dark_matter', 'dark_nolabels']) {
  assert(/carto\.com/.test(TILE_LAYERS[k].attribution), `${k} must link CARTO`);
  assert(/openstreetmap\.org\/copyright/.test(TILE_LAYERS[k].attribution), `${k} must link OSM`);
}
for (const k of ['tf_landscape', 'tf_outdoors', 'tf_pioneer', 'tf_atlas']) {
  assert(/thunderforest\.com/.test(TILE_LAYERS[k].attribution), `${k} must link Thunderforest`);
  assert(/openstreetmap\.org\/copyright/.test(TILE_LAYERS[k].attribution), `${k} must link OSM`);
}
for (const k of ['mt_hybrid', 'mt_topo', 'mt_streets', 'mt_winter']) {
  assert(/maptiler\.com/.test(TILE_LAYERS[k].attribution), `${k} must link MapTiler`);
  assert(/openstreetmap\.org\/copyright/.test(TILE_LAYERS[k].attribution), `${k} must link OSM`);
}
for (const k of ['stamen_toner_lite', 'stamen_toner_dark', 'stamen_terrain', 'stadia_outdoors']) {
  assert(/stadiamaps\.com/.test(TILE_LAYERS[k].attribution), `${k} must link Stadia Maps`);
  assert(/openstreetmap\.org\/copyright/.test(TILE_LAYERS[k].attribution), `${k} must link OSM`);
}
assert(/opentopomap\.org/.test(TILE_LAYERS.topo.attribution) &&
  /openstreetmap\.org\/copyright/.test(TILE_LAYERS.topo.attribution),
  'OpenTopoMap must link both itself and OSM');
for (const k of ['esri_topo', 'esri_streets', 'esri_sat', 'esri_gray_dark', 'esri_hillshade']) {
  assert(TILE_LAYERS[k].attribution.startsWith('Esri,'), `${k} must use the service's own Esri credit`);
  assert(!/^Tiles © Esri\b/.test(TILE_LAYERS[k].attribution), `${k} still carries the old abbreviated credit`);
}
assert(src.includes('leafletMap.attributionControl.setPrefix(false)'),
  'the main map must drop the Leaflet prefix');
assert(liteSrc.includes('map.attributionControl.setPrefix(false)'),
  'the lite map must drop the Leaflet prefix');
assert(src.includes('pipMap.attributionControl.setPrefix(false)'),
  'the PiP map must drop the Leaflet prefix too');
assert(src.includes('attribution: _pipDef.attribution'),
  'the PiP map must carry the basemap provider credit');

console.log('ok: map keys, default layer and menu order (free first, then TF/MT/CARTO/Stadia)');
console.log('ok: provider credits present, Leaflet prefix removed');

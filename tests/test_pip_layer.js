// Minimap (PiP) base layer follows the main map (S468) — real functions from
// app.js run against fakes.
//
// Filip: "the selected map layer is not reflected in the mini-map". Cause: the
// minimap built its own tile layer ONCE at init, from the saved key, with no
// cachePrefix — so it never followed a change, and every layer it showed used
// the default 'osm' cache slot (wrong cached tiles in, wrong tiles cached out).
// Fix: one helper, _applyTileLayer(), used by both maps.
//
// What must hold:
//  - a new layer is built with the cache key of the layer it draws;
//  - switching updates the cache key BEFORE setUrl, swaps credits, sets zooms;
//  - an unknown key (saved shared layer, list not loaded yet) draws AND caches
//    as the default layer, while the REQUESTED key stays saved;
//  - the minimap reuses one layer and re-syncs on every main-map change and
//    every shared-list change.
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

const env = new Function([
  'const log = [];',
  'const store = {};',
  'const localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };',
  'const DEFAULT_TILE_LAYER = "osm";',
  'const TILE_LAYERS = { osm: { url: "U-osm", attribution: "A-osm", maxZoom: 19 } };',
  'let shared = {};',
  'function allTileLayers() { return Object.assign({ sat: { url: "U-sat", attribution: "A-sat", maxZoom: 18 } }, TILE_LAYERS, shared); }',
  'function _resolveTileUrl(u) { return u; }',
  'function _keyTag(k) { return ":" + k; }',
  'function makeMap(name) {',
  '  const credits = new Set();',
  '  return { name, layers: [], credits, attributionControl: {',
  '    removeAttribution: a => { credits.delete(a); log.push(name + " -credit " + a); },',
  '    addAttribution: a => { credits.add(a); log.push(name + " +credit " + a); } } };',
  '}',
  'function OfflineTileLayer(url, options) {',
  '  this.url = url; this.options = Object.assign({}, options);',
  '  this.setUrl = u => { log.push("setUrl " + u + " with prefix " + this.options.cachePrefix); this.url = u; };',
  '  this.addTo = m => { m.layers.push(this); if (this.options.attribution) m.credits.add(this.options.attribution); return this; };',
  '}',
  'let leafletMap = makeMap("MAIN"), pipMap = null, _activeTileLayer = null, _pipTileLayer = null;',
  'const document = { querySelectorAll: () => [], getElementById: () => null };',
  extract('_applyTileLayer'),
  extract('setTileLayer'),
  extract('_pipSyncTileLayer'),
  'return { _applyTileLayer, setTileLayer, _pipSyncTileLayer, store, log, makeMap,',
  '  openPip: () => { pipMap = makeMap("PIP"); _pipSyncTileLayer(); },',
  '  freshPip: () => { pipMap = null; _pipTileLayer = null; },',
  '  main: () => leafletMap, pip: () => pipMap, mainLayer: () => _activeTileLayer, pipLayer: () => _pipTileLayer,',
  '  setShared: s => { shared = s; } };',
].join('\n'))();

// ── 1. main map: build, then switch ─────────────────────────────────────────
env.setTileLayer('sat');
let L1 = env.mainLayer();
assert(L1 && L1.url === 'U-sat' && L1.options.cachePrefix === 'sat' && L1.options.keyTag === ':sat', 'new layer carries its own cache key');
assert(env.store.mapTileLayer === 'sat', 'the choice is saved');
env.log.length = 0;
env.setTileLayer('osm');
assert(env.mainLayer() === L1, 'switching reuses the same layer object');
assert(env.log[0] === 'setUrl U-osm with prefix osm', 'cache prefix is updated BEFORE setUrl: ' + env.log[0]);
assert(env.main().credits.has('A-osm') && !env.main().credits.has('A-sat'), 'credits are swapped');
assert(L1.options.maxZoom === 19, 'zoom limit follows the layer');

// ── 2. minimap follows the main map ─────────────────────────────────────────
env.setTileLayer('sat');
env.openPip();                                   // opened after a choice was made
let P1 = env.pipLayer();
assert(P1 && P1.url === 'U-sat' && P1.options.cachePrefix === 'sat', 'minimap opens on the saved layer, with that layer\'s cache key');
assert(env.pip().layers.length === 1, 'one layer on the minimap');
env.setTileLayer('osm');                         // Filip changes the layer on the Map tab
assert(env.pipLayer() === P1 && P1.url === 'U-osm' && P1.options.cachePrefix === 'osm', 'minimap follows the change (same layer, new URL + cache key)');
assert(env.pip().layers.length === 1, 'no second layer stacked on the minimap');
assert(env.pip().credits.has('A-osm') && !env.pip().credits.has('A-sat'), 'minimap credits follow too');

// ── 3. saved shared layer before its list loaded ────────────────────────────
env.setTileLayer('shared_topo');                 // e.g. restored at startup, list not loaded yet
assert(env.store.mapTileLayer === 'shared_topo', 'the REQUESTED key stays saved (restored when the list arrives)');
assert(env.mainLayer().url === 'U-osm' && env.mainLayer().options.cachePrefix === 'osm',
  'unknown layer draws AND caches as the default — never default tiles under the shared key');
assert(env.pipLayer().url === 'U-osm' && env.pipLayer().options.cachePrefix === 'osm', 'minimap: same fallback');
env.freshPip(); env.openPip();                   // the minimap's FIRST open at startup, list still not loaded
assert(env.pipLayer().url === 'U-osm' && env.pipLayer().options.cachePrefix === 'osm'
  && env.pipLayer().options.keyTag === ':osm', 'a NEW layer for an unknown key is also built with the default\'s cache key');
env.setShared({ shared_topo: { url: 'U-topo', attribution: 'A-topo', maxZoom: 16, maxNativeZoom: 14 } });
env._pipSyncTileLayer();                         // what refreshBaseLayerPanelOptions() now does on a list change
assert(env.pipLayer().url === 'U-topo' && env.pipLayer().options.cachePrefix === 'shared_topo'
  && env.pipLayer().options.maxNativeZoom === 14, 'list arrives -> minimap switches to the shared layer with its limits');

// ── 4. wiring ────────────────────────────────────────────────────────────────
const init = extract('initPipMap');
assert(init.includes('_pipSyncTileLayer()') && !init.includes('new OfflineTileLayer'), 'initPipMap uses the shared path, no private layer');
assert(/_pipSyncTileLayer\(\)/.test(extract('setTileLayer')), 'every main-map layer change re-syncs the minimap');
const refresh = extract('refreshBaseLayerPanelOptions');
assert(refresh.indexOf('_pipSyncTileLayer()') >= 0
  && refresh.indexOf('_pipSyncTileLayer()') < refresh.indexOf('if (!_baseLayerMenuPanel) return;'),
  'a shared-list change re-syncs the minimap BEFORE the panel early-return (the panel may not exist yet)');

console.log('ok: minimap follows the main map layer (same code path, own cache key, shared-layer fallback)');

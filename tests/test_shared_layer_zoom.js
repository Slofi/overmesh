// An OPS-TOC shared layer must use the MBTiles pack's REAL zoom limits.
//
// Found on CD 2026-10-17 (S454): mbtileserver's /services LIST carries only name/url/imageType, so OM fell back to
// maxZoom 19 while the packs end at 17 and 15. Past max zoom mbtileserver answers HTTP 200 with a blank 256x256
// tile, so over-zooming looked like an empty map instead of an error. The fix reads /services/<id> (which has
// minzoom/maxzoom/bounds) and lets Leaflet over-zoom the deepest real tile.
const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

function extract(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  assert(start >= 0, `${startMarker} not found`);
  const end = src.indexOf(endMarker, start);
  assert(end > start, `${endMarker} not found after ${startMarker}`);
  return src.slice(start, end + endMarker.length);
}

const factory = [
  'let _sharedTileServerUrl = "http://localhost:8092";',
  'function _mapAppLocalLayerKey(id) { return "shared_" + String(id).replace(/[^A-Za-z0-9_-]/g, "_"); }',
  // Credits used by the shared-layer derivation; the real values are pinned by test_map_keys_ui.js.
  'const TILE_LAYERS = { esri_topo: { attribution: "ESRI_TOPO" }, esri_sat: { attribution: "ESRI_SAT" },'
    + ' esri_streets: { attribution: "ESRI_STREETS" }, esri_gray_dark: { attribution: "ESRI_GRAY" },'
    + ' esri_hillshade: { attribution: "ESRI_HILL" }, tf_atlas: { attribution: "TF" },'
    + ' voyager: { attribution: "CARTO" }, mt_topo: { attribution: "MT" }, topo: { attribution: "OPENTOPO" },'
    + ' osm: { attribution: "OSM" } };',
  extract('function _sharedLayerCredit(', '\n  }'),
  extract('function _sharedLayerDef(', '\n  }'),
  'return _sharedLayerDef;',
].join('\n');
const sharedLayerDef = new Function(factory)();

const entry = {
  url: 'http://localhost:8092/services/slovenija-esri-topo-z9-16',
  name: 'Slovenija - Esri Topo',
  imageType: 'jpg',
};

// Full detail: the pack ends at z17; the layer must over-zoom from there, not request blank z18/z19 tiles.
let built = sharedLayerDef(entry, { maxzoom: 17, minzoom: 9, attribution: 'Tiles (c) Esri' });
assert(built && built.key === 'shared_slovenija-esri-topo-z9-16', 'the layer key must come from the service id');
assert(built.def.maxNativeZoom === 17, `maxNativeZoom must be the pack's maxzoom, got ${built.def.maxNativeZoom}`);
assert(built.def.minNativeZoom === 9, `minNativeZoom must be the pack's minzoom, got ${built.def.minNativeZoom}`);
assert(built.def.maxZoom === 19, 'the map stays zoomable, but from native z17 upscaled');
assert(built.def.url === 'http://localhost:8092/services/slovenija-esri-topo-z9-16/tiles/{z}/{x}/{y}.jpg',
  `tile URL must keep the service id and image type, got ${built.def.url}`);
assert(built.def.attribution === 'Tiles (c) Esri', 'detail attribution wins when the list has none');

// The real mbtileserver detail has source_url/source_layer_name but NO attribution. The credit
// must come from the pack's own recorded source, never be invented and never stay blank.
built = sharedLayerDef(entry, {
  maxzoom: 17, minzoom: 9,
  source_url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
});
assert(built.def.attribution === 'ESRI_TOPO', `an Esri Topo pack must carry the Topo credit, got ${built.def.attribution}`);
built = sharedLayerDef({ url: 'http://localhost:8092/services/tf-atlas', name: 'TF Atlas', imageType: 'png' }, {
  source_url: 'https://tile.thunderforest.com/atlas/{z}/{x}/{y}.png?apikey=x',
});
assert(built.def.attribution === 'TF', `a Thunderforest pack must carry the TF credit, got ${built.def.attribution}`);
built = sharedLayerDef({ url: 'http://localhost:8092/services/unknown', name: 'Unknown', imageType: 'png' }, {});
assert(built.def.attribution === 'Local tiles · Unknown', 'an unknown pack stays honest instead of inventing a provider');

// Detail unreadable: keep the old behaviour (19), never NaN and never a dropped layer.
built = sharedLayerDef(entry, null);
assert(built && built.def.maxNativeZoom === 19, 'a failed detail read must fall back to 19, not break the layer');
assert(built.def.minNativeZoom === 0, 'a failed detail read must not invent a min zoom');

// No id anywhere: skip the layer instead of building a broken one.
assert(sharedLayerDef({ name: 'no id' }, null) === null, 'an entry without an id must be skipped');

// Wiring: the loader really reads the detail, and setTileLayer really applies the limits (a correct helper that
// nobody calls is the S453 failure mode this repo already has a rule about).
assert(src.includes('${_sharedTileServerUrl}/services/${encodeURIComponent(id)}'), 'the loader must fetch the per-service detail');
assert(src.includes('maxNativeZoom: def.maxNativeZoom'), 'the layer must be constructed with maxNativeZoom');
assert(src.includes('options.maxNativeZoom = def.maxNativeZoom'), 'switching layers must update maxNativeZoom');
assert(src.includes('options.minNativeZoom = def.minNativeZoom'), 'switching layers must update minNativeZoom');

console.log('ok: shared OPS-TOC layers use the real max zoom');

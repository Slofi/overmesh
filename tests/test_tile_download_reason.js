// Tile downloads must explain themselves: a rejected key, a provider that blocks browser downloads (no CORS) and a dead
// network all used to look like "N failed".
//
// Found while checking the tile cache downloads for Filip (2026-10-16). The message matters because a CORS-blocked
// provider still renders fine as an <img> on the map — so the user sees a working layer and a download that "fails",
// with nothing to act on.
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
  return src.slice(start, end + endMarker.length);
}

function makeReasonFn(fetchImpl, shared = false) {
  // 'shared' simulates an OPS-TOC MBTiles layer (sharedTileServer: true), which must be probed at the server, not a tile.
  const code = [
    'let _sharedTileServerUrl = "http://localhost:8092";',
    'let SHARED = {};',
    'function allTileLayers() { return SHARED; }',
    'function _tileUrlForLayer(layerKey, z, x, y) { return `https://example.test/${layerKey}/${z}/${x}/${y}.png`; }',
    extract('async function _tileDownloadReason(', '\n  }'),
    'return _tileDownloadReason;',
  ].join('\n');
  const fn = new Function('fetch', code)(fetchImpl);
  return (layerKey, z, x, y) => fn(layerKey, z, x, y);
}

function respond(status) {
  return async () => ({ ok: status >= 200 && status < 300, status });
}

(async () => {
  // a healthy layer downloads: the probe must not get in the way
  assert(await makeReasonFn(respond(200))('esri_topo') === null, 'HTTP 200 must mean "go ahead"');

  const rejected = await makeReasonFn(respond(401))('tf_landscape');
  assert(/rejected the key/.test(rejected) && /401/.test(rejected), `401 must name the key: got ${rejected}`);
  const forbidden = await makeReasonFn(respond(403))('mt_topo');
  assert(/rejected the key/.test(forbidden), `403 must name the key: got ${forbidden}`);

  const limited = await makeReasonFn(respond(429))('osm');
  assert(/rate-limiting/.test(limited), `429 must name rate limiting: got ${limited}`);

  const odd = await makeReasonFn(respond(500))('osm');
  assert(/HTTP 500/.test(odd), `other statuses must be passed through: got ${odd}`);

  // fetch() throwing is what a missing CORS header looks like, and it is the case the user could not diagnose
  const blocked = await makeReasonFn(async () => { throw new TypeError('Failed to fetch'); })('tf_outdoors');
  assert(/CORS|offline/.test(blocked), `a thrown fetch must explain CORS/offline: got ${blocked}`);

  // ── an OPS-TOC shared layer is probed at the SERVER, never at a tile ──
  // A regional MBTiles has no tile at the world origin, so probing a tile would call a healthy layer broken.
  const seen = [];
  const sharedFn = new Function('fetch',
    ['let _sharedTileServerUrl = "http://localhost:8092";',
     'let SHARED = { shared_si: { label: "Slovenia", url: "http://localhost:8092/services/si/tiles/{z}/{x}/{y}.png", sharedTileServer: true } };',
     'function allTileLayers() { return SHARED; }',
     'function _tileUrlForLayer(l, z, x, y) { return "TILE"; }',
     extract('async function _tileDownloadReason(', '\n  }'),
     'return _tileDownloadReason;'].join('\n'))(async (u) => { seen.push(u); return { ok: true, status: 200 }; });

  const offline = await sharedFn('shared_si', 9, 270, 178);
  assert(/already stored offline/.test(offline), `a served shared layer must say the tiles are already local: got ${offline}`);
  assert(!seen.some(u => u.includes('/tiles/')), `a shared layer must NOT be probed at a tile: ${seen.join(', ')}`);
  assert(seen.some(u => u.endsWith('/services')), `a shared layer must be probed at the tile server: ${seen.join(', ')}`);

  const down = new Function('fetch',
    ['let _sharedTileServerUrl = "http://localhost:8092";',
     'let SHARED = { shared_si: { url: "x", sharedTileServer: true } };',
     'function allTileLayers() { return SHARED; }',
     'function _tileUrlForLayer() { return "TILE"; }',
     extract('async function _tileDownloadReason(', '\n  }'),
     'return _tileDownloadReason;'].join('\n'))(async () => { throw new TypeError('Failed to fetch'); });
  const downMsg = await down('shared_si', 9, 270, 178);
  assert(/OPS-TOC tile server/.test(downMsg) && /not answering/.test(downMsg),
    `a dead local tile server must be named: got ${downMsg}`);

  // ── and both download paths must use it, before burning requests on a hopeless area ──
  for (const fn of ['saveTilesCurrentView', 'downloadSelectedRegion']) {
    const start = src.indexOf('function ' + fn + '(');
    assert(start >= 0, `${fn} not found`);
    const body = src.slice(start, start + 4000);
    assert(body.includes('_tileDownloadReason'),
      `${fn} must probe the layer before downloading (otherwise the user gets "N failed" and no reason)`);
    assert(/firstFailure/.test(body),
      `${fn} must report the first failure reason, not just the count`);
    assert(/_tileDownloadReason\([^)]*tiles\[0\]/.test(body),
      `${fn} must probe a tile the download actually needs (a regional layer has no world-origin tile)`);
  }

  console.log('ok: tile downloads explain a rejected key, a rate limit, a CORS/offline failure — and both paths ask first');
})();

// EST-248 (design EST-247): a page refresh in Sense mode must keep the MeshCore
// contacts on the map, and the "MC: n" counter must count what is drawn.
//
// Filip, S480, on CD's OverMesh: "shows no contacts on the map ... I saw nodes,
// but after a page refresh, they are gone again".
//
// This runs the real app.js up to its Init section in a tiny DOM/Leaflet stub,
// then replays the load order measured in a browser with his own localStorage
// (activeTab=map, sensePanelOpen=1, senseNet=mc, chatNetwork=mc, mapShowMc=1,
// mapShowMt=0, an MC radio connected and an MT radio configured):
//   1. the MT status arrives first; the MC status has NOT arrived yet,
//   2. initChat() applies the saved Chat choice (both pills share one choice),
//   3. doMapInit restores the Sense panel from storage,
//   4. /api/mc/status + contacts arrive and the contact render runs.
//
// On the old code a "nothing is connected yet" snapshot was read as "MeshCore is
// gone": the saved MC choice was rewritten to MT, the Sense restore set
// mapShowMc=false in memory, and the marker render then drew nothing while the
// contacts list was full. The counter showed a number from the Nodes table that
// had never been rendered in Sense mode.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.join(__dirname, '..');
let src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

// Cut the boot sequence: the test drives the same calls itself, in the measured order.
const initAt = src.indexOf('// ── Init ──');
if (initAt < 0) throw new Error('could not find the Init section in app.js');
src = src.slice(0, initAt);

function makeClassList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach(x => set.add(x)),
    remove: (...c) => c.forEach(x => set.delete(x)),
    toggle: (c, force) => {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    },
    contains: c => set.has(c),
  };
}

function makeElement(id) {
  return {
    id,
    style: {},
    dataset: {},
    classList: makeClassList(),
    textContent: '',
    innerHTML: '',
    value: '',
    checked: false,
    disabled: false,
    hidden: false,
    offsetHeight: 0,
    offsetWidth: 0,
    scrollHeight: 0,
    scrollTop: 0,
    getBoundingClientRect: () => ({ top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0 }),
    appendChild() {},
    removeChild() {},
    addEventListener() {},
    removeEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    focus() {},
    click() {},
    close() {},
    getContext: () => null,
  };
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}

// Filip's own localStorage while the bug was measured.
const storage = new Map(Object.entries({
  activeTab: 'map',
  sensePanelOpen: '1',
  senseNet: 'mc',
  chatNetwork: 'mc',
  mapShowMc: '1',
  mapShowMt: '0',
}));

function FakeLayer() {}
FakeLayer.prototype.addTo = function () { return this; };
FakeLayer.prototype.on = function () { return this; };
FakeLayer.prototype.off = function () { return this; };
FakeLayer.prototype.setStyle = function () { return this; };
FakeLayer.prototype.remove = function () {};
function SubLayer() {}
SubLayer.prototype = FakeLayer.prototype;
FakeLayer.extend = function () { return SubLayer; };

const L = {
  TileLayer: FakeLayer,
  Layer: FakeLayer,
  Control: FakeLayer,
  divIcon: opts => ({ opts }),
  icon: opts => ({ opts }),
  marker: (latlng, opts) => ({
    _latlng: latlng,
    _opts: opts,
    bindPopup() { return this; },
    bindTooltip() { return this; },
    addTo() { return this; },
    on() { return this; },
    closeTooltip() { return this; },
    openPopup() { return this; },
    closePopup() { return this; },
    setLatLng() { return this; },
    setIcon() { return this; },
    setPopupContent() { return this; },
    setTooltipContent() { return this; },
    setZIndexOffset() { return this; },
    getLatLng() { return { lat: latlng[0], lng: latlng[1] }; },
    getElement() { return null; },
  }),
  polyline: () => ({
    addTo() { return this; },
    bindPopup() { return this; },
    setStyle() { return this; },
    getBounds() { return { isValid: () => false }; },
    remove() {},
  }),
  circle: () => ({
    addTo() { return this; },
    bindPopup() { return this; },
    setStyle() { return this; },
  }),
  layerGroup: () => ({
    addTo() { return this; },
    clearLayers() {},
    addLayer() {},
  }),
};
L.TileLayer.prototype = { addTo() { return this; }, on() { return this; } };
L.Layer.prototype = { extend() { return L.TileLayer; } };
L.Control.prototype = { extend() { return L.TileLayer; }, addTo() { return this; } };

const sandbox = {
  console,
  L,
  window: null,
  document: {
    body: makeElement('body'),
    documentElement: makeElement('html'),
    getElementById: id => el(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: tag => makeElement(tag),
    createTextNode: () => ({}),
    addEventListener() {},
    removeEventListener() {},
  },
  localStorage: {
    getItem: k => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: k => storage.delete(k),
  },
  navigator: { setAppBadge() {}, clearAppBadge() {} },
  fetch: () => new Promise(() => {}),
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  Event: class {},
  CustomEvent: class {},
  TextEncoder,
  TextDecoder,
  Blob: class {},
  URL: { createObjectURL: () => '', revokeObjectURL: () => {} },
  Notification: undefined,
  ResizeObserver: class { observe() {} disconnect() {} },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.removeEventListener = () => {};
sandbox.window.setTimeout = sandbox.setTimeout;
sandbox.window.clearTimeout = sandbox.clearTimeout;
sandbox.window.setInterval = sandbox.setInterval;
sandbox.window.clearInterval = sandbox.clearInterval;
sandbox.window.requestAnimationFrame = sandbox.requestAnimationFrame;
sandbox.window.innerWidth = 1280;
sandbox.window.innerHeight = 800;
sandbox.window.devicePixelRatio = 1;
sandbox.window.location = { href: '' };
sandbox.window.BASE_PATH = '';
sandbox.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

// The served markup ships the Sense panel closed (templates/index.html: style="display:none"), and the Map tab is the
// saved tab, so this is the page as he left it just before the reload.
el('sense-panel').style.display = 'none';
el('sense-log-panel').style.display = 'none';

const test = `
  const failures = [];
  function check(cond, msg) { if (!cond) failures.push(msg); }

  // The page is on Map/Sense; Leaflet is ready but no radio status has arrived.
  leafletMap = {
    _layers: [],
    addLayer(l) { this._layers.push(l); return this; },
    removeLayer(l) { const i = this._layers.indexOf(l); if (i >= 0) this._layers.splice(i, 1); return this; },
    hasLayer(l) { return this._layers.includes(l); },
    invalidateSize() {},
    getZoom() { return 12; },
    setView() { return this; },
    flyTo() { return this; },
    latLngToLayerPoint() { return { x: 0, y: 0 }; },
    layerPointToLatLng() { return { lat: 0, lng: 0 }; },
    closePopup() {},
    openPopup() {},
  };
  allNodes = [];
  currentTab = 'map';

  // 1. The MT status arrives first and the status poll refreshes the pills (loadLive does exactly this at boot and
  //    every 15 s). The MC status has NOT arrived yet, so its list is empty — unknown, not "no MC radios".
  updateRadioSelector({ mt1: { name: 'MT1', status: 'connected', enabled: true } });
  updateMcPills();

  // 2. initChat() applies the saved Chat/Sense choice (boot runs this before the map tab).
  setChatNetwork(chatNetwork);
  check(localStorage.getItem('senseNet') === 'mc',
        'a load-time guess overwrote the saved senseNet (now ' + localStorage.getItem('senseNet') + ')');
  check(localStorage.getItem('chatNetwork') === 'mc',
        'a load-time guess overwrote the saved chatNetwork (now ' + localStorage.getItem('chatNetwork') + ')');
  check(_senseNet === 'mc', 'the in-memory choice moved to ' + _senseNet + ' before any MC status arrived');
  check(mapShowMc && !mapShowMt, 'the Chat/Sense choice must not rewrite the saved Map filters');

  // 3. doMapInit restores the Sense panel from storage.
  toggleSensePanel(true);
  check(localStorage.getItem('senseNet') === 'mc',
        'restoring Sense overwrote the saved senseNet (now ' + localStorage.getItem('senseNet') + ')');
  check(mapShowMc, 'the Sense restore cleared mapShowMc in memory');
  check(!mapShowMt, 'the Sense restore set mapShowMt in memory over the saved 0');

  // 4. The MC radio status arrives (SSE), then its contacts (the contacts fetch).
  handleMcSseEvent({ type: 'mc_status', radio_id: 'mc1', name: 'MC1', status: 'connected' });
  activeMcRadioId = 'mc1';
  mcContacts = { mc1: {
    c1: { id: 'c1', type: 1, long_name: 'Alpha',   latitude: 46.00, longitude: 14.50, last_heard_ts: 1700000000 },
    c2: { id: 'c2', type: 1, long_name: 'Bravo',   latitude: 46.10, longitude: 14.60, last_heard_ts: 1700000000 },
    c3: { id: 'c3', type: 1, long_name: 'Charlie', latitude: 46.20, longitude: 14.70, last_heard_ts: 1700000000 },
  } };
  setChatNetwork('mc');
  check(_senseNet === 'mc', 'Sense stayed on ' + _senseNet + ' after the saved MC choice was applied');

  // The contacts fetch always ends with a marker render.
  renderMcMapMarkers();
  const drawn = mcMapMarkers.length + Object.keys(mcClusterMarkers).length;
  check(drawn === 3, 'expected 3 MC map markers, drew ' + drawn);

  // 5. The toolbar counter must count what is drawn, not the Nodes table.
  const counter = document.getElementById('map-node-count').textContent;
  check(counter === 'MC: 3', 'Sense counter should read "MC: 3", got "' + counter + '"');

  // 6. Leaving Sense restores the saved Map filters and still draws the contacts.
  toggleSensePanel(false);
  check(mapShowMc, 'leaving Sense lost the saved mapShowMc=true');
  check(!mapShowMt, 'leaving Sense restored mapShowMt=true over the saved false');
  const after = mcMapMarkers.length + Object.keys(mcClusterMarkers).length;
  check(after === 3, 'saved Map filters should still draw 3 MC markers, drew ' + after);

  if (failures.length) throw new Error(failures.join('; '));
  console.log('ok: Sense load order keeps the MC markers, the saved choice and the counter honest');
`;

try {
  vm.runInNewContext(src + '\n' + test, sandbox, { filename: 'app.js+test' });
} catch (e) {
  console.error('FAIL: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
}

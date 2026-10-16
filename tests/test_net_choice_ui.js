// Chat and Sense pills share ONE remembered MT/MC choice; the ordinary Map filter does not.
//
// Filip, 2026-10-16: *"I mostly look at the Map/Sense tab, not map only.. leave the 'ordinary' map as it is, so both can
// be selected and only pair the Chat and Map/Sense pills, so they 'stick'/remember the MT/MC choice.."*
//
// A source-grep cannot check "the map is NOT changed", which is the whole point of his instruction — so this runs the
// real setChatNetwork()/switchSenseNet() in a sandbox with a stub DOM and storage and asserts the side effects.
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
  assert(end > start, `${startMarker} has no end`);
  return src.slice(start, end + endMarker.length);
}

const store = {
  _v: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
  setItem(k, v) { this._v[k] = String(v); },
  removeItem(k) { delete this._v[k]; },
};

function el() {
  const cls = new Set();
  return {
    style: {},
    dataset: {},
    textContent: '',
    innerHTML: '',
    classList: {
      add: c => cls.add(c),
      remove: c => cls.delete(c),
      contains: c => cls.has(c),
      toggle: (c, on) => { if (on === undefined) { cls.has(c) ? cls.delete(c) : cls.add(c); } else if (on) { cls.add(c); } else { cls.delete(c); } },
      has: c => cls.has(c),
    },
    _cls: cls,
  };
}

const els = {};
const docStub = { getElementById: id => (els[id] = els[id] || el()) };

const stubs = [
  'function normalizeChatNetwork(n){ return n === "mc" ? "mc" : "mt"; }',
  'function _updateMcChatRadioLabel(){}',
  'function renderMcMessages(){}',
  'function _updateMcInput(){}',
  'function _updateMtInput(){}',
  'function _updateNetworkPillVisibility(){}',
  'function _updateMapNodeCount(){}',
  'function _clearTraceLines(){}',
  'function _mtRefreshSenseRouteSelection(){}',
  'function updateMapMarkers(){}',
  'function _updateMapLegend(){}',
  'function renderMcMapMarkers(){}',
  'function renderMcSensePanel(){}',
  'function clearMcPathLines(){}',
  'function _clearMcRxLines(){}',
  'function _mcRxTracesRefresh(){}',
  'let _mcRxTracesOn = false;',
  'let _mcHoverLines = [];',
  'let _activeMtRouteKey = null;',
  'let _activeMtRouteEntryKey = null;',
  'let _mcRxStamp = null;',
  'let leafletMap = null;',
  'let allNodes = [];',
  'let _updateScanBtnVisibility = null;',
  'function _scheduleSignalHeatmapRefresh(){}',
];

const code = [
  'let mapShowMt = true;',
  'let mapShowMc = true;',
  "let chatNetwork = 'mt';",
  "let _senseNet = 'mt';",
  "let _netChoice = 'mt';",
  ...stubs,
  extract('function setChatNetwork(', '\n  }'),
  extract('function switchSenseNet(', '\n  }'),
  `return {
     get mapShowMt() { return mapShowMt; },
     get mapShowMc() { return mapShowMc; },
     get chatNetwork() { return chatNetwork; },
     get senseNet() { return _senseNet; },
     get netChoice() { return _netChoice; },
     setChatNetwork, switchSenseNet,
   };`,
].join('\n');

const api = new Function('document', 'localStorage', code)(docStub, store);

// ── 1. picking in Chat moves the Sense pill and remembers the choice ────────
api.setChatNetwork('mc');
assert(api.chatNetwork === 'mc', 'chat choice applied');
assert(api.senseNet === 'mc', 'the Sense pill must follow the Chat choice (they are paired)');
assert(api.netChoice === 'mc', 'one shared choice');
assert(store.getItem('chatNetwork') === 'mc', 'chat choice persisted');
assert(store.getItem('senseNet') === 'mc', 'sense choice persisted too, so a reload lands the same way');
assert(els['sense-net-mc'] && els['sense-net-mc']._cls.has('active'), 'the Sense MC pill is highlighted');
assert(els['sense-net-mt'] && !els['sense-net-mt']._cls.has('active'), 'the Sense MT pill is not');

// ── 2. the ordinary Map filter is NOT part of the pairing ──────────────────
assert(api.mapShowMt === true && api.mapShowMc === true,
  'THE MAP MUST NOT CHANGE: the Chat pill must leave the Map MT/MC filter alone (both can stay selected)');

// ── 3. and the pairing works the other way, from the Sense pill ────────────
api.switchSenseNet('mt');
assert(api.chatNetwork === 'mt', 'the Chat pill must follow the Sense choice');
assert(api.senseNet === 'mt', 'sense choice applied');
assert(els['chat-net-mt'] && els['chat-net-mt']._cls.has('active'), 'the Chat MT pill is highlighted');
assert(api.mapShowMc === false && api.mapShowMt === true,
  'the Sense pill keeps its own map-focus behaviour (unchanged from before)');

// ── 4. no recursion: a second identical pick is a no-op and terminates ─────
api.setChatNetwork('mt');
api.switchSenseNet('mt');
assert(api.netChoice === 'mt' && api.chatNetwork === 'mt' && api.senseNet === 'mt', 'stable after repeats');

console.log('ok: Chat and Sense pills share one remembered MT/MC choice; the ordinary Map filter is untouched');

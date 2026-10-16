// Chat and Sense pills share ONE remembered MT/MC choice; the ordinary Map filter does not.
//
// Filip, 2026-10-16: *"I mostly look at the Map/Sense tab, not map only.. leave the 'ordinary' map as it is, so both can
// be selected and only pair the Chat and Map/Sense pills, so they 'stick'/remember the MT/MC choice.."*
// Then, after testing it in the browser: *"caht pill on MC, after the refresh it went to MT … So the refresh defults it
// to MT, but switching stucks, so it is fine for now.."* — so a reload must restore his choice, and that is pinned here.
//
// A source-grep cannot check "the map is NOT changed" or "the stored pick survives", which is the whole point of his
// instructions — so this runs the REAL setChatNetwork()/_setNetChoice()/switchSenseNet()/_updateNetworkPillVisibility(),
// and for the reload case the app's own initialisation lines, in a sandbox with a stub DOM and storage.
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

// The app's own initialisation of the three variables, so "a reload restores the choice" is tested against the real
// lines rather than a paraphrase of them. If someone makes the init ignore storage, section 7 fails.
function initLines() {
  const patterns = [
    /^  let chatNetwork\s+= localStorage\.getItem\('chatNetwork'\)[^\n]*/m,
    /^  let _netChoice\s+= localStorage\.getItem\('chatNetwork'\)[^\n]*/m,
    /^  let _senseNet\s+= _netChoice;[^\n]*/m,
  ];
  return patterns.map((re, i) => {
    const m = src.match(re);
    assert(m, `initialisation line ${i + 1} not found — the reload behaviour would have no test`);
    return m[0];
  });
}

// The two boot statements initChat() runs, extracted so the reload case performs the app's real boot steps instead of a
// paraphrase. If initChat stops applying the choice to either pill group, the extraction fails loudly.
function bootLines() {
  const m = src.match(/^  function initChat\(\) \{\n([\s\S]*?)^  \}/m);
  assert(m, 'initChat not found');
  const applyChat = m[1].match(/^\s+setChatNetwork\(chatNetwork\)[^\n]*/m);
  const applySense = m[1].match(/^\s+_setNetChoice\(_netChoice[^\n]*/m);
  assert(applyChat, 'initChat no longer applies the remembered choice to the Chat pills at boot');
  assert(applySense, 'initChat no longer applies the remembered choice to the Sense pills at boot');
  return [applyChat[0].trim(), applySense[0].trim()];
}

function freshStore(seed = {}) {
  const v = { ...seed };
  return {
    _v: v,
    getItem(k) { return Object.prototype.hasOwnProperty.call(v, k) ? v[k] : null; },
    setItem(k, val) { v[k] = String(val); },
    removeItem(k) { delete v[k]; },
  };
}

function el() {
  const cls = new Set();
  return {
    style: {}, dataset: {}, textContent: '', innerHTML: '',
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

const STUBS = [
  'function normalizeChatNetwork(n){ return n === "mc" ? "mc" : "mt"; }',
  'function _updateMcChatRadioLabel(){}',
  'function renderMcMessages(){}',
  'function _updateMcInput(){}',
  'function _updateMtInput(){}',
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
  'function _scheduleSignalHeatmapRefresh(){}',
  'let _mcRxTracesOn = false;',
  'let _mcHoverLines = [];',
  'let _activeMtRouteKey = null;',
  'let _activeMtRouteEntryKey = null;',
  'let _mcRxStamp = null;',
  'let leafletMap = null;',
  'let allNodes = [];',
  'let _updateScanBtnVisibility = null;',
  'let mapShowMt = true;',
  'let mapShowMc = true;',
];

// Connectivity is derived from the two status snapshots exactly as the app derives it — which is what makes the
// "snapshot has not arrived yet" fault reproducible here at all (MT and MC status come from different payloads).
const STATUS_FNS = [
  'let lastStatus = {}, mcLastStatus = {};',
  'function hasConnectedMtRadios(){ return Object.values(lastStatus).some(s => s && s.enabled !== false && s.status === "connected"); }',
  'function hasConnectedMcRadios(){ return Object.values(mcLastStatus).some(s => s && s.status === "connected"); }',
];

function makeApi(store) {
  const els = {};
  const code = [
    ...initLines(),
    ...STATUS_FNS,
    ...STUBS,
    extract('function setChatNetwork(', '\n  }'),
    extract('function _setNetChoice(', '\n  }'),
    extract('function switchSenseNet(', '\n  }'),
    extract('function _updateNetworkPillVisibility(', '\n  }'),
    `function boot() {\n${bootLines().map(l => '     ' + l).join('\n')}\n   }`,
    `return {
       get mapShowMt() { return mapShowMt; },
       get mapShowMc() { return mapShowMc; },
       get chatNetwork() { return chatNetwork; },
       get senseNet() { return _senseNet; },
       get netChoice() { return _netChoice; },
       setChatNetwork, switchSenseNet, boot,
       updatePillVisibility: _updateNetworkPillVisibility,
       setStatus(mt, mc) { lastStatus = mt || {}; mcLastStatus = mc || {}; },
       setConnected(mt, mc) {
         lastStatus = { mt1: { status: mt ? 'connected' : 'disconnected', enabled: true } };
         mcLastStatus = { mc1: { status: mc ? 'connected' : 'disconnected' } };
       },
     };`,
  ].join('\n');
  const doc = { getElementById: id => (els[id] = els[id] || el()) };
  return { api: new Function('document', 'localStorage', code)(doc, store), els };
}

// ── the first session: both radios present ─────────────────────────────────
const store = freshStore();
const { api, els } = makeApi(store);
api.setConnected(true, true);

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
assert(store.getItem('chatNetwork') === 'mt', 'and the operator pick is stored');

// ── 5. a connectivity change moves BOTH pills, without rewriting his pick ──
// Sweep finding (2026-10-16): this used to set _senseNet directly, so after the MT radio went away the Sense pill still
// highlighted MT while the app was on MC — and Chat could disagree with both.
const mapBefore = { mt: api.mapShowMt, mc: api.mapShowMc };   // section 3's pill click legitimately moved it; compare, don't assume
api.setConnected(false, true);      // MT radio gone, MC still there
api.updatePillVisibility();
assert(api.senseNet === 'mc', 'the Sense side follows the only connected network');
assert(api.chatNetwork === 'mc', 'and so does Chat, or the two pills disagree again');
assert(api.netChoice === 'mc', 'the shared choice moved with them');
assert(api.mapShowMt === mapBefore.mt && api.mapShowMc === mapBefore.mc,
  'a FORCED move must not touch the Map MT/MC filter — that pill pair stays the operator\'s own');
assert(els['sense-net-mc']._cls.has('active'), 'the Sense pill highlight follows the choice');
assert(!els['sense-net-mt']._cls.has('active'), 'the old Sense pill is not left highlighted');
assert(els['chat-net-mc']._cls.has('active'), 'the Chat pill highlight follows too');
assert(store.getItem('chatNetwork') === 'mt' && store.getItem('senseNet') === 'mt',
  'a FORCED move must leave the remembered operator pick alone (it is a mirror of reality, not a decision)');

// ── 6. the reported bug: an incomplete snapshot must not move (or save) the choice ──
// His words: "caht pill on MC, after the refresh it went to MT". Cause: MT and MC status arrive in different payloads, so
// at boot one of them is still missing, the other network looked absent, and the auto-switch saved that guess over his pick.
const store2 = freshStore();
const { api: api2 } = makeApi(store2);
api2.setConnected(true, true);
api2.setChatNetwork('mc');                                        // the operator picks MC
assert(store2.getItem('chatNetwork') === 'mc', 'his pick is remembered');

api2.setStatus({ mt1: { status: 'connected', enabled: true } });   // MT's snapshot arrived; MC's has not
api2.updatePillVisibility();
assert(api2.senseNet === 'mc' && api2.chatNetwork === 'mc',
  'an incomplete snapshot must not move the choice (this is what made his reload land on MT)');
assert(store2.getItem('chatNetwork') === 'mc' && store2.getItem('senseNet') === 'mc',
  'and must not overwrite the remembered choice with the guess');

// MC's snapshot arrives; MC is genuinely absent while MT is up, so the session follows MT to stay usable…
api2.setStatus({ mt1: { status: 'connected', enabled: true } }, { mc1: { status: 'disconnected' } });
api2.updatePillVisibility();
assert(api2.senseNet === 'mt' && api2.chatNetwork === 'mt', 'a CONFIRMED absent network moves the session');
// …but his choice survives, so it comes back when MC does.
assert(store2.getItem('chatNetwork') === 'mc' && store2.getItem('senseNet') === 'mc',
  'a forced move must not persist over the operator choice');

// ── 7. a reload restores the remembered network (his exact complaint) ──────
const reloadStore = freshStore({ chatNetwork: 'mc', senseNet: 'mc' });
const { api: api3, els: els3 } = makeApi(reloadStore);
api3.setConnected(true, true);                 // both radios present again
api3.boot();                                   // the app's own boot steps for the remembered choice
api3.updatePillVisibility();
assert(api3.chatNetwork === 'mc', 'a reload lands on the remembered network, not MT');
assert(api3.senseNet === 'mc', 'and the paired Sense pill agrees');
assert(api3.netChoice === 'mc', 'the shared choice reads from storage');
assert(els3['chat-net-mc']._cls.has('active') && els3['sense-net-mc']._cls.has('active'),
  'both pills come up highlighted on the remembered network');

console.log('ok: Chat and Sense share one remembered choice (surviving reloads and connectivity changes); the Map filter is untouched');

// Settings layout refresh (S472) — real functions from app.js run against fakes.
//
// Filip asked for a tidy-up of the whole Settings page that keeps every function working.
// What must hold:
//  - channel lists (MT + MC) show used slots, fold empty ones away by default, and offer
//    "Add channel" on the FIRST free slot; the toggle shows/hides exactly the empty rows;
//  - collapsible groups restore their open/closed state per browser, save it on toggle,
//    bind only once, and survive a localStorage that throws;
//  - _stReveal opens every collapsed group around a target (map pick returns into a group).
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

function line(prefix) {
  const i = src.indexOf(prefix);
  assert(i >= 0, `${prefix} not found in app.js`);
  return src.slice(i, src.indexOf('\n', i));
}

// A list element whose innerHTML is parsed just enough for these checks: rows by class.
function fakeList() {
  const list = { innerHTML: '', textContent: '', _rows: [], _toggle: null };
  list.querySelectorAll = sel => {
    assert(sel === '.st-empty-slot', 'unexpected selector ' + sel);
    return list._rows;
  };
  list.querySelector = sel => {
    assert(sel === '.st-empty-toggle', 'unexpected selector ' + sel);
    return list._toggle;
  };
  list.parse = () => {
    const n = (list.innerHTML.match(/class="st-ch-row[^"]*st-empty-slot"/g) || []).length;
    list._rows = Array.from({ length: n }, () => ({ style: {} }));
    const m = list.innerHTML.match(/class="st-empty-toggle" data-count="(\d+)"/);
    list._toggle = m ? { dataset: { count: m[1] }, textContent: '' } : null;
  };
  return list;
}

const flush = () => new Promise(r => setTimeout(r, 0));

// ── empty-slot folding helpers ─────────────────────────────────────────────
function makeFold() {
  return new Function('document', [
    line('const _stShowEmptySlots'),
    extract('stToggleEmptySlots'),
    extract('_stApplyEmptySlots'),
    'return { stToggleEmptySlots, _stApplyEmptySlots, flags: _stShowEmptySlots };',
  ].join('\n'));
}

(async () => {
  // MT channel list: 3 used + 5 disabled slots
  {
    const lists = { 'node-cfg-channels-list': fakeList() };
    const document = { getElementById: id => lists[id] || null };
    const mk = new Function('document', 'fetch', [
      'const BASE_PATH = "";',
      'let _selectedNodeId = "r1";',
      line('let _chCache'),
      line('const CH_ROLES'),
      line('const CH_ROLE_COLOR'),
      extract('escHtml'),
      line('const _stShowEmptySlots'),
      extract('stToggleEmptySlots'),
      extract('_stApplyEmptySlots'),
      extract('loadNodeChannels'),
      'return { loadNodeChannels, stToggleEmptySlots, _stApplyEmptySlots };',
    ].join('\n'));
    const data = [0, 1, 2, 3, 4, 5, 6, 7].map(i => ({
      index: i, role: i === 0 ? 1 : (i < 3 ? 2 : 0), name: i < 3 ? `ch${i}` : '', psk_set: i < 3,
    }));
    const fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
    // render, then parse the produced markup so the fold step sees real rows
    const list = lists['node-cfg-channels-list'];
    let html = '';
    Object.defineProperty(list, 'innerHTML', {
      get: () => html,
      set: v => { html = v; list.parse(); },
    });
    const t = mk(document, fetch);
    t.loadNodeChannels();
    await flush(); await flush();
    assert(/st-ch-row"[\s\S]*ch0/.test(html), 'MT: a used channel must render as a normal row');
    assert(list._rows.length === 5, `MT: 5 disabled slots must be marked st-empty-slot, got ${list._rows.length}`);
    assert(list._rows.every(r => r.style.display === 'none'), 'MT: empty slots must be folded by default');
    assert(list._toggle && list._toggle.textContent === 'Show 5 empty slots', `MT: toggle label wrong: ${list._toggle && list._toggle.textContent}`);
    assert(html.includes('onclick="openChEdit(3)"') && /openChEdit\(3\)"[^>]*>＋ Add channel/.test(html),
      'MT: "Add channel" must open the FIRST free slot (3)');
    assert((html.match(/>Info\/Share</g) || []).length === 3, 'MT: Info/Share only on the 3 used channels');
    t.stToggleEmptySlots('mt');
    assert(list._rows.every(r => r.style.display === ''), 'MT: toggle must show the empty slots');
    assert(list._toggle.textContent === 'Hide empty slots', 'MT: toggle label must flip');
  }

  // MC channel list: 2 named + 38 unnamed, first free slot is 2
  {
    const lists = { 'mc-channels-list': fakeList() };
    const document = { getElementById: id => lists[id] || null };
    const list = lists['mc-channels-list'];
    let html = '';
    Object.defineProperty(list, 'innerHTML', { get: () => html, set: v => { html = v; list.parse(); } });
    const mk = new Function('document', 'fetch', [
      'const BASE_PATH = "";',
      'let mcSettingsRadioId = "mc1", activeMcRadioId = "mc1";',
      extract('escHtml'),
      extract('jsSafe'),
      line('const _stShowEmptySlots'),
      extract('stToggleEmptySlots'),
      extract('_stApplyEmptySlots'),
      extract('loadMcChannelsList'),
      'return { loadMcChannelsList, stToggleEmptySlots };',
    ].join('\n'));
    const chs = Array.from({ length: 40 }, (_, i) => ({ idx: i, name: i < 2 ? `c${i}` : '', hash: i < 2 ? 'ab' : '37' }));
    const fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ channels: chs }) });
    const t = mk(document, fetch);
    t.loadMcChannelsList('mc1');
    await flush(); await flush();
    assert(list._rows.length === 38, `MC: 38 unnamed slots must be folded, got ${list._rows.length}`);
    assert(list._rows.every(r => r.style.display === 'none'), 'MC: empty slots folded by default');
    assert(list._toggle.textContent === 'Show 38 empty slots', `MC: toggle label wrong: ${list._toggle.textContent}`);
    assert(/openMcChEdit\('mc1',2,'','37'\)"[^>]*>＋ Add channel/.test(html), 'MC: "Add channel" must open the FIRST free slot (2)');
    assert(html.includes("shareMcChannel('mc1',0,'c0','ab')"), 'MC: a named channel keeps Info/Share');
    t.stToggleEmptySlots('mc');
    assert(list._rows.every(r => r.style.display === ''), 'MC: toggle must show the empty slots');
  }

  // Singular label + no toggle when nothing is empty
  {
    const list = fakeList();
    list.innerHTML = '<div class="st-ch-row st-empty-slot"></div><button class="st-empty-toggle" data-count="1"></button>';
    list.parse();
    const f = makeFold()({ getElementById: () => list });
    f._stApplyEmptySlots('mt');
    assert(list._toggle.textContent === 'Show 1 empty slot', `singular label wrong: ${list._toggle.textContent}`);
    const none = fakeList(); none.parse();
    makeFold()({ getElementById: () => none })._stApplyEmptySlots('mt'); // must not throw without a toggle
  }

  // ── collapsible groups: restore, save, bind once, broken storage ───────────
  {
    const mkGroups = (storage, groups) => new Function('document', 'localStorage', [
      line('const _ST_GROUPS_KEY'),
      extract('_stLoadGroupState'),
      extract('_stInitGroups'),
      'return { _stInitGroups };',
    ].join('\n'))({ querySelectorAll: () => groups }, storage);
    const group = (key, open) => {
      const g = { dataset: { key }, open, _l: [] };
      g.addEventListener = (ev, fn) => { assert(ev === 'toggle', 'listens to ' + ev); g._l.push(fn); };
      g.fire = () => g._l.forEach(fn => fn());
      return g;
    };
    const store = { data: { omSettingsGroups: JSON.stringify({ a: false, b: true }) },
      getItem(k) { return this.data[k] ?? null; }, setItem(k, v) { this.data[k] = String(v); } };
    const a = group('a', true), b = group('b', false), c = group('c', true);
    const g = mkGroups(store, [a, b, c]);
    g._stInitGroups();
    assert(a.open === false && b.open === true, 'saved open/closed state must be restored');
    assert(c.open === true, 'a group with no saved state keeps its markup default');
    c.open = false; c.fire();
    assert(JSON.parse(store.data.omSettingsGroups).c === false, 'toggling must save the new state');
    assert(JSON.parse(store.data.omSettingsGroups).b === true, 'saving one group must keep the others');
    g._stInitGroups();
    assert(a._l.length === 1 && c._l.length === 1, 'init twice must not bind twice');

    const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
    const d = group('d', true);
    const g2 = mkGroups(broken, [d]);
    g2._stInitGroups();
    assert(d.open === true, 'with storage unavailable the markup default must stay');
    d.open = false; d.fire(); // must not throw
  }

  // ── _stReveal opens every collapsed ancestor group ─────────────────────────
  {
    const outer = { open: false, parentElement: null };
    const inner = { open: false };
    inner.parentElement = { closest: sel => (sel === 'details' ? outer : null) };
    outer.parentElement = { closest: () => null };
    const target = { closest: sel => (sel === 'details' ? inner : null), scrollIntoView() { target.scrolled = true; } };
    const reveal = new Function('document', 'setTimeout', [extract('_stReveal'), 'return _stReveal;'].join('\n'))(
      { getElementById: id => (id === 'x' ? target : null) }, fn => fn());
    reveal('x');
    assert(inner.open && outer.open, 'all enclosing groups must open');
    assert(target.scrolled, 'the target must be scrolled into view');
    reveal('missing'); // must not throw
  }

  console.log('ok: settings layout (channel folding MT+MC, remembered groups, reveal)');
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });

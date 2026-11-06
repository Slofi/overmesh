// Chat message header: clicking the hop count opens a hop list (EST-322).
//
// Filip asked that the hop badge on a chat message list the repeaters by name,
// with a button to the map, instead of jumping straight to the map. This test
// runs the real row builder and summary text against fake packet paths:
//  - one row per hop in the order the packet travelled;
//  - a resolved repeater shows its name, with the raw hash below it;
//  - an unresolved hop shows the raw id and never an invented name;
//  - an ambiguous match shows the raw id, with the candidate only as "possibly";
//  - a direct message says so, and flood mode says the packet named no repeaters;
//  - names are HTML-escaped in the popup and in the hover title;
//  - the chat badges open the popup, and the popup has all three close paths.
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

const code = [
  extract('_mcRawHopHashesForEntry'),
  extract('_mcSortedPathHops'),
  extract('_mcHopListRows'),
  extract('_hopPopoverRowsHtml'),
  extract('_mcHopSummary'),
  extract('_hopHoverTitle'),
  extract('escHtml'),
  'return { rows: _mcHopListRows, html: _hopPopoverRowsHtml, summary: _mcHopSummary, title: _hopHoverTitle };',
].join('\n');

const api = new Function(code)();

// --- order and names: packet order is aa0, bb0, cc0 (sender to this radio)
const entry = { kind: 'message', hopLen: 3, rawPath: 'aabbcc', pathHashSize: 1 };
const resolved = {
  hops: [
    { hash: 'aa', name: 'RPTR A', pointIndex: 3, confidence: 'unique-1B' },
    { hash: 'cc', name: 'RPTR C', pointIndex: 1, confidence: 'unique-1B' },
  ],
};
const rows = api.rows(entry, resolved);
assert(rows.length === 3, `expected 3 hop rows, got ${rows.length}`);
assert(rows[0].primary === 'RPTR A', `first row must be RPTR A, got ${rows[0].primary}`);
assert(rows[1].primary === 'bb', `unknown hop must show its raw id, got ${rows[1].primary}`);
assert(rows[2].primary === 'RPTR C', `last row must be RPTR C, got ${rows[2].primary}`);
assert(rows[0].detail === 'aa', 'a named hop must still show its raw id below the name');
assert(rows[1].unresolved === true, 'an unknown hop must be marked unresolved');
assert(!rows[1].detail || !/RPTR/.test(rows[1].detail), 'an unknown hop must not borrow another name');

// --- an ambiguous match shows the raw id, and the candidate only as a maybe
const ambiguousRows = api.rows(entry, {
  hops: [{ hash: 'aa', name: 'Maybe RPTR', pointIndex: 1, ambiguous: true }],
});
assert(ambiguousRows[0].primary === 'aa', 'an ambiguous hop must lead with the raw id');
assert(/possibly Maybe RPTR/.test(ambiguousRows[0].detail), 'the ambiguous candidate must be worded as possibly');
assert(/ambiguous/.test(ambiguousRows[0].detail), 'the ambiguous row must say it is ambiguous');

// --- resolved hops still appear when the raw path could not be split
const noRaw = api.rows({ kind: 'message', hopLen: 1 }, { hops: [{ hash: 'dd', name: 'RPTR D', pointIndex: 1 }] });
assert(noRaw.length === 1 && noRaw[0].primary === 'RPTR D', 'a resolved hop must not be lost when rawPath is missing');

// --- summary words: direct, flood, counted, and unknown
assert(/Heard direct/.test(api.summary(0, 'DIRECT', null)), 'a direct message must say it was heard direct');
assert(/Flood mode/.test(api.summary(255, 'flood mode', { flood: true })), 'flood mode must be named');
assert(/Flood mode/.test(api.summary(2, '2 hops', { flood: true })), 'a flood path must be named even with a hop count');
assert(/Direct route/.test(api.summary(255, 'direct route', null)), 'the 255 direct route label must say direct');
assert(/3 hops/.test(api.summary(3, '3 hops', {})), 'a routed message must state its hop count');
assert(/No hop information/.test(api.summary(-1, '', null)), 'a message with no hop data must say so');

// --- HTML: rows in order, escaped, with a numbered list
const evil = api.rows(entry, {
  hops: [
    { hash: 'aa', name: '<img src=x onerror=alert(1)>', pointIndex: 1 },
    { hash: 'cc', name: 'RPTR C', pointIndex: 2 },
  ],
});
const html = api.html(evil);
assert(html.indexOf('&lt;img') >= 0, 'a contact name must be escaped in the popup');
assert(html.indexOf('<img') < 0, 'a contact name must not inject markup');
assert(html.indexOf('RPTR C') > html.indexOf('&lt;img'), 'rows must keep the packet order');
assert(html.includes('hop-pop-num'), 'each hop must be numbered');

// --- hover title lists the names, or says direct
const title = api.title({ summary: 'Heard with 3 hops.', rows });
assert(/RPTR A/.test(title) && /RPTR C/.test(title) && /bb/.test(title), 'the hover title must list every hop name and raw id');
assert(/Click for the hop list/.test(title), 'the hover title must say the badge opens a hop list');
const directTitle = api.title({ summary: 'Heard direct. No repeater hops.', rows: [] });
assert(/Heard direct/.test(directTitle), 'the hover title must say direct when there are no repeaters');

// --- MT: names can come only from a cached traceroute, never from packet hops
const mtCode = [
  extract('_mtNodeName'),
  extract('_mtHopListRows'),
  extract('_mtHopSummary'),
  'return { rows: _mtHopListRows, summary: _mtHopSummary };',
].join('\n');
const mt = new Function('allNodes', mtCode)([
  { id: '!a1', long_name: 'RPTR A', radio_id: 'r1' },
  { id: '!a2', long_name: 'RPTR B', radio_id: 'r1' },
]);

const mtMeta = {
  hops: 2,
  radioId: 'r1',
  cached: { data: { routeBackIds: ['!node', '!a1', '!a2', 'local'], routeIds: ['local', '!a2', '!a1', '!node'] } },
};
const mtRows = mt.rows(mtMeta, false);
assert(mtRows.length === 2, `expected 2 MT rows, got ${mtRows.length}`);
assert(mtRows[0].primary === 'RPTR A' && mtRows[1].primary === 'RPTR B', 'MT rows must follow the received direction');
assert(mtRows[0].detail === '!a1', 'an MT hop must show its node id below the name');
const mtFallback = mt.rows({ hops: 1, cached: { data: { routeIds: ['local', '!a1', '!node'] } } }, false);
assert(mtFallback.length === 1 && mtFallback[0].primary === 'RPTR A', 'MT must reverse the toward-node route for a received message');
const mtUnknown = mt.rows({ hops: 1, cached: { data: { routeBackIds: ['!node', '!zz', 'local'] } } }, false);
assert(mtUnknown[0].primary === '!zz' && mtUnknown[0].unresolved === true, 'an unknown MT relay must show its raw id');
assert(/Heard direct/.test(mt.summary({ hops: 0 }, false)), 'a direct MT message must say it was heard direct');
assert(/Sent direct/.test(mt.summary({ hops: 0 }, true)), 'a direct sent MT message must say sent direct');
assert(/does not name the repeaters/.test(mt.summary({ hops: 3 }, false)), 'MT packet hops alone must not pretend to name repeaters');
assert(/cached traceroute/.test(mt.summary(mtMeta, false)), 'a cached MT traceroute must be named as the source');

// --- the two chat badges open the popup instead of jumping to the map
assert(src.includes("openMcHopPopover(event,'"), 'the MC hop badge must open the hop popover');
assert(src.includes("openMtHopPopover(event,'"), 'the MT hop badge must open the hop popover');
assert(src.includes("'Show on map'") || src.includes('>Show on map<'), 'the popup must offer a Show on map button');
assert(src.includes("e.key === 'Escape'") && src.includes('_closeHopPopover'), 'Escape must close the popup');
assert(src.includes("'pointerdown'"), 'a tap outside must close the popup');
assert(src.includes('hop-pop-close'), 'the popup must have a close button');
assert(src.includes('inputGuard'), 'the popup must stay above the chat input bar at the bottom');

// --- the popup's look is the map's own popup (Filip, S487): tapping a repeater
// on the map is the reference card, so the hop list borrows its design instead
// of inventing a second one. A bare × in the corner, not a worded Close, the
// small network chip beside the title, and the existing dark background kept.
assert(!/>Close</.test(src), 'the close control must no longer be a worded Close button');
assert(/class="hop-pop-close"[^>]*>&times;</.test(src), 'the close control must be a bare × glyph');
assert(/class="hop-pop-close"[^>]*aria-label="Close hop list"/.test(src), 'the × must still be named for a screen reader');
assert(/hop-pop-badge hop-pop-badge-/.test(src), 'the header must carry the small network chip the map popup uses for its network badges');
assert(src.includes("net: 'MC'") && src.includes("net: 'MT'"), 'each hop model must say which network it came from, so the chip is honest');

const css = fs.readFileSync(path.join(REPO, 'static/css/app.css'), 'utf8');
const cssRule = name => {
  const start = css.indexOf(name + ' {');
  assert(start >= 0, `CSS rule ${name} not found`);
  return css.slice(start, css.indexOf('\n', start));
};
const panel = cssRule('.hop-popover');
assert(/background:var\(--bg2\)/.test(panel), 'the hop popover must keep the dark background, never the white one');
assert(/border-radius:var\(--surface-radius\)/.test(panel), 'the hop popover must take its corners from the shared surface token');
assert(/box-shadow:var\(--surface-bubble-shadow\)/.test(panel), 'the hop popover must take its shadow from the shared bubble token');
assert(/--surface-radius:\s*12px/.test(css), 'the shared radius token must stay 12px (the map popup and the dialogs use it)');
assert(/--surface-bubble-shadow:\s*0 3px 14px rgba\(0,0,0,0\.4\)/.test(css), 'the shared bubble shadow must stay the map popup shadow');
// One close control across the app (S487): the hop popover uses the shared rule,
// which carries the fingertip-sized hit area for the CD touchscreen.
const closeGroup = css.slice(css.indexOf('.surface-close, .modal-close'), css.indexOf('.hop-pop-close {'));
assert(/\.hop-pop-close/.test(closeGroup), 'the hop close must be part of the one shared close rule');
assert(/min-width:\s*34px/.test(closeGroup), 'the shared close needs a 34px box');
assert(/inset:\s*-5px/.test(closeGroup), 'the shared close needs a 44px hit area around the 34px box');
assert(/position:absolute/.test(cssRule('.hop-pop-close')), 'the hop close must still sit in the popup corner');

// --- a hop list belongs to the chat it was opened from (EST-325). A tab
// switch, a radio switch or a chat-network switch replaces that chat under the
// popup, so the popup must go with it instead of floating over another screen
// with a Show on map button for a message you can no longer see.
assert(src.includes('if (name !== currentTab) _closeHopPopover();'),
  'switching tab must close the hop popup');
const radioFn = src.slice(src.indexOf('function toggleRadioSelection('), src.indexOf('function switchRadio('));
assert(radioFn.includes('_closeHopPopover()'), 'switching radio must close the hop popup');
const netFn = src.slice(src.indexOf('function setChatNetwork('), src.indexOf('function setChatNetwork(') + 2500);
assert(netFn.includes('_closeHopPopover()'), 'switching the chat network (MC/MT) must close the hop popup');

// --- the same hash twice in one path is two hops, not one (EST-325). The list
// must show both, in packet order, and never collapse them into one row.
const repeatRows = api.rows(
  { kind: 'message', hopLen: 3, rawPath: 'aabbaa', pathHashSize: 1 },
  { hops: [
    { hash: 'aa', name: 'RPTR A', pointIndex: 1 },
    { hash: 'bb', name: 'RPTR B', pointIndex: 2 },
  ] }
);
assert(repeatRows.length === 3, `a repeated hash must keep both rows, got ${repeatRows.length}`);
assert(repeatRows[0].primary === 'RPTR A' && repeatRows[1].primary === 'RPTR B' && repeatRows[2].primary === 'RPTR A',
  'the repeated hash must keep its packet position: ' + repeatRows.map(r => r.primary).join(' > '));

console.log('ok: chat hop popover rows, names, summary and close paths');

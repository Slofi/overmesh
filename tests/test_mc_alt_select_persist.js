// S504: choosing a path in the MC activity popup is a SELECTION, not a hover.
//
// Filip: "when I click on alternate paths, those do show up, but only while the
// mouse is hovering over it. And also the main path is gone after that.. So
// change it, so I can click on alternate and then back to the main and the line
// drawn changes to represent selected path."
//
// Two defects pinned here:
//  1. The pin was resolved by object identity. A later packet for the same
//     message re-creates the entry object ({...old, ...new}), so the identity
//     lookup failed and every hover-leave cleared the map. It resolves by uid.
//  2. Hover-leave had no branch for "the message is pinned but no alternate is",
//     so leaving a copy wiped the main path. It restores the selection now.
// The list also gained an explicit "Main path" block (copyIdx -1) to go back.
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

// -- 1. the pin survives a re-created entry object -------------------------
function buildResolver() {
  const body = [
    'let _mcSenseLogEntries = [];',
    'let _mcAltPathPinned = null;',
    'let _mcEntryUidSeq = 0;',
    extract('_mcEntryUid'),
    extract('_mcAltPinIndex'),
    'return {',
    '  uid: _mcEntryUid,',
    '  resolve: function () { return _mcAltPinIndex(); },',
    '  set: function (entries, pin) { _mcSenseLogEntries = entries; _mcAltPathPinned = pin; },',
    '};',
  ].join('\n');
  return new Function(body)();
}

const r = buildResolver();
const first = { kind: 'message', text: 'hello', channel: 1 };
const uid = r.uid(first);
assert(!!uid, 'an entry gets a uid');

// The log rebuilds the entry on the next packet: the spread keeps the uid, the
// object identity does not.
const recreated = { ...first };
assert(recreated !== first, 'the re-created entry must be a different object');
r.set([recreated], { entry: first, uid, copyIdx: 1, domIdx: 0 });
assert(r.resolve() === 0, `the pin must follow the uid to the new object (got ${r.resolve()})`);

// Control: the same object in place still resolves.
r.set([first], { entry: first, uid, copyIdx: 1, domIdx: 0 });
assert(r.resolve() === 0, 'a pin for an entry that is still there resolves');

// Control: an entry that is really gone resolves to -1, so the caller does nothing.
r.set([], { entry: first, uid, copyIdx: 1, domIdx: 0 });
assert(r.resolve() === -1, 'a pin whose entry is gone must resolve to -1');

// -- 2. the source does what the behaviour needs ---------------------------
const hover = extract('_mcAltCopyHover');
assert(hover.includes('_mcAltPinIndex()'), 'hover-leave must consult the pin');
assert(hover.includes('_mcShowMainPathForEntry(mainIdx'),
  'hover-leave must restore the MAIN path when only the message is pinned');
assert(hover.includes('_mcLogPinnedIdx !== null'),
  'hover-leave must check whether a message is pinned at all');
assert(hover.includes('copyIdx === -1'), 'the main block must be recognised in hover');
assert(hover.includes('_mcEntryUid('),
  'the pinned-same check must compare uids, not object identity');

const click = extract('_mcAltCopyClick');
assert(click.includes('copyIdx === -1'), 'clicking the main block must be handled');
assert(click.includes('uid: _mcEntryUid(entry)'), 'the pin must carry the entry uid');

const mainFn = extract('_mcShowMainPathForEntry');
assert(mainFn.includes('showMcHoverPath(pathToShow, true)'),
  'the main path must be drawn through the same map call as the copies');
assert(mainFn.includes('Shown on map'), 'the main block must say it is on the map');

const list = extract('_mcAltPathsHtml');
assert(list.includes('data-copy-idx="-1"'), 'the list must render a Main path block');
assert(list.includes('_mcAltCopyClick(event,${entryIdx},-1)'), 'the Main block must be clickable');
assert(list.includes('mc-alt-copy-status-${entryIdx}-main'), 'the Main block needs its own status line');
assert(list.includes('Main path'), 'the Main block needs its label');

const focus = extract('_mcFocusAltCopyHop');
assert(focus.includes('uid: _mcEntryUid(entry)'), 'the hop focus pin must carry the uid too');

console.log('ok: alternate path choice is a selection, and the main path can be picked again');

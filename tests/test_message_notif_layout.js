const fs = require('fs');

const src = fs.readFileSync('/home/slofi/overmesh/static/js/app.js', 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// Exercise the real layout function (extracted + evaluated), not a copy of it.
const m = src.match(/function _msgNotifParts\(system, \{[\s\S]*?\n  \}/);
assert(m, '_msgNotifParts() must exist');
const escHtml = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const _msgNotifParts = new Function('escHtml', 'return ' + m[0])(escHtml);

// row 1 = system + channel, row 2 = sender, row 3 = message
let p = _msgNotifParts('MT', { chanName: 'Slovenija', sender: 'Filip', text: 'hello there' });
assert(p.title === 'MT Slovenija', 'MT channel title (got ' + p.title + ')');
assert(p.plain === 'Filip\nhello there', 'plain body rows (got ' + JSON.stringify(p.plain) + ')');
assert(p.html === 'Filip<br>hello there', 'html body rows (got ' + p.html + ')');

p = _msgNotifParts('MC', { chanName: "Don't Panic", sender: 'EDC-3', text: 'test' });
assert(p.title === "MC Don't Panic", 'MC channel title (got ' + p.title + ')');
assert(p.html === 'EDC-3<br>test', 'MC body rows');

// DMs: no channel name, still sender + message
p = _msgNotifParts('MT', { isDm: true, chanName: 'ignored', sender: 'Filip', text: 'psst' });
assert(p.title === 'MT DM', 'DM title (got ' + p.title + ')');
p = _msgNotifParts('MC', { isDm: true, sender: 'EDC-3', text: 'hi' });
assert(p.title === 'MC DM', 'MC DM title');

// no sender / no channel name must not produce empty rows
p = _msgNotifParts('MT', { sender: '', text: 'x' });
assert(p.title === 'MT', 'no channel -> bare system title');
assert(p.plain.startsWith('Unknown\n'), 'missing sender shows Unknown, not a blank row');

// escaping: sender and text must be escaped in the HTML body (XSS-safe)
p = _msgNotifParts('MC', { chanName: 'c', sender: '<b>x</b>', text: '<img src=x onerror=1>' });
assert(p.html.indexOf('&lt;b&gt;x&lt;/b&gt;') === 0, 'sender escaped in toast body');
assert(p.html.indexOf('<img') === -1, 'message escaped in toast body');

// both call sites must use the shared layout
assert(/const _p = _msgNotifParts\('MT', \{/.test(src), 'MT notification must use the shared layout');
assert(/const _p = _msgNotifParts\('MC', \{/.test(src), 'MC notification must use the shared layout');
assert(/sendNotif\(title, _p\.plain,/.test(src), 'system notification must get the plain multi-row body');
assert(/maybeShowInAppMessage\(title, _p\.html,/.test(src), 'in-app toast must get the HTML multi-row body');
assert(/function _mcSenderName\(data\)/.test(src), 'MC sender must be resolved from the contact list');

// MC sender-name resolution must never credit an ambiguous pubkey prefix to one
// contact (showing the wrong sender is worse than showing the prefix).
const mp = src.match(/function _pickMcSenderName\(map, dmCache, fid\) \{[\s\S]*?\n  \}/);
assert(mp, '_pickMcSenderName() must exist');
const _pickMcSenderName = eval('(' + mp[0] + ')');

const FULL = 'f'.repeat(58);
const EDC  = { id: 'abc123', full_key: 'abc123' + FULL, long_name: 'EDC-3' };
// Real ambiguity: neither id equals the incoming short prefix, but both full keys
// start with it — the name must NOT be guessed.
const AMB1 = { id: 'abc111', full_key: 'abc111' + FULL, long_name: 'Wrong One' };
const AMB2 = { id: 'abc222', full_key: 'abc222' + FULL, long_name: 'Wrong Two' };

assert(_pickMcSenderName({ k: EDC }, {}, 'abc123') === 'EDC-3', 'exact id match wins');
assert(_pickMcSenderName({ k: EDC }, {}, EDC.full_key) === 'EDC-3', 'exact full_key match wins');
assert(_pickMcSenderName({ k: EDC }, {}, 'abc12') === 'EDC-3', 'single prefix match resolves');
assert(_pickMcSenderName({ a: AMB1, b: AMB2 }, {}, 'abc') === 'abc',
       'ambiguous prefix falls back to the prefix (got ' +
       _pickMcSenderName({ a: AMB1, b: AMB2 }, {}, 'abc') + ')');
assert(_pickMcSenderName({ k: EDC }, { abc123: 'Cached DM Name' }, 'abc123') === 'Cached DM Name',
       'DM name cache takes precedence');
assert(_pickMcSenderName({}, {}, '') === '?', 'no sender -> ?');
assert(_pickMcSenderName({ k: { id: 'x', full_key: 'x', long_name: '' } }, {}, 'x') === 'x',
       'a nameless contact falls back to the id, not an empty row');

console.log('ok: message pop-up layout (system + channel / sender / message) for MT and MC');

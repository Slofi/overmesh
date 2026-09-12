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
assert(/function _mcSenderInfo\(m\)/.test(src), 'MC sender must be resolved by the existing _mcSenderInfo() helper');

// MC channel messages carry NO pubkey at all — the library's payload has
// type/channel_idx/txt_type/sender_timestamp/text only — so from_id is literally
// '?' and the sender lives in the text prefix. Filip's live pop-up showed:
//     MC Public / ? / IW3QVZ_mau_provv.: @[IK4FMY] ciao, a Trieste 8 hop
// i.e. a "?" sender row and the name still inside the message. Drive the real
// resolver and the real layout with that exact message.
const si = src.match(/function _mcSenderInfo\(m\) \{[\s\S]*?\n  \}/);
assert(si, '_mcSenderInfo() must exist');
const mkSenderInfo = find => new Function('_mcFindContact', 'return ' + si[0])(find);

const mt = src.match(/function _mcMsgText\(m, senderName\) \{[\s\S]*?\n  \}/);
assert(mt, '_mcMsgText() must exist');
const _mcMsgText = eval('(' + mt[0] + ')');

const live = {
  text: 'IW3QVZ_mau_provv.: @[IK4FMY] ciao, a Trieste 8 hop',
  from_id: '?',
  radio_id: 'r1',
};
const noContacts = mkSenderInfo(() => null);
const liveSender = noContacts(live).name;
assert(liveSender === 'IW3QVZ_mau_provv.',
       'sender must come from the message prefix when there is no pubkey (got ' + liveSender + ')');

const liveParts = _msgNotifParts('MC', {
  chanName: 'Public',
  sender: liveSender,
  text: _mcMsgText(live, liveSender),
});
assert(liveParts.title === 'MC Public', 'row 1 = system + channel');
assert(liveParts.plain === 'IW3QVZ_mau_provv.\n@[IK4FMY] ciao, a Trieste 8 hop',
       'rows 2/3 = sender then message only (got ' + JSON.stringify(liveParts.plain) + ')');
assert(liveParts.plain.split('\n')[0] !== '?', 'row 2 must never be a bare "?" for channel messages');
assert(liveParts.plain.indexOf('@[IK4FMY]') > 0, 'message body is preserved');

// A contact hit (matched by the embedded name) wins over the raw prefix
const known = mkSenderInfo((rid, fid, name) => (name === 'IW3QVZ_mau_provv.' ? { long_name: 'IW3QVZ (Mauro)' } : null));
assert(known(live).name === 'IW3QVZ (Mauro)', 'contact match by name takes precedence');

// Hex-only senders still read as a hex id, not "?"
const hexSender = mkSenderInfo(() => null)({ text: 'E905518E: test', from_id: '?', radio_id: 'r1' });
assert(hexSender.name === 'E905518E', 'hex-prefixed sender is used as-is (got ' + hexSender.name + ')');

// The notification must use this resolver — and my old prefix-only helper is gone
assert(/const _sender = _mcSenderInfo\(data\)\.name;/.test(src),
       'the MC notification must resolve the sender with _mcSenderInfo()');
assert(!/_mcSenderName|_pickMcSenderName/.test(src),
       'the removed prefix-only helper must not linger');

// ── Alerts panel parity (Filip: "fix those two") ────────────────────────────
// The Alerts record must mirror the pop-up (sender row) ...
assert(/_logAlert\('message', title, _p\.html, \{\s*\n\s*network: 'mt',/.test(src),
       'MT alert body must carry the sender row and an mt network tag');
assert(/_logAlert\('message', title, _p\.html, \{ network: 'mc',/.test(src),
       'MC alert body must carry the sender row and an mc network tag');

// ... and an Alerts click must jump to the right conversation on BOTH networks.
assert(/a\.meta\.network === 'mt'/.test(src), 'the Alerts panel must dispatch MT alerts');
assert(/jumpToMtChat\(/.test(src), 'jumpToMtChat() must exist and be used');

// Drive the real jumpToMtChat: channel message -> channel tab, DM -> dm tab.
const jm = src.match(/function jumpToMtChat\(channel, fromId\) \{[\s\S]*?\n  \}/);
assert(jm, 'jumpToMtChat() must exist');
const calls = [];
const jumpToMtChat = new Function(
  'setChatNetwork', 'switchTab', 'switchChatChannel', 'document', 'setTimeout',
  'return ' + jm[0]
)(
  net => calls.push('network:' + net),
  tab => calls.push('tab:' + tab),
  idx => calls.push('channel:' + idx),
  { getElementById: () => ({ scrollTop: 0, scrollHeight: 42 }) },
  fn => fn()   // run timers synchronously
);
jumpToMtChat(3, null);
assert(calls.includes('network:mt'), 'jump must select the MT network');
assert(calls.includes('tab:chat'), 'jump must open the Chat tab');
assert(calls.includes('channel:3'), 'channel message must select that channel');
calls.length = 0;
jumpToMtChat(0, '!abc123');
assert(calls.includes('channel:dm:!abc123'), 'DM must select the dm: tab (got ' + calls.join(',') + ')');

// ── Sweep #6: caches and alert text ─────────────────────────────────────────
// The toast dedupe map is keyed by tag, and message tags carry the message id, so
// it must be pruned or it grows for the life of the page.
const pm = src.match(/function _pruneToastSeen\(map, now, windowMs = 15000, max = 500\) \{[\s\S]*?\n  \}/);
assert(pm, '_pruneToastSeen() must exist');
const _pruneToastSeen = eval('(' + pm[0] + ')');

const small = new Map([['a', 1000], ['b', 1001]]);
_pruneToastSeen(small, 1100);
assert(small.size === 2, 'a small map is left alone');

// all fresh -> keep the newest ~80% of the cap, drop the oldest
const fresh = new Map();
for (let i = 0; i < 1000; i++) fresh.set('t' + i, 100000 + i);
_pruneToastSeen(fresh, 100999);
assert(fresh.size === 400, 'over-cap map trimmed to 80% of max (got ' + fresh.size + ')');
assert(fresh.has('t999') && fresh.has('t600') && !fresh.has('t0'),
       'the newest entries survive, the oldest go');

// stale entries go first, and fresh ones are never dropped to satisfy the cap
const mixed = new Map();
for (let i = 0; i < 600; i++) mixed.set('old' + i, 1000);          // far older than the window
mixed.set('fresh1', 100500); mixed.set('fresh2', 100500);
_pruneToastSeen(mixed, 100500);
assert(mixed.size === 2 && mixed.has('fresh1') && mixed.has('fresh2'),
       'stale entries pruned first, fresh ones kept (got ' + mixed.size + ')');

// Alert bodies are HTML now, so the Log prefill must turn <br> into a real newline
// BEFORE stripping tags, or the sender row and the message run together.
const ap = src.match(/function _alertPlainText\(html\) \{[\s\S]*?\n  \}/);
assert(ap, '_alertPlainText() must exist');
const _alertPlainText = eval('(' + ap[0] + ')');
assert(_alertPlainText('EDC-3<br>hello there') === 'EDC-3\nhello there',
       'sender row and message keep their line break (got ' +
       JSON.stringify(_alertPlainText('EDC-3<br>hello there')) + ')');
assert(_alertPlainText('a<br/>b<br />c') === 'a\nb\nc', 'all <br> spellings become newlines');
assert(_alertPlainText('<b>x</b> plain') === 'x plain', 'other tags still stripped');
assert(_alertPlainText(null) === '', 'null body is safe');
assert(_alertPlainText('single line') === 'single line', 'single-line bodies unchanged');

const uses = (src.match(/_alertPlainText\(a\.body\)/g) || []).length;
assert(uses === 4, 'every alert->Log path must use the helper (found ' + uses + ')');

console.log('ok: message pop-up layout (system + channel / sender / message) for MT and MC');

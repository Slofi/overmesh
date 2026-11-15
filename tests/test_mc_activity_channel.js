// S504: the Map/Sense "MC activity" panel must say WHICH CHANNEL a chat row
// came from. Before this, a channel message and a DM rendered the same, and the
// channel name the entry already carried was never shown.
//
// This runs the real entry builder and the real row markup against fake data:
//  - a channel message carries its channel, its resolved name and subtype;
//  - an unknown channel number still names itself readably;
//  - a DM says DM instead of a channel;
//  - the rendered row contains the channel chip;
//  - every field the panel showed before is still on the entry.
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
  'let mcContacts = {};',
  'const mcKnownChannels = { 0: "Public", 2: "Don\'t Panic" };',
  'function _mcResolveMessageContact() { return null; }',
  'function _mcMsgText(data, name) { return (name ? name + ": " : "") + (data.text || ""); }',
  'function _formatAppTime() { return "12:00:00"; }',
  'function _mcSenseRadioPos() { return [null, null]; }',
  'function decodeMcEventPath() { return null; }',
  'function decodeMcRelayPath() { return null; }',
  'function decodeMcPath() { return null; }',
  'function _mcReversePathResult(p) { return p; }',
  extract('_mcBuildSenseMessageEntry'),
  'return _mcBuildSenseMessageEntry;',
].join('\n');

const build = new Function(code)();

// 1. Channel message: channel index, resolved name, subtype.
const chanEntry = build({
  type: 'mc_message',
  radio_id: 'r1',
  subtype: 'channel',
  channel: 2,
  from_id: '?',
  text: 'hello all',
  ts: 1000,
  path_len: 1,
});
assert(chanEntry.channel === 2, 'the entry must keep the channel index');
assert(chanEntry.subtype === 'channel', 'the entry must carry its subtype');
assert(chanEntry.chanName === "Don't Panic", `channel name not resolved: ${chanEntry.chanName}`);
assert(chanEntry.text.includes('hello all'), 'the message text must still be there');

// 2. Unknown channel number still reads clearly.
const unknown = build({ type: 'mc_message', radio_id: 'r1', subtype: 'channel', channel: 7, from_id: '?', text: 'x', ts: 2 });
assert(unknown.chanName === 'CH 7', `unknown channel label wrong: ${unknown.chanName}`);

// 3. A DM is labelled as a DM, not as a channel.
const dmEntry = build({ type: 'mc_message', radio_id: 'r1', subtype: 'dm', channel: 0, from_id: 'abcd1234', text: 'hi', ts: 3 });
assert(dmEntry.subtype === 'dm', 'a DM keeps its subtype');
assert(dmEntry.chanName === 'DM', `DM label wrong: ${dmEntry.chanName}`);

// 4. Everything the panel showed before is still on the entry.
['name', 'text', 'ts', 'hopLen', 'pathResult', 'routeQuality', 'pathHashSize', 'rx_rssi', 'rx_snr'].forEach(k => {
  assert(Object.prototype.hasOwnProperty.call(chanEntry, k), `entry lost field "${k}"`);
});

// 5. The rendered row shows the chip (source-level, since renderMcSenseLog needs the DOM).
const messageBranch = src.slice(src.indexOf("if (e.kind === 'message') {"), src.indexOf("if (e.kind === 'sent_message')"));
assert(messageBranch.includes('e.chanName ||'), 'the message row must fall back to a channel label');
assert(messageBranch.includes("e.subtype === 'dm' ? 'DM'"), 'the message row must label DMs');
assert(messageBranch.includes('mc-route-badge'), 'the channel label must reuse the existing badge style');

// 6. The search box can find a row by its channel name.
assert(extract('_mcSenseLogEntrySearchText').includes('entry.chanName'), 'search text must include the channel name');

console.log('ok: MC activity rows name their channel');

// MC Traces: a packet heard DIRECT (0 hops) draws sender → us when it can be
// matched to the received message (S470) — the real functions extracted from
// app.js and run against fakes, not grepped.
//
// What must hold:
//  - a direct GRP_TXT whose time, RSSI and SNR all match a received channel
//    message from a contact with a known position becomes a sender → us line;
//  - time alone never matches: a packet on a channel we hold no key for (same
//    seconds, different signal) keeps the ring;
//  - a packet that HAS relay hops, merely unresolved, never gets a straight
//    sender line (it would hide the relays);
//  - two different senders matching = no line; wrong radio, wrong payload kind,
//    out of the time window, missing signal values, unknown or same-as-radio
//    position = no line.
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

const RADIO = [46.0409, 14.4981];
const EDC3 = [46.0549, 14.4925];
const OTHER = [46.0500, 14.5100];

const build = new Function('entries', [
  'let _mcSenseLogEntries = entries;',
  'const activeMcRadioId = "R1";',
  'const MC_RX_DIRECT_MATCH_S = 20;',
  `function _mcSenseRadioPos() { return [${RADIO[0]}, ${RADIO[1]}]; }`,
  'function _mcPickPathHopResolution() { return null; }',   // no hop resolves
  extract('_mcRxHops'),
  extract('_mcRxDirectSender'),
  extract('_mcRxPathResult'),
  'return _mcRxPathResult;',
].join('\n'));

function msg(over = {}) {
  return {
    kind: 'message', name: 'EDC-3 (SI)', radioId: 'R1', fromId: '?', hopLen: 0,
    lat: EDC3[0], lon: EDC3[1], ts_epoch: 1000, rx_rssi: -3, rx_snr: 6.5, snr: 6.5, ...over,
  };
}
function row(over = {}) {
  return { ts: 1000, path: '', path_len: 0, path_hash_size: 2, payload_type: 'GRP_TXT',
           route_type: 'FLOOD', rssi: -3, snr: 6.5, ...over };
}
const run = (entries, r) => build(entries)(r, new Map());
const isRing = res => res && res.direct === true && res.points.length === 1;

// 1. the real case: EDC-3 → CD channel message, heard direct
{
  const res = run([msg()], row());
  assert(res && res.direct === false, 'matched direct packet is drawn as a line, not a ring');
  assert(res.sender === 'EDC-3 (SI)', 'line names the sender: ' + (res && res.sender));
  assert(JSON.stringify(res.points) === JSON.stringify([EDC3, RADIO]), 'line runs sender → our radio: ' + JSON.stringify(res.points));
  assert(res.totalHops === 0 && res.hopMeta.length === 0, 'no relay hops are claimed');
}
// clock skew inside the window still matches
assert(run([msg({ts_epoch: 1015})], row()).sender, 'a match 15 s apart (clock skew) still counts');

// 2. same seconds, different signal (a packet on a channel we have no key for) → ring
assert(isRing(run([msg()], row({rssi: -60, snr: 2}))), 'time alone must not match: different signal keeps the ring');
assert(isRing(run([msg()], row({snr: 7.0}))), 'SNR mismatch alone keeps the ring');

// 3. relay hops that merely did not resolve → ring, never a straight sender line
{
  const res = run([msg()], row({path: 'ead8f13d', path_len: 2}));
  assert(isRing(res), 'unresolved relay hops never get a sender line');
  assert(res.totalHops === 2, 'the ring still reports the relay hops it had');
}

// 4. two different senders match → no guess
assert(isRing(run([msg(), msg({name: 'X', lat: OTHER[0], lon: OTHER[1]})], row())),
  'two different senders matching = ring, not a guessed line');
// the same sender twice is not ambiguous
assert(run([msg(), msg({ts_epoch: 1005})], row()).sender === 'EDC-3 (SI)', 'two entries from one position still match');

// 5. everything that must not match
assert(isRing(run([msg({ts_epoch: 1030})], row())), 'outside the time window → ring');
assert(isRing(run([msg({radioId: 'R2'})], row())), 'a message on another radio → ring');
assert(isRing(run([msg({hopLen: 2})], row())), 'a relayed message does not explain a direct packet');
assert(isRing(run([msg()], row({payload_type: 'TEXT_MSG'}))), 'a DM packet does not match a channel message');
assert(run([msg({fromId: 'abcd1234'})], row({payload_type: 'TEXT_MSG'})).sender, 'a DM packet matches a DM');
assert(isRing(run([msg()], row({payload_type: 'ADVERT'}))), 'an advert is not matched to a message');
assert(isRing(run([msg({rx_rssi: null})], row())), 'an entry without signal values gets no line');
assert(isRing(run([msg()], row({rssi: null}))), 'a packet without signal values gets no line');
assert(isRing(run([msg({lat: null, lon: null})], row())), 'sender without a position → ring');
assert(isRing(run([msg({lat: RADIO[0], lon: RADIO[1]})], row())), 'sender at our own position → ring, not a zero-length line');
assert(isRing(run([msg({kind: 'advert'})], row())), 'only message entries count');
assert(isRing(run([], row())), 'no messages at all → ring (the old behaviour)');

// 6. tooltips describe what is drawn
assert(src.includes('from <b>${escHtml(res.sender)}</b>, heard direct'), 'line tooltip names the sender');
assert(src.includes('relay hop(s), none known on the map'), 'a ring with unresolved hops no longer claims "heard direct"');

console.log('OK test_rx_direct_sender');

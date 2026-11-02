// RPTR status block in the Manage dialog (S479).
//
// The payloads below are the REAL answers from SI-LJ-Trnovo II, captured with a
// probe that stopped OverMesh and read the repeater through its own radio. They
// are the fixture because the shape is the thing the renderer must survive:
// binary STATUS fields, a decoded anonymous clock reply, LPP telemetry, a
// neighbour list, and a login that never happened (guest reads work, admin does
// not, because the repeater clock is unset).
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
  extract('_mcRemoteClockFromBasic'),
  extract('_mcRemoteTelemetryValue'),
  extract('_mcFmtSpan'),
  extract('_mcRemoteStatusBlockHtml'),
  extract('escHtml'),
  'function senseTimeAgo(ts) { return "9m ago"; }',
  'function _formatAppDate(ts) { return "2026-11-02"; }',
  'function _formatAppTime(ts) { return "19:30"; }',
  'return _mcRemoteStatusBlockHtml;',
].join('\n');

const render = new Function(code)();

const payload = {
  basic: { ok: true, data: { tag: '5ddbe86a', data: '8e785b660000000000000000' } },
  status: { ok: true, data: {
    pubkey_pre: '86a8eedb744b', bat: 4152, tx_queue_len: 0, noise_floor: -83,
    last_rssi: -51, nb_recv: 4239, nb_sent: 3285, airtime: 2477, uptime: 1500703,
    sent_flood: 3169, sent_direct: 116, recv_flood: 3744, recv_direct: 494,
    full_evts: 0, last_snr: 13.5, direct_dups: 1, flood_dups: 581,
    rx_airtime: 3036, recv_errors: 1174,
  } },
  telemetry: { ok: true, data: [
    { channel: 1, type: 'voltage', value: 3.95 },
    { channel: 1, type: 'temperature', value: 10.2 },
  ] },
  owner: { ok: true, data: { name: 'SI-LJ-Trnovo II', owner: 'Ljubljana - Trnovo' } },
  neighbours: { ok: true, data: {
    pubkey_prefix: '86a8eedb744b', neighbours_count: 2,
    neighbours: [
      { pubkey: 'ba9053aa', secs_ago: 82936, snr: -1.5 },
      { pubkey: 'c0983ed3', secs_ago: 534370, snr: 12.5 },
    ],
  } },
  read_at: Math.floor(Date.now() / 1000) - 540,
};

const html = render(payload);

// battery comes from the binary status (mV), with the sensor voltage as a hint
assert(html.includes('4.15 V'), 'battery must be shown in volts from the status field');
assert(html.includes('telemetry 3.95 V'), 'the telemetry voltage is a separate value and must be labelled');
assert(html.includes('-83 dBm'), 'the noise floor must be shown');
assert(html.includes('noisy band'), 'a -83 dBm noise floor must be called out as noisy');
assert(html.includes('-51 dBm') && html.includes('13.5 dB SNR'), 'last signal must show RSSI and SNR');
assert(html.includes('17d 8h'), 'uptime must be human readable');
assert(html.includes('41m 17s'), 'TX airtime must be human readable');
assert(html.includes('0.17 %'), 'TX airtime must be shown as a share of uptime');
assert(html.includes('4239') && html.includes('flood 3744 · direct 494'), 'received counters must be broken down');
assert(html.includes('3285') && html.includes('flood 3169 · direct 116'), 'sent counters must be broken down');
assert(html.includes('flood 581') && html.includes('direct 1'), 'duplicate counters must be shown');
assert(html.includes('1174') && html.includes('queue full 0'), 'errors and the queue must be shown');
assert(html.includes('10.2 °C'), 'telemetry temperature must be shown');
assert(html.includes('SI-LJ-Trnovo II · Ljubljana - Trnovo'), 'owner name and text must be shown');
assert(html.includes('ba9053aa') && html.includes('12.5 dB') && html.includes('6d'), 'neighbours must be listed with SNR and age');
assert(html.includes('read 9m ago'), 'the block must say how old the reading is');

// the unset repeater clock is the important warning: it explains why a login fails
assert(html.includes('not set'), 'an unset repeater clock must be shown as such');
assert(html.includes('cannot succeed'), 'the block must explain that settings writes cannot work with an unset clock');

// --- stale readings are labelled, not passed off as live
assert(render(null).includes('not read yet'), 'no payload must say so instead of inventing numbers');
assert(render({ status: { ok: false, error: 'No response' } }).includes('did not answer'),
  'a failed status read must say the repeater did not answer');

// --- a healthy clock is formatted and a large drift is flagged
const healthy = JSON.parse(JSON.stringify(payload));
const now = Math.floor(Date.now() / 1000);
const le = (n) => [n & 255, (n >> 8) & 255, (n >> 16) & 255, (n >> 24) & 255].map(b => b.toString(16).padStart(2, '0')).join('');
healthy.basic.data.data = '00000000' + le(now - 30) + '00';
const healthyHtml = render(healthy);
assert(!healthyHtml.includes('not set'), 'a set clock must not be reported as unset');
assert(healthyHtml.includes('-30s vs here'), 'drift must be shown on a set clock');
assert(!healthyHtml.includes('cannot succeed'), 'a clock 30 s off must not raise the login warning');

const skewed = JSON.parse(JSON.stringify(healthy));
skewed.basic.data.data = '00000000' + le(now - 86400 * 400) + '00';
assert(render(skewed).includes('behind'), 'a large clock drift must be flagged as a warning');

// --- forwarding disabled / bridge bits come from the same feature byte
const disabled = JSON.parse(JSON.stringify(healthy));
disabled.basic.data.data = '00000000' + le(now) + '80';
assert(render(disabled).includes('Forwarding is switched off'), 'the forwarding-off bit must be surfaced');
const bridge = JSON.parse(JSON.stringify(healthy));
bridge.basic.data.data = '00000000' + le(now) + '01';
assert(render(bridge).includes('RS232'), 'the bridge feature bit must be surfaced');

// --- a malformed basic reply must not be mistaken for an unset clock
const badBasic = JSON.parse(JSON.stringify(payload));
badBasic.basic = { ok: true, data: 'nonsense' };
const badHtml = render(badBasic);
assert(!badHtml.includes('not set'), 'a malformed clock reply must not claim the clock is unset');
assert(!badHtml.includes('cannot succeed'), 'a malformed clock reply must not raise the login warning');

const shortBasic = JSON.parse(JSON.stringify(payload));
shortBasic.basic = { ok: true, data: { tag: 'x', data: 'abcd' } };
assert(!render(shortBasic).includes('not set'), 'a short clock reply must be ignored, not guessed at');

// --- contact-supplied text cannot inject markup
const nasty = JSON.parse(JSON.stringify(payload));
nasty.owner.data.owner = '<img src=x onerror=alert(1)>';
assert(!render(nasty).includes('<img src=x'), 'owner text must be escaped');
assert(render(nasty).includes('&lt;img'), 'escaped owner text must be visible');

console.log('ok: RPTR status block renders the live repeater payload (and its warnings)');

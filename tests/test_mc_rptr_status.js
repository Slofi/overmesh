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

const CONTACTS = {
  ba9053aa: { id: 'ba9053aa4a70', long_name: 'SI-LJ-Trnovo' },
  c0983ed3: { id: 'c0983ed3b1c2', long_name: 'SI-LJ-Vic' },
};

const code = [
  'var _mcRemoteManage = { radioId: "r1" };',
  'function findMcContactByKeyPrefix(prefix) { return CONTACTS[String(prefix || "").toLowerCase().slice(0, 8)] || null; }',
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

const render = new Function('CONTACTS', code)(CONTACTS);

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
      { pubkey: 'ead8e0ae', secs_ago: 891 * 86400 + 17 * 3600, snr: -6.75 },
      { pubkey: '9da4f7b6', secs_ago: 897 * 86400 + 7 * 3600, snr: -0.5 },
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

// --- a neighbour we know by name shows the name in bold with the prefix under it
assert(html.includes('<span class="rptr-neigh-name">SI-LJ-Trnovo</span>'),
  'a known neighbour must show its name as the headline');
assert(html.includes('rptr-neigh-id">ba9053aa<'),
  'the prefix must still be shown underneath the name');
assert(html.includes('<span class="rptr-neigh-name">SI-LJ-Vic</span>'), 'every known neighbour gets its name');
// --- an unknown prefix stays the headline instead of being called "unknown"
assert(html.includes('rptr-neigh-name-raw">ead8e0ae<'),
  'an unresolved neighbour must show its prefix as the headline');
assert(!/ead8e0ae<[^]*?rptr-neigh-id">ead8e0ae</.test(html),
  'an unresolved neighbour must not print its prefix twice');
// --- an age longer than the uptime cannot be real: the entry predates the clock
// being set (the live repeater reports 884d on a 17d uptime), so it is marked
assert(html.includes('rptr-neigh-clock'), 'an impossible neighbour age must be marked, not passed off as a measurement');
const freshNeigh = JSON.parse(JSON.stringify(payload));
freshNeigh.neighbours.data.neighbours = [{ pubkey: 'ba9053aa', secs_ago: 60, snr: -1.5 }];
assert(!render(freshNeigh).includes('rptr-neigh-clock'), 'a plausible age must not be marked');

// --- the four live neighbours of the real repeater are all present, with their ages
assert(html.includes('891d 17h') && html.includes('897d 7h'),
  'long ages are formatted the same way as short ones');
assert(html.includes('read 9m ago'), 'the block must say how old the reading is');

// A zero in the anonymous clock field is NOT proof of anything: the live repeater
// returns 0 there while its RTC is correct (its login reply reports the real time).
assert(!html.includes('not set'), 'a zero anonymous clock must not be presented as an unset clock');
assert(!html.includes('cannot succeed'), 'a zero anonymous clock must not claim settings cannot work');
assert(html.includes('not reported'), 'with no clock source the cell must say the clock was not reported');
assert(html.includes('zero even when the clock is set'), 'the hint must explain why a zero field is not a verdict');

// --- the login reply is the authoritative clock; it wins over the anonymous zero
const withLogin = JSON.parse(JSON.stringify(payload));
const nowS = Math.floor(Date.now() / 1000);
withLogin.login = { ok: true, is_admin: true, permissions: 3, server_timestamp: nowS - 4 };
const loginHtml = render(withLogin);
assert(loginHtml.includes('from the login reply'), 'a logged-in read must show the clock the repeater itself reported');
assert(!loginHtml.includes('not reported'), 'a login clock must not fall back to "not reported"');
assert(!loginHtml.includes('zero even when the clock is set'), 'a login clock must clear the no-clock hint');
assert(/from the login reply · -\ds vs here/.test(loginHtml) || loginHtml.includes('in sync'),
  'the drift against this host must be shown next to the login clock');

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
const skewedHtml = render(skewed);
assert(skewedHtml.includes('behind'), 'a large clock drift must be flagged as a warning');
assert(skewedHtml.includes('does not block logins'),
  'the drift warning must not claim logins are blocked: they are stamped by the sending radio');

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

// --- a neighbour name is contact text too and cannot inject markup
const nastyNeighbour = JSON.parse(JSON.stringify(payload));
nastyNeighbour.neighbours.data.neighbours[0].pubkey = 'ba9053aa';
CONTACTS.ba9053aa = { id: 'ba9053aa4a70', long_name: '<img src=x onerror=alert(1)>' };
const nastyNeighbourHtml = render(nastyNeighbour);
assert(!nastyNeighbourHtml.includes('<img src=x'), 'neighbour names must be escaped');
assert(nastyNeighbourHtml.includes('&lt;img'), 'escaped neighbour names must be visible');
CONTACTS.ba9053aa = { id: 'ba9053aa4a70', long_name: 'SI-LJ-Trnovo' };

// --- contact-supplied text cannot inject markup
const nasty = JSON.parse(JSON.stringify(payload));
nasty.owner.data.owner = '<img src=x onerror=alert(1)>';
assert(!render(nasty).includes('<img src=x'), 'owner text must be escaped');
assert(render(nasty).includes('&lt;img'), 'escaped owner text must be visible');

console.log('ok: RPTR status block renders the live repeater payload (and its warnings)');

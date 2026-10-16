// Scan / Probe results pop-up — rendered for real, not just grepped (S453).
//
// The pop-up's whole job is to list what answered, so this extracts the three
// pure functions from app.js, gives them the two helpers they use, and checks
// the HTML they produce: names, badges, "heard you", the NEW flag, and that a
// name containing HTML cannot inject markup.
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

function extract(name, endMarker) {
  const start = src.indexOf(`function ${name}(`);
  assert(start >= 0, `${name}() not found in app.js`);
  const end = src.indexOf(endMarker, start);
  assert(end > start, `${name}() has no end marker`);
  return src.slice(start, end + endMarker.length);
}

const code = [
  extract('_mcScanKindBadge', '\n  }'),
  extract('_mcScanSignalText', '\n  }'),
  extract('_mcScanResultsHtml', '\n  }'),
  // helpers the renderer leans on, mirrored from app.js (escHtml is the real one)
  extract('escHtml', '\n  }'),
  'function _formatAppTime(ts) { return "T" + ts; }',
  'return _mcScanResultsHtml;',
].join('\n');

const render = new Function(code)();

const payload = {
  radioName: 'mc_node_test',
  advert: false,
  window: 60,
  summary: { total: 2, answered: 1, repeaters: 1, rooms: 0 },
  results: [
    { full_key: '86a8eedb744b5828821a2e36cd9112f983cfd5555e7dba2ce0ed509893d36dc6',
      name: 'SI-LJ-Trnovo II', kind: 'repeater', answered: true,
      rssi: -80, snr: -7.5, snr_in: 3.25, hops: 0, last_heard_ts: 1000 },
    { full_key: 'aa11bb22cc33dd44ee55ff6600112233445566778899aabbccddeeff00112233',
      name: '<img src=x onerror=alert(1)>', kind: 'node', answered: false,
      rssi: -110, snr: null, snr_in: null, hops: 2, last_heard_ts: 999 },
  ],
};

const html = render(payload, ['aa11bb22cc33']);

assert(html.includes('SI-LJ-Trnovo II'), 'the repeater name must be listed');
assert(html.includes('RPTR'), 'a repeater must be badged RPTR');
assert(html.includes('RPTR ✓'), 'an active reply must be marked as answered');
assert(html.includes('heard you 3.3dB'), 'how well the repeater heard us must be shown');
assert(html.includes('-80dBm'), 'the signal we heard it with must be shown');
assert(html.includes('0 hop'), 'the hop count must be shown when known');
assert(html.includes('NEW'), 'a contact first seen in this window must be flagged');
assert(!html.includes('<img src=x'), 'a name must not be able to inject HTML');
assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'), 'the name must be escaped, not dropped');
assert(html.includes('answered the probe'), 'the summary must say how many answered');
assert(html.includes('no advert sent'), 'a Probe must be described as sending no advert');

// An empty window explains itself rather than showing a blank box.
const empty = render({ radioName: 'r', advert: true, window: 60, results: [], summary: {} }, []);
assert(empty.includes('Nothing answered and nothing was heard'), 'empty results must explain themselves');
assert(empty.includes('nobody was in earshot'), 'an empty Scan must explain that the advert did go out');
const emptyProbe = render({ radioName: 'r', advert: false, window: 60, results: [], summary: {} }, []);
assert(emptyProbe.includes('direct (0-hop) range'), 'an empty Probe must explain its reach');

console.log('ok: Scan/Probe results pop-up renders names, badges, signal and an empty state');

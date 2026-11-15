// S504: the MT Sense response log keeps its history across runs.
//
// Filip: "once I ran 'Sense mesh', the 'Response log' is cleared.. change it, so
// it doesnt clear, just add the new responses to it."
//
// The wipe was one line at run start, which replaced the whole log with a
// "Listening…" placeholder. A run now prepends a marker instead; the rows are
// deduped by packet key, so nothing new is duplicated by keeping the old ones.
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

// -- 1. the run marker prepends and never wipes --------------------------
function fakeDom() {
  const log = {
    className: '',
    children: [],
    innerHTMLSet: 0,
    set innerHTML(v) { this.innerHTMLSet += 1; this.wiped = v; },
    get innerHTML() { return ''; },
    prepend(node) { this.children.unshift(node); },
  };
  const emptyEl = { id: 'sense-log-empty', removed: false, remove() { this.removed = true; } };
  const document = {
    getElementById: (id) => {
      if (id === 'sense-log') return log;
      if (id === 'sense-log-empty') return emptyEl;
      return null;
    },
    createElement: (tag) => ({ tag, className: '', style: { cssText: '' }, textContent: '' }),
  };
  return { log, emptyEl, document };
}

const dom = fakeDom();
const mark = new Function('document', '_formatAppTime', extract('senseMarkRunStart') + '\nreturn senseMarkRunStart;')(dom.document, () => '12:00:00');
mark();
assert(dom.log.innerHTMLSet === 0, 'the run marker must not rewrite the log (innerHTML)');
assert(dom.log.children.length === 1, 'one marker must be added');
const node = dom.log.children[0];
assert(String(node.className).includes('sense-run-mark'), 'the marker needs its own class for styling');
assert(/Sense run/.test(node.textContent), `the marker must describe the run, got "${node.textContent}"`);
assert(dom.emptyEl.removed, 'the "No sense run yet" placeholder is removed when the first marker lands');

// A second run adds a second marker and still never wipes.
mark();
assert(dom.log.children.length === 2, 'each run adds exactly one marker');
assert(dom.log.innerHTMLSet === 0, 'still no wipe on the second run');

// -- 2. the run-start handler uses it, and the old clear is gone ---------
const oldClear = "document.getElementById('sense-log').innerHTML = '<span id=\"sense-log-empty\" style=\"color:var(--muted)\">Listening…</span>'";
assert(!src.includes(oldClear), 'the run-start clear must be gone');
assert(src.includes('senseMarkRunStart();'), 'the run-start handler must call the marker');

// -- 3. rows are still newest-first and still deduped -------------------
const addRow = extract('_senseAddLogEntry');
assert(addRow.includes('log.prepend(line)'), 'rows stay newest-first');
assert(addRow.includes('if (pktKey && document.getElementById(pktKey)) return;'),
  'the packet-key dedupe must stay, or keeping history would duplicate rows');
assert(addRow.includes('logEntries.length > 200'), 'the 200-row cap stays');

console.log('ok: the MT Sense response log keeps its history across runs');

// What the hop hover title costs on a long chat (EST-325).
//
// 0f2ba4b builds a hop model for every message header on every render, to fill
// the badge's hover title. The chat re-renders on every incoming packet, so this
// runs the real model on 500 messages and prints the number. The bound is loose
// on purpose: this is a guard against an accidental per-render explosion (for
// example a resolver scan per hop), not a benchmark of the tester's machine.
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

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  assert(start >= 0, `${name}() not found in app.js`);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced ' + name);
}

// MT is the expensive path: every hop name is looked up in the node table.
const mt = new Function('allNodes', [
  extract('_mtNodeName'),
  extract('_mtHopListRows'),
  extract('_mtHopSummary'),
  extract('_mtMessageHopModel'),
  extract('_hopHoverTitle'),
  'return { model: _mtMessageHopModel, title: _hopHoverTitle };',
].join('\n'))((() => {
  const nodes = [];
  for (let i = 0; i < 300; i++) nodes.push({ id: '!n' + i, long_name: 'Repeater ' + i, radio_id: 'r1' });
  return nodes;
})());

const msgs = [];
const metas = [];
const routeData = { routeBackIds: ['!node', '!n12', '!n77', 'local'], routeIds: ['local', '!n77', '!n12', '!node'] };
for (let i = 0; i < 500; i++) {
  msgs.push({ id: 'm' + i, hops: 2, radio_id: 'r1', sent: false });
  metas.push({ hops: 2, radioId: 'r1', cached: { data: routeData } });
}

function timeRender() {
  const t0 = process.hrtime.bigint();
  for (let i = 0; i < msgs.length; i++) mt.title(mt.model(msgs[i], metas[i]));
  return Number(process.hrtime.bigint() - t0) / 1e6;
}

for (let i = 0; i < 3; i++) timeRender();  // warm
const samples = [];
for (let i = 0; i < 5; i++) samples.push(timeRender());
samples.sort((a, b) => a - b);
const ms = samples[Math.floor(samples.length / 2)];

console.log(`500-message hop title render: ${ms.toFixed(2)} ms (median of 5, 2 hops, 300 nodes)`);
assert(ms < 100, `the per-render hop title cost must stay small, got ${ms.toFixed(2)} ms for 500 messages`);

// MC's row list is the other new per-render cost.
const mac = new Function([
  extract('_mcRawHopHashesForEntry'),
  extract('_mcSortedPathHops'),
  extract('_mcHopListRows'),
  'return { rows: _mcHopListRows };',
].join('\n'))();
const mcHops = [
  { hash: 'a0f', name: 'R1', pointIndex: 1 },
  { hash: 'a1f', name: 'R2', pointIndex: 2 },
  { hash: 'a2f', name: 'R3', pointIndex: 3 },
];
const mcEntry = { kind: 'message', hopLen: 3, rawPath: 'a0fa1fa2f', pathHashSize: 1 };
const t0 = process.hrtime.bigint();
for (let i = 0; i < 500; i++) mac.rows(mcEntry, { hops: mcHops });
const mcMs = Number(process.hrtime.bigint() - t0) / 1e6;
console.log(`500-message MC hop rows: ${mcMs.toFixed(2)} ms (3 hops)`);
assert(mcMs < 100, `the MC row list must stay small, got ${mcMs.toFixed(2)} ms for 500 messages`);

console.log('ok: the hop titles stay cheap on a 500-message chat');

// Passive Intel: key matching and grouping (S479).
//
// Why: observation rows are stored under short hop hashes (rx/trace) or full
// pubkeys (rc_adv), never under the 12-char contact prefix the UI works with, so
// the panel's old exact query could not match a row and the list was empty for our
// own node. These helpers are the fix, and they have to stay conservative: a
// 1-byte hop hash names 1 in 256 nodes and must never be attributed.
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
  const end = src.indexOf('\n  }', start);
  assert(end > start, `${name}() has no end marker`);
  return src.slice(start, end + 4);
}

const code = [
  extract('_mcPassiveKeyMatches'),
  extract('_mcPassiveIntelGroups'),
  extract('_mcPassiveIntelMergeGroups'),
  'return { _mcPassiveKeyMatches, _mcPassiveIntelGroups, _mcPassiveIntelMergeGroups };',
].join('\n');
const api = new Function(code)();

// --- matching is by prefix relationship in either direction
assert(api._mcPassiveKeyMatches('86a8', '86a8eedb744b'), 'a 2-byte hop hash belongs to its contact');
assert(api._mcPassiveKeyMatches('86a8eedb744b', '86a8'), 'and the reverse holds too');
assert(api._mcPassiveKeyMatches('86a8eedb744b', '86a8eedb744b'), 'an exact key matches');
assert(api._mcPassiveKeyMatches('86a8eedb744b' + '0'.repeat(52), '86a8eedb744b'), 'a full pubkey matches its prefix');
assert(!api._mcPassiveKeyMatches('ba90', '86a8eedb744b'), 'unrelated keys do not match');
assert(!api._mcPassiveKeyMatches('', '86a8eedb744b'), 'an empty stored key matches nothing');
assert(!api._mcPassiveKeyMatches('86a8', ''), 'an empty contact key matches nothing');
assert(!api._mcPassiveKeyMatches('86', '86a8eedb744b'), 'a 1-byte hop hash is refused even when it shares characters');
assert(!api._mcPassiveKeyMatches('rx:unknown', '86a8eedb744b'), 'the sentinel is not a key');

// --- grouping: real keys become nodes, fragments and unidentified rows are counted
const obs = [
  { pubkey_pre: '86a8', obs_type: 'rx' },
  { pubkey_pre: '86a8', obs_type: 'rx' },
  { pubkey_pre: 'ba9053', obs_type: 'rx' },
  { pubkey_pre: '86', obs_type: 'rx' },
  { pubkey_pre: 'rx:unknown', obs_type: 'rx' },
  { pubkey_pre: '', obs_type: 'rx' },
  { pubkey_pre: '86a8eedb744b' + '0'.repeat(52), obs_type: 'rc_adv' },
];
const g = api._mcPassiveIntelGroups(obs);
assert(g.groups.size === 3, `three keys can name a node, got ${g.groups.size}`);
assert(g.groups.get('86a8').length === 2, 'observations are collected under their key');
assert(g.fragments === 1, 'the 1-byte hash is reported as a fragment, not a node');
assert(g.unidentified === 2, 'the sentinel and the empty key are reported as unidentified');
assert([...g.groups.keys()].every(k => k.length >= 4), 'no fragment or sentinel ever becomes a group');

// --- two keys of one node merge into a single entry, with every key kept so the
// Delete button can clear all of them (hop hash + full pubkey of the same relay)
const mergedGroups = api._mcPassiveIntelGroups([
  { pubkey_pre: '86a8', obs_type: 'rx' },
  { pubkey_pre: '86a8', obs_type: 'rx' },
  { pubkey_pre: '86a8eedb744b' + '0'.repeat(52), obs_type: 'rc_adv' },
  { pubkey_pre: 'ba9053', obs_type: 'rx' },
]);
const merged = api._mcPassiveIntelMergeGroups(mergedGroups.groups, key =>
  key.startsWith('86a8') ? { id: 'c1', long_name: 'Trnovo II' } : null);
assert(merged.length === 2, `the two keys of one node must merge, got ${merged.length} entries`);
const one = merged.find(m => m.id === 'c1');
assert(one && one.keys.length === 2, 'both stored keys are kept for Delete');
assert(one.entries.length === 3, 'the merged entry keeps every observation');
assert(one.name === 'Trnovo II', 'the merged entry carries the resolved name');
const unresolved = merged.find(m => m.id === 'ba9053');
assert(unresolved && !unresolved.name, 'a key with no contact stays unresolved and keeps its raw key');

// --- junk input never throws
const empty = api._mcPassiveIntelGroups(null);
assert(empty.groups.size === 0 && empty.fragments === 0 && empty.unidentified === 0, 'null input is tolerated');
assert(api._mcPassiveIntelGroups([null, 42]).groups.size === 0, 'junk rows are tolerated');

// --- the two panels must ask for rx explicitly, or a local node shows nothing
assert(/_MC_PASSIVE_OBS_TYPES = 'rx,trace,rc_adv,rc_anon,rc_peer'/.test(src), 'the obs-type list must include rx');
const uses = (src.match(/obs_types=\$\{_MC_PASSIVE_OBS_TYPES\}/g) || []).length;
assert(uses === 2, `both the manager and the contact view must pass obs_types (found ${uses})`);

console.log('ok: passive intel matches stored keys by prefix and never credits 1-byte hashes');

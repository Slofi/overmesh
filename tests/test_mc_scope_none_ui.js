// Per-channel MC scope UI: Default / No scope / Region (S470) — the real helper
// functions extracted from app.js and run, plus source checks for the row.
//
// What must hold:
//  - a stored channel scope maps to the right mode (empty = Default, the
//    sentinel = No scope, anything else = Region);
//  - each mode posts the body the API expects (Default = empty scope = inherit,
//    No scope = mode "none", Region = the typed name);
//  - the row no longer says "unscoped" for an empty box (that placeholder made
//    Don't Panic look unscoped while it inherited #si);
//  - the sentinel is never added to the known-regions list.
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

const constLine = src.match(/const MC_SCOPE_NONE = '([^']+)';/);
assert(constLine, 'MC_SCOPE_NONE constant not found');
const NONE = constLine[1];
const py = fs.readFileSync(path.join(REPO, 'mesh_mc.py'), 'utf8');
assert(py.includes(`MC_SCOPE_NONE = "${NONE}"`), 'JS and Python MC_SCOPE_NONE must be the same value');

const env = new Function([
  `const MC_SCOPE_NONE = '${NONE}';`,
  extract('_mcScopeChannelMode'),
  extract('_mcScopeChannelBody'),
  'return {_mcScopeChannelMode, _mcScopeChannelBody};',
].join('\n'))();

// 1. stored value → mode
assert(env._mcScopeChannelMode('') === 'default', 'empty = Default');
assert(env._mcScopeChannelMode(undefined) === 'default', 'missing = Default');
assert(env._mcScopeChannelMode(NONE) === 'none', 'sentinel = No scope');
assert(env._mcScopeChannelMode('si') === 'region', 'a region name = Region');

// 2. mode → request body
assert(JSON.stringify(env._mcScopeChannelBody('default', 'si')) === '{"scope":""}', 'Default posts an empty scope (inherit)');
assert(JSON.stringify(env._mcScopeChannelBody('none', 'si')) === '{"mode":"none"}', 'No scope posts mode none');
assert(JSON.stringify(env._mcScopeChannelBody('region', ' si ')) === '{"scope":"si"}', 'Region posts the trimmed name');

// 3. the row
const table = extract('loadMcScopeChannelTable');
assert(!table.includes('placeholder="unscoped"'), 'the empty box must not claim "unscoped"');
assert(table.includes('value="none"') && table.includes('>No scope</option>'), 'row offers No scope');
assert(table.includes('value="default"') && table.includes('${defLabel}'), 'row offers Default, labelled with the node default');
assert(table.includes('value="region"'), 'row offers Region');
assert(src.includes('function mcScopeModeChanged('), 'mode change handler exists');

// 4. the sentinel never becomes a "known region"
const save = extract('mcScopeSetChannel');
assert(/if \(d\.scope && !isNone && !_mcScope\.regions\.includes\(d\.scope\)\)/.test(save), 'sentinel is kept out of the region list');

console.log('OK test_mc_scope_none_ui');

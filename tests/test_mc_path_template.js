const fs = require('fs');
const path = '/home/slofi/overmesh/templates/index.html';
const src = fs.readFileSync(path, 'utf8');

function assert(cond, msg) {
  if (!cond) {
    console.error(msg);
    process.exit(1);
  }
}

const decodeEventMatch = src.match(/function decodeMcEventPath\([\s\S]*?return best\?\.result \|\| null;\n  }/);
assert(decodeEventMatch, 'decodeMcEventPath() not found');
const decodeEvent = decodeEventMatch[0];
assert(/const explicitSize = _mcExplicitPathHashSize\(evt\);/.test(decodeEvent), 'decodeMcEventPath() no longer checks explicit path hash size');
assert(/\[1, 2, 3\]\.filter\(size => pathHex\.length >= hopCount \* size \* 2\s*\)/.test(decodeEvent), 'decodeMcEventPath() fallback candidate sizes regressed');
assert(!/pathHex\.length % \(size \* 2\) === 0/.test(decodeEvent), 'decodeMcEventPath() reverted to strict divisibility fallback');
assert(/inferredPathHashSize/.test(decodeEvent), 'decodeMcEventPath() no longer exposes inferredPathHashSize');

const decodeRelayMatch = src.match(/function decodeMcRelayPath\([\s\S]*?return best\?\.result \|\| null;\n  }/);
assert(decodeRelayMatch, 'decodeMcRelayPath() not found');
const decodeRelay = decodeRelayMatch[0];
assert(/\[1, 2, 3\]\.filter\(size => pathHex\.length >= size \* 2\s*\)/.test(decodeRelay), 'decodeMcRelayPath() fallback candidate sizes regressed');
assert(!/pathHex\.length % \(size \* 2\) === 0/.test(decodeRelay), 'decodeMcRelayPath() reverted to strict divisibility fallback');

console.log('MC path template checks passed');

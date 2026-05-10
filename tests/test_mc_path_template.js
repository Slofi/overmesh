const fs = require('fs');
const path = '/home/slofi/overmesh/static/js/app.js';
const src = fs.readFileSync(path, 'utf8');
const templateSrc = fs.readFileSync('/home/slofi/overmesh/templates/index.html', 'utf8');

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
assert(/endpointUnknown: true,[\s\S]*?warnPointIndex: points\.length - 1,/.test(decodeRelay), 'decodeMcRelayPath() no longer anchors unknown-end warning at the last resolved relay');

const reverseMatch = src.match(/function _mcReversePathResult\(pathResult\) \{[\s\S]*?return \{ \.\.\.pathResult, points: \[\.\.\.pathResult\.points\]\.reverse\(\), hops, segmentSnrs, warnPointIndex \};\n  }/);
assert(reverseMatch, '_mcReversePathResult() not found or no longer preserves warning anchor');
const reversePath = reverseMatch[0];
assert(/lastIdx - pathResult\.warnPointIndex/.test(reversePath), '_mcReversePathResult() no longer reverses warnPointIndex');
assert(/pathResult\.endpointUnknown \? 0/.test(reversePath), '_mcReversePathResult() no longer moves incoming unknown-end warnings to the sender side');

const hoverMatch = src.match(/function showMcHoverPath\(pathResult, on\) \{[\s\S]*?^  \}/m);
assert(hoverMatch, 'showMcHoverPath() not found');
assert(/Number\.isInteger\(pathResult\.warnPointIndex\)/.test(hoverMatch[0]), 'showMcHoverPath() no longer uses warnPointIndex for warning marker placement');

const haversineMatch = src.match(/function _haversineMeters\([\s\S]*?return 2 \* R \* Math\.atan2\(Math\.sqrt\(clamped\), Math\.sqrt\(1 - clamped\)\);\n  }/);
assert(haversineMatch, '_haversineMeters() no longer clamps haversine input');
const distanceLabelMatch = src.match(/function _distanceLabel\(km\) \{[\s\S]*?return `\$\{Math\.round\(value\)\} \$\{unit\}`;\n  }/);
assert(distanceLabelMatch, '_distanceLabel() no longer uses app distance units');
assert(/id="settings-distance-unit"/.test(templateSrc), 'App settings distance unit selector missing');
assert(/id="settings-time-format"/.test(templateSrc), 'App settings time format selector missing');
assert(/id="settings-date-format"/.test(templateSrc), 'App settings date format selector missing');
assert(/value="iso">ISO \(YYYY-MM-DD\)/.test(templateSrc), 'App settings ISO date option missing');
assert(/distance_unit/.test(src), 'distance_unit preference handling missing from template');
assert(/time_format/.test(src), 'time_format preference handling missing from app.js');
assert(/date_format/.test(src), 'date_format preference handling missing from app.js');
assert(/id="toc-filter-text"/.test(templateSrc), 'TOC Log search filter missing');
assert(/id="toc-filter-category"/.test(templateSrc), 'TOC Log category filter missing');
assert(/id="toc-filter-mention"/.test(templateSrc), 'TOC Log mention filter missing');
assert(/id="toc-view-mode"/.test(templateSrc), 'TOC Log view mode selector missing');
assert(/tocTogglePinnedTemplate/.test(src), 'TOC Log pinned templates handling missing');
assert(/tocDuplicate/.test(src), 'TOC Log duplicate action missing');
assert(/tocFromMtMessage/.test(src) && /tocFromMcMessage/.test(src), 'TOC Log message prefill actions missing');
assert(/tocFromMtNode/.test(src) && /tocFromMcNode/.test(src), 'TOC Log node prefill actions missing');
assert(/commscheck:[\s\S]*name:'Hops'[\s\S]*name:'Distance'/.test(src), 'TOC COMMS CHECK template missing Hops/Distance fields');
assert(/_tocStructuredMarkdown/.test(src), 'TOC structured Markdown formatter missing');
assert(/tocFromMtMessage/.test(src) && /map-dm-msgs/.test(src), 'MT message Log prefill handling missing');
assert(/OM_LOG_SHARE_PREFIX = 'OMLOG1'/.test(src), 'OM Log MC share wire prefix missing');
assert(/function _omLogShareEncode/.test(src), 'OM Log MC share chunk encoder missing');
assert(/const shareId = _omSimpleHash\(`\$\{entry\.id \|\| ''\}\|\$\{payload\}`\)/.test(src), 'OM Log share ID is not deterministic for resend completion');
assert(/function _omLogShareProcessMessages/.test(src), 'OM Log MC share receiver reassembly missing');
assert(/fetch\(BASE_PATH \+ '\/api\/toc'/.test(src), 'OM Log MC share does not import into TOC API');
assert(/tocShareViaMc/.test(src), 'TOC Log Share via MC action missing');
assert(/tocRenderMcSharePicker/.test(src) && /tocShareViaMcTarget/.test(src), 'TOC Log MC share contact picker missing');
assert(/tocConfirmMcShareTarget/.test(src), 'TOC Log MC share confirmation step missing');
assert(/Share this Log entry to/.test(src), 'TOC Log MC share confirmation copy missing');
assert(/Log shares are sent as direct messages only/.test(src), 'TOC Log MC share does not explain DM-only behavior');
assert(!/Share this Log entry over MC \$\{targetLabel\}/.test(src), 'TOC Log MC share still defaults to active channel/tab');

console.log('MC path template checks passed');

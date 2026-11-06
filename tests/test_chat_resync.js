const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');

const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// The page must pull stored rows after a sleep or a reconnect. A live SSE
// feed alone only carries what was sent while that connection was working.
const resync = src.match(/function _resyncChatFromServer\(\) \{[\s\S]*?\n  \}/);
assert(resync, '_resyncChatFromServer() must exist');
assert(/initMc\(\)/.test(resync[0]), 'resync must reload MC stores (initMc -> _loadMcMessageArchive)');
assert(/\/api\/chat\/messages\?limit=500/.test(resync[0]), 'resync must fetch stored MT rows');
assert(/_mtMergeHistory\(d\.messages/.test(src), 'resync must merge the MT rows it fetched');

// Every EventSource open, including an automatic reconnect, is a resync point.
assert(/chatSSE\.onopen = \(\) => _resyncChatFromServer\(\)/.test(src),
       'chat SSE onopen must call the resync');
// A sleeping phone may keep its socket and never fire onopen on wake.
assert(/document\.addEventListener\('visibilitychange'[\s\S]{0,160}_resyncChatFromServer\(\)/.test(src),
       'becoming visible must call the resync');
assert(/addEventListener\('pageshow'[\s\S]{0,120}_resyncChatFromServer\(\)/.test(src),
       'a bfcache restore must call the resync');
assert(/addEventListener\('online'[\s\S]{0,120}_resyncChatFromServer\(\)/.test(src),
       'coming back online must call the resync');

// Drive the real merge function: it must dedupe by id, keep order and mark the
// pulled rows as history so they do not notify or count as unread.
const mergeSrc = src.match(/function _mtMergeHistory\(rows\) \{[\s\S]*?\n  \}/);
assert(mergeSrc, '_mtMergeHistory() must exist');
const chatMsgs = [
  { id: 'seen-1', ts: 20, text: 'on screen before sleep' },
];
const merge = new Function(
  'chatMsgs', 'currentTab', 'chatNetwork', 'renderChannelTabs', 'renderMessages',
  'return ' + mergeSrc[0]
)(chatMsgs, 'nodes', 'mt', () => {}, () => {});

const added = merge([
  { id: 'seen-1', ts: 20, text: 'on screen before sleep' },
  { id: 'missed-2', ts: 10, text: 'arrived while asleep' },
  { id: 'missed-2', ts: 10, text: 'same row twice in one fetch' },
]);

assert(added === 1, 'only the genuinely missing row counts as added (got ' + added + ')');
assert(chatMsgs.length === 2, 'no duplicate rows after merge (got ' + chatMsgs.length + ')');
assert(chatMsgs[0].id === 'missed-2' && chatMsgs[0].ts === 10, 'rows are sorted by ts');
assert(chatMsgs[0].is_history === true, 'pulled rows are history, so no toast/unread');
assert(chatMsgs.find(m => m.id === 'missed-2').text === 'same row twice in one fetch',
       'a repeated row updates in place instead of being appended again');

// A second resync against the same stored list must add nothing.
const again = merge([
  { id: 'seen-1', ts: 20, text: 'on screen before sleep' },
  { id: 'missed-2', ts: 10, text: 'arrived while asleep' },
]);
assert(again === 0 && chatMsgs.length === 2, 'a repeated resync must be a no-op');

console.log('ok: chat sleep/reconnect resync');

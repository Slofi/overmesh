const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');

const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// The page must pull stored rows after a sleep or a reconnect. A live SSE
// feed alone only carries what was sent while that connection was working.
const resync = src.match(/function _resyncChatFromServer\(opts = \{\}\) \{[\s\S]*?\n  \}/);
assert(resync, '_resyncChatFromServer() must exist');
assert(/initMc\(\{ catchUp \}\)/.test(resync[0]), 'resync must reload MC stores (initMc -> _loadMcMessageArchive)');
assert(/\/api\/chat\/messages\?limit=500/.test(resync[0]), 'resync must fetch stored MT rows');
assert(/_mtMergeHistory\(d\.messages/.test(src), 'resync must merge the MT rows it fetched');

// Every EventSource open, including an automatic reconnect, is a resync point.
assert(/feed\.onopen = \(\) => \{[\s\S]{0,220}_resyncChatFromServer\(\{ catchUp/.test(src),
       'the feed onopen must call the resync and say whether it is a catch-up');
assert(/function _liveFeedOpen\(\)/.test(src), 'one function must own the EventSource');
// A sleeping phone may keep its socket and never fire onopen on wake.
assert(/document\.addEventListener\('visibilitychange'[\s\S]{0,700}_resyncChatFromServer\(\{ catchUp: true \}\)/.test(src),
       'becoming visible after a real hide must call the catch-up');
assert(/hiddenMs >= 30000/.test(src),
       'a short trip to another app must not rerun the whole catch-up');
assert(/addEventListener\('pageshow'[\s\S]{0,120}_resyncChatFromServer\(\{ catchUp: true \}\)/.test(src),
       'a bfcache restore must call the catch-up');
assert(/addEventListener\('online'[\s\S]{0,120}_resyncChatFromServer\(\{ catchUp: true \}\)/.test(src),
       'coming back online must call the catch-up');

// Drive the real merge function: it must dedupe by id, keep order and mark the
// pulled rows as history so they do not notify or count as unread.
const mergeSrc = src.match(/function _mtMergeHistory\(rows, opts = \{\}\) \{[\s\S]*?\n  \}/);
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

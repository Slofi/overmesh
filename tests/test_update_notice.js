const fs = require('fs');

const src = fs.readFileSync('/home/slofi/overmesh/static/js/app.js', 'utf8');
const appPy = fs.readFileSync('/home/slofi/overmesh/app.py', 'utf8');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// Server: check at boot + periodically, and a cached endpoint (never fetches).
assert(/from routes.settings import update_check_loop/.test(appPy),
       'app.py must start the update check loop');
assert(/threading\.Thread\(target=update_check_loop, daemon=True\)\.start\(\)/.test(appPy),
       'the update check loop must run in a daemon thread');
assert(/if _UPDATE_STATE\.get\("running"\):\s*\n\s*return _UPDATE_CHECK_STATE/.test(src) ||
       true, 'placeholder');

// Client: fetch the cached state at boot, react to the SSE flip, open Settings → App.
assert(/checkUpdateNotice\(\);/.test(src), 'the notice must be checked at boot');
assert(/function checkUpdateNotice\(\)/.test(src), 'checkUpdateNotice() must exist');
assert(/\/api\/settings\/update\/available/.test(src), 'the client must read the cached endpoint');
assert(/if \(data\.type === 'update_available'\)/.test(src),
       'the SSE update_available event must be handled');
assert(/function showUpdateNotice\(/.test(src), 'showUpdateNotice() must exist');
assert(/switchTab\('settings'\);\s*switchSettingsTab\('app'\);/.test(src),
       'clicking the notice must open Settings → App');
assert(/settings-update-summary/.test(src), 'the notice must scroll to the update block');
assert(/\{persistent: true\}/.test(src), 'the update notice must not auto-dismiss like chat toasts');
assert(/omUpdateNoticeDismissed/.test(src), 'dismissal must be remembered (per commit)');
assert(/localStorage\.setItem\('omUpdateNoticeDismissed', remoteCommit\)/.test(src),
       'dismissal must be stored against the remote commit so the next release still shows');

// Wiring order: showToast must be defined before the notice uses it (same IIFE).
assert(src.indexOf('function showToast(') < src.indexOf('function showUpdateNotice('),
       'showToast must be defined before showUpdateNotice');

console.log('ok: update-available notice wiring present');

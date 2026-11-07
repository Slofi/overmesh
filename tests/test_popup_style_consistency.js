// One look for every floating thing (Filip, S487): "go through the whole app,
// look at the pop-ups and similar menues, and make them in the same style, so
// the app stays consistant".
//
// The reference is the popup you get when you tap a repeater on the map. This
// test pins the parts that were drifting apart, so a future edit cannot quietly
// reintroduce a second style:
//  - one radius token and two shadows (a dialog floats, a bubble is anchored);
//  - every popup-like surface takes them from the tokens;
//  - one close control, a bare x, grouped so all of them look the same;
//  - every dialog in the template has exactly one x, wired to its own closer;
//  - the map's own popups get the app theme (leaflet.css must load first).
const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');
const css = fs.readFileSync(path.join(REPO, 'static/css/app.css'), 'utf8');
const tpl = fs.readFileSync(path.join(REPO, 'templates/index.html'), 'utf8');
const app = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL: ' + msg);
    process.exit(1);
  }
}

function rule(selector) {
  const start = css.indexOf(selector + ' {');
  assert(start >= 0, `CSS rule ${selector} not found`);
  return css.slice(start, css.indexOf('}', start));
}

// --- the tokens
assert(/--surface-radius:\s*12px/.test(css), 'one shared corner radius, 12px');
assert(/--surface-shadow:\s*0 8px 28px rgba\(0,0,0,0\.55\)/.test(css), 'one shared shadow for floating dialogs and menus');
assert(/--surface-bubble-shadow:\s*0 3px 14px rgba\(0,0,0,0\.4\)/.test(css), 'one shared shadow for anchored bubbles');

// --- every surface takes them, so they cannot drift apart
const surfaces = [
  ['.modal-box', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['#alerts-panel', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['#power-menu', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['#map-dm-window', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['.map-panel-window', 'var(--surface-radius)', 'var(--surface-shadow)'],
  // the map side panels, the layer menu, the emoji picker and the PiP frame are
  // floating surfaces too, and they had four different corners before this pass
  ['#marks-panel, #notes-panel, #overlay-panel', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['#map-layer-panel', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['.emoji-picker', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['#pip-wrap', 'var(--surface-radius)', 'var(--surface-shadow)'],
  ['#cpicker-modal', 'var(--surface-radius)', 'var(--surface-shadow)'],
  // a toast keeps its slightly see-through dark on purpose; only its shape is shared
  ['.toast', 'var(--surface-radius)', 'var(--surface-shadow)', 'rgba(13, 17, 23, 0.94)'],
  ['.hop-popover', 'var(--surface-radius)', 'var(--surface-bubble-shadow)'],
  ['.leaflet-popup-content-wrapper', 'var(--surface-radius)', 'var(--surface-bubble-shadow)'],
  ['.map-legend', 'var(--surface-radius)', 'var(--surface-bubble-shadow)'],
];
surfaces.forEach(([sel, radius, shadow, bg]) => {
  const body = rule(sel);
  assert(body.includes(radius), `${sel} must take its corners from ${radius}`);
  assert(body.includes(shadow), `${sel} must take its shadow from ${shadow}`);
  if (bg) {
    assert(body.includes(bg), `${sel} must keep its own background ${bg}`);
  } else {
    assert(/background:\s*var\(--bg2\)/.test(body), `${sel} must be the dark panel, never white`);
  }
});

// --- the map's own popups are themed, which only works in this order
const leafletAt = tpl.indexOf("leaflet/leaflet.css");
const appAt = tpl.indexOf("css/app.css");
assert(leafletAt > 0 && appAt > 0, 'both stylesheets must be linked');
assert(leafletAt < appAt, 'leaflet.css must load BEFORE app.css, or its white popup wins again');
assert(/\.leaflet-popup-content-wrapper/.test(css), 'the Leaflet popup theme must exist in app.css');

// --- one close control, shared by every surface
const closeStart = css.indexOf('.surface-close, .modal-close');
assert(closeStart > 0, 'the shared close rule must exist');
const closeGroup = css.slice(closeStart, css.indexOf('.hop-pop-close {'));
['.modal-close', '.hop-pop-close', '.map-dm-close'].forEach(c => {
  assert(closeGroup.includes(c), `${c} must use the shared close look`);
});
assert(/font-size:\s*18px/.test(closeGroup), 'the shared close glyph is 18px');
assert(/color:\s*var\(--muted\)/.test(closeGroup), 'the shared close glyph is muted grey');
assert(/background:\s*transparent/.test(closeGroup), 'the shared close sits on the panel, no button box');
assert(/inset:\s*-5px/.test(closeGroup), 'the shared close has a finger-sized hit area');

// --- every dialog has exactly one x, and it uses the same glyph
const headers = tpl.match(/<div class="modal-header">[\s\S]*?<\/div>/g) || [];
assert(headers.length === 10, `expected 10 dialog headers, found ${headers.length}`);
headers.forEach((h, i) => {
  const n = (h.match(/class="modal-close"/g) || []).length;
  assert(n === 1, `dialog header ${i} must carry exactly one close control, found ${n}`);
  assert(h.includes('&times;'), `dialog header ${i} must use the same x glyph`);
  assert(/aria-label="Close"/.test(h), `dialog header ${i} close must be named for a screen reader`);
});
assert(!/&#10005;|&#x2715;/.test(tpl), 'the old close glyphs must be gone from the template');
// a dialog that has an x must also close on it, using its own closer
const closers = {
  'confirm-modal': 'confirmCancel()',
  'mc-delete-modal': 'mcDeleteCancel()',
  'node-cleanup-modal': 'closeNodeCleanupModal()',
  'sr-confirm-modal': 'srConfirmCancel()',
  'polar-customize-modal': 'closePolarCustomize()',
  'alert-modal': 'alertClose()',
  'toc-tpl-picker-modal': '_tocPickTemplateClose()',
  'intro-modal': 'closeOmIntro(true)',
  'manual-modal': 'closeOmManual()',
  'action-modal': 'closeModal()',
};
Object.entries(closers).forEach(([id, fn]) => {
  const at = tpl.indexOf('id="' + id + '"');
  assert(at > 0, `dialog ${id} missing`);
  const block = tpl.slice(at, tpl.indexOf('class="modal-body"', at));
  assert(block.includes('onclick="' + fn + '"'),
    `the x on ${id} must call ${fn}, the same closer the overlay uses`);
});

// --- the alerts panel is the ONE popup with no x, on purpose (Filip, S488):
//     it closes on an outside tap and on the bell instead, and the header keeps Clear.
assert(/class="alerts-close"/.test(app) === false, 'the alerts panel must carry no close control (S488)');
assert(/alerts-close/.test(css) === false, 'the shared close styling must no longer mention .alerts-close');
assert(/class="alert[^"]*"[^>]*>Close</.test(app) === false, 'no worded Close button in the alerts panel');
assert(/alerts-clear-btn/.test(app), 'the alerts header keeps Clear all');
assert(/alerts-actions/.test(app), 'the alerts header keeps its action group');
assert(/class="alerts-title">Alerts<\/span><\/div>/.test(app), 'the empty-state header is title-only');
// and the two ways it does close must stay in place
assert(/getElementById\('alerts-panel'\)\?\.classList\.remove\('open'\)/.test(app), 'an outside tap must still close the alerts panel');
assert(/id="alerts-btn"[^>]*onclick="toggleAlertsPanel\(event\)"/.test(tpl), 'the bell must still toggle the alerts panel');

// --- the map DM window uses the shared close and the same glyph
assert(/class="map-dm-close"[^>]*>&times;</.test(tpl), 'the map DM window close must be the shared x');

console.log('ok: one look for dialogs, menus, popups and the map bubble');

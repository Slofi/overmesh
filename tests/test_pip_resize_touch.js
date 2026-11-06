// Minimap resize grip: real initPipMap() from app.js run against fakes.
//
// Filip: "on CD (touchscreen) the minimap cant be resized - dragging with a
// finger doesn't work. Mouse works ok." The grip listened to mousedown only, so
// a touch never resized it (and an 18px corner is not a finger target).
//
// What must hold:
//  - the grip uses POINTER events only, with setPointerCapture and no private
//    mousedown path;
//  - pointerdown stops the page/map pan (preventDefault + stopPropagation);
//  - a drag moves the width/height, clamps it, and a mouse pointer still works;
//  - a different pointer id, a non-left mouse button and events after pointerup
//    change nothing;
//  - the CSS grip is at least 40x40 and sets touch-action: none;
//  - the header's mouse drag is still there.
const fs = require('fs');
const path = require('path');
const REPO = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(REPO, 'static/js/app.js'), 'utf8');
const css = fs.readFileSync(path.join(REPO, 'static/css/app.css'), 'utf8');

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

const env = new Function([
  'const listeners = {}, captured = [], released = [];',
  'function mkEl(id) {',
  '  return { id, style: {},',
  '    addEventListener(type, fn) { (listeners[id + ":" + type] = listeners[id + ":" + type] || []).push(fn); },',
  '    setPointerCapture(pid) { captured.push(pid); },',
  '    releasePointerCapture(pid) { released.push(pid); },',
  '    getBoundingClientRect() { return { width: 280, height: 210, right: 500, bottom: 400 }; } };',
  '}',
  'const wrap = mkEl("pip-wrap"), header = mkEl("pip-header"), handle = mkEl("pip-resize-handle");',
  'const document = { getElementById: id => id === "pip-wrap" ? wrap : id === "pip-header" ? header : id === "pip-resize-handle" ? handle : null,',
  '  addEventListener(type, fn) { (listeners["document:" + type] = listeners["document:" + type] || []).push(fn); } };',
  'const window = { innerWidth: 800, innerHeight: 600 };',
  'let pipOpen = true, pipInited = false, pipMap = null, pipMarkers = {}, allNodes = [];',
  'const pipMapFake = { setView() { return this; }, attributionControl: { setPrefix() {} }, invalidateSize() {} };',
  'const L = { map: () => pipMapFake, divIcon: o => o, marker: () => ({ bindTooltip() { return this; }, addTo() { return this; } }) };',
  'function _pipSyncTileLayer() {}',
  'function updatePipMarkers() {}',
  'function _pipTracesSync() {}',
  'class ResizeObserver { observe() {} }',
  'const setTimeout = () => 0;',
  extract('initPipMap'),
  'return { initPipMap, listeners, captured, released, wrap, header, handle, map: () => pipMap };',
].join('\n'))();

env.initPipMap();

const on = (id, type) => {
  const fn = env.listeners[id + ':' + type];
  assert(fn && fn.length, `${id} must listen for ${type}`);
  return fn[0];
};
const down  = on('pip-resize-handle', 'pointerdown');
const move  = on('pip-resize-handle', 'pointermove');
const up    = on('pip-resize-handle', 'pointerup');
const cancel = on('pip-resize-handle', 'pointercancel');
assert(env.listeners['pip-resize-handle:mousedown'] === undefined,
  'the grip has no separate mousedown path, pointer events serve mouse and finger');
assert(env.listeners['pip-header:mousedown'] && env.listeners['pip-header:mousedown'].length,
  'the header mouse drag is still there');

// pointerdown: capture, prevent page pan, stop the map
let ev = { pointerId: 7, button: 0, clientX: 300, clientY: 300, prevented: 0, stopped: 0,
           preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; } };
down(ev);
assert(ev.prevented === 1 && ev.stopped === 1, 'pointerdown prevents default and stops propagation');
assert(env.captured[0] === 7, 'pointerdown captures the pointer on the grip');

// drag left/up expands: +50 width, +40 height
move({ pointerId: 7, clientX: 250, clientY: 260, preventDefault() {} });
assert(env.wrap.style.width === '330px' && env.wrap.style.height === '250px',
  `drag resizes the minimap, got ${env.wrap.style.width} x ${env.wrap.style.height}`);

// another pointer id can not move it
move({ pointerId: 9, clientX: 0, clientY: 0, preventDefault() {} });
assert(env.wrap.style.width === '330px' && env.wrap.style.height === '250px', 'a different pointer id is ignored');

// clamps: at most half the window width and 60% of its height
move({ pointerId: 7, clientX: -1000, clientY: -1000, preventDefault() {} });
assert(env.wrap.style.width === '400px' && env.wrap.style.height === '360px',
  `size is clamped to half width / 60% height, got ${env.wrap.style.width} x ${env.wrap.style.height}`);

// pointerup ends the drag and releases capture
up({ pointerId: 7 });
assert(env.released.includes(7), 'pointerup releases the pointer capture');
const w = env.wrap.style.width;
move({ pointerId: 7, clientX: 0, clientY: 0, preventDefault() {} });
assert(env.wrap.style.width === w, 'no resize after pointerup');

// mouse still works through the same pointer path
down({ pointerId: 11, button: 0, clientX: 300, clientY: 300, preventDefault() {}, stopPropagation() {} });
move({ pointerId: 11, clientX: 280, clientY: 280, preventDefault() {} });
assert(env.wrap.style.width === '300px' && env.wrap.style.height === '230px',
  `mouse pointer resizes too, got ${env.wrap.style.width} x ${env.wrap.style.height}`);
cancel({ pointerId: 11 });
assert(env.released.includes(11), 'pointercancel ends the drag and releases capture');

// right mouse button does not start a resize
down({ pointerId: 12, button: 2, clientX: 300, clientY: 300, preventDefault() {}, stopPropagation() {} });
const w2 = env.wrap.style.width;
move({ pointerId: 12, clientX: 100, clientY: 100, preventDefault() {} });
assert(env.wrap.style.width === w2, 'a non-left mouse button does not resize');

// CSS: finger-sized grip and no page pan
const block = css.match(/#pip-resize-handle\s*\{([^}]*)\}/);
assert(block, '#pip-resize-handle rule exists');
const body = block[1];
const width = parseInt((body.match(/width:\s*(\d+)px/) || [])[1], 10);
const height = parseInt((body.match(/height:\s*(\d+)px/) || [])[1], 10);
assert(width >= 40 && height >= 40, `the grip is finger-sized (>= 40x40), got ${width}x${height}`);
assert(/touch-action:\s*none/.test(body), 'the grip sets touch-action: none so the page cannot pan under it');

console.log('OK test_pip_resize_touch');

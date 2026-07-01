// OverMesh Lite — frontend
// Single-file JS for 5-tab touch UI (Map, Nodes, Chat, Log, Settings)

'use strict';

// ── State ────────────────────────────────────────────────────────────────────
const S = {
  nodes:      {},   // MT: node_id → node obj
  mcNodes:    {},   // MC: radio_id → { contact_id → contact }
  mtRadios:   [],
  mcRadios:   [],
  mtChannels: [],   // [{name,index}]
  mcChannels: {},   // radio_id → [{name,index}]
  mtMsgs:     {},   // channel_idx → [msg]
  mtDmMsgs:   {},   // node_id → [msg]
  mcMsgs:     {},   // radio_id → { chan:{idx:[msg]}, dm:{contact_id:[msg]} }
  logEntries: [],
  logSubtab:  'toc',
  editingLogId: null,
  chatCtx:    null, // {type:'mt_chan'|'mt_dm'|'mc_chan'|'mc_dm', radioId, key}
  chatFilter: 'all',
  mapMsgFeed: [], // [{type,radioId,key,from,text,ts,label,pathLen?,routeType?,rssi?,snr?}]
  activeTab:  'map',
  nFilter:    'all',
  selectedNode: null, // {type:'mt'|'mc', id, radioId}
  selectedLogId: null,
  unread:     0,
  chUnread:   {}, // 'type:radioId:key' → count, per-channel unread badge
  senseOn:    false, traceOn: false,
  sseSource:  null,
  layerOpen:  false,
  activeDms:  new Set(), // 'type:radioId:nodeId' keys for active DM threads
  gpsMarker:  null,
  selfMarker: null, // HD's own set/advertised position marker
  activeTraceLine: null, // single persistent trace line drawn from tapping a message
  activeTraceMsgIdx: null, // mapMsgFeed index the active trace belongs to (for toggle-off)
  markers:    {},   // node_id/contact_id → L.circleMarker
  markerTypes: {}, // node_id → 'mt'|'mc'
  mapFilter:  'all',
  traceLines: [],
  followGps:  localStorage.getItem('lm_follow_gps') === '1',
  gpsCenterPrimed: false,
  activity:   [],
  sending:    false,
  accent:     localStorage.getItem('om_accent') || '#e8b04f',
  updateInfo: null,
  updateRunning: false,
  pendingLogContext: null, // sender/network/time/text captured from "Log this message"
  favNodes:   new Set(JSON.parse(localStorage.getItem('om_favs') || '[]')),
};

// ── Map ──────────────────────────────────────────────────────────────────────
let map, activeLayer;
let _mcPosPickRadio = null;

const LAYERS = {
  voyager: { label: 'Voyager',          url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', attr: '© OSM © CARTO', maxZoom: 19 },
  dark:    { label: 'Dark Matter',      url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', attr: '© OSM © CARTO', maxZoom: 19 },
  osm:     { label: 'OpenStreetMap',    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', attr: '© OSM', maxZoom: 19 },
  topo:    { label: 'OpenTopoMap',      url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', attr: '© OSM © OpenTopoMap', maxZoom: 17 },
  sat:     { label: 'Esri Satellite',   url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr: '© Esri', maxZoom: 18 },
  terrain: { label: 'Stadia Outdoors',  url: 'https://tiles.stadiamaps.com/tiles/outdoors/{z}/{x}/{y}{r}.png', attr: '© Stadia © OSM', maxZoom: 20 },
};

function initMap() {
  const panel = document.getElementById('tab-map');
  map = L.map('map', {
    zoomControl: true,
    attributionControl: true,
    dragging: true,
    touchZoom: true,
    doubleClickZoom: true,
    tap: false,
  }).setView([46.1, 14.8], 10);
  map.zoomControl.setPosition('bottomleft');
  const savedKey = localStorage.getItem('lm_layer');
  const saved = LAYERS[savedKey] ? savedKey : 'voyager';
  applyLayer(saved);
  renderLayerPanel();
  map.on('click', (e) => {
    if (_mcPosPickRadio) {
      const radioId = _mcPosPickRadio;
      _mcPosPickRadio = null;
      document.getElementById('map-pick-banner')?.setAttribute('hidden', '');
      setMcPosition(radioId, e.latlng.lat, e.latlng.lng);
      return;
    }
    closeLayerPanel(); closeMapMenu();
  });
  map.on('dragstart zoomstart', () => {
    if (S.followGps) {
      S.followGps = false;
      S.gpsCenterPrimed = false;
      localStorage.setItem('lm_follow_gps', '0');
      document.getElementById('btn-follow')?.classList.remove('active');
    }
  });
  map.dragging.enable();
  map.touchZoom.enable();
  map.scrollWheelZoom.enable();
  map.doubleClickZoom.enable();
  installTouchPanFallback();
  refreshMapLayout();
  window.addEventListener('resize', refreshMapLayout);
  setTimeout(refreshMapLayout, 80);
}

function refreshMapLayout() {
  if (!map) return;
  map.invalidateSize({ pan: false });
}

function installTouchPanFallback() {
  const el = document.getElementById('map');
  if (!el || el.dataset.touchPanFallback === '1') return;
  el.dataset.touchPanFallback = '1';
  const state = { active: false, moved: false, x: 0, y: 0, pointerId: null, mode: null };
  const isControl = target => !!target.closest?.(
    '.leaflet-control, .leaflet-marker-icon, .leaflet-popup, .map-fab, #map-status-bar, #map-menu-panel, #layer-panel'
  );
  const releaseFollow = () => {
    if (!S.followGps) return;
    S.followGps = false;
    S.gpsCenterPrimed = false;
    localStorage.setItem('lm_follow_gps', '0');
    document.getElementById('btn-follow')?.classList.remove('active');
  };
  el.addEventListener('touchstart', ev => {
    if (ev.touches.length !== 1 || isControl(ev.target)) {
      state.active = false;
      state.mode = null;
      return;
    }
    state.active = true;
    state.moved = false;
    state.mode = 'touch';
    state.pointerId = null;
    state.x = ev.touches[0].clientX;
    state.y = ev.touches[0].clientY;
  }, { passive: true });
  el.addEventListener('touchmove', ev => {
    if (!state.active || state.mode !== 'touch' || ev.touches.length !== 1 || !map) return;
    const t = ev.touches[0];
    const dx = t.clientX - state.x;
    const dy = t.clientY - state.y;
    if (!state.moved && Math.hypot(dx, dy) < 5) return;
    state.moved = true;
    releaseFollow();
    ev.preventDefault();
    map.panBy([-dx, -dy], { animate: false });
    state.x = t.clientX;
    state.y = t.clientY;
  }, { passive: false });
  el.addEventListener('touchend', () => {
    if (state.mode === 'touch') {
      state.active = false;
      state.mode = null;
    }
  }, { passive: true });
  el.addEventListener('touchcancel', () => {
    if (state.mode === 'touch') {
      state.active = false;
      state.mode = null;
    }
  }, { passive: true });

  el.addEventListener('pointerdown', ev => {
    if (state.mode === 'touch') return;
    if (!ev.isPrimary || isControl(ev.target)) return;
    if (ev.pointerType === 'mouse' && ev.button !== 0) return;
    state.active = true;
    state.moved = false;
    state.mode = 'pointer';
    state.pointerId = ev.pointerId;
    state.x = ev.clientX;
    state.y = ev.clientY;
    try { el.setPointerCapture(ev.pointerId); } catch {}
  }, { passive: true });
  el.addEventListener('pointermove', ev => {
    if (!state.active || state.mode !== 'pointer' || state.pointerId !== ev.pointerId || !map) return;
    const dx = ev.clientX - state.x;
    const dy = ev.clientY - state.y;
    if (!state.moved && Math.hypot(dx, dy) < 4) return;
    state.moved = true;
    releaseFollow();
    ev.preventDefault();
    map.panBy([-dx, -dy], { animate: false });
    state.x = ev.clientX;
    state.y = ev.clientY;
  }, { passive: false });
  const endPointerPan = ev => {
    if (state.pointerId === ev.pointerId) {
      state.active = false;
      state.mode = null;
      state.pointerId = null;
      try { el.releasePointerCapture(ev.pointerId); } catch {}
    }
  };
  el.addEventListener('pointerup', endPointerPan, { passive: true });
  el.addEventListener('pointercancel', endPointerPan, { passive: true });
}

function applyLayer(key) {
  const def = LAYERS[key] || LAYERS.voyager;
  if (activeLayer) map.removeLayer(activeLayer);
  activeLayer = L.tileLayer(def.url, { attribution: def.attr, maxZoom: def.maxZoom }).addTo(map);
  localStorage.setItem('lm_layer', LAYERS[key] ? key : 'voyager');
  document.querySelectorAll('.layer-item').forEach(el =>
    el.classList.toggle('active', el.dataset.key === key));
}

function renderLayerPanel() {
  const el = document.getElementById('layer-panel');
  const activeKey = LAYERS[localStorage.getItem('lm_layer')] ? localStorage.getItem('lm_layer') : 'voyager';
  el.innerHTML = Object.entries(LAYERS).map(([k, d]) =>
    `<div class="layer-item${activeKey===k?' active':''}"
      data-key="${k}" onclick="applyLayer('${k}');closeLayerPanel()">${d.label}</div>`
  ).join('');
}

function toggleLayerPanel() {
  S.layerOpen = !S.layerOpen;
  document.getElementById('layer-panel').hidden = !S.layerOpen;
}
function closeLayerPanel() {
  S.layerOpen = false;
  document.getElementById('layer-panel').hidden = true;
}

function gpsButton() {
  if (!S.gpsMarker) { toast('No GPS fix yet'); return; }
  const btn = document.getElementById('btn-follow');
  const pos = S.gpsMarker.getLatLng();
  if (S.followGps) {
    S.followGps = false;
    S.gpsCenterPrimed = false;
    localStorage.setItem('lm_follow_gps', '0');
    btn?.classList.remove('active');
    toast('GPS follow off');
    return;
  }
  if (!S.gpsCenterPrimed) {
    map.setView(pos, Math.max(map.getZoom(), 14));
    S.gpsCenterPrimed = true;
    btn?.classList.add('active');
    toast('GPS centered. Tap again to follow.');
    return;
  }
  S.followGps = true;
  S.gpsCenterPrimed = false;
  localStorage.setItem('lm_follow_gps', '1');
  btn?.classList.add('active');
  map.setView(pos, Math.max(map.getZoom(), 14));
  toast('Following GPS');
}

function updateGpsMarker(lat, lon) {
  const pos = [lat, lon];
  if (!S.gpsMarker) {
    S.gpsMarker = L.circleMarker(pos, {
      radius: 7, color: '#fff', weight: 2,
      fillColor: '#4a9eff', fillOpacity: 1,
    }).bindTooltip('GPS').addTo(map);
  } else {
    S.gpsMarker.setLatLng(pos);
  }
  if (S.followGps) map.panTo(pos, { animate: true });
}

let _mapMenuOpen = false;
function toggleMapMenu() {
  _mapMenuOpen = !_mapMenuOpen;
  const panel = document.getElementById('map-menu-panel');
  if (_mapMenuOpen) {
    panel.innerHTML = `
      <div class="layer-item${S.senseOn?' active':''}" onclick="toggleSense()">
        <span style="font-size:15px">📡</span> Sense (MT)
      </div>
      <div class="layer-item${S.traceOn?' active':''}" onclick="toggleTrace()">
        <span style="font-size:15px">〰</span> Trace (MC)
      </div>
      <div class="layer-item" onclick="openActivitySheet();closeMapMenu()">
        <span style="font-size:15px">◷</span> Recent activity
      </div>`;
    panel.hidden = false;
  } else {
    panel.hidden = true;
  }
}
function closeMapMenu() { _mapMenuOpen = false; document.getElementById('map-menu-panel').hidden = true; }

function toggleSense() {
  S.senseOn = !S.senseOn;
  fetch('/api/mesh/sense/passive', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ enabled: S.senseOn }) }).catch(() => {});
  toast(S.senseOn ? 'Sense: ON' : 'Sense: OFF');
  _applyMapMode(S.senseOn ? 'sense' : 'map');
  closeMapMenu();
}

function toggleTrace() {
  S.traceOn = !S.traceOn;
  if (!S.traceOn) { S.traceLines.forEach(l => map.removeLayer(l)); S.traceLines = []; }
  toast(S.traceOn ? 'Trace: ON — trigger from node sheet' : 'Trace: OFF');
  closeMapMenu();
}

function mtMarkerColor(node) {
  if (node?.is_local) return '#3b82f6';
  const ts = Number(node?.last_heard_ts || node?.last_heard || 0);
  if (!ts) return '#6e7681';
  const age = Date.now() / 1000 - ts;
  if (age < 1800) return '#86efac';
  if (age < 7200) return '#e07b30';
  return '#f85149';
}

function svgIcon(svg, size, popupY) {
  const s = size || 24;
  const c = s / 2;
  return L.divIcon({
    html: svg,
    className: '',
    iconSize: [s, s],
    iconAnchor: [c, c],
    popupAnchor: [0, popupY || -(c + 2)],
  });
}

function mtIcon(node) {
  const color = mtMarkerColor(node);
  const isLocal = !!node?.is_local;
  const outer = isLocal ? 10 : 7;
  const inner = isLocal ? 7 : 4;
  const sz = (outer + 5) * 2;
  const c = sz / 2;
  const pulse = isLocal
    ? `<circle cx="${c}" cy="${c}" r="${outer + 4}" fill="none" stroke="${color}" stroke-width="1.5" opacity="0.75"/>`
    : '';
  return svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}" style="filter:drop-shadow(0 1px 4px rgba(0,0,0,0.8));overflow:visible">
    ${pulse}
    <circle cx="${c}" cy="${c}" r="${outer}" fill="white"/>
    <circle cx="${c}" cy="${c}" r="${inner}" fill="${color}"/>
  </svg>`, sz);
}

function mcType(contact) {
  const raw = contact?.node_type ?? contact?.contact_type ?? contact?.role ?? contact?.type;
  const num = Number(raw);
  if (Number.isFinite(num)) return num;
  const text = String(raw ?? contact?.long_name ?? contact?.name ?? '').toLowerCase();
  if (text.includes('room')) return 3;
  if (text.includes('rptr') || text.includes('repeat')) return 2;
  return 0;
}

function mcIcon(contact) {
  const t = mcType(contact);
  const isRptr = t === 2;
  const isRoom = t === 3;
  const color = isRptr ? '#3b82f6' : isRoom ? '#fb923c' : '#60a5fa';
  const outer = 7;
  const inner = 4;
  const sz = (outer + 5) * 2;
  const c = sz / 2;
  const o = c - outer;
  const i = c - inner;
  const shape = (isRptr || isRoom)
    ? `<rect x="${o}" y="${o}" width="${outer * 2}" height="${outer * 2}" fill="white" rx="2"/>
       <rect x="${i}" y="${i}" width="${inner * 2}" height="${inner * 2}" fill="${color}" rx="1"/>`
    : `<circle cx="${c}" cy="${c}" r="${outer}" fill="white"/>
       <circle cx="${c}" cy="${c}" r="${inner}" fill="${color}"/>`;
  return svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}" style="filter:drop-shadow(0 1px 4px rgba(0,0,0,0.8));overflow:visible">
    ${shape}
  </svg>`, sz);
}

function markerIcon(type, node) {
  return type === 'mc' ? mcIcon(node) : mtIcon(node);
}

function markerLabel(type, node) {
  if (type === 'mt') return 'MT';
  const t = mcType(node);
  if (t === 2) return 'MC RPTR';
  if (t === 3) return 'MC ROOM';
  return 'MC';
}

function placeNodeMarker(id, lat, lon, type, name, online, nodeObj) {
  if (lat == null || lon == null) return;
  S.markerTypes[id] = type;
  const node = { ...(nodeObj || {}), online, lat, lon, name };
  const icon = markerIcon(type, node);
  const popup = `<b>${esc(name)}</b><br><span style="font-size:11px;color:var(--muted)">${markerLabel(type, node)}</span>`;
  const radioId = node.radio_id || node.radioId || '';
  if (S.markers[id]) {
    S.markers[id].setLatLng([lat, lon]).setIcon(icon).setPopupContent(popup);
  } else {
    S.markers[id] = L.marker([lat, lon], { icon })
      .addTo(map)
      .bindPopup(popup)
      .on('click', () => openNodeDetail(id, type, radioId));
    if (S.mapFilter !== 'all' && S.mapFilter !== type) map.removeLayer(S.markers[id]);
  }
}

function removeMarker(id) {
  if (S.markers[id]) { map.removeLayer(S.markers[id]); delete S.markers[id]; }
}

function updateMapStatus() {
  const types = Object.values(S.markerTypes);
  const mtN = types.filter(t => t === 'mt').length;
  const mcN = types.filter(t => t === 'mc').length;
  const total = mtN + mcN;
  let label = '';
  if (total) label = S.mapFilter === 'mt' ? `${mtN} MT` : S.mapFilter === 'mc' ? `${mcN} MC` : `${total} nodes`;
  document.getElementById('map-node-count').textContent = label;
}

// ── SSE ──────────────────────────────────────────────────────────────────────
function startSSE() {
  if (S.sseSource) { S.sseSource.close(); }
  const src = new EventSource('/api/chat/stream');
  S.sseSource = src;
  src.onmessage = e => handleSSE(JSON.parse(e.data));
  src.onerror = () => setTimeout(startSSE, 5000);
}

function handleSSE(d) {
  switch (d.type) {
    case 'node_status':
      onNodeStatus(d); break;
    case 'node_last_heard':
      if (S.nodes[d.from_id]) { S.nodes[d.from_id].last_heard = d.ts; renderNodes(); } break;
    case 'message':
      onMtMessage(d); break;
    case 'ack':
      break; // ignore for now
    case 'mc_status':
      onMcStatus(d); break;
    case 'mc_node':
      onMcNode(d); break;
    case 'mc_message':
      onMcMessage(d); break;
    case 'mc_trace_data':
      onMcTrace(d); break;
    case 'sense_response':
      onSenseResp(d); break;
    case 'gps_update':
    case 'gps_position':
      if (d.lat && d.lon && d.fix !== false) {
        updateGpsMarker(d.lat, d.lon);
        updateHdrGps(true, d.lat, d.lon);
      } else {
        updateHdrGps(false);
      }
      break;
    case 'sense_done':
      break;
  }
}

let _pathLines = [];
function drawMsgPath(lat, lon, color) {
  if (!lat || !lon || !map) return;
  let to;
  if (S.gpsMarker) { const p = S.gpsMarker.getLatLng(); to = [p.lat, p.lng]; }
  else if (S.selfMarker) { const p = S.selfMarker.getLatLng(); to = [p.lat, p.lng]; }
  else {
    const me = Object.values(S.nodes).find(n => n.is_me || n.my_node);
    if (!me?.lat) return;
    to = [me.lat, me.lon];
  }
  const line = L.polyline([[lat, lon], to], {
    color, weight: 2, opacity: 0.75, dashArray: '6 4', interactive: false
  }).addTo(map);
  _pathLines.push(line);
  setTimeout(() => {
    try { map.removeLayer(line); } catch(e) {}
    _pathLines = _pathLines.filter(l => l !== line);
  }, 7000);
}

// ── MC multi-hop path resolution ─────────────────────────────────────────────
// Ported from the full OM app: decodes a message's raw per-hop path hash string
// into real relay/repeater positions, resolving each hash against known contacts
// with the same geometry/plausibility scoring desktop uses (detour distance,
// bearing, segment length, contact freshness). Falls back progressively: this
// message's own embedded path -> contact's last-known cached route -> a plain
// direct line when no path can be resolved at all. No fabricated positions —
// unresolved hops are simply omitted and the result is marked partial.

const MC_PATH_SOFT_DETOUR_KM = 45;
const MC_PATH_HARD_DETOUR_KM = 180;
const MC_PATH_IMPLAUSIBLE_SEGMENT_KM = 200;
const MC_PATH_RADIO_ONLY_MAX_KM = 200;
const MC_PATH_SOFT_BEARING_DELTA_DEG = 55;
const MC_PATH_HARD_BEARING_DELTA_DEG = 105;
const MC_PATH_STALE_REMOTE_KM = 120;

function _mcTraceRadioPos() {
  if (S.gpsMarker) { const p = S.gpsMarker.getLatLng(); return [p.lat, p.lng]; }
  if (S.selfMarker) { const p = S.selfMarker.getLatLng(); return [p.lat, p.lng]; }
  return [null, null];
}

function _haversineMeters(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  const clamped = Math.max(0, Math.min(1, a));
  return 2 * R * Math.atan2(Math.sqrt(clamped), Math.sqrt(1 - clamped));
}

function _mcPathDirectKm(lat1, lon1, lat2, lon2) {
  if (lat1 == null || lon1 == null || lat2 == null || lon2 == null) return Number.POSITIVE_INFINITY;
  return _haversineMeters(lat1, lon1, lat2, lon2) / 1000;
}

function _mcPathDistanceScore(candidate, expectedLat, expectedLon) {
  const lat = candidate.latitude ?? candidate.lat;
  const lon = candidate.longitude ?? candidate.lon;
  if (lat == null || lon == null) return Number.POSITIVE_INFINITY;
  const dLat = lat - expectedLat;
  const dLon = (lon - expectedLon) * Math.cos((expectedLat || 0) * Math.PI / 180);
  return dLat * dLat + dLon * dLon;
}

function _mcPathGeoKm(candidate, expectedLat, expectedLon) {
  const score = _mcPathDistanceScore(candidate, expectedLat, expectedLon);
  if (!Number.isFinite(score)) return Number.POSITIVE_INFINITY;
  return Math.sqrt(score) * 111.32;
}

function _mcPathDetourKm(candidate, radioLat, radioLon, endpointLat, endpointLon) {
  const lat = candidate.latitude ?? candidate.lat;
  const lon = candidate.longitude ?? candidate.lon;
  if (lat == null || lon == null) return Number.POSITIVE_INFINITY;
  const directKm = _mcPathDirectKm(radioLat, radioLon, endpointLat, endpointLon);
  if (!Number.isFinite(directKm)) return Number.POSITIVE_INFINITY;
  const viaKm = _mcPathDirectKm(radioLat, radioLon, lat, lon) + _mcPathDirectKm(lat, lon, endpointLat, endpointLon);
  return Math.max(0, viaKm - directKm);
}

function _mcPathSegmentMaxKm(candidate, radioLat, radioLon, endpointLat, endpointLon) {
  const lat = candidate.latitude ?? candidate.lat;
  const lon = candidate.longitude ?? candidate.lon;
  if (lat == null || lon == null) return Number.POSITIVE_INFINITY;
  const a = _mcPathDirectKm(radioLat, radioLon, lat, lon);
  const b = _mcPathDirectKm(lat, lon, endpointLat, endpointLon);
  return Math.max(a, b);
}

function _mcPathBearingDeg(lat1, lon1, lat2, lon2) {
  if (lat1 == null || lon1 == null || lat2 == null || lon2 == null) return null;
  const toRad = d => d * Math.PI / 180;
  const lat1r = toRad(lat1), lat2r = toRad(lat2);
  const dLon = toRad(lon2 - lon1);
  const y = Math.sin(dLon) * Math.cos(lat2r);
  const x = Math.cos(lat1r) * Math.sin(lat2r) - Math.sin(lat1r) * Math.cos(lat2r) * Math.cos(dLon);
  const deg = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  return Number.isFinite(deg) ? deg : null;
}

function _mcPathBearingDeltaDeg(a, b) {
  if (a == null || b == null) return null;
  const diff = Math.abs(a - b) % 360;
  return diff > 180 ? 360 - diff : diff;
}

function _mcContactSeenTs(contact) {
  if (!contact) return 0;
  return contact.last_heard_ts || contact.last_seen_ts || contact.last_advert || 0;
}

function _mcPathCandidateScore(candidate, ctx = {}) {
  const hashBytes = Number(ctx.hashBytes || 1);
  let score = hashBytes >= 3 ? 46 : hashBytes === 2 ? 34 : 16;
  // Lite tags contact.type with the literal string 'mc'; the original numeric
  // MeshCore type (0/1/2, 2=repeater) is preserved separately as contact_type.
  const type = Number(candidate.contact_type ?? 0);
  if (type === 2) score += 24;
  else if (type > 0) score += 3;

  const lat = candidate.latitude ?? candidate.lat;
  const lon = candidate.longitude ?? candidate.lon;
  if (lat != null && lon != null) score += 8;

  const seenTs = _mcContactSeenTs(candidate);
  let age = null;
  if (seenTs) {
    age = Math.max(0, Math.floor(Date.now() / 1000) - seenTs);
    if (age < 3600) score += 16;
    else if (age < 6 * 3600) score += 11;
    else if (age < 24 * 3600) score += 7;
    else if (age < 7 * 24 * 3600) score += 3;
  }

  if (ctx.expectedLat != null && ctx.expectedLon != null && lat != null && lon != null) {
    const km = _mcPathGeoKm(candidate, ctx.expectedLat, ctx.expectedLon);
    score += Math.max(0, 42 - Math.min(42, km * 1.35));
  }
  if ((ctx.expectedLat == null || ctx.expectedLon == null) && ctx.radioLat != null && ctx.radioLon != null && lat != null && lon != null) {
    const radioKm = _mcPathDirectKm(ctx.radioLat, ctx.radioLon, lat, lon);
    if (Number.isFinite(radioKm)) {
      score += Math.max(0, 26 - Math.min(26, radioKm * 0.22));
      if (ctx.hashBytes <= 1 && radioKm > MC_PATH_RADIO_ONLY_MAX_KM) {
        score -= Math.min(180, (radioKm - MC_PATH_RADIO_ONLY_MAX_KM) * 1.4 + 42);
      }
      if (ctx.hashBytes <= 1 && age != null && age > 24 * 3600 && radioKm > MC_PATH_STALE_REMOTE_KM) {
        score -= Math.min(96, ((age - 24 * 3600) / 3600) * 0.35 + (radioKm - MC_PATH_STALE_REMOTE_KM) * 0.22 + 14);
      }
    }
  }

  if (ctx.radioLat != null && ctx.radioLon != null && ctx.endpointLat != null && ctx.endpointLon != null && lat != null && lon != null) {
    const directKm = _mcPathDirectKm(ctx.radioLat, ctx.radioLon, ctx.endpointLat, ctx.endpointLon);
    const radioKm = _mcPathDirectKm(ctx.radioLat, ctx.radioLon, lat, lon);
    const detourKm = _mcPathDetourKm(candidate, ctx.radioLat, ctx.radioLon, ctx.endpointLat, ctx.endpointLon);
    if (Number.isFinite(directKm) && Number.isFinite(detourKm)) {
      const softLimit = Math.max(MC_PATH_SOFT_DETOUR_KM, directKm * 0.25);
      const hardLimit = Math.max(MC_PATH_HARD_DETOUR_KM, directKm * 0.55);
      if (detourKm > hardLimit) score -= Math.min(180, (detourKm - hardLimit) * 1.6 + 36);
      else if (detourKm > softLimit) score -= Math.min(48, (detourKm - softLimit) * 0.7 + 10);
    }
    const segmentMaxKm = _mcPathSegmentMaxKm(candidate, ctx.radioLat, ctx.radioLon, ctx.endpointLat, ctx.endpointLon);
    if (Number.isFinite(segmentMaxKm) && segmentMaxKm > MC_PATH_IMPLAUSIBLE_SEGMENT_KM) {
      score -= Math.min(160, (segmentMaxKm - MC_PATH_IMPLAUSIBLE_SEGMENT_KM) * 1.25 + 40);
    }
    if (Number.isFinite(directKm) && Number.isFinite(radioKm) && directKm > 20) {
      const expectedFrac = Number.isFinite(ctx.expectedFrac) ? ctx.expectedFrac : null;
      if (expectedFrac != null) {
        const expectedKm = directKm * expectedFrac;
        const fracTolKm = Math.max(20, directKm * 0.18);
        const fracMiss = Math.abs(radioKm - expectedKm);
        if (fracMiss > fracTolKm) {
          const missScale = ctx.hashBytes <= 1 ? 0.65 : 0.4;
          score -= Math.min(ctx.hashBytes <= 1 ? 80 : 48, (fracMiss - fracTolKm) * missScale + 8);
        }
      }
      const endpointBearing = _mcPathBearingDeg(ctx.radioLat, ctx.radioLon, ctx.endpointLat, ctx.endpointLon);
      const candidateBearing = _mcPathBearingDeg(ctx.radioLat, ctx.radioLon, lat, lon);
      const bearingDelta = _mcPathBearingDeltaDeg(endpointBearing, candidateBearing);
      if (bearingDelta != null && radioKm > 15) {
        const soft = ctx.hashBytes <= 1 ? MC_PATH_SOFT_BEARING_DELTA_DEG : MC_PATH_SOFT_BEARING_DELTA_DEG + 15;
        const hard = ctx.hashBytes <= 1 ? MC_PATH_HARD_BEARING_DELTA_DEG : MC_PATH_HARD_BEARING_DELTA_DEG + 20;
        if (bearingDelta > hard) score -= Math.min(ctx.hashBytes <= 1 ? 130 : 84, (bearingDelta - hard) * 1.25 + 34);
        else if (bearingDelta > soft) score -= Math.min(ctx.hashBytes <= 1 ? 52 : 34, (bearingDelta - soft) * 0.75 + 8);
      }
    }
  }

  const outLen = Number(candidate.out_path_len);
  if (Number.isFinite(outLen) && outLen >= 0) score += 2;
  return score;
}

function _mcPathConfidence(hashBytes, matchCount, selectedCount, scoreGap, ambiguous) {
  if (ambiguous) return 'ambiguous';
  if (matchCount <= 1) {
    if (hashBytes >= 3) return 'exact';
    if (hashBytes === 2) return 'unique-2B';
    return 'unique-1B';
  }
  if (hashBytes >= 3 && scoreGap >= 6) return 'likely';
  if (hashBytes === 2 && scoreGap >= 10) return 'likely';
  if (hashBytes === 1 && scoreGap >= 16) return 'estimated';
  return selectedCount <= 1 ? 'likely' : 'estimated';
}

function findMcContactsByKeyPrefix(prefix, radioId = null) {
  if (!prefix) return [];
  const lp = String(prefix || '').toLowerCase();
  const seen = new Set();
  const matches = [];
  const addMatches = (contacts) => {
    for (const c of Object.values(contacts || {})) {
      const fk = (c.full_key || c.id || '').toLowerCase();
      if (!fk || !fk.startsWith(lp)) continue;
      const key = c.full_key || c.id;
      if (seen.has(key)) continue;
      seen.add(key);
      matches.push(c);
    }
  };
  if (radioId && S.mcNodes[radioId]) addMatches(S.mcNodes[radioId]);
  else Object.values(S.mcNodes).forEach(addMatches);
  return matches;
}

function _mcPickPathHopResolution(hashHex, radioId, radioLat, radioLon, endpointLat, endpointLon, hopIdx, hopCount, opts = {}) {
  const hash = String(hashHex || '').toLowerCase();
  if (!hash) return null;
  const hashBytes = Math.max(1, Math.min(3, Math.ceil(hash.length / 2)));
  const allMatches = findMcContactsByKeyPrefix(hash, radioId);
  if (!allMatches.length) return null;

  let candidates = opts.requireGps === false
    ? [...allMatches]
    : allMatches.filter(c => (c.latitude ?? c.lat) != null && (c.longitude ?? c.lon) != null);
  if (!candidates.length) return null;

  const repeaters = candidates.filter(c => (c.contact_type ?? 0) === 2);
  if (repeaters.length) candidates = repeaters;

  let expectedLat = null, expectedLon = null;
  if (radioLat != null && radioLon != null && endpointLat != null && endpointLon != null) {
    const frac = (hopIdx + 1) / (hopCount + 1);
    expectedLat = radioLat + (endpointLat - radioLat) * frac;
    expectedLon = radioLon + (endpointLon - radioLon) * frac;
  }

  const ranked = candidates.map(contact => ({
    contact,
    score: _mcPathCandidateScore(contact, {
      hashBytes, expectedLat, expectedLon, radioLat, radioLon, endpointLat, endpointLon,
      expectedFrac: hopCount >= 0 ? ((hopIdx + 1) / (hopCount + 1)) : null,
    }),
  })).sort((a, b) => b.score - a.score);
  const best = ranked[0];
  if (!best) return null;

  const second = ranked[1] || null;
  const scoreGap = second ? best.score - second.score : Number.POSITIVE_INFINITY;
  const gapNeeded = hashBytes >= 3 ? 4 : hashBytes === 2 ? 8 : 14;
  const noGeometry = expectedLat == null || expectedLon == null;
  const bestDetourKm = _mcPathDetourKm(best.contact, radioLat, radioLon, endpointLat, endpointLon);
  const directKm = _mcPathDirectKm(radioLat, radioLon, endpointLat, endpointLon);
  const bestSegmentMaxKm = _mcPathSegmentMaxKm(best.contact, radioLat, radioLon, endpointLat, endpointLon);
  const bestRadioOnlyKm = _mcPathDirectKm(radioLat, radioLon, best.contact?.latitude ?? best.contact?.lat, best.contact?.longitude ?? best.contact?.lon);
  const absurdDetour = Number.isFinite(bestDetourKm) && Number.isFinite(directKm) && bestDetourKm > Math.max(MC_PATH_HARD_DETOUR_KM, directKm * 1.1);
  const implausibleSegment = Number.isFinite(bestSegmentMaxKm) && bestSegmentMaxKm > MC_PATH_IMPLAUSIBLE_SEGMENT_KM;
  const implausibleRadioOnly = noGeometry && hashBytes <= 1 && Number.isFinite(bestRadioOnlyKm) && bestRadioOnlyKm > MC_PATH_RADIO_ONLY_MAX_KM;
  const ambiguous = ranked.length > 1 && (
    scoreGap < gapNeeded
    || (noGeometry && hashBytes <= 1 && scoreGap < 22)
    || ((absurdDetour || implausibleSegment || implausibleRadioOnly) && scoreGap < gapNeeded + 18)
  );

  if ((absurdDetour || implausibleSegment || implausibleRadioOnly) && (ranked.length > 1 || hashBytes <= 1) && scoreGap < gapNeeded + 18) return null;

  return {
    contact: best.contact, hash, hashBytes,
    matchCount: allMatches.length, selectedCount: candidates.length, ambiguous,
    confidence: _mcPathConfidence(hashBytes, allMatches.length, candidates.length, scoreGap, ambiguous),
    scoreGap,
  };
}

function _mcPathHopMeta(hashHex, contact, pointIndex, resolution = null) {
  const name = contact ? (contact.long_name || contact.name || contact.id || hashHex) : (hashHex || '?');
  return {
    hash: String(hashHex || '').toLowerCase(), name, pointIndex,
    contactId: contact?.id || contact?.full_key || null,
    confidence: resolution?.confidence || null,
    ambiguous: !!resolution?.ambiguous,
    matchCount: resolution?.matchCount || null,
  };
}

function _mcPathHashSize(value, mode = null) {
  const size = Number(value);
  if (Number.isFinite(size) && size >= 1 && size <= 3) return size;
  const m = Number(mode);
  if (Number.isFinite(m) && m >= 0 && m <= 2) return m + 1;
  return 1;
}

function _mcExplicitPathHashSize(evt) {
  const size = Number(evt?.pathHashSize);
  if (Number.isFinite(size) && size >= 1 && size <= 3) return size;
  const mode = Number(evt?.pathHashMode);
  if (Number.isFinite(mode) && mode >= 0 && mode <= 2) return mode + 1;
  return null;
}

function _mcPathLengthKm(points) {
  if (!points || points.length < 2) return Number.POSITIVE_INFINITY;
  let meters = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    if (!a || !b) continue;
    meters += _haversineMeters(a[0], a[1], b[0], b[1]);
  }
  return meters / 1000;
}

function _mcPathMonotonicPenalty(points) {
  if (!points || points.length < 3) return 0;
  const start = points[0], end = points[points.length - 1];
  let penalty = 0;
  const tolKm = 5;
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1], cur = points[i];
    const prevFromStart = _mcPathDirectKm(start[0], start[1], prev[0], prev[1]);
    const curFromStart = _mcPathDirectKm(start[0], start[1], cur[0], cur[1]);
    if (Number.isFinite(prevFromStart) && Number.isFinite(curFromStart) && curFromStart + tolKm < prevFromStart) {
      penalty += (prevFromStart - curFromStart);
    }
    const prevToEnd = _mcPathDirectKm(prev[0], prev[1], end[0], end[1]);
    const curToEnd = _mcPathDirectKm(cur[0], cur[1], end[0], end[1]);
    if (Number.isFinite(prevToEnd) && Number.isFinite(curToEnd) && curToEnd > prevToEnd + tolKm) {
      penalty += (curToEnd - prevToEnd);
    }
  }
  return penalty;
}

function _mcRelayProgressPenalty(points) {
  if (!points || points.length < 3) return 0;
  const start = points[0];
  let penalty = 0;
  const tolKm = 5;
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1], cur = points[i];
    const prevDist = _mcPathDirectKm(start[0], start[1], prev[0], prev[1]);
    const curDist = _mcPathDirectKm(start[0], start[1], cur[0], cur[1]);
    if (Number.isFinite(prevDist) && Number.isFinite(curDist) && curDist + tolKm < prevDist) {
      penalty += (prevDist - curDist);
    }
  }
  return penalty;
}

function _mcDecodedPathScore(result, expectedHopCount) {
  if (!result?.points || result.points.length < 2) return Number.POSITIVE_INFINITY;
  const directKm = _mcPathLengthKm([result.points[0], result.points[result.points.length - 1]]);
  const pathKm = _mcPathLengthKm(result.points);
  const missing = Math.max(0, (expectedHopCount || 0) - (result.hops || []).length);
  const ambiguity = (result.hops || []).filter(h => h.ambiguous || ['likely', 'estimated'].includes(h.confidence)).length;
  const detourKm = Number.isFinite(directKm) ? Math.max(0, pathKm - directKm) : pathKm;
  const monotonicPenalty = _mcPathMonotonicPenalty(result.points);
  return pathKm + detourKm * 0.75 + missing * 250 + ambiguity * 60 + monotonicPenalty * 12;
}

function _mcReversePathResult(pathResult) {
  if (!pathResult?.points) return pathResult;
  const lastIdx = pathResult.points.length - 1;
  const hops = (pathResult.hops || []).map(h => ({ ...h, pointIndex: lastIdx - h.pointIndex }));
  return { ...pathResult, points: [...pathResult.points].reverse(), hops };
}

function decodeMcPath(contact, radioLat, radioLon, radioId = null) {
  const pathHex = contact.out_path || '';
  const hashBytes = _mcPathHashSize(contact.out_path_hash_size, contact.out_path_hash_mode);
  const hashChars = hashBytes * 2;
  const hopCount = contact.out_path_len ?? -1;
  const cLat = contact.latitude ?? contact.lat;
  const cLon = contact.longitude ?? contact.lon;
  if (cLat == null || cLon == null) return null;

  if (hopCount < 0) {
    if (radioLat != null && radioLon != null) return { points: [[radioLat, radioLon], [cLat, cLon]], partial: false, flood: true };
    return null;
  }
  if (hopCount === 0) {
    if (radioLat != null && radioLon != null) return { points: [[radioLat, radioLon], [cLat, cLon]], partial: false };
    return null;
  }

  const points = [];
  const hops = [];
  let partial = false;
  if (radioLat != null && radioLon != null) points.push([radioLat, radioLon]);

  for (let i = 0; i < hopCount; i++) {
    const hashHex = pathHex.slice(i * hashChars, (i + 1) * hashChars);
    if (!hashHex || hashHex.replace(/0/g, '') === '') break;
    const hopResolution = _mcPickPathHopResolution(hashHex, radioId, radioLat, radioLon, cLat, cLon, i, hopCount);
    const hopContact = hopResolution?.contact || null;
    if (!hopContact) { partial = true; continue; }
    const hLat = hopContact.latitude ?? hopContact.lat;
    const hLon = hopContact.longitude ?? hopContact.lon;
    if (hLat == null || hLon == null) { partial = true; continue; }
    points.push([hLat, hLon]);
    hops.push(_mcPathHopMeta(hashHex, hopContact, points.length - 1, hopResolution));
    if (hopResolution?.ambiguous) partial = true;
  }
  points.push([cLat, cLon]);
  return points.length >= 2 ? { points, partial, hops } : null;
}

function decodeMcEventPath(evt, endpointLat, endpointLon, radioLat, radioLon, radioId = null) {
  const pathHex = evt?.path || '';
  const hopCount = evt?.pathLen;
  if (endpointLat == null || endpointLon == null) return null;
  if (hopCount == null || hopCount < 0 || hopCount === 255) return null;
  if (hopCount === 0) {
    if (radioLat != null && radioLon != null) return { points: [[radioLat, radioLon], [endpointLat, endpointLon]], partial: false };
    return null;
  }
  if (!pathHex) return null;
  const explicitSize = _mcExplicitPathHashSize(evt);
  const candidateSizes = explicitSize ? [explicitSize] : [1, 2, 3].filter(size => pathHex.length >= hopCount * size * 2);
  const build = (hashes, orderLabel, hashSize) => {
    const points = [];
    const hops = [];
    let partial = false;
    if (radioLat != null && radioLon != null) points.push([radioLat, radioLon]);
    hashes.forEach((hashHex, i) => {
      const hopResolution = _mcPickPathHopResolution(hashHex, radioId, radioLat, radioLon, endpointLat, endpointLon, i, hashes.length);
      const hopContact = hopResolution?.contact || null;
      if (!hopContact) { partial = true; return; }
      const hLat = hopContact.latitude ?? hopContact.lat;
      const hLon = hopContact.longitude ?? hopContact.lon;
      if (hLat == null || hLon == null) { partial = true; return; }
      points.push([hLat, hLon]);
      hops.push(_mcPathHopMeta(hashHex, hopContact, points.length - 1, hopResolution));
      if (hopResolution?.ambiguous) partial = true;
    });
    points.push([endpointLat, endpointLon]);
    return points.length >= 2 ? { points, partial, hops, rawPathOrder: orderLabel, inferredPathHashSize: hashSize } : null;
  };
  let best = null;
  candidateSizes.forEach(hashSize => {
    const hashChars = hashSize * 2;
    const hashList = [];
    for (let i = 0; i < hopCount; i++) {
      const hashHex = pathHex.slice(i * hashChars, (i + 1) * hashChars);
      if (!hashHex || hashHex.replace(/0/g, '') === '') break;
      hashList.push(hashHex);
    }
    const forward = build(hashList, 'as-received', hashSize);
    const reverse = hashList.length > 1 ? build([...hashList].reverse(), 'reversed', hashSize) : null;
    const chosen = (!reverse) ? forward
      : (!forward) ? reverse
      : ((_mcDecodedPathScore(forward, hopCount) + 25 < _mcDecodedPathScore(reverse, hopCount)) ? forward : reverse);
    if (!chosen) return;
    const score = _mcDecodedPathScore(chosen, hopCount);
    if (!best || score < best.score) best = { score, result: chosen };
  });
  return best?.result || null;
}

function decodeMcRelayPath(evt, radioLat, radioLon, radioId = null) {
  const pathHex = evt?.path || '';
  if (radioLat == null || radioLon == null) return null;
  if (!pathHex) return null;
  const rawHopCount = evt?.pathLen;
  const explicitSize = _mcExplicitPathHashSize(evt);
  const candidates = explicitSize ? [explicitSize] : [1, 2, 3].filter(size => pathHex.length >= size * 2);
  let best = null;
  candidates.forEach(hashSize => {
    const hashChars = hashSize * 2;
    const hopCount = (rawHopCount != null && rawHopCount > 0 && rawHopCount !== 255)
      ? rawHopCount : Math.floor(pathHex.length / hashChars);
    if (!hopCount) return;
    const hashList = [];
    for (let i = 0; i < hopCount; i++) {
      const hashHex = pathHex.slice(i * hashChars, (i + 1) * hashChars);
      if (!hashHex || hashHex.replace(/0/g, '') === '') break;
      hashList.push(hashHex);
    }
    const build = (hashes) => {
      const points = [[radioLat, radioLon]];
      const hops = [];
      let unresolved = 0;
      hashes.forEach((hashHex, i) => {
        const hopResolution = _mcPickPathHopResolution(hashHex, radioId, radioLat, radioLon, null, null, i, hashes.length);
        const hopContact = hopResolution?.contact || null;
        if (!hopContact) { unresolved++; return; }
        const hLat = hopContact.latitude ?? hopContact.lat;
        const hLon = hopContact.longitude ?? hopContact.lon;
        if (hLat == null || hLon == null) { unresolved++; return; }
        points.push([hLat, hLon]);
        hops.push(_mcPathHopMeta(hashHex, hopContact, points.length - 1, hopResolution));
        if (hopResolution?.ambiguous) unresolved += 0.5;
      });
      if (points.length < 2) return null;
      const progressPenalty = _mcRelayProgressPenalty(points);
      const score = (hops.length * 100) - (unresolved * 20) - (Math.abs(hopCount - hops.length) * 12) - (progressPenalty * 10);
      return { score, result: { points, partial: true, endpointUnknown: true, hops, unresolved, inferredPathHashSize: hashSize, inferredHopLen: hopCount } };
    };
    const forward = build(hashList);
    const reverse = hashList.length > 1 ? build([...hashList].reverse()) : null;
    [forward, reverse].filter(Boolean).forEach(cand => {
      if (!best || cand.score > best.score) best = cand;
    });
  });
  return best?.result || null;
}

// Single persistent trace showing the full resolved hop chain (sender -> known
// relays -> HD) for deliberately tapping a message. Matches the full OM app's
// "message" trace style (violet, dashed) with a dark halo for contrast, drawn
// per segment; unresolved hops are simply omitted (path shown as partial).
// Stays until toggled off or replaced by tapping another message.
function showMsgTrace(pathResult, msgIdx) {
  if (!pathResult?.points || pathResult.points.length < 2 || !map) return;
  if (S.activeTraceLine) map.removeLayer(S.activeTraceLine);
  const points = pathResult.points;
  const group = L.layerGroup();
  for (let i = 0; i < points.length - 1; i++) {
    const seg = [points[i], points[i + 1]];
    L.polyline(seg, { color: '#111', weight: 6, opacity: 0.4, lineCap: 'round', lineJoin: 'round', interactive: false }).addTo(group);
    L.polyline(seg, { color: '#8b5cf6', weight: 3, opacity: 1.0, dashArray: '2,6', lineCap: 'round', lineJoin: 'round', interactive: false }).addTo(group);
  }
  points.forEach(p => {
    L.circleMarker(p, { radius: 6, color: '#000', fillColor: '#8b5cf6', fillOpacity: 1.0, weight: 2, interactive: false }).addTo(group);
  });
  group.addTo(map);
  S.activeTraceLine = group;
  S.activeTraceMsgIdx = msgIdx;
  if (pathResult.partial) toast('Partial path — some hops unresolved', 3000);
}

function addMsgToFeed(type, radioId, key, from, text, ts, label, extra) {
  S.mapMsgFeed.unshift({ type, radioId, key, from, text, ts, label, ...(extra||{}) });
  if (S.mapMsgFeed.length > 60) S.mapMsgFeed.pop();
  if (S.activeTab === 'map') renderMapActivity();
}

function addActivity(kind, title, sub) {
  S.activity.unshift({ kind, title, sub, ts: Math.floor(Date.now() / 1000) });
  if (S.activity.length > 80) S.activity.pop();
}

function openActivitySheet() {
  renderActivity();
  openSheet('activity-sheet');
}


function onNodeStatus(d) {
  if (!d.radio_id) return;
  const node = {
    id: d.radio_id, name: d.long_name || d.short_name || d.radio_id,
    short: d.short_name, lat: d.lat, lon: d.lon,
    snr: d.snr, hops: d.hops_away, last_heard: d.last_heard,
    online: d.status === 'online' || d.connected,
    type: 'mt',
  };
  S.nodes[d.radio_id] = { ...(S.nodes[d.radio_id] || {}), ...node };
  if (!S.nodes[d.radio_id]?._seenActivity) {
    addActivity('📡', node.name || d.radio_id, 'MT node heard');
    S.nodes[d.radio_id]._seenActivity = true;
  }
  placeNodeMarker(d.radio_id, node.lat, node.lon, 'mt', node.name, node.online, node);
  renderNodes(); updateMapStatus();
}

function onMcNode(d) {
  const cid = d.contact_id || d.id || (d.full_key ? String(d.full_key).slice(0, 12) : '');
  if (!d.radio_id || !cid) return;
  if (!S.mcNodes[d.radio_id]) S.mcNodes[d.radio_id] = {};
  const c = {
    ...(S.mcNodes[d.radio_id][cid] || {}),
    ...d,
    contact_id: cid,
    id: cid,
    name: d.name || d.long_name || cid,
    lat: d.lat ?? d.latitude,
    lon: d.lon ?? d.longitude,
    last_heard: d.last_heard ?? d.last_seen_ts,
    contact_type: d.contact_type ?? d.type,
    type: 'mc',
  };
  S.mcNodes[d.radio_id][cid] = c;
  if (!c._seenActivity) {
    addActivity('◇', c.name || cid, 'MC contact heard');
    c._seenActivity = true;
  }
  placeNodeMarker(cid, c.lat, c.lon, 'mc', c.name || cid, true, c);
  renderNodes();
}

function onMcStatus(d) {
  const r = S.mcRadios.find(r => r.id === d.radio_id);
  if (r) { r.connected = d.connected; updateMapStatus(); renderSettings(); }
}

function _hopLabel(pathLen) {
  if (pathLen === 255) return 'route';
  if (pathLen === 0) return 'direct';
  if (pathLen > 0) return pathLen + ' hop' + (pathLen !== 1 ? 's' : '');
  return 'flood';
}

function _mcNameFor(radioId, fromId) {
  if (!fromId || fromId === '?') return fromId || '?';
  var cs = S.mcNodes[radioId] || {};
  if (cs[fromId]) return cs[fromId].name || cs[fromId].long_name || fromId.slice(0, 8);
  var found = null;
  var keys = Object.keys(cs);
  for (var i = 0; i < keys.length; i++) {
    var k = cs[keys[i]].contact_id || cs[keys[i]].id || '';
    if (k && (k.startsWith(fromId) || fromId.startsWith(k))) { found = cs[keys[i]]; break; }
  }
  return found ? (found.name || found.long_name || fromId.slice(0, 8)) : fromId.slice(0, 8);
}

function onMtMessage(d) {
  const idx = d.channel ?? 0;
  if (!S.mtMsgs[idx]) S.mtMsgs[idx] = [];
  S.mtMsgs[idx].push(d);
  const _mtChName = (S.mtChannels.find(c => c.index === idx)?.name) || ('ch' + idx);
  if (S.activeTab === 'chat' && S.chatCtx?.type === 'mt_chan' && S.chatCtx.key === idx) {
    renderMessages();
  } else {
    addActivity('💬', d.from_name || d.from_id || 'MT message', d.text || d.message || '');
    bumpUnread();
    if (!d.from_me && !d.sent) {
      bumpChUnread('mt_chan', null, idx);
      showMsgToast(`${d.from_name || d.from_id || '?'} · #${_mtChName}`, d.text || d.message || '',
        () => { switchTab('chat'); selectChat('mt_chan', null, idx); });
    }
  }
  const _mtNode = S.nodes[d.from_id];
  if (_mtNode?.lat && !d.from_me) drawMsgPath(_mtNode.lat, _mtNode.lon, '#facc15');
  addMsgToFeed('mt_chan', null, idx, d.from_name || d.from_id || '?', d.text || d.message || '', d.ts || Math.floor(Date.now()/1000), 'MT #' + _mtChName, { fromId: d.from_id });
}

function onMcMessage(d) {
  const rid = d.radio_id;
  if (!S.mcMsgs[rid]) S.mcMsgs[rid] = { chan: {}, dm: {} };
  const dmId = d.contact_id || (d.subtype === 'dm' ? (d.sent ? d.to_id : d.from_id) : '');
  const idx = d.channel ?? d.channel_index ?? 0;
  if (dmId) {
    if (!S.mcMsgs[rid].dm[dmId]) S.mcMsgs[rid].dm[dmId] = [];
    S.mcMsgs[rid].dm[dmId].push(d);
  } else {
    if (!S.mcMsgs[rid].chan[idx]) S.mcMsgs[rid].chan[idx] = [];
    S.mcMsgs[rid].chan[idx].push(d);
  }
  const active = S.activeTab === 'chat' && S.chatCtx?.radioId === rid &&
    (dmId ? (S.chatCtx?.type === 'mc_dm' && String(S.chatCtx?.key) === String(dmId))
          : (S.chatCtx?.type === 'mc_chan' && Number(S.chatCtx?.key) === Number(idx)));
  if (active) renderMessages();
  else {
    addActivity('💬', d.from_name || d.from_id || 'MC message', d.text || d.message || '');
    bumpUnread();
    if (!d.sent) {
      const _mcFrom0 = d.from_name || _mcNameFor(rid, d.from_id);
      if (dmId) {
        const dmKey = `mc_dm:${rid}:${dmId}`;
        if (!S.activeDms.has(dmKey)) {
          S.activeDms.add(dmKey);
          renderActiveDms();
          if (S.activeTab === 'chat') renderChatSelector();
        }
        bumpChUnread('mc_dm', rid, dmId);
        showMsgToast(`${_mcFrom0} (DM)`, d.text || '', () => { switchTab('chat'); selectChat('mc_dm', rid, dmId); });
      } else {
        const _mcChName0 = (S.mcChannels[rid] || []).find(c => c.index === idx)?.name || ('ch' + idx);
        bumpChUnread('mc_chan', rid, idx);
        showMsgToast(`${_mcFrom0} · #${_mcChName0}`, d.text || '', () => { switchTab('chat'); selectChat('mc_chan', rid, idx); });
      }
    }
  }
  if (!d.sent) {
    const _allMcC = S.mcNodes[rid] || {};
    const _mcContact = _allMcC[d.from_id] || Object.values(_allMcC).find(function(c) {
      var k = c.contact_id || c.id || '';
      return k && d.from_id && (k.startsWith(d.from_id) || d.from_id.startsWith(k));
    });
    if (_mcContact && _mcContact.lat) drawMsgPath(_mcContact.lat, _mcContact.lon, '#22d3ee');
  }
  const _mcChanName = (S.mcChannels[rid] || []).find(c => c.index === (d.channel ?? 0))?.name || ('ch' + (d.channel ?? 0));
  const _mcFeedType = dmId ? 'mc_dm' : 'mc_chan';
  const _mcFeedKey = dmId || (d.channel ?? 0);
  const _mcFrom = d.from_name || _mcNameFor(rid, d.from_id);
  addMsgToFeed(_mcFeedType, rid, _mcFeedKey, _mcFrom, d.text || '', d.ts || Math.floor(Date.now()/1000), 'MC #' + _mcChanName, { pathLen: d.path_len, pathHashSize: d.path_hash_size, pathHashMode: d.path_hash_mode, path: d.path, routeType: d.route_type, rssi: d.rx_rssi, snr: d.rx_snr, fromId: dmId || d.from_id });
}

function onMcTrace(d) {
  document.getElementById('ns-result').textContent =
    `Hops: ${(d.path||[]).join(' → ')}\nRSSI: ${d.rssi ?? '?'}  SNR: ${d.snr ?? '?'}`;
}

function onSenseResp(d) {
  // Draw sense lines on map for a moment
  if (!d.node || !d.node.lat || !d.node.lon) return;
  const line = L.polyline([[d.node.lat, d.node.lon], [d.node.lat+0.001, d.node.lon+0.001]], {
    color: '#4ade80', weight: 1, opacity: 0.5, dashArray: '4 4',
  }).addTo(map);
  setTimeout(() => map.removeLayer(line), 8000);
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-panel').forEach(el => el.hidden = true);
  document.querySelectorAll('.nav-btn').forEach(el =>
    el.classList.toggle('active', el.dataset.tab === tab));
  document.getElementById('tab-' + tab).hidden = false;
  S.activeTab = tab;
  if (tab === 'map') setTimeout(() => { refreshMapLayout(); _applyMapMode(S.senseOn ? 'sense' : 'map'); }, 60);
  if (tab === 'nodes') renderNodes();
  if (tab === 'chat') { renderChatSidebar(); S.mcRadios.forEach(r => loadMcMessages(r.id)); }
  if (tab === 'log') renderLog();
  if (tab === 'settings') renderSettings();
  if (tab === 'chat') clearUnread();
}

// ── Nodes tab ────────────────────────────────────────────────────────────────
function setNFilter(f, el) {
  S.nFilter = f;
  document.querySelectorAll('.fpill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  renderNodes();
}

function renderNodes() {
  const q = (document.getElementById('nodes-search')?.value || '').toLowerCase();
  const el = document.getElementById('nodes-list');
  const now = Date.now() / 1000;
  const items = [];

  const includeNode = n => {
    if (S.nFilter === 'fav') return S.favNodes.has(n.id || n.contact_id);
    if (S.nFilter === 'fresh') return Number(n.last_heard || n.last_seen_ts || 0) > now - 3600;
    if (S.nFilter === 'pos') return !!(n.lat && n.lon);
    return true;
  };

  if (S.nFilter !== 'mc') {
    Object.values(S.nodes).forEach(n => {
      if (q && !n.name?.toLowerCase().includes(q)) return;
      if (!includeNode(n)) return;
      items.push({ id: n.id, type: 'mt', name: n.name || n.id, n, radioId: null, isFav: S.favNodes.has(n.id) });
    });
  }
  if (S.nFilter !== 'mt') {
    Object.values(S.mcNodes).forEach(byRadio => {
      Object.values(byRadio).forEach(c => {
        if (q && !c.name?.toLowerCase().includes(q)) return;
        const cid = c.contact_id || c.id;
        if (!includeNode({ ...c, id: cid })) return;
        items.push({ id: cid, type: 'mc', name: c.name || c.long_name || cid, n: c, radioId: c.radio_id, isFav: S.favNodes.has(cid) });
      });
    });
  }

  items.sort((a, b) => {
    if (a.isFav !== b.isFav) return a.isFav ? -1 : 1;
    return Number(b.n.last_heard || b.n.last_seen_ts || 0) - Number(a.n.last_heard || a.n.last_seen_ts || 0);
  });

  renderNodesSummary();
  el.innerHTML = items.length
    ? items.map(it => nodeRow(it.id, it.type, it.name, it.n, it.radioId, it.isFav)).join('')
    : `<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">No nodes</div>`;
}

function renderNodesSummary() {
  const el = document.getElementById('nodes-summary');
  if (!el) return;
  const mt = Object.keys(S.nodes).length;
  const mc = Object.values(S.mcNodes).reduce((sum, byRadio) => sum + Object.keys(byRadio).length, 0);
  const pos = [
    ...Object.values(S.nodes),
    ...Object.values(S.mcNodes).flatMap(Object.values),
  ].filter(n => n.lat && n.lon).length;
  el.innerHTML = `<span class="mini-chip">MT ${mt}</span><span class="mini-chip">MC ${mc}</span><span class="mini-chip">GPS ${pos}</span>`;
}

function nodeRow(id, type, name, n, radioId, isFav) {
  const lastheard = n.last_heard ? relTime(n.last_heard) : '—';
  const meta = type === 'mt'
    ? `${lastheard}${n.hops != null ? ' · ' + n.hops + ' hops' : ''}`
    : `${lastheard}${n.snr != null ? ' · SNR ' + n.snr : ''}`;
  const star = isFav ? `<span style="color:var(--accent);font-size:12px;flex-shrink:0;margin-left:auto;padding-left:6px">★</span>` : '';
  return `<div class="node-row" onclick="openNodeDetail('${esc(id)}','${type}','${esc(radioId||'')}')">
    <span class="ntype ${type}"></span>
    <span class="node-name">${esc(name)}</span>
    <div class="node-meta">${meta}</div>
    ${star}
  </div>`;
}

// ── Node detail sheet ─────────────────────────────────────────────────────────
function openNodeDetail(id, type, radioId) {
  S.selectedNode = { id, type, radioId: radioId || null };
  const n = type === 'mt' ? S.nodes[id] : (S.mcNodes[radioId]?.[id] || {});
  const name = n.name || n.long_name || id;

  document.getElementById('ns-name').textContent = name;

  const parts = [];
  if (type === 'mt') {
    if (n.snr != null) parts.push(`SNR: ${n.snr} dB`);
    if (n.hops != null) parts.push(`Hops: ${n.hops}`);
    if (n.lat) parts.push(`Pos: ${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}`);
    parts.push(`Type: MT`);
  } else {
    if (n.snr != null) parts.push(`SNR: ${n.snr} dB`);
    if (n.lat) parts.push(`Pos: ${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}`);
    parts.push(`Type: MC · Radio: ${radioId}`);
  }
  if (n.last_heard) parts.push(`Last heard: ${relTime(n.last_heard)}`);
  document.getElementById('ns-meta').innerHTML = parts.join('<br>');
  document.getElementById('ns-result').textContent = '';
  document.getElementById('ns-dm').hidden = false;
  const _nsId = id;
  const favBtn = document.getElementById('ns-fav');
  if (favBtn) favBtn.textContent = S.favNodes.has(_nsId) ? '★ Fav' : '☆ Fav';
  openSheet('node-sheet');
}

function nodeAction(action) {
  const { id, type, radioId } = S.selectedNode || {};
  if (!id) return;
  if (action === 'fav') {
    if (S.favNodes.has(id)) S.favNodes.delete(id);
    else S.favNodes.add(id);
    localStorage.setItem('om_favs', JSON.stringify([...S.favNodes]));
    const favBtn = document.getElementById('ns-fav');
    if (favBtn) favBtn.textContent = S.favNodes.has(id) ? '★ Fav' : '☆ Fav';
    renderNodes();
    return;
  } else if (action === 'dm') {
    const dmType = type === 'mc' ? 'mc_dm' : 'mt_dm';
    const dmRadioId = type === 'mc' ? radioId : null;
    S.chatCtx = { type: dmType, radioId: dmRadioId, key: id };
    const dmKey = `${dmType}:${dmRadioId || ''}:${id}`;
    S.activeDms.add(dmKey);
    closeAllSheets(); switchTab('chat'); renderChatSidebar(); renderMessages();
    renderActiveDms();
  } else if (action === 'ping') {
    document.getElementById('ns-result').textContent = 'Pinging…';
    if (type === 'mt') {
      fetch(`/api/node/${encodeURIComponent(id)}/traceroute`, { method: 'POST',
        headers: {'Content-Type':'application/json'}, body: JSON.stringify({ ping_only: true }) })
        .then(r => r.json()).then(d => {
          document.getElementById('ns-result').textContent = d.ok ? 'Ping sent' : 'Ping failed';
          if (d.ok) addActivity('✓', 'MT ping sent', id);
        }).catch(() => { document.getElementById('ns-result').textContent = 'Error'; });
    } else if (radioId) {
      fetch(`/api/mc/${encodeURIComponent(radioId)}/statusreq/${encodeURIComponent(id)}`, { method: 'POST' })
        .then(() => {
          document.getElementById('ns-result').textContent = 'Status req sent';
          addActivity('✓', 'MC ping sent', id);
        })
        .catch(() => { document.getElementById('ns-result').textContent = 'Error'; });
    }
  } else if (action === 'trace') {
    document.getElementById('ns-result').textContent = 'Tracerouting…';
    const traceUrl = type === 'mc' && radioId
      ? `/api/mc/${encodeURIComponent(radioId)}/trace`
      : `/api/node/${encodeURIComponent(id)}/traceroute`;
    const traceBody = type === 'mc' && radioId
      ? {}
      : {};
    fetch(traceUrl, { method: 'POST',
      headers: {'Content-Type':'application/json'}, body: JSON.stringify(traceBody) })
      .then(r => r.json())
      .then(d => {
        document.getElementById('ns-result').textContent = d.ok ? 'Traceroute sent — waiting for result' : 'Failed';
        if (d.ok) addActivity('〰', 'Trace sent', id);
      })
      .catch(() => { document.getElementById('ns-result').textContent = 'Error'; });
  } else if (action === 'map') {
    const n = type === 'mt' ? S.nodes[id] : (S.mcNodes[radioId]?.[id] || {});
    if (n?.lat && n?.lon) {
      closeAllSheets(); switchTab('map');
      setTimeout(() => map.setView([n.lat, n.lon], 14), 80);
    } else { toast('No position for this node'); }
  }
}

// ── Chat tab ─────────────────────────────────────────────────────────────────
function renderChatSidebar() { renderChatSelector(); } // public alias — keep for external callers

function renderChatSelector() {
  const body = document.getElementById('chat-select-body');
  const pillsEl = document.getElementById('chat-ch-pills');
  let pillsHtml = '';
  let sheetHtml = '';

  const f = S.chatFilter;
  ['mt','mc'].forEach(n => { const el = document.getElementById('cpill-' + n); if (el) el.classList.toggle('active', f === n); });

  // MT channels → pills
  if (f === 'all' || f === 'mt') {
    S.mtChannels.forEach(ch => {
      const active = isActiveChat('mt_chan', null, ch.index);
      const unread = S.chUnread[_chKey('mt_chan', null, ch.index)];
      const badge = unread ? `<span class="cpill-badge">${unread > 9 ? '9+' : unread}</span>` : '';
      pillsHtml += `<button class="cpill${active?' active':''}" onclick="selectChat('mt_chan',null,${ch.index})">#${esc(ch.name || 'ch' + ch.index)}${badge}</button>`;
    });
  }

  // MC channels → pills (skip unnamed slots unless they have live messages)
  if (f === 'all' || f === 'mc') S.mcRadios.forEach(r => {
    (S.mcChannels[r.id] || []).forEach(ch => {
      if (!ch.name && !(S.mcMsgs[r.id]?.chan[ch.index]?.length)) return;
      const active = isActiveChat('mc_chan', r.id, ch.index);
      const unread = S.chUnread[_chKey('mc_chan', r.id, ch.index)];
      const badge = unread ? `<span class="cpill-badge">${unread > 9 ? '9+' : unread}</span>` : '';
      pillsHtml += `<button class="cpill${active?' active':''}" onclick="selectChat('mc_chan','${esc(r.id)}',${ch.index})">#${esc(ch.name || 'ch' + ch.index)}${badge}</button>`;
    });
  });

  // Active DMs → pills with X (shown regardless of filter)
  S.activeDms.forEach(key => {
    const [type, radioId, nodeId] = key.split(':');
    let name = nodeId;
    if (type === 'mt_dm') name = S.nodes[nodeId]?.name || nodeId;
    else if (type === 'mc_dm') name = S.mcNodes[radioId]?.[nodeId]?.name || nodeId;
    const active = S.chatCtx?.type === type && S.chatCtx?.key == nodeId && S.chatCtx?.radioId == (radioId || null);
    const unread = S.chUnread[_chKey(type, radioId, nodeId)];
    const badge = unread ? `<span class="cpill-badge">${unread > 9 ? '9+' : unread}</span>` : '';
    pillsHtml += `<button class="cpill${active?' active':''}" style="display:inline-flex;align-items:center;gap:4px" onclick="selectChat('${type}','${radioId}','${nodeId}')">@${esc(name)}${badge}<span onclick="event.stopPropagation();removeDm('${key}')" style="opacity:0.6;font-size:9px;margin-left:1px">✕</span></button>`;
  });

  if (!pillsHtml) pillsHtml = `<span style="font-size:11px;color:var(--muted);padding:0 4px">No channels</span>`;
  if (pillsEl) pillsEl.innerHTML = pillsHtml;

  // Sheet content: DMs selectable from Nodes tab appear here too
  if (sheetHtml || S.activeDms.size) {
    if (body) body.innerHTML = sheetHtml || `<div style="padding:16px 14px;font-size:13px;color:var(--muted)">Start a DM from the Nodes tab.</div>`;
  } else if (body) {
    body.innerHTML = `<div style="padding:16px 14px;font-size:13px;color:var(--muted)">Start a DM from the Nodes tab.</div>`;
  }
}

function renderActiveDms() {
  const bar = document.getElementById('active-dms-bar');
  const list = document.getElementById('active-dms-list');
  if (!bar || !list) return;
  if (!S.activeDms.size) { bar.hidden = true; return; }
  bar.hidden = false;
  list.innerHTML = [...S.activeDms].map(key => {
    const [type, radioId, nodeId] = key.split(':');
    let name = nodeId;
    if (type === 'mt_dm') name = S.nodes[nodeId]?.name || nodeId;
    else if (type === 'mc_dm') name = S.mcNodes[radioId]?.[nodeId]?.name || nodeId;
    return `<button style="display:inline-flex;align-items:center;gap:4px;background:var(--bg3);border:1px solid var(--border);border-radius:99px;padding:3px 8px;font-size:11px;color:var(--text);cursor:pointer;min-height:28px" onclick="selectChat('${type}','${radioId}','${nodeId}');switchTab('chat')">@${esc(name)}<span onclick="event.stopPropagation();removeDm('${key}')" style="color:var(--muted);font-size:11px;margin-left:2px;line-height:1">✕</span></button>`;
  }).join('');
}

function removeDm(key) {
  S.activeDms.delete(key);
  renderActiveDms();
  renderChatSelector();
}

function sendAdvert(flood) {
  const radioId = (S.chatCtx?.type?.startsWith('mc') ? S.chatCtx.radioId : null) || S.mcRadios[0]?.id;
  if (!radioId) { toast('No MC radio connected'); return; }
  fetch(`/api/mc/${encodeURIComponent(radioId)}/advert`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ flood })
  }).then(r => r.json()).then(d => {
    if (d.ok) addActivity('📡', flood ? 'Flood advert sent' : 'Local advert sent', '');
    else toast('Advert failed: ' + (d.error || '?'));
  }).catch(() => toast('Advert error'));
}

function renderMapActivity() {
  const el = document.getElementById('map-act-body');
  if (!el) return;
  const filtered = S.mapFilter === 'all' ? S.mapMsgFeed
    : S.mapMsgFeed.filter(m => S.mapFilter === 'mc' ? m.type.startsWith('mc') : !m.type.startsWith('mc'));
  if (!filtered.length) { el.innerHTML = '<div style="padding:12px;font-size:11px;color:var(--muted)">No messages yet</div>'; return; }
  el.innerHTML = filtered.map((m) => {
    const i = S.mapMsgFeed.indexOf(m);
    const isMc = m.type.startsWith('mc');
    return `<div class="mact-item${S.activeTraceMsgIdx === i ? ' active' : ''}" onclick="tapMsgFeed(${i})">
      <span class="mact-badge ${isMc?'mc':'mt'}">${isMc?'MC':'MT'}</span>
      <div class="mact-body">
        <div class="mact-from">${m.from && m.from !== '?' ? esc(m.from) + ' ' : ''}<span style="font-size:9px;color:var(--muted);font-weight:400">${esc(m.label)}</span></div>
        <div class="mact-text">${esc(m.text)}</div>
        <div class="mact-meta">${relTime(m.ts)}${m.pathLen != null ? ' · ' + _hopLabel(m.pathLen) + (m.rssi != null ? ' · ' + m.rssi + 'dBm' : '') : ''}</div>
      </div>
    </div>`;
  }).join('');
}

function tapMsgFeed(i) {
  const m = S.mapMsgFeed[i];
  if (!m) return;
  // Toggle off if tapping the same message again
  if (S.activeTraceMsgIdx === i) {
    if (S.activeTraceLine) map.removeLayer(S.activeTraceLine);
    S.activeTraceLine = null;
    S.activeTraceMsgIdx = null;
  } else if (m.type.startsWith('mt')) {
    // MT: direct line only (no hop-hash path decoding exists for Meshtastic here)
    const node = (m.fromId && S.nodes[m.fromId])
      || Object.values(S.nodes).find(n => n.name === m.from || n.id === m.from);
    const [radioLat, radioLon] = _mcTraceRadioPos();
    if (node?.lat && radioLat != null) {
      showMsgTrace({ points: [[node.lat, node.lon], [radioLat, radioLon]] }, i);
      map.panTo([node.lat, node.lon]);
    } else toast('No known position for sender', 3000);
  } else {
    // MC: resolve the sender contact, then decode the full hop-by-hop path
    const allC = m.radioId ? (S.mcNodes[m.radioId] || {}) : {};
    let c = null;
    if (m.fromId && m.fromId !== '?') {
      c = allC[m.fromId] || Object.values(allC).find(x => {
        const k = x.contact_id || x.id || '';
        return k && (k.startsWith(m.fromId) || m.fromId.startsWith(k));
      });
    }
    // Channel broadcasts often carry no real sender id ("?") — the sender rides
    // inside the text itself ("Name: message"), so fall back to matching that.
    if (!c) {
      const parsedName = _parseChannelSenderName(m.text).toLowerCase();
      if (parsedName) {
        c = Object.values(allC).find(x => {
          const cands = [x.name, x.long_name, x.short_name].filter(Boolean).map(s => String(s).toLowerCase());
          return cands.some(cand => cand === parsedName || cand.startsWith(parsedName) || parsedName.startsWith(cand));
        });
      }
    }
    if (!c) c = Object.values(allC).find(x => x.name === m.from || x.id === m.from || x.contact_id === m.from);

    const [radioLat, radioLon] = _mcTraceRadioPos();
    const endLat = c?.lat ?? null, endLon = c?.lon ?? null;

    let pathResult = null;
    if (endLat != null && endLon != null) pathResult = decodeMcEventPath(m, endLat, endLon, radioLat, radioLon, m.radioId);
    if (!pathResult) pathResult = decodeMcRelayPath(m, radioLat, radioLon, m.radioId);
    if (!pathResult && c) pathResult = decodeMcPath(c, radioLat, radioLon, m.radioId);
    if (!pathResult && endLat != null && endLon != null && radioLat != null) {
      pathResult = { points: [[radioLat, radioLon], [endLat, endLon]], flood: true };
    }
    if (pathResult?.points) pathResult = _mcReversePathResult(pathResult);

    if (pathResult?.points?.length >= 2) { showMsgTrace(pathResult, i); map.panTo(pathResult.points[0]); }
    else toast('No known position for sender', 3000);
  }
  renderMapActivity();

  // In Sense mode, tapping an entry just shows the trace on the map — stay put.
  if (S.senseOn) return;

  // Normal Map mode: jump to Chat tab and select the message's channel
  if (m.type && m.key != null) {
    switchTab('chat');
    selectChat(m.type, m.radioId || null, m.key);
    // Scroll to the message by timestamp after render
    setTimeout(() => {
      const msgs = document.getElementById('chat-messages');
      if (!msgs) return;
      const target = msgs.querySelector(`[data-ts="${m.ts}"]`);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      else msgs.scrollTop = msgs.scrollHeight;
    }, 80);
  }
}

function setMapFilter(f) {
  S.mapFilter = f;
  document.querySelectorAll('.mfpill').forEach(b => b.classList.remove('active'));
  const active = document.getElementById('mf-' + f);
  if (active) active.classList.add('active');
  // Show/hide markers
  Object.entries(S.markers).forEach(([id, marker]) => {
    const t = S.markerTypes[id];
    if (!t) return;
    if (f === 'all' || f === t) {
      if (!map.hasLayer(marker)) marker.addTo(map);
    } else {
      if (map.hasLayer(marker)) map.removeLayer(marker);
    }
  });
  updateMapStatus();
  renderMapActivity();
}

function mapModeSet(mode) {
  if (mode === 'sense' && !S.senseOn) toggleSense();
  else if (mode === 'map' && S.senseOn) toggleSense();
  else _applyMapMode(mode);
}

function _applyMapMode(mode) {
  const panel = document.getElementById('map-act-panel');
  if (panel) {
    panel.hidden = false;
    const layers = document.getElementById('btn-layers');
    const follow = document.getElementById('btn-follow');
    if (layers) layers.style.right = '';
    if (follow) follow.style.right = '';
    const h4 = panel.querySelector('h4');
    if (h4) h4.textContent = mode === 'sense' ? 'SENSE — PACKETS' : 'RECENT MESSAGES';
    renderMapActivity();
    refreshMapLayout();
  }
  document.querySelectorAll('[data-mode]').forEach(el => {
    el.classList.toggle('active', el.dataset.mode === mode);
  });
}

function setChatFilter(f) {
  S.chatFilter = f;
  renderChatSelector();
}

function isActiveChat(type, radioId, key) {
  return S.chatCtx?.type === type && S.chatCtx?.radioId === radioId &&
    String(S.chatCtx?.key) === String(key);
}

function selectChat(type, radioId, key) {
  S.chatCtx = { type, radioId: radioId || null, key };
  closeSheet('chat-select-sheet');
  if (type.startsWith('mc') && radioId) loadMcMessages(radioId);
  renderMessages();
  clearChUnread(type, radioId || null, key);

  // Track active DMs
  if (type === 'mt_dm' || type === 'mc_dm') {
    const dmKey = `${type}:${radioId || ''}:${key}`;
    S.activeDms.add(dmKey);
    renderActiveDms();
  }

  const nameEl = document.getElementById('chat-current-name');
  if (nameEl) { nameEl.textContent = ''; }
  document.getElementById('chat-input-bar').hidden = false;
  document.getElementById('chat-empty').hidden = true;
  const _isMc = type.startsWith('mc');
  ['adv-btn-local','adv-btn-flood','adv-sep'].forEach(id => {
    const el = document.getElementById(id); if (el) el.hidden = !_isMc;
  });
  renderChatSelector(); // refresh pill active states
}

function renderMessages() {
  const el = document.getElementById('chat-messages');
  const ctx = S.chatCtx;
  if (!ctx) { el.innerHTML = ''; S.chatMsgsView = []; return; }
  let msgs = [];
  if (ctx.type === 'mt_chan') msgs = S.mtMsgs[ctx.key] || [];
  else if (ctx.type === 'mt_dm') msgs = S.mtDmMsgs[ctx.key] || [];
  else if (ctx.type === 'mc_chan') msgs = S.mcMsgs[ctx.radioId]?.chan[ctx.key] || [];
  else if (ctx.type === 'mc_dm') msgs = S.mcMsgs[ctx.radioId]?.dm[ctx.key] || [];
  S.chatMsgsView = msgs;

  el.innerHTML = msgs.map((m, i) => {
    const out = m.from_me || m.is_mine || m.sent;
    const isMc = ctx.type.startsWith('mc');
    const senderName = isMc ? (_mcNameFor(ctx.radioId, m.from_id) || m.from_name || '') : (m.sender || m.from_name || m.from_id || '');
    const sender = out ? 'Me' : esc(senderName || '?');
    const rawTs = m.timestamp || m.ts;
    const ts = rawTs ? new Date(rawTs * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
    const pathParts = isMc && !out && m.path_len != null
      ? [_hopLabel(m.path_len)].concat(m.path_hash_size ? [m.path_hash_size + 'B'] : []).concat(m.rx_rssi != null ? [m.rx_rssi + 'dBm'] : [])
      : [];
    const pathInfo = pathParts.join(' · ');
    return `<div class="msg-row ${out ? 'out' : 'in'}" data-ts="${rawTs || ''}" onclick="openMsgActions(${i})">
      <div class="msg-bubble">${esc(m.text || m.message || '')}</div>
      <div class="msg-meta">${out ? '' : sender + ' · '}${ts}${pathInfo ? ' · ' + esc(pathInfo) : ''}</div>
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
  const sendBtn = document.getElementById('chat-send');
  if (sendBtn) {
    sendBtn.disabled = !!S.sending;
    sendBtn.textContent = S.sending ? '...' : 'Send';
  }
}

// MeshCore channel broadcasts don't carry verified sender identity — the sender
// commonly rides inside the text itself as "Name: message". Used as a Reply/Log
// fallback only (never for DM, which needs a real contact id to target).
function _parseChannelSenderName(text) {
  const m = /^([^:]{1,32}):\s?/.exec(text || '');
  return m ? m[1].trim() : '';
}

function _msgDisplayName(ctx, m) {
  const isMc = ctx.type.startsWith('mc');
  let name = isMc ? (_mcNameFor(ctx.radioId, m.from_id) || m.from_name || '') : (m.sender || m.from_name || m.from_id || '');
  if ((!name || name === '?') && ctx.type === 'mc_chan') name = _parseChannelSenderName(m.text);
  return name;
}

function openMsgActions(i) {
  const ctx = S.chatCtx;
  const m = S.chatMsgsView?.[i];
  if (!ctx || !m) return;
  const out = m.from_me || m.is_mine || m.sent;
  const name = out ? '' : _msgDisplayName(ctx, m);
  const isDmView = ctx.type === 'mt_dm' || ctx.type === 'mc_dm';
  const canDm = !out && !isDmView && !!m.from_id;
  const canReply = !out && !!name;

  document.getElementById('msg-actions-preview').textContent =
    (out ? 'Me' : (name || '?')) + ': ' + (m.text || m.message || '');

  let btns = '';
  if (canDm) btns += `<button class="btn btn-sm" onclick="closeSheet('msg-actions-sheet');msgStartDm(${i})">DM ${esc(name)}</button>`;
  if (canReply) btns += `<button class="btn btn-sm" onclick="closeSheet('msg-actions-sheet');msgReply(${i})">↩ Reply</button>`;
  btns += `<button class="btn btn-sm" onclick="closeSheet('msg-actions-sheet');msgToLog(${i})">Log this message</button>`;
  document.getElementById('msg-actions-btns').innerHTML = btns;
  openSheet('msg-actions-sheet');
}

function msgReply(i) {
  const ctx = S.chatCtx;
  const m = S.chatMsgsView?.[i];
  if (!ctx || !m) return;
  const isMc = ctx.type.startsWith('mc');
  const name = _msgDisplayName(ctx, m);
  if (!name) return;
  const input = document.getElementById('chat-input');
  if (!input) return;
  input.value = isMc ? `@[${String(name).replace(/[[\]\r\n]/g, '').trim()}] ` : `@${name}: `;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 80) + 'px';
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function msgStartDm(i) {
  const ctx = S.chatCtx;
  const m = S.chatMsgsView?.[i];
  if (!ctx || !m || !m.from_id) return;
  const isMc = ctx.type.startsWith('mc');
  switchTab('chat');
  selectChat(isMc ? 'mc_dm' : 'mt_dm', isMc ? ctx.radioId : null, m.from_id);
}

function msgToLog(i) {
  const ctx = S.chatCtx;
  const m = S.chatMsgsView?.[i];
  if (!ctx || !m) return;
  const isMc = ctx.type.startsWith('mc');
  const out = m.from_me || m.is_mine || m.sent;
  const senderName = out ? 'Me' : (_msgDisplayName(ctx, m) || '?');
  const network = isMc
    ? (ctx.type === 'mc_dm' ? 'MeshCore DM' : `MeshCore #${(S.mcChannels[ctx.radioId] || []).find(c => c.index === ctx.key)?.name || ('ch' + ctx.key)}`)
    : (ctx.type === 'mt_dm' ? 'Meshtastic DM' : `Meshtastic CH${ctx.key}`);
  const rawTs = m.timestamp || m.ts;
  S.pendingLogContext = {
    sender: senderName,
    to: out ? network : 'Me',
    network,
    result: out ? 'sent' : 'received',
    time: rawTs ? new Date(rawTs * 1000).toLocaleString() : '',
    text: m.text || m.message || '',
  };
  switchTab('log');
  setTimeout(() => _openLogFormInner('COMMS'), 100);
}

function sendMsg() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || !S.chatCtx || S.sending) return;
  const ctx = S.chatCtx;
  let url, body;

  if (ctx.type === 'mt_chan') {
    const channel = Number(ctx.key);
    url = '/api/chat/send';
    body = { text, channel: Number.isFinite(channel) ? channel : 0 };
  } else if (ctx.type === 'mt_dm') {
    url = `/api/node/${encodeURIComponent(ctx.key)}/dm`;
    body = { text };
  } else if (ctx.type === 'mc_chan') {
    const channel = Number(ctx.key);
    url = `/api/mc/${encodeURIComponent(ctx.radioId)}/send_chan`;
    body = {
      text,
      channel: Number.isFinite(channel) ? channel : 0,
      channel_index: Number.isFinite(channel) ? channel : 0,
    };
  } else if (ctx.type === 'mc_dm') {
    url = `/api/mc/${encodeURIComponent(ctx.radioId)}/send_dm`;
    body = { text, target: ctx.key };
  }

  if (!url) return;
  S.sending = true;
  const sendBtn = document.getElementById('chat-send');
  if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '...'; }
  fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r => r.json())
    .then(d => {
      if (d.ok || d.status === 'ok') {
        input.value = ''; input.style.height = 'auto';
        // Optimistically add to local history
        const store = ctx.type === 'mt_chan' ? S.mtMsgs : ctx.type === 'mt_dm' ? S.mtDmMsgs :
          ctx.type === 'mc_chan' ? (S.mcMsgs[ctx.radioId] = S.mcMsgs[ctx.radioId]||{chan:{},dm:{}}
            ,S.mcMsgs[ctx.radioId].chan) : (S.mcMsgs[ctx.radioId] = S.mcMsgs[ctx.radioId]||{chan:{},dm:{}}
            ,S.mcMsgs[ctx.radioId].dm);
        if (!store[ctx.key]) store[ctx.key] = [];
        store[ctx.key].push({ text, from_me: true, timestamp: Date.now()/1000 });
        addActivity('✓', d.queued ? 'Message queued' : 'Message sent', chatTitle(ctx));
        renderMessages();
      } else { toast('Send failed: ' + (d.error || d.message || '?')); }
    }).catch(() => toast('Send error'))
    .finally(() => {
      S.sending = false;
      const btn = document.getElementById('chat-send');
      if (btn) { btn.disabled = false; btn.textContent = 'Send'; }
    });
}

function chatTitle(ctx) {
  if (!ctx) return '';
  if (ctx.type === 'mt_chan') return `MT #${ctx.key}`;
  if (ctx.type === 'mt_dm') return S.nodes[ctx.key]?.name || ctx.key;
  if (ctx.type === 'mc_chan') return `MC #${ctx.key}`;
  if (ctx.type === 'mc_dm') return S.mcNodes[ctx.radioId]?.[ctx.key]?.name || ctx.key;
  return '';
}

function bumpUnread() {
  S.unread++;
  const b = document.getElementById('chat-badge');
  b.textContent = S.unread > 9 ? '9+' : S.unread;
  b.hidden = false;
}
function clearUnread() {
  S.unread = 0;
  document.getElementById('chat-badge').hidden = true;
}

function _chKey(type, radioId, key) { return `${type}:${radioId || ''}:${key}`; }

function bumpChUnread(type, radioId, key) {
  const k = _chKey(type, radioId, key);
  S.chUnread[k] = (S.chUnread[k] || 0) + 1;
  if (S.activeTab === 'chat') renderChatSelector();
}

function clearChUnread(type, radioId, key) {
  delete S.chUnread[_chKey(type, radioId, key)];
  renderChatSelector();
}

function showMsgToast(title, body, onTap) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.innerHTML = `<div class="toast-title">${esc(title)}</div><div class="toast-body">${esc(body)}</div>`;
  el.classList.add('show');
  el.classList.toggle('tappable', !!onTap);
  el.onclick = onTap ? () => { el.classList.remove('show'); onTap(); } : null;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}

// ── Log tab ───────────────────────────────────────────────────────────────────

function loadLog() {
  fetch('/api/toc').then(r => r.json()).then(d => {
    S.logEntries = d.entries || d || [];
    S.logEntries.sort((a, b) => Number(b.ts || 0) - Number(a.ts || 0));
    renderMissionControls();
    if (S.activeTab === 'log') renderLog();
  }).catch(() => {});
}

function logMissionFromBody(body) {
  const text = String(body || '');
  const m = text.match(/^\*\*Mission \/ Folder:\*\*\s*(.+)$/mi)
    || text.match(/^Mission \/ Folder:\s*(.+)$/mi);
  return m ? m[1].trim() : '';
}

function logBodyWithoutMission(body) {
  return String(body || '')
    .split('\n')
    .filter(line => !/^\*\*?Mission \/ Folder:/.test(line))
    .join('\n')
    .trim();
}

function logWithMission(body, mission) {
  const cleanBody = logBodyWithoutMission(body);
  const cleanMission = String(mission || '').trim();
  return cleanMission ? `**Mission / Folder:** ${cleanMission}\n${cleanBody}`.trim() : cleanBody;
}

function missionStats() {
  const stats = new Map();
  S.logEntries.forEach(e => {
    const mission = logMissionFromBody(e.body);
    if (!mission) return;
    const key = mission.toLowerCase();
    const cur = stats.get(key) || { name: mission, count: 0, lastTs: 0, cats: new Map() };
    cur.count++;
    cur.lastTs = Math.max(cur.lastTs, Number(e.ts || 0));
    cur.cats.set(e.category || 'NOTE', (cur.cats.get(e.category || 'NOTE') || 0) + 1);
    stats.set(key, cur);
  });
  return [...stats.values()].sort((a, b) => b.lastTs - a.lastTs || a.name.localeCompare(b.name));
}

function renderMissionControls() {
  const missions = missionStats();
  const sel = document.getElementById('log-mission-filter');
  const list = document.getElementById('log-mission-list');
  const current = sel?.value || '';
  if (sel) {
    sel.innerHTML = '<option value="">All missions</option>' + missions
      .map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join('');
    if (current && missions.some(m => m.name === current)) sel.value = current;
  }
  if (list) list.innerHTML = missions.map(m => `<option value="${esc(m.name)}"></option>`).join('');
}

function renderMissionStrip() {
  const wrap = document.getElementById('log-missions');
  if (!wrap) return;
  const active = (document.getElementById('log-mission-filter')?.value || '').toLowerCase();
  const missions = missionStats();
  if (!missions.length) {
    wrap.innerHTML = `<span style="color:var(--muted);font-size:12px;padding:4px 0">No missions yet</span>`;
    return;
  }
  const chOpts = channels.map(ch => `<option value="ch:${ch.index}" ${!S.activeDmNodeId && ch.index === activeCh ? "selected" : ""}>${esc(ch.name)}</option>`).join("");
  const dmOpts = convs.map(c => `<option value="dm:${c.id}" ${S.activeDmNodeId === c.id ? "selected" : ""}>DM: ${esc(c.name)}</option>`).join("");
  return `<optgroup label="Channels">${chOpts}</optgroup><optgroup label="Direct Messages">${dmOpts}</optgroup>`;
}

function setMsgNet(net) {
  S.activeMsgNet = net === "mc" ? "mc" : "mt";
  S.activeDmNodeId = null;
  const chSel = $("msg-ch");
  if (chSel) {
    chSel.innerHTML = renderMsgChannelOptions();
    chSel.value = `ch:${S.activeMsgNet === "mc" ? S.activeMcCh : S.activeMtCh}`;
  }
  document.querySelectorAll("[data-msg-net]").forEach(btn => {
    const active = btn.dataset.msgNet === S.activeMsgNet;
    btn.classList.toggle("active", active);
    btn.style.color = active ? "var(--accent)" : "";
  });
  renderMsgList();
}

function filterLogMission(mission) {
  const sel = document.getElementById('log-mission-filter');
  if (sel) sel.value = mission;
  renderLog();
}

function renderLog() {
  const cat = document.getElementById('log-cat-filter')?.value || '';
  const q = (document.getElementById('log-search')?.value || '').trim().toLowerCase();
  const mission = (document.getElementById('log-mission-filter')?.value || '').trim().toLowerCase();
  const entries = S.logEntries.filter(e => {
    if (cat && e.category !== cat) return false;
    if (mission && logMissionFromBody(e.body).toLowerCase() !== mission) return false;
    if (q && !(`${e.category || ''} ${e.body || ''}`.toLowerCase().includes(q))) return false;
    return true;
  });

  $("msg-ch").onchange = e => {
    const val = e.target.value;
    if (val.startsWith("dm:")) {
      S.activeDmNodeId = val.slice(3);
    } else {
      S.activeDmNodeId = null;
      const idx = parseInt(val.replace("ch:", ""));
      if (S.activeMsgNet === "mt") S.activeMtCh = idx;
      else S.activeMcCh = idx;
    }
    updateDmDelBtn();
    renderMsgList();
  };

  function updateDmDelBtn() {
    const btn = $("dm-del-btn");
    if (!btn) return;
    btn.style.display = S.activeDmNodeId ? "" : "none";
    btn.onclick = async () => {
      if (!confirm("Delete DM conversation with this contact?")) return;
      const net = S.activeMsgNet;
      if (net === "mc" && S.activeMcRadio) {
        await apiFetch(`/api/mc/${S.activeMcRadio}/dm_messages/${S.activeDmNodeId}`, { method: "DELETE" });
      }
      S.messages = S.messages.filter(m => !(m.network === net && m.is_dm && (m.from_id === S.activeDmNodeId || m.to_id === S.activeDmNodeId)));
      S.activeDmNodeId = null;
      const chSel = $("msg-ch");
      if (chSel) {
        chSel.innerHTML = renderMsgChannelOptions();
        chSel.value = `ch:${S.activeMsgNet === "mc" ? S.activeMcCh : S.activeMtCh}`;
      }
      updateDmDelBtn();
      renderMsgList();
    };
  }
  renderMissionControls();
  renderMissionStrip();
  if (!entries.length) {
    el.innerHTML = `<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">${S.logEntries.length ? 'No matching entries' : 'No entries yet'}</div>`;
    return;
  }
  el.innerHTML = entries.map(e => {
    const missionName = logMissionFromBody(e.body);
    const missionBadge = missionName ? `<span class="log-mission" onclick="event.stopPropagation();filterLogMission('${jsSafe(missionName)}')">${esc(missionName)}</span>` : '<span></span>';
    return `<div class="log-row" onclick="openLogDetail(${e.id})">
      <div class="log-ts">${fmtLogTs(e.ts)}</div>
      <span class="log-cat ${e.category||''}">${esc(e.category||'NOTE')}</span>
      <div><div class="log-text">${esc(logBodyWithoutMission(e.body)||'')}</div>${missionBadge}</div>
      <div class="log-actions">
        <button class="icon-btn" onclick="event.stopPropagation();editLogEntry(${e.id})" title="Edit">✎</button>
        <button class="icon-btn" onclick="event.stopPropagation();duplicateLogEntry(${e.id})" title="Duplicate">⧉</button>
      </div>
    </div>`;
  }).join('');
}

function openLogForm(cat, body) {
  S.pendingLogContext = null;
  _openLogFormInner(cat, body);
}

function _openLogFormInner(cat, body) {
  S.editingLogId = null;
  document.getElementById('log-form-title').textContent = 'New TOC Entry';
  document.getElementById('lf-status').textContent = '';
  const catEl = document.getElementById('lf-cat');
  catEl.value = cat || 'NOTE';
  document.getElementById('lf-mission').value = '';
  onLogCatChange();
  if (body != null) document.getElementById('lf-body').value = body;
  else if (S.pendingLogContext) document.getElementById('lf-body').value = _smartFillLogTemplate(LOG_TEMPLATES[LOG_CAT_TPL[catEl.value]], S.pendingLogContext);
  else document.getElementById('lf-body').value = '';
  openSheet('log-form-sheet');
}

function quickLog(cat, tpl) {
  const body = LOG_TEMPLATES[tpl] || '';
  switchTab('log');
  setTimeout(() => openLogForm(cat, body), 100);
}

const LOG_TEMPLATES = {
  plan: `Area / Route:\nObjective:\nWindow / Timing:\nMC / MT Setup:\nCheckpoints:\nRisks:\nComms Plan:\nAbort criteria:`,
  sitrep: `Location / Area:\nSituation:\nStatus:\nKnown Nodes:\nIssues / Risks:\nIntent / Plan:\nNext Update:`,
  commscheck: `From:\nTo:\nNetwork / Channel:\nResult:\nFollow-up:`,
  contact: `Node / Station:\nNetwork / Channel:\nFirst Heard:\nAction:`,
  position: `Node / Asset:\nSource:\nAccuracy:\nMovement:`,
  alert: `Priority:\nType:\nStatus:\nImmediate Action:`,
  action: `Task:\nAssigned To:\nStatus:\nFollow-up:`,
};

const LOG_CAT_TPL = {
  PLAN: 'plan', SITREP: 'sitrep', COMMS: 'commscheck',
  CONTACT: 'contact', POSITION: 'position', ALERT: 'alert', ACTION: 'action',
};

// Maps template field labels (lowercased) to keys on a pendingLogContext object,
// so "Log this message" can fill in whichever fields are actually applicable
// per category instead of dumping one fixed body shape.
const LOG_FIELD_ALIASES = {
  'from': 'sender',
  'node / station': 'sender',
  'node / asset': 'sender',
  'to': 'to',
  'network / channel': 'network',
  'result': 'result',
  'first heard': 'time',
};

function _smartFillLogTemplate(tpl, ctx) {
  if (!tpl) return '';
  if (!ctx) return tpl;
  const lines = tpl.split('\n').map(line => {
    const idx = line.indexOf(':');
    if (idx === -1) return line;
    const label = line.slice(0, idx).trim().toLowerCase();
    const key = LOG_FIELD_ALIASES[label];
    const val = key ? ctx[key] : null;
    return val ? `${line.slice(0, idx + 1)} ${val}` : line;
  });
  let out = lines.join('\n');
  if (ctx.text) out += `\n\n"${ctx.text}"`;
  return out;
}

function fillLogTemplate(tpl) {
  const raw = LOG_TEMPLATES[tpl] || '';
  document.getElementById('lf-body').value = _smartFillLogTemplate(raw, S.pendingLogContext);
}

function onLogCatChange() {
  const cat = document.getElementById('lf-cat').value;
  const btns = document.getElementById('lf-tpl-btns');
  const tpl = LOG_CAT_TPL[cat];
  btns.innerHTML = tpl
    ? `<button class="btn btn-sm" onclick="fillLogTemplate('${tpl}')">Fill template</button>`
    : '';
}

function clearLogForm() {
  S.editingLogId = null;
  S.pendingLogContext = null;
  document.getElementById('log-form-title').textContent = 'New TOC Entry';
  document.getElementById('lf-cat').value = 'NOTE';
  document.getElementById('lf-mission').value = '';
  document.getElementById('lf-body').value = '';
  document.getElementById('lf-status').textContent = '';
  onLogCatChange();
}

function submitLog() {
  const cat  = document.getElementById('lf-cat').value;
  const mission = document.getElementById('lf-mission').value.trim();
  const rawBody = document.getElementById('lf-body').value.trim();
  if (!rawBody) { toast('Body required'); return; }
  const body = logWithMission(rawBody, mission);
  const isEdit = !!S.editingLogId;
  const url = isEdit ? `/api/toc/${S.editingLogId}` : '/api/toc';
  fetch(url, { method: isEdit ? 'PATCH' : 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ category: cat, body }) })
    .then(r => r.json()).then(d => {
      if (d.id || d.ok) {
        closeSheet('log-form-sheet'); clearLogForm(); loadLog(); toast(isEdit ? 'Entry updated' : 'Entry saved');
      } else { toast('Save failed'); }
    }).catch(() => toast('Save error'));
}

function openLogDetail(id) {
  const e = S.logEntries.find(x => x.id === id);
  if (!e) return;
  S.selectedLogId = id;
  document.getElementById('ld-cat').textContent = e.category || 'NOTE';
  const mission = logMissionFromBody(e.body);
  document.getElementById('ld-ts').textContent = `${fmtLogTs(e.ts, true)}${mission ? ' · ' + mission : ''}`;
  document.getElementById('ld-body').textContent = e.body || '';
  openSheet('log-detail-sheet');
}

function editLogEntry(id) {
  const targetId = id || S.selectedLogId;
  const e = S.logEntries.find(x => Number(x.id) === Number(targetId));
  if (!e) return;
  S.editingLogId = e.id;
  closeSheet('log-detail-sheet');
  document.getElementById('log-form-title').textContent = `Edit ${e.category || 'NOTE'}`;
  document.getElementById('lf-cat').value = e.category || 'NOTE';
  document.getElementById('lf-mission').value = logMissionFromBody(e.body);
  document.getElementById('lf-body').value = logBodyWithoutMission(e.body);
  document.getElementById('lf-status').textContent = `Editing #${e.id}`;
  onLogCatChange();
  openSheet('log-form-sheet');
}

function duplicateLogEntry(id) {
  const targetId = id || S.selectedLogId;
  const e = S.logEntries.find(x => Number(x.id) === Number(targetId));
  if (!e) return;
  closeSheet('log-detail-sheet');
  openLogForm(e.category || 'NOTE', logBodyWithoutMission(e.body));
  document.getElementById('lf-mission').value = logMissionFromBody(e.body);
  document.getElementById('lf-status').textContent = `Duplicating #${e.id}`;
}

function deleteLogEntry() {
  if (!S.selectedLogId) return;
  if (!confirm('Delete this log entry?')) return;
  fetch(`/api/toc/${S.selectedLogId}`, { method: 'DELETE' })
    .then(r => r.json()).then(d => {
      if (d.ok) { closeSheet('log-detail-sheet'); loadLog(); toast('Deleted'); }
      else { toast('Delete failed'); }
    }).catch(() => toast('Error'));
}

function exportLog(fmt='text') {
  window.location = `/api/toc/export?fmt=${encodeURIComponent(fmt)}`;
}

function importLog(ev) {
  const file = ev.target.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fetch('/api/toc/import', { method: 'POST', body: fd })
    .then(r => r.json()).then(d => {
      if (d.error) {
        toast('Import failed: ' + d.error);
      } else {
        toast(d.imported != null ? `Imported ${d.imported} entries` : 'Import done');
        loadLog();
      }
    }).catch(() => toast('Import error'));
  ev.target.value = '';
}

function jsSafe(s) {
  return String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '');
}

// ── Settings tab ──────────────────────────────────────────────────────────────
const DEFAULT_ACCENT = '#e8b04f';

function normalizeHexColor(color) {
  const v = String(color || '').trim();
  return /^#[0-9a-fA-F]{6}$/.test(v) ? v.toLowerCase() : DEFAULT_ACCENT;
}

function applyAccent(color) {
  const c = normalizeHexColor(color);
  S.accent = c;
  localStorage.setItem('om_accent', c);
  document.documentElement.style.setProperty('--accent', c);
  document.documentElement.style.setProperty('--accent-dim', `${c}24`);
  const input = document.getElementById('accent-input');
  if (input) input.value = c;
}

function loadAppSettings() {
  fetch('/api/settings/app').then(r => r.json()).then(d => {
    if (d.accent_color) applyAccent(d.accent_color);
  }).catch(() => {});
}

function saveAccent(color) {
  const c = normalizeHexColor(color);
  applyAccent(c);
  fetch('/api/settings/app', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ accent_color: c }),
  }).then(r => r.json()).then(d => {
    if (d.ok === false || d.error) toast('Accent saved locally only');
    else toast('Accent saved');
  }).catch(() => toast('Accent saved locally only'));
}

function resetAccent() {
  saveAccent(DEFAULT_ACCENT);
}

function updateSummaryHTML() {
  const raw = S.updateInfo;
  if (!raw) return '<span style="color:var(--muted)">Not checked yet</span>';
  // API returns { info:{...}, state:{...} } — normalise
  const u = (raw.info) ? raw.info : raw;
  const st = (raw.state) ? raw.state : {};
  const avail = u.update_available;
  const status = avail
    ? '<span style="color:var(--accent);font-weight:700">Update available</span>'
    : '<span style="color:#4ade80;font-weight:700">Up to date</span>';
  const cur = esc(u.version || u.current_version || u.local_version || 'unknown');
  const latest = esc(u.remote_commit || u.latest_version || u.remote_version || 'unknown');
  const extra = u.behind ? ` (${u.behind} commit${u.behind>1?'s':''} behind)` : '';
  return `${status}${extra}<div style="margin-top:4px;color:var(--muted);font-size:12px">Current ${cur} · Latest ${latest}</div>`;
}

function updateLogHTML() {
  const raw = S.updateInfo || {};
  const st = raw.state || raw;
  const lines = st.log ? (Array.isArray(st.log) ? st.log.join('\n') : st.log)
    : (st.message || raw.output || raw.log || '');
  return lines ? esc(lines) : 'No update log yet.';
}

function checkUpdate() {
  const summary = document.getElementById('update-summary');
  if (summary) summary.innerHTML = 'Checking…';
  fetch('/api/settings/update/status').then(r => r.json()).then(d => {
    S.updateInfo = d;
    if (S.activeTab === 'settings') renderSettings();
  }).catch(() => {
    if (summary) summary.innerHTML = '<span style="color:var(--red)">Update check failed</span>';
  });
}

function runUpdate() {
  if (S.updateRunning) return;
  askConfirm('Run update?', 'The app will pull the latest OM Lite files. Restart OM Lite after the update finishes.', 'Run update', () => {
    S.updateRunning = true;
    const log = document.getElementById('update-log');
    if (log) log.textContent = 'Running update…';
    fetch('/api/settings/update/run', { method: 'POST' }).then(r => r.json()).then(d => {
      S.updateInfo = d;
      toast(d.ok === false || d.error ? 'Update failed' : 'Update finished');
      if (S.activeTab === 'settings') renderSettings();
    }).catch(() => toast('Update failed')).finally(() => { S.updateRunning = false; });
  });
}

function askConfirm(title, body, okLabel, onOk, danger=true) {
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-body').textContent = body;
  const ok = document.getElementById('confirm-ok');
  ok.textContent = okLabel || 'Confirm';
  ok.className = danger ? 'btn btn-danger' : 'btn btn-accent';
  ok.onclick = () => {
    closeSheet('confirm-sheet');
    onOk?.();
  };
  openSheet('confirm-sheet');
}

function showServiceSplash(mode) {
  const el = document.getElementById('service-splash');
  if (!el) return;
  el.dataset.mode = mode || 'restart';
  el.removeAttribute('hidden');
}

function restartApp() {
  askConfirm('Restart OM Lite?', 'The service will restart and this page will reconnect after a few seconds.', 'Restart', () => {
    showServiceSplash('restart');
    fetch('/api/restart', { method: 'POST' }).finally(() => {
      setTimeout(() => location.reload(), 6500);
    });
  });
}

function shutdownApp() {
  askConfirm('Shutdown OM Lite?', 'This stops the OM Lite service. Use the launcher or SSH to start it again.', 'Shutdown', () => {
    showServiceSplash('shutdown');
    fetch('/api/shutdown', { method: 'POST' }).catch(() => {});
  });
}

function renderSettings() {
  const el = document.getElementById('settings-content');
  let html = '';
  const mtOnline = S.mtRadios.filter(r => r.connected).length;
  const mcOnline = S.mcRadios.filter(r => r.connected).length;
  const mtTotal = S.mtRadios.length;
  const mcTotal = S.mcRadios.length;

  html += `<div class="set-section"><div class="set-h">Field Status</div>
    <div class="settings-grid">
      <div class="summary-chip" style="text-align:center;padding:8px"><div style="color:var(--accent);font-weight:700">${mtOnline}/${mtTotal}</div><div>MT</div></div>
      <div class="summary-chip" style="text-align:center;padding:8px"><div style="color:var(--accent);font-weight:700">${mcOnline}/${mcTotal}</div><div>MC</div></div>
      <div class="summary-chip" style="text-align:center;padding:8px"><div style="color:var(--accent);font-weight:700">${S.followGps ? 'FOLLOW' : (S.gpsMarker ? 'FIX' : 'NO FIX')}</div><div>GPS</div></div>
    </div>
  </div>`;

  html += `<div class="set-section"><div class="set-h">Display</div>
    <div class="color-row">
      <input id="accent-input" type="color" value="${esc(S.accent)}" onchange="saveAccent(this.value)">
      <button class="btn btn-sm btn-accent" onclick="resetAccent()">Gold default</button>
      <span style="font-size:12px;color:var(--muted)">Accent color</span>
    </div>
  </div>`;

  // MT Radios
  html += `<div class="set-section"><div class="set-h">MT Radios (Meshtastic)</div>`;
  if (S.mtRadios.length) {
    S.mtRadios.forEach(r => {
      html += `<div class="radio-row">
        <span class="radio-name">${esc(r.name || r.port || r.id)}</span>
        <span class="radio-status ${r.connected ? 'on' : ''}">${r.connected ? 'Online' : 'Offline'}</span>
        <button class="btn btn-sm" onclick="toggleRadio('mt','${esc(r.id)}',${r.enabled !== false})">${r.enabled !== false ? 'Disable' : 'Enable'}</button>
        <button class="btn btn-danger btn-sm" onclick="removeRadio('mt','${esc(r.id)}')">Remove</button>
      </div>`;
    });
  } else {
    html += `<div style="font-size:13px;color:var(--muted);padding:4px 0">No MT radios added</div>`;
  }
  html += `<div style="display:flex;gap:8px;margin-top:10px">
    <input id="mt-add-port" class="form-input" placeholder="/dev/ttyUSB0 or TCP:host:port" style="flex:1">
    <button class="btn btn-accent btn-sm" onclick="addRadio('mt')">Add</button>
  </div></div>`;

  // MC Radios
  html += `<div class="set-section"><div class="set-h">MC Radios (MeshCore)</div>`;
  if (S.mcRadios.length) {
    S.mcRadios.forEach(r => {
      if (r.adv_loc_policy === undefined) { r.adv_loc_policy = null; loadMcSelfInfo(r.id); }
      html += `<div class="radio-row">
        <span class="radio-name">${esc(r.name || r.port || r.id)}</span>
        <span class="radio-status ${r.connected ? 'on' : ''}">${r.connected ? 'Online' : 'Offline'}</span>
        <button class="btn btn-sm" onclick="toggleRadio('mc','${esc(r.id)}',${r.enabled !== false})">${r.enabled !== false ? 'Disable' : 'Enable'}</button>
        <button class="btn btn-danger btn-sm" onclick="removeRadio('mc','${esc(r.id)}')">Remove</button>
      </div>
      <div class="radio-row" style="padding-top:0">
        <span class="radio-name" style="font-size:11px;color:var(--muted)">Position</span>
        <button class="btn btn-sm" onclick="toggleMcLocPolicy('${esc(r.id)}',${r.adv_loc_policy ? 1 : 0})">Advertise: ${r.adv_loc_policy == null ? '…' : (r.adv_loc_policy ? 'ON' : 'OFF')}</button>
        <button class="btn btn-sm" onclick="startMcPositionPick('${esc(r.id)}')">📍 Pick on map</button>
        <button class="btn btn-sm" onclick="useGpsForMcPosition('${esc(r.id)}')">Use GPS position</button>
      </div>`;
    });
  } else {
    html += `<div style="font-size:13px;color:var(--muted);padding:4px 0">No MC radios added</div>`;
  }
  html += `<div style="display:flex;gap:8px;margin-top:10px">
    <input id="mc-add-port" class="form-input" placeholder="/dev/ttyUSB0 or TCP:host:port" style="flex:1">
    <button class="btn btn-accent btn-sm" onclick="addRadio('mc')">Add</button>
  </div></div>`;

  // RPTR Manage (simplified)
  html += `<div class="set-section"><div class="set-h">Repeater Manage</div>
  <div id="rptr-manage">
    <div class="form-group">
      <label class="form-label">MC Radio</label>
      <select id="rptr-radio" class="form-select">
        ${S.mcRadios.map(r => `<option value="${esc(r.id)}">${esc(r.name||r.port||r.id)}</option>`).join('')}
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">Repeater node</label>
      <select id="rptr-node" class="form-select">
        <option value="">— select —</option>
        ${Object.values(S.mcNodes).flatMap(Object.values)
          .filter(c => c.name?.toLowerCase().includes('rptr') || c.name?.toLowerCase().includes('repeat'))
          .map(c => `<option value="${esc(c.contact_id)}" data-radio="${esc(c.radio_id)}">${esc(c.name||c.contact_id)}</option>`).join('')}
      </select>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <button class="btn btn-sm" onclick="rptr('login')">Login + Read</button>
      <button class="btn btn-sm" onclick="rptr('advert')">Flood Advert</button>
      <button class="btn btn-sm" onclick="rptr('reboot')">Reboot</button>
    </div>
    <div class="form-group" style="display:flex;gap:8px">
      <input id="rptr-cmd" class="form-input" placeholder="Remote command…" style="flex:1">
      <button class="btn btn-sm" onclick="rptr('cmd')">Send</button>
    </div>
    <div id="rptr-log"></div>
  </div></div>`;

  html += `<div class="set-section"><div class="set-h">Update</div>
    <div id="update-summary" style="font-size:13px;margin-bottom:8px">${updateSummaryHTML()}</div>
    <div class="sys-actions">
      <button class="btn btn-sm" onclick="checkUpdate()">Check</button>
      <button class="btn btn-accent btn-sm" onclick="runUpdate()" ${S.updateRunning ? 'disabled' : ''}>Update</button>
    </div>
    <div id="update-log" class="update-log">${updateLogHTML()}</div>
  </div>`;

  html += `<div class="set-section"><div class="set-h">System</div>
    <div class="sys-actions">
      <button class="btn btn-sm" onclick="restartApp()">Restart OM Lite</button>
      <button class="btn btn-danger btn-sm" onclick="shutdownApp()">Shutdown OM Lite</button>
    </div>
  </div>`;

  el.innerHTML = html;
}

function addRadio(type) {
  const portEl = document.getElementById(`${type}-add-port`);
  const port = portEl.value.trim();
  if (!port) return;
  const url = type === 'mt' ? '/api/settings/nodes/add' : '/api/settings/mc_nodes/add';
  fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ port }) })
    .then(r => r.json()).then(d => {
      if (d.ok || d.node_id) { portEl.value = ''; loadRadios(); toast('Radio added'); }
      else { toast('Add failed: ' + (d.error || '?')); }
    }).catch(() => toast('Error'));
}

function removeRadio(type, id) {
  if (!confirm('Remove this radio?')) return;
  const url = type === 'mt' ? `/api/settings/nodes/${encodeURIComponent(id)}/remove`
    : `/api/settings/mc_nodes/${encodeURIComponent(id)}/remove`;
  fetch(url, { method: 'POST' })
    .then(r => r.json()).then(d => {
      if (d.ok) { loadRadios(); toast('Removed'); }
      else { toast('Remove failed'); }
    }).catch(() => toast('Error'));
}

function toggleRadio(type, id, currentlyEnabled) {
  const url = type === 'mt' ? `/api/settings/nodes/${encodeURIComponent(id)}/set_enabled`
    : `/api/settings/mc_nodes/${encodeURIComponent(id)}/set_enabled`;
  fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ enabled: !currentlyEnabled }) })
    .then(r => r.json()).then(() => loadRadios()).catch(() => toast('Error'));
}

function rptr(action) {
  const radioId = document.getElementById('rptr-radio')?.value;
  const nodeId = document.getElementById('rptr-node')?.value;
  const logEl = document.getElementById('rptr-log');
  if (!radioId) { toast('Select a radio'); return; }
  if (action !== 'cmd' && !nodeId) { toast('Select a repeater node'); return; }
  const addLog = s => { if (logEl) logEl.textContent = (logEl.textContent ? logEl.textContent + '\n' : '') + s; };
  addLog(`> ${action}…`);

  if (action === 'login') {
    fetch(`/api/mc/${encodeURIComponent(radioId)}/remote/${encodeURIComponent(nodeId)}/read`, { method: 'POST' })
      .then(r => r.json()).then(d => addLog(JSON.stringify(d, null, 2))).catch(() => addLog('Error'));
  } else if (action === 'advert') {
    fetch(`/api/mc/${encodeURIComponent(radioId)}/advert`, { method: 'POST',
      headers: {'Content-Type':'application/json'}, body: JSON.stringify({ node_id: nodeId }) })
      .then(r => r.json()).then(d => addLog(d.ok ? 'Advert sent' : 'Failed: ' + d.error)).catch(() => addLog('Error'));
  } else if (action === 'reboot') {
    if (!confirm('Reboot this repeater?')) return;
    fetch(`/api/mc/${encodeURIComponent(radioId)}/remote/${encodeURIComponent(nodeId)}/command`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ command: 'reboot' }) })
      .then(r => r.json()).then(d => addLog(d.ok ? 'Reboot sent' : 'Failed')).catch(() => addLog('Error'));
  } else if (action === 'cmd') {
    const cmd = document.getElementById('rptr-cmd')?.value.trim();
    if (!cmd || !nodeId) return;
    fetch(`/api/mc/${encodeURIComponent(radioId)}/remote/${encodeURIComponent(nodeId)}/command`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ command: cmd }) })
      .then(r => r.json()).then(d => addLog(d.reply || d.ok ? 'Sent' : 'Failed')).catch(() => addLog('Error'));
  }
}

// ── Sheets ────────────────────────────────────────────────────────────────────
function openSheet(id) {
  document.querySelectorAll('.bsheet').forEach(s => s.classList.remove('open'));
  document.getElementById(id)?.classList.add('open');
  document.getElementById('sheet-overlay').classList.add('show');
}
function closeSheet(id) {
  document.getElementById(id)?.classList.remove('open');
  const anyOpen = document.querySelector('.bsheet.open');
  if (!anyOpen) document.getElementById('sheet-overlay').classList.remove('show');
}
function closeAllSheets() {
  document.querySelectorAll('.bsheet').forEach(s => s.classList.remove('open'));
  document.getElementById('sheet-overlay').classList.remove('show');
}

// ── Data loading ──────────────────────────────────────────────────────────────
function _updateHdrRadio() {
  const dot = document.getElementById('hdr-radio-dot');
  const name = document.getElementById('hdr-radio-name');
  if (!dot || !name) return;
  const allRadios = [...S.mtRadios, ...S.mcRadios];
  const connected = allRadios.filter(r => r.connected);
  if (connected.length > 0) {
    dot.classList.add('on');
    name.textContent = connected[0].name || connected[0].port || connected[0].id || '?';
    name.title = connected.map(r => r.name || r.port || r.id).join(', ');
  } else if (allRadios.length > 0) {
    dot.classList.remove('on');
    name.textContent = allRadios[0].name || allRadios[0].port || allRadios[0].id || 'Offline';
  } else {
    dot.classList.remove('on');
    name.textContent = '—';
    name.title = '';
  }
}

function loadRadios() {
  fetch('/api/settings/nodes').then(r => r.json()).then(d => {
    S.mtRadios = (d.nodes || d || []).map(r => ({
      ...r,
      connected: r.connected ?? r.status === 'connected',
    }));
    _updateHdrRadio(); updateMapStatus(); renderChatSidebar();
    if (S.activeTab === 'settings') renderSettings();
  }).catch(() => {});

  fetch('/api/settings/mc_nodes').then(r => r.json()).then(d => {
    S.mcRadios = (d.mc_nodes || d.nodes || d || []).map(r => ({
      ...r,
      connected: r.connected ?? r.status === 'connected',
    }));
    _updateHdrRadio(); updateMapStatus(); renderChatSidebar();
    if (S.activeTab === 'settings') renderSettings();
    // Load channels for any radio that doesn't have them yet
    S.mcRadios.forEach(r => {
      if (S.mcChannels[r.id]) return;
      fetch(`/api/mc/${encodeURIComponent(r.id)}/channels`).then(res => res.json()).then(d => {
        S.mcChannels[r.id] = (d.channels || d || []).map((c, i) =>
          ({ ...c, index: c.index ?? c.idx ?? i }));
        renderChatSidebar();
      }).catch(() => {});
    });
    // Load self position (for the map self-marker) regardless of whether Settings has been opened
    S.mcRadios.forEach(r => loadMcSelfInfo(r.id));
  }).catch(() => {});
}

function loadMcSelfInfo(radioId) {
  fetch(`/api/mc/${encodeURIComponent(radioId)}/self`).then(r => r.json()).then(d => {
    const r = S.mcRadios.find(x => x.id === radioId);
    if (r) {
      r.adv_loc_policy = d.node_info?.adv_loc_policy;
      r.adv_lat = d.node_info?.adv_lat;
      r.adv_lon = d.node_info?.adv_lon;
    }
    if (r?.adv_loc_policy && r.adv_lat != null && r.adv_lon != null) updateSelfMarker(r.adv_lat, r.adv_lon);
    else clearSelfMarker();
    if (S.activeTab === 'settings') renderSettings();
  }).catch(() => {});
}

function updateSelfMarker(lat, lon) {
  const pos = [lat, lon];
  if (!S.selfMarker) {
    S.selfMarker = L.marker(pos, {
      icon: L.divIcon({
        html: `<div style="width:12px;height:12px;background:var(--accent);border:2px solid #fff;transform:rotate(45deg);box-shadow:0 0 6px rgba(0,0,0,.5)"></div>`,
        className: '', iconSize: [16, 16], iconAnchor: [8, 8],
      }),
    }).bindTooltip('HD (set position)').addTo(map);
  } else {
    S.selfMarker.setLatLng(pos);
  }
}

function clearSelfMarker() {
  if (S.selfMarker) { map.removeLayer(S.selfMarker); S.selfMarker = null; }
}

function useGpsForMcPosition(radioId) {
  if (!S.gpsMarker) { toast('No GPS fix yet', 3000); return; }
  const pos = S.gpsMarker.getLatLng();
  setMcPosition(radioId, pos.lat, pos.lng);
}

function toggleMcLocPolicy(radioId, current) {
  const next = current ? 0 : 1;
  fetch(`/api/mc/${encodeURIComponent(radioId)}/loc_policy`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ policy: next })
  }).then(r => r.json()).then(d => {
    if (d.ok) { toast(next ? 'Advertising position: ON' : 'Advertising position: OFF', 3000); loadMcSelfInfo(radioId); }
    else toast('Failed: ' + (d.error || '?'), 3500);
  }).catch(() => toast('Error', 3000));
}

function startMcPositionPick(radioId) {
  _mcPosPickRadio = radioId;
  switchTab('map');
  const banner = document.getElementById('map-pick-banner');
  if (banner) { banner.textContent = 'Tap the map to set HD position'; banner.hidden = false; }
}

function cancelMcPositionPick() {
  _mcPosPickRadio = null;
  document.getElementById('map-pick-banner')?.setAttribute('hidden', '');
}

function setMcPosition(radioId, lat, lon) {
  fetch(`/api/mc/${encodeURIComponent(radioId)}/coords`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ lat, lon })
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      toast(`Position set: ${lat.toFixed(5)}, ${lon.toFixed(5)}`, 4000);
      updateSelfMarker(lat, lon);
      switchTab('settings');
    } else toast('Failed to set position: ' + (d.error || '?'), 4000);
  }).catch(() => toast('Position set error', 3500));
}

function loadChannels() {
  fetch('/api/chat/channels').then(r => r.json()).then(d => {
    S.mtChannels = (d.channels || d || []).map((c, i) =>
      typeof c === 'string' ? { name: c, index: i } : { ...c, index: c.index ?? c.idx ?? i });
    renderChatSidebar();
  }).catch(() => {});

  S.mcRadios.forEach(r => {
    fetch(`/api/mc/${encodeURIComponent(r.id)}/channels`).then(res => res.json()).then(d => {
      S.mcChannels[r.id] = (d.channels || d || []).map((c, i) =>
        ({ ...c, index: c.index ?? c.idx ?? i }));
      renderChatSidebar();
    }).catch(() => {});
  });
}

function loadNodes() {
  fetch('/api/nodes').then(r => r.json()).then(d => {
    (d.nodes || d || []).forEach(n => {
      const id = n.id || n.node_id;
      if (!id) return;
      const item = {
        ...n,
        id,
        name: n.name || n.long_name || id,
        lat: n.lat ?? n.latitude,
        lon: n.lon ?? n.longitude,
        last_heard: n.last_heard ?? n.last_heard_ts,
        online: n.online ?? n.connected ?? n.status === 'online',
        type: 'mt',
      };
      S.nodes[id] = item;
      placeNodeMarker(id, item.lat, item.lon, 'mt', item.name, item.online, item);
    });
    renderNodes(); updateMapStatus();
  }).catch(() => {});

  S.mcRadios.forEach(r => {
    fetch(`/api/mc/${encodeURIComponent(r.id)}/contacts`).then(res => res.json()).then(d => {
      if (!S.mcNodes[r.id]) S.mcNodes[r.id] = {};
      (d.contacts || d || []).forEach(c => {
        const cid = c.contact_id || c.id || (c.full_key ? String(c.full_key).slice(0, 12) : '');
        if (!cid) return;
        const item = {
          ...c,
          contact_id: cid,
          id: cid,
          name: c.name || c.long_name || cid,
          lat: c.lat ?? c.latitude,
          lon: c.lon ?? c.longitude,
          last_heard: c.last_heard ?? c.last_seen_ts,
          radio_id: r.id,
          contact_type: c.contact_type ?? c.type,
          type: 'mc',
        };
        S.mcNodes[r.id][cid] = item;
        placeNodeMarker(cid, item.lat, item.lon, 'mc', item.name || cid, true, item);
      });
      renderNodes();
    }).catch(() => {});
  });
}

function loadMessages() {
  // MT messages arrive via SSE only; no history endpoint available
}

function loadMcMessages(radioId) {
  fetch(`/api/mc/${encodeURIComponent(radioId)}/messages`)
    .then(r => r.json())
    .then(d => {
      const msgs = d.messages || d || [];
      const chan = {}, dm = {};
      msgs.forEach(m => {
        const dmId = m.contact_id || (m.subtype === 'dm' ? (m.sent ? m.to_id : m.from_id) : '');
        if (dmId) {
          if (!dm[dmId]) dm[dmId] = [];
          dm[dmId].push(m);
        } else {
          const idx = m.channel ?? m.channel_index ?? 0;
          if (!chan[idx]) chan[idx] = [];
          chan[idx].push(m);
        }
      });
      [chan, dm].forEach(store =>
        Object.values(store).forEach(arr =>
          arr.sort((a, b) => (a.ts || a.timestamp || 0) - (b.ts || b.timestamp || 0))
        )
      );
      S.mcMsgs[radioId] = { chan, dm };
      if (S.chatCtx?.radioId === radioId) renderMessages();
    }).catch(() => {});
}

// ── Utils ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function relTime(ts) {
  if (!ts) return '—';
  const diff = Math.floor(Date.now()/1000 - ts);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

function fmtLogTs(ts, full) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (full) return d.toLocaleString();
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  return d.toLocaleDateString([], {month:'short',day:'numeric'});
}

let _toastTimer;
function toast(msg, dur=2500) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  el.classList.remove('tappable'); el.onclick = null;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), dur);
}

// ── Init ──────────────────────────────────────────────────────────────────────
// ── Header clock ────────────────────────────────────────────────────────────
function _tickClock() {
  const timeEl = document.getElementById('hdr-time');
  const dateEl = document.getElementById('hdr-date');
  if (!timeEl) return;
  const now = new Date();
  const h = String(now.getHours()).padStart(2, '0');
  const m = String(now.getMinutes()).padStart(2, '0');
  const s = String(now.getSeconds()).padStart(2, '0');
  timeEl.textContent = `${h}:${m}:${s}`;
  if (dateEl) {
    const d = String(now.getDate()).padStart(2, '0');
    const mo = String(now.getMonth() + 1).padStart(2, '0');
    const y = String(now.getFullYear()).slice(-2);
    dateEl.textContent = `${d}.${mo}.${y}`;
  }
}
function startClock() {
  _tickClock();
  setInterval(_tickClock, 1000);
}

// ── Header GPS status ────────────────────────────────────────────────────────
function updateHdrGps(hasFix, lat, lon) {
  const dot  = document.getElementById('hdr-gps-dot');
  const text = document.getElementById('hdr-gps-text');
  if (!dot || !text) return;
  if (hasFix && lat && lon) {
    dot.className = 'fix';
    const latStr = (lat >= 0 ? lat.toFixed(4) + '°N' : Math.abs(lat).toFixed(4) + '°S');
    const lonStr = (lon >= 0 ? lon.toFixed(4) + '°E' : Math.abs(lon).toFixed(4) + '°W');
    text.textContent = `${latStr}  ${lonStr}`;
  } else {
    dot.className = '';
    text.textContent = 'No GPS';
  }
}

function init() {
  applyAccent(S.accent);
  loadAppSettings();
  initMap();
  startClock();
  document.getElementById('btn-follow')?.classList.toggle('active', S.followGps);
  loadRadios();
  setTimeout(() => {
    loadNodes();
    loadChannels();
    loadLog();
    startSSE();
    S.mcRadios.forEach(r => loadMcMessages(r.id));
  }, 500);
  // Refresh nodes/status periodically
  setInterval(loadNodes, 30000);
  setInterval(loadRadios, 15000);
  setInterval(() => S.mcRadios.forEach(r => loadMcMessages(r.id)), 12000);
}

document.addEventListener('DOMContentLoaded', init);

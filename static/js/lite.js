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
  mapMsgFeed: [], // [{type,radioId,key,from,text,ts,label,pathLen?,pathHashSize?,routeType?,rssi?,snr?}]
  activeTab:  'map',
  nFilter:    'all',
  selectedNode: null, // {type:'mt'|'mc', id, radioId}
  selectedLogId: null,
  unread:     0,
  senseOn:    false, traceOn: false,
  sseSource:  null,
  layerOpen:  false,
  gpsMarker:  null,
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
};

// ── Map ──────────────────────────────────────────────────────────────────────
let map, activeLayer;

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
  document.getElementById('map').style.height = panel.offsetHeight + 'px';
  map = L.map('map', { zoomControl: true, attributionControl: true }).setView([46.1, 14.8], 10);
  map.zoomControl.setPosition('bottomleft');
  const savedKey = localStorage.getItem('lm_layer');
  const saved = LAYERS[savedKey] ? savedKey : 'voyager';
  applyLayer(saved);
  renderLayerPanel();
  map.on('click', () => { closeLayerPanel(); closeMapMenu(); });
  map.on('dragstart zoomstart', () => {
    if (S.followGps) {
      S.followGps = false;
      S.gpsCenterPrimed = false;
      localStorage.setItem('lm_follow_gps', '0');
      document.getElementById('btn-follow')?.classList.remove('active');
    }
  });
  setTimeout(() => { map.invalidateSize(); }, 50);
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
      if (d.lat && d.lon && d.fix !== false) updateGpsMarker(d.lat, d.lon);
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

function addMsgToFeed(type, radioId, key, from, text, ts, label, extra) {
  S.mapMsgFeed.unshift({ type, radioId, key, from, text, ts, label, ...(extra||{}) });
  if (S.mapMsgFeed.length > 60) S.mapMsgFeed.pop();
  if (S.activeTab === 'map') renderMapActivity();
}

function resolveMcName(radioId, fromId) {
  if (!fromId || fromId === '?') return '?';
  const contacts = S.mcNodes[radioId] || {};
  if (contacts[fromId]) return contacts[fromId].name || contacts[fromId].long_name || fromId.slice(0, 8);
  const found = Object.values(contacts).find(c => {
    const k = c.contact_id || c.id || '';
    return k && (k.startsWith(fromId) || fromId.startsWith(k));
  });
  return found ? (found.name || found.long_name || fromId.slice(0, 8)) : fromId.slice(0, 8);
}

function _hopLabel(pathLen) {
  if (pathLen === 255) return 'route';
  if (pathLen === 0) return 'direct';
  if (pathLen > 0) return pathLen + ' hop' + (pathLen !== 1 ? 's' : '');
  return 'flood';
}

function addActivity(kind, title, sub) {
  S.activity.unshift({ kind, title, sub, ts: Math.floor(Date.now() / 1000) });
  if (S.activity.length > 80) S.activity.pop();
}

function openActivitySheet() {
  renderActivity();
  openSheet('activity-sheet');
}

  if (t === "mc_message") {
    const msg = normMcMsg(d);
    pushMsg(msg);
    if (S.tab === "messages") renderMsgList();
    if (S.dmTarget && d.subtype === "dm" && d.from_id === S.dmTarget._id) renderDmHistory();
    if (d.subtype === "dm" && !msg.sent) {
      toast("DM from " + (msg.from_name || d.from_id));
      if (S.tab === "messages") {
        const chSel = $("msg-ch");
        if (chSel) {
          const prevVal = chSel.value;
          chSel.innerHTML = renderMsgChannelOptions();
          chSel.value = prevVal || (S.activeDmNodeId ? `dm:${S.activeDmNodeId}` : `ch:${S.activeMsgNet === "mc" ? S.activeMcCh : S.activeMtCh}`);
        }
        if (S.activeDmNodeId === msg.from_id) renderMsgList();
      }
    }
    return;
  }
  el.innerHTML = S.activity.map(a => `<div class="activity-item">
    <div class="activity-kind">${esc(a.kind)}</div>
    <div><div class="activity-title">${esc(a.title)}</div><div class="activity-sub">${esc(a.sub || '')}</div></div>
    <div class="activity-time">${relTime(a.ts)}</div>
  </div>`).join('');
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

function onMtMessage(d) {
  const idx = d.channel ?? 0;
  if (!S.mtMsgs[idx]) S.mtMsgs[idx] = [];
  S.mtMsgs[idx].push(d);
  if (S.chatCtx?.type === 'mt_chan' && S.chatCtx.key === idx) {
    renderMessages();
  } else {
    addActivity('💬', d.from_name || d.from_id || 'MT message', d.text || d.message || '');
    bumpUnread();
  }
  const _mtNode = S.nodes[d.from_id];
  if (_mtNode?.lat && !d.from_me) drawMsgPath(_mtNode.lat, _mtNode.lon, '#facc15');
  const _mtChName = (S.mtChannels.find(c => c.index === idx)?.name) || ('ch' + idx);
  addMsgToFeed('mt_chan', null, idx, d.from_name || d.from_id || '?', d.text || d.message || '', d.ts || Math.floor(Date.now()/1000), 'MT #' + _mtChName);
}

function onMcMessage(d) {
  const rid = d.radio_id;
  if (!S.mcMsgs[rid]) S.mcMsgs[rid] = { chan: {}, dm: {} };
  const dmId = d.contact_id || (d.subtype === 'dm' ? (d.sent ? d.to_id : d.from_id) : '');
  if (dmId) {
    if (!S.mcMsgs[rid].dm[dmId]) S.mcMsgs[rid].dm[dmId] = [];
    S.mcMsgs[rid].dm[dmId].push(d);
  } else {
    const idx = d.channel ?? d.channel_index ?? 0;
    if (!S.mcMsgs[rid].chan[idx]) S.mcMsgs[rid].chan[idx] = [];
    S.mcMsgs[rid].chan[idx].push(d);
  }
  const active = S.chatCtx?.type?.startsWith('mc') && S.chatCtx?.radioId === rid;
  if (active) renderMessages();
  else {
    addActivity('💬', d.from_name || d.from_id || 'MC message', d.text || d.message || '');
    bumpUnread();
  }
  if (!d.sent) {
    const _mcContact = (() => {
      const cs = S.mcNodes[rid] || {};
      if (cs[d.from_id]) return cs[d.from_id];
      return Object.values(cs).find(c => { const k = c.contact_id || c.id || ''; return k && (k.startsWith(d.from_id) || d.from_id.startsWith(k)); });
    })();
    if (_mcContact?.lat) drawMsgPath(_mcContact.lat, _mcContact.lon, '#22d3ee');
  }
  const _mcChanName = (S.mcChannels[rid] || []).find(c => c.index === (d.channel ?? 0))?.name || ('ch' + (d.channel ?? 0));
  const _mcFeedType = dmId ? 'mc_dm' : 'mc_chan';
  const _mcFeedKey = dmId || (d.channel ?? 0);
  const _mcFrom = d.from_name || resolveMcName(rid, d.from_id);
  addMsgToFeed(_mcFeedType, rid, _mcFeedKey, _mcFrom, d.text || '', d.ts || Math.floor(Date.now()/1000), 'MC #' + _mcChanName, { pathLen: d.path_len, pathHashSize: d.path_hash_size, routeType: d.route_type, rssi: d.rx_rssi, snr: d.rx_snr });
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
  if (tab === 'map') setTimeout(() => { map.invalidateSize(); _applyMapMode(S.senseOn ? 'sense' : 'map'); }, 60);
  if (tab === 'nodes') renderNodes();
  if (tab === 'chat') renderChatSidebar();
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
  const rows = [];
  const now = Date.now() / 1000;
  const includeNode = n => {
    if (S.nFilter === 'fresh') return Number(n.last_heard || n.last_seen_ts || 0) > now - 3600;
    if (S.nFilter === 'pos') return !!(n.lat && n.lon);
    return true;
  };

  if (S.nFilter !== 'mc') {
    Object.values(S.nodes).forEach(n => {
      if (q && !n.name?.toLowerCase().includes(q)) return;
      if (!includeNode(n)) return;
      rows.push(nodeRow(n.id, 'mt', n.name || n.id, n));
    });
  }
  if (S.nFilter !== 'mt') {
    Object.values(S.mcNodes).forEach(byRadio => {
      Object.values(byRadio).forEach(c => {
        if (q && !c.name?.toLowerCase().includes(q)) return;
        if (!includeNode(c)) return;
        const cid = c.contact_id || c.id;
        rows.push(nodeRow(cid, 'mc', c.name || c.long_name || cid, c, c.radio_id));
      });
    });
  }

  renderNodesSummary();
  el.innerHTML = rows.length ? rows.join('') :
    `<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">No nodes</div>`;
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

function nodeRow(id, type, name, n, radioId) {
  const lastheard = n.last_heard ? relTime(n.last_heard) : '—';
  const meta = type === 'mt'
    ? `${lastheard}${n.hops != null ? ' · ' + n.hops + ' hops' : ''}`
    : `${lastheard}${n.snr != null ? ' · SNR ' + n.snr : ''}`;
  return `<div class="node-row" onclick="openNodeDetail('${esc(id)}','${type}','${esc(radioId||'')}')">
    <span class="ntype ${type}"></span>
    <span class="node-name">${esc(name)}</span>
    <div class="node-meta">${meta}</div>
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
  openSheet('node-sheet');
}

function nodeAction(action) {
  const { id, type, radioId } = S.selectedNode || {};
  if (!id) return;
  if (action === 'dm') {
    S.chatCtx = type === 'mc'
      ? { type: 'mc_dm', radioId, key: id }
      : { type: 'mt_dm', key: id };
    closeAllSheets(); switchTab('chat'); renderChatSidebar(); renderMessages();
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
  let html = '';

  // MT section
  const f = S.chatFilter;
  ['mt','mc'].forEach(n => { const el = document.getElementById('cpill-' + n); if (el) el.classList.toggle('active', f === n); });
  if ((f === 'all' || f === 'mt') && (S.mtChannels.length || Object.keys(S.mtDmMsgs).length)) {
    html += `<div class="cg-header">📻 Meshtastic</div>`;
    S.mtChannels.forEach(ch => {
      const active = isActiveChat('mt_chan', null, ch.index);
      html += `<div class="ch-item${active?' active':''}" onclick="selectChat('mt_chan',null,${ch.index})">
        <span style="color:var(--blue)">#</span> ${esc(ch.name || 'ch' + ch.index)}
        <span style="margin-left:auto;font-size:10px;color:var(--muted)">MT</span>
      </div>`;
    });
    Object.keys(S.mtDmMsgs).forEach(nid => {
      const active = isActiveChat('mt_dm', null, nid);
      html += `<div class="ch-item${active?' active':''}" onclick="selectChat('mt_dm',null,'${esc(nid)}')">
        <span style="color:var(--blue)">@</span> ${esc(S.nodes[nid]?.name || nid)}
        <span style="margin-left:auto;font-size:10px;color:var(--muted)">MT DM</span>
      </div>`;
    });
  }

  // MC section
  if (f === 'all' || f === 'mc') S.mcRadios.forEach(r => {
    const channels = S.mcChannels[r.id] || [];
    const contacts = S.mcNodes[r.id] ? Object.values(S.mcNodes[r.id]) : [];
    if (!channels.length && !contacts.length) return;
    html += `<div class="cg-header">🔗 MeshCore — ${esc(r.name || r.port)}</div>`;
    channels.forEach(ch => {
      const active = isActiveChat('mc_chan', r.id, ch.index);
      html += `<div class="ch-item${active?' active':''}" onclick="selectChat('mc_chan','${esc(r.id)}',${ch.index})">
        <span style="color:var(--accent)">#</span> ${esc(ch.name || 'ch' + ch.index)}
        <span style="margin-left:auto;font-size:10px;color:var(--muted)">MC</span>
      </div>`;
    });
    contacts.forEach(c => {
      const cid = c.contact_id || c.id;
      const active = isActiveChat('mc_dm', r.id, cid);
      html += `<div class="ch-item${active?' active':''}" onclick="selectChat('mc_dm','${esc(r.id)}','${esc(cid)}')">
        <span style="color:var(--accent)">@</span> ${esc(c.name || c.long_name || cid)}
        <span style="margin-left:auto;font-size:10px;color:var(--muted)">MC DM</span>
      </div>`;
    });
  });

  if (!html) html = `<div style="padding:16px 14px;font-size:13px;color:var(--muted)">No radios connected yet.</div>`;
  body.innerHTML = html;
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
    return `<div class="mact-item" onclick="tapMsgFeed(${i})">
      <span class="mact-badge ${isMc?'mc':'mt'}">${isMc?'MC':'MT'}</span>
      <div class="mact-body">
        <div class="mact-from">${esc(m.from)}</div>
        <div class="mact-text">${esc(m.text)}</div>
        <div class="mact-meta">${esc(m.label)} · ${relTime(m.ts)}${m.pathLen != null ? '<br>' + _hopLabel(m.pathLen) + (m.pathHashSize ? ' · ' + m.pathHashSize + 'B' : '') + (m.routeType ? ' · ' + esc(m.routeType) : '') + (m.rssi != null ? ' · ' + m.rssi + 'dBm' : '') : ''}</div>
      </div>
    </div>`;
  }).join('');
}

function tapMsgFeed(i) {
  const m = S.mapMsgFeed[i];
  if (!m) return;
  if (m.type.startsWith('mt')) {
    const node = Object.values(S.nodes).find(n => n.name === m.from || n.id === m.from);
    if (node?.lat) { drawMsgPath(node.lat, node.lon, '#facc15'); map.panTo([node.lat, node.lon]); }
  } else {
    const allC = m.radioId ? (S.mcNodes[m.radioId] || {}) : {};
    const c = Object.values(allC).find(c => c.name === m.from || c.id === m.from || c.contact_id === m.from);
    if (c?.lat) { drawMsgPath(c.lat, c.lon, '#22d3ee'); map.panTo([c.lat, c.lon]); }
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
    const pw = panel.offsetWidth > 0 ? panel.offsetWidth : Math.min(Math.round(window.innerWidth * 0.3), 220);
    const offset = (pw + 8) + 'px';
    const layers = document.getElementById('btn-layers');
    const follow = document.getElementById('btn-follow');
    if (layers) layers.style.right = offset;
    if (follow) follow.style.right = offset;
    const h4 = panel.querySelector('h4');
    if (h4) h4.textContent = mode === 'sense' ? 'SENSE — PACKETS' : 'RECENT MESSAGES';
    renderMapActivity();
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
  S.chatCtx = { type, radioId, key };
  closeSheet('chat-select-sheet');
  renderMessages();

  let name = '';
  if (type === 'mt_chan') {
    const ch = S.mtChannels.find(c => c.index === key);
    name = '# ' + (ch?.name || 'ch' + key);
  } else if (type === 'mt_dm') {
    name = '@ ' + (S.nodes[key]?.name || key);
  } else if (type === 'mc_chan') {
    const ch = (S.mcChannels[radioId] || []).find(c => c.index === key);
    name = '# ' + (ch?.name || 'ch' + key);
  } else if (type === 'mc_dm') {
    const c = S.mcNodes[radioId]?.[key];
    name = '@ ' + (c?.name || c?.long_name || key);
  }
  const nameEl = document.getElementById('chat-current-name');
  nameEl.textContent = name; nameEl.style.color = '';
  document.getElementById('chat-input-bar').hidden = false;
  document.getElementById('chat-empty').hidden = true;
  const _isMc = type.startsWith('mc');
  ['adv-btn-local','adv-btn-flood','adv-sep'].forEach(id => {
    const el = document.getElementById(id); if (el) el.hidden = !_isMc;
  });
}

function renderMessages() {
  const el = document.getElementById('chat-messages');
  const ctx = S.chatCtx;
  if (!ctx) { el.innerHTML = ''; return; }
  let msgs = [];
  if (ctx.type === 'mt_chan') msgs = S.mtMsgs[ctx.key] || [];
  else if (ctx.type === 'mt_dm') msgs = S.mtDmMsgs[ctx.key] || [];
  else if (ctx.type === 'mc_chan') msgs = S.mcMsgs[ctx.radioId]?.chan[ctx.key] || [];
  else if (ctx.type === 'mc_dm') msgs = S.mcMsgs[ctx.radioId]?.dm[ctx.key] || [];

  el.innerHTML = msgs.map(m => {
    const out = m.from_me || m.is_mine;
    const isMc = ctx.type.startsWith('mc');
    const rawSender = isMc
      ? (m.from_name || resolveMcName(ctx.radioId, m.from_id))
      : (m.sender || m.from_name || m.from_id || '?');
    const sender = out ? 'Me' : esc(rawSender);
    const rawTs = m.timestamp || m.ts;
    const ts = rawTs ? new Date(rawTs * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
    let pathInfo = '';
    if (isMc && !out && m.path_len != null) {
      const parts = [_hopLabel(m.path_len)];
      if (m.path_hash_size) parts.push(m.path_hash_size + 'B');
      if (m.rx_rssi != null) parts.push(m.rx_rssi + 'dBm');
      pathInfo = parts.join(' · ');
    }
    return `<div class="msg-row ${out ? 'out' : 'in'}">
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

function sendMsg() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || !S.chatCtx || S.sending) return;
  const ctx = S.chatCtx;
  let url, body;

  if (ctx.type === 'mt_chan') {
    url = '/api/chat/send';
    body = { text, channel: ctx.key };
  } else if (ctx.type === 'mt_dm') {
    url = `/api/node/${encodeURIComponent(ctx.key)}/dm`;
    body = { text };
  } else if (ctx.type === 'mc_chan') {
    url = `/api/mc/${encodeURIComponent(ctx.radioId)}/send_chan`;
    body = { text, channel: ctx.key };
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
        addActivity('✓', 'Message sent', chatTitle(ctx));
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
  S.editingLogId = null;
  document.getElementById('log-form-title').textContent = 'New TOC Entry';
  document.getElementById('lf-status').textContent = '';
  const catEl = document.getElementById('lf-cat');
  catEl.value = cat || 'NOTE';
  document.getElementById('lf-mission').value = '';
  if (body) document.getElementById('lf-body').value = body;
  else document.getElementById('lf-body').value = '';
  onLogCatChange();
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

function onLogCatChange() {
  const cat = document.getElementById('lf-cat').value;
  const tplMap = {
    PLAN: 'plan', SITREP: 'sitrep', COMMS: 'commscheck',
    CONTACT: 'contact', POSITION: 'position', ALERT: 'alert', ACTION: 'action',
  };
  const btns = document.getElementById('lf-tpl-btns');
  const tpl = tplMap[cat];
  btns.innerHTML = tpl
    ? `<button class="btn btn-sm" onclick="document.getElementById('lf-body').value=LOG_TEMPLATES['${tpl}']">Fill template</button>`
    : '';
}

function clearLogForm() {
  S.editingLogId = null;
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

function restartApp() {
  askConfirm('Restart OM Lite?', 'The service will restart and this page will reconnect after a few seconds.', 'Restart', () => {
    fetch('/api/restart', { method: 'POST' }).finally(() => {
      toast('Restarting…', 4000);
      setTimeout(() => location.reload(), 6500);
    });
  });
}

function shutdownApp() {
  askConfirm('Shutdown OM Lite?', 'This stops the OM Lite service. Use the launcher or SSH to start it again.', 'Shutdown', () => {
    fetch('/api/shutdown', { method: 'POST' }).finally(() => toast('Shutdown requested', 4000));
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
      html += `<div class="radio-row">
        <span class="radio-name">${esc(r.name || r.port || r.id)}</span>
        <span class="radio-status ${r.connected ? 'on' : ''}">${r.connected ? 'Online' : 'Offline'}</span>
        <button class="btn btn-sm" onclick="toggleRadio('mc','${esc(r.id)}',${r.enabled !== false})">${r.enabled !== false ? 'Disable' : 'Enable'}</button>
        <button class="btn btn-danger btn-sm" onclick="removeRadio('mc','${esc(r.id)}')">Remove</button>
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
function loadRadios() {
  fetch('/api/settings/nodes').then(r => r.json()).then(d => {
    S.mtRadios = (d.nodes || d || []).map(r => ({
      ...r,
      connected: r.connected ?? r.status === 'connected',
    }));
    updateMapStatus(); renderChatSidebar();
    if (S.activeTab === 'settings') renderSettings();
  }).catch(() => {});

  fetch('/api/settings/mc_nodes').then(r => r.json()).then(d => {
    S.mcRadios = (d.mc_nodes || d.nodes || d || []).map(r => ({
      ...r,
      connected: r.connected ?? r.status === 'connected',
    }));
    updateMapStatus(); renderChatSidebar();
    if (S.activeTab === 'settings') renderSettings();
  }).catch(() => {});
}

function loadChannels() {
  fetch('/api/chat/channels').then(r => r.json()).then(d => {
    S.mtChannels = (d.channels || d || []).map((c, i) =>
      typeof c === 'string' ? { name: c, index: i } : { ...c, index: c.index ?? i });
    renderChatSidebar();
  }).catch(() => {});

  S.mcRadios.forEach(r => {
    fetch(`/api/mc/${encodeURIComponent(r.id)}/channels`).then(res => res.json()).then(d => {
      S.mcChannels[r.id] = (d.channels || d || []).map((c, i) =>
        ({ ...c, index: c.index ?? i }));
      if (S.activeTab === 'chat') renderChatSidebar();
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
  S.mtChannels.forEach(ch => {
    // Messages come via SSE stream; preload would need a separate history endpoint
  });
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
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), dur);
}

// ── Init ──────────────────────────────────────────────────────────────────────
function init() {
  applyAccent(S.accent);
  loadAppSettings();
  initMap();
  document.getElementById('btn-follow')?.classList.toggle('active', S.followGps);
  loadRadios();
  setTimeout(() => {
    loadNodes();
    loadChannels();
    loadLog();
    startSSE();
  }, 500);
  // Refresh nodes/status periodically
  setInterval(loadNodes, 30000);
  setInterval(loadRadios, 15000);
}

document.addEventListener('DOMContentLoaded', init);

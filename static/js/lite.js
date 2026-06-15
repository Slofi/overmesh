"use strict";

// ════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════
const S = {
  tab:          "contacts",
  mtNodes:      {},   // node_id → node
  mcContacts:   {},   // contact_id → contact
  mcRadios:     [],   // [{id, name, status}]
  mtStatuses:   {},   // radio_id → {status, name}
  activeMcRadio: null,
  messages:     [],   // all messages (MT + MC)
  tocEntries:   [],
  mtChannels:   [],   // [{index, name}]
  activeMsgNet: "mt",
  activeMtCh:   0,
  activeMcCh:   0,
  gpsOk:        false,
  action:       null, // current action sheet node object
  dmTarget:     null, // current DM target node object
};

// ════════════════════════════════════════════════════════
// DOM helpers
// ════════════════════════════════════════════════════════
const $  = id => document.getElementById(id);
const esc = s => String(s || "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// ════════════════════════════════════════════════════════
// Accent color
// ════════════════════════════════════════════════════════
async function applyAccent() {
  let c = "#e8b04f";
  try {
    const r = await fetch("http://localhost:8080/accent");
    if (r.ok) { const d = await r.json(); if (d.color) c = d.color; }
  } catch {
    c = localStorage.getItem("overmesh_accent") || "#e8b04f";
  }
  document.documentElement.style.setProperty("--accent", c);
}

function pollAccent() {
  setInterval(async () => {
    try {
      const r = await fetch("http://localhost:8080/accent");
      if (!r.ok) return;
      const d = await r.json();
      if (d.color) document.documentElement.style.setProperty("--accent", d.color);
    } catch {}
  }, 10000);
}

// ════════════════════════════════════════════════════════
// Map
// ════════════════════════════════════════════════════════
let map, mtGroup, mcGroup, gpsMarker;
const mtMarkers = {};
const mcMarkers = {};

function initMap() {
  map = L.map("map", { center: [46.1, 14.8], zoom: 9, zoomControl: true });
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Tiles © Esri", maxZoom: 18,
  }).addTo(map);
  mtGroup = L.layerGroup().addTo(map);
  mcGroup = L.layerGroup().addTo(map);
}

function _divIcon(color, border, size) {
  const s = size || 12;
  return L.divIcon({
    html: `<div style="width:${s}px;height:${s}px;background:${color};border:2px solid ${border};border-radius:50%;box-shadow:0 1px 3px rgba(0,0,0,.5)"></div>`,
    className: "", iconSize: [s, s], iconAnchor: [s / 2, s / 2],
  });
}

function upsertMtMarker(node) {
  if (!node.latitude || !node.longitude) return;
  const id   = node.id || node.node_id;
  if (!id) return;
  const ll   = [node.latitude, node.longitude];
  const name = node.long_name || id;
  const icon = node.is_local ? _divIcon("var(--accent)", "#fff", 14) : _divIcon("#1565c0", "#90caf9");
  if (mtMarkers[id]) {
    mtMarkers[id].setLatLng(ll).setIcon(icon).setTooltipContent(name);
  } else {
    const m = L.marker(ll, { icon })
      .bindTooltip(name, { permanent: false, direction: "top" })
      .on("click", () => openActionForNode({ ...node, network: "mt", _id: id }))
      .addTo(mtGroup);
    mtMarkers[id] = m;
  }
}

function upsertMcMarker(contact) {
  if (!contact.latitude || !contact.longitude) return;
  const id   = contact.id;
  if (!id) return;
  const ll   = [contact.latitude, contact.longitude];
  const name = contact.long_name || id;
  if (mcMarkers[id]) {
    mcMarkers[id].setLatLng(ll).setTooltipContent(name);
  } else {
    const m = L.marker(ll, { icon: _divIcon("#1b5e20", "#a5d6a7") })
      .bindTooltip(name, { permanent: false, direction: "top" })
      .on("click", () => openActionForNode({ ...contact, network: "mc", _id: id }))
      .addTo(mcGroup);
    mcMarkers[id] = m;
  }
}

function centerMap(lat, lon, zoom) {
  map.setView([lat, lon], zoom || map.getZoom());
}

function updateGpsMarker(lat, lon) {
  const pos = [lat, lon];
  if (!gpsMarker) {
    gpsMarker = L.circleMarker(pos, {
      radius: 7, color: "#fff", weight: 2,
      fillColor: "#4a9eff", fillOpacity: 1,
    }).bindTooltip("GPS Position").addTo(map);
  } else {
    gpsMarker.setLatLng(pos);
  }
  // Update fill color from CSS variable
  try {
    const c = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    gpsMarker.setStyle({ fillColor: c });
  } catch {}
}

// ════════════════════════════════════════════════════════
// Status dots
// ════════════════════════════════════════════════════════
function updateStatusDots() {
  const mtStatuses = Object.values(S.mtStatuses);
  const mtOk   = mtStatuses.some(s => s.status === "connected");
  const mtWarn = !mtOk && mtStatuses.some(s => s.status === "connecting");
  $("dot-mt").className = "sdot" + (mtOk ? " ok" : mtWarn ? " warn" : "");

  const mcOk   = S.mcRadios.some(r => r.status === "connected");
  const mcWarn = !mcOk && S.mcRadios.some(r => r.status === "connecting");
  $("dot-mc").className = "sdot" + (mcOk ? " ok" : mcWarn ? " warn" : "");

  $("dot-gps").className = "sdot" + (S.gpsOk ? " ok" : "");
}

// ════════════════════════════════════════════════════════
// SSE
// ════════════════════════════════════════════════════════
let sseSource, sseRetry;
let _liteUpdatePoll = null;

function connectSSE() {
  if (sseSource) { try { sseSource.close(); } catch {} }
  sseSource = new EventSource("/api/chat/stream");

  sseSource.onmessage = e => {
    let d;
    try { d = JSON.parse(e.data); } catch { return; }
    onSSE(d);
  };

  sseSource.onerror = () => {
    try { sseSource.close(); } catch {}
    clearTimeout(sseRetry);
    sseRetry = setTimeout(connectSSE, 5000);
  };
}

function onSSE(d) {
  const t = d.type;

  // MT message (no type field)
  if (!t) {
    pushMsg(normMtMsg(d));
    if (S.tab === "messages") renderMsgList();
    return;
  }

  if (t === "mc_message") {
    const msg = normMcMsg(d);
    pushMsg(msg);
    if (S.tab === "messages") renderMsgList();
    if (S.dmTarget && d.subtype === "dm" && d.from_id === S.dmTarget._id) renderDmHistory();
    return;
  }

  if (t === "node_status") {
    S.mtStatuses[d.radio_id] = { status: d.status, name: d.name };
    updateStatusDots();
    return;
  }

  if (t === "mc_status") {
    const idx = S.mcRadios.findIndex(r => r.id === d.radio_id);
    if (idx >= 0) S.mcRadios[idx].status = d.status;
    else S.mcRadios.push({ id: d.radio_id, name: d.name || d.radio_id, status: d.status });
    if (!S.activeMcRadio && d.status === "connected") S.activeMcRadio = d.radio_id;
    updateStatusDots();
    if (S.tab === "settings") renderSettings();
    return;
  }

  if (t === "mc_node") {
    const c = {
      id:           d.id,
      full_key:     d.full_key,
      long_name:    d.long_name || d.id,
      latitude:     d.latitude || null,
      longitude:    d.longitude || null,
      last_seen_ts: d.last_heard_ts || 0,
      network:      "mc",
      radio_id:     d.radio_id,
      type:         d.contact_type || d.type || 0,
    };
    S.mcContacts[c.id] = Object.assign(S.mcContacts[c.id] || {}, c);
    upsertMcMarker(S.mcContacts[c.id]);
    if (S.tab === "contacts") renderContacts();
    return;
  }

  if (t === "node_last_heard") {
    const node = S.mtNodes[d.from_id];
    if (node) {
      node.last_heard_ts = d.ts;
      if (d.hops_away != null) node.hops_away = d.hops_away;
    }
    return;
  }

  if (t === "gps_update" || t === "gps_position") {
    if (d.lat && d.lon) {
      S.gpsOk = d.fix !== false;
      updateStatusDots();
      if (S.gpsOk) updateGpsMarker(d.lat, d.lon);
    }
    return;
  }

  if (t === "gps_error") {
    S.gpsOk = false;
    updateStatusDots();
  }
}

// ════════════════════════════════════════════════════════
// Message normalizers
// ════════════════════════════════════════════════════════
function normMtMsg(d) {
  return {
    _mid:      d.id,
    network:   "mt",
    from_name: d.from_name || d.from_id || "?",
    from_id:   d.from_id,
    text:      d.text || "",
    ts:        d.ts || 0,
    channel:   d.channel ?? 0,
    is_dm:     !!d.is_dm,
    sent:      !!d.sent,
    radio_id:  d.radio_id,
    subtype:   d.is_dm ? "dm" : "channel",
  };
}

function normMcMsg(d) {
  return {
    _mid:      d.id,
    network:   "mc",
    from_name: d.from_name || resolveMcName(d.from_id),
    from_id:   d.from_id,
    text:      d.text || "",
    ts:        d.ts || 0,
    channel:   d.channel ?? 0,
    is_dm:     d.subtype === "dm",
    sent:      !!d.sent,
    radio_id:  d.radio_id,
    subtype:   d.subtype || "channel",
  };
}

function resolveMcName(fromId) {
  if (!fromId) return "?";
  const c = S.mcContacts[fromId];
  return c ? (c.long_name || fromId.slice(0, 8)) : fromId.slice(0, 8);
}

function pushMsg(msg) {
  if (msg._mid && S.messages.some(m => m._mid === msg._mid)) return;
  S.messages.push(msg);
  if (S.messages.length > 600) S.messages.shift();
}

// ════════════════════════════════════════════════════════
// API
// ════════════════════════════════════════════════════════
async function apiFetch(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const txt = await r.text().catch(() => "");
    throw new Error(`${r.status}: ${txt.slice(0, 80) || r.statusText}`);
  }
  return r.json();
}

// ════════════════════════════════════════════════════════
// Data loading
// ════════════════════════════════════════════════════════
async function loadAll() {
  await Promise.allSettled([
    loadMtStatus(),
    loadMcStatus(),
    loadMtNodes(),
    loadMtChannels(),
    loadToc(),
  ]);
  if (S.activeMcRadio) {
    await Promise.allSettled([loadMcContacts(), loadMcMessages()]);
  }
  updateStatusDots();
  renderContacts();
}

async function loadMtStatus() {
  try {
    const data = await apiFetch("/api/status");
    S.mtStatuses = {};
    for (const [id, s] of Object.entries(data)) {
      S.mtStatuses[id] = { status: s.status, name: s.name };
    }
  } catch {}
}

async function loadMcStatus() {
  try {
    const data = await apiFetch("/api/mc/status");
    S.mcRadios = (data.mc_nodes || []).map(r => ({ id: r.id, name: r.name, status: r.status }));
    if (!S.activeMcRadio) {
      const connected = S.mcRadios.find(r => r.status === "connected");
      S.activeMcRadio = connected ? connected.id : (S.mcRadios[0]?.id || null);
    }
  } catch {}
}

async function loadMtNodes() {
  try {
    const nodes = await apiFetch("/api/nodes");
    S.mtNodes = {};
    for (const n of nodes) {
      if (!n.id) continue;
      S.mtNodes[n.id] = n;
      upsertMtMarker(n);
    }
  } catch {}
}

async function loadMcContacts() {
  if (!S.activeMcRadio) return;
  try {
    const data = await apiFetch(`/api/mc/${S.activeMcRadio}/contacts`);
    for (const c of (data.contacts || [])) {
      S.mcContacts[c.id] = { ...c, long_name: c.long_name || c.id, network: "mc" };
      upsertMcMarker(S.mcContacts[c.id]);
    }
  } catch {}
}

async function loadMtChannels() {
  try {
    S.mtChannels = await apiFetch("/api/chat/channels");
  } catch {
    S.mtChannels = [{ index: 0, name: "Primary" }];
  }
}

async function loadMcMessages() {
  if (!S.activeMcRadio) return;
  try {
    const data = await apiFetch(`/api/mc/${S.activeMcRadio}/messages?limit=200`);
    for (const m of (data.messages || [])) {
      pushMsg(normMcMsg(m));
    }
    S.messages.sort((a, b) => (a.ts || 0) - (b.ts || 0));
  } catch {}
}

async function loadToc() {
  try {
    const entries = await apiFetch("/api/toc");
    S.tocEntries = entries; // already DESC from API
  } catch {}
}

// ════════════════════════════════════════════════════════
// Contacts panel
// ════════════════════════════════════════════════════════
function renderContacts() {
  const panel = $("panel-contacts");

  const all = [
    ...Object.values(S.mtNodes)
      .filter(n => n.id && n.radio_status !== "disconnected")
      .map(n => ({
        _id:      n.id,
        long_name: n.long_name || n.id,
        network:  "mt",
        last_ts:  n.last_heard_ts || 0,
        is_local: n.is_local,
        has_pos:  !!(n.latitude && n.longitude),
        radio_id: n.radio_id,
        hops:     n.hops_away,
        battery:  n.battery,
        snr:      n.snr,
      })),
    ...Object.values(S.mcContacts)
      .map(c => ({
        _id:      c.id,
        long_name: c.long_name || c.id,
        network:  "mc",
        last_ts:  c.last_seen_ts || 0,
        is_local: false,
        has_pos:  !!(c.latitude && c.longitude),
        radio_id: c.radio_id || S.activeMcRadio,
        type:     c.type,
      })),
  ].sort((a, b) => b.last_ts - a.last_ts);

  if (!all.length) {
    panel.innerHTML = '<div class="empty">No nodes heard yet</div>';
    return;
  }

  panel.innerHTML = all.map(n => {
    const local = n.is_local ? ' <span style="font-size:10px;color:var(--accent)">●</span>' : "";
    const pos   = n.has_pos ? "📍 " : "";
    const ts    = n.last_ts ? ago(n.last_ts) : "—";
    return `<div class="contact-row" data-net="${n.network}" data-id="${esc(n._id)}">
      <span class="net-badge ${n.network}">${n.network.toUpperCase()}</span>
      <span class="cname">${esc(n.long_name)}${local}</span>
      <span class="cmeta">${pos}${ts}</span>
    </div>`;
  }).join("");

  panel.onclick = e => {
    const row = e.target.closest(".contact-row");
    if (!row) return;
    const { net, id } = row.dataset;
    const raw = net === "mt" ? S.mtNodes[id] : S.mcContacts[id];
    if (raw) openActionForNode({ ...raw, network: net, _id: id });
  };
}

// ════════════════════════════════════════════════════════
// Action sheet
// ════════════════════════════════════════════════════════
function openActionForNode(node) {
  S.action = node;
  $("action-title").textContent = node.long_name || node._id;
  $("action-sub").textContent   = `${node.network.toUpperCase()} · ${node._id}`;

  const infoItems = [];
  const ts = node.last_heard_ts || node.last_seen_ts || node.last_ts;
  if (ts) infoItems.push(["Last heard", ago(ts)]);
  if (node.hops_away != null) infoItems.push(["Hops", node.hops_away]);
  if (node.snr != null) infoItems.push(["SNR", `${node.snr} dB`]);
  if (node.battery != null) infoItems.push(["Battery", `${node.battery}%`]);
  if (node.latitude && node.longitude)
    infoItems.push(["Position", `${node.latitude.toFixed(4)}, ${node.longitude.toFixed(4)}`]);
  if (node.type != null && node.network === "mc")
    infoItems.push(["Type", ["User", "Repeater", "Room"][node.type] ?? `${node.type}`]);

  $("action-info").innerHTML = infoItems
    .map(([k, v]) => `<span class="ig-key">${esc(k)}</span><span class="ig-val">${esc(v)}</span>`)
    .join("");

  const btnsEl = $("action-btns");
  btnsEl.innerHTML = "";

  if (node.latitude && node.longitude) {
    btnsEl.appendChild(mkBtn("muted", "Center on map", () => {
      centerMap(node.latitude, node.longitude, 14);
      closeActionSheet();
    }));
  }

  btnsEl.appendChild(mkBtn("muted", "Send DM", () => openDm(node)));

  if (node.network === "mc" && S.activeMcRadio) {
    btnsEl.appendChild(mkBtn("muted", "Ping", () => pingMc(node._id, node.radio_id)));
  }

  $("action-overlay").classList.remove("hidden");
}

function closeActionSheet() {
  $("action-overlay").classList.add("hidden");
  S.action = null;
}

function mkBtn(cls, label, onclick) {
  const b = document.createElement("button");
  b.className = `btn ${cls}`;
  b.textContent = label;
  b.onclick = onclick;
  return b;
}

// ════════════════════════════════════════════════════════
// DM overlay
// ════════════════════════════════════════════════════════
function openDm(node) {
  closeActionSheet();
  S.dmTarget = node;
  $("dm-title").textContent = `DM → ${node.long_name || node._id}`;
  renderDmHistory();
  $("dm-overlay").classList.remove("hidden");
  setTimeout(() => $("dm-input").focus(), 50);
}

function closeDm() {
  $("dm-overlay").classList.add("hidden");
  $("dm-input").value = "";
  S.dmTarget = null;
}

function renderDmHistory() {
  const target = S.dmTarget;
  if (!target) return;
  const tid  = target._id;
  const hist = S.messages.filter(m =>
    m.is_dm && (m.from_id === tid || m.sent)
  ).slice(-30);

  const el = $("dm-history");
  if (!hist.length) {
    el.innerHTML = '<div style="color:var(--text2);font-size:12px">No DM history</div>';
    return;
  }
  el.innerHTML = hist.map(m =>
    `<div class="msg-row ${m.sent ? "sent" : m.network}">
      <div class="msg-hdr">
        <span class="msg-sender">${esc(m.from_name)}</span>
        <span>${ago(m.ts)}</span>
      </div>
      <div class="msg-text">${esc(m.text)}</div>
    </div>`
  ).join("");
  el.scrollTop = el.scrollHeight;
}

async function sendDm() {
  const target = S.dmTarget;
  if (!target) return;
  const text = $("dm-input").value.trim();
  if (!text) return;
  try {
    if (target.network === "mt") {
      await apiFetch(`/api/node/${target._id}/dm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
    } else {
      const rid = target.radio_id || S.activeMcRadio;
      if (!rid) { toast("No MC radio"); return; }
      await apiFetch(`/api/mc/${rid}/send_dm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, dest_id: target._id }),
      });
    }
    $("dm-input").value = "";
    toast("DM sent");
  } catch (e) {
    toast("Send failed: " + e.message, 3500);
  }
}

// ════════════════════════════════════════════════════════
// Ping MC
// ════════════════════════════════════════════════════════
async function pingMc(contactId, radioId) {
  closeActionSheet();
  const rid = radioId || S.activeMcRadio;
  if (!rid) { toast("No MC radio"); return; }
  try {
    await apiFetch(`/api/mc/${rid}/statusreq/${contactId}`, { method: "POST" });
    toast("Ping sent");
  } catch (e) {
    toast("Ping failed: " + e.message, 3500);
  }
}

// ════════════════════════════════════════════════════════
// Messages panel
// ════════════════════════════════════════════════════════
function renderMessages() {
  const panel = $("panel-messages");
  const mtOpts = (S.mtChannels.length ? S.mtChannels : [{ index: 0, name: "Primary" }])
    .map(ch => `<option value="${ch.index}" ${ch.index === S.activeMtCh ? "selected" : ""}>${esc(ch.name)}</option>`)
    .join("");

  panel.innerHTML = `
    <div class="ctrl-row">
      <select id="msg-net">
        <option value="mt" ${S.activeMsgNet === "mt" ? "selected" : ""}>Meshtastic</option>
        <option value="mc" ${S.activeMsgNet === "mc" ? "selected" : ""}>MeshCore</option>
      </select>
      <select id="msg-ch">
        ${S.activeMsgNet === "mt" ? mtOpts : '<option value="0">Channel 0</option>'}
      </select>
    </div>
    <div id="msg-list"></div>
    <div class="compose-wrap">
      <input id="msg-input" type="text" autocomplete="off" placeholder="Message…">
      <button class="btn" id="msg-send-btn">Send</button>
    </div>
  `;

  $("msg-net").onchange = e => {
    S.activeMsgNet = e.target.value;
    const chSel = $("msg-ch");
    if (S.activeMsgNet === "mt") {
      chSel.innerHTML = (S.mtChannels.length ? S.mtChannels : [{ index: 0, name: "Primary" }])
        .map(ch => `<option value="${ch.index}">${esc(ch.name)}</option>`).join("");
    } else {
      chSel.innerHTML = '<option value="0">Channel 0</option>';
    }
    renderMsgList();
  };

  $("msg-ch").onchange = e => {
    if (S.activeMsgNet === "mt") S.activeMtCh = parseInt(e.target.value);
    else S.activeMcCh = parseInt(e.target.value);
    renderMsgList();
  };

  $("msg-input").onkeydown = e => { if (e.key === "Enter") sendMsg(); };
  $("msg-send-btn").onclick = sendMsg;

  renderMsgList();
  setTimeout(() => $("msg-input")?.focus(), 50);
}

function renderMsgList() {
  const listEl = $("msg-list");
  if (!listEl) return;
  const net = S.activeMsgNet;
  const ch  = net === "mt" ? S.activeMtCh : S.activeMcCh;
  const msgs = S.messages
    .filter(m => m.network === net && !m.is_dm && (m.channel ?? 0) === ch)
    .slice(-80);

  if (!msgs.length) {
    listEl.innerHTML = '<div class="empty">No messages yet</div>';
    return;
  }
  listEl.innerHTML = msgs.map(m =>
    `<div class="msg-row ${m.sent ? "sent" : m.network}">
      <div class="msg-hdr">
        <span class="msg-sender">${esc(m.from_name)}</span>
        <span>${ago(m.ts)}</span>
      </div>
      <div class="msg-text">${esc(m.text)}</div>
    </div>`
  ).join("");
  listEl.scrollTop = listEl.scrollHeight;
}

async function sendMsg() {
  const input = $("msg-input");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  try {
    if (S.activeMsgNet === "mt") {
      await apiFetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, channel: S.activeMtCh }),
      });
    } else {
      if (!S.activeMcRadio) { toast("No MC radio connected"); return; }
      await apiFetch(`/api/mc/${S.activeMcRadio}/send_chan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, channel_idx: S.activeMcCh }),
      });
    }
    input.value = "";
    toast("Sent");
  } catch (e) {
    toast("Send failed: " + e.message, 3500);
  }
}

// ════════════════════════════════════════════════════════
// Log panel
// ════════════════════════════════════════════════════════
// ── Log templates (mirrors main OM) ─────────────────────────────────────────
const LOG_TEMPLATES = {
  NOTE: null, // plain textarea
  SITREP: [
    {name:"Location / Area", hint:"Site, route, grid, or operating area"},
    {name:"Situation", hint:"What is happening now", multiline:true},
    {name:"Status", hint:"Normal, degraded, blocked, urgent…"},
    {name:"Known Nodes / Assets", hint:"Nodes, teams, vehicles, stations"},
    {name:"Issues / Risks", hint:"Outages, safety, weather, access, interference"},
    {name:"Intent / Plan", hint:"Next steps or operating plan", multiline:true},
    {name:"Next Update", hint:"When another report is expected"},
    {name:"Notes", hint:"Extra context", multiline:true},
  ],
  "COMMS CHECK": [
    {name:"From", hint:"Calling station"},
    {name:"To", hint:"Receiving station or group"},
    {name:"Network / Channel", hint:"MT channel, MC room/contact"},
    {name:"Message / Check", hint:"What was sent or tested", multiline:true},
    {name:"Signal", hint:"SNR/RSSI/readability"},
    {name:"Hops", hint:"Direct, 1 hop, 2 hops, flood, unknown"},
    {name:"Distance", hint:"Estimated distance if known"},
    {name:"Result", hint:"Good copy, weak, no ack, delayed, failed…"},
    {name:"Notes", hint:"Extra RF/path context", multiline:true},
  ],
  CONTACT: [
    {name:"Node / Station", hint:"Node or station heard"},
    {name:"Network / Channel", hint:"MT channel, MC contact, or room"},
    {name:"First Heard", hint:"Time first heard or observed"},
    {name:"Signal", hint:"SNR/RSSI/quality report"},
    {name:"Hops", hint:"Direct, 1 hop, 2 hops, flood, unknown"},
    {name:"Distance", hint:"Estimated distance if known"},
    {name:"Position", hint:"Coordinates, place, or unknown"},
    {name:"Action / Follow-up", hint:"DM sent, pinged, added to contacts…"},
    {name:"Notes", hint:"Extra contact details", multiline:true},
  ],
  POSITION: [
    {name:"Node / Asset", hint:"Node, person, vehicle, or station"},
    {name:"Coordinates / Place", hint:"Lat/lon, grid, landmark, or route point"},
    {name:"Source", hint:"GPS, manual, report, map pick, inferred"},
    {name:"Accuracy / Confidence", hint:"Exact, approximate, stale, unknown"},
    {name:"Movement / Heading", hint:"Static, moving, heading/speed if known"},
    {name:"Last Heard / Seen", hint:"Time or age of position"},
    {name:"Notes", hint:"Extra position context", multiline:true},
  ],
  WEATHER: [
    {name:"Location / Area", hint:"Site, route, or operating area"},
    {name:"Temperature", hint:"°C, feel, trend"},
    {name:"Wind", hint:"Direction, speed, gusts"},
    {name:"Conditions", hint:"Clear, overcast, fog, rain, snow…"},
    {name:"Visibility", hint:"km, good/limited/poor"},
    {name:"Precipitation", hint:"None, light rain, heavy rain, snow, hail…"},
    {name:"Forecast / Trend", hint:"Expected changes", multiline:true},
    {name:"Operational Impact", hint:"Effect on RF, access, safety, power", multiline:true},
    {name:"Notes", hint:"Extra weather context", multiline:true},
  ],
  PLAN: [
    {name:"Area / Route", hint:"Basecamp, route, or work area"},
    {name:"Objective", hint:"What should be achieved", multiline:true},
    {name:"Window / Timing", hint:"Start time, end time, update cadence"},
    {name:"People / Nodes / Assets", hint:"Operators, MC contacts, MT nodes"},
    {name:"MC / MT Setup", hint:"Radios, channels, antenna, power, GPS", multiline:true},
    {name:"Checkpoints / Triggers", hint:"When to log POSITION, COMMS, ALERT", multiline:true},
    {name:"Risks / Constraints", hint:"Battery, weather, GPS, RF path, access", multiline:true},
    {name:"Comms Plan", hint:"Who to ping, where to report, fallback"},
    {name:"Abort / Change Criteria", hint:"When to stop or change plan"},
    {name:"Notes", hint:"Extra planning context", multiline:true},
  ],
  ALERT: [
    {name:"Priority", hint:"Low, medium, high, urgent"},
    {name:"Type", hint:"Safety, weather, power, comms, security, other"},
    {name:"Location", hint:"Place, grid, or affected area"},
    {name:"Affected Node(s) / People", hint:"Who or what is affected"},
    {name:"Details", hint:"What happened and why it matters", multiline:true},
    {name:"Immediate Action", hint:"Action already taken or needed now", multiline:true},
    {name:"Status", hint:"Open, monitoring, contained, resolved"},
    {name:"Follow-up", hint:"Who checks next and when"},
    {name:"Notes", hint:"Extra details", multiline:true},
  ],
  ADMIN: null, // plain textarea
};

function _logFieldId(name) {
  return "lf-" + name.replace(/[^a-z0-9]/gi, "_").toLowerCase();
}

function renderLogCompose() {
  const cat = ($("log-cat") || {}).value || "NOTE";
  const fields = LOG_TEMPLATES[cat];
  const composeEl = $("log-fields");
  if (!composeEl) return;

  if (!fields) {
    composeEl.innerHTML = `<textarea id="log-body" placeholder="Entry text…" style="width:100%;min-height:80px;padding:8px 10px;background:var(--bg3);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);font-size:13px;font-family:inherit;resize:vertical"></textarea>`;
    return;
  }

  composeEl.innerHTML = fields.map(f => {
    const id = _logFieldId(f.name);
    const input = f.multiline
      ? `<textarea id="${id}" placeholder="${esc(f.hint)}" rows="2" style="width:100%;padding:6px 8px;background:var(--bg3);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;resize:vertical"></textarea>`
      : `<input id="${id}" type="text" placeholder="${esc(f.hint)}" style="width:100%;padding:6px 8px;background:var(--bg3);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px">`;
    return `<div style="margin-bottom:6px">
      <div style="font-size:11px;color:var(--accent);font-weight:600;margin-bottom:3px">${esc(f.name)}</div>
      ${input}
    </div>`;
  }).join("");
}

function renderLog() {
  const panel = $("panel-log");
  const catOpts = Object.keys(LOG_TEMPLATES).map(c =>
    `<option value="${c}">${c}</option>`
  ).join("");

  const entries = S.tocEntries.slice(0, 60).map(e =>
    `<div class="log-row">
      <div class="log-hdr">
        <span class="log-cat">${esc(e.category || "NOTE")}</span>
        <span>${ago(e.ts || 0)}</span>
      </div>
      <div class="log-body">${esc(e.body || "")}</div>
    </div>`
  ).join("") || '<div class="empty">No entries</div>';

  panel.innerHTML = `
    <div class="log-compose">
      <select id="log-cat" style="width:100%;margin-bottom:8px;padding:7px 10px;background:var(--bg3);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);font-size:13px">
        ${catOpts}
      </select>
      <div id="log-fields"></div>
      <button class="btn sm" id="log-submit" style="width:100%;margin-top:8px">Add entry</button>
    </div>
    <div class="shdr">Recent entries</div>
    ${entries}
  `;

  $("log-cat").onchange = renderLogCompose;
  $("log-submit").onclick = submitLog;
  renderLogCompose();
}

async function submitLog() {
  const cat    = $("log-cat")?.value || "NOTE";
  const fields = LOG_TEMPLATES[cat];
  let body;

  if (!fields) {
    body = $("log-body")?.value.trim();
  } else {
    const lines = fields
      .map(f => {
        const val = ($(_logFieldId(f.name)) || {}).value?.trim() || "";
        return val ? `**${f.name}:** ${val}` : null;
      })
      .filter(Boolean);
    body = lines.join("\n");
  }

  if (!body) { toast("Entry text required"); return; }
  try {
    await apiFetch("/api/toc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category: cat, body }),
    });
    // Clear fields
    if (!fields) { if ($("log-body")) $("log-body").value = ""; }
    else fields.forEach(f => { const el = $(_logFieldId(f.name)); if (el) el.value = ""; });
    toast("Entry added");
    await loadToc();
    renderLog();
  } catch (e) {
    toast("Failed: " + e.message, 3500);
  }
}

// ════════════════════════════════════════════════════════
// Update helpers
// ════════════════════════════════════════════════════════
function _liteUpdateSummaryHTML(info, state) {
  if (!info || !info.managed) {
    return `<span style="color:var(--text2)">${esc(info?.error || 'Not a Git install')}</span>`;
  }
  const parts = [
    `v<code>${esc(info.version || '?')}</code>`,
    info.remote_commit ? `GitHub <code>${esc(info.remote_commit)}</code>` : null,
  ].filter(Boolean);
  if (info.dirty) {
    parts.push('<span style="color:#e57373">local changes</span>');
  } else if ((info.ahead || 0) > 0) {
    parts.push(`<span style="color:#ffb74d">ahead by ${info.ahead}</span>`);
  } else if (info.update_available) {
    parts.push(`<span style="color:var(--accent)">${info.behind} update${info.behind !== 1 ? 's' : ''} available</span>`);
  } else if (info.remote_commit) {
    parts.push('<span style="color:#81c784">up to date</span>');
  }
  if (state?.message) {
    const color = state.ok === false ? '#e57373' : state.ok === true ? '#81c784' : 'var(--text2)';
    parts.push(`<span style="color:${color}">${esc(state.message)}</span>`);
  }
  return parts.join(' \xb7 ');
}

function _liteApplyUpdatePayload(payload) {
  const info       = payload?.info  || {};
  const state      = payload?.state || {};
  const sumEl      = $('update-summary');
  const runBtn     = $('update-run-btn');
  const checkBtn   = $('update-check-btn');
  const logEl      = $('update-log');
  const restartRow = $('update-restart-row');
  if (!sumEl) return;
  if (payload?.error) {
    sumEl.innerHTML = `<span style="color:#e57373">${esc(payload.error)}</span>`;
  } else {
    sumEl.innerHTML = _liteUpdateSummaryHTML(info, state);
  }
  const running = !!state?.running;
  if (checkBtn) checkBtn.disabled = running;
  if (runBtn) {
    let blocked = '';
    if (!info.managed)           blocked = 'Not a Git install';
    else if (info.dirty)         blocked = 'Local changes present';
    else if ((info.ahead || 0) > 0) blocked = 'Local commits ahead of GitHub';
    else if (!info.update_available) blocked = 'Already up to date';
    runBtn.disabled    = running || !!blocked;
    runBtn.title       = blocked || (running ? 'Updating…' : 'Pull latest from GitHub');
    runBtn.textContent = running ? 'Updating…' : 'Update';
  }
  if (logEl) {
    const lines = Array.isArray(state?.log) ? state.log : [];
    logEl.style.display = lines.length ? '' : 'none';
    logEl.textContent   = lines.join('\n');
    if (lines.length) logEl.scrollTop = logEl.scrollHeight;
  }
  if (restartRow) {
    restartRow.style.display = (state?.ok && /Restart required/i.test(state?.message || '')) ? '' : 'none';
  }
}

async function liteCheckUpdate(fetchRemote) {
  const checkBtn = $('update-check-btn');
  const sumEl    = $('update-summary');
  if (fetchRemote && checkBtn) { checkBtn.disabled = true; checkBtn.textContent = 'Checking…'; }
  if (fetchRemote && sumEl)    sumEl.innerHTML = '<span style="color:var(--text2)">Checking GitHub…</span>';
  try {
    const r    = await fetch(`/api/settings/update/status${fetchRemote ? '?fetch=1' : ''}`);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) data.error = data.error || `HTTP ${r.status}`;
    _liteApplyUpdatePayload(data);
    if (data.state?.running && !_liteUpdatePoll) {
      _liteUpdatePoll = setInterval(() => liteCheckUpdate(false), 1500);
    }
    if (!data.state?.running && _liteUpdatePoll) {
      clearInterval(_liteUpdatePoll);
      _liteUpdatePoll = null;
    }
  } catch(e) {
    const s = $('update-summary');
    if (s) s.innerHTML = `<span style="color:#e57373">${esc(String(e.message || e))}</span>`;
  } finally {
    const btn = $('update-check-btn');
    if (btn) { btn.disabled = false; btn.textContent = 'Check'; }
  }
}

let _liteUpdateConfirmTimer = null;
async function liteRunUpdate() {
  const runBtn = $('update-run-btn');
  if (runBtn && runBtn.dataset.confirm !== '1') {
    runBtn.dataset.confirm  = '1';
    runBtn.textContent      = 'Confirm?';
    runBtn.style.background = 'var(--accent)';
    runBtn.style.color      = '#000';
    clearTimeout(_liteUpdateConfirmTimer);
    _liteUpdateConfirmTimer = setTimeout(() => {
      if (runBtn) {
        runBtn.dataset.confirm  = '';
        runBtn.textContent      = 'Update';
        runBtn.style.background = '';
        runBtn.style.color      = '';
      }
    }, 3000);
    return;
  }
  if (runBtn) {
    runBtn.dataset.confirm  = '';
    runBtn.textContent      = 'Updating…';
    runBtn.style.background = '';
    runBtn.style.color      = '';
    runBtn.disabled         = true;
  }
  clearTimeout(_liteUpdateConfirmTimer);
  try {
    const r    = await fetch('/api/settings/update/run', { method: 'POST' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    _liteApplyUpdatePayload(data);
    if (!_liteUpdatePoll) _liteUpdatePoll = setInterval(() => liteCheckUpdate(false), 1500);
  } catch(e) {
    toast('Update failed: ' + (e.message || e), 4000);
    await liteCheckUpdate(false);
  }
}

async function liteRestartOM() {
  const btn = $('update-restart-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Restarting…'; }
  try {
    await fetch('/api/restart', { method: 'POST' });
    toast('Restarting OM… page will reload shortly', 5000);
    setTimeout(() => location.reload(), 6000);
  } catch(e) {
    toast('Restart failed: ' + (e.message || e), 3500);
    if (btn) { btn.disabled = false; btn.textContent = 'Restart'; }
  }
}

// ════════════════════════════════════════════════════════
// Settings panel
// ════════════════════════════════════════════════════════
function renderSettings() {
  const panel = $("panel-settings");
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#e8b04f";
  const radioOpts = S.mcRadios.length
    ? S.mcRadios.map(r =>
        `<option value="${esc(r.id)}" ${r.id === S.activeMcRadio ? "selected" : ""}>${esc(r.name)} (${r.status})</option>`
      ).join("")
    : '<option value="">None</option>';
  const ver = document.body.dataset.version || "";

  panel.innerHTML = `
    <div class="shdr">Radio</div>
    <div class="srow">
      <div><div class="slbl">MC Radio</div><div class="ssub">Active MeshCore radio</div></div>
      <select id="mc-radio-sel">${radioOpts}</select>
    </div>

    <div class="shdr" style="margin-top:14px">Map</div>
    <div class="srow">
      <div><div class="slbl">Reload nodes</div><div class="ssub">Refresh MT + MC contacts</div></div>
      <button class="btn sm muted" id="reload-btn">Refresh</button>
    </div>
    <div class="srow">
      <div><div class="slbl">Center Slovenia</div><div class="ssub">Reset map view</div></div>
      <button class="btn sm muted" id="center-btn">Center</button>
    </div>

    <div class="shdr" style="margin-top:14px">Display</div>
    <div class="srow">
      <div><div class="slbl">Accent color</div></div>
      <input type="color" id="accent-input" value="${esc(accent)}" style="width:40px;height:32px;border:none;border-radius:4px;cursor:pointer;background:transparent">
    </div>

    <div class="shdr" style="margin-top:14px">Navigation</div>
    <div class="srow">
      <div><div class="slbl">Full OM</div><div class="ssub">Switch to full OverMesh UI</div></div>
      <a href="/" class="btn sm muted" style="text-decoration:none">Open →</a>
    </div>


    <div class="shdr" style="margin-top:14px">System</div>
    <div id="update-summary" style="font-size:11px;color:var(--text2);margin:4px 10px 8px">—</div>
    <div class="srow">
      <div><div class="slbl">Update</div><div class="ssub">Pull latest from GitHub</div></div>
      <div style="display:flex;gap:6px">
        <button class="btn sm muted" id="update-check-btn">Check</button>
        <button class="btn sm" id="update-run-btn" disabled title="Check for updates first">Update</button>
      </div>
    </div>
    <pre id="update-log" style="display:none;margin:4px 10px;padding:8px;background:var(--bg3);border-radius:6px;font-size:10px;color:var(--text2);max-height:100px;overflow-y:auto;white-space:pre-wrap;word-break:break-word"></pre>
    <div class="srow" id="update-restart-row" style="display:none">
      <div><div class="slbl">Restart required</div><div class="ssub">Apply the update</div></div>
      <button class="btn sm" id="update-restart-btn">Restart</button>
    </div>
    ${ver ? `<div style="margin-top:20px;font-size:11px;color:var(--text2);text-align:center">OM Lite · ${esc(ver)}</div>` : ""}
  `;

  $("mc-radio-sel").onchange = e => {
    S.activeMcRadio = e.target.value;
    S.mcContacts = {};
    Object.values(mcMarkers).forEach(m => m.remove());
    Object.keys(mcMarkers).forEach(k => delete mcMarkers[k]);
    loadMcContacts().then(() => loadMcMessages()).then(() => {
      if (S.tab === "contacts") renderContacts();
      if (S.tab === "messages") renderMsgList();
    });
  };

  $("reload-btn").onclick = async () => {
    toast("Refreshing…");
    await Promise.allSettled([loadMtNodes(), loadMcContacts()]);
    if (S.tab === "contacts") renderContacts();
    toast("Refreshed");
  };

  $("center-btn").onclick = () => map.setView([46.1, 14.8], 9);
  $("accent-input").oninput = e => {
    document.documentElement.style.setProperty("--accent", e.target.value);
    localStorage.setItem("overmesh_accent", e.target.value);
  };

  $("update-check-btn").onclick = () => liteCheckUpdate(true);
  $("update-run-btn").onclick   = liteRunUpdate;
  const restBtn = $("update-restart-btn");
  if (restBtn) restBtn.onclick = liteRestartOM;
  liteCheckUpdate(false);
}

// ════════════════════════════════════════════════════════
// Tab switching
// ════════════════════════════════════════════════════════
function switchTab(tab) {
  S.tab = tab;
  document.querySelectorAll(".tab-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
  const panel = $(`panel-${tab}`);
  if (!panel) return;
  panel.classList.add("active");

  if (tab === "contacts") renderContacts();
  else if (tab === "messages") renderMessages();
  else if (tab === "log") renderLog();
  else if (tab === "settings") renderSettings();
}

// ════════════════════════════════════════════════════════
// Toast
// ════════════════════════════════════════════════════════
let _toastTimer;
function toast(msg, dur = 2500) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), dur);
}

// ════════════════════════════════════════════════════════
// Utils
// ════════════════════════════════════════════════════════
function ago(ts) {
  if (!ts) return "—";
  const d = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (d < 60)    return `${d}s ago`;
  if (d < 3600)  return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

// ════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════
async function init() {
  await applyAccent();
  pollAccent();
  initMap();

  // Tab buttons
  document.querySelectorAll(".tab-btn").forEach(btn =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );

  // Action sheet
  $("action-close").onclick = closeActionSheet;
  $("action-overlay").onclick = e => { if (e.target === $("action-overlay")) closeActionSheet(); };

  // DM overlay
  $("dm-close").onclick = closeDm;
  $("dm-overlay").onclick = e => { if (e.target === $("dm-overlay")) closeDm(); };
  $("dm-send-btn").onclick = sendDm;
  $("dm-input").addEventListener("keydown", e => { if (e.key === "Enter") sendDm(); });

  connectSSE();
  await loadAll();
}

document.addEventListener("DOMContentLoaded", init);

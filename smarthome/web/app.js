/* ==========================================================================
   SmartHome UI
   --------------------------------------------------------------------------
   The UI is intentionally generic: every control is generated from the device
   type manifest the server sends (properties + control hints), so adding a new
   device type never requires touching this file.
   ========================================================================== */

'use strict';

/* ------------------------------------------------------------ utilities -- */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, ms);
  };
  wrapped.flush = (...args) => { clearTimeout(timer); timer = null; fn(...args); };
  wrapped.cancel = () => { clearTimeout(timer); timer = null; };
  return wrapped;
}

const fmtNumber = (value, precision) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '–');
  if (typeof precision === 'number') return number.toFixed(precision);
  return String(Math.round(number * 10) / 10);
};

const relTime = (timestamp) => {
  if (!timestamp) return 'never';
  const seconds = Math.round((Date.now() / 1000) - Number(timestamp));
  if (!Number.isFinite(seconds)) return 'never';
  if (seconds < 0) return 'just now';
  if (seconds < 10) return 'just now';
  if (seconds < 60) return seconds + 's ago';
  if (seconds < 3600) return Math.round(seconds / 60) + 'm ago';
  if (seconds < 86400) return Math.round(seconds / 3600) + 'h ago';
  return Math.round(seconds / 86400) + 'd ago';
};

const clockTime = (timestamp) => {
  if (!timestamp) return '';
  const date = new Date(Number(timestamp) * 1000);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

/* ------------------------------------------------------------------ api -- */
async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: {}, credentials: 'same-origin' };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  if (response.status === 401) {
    location.replace('/');
    throw new Error('Not signed in');
  }
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { error: text }; }
  }
  if (!response.ok) {
    throw new Error((payload && payload.error) || ('Request failed (' + response.status + ')'));
  }
  return payload;
}

/* ---------------------------------------------------------------- icons -- */
const ICONS = {
  strip: '<path d="M3 9h2v6H3zm4 0h2v6H7zm4 0h2v6h-2zm4 0h2v6h-2zm4 0h2v6h-2zM2 7h20v2H2zm0 8h20v2H2z"/>',
  bulb: '<path d="M9 21h6v-1H9zm3-19a7 7 0 00-4 12.7V17a1 1 0 001 1h6a1 1 0 001-1v-2.3A7 7 0 0012 2zm2.4 10.4l-.4.3V16h-4v-3.3l-.4-.3A5 5 0 1114.4 12.4z"/>',
  plug: '<path d="M8 3h2v5h4V3h2v5a5 5 0 01-4 4.9V17h4v2H8v-2h4v-4.1A5 5 0 018 8z"/>',
  relay: '<path d="M4 4h16v2h-6v4.1a3 3 0 11-4 0V6H4zm8 15a3 3 0 110 6 3 3 0 010-6zm0 2a1 1 0 100 2 1 1 0 000-2z"/>',
  thermometer: '<path d="M14 14.8V5a2 2 0 10-4 0v9.8a4 4 0 104 0zM12 20a2 2 0 01-1-3.7V5a1 1 0 012 0v11.3A2 2 0 0112 20z"/>',
  humidity: '<path d="M12 2s6 6.4 6 11a6 6 0 11-12 0c0-4.6 6-11 6-11zm0 16a4 4 0 004-4h-8a4 4 0 004 4z"/>',
  motion: '<path d="M13 2a2 2 0 110 4 2 2 0 010-4zM6 8l3-2 3 2 3-1v4l-3 1v9h-3v-6l-2 3-1-1 2-4-2-2z"/>',
  button: '<path d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 3a7 7 0 110 14 7 7 0 010-14zm0 4a3 3 0 100 6 3 3 0 000-6z"/>',
  door: '<path d="M4 2h16v20h-2V4H6v18H4zm4 3h8v14H8zm2 7v2h1v-2z"/>',
  fan: '<path d="M12 11a2 2 0 100 4 2 2 0 000-4zm1-9c2.5 0 4 2 4 4.5 0 2-1.2 3.5-3 4.7l-1-1.6c1.4-.9 2-1.9 2-3.1 0-1.5-.8-2.5-2-2.5zm-2 0c1.6 0 2.5 1.2 2.5 2.7l-1.8.2c0-.7-.3-1.1-.7-1.1zm-9 8c0-2.1 1.8-3.6 4-3.6 1.9 0 3.4 1.1 4.5 2.7l-1.5 1c-.8-1.1-1.7-1.7-3-1.7-1.3 0-2.2.7-2.2 1.6zM4 12c0-1.3 1-2 2.2-2l.2 1.7c-.5 0-.8.2-.8.5zm16 0c0 2.1-1.8 3.6-4 3.6-1.9 0-3.4-1.1-4.5-2.7l1.5-1c.8 1.1 1.7 1.7 3 1.7 1.3 0 2.2-.7 2.2-1.6zm2 0c0 1.3-1 2-2.2 2l-.2-1.7c.5 0 .8-.2.8-.5zm-9 6c-2.5 0-4-2-4-4.5 0-2 1.2-3.5 3-4.7l1 1.6c-1.4.9-2 1.9-2 3.1 0 1.5.8 2.5 2 2.5zm2 0c-1.6 0-2.5-1.2-2.5-2.7l1.8-.2c0 .7.3 1.1.7 1.1z"/>',
  generic: '<path d="M12 2l9 5v10l-9 5-9-5V7zm0 2.3L5 8.2v7.6l7 4 7-4V8.2z"/>',
};
const iconMarkup = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.generic}</svg>`;

const TONES = ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4', '#ef4444'];

/* ---------------------------------------------------------------- state -- */
const state = {
  devices: new Map(),
  nodes: new Map(),
  types: new Map(),
  settings: {},
  busied: new Set(),
  dismissedErrors: new Set(),
  connected: false,
  socket: null,
  reconnectDelay: 1000,
  reconnectTimer: null,
  uiPingTimer: null,
  lastServerTime: 0,
  enrollmentTimer: null,
  enrollment: { open: false, seconds_left: 0 },
};

/* ------------------------------------------------------------- websocket -- */
function connectSocket() {
  if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/ui';
  let socket;
  try {
    socket = new WebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }
  state.socket = socket;

  socket.addEventListener('open', () => {
    state.reconnectDelay = 1000;
    setConnected(true);
    clearInterval(state.uiPingTimer);
    state.uiPingTimer = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ t: 'ping', ts: Date.now() }));
    }, 25000);
  });

  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    handleMessage(message);
  });

  socket.addEventListener('close', (event) => {
    setConnected(false);
    clearInterval(state.uiPingTimer);
    if (event.code === 4401) {
      location.replace('/');
      return;
    }
    scheduleReconnect();
  });

  socket.addEventListener('error', () => {
    setConnected(false);
  });
}

function scheduleReconnect() {
  if (state.reconnectTimer) return;
  const delay = state.reconnectDelay;
  state.reconnectDelay = Math.min(state.reconnectDelay * 1.8, 15000);
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connectSocket();
  }, delay);
  setConnected(false);
}

function setConnected(connected) {
  state.connected = connected;
  const element = $('#conn-status');
  if (!element) return;
  element.className = 'conn ' + (connected ? 'online' : 'offline');
  $('span', element).textContent = connected ? 'live' : 'reconnecting…';
  if (connected) refreshFromApi();
}

async function refreshFromApi() {
  try {
    const snapshot = await api('/api/bootstrap');
    applySnapshot(snapshot);
  } catch {
    /* socket will resync */
  }
}

/* -------------------------------------------------------------- messages -- */
function handleMessage(message) {
  switch (message.t) {
    case 'snapshot':
      applySnapshot(message);
      break;
    case 'device':
      state.devices.set(message.device.uid, message.device);
      renderDevice(message.device.uid);
      renderStats();
      break;
    case 'removed':
      state.devices.delete(message.uid);
      renderAll();
      break;
    case 'node':
      if (message.node && message.node.node_id) {
        state.nodes.set(message.node.node_id, message.node);
        renderNodesIfVisible();
        renderDeviceBadges();
      }
      break;
    case 'settings':
      state.settings = message.settings;
      applySettings();
      break;
    case 'types':
      applyTypes(message.types, message.type_errors);
      renderAll();
      break;
    case 'enrollment':
      state.enrollment = { open: !!message.enrollment_open, seconds_left: message.enrollment_seconds_left || 0 };
      renderEnrollment();
      break;
    case 'event':
      showToast(eventText(message.event), 'info');
      break;
    case 'toast':
      showToast(message.text, message.level || 'info');
      break;
    case 'nodes_changed':
      refreshFromApi();
      break;
    case 'pong':
      state.lastServerTime = Date.now();
      break;
    case 'bye':
      setConnected(false);
      scheduleReconnect();
      break;
    default:
      break;
  }
}

function applySnapshot(snapshot) {
  state.devices = new Map();
  for (const device of snapshot.devices || []) state.devices.set(device.uid, device);
  state.nodes = new Map();
  for (const node of snapshot.nodes || []) state.nodes.set(node.node_id, node);
  state.settings = snapshot.settings || {};
  applyTypes(snapshot.types || [], snapshot.type_errors || {});
  if (snapshot.server_time) state.lastServerTime = snapshot.server_time * 1000;
  applySettings();
  renderAll();
}

function applyTypes(types, errors) {
  state.types = new Map();
  for (const type of types) state.types.set(type.id, type);
  const notice = $('#type-errors');
  const entries = Object.entries(errors || {});
  if (!entries.length) {
    notice.hidden = true;
    notice.innerHTML = '';
    return;
  }
  notice.hidden = false;
  notice.innerHTML = '<b>Some device types could not be loaded.</b><ul>' +
    entries.map(([file, detail]) => `<li><code>${escapeHtml(file)}</code> — ${escapeHtml(detail)}</li>`).join('') +
    '</ul>';
}

function applySettings() {
  const title = state.settings.title || 'SmartHome';
  document.title = title;
  $('#app-title').textContent = title;
  const online = [...state.devices.values()].filter((device) => device.online).length;
  $('#footer-info').textContent = online + ' of ' + state.devices.size + ' devices online · ' +
    (state.settings.heartbeat_s ? 'heartbeat ' + state.settings.heartbeat_s + 's' : '');
}

/* -------------------------------------------------------------- rendering -- */
function renderAll() {
  renderStats();
  renderDevices();
  renderEnrollment();
}

function renderStats() {
  const devices = [...state.devices.values()];
  const online = devices.filter((device) => device.online).length;
  const nodesOnline = [...state.nodes.values()].filter((node) => node.online).length;
  $('#stat-devices').textContent = String(devices.length);
  $('#stat-online').textContent = String(online);
  $('#stat-nodes').textContent = String(nodesOnline);
  $('#empty-state').hidden = devices.length > 0;
  applySettings();
}function roomOf(device) {
  return (device.room || '').trim();
}

function sortedDevices() {
  const typeOrder = (device) => {
    const type = state.types.get(device.type);
    return type ? 0 : 1;
  };
  return [...state.devices.values()].sort((a, b) => {
    const roomA = roomOf(a).toLowerCase();
    const roomB = roomOf(b).toLowerCase();
    if (roomA !== roomB) {
      if (!roomA) return 1;
      if (!roomB) return -1;
      return roomA.localeCompare(roomB);
    }
    if (typeOrder(a) !== typeOrder(b)) return typeOrder(a) - typeOrder(b);
    return (a.sort_index || 0) - (b.sort_index || 0) || a.name.localeCompare(b.name);
  });
}

function renderDevices() {
  const container = $('#devices');
  const devices = sortedDevices();
  const rooms = new Map();
  for (const device of devices) {
    const room = roomOf(device);
    if (!rooms.has(room)) rooms.set(room, []);
    rooms.get(room).push(device);
  }

  const known = new Set(devices.map((device) => device.uid));
  for (const element of $$('.card', container)) {
    if (!known.has(element.dataset.uid)) element.remove();
  }
  for (const section of $$('.room-section', container)) {
    if (section.dataset.count === '0') section.remove();
  }

  const fragment = document.createDocumentFragment();
  let index = 0;
  const order = [...rooms.keys()].sort((a, b) => (a ? (b ? a.localeCompare(b) : -1) : 1));
  for (const room of order) {
    const section = ensureRoomSection(container, room, fragment);
    const grid = $('.grid', section);
    for (const device of rooms.get(room)) {
      let card = $(`.card[data-uid="${cssEscape(device.uid)}"]`, container);
      if (!card) {
        card = buildCard(device);
        grid.appendChild(card);
      } else if (card.parentElement !== grid) {
        grid.appendChild(card);
      }
      updateCard(card, device);
      grid.appendChild(card); // keep order stable
      index += 1;
    }
    const count = $('.count', section);
    if (count) count.textContent = String(rooms.get(room).length);
    section.dataset.count = String(rooms.get(room).length);
  }
  if (fragment.childNodes.length) container.appendChild(fragment);
}

function ensureRoomSection(container, room, fragment) {
  const id = 'room-' + (room || '_other').replace(/[^a-z0-9_-]/gi, '_');
  let section = container.querySelector(`[data-room-id="${cssEscape(id)}"]`);
  if (section) return section;
  section = document.createElement('section');
  section.className = 'room';
  section.dataset.roomId = id;
  section.dataset.count = '0';
  section.innerHTML = `<div class="room-head"><h2>${escapeHtml(room || 'Other')}</h2><span class="count"></span></div><div class="grid"></div>`;
  fragment.appendChild(section);
  return section;
}

const cssEscape = (value) => (window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/"/g, '\\"'));

function renderDevice(uid) {
  const device = state.devices.get(uid);
  if (!device) return;
  renderDevices();
}

function renderNodesIfVisible() {
  const dialog = $('#dialog');
  if (dialog.open && dialog.dataset.kind === 'settings') paintSettingsDialog();
}

function renderDeviceBadges() {
  for (const card of $$('.card')) {
    const device = state.devices.get(card.dataset.uid);
    if (device) updateCard(card, device);
  }
}

/* ------------------------------------------------------------------ card -- */
function buildCard(device) {
  const template = $('#tpl-device');
  const card = template.content.firstElementChild.cloneNode(true);
  card.dataset.uid = device.uid;

  $('[data-menu]', card).addEventListener('click', (event) => {
    event.stopPropagation();
    openDeviceDialog(device.uid);
  });

  const controls = $('[data-controls]', card);
  controls.dataset.rendered = '';

  wireControls(card, device.uid);
  return card;
}

function updateCard(card, device) {
  const type = state.types.get(device.type);
  card.classList.toggle('on', isOn(device));
  card.classList.toggle('offline', !device.online);
  card.classList.toggle('busy', state.busied.has(device.uid));

  const icon = $('[data-icon]', card);
  if (icon.dataset.icon !== (type ? type.icon : 'generic')) {
    icon.dataset.icon = type ? type.icon : 'generic';
    icon.innerHTML = iconMarkup(type ? type.icon : 'generic');
  }

  $('[data-name]', card).textContent = device.name;
  const sub = [];
  sub.push(type ? type.name : device.type);
  sub.push(device.node_id || 'unassigned');
  $('[data-sub]', card).textContent = sub.join(' · ');

  const badge = $('[data-badge]', card);
  if (!device.node_id) {
    badge.textContent = 'unbound';
    badge.className = 'badge pending';
  } else if (!device.online) {
    badge.textContent = device.node_online ? 'not reporting' : 'offline';
    badge.className = 'badge off';
    badge.title = device.node_online
      ? 'The node is connected but does not report this device yet.'
      : 'The node is not connected. Commands are queued.';
  } else if (device.pending && device.pending.length) {
    badge.textContent = 'sending';
    badge.className = 'badge pending';
    badge.title = 'Waiting for the device to acknowledge.';
  } else {
    badge.textContent = 'online';
    badge.className = 'badge ok';
    badge.title = 'Connected and reporting.';
  }

  const error = $('.card-error', card);
  const showError = device.error && !state.dismissedErrors.has(device.uid);
  if (showError) {
    if (!error) {
      const element = document.createElement('div');
      element.className = 'card-error';
      element.innerHTML = '<span class="grow"></span><button class="icon-btn small" aria-label="Dismiss">&#10005;</button>';
      $('.card-head', card).after(element);
      $('button', element).addEventListener('click', () => {
        state.dismissedErrors.add(device.uid);
        updateCard(card, device);
      });
    }
    $('.card-error .grow', card).textContent = device.error;
  } else if (error) {
    error.remove();
  }

  const controls = $('[data-controls]', card);
  const signature = JSON.stringify([
    device.type,
    (type ? type.properties : {}),
    Object.keys(type ? type.properties : {}),
  ]);
  if (controls.dataset.signature !== signature) {
    controls.dataset.signature = signature;
    controls.innerHTML = buildControlsMarkup(device);
    wireControls(card, device.uid);
  }
  syncControls(card, device, type);

  const foot = $('[data-foot]', card);
  const actions = type ? Object.entries(type.actions || {}) : [];
  if (actions.length) {
    foot.hidden = false;
    const signatureActions = actions.map(([name]) => name).join(',');
    if (foot.dataset.actions !== signatureActions) {
      foot.dataset.actions = signatureActions;
      foot.innerHTML = actions.map(([name, spec]) =>
        `<button class="btn sm ghost" data-action="${escapeHtml(name)}">${escapeHtml(spec.label || name)}</button>`
      ).join('') + '<span class="grow"></span><span data-updated></span>';
    }
    $('[data-updated]', foot).textContent = device.online ? 'synced ' + relTime(device.updated) : 'last seen ' + relTime(device.updated);
  } else {
    foot.hidden = false;
    foot.innerHTML = `<span class="grow"></span><span data-updated>${device.online ? 'synced ' + relTime(device.updated) : 'last seen ' + relTime(device.updated)}</span>`;
  }
}

function isOn(device) {
  const reported = device.reported || {};
  const desired = device.desired || {};
  // Show the hardware's word when it has one, otherwise the queued intent.
  if (typeof reported.power === 'boolean') return reported.power;
  if (typeof desired.power === 'boolean') return desired.power;
  if (typeof reported.brightness === 'number' && 'brightness' in reported) {
    return (reported.brightness || 0) > 0;
  }
  return false;
}

/* -------------------------------------------------------------- controls -- */
function propertyEntries(type) {
  if (!type) return [];
  return Object.entries(type.properties || {}).filter(([, spec]) => !spec.advanced);
}

function buildControlsMarkup(device) {
  const type = state.types.get(device.type);
  if (!type) {
    return '<p class="muted" style="font-size:13px">Unknown device type. The server has no manifest for <code>' +
      escapeHtml(device.type) + '</code>.</p>';
  }
  const entries = propertyEntries(type);
  if (!entries.length) return '<p class="muted" style="font-size:13px">This device has no controls.</p>';

  const html = [];
  for (const [key, spec] of entries) {
    const control = spec.control || 'text';
    const label = escapeHtml(spec.label || prettyName(key));
    const settable = spec.settable !== false;
    if (control === 'switch') {
      // The manifest may name the switch ("Power", "State"); fall back to a
      // neutral label so a bare property name never leaks into the UI.
      const switchLabel = escapeHtml(spec.label || (key === 'power' ? 'Power' : prettyName(key)));
      html.push(`<div class="switch-row" data-control-row="${escapeHtml(key)}">
        <span class="switch-label">${switchLabel}</span>
        <button class="switch" role="switch" aria-checked="false" aria-label="${switchLabel}"
                data-prop="${escapeHtml(key)}" data-kind="bool" ${settable ? '' : 'disabled'}></button>
      </div>`);
    } else if (control === 'slider') {
      const min = Number(spec.min ?? 0);
      const max = Number(spec.max ?? 100);
      const step = Number(spec.step ?? (spec.type === 'float' ? 0.1 : 1));
      html.push(`<div class="control" data-control-row="${escapeHtml(key)}">
        <div class="control-label"><span>${label}</span>
          <span class="control-value" data-value></span></div>
        <input type="range" min="${min}" max="${max}" step="${step}"
               data-prop="${escapeHtml(key)}" data-kind="${escapeHtml(spec.type || 'int')}"
               ${settable ? '' : 'disabled'} aria-label="${label}">
      </div>`);
    } else if (control === 'number') {
      html.push(`<div class="control" data-control-row="${escapeHtml(key)}">
        <div class="control-label"><span>${label}</span><span class="control-value" data-value></span></div>
        <input class="control-input" type="number" min="${spec.min ?? ''}" max="${spec.max ?? ''}"
               ${spec.step ? `step="${spec.step}"` : ''} data-prop="${escapeHtml(key)}"
               data-kind="${escapeHtml(spec.type || 'int')}" ${settable ? '' : 'disabled'} aria-label="${label}">
      </div>`);
    } else if (control === 'select') {
      const options = (spec.options || []).map((option) => {
        const value = typeof option === 'object' ? option.value : option;
        const text = typeof option === 'object' ? (option.label ?? option.value) : option;
        return `<option value="${escapeHtml(value)}">${escapeHtml(text)}</option>`;
      }).join('');
      html.push(`<div class="control" data-control-row="${escapeHtml(key)}">
        <div class="control-label"><span>${label}</span></div>
        <select class="control-select" data-prop="${escapeHtml(key)}" data-kind="enum"
                ${settable ? '' : 'disabled'} aria-label="${label}">${options}</select>
      </div>`);
    } else if (control === 'color') {
      html.push(`<div class="control" data-control-row="${escapeHtml(key)}">
        <div class="control-label"><span>${label}</span><span class="control-value" data-value></span></div>
        <div class="color-row">
          <input type="color" data-prop="${escapeHtml(key)}" data-kind="string"
                 ${settable ? '' : 'disabled'} aria-label="${label}">
        </div>
      </div>`);
    } else if (control === 'readonly') {
      html.push(`<div class="control" data-control-row="${escapeHtml(key)}">
        <div class="control-label"><span>${label}</span></div>
        <div class="readout"><span class="value" data-readout></span><span class="unit">${escapeHtml(spec.unit || '')}</span></div>
      </div>`);
    } else {
      html.push(`<div class="control" data-control-row="${escapeHtml(key)}">
        <div class="control-label"><span>${label}</span></div>
        <input class="control-input" type="text" data-prop="${escapeHtml(key)}" data-kind="string"
               ${settable ? '' : 'disabled'} aria-label="${label}">
      </div>`);
    }
  }
  return html.join('');
}

const prettyName = (key) => key.replace(/_/g, ' ').replace(/\b\w/g, (ch) => ch.toUpperCase());

function valuesFor(device, type) {
  // Read-only properties come from the hardware only.  Everything else shows
  // desired first: it is the user's intent and updates instantly, while
  // ``reported`` catches up when the hardware acks.
  const values = {};
  const specs = (type && type.properties) || {};
  for (const key of Object.keys(specs)) {
    const spec = specs[key];
    if (spec.settable === false) {
      values[key] = key in (device.reported || {}) ? device.reported[key]
        : key in (device.desired || {}) ? device.desired[key]
        : (spec.default ?? null);
      continue;
    }
    values[key] = key in (device.desired || {}) ? device.desired[key]
      : key in (device.reported || {}) ? device.reported[key]
      : (spec.default ?? null);
  }
  return values;
}

function syncControls(card, device, type) {
  if (!type) return;
  const controls = $('[data-controls]', card);
  const values = valuesFor(device, type);
  // Offline devices stay editable on purpose: the hub stores the desired value
  // and applies it when the node comes back, which is what people expect from
  // a "set it for later" interaction.  Only read-only properties are locked.
  for (const element of $$('[data-prop]', controls)) {
    const key = element.dataset.prop;
    const spec = type.properties[key] || {};
    const value = values[key];
    const locked = spec.settable === false;
    if (element.dataset.editing === '1') {
      continue; // user is dragging / typing
    }
    if (element.dataset.kind === 'bool') {
      element.setAttribute('aria-checked', value ? 'true' : 'false');
      element.disabled = locked;
    } else if (element.tagName === 'INPUT' && element.type === 'range') {
      const min = Number(spec.min ?? 0);
      const max = Number(spec.max ?? 100);
      const numeric = Number(value ?? spec.default ?? min);
      element.value = String(numeric);
      element.style.setProperty('--fill', ((numeric - min) / Math.max(1e-9, max - min)) * 100 + '%');
      element.disabled = locked;
      const target = $('[data-value]', element.closest('[data-control-row]'));
      if (target) target.textContent = fmtNumber(numeric, typeof spec.precision === 'number' ? spec.precision : undefined) + (spec.unit ? ' ' + spec.unit : '');
    } else if (element.tagName === 'INPUT' && element.type === 'number') {
      element.value = value === null || value === undefined ? '' : String(value);
      element.disabled = locked;
    } else if (element.tagName === 'SELECT') {
      element.value = String(value ?? '');
      element.disabled = locked;
    } else if (element.tagName === 'INPUT' && element.type === 'color') {
      element.value = normalizeColor(value);
      element.disabled = locked;
      const label = $('[data-value]', element.closest('[data-control-row]'));
      if (label) label.textContent = normalizeColor(value).toUpperCase();
    } else if (element.dataset.readout !== undefined || element.closest('[data-control-row]')?.querySelector('[data-readout]')) {
      const readout = element.closest('[data-control-row]')?.querySelector('[data-readout]');
      if (readout) readout.textContent = value === null || value === undefined ? '–' : fmtNumber(value, typeof spec.precision === 'number' ? spec.precision : undefined);
    } else if (element.tagName === 'INPUT' && element.type === 'text') {
      if (element.dataset.editing !== '1') element.value = value === null || value === undefined ? '' : String(value);
      element.disabled = locked;
    }
  }

  // readouts have no [data-prop]; fill them here
  for (const row of $$('[data-control-row]', controls)) {
    const readout = $('[data-readout]', row);
    if (!readout) continue;
    const key = row.dataset.controlRow;
    const spec = type.properties[key] || {};
    readout.textContent = values[key] === null || values[key] === undefined
      ? '–'
      : fmtNumber(values[key], typeof spec.precision === 'number' ? spec.precision : undefined);
  }
}

function normalizeColor(value) {
  const text = String(value || '').trim();
  return /^#[0-9a-f]{6}$/i.test(text) ? text : '#ffffff';
}

function wireControls(card, uid) {
  const controls = $('[data-controls]', card);
  const send = (props) => sendProps(uid, props);

  const rangeSend = debounce((key, value) => send({ [key]: value }), 120);

  for (const element of $$('[data-prop]', controls)) {
    const key = element.dataset.prop;
    const kind = element.dataset.kind;

    if (kind === 'bool') {
      element.addEventListener('click', () => {
        const next = element.getAttribute('aria-checked') !== 'true';
        element.setAttribute('aria-checked', next ? 'true' : 'false');
        send({ [key]: next });
      });
    } else if (element.tagName === 'INPUT' && element.type === 'range') {
      element.addEventListener('pointerdown', () => { element.dataset.editing = '1'; });
      element.addEventListener('input', () => {
        const spec = currentSpec(uid, key);
        const min = Number(spec.min ?? 0);
        const max = Number(spec.max ?? 100);
        element.style.setProperty('--fill', ((Number(element.value) - min) / Math.max(1e-9, max - min)) * 100 + '%');
        const target = $('[data-value]', element.closest('[data-control-row]'));
        if (target) target.textContent = fmtNumber(element.value, typeof spec.precision === 'number' ? spec.precision : undefined) + (spec.unit ? ' ' + spec.unit : '');
        rangeSend(key, Number(element.value));
      });
      const finish = () => {
        element.dataset.editing = '0';
        rangeSend.flush(key, Number(element.value));
      };
      element.addEventListener('change', finish);
      element.addEventListener('pointerup', finish);
      element.addEventListener('pointercancel', finish);
    } else if (element.tagName === 'INPUT' && element.type === 'number') {
      element.addEventListener('focus', () => { element.dataset.editing = '1'; });
      element.addEventListener('change', () => {
        element.dataset.editing = '0';
        const spec = currentSpec(uid, key);
        if (element.value === '') return;
        const value = Number(element.value);
        if (!Number.isFinite(value)) return;
        const low = spec.min !== undefined ? Number(spec.min) : -Infinity;
        const high = spec.max !== undefined ? Number(spec.max) : Infinity;
        send({ [key]: clamp(value, low, high) });
      });
      element.addEventListener('blur', () => { element.dataset.editing = '0'; });
    } else if (element.tagName === 'SELECT') {
      element.addEventListener('change', () => send({ [key]: element.value }));
    } else if (element.tagName === 'INPUT' && element.type === 'color') {
      element.addEventListener('input', debounce(() => send({ [key]: element.value }), 220));
      element.addEventListener('change', () => send({ [key]: element.value }));
    } else if (element.tagName === 'INPUT' && element.type === 'text') {
      element.addEventListener('change', () => send({ [key]: element.value }));
    }
  }

  for (const button of $$('[data-action]', card)) {
    if (button.dataset.wired) continue;
    button.dataset.wired = '1';
    button.addEventListener('click', () => runAction(uid, button.dataset.action, button));
  }
  const menu = $('[data-menu]', card);
  if (menu && !menu.dataset.wired) {
    menu.dataset.wired = '1';
    menu.addEventListener('click', (event) => {
      event.stopPropagation();
      openDeviceDialog(uid);
    });
  }
}

function currentSpec(uid, key) {
  const device = state.devices.get(uid);
  const type = device && state.types.get(device.type);
  return (type && type.properties[key]) || {};
}

/* ------------------------------------------------------------- commands -- */
async function sendProps(uid, props) {
  state.dismissedErrors.delete(uid);
  state.busied.add(uid);
  const card = $(`.card[data-uid="${cssEscape(uid)}"]`);
  if (card) card.classList.add('busy');
  try {
    const device = await api('/api/devices/' + encodeURIComponent(uid) + '/props', {
      method: 'POST', body: { props },
    });
    state.devices.set(uid, device);
    renderDevices();
    if (device.error) showToast(device.error, 'error');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.busied.delete(uid);
    const current = $(`.card[data-uid="${cssEscape(uid)}"]`);
    if (current) current.classList.remove('busy');
  }
}

async function runAction(uid, action, button) {
  const type = state.types.get((state.devices.get(uid) || {}).type);
  const spec = type && type.actions ? type.actions[action] : null;
  let args = {};
  if (spec && spec.args && Object.keys(spec.args).length) {
    const values = await promptArgs(uid, action, spec);
    if (values === null) return;
    args = values;
  }
  if (button) button.disabled = true;
  try {
    await api('/api/devices/' + encodeURIComponent(uid) + '/action', {
      method: 'POST', body: { action, args },
    });
    showToast((spec && spec.label ? spec.label : action) + ' sent', 'ok');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

/* ---------------------------------------------------------------- dialog -- */
function openDialog({ title, body, actions = [], kind = '' }) {
  const dialog = $('#dialog');
  dialog.dataset.kind = kind;
  $('#dialog-title').textContent = title;
  const bodyElement = $('#dialog-body');
  if (typeof body === 'string') bodyElement.innerHTML = body;
  else { bodyElement.innerHTML = ''; bodyElement.appendChild(body); }
  const footer = $('#dialog-actions');
  footer.innerHTML = '';
  for (const action of actions) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn ' + (action.style || '');
    button.textContent = action.label;
    if (action.id) button.id = action.id;
    button.addEventListener('click', () => action.onClick && action.onClick(button));
    footer.appendChild(button);
  }
  if (!actions.length) {
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn';
    close.textContent = 'Close';
    close.addEventListener('click', () => dialog.close());
    footer.appendChild(close);
  }
  if (!dialog.open) dialog.showModal();
  return dialog;
}

function closeDialog() {
  const dialog = $('#dialog');
  if (dialog.open) dialog.close();
}

/* ---- device dialog ------------------------------------------------------ */
function openDeviceDialog(uid) {
  const device = state.devices.get(uid);
  if (!device) return;
  const type = state.types.get(device.type);
  const body = document.createElement('div');
  body.innerHTML = renderDeviceDialog(device, type);
  const dialog = openDialog({
    title: device.name,
    body,
    kind: 'device',
    actions: [
      { label: 'Delete', style: 'danger', onClick: () => deleteDevice(uid) },
      { label: 'Save', style: 'primary', onClick: () => saveDevice(uid) },
    ],
  });
  wireDeviceDialog(dialog, device, type);
}

function renderDeviceDialog(device, type) {
  const tabs = `
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="true" data-tab="general">General</button>
      <button role="tab" aria-selected="false" data-tab="hardware">Hardware</button>
      <button role="tab" aria-selected="false" data-tab="advanced">Advanced</button>
    </div>`;

  const hardware = type ? Object.entries(type.config || {}) : [];
  const advanced = type ? Object.entries(type.properties || {}).filter(([, spec]) => spec.advanced) : [];

  return tabs + `
    <section data-panel="general">
      <div class="field-row">
        <label class="field"><span>Name</span>
          <input type="text" data-field="name" value="${escapeHtml(device.name)}" maxlength="64">
        </label>
        <label class="field"><span>Room</span>
          <input type="text" data-field="room" value="${escapeHtml(device.room || '')}"
                 maxlength="48" list="room-list" placeholder="e.g. Living room">
          <datalist id="room-list">${[...new Set([...state.devices.values()].map(roomOf).filter(Boolean))]
            .map((room) => `<option value="${escapeHtml(room)}">`).join('')}</datalist>
        </label>
      </div>
      <label class="field"><span>Node</span>
        <select data-field="node_id">
          <option value="">— unassigned —</option>
          ${[...state.nodes.values()].map((node) =>
            `<option value="${escapeHtml(node.node_id)}" ${node.node_id === device.node_id ? 'selected' : ''}>
              ${escapeHtml(node.node_id)}${node.online ? ' (online)' : ''}</option>`).join('')}
        </select>
        <span class="hint">A node is one microcontroller; it can host several devices.</span>
      </label>
      <dl class="kv">
        <dt>ID</dt><dd><span class="chip">${escapeHtml(device.uid)}</span></dd>
        <dt>Type</dt><dd>${escapeHtml(type ? type.name : device.type)}</dd>
        <dt>Status</dt><dd>${device.online ? 'online' : 'offline'}</dd>
      </dl>
    </section>
    <section data-panel="hardware" hidden>
      ${hardware.length ? `<div class="field-row">${hardware.map(([key, spec]) => renderConfigField(key, spec, device.config[key])).join('')}</div>`
        : '<p class="muted">This device type has no hardware settings.</p>'}
      <p class="hint">Changing wiring requires the node to reconnect (or reboot) to apply the new configuration.</p>
    </section>
    <section data-panel="advanced" hidden>
      ${advanced.length ? `<div class="field-row">${advanced.map(([key, spec]) => renderAdvancedField(key, spec, device)).join('')}</div>`
        : '<p class="muted">Nothing here.</p>'}
      <div class="field">
        <span>History (last 24 h)</span>
        <div data-history class="muted" style="font-size:12px">loading…</div>
      </div>
    </section>`;
}

function renderConfigField(key, spec, value) {
  const label = escapeHtml(spec.label || prettyName(key));
  const field = (input) => `<label class="field" data-config-row="${escapeHtml(key)}">
      <span>${label}${spec.unit ? ` <span class="muted">(${escapeHtml(spec.unit)})</span>` : ''}</span>${input}</label>`;
  if (spec.type === 'bool') {
    return `<label class="field"><span>${label}</span>
      <select data-config="${escapeHtml(key)}" data-kind="bool">
        <option value="false" ${value ? '' : 'selected'}>No</option>
        <option value="true" ${value ? 'selected' : ''}>Yes</option>
      </select></label>`;
  }
  if (spec.type === 'enum') {
    return field(`<select data-config="${escapeHtml(key)}" data-kind="enum">
      ${(spec.options || []).map((option) => {
        const optionValue = typeof option === 'object' ? option.value : option;
        const optionLabel = typeof option === 'object' ? (option.label ?? option.value) : option;
        return `<option value="${escapeHtml(optionValue)}" ${String(value) === String(optionValue) ? 'selected' : ''}>${escapeHtml(optionLabel)}</option>`;
      }).join('')}</select>`);
  }
  if (spec.type === 'int' || spec.type === 'float') {
    return field(`<input type="number" data-config="${escapeHtml(key)}" data-kind="${escapeHtml(spec.type)}"
      value="${escapeHtml(value ?? '')}" min="${spec.min ?? ''}" max="${spec.max ?? ''}" step="${spec.type === 'float' ? '0.1' : '1'}">`);
  }
  return field(`<input type="text" data-config="${escapeHtml(key)}" data-kind="string" value="${escapeHtml(value ?? '')}">`);
}

function renderAdvancedField(key, spec, device) {
  const label = escapeHtml(spec.label || prettyName(key));
  const value = key in (device.desired || {}) ? device.desired[key] : (spec.default ?? '');
  if (spec.control === 'slider' || spec.control === 'number') {
    return `<label class="field"><span>${label}${spec.unit ? ` (${escapeHtml(spec.unit)})` : ''}</span>
      <input type="number" data-prop-advanced="${escapeHtml(key)}" data-kind="${escapeHtml(spec.type)}"
             value="${escapeHtml(value)}" min="${spec.min ?? ''}" max="${spec.max ?? ''}"
             step="${spec.type === 'float' ? '0.1' : '1'}"></label>`;
  }
  return `<label class="field"><span>${label}</span>
    <input type="text" data-prop-advanced="${escapeHtml(key)}" data-kind="string" value="${escapeHtml(value)}"></label>`;
}

function wireDeviceDialog(dialog, device, type) {
  for (const tab of $$('[data-tab]', dialog)) {
    tab.addEventListener('click', () => {
      for (const other of $$('[data-tab]', dialog)) other.setAttribute('aria-selected', String(other === tab));
      for (const panel of $$('[data-panel]', dialog)) panel.hidden = panel.dataset.panel !== tab.dataset.tab;
      if (tab.dataset.tab === 'advanced') loadHistory(device.uid);
    });
  }
}

async function loadHistory(uid) {
  const target = $('#dialog [data-history]');
  if (!target) return;
  try {
    const data = await api('/api/devices/' + encodeURIComponent(uid) + '/history?hours=24');
    if (!data.points || !data.points.length) {
      target.textContent = 'No history yet.';
      return;
    }
    target.innerHTML = renderSparkline(data.points);
  } catch (error) {
    target.textContent = error.message;
  }
}

function renderSparkline(points) {
  const series = points.map((point) => {
    const values = point.values.v || point.values;
    if (typeof values.temperature === 'number') return values.temperature;
    if (typeof values.power === 'boolean') return values.power ? 1 : 0;
    if (typeof values.brightness === 'number') return values.brightness;
    return null;
  });
  const usable = series.filter((value) => value !== null);
  if (usable.length < 2) return 'Not enough data points yet.';
  const low = Math.min(...usable);
  const high = Math.max(...usable);
  const span = high - low || 1;
  const width = 100;
  const height = 32;
  const step = width / (points.length - 1 || 1);
  const coords = series.map((value, index) => {
    const y = height - ((value === null ? low : value) - low) / span * (height - 4) - 2;
    return [index * step, y];
  });
  const line = coords.map(([x, y], index) => (index ? 'L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2)).join(' ');
  const area = line + ` L${width} ${height} L0 ${height} Z`;
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <path class="area" d="${area}"></path><path class="line" d="${line}"></path>
    </svg>
    <div class="muted" style="font-size:12px">${fmtNumber(low)} – ${fmtNumber(high)} over ${points.length} samples</div>`;
}

async function saveDevice(uid) {
  const dialog = $('#dialog');
  const name = $('[data-field="name"]', dialog).value.trim();
  const room = $('[data-field="room"]', dialog).value.trim();
  const nodeId = $('[data-field="node_id"]', dialog).value;
  const config = {};
  for (const input of $$('[data-config]', dialog)) {
    const kind = input.dataset.kind;
    let value = input.value;
    if (kind === 'bool') value = value === 'true';
    else if (kind === 'int' || kind === 'float') value = value === '' ? undefined : Number(value);
    if (value !== undefined) config[input.dataset.config] = value;
  }
  const payload = { name, room, node_id: nodeId || null };
  if (Object.keys(config).length) payload.config = config;

  const advanced = {};
  for (const input of $$('[data-prop-advanced]', dialog)) {
    if (input.value === '') continue;
    advanced[input.dataset.propAdvanced] = input.dataset.kind === 'int' || input.dataset.kind === 'float'
      ? Number(input.value) : input.value;
  }

  try {
    let device = await api('/api/devices/' + encodeURIComponent(uid), { method: 'PATCH', body: payload });
    if (Object.keys(advanced).length) {
      device = await api('/api/devices/' + encodeURIComponent(uid) + '/props', {
        method: 'POST', body: { props: advanced },
      });
    }
    state.devices.set(uid, device);
    closeDialog();
    renderDevices();
    showToast('Saved', 'ok');
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function deleteDevice(uid) {
  const device = state.devices.get(uid);
  if (!device) return;
  if (!confirm(`Delete “${device.name}”?\n\nThe microcontroller will announce it again if it still reports this device.`)) return;
  try {
    await api('/api/devices/' + encodeURIComponent(uid), { method: 'DELETE' });
    state.devices.delete(uid);
    closeDialog();
    renderAll();
    showToast('Device deleted', 'ok');
  } catch (error) {
    showToast(error.message, 'error');
  }
}

/* ---- argument prompt ---------------------------------------------------- */
function promptArgs(uid, action, spec) {
  return new Promise((resolve) => {
    const rows = Object.entries(spec.args || {}).map(([key, arg]) => {
      const label = escapeHtml(arg.label || prettyName(key));
      const value = arg.default ?? '';
      if (arg.type === 'bool') {
        return `<label class="field"><span>${label}</span>
          <select data-arg="${escapeHtml(key)}" data-kind="bool">
            <option value="true" ${arg.default ? 'selected' : ''}>Yes</option>
            <option value="false" ${arg.default ? '' : 'selected'}>No</option></select></label>`;
      }
      if (arg.type === 'enum') {
        return `<label class="field"><span>${label}</span><select data-arg="${escapeHtml(key)}" data-kind="enum">
          ${(arg.options || []).map((option) => {
            const optionValue = typeof option === 'object' ? option.value : option;
            const optionLabel = typeof option === 'object' ? (option.label ?? option.value) : option;
            return `<option value="${escapeHtml(optionValue)}" ${String(value) === String(optionValue) ? 'selected' : ''}>${escapeHtml(optionLabel)}</option>`;
          }).join('')}</select></label>`;
      }
      if (arg.type === 'int' || arg.type === 'float') {
        return `<label class="field"><span>${label}${arg.unit ? ` (${escapeHtml(arg.unit)})` : ''}</span>
          <input type="number" data-arg="${escapeHtml(key)}" data-kind="${escapeHtml(arg.type)}"
                 value="${escapeHtml(value)}" min="${arg.min ?? ''}" max="${arg.max ?? ''}"
                 step="${arg.type === 'float' ? '0.1' : '1'}"></label>`;
      }
      return `<label class="field"><span>${label}</span>
        <input type="text" data-arg="${escapeHtml(key)}" data-kind="string" value="${escapeHtml(value)}"></label>`;
    }).join('');

    openDialog({
      title: spec.label || prettyName(action),
      kind: 'args',
      body: rows,
      actions: [
        { label: 'Cancel', onClick: () => { closeDialog(); resolve(null); } },
        {
          label: 'Run', style: 'primary', onClick: (button) => {
            const args = {};
            for (const input of $$('[data-arg]')) {
              const kind = input.dataset.kind;
              args[input.dataset.arg] = kind === 'bool' ? input.value === 'true'
                : (kind === 'int' || kind === 'float') ? Number(input.value) : input.value;
            }
            closeDialog();
            resolve(args);
          },
        },
      ],
    });
  });
}

/* ---- add device dialog -------------------------------------------------- */
function openAddDialog() {
  const types = [...state.types.values()];
  if (!types.length) {
    showToast('No device types available', 'error');
    return;
  }
  const body = document.createElement('div');
  body.innerHTML = `
    <div class="field">
      <span>Type</span>
      <div class="chip-row" data-types>
        ${types.map((type, index) => `<button type="button" class="chip btnlike" data-type="${escapeHtml(type.id)}" aria-pressed="${index === 0}">
          ${escapeHtml(type.name)}</button>`).join('')}
      </div>
      <span class="hint" data-type-hint>${escapeHtml(types[0].description || '')}</span>
    </div>
    <div class="field-row">
      <label class="field"><span>Name</span>
        <input type="text" data-field="name" placeholder="e.g. Desk light" maxlength="64">
      </label>
      <label class="field"><span>Room</span>
        <input type="text" data-field="room" placeholder="e.g. Office" maxlength="48" list="room-list-add">
        <datalist id="room-list-add">${[...new Set([...state.devices.values()].map(roomOf).filter(Boolean))]
          .map((room) => `<option value="${escapeHtml(room)}">`).join('')}</datalist>
      </label>
    </div>
    <label class="field"><span>Device ID</span>
      <input type="text" data-field="uid" placeholder="auto" maxlength="64">
      <span class="hint">Must match the <code>uid</code> used in the board's firmware. Empty = auto-generated (for a device you create before it connects).</span>
    </label>
    <label class="field"><span>Node</span>
      <select data-field="node_id">
        <option value="">— unassigned (bind when it connects) —</option>
        ${[...state.nodes.values()].map((node) =>
          `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.node_id)}${node.online ? ' (online)' : ''}</option>`).join('')}
      </select>
    </label>
    <div class="field-row" data-config-fields></div>`;

  const dialog = openDialog({
    title: 'Add a device',
    body,
    kind: 'add',
    actions: [
      { label: 'Cancel', onClick: () => closeDialog() },
      { label: 'Add device', style: 'primary', id: 'add-confirm', onClick: () => createDevice() },
    ],
  });

  let selectedType = types[0].id;
  const selectType = (id) => {
    selectedType = id;
    for (const chip of $$('[data-type]', dialog)) chip.setAttribute('aria-pressed', String(chip.dataset.type === id));
    const type = state.types.get(id);
    $('[data-type-hint]', dialog).textContent = type.description || '';
    const configFields = $('[data-config-fields]', dialog);
    configFields.innerHTML = Object.entries(type.config || {}).map(([key, spec]) => {
      const label = escapeHtml(spec.label || prettyName(key));
      if (spec.type === 'bool') {
        return `<label class="field"><span>${label}</span>
          <select data-config="${escapeHtml(key)}" data-kind="bool">
            <option value="false" ${spec.default ? '' : 'selected'}>No</option>
            <option value="true" ${spec.default ? 'selected' : ''}>Yes</option></select></label>`;
      }
      if (spec.type === 'int' || spec.type === 'float') {
        return `<label class="field"><span>${label}${spec.unit ? ` (${escapeHtml(spec.unit)})` : ''}</span>
          <input type="number" data-config="${escapeHtml(key)}" data-kind="${escapeHtml(spec.type)}"
                 value="${escapeHtml(spec.default ?? '')}" min="${spec.min ?? ''}" max="${spec.max ?? ''}"
                 step="${spec.type === 'float' ? '0.1' : '1'}"></label>`;
      }
      return `<label class="field"><span>${label}</span>
        <input type="text" data-config="${escapeHtml(key)}" data-kind="string" value="${escapeHtml(spec.default ?? '')}"></label>`;
    }).join('');
  };
  for (const chip of $$('[data-type]', dialog)) {
    chip.addEventListener('click', () => selectType(chip.dataset.type));
  }
  selectType(selectedType);
  dialog._selectedType = () => selectedType;
}

async function createDevice() {
  const dialog = $('#dialog');
  const type = dialog._selectedType();
  const name = $('[data-field="name"]', dialog).value.trim();
  const room = $('[data-field="room"]', dialog).value.trim();
  const uid = $('[data-field="uid"]', dialog).value.trim();
  const nodeId = $('[data-field="node_id"]', dialog).value;
  const config = {};
  for (const input of $$('[data-config]', dialog)) {
    const kind = input.dataset.kind;
    if (input.value === '') continue;
    config[input.dataset.config] = kind === 'bool' ? input.value === 'true'
      : (kind === 'int' || kind === 'float') ? Number(input.value) : input.value;
  }
  try {
    const device = await api('/api/devices', {
      method: 'POST',
      body: { type, name: name || undefined, room, uid: uid || undefined, node_id: nodeId || undefined, config },
    });
    state.devices.set(device.uid, device);
    closeDialog();
    renderAll();
    showToast('Device added', 'ok');
  } catch (error) {
    showToast(error.message, 'error');
  }
}

/* ---- settings dialog ---------------------------------------------------- */
function openSettingsDialog() {
  const body = document.createElement('div');
  openDialog({
    title: 'Settings',
    body,
    kind: 'settings',
    actions: [
      { label: 'API key', onClick: showApiKey },
      { label: 'Sign out', onClick: signOut },
      { label: 'Save', style: 'primary', onClick: saveSettings },
    ],
  });
  paintSettingsDialog();
}

function paintSettingsDialog() {
  const body = $('#dialog-body');
  if (!body) return;
  const settings = state.settings;
  const nodes = [...state.nodes.values()];
  const enrollment = state.enrollment;
  body.innerHTML = `
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="true" data-tab="network">Network</button>
      <button role="tab" aria-selected="false" data-tab="nodes">Nodes</button>
      <button role="tab" aria-selected="false" data-tab="types">Device types</button>
    </div>
    <section data-panel="network">
      <div class="field-row">
        <label class="field"><span>Heartbeat interval (s)</span>
          <input type="number" data-setting="heartbeat_s" value="${settings.heartbeat_s}" min="1" max="3600" step="1">
          <span class="hint">How often the hub pings a node.</span></label>
        <label class="field"><span>Offline timeout (s)</span>
          <input type="number" data-setting="offline_after_s" value="${settings.offline_after_s}" min="3" max="86400" step="1">
          <span class="hint">Silence before a node is marked offline.</span></label>
      </div>
      <div class="field-row">
        <label class="field"><span>Command timeout (s)</span>
          <input type="number" data-setting="ack_timeout_s" value="${settings.ack_timeout_s}" min="0.5" max="60" step="0.5"></label>
        <label class="field"><span>Command retries</span>
          <input type="number" data-setting="ack_retries" value="${settings.ack_retries}" min="0" max="10" step="1"></label>
      </div>
      <div class="field-row">
        <label class="field"><span>Device title</span>
          <input type="text" data-setting="title" value="${escapeHtml(settings.title)}" maxlength="40"></label>
        <label class="field"><span>History (days)</span>
          <input type="number" data-setting="history_days" value="${settings.history_days}" min="1" max="3650" step="1"></label>
      </div>
    </section>
    <section data-panel="nodes" hidden>
      <div class="field">
        <span>Pair a new microcontroller</span>
        <div class="chip-row">
          <button class="btn sm primary" data-enroll ${enrollment.open ? 'disabled' : ''}>
            ${enrollment.open ? 'Enrolling (' + Math.round(enrollment.seconds_left) + 's)' : 'Allow new devices for 2 min'}
          </button>
          ${enrollment.open ? '<button class="btn sm" data-enroll-close>Stop</button>' : ''}
        </div>
        <span class="hint">While the window is open, a board with no token is issued one automatically.</span>
      </div>
      ${nodes.length ? nodes.map((node) => `
        <div class="chip-row" style="justify-content:space-between;border-top:1px solid var(--border);padding-top:10px">
          <div>
            <b>${escapeHtml(node.node_id)}</b>
            <div class="muted" style="font-size:12px">
              ${escapeHtml(node.board || 'unknown board')} · ${node.online ? 'online' : 'offline'}
              ${node.ip ? ' · ' + escapeHtml(node.ip) : ''}${node.rssi ? ' · ' + node.rssi + ' dBm' : ''}
              · ${node.device_count} device(s)
            </div>
          </div>
          <div class="chip-row">
            <button class="btn sm" data-node-token="${escapeHtml(node.node_id)}">Token</button>
            <button class="btn sm danger" data-node-delete="${escapeHtml(node.node_id)}">Forget</button>
          </div>
        </div>`).join('') : '<p class="muted">No nodes have connected yet.</p>'}
    </section>
    <section data-panel="types" hidden>
      <div class="chip-row">
        <button class="btn sm" data-types-reload>Reload manifests</button>
      </div>
      <div class="log-list">
        ${[...state.types.values()].map((type) => `
          <div class="log-item"><div class="when">v${type.version}</div>
            <div class="what"><b>${escapeHtml(type.name)}</b><small>${escapeHtml(type.id)}</small>
              <span class="muted" style="font-size:12px">${escapeHtml(type.description || '')}</span></div></div>`).join('')}
      </div>
      <p class="hint">Drop a new JSON manifest into <code>device_types/</code> on the Pi, then press reload.</p>
    </section>`;
  wireSettingsDialog(body);
}

function wireSettingsDialog(body) {
  for (const tab of $$('[data-tab]', body)) {
    tab.addEventListener('click', () => {
      for (const other of $$('[data-tab]', body)) other.setAttribute('aria-selected', String(other === tab));
      for (const panel of $$('[data-panel]', body)) panel.hidden = panel.dataset.panel !== tab.dataset.tab;
    });
  }
  const enrollButton = $('[data-enroll]', body);
  if (enrollButton) {
    enrollButton.addEventListener('click', async () => {
      try {
        const result = await api('/api/enroll', { method: 'POST', body: { seconds: 120 } });
        state.enrollment = { open: result.enrollment_open, seconds_left: result.enrollment_seconds_left };
        renderEnrollment();
        paintSettingsDialog();
        showToast('Enrolment open for 2 minutes', 'ok');
      } catch (error) { showToast(error.message, 'error'); }
    });
  }
  const stopButton = $('[data-enroll-close]', body);
  if (stopButton) {
    stopButton.addEventListener('click', async () => {
      try {
        const result = await api('/api/enroll', { method: 'POST', body: { close: true } });
        state.enrollment = { open: result.enrollment_open, seconds_left: result.enrollment_seconds_left };
        renderEnrollment();
        paintSettingsDialog();
      } catch (error) { showToast(error.message, 'error'); }
    });
  }
  for (const button of $$('[data-node-token]', body)) {
    button.addEventListener('click', () => showNodeToken(button.dataset.nodeToken));
  }
  for (const button of $$('[data-node-delete]', body)) {
    button.addEventListener('click', async () => {
      const nodeId = button.dataset.nodeDelete;
      if (!confirm(`Forget node “${nodeId}”?\n\nIts token is revoked and its devices become unassigned.`)) return;
      try {
        await api('/api/nodes/' + encodeURIComponent(nodeId), { method: 'DELETE' });
        state.nodes.delete(nodeId);
        await refreshFromApi();
        paintSettingsDialog();
      } catch (error) { showToast(error.message, 'error'); }
    });
  }
  const reloadButton = $('[data-types-reload]', body);
  if (reloadButton) {
    reloadButton.addEventListener('click', async () => {
      try {
        const result = await api('/api/types/reload', { method: 'POST' });
        applyTypes(result.types, result.errors);
        renderAll();
        paintSettingsDialog();
        showToast('Device types reloaded', 'ok');
      } catch (error) { showToast(error.message, 'error'); }
    });
  }
}

async function saveSettings() {
  const values = {};
  for (const input of $$('[data-setting]')) {
    if (input.value === '') continue;
    values[input.dataset.setting] = input.type === 'number' ? Number(input.value) : input.value;
  }
  try {
    const result = await api('/api/settings', { method: 'PATCH', body: values });
    state.settings = result.settings;
    applySettings();
    closeDialog();
    showToast('Settings saved', 'ok');
  } catch (error) { showToast(error.message, 'error'); }
}

async function showNodeToken(nodeId) {
  try {
    const result = await api('/api/nodes/' + encodeURIComponent(nodeId) + '/token', { method: 'POST', body: {} });
    const snippet = `{"node_id": "${nodeId}", "token": "${result.token}"}`;
    openDialog({
      title: 'Token for ' + nodeId,
      kind: 'token',
      body: `<p class="muted" style="margin:0;font-size:13px">Paste this into the board's <code>/config.json</code>
              (any existing token for this node stops working).</p>
            <div class="token-box">${escapeHtml(result.token)}</div>
            <div class="snippet">${escapeHtml(snippet)}<button class="btn sm snippet-copy" data-copy>Copy</button></div>`,
      actions: [{ label: 'Done', style: 'primary', onClick: () => closeDialog() }],
    });
    $('[data-copy]').addEventListener('click', () => copyText(snippet));
  } catch (error) { showToast(error.message, 'error'); }
}

async function showApiKey() {
  try {
    const info = await api('/api/settings');
    const key = prompt('API key (use as ?key=… or the X-API-Key header):', '');
    if (key === null) return;
  } catch (error) { showToast(error.message, 'error'); }
}

async function signOut() {
  try { await api('/api/logout', { method: 'POST' }); } catch { /* ignore */ }
  location.replace('/');
}

/* ---- activity log ------------------------------------------------------- */
async function openEventsDialog() {
  const body = document.createElement('div');
  body.innerHTML = '<p class="muted" style="font-size:13px">Loading…</p>';
  openDialog({ title: 'Activity', body, kind: 'events' });
  try {
    const data = await api('/api/events?limit=80');
    const items = (data.events || []).map((event) => `
      <div class="log-item">
        <div class="when">${clockTime(event.ts)}</div>
        <div class="what"><b>${escapeHtml(event.name || event.uid || event.kind)}</b>
          <span class="muted" style="font-size:12px">${escapeHtml(eventText(event))}</span></div>
      </div>`).join('');
    body.innerHTML = items ? `<div class="log-list">${items}</div>` : '<p class="muted">Nothing yet.</p>';
  } catch (error) {
    body.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
  }
}

function eventText(event) {
  if (!event) return '';
  const data = event.data || {};
  if (event.kind === 'device_event') {
    const detail = data.data && Object.keys(data.data).length ? ' ' + JSON.stringify(data.data) : '';
    return (data.event || 'event') + detail;
  }
  if (event.kind === 'action') return 'ran ' + (data.action || 'action');
  if (event.kind === 'node_online') return 'node connected' + (data.ip ? ' from ' + data.ip : '');
  if (event.kind === 'node_offline') return 'node offline';
  if (event.kind === 'device_joined') return 'device joined';
  if (event.kind === 'device_created') return 'device created';
  if (event.kind === 'device_updated') return 'device updated';
  if (event.kind === 'device_deleted') return 'device deleted';
  return event.kind;
}

/* ----------------------------------------------------------------- toast -- */
function showToast(text, level = 'info') {
  if (!text) return;
  const container = $('#toasts');
  const toast = document.createElement('div');
  toast.className = 'toast ' + (level === 'error' ? 'error' : level === 'ok' ? 'ok' : '');
  toast.textContent = text;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('out');
    setTimeout(() => toast.remove(), 200);
  }, level === 'error' ? 5000 : 2600);
  while (container.children.length > 4) container.firstElementChild.remove();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast('Copied', 'ok');
  } catch {
    showToast('Copy failed — select the text manually', 'error');
  }
}

/* ------------------------------------------------------------ enrollment -- */
function renderEnrollment() {
  const chip = $('#stat-enroll');
  const open = state.enrollment.open && state.enrollment.seconds_left > 0;
  chip.hidden = !open;
  if (open) $('#enroll-left').textContent = String(Math.max(0, Math.round(state.enrollment.seconds_left)));
}

function tickEnrollment() {
  if (!state.enrollment.open) return;
  state.enrollment.seconds_left = Math.max(0, state.enrollment.seconds_left - 1);
  if (state.enrollment.seconds_left <= 0) state.enrollment.open = false;
  renderEnrollment();
}

/* ----------------------------------------------------------------- theme -- */
function applyTheme(theme) {
  if (!theme || theme === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('smarthome-theme', theme); } catch { /* ignore */ }
}

function initTheme() {
  let stored = 'auto';
  try { stored = localStorage.getItem('smarthome-theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(stored);
}

function cycleTheme() {
  const current = document.documentElement.dataset.theme || 'auto';
  applyTheme(current === 'auto' ? 'dark' : current === 'dark' ? 'light' : 'auto');
}

/* ------------------------------------------------------------------ init -- */
async function boot() {
  initTheme();
  try {
    const me = await api('/api/me');
    if (!me.authenticated) {
      location.replace('/');
      return;
    }
  } catch {
    location.replace('/');
    return;
  }

  $('#app').hidden = false;
  wireChrome();
  await refreshFromApi();
  connectSocket();
  setInterval(tickEnrollment, 1000);
  setInterval(applySettings, 30000);
  // A short poll keeps "online/offline" honest if the socket is wedged.
  setInterval(() => {
    if (!state.connected) refreshFromApi();
  }, 15000);
}

function wireChrome() {
  $('#btn-refresh').addEventListener('click', () => {
    refreshFromApi();
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify({ t: 'resync' }));
    }
  });
  $('#btn-theme').addEventListener('click', cycleTheme);
  $('#btn-menu').addEventListener('click', openSettingsDialog);
  $('#btn-add').addEventListener('click', openAddDialog);
  $('#btn-empty-add').addEventListener('click', openAddDialog);
  $('#btn-empty-enroll').addEventListener('click', async () => {
    try {
      const result = await api('/api/enroll', { method: 'POST', body: { seconds: 120 } });
      state.enrollment = { open: result.enrollment_open, seconds_left: result.enrollment_seconds_left };
      renderEnrollment();
      showToast('Enrolment open for 2 minutes — power on the board now', 'ok');
    } catch (error) { showToast(error.message, 'error'); }
  });
  $('#btn-events').addEventListener('click', openEventsDialog);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !state.connected) {
      state.reconnectDelay = 1000;
      connectSocket();
    }
  });
  window.addEventListener('online', () => { state.reconnectDelay = 1000; connectSocket(); });
}

document.addEventListener('DOMContentLoaded', boot);

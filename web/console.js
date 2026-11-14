import { PttClient, api, bindPttButton, getToken, login, logout } from '/ptt-core.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const KIND = { radio: 'Radio', ipsc: 'Relais IP', smartphone: 'Mobile', console: 'Console', simulateur: 'Simulateur' };
const STATUS = { en_ligne: 'en ligne', degrade: 'dégradé', erreur: 'en erreur', arret: 'arrêté' };

let me;
let snapshot = null;
let map = null;
const markers = new Map();
let events = [];

async function start() {
  if (!getToken()) return showLogin();
  try {
    me = await api('/api/me');
  } catch {
    return showLogin();
  }
  if (!['operateur', 'admin'].includes(me.user.role)) {
    document.body.innerHTML = '<p style="padding:24px">La console est réservée aux opérateurs. <a href="/">Retour à l\'alternat</a></p>';
    return;
  }
  $('app').hidden = false;
  $('who').textContent = `${me.user.display_name} · ${me.user.role}`;
  $('logout').addEventListener('click', logout);

  const initial = await api('/api/state');
  events = initial.events || [];
  applyState(initial);
  renderEvents();
  initMap();
  loadRecordings();
  $('refresh-rec').addEventListener('click', loadRecordings);

  const sel = $('dispatch-channel');
  for (const c of me.channels) sel.add(new Option(c.name, c.id));
  const client = new PttClient({ mode: 'console', onEvent: (ev) => onClientEvent(ev, client) });
  bindPttButton($('ptt'), client);
  sel.addEventListener('change', () => client.join(sel.value));
  // Barre d'espace = alternat, sauf dans un champ de saisie
  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && !e.repeat && !['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(document.activeElement.tagName)) {
      e.preventDefault();
      client.startTalking();
    }
  });
  document.addEventListener('keyup', (e) => {
    if (e.code === 'Space' && !['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(document.activeElement.tagName)) {
      e.preventDefault();
      client.stopTalking();
    }
  });
  client.connect();

  $('patch-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const channels = [...document.querySelectorAll('#patch-checks input:checked')].map((i) => i.value);
    $('patch-error').hidden = true;
    try {
      await api('/api/patches', { method: 'POST', body: JSON.stringify({ channels }) });
      document.querySelectorAll('#patch-checks input').forEach((i) => { i.checked = false; });
    } catch (err) {
      $('patch-error').textContent = err.message;
      $('patch-error').hidden = false;
    }
  });
}

function onClientEvent(ev, client) {
  const ptt = $('ptt');
  switch (ev.type) {
    case 'welcome':
      $('dispatch-channel').value = ev.channel;
      break;
    case 'moved':
      $('dispatch-channel').value = ev.channel;
      break;
    case 'systeme':
      applyState(ev);
      break;
    case 'event':
      events.push(ev.event);
      if (events.length > 300) events = events.slice(-300);
      renderEvents();
      if (ev.event.kind === 'emission_fin') setTimeout(loadRecordings, 500);
      break;
    default:
      break;
  }
  if (ev.type === 'state' && ev.state) {
    $('link-dot').className = `dot ${ev.state === 'deconnecte' ? 'erreur' : 'en_ligne'}`;
    ptt.disabled = ev.state === 'deconnecte';
    ptt.className = `ptt ${ev.state}`;
    ptt.textContent = ev.state === 'emission' ? 'EN ÉMISSION' : 'MAINTENIR POUR PARLER';
    const s = $('dispatch-status');
    if (ev.state === 'reception') s.textContent = `${ev.talker} parle`;
    else if (ev.busy) s.textContent = `Canal occupé : ${ev.busy} parle`;
    else if (ev.reason === 'preempted') s.textContent = 'Interrompu par un poste prioritaire';
    else if (ev.state === 'deconnecte') s.textContent = 'Hors ligne, reconnexion…';
    else if (ev.state === 'emission') s.textContent = 'En émission';
    else s.textContent = "Barre d'espace : alternat";
  }
  if (ev.type === 'error') $('dispatch-status').textContent = ev.message;
}

function applyState(s) {
  if (!s || !s.channels) return;
  snapshot = s;
  renderChannels();
  renderPatches();
  renderMarkers();
}

function renderChannels() {
  const { channels, endpoints, floors, patches } = snapshot;
  const patchOf = (id) => patches.find((p) => p.channels.includes(id));
  $('channels').innerHTML = channels
    .map((c) => {
      const floor = floors.find((f) => f.channels.includes(c.id));
      const members = endpoints.filter((e) => e.channel === c.id);
      const patch = patchOf(c.id);
      return `<div class="channel ${floor ? 'active' : ''}">
        <div class="channel-head"><strong>${esc(c.name)}</strong>${patch ? `<span class="tag">relié n°${patch.id}</span>` : ''}</div>
        ${floor ? `<div class="talker">● ${esc(floor.talker_name)} émet (${floor.seconds} s)</div>` : ''}
        ${members.length ? members.map((e) => memberHtml(e, channels)).join('') : '<div class="muted" style="font-size:0.875rem">Aucun participant</div>'}
      </div>`;
    })
    .join('');
  $('channels').querySelectorAll('select[data-ep]').forEach((sel) =>
    sel.addEventListener('change', async () => {
      try {
        await api(`/api/endpoints/${encodeURIComponent(sel.dataset.ep)}/move`, { method: 'POST', body: JSON.stringify({ channel: sel.value }) });
      } catch (err) {
        alert(err.message);
      }
    }),
  );
  $('channels').querySelectorAll('button[data-capture]').forEach((b) =>
    b.addEventListener('click', () => api(`/api/ipsc/${encodeURIComponent(b.dataset.capture)}/capture`, { method: 'POST' }).catch((e) => alert(e.message))),
  );
  const checks = $('patch-checks');
  if (!checks.children.length) {
    checks.innerHTML = channels.map((c) => `<label><input type="checkbox" value="${esc(c.id)}"> ${esc(c.name)}</label>`).join('');
  }
}

function memberHtml(e, channels) {
  const options = channels.map((c) => `<option value="${esc(c.id)}" ${c.id === e.channel ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
  const level = e.level_dbfs != null ? ` · ${e.level_dbfs} dBFS` : '';
  const capture = e.kind === 'ipsc' && me.user.role === 'admin'
    ? `<button class="btn btn-small" type="button" data-capture="${esc(e.id)}" title="Capturer un appel modèle pour l'émission IPSC">Modèle</button>` : '';
  return `<div class="member" title="${esc(e.detail || '')}">
    <span class="dot ${esc(e.status)}" aria-label="${esc(STATUS[e.status] || e.status)}"></span>
    <span class="name">${esc(e.name)} <span class="tag">${esc(KIND[e.kind] || e.kind)}</span>${e.tx ? ' <span class="tag" style="color:#ffb3ab">TX</span>' : ''}
      <span class="muted" style="display:block;font-size:0.75rem">${esc(STATUS[e.status] || e.status)}${esc(level)}${e.detail ? ' – ' + esc(e.detail) : ''}</span></span>
    <span class="actions">${capture}
      <label class="sr-only" for="mv-${esc(e.id)}">Déplacer ${esc(e.name)}</label>
      <select id="mv-${esc(e.id)}" data-ep="${esc(e.id)}">${options}</select></span>
  </div>`;
}

function renderPatches() {
  const { patches, channels } = snapshot;
  const name = (id) => channels.find((c) => c.id === id)?.name || id;
  $('patches').innerHTML = patches.length
    ? patches.map((p) => `<div class="patch-row"><span>${p.channels.map((c) => esc(name(c))).join(' ↔ ')}</span>
        <button class="btn btn-small btn-danger" type="button" data-unpatch="${p.id}">Séparer</button></div>`).join('')
    : '<div class="muted" style="font-size:0.875rem">Aucune interconnexion active</div>';
  $('patches').querySelectorAll('button[data-unpatch]').forEach((b) =>
    b.addEventListener('click', () => api(`/api/patches/${b.dataset.unpatch}`, { method: 'DELETE' }).catch((e) => alert(e.message))),
  );
}

function renderEvents() {
  const recent = events.slice(-120).reverse();
  $('events').innerHTML = recent
    .map((e) => `<div class="event ${esc(e.level)}"><time>${new Date(e.ts * 1000).toLocaleTimeString('fr-FR')}</time><span>${esc(e.message)}</span></div>`)
    .join('') || '<div class="muted">Aucun événement</div>';
}

async function loadRecordings() {
  try {
    const list = await api('/api/recordings');
    $('recordings').innerHTML = list.slice(0, 30).map((r) => {
      const label = r.path.split('/').pop().replace('.wav', '').replace(/_/g, ' · ');
      return `<div class="rec"><span>${esc(r.path.split('/')[0])} ${esc(label)} (${r.seconds} s)</span>
        <audio controls preload="none" src="/api/recordings/file?path=${encodeURIComponent(r.path)}&token=${encodeURIComponent(getToken())}"></audio></div>`;
    }).join('') || '<div class="muted" style="font-size:0.875rem">Aucun enregistrement</div>';
  } catch (err) {
    $('recordings').textContent = err.message;
  }
}

function initMap() {
  if (!window.maplibregl) {
    $('map').innerHTML = '<p style="padding:16px" class="muted">Carte indisponible.</p>';
    return;
  }
  const m = me.map || {};
  map = new maplibregl.Map({
    container: 'map',
    center: m.center || [-5.55, 7.54],
    zoom: m.zoom || 6,
    attributionControl: { compact: true },
    style: {
      version: 8,
      sources: { fond: { type: 'raster', tiles: [m.tiles], tileSize: 256, attribution: m.attribution || '' } },
      layers: [
        { id: 'fond-uni', type: 'background', paint: { 'background-color': '#1a2630' } },
        { id: 'fond', type: 'raster', source: 'fond', paint: { 'raster-saturation': -0.6, 'raster-brightness-max': 0.75 } },
      ],
    },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
  // Sans accès au serveur de tuiles (réseau isolé), on l'indique une fois au lieu d'échouer en silence
  let tileErrors = 0;
  map.on('error', (e) => {
    if (e.sourceId === 'fond' && ++tileErrors === 3) {
      const note = document.createElement('div');
      note.className = 'map-note';
      note.textContent = 'Fond de carte indisponible (hors ligne) : positions affichées sans carte';
      document.querySelector('.map-wrap').appendChild(note);
    }
  });
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }));
  renderMarkers();
}

function renderMarkers() {
  if (!map || !snapshot) return;
  const seen = new Set();
  for (const p of snapshot.positions || []) {
    seen.add(p.user);
    let mk = markers.get(p.user);
    const age = Math.round((Date.now() / 1000 - p.ts) / 60);
    const html = `<strong>${esc(p.name)}</strong><br>${p.online ? 'en ligne' : 'hors ligne'} · il y a ${age} min${p.acc ? `<br>précision ±${p.acc} m` : ''}`;
    if (!mk) {
      const el = document.createElement('div');
      el.className = 'marker';
      el.setAttribute('aria-label', p.name);
      mk = new maplibregl.Marker({ element: el }).setLngLat([p.lon, p.lat]).setPopup(new maplibregl.Popup({ offset: 12 })).addTo(map);
      markers.set(p.user, mk);
    }
    mk.setLngLat([p.lon, p.lat]);
    mk.getPopup().setHTML(html);
    mk.getElement().className = `marker ${p.online ? '' : 'off'}`;
  }
  for (const [user, mk] of markers) {
    if (!seen.has(user)) { mk.remove(); markers.delete(user); }
  }
}

function showLogin() {
  $('login').hidden = false;
  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('login-error').hidden = true;
    try {
      await login($('username').value.trim(), $('password').value);
      location.reload();
    } catch (err) {
      $('login-error').textContent = err.message;
      $('login-error').hidden = false;
    }
  });
}

start();

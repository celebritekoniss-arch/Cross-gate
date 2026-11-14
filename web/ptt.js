import { PttClient, api, bindPttButton, getToken, login, logout } from '/ptt-core.js';

const $ = (id) => document.getElementById(id);

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

async function start() {
  if (!getToken()) return showLogin();
  let me;
  try {
    me = await api('/api/me');
  } catch {
    return showLogin();
  }
  $('app').hidden = false;
  $('who').textContent = me.user.display_name;
  $('console-link').hidden = !['operateur', 'admin'].includes(me.user.role);
  const select = $('channel');
  select.innerHTML = '';
  for (const c of me.channels) select.add(new Option(c.name, c.id));

  const client = new PttClient({ mode: 'ptt', onEvent: render });
  bindPttButton($('ptt'), client);
  select.addEventListener('change', () => client.join(select.value));
  $('logout').addEventListener('click', logout);
  client.connect();
  client.startGps();

  function setStatus(big, small = '', cls = '') {
    $('status-big').textContent = big;
    $('status-small').textContent = small;
    $('status').className = `status-card ${cls}`;
  }

  function render(ev) {
    const ptt = $('ptt');
    switch (ev.type) {
      case 'welcome':
        select.value = ev.channel;
        break;
      case 'moved':
        select.value = ev.channel;
        setStatus('Canal libre', `Canal ${select.selectedOptions[0]?.text || ''}`);
        break;
      case 'state': {
        $('link-dot').className = `dot ${ev.state === 'deconnecte' ? 'erreur' : 'en_ligne'}`;
        ptt.disabled = ev.state === 'deconnecte';
        ptt.className = `ptt ${ev.state}`;
        ptt.textContent = ev.state === 'emission' ? 'EN ÉMISSION' : 'PARLER';
        if (ev.state === 'deconnecte') setStatus('Hors ligne', 'Reconnexion automatique…', 'occupe');
        else if (ev.state === 'emission') setStatus('En émission', 'Relâchez pour écouter', 'emission');
        else if (ev.state === 'demande') setStatus('Demande de parole…');
        else if (ev.state === 'reception') setStatus(ev.talker, 'parle', 'reception');
        else if (ev.busy) setStatus('Canal occupé', `${ev.busy} parle`, 'occupe');
        else if (ev.reason === 'preempted') setStatus('Interrompu', `Priorité à ${ev.by || 'un poste prioritaire'}`, 'occupe');
        else if (ev.reason === 'timeout') setStatus('Émission coupée', 'Durée maximale atteinte', 'occupe');
        else setStatus('Canal libre', `Canal ${select.selectedOptions[0]?.text || ''}`);
        break;
      }
      case 'reconnect':
        $('status-small').textContent = `Nouvelle tentative dans ${Math.round(ev.delay / 1000)} s`;
        break;
      case 'level':
        $('meter').style.width = `${Math.min(100, ev.level * 140)}%`;
        break;
      case 'gps':
        $('gps').textContent = `Position : ±${Math.round(ev.acc)} m`;
        break;
      case 'gps_error':
        $('gps').textContent = 'Position : indisponible';
        break;
      case 'error':
        setStatus('Micro indisponible', ev.message, 'occupe');
        break;
    }
  }
}

function showLogin() {
  $('login').hidden = false;
  $('app').hidden = true;
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

// Client CrossGate commun au smartphone et à la console : connexion, alternat, audio, GPS.

const TOKEN_KEY = 'crossgate.token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function logout() {
  localStorage.removeItem(TOKEN_KEY);
  location.reload();
}

export async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken() || ''}`, ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== '/api/login') {
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
  }
  if (!res.ok) throw new Error(body.error || `Erreur ${res.status}`);
  return body;
}

export async function login(username, password) {
  const body = await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password }) });
  localStorage.setItem(TOKEN_KEY, body.token);
  return body;
}

export class PttClient {
  constructor({ mode = 'ptt', onEvent = () => {} } = {}) {
    this.mode = mode;
    this.onEvent = onEvent;
    this.ws = null;
    this.ctx = null;
    this.capture = null;
    this.playback = null;
    this.state = 'deconnecte'; // deconnecte | libre | demande | emission | reception
    this.wantTalk = false;
    this.retry = 0;
    this.closed = false;
    this.lastGps = 0;
  }

  emit(type, data = {}) {
    this.onEvent({ type, ...data });
  }

  setState(s, extra = {}) {
    this.state = s;
    this.emit('state', { state: s, ...extra });
  }

  // ---------------------------------------------------------------- connexion
  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(getToken())}&mode=${this.mode}`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; };
    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        if (this.playback) this.playback.port.postMessage({ frame: e.data }, [e.data]);
        return;
      }
      this.handle(JSON.parse(e.data));
    };
    ws.onclose = (e) => {
      this.stopTalking(false);
      this.setState('deconnecte');
      if (e.code === 4401) { logout(); return; }
      if (this.closed) return;
      // Reconnexion progressive : 1, 2, 4, 8 puis 15 s (pas de rafale de tentatives)
      const delay = Math.min(15000, 1000 * 2 ** this.retry++);
      this.emit('reconnect', { delay });
      setTimeout(() => this.connect(), delay);
    };
    clearInterval(this.pinger);
    this.pinger = setInterval(() => this.send({ t: 'ping' }), 20000);
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  handle(m) {
    switch (m.t) {
      case 'welcome':
        this.channel = m.channel;
        this.emit('welcome', m);
        this.setState('libre');
        break;
      case 'floor':
        if (m.state === 'granted') {
          if (!this.wantTalk) { this.send({ t: 'ptt', on: false }); return; }
          this.beep([1250], 0.07);
          this.capture && this.capture.port.postMessage({ active: true });
          this.setState('emission');
        } else if (m.state === 'denied') {
          this.wantTalk = false;
          this.beep([420, 0, 420], 0.09);
          this.setState('libre', { busy: m.holder });
        } else {
          // released, preempted, timeout
          this.capture && this.capture.port.postMessage({ active: false });
          if (m.state !== 'released') this.beep([500, 0, 380], 0.12);
          this.wantTalk = false;
          this.setState('libre', { reason: m.state, by: m.by });
        }
        break;
      case 'rx':
        this.playback && this.playback.port.postMessage({ clear: true });
        this.setState('reception', { talker: m.talker });
        break;
      case 'rx_end':
        this.playback && this.playback.port.postMessage({ flush: true });
        this.beep([900], 0.05, 0.25);
        if (this.state === 'reception') this.setState('libre');
        break;
      case 'moved':
        this.channel = m.channel;
        this.emit('moved', m);
        break;
      default:
        this.emit(m.t, m);
    }
  }

  join(channel) {
    this.send({ t: 'join', channel });
  }

  // ---------------------------------------------------------------- audio
  async initAudio() {
    if (this.ctx) {
      if (this.ctx.state !== 'running') await this.ctx.resume();
      return;
    }
    if (!window.isSecureContext) throw new Error('Le micro exige une connexion HTTPS.');
    if (!window.AudioWorkletNode || !navigator.mediaDevices?.getUserMedia) {
      // Terminaux anciens (Android 4/5, navigateur intégré) : utiliser le raccordement en radio donneuse
      throw new Error('Navigateur trop ancien pour l\'alternat. Installez un Chrome récent, ou raccordez ce terminal en radio donneuse.');
    }
    this.ctx = new AudioContext({ latencyHint: 'interactive' });
    await this.ctx.audioWorklet.addModule('/audio-worklet.js');
    this.playback = new AudioWorkletNode(this.ctx, 'cg-playback', { outputChannelCount: [1] });
    this.playback.connect(this.ctx.destination);
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const src = this.ctx.createMediaStreamSource(stream);
    this.capture = new AudioWorkletNode(this.ctx, 'cg-capture');
    this.capture.port.onmessage = (e) => {
      if (e.data.frame && this.state === 'emission' && this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(e.data.frame);
      } else if ('level' in e.data) {
        this.emit('level', { level: e.data.level });
      }
    };
    src.connect(this.capture);
    // Le nœud de capture doit être relié à la sortie pour être cadencé (gain nul : rien n'est entendu)
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    this.capture.connect(mute).connect(this.ctx.destination);
    if (this.ctx.state !== 'running') await this.ctx.resume();
    this.keepAwake();
  }

  beep(freqs, dur = 0.08, vol = 0.18) {
    if (!this.ctx) return;
    let t = this.ctx.currentTime;
    for (const f of freqs) {
      if (f) {
        const o = this.ctx.createOscillator();
        const g = this.ctx.createGain();
        o.frequency.value = f;
        g.gain.setValueAtTime(vol, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + dur);
        o.connect(g).connect(this.ctx.destination);
        o.start(t);
        o.stop(t + dur);
      }
      t += dur + 0.03;
    }
  }

  async keepAwake() {
    try {
      if ('wakeLock' in navigator && !this.wakeLock) {
        this.wakeLock = await navigator.wakeLock.request('screen');
        this.wakeLock.addEventListener('release', () => { this.wakeLock = null; });
      }
    } catch { /* non supporté */ }
  }

  // ---------------------------------------------------------------- alternat
  async startTalking() {
    if (this.state === 'deconnecte' || this.wantTalk) return;
    try {
      await this.initAudio();
    } catch (e) {
      this.emit('error', { message: e.message || String(e) });
      return;
    }
    this.wantTalk = true;
    this.setState('demande');
    this.send({ t: 'ptt', on: true });
  }

  stopTalking(notify = true) {
    const was = this.wantTalk || this.state === 'emission' || this.state === 'demande';
    this.wantTalk = false;
    this.capture && this.capture.port.postMessage({ active: false });
    if (was && notify) {
      this.send({ t: 'ptt', on: false });
      if (this.state === 'emission' || this.state === 'demande') this.setState('libre');
    }
  }

  // ---------------------------------------------------------------- position
  startGps() {
    if (!('geolocation' in navigator)) return;
    navigator.geolocation.watchPosition(
      (pos) => {
        const now = Date.now();
        if (now - this.lastGps < 10000) return; // au plus une position toutes les 10 s
        this.lastGps = now;
        const { latitude: lat, longitude: lon, accuracy: acc } = pos.coords;
        this.send({ t: 'gps', lat, lon, acc: Math.round(acc) });
        this.emit('gps', { lat, lon, acc });
      },
      (err) => this.emit('gps_error', { message: err.message }),
      { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 },
    );
  }
}

// Bouton d'alternat robuste au tactile : appui maintenu, relâché même si le doigt glisse hors du bouton
export function bindPttButton(button, client) {
  const down = (e) => {
    e.preventDefault();
    if (e.pointerId !== undefined) button.setPointerCapture(e.pointerId);
    client.startTalking();
  };
  const up = (e) => {
    e.preventDefault();
    client.stopTalking();
  };
  button.addEventListener('pointerdown', down);
  button.addEventListener('pointerup', up);
  button.addEventListener('pointercancel', up);
  button.addEventListener('lostpointercapture', up);
  button.addEventListener('contextmenu', (e) => e.preventDefault());
  button.addEventListener('keydown', (e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) down(e); });
  button.addEventListener('keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') up(e); });
}

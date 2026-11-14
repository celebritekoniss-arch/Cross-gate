// Moteur audio temps réel de CrossGate (s'exécute dans le thread audio du navigateur).
// Format réseau : PCM 16 bits mono 8 kHz, trames de 20 ms (160 échantillons).

const NET_RATE = 8000;
const FRAME = 160;

// Filtre passe-bas biquadratique (Butterworth) : coupe au-dessus de la bande voix avant décimation
function lowpass(rate, cutoff) {
  const w = (2 * Math.PI * cutoff) / rate;
  const alpha = Math.sin(w) / Math.SQRT2;
  const cos = Math.cos(w);
  const a0 = 1 + alpha;
  return {
    b0: (1 - cos) / 2 / a0, b1: (1 - cos) / a0, b2: (1 - cos) / 2 / a0,
    a1: (-2 * cos) / a0, a2: (1 - alpha) / a0,
    x1: 0, x2: 0, y1: 0, y2: 0,
  };
}
function biquad(f, x) {
  const y = f.b0 * x + f.b1 * f.x1 + f.b2 * f.x2 - f.a1 * f.y1 - f.a2 * f.y2;
  f.x2 = f.x1; f.x1 = x; f.y2 = f.y1; f.y1 = y;
  return y;
}

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / NET_RATE;
    this.pos = 0;
    this.prev = 0;
    this.filters = [lowpass(sampleRate, 3400), lowpass(sampleRate, 3400)]; // 4e ordre
    this.out = new Int16Array(FRAME);
    this.n = 0;
    this.active = false;
    this.peak = 0;
    this.port.onmessage = (e) => {
      if ('active' in e.data) {
        this.active = e.data.active;
        this.n = 0;
      }
    };
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const x = biquad(this.filters[1], biquad(this.filters[0], input[i]));
      // Rééchantillonnage fractionnaire par interpolation linéaire
      while (this.pos < 1) {
        const s = this.prev + (x - this.prev) * this.pos;
        this.pos += this.step;
        const v = Math.max(-1, Math.min(1, s));
        this.peak = Math.max(this.peak, Math.abs(v));
        if (this.active) {
          this.out[this.n++] = v * 32767;
          if (this.n === FRAME) {
            this.port.postMessage({ frame: this.out.buffer }, [this.out.buffer]);
            this.out = new Int16Array(FRAME);
            this.n = 0;
          }
        }
      }
      this.pos -= 1;
      this.prev = x;
    }
    if (currentFrame % 4800 < 128) {
      this.port.postMessage({ level: this.peak });
      this.peak = 0;
    }
    return true;
  }
}

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = sampleRate * 2;
    this.buf = new Float32Array(this.size);
    this.r = 0;
    this.w = 0;
    this.count = 0;
    this.buffering = true;
    this.prebuffer = Math.round(sampleRate * 0.08); // 80 ms de tampon anti-gigue
    this.ratio = sampleRate / NET_RATE;
    this.last = 0;
    this.port.onmessage = (e) => {
      if (e.data.frame) this.push(new Int16Array(e.data.frame));
      if (e.data.flush) this.buffering = false; // fin d'émission : jouer ce qui reste
      if (e.data.clear) { this.count = 0; this.r = this.w = 0; this.buffering = true; }
    };
  }

  push(frame) {
    // Suréchantillonnage linéaire 8 kHz → fréquence du contexte audio
    const total = Math.round(frame.length * this.ratio);
    for (let i = 0; i < total; i++) {
      const p = i / this.ratio;
      const k = Math.floor(p);
      const a = k === 0 ? this.last : frame[k - 1] / 32768;
      const b = frame[Math.min(k, frame.length - 1)] / 32768;
      const v = a + (b - a) * (p - k);
      if (this.count >= this.size) { this.r = (this.r + 1) % this.size; this.count--; } // débordement : on jette l'ancien
      this.buf[this.w] = v;
      this.w = (this.w + 1) % this.size;
      this.count++;
    }
    this.last = frame[frame.length - 1] / 32768;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (this.buffering && this.count >= this.prebuffer) this.buffering = false;
    for (let i = 0; i < out.length; i++) {
      if (this.buffering || this.count === 0) {
        out[i] = 0;
        if (this.count === 0) this.buffering = true;
      } else {
        out[i] = this.buf[this.r];
        this.r = (this.r + 1) % this.size;
        this.count--;
      }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}

registerProcessor('cg-capture', CaptureProcessor);
registerProcessor('cg-playback', PlaybackProcessor);

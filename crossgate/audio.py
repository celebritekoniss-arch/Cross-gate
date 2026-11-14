"""Traitement audio commun : trames PCM 16 bits mono 8 kHz de 20 ms.

Toutes les sources (radios, smartphones, IPSC) sont converties dans ce format
pivot avant d'entrer dans le bus de routage.
"""

from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = FRAME_SAMPLES * 2  # 320

Frame = np.ndarray  # int16, longueur FRAME_SAMPLES


def silence() -> Frame:
    return np.zeros(FRAME_SAMPLES, dtype=np.int16)


def from_bytes(data: bytes) -> Frame:
    """Trame PCM little-endian (format du WebSocket smartphone)."""
    if len(data) != FRAME_BYTES:
        raise ValueError(f"Trame de {len(data)} octets, {FRAME_BYTES} attendus")
    return np.frombuffer(data, dtype="<i2").astype(np.int16)


def to_bytes(frame: Frame) -> bytes:
    return frame.astype("<i2").tobytes()


def level_dbfs(frame: Frame) -> float:
    """Niveau RMS en dBFS (−inf pour le silence absolu)."""
    if frame.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
    if rms <= 0:
        return -math.inf
    return 20 * math.log10(rms / 32768.0)


def apply_gain(frame: Frame, gain_db: float) -> Frame:
    if gain_db == 0:
        return frame
    g = 10 ** (gain_db / 20)
    return np.clip(frame.astype(np.float64) * g, -32768, 32767).astype(np.int16)


def tone(freq_hz: float, n_frames: int, amplitude: float = 0.3, phase: float = 0.0) -> list[Frame]:
    """Génère un bip (utilisé par le simulateur et les signaux de fin d'émission)."""
    total = n_frames * FRAME_SAMPLES
    t = (np.arange(total) / SAMPLE_RATE) + phase
    wave = (np.sin(2 * np.pi * freq_hz * t) * amplitude * 32767).astype(np.int16)
    return [wave[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] for i in range(n_frames)]


def _lowpass_taps(cutoff: float, numtaps: int = 63) -> np.ndarray:
    """Filtre passe-bas à fenêtre de Hamming. cutoff en fraction de Nyquist (0..1)."""
    n = np.arange(numtaps) - (numtaps - 1) / 2
    h = cutoff * np.sinc(cutoff * n) * np.hamming(numtaps)
    return h / h.sum()


class Resampler:
    """Rééchantillonneur à rapport entier (48 kHz ↔ 8 kHz, 16 kHz ↔ 8 kHz…), avec état
    pour traiter un flux continu bloc par bloc sans craquements aux jonctions."""

    def __init__(self, in_rate: int, out_rate: int, numtaps: int = 63):
        if in_rate == out_rate:
            self.mode = "same"
            return
        if in_rate % out_rate == 0:
            self.mode, self.ratio = "down", in_rate // out_rate
        elif out_rate % in_rate == 0:
            self.mode, self.ratio = "up", out_rate // in_rate
        else:
            raise ValueError(
                f"Rapport {in_rate}→{out_rate} Hz non entier : utilisez 8000, 16000, 24000 ou 48000 Hz"
            )
        self.taps = _lowpass_taps(1.0 / self.ratio, numtaps)
        if self.mode == "up":
            self.taps = self.taps * self.ratio  # compense l'insertion de zéros
        self.history = np.zeros(numtaps - 1, dtype=np.float64)
        self.phase = 0  # position de décimation conservée entre deux blocs

    def process(self, samples: np.ndarray) -> np.ndarray:
        if self.mode == "same":
            return samples.astype(np.int16)
        x = samples.astype(np.float64)
        if self.mode == "up":
            up = np.zeros(len(x) * self.ratio)
            up[:: self.ratio] = x
            x = up
        buf = np.concatenate([self.history, x])
        y = np.convolve(buf, self.taps, mode="valid")
        self.history = buf[len(buf) - (len(self.taps) - 1):]
        if self.mode == "down":
            start = self.phase
            out = y[start:: self.ratio]
            self.phase = (start - len(y)) % self.ratio
            y = out
        return np.clip(np.round(y), -32768, 32767).astype(np.int16)


class Framer:
    """Découpe un flux d'échantillons de longueur quelconque en trames de 20 ms."""

    def __init__(self) -> None:
        self.buf = np.zeros(0, dtype=np.int16)

    def push(self, samples: np.ndarray) -> list[Frame]:
        self.buf = np.concatenate([self.buf, samples.astype(np.int16)])
        frames = []
        while len(self.buf) >= FRAME_SAMPLES:
            frames.append(self.buf[:FRAME_SAMPLES].copy())
            self.buf = self.buf[FRAME_SAMPLES:]
        return frames

    def reset(self) -> None:
        self.buf = np.zeros(0, dtype=np.int16)

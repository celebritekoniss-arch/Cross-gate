"""Radio raccordée par interface audio : carte son USB + alternat (PTT) + détection de porteuse (COS).

C'est le mode de raccordement le plus fiable : la radio (DM4600 en station de contrôle,
Barrett 4090, poste analogique…) fait elle-même son codage, numérique ou analogique.

Semi-duplex : pendant que CrossGate émet sur la radio, l'audio reçu de la radio est ignoré
pour éviter les boucles.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from crossgate.audio import SAMPLE_RATE, Framer, Resampler, apply_gain, level_dbfs
from crossgate.endpoints.base import Endpoint

log = logging.getLogger("crossgate")


# ---------------------------------------------------------------- alternat


class NoPtt:
    def set(self, on: bool) -> None:
        pass

    def close(self) -> None:
        pass


class SerialPtt:
    """Alternat sur une ligne RTS ou DTR d'un port série (interface type RIGblaster, câble FTDI…)."""

    def __init__(self, port: str, line: str = "rts", invert: bool = False, ser=None):
        if ser is None:
            import serial

            ser = serial.Serial(port)
        self.ser, self.line, self.invert = ser, line, invert
        self.set(False)

    def set(self, on: bool) -> None:
        level = on != self.invert
        if self.line == "rts":
            self.ser.rts = level
        else:
            self.ser.dtr = level

    def close(self) -> None:
        try:
            self.set(False)
            self.ser.close()
        except Exception:
            pass


class Cm108Ptt:
    """Alternat sur une broche GPIO d'une carte son CM108/CM119 (interfaces DMK URI, RA-35…)."""

    def __init__(self, hidraw: str, gpio: int = 3, invert: bool = False):
        self.path, self.mask, self.invert = hidraw, 1 << (gpio - 1), invert
        self.fd = open(hidraw, "wb", buffering=0)
        self.set(False)

    def set(self, on: bool) -> None:
        level = on != self.invert
        # Rapport HID : [0, 0, direction, état, 0]
        self.fd.write(bytes([0, 0, self.mask, self.mask if level else 0, 0]))

    def close(self) -> None:
        try:
            self.set(False)
            self.fd.close()
        except Exception:
            pass


# ---------------------------------------------------------------- détection de porteuse


class VoxCos:
    """Détection d'activité par le niveau audio (pour les radios sans sortie COS)."""

    def __init__(self, threshold_dbfs: float, hang_frames: int = 10):
        self.threshold = threshold_dbfs
        self.hang_frames = hang_frames
        self.quiet = hang_frames

    def active(self, frame) -> bool:
        if level_dbfs(frame) >= self.threshold:
            self.quiet = 0
        else:
            self.quiet += 1
        return self.quiet < self.hang_frames


class SerialCos:
    """Sortie COS/squelch de la radio câblée sur CTS, DSR ou CD d'un port série."""

    def __init__(self, port: str, line: str, invert: bool = False, ser=None):
        if ser is None:
            import serial

            ser = serial.Serial(port)
        self.ser, self.attr, self.invert = ser, {"serie_cts": "cts", "serie_dsr": "dsr", "serie_cd": "cd"}[line], invert

    def active(self, frame) -> bool:
        return bool(getattr(self.ser, self.attr)) != self.invert


# ---------------------------------------------------------------- point de raccordement


class SoundcardEndpoint(Endpoint):
    kind = "radio"

    def __init__(self, cfg, sd_module=None):
        super().__init__(cfg.id, cfg.name, cfg.channel, cfg.priority, queue_frames=100)
        self.cfg = cfg
        self.sd = sd_module
        self.ptt = NoPtt()
        self.cos = None
        self.in_stream = None
        self.out_stream = None
        self.rx_resampler = Resampler(cfg.sample_rate, SAMPLE_RATE)
        self.tx_resampler = Resampler(SAMPLE_RATE, cfg.sample_rate)
        self.framer = Framer()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.transmitting = False
        self.tx_buffer = np.zeros(0, dtype=np.int16)
        self.receiving = False
        self.task: asyncio.Task | None = None
        self.level = -120.0

    async def start(self, bus) -> None:
        await super().start(bus)
        self.loop = asyncio.get_running_loop()
        try:
            if self.sd is None:
                import sounddevice  # PortAudio

                self.sd = sounddevice
            self._open_ptt()
            self._open_cos()
            block = self.cfg.sample_rate // 50  # 20 ms
            self.in_stream = self.sd.InputStream(
                device=self.cfg.input_device, samplerate=self.cfg.sample_rate, channels=1, dtype="int16",
                blocksize=block, callback=self._on_input,
            )
            self.out_stream = self.sd.OutputStream(
                device=self.cfg.output_device, samplerate=self.cfg.sample_rate, channels=1, dtype="int16",
                blocksize=block, callback=self._on_output,
            )
            self.in_stream.start()
            self.out_stream.start()
            self.task = asyncio.create_task(self._tx_loop())
            self.set_status("en_ligne", f"carte son {self.cfg.input_device or 'par défaut'}, alternat {self.cfg.ptt.type}, détection {self.cfg.cos.type}")
        except Exception as e:
            self.set_status("erreur", f"interface audio indisponible : {e}")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        for s in (self.in_stream, self.out_stream):
            if s:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self.ptt.close()
        await super().stop()

    def _open_ptt(self) -> None:
        c = self.cfg.ptt
        if c.type == "serie":
            if not c.port:
                raise ValueError("ptt.port manquant")
            self.ptt = SerialPtt(c.port, c.line, c.invert)
        elif c.type == "cm108":
            self.ptt = Cm108Ptt(c.port or "/dev/hidraw0", c.gpio, c.invert)

    def _open_cos(self) -> None:
        c = self.cfg.cos
        if c.type == "vox":
            # Maintien de la détection pendant les blancs de parole (hang time du bus, en trames de 20 ms)
            self.cos = VoxCos(c.threshold_dbfs, hang_frames=max(1, int(self.bus.hang * 50)) if self.bus else 40)
        else:
            if not c.port:
                raise ValueError("cos.port manquant")
            ser = getattr(self.ptt, "ser", None) if c.port == self.cfg.ptt.port else None
            self.cos = SerialCos(c.port, c.type, c.invert, ser=ser)

    # ----- réception (radio → bus) : appelé dans le thread audio
    def _on_input(self, indata, frames, time_info, status) -> None:
        samples = self.rx_resampler.process(indata[:, 0])
        for frame in self.framer.push(samples):
            if self.loop:
                self.loop.call_soon_threadsafe(self._handle_rx_frame, frame)

    def _handle_rx_frame(self, frame) -> None:
        assert self.bus and self.cos
        frame = apply_gain(frame, self.cfg.rx_gain_db)
        self.level = level_dbfs(frame)
        if self.transmitting:
            return
        if self.cos.active(frame):
            if self.bus.push_audio(self, frame, implicit=True):
                self.receiving = True
        elif self.receiving:
            self.receiving = False
            self.bus.end_audio(self)

    # ----- émission (bus → radio)
    def _on_output(self, outdata, frames, time_info, status) -> None:
        n = len(outdata)
        chunk = self.tx_buffer[:n]
        self.tx_buffer = self.tx_buffer[n:]
        outdata[:, 0] = 0
        outdata[: len(chunk), 0] = chunk

    async def _tx_loop(self) -> None:
        last_audio = 0.0
        tail = self.cfg.ptt.tail_ms / 1000
        while True:
            try:
                frame = await asyncio.wait_for(self.out_queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                frame = None  # rien à émettre pendant 50 ms
            if isinstance(frame, np.ndarray):
                if not self.transmitting:
                    self.transmitting = True
                    self.ptt.set(True)
                    await asyncio.sleep(self.cfg.ptt.pre_ms / 1000)
                out = self.tx_resampler.process(apply_gain(frame, self.cfg.tx_gain_db))
                self.tx_buffer = np.concatenate([self.tx_buffer, out])
                last_audio = time.monotonic()
            elif self.transmitting and len(self.tx_buffer) == 0 and time.monotonic() - last_audio > tail:
                self.ptt.set(False)
                self.transmitting = False

    def snapshot(self):
        snap = super().snapshot()
        snap["level_dbfs"] = None if self.level < -119 else round(self.level, 1)
        snap["tx"] = self.transmitting
        return snap

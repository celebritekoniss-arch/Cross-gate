"""Vocodeur AMBE+2 matériel (puce DVSI AMBE-3000 : ThumbDV, DVstick 30…).

La DMR numérique transporte la voix en AMBE+2, codec propriétaire : CrossGate ne le décode
pas en logiciel et s'appuie sur la puce DVSI. Sans boîtier, NullVocoder signale que la
liaison IPSC numérique est indisponible, et l'interface audio prend le relais.

Protocole série DVSI : en-tête 0x61, longueur (2 octets, gros-boutiste, octets après le type),
type (0x00 contrôle, 0x01 canal, 0x02 parole), puis les champs.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from crossgate.audio import FRAME_SAMPLES, Frame

log = logging.getLogger("crossgate")

START = 0x61
TYPE_CONTROL, TYPE_CHANNEL, TYPE_SPEECH = 0x00, 0x01, 0x02
PKT_RATET = 0x09
PKT_PRODID = 0x30
PKT_VERSTRING = 0x31
PKT_RESET = 0x33
PKT_READY = 0x39
FIELD_CHAND = 0x01
FIELD_SPEECHD = 0x00

# Index de débit DVSI : 33 = 3600 bit/s avec FEC (trame de 72 bits),
# 34 = 2450 bit/s sans FEC (trame de 49 bits, celle extraite des paquets IPSC).
RATE_DMR_NOFEC = 34
AMBE_BITS = 49
AMBE_BYTES = 7


def build_packet(ptype: int, payload: bytes) -> bytes:
    return bytes([START]) + len(payload).to_bytes(2, "big") + bytes([ptype]) + payload


def channel_packet(ambe: bytes, nbits: int = AMBE_BITS) -> bytes:
    return build_packet(TYPE_CHANNEL, bytes([FIELD_CHAND, nbits]) + ambe)


def speech_packet(frame: Frame) -> bytes:
    pcm = frame.astype(">i2").tobytes()
    return build_packet(TYPE_SPEECH, bytes([FIELD_SPEECHD, FRAME_SAMPLES]) + pcm)


def parse_speech(payload: bytes) -> Frame:
    if len(payload) < 2 or payload[0] != FIELD_SPEECHD:
        raise ValueError("Paquet de parole invalide")
    n = payload[1]
    return np.frombuffer(payload[2:2 + n * 2], dtype=">i2").astype(np.int16)


def parse_channel(payload: bytes) -> bytes:
    if len(payload) < 2 or payload[0] != FIELD_CHAND:
        raise ValueError("Paquet canal invalide")
    nbits = payload[1]
    return payload[2:2 + (nbits + 7) // 8]


class NullVocoder:
    available = False
    detail = "aucun vocodeur AMBE+2 : branchez un ThumbDV ou utilisez l'interface audio"

    async def open(self) -> None:
        return None

    async def decode(self, ambe: bytes) -> Frame:
        raise RuntimeError(self.detail)

    async def encode(self, frame: Frame) -> bytes:
        raise RuntimeError(self.detail)

    def close(self) -> None:
        return None


class ThumbDV:
    """Puce DVSI sur port série. Les échanges sont synchrones (une requête, une réponse) ;
    ils tournent dans un thread pour ne pas bloquer la boucle asyncio."""

    def __init__(self, port: str, baudrate: int = 460800, serial_factory=None):
        self.port = port
        self.baudrate = baudrate
        self.serial_factory = serial_factory
        self.ser = None
        self.lock = threading.Lock()
        self.available = False
        self.detail = ""

    # ----- E/S brutes
    def _read_packet(self) -> tuple[int, bytes]:
        while True:
            b = self.ser.read(1)
            if not b:
                raise TimeoutError("Le vocodeur ne répond pas")
            if b[0] == START:
                break
        head = self.ser.read(3)
        if len(head) != 3:
            raise TimeoutError("Paquet vocodeur incomplet")
        length = int.from_bytes(head[:2], "big")
        payload = self.ser.read(length)
        if len(payload) != length:
            raise TimeoutError("Paquet vocodeur incomplet")
        return head[2], payload

    def _transact(self, packet: bytes, expect: int) -> bytes:
        with self.lock:
            self.ser.write(packet)
            for _ in range(4):
                ptype, payload = self._read_packet()
                if ptype == expect:
                    return payload
            raise RuntimeError("Réponse inattendue du vocodeur")

    def _open_sync(self) -> None:
        if self.serial_factory:
            self.ser = self.serial_factory()
        else:
            import serial  # pyserial

            self.ser = serial.Serial(self.port, self.baudrate, timeout=1.0)
        self.ser.reset_input_buffer()
        self.ser.write(build_packet(TYPE_CONTROL, bytes([PKT_RESET])))
        ptype, payload = self._read_packet()
        if ptype != TYPE_CONTROL or not payload or payload[0] != PKT_READY:
            raise RuntimeError("Le vocodeur n'a pas confirmé sa réinitialisation")
        reply = self._transact(build_packet(TYPE_CONTROL, bytes([PKT_RATET, RATE_DMR_NOFEC])), TYPE_CONTROL)
        if len(reply) < 2 or reply[0] != PKT_RATET or reply[1] != 0:
            raise RuntimeError("Le vocodeur a refusé le débit DMR")
        prod = self._transact(build_packet(TYPE_CONTROL, bytes([PKT_PRODID])), TYPE_CONTROL)
        self.detail = prod[1:].split(b"\x00")[0].decode(errors="replace") or "AMBE-3000"
        self.available = True

    async def open(self) -> None:
        try:
            await asyncio.to_thread(self._open_sync)
            log.info("Vocodeur prêt : %s", self.detail)
        except Exception as e:  # matériel absent ou débranché
            self.available = False
            self.detail = f"vocodeur indisponible sur {self.port} : {e}"
            log.warning(self.detail)

    async def decode(self, ambe: bytes) -> Frame:
        payload = await asyncio.to_thread(self._transact, channel_packet(ambe), TYPE_SPEECH)
        return parse_speech(payload)

    async def encode(self, frame: Frame) -> bytes:
        payload = await asyncio.to_thread(self._transact, speech_packet(frame), TYPE_CHANNEL)
        return parse_channel(payload)

    def close(self) -> None:
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass

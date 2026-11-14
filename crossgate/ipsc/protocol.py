"""Protocole Motorola IPSC (IP Site Connect) : paquets d'enregistrement, de maintien et de voix.

Références : travaux publics de la communauté radioamateur (DMRlink, n0mjs) sur l'IPSC.
Les décalages des champs voix sont regroupés ici pour pouvoir être ajustés après une
capture sur le relais réel si une version de firmware différait.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

# --- Codes d'opération
GROUP_VOICE = 0x80
PVT_VOICE = 0x84
GROUP_DATA = 0x83
PVT_DATA = 0x86
XCMP_XNL = 0x70
CALL_MON_STATUS = 0x61
CALL_MON_RPT = 0x62
CALL_MON_NACK = 0x63
RPT_WAKE_UP = 0x85
MASTER_REG_REQ = 0x90
MASTER_REG_REPLY = 0x91
PEER_LIST_REQ = 0x92
PEER_LIST_REPLY = 0x93
PEER_REG_REQ = 0x94
PEER_REG_REPLY = 0x95
MASTER_ALIVE_REQ = 0x96
MASTER_ALIVE_REPLY = 0x97
PEER_ALIVE_REQ = 0x98
PEER_ALIVE_REPLY = 0x99
DE_REG_REQ = 0x9A
DE_REG_REPLY = 0x9B

OPCODE_NAMES = {
    v: k for k, v in globals().items() if k.isupper() and isinstance(v, int) and not k.startswith(("BURST", "MODE", "FLAG", "AUTH", "IPSC", "LINK", "AMBE", "OFF"))
}

# --- Version annoncée (IPSC 1.7 / compatibilité 1.6)
LINK_TYPE_IPSC = 0x04
IPSC_VERSION = bytes([LINK_TYPE_IPSC, 0x02, LINK_TYPE_IPSC, 0x01])

# --- Octet de mode : pair opérationnel, numérique, TS1 et TS2 actifs
MODE_OPERATIONAL = 0b01000000
MODE_DIGITAL = 0b00100000
MODE_TS1_ON = 0b00001000
MODE_TS2_ON = 0b00000010
DEFAULT_MODE = MODE_OPERATIONAL | MODE_DIGITAL | MODE_TS1_ON | MODE_TS2_ON  # 0x6A

# --- Drapeaux (4 octets) : services voix et données, authentification
FLAG_AUTH = 0x10
FLAG_DATA = 0x08
FLAG_VOICE = 0x04

AUTH_LEN = 10  # HMAC-SHA1 tronqué à 10 octets

# --- Types de rafale (octet 30 des paquets voix)
BURST_VOICE_HEAD = 0x01
BURST_VOICE_TERM = 0x02
BURST_SLOT1_VOICE = 0x0A
BURST_SLOT2_VOICE = 0x8A

# --- Décalages dans un paquet voix
OFF_PEER = slice(1, 5)
OFF_SEQ = 5
OFF_SRC = slice(6, 9)
OFF_DST = slice(9, 12)
OFF_CALL_TYPE = 12
OFF_CALL_CTRL = slice(13, 17)
OFF_CALL_INFO = 17
OFF_RTP = slice(18, 30)
OFF_RTP_SEQ = slice(20, 22)
OFF_RTP_TS = slice(22, 26)
OFF_BURST = 30
OFF_AMBE = slice(33, 52)  # 19 octets : 3 trames AMBE+2 de 49 bits
AMBE_FRAME_BITS = 49
AMBE_FRAME_STARTS = (0, 50, 100)  # un bit de séparation entre les trames
CALL_INFO_TS2 = 0x20
CALL_INFO_END = 0x40
RTP_TS_STEP = 480  # 60 ms à 8 kHz par paquet


def auth_key(hex_key: str | None) -> bytes | None:
    if not hex_key:
        return None
    return bytes.fromhex(hex_key.rjust(40, "0"))


def sign(key: bytes | None, packet: bytes) -> bytes:
    if not key:
        return packet
    return packet + hmac.new(key, packet, hashlib.sha1).digest()[:AUTH_LEN]


def verify(key: bytes | None, data: bytes) -> bytes | None:
    """Renvoie le paquet sans signature s'il est authentique, None sinon."""
    if not key:
        return data
    if len(data) <= AUTH_LEN:
        return None
    packet, mac = data[:-AUTH_LEN], data[-AUTH_LEN:]
    expected = hmac.new(key, packet, hashlib.sha1).digest()[:AUTH_LEN]
    return packet if hmac.compare_digest(mac, expected) else None


def flags_bytes(authenticated: bool) -> bytes:
    return bytes([0x00, 0x00, 0x00, FLAG_VOICE | FLAG_DATA | (FLAG_AUTH if authenticated else 0)])


def rid(radio_id: int) -> bytes:
    return radio_id.to_bytes(4, "big")


def master_reg_req(radio_id: int, authenticated: bool, mode: int = DEFAULT_MODE) -> bytes:
    return bytes([MASTER_REG_REQ]) + rid(radio_id) + bytes([mode]) + flags_bytes(authenticated) + IPSC_VERSION


def master_alive_req(radio_id: int, authenticated: bool, mode: int = DEFAULT_MODE) -> bytes:
    return bytes([MASTER_ALIVE_REQ]) + rid(radio_id) + bytes([mode]) + flags_bytes(authenticated) + IPSC_VERSION


def peer_list_req(radio_id: int) -> bytes:
    return bytes([PEER_LIST_REQ]) + rid(radio_id)


def peer_reg_req(radio_id: int) -> bytes:
    return bytes([PEER_REG_REQ]) + rid(radio_id) + IPSC_VERSION


def peer_reg_reply(radio_id: int) -> bytes:
    return bytes([PEER_REG_REPLY]) + rid(radio_id) + IPSC_VERSION


def peer_alive_req(radio_id: int, authenticated: bool, mode: int = DEFAULT_MODE) -> bytes:
    return bytes([PEER_ALIVE_REQ]) + rid(radio_id) + bytes([mode]) + flags_bytes(authenticated)


def peer_alive_reply(radio_id: int, authenticated: bool, mode: int = DEFAULT_MODE) -> bytes:
    return bytes([PEER_ALIVE_REPLY]) + rid(radio_id) + bytes([mode]) + flags_bytes(authenticated)


def de_reg_req(radio_id: int) -> bytes:
    return bytes([DE_REG_REQ]) + rid(radio_id)


@dataclass
class Peer:
    radio_id: int
    ip: str
    port: int
    mode: int


def parse_peer_list(packet: bytes) -> list[Peer]:
    """Réponse 0x93 : opcode, id du maître (4), longueur de la liste (2), puis 11 octets par pair."""
    if len(packet) < 7:
        return []
    length = int.from_bytes(packet[5:7], "big")
    body = packet[7:7 + length]
    peers = []
    for i in range(0, len(body) - len(body) % 11, 11):
        e = body[i:i + 11]
        peers.append(
            Peer(
                radio_id=int.from_bytes(e[0:4], "big"),
                ip=".".join(str(b) for b in e[4:8]),
                port=int.from_bytes(e[8:10], "big"),
                mode=e[10],
            )
        )
    return peers


@dataclass
class VoicePacket:
    opcode: int
    peer: int
    seq: int
    src: int
    dst: int
    call_type: int
    timeslot: int
    end: bool
    burst: int
    rtp_seq: int
    rtp_ts: int
    raw: bytes

    @property
    def is_voice_burst(self) -> bool:
        return (self.burst & 0x7F) == BURST_SLOT1_VOICE

    @property
    def is_header(self) -> bool:
        return self.burst == BURST_VOICE_HEAD

    @property
    def is_terminator(self) -> bool:
        return self.burst == BURST_VOICE_TERM


def parse_voice(packet: bytes) -> VoicePacket | None:
    if len(packet) < 31 or packet[0] not in (GROUP_VOICE, PVT_VOICE):
        return None
    info = packet[OFF_CALL_INFO]
    return VoicePacket(
        opcode=packet[0],
        peer=int.from_bytes(packet[OFF_PEER], "big"),
        seq=packet[OFF_SEQ],
        src=int.from_bytes(packet[OFF_SRC], "big"),
        dst=int.from_bytes(packet[OFF_DST], "big"),
        call_type=packet[OFF_CALL_TYPE],
        timeslot=2 if info & CALL_INFO_TS2 else 1,
        end=bool(info & CALL_INFO_END),
        burst=packet[OFF_BURST],
        rtp_seq=int.from_bytes(packet[OFF_RTP_SEQ], "big"),
        rtp_ts=int.from_bytes(packet[OFF_RTP_TS], "big"),
        raw=packet,
    )


# --- Manipulation des 3 trames AMBE (49 bits chacune) d'une rafale voix


def _to_bits(data: bytes) -> list[int]:
    return [(byte >> (7 - i)) & 1 for byte in data for i in range(8)]


def _from_bits(bits: list[int]) -> bytes:
    bits = bits + [0] * (-len(bits) % 8)
    return bytes(sum(b << (7 - i) for i, b in enumerate(bits[j:j + 8])) for j in range(0, len(bits), 8))


def extract_ambe(packet: bytes) -> list[bytes]:
    """Renvoie les 3 trames AMBE+2, chacune sur 7 octets (49 bits alignés à gauche)."""
    payload = packet[OFF_AMBE]
    if len(payload) < 19:
        return []
    bits = _to_bits(payload)
    return [_from_bits(bits[s:s + AMBE_FRAME_BITS]) for s in AMBE_FRAME_STARTS]


def insert_ambe(packet: bytes, frames: list[bytes]) -> bytes:
    """Remplace les 3 trames AMBE d'un paquet voix (les bits de séparation sont conservés)."""
    if len(frames) != 3:
        raise ValueError("Une rafale contient exactement 3 trames AMBE")
    bits = _to_bits(packet[OFF_AMBE])
    for start, frame in zip(AMBE_FRAME_STARTS, frames):
        bits[start:start + AMBE_FRAME_BITS] = _to_bits(frame)[:AMBE_FRAME_BITS]
    payload = _from_bits(bits)[:19]
    return packet[:OFF_AMBE.start] + payload + packet[OFF_AMBE.stop:]


def restamp(packet: bytes, *, peer: int, seq: int, src: int, dst: int, timeslot: int, rtp_seq: int, rtp_ts: int, end: bool | None = None) -> bytes:
    """Réécrit les champs d'identité et de séquence d'un paquet voix modèle."""
    b = bytearray(packet)
    b[OFF_PEER] = rid(peer)
    b[OFF_SEQ] = seq & 0xFF
    b[OFF_SRC] = src.to_bytes(3, "big")
    b[OFF_DST] = dst.to_bytes(3, "big")
    info = b[OFF_CALL_INFO] & ~CALL_INFO_TS2
    if timeslot == 2:
        info |= CALL_INFO_TS2
    if end is not None:
        info = (info | CALL_INFO_END) if end else (info & ~CALL_INFO_END)
    b[OFF_CALL_INFO] = info & 0xFF
    b[OFF_RTP_SEQ] = (rtp_seq & 0xFFFF).to_bytes(2, "big")
    b[OFF_RTP_TS] = (rtp_ts & 0xFFFFFFFF).to_bytes(4, "big")
    if (b[OFF_BURST] & 0x7F) == BURST_SLOT1_VOICE:
        b[OFF_BURST] = BURST_SLOT2_VOICE if timeslot == 2 else BURST_SLOT1_VOICE
    return bytes(b)

import asyncio
import random
import socket
from pathlib import Path

import numpy as np
import pytest

from crossgate import audio
from crossgate.bus import Bus, Channel
from crossgate.config import IpscRadio
from crossgate.endpoints.base import Endpoint
from crossgate.endpoints.ipsc import IpscEndpoint
from crossgate.events import EventLog
from crossgate.ipsc import protocol as p
from crossgate import vocoder as v

KEY = "1A2B3C4D5E"


def voice_packet(burst, src=3101, dst=9, ts=1, end=False, seq=7, rtp_seq=100, ambe=None):
    info = (p.CALL_INFO_TS2 if ts == 2 else 0) | (p.CALL_INFO_END if end else 0)
    b = bytearray(54)
    b[0] = p.GROUP_VOICE
    b[1:5] = (5000).to_bytes(4, "big")
    b[5] = seq
    b[6:9] = src.to_bytes(3, "big")
    b[9:12] = dst.to_bytes(3, "big")
    b[12] = 0x02
    b[17] = info
    b[18] = 0x80
    b[19] = 0x5D
    b[20:22] = rtp_seq.to_bytes(2, "big")
    b[30] = burst
    pkt = bytes(b)
    if ambe:
        pkt = p.insert_ambe(pkt, ambe)
    return pkt


# ---------------------------------------------------------------- protocole


def test_sign_and_verify():
    key = p.auth_key(KEY)
    assert len(key) == 20
    pkt = p.master_reg_req(311000, True)
    signed = p.sign(key, pkt)
    assert len(signed) == len(pkt) + 10
    assert p.verify(key, signed) == pkt
    tampered = bytearray(signed)
    tampered[3] ^= 1
    assert p.verify(key, bytes(tampered)) is None
    assert p.verify(None, pkt) == pkt


def test_registration_layout():
    pkt = p.master_reg_req(0x01020304, authenticated=True)
    assert pkt[0] == 0x90 and pkt[1:5] == b"\x01\x02\x03\x04"
    assert pkt[5] == 0x6A
    assert pkt[6:10] == bytes([0, 0, 0, 0x1C])
    assert pkt[10:14] == p.IPSC_VERSION
    assert len(p.peer_alive_req(1, False)) == 10


def test_peer_list():
    entries = b"".join(
        rid.to_bytes(4, "big") + bytes([10, 0, 0, n]) + (50000 + n).to_bytes(2, "big") + b"\x6a"
        for n, rid in ((2, 1002), (3, 1003))
    )
    pkt = bytes([p.PEER_LIST_REPLY]) + (1).to_bytes(4, "big") + len(entries).to_bytes(2, "big") + entries
    peers = p.parse_peer_list(pkt)
    assert [(x.radio_id, x.ip, x.port) for x in peers] == [(1002, "10.0.0.2", 50002), (1003, "10.0.0.3", 50003)]


def test_ambe_roundtrip_keeps_separator_bits():
    rnd = random.Random(1)
    frames = [bytes(rnd.getrandbits(8) for _ in range(6)) + bytes([rnd.getrandbits(1) << 7]) for _ in range(3)]
    base = bytearray(voice_packet(p.BURST_SLOT1_VOICE))
    base[33:52] = bytes([0xFF] * 19)  # bits de séparation à 1 : ils doivent survivre
    pkt = p.insert_ambe(bytes(base), frames)
    assert p.extract_ambe(pkt) == frames
    bits = p._to_bits(pkt[33:52])
    assert bits[49] == 1 and bits[99] == 1
    assert len(pkt) == len(base)


def test_parse_and_restamp():
    pkt = voice_packet(p.BURST_SLOT1_VOICE, src=3101, dst=9, ts=2, end=False)
    vp = p.parse_voice(pkt)
    assert (vp.src, vp.dst, vp.timeslot, vp.end, vp.is_voice_burst) == (3101, 9, 2, False, True)
    out = p.restamp(pkt, peer=777, seq=3, src=777, dst=12, timeslot=1, rtp_seq=5, rtp_ts=960, end=True)
    vp2 = p.parse_voice(out)
    assert (vp2.peer, vp2.seq, vp2.src, vp2.dst, vp2.timeslot, vp2.end, vp2.rtp_seq, vp2.rtp_ts) == (777, 3, 777, 12, 1, True, 5, 960)
    assert out[p.OFF_BURST] == p.BURST_SLOT1_VOICE
    assert p.parse_voice(b"\x90abc") is None


# ---------------------------------------------------------------- vocodeur (puce simulée)


class FakeDvsi:
    """Simule le protocole série d'une puce AMBE-3000 : décode = 160 échantillons dérivés des bits."""

    def __init__(self):
        self.rx = bytearray()

    def write(self, data):
        ptype, payload = data[3], data[4:]
        if ptype == v.TYPE_CONTROL and payload[0] == v.PKT_RESET:
            self.rx += v.build_packet(v.TYPE_CONTROL, bytes([v.PKT_READY]))
        elif ptype == v.TYPE_CONTROL and payload[0] == v.PKT_RATET:
            assert payload[1] == v.RATE_DMR_NOFEC
            self.rx += v.build_packet(v.TYPE_CONTROL, bytes([v.PKT_RATET, 0]))
        elif ptype == v.TYPE_CONTROL and payload[0] == v.PKT_PRODID:
            self.rx += v.build_packet(v.TYPE_CONTROL, bytes([v.PKT_PRODID]) + b"AMBE3000R\x00")
        elif ptype == v.TYPE_CHANNEL:
            ambe = v.parse_channel(payload)
            pcm = np.full(160, int.from_bytes(ambe[:2], "big") - 32768, dtype=np.int16)
            self.rx += v.speech_packet(pcm)
        elif ptype == v.TYPE_SPEECH:
            frame = v.parse_speech(payload)
            self.rx += v.channel_packet((int(frame[0]) + 32768).to_bytes(2, "big") + bytes(5))

    def read(self, n):
        out, self.rx = bytes(self.rx[:n]), self.rx[n:]
        return out

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        pass


def test_thumbdv_protocol():
    async def go():
        dv = v.ThumbDV("/dev/fake", serial_factory=FakeDvsi)
        await dv.open()
        assert dv.available and dv.detail == "AMBE3000R"
        pcm = await dv.decode(bytes([0x80, 0x10, 0, 0, 0, 0, 0]))
        assert len(pcm) == 160 and pcm[0] == 0x8010 - 32768
        ambe = await dv.encode(np.full(160, 1234, dtype=np.int16))
        assert len(ambe) == 7 and int.from_bytes(ambe[:2], "big") == 1234 + 32768

    asyncio.run(go())


def test_thumbdv_absent_degrades_gracefully():
    async def go():
        dv = v.ThumbDV("/dev/introuvable")
        await dv.open()
        assert not dv.available and "indisponible" in dv.detail

    asyncio.run(go())


# ---------------------------------------------------------------- liaison complète avec un faux relais


class Listener(Endpoint):
    def __init__(self):
        super().__init__("ecoute", "Écoute", "vhf")
        self.got = []
        self.ended = False

    def deliver(self, frame):
        self.got.append(frame)

    def on_rx_end(self):
        self.ended = True


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeMaster(asyncio.DatagramProtocol):
    def __init__(self, key):
        self.key = key
        self.peer_addr = None
        self.received = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        pkt = p.verify(self.key, data)
        assert pkt is not None, "paquet mal signé par la passerelle"
        self.received.append(pkt)
        self.peer_addr = addr
        if pkt[0] == p.MASTER_REG_REQ:
            reply = bytes([p.MASTER_REG_REPLY]) + (1).to_bytes(4, "big") + b"\x6a" + p.flags_bytes(True) + b"\x00\x01" + p.IPSC_VERSION
            self.transport.sendto(p.sign(self.key, reply), addr)
        elif pkt[0] == p.PEER_LIST_REQ:
            reply = bytes([p.PEER_LIST_REPLY]) + (1).to_bytes(4, "big") + b"\x00\x00"
            self.transport.sendto(p.sign(self.key, reply), addr)
        elif pkt[0] == p.MASTER_ALIVE_REQ:
            self.transport.sendto(p.sign(self.key, bytes([p.MASTER_ALIVE_REPLY]) + (1).to_bytes(4, "big")), addr)


def test_ipsc_link_receives_call(tmp_path: Path):
    async def go():
        key = p.auth_key(KEY)
        loop = asyncio.get_running_loop()
        master_port, local_port = free_udp_port(), free_udp_port()
        master = FakeMaster(key)
        mt, _ = await loop.create_datagram_endpoint(lambda: master, local_addr=("127.0.0.1", master_port))

        bus = Bus(EventLog(), hang_ms=300, tot_s=30)
        bus.add_channel(Channel("vhf", "VHF"))
        cfg = IpscRadio(
            type="ipsc", id="slr", name="SLR5500", channel="vhf", master_ip="127.0.0.1", master_port=master_port,
            local_port=local_port, radio_id=311999, auth_key=KEY, talkgroup=9, keepalive_s=0.2,
        )
        dv = v.ThumbDV("/dev/fake", serial_factory=FakeDvsi)
        ep, listener = IpscEndpoint(cfg, dv, tmp_path), Listener()
        await bus.add_endpoint(listener)
        await bus.add_endpoint(ep)
        bus.start_clock()
        for _ in range(50):
            await asyncio.sleep(0.05)
            if ep.registered:
                break
        assert ep.registered and ep.status == "en_ligne", ep.detail

        ambe = [bytes([0x90, i, 0, 0, 0, 0, 0]) for i in range(3)]
        send = lambda pkt: mt.sendto(p.sign(key, pkt), master.peer_addr)
        send(voice_packet(p.BURST_VOICE_HEAD))
        send(voice_packet(p.BURST_SLOT1_VOICE, ambe=ambe, rtp_seq=101))
        send(voice_packet(p.BURST_SLOT1_VOICE, ambe=ambe, rtp_seq=102))
        send(voice_packet(p.BURST_SLOT1_VOICE, dst=55, ambe=ambe))  # autre groupe : ignoré
        send(voice_packet(p.BURST_SLOT1_VOICE, ts=2, ambe=ambe))  # autre slot : ignoré
        send(voice_packet(p.BURST_VOICE_TERM, end=True))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if listener.ended:
                break
        assert len(listener.got) == 6, len(listener.got)
        assert listener.got[0][0] == 0x9000 - 32768
        assert listener.ended
        await bus.shutdown()
        await asyncio.sleep(0.1)
        assert any(x[0] == p.DE_REG_REQ for x in master.received)
        mt.close()

    asyncio.run(go())


def test_ipsc_without_vocoder_reports_degraded(tmp_path: Path):
    async def go():
        bus = Bus(EventLog())
        bus.add_channel(Channel("vhf", "VHF"))
        cfg = IpscRadio(type="ipsc", id="slr", name="SLR", channel="vhf", master_ip="127.0.0.1", master_port=free_udp_port(),
                        local_port=free_udp_port(), radio_id=1, vocoder={"type": "aucun"})
        ep = IpscEndpoint(cfg, v.NullVocoder(), tmp_path)
        await bus.add_endpoint(ep)
        assert ep.status == "degrade"
        await bus.shutdown()

    asyncio.run(go())

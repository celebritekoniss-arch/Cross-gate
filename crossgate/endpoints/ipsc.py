"""Liaison IP directe avec un relais Motorola (SLR5500…) en IPSC, comme un site pair.

Réception (relais → CrossGate) : les rafales voix AMBE+2 sont décodées par le vocodeur
et injectées sur le canal. Émission (CrossGate → relais) : expérimentale, à partir d'un
modèle d'appel capturé sur le relais réel (voir README, « Émission IPSC »).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path

from crossgate.audio import Frame
from crossgate.endpoints.base import Endpoint
from crossgate.ipsc import protocol as p

log = logging.getLogger("crossgate")


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "IpscEndpoint"):
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner.on_datagram(data, addr)


class IpscEndpoint(Endpoint):
    kind = "ipsc"

    def __init__(self, cfg, vocoder, data_dir: Path):
        super().__init__(cfg.id, cfg.name, cfg.channel, cfg.priority, queue_frames=150)
        self.cfg = cfg
        self.vocoder = vocoder
        self.key = p.auth_key(cfg.auth_key)
        self.master = (cfg.master_ip, cfg.master_port)
        self.peers: dict[int, p.Peer] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self.registered = False
        self.last_master_reply = 0.0
        self.tasks: list[asyncio.Task] = []
        self.decode_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=200)
        self.current_call: tuple[int, int] | None = None  # (source, séquence)
        self.template_path = Path(cfg.tx_template) if cfg.tx_template else data_dir / f"ipsc_modele_{cfg.id}.json"
        self.capture: list[str] | None = None
        self.template: dict | None = self._load_template()
        self.tx_seq = random.randint(0, 255)

    # ------------------------------------------------------------------ cycle de vie
    async def start(self, bus) -> None:
        await super().start(bus)
        await self.vocoder.open()
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self), local_addr=("0.0.0.0", self.cfg.local_port)
        )
        self.tasks = [asyncio.create_task(self._keepalive()), asyncio.create_task(self._decoder())]
        if self.cfg.tx_enabled:
            self.tasks.append(asyncio.create_task(self._transmitter()))
        self._update_status()

    async def stop(self) -> None:
        if self.transport and self.registered:
            self._send(p.de_reg_req(self.cfg.radio_id), self.master)
            await asyncio.sleep(0.05)  # laisse partir le désenregistrement avant de fermer
        for t in self.tasks:
            t.cancel()
        if self.transport:
            self.transport.close()
        self.vocoder.close()
        await super().stop()

    def _update_status(self) -> None:
        if not self.registered:
            self.set_status("degrade", f"connexion au relais {self.master[0]}:{self.master[1]}…")
        elif not self.vocoder.available:
            self.set_status("degrade", f"relais connecté, {self.vocoder.detail}")
        else:
            tx = "émission active" if self.cfg.tx_enabled and self.template else "réception seule"
            self.set_status("en_ligne", f"relais connecté, TS{self.cfg.timeslot} GT {self.cfg.talkgroup}, {tx}")

    # ------------------------------------------------------------------ réseau
    def _send(self, packet: bytes, addr) -> None:
        if self.transport:
            self.transport.sendto(p.sign(self.key, packet), addr)

    async def _keepalive(self) -> None:
        auth = self.key is not None
        while True:
            now = time.monotonic()
            if self.registered and now - self.last_master_reply > self.cfg.keepalive_s * 3:
                self.registered = False
                self.peers.clear()
                self._update_status()
            if not self.registered:
                self._send(p.master_reg_req(self.cfg.radio_id, auth), self.master)
            else:
                self._send(p.master_alive_req(self.cfg.radio_id, auth), self.master)
                for peer in self.peers.values():
                    self._send(p.peer_alive_req(self.cfg.radio_id, auth), (peer.ip, peer.port))
            await asyncio.sleep(self.cfg.keepalive_s)

    def on_datagram(self, data: bytes, addr) -> None:
        packet = p.verify(self.key, data)
        if packet is None or not packet:
            log.debug("IPSC : paquet non authentifié de %s ignoré", addr)
            return
        op = packet[0]
        auth = self.key is not None
        if op == p.MASTER_REG_REPLY:
            self.last_master_reply = time.monotonic()
            if not self.registered:
                self.registered = True
                self._send(p.peer_list_req(self.cfg.radio_id), self.master)
                self._update_status()
        elif op == p.MASTER_ALIVE_REPLY:
            self.last_master_reply = time.monotonic()
        elif op == p.PEER_LIST_REPLY:
            self.last_master_reply = time.monotonic()
            self.peers = {peer.radio_id: peer for peer in p.parse_peer_list(packet) if peer.radio_id != self.cfg.radio_id}
            for peer in self.peers.values():
                self._send(p.peer_reg_req(self.cfg.radio_id), (peer.ip, peer.port))
        elif op == p.PEER_REG_REQ:
            self._send(p.peer_reg_reply(self.cfg.radio_id), addr)
        elif op == p.PEER_ALIVE_REQ:
            self._send(p.peer_alive_reply(self.cfg.radio_id, auth), addr)
        elif op in (p.GROUP_VOICE, p.PVT_VOICE):
            self._on_voice(packet)

    # ------------------------------------------------------------------ réception voix
    def _on_voice(self, packet: bytes) -> None:
        v = p.parse_voice(packet)
        if not v or v.timeslot != self.cfg.timeslot:
            return
        if v.opcode == p.GROUP_VOICE and v.dst != self.cfg.talkgroup:
            return
        if self.capture is not None:
            self._capture(v)
        call = (v.src, v.seq)
        if v.is_header or (self.current_call != call and v.is_voice_burst):
            if self.current_call != call:
                self.current_call = call
                self._queue(("start", v.src))
        if v.is_voice_burst and self.current_call == call:
            for ambe in p.extract_ambe(packet):
                self._queue(("ambe", ambe))
        if v.is_terminator or v.end:
            if self.current_call == call:
                self.current_call = None
                self._queue(("end", v.src))

    def _queue(self, item) -> None:
        if self.decode_queue.full():
            self.decode_queue.get_nowait()
        self.decode_queue.put_nowait(item)

    async def _decoder(self) -> None:
        assert self.bus
        while True:
            kind, value = await self.decode_queue.get()
            if kind == "start":
                self.bus.events.emit("ipsc", f"{self.name} : appel de la radio {value} (GT {self.cfg.talkgroup})", radio=value)
            elif kind == "ambe":
                if not self.vocoder.available:
                    continue
                try:
                    frame = await self.vocoder.decode(value)  # type: ignore[arg-type]
                except Exception as e:
                    self.set_status("erreur", f"vocodeur : {e}")
                    continue
                self.bus.push_audio(self, frame, implicit=True)
            elif kind == "end":
                self.bus.end_audio(self)

    # ------------------------------------------------------------------ capture du modèle d'émission
    def start_capture(self) -> None:
        self.capture = []
        self.bus.events.emit("ipsc", f"{self.name} : capture d'un appel modèle en cours, faites un appel depuis une radio du groupe", "alerte")

    def _capture(self, v: p.VoicePacket) -> None:
        assert self.capture is not None
        self.capture.append(v.raw.hex())
        if v.is_terminator or v.end:
            raw = [bytes.fromhex(h) for h in self.capture]
            heads = [r for r in raw if r[p.OFF_BURST] == p.BURST_VOICE_HEAD]
            bursts = [r for r in raw if (r[p.OFF_BURST] & 0x7F) == p.BURST_SLOT1_VOICE]
            terms = [r for r in raw if r[p.OFF_BURST] == p.BURST_VOICE_TERM]
            self.capture = None
            if len(bursts) < 6 or not heads or not terms:
                self.bus.events.emit("ipsc", f"{self.name} : appel modèle trop court, recommencez (au moins 1 s de parole)", "alerte")
                return
            self.template = {"heads": [h.hex() for h in heads[:3]], "bursts": [b.hex() for b in bursts[:6]], "term": terms[0].hex()}
            self.template_path.parent.mkdir(parents=True, exist_ok=True)
            self.template_path.write_text(json.dumps(self.template, indent=1))
            self.bus.events.emit("ipsc", f"{self.name} : modèle d'émission enregistré", template=str(self.template_path))
            self._update_status()

    def _load_template(self) -> dict | None:
        try:
            return json.loads(self.template_path.read_text())
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------------ émission (expérimentale)
    async def _transmitter(self) -> None:
        """Encode 3 trames de 20 ms en une rafale IPSC de 60 ms à partir du modèle capturé."""
        pending: list[Frame] = []
        in_call = False
        rtp_seq = random.randint(0, 0xFFFF)
        rtp_ts = random.randint(0, 0xFFFFFFFF)
        burst_index = 0

        def stamp(tpl_hex: str, end: bool | None = None) -> bytes:
            return p.restamp(
                bytes.fromhex(tpl_hex), peer=self.cfg.radio_id, seq=self.tx_seq, src=self.cfg.radio_id,
                dst=self.cfg.talkgroup, timeslot=self.cfg.timeslot, rtp_seq=rtp_seq, rtp_ts=rtp_ts, end=end,
            )

        def broadcast(pkt: bytes) -> None:
            self._send(pkt, self.master)
            for peer in self.peers.values():
                self._send(pkt, (peer.ip, peer.port))

        while True:
            frame = await self.out_queue.get()
            if not (self.registered and self.vocoder.available and self.template):
                pending.clear()
                in_call = False
                continue
            if frame is None:
                if in_call:
                    rtp_seq += 1
                    rtp_ts += p.RTP_TS_STEP
                    broadcast(stamp(self.template["term"], end=True))
                in_call, pending, burst_index = False, [], 0
                continue
            if not in_call:
                in_call = True
                self.tx_seq = (self.tx_seq + 1) & 0xFF
                for h in self.template["heads"]:
                    rtp_seq += 1
                    broadcast(stamp(h, end=False))
            pending.append(frame)
            if len(pending) == 3:
                try:
                    ambe = [await self.vocoder.encode(f) for f in pending]
                except Exception as e:
                    self.set_status("erreur", f"vocodeur : {e}")
                    pending.clear()
                    continue
                pending.clear()
                rtp_seq += 1
                rtp_ts += p.RTP_TS_STEP
                tpl = bytes.fromhex(self.template["bursts"][burst_index % 6])
                pkt = p.insert_ambe(tpl, ambe)
                broadcast(stamp(pkt.hex(), end=False))
                burst_index += 1

    def snapshot(self):
        snap = super().snapshot()
        snap["peers"] = len(self.peers)
        snap["template"] = bool(self.template)
        return snap

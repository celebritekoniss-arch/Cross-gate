"""Bus de routage : canaux, interconnexions (patchs) et prise de parole.

Règles :
- chaque point est sur un canal ; des canaux peuvent être interconnectés (patch) ;
- un groupe (canal seul ou canaux interconnectés) n'a qu'un seul émetteur à la fois (semi-duplex) ;
- un émetteur de priorité strictement supérieure peut préempter (ex. poste de commandement) ;
- les radios prennent la parole implicitement à la première trame audio, et la relâchent
  après le « hang time » sans audio ; les smartphones la demandent explicitement (alternat) ;
- une émission est coupée après la durée maximale (TOT) pour éviter un canal bloqué.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from crossgate.audio import Frame
from crossgate.endpoints.base import Endpoint
from crossgate.events import EventLog


@dataclass
class Channel:
    id: str
    name: str
    description: str = ""


@dataclass
class Floor:
    talker: str  # id du point qui émet
    started: float
    last_audio: float
    explicit: bool  # True : alternat explicite (smartphone), False : détection audio (radio)
    priority: int
    channel: str
    frames: int = 0


@dataclass
class Patch:
    id: int
    channels: frozenset[str]
    created_by: str = ""
    created: float = field(default_factory=time.time)


class FloorDenied(Exception):
    def __init__(self, holder: Endpoint):
        self.holder = holder


class Bus:
    def __init__(self, events: EventLog, hang_ms: int = 800, tot_s: int = 60):
        self.events = events
        self.hang = hang_ms / 1000
        self.tot = tot_s
        self.channels: dict[str, Channel] = {}
        self.endpoints: dict[str, Endpoint] = {}
        self.patches: dict[int, Patch] = {}
        self.floors: dict[frozenset[str], Floor] = {}
        self._patch_ids = itertools.count(1)
        self.change_listeners: list[Callable[[], None]] = []
        self.tx_listeners: list[Any] = []  # enregistreur : on_tx_start / on_tx_frame / on_tx_end
        self._tick_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ configuration
    def add_channel(self, channel: Channel) -> None:
        self.channels[channel.id] = channel

    async def add_endpoint(self, ep: Endpoint) -> None:
        if ep.channel not in self.channels:
            raise ValueError(f"{ep.name} : canal inconnu « {ep.channel} »")
        self.endpoints[ep.id] = ep
        await ep.start(self)
        self.notify_change()

    async def remove_endpoint(self, ep_id: str) -> None:
        ep = self.endpoints.pop(ep_id, None)
        if not ep:
            return
        for key, fl in list(self.floors.items()):
            if fl.talker == ep_id:
                self._end_floor(key, "released")
        await ep.stop()
        self.notify_change()

    def notify_change(self) -> None:
        for listener in list(self.change_listeners):
            listener()

    # ------------------------------------------------------------------ groupes
    def group_of(self, channel: str) -> frozenset[str]:
        for p in self.patches.values():
            if channel in p.channels:
                return p.channels
        return frozenset([channel])

    def members(self, group: frozenset[str]) -> list[Endpoint]:
        return [e for e in self.endpoints.values() if e.channel in group]

    def patch(self, channels: list[str], by: str = "") -> Patch:
        chans = set(channels)
        unknown = chans - set(self.channels)
        if unknown:
            raise ValueError(f"Canaux inconnus : {', '.join(sorted(unknown))}")
        if len(chans) < 2:
            raise ValueError("Une interconnexion relie au moins deux canaux")
        # Un canal déjà interconnecté rejoint le nouveau groupe (fusion)
        for pid, p in list(self.patches.items()):
            if p.channels & chans:
                chans |= p.channels
                del self.patches[pid]
        self._end_floors_touching(chans)
        patch = Patch(next(self._patch_ids), frozenset(chans), by)
        self.patches[patch.id] = patch
        names = " ↔ ".join(self.channels[c].name for c in sorted(chans))
        self.events.emit("patch", f"Interconnexion {names}", patch=patch.id, by=by)
        self.notify_change()
        return patch

    def unpatch(self, patch_id: int, by: str = "") -> None:
        patch = self.patches.pop(patch_id, None)
        if not patch:
            raise KeyError(patch_id)
        self._end_floors_touching(set(patch.channels))
        names = " ↔ ".join(self.channels[c].name for c in sorted(patch.channels))
        self.events.emit("patch", f"Fin d'interconnexion {names}", patch=patch_id, by=by)
        self.notify_change()

    def move(self, ep_id: str, channel: str) -> None:
        ep = self.endpoints[ep_id]
        if channel not in self.channels:
            raise ValueError(f"Canal inconnu « {channel} »")
        for key, fl in list(self.floors.items()):
            if fl.talker == ep_id:
                self._end_floor(key, "released")
        ep.channel = channel
        self.notify_change()

    def _end_floors_touching(self, chans: set[str]) -> None:
        for key in list(self.floors):
            if key & chans:
                self._end_floor(key, "released")

    # ------------------------------------------------------------------ prise de parole
    def floor_of(self, ep: Endpoint) -> Floor | None:
        return self.floors.get(self.group_of(ep.channel))

    def request_floor(self, ep: Endpoint, explicit: bool = True) -> None:
        """Accorde la parole à ep, ou lève FloorDenied."""
        key = self.group_of(ep.channel)
        now = time.monotonic()
        current = self.floors.get(key)
        if current and current.talker == ep.id:
            return
        if current:
            holder = self.endpoints.get(current.talker)
            if holder and ep.priority > current.priority:
                self._end_floor(key, "preempted", by=ep.name)
            elif holder:
                raise FloorDenied(holder)
            else:
                self._end_floor(key, "released")
        self.floors[key] = Floor(ep.id, now, now, explicit, ep.priority, ep.channel)
        ep.on_floor("granted", {})
        for other in self.members(key):
            if other.id != ep.id:
                other.on_rx_start(ep.name, ep.channel)
        for listener in self.tx_listeners:
            listener.on_tx_start(ep, key, self)
        self.events.emit(
            "emission", f"{ep.name} émet sur {self._group_name(key)}", talker=ep.id, channel=ep.channel
        )
        self.notify_change()

    def release_floor(self, ep: Endpoint) -> None:
        key = self.group_of(ep.channel)
        fl = self.floors.get(key)
        if fl and fl.talker == ep.id:
            self._end_floor(key, "released")

    def _end_floor(self, key: frozenset[str], reason: str, by: str = "") -> None:
        fl = self.floors.pop(key, None)
        if not fl:
            return
        talker = self.endpoints.get(fl.talker)
        duration = time.monotonic() - fl.started
        if talker:
            talker.on_floor(reason, {"by": by})
        for other in self.members(key):
            if not talker or other.id != talker.id:
                other.on_rx_end()
        for listener in self.tx_listeners:
            listener.on_tx_end(fl, self)
        name = talker.name if talker else fl.talker
        msg = {
            "released": f"Fin d'émission de {name} ({duration:.1f} s)",
            "preempted": f"{name} interrompu par {by} (priorité)",
            "timeout": f"{name} coupé : durée maximale de {self.tot} s atteinte",
        }.get(reason, f"Fin d'émission de {name}")
        self.events.emit("emission_fin", msg, "info" if reason == "released" else "alerte", talker=fl.talker, duration=round(duration, 1))
        self.notify_change()

    # ------------------------------------------------------------------ audio
    def push_audio(self, ep: Endpoint, frame: Frame, implicit: bool = False) -> bool:
        """Audio émis par ep. implicit=True : prend la parole automatiquement (radio, IPSC).
        Renvoie False si l'audio est rejeté (canal occupé ou pas de parole)."""
        key = self.group_of(ep.channel)
        fl = self.floors.get(key)
        if not fl or fl.talker != ep.id:
            if not implicit:
                return False
            try:
                self.request_floor(ep, explicit=False)
            except FloorDenied:
                return False
            fl = self.floors[key]
        fl.last_audio = time.monotonic()
        fl.frames += 1
        for other in self.members(key):
            if other.id != ep.id:
                other.deliver(frame)
        for listener in self.tx_listeners:
            listener.on_tx_frame(fl, frame)
        return True

    def end_audio(self, ep: Endpoint) -> None:
        """Fin explicite d'émission signalée par une radio (fin d'appel IPSC, retombée du COS)."""
        self.release_floor(ep)

    # ------------------------------------------------------------------ horloge
    def tick(self) -> None:
        now = time.monotonic()
        for key, fl in list(self.floors.items()):
            if now - fl.started > self.tot:
                self._end_floor(key, "timeout")
            elif not fl.explicit and now - fl.last_audio > self.hang:
                self._end_floor(key, "released")
            elif fl.explicit and now - fl.last_audio > max(self.hang * 10, 10):
                # Smartphone qui a perdu le réseau en gardant l'alternat
                self._end_floor(key, "timeout")

    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self.tick()

    def start_clock(self) -> None:
        self._tick_task = asyncio.create_task(self.run())

    async def shutdown(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        for ep_id in list(self.endpoints):
            await self.remove_endpoint(ep_id)

    # ------------------------------------------------------------------ état
    def _group_name(self, key: frozenset[str]) -> str:
        return " ↔ ".join(self.channels[c].name for c in sorted(key) if c in self.channels)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "channels": [c.__dict__ for c in self.channels.values()],
            "endpoints": [e.snapshot() for e in self.endpoints.values()],
            "patches": [{"id": p.id, "channels": sorted(p.channels), "by": p.created_by} for p in self.patches.values()],
            "floors": [
                {
                    "channels": sorted(k),
                    "talker": f.talker,
                    "talker_name": self.endpoints[f.talker].name if f.talker in self.endpoints else f.talker,
                    "seconds": round(now - f.started, 1),
                }
                for k, f in self.floors.items()
            ],
        }

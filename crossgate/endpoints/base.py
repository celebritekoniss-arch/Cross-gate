"""Point de raccordement au bus : une radio, un smartphone, un lien IPSC, le simulateur…"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from crossgate.audio import Frame

if TYPE_CHECKING:
    from crossgate.bus import Bus

log = logging.getLogger("crossgate")


class Endpoint:
    kind = "generique"

    def __init__(self, id: str, name: str, channel: str, priority: int = 0, queue_frames: int = 50):
        self.id = id
        self.name = name
        self.channel = channel
        self.priority = priority
        self.status = "arret"  # arret | en_ligne | degrade | erreur
        self.detail = ""
        self.bus: Bus | None = None
        # File d'émission vers ce point (audio venant des autres). Bornée : en cas de retard,
        # on jette les trames les plus anciennes plutôt que d'accumuler de la latence.
        self.out_queue: asyncio.Queue[Frame | None] = asyncio.Queue(maxsize=queue_frames)
        self.rx_talker: str | None = None  # nom de celui qu'on entend actuellement

    # ----- cycle de vie -----
    async def start(self, bus: "Bus") -> None:
        self.bus = bus
        self.status = "en_ligne"

    async def stop(self) -> None:
        self.status = "arret"

    def set_status(self, status: str, detail: str = "") -> None:
        changed = (status, detail) != (self.status, self.detail)
        self.status, self.detail = status, detail
        if changed and self.bus:
            level = "info" if status == "en_ligne" else ("erreur" if status == "erreur" else "alerte")
            self.bus.events.emit("etat", f"{self.name} : {status}{' – ' + detail if detail else ''}", level, endpoint=self.id)
            self.bus.notify_change()

    # ----- audio entrant (venant des autres) -----
    def deliver(self, frame: Frame) -> None:
        """Appelé par le bus pour chaque trame à faire entendre sur ce point."""
        if self.out_queue.full():
            try:
                self.out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.out_queue.put_nowait(frame)

    def on_rx_start(self, talker_name: str, channel: str) -> None:
        self.rx_talker = talker_name

    def on_rx_end(self) -> None:
        self.rx_talker = None
        try:
            self.out_queue.put_nowait(None)  # marqueur de fin d'émission
        except asyncio.QueueFull:
            pass

    def on_floor(self, state: str, info: dict[str, Any]) -> None:
        """granted | denied | released | preempted | timeout"""

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "channel": self.channel,
            "priority": self.priority,
            "status": self.status,
            "detail": self.detail,
            "rx": self.rx_talker,
        }

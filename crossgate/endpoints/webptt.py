"""Smartphone ou poste de console connecté en WebSocket (alternat explicite)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from crossgate.audio import Frame, to_bytes
from crossgate.endpoints.base import Endpoint

log = logging.getLogger("crossgate")


class WebPttEndpoint(Endpoint):
    kind = "smartphone"

    def __init__(self, id: str, name: str, channel: str, priority: int, username: str, role: str):
        super().__init__(id, name, channel, priority)
        self.username = username
        self.role = role
        # File unique de sortie : octets (audio) ou dict (messages JSON), dans l'ordre d'émission
        self.outbox: asyncio.Queue[bytes | dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.position: dict[str, Any] | None = None

    def _send(self, item: bytes | dict[str, Any]) -> None:
        if self.outbox.full():
            # Client trop lent (réseau dégradé) : on sacrifie l'audio le plus ancien
            try:
                self.outbox.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.outbox.put_nowait(item)

    def deliver(self, frame: Frame) -> None:
        self._send(to_bytes(frame))

    def on_rx_start(self, talker_name: str, channel: str) -> None:
        super().on_rx_start(talker_name, channel)
        self._send({"t": "rx", "talker": talker_name, "channel": channel})

    def on_rx_end(self) -> None:
        self.rx_talker = None
        self._send({"t": "rx_end"})

    def on_floor(self, state: str, info: dict[str, Any]) -> None:
        self._send({"t": "floor", "state": state, **info})

    def send_json(self, msg: dict[str, Any]) -> None:
        self._send(msg)

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap["user"] = self.username
        return snap

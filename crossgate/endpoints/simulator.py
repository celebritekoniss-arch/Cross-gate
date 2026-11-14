"""Radio simulée, pour tester sans matériel.

- echo : répète ce qu'elle vient d'entendre, une seconde après la fin de l'émission (test « perroquet ») ;
- bip  : émet un signal de test à intervalle régulier.
"""

from __future__ import annotations

import asyncio

from crossgate.audio import tone
from crossgate.endpoints.base import Endpoint


class SimulatorEndpoint(Endpoint):
    kind = "simulateur"

    def __init__(self, id: str, name: str, channel: str, priority: int = 0, mode: str = "echo", interval_s: float = 30.0):
        super().__init__(id, name, channel, priority, queue_frames=3000)  # 60 s d'écho au plus
        self.mode = mode
        self.interval = interval_s
        self.task: asyncio.Task | None = None

    async def start(self, bus) -> None:
        await super().start(bus)
        self.task = asyncio.create_task(self._echo() if self.mode == "echo" else self._beacon())
        self.detail = "répète ce qu'elle entend" if self.mode == "echo" else f"bip toutes les {self.interval:g} s"

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        await super().stop()

    async def _transmit(self, frames) -> None:
        assert self.bus
        for f in frames:
            if not self.bus.push_audio(self, f, implicit=True):
                return  # canal occupé : on abandonne
            await asyncio.sleep(0.02)
        self.bus.end_audio(self)

    async def _echo(self) -> None:
        recorded = []
        while True:
            frame = await self.out_queue.get()
            if frame is None:
                if recorded:
                    await asyncio.sleep(1.0)
                    await self._transmit(recorded)
                recorded = []
            else:
                recorded.append(frame)

    async def _beacon(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            frames = tone(880, 15) + tone(0, 5, 0) + tone(660, 15)
            await self._transmit(frames)
            # Vide ce qui a été reçu entre-temps (un simulateur bip n'écoute pas)
            while not self.out_queue.empty():
                self.out_queue.get_nowait()

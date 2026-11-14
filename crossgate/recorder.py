"""Enregistrement de chaque émission dans un fichier WAV (8 kHz, 16 bits, mono)."""

from __future__ import annotations

import re
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from crossgate.audio import SAMPLE_RATE, Frame, to_bytes

if TYPE_CHECKING:
    from crossgate.bus import Bus, Floor
    from crossgate.endpoints.base import Endpoint


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-")[:40] or "x"


class Recorder:
    def __init__(self, root: Path):
        self.root = root / "enregistrements"
        self.root.mkdir(parents=True, exist_ok=True)
        self.open: dict[str, tuple[wave.Wave_write, Path]] = {}

    def on_tx_start(self, ep: "Endpoint", group: frozenset[str], bus: "Bus") -> None:
        day = self.root / time.strftime("%Y-%m-%d")
        day.mkdir(exist_ok=True)
        name = f"{time.strftime('%H%M%S')}_{_safe('+'.join(sorted(group)))}_{_safe(ep.name)}.wav"
        path = day / name
        w = wave.open(str(path), "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        self.open[ep.id] = (w, path)

    def on_tx_frame(self, floor: "Floor", frame: Frame) -> None:
        entry = self.open.get(floor.talker)
        if entry:
            entry[0].writeframes(to_bytes(frame))

    def on_tx_end(self, floor: "Floor", bus: "Bus") -> None:
        entry = self.open.pop(floor.talker, None)
        if not entry:
            return
        w, path = entry
        w.close()
        if floor.frames == 0:
            path.unlink(missing_ok=True)  # prise de parole sans audio : rien à garder

    def list(self, limit: int = 200) -> list[dict]:
        files = sorted(self.root.glob("*/*.wav"), reverse=True)[:limit]
        return [
            {"path": f"{f.parent.name}/{f.name}", "size": f.stat().st_size, "seconds": round((f.stat().st_size - 44) / (SAMPLE_RATE * 2), 1)}
            for f in files
        ]

    def resolve(self, rel: str) -> Path | None:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents or not p.is_file() or p.suffix != ".wav":
            return None
        return p

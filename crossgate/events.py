"""Journal d'événements : mémoire récente pour la console + fichier JSONL horodaté (traçabilité)."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("crossgate")

LEVELS = ("info", "alerte", "erreur")


class EventLog:
    def __init__(self, data_dir: Path | None = None, maxlen: int = 500):
        self.recent: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.file = None
        if data_dir:
            (data_dir / "journal").mkdir(parents=True, exist_ok=True)
            self.path = data_dir / "journal" / f"{time.strftime('%Y-%m')}.jsonl"
            self.file = open(self.path, "a", encoding="utf-8")

    def emit(self, kind: str, message: str, level: str = "info", **data: Any) -> dict[str, Any]:
        event = {"ts": time.time(), "kind": kind, "level": level, "message": message, **data}
        self.recent.append(event)
        if self.file:
            self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.file.flush()
        getattr(log, "warning" if level != "info" else "info")("%s: %s", kind, message)
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:  # un auditeur défaillant ne doit pas bloquer le routage
                log.exception("Auditeur d'événements en erreur")
        return event

    def close(self) -> None:
        if self.file:
            self.file.close()
            self.file = None

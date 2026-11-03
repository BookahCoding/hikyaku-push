"""Pushes still to be delivered, kept on disk so a restart or a failing ntfy server doesn't lose them.

Each item is a sealed envelope: the message text is never written here in the clear. Items for one phone are
delivered oldest first. A failed attempt waits (5 s, 30 s, 5 min, then an hour); an item older than a day is
dropped, since the phone ignores anything older anyway; and a phone keeps at most MAX_PER_DEVICE items, oldest
dropped first.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional

from .store import _write_private

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (5, 30, 300, 3600)
MAX_AGE_SECONDS = 24 * 3600
MAX_PER_DEVICE = 200
# A new push for a phone that is backing off makes its oldest item retry within this long: ntfy may be back.
PROBE_SECONDS = 30


@dataclass
class Item:
    id: str
    device_id: str
    topic: str
    body: str
    queued_at: float
    attempts: int = 0
    next_try: float = 0.0


class Outbox:
    def __init__(self, path, now: Callable[[], float] = time.time, max_age: float = MAX_AGE_SECONDS,
                 max_per_device: int = MAX_PER_DEVICE):
        self.path = Path(path)
        self.now = now
        self.max_age = max_age
        self.max_per_device = max_per_device
        self._lock = threading.Lock()
        self._items: List[Item] = self._load()

    def _load(self) -> List[Item]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Item(**row) for row in raw["items"]]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            log.warning("herald-push: the outbox can't be read; starting with an empty one")
            return []

    def _save(self) -> None:
        _write_private(self.path, {"items": [asdict(item) for item in self._items]})

    def _expire(self) -> bool:
        cutoff = self.now() - self.max_age
        kept = [item for item in self._items if item.queued_at >= cutoff]
        changed = len(kept) != len(self._items)
        self._items = kept
        return changed

    def add(self, device_id: str, topic: str, body: str) -> Item:
        with self._lock:
            item = Item(uuid.uuid4().hex, device_id, topic, body, self.now())
            self._items.append(item)
            mine = [i for i in self._items if i.device_id == device_id]
            soon = self.now() + PROBE_SECONDS
            for waiting in mine:
                if waiting.next_try > soon:
                    waiting.next_try = soon
            excess = len(mine) - self.max_per_device
            if excess > 0:
                dropped = {i.id for i in mine[:excess]}
                self._items = [i for i in self._items if i.id not in dropped]
            self._expire()
            self._save()
            return replace(item)

    def head(self, device_id: str) -> Optional[Item]:
        """The oldest item for a phone, or None. A copy: change the queue only through this class."""
        with self._lock:
            if self._expire():
                self._save()
            for item in self._items:
                if item.device_id == device_id:
                    return replace(item)
            return None

    def done(self, item_id: str) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.id != item_id]
            self._save()

    def retry_later(self, item_id: str) -> None:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    item.attempts += 1
                    item.next_try = self.now() + BACKOFF_SECONDS[min(item.attempts, len(BACKOFF_SECONDS)) - 1]
            self._save()

    def device_ids(self) -> List[str]:
        with self._lock:
            seen: List[str] = []
            for item in self._items:
                if item.device_id not in seen:
                    seen.append(item.device_id)
            return seen

    def drop_device(self, device_id: str) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.device_id != device_id]
            self._save()

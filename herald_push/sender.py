"""Delivers pushes without letting one phone's server hold up another's.

``submit`` seals a message, writes it to the outbox and wakes that phone's worker. The worker posts the oldest
item, removes it once ntfy took it (or once retrying can't help) and otherwise tries again later, as the outbox
schedules. A phone that was removed or re-registered since an item was queued gets nothing: the sealed envelope
is bound to the topic it was queued for.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

import httpx

from . import push
from .outbox import Outbox
from .store import Device, Store

log = logging.getLogger(__name__)

# The longest a worker sleeps without looking again: a missed wake-up or a clock jump costs at most this.
MAX_SLEEP = 60.0
# After an unexpected error (a full disk, say) a worker waits this long before looking again.
ERROR_SLEEP = 5.0


class SendQueue:
    def __init__(self, store: Store, outbox: Outbox,
                 post: Callable[[Device, str, Optional[httpx.Client]], str] = push.post,
                 client: Optional[httpx.Client] = None, now: Callable[[], float] = time.time):
        self.store = store
        self.outbox = outbox
        self.post = post
        self.client = client
        self.now = now
        self._lock = threading.Lock()
        self._running = False
        self._stopping = threading.Event()
        self._wakes: Dict[str, threading.Event] = {}
        self._threads: Dict[str, threading.Thread] = {}

    def submit(self, device: Device, payload: dict, sig_key) -> bool:
        """Seals [payload] for [device] and queues it. False when it can't be queued (too large, or the disk
        refuses); the caller treats that as not sent."""
        try:
            body = push.envelope_for(device, payload, sig_key)
        except ValueError:
            log.warning("herald-push: a %s message is too large to send", payload.get("type"))
            return False
        try:
            self.outbox.add(device.device_id, device.push_topic, body)
        except OSError:
            log.exception("herald-push: couldn't queue a %s message", payload.get("type"))
            return False
        self._wake(device.device_id)
        return True

    def start(self) -> None:
        """Begins delivering, including whatever an earlier run left in the outbox."""
        with self._lock:
            self._running = True
        for device_id in self.outbox.device_ids():
            self._wake(device_id)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            threads = list(self._threads.values())
            events = list(self._wakes.values())
        self._stopping.set()
        for event in events:
            event.set()
        for thread in threads:
            thread.join(timeout=5)

    def step(self, device_id: str) -> Optional[float]:
        """Handles the oldest item for a phone if it is due. None: nothing is queued. 0.0: one item was handled,
        look again. Otherwise the seconds until the oldest item is due."""
        item = self.outbox.head(device_id)
        if item is None:
            return None
        wait = item.next_try - self.now()
        if wait > 0:
            return wait
        device = self.store.get(device_id)
        if device is None or device.push_topic != item.topic:
            self.outbox.done(item.id)  # the phone is gone or has new topics: this envelope can't reach it
            return 0.0
        try:
            outcome = self.post(device, item.body, self.client)
        except Exception:
            log.exception("herald-push: sending a push failed")
            outcome = push.RETRY
        if outcome == push.RETRY:
            self.outbox.retry_later(item.id)
        else:
            self.outbox.done(item.id)
        return 0.0

    def _wake(self, device_id: str) -> None:
        with self._lock:
            if not self._running:
                return
            event = self._wakes.get(device_id)
            if event is None:
                event = self._wakes[device_id] = threading.Event()
                thread = threading.Thread(target=self._work, args=(device_id, event),
                                          name=f"herald-push-send-{device_id[:8]}", daemon=True)
                self._threads[device_id] = thread
                thread.start()
        event.set()

    def _work(self, device_id: str, wake: threading.Event) -> None:
        while not self._stopping.is_set():
            try:
                wait = self.step(device_id)
            except Exception:
                log.exception("herald-push: the send queue hit an error")
                wait = ERROR_SLEEP
            if wait == 0.0:
                continue
            wake.wait(MAX_SLEEP if wait is None else min(wait, MAX_SLEEP))
            wake.clear()

import time

import pytest

from herald_push import crypto
from herald_push.store import Device


class Clock:
    """A settable time source: call it for the time, change ``t`` to move it."""

    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_device():
    count = {"n": 0}

    def make(device_id=None, server="https://ntfy.sh") -> Device:
        count["n"] += 1
        return Device.parse({
            "device_id": device_id or "00000000-0000-4000-8000-%012d" % count["n"],
            "name": "Phone %d" % count["n"], "server": server,
            "push_topic": crypto.new_topic(), "reply_topic": crypto.new_topic(),
            "enc_pub": crypto.encode_public(crypto.generate_key().public_key()),
            "sig_pub": crypto.encode_public(crypto.generate_key().public_key()),
        })

    return make


@pytest.fixture
def wait_until():
    def wait(condition, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return bool(condition())

    return wait

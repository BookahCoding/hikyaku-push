import threading

import pytest

from herald_push import crypto, push
from herald_push.outbox import Outbox
from herald_push.sender import SendQueue
from herald_push.store import Store

SIG = crypto.generate_key()


def build(tmp_path, clock, make_device, post, phones=1):
    store = Store(tmp_path / "data")
    devices = [make_device() for _ in range(phones)]
    for device in devices:
        store.upsert(device)
    box = Outbox(tmp_path / "data" / "outbox.json", now=clock)
    return store, box, SendQueue(store, box, post=post, now=clock), devices


def test_a_delivered_push_leaves_the_outbox(tmp_path, clock, make_device):
    calls = []

    def post(device, body, client):
        calls.append((device.device_id, body))
        return push.OK

    _, box, queue, (phone,) = build(tmp_path, clock, make_device, post)
    assert queue.submit(phone, push.message("ping"), SIG) is True
    assert queue.step(phone.device_id) == 0.0
    assert len(calls) == 1 and calls[0][0] == phone.device_id
    assert box.head(phone.device_id) is None
    assert queue.step(phone.device_id) is None


def test_a_failed_push_waits_then_is_retried_with_the_same_body(tmp_path, clock, make_device):
    answers, bodies = [push.RETRY, push.OK], []

    def post(device, body, client):
        bodies.append(body)
        return answers.pop(0)

    _, box, queue, (phone,) = build(tmp_path, clock, make_device, post)
    queue.submit(phone, push.message("ping"), SIG)
    assert queue.step(phone.device_id) == 0.0
    assert box.head(phone.device_id).attempts == 1
    assert queue.step(phone.device_id) == pytest.approx(5.0)
    assert len(bodies) == 1  # not due yet: nothing posted
    clock.t += 5
    assert queue.step(phone.device_id) == 0.0
    assert bodies[0] == bodies[1]
    assert box.head(phone.device_id) is None


def test_an_answer_retrying_cant_fix_is_dropped_not_retried(tmp_path, clock, make_device):
    _, box, queue, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: push.DROP)
    queue.submit(phone, push.message("ping"), SIG)
    queue.step(phone.device_id)
    assert box.head(phone.device_id) is None


def test_a_post_that_raises_counts_as_a_retry(tmp_path, clock, make_device):
    def post(device, body, client):
        raise RuntimeError("boom")

    _, box, queue, (phone,) = build(tmp_path, clock, make_device, post)
    queue.submit(phone, push.message("ping"), SIG)
    assert queue.step(phone.device_id) == 0.0
    assert box.head(phone.device_id).attempts == 1


def test_a_phone_that_re_registered_gets_nothing_on_its_old_topic(tmp_path, clock, make_device):
    calls = []
    store, box, queue, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: calls.append(b) or push.OK)
    queue.submit(phone, push.message("ping"), SIG)
    store.upsert(make_device(device_id=phone.device_id))  # same phone, new topics
    assert queue.step(phone.device_id) == 0.0
    assert calls == [] and box.head(phone.device_id) is None


def test_a_removed_phone_gets_nothing(tmp_path, clock, make_device):
    calls = []
    store, box, queue, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: calls.append(b) or push.OK)
    queue.submit(phone, push.message("ping"), SIG)
    store.remove(phone.device_id)
    queue.step(phone.device_id)
    assert calls == [] and box.head(phone.device_id) is None


def test_a_message_too_big_to_seal_is_refused_and_not_queued(tmp_path, clock, make_device):
    _, box, queue, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: push.OK)
    too_big = {"id": "x", "ts": 1, "type": "x", "blob": "y" * 10000}
    assert queue.submit(phone, too_big, SIG) is False
    assert box.head(phone.device_id) is None


def test_a_full_disk_refuses_instead_of_raising(tmp_path, clock, make_device, monkeypatch):
    _, box, queue, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: push.OK)

    def full(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(box, "add", full)
    assert queue.submit(phone, push.message("ping"), SIG) is False


def test_one_hung_server_does_not_hold_up_another_phone(tmp_path, clock, make_device, wait_until):
    release, delivered = threading.Event(), []
    store, box, queue, (stuck, fine) = build(tmp_path, clock, make_device, None, phones=2)

    def post(device, body, client):
        if device.device_id == stuck.device_id:
            release.wait(5)
            return push.OK
        delivered.append(device.device_id)
        return push.OK

    queue.post = post
    queue.start()
    try:
        queue.submit(stuck, push.message("ping"), SIG)
        queue.submit(fine, push.message("ping"), SIG)
        assert wait_until(lambda: delivered == [fine.device_id])
        assert box.head(stuck.device_id) is not None  # still waiting on its own server
    finally:
        release.set()
        assert wait_until(lambda: box.head(stuck.device_id) is None)
        queue.stop()


def test_start_resumes_what_a_restart_left_behind(tmp_path, clock, make_device, wait_until):
    store, box, first, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: push.OK)
    first.submit(phone, push.message("ping"), SIG)  # never delivered: the process "died" here
    calls = []
    reloaded = Outbox(tmp_path / "data" / "outbox.json", now=clock)
    second = SendQueue(store, reloaded, post=lambda d, b, c: calls.append(b) or push.OK, now=clock)
    second.start()
    try:
        assert wait_until(lambda: len(calls) == 1)
        assert wait_until(lambda: reloaded.head(phone.device_id) is None)
    finally:
        second.stop()


def test_stop_ends_the_workers(tmp_path, clock, make_device, wait_until):
    _, box, queue, (phone,) = build(tmp_path, clock, make_device, lambda d, b, c: push.OK)
    queue.start()
    queue.submit(phone, push.message("ping"), SIG)
    assert wait_until(lambda: box.head(phone.device_id) is None)
    threads = list(queue._threads.values())
    queue.stop()
    assert all(not t.is_alive() for t in threads)


def test_ntfy_coming_back_delivers_a_new_push_within_half_a_minute(tmp_path, clock, make_device):
    answers, bodies = [push.RETRY] * 4, []

    def post(device, body, client):
        bodies.append(body)
        return answers.pop(0) if answers else push.OK

    _, box, queue, (phone,) = build(tmp_path, clock, make_device, post)
    queue.submit(phone, push.message("ping"), SIG)
    for _ in range(4):
        assert queue.step(phone.device_id) == 0.0
        clock.t += 3600  # let each backoff pass
    assert box.head(phone.device_id) is not None
    clock.t -= 3600  # back to just after the 4th failure, an hour of backoff pending
    queue.submit(phone, push.message("ping"), SIG)
    assert queue.step(phone.device_id) == pytest.approx(30.0)
    clock.t += 30
    assert queue.step(phone.device_id) == 0.0
    assert box.head(phone.device_id).body != ""  # the old one went first
    assert queue.step(phone.device_id) == 0.0
    assert box.head(phone.device_id) is None

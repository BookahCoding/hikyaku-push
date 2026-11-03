import os
import stat
import sys

import pytest

from herald_push.outbox import BACKOFF_SECONDS, Outbox


def test_items_survive_a_restart(tmp_path, clock):
    path = tmp_path / "outbox.json"
    item = Outbox(path, now=clock).add("dev-1", "hp-a", "BODY")
    head = Outbox(path, now=clock).head("dev-1")
    assert (head.id, head.topic, head.body, head.attempts) == (item.id, "hp-a", "BODY", 0)


def test_oldest_first_for_each_device(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock)
    box.add("a", "t", "1")
    box.add("b", "t", "x")
    box.add("a", "t", "2")
    assert box.device_ids() == ["a", "b"]
    box.done(box.head("a").id)
    assert box.head("a").body == "2" and box.head("b").body == "x"


def test_backoff_steps_then_stays_hourly(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock)
    item = box.add("a", "t", "1")
    delays = []
    for _ in range(6):
        box.retry_later(item.id)
        delays.append(box.head("a").next_try - clock.t)
    assert delays == [5, 30, 300, 3600, 3600, 3600]
    assert tuple(delays[:4]) == BACKOFF_SECONDS
    assert box.head("a").attempts == 6


def test_an_item_expires_after_a_day_and_is_gone_from_disk(tmp_path, clock):
    path = tmp_path / "outbox.json"
    box = Outbox(path, now=clock)
    box.add("a", "t", "old")
    clock.t += 24 * 3600 - 1
    assert box.head("a").body == "old"
    clock.t += 2
    assert box.head("a") is None
    assert Outbox(path, now=clock).head("a") is None


def test_the_cap_drops_the_oldest(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock, max_per_device=3)
    for n in range(5):
        box.add("a", "t", str(n))
    box.add("b", "t", "other")
    bodies = []
    while True:
        head = box.head("a")
        if head is None:
            break
        bodies.append(head.body)
        box.done(head.id)
    assert bodies == ["2", "3", "4"]
    assert box.head("b").body == "other"


def test_an_unreadable_file_starts_empty_and_works(tmp_path, clock):
    path = tmp_path / "outbox.json"
    path.write_text("{not json", encoding="utf-8")
    box = Outbox(path, now=clock)
    assert box.head("a") is None
    box.add("a", "t", "fresh")
    assert Outbox(path, now=clock).head("a").body == "fresh"


def test_a_file_with_the_wrong_shape_starts_empty(tmp_path, clock):
    path = tmp_path / "outbox.json"
    path.write_text('{"items": [{"unexpected": 1}]}', encoding="utf-8")
    assert Outbox(path, now=clock).head("a") is None


def test_drop_device_forgets_only_that_device(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock)
    box.add("a", "t", "1")
    box.add("b", "t", "2")
    box.drop_device("a")
    assert box.head("a") is None and box.head("b").body == "2"


def test_a_copy_is_returned_so_callers_cant_edit_the_queue(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock)
    box.add("a", "t", "1")
    box.head("a").attempts = 99
    assert box.head("a").attempts == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_file_is_private(tmp_path, clock):
    path = tmp_path / "outbox.json"
    Outbox(path, now=clock).add("a", "t", "1")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_new_push_gives_a_backed_off_phone_an_early_retry(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock)
    old = box.add("a", "t", "old")
    for _ in range(4):
        box.retry_later(old.id)  # now waiting an hour
    assert box.head("a").next_try - clock.t == 3600
    clock.t += 400  # ntfy is back, and a fresh reply is queued
    box.add("a", "t", "fresh")
    assert box.head("a").body == "old"
    assert box.head("a").next_try - clock.t <= 30
    other = box.add("b", "t", "x")
    assert box.head("b").next_try == other.next_try  # untouched: nothing was backed off


def test_a_phone_not_backed_off_keeps_its_schedule(tmp_path, clock):
    box = Outbox(tmp_path / "outbox.json", now=clock)
    box.add("a", "t", "1")
    box.add("a", "t", "2")
    assert box.head("a").next_try == 0.0

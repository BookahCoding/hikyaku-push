import os
import sqlite3

from herald_push import push, watcher
from herald_push.store import Store


def _make_db(home):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                 " tool_calls TEXT, active INTEGER DEFAULT 1, display_kind TEXT, timestamp REAL)")
    conn.commit()
    conn.close()


def _setup(tmp_path, monkeypatch, make_device, clock, tip):
    root = tmp_path / "hermes"
    _make_db(root)
    reads = []

    def fake_tip(home):
        reads.append(home)
        return tip(len(reads))

    monkeypatch.setattr(watcher, "bot_chat_tip", fake_tip)
    monkeypatch.setattr(push, "publish", lambda *a, **k: True)
    store = Store(tmp_path / "data")
    store.upsert(make_device())
    signature = {"v": ("a",)}
    w = watcher.Watcher(root, store, now=clock, signature=lambda home: signature["v"])
    return w, reads, signature


def test_an_unchanged_database_is_not_read_again(tmp_path, monkeypatch, make_device, clock):
    w, reads, signature = _setup(tmp_path, monkeypatch, make_device, clock, lambda n: "s1")
    w.poll()
    w.poll()
    assert len(reads) == 1
    signature["v"] = ("b",)
    w.poll()
    assert len(reads) == 2
    w.poll()
    assert len(reads) == 2


def test_it_is_read_again_after_a_minute_whatever_the_signature_says(tmp_path, monkeypatch, make_device, clock):
    w, reads, _ = _setup(tmp_path, monkeypatch, make_device, clock, lambda n: "s1")
    w.poll()
    clock.t += watcher.FULL_READ_SECONDS - 1
    w.poll()
    assert len(reads) == 1
    clock.t += 1
    w.poll()
    assert len(reads) == 2


def test_a_read_that_failed_is_tried_again_at_once(tmp_path, monkeypatch, make_device, clock):
    def tip(n):
        if n <= 2:
            raise OSError("database is locked")
        return "s1"

    w, reads, _ = _setup(tmp_path, monkeypatch, make_device, clock, tip)
    w.poll()
    w.poll()
    w.poll()
    assert len(reads) == 3  # the signature never changed, yet each failed read was repeated
    w.poll()
    assert len(reads) == 3  # and once it worked, the unchanged database is left alone


def test_a_poll_that_raised_forgets_what_it_saw(tmp_path, monkeypatch, make_device, clock):
    w, reads, _ = _setup(tmp_path, monkeypatch, make_device, clock, lambda n: "s1")
    w.poll()
    w._seen.clear()  # what _run does after a poll raised
    w.poll()
    assert len(reads) == 2


def test_without_a_signature_function_everything_is_read_every_time(tmp_path, monkeypatch, make_device, clock):
    w, reads, _ = _setup(tmp_path, monkeypatch, make_device, clock, lambda n: "s1")
    w.signature = None
    w.poll()
    w.poll()
    assert len(reads) == 2


def test_db_signature_follows_the_database_and_its_log(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    assert watcher.db_signature(home) == (None, None)
    (home / "state.db").write_bytes(b"x" * 10)
    first = watcher.db_signature(home)
    assert first[0] is not None and first[1] is None
    (home / "state.db-wal").write_bytes(b"y")
    second = watcher.db_signature(home)
    assert second != first
    stat = os.stat(home / "state.db")
    os.utime(home / "state.db", ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert watcher.db_signature(home) != second

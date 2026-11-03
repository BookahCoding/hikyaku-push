import sqlite3

import pytest

from herald_push import push, watcher
from herald_push.sender import SendQueue
from herald_push.store import Store


def _make_db(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                 " tool_calls TEXT, active INTEGER DEFAULT 1, display_kind TEXT, timestamp REAL)")
    conn.executemany("INSERT INTO messages (session_id, role, content, tool_calls, active, display_kind, timestamp) "
                     "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _add(home, *rows):
    conn = sqlite3.connect(home / "state.db")
    conn.executemany("INSERT INTO messages (session_id, role, content, tool_calls, active, display_kind, timestamp) "
                     "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class FakeSender:
    def __init__(self, accept=True):
        self.items, self.accept = [], accept

    def submit(self, device, payload, sig_key):
        self.items.append((device.device_id, payload))
        return self.accept


def _watcher(tmp_path, monkeypatch, make_device, sender):
    now = 1_800_000_000.0
    root = tmp_path / "hermes"
    _make_db(root, [])
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: "s1" if (home / "state.db").exists() else None)
    monkeypatch.setattr(push, "publish", lambda *a, **k: pytest.fail("posted straight to ntfy"))
    store = Store(tmp_path / "data")
    device = make_device()
    store.upsert(device)
    return root, device, watcher.Watcher(root, store, now=lambda: now, sender=sender), now


def test_replies_go_to_the_sender_not_straight_to_ntfy(tmp_path, monkeypatch, make_device):
    sender = FakeSender()
    root, device, w, now = _watcher(tmp_path, monkeypatch, make_device, sender)
    w.poll()
    _add(root, ("s1", "assistant", "Hello", None, 1, None, now))
    assert w.poll() == 1
    (device_id, payload), = sender.items
    assert device_id == device.device_id
    assert payload["type"] == "bot_message" and payload["text"] == "Hello" and payload["bot"] == "default"


def test_a_refused_push_is_not_counted(tmp_path, monkeypatch, make_device):
    root, _, w, now = _watcher(tmp_path, monkeypatch, make_device, FakeSender(accept=False))
    w.poll()
    _add(root, ("s1", "assistant", "Hello", None, 1, None, now))
    assert w.poll() == 0


def test_without_a_sender_the_watcher_still_posts_directly(tmp_path, monkeypatch, make_device):
    root, _, w, now = _watcher(tmp_path, monkeypatch, make_device, None)
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append(payload) or True)
    w.poll()
    _add(root, ("s1", "assistant", "Hello", None, 1, None, now))
    assert w.poll() == 1 and sent[0]["text"] == "Hello"


def test_the_leader_sends_through_a_queue_and_stops_it(tmp_path, wait_until):
    root = tmp_path / "hermes"
    root.mkdir()
    w = watcher.Watcher(root, Store(tmp_path / "data"))
    w.start()
    try:
        assert wait_until(lambda: w.sender is not None)
        assert isinstance(w.sender, SendQueue)
        queue = w.sender
    finally:
        w.stop()
    assert queue._stopping.is_set()
    assert w.sender is None

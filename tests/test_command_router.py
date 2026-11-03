import json

import pytest

from herald_push import commands
from herald_push.phone import Phones
from herald_push.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def answer(handle, raw):
    return json.loads(handle(raw))


def handler(store, **kwargs):
    return commands.make_handler(store, "t", lambda: True, trusted=lambda: True, **kwargs)


def test_an_unknown_operation_gets_the_usage(store):
    assert answer(handler(store), "bogus") == {"ok": False, "error": commands.USAGE}


def test_the_trust_check_comes_first(store):
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: False)
    assert answer(handle, "bogus")["error"].startswith("Herald push can only be managed")


def test_too_long_is_refused(store):
    assert answer(handler(store), "register " + "x" * 9000) == {"ok": False, "error": "too long"}


def test_register_that_isnt_json_says_so(store):
    assert answer(handler(store), "register nope") == {"ok": False, "error": "not JSON"}


def test_the_empty_command_is_info(store):
    handle = handler(store)
    assert answer(handle, "") == answer(handle, "info")
    assert answer(handle, "info")["ok"] is True


@pytest.mark.parametrize("raw", ["cron inbox nope", "profiles get nope"])
def test_namespaces_report_arguments_that_arent_json(store, raw):
    assert answer(handler(store), raw) == {"ok": False, "error": "not JSON"}


@pytest.mark.parametrize("raw", ["cron inbox [1]", "profiles get 5"])
def test_namespaces_want_an_object_with_a_device_id(store, raw):
    assert answer(handler(store), raw) == {"ok": False, "error": "bad device_id"}


def test_the_phone_namespace_exists_only_with_phones(store):
    assert answer(handler(store), "phone tools {}") == {"ok": False, "error": commands.USAGE}
    with_phones = handler(store, phones=Phones(store))
    assert answer(with_phones, "phone tools nope") == {"ok": False, "error": "not JSON"}
    assert answer(with_phones, "phone tools [1]") == {"ok": False, "error": "not an object"}


def test_op_and_json_splits_and_parses(store):
    reply = lambda **data: json.dumps(data)  # noqa: E731
    assert commands.op_and_json('get {"a": 1}', reply) == ("get", {"a": 1}, None)
    op, data, bad = commands.op_and_json("get nope", reply)
    assert op == "get" and data is None and json.loads(bad) == {"ok": False, "error": "not JSON"}
    assert commands.op_and_json("get null", reply) == ("get", None, None)


def test_info_announces_reliable_push_next_to_the_profile_allowlist(store):
    features = answer(handler(store), "info")["features"]
    assert "reliable_push" in features and "profile_allowlist" in features

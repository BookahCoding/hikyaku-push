import pytest

from herald_push import approvals, push
from herald_push.approvals import DEDUPE_SECONDS, MAX_COMMAND, MAX_DESCRIPTION, ApprovalNotifier
from herald_push.store import Store


@pytest.fixture(autouse=True)
def fresh_dedupe_map():
    approvals._recent.clear()
    yield
    approvals._recent.clear()


class Sent:
    def __init__(self):
        self.posts, self.payloads = [], []

    def seal(self, device, payload, sig_key):
        self.payloads.append((device.device_id, payload))
        return "BODY-" + device.device_id[-2:]

    def post(self, device, body, client=None):
        self.posts.append((device.device_id, body))
        return push.OK


def build(tmp_path, clock, make_device, *, redact=lambda text: text.replace("SECRET", "***"), enabled=True,
          spawn=None, phones=1, profile="default", real_seal=False, resolve=lambda session_key: "", sent=None,
          store=None):
    store = store or Store(tmp_path / "data")
    devices = [make_device() for _ in range(phones)]
    for device in devices:
        store.upsert(device)
    sent = sent or Sent()
    notifier = ApprovalNotifier(
        store, profile, tmp_path / "home", tmp_path / "hermes", now=clock, post=sent.post,
        seal=push.envelope_for if real_seal else sent.seal, redact=redact,
        enabled=lambda home: enabled, spawn=spawn or (lambda fn: fn()), resolve=resolve)
    return store, devices, sent, notifier


def ask(notifier, **overrides):
    args = dict(command="rm -rf build", description="recursive delete", pattern_key="rm", session_key="s1",
                surface="gateway")
    args.update(overrides)
    notifier.on_request(**args)


def test_an_approval_request_reaches_the_phone(tmp_path, clock, make_device):
    _, (phone,), sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier)
    assert [device_id for device_id, _ in sent.posts] == [phone.device_id]
    (_, payload), = sent.payloads
    assert payload["type"] == "approval" and payload["bot"] == "default" and payload["label"] == "default"
    assert payload["session"] == "s1" and payload["description"] == "recursive delete"
    assert payload["command"] == "rm -rf build"
    assert len(payload["request_id"]) == 12 and "choices" not in payload and "expires" not in payload


def test_the_cli_surface_pushes_too(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier, surface="cli")
    assert len(sent.posts) == 1


@pytest.mark.parametrize("surface", ["smart", "transport:other", "mcp-elicitation/x", "vault-payment", "", None])
def test_surfaces_where_no_person_is_asked_here_push_nothing(tmp_path, clock, make_device, surface):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier, surface=surface)
    assert sent.posts == []


def test_a_coalesced_follower_pushes_nothing(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier, coalesced=True)
    assert sent.posts == []


def test_only_phones_allowed_that_profile_are_told(tmp_path, clock, make_device):
    store, (allowed, other), sent, notifier = build(tmp_path, clock, make_device, phones=2)
    store.set_allowed_profiles(allowed.device_id, ["default"])
    store.set_allowed_profiles(other.device_id, ["somebody-else"])
    ask(notifier)
    assert [device_id for device_id, _ in sent.posts] == [allowed.device_id]


def test_a_phone_with_no_list_hears_every_profile(tmp_path, clock, make_device):
    _, (phone,), sent, notifier = build(tmp_path, clock, make_device, profile="side")
    ask(notifier)
    assert [device_id for device_id, _ in sent.posts] == [phone.device_id]
    assert sent.payloads[0][1]["bot"] == "side"


def test_the_sessions_own_profile_decides_who_is_told(tmp_path, clock, make_device):
    """The hook ran in one profile's plugin, but the session belongs to another: the allowlist and the label
    follow the session."""
    store, (phone_default, phone_other), sent, notifier = build(
        tmp_path, clock, make_device, phones=2, profile="default", resolve=lambda session_key: "other")
    store.set_allowed_profiles(phone_default.device_id, ["default"])
    store.set_allowed_profiles(phone_other.device_id, ["other"])
    ask(notifier)
    assert [device_id for device_id, _ in sent.posts] == [phone_other.device_id]
    assert sent.payloads[0][1]["bot"] == "other"


def test_a_session_that_belongs_to_no_profile_uses_the_plugins_own(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device, profile="side", resolve=lambda session_key: "")
    ask(notifier)
    assert sent.payloads[0][1]["bot"] == "side"


def test_two_notifiers_that_see_the_same_request_push_once(tmp_path, clock, make_device):
    store, devices, sent, first = build(tmp_path, clock, make_device, profile="default")
    _, _, _, second = build(tmp_path, clock, make_device, profile="side", sent=sent, store=store, phones=0)
    ask(first)
    ask(second)
    assert len(sent.posts) == 1


def test_the_secret_in_a_command_is_redacted(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier, command="curl -H token=SECRET https://example.test")
    command = sent.payloads[0][1]["command"]
    assert "SECRET" not in command and "***" in command


def test_when_redaction_is_unavailable_only_the_description_goes(tmp_path, clock, make_device):
    def missing(text):
        raise ImportError("no agent.redact")

    _, _, sent, notifier = build(tmp_path, clock, make_device, redact=missing)
    ask(notifier, command="curl -H token=SECRET https://example.test")
    payload = sent.payloads[0][1]
    assert "command" not in payload and payload["description"] == "recursive delete"


def test_long_text_is_clipped(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier, command="c" * 400, description="d" * 500)
    payload = sent.payloads[0][1]
    assert len(payload["command"]) <= MAX_COMMAND and len(payload["description"]) <= MAX_DESCRIPTION
    assert payload["command"].endswith("…") and payload["description"].endswith("…")


def test_a_bot_with_notifications_off_sends_nothing(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device, enabled=False)
    ask(notifier)
    assert sent.posts == []


def test_the_same_request_is_pushed_once_a_minute(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    ask(notifier)
    ask(notifier)
    assert len(sent.posts) == 1
    ask(notifier, command="ls")  # a different request is its own push
    assert len(sent.posts) == 2
    clock.t += DEDUPE_SECONDS
    ask(notifier)
    assert len(sent.posts) == 3


def test_the_hook_returns_before_any_post_runs(tmp_path, clock, make_device):
    pending = []
    _, _, sent, notifier = build(tmp_path, clock, make_device, spawn=pending.append)
    ask(notifier)
    assert sent.posts == [] and len(pending) == 1
    pending[0]()
    assert len(sent.posts) == 1


def test_no_registered_phone_means_no_work(tmp_path, clock, make_device):
    pending = []
    _, _, sent, notifier = build(tmp_path, clock, make_device, phones=0, spawn=pending.append)
    ask(notifier)
    assert pending == [] and sent.posts == []


def test_a_failing_post_never_reaches_the_agent(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)

    def boom(device, body, client=None):
        raise RuntimeError("ntfy is on fire")

    notifier.post = boom
    ask(notifier)  # must not raise


def test_a_failing_seal_never_reaches_the_agent(tmp_path, clock, make_device):
    _, _, _, notifier = build(tmp_path, clock, make_device)

    def too_big(device, payload, sig_key):
        raise ValueError("push message too large")

    notifier.seal = too_big
    ask(notifier)  # must not raise


def test_a_failing_session_lookup_falls_back_and_never_raises(tmp_path, clock, make_device):
    def broken(session_key):
        raise OSError("state.db is locked")

    _, _, sent, notifier = build(tmp_path, clock, make_device, resolve=broken)
    ask(notifier)
    assert len(sent.posts) == 1 and sent.payloads[0][1]["bot"] == "default"


def test_a_hook_call_with_odd_arguments_never_raises(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device)
    notifier.on_request()
    notifier.on_request(command=None, description=None, session_key=None, pattern_key=None, surface="gateway")
    notifier.on_request(command=5, description=["x"], surface="gateway", extra="ignored")


def test_the_worst_case_escaped_text_still_fits_one_push(tmp_path, clock, make_device):
    _, _, sent, notifier = build(tmp_path, clock, make_device, real_seal=True, redact=lambda text: text)
    # Bounds the worst case at the clip limits: control characters, which JSON writes as six bytes each (\u0001) and
    # clipping leaves alone. It fits with room to spare; SHRINKABLE (push.py) is the margin for a longer label or id.
    ask(notifier, command="\x01" * 400, description="\x01" * 500)
    assert sent.posts, "the push was refused instead of shortened"
    assert all(len(body.encode("utf-8")) <= push.MAX_BODY for _, body in sent.posts)

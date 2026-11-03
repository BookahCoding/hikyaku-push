import httpx
import pytest

from herald_push import crypto, push


def client_answering(status=None, raises=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raises is not None:
            raise raises
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [200, 202, 299])
def test_a_2xx_answer_is_delivery(make_device, status):
    assert push.post(make_device(), "body", client_answering(status)) == push.OK


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_busy_or_broken_servers_are_worth_retrying(make_device, status):
    assert push.post(make_device(), "body", client_answering(status)) == push.RETRY


@pytest.mark.parametrize("status", [301, 400, 403, 404, 413])
def test_answers_that_retrying_cannot_fix_are_dropped(make_device, status):
    assert push.post(make_device(), "body", client_answering(status)) == push.DROP


def test_no_connection_is_worth_retrying(make_device):
    client = client_answering(raises=httpx.ConnectError("down"))
    assert push.post(make_device(), "body", client) == push.RETRY


def test_post_sends_the_body_to_the_phones_topic(make_device):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.content, request.headers.get("content-type")))
        return httpx.Response(200)

    device = make_device()
    push.post(device, "sealed-body", httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen == [(f"{device.server}/{device.push_topic}", b"sealed-body", "text/plain")]


def test_publish_keeps_its_meaning(make_device):
    device, sig = make_device(), crypto.generate_key()
    assert push.publish(device, push.message("ping"), sig, client_answering(200)) is True
    assert push.publish(device, push.message("ping"), sig, client_answering(500)) is False
    too_big = {"id": "x", "ts": 1, "type": "x", "blob": "y" * 10000}
    assert push.publish(device, too_big, sig, client_answering(200)) is False

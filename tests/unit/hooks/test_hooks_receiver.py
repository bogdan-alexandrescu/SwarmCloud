"""The public receiver `swarm-github-hooks` does exactly what
docs/design/graph-service.md §6.2 says, and nothing more (lane GS-HOOKS, part
of #1384).

It is the one unauthenticated door into the platform, so each rule is held
here with its control, the request that differs from a refused one in the
single property the rule is about:

  * only `POST /github/events`, a body of at most 1 MiB (refused from its
    Content-Length, before a byte is read);
  * a valid `X-Hub-Signature-256` -- HMAC-SHA256 of the raw body under the
    App's webhook secret -- or a 401 that never reaches Firestore;
  * `ping` is a 204, any event but `push` is a 204 and dropped;
  * only a non-deleting push to `refs/heads/<repository.default_branch>`;
  * `X-GitHub-Delivery` deduped by `create()`, a redelivery is a 200 and
    nothing more, the document expires in 7 days;
  * swarm-api is told the repository's name and nothing else -- never the
    payload's `after` sha;
  * one log line per delivery, carrying the delivery id and the outcome and
    never the payload or another header's value.

The secret is cached; a mismatch re-reads it at most once a minute, so a
stranger's bad signatures cannot turn into a stream of Secret Manager reads.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta

import pytest

from swarm_github_hooks.receiver import (
    DELIVERY_TTL,
    MAX_BODY_BYTES,
    SECRET_REFRESH_SECONDS,
    Receiver,
    WebhookSecret,
)

from hooks_fixtures import NOW, Delivery, Forward, Secret, Store, new_secret, push, sign


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def key() -> bytes:
    return new_secret()


@pytest.fixture
def parts(key):
    source = Secret(key)
    clock = Clock()
    store = Store()
    forward = Forward()
    receiver = Receiver(secret=WebhookSecret(source, monotonic=clock), deliveries=store,
                        forward=forward, clock=lambda: NOW)
    return receiver, source, clock, store, forward


def _send(receiver: Receiver, delivery: Delivery):
    read: list[int] = []
    body = delivery.body

    def reader(n: int) -> bytes:
        read.append(n)
        return body[:n]

    reply = receiver.handle(delivery.method, delivery.path, delivery.headers(), reader)
    return reply, read


# --- the happy path, and what is forwarded ----------------------------------

def test_a_signed_push_to_the_default_branch_is_forwarded_by_name_only(parts, key):
    receiver, _, _, store, forward = parts
    delivery = push(key, after=secrets.token_hex(20))
    reply, _ = _send(receiver, delivery)

    assert reply.status == 200 and reply.outcome == "forwarded"
    assert forward.calls == [("Example-Org/Widgets",)], forward.calls
    assert list(store.docs) == [delivery.delivery]


def test_the_payloads_after_sha_never_reaches_swarm_api(parts, key):
    receiver, _, _, _, forward = parts
    after = secrets.token_hex(20)
    reply, _ = _send(receiver, push(key, after=after))
    assert reply.outcome == "forwarded"
    assert after not in repr(forward.calls)
    assert "refs/heads/main" not in repr(forward.calls)


def test_the_delivery_document_expires_in_seven_days(parts, key):
    receiver, _, _, store, _ = parts
    delivery = push(key)
    _send(receiver, delivery)
    assert DELIVERY_TTL == timedelta(days=7)
    assert store.docs[delivery.delivery] == NOW + timedelta(days=7)


# --- the path and the method ----------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/github/events"),
    ("PUT", "/github/events"),
    ("POST", "/"),
    ("POST", "/github/events/"),
    ("POST", "/github/events?x=1"),
    ("POST", "/v1/admin/repositories/push"),
    ("POST", "/healthz"),
])
def test_only_post_github_events_is_served(parts, key, method, path):
    receiver, source, _, store, forward = parts
    delivery = push(key)
    delivery.method, delivery.path = method, path
    reply, read = _send(receiver, delivery)
    assert reply.status == 404 and reply.outcome == "not_found"
    assert read == [] and store.calls == [] and forward.calls == [] and source.reads == 0


# --- the size cap ----------------------------------------------------------------

def test_a_body_over_one_mib_is_refused_before_it_is_read(parts, key):
    receiver, source, _, store, forward = parts
    delivery = push(key)
    delivery.length = MAX_BODY_BYTES + 1
    reply, read = _send(receiver, delivery)
    assert MAX_BODY_BYTES == 1024 * 1024
    assert reply.status == 413 and reply.outcome == "too_large"
    assert read == [] and source.reads == 0 and store.calls == [] and forward.calls == []


def test_a_body_of_exactly_one_mib_is_read(parts, key):
    """The control: the cap is inclusive of 1 MiB, and a body that size is
    read and judged on its signature, not its length."""
    receiver, _, _, _, _ = parts
    body = b" " * MAX_BODY_BYTES
    reply, read = _send(receiver, Delivery(secret=key, body=body, signature="sha256=" + "0" * 64))
    assert read == [MAX_BODY_BYTES]
    assert reply.status == 401


@pytest.mark.parametrize("length", ["", "-1", "abc", "1e3"])
def test_a_missing_or_malformed_length_is_refused_unread(parts, key, length):
    receiver, _, _, store, _ = parts
    delivery = push(key)
    headers = [(n, v) for n, v in delivery.headers() if n != "Content-Length"]
    if length:
        headers.append(("Content-Length", length))
    read: list[int] = []
    reply = receiver.handle("POST", "/github/events", headers, lambda n: read.append(n) or b"")
    assert reply.status == 411 and reply.outcome == "length_required"
    assert read == [] and store.calls == []


def test_a_short_body_is_refused(parts, key):
    """A body that ends before its Content-Length is never verified as if it
    were whole."""
    receiver, _, _, store, _ = parts
    delivery = push(key)
    reply = receiver.handle("POST", "/github/events", delivery.headers(), lambda n: delivery.body[:-5])
    assert reply.status == 400 and store.calls == []


# --- the signature ---------------------------------------------------------------

def test_an_unsigned_request_is_401_and_never_reaches_firestore(parts, key):
    receiver, _, _, store, forward = parts
    delivery = push(key)
    delivery.signature = ""
    reply, _ = _send(receiver, delivery)
    assert reply.status == 401 and reply.outcome == "unsigned"
    assert store.calls == [] and forward.calls == []


@pytest.mark.parametrize("signature", [
    "sha256=" + "0" * 64,
    "sha1=" + "0" * 40,
    "sha256=",
    "sha256=zz",
    "garbage",
])
def test_a_badly_signed_request_is_401_and_never_reaches_firestore(parts, key, signature):
    receiver, _, _, store, forward = parts
    delivery = push(key)
    delivery.signature = signature
    reply, _ = _send(receiver, delivery)
    assert reply.status == 401
    assert store.calls == [] and forward.calls == []


def test_a_signature_under_another_secret_is_401(parts, key):
    receiver, _, _, store, _ = parts
    delivery = push(key)
    delivery.signature = sign(new_secret(), delivery.body)
    reply, _ = _send(receiver, delivery)
    assert reply.status == 401 and reply.outcome == "bad_signature"
    assert store.calls == []


def test_a_signature_over_another_body_is_401(parts, key):
    """The MAC is over the RAW body: the same payload re-serialised is not it."""
    receiver, _, _, store, _ = parts
    delivery = push(key)
    delivery.signature = sign(key, json.dumps(json.loads(delivery.body), indent=1).encode())
    reply, _ = _send(receiver, delivery)
    assert reply.status == 401 and store.calls == []


def test_the_signature_is_compared_in_constant_time():
    import inspect

    from swarm_github_hooks import receiver

    source = inspect.getsource(receiver)
    assert "hmac.compare_digest(" in source
    assert "== expected" not in source and "expected ==" not in source


def test_an_unsigned_ping_is_401_too(parts, key):
    """Nothing but a 401 is answered to a request without a valid signature,
    whatever event it claims."""
    receiver, _, _, _, _ = parts
    reply, _ = _send(receiver, Delivery(secret=key, body=b'{"zen":"x"}', event="ping", signature=""))
    assert reply.status == 401


# --- the secret: cached, re-read on a mismatch at most once a minute -----------

def test_the_secret_is_read_once_and_cached(parts, key):
    receiver, source, _, _, _ = parts
    for _ in range(3):
        assert _send(receiver, push(key))[0].outcome == "forwarded"
    assert source.reads == 1


def test_a_rotated_secret_is_picked_up_on_the_first_mismatch(parts, key):
    receiver, source, clock, _, _ = parts
    assert _send(receiver, push(key))[0].outcome == "forwarded"
    rotated = new_secret()
    source.value = rotated
    clock.t += SECRET_REFRESH_SECONDS
    reply, _ = _send(receiver, push(rotated))
    assert reply.outcome == "forwarded"
    assert source.reads == 2


def test_bad_signatures_re_read_the_secret_at_most_once_a_minute(parts, key):
    receiver, source, clock, store, _ = parts
    assert _send(receiver, push(key))[0].outcome == "forwarded"
    assert SECRET_REFRESH_SECONDS == 60.0
    clock.t += SECRET_REFRESH_SECONDS
    for _ in range(20):
        delivery = push(key)
        delivery.signature = sign(new_secret(), delivery.body)
        assert _send(receiver, delivery)[0].status == 401
        clock.t += 1
    # One re-read for the first mismatch; the next 19 fall inside its minute.
    assert source.reads == 2
    clock.t += SECRET_REFRESH_SECONDS
    delivery = push(key)
    delivery.signature = "sha256=" + "0" * 64
    _send(receiver, delivery)
    assert source.reads == 3


def test_an_unreadable_secret_is_503_and_reaches_nothing(key):
    """Deployed before the owner stored the secret: every signed request is
    503, nothing is written, nothing forwarded, and Secret Manager is asked
    at most once a minute."""
    source = Secret(None)
    clock = Clock()
    store, forward = Store(), Forward()
    receiver = Receiver(secret=WebhookSecret(source, monotonic=clock), deliveries=store,
                        forward=forward, clock=lambda: NOW)
    for _ in range(5):
        reply, _ = _send(receiver, push(key))
        assert reply.status == 503 and reply.outcome == "secret_unavailable"
    assert source.reads == 1
    assert store.calls == [] and forward.calls == []
    # Stored later: picked up within the minute, without a restart.
    source.value = key
    clock.t += SECRET_REFRESH_SECONDS
    assert _send(receiver, push(key))[0].outcome == "forwarded"


def test_an_unsigned_request_never_reads_the_secret(key):
    source = Secret(key)
    receiver = Receiver(secret=WebhookSecret(source, monotonic=Clock()), deliveries=Store(),
                        forward=Forward(), clock=lambda: NOW)
    delivery = push(key)
    delivery.signature = ""
    _send(receiver, delivery)
    assert source.reads == 0


# --- the event filter ------------------------------------------------------------

def test_a_signed_ping_is_204_and_writes_nothing(parts, key):
    receiver, _, _, store, forward = parts
    body = b'{"zen":"Keep it logically awesome.","hook_id":1}'
    reply, _ = _send(receiver, Delivery(secret=key, body=body, event="ping"))
    assert reply.status == 204 and reply.outcome == "ping"
    assert store.calls == [] and forward.calls == []


@pytest.mark.parametrize("event", ["pull_request", "issues", "installation", "create", "delete",
                                   "Push", "push ", ""])
def test_any_other_event_is_204_and_dropped(parts, key, event):
    receiver, _, _, store, forward = parts
    delivery = push(key)
    delivery.event = event
    reply, _ = _send(receiver, delivery)
    assert reply.status == 204 and reply.outcome == "ignored_event"
    assert store.calls == [] and forward.calls == []


# --- the branch and the deletion -------------------------------------------------

@pytest.mark.parametrize("ref,default", [
    ("refs/heads/feature", "main"),
    ("refs/heads/main", "develop"),
    ("refs/tags/main", "main"),
    ("refs/heads/main/x", "main"),
    ("main", "main"),
])
def test_a_push_to_another_ref_is_dropped(parts, key, ref, default):
    receiver, _, _, store, forward = parts
    reply, _ = _send(receiver, push(key, ref=ref, default_branch=default))
    assert reply.status == 204 and reply.outcome == "not_default_branch"
    assert store.calls == [] and forward.calls == []


def test_a_push_to_a_non_main_default_branch_is_kept(parts, key):
    receiver, _, _, _, forward = parts
    reply, _ = _send(receiver, push(key, ref="refs/heads/trunk", default_branch="trunk"))
    assert reply.outcome == "forwarded" and len(forward.calls) == 1


@pytest.mark.parametrize("deleted", [True, None, "false", 0])
def test_a_deletion_or_an_unclear_one_is_dropped(parts, key, deleted):
    """`deleted` must be exactly false: a deletion, a missing flag or a
    stringly one is not a push this receiver forwards."""
    receiver, _, _, store, forward = parts
    reply, _ = _send(receiver, push(key, deleted=deleted))
    assert reply.status == 204 and reply.outcome == "deleted"
    assert store.calls == [] and forward.calls == []


@pytest.mark.parametrize("body", [
    b"not json",
    b"[]",
    b'{"ref":"refs/heads/main","deleted":false}',
    b'{"ref":"refs/heads/main","deleted":false,"repository":{"default_branch":"main"}}',
    b'{"ref":"refs/heads/main","deleted":false,"repository":{"full_name":"a/b"}}',
    b'{"ref":"refs/heads/main","deleted":false,"repository":{"full_name":"a/b/c","default_branch":"main"}}',
    b'{"ref":"refs/heads/main","deleted":false,"repository":{"full_name":"../b","default_branch":"main"}}',
    b'{"ref":"refs/heads/main","deleted":false,"repository":{"full_name":7,"default_branch":"main"}}',
])
def test_a_signed_but_malformed_payload_is_400_and_forwards_nothing(parts, key, body):
    receiver, _, _, store, forward = parts
    reply, _ = _send(receiver, Delivery(secret=key, body=body))
    assert reply.status == 400 and reply.outcome == "bad_payload"
    assert store.calls == [] and forward.calls == []


# --- the delivery dedup ----------------------------------------------------------

def test_a_redelivery_is_200_and_nothing_more(parts, key):
    receiver, _, _, store, forward = parts
    first = push(key)
    assert _send(receiver, first)[0].outcome == "forwarded"
    again = Delivery(secret=key, body=first.body, delivery=first.delivery)
    reply, _ = _send(receiver, again)
    assert reply.status == 200 and reply.outcome == "duplicate"
    assert len(forward.calls) == 1
    assert store.calls == [first.delivery, first.delivery]


def test_two_deliveries_of_the_same_push_are_both_forwarded(parts, key):
    """The control: dedup is by delivery id, not by body."""
    receiver, _, _, _, forward = parts
    first = push(key)
    _send(receiver, first)
    _send(receiver, Delivery(secret=key, body=first.body))
    assert len(forward.calls) == 2


@pytest.mark.parametrize("delivery", ["", "../x", "a/b", "x" * 200, "id with space"])
def test_a_missing_or_unsafe_delivery_id_is_400(parts, key, delivery):
    receiver, _, _, store, forward = parts
    d = push(key)
    d.delivery = delivery
    reply, _ = _send(receiver, d)
    assert reply.status == 400 and reply.outcome == "bad_delivery"
    assert store.calls == [] and forward.calls == []


def test_a_firestore_failure_is_503_and_forwards_nothing(parts, key):
    receiver, _, _, store, forward = parts
    store.fail = True
    reply, _ = _send(receiver, push(key))
    assert reply.status == 503 and reply.outcome == "store_failed"
    assert forward.calls == []


def test_a_swarm_api_failure_is_502(parts, key):
    receiver, _, _, _, forward = parts
    forward.status = 503
    reply, _ = _send(receiver, push(key))
    assert reply.status == 502 and reply.outcome == "forward_failed"


def test_a_forwarder_exception_is_502_not_a_traceback(parts, key):
    receiver, _, _, _, _ = parts

    def boom(full_name: str) -> int:
        raise ConnectionError("swarm-api unreachable")

    receiver._forward = boom
    reply, _ = _send(receiver, push(key))
    assert reply.status == 502 and reply.outcome == "forward_failed"


# --- the log line ----------------------------------------------------------------

def test_one_log_line_per_delivery_with_its_id_and_outcome_and_nothing_else(parts, key, caplog):
    receiver, _, _, _, _ = parts
    after = secrets.token_hex(20)
    delivery = push(key, after=after)
    caplog.set_level(logging.INFO, logger="swarm_github_hooks")
    _send(receiver, delivery)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("swarm_github_hooks")]
    assert len(lines) == 1, lines
    line = json.loads(lines[0])
    assert line["event"] == "github_delivery"
    assert line["delivery"] == delivery.delivery and line["outcome"] == "forwarded"
    text = lines[0]
    for value in (after, "Example-Org", "Widgets", "refs/heads/main", sign(key, delivery.body),
                  key.decode(), "GitHub-Hookshot", "someone"):
        assert value not in text, f"the log line carries {value!r}"


def test_a_refused_delivery_logs_no_unvalidated_id(parts, key, caplog):
    receiver, _, _, _, _ = parts
    delivery = push(key)
    delivery.delivery = "../" + secrets.token_hex(8)
    delivery.signature = ""
    caplog.set_level(logging.INFO, logger="swarm_github_hooks")
    _send(receiver, delivery)
    (record,) = [r for r in caplog.records if r.name.startswith("swarm_github_hooks")]
    assert delivery.delivery not in record.getMessage()
    assert json.loads(record.getMessage())["outcome"] == "unsigned"

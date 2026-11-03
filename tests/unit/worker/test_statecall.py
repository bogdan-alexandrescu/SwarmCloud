"""`statecall.StateCall`: the signed wire under `RemoteControlPlane`.

Lane ISO-WORKER-CORE (docs/design/tenant-settings-integrity.md §4.1, §4.2,
§4.7; part of #1457). A recording transport stands in for `children.ChildApi`,
so each rule is read off exactly what the worker would have sent: the proof
over method, path, body hash and timestamp under its own purpose, nothing
sent before `state/open`, a retry of the SAME bytes with the child path's
backoff, and each refusal mapped to the error the lifecycle already acts on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from google.api_core import exceptions as core_exceptions

from agent_worker import statecall
from agent_worker.children import AttemptKey, ChildApiError, request_message
from agent_worker.errors import ControlPlaneError, FencedWriteRefused
from agent_worker.statecall import StateCall, StateRefused, state_request_message
from swarm_api import childkey as api_childkey

NONCE = "nonce-" + secrets.token_hex(8)


class Transport:
    """`ChildApi.call`, recorded; answers from `answers`, a list of a payload
    or a `ChildApiError` per call, the last repeated."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [{"ok": True}]
        self.sent: list[dict[str, Any]] = []

    def call(self, method: str, path: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        self.sent.append({"method": method, "path": path, "body": body,
                          "headers": dict(headers or {})})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return 200, answer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _calls(transport: Transport, clock: Clock | None = None) -> StateCall:
    clock = clock or Clock()
    return StateCall(transport, tenant_id="eng", task_id="task_1", attempt_id="att_1",
                     lease_id="lease_1", generation=3, sleep=clock.sleep,
                     clock=lambda: clock.now, wall=lambda: 1_700_000_000.0)


def _opened(transport: Transport, clock: Clock | None = None) -> tuple[StateCall, AttemptKey]:
    calls = _calls(transport, clock)
    key = AttemptKey()
    calls.open(key, nonce=NONCE, unprotected=False, restore_attempt_id=None,
               deadline_seconds=30)
    transport.sent.clear()
    return calls, key


def _verify(key: AttemptKey, sent: dict[str, Any], *, path: str, body: bytes) -> None:
    raw = base64.urlsafe_b64decode(key.public + "=" * (-len(key.public) % 4))
    proof = sent["headers"]["X-Swarm-Attempt-Proof"]
    signature = base64.urlsafe_b64decode(proof + "=" * (-len(proof) % 4))
    timestamp = sent["headers"]["X-Swarm-Attempt-Timestamp"]
    message = api_childkey.state_request_message(sent["method"], path, body, timestamp)
    Ed25519PublicKey.from_public_bytes(raw).verify(signature, message)


def test_the_message_is_swarm_apis_and_not_the_child_routes():
    body = b'{"a":1}'
    mine = state_request_message("post", "/v1/attempts/a/state/heartbeat", body, "17")
    assert mine == api_childkey.state_request_message(
        "POST", "/v1/attempts/a/state/heartbeat", body, "17")
    assert mine.startswith(b"swarm-state-req/v1\n")
    assert mine != request_message("POST", "/v1/attempts/a/state/heartbeat", body, "17")
    assert hashlib.sha256(body).hexdigest().encode() in mine


def test_open_is_unsigned_and_spends_the_nonce_on_the_key():
    transport = Transport({"ok": True, "registration": {"registered": "key"}})
    calls = _calls(transport)
    key = AttemptKey()

    calls.open(key, nonce=NONCE, unprotected=True, restore_attempt_id="att_0",
               deadline_seconds=30)

    (sent,) = transport.sent
    assert sent["path"] == "/v1/attempts/att_1/state/open" and sent["headers"] == {}
    body = json.loads(sent["body"])
    assert body == {"tenant_id": "eng", "task_id": "task_1", "lease_id": "lease_1",
                    "generation": 3, "nonce": NONCE, "public_key": key.public,
                    "unprotected": True, "restore_attempt_id": "att_0"}
    assert calls.opened and calls.key is key


def test_nothing_is_sent_before_open():
    transport = Transport()
    calls = _calls(transport)

    with pytest.raises(ControlPlaneError):
        calls.post("heartbeat", {}, deadline_seconds=30)
    with pytest.raises(ControlPlaneError):
        calls.get("poll", {}, deadline_seconds=30)

    assert transport.sent == []


def test_a_post_is_signed_over_its_exact_bytes():
    transport = Transport()
    calls, key = _opened(transport)

    calls.post("event", {"event_id": "evt_" + "0" * 20, "type": "heartbeat"},
               deadline_seconds=30)

    (sent,) = transport.sent
    assert sent["path"] == "/v1/attempts/att_1/state/event"
    assert json.loads(sent["body"])["generation"] == 3
    _verify(key, sent, path=sent["path"], body=sent["body"])


def test_a_get_signs_its_path_with_the_query():
    transport = Transport()
    calls, key = _opened(transport)

    calls.get("upstream", {"upstream_task_id": "task_p", "ignored": None}, deadline_seconds=30)

    (sent,) = transport.sent
    path = sent["path"]
    assert path.startswith("/v1/attempts/att_1/state/upstream?")
    assert "tenant_id=eng" in path and "generation=3" in path
    assert "upstream_task_id=task_p" in path and "ignored" not in path
    assert sent["body"] is None
    _verify(key, sent, path=path, body=b"")


def test_an_outage_is_retried_with_the_same_bytes_and_the_child_backoff():
    clock = Clock()
    down = ChildApiError(503, "state_unavailable", "", retryable=True)
    transport = Transport(down, down, down, {"ok": True})
    calls, _key = _opened(Transport(), clock)
    calls._api = transport

    calls.post("finish", {"request_id": "r1", "state": "SUCCEEDED"}, deadline_seconds=60)

    assert clock.slept == [1.0, 2.0, 4.0]
    assert len({s["body"] for s in transport.sent}) == 1 and len(transport.sent) == 4


def test_an_outage_past_the_budget_raises_service_unavailable():
    clock = Clock()
    transport = Transport(ChildApiError(None, "api_unavailable", "URLError", retryable=True))
    calls, _key = _opened(Transport(), clock)
    calls._api = transport

    with pytest.raises(core_exceptions.ServiceUnavailable):
        calls.post("heartbeat", {}, deadline_seconds=10)

    assert sum(clock.slept) <= 10 and clock.slept[:3] == [1.0, 2.0, 4.0]


def _bare_404() -> ChildApiError:
    """What `ChildApi.call` raises for a 404 whose body is not swarm-api's
    refusal shape: FastAPI's router `{"detail": "Not Found"}`, or a page from
    in front of the service, has no `code`, so the transport names it
    `http_404` with no message (#3041)."""
    return ChildApiError(404, "http_404", "", retryable=False)


def test_a_bare_404_is_retried_like_an_outage_and_the_attempt_goes_on():
    # #3041: a running task failed on a 404 that carried no code while a
    # release rolled out. A 404 swarm-api did not name is the edge or the
    # router, not a refusal: retried with the same bytes within the budget.
    clock = Clock()
    transport = Transport(_bare_404(), _bare_404(), {"ok": True, "task": {}})
    calls, _key = _opened(Transport(), clock)
    calls._api = transport

    answer = calls.get("poll", {"provider": "anthropic"}, deadline_seconds=60)

    assert answer == {"ok": True, "task": {}}
    assert len(transport.sent) == 3 and clock.slept == [1.0, 2.0]
    assert len({s["path"] for s in transport.sent}) == 1


def test_a_bare_404_past_the_budget_is_an_outage_not_a_refusal():
    clock = Clock()
    transport = Transport(_bare_404())
    calls, _key = _opened(Transport(), clock)
    calls._api = transport

    with pytest.raises(core_exceptions.ServiceUnavailable) as caught:
        calls.post("heartbeat", {}, deadline_seconds=10)

    assert sum(clock.slept) <= 10 and clock.slept[:3] == [1.0, 2.0, 4.0]
    assert "http_404" in str(caught.value)


@pytest.mark.parametrize("code", ["not_found", "attempt_not_found", "task_not_found"])
def test_a_404_swarm_api_names_stays_final(code):
    transport = Transport(ChildApiError(404, code, "no such attempt", retryable=False))
    calls, _key = _opened(Transport())
    calls._api = transport

    with pytest.raises(StateRefused) as caught:
        calls.post("heartbeat", {}, deadline_seconds=60)

    assert len(transport.sent) == 1
    assert caught.value.status == 404 and caught.value.code == code


def test_a_409_fence_is_still_not_retried_after_a_bare_404():
    clock = Clock()
    fenced = ChildApiError(409, "attempt_fenced", "stale generation", retryable=False)
    transport = Transport(_bare_404(), fenced)
    calls, _key = _opened(Transport(), clock)
    calls._api = transport

    with pytest.raises(FencedWriteRefused):
        calls.post("checkpoint", {"checkpoint_id": "c1"}, deadline_seconds=60)

    assert len(transport.sent) == 2 and clock.slept == [1.0]


@pytest.mark.parametrize("code", ["attempt_fenced", "child_key_taken", "child_key_window_closed"])
def test_a_409_fence_is_a_refused_write_and_is_not_retried(code):
    transport = Transport(ChildApiError(409, code, "stale generation", retryable=False))
    calls, _key = _opened(Transport())
    calls._api = transport

    with pytest.raises(FencedWriteRefused) as caught:
        calls.post("park", {"request_id": "r1"}, deadline_seconds=60)

    assert len(transport.sent) == 1
    assert caught.value.write == "park" and caught.value.expected == 3


@pytest.mark.parametrize(
    ("status", "code", "raised"),
    [(401, "attempt_unauthenticated", core_exceptions.Unauthenticated),
     (403, "attempt_unproven", core_exceptions.PermissionDenied),
     (422, "transition_refused", StateRefused),
     (409, "state_conflict", StateRefused),
     (403, "upstream_not_declared", StateRefused)],
)
def test_each_refusal_is_the_error_the_lifecycle_acts_on(status, code, raised):
    transport = Transport(ChildApiError(status, code, "no", retryable=False))
    calls, _key = _opened(Transport())
    calls._api = transport

    with pytest.raises(raised):
        calls.post("transition", {"request_id": "r1", "to": "RUNNING"}, deadline_seconds=60)

    assert len(transport.sent) == 1


def test_no_error_carries_the_nonce_or_the_body():
    transport = Transport(ChildApiError(422, "validation_failed", "bad", retryable=False))
    calls = _calls(transport)

    with pytest.raises(StateRefused) as caught:
        calls.open(AttemptKey(), nonce=NONCE, unprotected=False, restore_attempt_id=None,
                   deadline_seconds=5)

    assert NONCE not in str(caught.value) and not calls.opened


def test_request_ids_are_what_the_routes_accept_and_never_repeat():
    import re

    ids = {statecall.new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(r"[a-z0-9-]{1,64}", i) for i in ids)


#: The state ops the worker sends outside `RemoteControlPlane`: the merge
#: slot and train, the CI-fix count, and the index run's two reads.
_MODULE_OPS = {
    "mergeslot.py": {"merge-slot"},
    "mergetrain.py": {"merge-train"},
    "inputs.py": {"ci-fix-tasks", "upstream"},
    "indexrun.py": {"context", "repository"},
    "statecall.py": {"jev-usage", "jev-settle"},
}


def _worker_ops() -> set[str]:
    import re
    from pathlib import Path

    import agent_worker

    root = Path(agent_worker.__file__).parent
    control = (root / "control.py").read_text()
    ops = set(re.findall(r'self\._(?:post|get)\(\s*"([a-z][a-z-]*)"', control))
    for name, expected in _MODULE_OPS.items():
        text = (root / name).read_text()
        for op in expected:
            assert re.search(rf'\.(?:post|get)\(\s*"{op}"', text), (name, op)
        ops |= expected
    return ops | {"open"}


def test_swarm_api_serves_every_state_route_the_worker_calls():
    # #3041: a worker must never call a state route the API it talks to lacks.
    # In one commit, every op the worker sends is a route swarm-api serves, by
    # method, so the API carries a route no later than the worker that uses it.
    from swarm_api.routes import attemptstate

    prefix = attemptstate.PREFIX + "/"
    served = {
        (method, route.path[len(prefix):])
        for route in attemptstate.router.routes
        if route.path.startswith(prefix)
        for method in route.methods
    }
    gets = {"poll", "upstream", "context", "repository", "jev-usage"}
    ops = _worker_ops()

    assert {"heartbeat", "poll", "transition", "finish", "park"} <= ops
    missing = sorted(op for op in ops
                     if ("GET" if op in gets else "POST", op) not in served)
    assert missing == []


# ---------------------------------------------------------------------------
# The Jev ledger (docs/design/agent-quality/trusted-state.md §3)
# ---------------------------------------------------------------------------


def test_jev_usage_reads_the_route_and_returns_its_three_counts():
    transport = Transport({"ok": True, "month": "2026-11", "month_used_tokens": 900,
                           "task_calls": 3, "task_input_tokens": 400})
    calls, key = _opened(transport)

    assert calls.jev_usage(deadline_seconds=5) == (900, 3, 400)
    (sent,) = transport.sent
    assert sent["method"] == "GET"
    assert sent["path"].startswith("/v1/attempts/att_1/state/jev-usage?tenant_id=eng&")
    _verify(key, sent, path=sent["path"], body=b"")


@pytest.mark.parametrize("answer", [
    {"ok": True},
    {"ok": True, "month_used_tokens": -1, "task_calls": 0, "task_input_tokens": 0},
    {"ok": True, "month_used_tokens": True, "task_calls": 0, "task_input_tokens": 0},
    {"ok": True, "month_used_tokens": "9", "task_calls": 0, "task_input_tokens": 0},
])
def test_a_jev_usage_answer_missing_a_count_is_an_error_never_a_zero(answer):
    """An unread count is unknown: the caller turns the judgments off, it
    never runs them against a month it takes to be empty."""
    calls, _key = _opened(Transport(answer))

    with pytest.raises(ControlPlaneError):
        calls.jev_usage(deadline_seconds=5)


def test_jev_settle_sends_the_counts_and_one_request_id_across_retries():
    outage = ChildApiError(503, "http_503", "", retryable=True)
    transport = Transport({"ok": True}, outage, {"ok": True, "repeated": False,
                                   "settled": {"calls": 3, "input_tokens": 1200}})
    calls, key = _opened(transport)
    split = {"J1": {"calls": 3, "input_tokens": 1200, "output_tokens": None,
                    "outcomes": {"ok": 3}}}

    settled = calls.jev_settle(calls=3, input_tokens=1200, by_kind=split, deadline_seconds=30)

    assert settled == (3, 1200)
    first, second = transport.sent
    assert first["body"] == second["body"]
    body = json.loads(first["body"])
    assert body == {"tenant_id": "eng", "task_id": "task_1", "lease_id": "lease_1",
                    "generation": 3, "request_id": body["request_id"], "calls": 3,
                    "input_tokens": 1200, "by_kind": split}
    assert len(body["request_id"]) == 32
    _verify(key, second, path="/v1/attempts/att_1/state/jev-settle", body=second["body"])


def test_a_repeated_jev_settle_reports_what_the_first_one_settled():
    transport = Transport({"ok": True, "repeated": True,
                           "settled": {"calls": 0, "input_tokens": 0}})
    calls, _key = _opened(transport)

    assert calls.jev_settle(calls=2, input_tokens=10, by_kind={},
                            deadline_seconds=5) == (2, 10)


def test_a_fenced_jev_settle_raises_fenced():
    fenced = ChildApiError(409, "attempt_fenced", "superseded", retryable=False)
    calls, _key = _opened(Transport({"ok": True}, fenced))

    with pytest.raises(FencedWriteRefused):
        calls.jev_settle(calls=1, input_tokens=1, by_kind={}, deadline_seconds=5)

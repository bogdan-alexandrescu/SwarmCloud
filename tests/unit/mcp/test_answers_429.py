"""A step row says which questions are answered, and a 429 says whose it was (ANSWERS-429; part of #1904, #1816).

1. `compact.step_result` served a step's `questions` without the owner's
   answers to them, although the follow payload (`_slim_outcome`) already
   carried `owner_answers` (TASK-ANSWERS). A per-step row could not tell an
   answered question from an open one, so the session re-asked answered ones
   (#1904, left by PR 1908). It now carries `owner_answers` -- each answer's
   index, choice and text as recorded on the task, never more -- and no key
   when nothing is answered.

2. A 429 from the API's own token bucket (`swarm_api.errors.RateLimited`,
   body `{"code": "rate_limited", "message": ..., "detail":
   {"retry_after_seconds": ...}}`) and a 429 from Cloud Run's front end
   ("no available instance") read the same: "you are polling too much"
   (#1816). The bridge now raises `ApiRateLimited` for the first, with
   today's wording, and `NoInstanceFree` for anything else, saying "SwarmCloud's
   API had no instance free (Cloud Run)" and whether it retried.
"""

from __future__ import annotations

import email.message
import io
import json
import threading

import pytest
import urllib.error

from swarm_mcp import client as mcp_client
from swarm_mcp import compact
from swarm_mcp.client import SwarmClient, SwarmError


def _question(n: int) -> dict:
    return {
        "question": f"Wake the park on a hold release? ({n})",
        "options": [{"label": "wake", "description": "wake it"}, {"label": "wait", "description": "leave it"}],
        "recommended": "wake",
        "context": f"QUOTA-PARK-RECHECK ({n})",
    }


def _row(answers: list) -> dict:
    return {
        "task_id": "task_1",
        "state": "SUCCEEDED",
        "outcome": {
            "state": "SUCCEEDED", "answer_excerpt": "done", "pr_url": "", "artifacts": ["questions.json"],
            "last_error": "", "console": "https://c.test/agents/task_1",
            "questions": [_question(0), _question(1)],
            "owner_answers": answers,
        },
    }


# --------------------------------------------------------------------------
# 1. step_result carries the owner's answers
# --------------------------------------------------------------------------

def test_a_step_result_carries_the_answer_to_question_0_of_2_and_nothing_more():
    recorded = {"question_index": 0, "question": "Wake the park on a hold release? (0)", "choice": "wake",
                "text": "", "by": "owner@example.test", "at": "2026-10-22T01:10:00Z", "history": []}
    result = compact.step_result(_row([recorded]))
    # MUTATION: leaving `owner_answers` out of `step_result` (the #1904
    # defect) loses the answer here, and the row re-asks question 0.
    # MUTATION: passing the recorded answer through whole carries `by`, `at`
    # and `history` too: only the index, the choice and the text are served.
    assert result["owner_answers"] == [{"question_index": 0, "choice": "wake", "text": ""}], result
    # MUTATION: dropping a question once it is answered: both stay.
    assert len(result["questions"]) == 2
    # The no-null rule still holds over the new key.
    assert None not in result["owner_answers"][0].values()


def test_a_step_result_with_no_answers_has_no_owner_answers_key():
    # MUTATION: always setting `owner_answers: []` puts the key on a task the
    # owner never answered; it stays absent, as `answered` does in run.js.
    assert "owner_answers" not in compact.step_result(_row([]))
    assert "owner_answers" not in compact.step_result({"state": "RUNNING"})


def test_a_malformed_answer_is_not_served():
    # MUTATION: keeping an entry with no integer index serves an answer no
    # question can be matched to.
    got = compact.step_result(_row([{"question_index": "0", "choice": "wake"}, "wake", {"choice": "x"},
                                    {"question_index": True, "choice": "x"}]))
    assert "owner_answers" not in got, got


# --------------------------------------------------------------------------
# 2. whose 429 it was
# --------------------------------------------------------------------------

class _Response:
    status = 200

    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _api_rate_limited(url: str) -> urllib.error.HTTPError:
    """The API's own refusal, as `swarm_api.main.api_error_handler` serves `RateLimited`."""
    headers = email.message.Message()
    headers["Retry-After"] = "1"
    body = json.dumps({"code": "rate_limited", "message": "rate limit exceeded for this principal",
                       "detail": {"retry_after_seconds": 0.05}}).encode()
    return urllib.error.HTTPError(url, 429, "Too Many Requests", headers, io.BytesIO(body))


def _front_end(body: bytes):
    def _raise(url: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(url, 429, "Too Many Requests", email.message.Message(), io.BytesIO(body))
    return _raise


@pytest.fixture()
def answers(monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Answers", (), {})()
    state.queue = []
    lock = threading.Lock()

    def _opener(req, timeout=None):  # noqa: ARG001
        with lock:
            answer = state.queue.pop(0) if state.queue else {"attempts": []}
        if callable(answer):
            raise answer(req.full_url)
        return _Response(answer)

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return state


def test_the_apis_own_429_raises_the_rate_limit_error_in_todays_words(answers):
    answers.queue = [_api_rate_limited]
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="http://api.invalid").attempts("t1")
    # MUTATION: classifying every 429 as Cloud Run's (or none) fails here.
    assert isinstance(caught.value, mcp_client.ApiRateLimited), type(caught.value)
    assert not isinstance(caught.value, mcp_client.NoInstanceFree)
    assert caught.value.status == 429 and caught.value.code == "rate_limited"
    # MUTATION: rewording the API's own refusal: it keeps its message.
    assert "rate limit exceeded for this principal" in str(caught.value)
    assert mcp_client.NO_INSTANCE_FREE not in str(caught.value)


@pytest.mark.parametrize("body", [
    b"Rate exceeded.",
    b"<html><body>429 Too Many Requests</body></html>",
    json.dumps({"detail": "rate limit exceeded"}).encode(),
    json.dumps({"code": "quota_exceeded", "message": "no"}).encode(),
    b"",
], ids=["text", "html", "fastapi-detail", "other-code", "empty"])
def test_any_other_429_raises_the_instance_unavailable_error(answers, body):
    answers.queue = [_front_end(body)]
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="http://api.invalid").attempts("t1")
    # MUTATION: reading a 429 as the API's whenever its body parses as JSON
    # (or by status alone) makes these the rate-limit error.
    assert isinstance(caught.value, mcp_client.NoInstanceFree), (body, type(caught.value))
    assert not isinstance(caught.value, mcp_client.ApiRateLimited)
    assert caught.value.status == 429
    assert "SwarmCloud's API had no instance free (Cloud Run)" in str(caught.value)
    # A one-shot client does not retry, and says so rather than claim it did.
    assert "not retried" in str(caught.value)


def test_the_bridge_says_it_retried_cloud_runs_429(answers):
    sleeps: list[float] = []
    client = SwarmClient(base_url="http://api.invalid")
    client.pace_for_bridge(environ={}, sleep=sleeps.append)
    answers.queue = [_front_end(b"Rate exceeded.")] * (len(mcp_client.RETRY_429_DELAYS) + 1)
    with pytest.raises(SwarmError) as caught:
        client.attempts("t1")
    tried = len(mcp_client.RETRY_429_DELAYS)
    # The retry is BRIDGE-PACING's, unchanged: every delay was taken.
    assert len(sleeps) == tried
    assert isinstance(caught.value, mcp_client.NoInstanceFree)
    # MUTATION: raising the last try's error as it was says "not retried"
    # after three retries.
    assert f"SwarmCloud's API had no instance free (Cloud Run); retried {tried} time(s)" in str(caught.value)
    assert caught.value.status == 429


def test_the_bridges_retry_of_the_apis_own_429_keeps_its_words(answers):
    sleeps: list[float] = []
    client = SwarmClient(base_url="http://api.invalid")
    client.pace_for_bridge(environ={}, sleep=sleeps.append)
    answers.queue = [_api_rate_limited] * (len(mcp_client.RETRY_429_DELAYS) + 1)
    with pytest.raises(SwarmError) as caught:
        client.attempts("t1")
    # MUTATION: rewording every retried 429 as Cloud Run's fails here.
    assert isinstance(caught.value, mcp_client.ApiRateLimited)
    assert mcp_client.NO_INSTANCE_FREE not in str(caught.value)
    assert sleeps == [1.0] * len(mcp_client.RETRY_429_DELAYS)

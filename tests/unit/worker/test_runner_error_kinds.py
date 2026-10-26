"""The runner tells a provider ERROR apart from a rate limit (RUNNER-ERROR-KINDS, #2302).

BGD decided on 2026-10-25 that the claude-code runner classifies what its CLI
ended in, so the held pool account's health record says which: an overloaded
529, a server 5xx, or a network failure -- each its own `kind` on the broker's
`/account-error` route, which counts on the account and never touches AIMD.
Before this, `overloaded_error` was one of the rate-limit markers: a 529 parked
the attempt as PROVIDER_QUOTA_EXHAUSTED, moved it off a healthy account as
"exhausted" and reported a rate limit for the tenant. A 5xx or a connection
reset reached nobody's record at all.

These pin:

  * the classifier: overloaded, server_error and network each from the shapes
    the CLI prints, a 429 still a rate limit and never an error kind;
  * the runner on a held account writes the kind and a short REDACTED message
    to the account channel, and an overloaded run is not a rate limit;
  * the worker reports that kind for the held account, end to end through the
    real runner, and nothing at all when the attempt holds no pool account.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from agent_worker.accountlease import Assignment, NoAccount
from agent_worker.runners.base import QuotaExhaustedSignal, RunnerContext, RunnerFailure
from agent_worker.runners.cliagent import (
    ACCOUNT_STREAM_ENV,
    CliAgentSpec,
    classify_account_error,
    detect_rate_limit,
    run_cli_agent,
)
from swarm_common.states import EventType, TaskState

from worker_seeds import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient

FIRST = f"{TENANT}:first"


def _token() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "oat01-" + pysecrets.token_hex(16)


def _api_error(status: int, error_type: str, message: str) -> str:
    """What the CLI prints for an API error it gave up retrying."""
    body = json.dumps({"type": "error", "error": {"type": error_type, "message": message}})
    return f"API Error: {status} {body}"


OVERLOADED = _api_error(529, "overloaded_error", "Overloaded")
SERVER = _api_error(500, "api_error", "Internal server error")
RESET = "API Error: Connection error. (cause: read ECONNRESET)"
RATE_LIMITED = _api_error(429, "rate_limit_error", "Number of requests has exceeded your rate limit")


# -- the classifier ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,kind",
    [
        (OVERLOADED, "overloaded"),
        ('{"type":"result","is_error":true,"api_error_status":529}', "overloaded"),
        (SERVER, "server_error"),
        (_api_error(502, "api_error", "Bad gateway"), "server_error"),
        ("API Error: 503 Service Unavailable", "server_error"),
        (RESET, "network"),
        ("connection reset by peer", "network"),
        ("Error: getaddrinfo ENOTFOUND api.anthropic.com", "network"),
        ("API Error: Request timed out.", "network"),
    ],
)
def test_an_error_the_provider_gave_up_on_is_classified(text, kind):
    found = classify_account_error(text)
    assert found is not None and found[0] == kind
    hit, _, _ = detect_rate_limit(text)
    assert hit is False, "an error kind is not a rate limit"


def test_an_overloaded_529_with_a_retry_after_is_still_not_a_rate_limit():
    hit, _, _ = detect_rate_limit(OVERLOADED + "\nretry-after: 30")
    assert hit is False


@pytest.mark.parametrize(
    "text", [RATE_LIMITED, "HTTP 429 Too Many Requests", '{"type":"rate_limit_error"}']
)
def test_a_429_is_still_a_rate_limit_and_no_error_kind(text):
    assert detect_rate_limit(text)[0] is True
    assert classify_account_error(text) is None


def _token_with_429() -> str:
    """A runtime-built token whose hex carries `429` between hex digits.

    `_token()`'s random hex contains `429` about one run in 137, which is how
    `test_the_message_in_the_channel_is_redacted` went red on PRs 2316 and 2370
    (FIX-RATE-LIMIT-MARKER, part of #2379). This one carries it every time.
    """
    return "sk-ant-" + "oat01-" + pysecrets.token_hex(8) + "a429f" + pysecrets.token_hex(8)


@pytest.mark.parametrize(
    "identifier",
    [
        lambda: _token_with_429(),
        lambda: "sk-ant-" + "oat01-" + "429" + pysecrets.token_hex(8),
        lambda: "req_" + "429" + pysecrets.token_hex(6),
        lambda: str(uuid.uuid4())[:9] + "4290" + str(uuid.uuid4())[13:],
    ],
    ids=["hex", "after-a-dash", "after-an-underscore", "uuid-group"],
)
def test_a_429_inside_an_identifier_is_not_a_rate_limit(identifier):
    text = _api_error(500, "api_error", f"bad request with {identifier()}")
    assert detect_rate_limit(text)[0] is False
    found = classify_account_error(text)
    assert found is not None and found[0] == "server_error"


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 429",
        '{"status": 429}',
        "request failed (429)",
        "status=429, retry later",
    ],
)
def test_a_429_standing_as_a_status_is_still_a_rate_limit(text):
    assert detect_rate_limit(text)[0] is True


@pytest.mark.parametrize("text", ["", "the build failed", "ModuleNotFoundError: foo"])
def test_an_ordinary_failure_is_no_error_kind(text):
    assert classify_account_error(text) is None


def test_the_message_is_one_short_line():
    found = classify_account_error("noise\n" + SERVER + " " + "x" * 2000 + "\nmore")
    assert found is not None
    message = found[1]
    assert "\n" not in message and len(message) <= 200
    assert "500" in message


# -- the runner, on a held account ------------------------------------------


def _ctx(tmp_path: Path) -> RunnerContext:
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    return RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "i",
                         result_path=work / "r", quota_path=work / "q",
                         payload={"prompt": "p"})


def _failing_cli(tmp_path: Path, text: str) -> Path:
    """A CLI whose stream ends in the API error `text` and exits 1."""
    script = tmp_path / "fake-cli"
    sid = str(uuid.uuid4())
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        f"print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': {sid!r}}}))\n"
        f"print(json.dumps({{'type': 'assistant', 'isApiErrorMessage': True, 'message': "
        f"{{'content': [{{'type': 'text', 'text': {text!r}}}]}}}}))\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': True, "
        f"'result': {text!r}}}))\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    return script


def _spec() -> CliAgentSpec:
    return CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None, resume_flag="--resume")


def _run_on_account(tmp_path, monkeypatch, text: str) -> tuple[BaseException, dict]:
    channel = tmp_path / "account-stream.json"
    monkeypatch.setenv("FAKE_BIN", str(_failing_cli(tmp_path, text)))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    monkeypatch.setenv(ACCOUNT_STREAM_ENV, str(channel))
    with pytest.raises(RunnerFailure) as caught:
        run_cli_agent(_ctx(tmp_path), _spec())
    return caught.value, json.loads(channel.read_text())


def test_an_overloaded_run_is_a_failure_not_a_rate_limit_and_names_its_kind(
    tmp_path, monkeypatch
):
    failure, channel = _run_on_account(tmp_path, monkeypatch, OVERLOADED)

    assert not isinstance(failure, QuotaExhaustedSignal), "a 529 must not park as quota"
    assert channel["error"]["kind"] == "overloaded"
    assert "529" in channel["error"]["message"]


@pytest.mark.parametrize("text,kind", [(SERVER, "server_error"), (RESET, "network")])
def test_a_5xx_and_a_connection_reset_each_name_their_kind(tmp_path, monkeypatch, text, kind):
    failure, channel = _run_on_account(tmp_path, monkeypatch, text)

    assert not isinstance(failure, QuotaExhaustedSignal)
    assert channel["error"]["kind"] == kind


def test_a_429_on_a_held_account_parks_as_before_and_names_no_error(tmp_path, monkeypatch):
    channel = tmp_path / "account-stream.json"
    monkeypatch.setenv("FAKE_BIN", str(_failing_cli(tmp_path, RATE_LIMITED)))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    monkeypatch.setenv(ACCOUNT_STREAM_ENV, str(channel))
    with pytest.raises(QuotaExhaustedSignal):
        run_cli_agent(_ctx(tmp_path), _spec())
    assert json.loads(channel.read_text()).get("error") is None


def test_the_message_in_the_channel_is_redacted(tmp_path, monkeypatch):
    leaked = _token()
    failure, channel = _run_on_account(
        tmp_path, monkeypatch, _api_error(500, "api_error", f"bad request with {leaked}")
    )

    assert channel["error"]["kind"] == "server_error"
    assert leaked not in json.dumps(channel)
    assert "oat01-" not in channel["error"]["message"]


def test_a_500_carrying_a_token_with_429_in_its_hex_fails_and_does_not_park(
    tmp_path, monkeypatch
):
    leaked = _token_with_429()
    failure, channel = _run_on_account(
        tmp_path, monkeypatch, _api_error(500, "api_error", f"bad request with {leaked}")
    )

    assert not isinstance(failure, QuotaExhaustedSignal), "a 429 in a token is not a status"
    assert channel["error"]["kind"] == "server_error"
    assert leaked not in json.dumps(channel)


# -- the worker reports it for the held account -----------------------------


class _Reporter:
    """The quota broker's routes, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def report(self, *, provider, tenant_id, route, body):
        self.calls.append((route, json.loads(json.dumps(body))))


class _Broker:
    """One pool account, no other to move to."""

    def __init__(self) -> None:
        self.first = Assignment(
            account_id=FIRST,
            secret=f"swarm-account-{TENANT}--first",
            assignment_id="asg-1",
            account={"account_id": FIRST, "owner_tenant": TENANT, "label": "first"},
        )
        self.swaps: list[str] = []
        self.releases: list[tuple[str, str]] = []

    def assign(self, provider, *, exclude=()):
        return self.first

    def release(self, account_id, assignment_id, *, unusable=""):
        self.releases.append((account_id, unusable))
        return 0

    def swap(self, assignment, *, provider, reason, exclude=()):
        self.swaps.append(reason)
        return NoAccount(reason="no_account_available")

    def hold_status(self, assignment):
        return {"held": True, "move": None}

    def report_reading(self, assignment, windows):
        return True


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """Points the claude-code runner at a fake CLI ending in `text`."""

    def _point(text: str) -> None:
        sub = tmp_path / "cli"
        sub.mkdir(exist_ok=True)
        monkeypatch.setenv("CLAUDE_CODE_BIN", str(_failing_cli(sub, text)))
        monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)

    return _point


def _run_worker(db, worker_factory, *, held: bool):
    reporter = _Reporter()
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({
        f"swarm-account-{TENANT}--first": _token(),
        f"swarm-tenant-{TENANT}-anthropic": _token(),
    })
    worker, _config, _ = worker_factory(
        runner_profile="claude-code", secret_client=secrets, quota_reporter=reporter,
    )
    broker = _Broker()
    if held:
        worker._account_broker = broker
    worker.run()
    return reporter, broker


def _errors(reporter: _Reporter) -> list[dict[str, Any]]:
    return [body for route, body in reporter.calls if route == "account-error"]


def test_an_overloaded_529_is_reported_for_the_held_account_and_parks_nothing(
    db, worker_factory, claude, log_stream
):
    claude(OVERLOADED)
    reporter, broker = _run_worker(db, worker_factory, held=True)

    errors = _errors(reporter)
    assert [e["kind"] for e in errors] == ["overloaded"], log_stream.getvalue()[-3000:]
    assert errors[0]["account"]["account_id"] == FIRST
    assert errors[0]["account"]["assignment_id"] == "asg-1"
    # Not a rate limit: nothing halved, nothing parked, no move as exhausted.
    assert not [r for r, _ in reporter.calls if r == "rate-limit"]
    assert broker.swaps == []
    assert db.doc("tasks/task_1")["state"] != TaskState.PARKED.value
    assert EventType.QUOTA_EXHAUSTED.value not in db.event_types("task_1")


@pytest.mark.parametrize("text,kind", [(SERVER, "server_error"), (RESET, "network")])
def test_a_5xx_and_a_connection_reset_are_reported_with_their_kind(
    db, worker_factory, claude, text, kind
):
    claude(text)
    reporter, _broker = _run_worker(db, worker_factory, held=True)

    assert [e["kind"] for e in _errors(reporter)] == [kind]


def test_a_429_reports_a_rate_limit_as_before_and_no_account_error(
    db, worker_factory, claude
):
    claude(RATE_LIMITED)
    reporter, broker = _run_worker(db, worker_factory, held=True)

    assert _errors(reporter) == []
    assert [r for r, _ in reporter.calls if r == "rate-limit"]
    assert broker.swaps == ["exhausted"]


def test_nothing_is_reported_when_the_attempt_holds_no_pool_account(
    db, worker_factory, claude
):
    claude(OVERLOADED)
    reporter, _broker = _run_worker(db, worker_factory, held=False)

    assert _errors(reporter) == []


def test_the_reported_message_is_redacted(db, worker_factory, claude):
    leaked = _token()
    claude(_api_error(500, "api_error", f"upstream echoed {leaked}"))
    reporter, _broker = _run_worker(db, worker_factory, held=True)

    errors = _errors(reporter)
    assert [e["kind"] for e in errors] == ["server_error"]
    assert leaked not in json.dumps(reporter.calls)
    assert "oat01-" not in errors[0].get("message", "")

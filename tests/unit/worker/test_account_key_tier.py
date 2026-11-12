"""A worker runs on a plan or an API key as the broker's chain answers (LP-W1).

docs/design/llm-providers.md §3 (the four cells), §5 (the fallback order) and
§8.2 (contract request 152). These pin the worker's half:

  * each key shape's environment for each runner, and a plan's unchanged;
  * `credit_exhausted` is reported as the key's, to the broker's `exhausted`
    route, and a rate limit is not reported as it;
  * the tier the broker answered is on the assignment and swap events;
  * a plan-to-key swap restarts with the key's environment and a key-to-plan
    move back (Q1) does the same, at a turn boundary;
  * the attempt records `account_id`, `credential_source` and
    `credential_tier` (`plan` or `api_key` only) after a run;
  * the running spend rides on a key holder's hold-status read;
  * no key value reaches a log line or an event.

Every fake key is built at runtime (`secrets.token_hex`): no credential-shaped
literal in this file.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import urllib.error
from typing import Any

import pytest

from agent_worker.accountlease import (
    ACCOUNT_TOKEN_ENV,
    ANTHROPIC_KEY_ENV,
    OPENAI_KEY_ENV,
    PLAN_ACCOUNT_ENV,
    PLAN_TOKEN_ENV,
    TIER_METERED,
    TIER_PLAN,
    AccountBroker,
    Assignment,
    credential_env_from_account,
    takes_key,
)
from agent_worker.control import credential_fields
from agent_worker.errors import ExitCode
from agent_worker.runners.cliagent import (
    detect_credit_exhausted,
    detect_rate_limit,
)
from swarm_common.profiles import RUNNER_PROFILES

from test_account_hot_swap import FAKE_CLI, FakeBroker, _Secrets
from worker_seeds import TENANT, seed_attempt, seed_tenant

PLAN = f"{TENANT}:plan"
KEY = f"{TENANT}:key"
PLAN_SECRET = f"swarm-account-{TENANT}--plan"
KEY_SECRET = f"swarm-account-{TENANT}--key-main"


def _anthropic_key() -> str:
    return "sk-" + "ant-" + "api03-" + pysecrets.token_hex(24)


def _openai_key() -> str:
    return "sk-" + "proj-" + pysecrets.token_hex(24)


def _plan_token() -> str:
    return "sk-" + "ant-" + "oat01-" + pysecrets.token_hex(16)


def _key_secret(kind: str, key: str) -> str:
    return json.dumps({"kind": kind, "api_key": key})


def _plan(n: int = 1) -> Assignment:
    return Assignment(
        account_id=PLAN, secret=PLAN_SECRET, assignment_id=f"asg-{n}",
        account={"account_id": PLAN, "owner_tenant": TENANT, "label": "plan",
                 "provider": "anthropic"},
        tier=TIER_PLAN,
    )


def _key(n: int = 1) -> Assignment:
    return Assignment(
        account_id=KEY, secret=KEY_SECRET, assignment_id=f"asg-k{n}",
        account={"account_id": KEY, "owner_tenant": TENANT, "label": "key-main",
                 "provider": "anthropic-api"},
        tier=TIER_METERED,
    )


# -- the key shapes ---------------------------------------------------------


def test_an_anthropic_key_fills_anthropic_api_key_for_claude_code():
    key = _anthropic_key()
    env = credential_env_from_account(
        _key_secret("anthropic_api_key", key),
        secret_env_names=tuple(RUNNER_PROFILES["claude-code"].secrets),
        secret_name=KEY_SECRET,
        provider="anthropic-api",
    )
    assert env == {ANTHROPIC_KEY_ENV: key}


def test_an_openai_key_fills_openai_api_key_for_codex():
    key = _openai_key()
    env = credential_env_from_account(
        _key_secret("openai_api_key", key),
        secret_env_names=tuple(RUNNER_PROFILES["codex"].secrets),
        secret_name=KEY_SECRET,
        provider="openai-api",
    )
    assert env == {OPENAI_KEY_ENV: key}


def test_a_plan_stays_as_today_for_each_runner():
    token = _plan_token()
    assert credential_env_from_account(
        token, secret_env_names=tuple(RUNNER_PROFILES["claude-code"].secrets),
        secret_name=PLAN_SECRET, provider="anthropic",
    ) == {ACCOUNT_TOKEN_ENV: token}
    access, account = pysecrets.token_hex(20), pysecrets.token_hex(8)
    assert credential_env_from_account(
        json.dumps({"access_token": access, "account_id": account}),
        secret_env_names=tuple(RUNNER_PROFILES["codex"].secrets),
        secret_name=PLAN_SECRET, provider="openai",
    ) == {PLAN_TOKEN_ENV: access, PLAN_ACCOUNT_ENV: account}


@pytest.mark.parametrize("payload_of", [
    lambda key: _key_secret("openai_api_key", key),   # the other vendor's kind
    lambda key: key,                                  # a bare string
    lambda key: json.dumps({"kind": "anthropic_api_key"}),
])
def test_a_key_secret_of_another_shape_is_refused_without_its_value(payload_of):
    key = _anthropic_key()
    with pytest.raises(ValueError) as caught:
        credential_env_from_account(
            payload_of(key),
            secret_env_names=tuple(RUNNER_PROFILES["claude-code"].secrets),
            secret_name=KEY_SECRET,
            provider="anthropic-api",
        )
    assert key not in str(caught.value)


def test_a_profile_without_the_key_variable_takes_no_key():
    key = _anthropic_key()
    with pytest.raises(ValueError) as caught:
        credential_env_from_account(
            _key_secret("anthropic_api_key", key),
            secret_env_names=(ACCOUNT_TOKEN_ENV,),
            secret_name=KEY_SECRET,
            provider="anthropic-api",
        )
    assert key not in str(caught.value)
    assert not takes_key("anthropic", (ACCOUNT_TOKEN_ENV,))
    assert takes_key("anthropic", tuple(RUNNER_PROFILES["claude-code"].secrets))
    assert takes_key("openai", tuple(RUNNER_PROFILES["codex"].secrets))


# -- the broker client ------------------------------------------------------


class _Wire(AccountBroker):
    """The real client with its transport replaced by a list of answers."""

    def __init__(self, answers: list[Any]) -> None:
        super().__init__("http://broker.invalid", logger=None, token_fetcher=lambda _a: "t",
                         task_id="task_1", attempt_id="att_1")
        self.answers = list(answers)
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def _post(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        self.sent.append((path, dict(payload or {})))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _granted(tier: str | None, provider: str) -> dict[str, Any]:
    return {"account_id": KEY, "assignment_id": "asg-9", "secret": KEY_SECRET,
            "tier": tier, "account": {"provider": provider}, "swapped": True}


def test_assign_asks_for_the_key_tier_and_reads_the_tier_answered():
    wire = _Wire([_granted("api_key", "anthropic-api")])
    got = wire.assign("anthropic", key_tier=True)
    assert wire.sent[0][1]["key_tier"] is True
    assert isinstance(got, Assignment) and got.tier == TIER_METERED and got.is_key
    assert got.provider == "anthropic-api"


def test_assign_without_the_key_tier_sends_what_an_older_worker_sent():
    wire = _Wire([_granted("plan", "anthropic")])
    got = wire.assign("anthropic")
    assert "key_tier" not in wire.sent[0][1]
    assert got.tier == TIER_PLAN


def test_swap_reads_the_tier_and_carries_the_hold_spend():
    wire = _Wire([_granted("api_key", "anthropic-api")])
    got = wire.swap(_plan(), provider="anthropic", reason="exhausted", key_tier=True,
                    cost_usd=0.25)
    body = wire.sent[0][1]
    assert body["key_tier"] is True and body["cost_usd"] == 0.25
    assert got.tier == TIER_METERED


def test_release_carries_the_spend_and_omits_none():
    wire = _Wire([{"assigned": 0}, {"assigned": 0}])
    wire.release(KEY, "asg-1", cost_usd=1.5)
    wire.release(KEY, "asg-1")
    assert wire.sent[0][1]["cost_usd"] == 1.5
    assert "cost_usd" not in wire.sent[1][1]


def test_a_key_holders_hold_status_carries_the_running_spend():
    wire = _Wire([{"held": True, "move": None}])
    wire.hold_status(_key(), cost_usd=0.75)
    assert wire.sent[0][1]["cost_usd"] == 0.75


def test_a_plan_holders_hold_status_carries_no_spend():
    wire = _Wire([{"held": True, "move": None}])
    wire.hold_status(_plan(), cost_usd=0.75)
    assert "cost_usd" not in wire.sent[0][1]


def test_a_broker_that_refuses_the_spend_field_still_answers_the_status():
    from agent_worker.accountlease import BrokerUnavailable

    refused = BrokerUnavailable("the quota broker answered 422")
    refused.status = 422
    wire = _Wire([refused, {"held": True, "move": "plan_available"},
                  {"held": True, "move": None}])
    assert wire.hold_status(_key(), cost_usd=0.5)["move"] == "plan_available"
    wire.hold_status(_key(), cost_usd=0.6)
    assert "cost_usd" in wire.sent[0][1]
    assert "cost_usd" not in wire.sent[1][1]
    assert "cost_usd" not in wire.sent[2][1], "a refused field is not sent again"


def test_a_422_carries_its_status_from_the_transport(monkeypatch):
    import io
    from agent_worker.accountlease import BrokerUnavailable

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError("http://broker.invalid", 422, "no", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    broker = AccountBroker("http://broker.invalid", logger=None, token_fetcher=lambda _a: "t",
                           task_id="task_1", attempt_id="att_1")
    with pytest.raises(BrokerUnavailable) as caught:
        broker.hold_status(_plan())
    assert caught.value.status == 422


# -- credit gone versus a rate limit ----------------------------------------


@pytest.mark.parametrize("text", [
    'API Error: 429 {"type":"error","error":{"type":"credit_balance_exhausted"}}',
    'HTTP 429 {"error": {"code": "insufficient_quota"}}',
])
def test_the_two_credit_codes_are_credit_exhausted(text):
    assert detect_credit_exhausted(text)


@pytest.mark.parametrize("text", [
    'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
    '{"error": {"code": "rate_limit_exceeded"}} retry-after: 20',
])
def test_a_rate_limit_is_not_credit_exhausted(text):
    assert not detect_credit_exhausted(text)
    assert detect_rate_limit(text)[0]


# -- the attempt's record ---------------------------------------------------


def test_the_record_takes_plan_or_api_key_only():
    assert credential_fields({"account_id": KEY, "credential_source": "account_pool",
                              "credential_tier": "api_key"})["credential_tier"] == "api_key"
    assert credential_fields({"account_id": PLAN, "credential_source": "account_pool",
                              "credential_tier": "voice_dedicated"})["credential_tier"] is None
    assert credential_fields({"account_id": None, "credential_source": "tenant_secret",
                              "credential_tier": "plan"}) == {
        "account_id": None, "credential_source": "tenant_secret", "credential_tier": None}
    assert credential_fields({"credential_source": "somewhere"}) == {}


# -- the worker, end to end -------------------------------------------------


#: The hot-swap fake CLI, recording the key variable beside the plan token,
#: with a `credit` mode that ends the run on a spent key's 429.
KEY_CLI = FAKE_CLI.replace(
    'token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")',
    'token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")\n'
    'api_key = os.environ.get("ANTHROPIC_API_KEY", "")',
).replace(
    '"token": token,',
    '"token": token, "api_key": api_key,',
).replace(
    'if mode == "refused":',
    'if mode == "credit":\n'
    '    say({"type": "result", "subtype": "error_during_execution", "is_error": True,\n'
    '         "result": "API Error: 429 {\\"type\\":\\"error\\",\\"error\\":'
    '{\\"type\\":\\"credit_balance_exhausted\\"}}"})\n'
    '    sys.exit(1)\n'
    'if mode == "refused":',
)
assert '"api_key": api_key' in KEY_CLI and 'mode == "credit"' in KEY_CLI


@pytest.fixture
def key_cli(tmp_path, monkeypatch):
    binary = tmp_path / "fake-claude"
    plan = tmp_path / "plan"
    plan.mkdir()
    binary.write_text(KEY_CLI.replace("PLAN_DIR", repr(str(plan)), 1))
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


class _Reports:
    """The broker's quota routes, recorded."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def report(self, *, provider: str, tenant_id: str, route: str, body: Any) -> None:
        self.calls.append({"provider": provider, "route": route, "body": dict(body)})


class _ChainBroker(FakeBroker):
    """`FakeBroker` starting on `first`, with a key holder's status answer."""

    def __init__(self, first: Assignment, *, swap_to, status_move=None) -> None:
        super().__init__(swap_to=swap_to)
        self.first = first
        self.status_move = status_move
        self.status_spend: list[Any] = []
        self.swap_kw: list[dict[str, Any]] = []
        self.assign_kw: list[dict[str, Any]] = []

    def assign(self, provider, *, exclude=(), **kw):
        self.assign_kw.append(kw)
        return super().assign(provider, exclude=exclude)

    def swap(self, assignment, *, provider, reason, exclude=(), **kw):
        self.swap_kw.append(kw)
        return super().swap(assignment, provider=provider, reason=reason, exclude=exclude)

    def hold_status(self, assignment, **kw):
        self.statuses.append(assignment.account_id)
        self.status_spend.append(kw.get("cost_usd"))
        move = self.status_move if assignment.is_key else None
        return {"held": True, "move": move}


def _run(db, worker_factory, tmp_path, broker, *, modes):
    import uuid

    values = {PLAN_SECRET: _plan_token(),
              KEY_SECRET: _key_secret("anthropic_api_key", _anthropic_key())}
    sessions = [str(uuid.uuid4()) for _ in modes]
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=["anthropic"])
    worker, _config, _ = worker_factory(
        runner_profile="claude-code", secret_client=_Secrets(values), timeout_seconds=60,
    )
    worker._account_broker = broker
    reports = _Reports()
    worker.control._quota_reporter = reports
    plan = tmp_path / "plan"
    (plan / "plan.json").write_text(json.dumps({"modes": modes, "sessions": sessions}))
    code = worker.run()
    runs_file = plan / "runs.jsonl"
    runs = [json.loads(line) for line in runs_file.read_text().splitlines()] if (
        runs_file.exists()) else []
    key = json.loads(values[KEY_SECRET])["api_key"]
    return code, worker, runs, values[PLAN_SECRET], key, reports


def _assigned(db) -> list[dict[str, Any]]:
    return [e["detail"] for e in db.events("task_1")
            if (e.get("detail") or {}).get("cause") == "account_assigned"]


def _no_key_anywhere(db, log_stream, *values: str) -> None:
    text = log_stream.getvalue() + json.dumps(db.events("task_1"), default=str)
    text += json.dumps(db.doc("attempts/att_1"), default=str)
    for value in values:
        assert value not in text


def test_a_plan_to_key_swap_restarts_with_the_keys_environment(
    db, worker_factory, tmp_path, key_cli, log_stream
):
    broker = _ChainBroker(_plan(), swap_to=[_key(2)])
    code, worker, runs, token, key, _reports = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"])

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert broker.assign_kw == [{"exclude": ()}] or broker.assign_kw[0].get("key_tier") is True
    assert broker.assign_kw[0].get("key_tier") is True
    assert broker.swap_kw[0].get("key_tier") is True
    # The first start on the plan's token alone; the restart on the key alone.
    assert runs[0]["token"] == token and runs[0]["api_key"] == ""
    assert runs[1]["api_key"] == key and runs[1]["token"] == ""
    assert runs[1]["resume"] is not None
    # The tier on the assignment and on the swap event.
    events = _assigned(db)
    assert events[0]["tier"] == "plan"
    assert events[1]["tier"] == "api_key" and events[1]["from_tier"] == "plan"
    # The attempt keeps the LAST account it ran on, and its tier.
    attempt = db.doc("attempts/att_1")
    assert attempt["account_id"] == KEY
    assert attempt["credential_source"] == "account_pool"
    assert attempt["credential_tier"] == "api_key"
    _no_key_anywhere(db, log_stream, key, token)


def test_a_key_holder_moves_back_to_a_plan_at_a_turn_boundary(
    db, worker_factory, tmp_path, key_cli, log_stream
):
    broker = _ChainBroker(_key(), swap_to=[_plan(2)], status_move="plan_available")
    code, worker, runs, token, key, _reports = _run(
        db, worker_factory, tmp_path, broker, modes=["turns", "finish"])

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [s["reason"] for s in broker.swaps] == ["plan_available"]
    assert runs[0]["api_key"] == key and runs[0]["token"] == ""
    assert runs[1]["token"] == token and runs[1]["api_key"] == ""
    assert runs[1]["resume"] is not None
    events = _assigned(db)
    assert events[0]["tier"] == "api_key"
    assert events[1]["tier"] == "plan" and events[1]["from_tier"] == "api_key"
    attempt = db.doc("attempts/att_1")
    assert attempt["account_id"] == PLAN and attempt["credential_tier"] == "plan"
    _no_key_anywhere(db, log_stream, key, token)


def test_a_move_back_that_finds_the_plan_full_resumes_on_the_key(
    db, worker_factory, tmp_path, key_cli, log_stream
):
    broker = _ChainBroker(_key(), swap_to=[], status_move="plan_available")
    broker.status_move = "plan_available"
    code, worker, runs, token, key, _reports = _run(
        db, worker_factory, tmp_path, broker, modes=["turns", "finish"])

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [s["reason"] for s in broker.swaps][:1] == ["plan_available"]
    # Never two holds, never none: the key was not given back mid-run.
    assert broker.releases == [(KEY, "")]
    assert all(r["api_key"] == key for r in runs) and len(runs) >= 2
    assert db.doc("attempts/att_1")["credential_tier"] == "api_key"


def test_a_spent_keys_credit_is_reported_exhausted_and_the_attempt_moves_on(
    db, worker_factory, tmp_path, key_cli, log_stream
):
    broker = _ChainBroker(_key(), swap_to=[_plan(2)])
    code, worker, runs, token, key, reports = _run(
        db, worker_factory, tmp_path, broker, modes=["credit", "finish"])

    exhausted = [c for c in reports.calls if c["route"] == "exhausted"]
    assert len(exhausted) == 1, reports.calls
    assert exhausted[0]["provider"] == "anthropic-api"
    assert exhausted[0]["body"]["account"]["account_id"] == KEY
    assert exhausted[0]["body"]["account"]["assignment_id"] == "asg-k1"
    # Not ALSO reported as the key's rate limit.
    assert not [c for c in reports.calls
                if c["route"] == "rate-limit" and c["body"].get("account")]
    assert [s["reason"] for s in broker.swaps] == ["exhausted"]
    _no_key_anywhere(db, log_stream, key, token)


def test_a_rate_limit_on_a_plan_is_not_reported_as_credit(
    db, worker_factory, tmp_path, key_cli, log_stream
):
    broker = _ChainBroker(_plan(), swap_to=[_key(2)])
    code, worker, runs, token, key, reports = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"])

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert not [c for c in reports.calls if c["route"] == "exhausted"]


def test_an_attempt_on_the_tenant_secret_records_so(db, worker_factory, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, _config, _ = worker_factory(runner_profile="claude-code")
    worker._account_broker = None
    worker._lease_account(worker.cfg.profile)
    worker._spend = {"total_cost_usd": 0.5}
    worker._record_spend()
    attempt = db.doc("attempts/att_1")
    assert attempt["credential_source"] == "tenant_secret"
    assert attempt["account_id"] is None and attempt["credential_tier"] is None


def test_a_key_holders_spend_is_what_ran_on_its_hold(db, worker_factory):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, _config, _ = worker_factory(runner_profile="claude-code")
    worker._spend = {"total_cost_usd": 2.0}
    worker._hold_began()
    worker._spend = {"total_cost_usd": 2.75}
    assert worker._hold_spend() == 0.75
    worker._spend = {"total_cost_usd": 2.0}
    assert worker._hold_spend() is None

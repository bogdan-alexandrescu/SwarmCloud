"""A dead pool token moves the attempt; a declined pool parks (H12-MOVE-2560).

#1855 (BGD 2026-10-28: "it should automatically move in the future on this
error"). A long claude-code attempt failed after 6,160 s on "401 OAuth access
token has expired": the worker reloaded the SAME account's secret, which held
the same dead token, and then failed. Now a refused credential on a held
account whose token came back unchanged, whose reloaded token is refused
again, or whose account the broker says needs re-authentication, MOVES the
attempt -- one `unusable` swap through the broker, a checkpoint first, then
`--resume` of the same session on the new account in the same lease, attempt
and generation. Only when the broker has no healthy account does it park
(NO_ACCOUNT_RETRY_SECONDS), never fail.

#2560. `_lease_account` declined the pool for task_d212c88b with no event and
fell back to the tenant's own Anthropic secret, which Anthropic refused (401).
Now every decline emits `account_pool_declined`; a broker that does not answer
is asked once more, and if it still does not, the task parks rather than
running on the tenant secret. The tenant secret stays only where the
workspace has no pool at all.

These run the real worker and the real claude-code runner against the fake
CLI and fake broker of `test_account_hot_swap`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from agent_worker import lifecycle
from agent_worker.accountlease import (
    Assignment,
    BrokerRefused,
    BrokerUnavailable,
    NoAccount,
    NoAccountAvailable,
)
from agent_worker.errors import ExitCode
from swarm_common.states import EventType, ParkReason, TaskState

from worker_seeds import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient
from test_account_hot_swap import (  # noqa: F401 - `cli` is a fixture
    FIRST,
    SECOND,
    FakeBroker,
    _assignment,
    _Secrets,
    _token,
    cli,
)

FIRST_SECRET = f"swarm-account-{TENANT}--first"
SECOND_SECRET = f"swarm-account-{TENANT}--second"
TENANT_SECRET = f"swarm-tenant-{TENANT}-anthropic"


#: The account state the real broker's hold-status route returns once its
#: refresh failed: `quota_broker.accounts.AccountState.REAUTH_REQUIRED.value`.
#: The lowercase `reauth_required` is a refresh-outcome reason, never a state.
BROKER_STATE_REAUTH_REQUIRED = "REAUTH_REQUIRED"


class _StateBroker(FakeBroker):
    """The hot-swap broker, with the held account's `state` on its hold status."""

    def __init__(self, *, state=None, **kwargs):
        super().__init__(**kwargs)
        self.state = state

    def hold_status(self, assignment):
        status = super().hold_status(assignment)
        if assignment.account_id == FIRST:
            status["state"] = self.state
        return status


def _run(db, worker_factory, tmp_path, broker, *, modes, rotating=()):
    tokens = {f"swarm-account-{TENANT}--{label}": _token()
              for label in ("first", "second", "third")}
    sessions = [str(uuid.uuid4()) for _ in modes]
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=["anthropic"])
    secrets = _Secrets(tokens, rotating=rotating)
    worker, _config, _ = worker_factory(
        runner_profile="claude-code", secret_client=secrets, timeout_seconds=60,
    )
    worker._account_broker = broker
    plan = tmp_path / "plan"
    (plan / "plan.json").write_text(json.dumps(
        {"modes": modes, "sessions": sessions, "flood_lines": 0}))
    code = worker.run()
    runs_file = plan / "runs.jsonl"
    runs = [json.loads(line) for line in runs_file.read_text().splitlines()] if (
        runs_file.exists()) else []
    return code, worker, runs, tokens, sessions


def _details(db, cause: str) -> list[dict]:
    return [e["detail"] for e in db.events("task_1")
            if (e.get("detail") or {}).get("cause") == cause]


def _no_secret_in_events(db, values) -> None:
    events = json.dumps(db.events("task_1"), default=str)
    for value in values:
        assert value not in events, "an event carried a credential"


# -- #1855: a dead token moves the attempt -------------------------------------


def test_an_unchanged_dead_token_moves_to_a_healthy_account_and_resumes(
    db, worker_factory, tmp_path, cli, log_stream
):
    """The refused token is what the secret still holds: a reload would start
    the CLI on the same dead token. The attempt moves instead."""
    broker = FakeBroker()
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["refused", "finish"]
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [(s["from"], s["reason"]) for s in broker.swaps] == [(FIRST, "unusable")]
    # Resumed from the same session on the new account, in the same attempt.
    assert [r["resume"] for r in runs] == [None, sessions[0]]
    assert runs[1]["token"] == tokens[SECOND_SECRET]
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert EventType.PARKED.value not in db.event_types("task_1")
    assert int(task.get("attempt_count", 1)) == 1
    # No reload of the dead token first.
    assert _details(db, "credential_reloaded") == []
    # The move, named by pool label on both sides, and never a credential.
    moved = _details(db, "account_moved")
    assert len(moved) == 1
    assert moved[0]["from_account"] == "first"
    assert moved[0]["to_account"] == "second"
    assert moved[0]["why"] == "token_unchanged"
    # A checkpoint was taken before the move (invariant 8).
    assert any(e["type"] == EventType.CHECKPOINT_STARTED.value
               and (e.get("detail") or {}).get("label") == "account-move"
               for e in db.events("task_1")), db.event_types("task_1")
    _no_secret_in_events(db, [*tokens.values(), *sessions])


def test_a_refreshed_token_refused_again_moves_instead_of_failing(
    db, worker_factory, tmp_path, cli, log_stream
):
    """The refresher wrote a new token each time, and each was refused: past
    the reloads the attempt moves rather than failing."""
    broker = FakeBroker()
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker,
        modes=["refused", "refused", "refused", "finish"], rotating=(FIRST_SECRET,),
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert len(_details(db, "credential_reloaded")) == lifecycle.MAX_CREDENTIAL_RELOADS
    assert [(s["from"], s["reason"]) for s in broker.swaps] == [(FIRST, "unusable")]
    assert runs[-1]["token"] == tokens[SECOND_SECRET]
    # The fake CLI names a session per start: the move resumes the latest.
    assert runs[-1]["resume"] == sessions[len(runs) - 2]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert [m["why"] for m in _details(db, "account_moved")] == ["refused_after_reload"]


def test_a_rotated_token_is_reloaded_on_the_same_account(
    db, worker_factory, tmp_path, cli, log_stream
):
    """The refresher DID write a successor: a reload, not a move (#626)."""
    broker = FakeBroker()
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["refused", "finish"],
        rotating=(FIRST_SECRET,),
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert broker.swaps == []
    assert len(_details(db, "credential_reloaded")) == 1
    assert runs[1]["resume"] == sessions[0]
    assert runs[1]["token"] not in (runs[0]["token"], tokens[SECOND_SECRET])


def test_an_account_marked_reauth_required_moves_at_once(
    db, worker_factory, tmp_path, cli, log_stream
):
    """The broker says the account needs a person: no reload is tried."""
    broker = _StateBroker(state=BROKER_STATE_REAUTH_REQUIRED)
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["refused", "finish"],
        rotating=(FIRST_SECRET,),
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert _details(db, "credential_reloaded") == []
    assert [(s["from"], s["reason"]) for s in broker.swaps] == [(FIRST, "unusable")]
    assert runs[1]["token"] == tokens[SECOND_SECRET]
    assert [m["why"] for m in _details(db, "account_moved")] == ["reauth_required"]


def test_a_running_agent_on_a_reauth_required_account_moves_at_its_turn_boundary(
    db, worker_factory, tmp_path, cli, log_stream
):
    """Before the token dies: the hold's status says reauth_required, and the
    agent moves at its next turn boundary as for a drain."""
    broker = _StateBroker(state=BROKER_STATE_REAUTH_REQUIRED)
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["turns", "finish"]
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [(s["from"], s["reason"]) for s in broker.swaps] == [(FIRST, "unusable")]
    assert [r["resume"] for r in runs] == [None, sessions[0]]
    assert runs[1]["token"] == tokens[SECOND_SECRET]
    assert [m["why"] for m in _details(db, "account_moved")] == ["reauth_required"]


def test_with_no_healthy_account_a_dead_token_parks_no_account_and_never_fails(
    db, worker_factory, tmp_path, cli, log_stream
):
    broker = FakeBroker(swap_to=[])
    before = datetime.now(timezone.utc)
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["refused", "finish"]
    )

    assert code == ExitCode.PARKED, log_stream.getvalue()[-3000:]
    assert len(runs) == 1
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    delay = (task["next_eligible_at"] - before).total_seconds()
    assert abs(delay - lifecycle.NO_ACCOUNT_RETRY_SECONDS) < 60
    # The dead account went back marked unusable; nothing else was held.
    assert broker.releases[0][0] == FIRST and broker.releases[0][1]
    assert [r[0] for r in broker.releases] == [FIRST]
    _no_secret_in_events(db, [*tokens.values(), *sessions])


def test_a_fenced_attempt_never_runs_the_agent_on_the_new_account(
    db, worker_factory, tmp_path, cli
):
    def fence():
        db.doc("tasks/task_1")["current_generation"] = 2

    broker = FakeBroker(swap_error=BrokerRefused("403 not_holder"), on_swap=fence)
    code, worker, runs, _tokens, _ = _run(
        db, worker_factory, tmp_path, broker, modes=["refused", "finish"]
    )

    assert code == ExitCode.GENERATION_FENCED
    assert len(runs) == 1, "a fenced attempt never resumes the agent"
    assert db.doc("tasks/task_1")["state"] != TaskState.SUCCEEDED.value


# -- #2560: a pool decline is recorded, re-asked, then parked ------------------


class _FlakyBroker:
    """`assign` answers each of `answers` in turn: an exception is raised."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.assigns = 0
        self.releases: list[str] = []

    def assign(self, provider, *, exclude=()):
        self.assigns += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def release(self, account_id, assignment_id, *, unusable=""):
        self.releases.append(account_id)
        return 0


def _pool_worker(db, worker_factory, tmp_path, broker):
    from agent_worker import workspace as workspace_mod

    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "x"})
    seed_tenant(db, credentials=["anthropic"])
    values = {FIRST_SECRET: _token(), TENANT_SECRET: _token()}
    secrets = FakeSecretClient(values)
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = broker
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    return worker, secrets, values


@pytest.mark.parametrize("error", [
    BrokerUnavailable("connection refused"), RuntimeError("something unforeseen"),
])
def test_a_declined_pool_is_recorded_and_asked_again(db, worker_factory, tmp_path, error):
    broker = _FlakyBroker([error, _assignment(FIRST, 1)])
    worker, secrets, values = _pool_worker(db, worker_factory, tmp_path, broker)

    env = worker._build_child_env()

    assert broker.assigns == 2
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == values[FIRST_SECRET]
    assert TENANT_SECRET not in secrets.accessed
    declined = _details(db, "account_pool_declined")
    assert len(declined) == 1
    assert declined[0]["reason"] in ("broker_unreachable", "broker_client_error")
    assert declined[0]["pool"] == "anthropic"
    assert declined[0]["task_id"] == "task_1"
    assert declined[0]["attempt_id"] == "att_1"
    assert declined[0]["then"] == "asking_again"
    _no_secret_in_events(db, values.values())


def test_a_pool_that_still_declines_parks_instead_of_using_the_tenant_secret(
    db, worker_factory, tmp_path
):
    broker = _FlakyBroker([BrokerUnavailable("connection refused")])
    worker, secrets, values = _pool_worker(db, worker_factory, tmp_path, broker)

    with pytest.raises(NoAccountAvailable) as raised:
        worker._build_child_env()

    assert broker.assigns == 2, "asked once more, and only once"
    assert raised.value.decision.reason == "broker_unreachable"
    assert TENANT_SECRET not in secrets.accessed
    assert [d["then"] for d in _details(db, "account_pool_declined")] == [
        "asking_again", "parking"]
    _no_secret_in_events(db, values.values())


def test_a_whole_attempt_whose_pool_declines_parks_and_never_runs(
    db, worker_factory, tmp_path, cli, log_stream
):
    broker = _FlakyBroker([BrokerUnavailable("connection refused")])
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "x"})
    seed_tenant(db, credentials=["anthropic"])
    values = {TENANT_SECRET: _token()}
    secrets = FakeSecretClient(values)
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = broker
    before = datetime.now(timezone.utc)

    code = worker.run()

    assert code == ExitCode.PARKED, log_stream.getvalue()[-3000:]
    assert not (tmp_path / "plan" / "runs.jsonl").exists(), "the agent never started"
    assert TENANT_SECRET not in secrets.accessed
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    delay = (task["next_eligible_at"] - before).total_seconds()
    assert abs(delay - lifecycle.NO_ACCOUNT_RETRY_SECONDS) < 60
    _no_secret_in_events(db, values.values())


@pytest.mark.parametrize("broker_answer, cause", [
    (None, "no_broker_configured"),
    (NoAccount(reason="no_accounts_registered"), "no_accounts_registered"),
])
def test_a_workspace_with_no_pool_keeps_the_tenant_secret_and_says_so(
    db, worker_factory, tmp_path, broker_answer, cause
):
    broker = None if broker_answer is None else _FlakyBroker([broker_answer])
    worker, secrets, values = _pool_worker(db, worker_factory, tmp_path, broker)

    env = worker._build_child_env()

    assert TENANT_SECRET in secrets.accessed
    assert values[TENANT_SECRET] in env.values()
    declined = _details(db, "account_pool_declined")
    assert [(d["reason"], d["then"]) for d in declined] == [(cause, "tenant_secret")]
    _no_secret_in_events(db, values.values())


_ = (Assignment, SECOND)

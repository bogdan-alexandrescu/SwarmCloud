"""A running agent moves to another account WITHIN its attempt (S13/S14).

Owner decision 2026-10-01: an agent whose account runs out, or whose account
an operator drains, does not checkpoint, park and requeue. At the next TURN
BOUNDARY -- after a complete `user` event, before the next model request --
the runner stops the CLI, the worker moves the hold through ONE broker call,
and the CLI is restarted with `--resume <session_id>` under the new account's
credential: the same lease, the same attempt, the same workspace, no PARKED
transition, no new attempt.

These run the real worker and the real claude-code runner against a fake CLI
(a Python script printing a stream-json conversation) and a fake broker. They
pin:

  * an exhausted account at a turn boundary swaps and the run continues under
    `--resume` in the same attempt with no PARKED event;
  * no other account keeps the hold and parks as today;
  * a drain is learned from the hold's status at a turn boundary and moves the
    agent the same way;
  * a fenced attempt's swap is refused, and the attempt stands down;
  * an account that cannot be read after a move is moved off as `unusable`;
  * the session id and both tokens appear in no log line and no event;
  * readings reach the broker, and a broker 5xx on them changes nothing.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker.accountlease import (
    ACCOUNT_TOKEN_ENV,
    Assignment,
    BrokerRefused,
    BrokerUnavailable,
    NoAccount,
)
from agent_worker.errors import ExitCode
from swarm_common.states import EventType, ParkReason, TaskState

from conftest import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient

FIRST = f"{TENANT}:first"
SECOND = f"{TENANT}:second"
THIRD = f"{TENANT}:third"


def _token() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "oat01-" + pysecrets.token_hex(16)


def _assignment(account_id: str, n: int) -> Assignment:
    label = account_id.split(":", 1)[1]
    return Assignment(
        account_id=account_id,
        secret=f"swarm-account-{TENANT}--{label}",
        assignment_id=f"asg-{n}",
        account={"account_id": account_id, "owner_tenant": TENANT, "label": label},
    )


class FakeBroker:
    """The pool routes, recording every call. `swap_to` is the queue of answers."""

    def __init__(self, *, swap_to=None, move=None, swap_error=None,
                 reading_error=None, on_swap=None) -> None:
        self.first = _assignment(FIRST, 1)
        self.swap_to = list(swap_to if swap_to is not None else [_assignment(SECOND, 2)])
        self.move = move
        self.swap_error = swap_error
        self.reading_error = reading_error
        self.on_swap = on_swap
        self.assigns: list[str] = []
        self.swaps: list[dict[str, Any]] = []
        self.releases: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.readings: list[tuple[str, dict]] = []

    def assign(self, provider, *, exclude=()):
        self.assigns.append(provider)
        return self.first

    def release(self, account_id, assignment_id, *, unusable=""):
        self.releases.append((account_id, unusable))
        return 0

    def swap(self, assignment, *, provider, reason, exclude=()):
        self.swaps.append({"from": assignment.account_id, "reason": reason,
                           "exclude": tuple(exclude)})
        if self.on_swap is not None:
            self.on_swap()
        if self.swap_error is not None:
            raise self.swap_error
        if not self.swap_to:
            return NoAccount(reason="no_account_available")
        return self.swap_to.pop(0)

    def hold_status(self, assignment):
        self.statuses.append(assignment.account_id)
        return {"held": True, "move": self.move if assignment.account_id == FIRST else None}

    def report_reading(self, assignment, windows):
        self.readings.append((assignment.account_id, dict(windows)))
        if self.reading_error is not None:
            raise self.reading_error
        return True


#: The fake CLI. Its PLAN is a file named by the prompt; each start appends a
#: record of how it was started to `runs.jsonl` beside it.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
argv = sys.argv[1:]
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
plan_dir = pathlib.Path(PLAN_DIR)
plan = json.loads((plan_dir / "plan.json").read_text())
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
with (plan_dir / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "token": token}) + "\n")
runs = sum(1 for _ in (plan_dir / "runs.jsonl").open())

def say(event):
    print(json.dumps(event), flush=True)

session = plan["sessions"][min(runs - 1, len(plan["sessions"]) - 1)]
say({"type": "system", "subtype": "init", "session_id": session})
mode = plan["modes"][min(runs - 1, len(plan["modes"]) - 1)]
reset = int(time.time()) + 3600
if mode == "exhaust":
    say({"type": "assistant", "session_id": session,
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]}})
    say({"type": "rate_limit_event", "session_id": session, "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "resetsAt": reset}})
    say({"type": "user", "session_id": session, "parent_tool_use_id": None,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    time.sleep(30)
    sys.exit(0)
if mode == "turns":
    for i in range(200):
        say({"type": "assistant", "session_id": session, "message": {"content": [
            {"type": "tool_use", "id": "t%d" % i, "name": "Bash"}]}})
        say({"type": "rate_limit_event", "session_id": session, "rate_limit_info": {
            "status": "allowed", "rateLimitType": "seven_day", "resetsAt": reset * 2,
            "utilization": 0.25}})
        say({"type": "user", "session_id": session, "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % i,
                                      "content": "ok"}]}})
        time.sleep(0.2)
    sys.exit(0)
if mode == "refused":
    say({"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": "OAuth access token has been revoked"})
    sys.exit(1)
say({"type": "rate_limit_event", "session_id": session, "rate_limit_info": {
    "status": "allowed", "rateLimitType": "five_hour", "resetsAt": reset, "utilization": 0.1}})
say({"type": "result", "subtype": "success", "is_error": False, "result": "done",
     "session_id": session})
sys.exit(0)
'''


@pytest.fixture
def cli(tmp_path, monkeypatch) -> Path:
    binary = tmp_path / "fake-claude"
    plan = tmp_path / "plan"
    plan.mkdir()
    binary.write_text(FAKE_CLI.replace("PLAN_DIR", repr(str(plan)), 1))
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


class _Secrets(FakeSecretClient):
    """Secret Manager, with some secrets that cannot be read."""

    def __init__(self, values, unreadable=()):
        super().__init__(values)
        self.unreadable = set(unreadable)

    def access(self, secret_name, version="latest"):
        if secret_name in self.unreadable:
            self.accessed.append(secret_name)
            raise PermissionError("secretAccessor missing")
        return super().access(secret_name, version)


def _run(db, worker_factory, tmp_path, broker, *, modes, unreadable=(), timeout=60):
    tokens = {f"swarm-account-{TENANT}--{label}": _token()
              for label in ("first", "second", "third")}
    sessions = [str(uuid.uuid4()) for _ in modes]
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=["anthropic"])
    secrets = _Secrets(tokens, unreadable=unreadable)
    worker, config, _ = worker_factory(
        runner_profile="claude-code", secret_client=secrets, timeout_seconds=timeout,
    )
    worker._account_broker = broker
    plan = tmp_path / "plan"
    (plan / "plan.json").write_text(json.dumps({"modes": modes, "sessions": sessions}))
    code = worker.run()
    runs_file = plan / "runs.jsonl"
    runs = [json.loads(line) for line in runs_file.read_text().splitlines()] if (
        runs_file.exists()) else []
    return code, worker, runs, tokens, sessions


def _types(db) -> list[str]:
    return db.event_types("task_1")


def test_an_exhausted_account_swaps_at_a_turn_boundary_and_the_run_continues(
    db, worker_factory, tmp_path, cli, log_stream
):
    broker = FakeBroker()
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"]
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [s["reason"] for s in broker.swaps] == ["exhausted"]
    assert broker.swaps[0]["from"] == FIRST
    # The second start continued the FIRST session, under the SECOND account.
    assert [r["resume"] for r in runs] == [None, sessions[0]]
    assert runs[0]["token"] == tokens[f"swarm-account-{TENANT}--first"]
    assert runs[1]["token"] == tokens[f"swarm-account-{TENANT}--second"]
    # Same attempt, same lease, no park, no new attempt.
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert EventType.PARKED.value not in _types(db)
    assert EventType.QUOTA_EXHAUSTED.value not in _types(db)
    assert int(task.get("attempt_count", 1)) == 1
    assert [d for d in db.documents if d.startswith("attempts/")] == ["attempts/att_1"]
    # The exit path gave back the account it ended on, and only that one: the
    # first hold went in the swap's own transaction.
    assert broker.releases == [(SECOND, "")]
    assert broker.assigns == ["anthropic"], "a swap is not a second assign"
    moved = [e["detail"] for e in db.events("task_1")
             if (e.get("detail") or {}).get("swapped_from")]
    assert moved == [{"cause": "account_assigned", "account_id": SECOND,
                      "provider": "anthropic", "swapped_from": FIRST,
                      "swap_reason": "exhausted"}]


def test_no_other_account_keeps_the_hold_and_parks_as_today(
    db, worker_factory, tmp_path, cli
):
    broker = FakeBroker(swap_to=[])
    code, worker, runs, _tokens, _ = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"]
    )

    assert code == ExitCode.PARKED
    assert len(runs) == 1, "no resume without an account to resume on"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert [s["reason"] for s in broker.swaps] == ["exhausted"]
    # The broker kept the hold through the refused swap; the exit gave it back.
    assert broker.releases == [(FIRST, "")]


def test_a_drain_moves_the_agent_at_its_next_turn_boundary(
    db, worker_factory, tmp_path, cli, log_stream
):
    broker = FakeBroker(move="drain")
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["turns", "finish"]
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [s["reason"] for s in broker.swaps] == ["drain"]
    assert [r["resume"] for r in runs] == [None, sessions[0]]
    assert runs[1]["token"] == tokens[f"swarm-account-{TENANT}--second"]
    assert EventType.PARKED.value not in _types(db)
    assert broker.statuses and set(broker.statuses) <= {FIRST, SECOND}
    # At most once per turn, never per stream line: the fake prints three
    # lines a turn, and fewer status reads than turns were ever made.
    assert len(broker.statuses) <= 200


def test_a_fenced_attempts_swap_is_refused_and_it_stands_down(
    db, worker_factory, tmp_path, cli
):
    def fence():
        # The reconciler fenced this attempt while its CLI was stopping: the
        # task moved to a newer generation and the hold was released.
        db.doc("tasks/task_1")["current_generation"] = 2

    broker = FakeBroker(swap_error=BrokerRefused("403 not_holder"), on_swap=fence)
    code, worker, runs, _tokens, _ = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"]
    )

    assert code == ExitCode.GENERATION_FENCED
    assert len(runs) == 1, "a fenced attempt never resumes the agent"
    assert db.doc("tasks/task_1")["state"] != TaskState.SUCCEEDED.value


def test_an_unreadable_account_after_a_move_is_moved_off_as_unusable(
    db, worker_factory, tmp_path, cli
):
    broker = FakeBroker(swap_to=[_assignment(SECOND, 2), _assignment(THIRD, 3)])
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"],
        unreadable=(f"swarm-account-{TENANT}--second",),
    )

    assert code == ExitCode.OK
    assert [(s["from"], s["reason"]) for s in broker.swaps] == [
        (FIRST, "exhausted"), (SECOND, "unusable"),
    ]
    assert SECOND in broker.swaps[1]["exclude"]
    assert runs[1]["token"] == tokens[f"swarm-account-{TENANT}--third"]
    assert runs[1]["resume"] == sessions[0]


def test_every_unreadable_account_is_given_back_unusable_and_the_attempt_parks(
    db, worker_factory, tmp_path, cli
):
    broker = FakeBroker(swap_to=[_assignment(SECOND, 2)])
    code, worker, runs, _tokens, _ = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"],
        unreadable=(f"swarm-account-{TENANT}--second",),
    )

    assert code == ExitCode.PARKED
    assert len(runs) == 1
    assert broker.releases[0][0] == SECOND and broker.releases[0][1], "given back unusable"


def test_a_credential_refused_right_after_a_move_moves_again_as_unusable(
    db, worker_factory, tmp_path, cli
):
    broker = FakeBroker(swap_to=[_assignment(SECOND, 2), _assignment(THIRD, 3)])
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "refused", "finish"]
    )

    assert code == ExitCode.OK
    assert [(s["from"], s["reason"]) for s in broker.swaps] == [
        (FIRST, "exhausted"), (SECOND, "unusable"),
    ]
    assert runs[2]["token"] == tokens[f"swarm-account-{TENANT}--third"]


def test_no_session_id_or_token_reaches_a_log_line_or_an_event(
    db, store, worker_factory, tmp_path, cli, log_stream
):
    broker = FakeBroker()
    code, worker, runs, tokens, sessions = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"]
    )
    assert code == ExitCode.OK

    logs = log_stream.getvalue()
    # Every log the attempt uploaded: the worker's capture of the runner and
    # the runner's capture of the CLI's stderr. Not the CLI's stdout, which is
    # the agent's own stream-json transcript and names its session itself.
    stderr_keys = [k for k in store.list_keys("tenants/")
                   if k.endswith(".log") and "stdout" not in k]
    assert stderr_keys, "the attempt uploaded its logs"
    runner_stderr = "".join(
        store.download_bytes(k).decode("utf-8", errors="replace") for k in stderr_keys
    )
    events = json.dumps([e for e in db.events("task_1")], default=str)
    for secret in [*sessions, *tokens.values()]:
        assert secret not in logs, "a worker log line carried it"
        assert secret not in runner_stderr, "a runner log line carried it"
        assert secret not in events, "an event carried it"
    assert "<session>" in runner_stderr, "the resumed argv was logged, masked"


def test_readings_reach_the_broker_and_a_broker_5xx_changes_nothing(
    db, worker_factory, tmp_path, cli, log_stream
):
    broker = FakeBroker(reading_error=BrokerUnavailable("the quota broker answered 503"))
    code, worker, runs, _tokens, _ = _run(
        db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"]
    )

    assert code == ExitCode.OK
    sent = {(account, name) for account, windows in broker.readings for name in windows}
    assert (FIRST, "five_hour") in sent, "the exhausted window was reported for FIRST"
    first = [w for a, w in broker.readings if a == FIRST]
    assert first[0]["five_hour"]["utilization"] == 1.0
    assert set(first[0]["five_hour"]) == {"utilization", "resets_at"}
    assert "could not forward the account's rate-limit readings" in log_stream.getvalue()


def test_a_profile_that_holds_no_account_is_unchanged(db, worker_factory, tmp_path, cli):
    """No broker: the tenant secret, no channel, no watcher, no swap."""
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "x"})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(
        runner_profile="claude-code",
        secret_client=FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": _token()}),
    )
    from agent_worker import workspace as workspace_mod

    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    env = worker._build_child_env()

    assert ACCOUNT_TOKEN_ENV in env
    assert not any(k.startswith("SWARM_ACCOUNT_") or k == "SWARM_RESUME_SESSION" for k in env)


def test_the_swap_is_not_attempted_past_the_cap(db, worker_factory, tmp_path, cli):
    from agent_worker import lifecycle

    broker = FakeBroker()
    original = lifecycle.MAX_ACCOUNT_SWAPS
    lifecycle.MAX_ACCOUNT_SWAPS = 0
    try:
        code, worker, runs, _tokens, _ = _run(
            db, worker_factory, tmp_path, broker, modes=["exhaust", "finish"]
        )
    finally:
        lifecycle.MAX_ACCOUNT_SWAPS = original
    assert code == ExitCode.PARKED
    assert broker.swaps == []


_ = (datetime, timedelta, timezone)

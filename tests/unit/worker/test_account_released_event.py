"""The worker says when it gives a pool account back (#380, worker half).

`account_assigned` has been on the task's events since the pool shipped; the
release was only a log line. So "which account is this agent on NOW" could be
read from events only as "the last one assigned", which stays true after the
account went back. `_give_back` now emits `account_released` once per release,
naming the account and whether it went back unusable -- and nothing else:
never the token, never the secret's payload, never the broker's error text.
"""

from __future__ import annotations

import json
import secrets

from agent_worker.errors import ExitCode
from swarm_common.states import EventType

from worker_seeds import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient
from test_account_lease import (
    ACCOUNT_ID,
    ACCOUNT_SECRET,
    TOKEN,
    TENANT_SECRET,
    FakeBroker,
    _other,
    _pool_worker,
)

RELEASED = "account_released"
# Built at runtime: the point of the secret-absence assertions below is that no
# credential-shaped literal sits in this file for a scanner (or a log) to find.
TENANT_KEY = "sk-ant-" + secrets.token_hex(16)
OTHER_KEY = "sk-ant-" + secrets.token_hex(16)


def _released(db) -> list[dict]:
    return [
        event for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == RELEASED
    ]


def _secrets() -> FakeSecretClient:
    return FakeSecretClient({ACCOUNT_SECRET: TOKEN, TENANT_SECRET: TENANT_KEY})


def test_a_release_emits_account_released_once(db, worker_factory, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, _secrets(), broker, tmp_path)
    worker._build_child_env()

    worker._release_account()
    worker._release_account()

    events = _released(db)
    assert len(events) == 1, events
    event = events[0]
    assert event["type"] == EventType.LEASE_RELEASED.value
    assert event["task_id"] == "task_1" and event["attempt_id"] == "att_1"
    assert event["detail"]["account_id"] == ACCOUNT_ID
    assert event["detail"]["unusable"] is False


def test_the_event_carries_no_secret_material(db, worker_factory, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = _pool_worker(worker_factory, _secrets(), FakeBroker(), tmp_path)
    worker._build_child_env()

    worker._release_account()

    text = json.dumps(_released(db), default=str)
    assert TOKEN not in text
    assert TENANT_KEY not in text and OTHER_KEY not in text
    assert ACCOUNT_SECRET not in text, "the secret's name is not the event's to carry"
    assert "sk-ant" not in text


def test_an_unreadable_account_is_released_as_unusable_without_the_error_text(
    db, worker_factory, tmp_path
):
    from google.api_core import exceptions as gexc

    class NotFoundOnce(FakeSecretClient):
        def access(self, name, version="latest"):
            if name == ACCOUNT_SECRET:
                self.accessed.append(name)
                raise gexc.NotFound(f"Secret {ACCOUNT_SECRET} version latest not found.")
            return super().access(name, version)

    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    other = _other()
    broker = FakeBroker(outcomes=[FakeBroker().outcome, other])
    worker, _, _ = _pool_worker(
        worker_factory, NotFoundOnce({other.secret: OTHER_KEY}), broker, tmp_path
    )
    worker._build_child_env()
    worker._release_account()

    events = _released(db)
    assert [e["detail"]["account_id"] for e in events] == [ACCOUNT_ID, other.account_id]
    assert [e["detail"]["unusable"] for e in events] == [True, False]
    text = json.dumps(events, default=str)
    assert "not found" not in text.lower(), "the broker's reason is not copied in"
    assert TOKEN not in text
    assert TENANT_KEY not in text and OTHER_KEY not in text


def test_a_whole_run_emits_one_release_after_the_assignment(db, worker_factory):
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "x"})
    seed_tenant(db, credentials=["anthropic"])
    from swarm_common.models import ProviderState

    db.seed(
        f"quota/anthropic:{TENANT}",
        {"provider": "anthropic", "tenant_id": TENANT,
         "state": ProviderState.EXHAUSTED.value, "retry_after_seconds": 2400,
         "updated_at": None},
    )
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=_secrets())
    worker._account_broker = broker

    assert worker.run() == ExitCode.PARKED
    causes = [(e.get("detail") or {}).get("cause") for e in db.events("task_1")]
    assert causes.count("account_assigned") == 1
    assert causes.count(RELEASED) == 1
    assert causes.index("account_assigned") < causes.index(RELEASED)


def test_a_failed_release_emits_nothing(db, worker_factory, tmp_path):
    from agent_worker.accountlease import BrokerUnavailable

    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker(release_error=BrokerUnavailable("gone"))
    worker, _, _ = _pool_worker(worker_factory, _secrets(), broker, tmp_path)
    worker._build_child_env()

    worker._release_account()

    assert _released(db) == []


def test_a_fenced_worker_gives_the_account_back_and_writes_no_event(db, worker_factory):
    """The account is this worker's to return, the task's event stream is not:
    a superseded attempt writes into it nothing at all (`_stand_down`)."""
    from agent_worker.errors import FencedError

    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "x"})
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=_secrets())
    worker._account_broker = broker
    original = worker.control.validate_generation
    calls = {"n": 0}

    def fencing_validate():
        calls["n"] += 1
        if calls["n"] == 1:
            return original()
        raise FencedError(expected=1, actual=2)

    worker.control.validate_generation = fencing_validate

    assert worker.run() == ExitCode.GENERATION_FENCED
    assert broker.releases == [ACCOUNT_ID]
    assert _released(db) == []

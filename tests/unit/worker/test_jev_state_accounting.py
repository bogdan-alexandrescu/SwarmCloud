"""Jev spend on the state routes, end to end (lane AQ-JEV-ACCOUNTING).

The worker's own `judge.attempt.setup`, `JudgeClient.decide` and
`judge.attempt.settle` over its real `StateCall`, against swarm-api's real
routes on the in-memory Firestore (`state_routes_harness`). Before this lane
the state routes settled nothing (`settle` returned None) and a cap made
every call `skipped:budget_monthly`. Offline: no credentials, no emulator,
and no Firestore client on the worker's side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker.judge import account, attempt, budget, client, register, settings
from agent_worker.statecall import StateCall

from control_plane.fakes import FakeFirestore as ApiFirestore
from state_routes_harness import TASK, TENANT, RoutesOpener, api_client, remote_control, seed_attempt
from test_jev_wiring import INTERNAL_URL, FakeJev

ENV = {settings.INTERNAL_URL_ENV: INTERNAL_URL, attempt.VERDICT_ENV: "true"}
QUESTIONS = {"keep": {"type": "noul", "instructions": "Is this relevant?"}}


@pytest.fixture(autouse=True)
def no_firestore_client(monkeypatch):
    from google.cloud import firestore

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a Firestore client was built")

    monkeypatch.setattr(firestore, "Client", refuse)


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


@pytest.fixture
def opener(api_db) -> RoutesOpener:
    return RoutesOpener(api_client(api_db))


def _opened(api_db, opener, **ids: Any) -> StateCall:
    seed_attempt(api_db, **ids)
    control = remote_control(opener, **ids)
    control.validate_generation()
    return control._calls


def _cap(api_db, cap: int | None) -> None:
    api_db.docs[f"workspace_settings/{TENANT}"] = {
        "code_intelligence": {"jev": True, "jev_monthly_cap_tokens": cap}}


def _setup(tmp_path: Path, state: StateCall, name: str = "att") -> tuple[dict[str, str], dict]:
    work = tmp_path / name / "work"
    work.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {}
    env = attempt.setup(work=work, db=state, tenant_id=TENANT, repository_url=None,
                        environ=ENV, record=record)
    return env, record


def _client(env: dict[str, str], transport: Any) -> client.JudgeClient:
    judge, why = attempt.load(env, transport=transport, sleep=lambda _s: None)
    assert judge is not None, why
    return judge


def _row(api_db, generation: int = 1, task: str = TASK) -> dict[str, Any]:
    return api_db.docs[f"jev_usage/{TENANT}_{task}_g{generation}"]


def _settle(env, state, outcome: str = "succeeded"):
    return attempt.settle(env, db=state, run_transaction=lambda fn: None, tenant_id=TENANT,
                          task_id=state.task_id, generation=state.generation, outcome=outcome)


def test_settle_through_the_state_routes_records_one_record_per_call(api_db, opener, tmp_path):
    state = _opened(api_db, opener)
    _cap(api_db, None)
    env, record = _setup(tmp_path, state)
    assert env, record
    judge = _client(env, FakeJev())
    assert judge.decide("J4", {"diff": "x = 1"}, QUESTIONS).outcome == "ok"
    assert judge.decide("J4", {"diff": "y = 2"}, QUESTIONS).outcome == "ok"
    assert _settle(env, state) == (2, 2_400)
    row = _row(api_db)
    assert (row["calls"], row["input_tokens"]) == (2, 2_400)
    assert row["settlement"]["outcome"] == "succeeded"
    calls = [d for p, d in api_db.docs.items() if p.startswith("jev_usage/") and
             d.get("kind") == "call"]
    assert len(calls) == 2
    assert {c["requested_model"] for c in calls} == {judge.backend.model or None}
    assert api_db.docs[f"jev_usage/{TENANT}_reservations"]["held"] == {}


def test_a_capped_tenant_on_the_state_routes_is_granted_its_month(api_db, opener, tmp_path):
    """Before: an unread month counted as spent, so every call was skipped."""
    state = _opened(api_db, opener)
    _cap(api_db, 1_000_000)
    env, record = _setup(tmp_path, state)
    assert record["reservation"]["input_tokens"] == budget.MAX_INPUT_TOKENS_PER_TASK
    assert _client(env, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS).outcome == "ok"


@pytest.mark.parametrize("outcome", ["parked", "cancelled", "crashed", "retried"])
def test_the_park_cancel_and_crash_paths_settle(api_db, opener, tmp_path, outcome):
    state = _opened(api_db, opener)
    _cap(api_db, None)
    env, _ = _setup(tmp_path, state)
    _client(env, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS)
    if outcome == "cancelled":
        api_db.docs[f"tasks/{TASK}"]["state"] = "CANCELLED"
    assert _settle(env, state, outcome) == (1, 1_200)
    assert _row(api_db)["settlement"]["outcome"] == outcome
    assert _row(api_db)["input_tokens"] == 1_200


def test_a_replayed_settlement_does_not_charge_twice(api_db, opener, tmp_path):
    state = _opened(api_db, opener)
    _cap(api_db, None)
    env, _ = _setup(tmp_path, state)
    _client(env, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS)
    _settle(env, state)
    # The ledger forgets it settled (a lost answer, a restored checkpoint):
    # every record and the settle are sent again, and charged once.
    ledger = budget.Ledger(Path(json.loads(Path(env[attempt.CONFIG_ENV]).read_text())["ledger"]))
    ledger.restore_unsettled(1, 1_200, 1)
    _settle(env, state)
    assert (_row(api_db)["calls"], _row(api_db)["input_tokens"]) == (1, 1_200)
    month = [d for p, d in api_db.docs.items()
             if p.startswith(f"jev_usage/{TENANT}_20") and "kind" not in d]
    assert [m["input_tokens"] for m in month] == [1_200]


def test_two_concurrent_attempts_cannot_both_spend_the_last_of_the_allowance(
        api_db, opener, tmp_path):
    first = _opened(api_db, opener)
    second = _opened(api_db, opener, task_id="task_remote_2", attempt_id="att_remote_2",
                     lease_id="lease_remote_2")
    _cap(api_db, budget.MEASURED_TOKENS_PER_CALL)
    env_one, _ = _setup(tmp_path, first, "one")
    env_two, record_two = _setup(tmp_path, second, "two")
    assert record_two["reservation"]["reason"] == "budget_monthly"
    assert _client(env_one, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS).outcome == "ok"
    late = _client(env_two, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS)
    # Granted nothing: no call at all, whatever the local file would allow.
    assert (late.outcome, late.reason) == ("skipped", "budget_calls")


def test_editing_the_local_ledger_cannot_raise_authority(api_db, opener, tmp_path):
    state = _opened(api_db, opener)
    _cap(api_db, budget.MEASURED_TOKENS_PER_CALL)
    env, _ = _setup(tmp_path, state)
    config = json.loads(Path(env[attempt.CONFIG_ENV]).read_text())
    judge, _ = register.worker_client(register.Registered(
        True, None, Path(env[attempt.CONFIG_ENV]), Path(config["ledger"]),
        settings.Backend("internal-jev", INTERNAL_URL, "m", stringify_state=False),
        grant=budget.Grant.from_mapping(config["grant"]))), None
    judge.transport = FakeJev()
    assert judge.decide("J4", {"diff": "x"}, QUESTIONS).outcome == "ok"
    # The agent's user rewrites the ledger: a bigger allowance, no spend.
    path = Path(config["ledger"])
    forged = json.loads(path.read_text())
    forged.update({"max_calls": 99, "max_input_tokens": 10**9, "calls": 0,
                   "input_tokens": 0, "monthly_cap_tokens": None})
    path.write_text(json.dumps(forged))
    again = judge.decide("J4", {"diff": "y"}, QUESTIONS)
    assert again.outcome == "skipped" and again.reason.startswith("budget_")
    # And the reserve took no number from the worker's side at all.
    with pytest.raises(Exception):
        state.post(account.RESERVE_OP, {"granted_tokens": 10**9}, deadline_seconds=1)


def test_a_failed_reserve_is_jev_off_never_unbounded(api_db, opener, tmp_path, monkeypatch):
    state = _opened(api_db, opener)
    _cap(api_db, None)

    def refused(_state: Any) -> Any:
        raise account.AccountError("no answer")

    monkeypatch.setattr(account, "reserve", refused)
    env, record = _setup(tmp_path, state)
    assert env == {}
    assert record["on"] is False


@pytest.mark.parametrize("response", [
    {"answers": {"keep": {"noul": 0.9}}},
    {"answers": {"keep": {"noul": 0.9}}, "usage": {"input_tokens": -5}},
    {"answers": {"keep": {"noul": 0.9}}, "usage": {"input_tokens": "0"}},
    {"answers": {"keep": {"noul": 0.9}}, "usage": {"input_tokens": True}},
    {"answers": {"nope": 1}, "usage": "free"},
], ids=["no-usage", "negative", "string", "bool", "malformed"])
def test_an_invalid_jev_response_cannot_bypass_the_budget(tmp_path, response):
    """A response that states no usable token count is charged what the call
    was admitted at, never 0, and says it was estimated; a malformed one
    gives no answers, so no caller acts on it."""
    ledger = budget.Ledger.create(tmp_path / "ledger.json")
    backend = settings.Backend("internal-jev", INTERNAL_URL, "jev-m", stringify_state=False)

    def transport(url: str, body: bytes, headers: Any, timeout: float):
        return 200, json.dumps(response).encode()

    judge = client.JudgeClient(backend, ledger, transport=transport, sleep=lambda _s: None)
    decision = judge.decide("J4", {"diff": "x"}, QUESTIONS)
    state = ledger.read()
    assert state["input_tokens"] >= budget.MEASURED_TOKENS_PER_CALL
    [row] = state["audit"]
    assert row["tokens_estimated"] is True
    assert "output_tokens" not in row  # unknown, never 0
    assert row["requested_model"] == "jev-m"
    if "nope" in response["answers"]:
        assert decision.outcome == "malformed" and decision.answers is None


def test_a_valid_response_records_its_served_model_and_output_tokens(tmp_path):
    ledger = budget.Ledger.create(tmp_path / "ledger.json")
    backend = settings.Backend("internal-jev", INTERNAL_URL, "jev-m", stringify_state=False)
    body = {"answers": {"keep": {"noul": 0.9}}, "model": "jev-m-2",
            "usage": {"input_tokens": 700, "output_tokens": 40}}
    judge = client.JudgeClient(backend, ledger, transport=lambda *a: (200, json.dumps(body)
                                                                    .encode()))
    assert judge.decide("J4", {"diff": "x"}, QUESTIONS).outcome == "ok"
    [row] = ledger.read()["audit"]
    assert (row["input_tokens"], row["output_tokens"], row["served_model"],
            row["tokens_estimated"]) == (700, 40, "jev-m-2", False)
    record = account.call_record(row)
    assert record is not None
    assert (record["requested_model"], record["served_model"]) == ("jev-m", "jev-m-2")


@pytest.mark.parametrize("end,outcome", [
    ("SUCCEEDED", "succeeded"), ("FAILED", "failed"), ("PARKED", "parked"),
    ("CANCELLED", "cancelled"), ("READY", "retried"), (None, "crashed"),
])
def test_after_the_agent_records_go_and_the_exit_settles_with_the_committed_end(
        api_db, opener, tmp_path, end, outcome):
    """The lifecycle's two calls: `settle` after the agent sends the records
    only; `settle_at_exit`, on every exit, settles with how the attempt ended
    -- a worker that committed no end (a crash) settles `crashed`."""
    from swarm_common.states import TaskState

    state = _opened(api_db, opener)
    _cap(api_db, None)
    env, _ = _setup(tmp_path, state)
    _client(env, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS)
    attempt.settle(env, db=state, run_transaction=lambda fn: None, tenant_id=TENANT,
                   task_id=TASK, generation=1)
    assert _row(api_db)["calls"] == 1 and "settlement" not in _row(api_db)
    _client(env, FakeJev()).decide("J4", {"diff": "y"}, QUESTIONS)  # a post-step's call
    attempt.settle_at_exit(env, db=state, committed_end=TaskState(end) if end else None,
                           tenant_id=TENANT, task_id=TASK, generation=1)
    row = _row(api_db)
    assert row["settlement"]["outcome"] == outcome
    assert (row["calls"], row["input_tokens"]) == (2, 2_400)
    assert row["settlement"]["unrecorded_calls"] == 0


def test_calls_beyond_the_kept_audit_settle_as_unattributed_at_the_exit(
        api_db, opener, tmp_path):
    state = _opened(api_db, opener)
    _cap(api_db, None)
    env, _ = _setup(tmp_path, state)
    _client(env, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS)
    ledger = budget.Ledger(Path(json.loads(Path(env[attempt.CONFIG_ENV]).read_text())["ledger"]))
    with ledger._locked() as held:  # a call whose audit row was not kept
        held["calls"] += 1
        held["input_tokens"] += 50
    attempt.settle(env, db=state, run_transaction=lambda fn: None, tenant_id=TENANT,
                   task_id=TASK, generation=1)
    attempt.settle_at_exit(env, db=state, committed_end=None, tenant_id=TENANT,
                           task_id=TASK, generation=1)
    row = _row(api_db)
    assert (row["calls"], row["input_tokens"]) == (2, 1_250)
    assert row["by_kind"]["unattributed"]["calls"] == 1


def test_the_direct_path_is_not_settled_at_the_exit(tmp_path):
    """No state client: `settle_at_exit` makes no call (the direct path's own
    fenced settle runs after the agent)."""
    class NoDb:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"db.{name}")

    assert attempt.settle_at_exit({attempt.CONFIG_ENV: str(tmp_path / "c.json")}, db=NoDb(),
                                  committed_end=None, tenant_id=TENANT, task_id=TASK,
                                  generation=1) is None

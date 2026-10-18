"""A switched tenant's attempt, as the entrypoint wires it, makes no Firestore call.

Lane ISO-WIRE (docs/design/tenant-settings-integrity.md §4.6, part of #1457;
owner decision 2026-10-17 ~12:40 UTC). With `SWARM_STATE_VIA_API` on,
`__main__.build_worker` hands every module that took the Firestore client the
attempt's `StateCall` as `db`, and the lifecycle builds the merge slot and
train over the state routes (`RemoteMergeSlots`, `RemoteTrainEntries`) rather
than over `ControlPlane`'s transaction helpers.

  * a whole mock attempt, built by the entrypoint itself, completes against
    swarm-api's real state routes while any Firestore client the process could
    build raises on every call;
  * a switched merge step is handed the remote slot and train, each over the
    attempt's own state client; with the switch off it is handed the
    Firestore ones, as before.

MUTATIONS: leave `db = NO_DIRECT_FIRESTORE` in `build_worker` -- the attempt
meets a named ControlPlaneError at the tenant read and FAILS. Build the
Firestore slot or train regardless of the switch -- the merge test reads the
wrong type.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import agent_worker.__main__ as entrypoint
from agent_worker import lifecycle
from agent_worker import merge as merge_mod
from agent_worker import mergeslot as mergeslot_mod
from agent_worker import mergetrain as mergetrain_mod
from agent_worker import post_verdict as post_verdict_mod
from agent_worker import secrets as secrets_mod
from agent_worker.children import ChildApi
from agent_worker.config import WorkerConfig
from agent_worker.errors import ExitCode
from agent_worker.statecall import StateCall
from swarm_api.childkey import AttemptTuple, nonce
from swarm_common.config import Settings
from swarm_common.states import TaskState

import spec_keys
from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import ExplodingChildProcess
from state_routes_harness import BASE, CHILD_KEY, WORKER_TOKEN, RoutesOpener, api_client, remote_control
from worker_seeds import ENTRYPOINT_MEMORY, TENANT, seed_attempt


class FirestoreForbidden:
    """A Firestore client that fails the test on any use at all."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        pytest.fail(f"a switched attempt touched Firestore (`db.{name}`)")


def _mirror(worker_db: Any, api_db: ApiFirestore, ids: dict[str, str]) -> dict[str, Any]:
    """The attempt's documents where swarm-api reads them, the task signed
    as swarm-api signs it at submission."""
    for path, doc in worker_db.documents.items():
        if path.startswith(("tasks/", "leases/", "pools/", "tenants/")):
            api_db.docs[path] = copy.deepcopy(doc)
    task = api_db.docs[f"tasks/{ids['task_id']}"]
    spec_keys.sign_document(task, ids["task_id"])
    return task


@pytest.fixture
def no_firestore(monkeypatch) -> list[str]:
    """Every Firestore client the process asks for. Each one raises on use."""
    from google.cloud import firestore

    built: list[str] = []

    def client(*_args: Any, **_kwargs: Any) -> FirestoreForbidden:
        built.append("firestore.Client")
        return FirestoreForbidden()

    monkeypatch.setattr(firestore, "Client", client)
    return built


@pytest.fixture
def switched_env(tmp_path, monkeypatch) -> None:
    for name in ("QUOTA_BROKER_URL", "DISPATCH_TOPIC", "WAKE_TOPIC", "SPEC_SIGNING_MODE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
        "TENANT_ID": TENANT, "GENERATION": "1", "RUNNER_PROFILE": "mock",
        "PROJECT_ID": "swarm-test", "LOCAL_ARTIFACT_ROOT": str(tmp_path / "gcs"),
        "WORKSPACE_ROOT": str(tmp_path / "ws"), "DISABLE_CLOUD_MONITORING": "1",
        "SPEC_SIGNING_KEY": spec_keys.SIGNING_KEY,
        "SPEC_VERIFY_KEYS": json.dumps(spec_keys.VERIFY_KEYS),
        "SWARM_STATE_VIA_API": "1", "SWARM_API_URL": BASE,
    }.items():
        monkeypatch.setenv(name, value)


def _entrypoint_worker(db, monkeypatch) -> tuple[Any, RoutesOpener, ApiFirestore]:
    """A switched worker as `__main__.build_worker` builds it, its socket
    answered by swarm-api's app over the attempt's mirrored documents."""
    ids = seed_attempt(db)
    api_db = ApiFirestore()
    _mirror(db, api_db, ids)
    monkeypatch.setenv("SWARM_CHILD_NONCE", nonce(
        CHILD_KEY, AttemptTuple(TENANT, ids["task_id"], ids["attempt_id"], ids["lease_id"], 1)))
    opener = RoutesOpener(api_client(api_db))
    monkeypatch.setattr(entrypoint, "ChildApi", lambda _url, audience=None: ChildApi(
        BASE, token_fetcher=lambda _audience: WORKER_TOKEN, opener=opener))
    settings = Settings.from_env()
    config = WorkerConfig.from_env(settings)

    worker = entrypoint.build_worker(config, settings, memory=ENTRYPOINT_MEMORY)
    # As worker_seeds.build_worker does: no test kills its own session or
    # waits on a real backoff or a real connect.
    worker.reap_before_publish = lambda: ()
    worker.forge_sleep = lambda _seconds: None
    worker.egress_connect = lambda _target, timeout=None: type(
        "Answered", (), {"close": lambda self: None})()
    return worker, opener, api_db


def test_a_switched_attempt_built_by_the_entrypoint_never_touches_firestore(
    db, switched_env, no_firestore, monkeypatch
):
    worker, opener, api_db = _entrypoint_worker(db, monkeypatch)

    code = worker.run()

    assert code == ExitCode.OK, code
    assert no_firestore == []
    # What every module that took the Firestore client is handed: this
    # attempt's own state client, the one the control plane calls through.
    assert isinstance(worker.db, StateCall) and worker.db is worker.control._calls
    assert opener.routes()[0] == "open"
    assert api_db.docs["tasks/task_1"]["state"] == "SUCCEEDED"
    assert api_db.docs["leases/lease_1"]["released_at"] is not None
    # The worker's own copy was never the one written.
    assert db.documents["tasks/task_1"]["state"] == "LEASED"


def test_a_switched_worker_reads_its_tenant_over_the_state_routes(
    db, switched_env, no_firestore, monkeypatch
):
    """The read the lifecycle makes for a credential (`load_tenant(self.db, ...)`),
    on the `db` the entrypoint handed it."""
    worker, opener, _api_db = _entrypoint_worker(db, monkeypatch)
    worker.control.validate_generation()

    tenant = secrets_mod.load_tenant(
        worker.db, TENANT, call_options=worker.control.call_options()
    )

    assert tenant.tenant_id == TENANT
    assert opener.routes() == ["open", "context"]
    assert no_firestore == []


def _seed_merge(db) -> dict[str, str]:
    ids = seed_attempt(db, runner_profile="merge", task_input={"prompt": "act"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    doc["metadata"] = {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                    "merge_target": {"pull_request": "task_int"}}}
    return ids


def _recorded_merge(monkeypatch) -> list[post_verdict_mod.ActionContext]:
    seen: list[post_verdict_mod.ActionContext] = []

    def run(ctx):
        seen.append(ctx)
        return post_verdict_mod.ActionOutcome(
            state=TaskState.SUCCEEDED, end_cause=None, exit_code=ExitCode.OK,
            summary={"action": "merge", "merged_by_this_task": True},
        )

    monkeypatch.setattr(merge_mod, "run_merge", run)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    return seen


def test_a_switched_merge_step_is_handed_the_remote_slot_and_train(
    db, worker_factory, monkeypatch
):
    ids = _seed_merge(db)
    api_db = ApiFirestore()
    _mirror(db, api_db, ids)
    worker, _config, _exporter = worker_factory(runner_profile="merge", sign_spec=False)
    opener = RoutesOpener(api_client(api_db))
    worker.control = remote_control(opener, task_id=ids["task_id"],
                                    attempt_id=ids["attempt_id"], lease_id=ids["lease_id"])
    # What build_worker hands a switched worker as `db`: the attempt's state client.
    worker.db = worker.control._calls
    seen = _recorded_merge(monkeypatch)

    code = worker.run()

    assert code == ExitCode.OK, code
    ctx = seen[0]
    assert type(ctx.merge_slots) is mergeslot_mod.RemoteMergeSlots
    assert ctx.merge_slots._state is worker.control._calls
    assert ctx.merge_slots.tenant_id == TENANT
    assert type(ctx.merge_trains) is mergetrain_mod.RemoteTrainEntries
    assert ctx.merge_trains._state is worker.control._calls
    assert ctx.merge_trains.tenant_id == TENANT
    assert ctx.merge_trains._task_id == ids["task_id"]


def test_an_unswitched_merge_step_is_handed_the_firestore_slot_and_train(
    db, worker_factory, monkeypatch
):
    _seed_merge(db)
    worker, _config, _exporter = worker_factory(runner_profile="merge")
    seen = _recorded_merge(monkeypatch)

    code = worker.run()

    assert code == ExitCode.OK, code
    ctx = seen[0]
    assert type(ctx.merge_slots) is mergeslot_mod.FirestoreMergeSlots
    assert type(ctx.merge_trains) is mergetrain_mod.FirestoreTrainEntries

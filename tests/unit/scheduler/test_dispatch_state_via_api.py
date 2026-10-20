"""The scheduler half of the per-tenant state switch (ISO-SCHED phase 2; part of #1457).

docs/design/tenant-settings-integrity.md §4.6 and §6. The worker half is
built: a worker with SWARM_STATE_VIA_API on reaches its attempt's state only
through swarm-api's state routes, and refuses to start without SWARM_API_URL
and SWARM_CHILD_NONCE (agent_worker/config.py `__post_init__`). Nothing set
the switch, so no tenant could be switched. These hold:

  S-1  The scheduler reads SWARM_STATE_VIA_API_TENANTS (comma-separated) into
       `state_via_api_tenants`, empty by default. An entry that cannot be a
       tenant id is dropped and logged in ONE warning, never fatal.
  S-2  Every attempt of a switched tenant, on both backends, carries
       SWARM_STATE_VIA_API=true and SWARM_API_URL, and a nonce bound to its
       OWN tuple -- a child included, which on its own would get none. A child
       keyed only for the state routes still gets SWARM_CHILD_PATH=false.
  S-3  Membership is by exact tenant id: switching `eng` switches neither
       `eng2` nor `en`, and a child of an unswitched tenant stays unkeyed.
  S-4  A switched tenant on a deployment with no child key still gets the
       switch, with no nonce, so its worker refuses to start (fail closed)
       rather than quietly taking the direct Firestore path.
  S-5  An attempt of a tenant outside the set renders exactly what it renders
       today: the GKE manifest and the Cloud Run job and execution request
       are byte-for-byte identical with and without another tenant switched.

Each assertion carries a MUTATION comment naming the change in
apps/scheduler/scheduler/dispatch.py or settings.py that turns it red.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402
from control_plane.test_dispatch_manifests import (  # noqa: E402
    NOW,
    FakeJobsClient,
    make_lease,
    make_task,
)

from swarm_api.childkey import AttemptTuple, nonce  # noqa: E402
from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402

from scheduler.dispatch import (  # noqa: E402
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    gke_worker_env,
    worker_env,
)

API_URL = "https://api.example"
# Built at run time: nothing added here may look like a credential.
CHILD_KEY = secrets.token_hex(16)


def _tenant(tenant_id: str = "eng") -> Tenant:
    return Tenant(
        tenant_id=tenant_id, kind="group", principal=f"{tenant_id}@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-{tenant_id}@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{tenant_id}",
        namespace=f"swarm-tenant-{tenant_id}",
    )


def _task(profile: str = "mock", *, tenant_id: str = "eng", parent: str | None = None) -> Task:
    return dataclasses.replace(make_task(profile), tenant_id=tenant_id, parent_task_id=parent)


def _lease(task: Task, *, generation: int = 3) -> Lease:
    return dataclasses.replace(make_lease(task), tenant_id=task.tenant_id, generation=generation)


def _settings(*switched: str, child_key: str = CHILD_KEY, **extra: Any):
    return scheduler_settings(
        child_key=child_key, swarm_api_url=API_URL, swarm_api_audience="aud",
        state_via_api_tenants=frozenset(switched), **extra,
    )


def _env(task: Task, settings, *, generation: int = 3) -> dict[str, str]:
    return worker_env(task=task, lease=_lease(task, generation=generation),
                      tenant=_tenant(task.tenant_id), settings=settings)


def _expected_nonce(task: Task, *, generation: int = 3) -> str:
    return nonce(CHILD_KEY, AttemptTuple(task.tenant_id, task.id, "att_1", "lease_1", generation))


# --- S-1: the setting ---------------------------------------------------------


def test_the_switch_list_ships_empty():
    # MUTATION: default `state_via_api_tenants` to anything non-empty in
    # SchedulerSettings and a deployment switches a tenant nobody named.
    assert scheduler_settings().state_via_api_tenants == frozenset()


def test_the_scheduler_reads_the_switch_list_from_its_environment(monkeypatch):
    from scheduler.settings import SchedulerSettings

    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.delenv("SWARM_STATE_VIA_API_TENANTS", raising=False)
    assert SchedulerSettings.from_env().state_via_api_tenants == frozenset()
    monkeypatch.setenv("SWARM_STATE_VIA_API_TENANTS", " eng, u-alice ,,dev ")
    # MUTATION: drop `state_via_api_tenants=parse_state_via_api_tenants(...)`
    # from SchedulerSettings.from_env and this reads empty.
    assert SchedulerSettings.from_env().state_via_api_tenants == frozenset(
        {"eng", "u-alice", "dev"})


def test_an_entry_that_cannot_be_a_tenant_id_is_dropped_and_logged_once(caplog):
    from scheduler.settings import parse_state_via_api_tenants

    caplog.set_level(logging.WARNING, logger="scheduler.settings")
    raw = "eng,Eng,*,eng/../other,-x, ,smoke"
    # MUTATION: raise on an unknown entry in parse_state_via_api_tenants and
    # one typo stops the scheduler, and every tenant's dispatch with it.
    parsed = parse_state_via_api_tenants(raw)
    # MUTATION: keep every non-empty entry and `*` or `Eng` sits in the set.
    assert parsed == frozenset({"eng", "smoke"})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # MUTATION: log once per dropped entry and this counts four.
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    for dropped in ("Eng", "*", "eng/../other", "-x"):
        assert repr(dropped) in message, message


def test_a_clean_list_logs_nothing(caplog):
    from scheduler.settings import parse_state_via_api_tenants

    caplog.set_level(logging.WARNING, logger="scheduler.settings")
    assert parse_state_via_api_tenants("") == frozenset()
    assert parse_state_via_api_tenants("eng,smoke") == frozenset({"eng", "smoke"})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- S-2: a switched tenant ----------------------------------------------------


def test_a_switched_root_attempt_carries_the_switch_the_address_and_its_nonce():
    task = _task()
    env = _env(task, _settings("eng"))
    # MUTATION: drop `env["SWARM_STATE_VIA_API"] = "true"` from worker_env and
    # the worker stays on direct Firestore.
    assert env["SWARM_STATE_VIA_API"] == "true"
    assert env["SWARM_API_URL"] == API_URL
    assert env["SWARM_API_AUDIENCE"] == "aud"
    # MUTATION: mint the nonce for any tuple but this attempt's own (another
    # tenant, task, lease or generation) and swarm-api refuses state/open.
    assert env["SWARM_CHILD_NONCE"] == _expected_nonce(task)
    assert "SWARM_CHILD_PATH" not in env, "a root's child path is the worker's default"


def test_a_switched_child_is_keyed_and_offered_no_child_path():
    """`mock` offers no git tools, no secret slot and no MCP server, so before
    the switch this child got no nonce at all -- and a switched worker without
    one cannot start."""
    task = _task(parent="task_parent")
    env = _env(task, _settings("eng"))
    # MUTATION: leave `keyed = not is_child or ...` without `state_via_api or`
    # and a switched child gets no nonce, so it exits 78 at startup.
    assert env["SWARM_CHILD_NONCE"] == _expected_nonce(task)
    assert env["SWARM_STATE_VIA_API"] == "true"
    assert env["SWARM_API_URL"] == API_URL
    # MUTATION: count the state routes as `other_key_use` and a child keyed
    # only for them is offered a child-submission path.
    assert env["SWARM_CHILD_PATH"] == "false"


def test_a_switched_child_with_git_tools_keeps_its_child_path():
    env = _env(_task("claude-code", parent="task_parent"), _settings("eng"))
    assert env["SWARM_STATE_VIA_API"] == "true"
    assert "SWARM_CHILD_NONCE" in env
    # MUTATION: write SWARM_CHILD_PATH=false for every switched child and its
    # git tools lose the key they are signed with.
    assert "SWARM_CHILD_PATH" not in env


def test_each_generation_gets_its_own_nonce():
    """A stale generation's nonce does not open the current one."""
    task = _task(parent="task_parent")
    first, second = (_env(task, _settings("eng"), generation=g)["SWARM_CHILD_NONCE"]
                     for g in (3, 4))
    # MUTATION: mint the switched nonce without `generation=lease.generation`
    # and a stale worker's nonce opens the live attempt.
    assert first == _expected_nonce(task, generation=3)
    assert second == _expected_nonce(task, generation=4)
    assert first != second


def test_the_gke_pod_and_the_cloud_run_execution_carry_the_switch():
    gke = gke_worker_env(task=_task(), lease=_lease(_task()), tenant=_tenant(),
                         settings=_settings("eng"))
    assert gke["SWARM_STATE_VIA_API"] == "true"
    assert "SWARM_CHILD_NONCE" in gke

    client = FakeJobsClient()
    task = _task(parent="task_parent")
    CloudRunJobDispatcher(_settings("eng"), client=client).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES["mock"], tenant=_tenant())
    env = {e.name: e.value for e in client.runs[0]["overrides"].container_overrides[0].env}
    # MUTATION: render the switch only in gke_worker_env and a Cloud Run
    # child of a switched tenant runs on direct Firestore.
    assert env["SWARM_STATE_VIA_API"] == "true"
    assert env["SWARM_CHILD_NONCE"] == _expected_nonce(task)


# --- S-3: exact membership -------------------------------------------------------


@pytest.mark.parametrize("tenant_id", ["eng2", "en", "u-eng", "smoke"])
def test_switching_one_tenant_switches_no_other(tenant_id):
    child = _env(_task(tenant_id=tenant_id, parent="task_parent"), _settings("eng"))
    # MUTATION: test membership with a prefix or substring match and a
    # neighbouring tenant is switched.
    assert "SWARM_STATE_VIA_API" not in child
    # MUTATION: force `keyed` for every attempt once the set is non-empty and
    # every child of every tenant gets a nonce.
    assert "SWARM_CHILD_NONCE" not in child
    root = _env(_task(tenant_id=tenant_id), _settings("eng"))
    assert "SWARM_STATE_VIA_API" not in root


def test_the_switch_follows_the_tasks_tenant_not_a_neighbours_nonce():
    """The nonce a switched attempt carries is bound to its own tenant: a
    worker of `eng` holds nothing that opens an attempt of `smoke`."""
    eng = _task(tenant_id="eng")
    smoke = dataclasses.replace(_task(tenant_id="smoke"), id=eng.id)
    both = _settings("eng", "smoke")
    eng_nonce = _env(eng, both)["SWARM_CHILD_NONCE"]
    smoke_nonce = _env(smoke, both)["SWARM_CHILD_NONCE"]
    # MUTATION: mint with `tenant_id=` anything but `task.tenant_id`.
    assert eng_nonce == _expected_nonce(eng)
    assert smoke_nonce == _expected_nonce(smoke)
    assert eng_nonce != smoke_nonce


# --- S-4: fail closed --------------------------------------------------------------


def test_a_switched_tenant_without_the_child_key_still_gets_the_switch():
    env = _env(_task(), _settings("eng", child_key=""))
    # MUTATION: render the switch only inside `if child_key and api_url and
    # keyed` and a deployment missing the key quietly runs the tenant on
    # direct Firestore instead of refusing.
    assert env["SWARM_STATE_VIA_API"] == "true"
    assert env["SWARM_API_URL"] == API_URL
    assert "SWARM_CHILD_NONCE" not in env, "no key, no nonce: the worker refuses to start"


# --- S-5: an unswitched tenant renders what it renders today -----------------------


def _gke_manifest(settings, task: Task) -> bytes:
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    job = dispatcher._manifest(task=task, lease=_lease(task),
                               profile=RUNNER_PROFILES[task.runner_profile], tenant=_tenant())
    return json.dumps(job, sort_keys=True, default=str).encode()


def _cloud_run_requests(settings, task: Task) -> list[bytes]:
    client = FakeJobsClient()
    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES[task.runner_profile],
        tenant=_tenant())
    out = [type(c["job"]).serialize(c["job"]) for c in client.created]
    out += [type(r["overrides"]).serialize(r["overrides"]) for r in client.runs]
    assert len(out) == 2, "one Job created and one execution run"
    return out


def _gke_profiles() -> list[str]:
    from swarm_common.profiles import Backend, resolve_backend

    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is Backend.GKE_AUTOPILOT)


def _cloud_run_profiles() -> list[str]:
    from swarm_common.profiles import Backend, resolve_backend

    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is Backend.CLOUD_RUN_JOB)


@pytest.mark.parametrize("parent", [None, "task_parent"])
def test_an_unswitched_gke_manifest_is_byte_for_byte_unchanged(parent):
    visited = 0
    for profile in _gke_profiles():
        task = _task(profile, parent=parent)
        before = _gke_manifest(_settings(), task)
        # MUTATION: render SWARM_STATE_VIA_API (even "false") for an
        # unswitched tenant, or key its children, and these bytes differ.
        assert _gke_manifest(_settings("other", "smoke"), task) == before, profile
        visited += 1
    print(f"GKE profiles compared: {visited}")
    assert visited == len(_gke_profiles()) > 0


@pytest.mark.parametrize("parent", [None, "task_parent"])
def test_an_unswitched_cloud_run_job_and_execution_are_byte_for_byte_unchanged(parent):
    visited = 0
    for profile in _cloud_run_profiles():
        task = _task(profile, parent=parent)
        before = _cloud_run_requests(_settings(), task)
        # MUTATION: as above, on the Cloud Run path: the execution override
        # merges with the Job's env, so even an empty value changes the run.
        assert _cloud_run_requests(_settings("other", "smoke"), task) == before, profile
        visited += 1
    print(f"Cloud Run profiles compared: {visited}")
    assert visited == len(_cloud_run_profiles()) > 0


def test_the_comparison_can_tell_a_switched_tenant_apart():
    """The control: the same comparison, with `eng` switched, differs on both
    backends -- so the byte-for-byte tests above could have come out red."""
    gke_task = _task(_gke_profiles()[0])
    assert _gke_manifest(_settings("eng"), gke_task) != _gke_manifest(_settings(), gke_task)
    run_task = _task(_cloud_run_profiles()[0])
    assert _cloud_run_requests(_settings("eng"), run_task) != _cloud_run_requests(
        _settings(), run_task)

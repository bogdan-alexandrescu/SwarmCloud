"""Every attempt that runs on a user slot gets a registration nonce (#1041, step 3).

The forge broker (docs/design/user-scoped-secrets.md) releases a person's
`git-u-<hex>` slot only to an attempt that proves itself with the attempt key
it registered before its agent existed. Registering that key spends the
one-use nonce `worker_env` issues, which was issued only to root tasks (the
child path). A child running on its submitter's slot would then have no key,
and the broker would refuse it its own credential. So:

  N-1  A child whose signed `forge_credential` is a user slot gets the nonce,
       the API's address and audience, and SWARM_CHILD_PATH=false: its worker
       registers the key but offers its agent no child-submission path.
  N-2  A child on the tenant token (`git`, or none) gets no nonce, exactly as
       before.
  N-3  A root task is unchanged: a nonce, and no SWARM_CHILD_PATH.
  N-4  A deployment without the child key issues no nonce to anyone.
  N-5  All of the above holds only while FORGE_BROKER is on. Off -- the
       default, and dev's setting although dev has child tasks on -- a
       user-slot child gets no nonce, exactly as before the broker, and no
       worker is handed FORGE_BROKER; on, every worker is.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402

from swarm_api.childkey import AttemptTuple, nonce  # noqa: E402
from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

from scheduler.dispatch import worker_env  # noqa: E402

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
USER_SLOT = "git-u-" + "0123456789abcdef"


def _task(
    *, parent: str | None = None, forge: str | None = None, profile_name: str = "mock"
) -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_abc", tenant_id="eng", created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=profile_name, resource_class=profile.resource_class,
        input={}, submitted_by="alice@example.com", parent_task_id=parent,
        forge_credential=forge,
    )


def _lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1", task_id=task.id, attempt_id="att_1", tenant_id="eng",
        generation=2, pools=["global"], units=1, state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
    )


def _keyed(*, forge_broker: bool = True):
    return scheduler_settings(
        child_key="k", swarm_api_url="https://api.example", swarm_api_audience="aud",
        forge_broker=forge_broker,
    )


def _env(task: Task, settings) -> dict[str, str]:
    return worker_env(task=task, lease=_lease(task), tenant=_tenant(), settings=settings)


def test_a_child_with_a_user_slot_gets_a_nonce_and_no_child_path():
    env = _env(_task(parent="task_parent", forge=USER_SLOT), _keyed())
    assert env["SWARM_CHILD_NONCE"] == nonce(
        "k", AttemptTuple("eng", "task_abc", "att_1", "lease_1", 2)
    )
    assert env["SWARM_API_URL"] == "https://api.example"
    assert env["SWARM_API_AUDIENCE"] == "aud"
    assert env["SWARM_CHILD_PATH"] == "false"
    # Only the secret's NAME is a task field; the key never leaves the scheduler.
    assert "k" not in {v for name, v in env.items() if name != "SWARM_CHILD_NONCE"}


def test_a_child_with_the_tenant_token_gets_no_nonce():
    for forge in ("git", None, "git-r-" + "0" * 16):
        env = _env(_task(parent="task_parent", forge=forge), _keyed())
        assert "SWARM_CHILD_NONCE" not in env, forge
        assert "SWARM_API_URL" not in env, forge
        assert "SWARM_CHILD_PATH" not in env, forge


def test_a_root_task_is_unchanged():
    for forge in (USER_SLOT, "git", None):
        env = _env(_task(forge=forge), _keyed())
        assert env["SWARM_CHILD_NONCE"] == nonce(
            "k", AttemptTuple("eng", "task_abc", "att_1", "lease_1", 2)
        ), forge
        assert env["SWARM_API_URL"] == "https://api.example"
        # A root's child path is the worker's default; nothing is written for it.
        assert "SWARM_CHILD_PATH" not in env, forge


def test_no_deployment_child_key_means_no_nonce():
    unkeyed = scheduler_settings(swarm_api_url="https://api.example")
    for task in (_task(parent="task_parent", forge=USER_SLOT), _task(forge=USER_SLOT)):
        env = _env(task, unkeyed)
        assert "SWARM_CHILD_NONCE" not in env
        assert "SWARM_API_URL" not in env
        assert "SWARM_CHILD_PATH" not in env
    # Nor without the API's address: a nonce with nowhere to spend it.
    no_api = scheduler_settings(child_key="k")
    env = _env(_task(parent="task_parent", forge=USER_SLOT), no_api)
    assert "SWARM_CHILD_NONCE" not in env


def test_a_child_keyed_for_git_tools_keeps_its_child_path_on_a_user_slot():
    """A child that has a key for another reason is not told SWARM_CHILD_PATH=false.

    The git tools, a workspace secret and an MCP attach are all signed with the
    key, and a worker with the child path off marks its key unavailable to them
    (`ChildPath.available`). The flag belongs only to a child the broker alone
    keyed; swarm-api's depth limit is the guard either way.
    """
    env = _env(_task(parent="task_parent", forge=USER_SLOT, profile_name="claude-code"), _keyed())
    assert "SWARM_CHILD_NONCE" in env
    assert "SWARM_CHILD_PATH" not in env


def test_with_the_broker_off_dispatch_is_what_it_was_before_it():
    """Dev has child tasks on, so its scheduler holds the key; the broker must
    not go live there with them. Off, a user-slot child is keyed as before
    #1041 -- not at all -- and no worker is told FORGE_BROKER."""
    off = _keyed(forge_broker=False)
    child = _env(_task(parent="task_parent", forge=USER_SLOT), off)
    assert "SWARM_CHILD_NONCE" not in child
    assert "SWARM_API_URL" not in child
    assert "SWARM_CHILD_PATH" not in child
    assert "FORGE_BROKER" not in child
    root = _env(_task(forge=USER_SLOT), off)
    assert "SWARM_CHILD_NONCE" in root, "a root's child path does not depend on the broker"
    assert "FORGE_BROKER" not in root
    assert scheduler_settings().forge_broker is False, "the switch ships off"


def test_with_the_broker_on_every_worker_is_told():
    for task in (_task(forge=USER_SLOT), _task(parent="task_parent", forge=USER_SLOT),
                 _task(forge="git")):
        assert _env(task, _keyed())["FORGE_BROKER"] == "true"


def test_the_scheduler_reads_forge_broker_from_its_environment(monkeypatch):
    from scheduler.settings import SchedulerSettings

    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.delenv("FORGE_BROKER", raising=False)
    assert SchedulerSettings.from_env().forge_broker is False
    monkeypatch.setenv("FORGE_BROKER", "true")
    assert SchedulerSettings.from_env().forge_broker is True

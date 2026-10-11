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


def _task(*, parent: str | None = None, forge: str | None = None) -> Task:
    profile = RUNNER_PROFILES["mock"]
    return Task(
        id="task_abc", tenant_id="eng", created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile="mock", resource_class=profile.resource_class,
        input={}, submitted_by="alice@saga.xyz", parent_task_id=parent,
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
        tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=NOW,
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
    )


def _keyed():
    return scheduler_settings(
        child_key="k", swarm_api_url="https://api.example", swarm_api_audience="aud"
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

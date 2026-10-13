"""Lane G2 of docs/runner-git-tools.md (§1.4, "Every attempt needs a key").

The git tools are signed with the attempt key, the only thing that proves "the
worker, not the agent". Until this lane the scheduler issued the key's
registration nonce only to a task that is not itself a child, because the key
existed only for the child path and a child may not have children. A child
task is exactly what a fan-out lane runs as, and it needs the git tools too.

So: every attempt of a task whose profile offers git tools gets the nonce,
children included; its key registers; and having a key still gives a child no
way to submit children (`child_depth_exceeded`, depth 1, is unchanged). A child
whose profile is not offered git tools still gets no nonce -- the control,
without which "a child gets a nonce" would pass by handing one to everything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from swarm_api.childkey import CHILD_KEYS, AttemptTuple, nonce

from scheduler.dispatch import worker_env

from .conftest import PROJECT, scheduler_settings, seed_tenant
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    Attempt,
    _client,
    register,
    seed_parent,
    submit,
    worker_tokens,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CHILD = "task_child"
PARENT = "task_its_parent"


def _task(profile: str, *, parent: str | None = PARENT) -> Task:
    return Task(
        id=CHILD, tenant_id="eng", created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=profile,
        resource_class=RUNNER_PROFILES[profile].resource_class,
        input={}, submitted_by="alice@saga.xyz", parent_task_id=parent,
    )


def _lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_child_1", task_id=task.id, attempt_id="att_child_1", tenant_id="eng",
        generation=1, pools=["global"], units=1, state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=NOW,
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
    )


def _env(task: Task, **settings) -> dict[str, str]:
    keyed = scheduler_settings(
        **{"child_key": KEY, "swarm_api_url": "https://api.example", **settings}
    )
    return worker_env(task=task, lease=_lease(task), tenant=_tenant(), settings=keyed)


#: The runners lane G6 names the `swarm-git` shim in (claude-code's
#: `--mcp-config`, codex's MCP configuration). A profile is offered git tools
#: when its runner is one of them: a worker action (merge, post-verdict) runs
#: no agent, and the mock and browser runners have nowhere to name the shim.
#: Read from the frozen catalogue here, independently of the scheduler, so a
#: scheduler that offered them to everything would fail the control below.
AGENT_RUNNERS = {"agent_worker.runners.claude_code", "agent_worker.runners.codex"}


def _offered(name: str) -> bool:
    profile = RUNNER_PROFILES[name]
    return profile.worker_action is None and any(
        part in AGENT_RUNNERS for part in profile.runner_argv
    )


GIT_TOOL_PROFILES = sorted(name for name in RUNNER_PROFILES if _offered(name))
OTHER_PROFILES = sorted(name for name in RUNNER_PROFILES if not _offered(name))


def test_both_sides_of_the_rule_are_populated():
    """Without this an empty parametrisation would pass both tests below vacuously."""
    assert {"claude-code", "codex"} <= set(GIT_TOOL_PROFILES)
    assert {"mock", "merge", "post-verdict", "browser"} <= set(OTHER_PROFILES)


@pytest.mark.parametrize("profile", GIT_TOOL_PROFILES)
def test_a_child_offered_git_tools_gets_a_nonce_bound_to_its_own_attempt(profile):
    """Today absent: the nonce block refused every task with a parent."""
    env = _env(_task(profile))
    assert env.get("SWARM_CHILD_NONCE") == nonce(
        KEY, AttemptTuple("eng", CHILD, "att_child_1", "lease_child_1", 1)
    ), f"a {profile} child has no attempt-key nonce, so it can never sign a git tool call"
    # The nonce travels with the API it is spent at, as it does for a parent.
    assert env["SWARM_API_URL"] == "https://api.example"


@pytest.mark.parametrize("profile", OTHER_PROFILES)
def test_a_child_not_offered_git_tools_gets_no_nonce(profile):
    """The control: the child path's depth rule still holds for everything else."""
    env = _env(_task(profile))
    assert "SWARM_CHILD_NONCE" not in env and "SWARM_API_URL" not in env


@pytest.mark.parametrize("profile", ["mock", "claude-code"])
def test_a_parent_still_gets_its_nonce_whatever_its_profile(profile):
    """A task that is not a child keeps the child path exactly as before."""
    assert "SWARM_CHILD_NONCE" in _env(_task(profile, parent=None))


def test_an_unkeyed_or_api_less_deployment_gives_a_child_nothing():
    """A nonce with nowhere to spend it would sit unspent where the agent reads it."""
    assert "SWARM_CHILD_NONCE" not in _env(_task("claude-code"), child_key="")
    assert "SWARM_CHILD_NONCE" not in _env(_task("claude-code"), swarm_api_url="")


def test_a_childs_key_registers_with_the_nonce_its_dispatch_carried(
    db, worker_tokens, group_map, objects  # noqa: F811
):
    """End to end across the seam: the scheduler's nonce for a child is one swarm-api spends.

    The registration is depth-agnostic on purpose; only `submit` is depth-refused.
    """
    client = _client(db, worker_tokens, group_map, objects)
    seed_tenant(db, "eng")
    task = _task("claude-code")
    lease = _lease(task)
    env = worker_env(
        task=task, lease=lease, tenant=_tenant(),
        settings=scheduler_settings(child_key=KEY, swarm_api_url="https://api.example"),
    )
    attempt = Attempt(task=CHILD, attempt=lease.attempt_id, lease=lease.lease_id, generation=1)
    seed_parent(db, attempt, state="STARTING", parent_task_id=PARENT, runner_profile="claude-code")
    t = attempt.tuple
    response = register(
        client, db, attempt,
        body={
            "tenant_id": t.tenant_id, "task_id": t.task_id, "lease_id": t.lease_id,
            "generation": t.generation, "nonce": env.get("SWARM_CHILD_NONCE", "absent"),
            "public_key": attempt.public,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["registered"] == "key"
    stored = [d for p, d in db.docs.items() if p.startswith(f"{CHILD_KEYS}/")]
    assert [d["task_id"] for d in stored] == [CHILD]
    assert stored[0]["public_key"] == attempt.public


def test_a_child_with_a_registered_key_still_may_not_submit_children(
    db, worker_tokens, group_map, objects  # noqa: F811
):
    """A key proves the worker; it does not lift the depth limit."""
    client = _client(db, worker_tokens, group_map, objects)
    seed_tenant(db, "eng")
    attempt = Attempt(task=CHILD, attempt="att_child_1", lease="lease_child_1")
    doc = seed_parent(db, attempt, state="STARTING", parent_task_id=PARENT, runner_profile="claude-code")
    assert register(client, db, attempt).status_code == 201
    doc["state"] = "RUNNING"
    response = submit(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "child_depth_exceeded"
    assert not [p for p, d in db.docs.items() if d.get("parent_task_id") == CHILD]

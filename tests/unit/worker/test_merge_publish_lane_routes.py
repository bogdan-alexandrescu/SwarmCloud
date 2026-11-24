"""A merge step follows a person's `publish_to_pr` lane read through swarm-api's REAL state routes.

fr-5fdbfbf85bba (2026-11-23): four merge steps on a state-routes tenant
refused `head_moved`. Each pull request's head was a one-step `fix` lane a
person submitted with `publish_to_pr`, which fast-forwarded one commit onto
the parked pull request. `merge._person_lane` already takes such a lane as
the person's push (archived issue 2505) -- but it reads the lane through
`GET state/upstream`, which served a merge step only the tasks whose SIGNED
`continues` names the pull request's root, so the read failed and the lane
was "not a person's plain lane on this branch".

Here the worker's own state client (`state_routes_harness`) reads every
upstream through swarm-api's app, over the control-plane suite's in-memory
Firestore, with every task's spec signed as submission signs it. The forge
is `merge_world`'s fake GitHub. So the test fails if either side refuses.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any

import pytest
import spec_keys

import merge_world
from agent_worker import inputs, merge
from agent_worker.statecall import StateCall
from control_plane.conftest import seed_task
from control_plane.fakes import FakeFirestore as ApiFirestore
from merge_world import PINNED, MergeWorld
from state_routes_harness import (
    RoutesOpener,
    api_client,
    remote_control,
    seed_attempt,
)

#: The person whose pull request it is; the lane's worker publishes as them.
PAT = {"name": "Pat Person", "email": "pat@example.com"}
PAT_LOGIN = "pat-person"
LANE = "task_59dfcb5600000000000a"
LANE_WORKFLOW = "wf_2c0fe75500000000000a"
LANE_HEAD = "0fdbc8d8" + "1" * 32


@pytest.fixture(autouse=True)
def no_firestore_client(monkeypatch):
    """swarm-api's context builds no Firestore client of its own here."""
    import google.cloud.firestore as gcf

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a Firestore client was built in a unit test")

    monkeypatch.setattr(gcf, "Client", refuse)


def _signed(db: ApiFirestore, task_id: str, *, tenant: str = merge_world.TENANT,
            workflow: str | None = merge_world.WORKFLOW,
            repository_url: str | None = merge_world.REPO_URL,
            **fields: Any) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state="SUCCEEDED",
                    workflow_id=workflow)
    doc.update({"repository_url": repository_url, **fields})
    return spec_keys.sign_document(doc, task_id)


def _lane(db: ApiFirestore, *, root: str = merge_world.OPENER,
          tenant: str = merge_world.TENANT,
          repository_url: str = merge_world.REPO_URL) -> dict[str, Any]:
    """The person's one-step `fix` lane, signed as `publishtopr` signs it."""
    return _signed(db, LANE, tenant=tenant, workflow=LANE_WORKFLOW,
                   repository_url=repository_url, runner_profile="claude-code",
                   metadata={"dispatch": {"strategy": "direct-pr", "publish_to_pr": {
                       "number": merge_world.NUMBER, "task_id": root}}})


def _routed(tmp_path: Path, db: ApiFirestore) -> tuple[MergeWorld, Any]:
    """`merge_world`'s pull request, its head the lane's commit, and a context
    whose every upstream read goes through `GET state/upstream`."""
    world = MergeWorld(tmp_path)
    world.pr["user"] = {"login": PAT_LOGIN, "type": "User"}
    world.pr["assignees"] = []
    message = ("Apply the owner's pick\n\nMade by the worker for lane fix.\n\n"
               f"{merge.WORKER_COMMIT_LINE}{LANE}.")
    world.github.route("GET", f"{merge_world.API}/commits/{LANE_HEAD}", (200, {}, {
        "sha": LANE_HEAD, "parents": [{"sha": PINNED}],
        "commit": {"committer": dict(PAT), "author": dict(PAT), "message": message,
                   "verification": {"verified": False}},
        "committer": {"login": PAT_LOGIN, "type": "User"},
        "author": {"login": PAT_LOGIN, "type": "User"},
    }))
    world.serve_checks(LANE_HEAD)
    world.pr["head"]["sha"] = LANE_HEAD

    # The merge step's own attempt, signed: its target names the opener.
    seed_attempt(db, task_id=merge_world.TASK, state="STARTING",
                 workflow_id=merge_world.WORKFLOW, repository_url=merge_world.REPO_URL,
                 depends_on=[merge_world.REVIEW],
                 metadata={"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                        "merge_target": dict(world.target)}})
    for task_id in (merge_world.OPENER, merge_world.REVIEW):
        stored = copy.deepcopy(world.docs[task_id])
        _signed(db, task_id, metadata=stored.get("metadata") or {},
                result_summary=stored.get("result_summary"))
    control = remote_control(RoutesOpener(api_client(db)), task_id=merge_world.TASK)
    control.validate_generation()
    state = control._calls
    assert isinstance(state, StateCall) and state.opened

    def fetch(task_id: str) -> dict[str, Any]:
        return inputs.fetch_upstream_task(state, upstream_task_id=task_id,
                                          tenant_id=merge_world.TENANT)

    return world, dataclasses.replace(world.context(), fetch_upstream=fetch)


def test_a_persons_publish_to_pr_lane_read_through_the_routes_is_their_push(tmp_path):
    db = ApiFirestore()
    world, ctx = _routed(tmp_path, db)
    _lane(db)
    outcome = merge.run_merge(ctx)
    refusal = outcome.summary["refusal"]
    # MUTATION: serve a merge step no `publish_to_pr` lane (the state routes
    # before fr-5fdbfbf85bba) and this is `head_moved`, "not a person's
    # plain lane on this branch".
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH, refusal
    assert refusal["person"] == PAT_LOGIN
    assert refusal["commit"]["worker_task"] == LANE
    assert world.merge_calls() == []


def test_a_lane_the_routes_refuse_still_ends_head_moved(tmp_path):
    """The controls: a lane onto another pull request, of another tenant, of
    another repository, and one whose `publish_to_pr` was rewritten after
    signing are each unread, so the head stays `head_moved`."""
    cases = {
        "other_pull_request": dict(root="task_elsewhere"),
        "other_tenant": dict(tenant="research"),
        "other_repository": dict(repository_url="https://github.com/acme/gadgets.git"),
    }
    for name, kwargs in [*cases.items(), ("forged", {})]:
        db = ApiFirestore()
        world, ctx = _routed(tmp_path / name, db)
        if name == "forged":
            forged = _lane(db, root="task_elsewhere")
            forged["metadata"]["dispatch"]["publish_to_pr"]["task_id"] = merge_world.OPENER
        else:
            _lane(db, **kwargs)
        outcome = merge.run_merge(ctx)
        assert outcome.summary["refusal"]["code"] == "head_moved", name
        assert world.merge_calls() == [], name

"""A worker-action profile starts NO runner child (contract request 33, #295).

`merge` and `post-verdict` name a `WorkerAction` in the frozen catalogue and
carry no runner_argv, so the lifecycle performs the action itself: no agent
ever runs under the Job identity that reads the App key (docs/merge-step.md
§0, §1.3). The profile's kind is read from `swarm_common` -- this file holds
that the catalogue, not a list in the worker, decides.

Every worker here is the production worker over in-memory Firestore; the
action itself is replaced by a recorder, because what is under test is the
lifecycle's branch and how it ends the task from the action's outcome. The
actions are tested on their own in test_merge_action.py and
test_post_verdict_action.py.

MUTATIONS: drop the `worker_action` branch in `_prepare` -- `_runner_argv`
raises on the empty argv and `ChildProcess` explodes. Run the action before
`_verify_spec` -- the unsigned test reaches the recorder. Map a refusal to
RUNNER_ERROR -- the refusal test reads the wrong cause.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker import merge as merge_mod
from agent_worker import post_verdict as post_verdict_mod
from agent_worker.errors import ExitCode
from agent_worker.secrets import CredentialMissing
from swarm_common.models import EndCause
from swarm_common.profiles import RUNNER_PROFILES, WorkerAction
from swarm_common.states import EventType, TaskState

from conftest import seed_attempt
from fakes import ExplodingChildProcess

MERGES = {"author": "task_a", "review": "task_r", "post-verdict": "task_pv", "proof": "task_p"}


def _seed(db, profile: str) -> None:
    seed_attempt(db, runner_profile=profile, task_input={"prompt": "act"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    block: dict[str, Any] = {"strategy": "single-pr", "carrier": "checkpoints", "pr_role": "none"}
    if profile == "merge":
        block["merges"] = dict(MERGES)
    else:
        block["verdict_source"] = {"review": "task_r"}
    doc["metadata"] = {"dispatch": block}


@pytest.fixture
def no_runner(monkeypatch):
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    monkeypatch.setattr(lifecycle, "_runner_argv",
                        lambda _cfg: pytest.fail("a worker action asked for a runner argv"))


def _recorder(monkeypatch, outcome_for):
    seen: list[post_verdict_mod.ActionContext] = []

    def run(ctx):
        seen.append(ctx)
        return outcome_for(ctx)

    monkeypatch.setattr(merge_mod, "run_merge", run)
    monkeypatch.setattr(post_verdict_mod, "run_post_verdict", run)
    return seen


def _succeeded(ctx):
    return post_verdict_mod.ActionOutcome(
        state=TaskState.SUCCEEDED, end_cause=None, exit_code=ExitCode.OK,
        summary={"action": "x", "merged_by_this_task": True},
    )


def test_the_catalogue_names_the_worker_actions():
    """What the branch reads: the frozen profile's kind, not a worker list."""
    assert RUNNER_PROFILES["merge"].worker_action is WorkerAction.MERGE
    assert RUNNER_PROFILES["post-verdict"].worker_action is WorkerAction.POST_VERDICT
    assert RUNNER_PROFILES["merge"].runner_argv == ()
    assert RUNNER_PROFILES["claude-code-review"].worker_action is None


@pytest.mark.parametrize(("profile", "key"), [("merge", "merge"), ("post-verdict", "verdict")])
def test_a_worker_action_starts_no_runner_and_succeeds_from_its_outcome(
    db, worker_factory, monkeypatch, no_runner, profile, key
):
    _seed(db, profile)
    seen = _recorder(monkeypatch, _succeeded)
    worker, _, _ = worker_factory(runner_profile=profile)

    assert worker.run() == ExitCode.OK
    assert len(seen) == 1, "the action ran other than once"
    ctx = seen[0]
    assert ctx.workflow_id == "wf_1" and ctx.tenant_id == "eng"
    assert ctx.dispatch.get("pr_role") == "none"
    assert ctx.human_gate is False
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["result_summary"][key]["merged_by_this_task"] is True
    assert task["end_cause"] is None
    # No runner input was written, and no runner credential was read.
    assert "runner" not in task["result_summary"]


def test_a_merge_dispatched_to_the_worker_action_runs_the_merge_not_the_post(
    db, worker_factory, monkeypatch, no_runner
):
    _seed(db, "merge")
    which: list[str] = []
    monkeypatch.setattr(merge_mod, "run_merge", lambda ctx: which.append("merge") or _succeeded(ctx))
    monkeypatch.setattr(post_verdict_mod, "run_post_verdict",
                        lambda ctx: which.append("post") or _succeeded(ctx))
    worker, _, _ = worker_factory(runner_profile="merge")
    assert worker.run() == ExitCode.OK
    assert which == ["merge"]


def test_an_unsigned_worker_action_never_reaches_the_action(
    db, worker_factory, monkeypatch, no_runner
):
    _seed(db, "merge")
    seen = _recorder(monkeypatch, _succeeded)
    worker, _, _ = worker_factory(runner_profile="merge", sign_spec=False)
    worker.run()
    assert seen == []
    assert db.doc("tasks/task_1")["end_cause"] == EndCause.SPEC_SIGNATURE_INVALID.value


def test_a_refusal_ends_the_task_with_the_actions_end_cause_and_is_not_retried(
    db, worker_factory, monkeypatch, no_runner
):
    _seed(db, "merge")
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.refusal(
        {"action": "merge"}, EndCause.MERGE_REFUSED, "checks_pending", "ci-gate is queued"))
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, "a refusal was retried"
    assert task["end_cause"] == EndCause.MERGE_REFUSED.value
    assert task["result_summary"]["merge"]["refusal"]["code"] == "checks_pending"
    assert task["last_error"].startswith("checks_pending")


def test_a_forge_outage_fails_the_attempt_retryably(db, worker_factory, monkeypatch, no_runner):
    _seed(db, "post-verdict")
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.unavailable(
        {"action": "post_verdict"}, EndCause.VERDICT_FAILED, "502", retry_after=30))
    worker, _, _ = worker_factory(runner_profile="post-verdict")

    worker.run()
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, "an outage ended the task"
    assert EventType.RETRYING.value in db.event_types("task_1")


def test_an_unregistered_app_parks_the_step_at_no_cost(db, worker_factory, monkeypatch, no_runner):
    _seed(db, "merge")
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.ActionOutcome(
        state=TaskState.PARKED, end_cause=None, exit_code=ExitCode.PARKED, summary={},
        credential_missing=CredentialMissing("eng", "git-merge")))
    worker, _, _ = worker_factory(runner_profile="merge")
    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value


def test_the_action_reads_its_own_app_secret_and_nothing_else(
    db, worker_factory, monkeypatch, no_runner
):
    """`read_app_key` reads `swarm-tenant-<t>-git-merge` for the merge profile,
    parks when the tenant has not registered it, and reads no runner key."""
    from fake_github import app_secret_payload
    from fakes import FakeSecretClient

    _seed(db, "merge")
    keys: list[Any] = []

    def read(ctx):
        try:
            keys.append(ctx.read_app_key())
        except CredentialMissing as exc:
            keys.append(exc)
        return _succeeded(ctx)

    _recorder(monkeypatch, read)
    secrets = FakeSecretClient({"swarm-tenant-eng-git-merge": app_secret_payload(77)})
    db.doc("tenants/eng")["credentials"] = ["git-merge"]
    worker, _, _ = worker_factory(runner_profile="merge", secret_client=secrets)
    db.doc("tenants/eng")["credentials"] = ["git-merge"]
    worker.run()
    assert secrets.accessed == ["swarm-tenant-eng-git-merge"]
    assert keys and getattr(keys[0], "app_id", None) == 77

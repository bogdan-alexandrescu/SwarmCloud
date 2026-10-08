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

import json
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

from worker_seeds import seed_attempt
from fakes import ExplodingChildProcess

def _seed(db, profile: str) -> None:
    seed_attempt(db, runner_profile=profile, task_input={"prompt": "act"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    if profile == "merge":
        # Contract request 47: the merge step of an `integrate` workflow,
        # naming the integrator whose pull request it merges.
        block: dict[str, Any] = {"strategy": "integrate", "carrier": "checkpoints",
                                 "merge_target": {"pull_request": "task_int"}}
    else:
        block = {"strategy": "single-pr", "carrier": "checkpoints", "pr_role": "none",
                 "verdict_source": {"review": "task_r"}}
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
    assert ctx.dispatch.get("merge_target" if profile == "merge" else "verdict_source")
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


def _ended_lines(log_stream) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("message") == "worker action ended"]


def test_a_refusal_logs_the_line_the_end_cause_metric_counts(
    db, worker_factory, monkeypatch, no_runner, log_stream
):
    """terraform/modules/monitoring metrics.tf `worker_action_ended` filters on
    this message and extracts the top-level `end_cause`."""
    _seed(db, "merge")
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.refusal(
        {"action": "merge"}, EndCause.MERGE_REFUSED, "checks_pending", "ci-gate is queued"))
    worker, _, _ = worker_factory(runner_profile="merge")
    worker.run()
    [line] = _ended_lines(log_stream)
    assert line["severity"] == "ERROR"
    assert line["end_cause"] == EndCause.MERGE_REFUSED.value
    assert line["labels"]["tenant_id"] == "eng"


def test_an_outage_on_the_last_attempt_logs_the_line_the_end_cause_metric_counts(
    db, worker_factory, monkeypatch, no_runner, log_stream
):
    """An outage that spent its retries ends VERDICT_FAILED through
    `fail_retryably`, and must reach the metric like a refusal does."""
    _seed(db, "post-verdict")
    db.doc("tasks/task_1")["attempt_count"] = db.doc("tasks/task_1")["max_attempts"]
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.unavailable(
        {"action": "post_verdict"}, EndCause.VERDICT_FAILED, "502", retry_after=30))
    worker, _, _ = worker_factory(runner_profile="post-verdict")
    worker.run()
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == EndCause.VERDICT_FAILED.value
    [line] = _ended_lines(log_stream)
    assert line["end_cause"] == EndCause.VERDICT_FAILED.value


def test_an_outage_that_will_be_retried_logs_no_end(
    db, worker_factory, monkeypatch, no_runner, log_stream
):
    _seed(db, "post-verdict")
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.unavailable(
        {"action": "post_verdict"}, EndCause.VERDICT_FAILED, "502", retry_after=30))
    worker, _, _ = worker_factory(runner_profile="post-verdict")
    worker.run()
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value
    assert _ended_lines(log_stream) == []


def test_an_unregistered_app_parks_the_step_at_no_cost(db, worker_factory, monkeypatch, no_runner):
    _seed(db, "merge")
    _recorder(monkeypatch, lambda ctx: post_verdict_mod.ActionOutcome(
        state=TaskState.PARKED, end_cause=None, exit_code=ExitCode.PARKED, summary={},
        credential_missing=CredentialMissing("eng", "git-merge")))
    worker, _, _ = worker_factory(runner_profile="merge")
    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value


def test_post_verdict_reads_its_own_app_secret_and_nothing_else(
    db, worker_factory, monkeypatch, no_runner
):
    """`read_app_key` reads `swarm-tenant-<t>-git-review` for post-verdict and
    reads no runner key."""
    from fake_github import app_secret_payload
    from fakes import FakeSecretClient

    _seed(db, "post-verdict")
    keys: list[Any] = []

    def read(ctx):
        try:
            keys.append(ctx.read_app_key())
        except CredentialMissing as exc:
            keys.append(exc)
        return _succeeded(ctx)

    _recorder(monkeypatch, read)
    secrets = FakeSecretClient({"swarm-tenant-eng-git-review": app_secret_payload(77)})
    worker, _, _ = worker_factory(runner_profile="post-verdict", secret_client=secrets)
    db.doc("tenants/eng")["credentials"] = ["git-review"]
    worker.run()
    assert secrets.accessed == ["swarm-tenant-eng-git-review"]
    assert keys and getattr(keys[0], "app_id", None) == 77


def test_the_merge_reads_the_tenants_git_token_and_no_app_key(
    db, worker_factory, monkeypatch, no_runner
):
    """Contract request 47: `read_git_token` reads `swarm-tenant-<t>-git`, the
    tenant's existing forge token, and the merge never asks for an App key."""
    from fake_github import fresh_token
    from fakes import FakeSecretClient

    _seed(db, "merge")
    value = fresh_token()
    read: list[str] = []

    def act(ctx):
        read.append(ctx.read_git_token())
        return _succeeded(ctx)

    _recorder(monkeypatch, act)
    secrets = FakeSecretClient({"swarm-tenant-eng-git": value})
    worker, _, _ = worker_factory(runner_profile="merge", secret_client=secrets)
    db.doc("tenants/eng")["credentials"] = ["git"]
    assert worker.run() == ExitCode.OK
    assert secrets.accessed == ["swarm-tenant-eng-git"]
    assert read == [value]


def test_a_merge_for_a_tenant_without_a_git_token_parks_at_no_cost(
    db, worker_factory, monkeypatch, no_runner
):
    """`read_git_token` raises CredentialMissing for a tenant with no `git`
    credential; the action's park outcome ends the step PARKED."""
    _seed(db, "merge")
    monkeypatch.setattr(merge_mod, "run_merge", _park_on_missing)
    worker, _, _ = worker_factory(runner_profile="merge")
    db.doc("tenants/eng")["credentials"] = []
    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == "CREDENTIAL_MISSING"


def _park_on_missing(ctx):
    try:
        ctx.read_git_token()
    except CredentialMissing as exc:
        return post_verdict_mod.ActionOutcome(
            state=TaskState.PARKED, end_cause=None, exit_code=ExitCode.PARKED, summary={},
            credential_missing=exc)
    raise AssertionError("a tenant with no git credential had its token read")

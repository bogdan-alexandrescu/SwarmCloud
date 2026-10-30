"""A branch that touches `.github/workflows/` is published by the broker (AU-3).

docs/design/agent-autonomy.md §3.3, the publish half, owner decision Q3: the
tenant slot `swarm-tenant-<t>-git` has no `workflow` scope, so GitHub refuses a
push that adds or changes a workflow file, and the brief recorded lanes ending
SUCCEEDED with the file unpushed. The end-of-agent publish (`_publish_git`)
therefore routes a branch whose new commits touch `.github/workflows/**` or
`.github/actions/**` through swarm-api's `git_push_branch` -- the broker that
alone pushes those files, with a GitHub App token it mints per push and only
for a registration whose `workflow_files` switch is on (#2029, WF-APP-TOKEN) --
instead of the worker's own token push. A
refusal (`workflow_scope_missing`, a fence, no broker at all) publishes
nothing, sets `PUBLISH_FAILED_FIELD`, and leaves the workflow change as
`workflow-lines.patch` in the attempt's artifacts, so `_publish_failed` ends
the step FAILED, never SUCCEEDED.

Real git against a local bare remote, as in `test_strategy_end_to_end.py`;
only the forge's HTTP and the broker's signed call are faked. The fake broker
applies the bundle it is handed to the remote, so a branch on the remote
proves the bundle was whole.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import gittools, lifecycle

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

DIRECT = {"strategy": "direct-pr", "carrier": "checkpoints"}
WORKFLOW = ".github/workflows/ci.yml"
ACTION = ".github/actions/setup/action.yml"


def adds_a_workflow(repo: Path) -> None:
    """An agent that changed ordinary code and a workflow, uncommitted."""
    (repo / "agent.txt").write_text("work the agent never committed\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / WORKFLOW).write_text("name: ci\non: [push]\njobs: {}\n")


def adds_an_action(repo: Path) -> None:
    (repo / ".github" / "actions" / "setup").mkdir(parents=True)
    (repo / ACTION).write_text("name: setup\nruns:\n  using: composite\n  steps: []\n")


def plain_change(repo: Path) -> None:
    (repo / "agent.txt").write_text("work the agent never committed\n")
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text("* @acme/owners\n")


class FakeBroker:
    """swarm-api's `git_push_branch`, standing in for `GitTools.call`.

    `refuse` raises what the relay raises for a refusal, on every call;
    `blips` are raised one per call, in order, before the push is made (#2138).
    Otherwise the bundle is fetched into the bare remote at the branch it
    names, as swarm-api's `push_bundle` would push it."""

    def __init__(self, remote: Path, tmp: Path, *, refuse: Exception | None = None,
                 blips: list[Exception] | None = None) -> None:
        self.remote = remote
        self.tmp = tmp
        self.refuse = refuse
        self.blips = list(blips or [])
        self.calls: list[tuple[str, dict, str]] = []
        self.cfg = type("Cfg", (), {"inline_bundle_bytes": gittools.INLINE_BUNDLE_BYTES})()

    def call(self, tool: str, fields: dict, *, request_id: str) -> dict:
        self.calls.append((tool, dict(fields), request_id))
        if self.refuse is not None:
            raise self.refuse
        if self.blips:
            raise self.blips.pop(0)
        raw = base64.b64decode(fields["bundle"])
        assert hashlib.sha256(raw).hexdigest() == fields["bundle_sha256"]
        bundle = self.tmp / "broker.bundle"
        bundle.write_bytes(raw)
        heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], cwd=str(self.remote),
                               check=True, capture_output=True, text=True).stdout.split()
        subprocess.run(["git", "fetch", "--quiet", str(bundle),
                        f"{heads[1]}:refs/heads/{fields['branch']}"],
                       cwd=str(self.remote), check=True, capture_output=True)
        return {"branch": fields["branch"], "old_sha": None, "new_sha": fields["new_sha"],
                "rewrite": False}


@pytest.fixture
def own_pushes(monkeypatch) -> list[str]:
    """Every push the worker made with the tenant token (`gitops.push_branch`)."""
    seen: list[str] = []
    real = lifecycle.push_branch

    def recording(**kwargs):
        seen.append(kwargs["branch"])
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", recording)
    return seen


def _with_broker(monkeypatch, broker):
    """Offer `broker` as the attempt's git tools relay."""
    monkeypatch.setattr(lifecycle.Worker, "_workflow_broker", lambda self: broker,
                        raising=False)


def test_a_workflow_branch_is_pushed_by_the_broker_never_the_worker(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    _, config, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-wf",
                                 dispatch=DIRECT, edit=adds_a_workflow)
    branch = f"{config.git_branch_prefix}{config.task_id}"

    # MUTATION: drop the workflow route in `_publish_git` -> the worker's own push runs.
    assert own_pushes == [], "the tenant token pushed a workflow-touching branch"
    # MUTATION: call the broker with another tool name -> this reads "git_fetch" or nothing.
    assert [c[0] for c in broker.calls] == ["git_push_branch"]
    fields = broker.calls[0][1]
    # MUTATION: send the agent's clone HEAD instead of the publish repo's -> the sha differs.
    assert fields["branch"] == branch and fields["new_sha"] == refs(origin)[branch]
    # MUTATION: bundle without the worker's auto-commit -> the workflow is missing on the remote.
    assert {WORKFLOW, "agent.txt"} <= tree_at(origin, branch)
    # MUTATION: leave `published` unset on the broker path -> no pull request is owed.
    assert out["published"] is True and out.get(lifecycle.PUBLISH_FAILED_FIELD) is None
    # MUTATION: name nothing -> a reader cannot tell the broker pushed it.
    assert out["workflow_paths"] == [WORKFLOW] and out["workflow_publish"] == "broker"
    assert len(forge.pulls) == 1 and forge.pulls[0]["head"] == branch


def test_an_action_file_takes_the_broker_route_too(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    run_attempt(worker_factory, monkeypatch, origin, task_id="t-action",
                dispatch=DIRECT, edit=adds_an_action)
    # MUTATION: read only `.github/workflows` -> an action change goes out on the tenant token.
    assert own_pushes == [] and len(broker.calls) == 1


def test_a_branch_without_workflow_files_keeps_the_workers_own_push(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    _, config, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-plain",
                                 dispatch=DIRECT, edit=plain_change)
    # MUTATION: route every publish through the broker -> it is called here.
    assert broker.calls == []
    # MUTATION: treat all of `.github/` as workflow files -> the own push is skipped.
    assert own_pushes == [f"{config.git_branch_prefix}{config.task_id}"]
    assert out["published"] is True and "workflow_paths" not in out


def _refused_attempt(worker_factory, monkeypatch, origin, broker, task_id):
    _with_broker(monkeypatch, broker)
    return run_attempt(worker_factory, monkeypatch, origin, task_id=task_id,
                       dispatch=DIRECT, edit=adds_a_workflow)


def _assert_failed_with_patch(worker, config, out, origin, forge, store, own_pushes, tmp_path):
    branch = f"{config.git_branch_prefix}{config.task_id}"
    # MUTATION: fall back to the worker's own push on a refusal -> the branch lands.
    assert own_pushes == [] and branch not in refs(origin)
    # MUTATION: return without PUBLISH_FAILED_FIELD -> the step reads SUCCEEDED.
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True and out["published"] is False
    assert forge.pulls == []
    patch = worker.ws.artifacts / "workflow-lines.patch"
    # MUTATION: skip writing the patch -> the file is absent.
    assert patch.is_file()
    text = patch.read_text()
    # MUTATION: diff the whole branch -> agent.txt is in the patch too.
    assert f"+++ b/{WORKFLOW}" in text and "agent.txt" not in text
    # MUTATION: write the patch but not upload it -> no object at the artifact key.
    key = f"{config.artifact_prefix}/workflow-lines.patch"
    assert out["workflow_patch"]["uri"] == store.uri(key)
    assert (store.root / key).read_text() == text
    # MUTATION: leave the patch out of the reason -> `_publish_failed` cannot name it.
    assert "workflow-lines.patch" in out["publish_reason"]
    checkout = tmp_path / "apply-check"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(checkout)], check=True,
                   capture_output=True)
    # MUTATION: write the diff against the wrong base -> it does not apply to main.
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=str(checkout), check=True,
                   capture_output=True)


def test_a_refused_workflow_push_fails_the_publish_and_leaves_the_patch(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    refusal = gittools.GitToolRefused(
        "workflow_scope_missing", "the push touches .github/workflows/ci.yml",
        detail={"paths": [WORKFLOW]})
    broker = FakeBroker(origin, tmp_path, refuse=refusal)
    worker, config, out = _refused_attempt(worker_factory, monkeypatch, origin, broker, "t-refused")
    _assert_failed_with_patch(worker, config, out, origin, forge, store, own_pushes, tmp_path)
    # MUTATION: drop the broker's code -> the reason says nothing of why.
    assert "workflow_scope_missing" in out["publish_reason"]
    assert out["workflow_refused"]["code"] == "workflow_scope_missing"


def test_a_fenced_broker_call_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    fenced = gittools.GitToolFenced(409, gittools.GIT_FENCED, "superseded", retryable=False)
    broker = FakeBroker(origin, tmp_path, refuse=fenced)
    worker, config, out = _refused_attempt(worker_factory, monkeypatch, origin, broker, "t-fenced")
    _assert_failed_with_patch(worker, config, out, origin, forge, store, own_pushes, tmp_path)
    # MUTATION: catch only GitToolRefused -> the fence escapes as an unexpected publish error.
    assert out["workflow_refused"]["code"] == "fenced"


def test_without_a_broker_the_tenant_token_never_pushes_a_workflow(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    worker, config, out = _refused_attempt(worker_factory, monkeypatch, origin, None, "t-nobroker")
    _assert_failed_with_patch(worker, config, out, origin, forge, store, own_pushes, tmp_path)
    # MUTATION: treat "no broker" as "use the worker's push" -> the branch lands.
    assert out["workflow_refused"]["code"] == "git_tools_unavailable"


def test_a_refused_workflow_publish_never_ends_succeeded(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    refusal = gittools.GitToolRefused(
        "workflow_scope_missing", "the registration does not have workflow_files switched on")
    broker = FakeBroker(origin, tmp_path, refuse=refusal)
    worker, _, out = _refused_attempt(worker_factory, monkeypatch, origin, broker, "t-never")
    why = worker._publish_failed({"git": out, "artifacts": []})
    # MUTATION: return the refusal with `published: True` -> `_publish_failed` answers None.
    assert why is not None
    assert "workflow-lines.patch" in why and "workflow_scope_missing" in why


# -- a retryable refusal is asked again, bounded (#2138) ---------------------


def _unanswered() -> gittools.GitToolRefused:
    """What `GitTools.call` raises for every retryable ChildApiError."""
    return gittools.GitToolRefused(
        "api_unavailable", "the platform did not answer; ask again", retryable=True)


def _sleeping_into(worker_factory, slept: list[float]):
    """`worker_factory`, its workers sleeping into `slept` instead of the clock."""
    def factory(**kwargs):
        worker, config, rest = worker_factory(**kwargs)
        worker.forge_sleep = slept.append
        return worker, config, rest
    return factory


def test_a_retryable_broker_refusal_is_retried_with_the_same_request_id(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    # task_b7b69bce7da247e0afaf: one swarm-api blip failed a finished 42-minute build.
    broker = FakeBroker(origin, tmp_path, blips=[_unanswered()])
    _with_broker(monkeypatch, broker)
    slept: list[float] = []
    _, config, out = run_attempt(_sleeping_into(worker_factory, slept), monkeypatch, origin,
                                 task_id="t-blip", dispatch=DIRECT, edit=adds_a_workflow)
    branch = f"{config.git_branch_prefix}{config.task_id}"
    # MUTATION: return the retryable refusal as final -> one call, publish_failed.
    assert len(broker.calls) == 2
    (_, first, first_id), (_, second, second_id) = broker.calls
    # MUTATION: mint a fresh request_id per try -> the broker sees two logical calls.
    assert first_id == second_id
    # MUTATION: rebuild the bundle or re-upload per try -> the fields differ.
    assert first == second
    assert out["published"] is True and out.get(lifecycle.PUBLISH_FAILED_FIELD) is None
    assert out["workflow_publish"] == "broker"
    assert refs(origin)[branch] == second["new_sha"]
    # MUTATION: call time.sleep directly -> nothing is recorded here.
    assert len(slept) == 1 and slept[0] > 0
    assert own_pushes == []


def test_a_broker_that_never_answers_is_tried_within_the_bound_then_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    broker = FakeBroker(origin, tmp_path, refuse=_unanswered())
    _with_broker(monkeypatch, broker)
    slept: list[float] = []
    worker, config, out = run_attempt(_sleeping_into(worker_factory, slept), monkeypatch,
                                      origin, task_id="t-silent", dispatch=DIRECT,
                                      edit=adds_a_workflow)
    _assert_failed_with_patch(worker, config, out, origin, forge, store, own_pushes, tmp_path)
    assert out["workflow_refused"]["code"] == "api_unavailable"
    # MUTATION: retry without a count -> the call loops past forge_read_attempts.
    assert 1 < len(broker.calls) <= config.forge_read_attempts
    assert len({call[2] for call in broker.calls}) == 1
    # MUTATION: a fixed wait -> the waits do not grow.
    assert slept == sorted(slept) and len(set(slept)) == len(slept)
    budget = min(config.max_in_worker_retry_delay_seconds, worker._remaining_seconds())
    # MUTATION: sleep past the budget -> invariant 4's in-worker wait is exceeded.
    assert sum(slept) <= budget and all(0 < wait <= budget for wait in slept)
    assert len(slept) == len(broker.calls) - 1


@pytest.mark.parametrize("answer", [
    gittools.GitToolRefused("workflow_scope_missing", "no workflow scope"),
    gittools.GitToolFenced(409, gittools.GIT_FENCED, "superseded", retryable=False),
], ids=["not-retryable", "fenced"])
def test_a_non_retryable_or_fenced_broker_answer_is_tried_once(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path, answer
):
    broker = FakeBroker(origin, tmp_path, refuse=answer)
    _with_broker(monkeypatch, broker)
    slept: list[float] = []
    worker, config, out = run_attempt(_sleeping_into(worker_factory, slept), monkeypatch,
                                      origin, task_id="t-once", dispatch=DIRECT,
                                      edit=adds_a_workflow)
    _assert_failed_with_patch(worker, config, out, origin, forge, store, own_pushes, tmp_path)
    # MUTATION: retry every refusal, or a fence -> the broker is asked again (invariant 5).
    assert len(broker.calls) == 1 and slept == []


# -- the patch is in the manifest, and the reason is whole (#2023) -----------


def _finished_attempt(worker_factory, monkeypatch, origin, broker, task_id):
    """Clone, edit, then upload and publish the way `_finalise` does: the
    folder upload builds the manifest first, the deferred publish runs after
    it. Returns the worker, its config and the summary the finish writes."""
    _with_broker(monkeypatch, broker)
    worker, config, _ = run_attempt(worker_factory, monkeypatch, origin, task_id=task_id,
                                    dispatch=DIRECT, edit=adds_a_workflow, publish=False)
    summary = worker._upload_outputs(defer_publish=True)
    worker._publish_checked(summary, None)
    return worker, config, summary


def _listed(summary, name):
    return [entry for entry in summary["artifacts"] if entry["name"] == name]


def test_a_refused_workflow_push_lists_its_patch_in_the_manifest(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    refusal = gittools.GitToolRefused(
        "workflow_scope_missing", "the push touches .github/workflows/ci.yml")
    broker = FakeBroker(origin, tmp_path, refuse=refusal)
    worker, config, summary = _finished_attempt(
        worker_factory, monkeypatch, origin, broker, "t-listed")
    key = f"{config.artifact_prefix}/workflow-lines.patch"
    stored = (store.root / key).read_bytes()
    listed = _listed(summary, "workflow-lines.patch")
    # MUTATION: drop the manifest addition in `_publish_checked` -> the patch is
    # stored and in the error, and listed nowhere (task_58815d231b2146689808).
    assert len(listed) == 1, [entry["name"] for entry in summary["artifacts"]]
    # MUTATION: list the patch with the bytes of an earlier file -> its size is wrong.
    assert listed[0]["bytes"] == len(stored) and listed[0]["uri"] == store.uri(key)
    names = [entry["name"].split("/") for entry in summary["artifacts"]]
    # MUTATION: append without sorting -> the manifest leaves path order.
    assert names == sorted(names)
    # MUTATION: leave `artifact_bytes` as the folder upload counted it -> the
    # total disagrees with the entries it sums.
    assert summary["artifact_bytes"] >= len(stored)
    assert summary["git"][lifecycle.PUBLISH_FAILED_FIELD] is True


def test_a_patch_the_agent_wrote_under_the_same_name_is_listed_once(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    refusal = gittools.GitToolRefused("workflow_scope_missing", "no workflow scope")
    broker = FakeBroker(origin, tmp_path, refuse=refusal)
    _with_broker(monkeypatch, broker)
    worker, config, _ = run_attempt(worker_factory, monkeypatch, origin, task_id="t-own",
                                    dispatch=DIRECT, edit=adds_a_workflow, publish=False)
    (worker.ws.artifacts / "workflow-lines.patch").write_text("the agent's own text\n")
    summary = worker._upload_outputs(defer_publish=True)
    before = summary["artifact_bytes"]
    worker._publish_checked(summary, None)
    key = f"{config.artifact_prefix}/workflow-lines.patch"
    stored = (store.root / key).read_bytes()
    listed = _listed(summary, "workflow-lines.patch")
    # MUTATION: add without replacing the agent's entry -> the name is listed twice.
    assert len(listed) == 1
    # MUTATION: keep the agent's entry -> its size is the overwritten file's.
    assert listed[0]["bytes"] == len(stored)
    # MUTATION: add the patch's bytes without taking the replaced entry's off.
    assert summary["artifact_bytes"] == before - len("the agent's own text\n") + len(stored)


def test_a_workflow_push_that_was_not_refused_writes_and_lists_no_patch(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, store, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    worker, config, summary = _finished_attempt(
        worker_factory, monkeypatch, origin, broker, "t-pushed")
    # MUTATION: write the patch on every workflow publish -> the file exists.
    assert not (worker.ws.artifacts / "workflow-lines.patch").exists()
    assert not (store.root / f"{config.artifact_prefix}/workflow-lines.patch").exists()
    # MUTATION: add a manifest entry whatever the publish answered -> it is listed.
    assert _listed(summary, "workflow-lines.patch") == []
    assert summary["git"]["published"] is True


def _long_refusal() -> str:
    """About 400 characters, the last sentence the one the 300 cut broke."""
    head = ("the push touches .github/workflows/auto-merge.yml; GitHub takes a workflow "
            "file only from a credential with the workflow scope, and the slot holds no "
            "version, and the registration was read at the time of the push and its "
            "workflow_files switch is off. ")
    tail = ("Push without those files and write them as workflow-lines.patch for the "
            "operator to apply by hand, then ask for the switch to be turned on.")
    text = head + tail
    assert 380 <= len(text) <= 420, len(text)
    return text


def test_a_400_character_refusal_is_quoted_whole(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    message = _long_refusal()
    refusal = gittools.GitToolRefused("workflow_scope_missing", message)
    broker = FakeBroker(origin, tmp_path, refuse=refusal)
    worker, _, out = _refused_attempt(worker_factory, monkeypatch, origin, broker, "t-whole")
    # MUTATION: restore `[:300]` -> "write them as. Nothing was pushed".
    assert message in out["publish_reason"]
    why = worker._publish_failed({"git": out, "artifacts": []})
    # MUTATION: cut the step's error shorter than the reason -> the tail is lost there.
    assert why is not None and message in why
    assert "write them as. Nothing" not in why


@pytest.mark.parametrize("limit", [300, 301, 64])
def test_a_refusal_past_the_bound_is_cut_at_a_word_with_an_ellipsis(limit):
    message = _long_refusal()
    cut = lifecycle._cut_at_word(message, limit)
    # MUTATION: slice without the "…" -> a cut reads as the whole reason.
    assert cut.endswith("…") and len(cut) <= limit
    kept = cut[:-1]
    # MUTATION: slice at the limit -> the last word is half a word.
    assert message.startswith(kept) and not message[len(kept)].isalnum()
    # MUTATION: end the cut on the sentence's own full stop -> it reads as finished.
    assert not kept.endswith(".")


def test_a_refusal_within_the_bound_is_left_whole():
    message = _long_refusal()
    # MUTATION: always append "…" -> a whole reason reads as cut.
    assert lifecycle._cut_at_word(message, len(message)) == message
    assert lifecycle.WORKFLOW_REFUSAL_MAX_CHARS >= len(message)

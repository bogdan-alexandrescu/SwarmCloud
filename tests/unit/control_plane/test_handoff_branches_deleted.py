"""A finished workflow's step hand-off branches are deleted (the owner, 2026-11-02).

`handoffbranches.sweep_tenant`, run by the merge wake tick: once a workflow is
SUCCEEDED or CANCELLED and every step is terminal, each step's own
`swarm/<task_id>` hand-off branch is deleted while its head is still the one
the step recorded. A running workflow's are not; a moved head, a protected
branch, one with a pull request that did not merge at the recorded head
(open, closed without merging, merged at another head) or one a live
continuation reads is kept; a branch already gone is fine; a failed delete is recorded and retried.
GitHub is a fake transport under the real `forgewrite.GitHubWriter`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from swarm_api import handoffbranches, mergewake
from swarm_api.forgewrite import GitHubWriter

from . import forge_fakes
from .conftest import seed_tenant

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
OWNER, REPO = "octo-org", "widget-shop"
URL = f"https://github.com/{OWNER}/{REPO}"
BASE = f"/repos/{OWNER}/{REPO}"
HEAD_A, HEAD_B, MOVED = "a" * 40, "b" * 40, "f" * 40


class Branches:
    """GitHub's branches of one repository, as a write transport."""

    def __init__(self) -> None:
        self.heads: dict[str, str] = {}
        self.protected: set[str] = set()
        self.open_pulls: set[str] = set()
        #: Closed pull requests by head branch: (number, merged, head sha).
        self.closed_pulls: dict[str, list[tuple[int, bool, str]]] = {}
        self.delete_answer: tuple[int, Any] | None = None
        self.calls: list[tuple[str, str]] = []

    def deleted(self) -> list[str]:
        prefix = f"{BASE}/git/refs/heads/"
        return [unquote(path[len(prefix):]) for method, path in self.calls
                if method == "DELETE" and path.startswith(prefix)]

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        path = parsed.path
        self.calls.append((method, path))

        def answer(status: int, data: Any = None):
            return status, (b"" if data is None else json.dumps(data).encode()), {}

        if method == "GET" and path == BASE:
            return answer(200, {"default_branch": "main"})
        if method == "GET" and path.startswith(f"{BASE}/git/ref/heads/"):
            name = unquote(path[len(f"{BASE}/git/ref/heads/"):])
            if name not in self.heads:
                return answer(404, {"message": "Not Found"})
            return answer(200, {"object": {"sha": self.heads[name]}})
        if method == "GET" and path.startswith(f"{BASE}/branches/"):
            name = unquote(path[len(f"{BASE}/branches/"):])
            if name not in self.heads:
                return answer(404, {"message": "Branch not found"})
            return answer(200, {"name": name, "protected": name in self.protected})
        if method == "GET" and path == f"{BASE}/pulls":
            query = parse_qs(parsed.query)
            head = query["head"][0].split(":", 1)[1]
            repo = {"full_name": f"{OWNER}/{REPO}"}
            rows = []
            if head in self.open_pulls:
                rows.append({"number": 7, "state": "open", "html_url": "x",
                             "head": {"ref": head, "sha": self.heads[head], "repo": repo},
                             "base": {"ref": "main", "repo": repo}})
            if query["state"] == ["all"]:
                # GitHub's list answer has `merged_at`, never `merged`.
                rows += [{"number": number, "state": "closed", "html_url": "x",
                          "merged_at": "2026-11-01T00:00:00Z" if merged else None,
                          "head": {"ref": head, "sha": sha, "repo": repo},
                          "base": {"ref": "main", "repo": repo}}
                         for number, merged, sha in self.closed_pulls.get(head, [])]
            return answer(200, rows)
        if method == "DELETE" and path.startswith(f"{BASE}/git/refs/heads/"):
            name = unquote(path[len(f"{BASE}/git/refs/heads/"):])
            if self.delete_answer is not None:
                return answer(*self.delete_answer)
            if self.heads.pop(name, None) is None:
                return answer(422, {"message": "Reference does not exist"})
            return answer(204)
        return answer(404, {"message": f"no fake route {method} {path}"})


@pytest.fixture
def github() -> Branches:
    return Branches()


@pytest.fixture
def ctx(db, github):
    tenant = seed_tenant(db, "eng")
    store = SimpleNamespace(db=db, get_tenant=lambda tenant_id: tenant)
    return SimpleNamespace(store=store, forge_writer=GitHubWriter(send=github), tenant=tenant,
                           forge_tokens=forge_fakes.AnyTenantTokens(), now=lambda: NOW)


def _task(db, task_id: str, *, state: str = "SUCCEEDED", head: str | None = None,
          pull_request: int | None = None, tenant: str = "eng", **metadata: Any) -> None:
    summary: dict[str, Any] = {}
    if head is not None:
        summary["branch"] = {"name": f"swarm/{task_id}", "head": head}
    if pull_request is not None:
        summary["git"] = {"published": True, "pull_request": {"number": pull_request}}
    db.collection("tasks").document(task_id).set({
        "id": task_id, "tenant_id": tenant, "state": state, "repository_url": URL,
        "result_summary": summary, "metadata": {"dispatch": metadata} if metadata else {},
    })


def _workflow(db, workflow_id: str, steps: list[str], *, state: str = "SUCCEEDED",
              age: timedelta = timedelta(hours=1)) -> None:
    db.collection("workflows").document(workflow_id).set({
        "workflow_id": workflow_id, "tenant_id": "eng", "state": state,
        "created_at": NOW - age, "updated_at": NOW,
        "steps": [{"step_id": f"s{i}", "task_id": t} for i, t in enumerate(steps)],
    })


def _sweep(ctx, db, now: datetime = NOW) -> handoffbranches.SweepReport:
    return handoffbranches.sweep_tenant(ctx, ctx.tenant, "eng", now=now)


def _record(db, workflow_id: str) -> dict[str, Any]:
    return db.collection("branch_cleanups").document(workflow_id).get().to_dict()


def test_a_finished_workflows_hand_off_branches_are_deleted(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _task(db, "task_b", head=HEAD_B)
    _task(db, "task_pr", head="c" * 40, pull_request=41)
    _workflow(db, "wf_1", ["task_a", "task_b", "task_pr"])
    github.heads.update({"swarm/task_a": HEAD_A, "swarm/task_b": HEAD_B,
                         "swarm/task_pr": "c" * 40})

    report = _sweep(ctx, db)

    assert report.swept and report.workflows == 1 and report.deleted == 2
    assert sorted(github.deleted()) == ["swarm/task_a", "swarm/task_b"]
    # The pull request's own branch is the merge step's, not this sweep's.
    assert "swarm/task_pr" in github.heads
    record = _record(db, "wf_1")
    assert record["done"] is True
    assert {row["branch"]: row["outcome"] for row in record["branches"]} == {
        "swarm/task_a": "deleted", "swarm/task_b": "deleted"}
    # The token is in no record.
    (token,) = ctx.forge_tokens.issued.values()
    assert token not in json.dumps(record, default=str)


@pytest.mark.parametrize("state", ["RUNNING", "FAILED"])
def test_a_running_or_retryable_workflows_branches_are_not_deleted(ctx, db, github, state):
    _task(db, "task_a", head=HEAD_A)
    _task(db, "task_b", state="RUNNING" if state == "RUNNING" else "FAILED", head=HEAD_B)
    _workflow(db, "wf_1", ["task_a", "task_b"], state=state)
    github.heads.update({"swarm/task_a": HEAD_A, "swarm/task_b": HEAD_B})

    report = _sweep(ctx, db)

    assert github.deleted() == []
    assert report.workflows == 0
    assert not db.collection("branch_cleanups").document("wf_1").get().exists


def test_a_stored_final_state_with_a_step_still_running_deletes_nothing(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _task(db, "task_b", state="RUNNING", head=HEAD_B)
    _workflow(db, "wf_1", ["task_a", "task_b"], state="CANCELLED")
    github.heads.update({"swarm/task_a": HEAD_A, "swarm/task_b": HEAD_B})

    _sweep(ctx, db)

    assert github.deleted() == []


def test_a_cancelled_workflows_branches_are_deleted(ctx, db, github):
    _task(db, "task_a", state="CANCELLED", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"], state="CANCELLED")
    github.heads["swarm/task_a"] = HEAD_A

    _sweep(ctx, db)

    assert github.deleted() == ["swarm/task_a"]


@pytest.mark.parametrize("arrange, reason", [
    (lambda gh: gh.heads.update({"swarm/task_a": MOVED}), "head_moved"),
    (lambda gh: gh.protected.add("swarm/task_a"), "protected"),
    (lambda gh: gh.open_pulls.add("swarm/task_a"), "open_pull_request"),
])
def test_a_moved_protected_or_open_branch_is_kept(ctx, db, github, arrange, reason):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])
    github.heads["swarm/task_a"] = HEAD_A
    arrange(github)

    report = _sweep(ctx, db)

    assert github.deleted() == []
    assert report.kept == 1
    (row,) = _record(db, "wf_1")["branches"]
    assert row["outcome"] == "kept" and row["reason"] == reason


def test_a_branch_a_live_continuation_reads_is_kept(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])
    _task(db, "task_cont", state="RUNNING", continues="task_a")
    github.heads["swarm/task_a"] = HEAD_A

    _sweep(ctx, db)

    assert github.deleted() == []
    (row,) = _record(db, "wf_1")["branches"]
    assert row["reason"] == "continued"


def test_an_already_deleted_branch_is_fine(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])

    report = _sweep(ctx, db)

    assert report.already_deleted == 1 and report.failed == 0
    assert _record(db, "wf_1")["done"] is True


def test_a_delete_answered_reference_does_not_exist_is_already_deleted(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])
    github.heads["swarm/task_a"] = HEAD_A
    github.delete_answer = (422, {"message": "Reference does not exist"})

    report = _sweep(ctx, db)

    assert report.already_deleted == 1 and report.failed == 0


def test_a_failed_delete_is_recorded_and_retried_by_the_next_sweep(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])
    github.heads["swarm/task_a"] = HEAD_A
    github.delete_answer = (403, {"message": "Resource not accessible by integration"})

    report = _sweep(ctx, db)

    assert report.failed == 1
    record = _record(db, "wf_1")
    assert record["done"] is False
    assert record["branches"][0]["outcome"] == "failed"

    # Inside the sweep interval nothing runs; after it, the delete is retried.
    github.delete_answer = None
    assert _sweep(ctx, db, NOW + timedelta(minutes=5)).swept is False
    later = _sweep(ctx, db, NOW + timedelta(seconds=handoffbranches.SWEEP_SECONDS))
    assert later.deleted == 1
    assert _record(db, "wf_1")["done"] is True


def test_a_done_workflow_is_not_read_again(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])
    github.heads["swarm/task_a"] = HEAD_A
    _sweep(ctx, db)
    github.calls.clear()

    report = _sweep(ctx, db, NOW + timedelta(seconds=handoffbranches.SWEEP_SECONDS))

    assert report.workflows == 0
    assert github.calls == []


def test_another_tenants_workflow_is_never_swept(ctx, db, github):
    _task(db, "task_a", head=HEAD_A, tenant="research")
    db.collection("workflows").document("wf_x").set({
        "workflow_id": "wf_x", "tenant_id": "research", "state": "SUCCEEDED",
        "created_at": NOW - timedelta(hours=1), "steps": [{"step_id": "s", "task_id": "task_a"}],
    })
    github.heads["swarm/task_a"] = HEAD_A

    _sweep(ctx, db)

    assert github.deleted() == []


def test_the_merge_wake_tick_sweeps_the_tenant(ctx, db, github):
    _task(db, "task_a", head=HEAD_A)
    _workflow(db, "wf_1", ["task_a"])
    github.heads["swarm/task_a"] = HEAD_A

    report = mergewake.wake_tenant(ctx, "eng", limit=10)

    assert github.deleted() == ["swarm/task_a"]
    assert report.to_api()["branches"]["deleted"] == 1


def _merge_step(db, task_id: str, *, outcome: str, head: str = HEAD_B) -> None:
    db.collection("tasks").document(task_id).set({
        "id": task_id, "tenant_id": "eng", "state": "SUCCEEDED", "repository_url": URL,
        "result_summary": {"merge": {"merged_by_this_task": True, "branch_deleted": False,
                                     "branch_cleanup": {"branch": "swarm/task_int",
                                                        "head": head, "outcome": outcome}}},
    })


def test_a_merge_steps_failed_delete_is_retried_by_the_sweep(ctx, db, github):
    _merge_step(db, "task_merge", outcome="failed")
    _workflow(db, "wf_1", ["task_merge"])
    github.heads["swarm/task_int"] = HEAD_B

    report = _sweep(ctx, db)

    assert github.deleted() == ["swarm/task_int"]
    assert report.deleted == 1


@pytest.mark.parametrize("outcome, head", [("kept", HEAD_B), ("failed", MOVED)])
def test_a_merge_steps_kept_branch_or_moved_head_is_not_retried(ctx, db, github, outcome, head):
    _merge_step(db, "task_merge", outcome=outcome)
    _workflow(db, "wf_1", ["task_merge"])
    github.heads["swarm/task_int"] = head

    _sweep(ctx, db)

    assert github.deleted() == []


@pytest.mark.parametrize("merged, sha, reason", [
    (False, HEAD_A, "closed_unmerged"),
    (True, MOVED, "merged_at_other_head"),
])
def test_a_continued_steps_branch_whose_pull_request_did_not_merge_at_its_head_is_kept(
        ctx, db, github, merged, sha, reason):
    """A continuation published on the continued step's branch (`publish_to_pr`):
    that step records no pull request, and one closed without merging keeps it."""
    _task(db, "task_a", head=HEAD_A)
    _task(db, "task_cont", state="SUCCEEDED", publish_to_pr={"task_id": "task_a"})
    _workflow(db, "wf_1", ["task_a", "task_cont"], state="CANCELLED")
    github.heads["swarm/task_a"] = HEAD_A
    github.closed_pulls["swarm/task_a"] = [(43, merged, sha)]

    report = _sweep(ctx, db)

    assert github.deleted() == []
    assert report.kept == 1
    (row,) = _record(db, "wf_1")["branches"]
    assert row["outcome"] == "kept" and row["reason"] == reason
    assert row["pull_request"] == 43


def test_a_branch_whose_pull_request_merged_at_its_head_is_deleted(ctx, db, github):
    _merge_step(db, "task_merge", outcome="failed")
    _workflow(db, "wf_1", ["task_merge"])
    github.heads["swarm/task_int"] = HEAD_B
    github.closed_pulls["swarm/task_int"] = [(41, True, HEAD_B)]

    report = _sweep(ctx, db)

    assert github.deleted() == ["swarm/task_int"]
    assert report.deleted == 1

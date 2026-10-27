"""swarm-api lists a merge of main's workflow files only where the merge changed them (#2452).

MEASURED (task_1297d8f4 and task_f2b72856, 2026-10-26): both steps merged
`main` after PR 2446 changed `application.yml` and `release.yml`, and both
were refused as workflow pushes though neither changed a workflow file:
`git log --name-only -m` diffs a merge against EACH parent, and against the
branch parent a merge of main names every file main changed.

`git_push_branch` reads the pushed bundle with `--diff-merges=combined`, the
same listing as the worker's `Worker._workflow_paths`, so the two never
disagree about which route a branch takes:

  * a merge that only brings main's workflow change in is not a workflow
    push: with the registration's `workflow_files` switch off it lands, and
    nothing is minted;
  * a merge whose result differs from every parent in a workflow file (a
    conflict resolution, an "evil" merge) is still one;
  * a non-merge commit that renames a file away from `.github/workflows/`
    still counts, as before.

Real git against a bare origin on disk, through the route, as in
test_git_tools_workflow_slot.py. The bundle is thin over both of the merge's
outside parents, so swarm-api reads it over prerequisites it fetched at
depth 1 -- the shape the worker's bundle has (`_workflow_negatives`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .test_git_tools_api import (  # noqa: F401  (fixtures)
    OWN_BRANCH,
    _commit,
    _git,
    audit_rows,
    call,
    client,
    origin,
    push_fields,
    remote_tip,
    running_task,
    slots,
    worker_tokens,
)
from .test_git_tools_reads import forge, seed_registration  # noqa: F401  (forge: a fixture)
from .test_git_tools_workflow_slot import app, mint, switch  # noqa: F401  (fixtures)

WORKFLOW = ".github/workflows/ci.yml"


def _write(work: Path, path: str, text: str) -> None:
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n")
    _git(work, "add", path)


def _main_changes_a_workflow(origin: dict[str, Any]) -> str:
    """`main` on origin gains a workflow commit after the lane was cut at B."""
    work = origin["work"]
    _git(work, "checkout", "-q", "-b", "upstream", origin["b"])
    _write(work, WORKFLOW, "on: push  # main's")
    _git(work, "commit", "-q", "-m", "main changes ci")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", str(origin["remote"]), "HEAD:refs/heads/main")
    _git(work, "checkout", "-q", "-b", "lane", origin["b"])
    return sha


def _merge(work: Path, sha: str, *, resolve: str | None = None) -> str:
    if resolve is None:
        _git(work, "merge", "-q", "--no-ff", "--no-edit", sha)
    else:
        _git(work, "merge", "-q", "--no-ff", "--no-commit", sha)
        _write(work, WORKFLOW, resolve)
        _git(work, "commit", "-q", "--no-edit")
    return _git(work, "rev-parse", "HEAD")


def test_a_merge_that_only_brings_mains_workflow_change_in_is_not_a_workflow_push(
        client, db, origin, mint):
    seed_registration(db)  # workflow_files off: a workflow push is refused
    attempt, _ = running_task(client, db)
    main = _main_changes_a_workflow(origin)
    _commit(origin["work"], "lane.txt", "the lane's own work")
    head = _merge(origin["work"], main)

    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, bases=(origin["b"], main)))

    # MUTATION: list with `-m` -> main's ci.yml is the branch's, and this
    # reads 422 workflow_scope_missing (the measured refusal).
    assert response.status_code == 200, response.text
    assert remote_tip(origin, OWN_BRANCH) == head
    assert mint.calls == []
    assert audit_rows(db)[-1]["slot"] == "git"


def test_a_merge_that_resolves_a_workflow_file_its_own_way_is_still_a_workflow_push(
        client, db, origin, mint):
    seed_registration(db)
    attempt, _ = running_task(client, db)
    main = _main_changes_a_workflow(origin)
    _write(origin["work"], WORKFLOW, "on: push  # the lane's")
    _git(origin["work"], "commit", "-q", "-m", "the lane changes ci")
    lane = _git(origin["work"], "rev-parse", "HEAD")
    # The lane's own ci commit is already on origin: only the merge is new.
    _git(origin["work"], "push", "-q", str(origin["remote"]), f"{lane}:refs/heads/side")
    try:
        _merge(origin["work"], main)
    except Exception:  # noqa: BLE001 -- the conflict is the point
        pass
    _write(origin["work"], WORKFLOW, "on: [push, pull_request]  # resolved")
    _git(origin["work"], "commit", "-q", "--no-edit")

    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, bases=(lane, main)))

    # MUTATION: list with `--cc` or skip merges -> the resolution goes out unlisted.
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "workflow_scope_missing"
    assert body["detail"]["paths"] == [WORKFLOW]
    assert remote_tip(origin, OWN_BRANCH) is None


def test_an_evil_merge_of_a_workflow_file_main_changed_is_still_a_workflow_push(
        client, db, origin, mint):
    seed_registration(db)
    attempt, _ = running_task(client, db)
    main = _main_changes_a_workflow(origin)
    _commit(origin["work"], "lane.txt", "the lane's own work")
    _merge(origin["work"], main, resolve="on: push  # neither parent's")

    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, bases=(origin["b"], main)))

    # MUTATION: list only non-merge commits -> this lands with the token.
    assert response.json()["code"] == "workflow_scope_missing", response.text
    assert remote_tip(origin, OWN_BRANCH) is None


def test_a_rename_away_from_a_workflow_path_still_counts(client, db, origin, mint):
    seed_registration(db)
    attempt, _ = running_task(client, db)
    work = origin["work"]
    _write(work, WORKFLOW, "on: push")
    _git(work, "commit", "-q", "-m", "ci")
    ci = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", str(origin["remote"]), f"{ci}:refs/heads/main")
    _git(work, "mv", WORKFLOW, "ci.yml")
    _git(work, "commit", "-q", "-m", "move ci away")

    response = call(client, attempt, "git_push_branch", push_fields(origin, bases=(ci,)))

    # MUTATION: list with renames -> the deletion under .github/workflows is unseen.
    assert response.json()["code"] == "workflow_scope_missing", response.text
    assert response.json()["detail"]["paths"] == [WORKFLOW]

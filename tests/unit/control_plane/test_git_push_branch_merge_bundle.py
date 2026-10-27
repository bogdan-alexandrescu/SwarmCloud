"""`git_push_branch` pushes a thin bundle whose tip is a merge of main (#2452).

WHAT WAS SEEN. On 2026-10-26 two lane steps (task_1297d8f422484ecdac7e,
task_f2b7285633584cb4b1c0) merged `main` into their branch, were routed to
`git_push_branch` and were answered `api_unavailable: the platform did not
answer; ask again` -- the worker's words for a 5xx or no answer, which drop
the status and the code. The same day the broker pushed a branch's OWN
workflow change (PR 2460, 8a73a93cd), so the suspicion was the merge bundle:
two prerequisites (the branch's old tip and the merged `main` commit), each
fetched at depth 1 into swarm-api's scratch repository, under a merge commit.

WHAT IS PINNED, with real git over a real thin bundle built the way the
worker's `_push_through_broker` builds it (`HEAD ^<clone base> ^<merged main>`)
and the real route (`GitExec(local_remotes=True)`):

* over a branch the forge already holds, the lane commits a workflow change of
  its own and then merges a `main` that changed a workflow file and an
  ordinary one: the push lands, answering the merge's sha, with one App mint
  (the branch's own workflow file is in the range, `main`'s is not);
* the same merge on a branch the forge never had (no remote tip, no lease):
  it lands too;
* a merge bundle whose merged `main` commit the forge does not have is a
  typed 422 `bad_bundle`, never a 5xx.

No credentials, no network: every token is built at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .test_git_tools_api import (  # noqa: F401  (fixtures)
    OWN_BRANCH,
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

OWN_WF = ".github/workflows/lane.yml"
MAIN_WF = ".github/workflows/application.yml"


def _write_commit(work: Path, files: dict[str, str], message: str) -> str:
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text + "\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


def _merge_of_main(origin: dict[str, Any]) -> tuple[str, str]:
    """`main` gains a workflow change and an ordinary one; the lane merges it.
    Returns (the merged main commit, the merge)."""
    work = origin["work"]
    lane = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "-B", "upstream", origin["b"])
    main_tip = _write_commit(work, {MAIN_WF: "on: push", "m.txt": "m"}, "main changes ci")
    _git(work, "push", "-q", str(origin["remote"]), "HEAD:refs/heads/main")
    _git(work, "checkout", "-q", "--detach", lane)
    _git(work, "merge", "-q", "--no-ff", "--no-edit", main_tip)
    merge = _git(work, "rev-parse", "HEAD")
    assert _git(work, "rev-list", "--parents", "-n", "1", merge).split()[1:] == [lane, main_tip]
    return main_tip, merge


def test_a_merge_of_main_over_the_branchs_own_workflow_change_is_pushed(
        client, db, origin, mint):
    switch(db, seed_registration(db), True)
    attempt, _ = running_task(client, db)
    work = origin["work"]
    pushed = _write_commit(work, {"lane.txt": "lane"}, "the lane's earlier, pushed work")
    first = call(client, attempt, "git_push_branch", push_fields(origin), request_id="req-1")
    assert first.status_code == 200, first.text
    assert remote_tip(origin, OWN_BRANCH) == pushed
    assert mint.calls == []
    _write_commit(work, {OWN_WF: "on: pull_request"}, "the lane's own workflow")
    main_tip, merge = _merge_of_main(origin)
    fields = push_fields(origin, bases=(pushed, main_tip))
    response = call(client, attempt, "git_push_branch", fields, request_id="req-2")
    # MUTATION: let a merge bundle's second prerequisite, or the merge commit,
    # raise anything but GitExecError in push_bundle and this is a 5xx.
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["old_sha"], body["new_sha"], body["rewrite"]) == (pushed, merge, False)
    assert remote_tip(origin, OWN_BRANCH) == merge
    assert len(mint.calls) == 1
    row = audit_rows(db)[-1]
    assert row["result"] == "ok" and row["credential"] == "app_installation"
    # The branch's own file is the range's; main's is not (step 1, -c).
    assert row["workflow_paths"] == [OWN_WF]


def test_a_merge_of_main_on_a_branch_the_forge_never_had_is_pushed(client, db, origin, mint):
    switch(db, seed_registration(db), True)
    attempt, _ = running_task(client, db)
    _write_commit(origin["work"], {OWN_WF: "on: pull_request"}, "the lane's own workflow")
    main_tip, merge = _merge_of_main(origin)
    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, bases=(origin["b"], main_tip)))
    assert response.status_code == 200, response.text
    assert response.json()["new_sha"] == merge
    assert remote_tip(origin, OWN_BRANCH) == merge


def test_a_merge_bundle_built_on_a_main_the_forge_lacks_is_a_typed_refusal(
        client, db, origin, mint):
    switch(db, seed_registration(db), True)
    attempt, _ = running_task(client, db)
    work = origin["work"]
    _write_commit(work, {OWN_WF: "on: pull_request"}, "the lane's own workflow")
    lane = _git(work, "rev-parse", "HEAD")
    # A main commit the forge never received: the bundle names it as a prerequisite.
    _git(work, "checkout", "-q", "-B", "upstream", origin["b"])
    unseen = _write_commit(work, {"u.txt": "u"}, "never pushed")
    _git(work, "checkout", "-q", "--detach", lane)
    _git(work, "merge", "-q", "--no-ff", "--no-edit", unseen)
    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, bases=(origin["b"], unseen)))
    # MUTATION: map bad_bundle to a 5xx and the worker reads api_unavailable.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bad_bundle"
    assert remote_tip(origin, OWN_BRANCH) is None

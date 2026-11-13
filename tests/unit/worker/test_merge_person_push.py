"""A person's push to a merge step's pull request supersedes the step (#2505).

the owner's decision, 2026-10-27 (issue triage, epic #2525): "A distinct
non-failure end: superseded_by_person_push (not FAILED) when the committer is
not the platform identity, so failure-review stops filing it; a foreign head
from an unknown branch-mate still refuses."

The refusal itself is unchanged: `merge._accept` still takes no head a person
made of content, and nothing merges. What changes is the code the step ends
with. A head that is a single commit, or (#3601) a merge of the base onto an
accepted head that the purity proof refuses, that the platform did not
commit, and that GitHub links to a person on the pull request (its author or
an assignee) ends
`superseded_by_person_push`; every other head that is not accepted ends
`head_moved` as before. `EndCause` is frozen, so until contract request
139 (SUPERSEDED_BY_PERSON_PUSH) lands the step still ends FAILED/MERGE_REFUSED,
with the new code, which failure-review's `person_push` rule skips
(tests/unit/control_plane/test_failure_digest.py).

The control is `test_an_unknown_branch_mates_commit_still_ends_head_moved`:
without it every case could be a step that calls every refusal superseded.
"""

from __future__ import annotations

from typing import Any

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

import merge_world
from merge_world import API, PINNED, PR, MergeWorld

#: The person who opened the pull request (the tenant's `-git` token is
#: theirs), and who pushes to it from a laptop, as in #2446.
PAT = {"name": "Pat Person", "email": "pat@example.com"}
PAT_LOGIN = "pat-person"
#: A branch-mate GitHub knows, who is neither the author nor an assignee.
MALLORY = {"name": "Mallory", "email": "mallory@example.com"}
MALLORY_LOGIN = "mallory"
#: The person's commit on top of the pinned head.
HEAD = "9" * 40


def _account(login: str, kind: str = "User") -> dict[str, Any]:
    return {"login": login, "type": kind}


def _person_commit(world: MergeWorld, *, sha: str = HEAD, parents: list[str] | None = None,
                   committer: dict[str, str] | None = None, committer_account: Any = None,
                   author: dict[str, str] | None = None, author_account: Any = None,
                   verified: bool = False, message: str = "Push the workflow lines by hand") -> str:
    """Serve `GET .../commits/<sha>` as GitHub serves a pushed commit: the git
    identities under `commit`, and the GitHub accounts they link to at the top."""
    committer = committer or PAT
    world.github.route("GET", f"{API}/commits/{sha}", (200, {}, {
        "sha": sha, "parents": [{"sha": p} for p in (parents or [PINNED])],
        "commit": {"committer": dict(committer), "author": dict(author or committer),
                   "message": message, "verification": {"verified": verified}},
        "committer": committer_account if committer_account is not None else _account(PAT_LOGIN),
        "author": author_account if author_account is not None else _account(PAT_LOGIN),
    }))
    world.serve_checks(sha)
    return sha


def _world(tmp_path, *, author: str = PAT_LOGIN, kind: str = "User",
           assignees: list[str] | None = None) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.pr["user"] = _account(author, kind)
    world.pr["assignees"] = [_account(login) for login in assignees or []]
    return world


def test_the_owners_hand_push_on_the_parked_head_ends_superseded_not_head_moved(tmp_path):
    """#2446: a single-parent commit on the parked head, by the person whose
    pull request it is. Refused as before -- no merge call -- and named
    superseded_by_person_push, with who pushed it and the commit."""
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_commit(world)
    outcome = merge.run_merge(world.context())

    refusal = outcome.summary["refusal"]
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH == "superseded_by_person_push"
    assert refusal["person"] == PAT_LOGIN
    assert refusal["commit"]["sha"] == HEAD
    assert refusal["commit"]["why"] == "it is not a two-parent merge"
    assert HEAD in refusal["message"] and PAT_LOGIN in refusal["message"]
    assert world.merge_calls() == []
    # FROZEN: until the contract request lands, the cause stays MERGE_REFUSED.
    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.message.startswith("superseded_by_person_push: ")


def test_an_assignees_edit_in_githubs_web_ui_is_a_person_push(tmp_path):
    """GitHub commits and signs a web-UI edit; the person is its author."""
    world = _world(tmp_path, author="swarm-opener", assignees=[PAT_LOGIN])
    world.pr["head"]["sha"] = _person_commit(
        world, committer=merge_world.GITHUB_COMMITTER, committer_account=_account("web-flow"),
        author=PAT, author_account=_account(PAT_LOGIN), verified=True)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == merge.SUPERSEDED_BY_PERSON_PUSH
    assert outcome.summary["refusal"]["person"] == PAT_LOGIN
    assert world.merge_calls() == []


def test_an_unknown_branch_mates_commit_still_ends_head_moved(tmp_path):
    """The control: a person GitHub knows, who is not on the pull request."""
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_commit(world, committer=MALLORY,
                                             committer_account=_account(MALLORY_LOGIN),
                                             author_account=_account(MALLORY_LOGIN))
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert "person" not in outcome.summary["refusal"]
    assert world.merge_calls() == []


def test_a_commit_github_links_to_no_account_ends_head_moved(tmp_path):
    world = _world(tmp_path)
    world.github.route("GET", f"{API}/commits/{HEAD}", (200, {}, {
        "sha": HEAD, "parents": [{"sha": PINNED}],
        "commit": {"committer": dict(PAT), "author": dict(PAT), "message": "x",
                   "verification": {"verified": False}},
        "committer": None, "author": None}))
    world.pr["head"]["sha"] = HEAD
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"


def test_a_bot_account_on_the_pull_request_is_not_a_person(tmp_path):
    """An App opened the pull request and its bot pushed: not a person's push."""
    world = _world(tmp_path, author="swarm-app[bot]", kind="Bot")
    world.pr["head"]["sha"] = _person_commit(
        world, committer_account=_account("swarm-app[bot]", "Bot"),
        author_account=_account("swarm-app[bot]", "Bot"))
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"


def test_a_commit_the_worker_made_as_the_person_is_the_platforms(tmp_path):
    """#765: the worker publishes as the person who dispatched the work, so
    the committer names them. Its message's last line says the worker made
    it: a platform push that is not accepted is a defect failure-review must
    see, never a person's push."""
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_commit(
        world, message="A lane's change\n\nwhy\n\n" + merge.WORKER_COMMIT_LINE + "task_abc.")
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"


#: The recurrence of 2026-11-05 (#2505, task_5b7d62f1's merge of PR 3323):
#: the owner applied their pick through a one-step `publish_to_pr` lane they
#: dispatched (task_568ad862, wf_31277a25), whose worker published its kept
#: commit cdcbd71 AS the owner (#765) and ended it with the worker's line.
LANE = "task_568ad8627f3641d98055"
LANE_WORKFLOW = "wf_31277a251bb74b709c18"
LANE_HEAD = "cdcbd7134a6f72431411d64e0f40a1ecf44d5bad"
LANE_PARENT = "71e334299ce77db799c548de33587679c3cd32e3"


def _lane(world: MergeWorld, task_id: str = LANE, *, root: str = merge_world.OPENER,
          via: str = "publish_to_pr", workflow: str = LANE_WORKFLOW,
          **metadata: Any) -> str:
    """Seed the lane's task document as the task store holds it: a plain
    continuation of the pull request's branch task, by `continues` or by
    `publish_to_pr` (cifix.live_plain_continuation's two kinds)."""
    dispatch = ({"publish_to_pr": {"task_id": root, "number": merge_world.NUMBER}}
                if via == "publish_to_pr" else {"continues": root})
    world.docs[task_id] = {"tenant_id": merge_world.TENANT, "workflow_id": workflow,
                           "runner_profile": "claude-code", "state": "SUCCEEDED",
                           "metadata": {"dispatch": dispatch, **metadata}}
    return task_id


def _lane_commit(world: MergeWorld, task_id: str = LANE, **kwargs: Any) -> str:
    """cdcbd71's shape: one parent, the person's committer address, GitHub
    linking it to the person, and the lane's worker lines at its end."""
    message = ("The repo trivy gate accepts only the speech-lockfile entries\n\n"
               "Made by the worker for lane speech-gate-narrow.\n\n"
               f"{merge.WORKER_COMMIT_LINE}{task_id}.")
    kwargs.setdefault("sha", LANE_HEAD)
    kwargs.setdefault("parents", [LANE_PARENT])
    return _person_commit(world, message=message, **kwargs)


def test_the_owners_hand_commit_after_a_pick_fix_park_ends_superseded(tmp_path):
    """#2505, 2026-11-05: the step parked `owner_pick_fix_pending`; the owner
    applied the pick through a lane they dispatched onto the pull request,
    and the next wake read cdcbd71 -- one parent, committed as the owner,
    linked to the pull request's author, ending with the lane's worker line.
    It ended head_moved because `_person_pusher` read every worker-line
    commit as an unaccepted platform head. A plain lane on this pull
    request's branch, published as a person on the pull request, is that
    person's push: superseded, and still nothing merges."""
    world = _world(tmp_path)
    world.docs[merge_world.TASK]["metadata"] = {"merge_wait": {"code": "owner_pick_fix_pending"}}
    _lane(world)
    world.pr["head"]["sha"] = _lane_commit(world)
    outcome = merge.run_merge(world.context())

    refusal = outcome.summary["refusal"]
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH, refusal
    assert refusal["person"] == PAT_LOGIN
    assert refusal["commit"]["sha"] == LANE_HEAD
    assert refusal["commit"]["worker_task"] == LANE
    assert world.merge_calls() == []
    assert outcome.end_cause is EndCause.MERGE_REFUSED


def test_a_lane_by_continues_is_the_persons_push_too(tmp_path):
    world = _world(tmp_path)
    _lane(world, via="continues")
    world.pr["head"]["sha"] = _lane_commit(world)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == merge.SUPERSEDED_BY_PERSON_PUSH
    assert world.merge_calls() == []


def test_a_platform_head_that_is_not_a_persons_lane_still_ends_head_moved(tmp_path):
    """The controls: a round of this merge (a writer's mark), a lane on
    another branch, a task of this merge's own workflow, an issue run's
    task, a lane task the store cannot read, and a lane published as a
    branch-mate who is not on the pull request are platform heads the step
    did not accept -- defects failure-review must see."""
    cases = {
        "round": dict(merge_fix_round={"round": 1}),
        "issue_run": dict(issue_run={"run_id": "crun_1"}),
        "other_root": dict(root="task_other"),
        "own_workflow": dict(workflow=merge_world.WORKFLOW),
    }
    for name, kwargs in cases.items():
        world = _world(tmp_path / name)
        _lane(world, **kwargs)
        world.pr["head"]["sha"] = _lane_commit(world)
        outcome = merge.run_merge(world.context())
        assert outcome.summary["refusal"]["code"] == "head_moved", name
        assert world.merge_calls() == [], name

    world = _world(tmp_path / "unread")
    world.pr["head"]["sha"] = _lane_commit(world)
    assert merge.run_merge(world.context()).summary["refusal"]["code"] == "head_moved"

    world = _world(tmp_path / "unsigned")
    _lane(world)
    world.unverified[LANE] = "signature_mismatch"
    world.pr["head"]["sha"] = _lane_commit(world)
    assert merge.run_merge(world.context()).summary["refusal"]["code"] == "head_moved"

    world = _world(tmp_path / "mallory")
    _lane(world)
    world.pr["head"]["sha"] = _lane_commit(world, committer=MALLORY,
                                           committer_account=_account(MALLORY_LOGIN),
                                           author_account=_account(MALLORY_LOGIN))
    assert merge.run_merge(world.context()).summary["refusal"]["code"] == "head_moved"


def test_a_head_moved_refusal_records_why_no_person_was_named(tmp_path):
    """The 2026-11-05 refusal said who committed cdcbd71 by address only, so
    nothing showed why the person test named nobody. `refusal.commit` now
    carries that test's inputs: the logins GitHub linked, the task a worker
    line names, and the pull request's people -- logins, never an address
    beyond the committer's, never a token."""
    world = _world(tmp_path, assignees=["robin"])
    world.pr["head"]["sha"] = _person_commit(world, committer=MALLORY,
                                             committer_account=_account(MALLORY_LOGIN),
                                             author=PAT, author_account=_account(PAT_LOGIN))
    outcome = merge.run_merge(world.context())
    refusal = outcome.summary["refusal"]
    assert refusal["code"] == "head_moved"
    commit = refusal["commit"]
    assert commit["committer_login"] == MALLORY_LOGIN
    assert commit["author_login"] == PAT_LOGIN
    assert commit["worker_task"] is None
    assert sorted(commit["people"]) == sorted([PAT_LOGIN, "robin"])
    assert "people" in refusal["message"] and MALLORY_LOGIN in refusal["message"]
    text = repr(outcome.summary)
    assert PAT["email"] not in text and world.token not in text

    world = _world(tmp_path / "lane")
    world.pr["head"]["sha"] = _lane_commit(world)
    commit = merge.run_merge(world.context()).summary["refusal"]["commit"]
    assert commit["worker_task"] == LANE
    assert commit["committer_login"] == PAT_LOGIN and commit["people"] == [PAT_LOGIN]


class _Quiet:
    """The structured logger `replay_agent_commits` writes to, dropping all."""

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


def _kept_agent_commit_message(tmp_path) -> str:
    """The message `lifecycle.replay_agent_commits` gives an agent's commit it
    keeps, read back from a real repository after the rewrite."""
    import subprocess

    from agent_worker import lifecycle

    repo = tmp_path / "kept"
    repo.mkdir()

    def git(*argv: str) -> str:
        return subprocess.run(["git", *argv], cwd=str(repo), check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    ident = ["-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com"]
    git(*ident, "commit", "-q", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "test_widget.py").write_text("def test_negative(): assert False\n")
    git("add", "-A")
    git(*ident, "commit", "-q", "-m",
        "Tests: the widget refuses a negative size\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n")
    for name in ("private", "logs"):
        (tmp_path / name).mkdir()
    kept = lifecycle.replay_agent_commits(
        repo=repo, base=base, keep=None, task_id="task_abc", scrub=lambda text: text,
        author_name=PAT["name"], author_email=PAT["email"],
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs",
        timeout_seconds=60, logger=_Quiet(),
    )
    assert kept == 1
    assert git("log", "-1", "--format=%ce") == PAT["email"]
    return git("log", "-1", "--format=%B")


def test_a_kept_agent_commit_the_worker_pushed_as_the_person_is_the_platforms(tmp_path):
    """#2505 review: `replay_agent_commits` rewrites each agent commit it keeps
    under the dispatcher's identity (#765) -- one parent, the person's
    committer address, GitHub linking it to the pull request's author. Every
    red-first lane's tests commit has this shape. Without the worker's line
    its head read as the person's push, so a platform head the step did not
    accept ended superseded_by_person_push and failure-review skipped it."""
    message = _kept_agent_commit_message(tmp_path)
    assert message.strip().splitlines()[-1] == merge.WORKER_COMMIT_LINE + "task_abc.", message
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_commit(world, message=message)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert "person" not in outcome.summary["refusal"]


def test_a_commit_under_the_platforms_own_identity_ends_head_moved(tmp_path):
    world = _world(tmp_path)
    for email in sorted(merge.PLATFORM_COMMITTER_EMAILS):
        world.pr["head"]["sha"] = _person_commit(world, committer={"name": "bot", "email": email})
        outcome = merge.run_merge(world.context())
        assert outcome.summary["refusal"]["code"] == "head_moved", email


#: #3601, 2026-11-06 23:25Z: the owner merged main into six train-parked
#: pull requests by hand and resolved real conflicts. The merge base, the
#: pinned head and main's tip; both parents change notes.txt, and the
#: person's merge holds a third blob there, so the purity proof refuses it
#: ('both parents change notes.txt') though it is the person's own content.
MERGE_BASE = "1" * 40
#: A head a person pushed that is not the pinned head nor a round's.
STRAY = "5" * 40
#: The person's hand merge of the base.
HAND_MERGE = "7" * 40
_FILES = {
    MERGE_BASE: {"readme.txt": b"widgets\n", "notes.txt": b"one\ntwo\n"},
    PINNED: {"readme.txt": b"widgets\n", "notes.txt": b"ONE\ntwo\n"},
    merge_world.BASE_TIP: {"readme.txt": b"widgets\n", "notes.txt": b"one\nTWO\n"},
    STRAY: {"readme.txt": b"widgets, stray\n", "notes.txt": b"one\ntwo\n"},
}
#: The person's resolution: neither parent's notes.txt.
RESOLVED = {"readme.txt": b"widgets\n", "notes.txt": b"ONE\nTWO\n"}


def _blob_sha(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()[:40]


def _serve_tree(world: MergeWorld, commit_sha: str, files: dict[str, bytes]) -> str:
    """Serve `files` as `commit_sha`'s tree, every blob in it, and return the
    tree's sha (test_merge_action.py's `_serve_tree`)."""
    import base64
    entries = []
    for path, content in sorted(files.items()):
        sha = _blob_sha(content)
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha,
                        "size": len(content)})
        world.github.route("GET", f"{API}/git/blobs/{sha}", (200, {}, {
            "sha": sha, "size": len(content), "encoding": "base64",
            "content": base64.b64encode(content).decode()}))
    tree = _blob_sha(commit_sha.encode())
    world.github.route("GET", f"{API}/git/trees/{tree}", (200, {}, {
        "sha": tree, "tree": entries, "truncated": False}))
    return tree


def _serve_commit(world: MergeWorld, sha: str, parents: list[str], files: dict[str, bytes], *,
                  committer: dict[str, str] | None = None, login: str = PAT_LOGIN,
                  message: str = "Merge main, keeping both changes to notes.txt") -> None:
    """Serve `GET .../commits/<sha>` with its tree, as `_person_commit` does."""
    committer = committer or PAT
    world.github.route("GET", f"{API}/commits/{sha}", (200, {}, {
        "sha": sha, "parents": [{"sha": p} for p in parents],
        "commit": {"committer": dict(committer), "author": dict(committer), "message": message,
                   "verification": {"verified": False},
                   "tree": {"sha": _serve_tree(world, sha, files)}},
        "committer": _account(login), "author": _account(login)}))


def _person_merge(world: MergeWorld, *, first: str = PINNED,
                  parents: list[str] | None = None, on_main: bool = True,
                  **commit: Any) -> str:
    """A person's hand merge of main's tip into `first`, resolving the
    conflict on notes.txt, with everything the walk and the proof read
    served (test_merge_action.py's `_person_merge`)."""
    for sha in (MERGE_BASE, PINNED, merge_world.BASE_TIP):
        _serve_commit(world, sha, [] if sha == MERGE_BASE else [MERGE_BASE], _FILES[sha],
                      committer=merge_world.GITHUB_COMMITTER, login="web-flow")
    if first == STRAY:
        _serve_commit(world, STRAY, [MERGE_BASE], _FILES[STRAY])
    second = merge_world.BASE_TIP
    world.github.route("GET", f"{API}/compare/{first}...{second}", (200, {}, {
        "status": "diverged", "merge_base_commit": {"sha": MERGE_BASE}}))
    world.on_base(second, status="behind" if on_main else "diverged")
    _serve_commit(world, HAND_MERGE, parents or [first, second], RESOLVED, **commit)
    world.serve_checks(HAND_MERGE)
    return HAND_MERGE


def test_a_persons_merge_the_proof_refuses_ends_superseded_by_person_push(tmp_path):
    """#3601: the pull request's author merged main into the pinned head and
    resolved the conflict on notes.txt by hand. The purity proof refuses it
    -- both parents change notes.txt -- and until 2026-11-12 the step ended
    head_moved and failure-review filed it. A person on the pull request
    made that merge on top of an accepted head: it is their content (#2505),
    so the step is superseded, still merges nothing, and keeps the proof's
    reason in the commit record."""
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_merge(world)
    outcome = merge.run_merge(world.context())

    refusal = outcome.summary["refusal"]
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH, refusal
    assert refusal["person"] == PAT_LOGIN
    assert refusal["commit"]["sha"] == HAND_MERGE
    assert refusal["commit"]["first_parent_accepted"] is True
    assert refusal["commit"]["why"] == "both parents change notes.txt"
    assert world.merge_calls() == []
    assert world.update_calls() == []
    # FROZEN: until the contract request lands, the cause stays MERGE_REFUSED.
    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED


def test_a_branch_mates_refused_merge_still_ends_head_moved(tmp_path):
    """The controls: the same merge by a login not on the pull request, under
    the platform's own committer address, or carrying the worker's line for
    a task that is no person's lane, is not a person's push."""
    cases: dict[str, dict[str, Any]] = {
        "mallory": dict(committer=MALLORY, login=MALLORY_LOGIN),
        "worker_line": dict(message="Merge main\n\n" + merge.WORKER_COMMIT_LINE + "task_abc."),
    }
    for email in sorted(merge.PLATFORM_COMMITTER_EMAILS):
        cases[email] = dict(committer={"name": "bot", "email": email})
    for name, kwargs in cases.items():
        world = _world(tmp_path / name.replace("@", "_"))
        world.pr["head"]["sha"] = _person_merge(world, **kwargs)
        outcome = merge.run_merge(world.context())
        assert outcome.summary["refusal"]["code"] == "head_moved", name
        assert "person" not in outcome.summary["refusal"], name
        assert world.merge_calls() == [] and world.update_calls() == [], name


def test_a_persons_merge_onto_an_unaccepted_head_still_ends_head_moved(tmp_path):
    """The author's merge of main into a head the step never accepted: the
    merge carries more than the person's resolution, so it stays head_moved."""
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_merge(world, first=STRAY)
    outcome = merge.run_merge(world.context())
    refusal = outcome.summary["refusal"]
    assert refusal["code"] == "head_moved"
    assert refusal["commit"]["why"] == "its first parent is not an accepted head"
    assert world.merge_calls() == []


def test_a_persons_merge_whose_second_parent_is_off_the_base_still_ends_head_moved(tmp_path):
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_merge(world, on_main=False)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert outcome.summary["refusal"]["commit"]["why"] == "its second parent is not on main"
    assert world.merge_calls() == []


def test_a_persons_octopus_merge_still_ends_head_moved(tmp_path):
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_merge(
        world, parents=[PINNED, merge_world.BASE_TIP, MERGE_BASE])
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert world.merge_calls() == []


def test_a_person_push_while_mergeability_is_read_ends_superseded(tmp_path):
    """merge.py's second head_moved: the head moves between the checks and
    the mergeability reread. A person's push there is superseded too."""
    world = _world(tmp_path)
    _person_commit(world)
    reads = {"n": 0}

    def pull(_seen):
        reads["n"] += 1
        body = dict(world.pr)
        body["head"] = dict(world.pr["head"])
        if reads["n"] == 1:
            body["mergeable"] = None
        else:
            body["head"]["sha"] = HEAD
        return 200, {}, body

    world.github.route("GET", PR, pull)
    outcome = merge.run_merge(world.context())
    refusal = outcome.summary["refusal"]
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH, refusal
    assert refusal["person"] == PAT_LOGIN
    assert world.merge_calls() == []


def test_a_branch_mates_push_while_mergeability_is_read_stays_head_moved(tmp_path):
    world = _world(tmp_path)
    _person_commit(world, committer=MALLORY, committer_account=_account(MALLORY_LOGIN),
                   author_account=_account(MALLORY_LOGIN))
    reads = {"n": 0}

    def pull(_seen):
        reads["n"] += 1
        body = dict(world.pr)
        body["head"] = dict(world.pr["head"])
        if reads["n"] == 1:
            body["mergeable"] = None
        else:
            body["head"]["sha"] = HEAD
        return 200, {}, body

    world.github.route("GET", PR, pull)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"

def test_a_persons_refused_merge_while_mergeability_is_read_ends_superseded(tmp_path):
    """#3601 at the second head_moved: the author's refused hand merge onto
    the accepted head lands while mergeability is read."""
    world = _world(tmp_path)
    _person_merge(world)
    reads = {"n": 0}

    def pull(_seen):
        reads["n"] += 1
        body = dict(world.pr)
        body["head"] = dict(world.pr["head"])
        if reads["n"] == 1:
            body["mergeable"] = None
        else:
            body["head"]["sha"] = HAND_MERGE
        return 200, {}, body

    world.github.route("GET", PR, pull)
    outcome = merge.run_merge(world.context())
    refusal = outcome.summary["refusal"]
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH, refusal
    assert refusal["person"] == PAT_LOGIN
    assert refusal["commit"]["why"] == "both parents change notes.txt"
    assert world.merge_calls() == [] and world.update_calls() == []



def test_the_platform_identities_are_the_workers_own():
    """What `PLATFORM_COMMITTER_EMAILS` and `WORKER_COMMIT_LINE` restate."""
    from agent_worker.config import WorkerConfig
    from agent_worker.gitidentity import BOT_GIT_IDENTITY

    assert merge.PLATFORM_COMMITTER_EMAILS == frozenset(
        {WorkerConfig.git_author_email.lower(), BOT_GIT_IDENTITY[1].lower()})
    import inspect

    from agent_worker.lifecycle import Worker
    source = inspect.getsource(Worker._worker_commit_message)
    assert f"{merge.WORKER_COMMIT_LINE}{{self.cfg.task_id}}." in source


def test_failure_review_skips_the_code_the_worker_writes():
    from swarm_api import failuredigest

    assert failuredigest.SUPERSEDED_BY_PERSON_PUSH == merge.SUPERSEDED_BY_PERSON_PUSH


def test_a_lanes_writer_marks_are_the_ones_swarm_api_skips():
    """What `LANE_WRITER_MARKS` restates: `cifix.live_plain_continuation`'s
    writers with a gate of their own, which are never a person's plain lane."""
    from swarm_api import cifix

    assert set(merge.LANE_WRITER_MARKS) == {*cifix.PR_WRITER_KINDS, cifix.ISSUE_RUN_METADATA_KEY}

"""A person's push to a merge step's pull request supersedes the step (#2505).

BGD's decision, 2026-10-27 (issue triage, epic #2525): "A distinct
non-failure end: superseded_by_person_push (not FAILED) when the committer is
not the platform identity, so failure-review stops filing it; a foreign head
from an unknown branch-mate still refuses."

The refusal itself is unchanged: `merge._accept` still takes no head a person
made of content, and nothing merges. What changes is the code the step ends
with. A head that is not a merge, that the platform did not commit, and that
GitHub links to a person on the pull request (its author or an assignee) ends
`superseded_by_person_push`; every other head that is not accepted ends
`head_moved` as before. `EndCause` is frozen, so until contract request
"SUPERSEDED_BY_PERSON_PUSH" lands the step still ends FAILED/MERGE_REFUSED,
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


def test_a_persons_merge_the_proof_refuses_still_ends_head_moved(tmp_path):
    """BGD's decision is about a person's content commit. A two-parent merge
    the purity proof refuses stays head_moved: the proof may refuse what git
    accepts, and that is the platform's to look at."""
    world = _world(tmp_path)
    world.pr["head"]["sha"] = _person_commit(world, parents=[PINNED, merge_world.BASE_TIP])
    world.on_base(merge_world.BASE_TIP)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"


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

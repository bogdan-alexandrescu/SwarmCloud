"""A push from a clone reset to a pull request head that does not descend from
its base sends only the task's own commits (#2413).

THE DEFECT, REPRODUCED. Product Oracle's task_7e644638768e4a8cb57a cloned
`repository_ref` A, ran `git fetch origin B && git reset --hard B` in its
shell (B a pull request head sharing only an older ancestor C with A),
committed its fix and called `git_push_branch`. The push lists the commits it
sends as `rev-list HEAD ^<origin.haves>`, and `origin.haves` held only A: a
plain `git fetch` teaches the relay nothing. The publish repository is
shallow AT A (`prepare_publish_repo` writes the recorded base as the upload
side's shallow boundary), so `^A` excludes A alone, never C or anything
beneath it, and every commit of B's history counted as "new". The first of
them by somebody else was refused `foreign_author`: 7fb1ee81, old history
long on origin/main.

THE FIX, HELD HERE. Before it refuses, the relay asks swarm-api which of the
foreign commits origin already has (`git_fetch` with `confirm`, answered from
the forge, never from the clone) and excludes exactly those and their
history. A commit origin does not have is still refused, whatever ref the
agent wrote under `refs/remotes/origin/` or `refs/swarm/fetched/`, and the
refusal now says whether origin has it.

Real git against bare repositories on disk; swarm-api is test_gittools's
`FakeApi`, whose `confirm` answers from the bare origin.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gittools

from test_gittools import (
    _ENV,
    OWN_BRANCH,
    PERSON,
    World,
    _bundle_of,
    _commit,
    _commits_in,
    _git,
    _prerequisites,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: Who wrote origin's history: not this task.
ORACLE = ("Product Oracle", "oracle@example.invalid")
MALLORY = ("Mallory", "mallory@example.invalid")


class Diverged:
    """origin: main = OLD - C - A, and `pr` = C - B1 - B. The clone is of A
    (`--depth 1` when shallow, so its boundary hides C), and the agent then
    `git fetch origin <B> && git reset --hard <B>`s in its own shell."""

    def __init__(self, tmp_path: Path, *, shallow: bool) -> None:
        w = World(tmp_path, shallow=shallow)
        seed = w.seed
        self.old = _commit(seed, "docs/design.md", "design\n", "docs: design", who=ORACLE)
        self.c = _commit(seed, "src/core.py", "core = 1\n", "core", who=ORACLE)
        _git(seed, "push", "-q", str(w.origin), "main")
        _git(seed, "checkout", "-q", "-b", "pr")
        self.b1 = _commit(seed, "src/feature.py", "feature = 1\n", "feature", who=ORACLE)
        self.b = _commit(seed, "src/feature.py", "feature = 2\n", "feature, reviewed",
                         who=ORACLE)
        _git(seed, "push", "-q", str(w.origin), "pr")
        _git(seed, "checkout", "-q", "main")
        self.a = _commit(seed, "src/other.py", "other = 1\n", "main moves on", who=ORACLE)
        _git(seed, "push", "-q", str(w.origin), "main")
        # What GitHub serves: any reachable sha, asked for by name.
        _git(w.origin, "config", "uploadpack.allowAnySHA1InWant", "true")

        # The worker's clone, of `repository_ref` A, and its relay.
        shutil.rmtree(w.clone)
        depth = ["--depth", "1"] if shallow else []
        source = f"file://{w.origin}" if shallow else str(w.origin)
        subprocess.run(["git", "clone", "-q", *depth, source, str(w.clone)], check=True,
                       env=_ENV)
        assert _git(w.clone, "rev-parse", "HEAD") == self.a
        assert (w.clone / ".git" / "shallow").exists() == shallow
        w.base = self.a
        w.cfg = dataclasses.replace(w.cfg, clone_base=self.a)
        old = w.tools
        w.tools = gittools.GitTools(w.cfg, key=w.key, api=old._api, clone=w.clone,
                                    private_dir=old.private_dir, logs_dir=old.logs_dir,
                                    logger=old.log)
        w.tools.prepare(w.work)
        self.w = w

        # The step, in its own shell: plain git, so the relay learns nothing.
        _git(w.clone, "fetch", "-q", "origin", self.b)
        _git(w.clone, "reset", "-q", "--hard", self.b)

    def push(self, request_id: str = "p1") -> dict[str, Any]:
        return self.w.spool("git_push_branch", {}, request_id=request_id)

    def confirm_calls(self) -> list[list[str]]:
        return [c["body"]["confirm"] for c in self.w.api.calls
                if c["path"].endswith("/git_fetch") and "confirm" in c["body"]]

    def push_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.w.api.calls if c["path"].endswith("/git_push_branch")]


@pytest.fixture(params=[False, True], ids=["full-clone", "shallow-clone"])
def diverged(request, tmp_path: Path) -> Diverged:
    return Diverged(tmp_path, shallow=request.param)


# --------------------------------------------------------------------------
# (a) origin's history is not the task's to answer for
# --------------------------------------------------------------------------


def test_a_push_from_a_pr_head_off_the_base_sends_exactly_the_tasks_own_commit(
    diverged: Diverged, tmp_path: Path
):
    w = diverged.w
    own = _commit(w.clone, "src/fix.py", "fix = 1\n", "the fix")
    answer = diverged.push()
    # MUTATION: dropping the confirmation leaves B's history in the range, and
    # the first commit of it by Product Oracle is refused foreign_author.
    assert answer.get("result") == {"branch": OWN_BRANCH, "old_sha": None, "new_sha": own}, answer
    # Origin was asked about the newest foreign commit only: B covers the rest.
    assert diverged.confirm_calls() == [[diverged.b]]
    bundle = _bundle_of(diverged.push_calls()[-1], tmp_path / "push.bundle")
    # MUTATION: excluding B's history without B as the bundle's prerequisite
    # (or bundling over A alone) changes both.
    assert _prerequisites(bundle, w.origin) == [diverged.b]
    assert _commits_in(bundle, tmp_path, w.origin, exclude=[diverged.b]) == [own]
    assert diverged.push_calls()[-1]["body"]["commits"] == 1
    assert _git(w.origin, "rev-parse", f"refs/heads/{OWN_BRANCH}") == own


def test_a_confirmed_sha_is_a_have_for_the_next_push_without_asking_again(
    diverged: Diverged
):
    w = diverged.w
    _commit(w.clone, "src/fix.py", "fix = 1\n", "the fix")
    assert "result" in diverged.push("p1")
    second = _commit(w.clone, "src/fix.py", "fix = 2\n", "the fix, again")
    answer = diverged.push("p2")
    assert answer["result"]["new_sha"] == second
    # The pushed tip covers B now; nothing foreign is in the range to ask about.
    assert diverged.confirm_calls() == [[diverged.b]]


# --------------------------------------------------------------------------
# (b) a commit origin does not have is still somebody else's
# --------------------------------------------------------------------------


def test_another_authors_commit_only_in_the_clone_is_still_refused_foreign_author(
    diverged: Diverged
):
    w = diverged.w
    theirs = _commit(w.clone, "src/theirs.py", "theirs = 1\n", "not on origin", who=MALLORY)
    _commit(w.clone, "src/fix.py", "fix = 1\n", "the fix")
    refused = diverged.push()["refused"]
    # MUTATION: excluding every foreign commit (not only the confirmed ones)
    # pushes Mallory's commit.
    assert refused["code"] == "foreign_author", refused
    # MUTATION: naming the oldest foreign commit (Product Oracle's, which
    # origin has) instead of the one origin lacks fails here.
    assert refused["commit"] == theirs[:12]
    assert refused["on_origin"] is False
    assert "origin does not have it" in refused["message"]
    assert theirs[:12] in refused["message"]
    assert diverged.confirm_calls() == [[theirs]]
    assert diverged.push_calls() == []


# --------------------------------------------------------------------------
# (c) a ref the agent wrote vouches for nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("onto", ["pr-head", "base"])
def test_a_ref_the_agent_wrote_under_origin_vouches_for_nothing(diverged: Diverged, onto: str):
    w = diverged.w
    if onto == "base":
        _git(w.clone, "reset", "-q", "--hard", diverged.a)
    theirs = _commit(w.clone, "src/theirs.py", "theirs = 1\n", "not on origin", who=MALLORY)
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/pr",
                f"refs/remotes/origin/{OWN_BRANCH}", f"refs/swarm/fetched/{theirs}"):
        _git(w.clone, "update-ref", ref, theirs)
    _git(w.clone, "pack-refs", "--all")
    _commit(w.clone, "src/fix.py", "fix = 1\n", "the fix")
    refused = diverged.push()["refused"]
    # MUTATION: counting the clone's origin refs as haves (unconfirmed) lets
    # Mallory's commit through.
    assert refused["code"] == "foreign_author", refused
    assert refused["commit"] == theirs[:12] and refused["on_origin"] is False
    assert diverged.push_calls() == []


def test_a_confirmation_never_widens_past_the_shas_the_relay_asked_about(
    diverged: Diverged, monkeypatch
):
    """The answer is read as a subset of the question. A sha the relay did not
    ask about -- here one that would cover Mallory's commit -- is ignored."""
    w = diverged.w
    theirs = _commit(w.clone, "src/theirs.py", "theirs = 1\n", "not on origin", who=MALLORY)
    _git(w.clone, "checkout", "-q", "-b", "side")
    cover = _commit(w.clone, "src/side.py", "side = 1\n", "a side commit", who=MALLORY)
    _git(w.clone, "checkout", "-q", "-")
    _commit(w.clone, "src/fix.py", "fix = 1\n", "the fix")
    real = w.api.git_fetch

    def lying(body: dict[str, Any]) -> dict[str, Any]:
        if "confirm" in body:
            return {"confirmed": [cover]}
        return real(body)

    monkeypatch.setattr(w.api, "git_fetch", lying)
    refused = diverged.push()["refused"]
    # MUTATION: taking `confirmed` as given excludes `cover`'s history, which
    # holds Mallory's commit, and the push goes through.
    assert refused["code"] == "foreign_author", refused
    assert refused["commit"] == theirs[:12]
    assert diverged.push_calls() == []


def test_a_platform_that_cannot_confirm_leaves_the_refusal_saying_so(
    diverged: Diverged, monkeypatch
):
    """A swarm-api from before `confirm` 422s the field. The push is refused
    as it was, and the refusal says origin could not be asked."""
    w = diverged.w
    _commit(w.clone, "src/fix.py", "fix = 1\n", "the fix")
    real = w.tools.call

    def old_api(tool: str, fields: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        if tool == "git_fetch" and "confirm" in fields:
            raise gittools.GitToolRefused("bad_request", "Extra inputs are not permitted")
        return real(tool, fields, request_id=request_id)

    monkeypatch.setattr(w.tools, "call", old_api)
    refused = diverged.push()["refused"]
    assert refused["code"] == "foreign_author"
    assert refused["on_origin"] is None
    assert "could not be asked" in refused["message"]
    assert diverged.push_calls() == []


def test_a_push_whose_commits_are_all_the_tasks_own_asks_origin_nothing(tmp_path: Path):
    """The control: the common push makes no confirmation call."""
    w = World(tmp_path)
    own = _commit(w.clone, "a.txt", "a\n", "one", who=PERSON)
    assert w.spool("git_push_branch", {})["result"]["new_sha"] == own
    assert [c["path"].rsplit("/", 1)[1] for c in w.api.calls] == ["git_push_branch"]

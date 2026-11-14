"""A shallow clone's git_fetch reaches a merge base, and its merge publishes (#2901).

docs/design/git-fetch-shallow-clones.md, lane W. The clone is `--depth 1` of a
pull request's head H, whose base `main` moved on. The worker sends the
boundary it recorded before the agent ran (§5.6); swarm-api -- here its real
`GitExec`, behind the fake route of `test_gittools` -- deepens in bounded
rounds and answers the bundle with `shallow`, `unshallow` and `merge_base`;
the worker writes `.git/shallow` and then applies the bundle (§5.7). The agent
then merges `origin/main` without `--allow-unrelated-histories`, and the merge
publishes through the worker's clean repository.

Local `file://` repositories; no network, no credentials.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import gittools
from agent_worker.logs import build_logger
from swarm_api.gitexec import GitExec

from test_gittools import _ENV, OWN_BRANCH, PERSON, TASK, FakeApi, _commit, _dir, _git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class ShallowApi(FakeApi):
    """The route's shallow answer (swarm-api `gittools._fetch`), from the real
    `GitExec` against the bare origin; any other fetch is the base fake's."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_rounds = 8
        self.tamper: Any = None

    def git_fetch(self, body: dict[str, Any]) -> dict[str, Any]:
        if "shallow" not in body or "depth" in body:
            return super().git_fetch(body)
        fetched = GitExec(scratch_root=self.scratch / "gitexec", local_remotes=True,
                          shallow_max_rounds=self.max_rounds) \
            .fetch_bundle(url=f"file://{self.origin}", token=secrets.token_hex(8),
                          ref=body["ref"], haves=body["haves"], shallow=body["shallow"])
        answer: dict[str, Any] = {
            "resolved_sha": fetched.sha, "shallow": list(fetched.boundary),
            "unshallow": list(fetched.unshallow), "merge_base": fetched.merge_base,
            "deepened_to": fetched.depth}
        if fetched.bundle is not None:
            answer.update(bundle=base64.b64encode(fetched.bundle).decode(),
                          bundle_sha256=hashlib.sha256(fetched.bundle).hexdigest())
        if self.tamper is not None:
            self.tamper(answer)
        return answer


class PullRequest:
    """origin: `main` of `commits` commits; `feature` = main~`behind` plus H,
    by the person the task commits as. The clone is `--depth 1` of feature."""

    def __init__(self, tmp_path: Path, *, commits: int = 12, behind: int = 4,
                 seed_name: str = "seed") -> None:
        self.origin = tmp_path / "origin.git"
        seed = tmp_path / seed_name
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, env=_ENV)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=_ENV)
        for i in range(commits):
            _commit(seed, "log.txt", f"line {i}\n", f"main {i}",
                    who=("Someone Else", "else@example.invalid"))
        self.fork = _git(seed, "rev-parse", f"HEAD~{behind}")
        self.main = _git(seed, "rev-parse", "HEAD")
        _git(seed, "checkout", "-q", "-b", "feature", self.fork)
        self.head = _commit(seed, "feature.txt", "mine\n", "the pull request", who=PERSON)
        _git(seed, "push", "-q", str(self.origin), "main", "feature")
        self.clone = tmp_path / "work" / "repo"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "feature",
                        f"file://{self.origin}", str(self.clone)], check=True, env=_ENV,
                       capture_output=True)
        self.work = tmp_path / "work"
        scratch = tmp_path / "api"
        scratch.mkdir()
        key = children_mod.AttemptKey()
        self.api = ShallowApi(self.origin, scratch, key.public)
        client = children_mod.ChildApi("https://api.invalid", token_fetcher=lambda _a: "id",
                                       opener=self.api.open)
        cfg = gittools.GitToolsConfig(
            tenant_id="acme", task_id=TASK, attempt_id="att_1", lease_id="lease_1",
            generation=3, repository="acme/widgets", clone_base=self.head,
            pushable=(OWN_BRANCH,), author=PERSON, protected=("main",))
        self.tools = gittools.GitTools(
            cfg, key=key, api=client, clone=self.clone, private_dir=tmp_path / "private",
            logs_dir=_dir(tmp_path / "logs"),
            logger=build_logger(task_id=TASK, attempt_id="att_1", tenant_id="acme",
                                generation=3, runner_profile="mock", stream=io.StringIO()))
        self.tools.prepare(self.work)

    def spool(self, tool: str, arguments: dict[str, Any], request_id: str = "r1") -> dict:
        from agent_worker.gitmcp import server as shim
        import json

        shim.write_request(gittools.GitTools.spool(self.work), request_id, tool, arguments)
        self.tools.tick(self.work)
        response = gittools.GitTools.spool(self.work) / "responses" / f"{request_id}.json"
        return json.loads(response.read_text())

    def shallow_file(self) -> list[str]:
        path = self.clone / ".git" / "shallow"
        return path.read_text().split() if path.exists() else []

    def merge_main(self) -> str:
        _git(self.clone, "-c", f"user.name={PERSON[0]}", "-c", f"user.email={PERSON[1]}",
             "merge", "-q", "--no-ff", "-m", "merge main", "origin/main")
        return _git(self.clone, "rev-parse", "HEAD")


def test_fetch_sends_the_recorded_boundary_not_the_clones_file(tmp_path: Path):
    pr = PullRequest(tmp_path)
    assert pr.tools.origin.boundary == [pr.head]
    # The agent rewrites its shallow file after the worker recorded it.
    forged = hashlib.sha1(os.urandom(8)).hexdigest()
    (pr.clone / ".git" / "shallow").write_text(f"{pr.head}\n{forged}\n")
    pr.spool("git_fetch", {"ref": "main"})
    fetches = [c["body"] for c in pr.api.calls if c["path"].endswith("/git_fetch")]
    assert fetches[-1]["shallow"] == [pr.head]


def test_apply_writes_shallow_then_fetches_and_reaches_the_merge_base(tmp_path: Path):
    pr = PullRequest(tmp_path)
    answer = pr.spool("git_fetch", {"ref": "main"})
    assert "refused" not in answer, answer
    assert answer["result"]["merge_base"] == pr.fork
    # MUTATION: skip the shallow write -> the bundle does not apply.
    assert pr.head not in pr.shallow_file()
    assert _git(pr.clone, "merge-base", "HEAD", "origin/main") == pr.fork
    _git(pr.clone, "fsck", "--connectivity-only", "--no-dangling")
    assert pr.head not in pr.tools.origin.boundary


def test_a_shallow_clone_merging_its_base_publishes(tmp_path: Path):
    """#2901 end to end: a depth-1 clone of a pull request behind its base
    fetches the base through the platform, merges it -- no
    `--allow-unrelated-histories` -- and the merge publishes."""
    pr = PullRequest(tmp_path)
    assert "refused" not in pr.spool("git_fetch", {"ref": "main"}, request_id="f1")
    merge = pr.merge_main()
    parents = _git(pr.clone, "rev-list", "--parents", "-n", "1", merge).split()[1:]
    assert parents == [pr.head, pr.main]
    answer = pr.spool("git_push_branch", {}, request_id="p1")
    assert "refused" not in answer, answer
    assert _git(pr.origin, "rev-parse", f"refs/heads/{OWN_BRANCH}") == merge


def test_failed_apply_restores_the_shallow_file(tmp_path: Path):
    """The shallow file is written first (H unshallowed, main's cut-off
    added) and the bundle then fails to apply -- here it is not a bundle at
    all -- so the old file is put back, and the clone is as it was."""
    pr = PullRequest(tmp_path, commits=70)
    before = (pr.clone / ".git" / "shallow").read_bytes()
    written: list[list[str]] = []
    real_write = pr.tools._shallow_write

    def recording(lines: list[str]) -> None:
        written.append(list(lines))
        real_write(lines)

    pr.tools._shallow_write = recording  # type: ignore[method-assign]

    def broken(answer: dict[str, Any]) -> None:
        raw = b"# v2 git bundle\n" + secrets.token_bytes(64)
        answer.update(bundle=base64.b64encode(raw).decode(),
                      bundle_sha256=hashlib.sha256(raw).hexdigest())

    pr.api.tamper = broken
    answer = pr.spool("git_fetch", {"ref": "main"})
    assert "refused" in answer, answer
    # MUTATION: no restore -> the file keeps the new boundary.
    assert len(written) == 2 and pr.head not in written[0], written
    assert (pr.clone / ".git" / "shallow").read_bytes() == before
    assert pr.tools.origin.boundary == [pr.head]
    # The control: the same fetch, answered whole, applies.
    pr.api.tamper = None
    assert pr.spool("git_fetch", {"ref": "main"}, request_id="r2")["result"]["merge_base"] \
        == pr.fork


@pytest.mark.parametrize("kind", ["symlink", "gitfile"])
def test_refuses_a_symlinked_or_gitfile_git_dir(tmp_path: Path, kind: str):
    pr = PullRequest(tmp_path)
    real = tmp_path / "elsewhere.git"
    shutil.move(str(pr.clone / ".git"), str(real))
    if kind == "symlink":
        os.symlink(real, pr.clone / ".git")
    else:
        (pr.clone / ".git").write_text(f"gitdir: {real}\n")
    before = (real / "shallow").read_bytes()
    answer = pr.spool("git_fetch", {"ref": "main"})
    assert answer["refused"]["code"] == "fetch_failed", answer
    assert (real / "shallow").read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("shallow", ["abc123"]),
    ("shallow", ["--upload-pack=x"]),
    ("shallow", [f"{i:040x}" for i in range(gittools.MAX_SHALLOW_ANSWER + 1)]),
    ("merge_base", "not-a-sha"),
])
def test_rejects_malformed_shallow_lines_from_the_api(tmp_path: Path, field: str, value: Any):
    pr = PullRequest(tmp_path)
    before = (pr.clone / ".git" / "shallow").read_bytes()
    pr.api.tamper = lambda answer: answer.__setitem__(field, value)
    answer = pr.spool("git_fetch", {"ref": "main"})
    assert answer["refused"]["code"] == "api_unavailable", answer
    assert (pr.clone / ".git" / "shallow").read_bytes() == before


def test_rejects_unshallow_outside_the_boundary_sent(tmp_path: Path):
    pr = PullRequest(tmp_path)
    before = (pr.clone / ".git" / "shallow").read_bytes()
    pr.api.tamper = lambda answer: answer.__setitem__("unshallow", [pr.main])
    answer = pr.spool("git_fetch", {"ref": "main"})
    assert answer["refused"]["code"] == "api_unavailable", answer
    assert (pr.clone / ".git" / "shallow").read_bytes() == before


def test_rebase_onto_a_shallow_result(tmp_path: Path):
    pr = PullRequest(tmp_path)
    _commit(pr.clone, "src/app.py", "x = 1\n", "mine", who=PERSON)
    result = pr.spool("git_rebase_onto", {"ref": "main"}, request_id="rb")["result"]
    assert result["onto"] == pr.main and result["conflicts"] == []
    # H and the agent's commit, replayed onto main's tip.
    assert _git(pr.clone, "rev-parse", "HEAD~2") == pr.main
    assert _git(pr.clone, "log", "--format=%s", "-2").split("\n") == ["mine", "the pull request"]


def test_rebase_onto_refuses_without_a_merge_base(tmp_path: Path):
    pr = PullRequest(tmp_path / "rebase")
    pr.api.tamper = lambda answer: answer.__setitem__("merge_base", None)
    head = _git(pr.clone, "rev-parse", "HEAD")
    answer = pr.spool("git_rebase_onto", {"ref": "main"}, request_id="rb")
    assert answer["refused"]["code"] == "too_large", answer
    assert _git(pr.clone, "rev-parse", "HEAD") == head
    # A plain fetch delivers it and says there is no merge base (Q2 = A).
    other = PullRequest(tmp_path / "fetch")
    other.api.tamper = pr.api.tamper
    fetched = other.spool("git_fetch", {"ref": "main"}, request_id="f2")["result"]
    assert fetched["merge_base"] is None and "no commit" in fetched["note"]
    assert _git(other.clone, "rev-parse", "origin/main") == other.main

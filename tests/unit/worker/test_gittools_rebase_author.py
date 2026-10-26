"""Git-tools acceptance phase 2, reproduced: a continuation rebases the lane's
branch onto main through a planted conflict and pushes (#1805).

WHAT ACCEPTANCE RUNS (`scripts/acceptance/groups/git-tools.sh`): a service
account submits both tasks, so `commit_identity` names nobody and answers
`BOT_GIT_IDENTITY`. The LANE's agent writes `planted.txt` and the worker's
end-of-task publish folds its work into one commit written as
`Worker._publish_identity()` -- which, for a task naming nobody, is the
worker's own `git_author_*`, NOT `BOT_GIT_IDENTITY`. The CONTINUATION (another
task id, `metadata.dispatch.continues` = the lane, same submitter) clones the
lane's branch, `git_fetch`es it, `git_rebase_onto` main, resolves the
conflict with `git rebase --continue` under the runner's GIT_COMMITTER_* --
`commit_identity(task)` again -- and pushes through the relay `ChildPath`
builds.

THE CAUSE, MEASURED HERE: the rebased commit keeps the lane's author (the
worker's `git_author_*`) and takes the continuation's committer
(`BOT_GIT_IDENTITY`). Each identity alone is the task's own -- the relay's
`author` and `also_own` -- but `gitops._foreign` accepts only a WHOLE pair,
so the commit was refused `foreign_author` as "a pair the worker never
writes". Every rebase does write exactly that pair: git keeps a replayed
commit's author and makes the rebaser its committer.

Real git against a bare repository on disk; swarm-api is faked at the
`ChildApi.call` boundary, applying bundles the way test_gittools.FakeApi does.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import gitops, gittools
from agent_worker import workspace as workspace_mod
from agent_worker.gitidentity import BOT_GIT_IDENTITY, commit_identity, git_identity_env
from agent_worker.gitmcp import server as shim
from agent_worker.lifecycle import Worker
from agent_worker.logs import build_logger
from swarm_api import childkey as api_childkey

from test_gittools import FakeApi

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

LANE = "task_" + "1" * 20
CONTINUATION = "task_" + "2" * 20
LANE_BRANCH = f"swarm/{LANE}"
PLANTED = "tests/acceptance/fixtures/git-tools/planted.txt"
LANE_FILE = "tests/acceptance/fixtures/git-tools/lane.txt"
MAIN_LINE = "main: the default branch wrote this line"
LANE_LINE = "lane: the pull request wrote this line"
BOT_NAME, BOT_EMAIL = "swarmcloud agent", "swarmcloud-agent@users.noreply.github.com"
ROBOT = "swarm-accept@project.iam.gserviceaccount.com"
SUBMITTER = "ada@example.invalid"
MALLORY = ("Mallory", "mallory@example.invalid")

#: The submitters acceptance and a person submit as. `service account` is
#: what acceptance runs; `display name` is the control that pushed before.
SUBMITTERS: dict[str, dict[str, Any]] = {
    "service account": {"submitted_by": ROBOT},
    "service account with the bot's record": {
        "submitted_by": ROBOT,
        "git_identity": {"name": BOT_GIT_IDENTITY[0], "email": BOT_GIT_IDENTITY[1]}},
    "display name": {"submitted_by": SUBMITTER,
                     "git_identity": {"name": "Ada Lovelace", "email": SUBMITTER}},
}

_ENV = {k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG"))}
_ENV.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**_ENV, **(env or {})}).stdout.strip()


def _as(who: tuple[str, str], committer: tuple[str, str] | None = None) -> dict[str, str]:
    committer = committer or who
    return {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1],
            "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}


def _task(task_id: str, submitter: str, *, continues: str | None = None) -> dict[str, Any]:
    spec = SUBMITTERS[submitter]
    dispatch: dict[str, Any] = {"strategy": "direct-pr"}
    if "git_identity" in spec:
        dispatch["git_identity"] = dict(spec["git_identity"])
    if continues:
        dispatch["continues"] = continues
    return {"id": task_id, "submitted_by": spec["submitted_by"],
            "metadata": {"dispatch": dispatch}}


def _worker_cfg(task_id: str) -> SimpleNamespace:
    """The `WorkerConfig` fields `ChildPath` and `_publish_identity` read."""
    return SimpleNamespace(
        child_submit_retry_seconds=5, max_child_requests_per_tick=4,
        max_child_request_bytes=262144, tenant_id="acme", attempt_id="att_c1",
        lease_id="lease_c1", generation=1, task_id=task_id, swarm_api_url=None,
        swarm_api_audience=None, child_nonce="a1b2c3d4-test-nonce-not-a-secret",
        runner_profile="claude-code", git_branch_prefix="swarm/",
        git_author_name=BOT_NAME, git_author_email=BOT_EMAIL, git_clone_timeout_seconds=60,
    )


class Routes:
    """swarm-api's worker-only routes: the key registration, then the git
    tools answered from the bare origin with `test_gittools.FakeApi`."""

    def __init__(self, origin: Path, scratch: Path) -> None:
        self.public_key: str | None = None
        self.fake = FakeApi(origin, scratch, "")
        self.git_calls: list[str] = []

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        if path.endswith("/child-key"):
            self.public_key = json.loads(body).get("public_key")
            return 201, {"registered": "key"}
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        message = api_childkey.request_message(method, path, body or b"", stamp)
        assert self.public_key and proof and api_childkey.verify_proof(self.public_key, proof,
                                                                        message)
        tool = path.rsplit("/", 1)[1]
        self.git_calls.append(tool)
        return 200, getattr(self.fake, tool)(json.loads(body))


class Phase2:
    """origin, the lane's published branch, main moved, and the continuation's
    attempt with the relay `ChildPath._prepare_git` builds for it."""

    def __init__(self, tmp_path: Path, submitter: str, *,
                 lane_author: tuple[str, str] | None = None,
                 secrets_: tuple[str, ...] = ()) -> None:
        self.tmp = tmp_path
        self.log = build_logger(task_id=CONTINUATION, attempt_id="att_c1", tenant_id="acme",
                                generation=1, runner_profile="claude-code",
                                stream=io.StringIO())
        self.origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, env=_ENV)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=_ENV)
        (seed / "README.md").write_text("base\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base", env=_as(("Seed", "seed@example.invalid")))
        _git(seed, "push", "-q", str(self.origin), "main")

        # Phase 1, the lane: cloned acceptance/git-tools-base (before main's
        # planted.txt), its agent writes planted.txt and lane.txt, and the
        # publish folds that work into one commit written as the lane
        # worker's `_publish_identity()`.
        self.lane_task = _task(LANE, submitter)
        lane = tmp_path / "lane"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(lane)], check=True, env=_ENV)
        lane_base = _git(lane, "rev-parse", "HEAD")
        (lane / PLANTED).parent.mkdir(parents=True)
        (lane / PLANTED).write_text(LANE_LINE + "\n")
        (lane / LANE_FILE).write_text("acceptance-run:unit\n")
        _git(lane, "add", "-A")
        _git(lane, "commit", "-q", "-m", "lane", env=git_identity_env(self.lane_task))
        self.lane_identity = Worker._publish_identity(
            SimpleNamespace(_task=self.lane_task, cfg=_worker_cfg(LANE)))
        name, email = lane_author or self.lane_identity
        gitops.fold_agent_commits(
            repo=lane, base=lane_base, keep=None, message="git-tools acceptance lane",
            author_name=name, author_email=email, private_dir=_mk(tmp_path / "lane-private"),
            logs_dir=_mk(tmp_path / "lane-logs"), timeout_seconds=60, logger=self.log,
        )
        _git(lane, "push", "-q", str(self.origin), f"HEAD:refs/heads/{LANE_BRANCH}")
        self.lane_tip = _git(lane, "rev-parse", "HEAD")

        # main gains its planted.txt (sandbox-sync.sh's fixture), by someone else.
        (seed / PLANTED).parent.mkdir(parents=True)
        (seed / PLANTED).write_text(MAIN_LINE + "\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "main plants", env=_as(("Someone Else",
                                                                  "else@example.invalid")))
        _git(seed, "push", "-q", str(self.origin), "main")
        self.main_tip = _git(seed, "rev-parse", "HEAD")

        # Phase 2, the continuation: clones the continued branch
        # (`continuation.clone_ref`), in a Workspace laid out as the worker's.
        self.task = _task(CONTINUATION, submitter, continues=LANE)
        self.ws = workspace_mod.create(tmp_path / "attempts", "att_c1")
        self.repo = self.ws.work / "repo"
        subprocess.run(["git", "clone", "-q", "-b", LANE_BRANCH, str(self.origin),
                        str(self.repo)], check=True, env=_ENV)
        self.clone_base = _git(self.repo, "rev-parse", "HEAD")
        from agent_worker.hardening import PROTECTED, MemoryProtection

        self.routes = Routes(self.origin, _mk(tmp_path / "api"))
        self.path = children_mod.ChildPath(
            _worker_cfg(CONTINUATION), logger=self.log,
            memory=MemoryProtection(PROTECTED, "test"), api=self.routes)
        self.path.register(lambda _note: None)
        self.path._registered_secrets = lambda: secrets_  # type: ignore[method-assign]
        self.path.prepare(self.ws.work, task=self.task, checkout=self.repo,
                          repository_url="https://github.com/acme/widgets.git",
                          clone_base=self.clone_base, private_dir=self.ws.private,
                          logs_dir=self.ws.logs, store=None)
        assert self.path._git is not None, self.path.git_unavailable
        # The runner's environment for this task (`git_identity_env`).
        self.agent_env = git_identity_env(self.task)

    def spool(self, tool: str, arguments: dict[str, Any], request_id: str) -> dict[str, Any]:
        root = gittools.GitTools.spool(self.ws.work)
        shim.write_request(root, request_id, tool, arguments)
        self.path._git.tick(self.ws.work)
        return json.loads((root / "responses" / f"{request_id}.json").read_text())

    def rebase_through_the_conflict(self) -> dict[str, Any]:
        """The prompt's steps 1-3: fetch the lane's branch, rebase onto main,
        resolve planted.txt and `git rebase --continue` as the runner would."""
        fetched = self.spool("git_fetch", {"ref": LANE_BRANCH}, "fetch-own")
        assert fetched["result"]["resolved_sha"] == self.lane_tip
        rebased = self.spool("git_rebase_onto", {"ref": "main"}, "rebase")["result"]
        assert rebased["conflicts"] == [PLANTED] and rebased["onto"] == self.main_tip
        (self.repo / PLANTED).write_text(f"{MAIN_LINE}\n{LANE_LINE}\n")
        _git(self.repo, "add", PLANTED, env=self.agent_env)
        _git(self.repo, "rebase", "--continue", env={**self.agent_env, "GIT_EDITOR": "true"})
        return rebased

    def answers(self) -> list[dict[str, Any]]:
        path = self.ws.artifacts / gittools.ANSWERS_ARTIFACT
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _mk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ident(repo: Path, fmt: str) -> str:
    return _git(repo, "log", "-1", f"--format={fmt}")


# --------------------------------------------------------------------------
# The cause, named
# --------------------------------------------------------------------------


def test_the_rebased_commit_keeps_the_lanes_author_and_takes_the_continuations_committer(
    tmp_path: Path,
):
    """The pair the refusal was about: author = the lane worker's
    `git_author_*` (the fold), committer = `BOT_GIT_IDENTITY` (the
    continuation's runner). Neither is foreign alone."""
    world = Phase2(tmp_path, "service account")
    world.rebase_through_the_conflict()
    # MUTATION: if the lane's publish wrote BOT_GIT_IDENTITY (or the rebase
    # rewrote the author), the pair would be whole and this would fail.
    assert world.lane_identity == (BOT_NAME, BOT_EMAIL)
    assert _ident(world.repo, "%an <%ae>") == f"{BOT_NAME} <{BOT_EMAIL}>"
    # MUTATION: committing the rebase as the worker's `git_author_*` instead
    # of the runner's identity makes this read the bot, not BOT_GIT_IDENTITY.
    assert _ident(world.repo, "%cn <%ce>") == f"{BOT_GIT_IDENTITY[0]} <{BOT_GIT_IDENTITY[1]}>"
    # The relay's identities are exactly these two, as `_prepare_git` sets them.
    cfg = world.path._git.cfg
    # MUTATION: configuring the relay's author as `_publish_identity()` (the
    # GITTOOLS-AUTHOR regression) makes this fail.
    assert cfg.author == commit_identity(world.task) == BOT_GIT_IDENTITY
    assert cfg.also_own == ((BOT_NAME, BOT_EMAIL),)


# --------------------------------------------------------------------------
# The phase-2 reproduction pushes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("submitter", list(SUBMITTERS))
def test_a_continuation_rebased_through_the_planted_conflict_pushes_the_lanes_branch(
    tmp_path: Path, submitter: str
):
    world = Phase2(tmp_path, submitter)
    world.rebase_through_the_conflict()
    head = _git(world.repo, "rev-parse", "HEAD")
    answer = world.spool("git_push_branch", {}, "push")
    # MUTATION: dropping the rebase's (own author, this task's committer) pair
    # from `_check_authorship` refuses this foreign_author again (#1805).
    assert "result" in answer, answer
    assert answer["result"]["new_sha"] == head
    assert answer["result"]["branch"] == LANE_BRANCH
    # MUTATION: counting main's commit (authored by someone else) among the
    # commits to push would refuse it; the bundle carries the lane's one commit.
    assert _git(world.origin, "rev-parse", LANE_BRANCH) == head
    assert _git(world.origin, "rev-parse", f"{LANE_BRANCH}~1") == world.main_tip
    assert world.routes.git_calls == ["git_fetch", "git_fetch", "git_push_branch"]
    # MUTATION: recording a successful answer as a refusal would list it here.
    assert world.answers() == []


# --------------------------------------------------------------------------
# An outside identity is still refused, and the refusal says which and why
# --------------------------------------------------------------------------


@pytest.mark.parametrize("submitter", list(SUBMITTERS))
def test_a_lane_commit_authored_by_an_outside_identity_is_still_refused_after_the_rebase(
    tmp_path: Path, submitter: str
):
    """The rebase makes this task the committer of whatever it replays. That
    must not launder an outside author: the author still has to be the task's."""
    world = Phase2(tmp_path, submitter, lane_author=MALLORY)
    world.rebase_through_the_conflict()
    answer = world.spool("git_push_branch", {}, "push")
    # MUTATION: accepting any author under this task's committer (instead of
    # only the task's own authors) lets this push through.
    assert answer["refused"]["code"] == "foreign_author", answer
    # The third is the push asking origin about the foreign commit (#2413).
    assert world.routes.git_calls == ["git_fetch", "git_fetch", "git_fetch"]
    assert _git(world.origin, "rev-parse", LANE_BRANCH) == world.lane_tip


@pytest.mark.parametrize("submitter", list(SUBMITTERS))
def test_a_commit_made_as_an_outside_identity_after_the_rebase_is_refused(
    tmp_path: Path, submitter: str
):
    world = Phase2(tmp_path, submitter)
    world.rebase_through_the_conflict()
    (world.repo / "extra.txt").write_text("extra\n")
    _git(world.repo, "add", "-A")
    _git(world.repo, "commit", "-q", "-m", "extra", env=_as(MALLORY))
    answer = world.spool("git_push_branch", {}, "push")
    # MUTATION: removing the authorship check lets this push through.
    assert answer["refused"]["code"] == "foreign_author", answer
    # The third is the push asking origin about the foreign commit (#2413).
    assert world.routes.git_calls == ["git_fetch", "git_fetch", "git_fetch"]


def test_the_refusal_names_the_commit_the_field_and_both_identities(tmp_path: Path):
    world = Phase2(tmp_path, "service account", lane_author=MALLORY)
    world.rebase_through_the_conflict()
    head = _git(world.repo, "rev-parse", "HEAD")
    refused = world.spool("git_push_branch", {}, "push")["refused"]
    message = refused["message"]
    # MUTATION: a message without the sha (the issue: "nothing records which
    # commit") fails here.
    assert head[:12] in message and refused["commit"] == head[:12]
    # MUTATION: naming no field, or the committer, fails: the author is foreign.
    assert refused["fields"] == ["author"]
    assert "author" in message
    # MUTATION: dropping the found identity, or either expected one, fails.
    assert MALLORY[1] in message and refused["found"]["author"] == MALLORY[1]
    assert BOT_GIT_IDENTITY[1] in message and BOT_EMAIL in message
    assert refused["expected"] == [BOT_GIT_IDENTITY[1], BOT_EMAIL]


def test_a_foreign_committer_is_named_as_the_committer_field(tmp_path: Path):
    world = Phase2(tmp_path, "display name")
    world.rebase_through_the_conflict()
    (world.repo / "extra.txt").write_text("extra\n")
    _git(world.repo, "add", "-A")
    _git(world.repo, "commit", "-q", "-m", "extra", env=_as(("Ada Lovelace", SUBMITTER),
                                                             committer=MALLORY))
    refused = world.spool("git_push_branch", {}, "push")["refused"]
    # MUTATION: reporting the author field for a committer-only problem fails.
    assert refused["code"] == "foreign_author" and refused["fields"] == ["committer"]
    assert refused["found"] == {"author": SUBMITTER, "committer": MALLORY[1]}
    assert MALLORY[1] in refused["message"] and SUBMITTER in refused["message"]


def test_the_refusal_is_written_to_git_answers_jsonl_by_the_worker(tmp_path: Path):
    world = Phase2(tmp_path, "service account", lane_author=MALLORY)
    world.rebase_through_the_conflict()
    answer = world.spool("git_push_branch", {}, "push")
    rows = world.answers()
    # MUTATION: not appending the worker's refusal leaves the file absent, as
    # the issue's task had it.
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["step"] == gittools.ANSWERS_STEP and row["tool"] == "git_push_branch"
    assert row["request_id"] == "push"
    # MUTATION: writing a different (or truncated) answer than the agent got fails.
    assert row["answer"] == answer


def test_no_registered_secret_reaches_the_refusal_or_git_answers_jsonl(tmp_path: Path):
    value = "acc-" + secrets.token_hex(16)
    world = Phase2(tmp_path, "service account", secrets_=(value,))
    world.rebase_through_the_conflict()
    (world.repo / "extra.txt").write_text("extra\n")
    _git(world.repo, "add", "-A")
    _git(world.repo, "commit", "-q", "-m", "extra",
         env=_as((f"Mallory {value}", f"{value}@example.invalid")))
    answer = world.spool("git_push_branch", {}, "push")
    assert answer["refused"]["code"] == "foreign_author"
    # MUTATION: dropping `scrub_text` over the refusal leaves the value in both.
    assert value not in json.dumps(answer)
    written = (world.ws.artifacts / gittools.ANSWERS_ARTIFACT).read_text()
    assert written.strip() and value not in written


def test_git_answers_jsonl_is_not_written_through_a_link_the_agent_planted(tmp_path: Path):
    world = Phase2(tmp_path, "service account", lane_author=MALLORY)
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched\n")
    (world.ws.artifacts / gittools.ANSWERS_ARTIFACT).symlink_to(target)
    world.rebase_through_the_conflict()
    answer = world.spool("git_push_branch", {}, "push")
    # The agent still gets its answer; the worker does not follow the link.
    assert answer["refused"]["code"] == "foreign_author"
    # MUTATION: opening without O_NOFOLLOW appends to the link's target.
    assert target.read_text() == "untouched\n"


def test_the_relay_writes_its_answers_into_the_workspaces_artifacts_dir(tmp_path: Path):
    """`ChildPath` is handed `ws.logs`, not the Workspace: the artifacts dir is
    its sibling, the layout `workspace.create` makes."""
    world = Phase2(tmp_path, "service account")
    # MUTATION: deriving the path from anything but the logs dir's sibling
    # `artifacts` (the dir the worker uploads) fails here.
    assert world.path._git.answers_path == world.ws.artifacts / gittools.ANSWERS_ARTIFACT

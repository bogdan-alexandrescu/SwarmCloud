"""The worker's commit of uncommitted work is made in its own repository (#259, M1).

WHAT WENT WRONG. After the pre-publish reap, `commit_dirty` ran `git add
--all`, `git diff --cached --quiet` and `git commit` in the AGENT's clone. Those
commands read the clone's `.git/config` and the tree's `.gitattributes`, both
the agent's to write, so a filter driver the agent defined
(`filter.<name>.clean`) and named in `.gitattributes` ran as the worker, after
the reap, with the tenant token about to be read. `_NO_HOOKS` switches off
hooks, fsmonitor and gpg by name, and says itself that the filter class cannot
be closed by listing keys. The redesign promised that nothing the agent
configured runs at publish; this was the one place it still did.

WHAT IS PINNED (owner decision, 2026-09-28). After the reap the worker copies
the agent's working-tree files -- never anything under `.git`, and every link
as a link -- into the worker-owned clean repository the scans already use, and
stages and commits THERE, with only the worker's configuration. A
`.gitattributes` in the tree is data in that repository: it names a filter or a
diff driver the repository does not define, so it is inert. The agent's own
commits still arrive by the object-checked fetch, and its uncommitted work is
the worker's one commit on top.

Real git against a local `file://` remote; only the HTTP forge is faked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_agent_commits_are_kept import _commit
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    forge,
    local_urls,
    origin,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    """git in the agent's clone, as the agent, with an identity of its own."""
    ident = ["-c", "user.name=Agent", "-c", "user.email=agent@example.invalid"]
    return subprocess.run(
        ["git", *ident, *args], cwd=str(repo), check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _show(bare: Path, ref: str, path: str) -> bytes:
    """The bytes of `path` at `ref` on the remote -- what was really pushed."""
    return subprocess.run(
        ["git", "cat-file", "blob", f"{ref}:{path}"], cwd=str(bare), check=True, capture_output=True,
    ).stdout


def _mode(bare: Path, ref: str, path: str) -> str:
    listing = subprocess.run(
        ["git", "ls-tree", ref, "--", path], cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert listing, f"{path} is not on {ref}"
    return listing.split()[0]


def _attempt(
    worker_factory, monkeypatch, remote: Path, *, task_id: str, edit, marker: Path | None = None,
    link_artifacts: bool = False,
):
    """Clone, let the "agent" edit, then harvest and publish as teardown does.

    When `marker` is given, it is deleted at the pre-publish reap: the harvest
    before it reads the clone (a separate path), and what is pinned here is
    that nothing the agent configured runs from the reap onwards.
    """
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{remote}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    if link_artifacts:
        worker._link_checkout_artifacts()

    reaped = {"at": False}
    real_reap = worker.reap_before_publish

    def reap_then_forget_the_harvest() -> tuple[int, ...]:
        survivors = real_reap()
        if marker is not None:
            marker.unlink(missing_ok=True)
        reaped["at"] = True
        return survivors

    worker.reap_before_publish = reap_then_forget_the_harvest
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)
    out = worker._harvest_git(publish=True)
    assert reaped["at"], "the publish never reached the reap"
    return worker, config, out


def _recording_prepare(monkeypatch) -> list[str]:
    """Record the clean repository's own configuration as the worker made it."""
    configs: list[str] = []
    real = lifecycle.prepare_publish_repo

    def wrapper(**kwargs):
        path = real(**kwargs)
        configs.append(
            subprocess.run(
                ["git", "config", "--local", "--list"], cwd=str(path),
                capture_output=True, text=True,
            ).stdout
        )
        return path

    monkeypatch.setattr(lifecycle, "prepare_publish_repo", wrapper)
    return configs


# -- the agent's filter, diff driver, hooks and fsmonitor --------------------


def test_nothing_the_agent_configured_runs_at_publish_and_the_commit_is_the_working_tree(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The agent commits one file, then defines a clean/smudge filter, a diff
    driver (`textconv` and `command`), a hooks directory and an fsmonitor hook
    -- every one a program that records it ran -- names the filter and the
    driver for every path in `.gitattributes`, and leaves a file uncommitted.

    From the reap onwards none of those programs runs, and the pushed file is
    the bytes the agent left in its working tree, not what its filter would
    have made of them (the filter upper-cases)."""
    marker = tmp_path / "agent-program.ran"
    # The filter reads the file on stdin, which git always hands a filter as a
    # pipe; every other program only records that it ran and reads nothing,
    # so none of them can wait on an inherited stdin.
    program = tmp_path / "agent-filter.sh"
    program.write_text(f"#!/bin/sh\necho \"filter $*\" >> '{marker}'\ntr a-z A-Z\n")
    program.chmod(0o755)
    recorder = tmp_path / "agent-program.sh"
    recorder.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker}'\nexit 0\n")
    recorder.chmod(0o755)
    hooks = tmp_path / "agent-hooks"
    hooks.mkdir()
    for hook in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit",
                 "post-checkout", "reference-transaction", "post-index-change"):
        shutil.copy(recorder, hooks / hook)
    content = b"left uncommitted, in lower case\n"

    def edit(repo: Path) -> None:
        (repo / "one.txt").write_text("committed by the agent\n")
        _commit(repo, "Add one")
        _git(repo, "config", "filter.x.clean", str(program))
        _git(repo, "config", "filter.x.smudge", str(program))
        _git(repo, "config", "filter.x.required", "true")
        _git(repo, "config", "diff.x.textconv", str(recorder))
        _git(repo, "config", "diff.x.command", str(recorder))
        _git(repo, "config", "core.hooksPath", str(hooks))
        _git(repo, "config", "core.fsmonitor", str(recorder))
        (repo / ".gitattributes").write_text("* filter=x diff=x\n")
        (repo / "notes.txt").write_bytes(content)
        # The control: in the clone the filter is live, so a commit made there
        # stores what the filter prints and runs the agent's program.
        filtered = _git(repo, "hash-object", "--path", "notes.txt", "notes.txt")
        raw = _git(repo, "hash-object", "--no-filters", "notes.txt")
        assert filtered != raw, "the agent's filter is not active in its clone"
        assert marker.exists(), "the agent's program did not run in its clone"

    configs = _recording_prepare(monkeypatch)
    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-agent-filter", edit=edit, marker=marker,
    )

    assert out["published"] is True, out.get("publish_reason")
    assert not marker.exists(), (
        "a program the agent configured ran after the pre-publish reap: "
        + marker.read_text()
    )
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert _show(origin, branch, "notes.txt") == content, (
        "the pushed file is not the working tree's bytes: the agent's clean filter rewrote it"
    )
    assert _show(origin, branch, ".gitattributes") == b"* filter=x diff=x\n"
    assert _show(origin, branch, "one.txt") == b"committed by the agent\n"
    # The `.gitattributes` is inert in the worker's repository because nothing
    # there defines the filter or the driver it names.
    assert configs, "the clean repository was never prepared"
    for listing in configs:
        keys = [line.split("=", 1)[0] for line in listing.splitlines()]
        assert not [k for k in keys if k.startswith(("filter.", "diff.", "core.hookspath",
                                                     "core.fsmonitor"))], listing


# -- links are copied as links, never followed -------------------------------


def test_a_link_out_of_the_workspace_is_published_as_a_link_and_never_followed(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The agent leaves links to a folder and to a file outside the workspace,
    and a relative link that climbs out of it. Each is pushed as a link
    (mode 120000) whose blob is its target text; nothing outside the workspace
    is read into the branch."""
    outside = tmp_path / "outside-the-workspace"
    outside.mkdir()
    (outside / "secret.txt").write_text("not the agent's to publish\n")

    def edit(repo: Path) -> None:
        os.symlink(str(outside), repo / "escape")
        os.symlink(str(outside / "secret.txt"), repo / "escape-file")
        os.symlink("../../../..", repo / "climb")
        (repo / "plain.txt").write_text("a plain file beside the links\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-agent-links", edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin, branch)
    for link, target in (
        ("escape", str(outside)),
        ("escape-file", str(outside / "secret.txt")),
        ("climb", "../../../.."),
    ):
        assert _mode(origin, branch, link) == "120000", f"{link} was not pushed as a link"
        assert _show(origin, branch, link) == target.encode(), link
    assert "escape/secret.txt" not in files, "a link to a folder was followed"
    assert "plain.txt" in files


# -- the copy is a mirror: deletions and modes carry ------------------------


def test_a_deletion_and_a_mode_change_left_uncommitted_reach_the_branch(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The agent deletes the repository's README and makes a new script
    executable, committing neither. The branch has no README and the script is
    mode 100755: the worker's commit is the working tree, not the working tree
    laid over the old one."""
    def edit(repo: Path) -> None:
        (repo / "README.md").unlink()
        script = repo / "run.sh"
        script.write_text("#!/bin/sh\necho run\n")
        script.chmod(0o755)
        nested = repo / "deep" / "er"
        nested.mkdir(parents=True)
        (nested / "file.txt").write_text("nested\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-agent-deletes", edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin, branch)
    assert "README.md" not in files, "a file the agent deleted came back on the branch"
    assert _mode(origin, branch, "run.sh") == "100755"
    assert _show(origin, branch, "deep/er/file.txt") == b"nested\n"


# -- the worker's own ./artifacts link stays out of the branch ---------------


def test_the_workers_artifacts_link_is_not_published(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The checkout's `./artifacts` links to this attempt's artifacts
    directory and the worker hides it from git (#226). The hiding lived in the
    clone's `.git/info/exclude`; the worker's repository has to hide it too."""
    def edit(repo: Path) -> None:
        link = repo / "artifacts"
        assert link.is_symlink(), "the worker did not link the checkout's ./artifacts"
        (link / "report.txt").write_text("an artifact, not part of the work\n")
        (repo / "work.txt").write_text("the work\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-agent-artifacts", edit=edit,
        link_artifacts=True,
    )

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin, branch)
    assert "work.txt" in files
    assert not [f for f in files if f == "artifacts" or f.startswith("artifacts/")], files

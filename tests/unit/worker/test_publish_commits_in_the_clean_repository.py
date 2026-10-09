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
    link_artifacts: bool = False, publish: bool = True,
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
    out = worker._harvest_git(publish=publish)
    if publish:
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


def test_a_gitignore_that_unignores_the_artifacts_link_does_not_publish_it(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The tree's own `.gitignore` outranks `.git/info/exclude`, so hiding the
    worker's link by an exclude pattern alone let a `!/artifacts` line in the
    agent's `.gitignore` un-hide it (#346). The link is not created in the
    publish repository at all, so no pattern can bring it back."""
    def edit(repo: Path) -> None:
        link = repo / "artifacts"
        assert link.is_symlink(), "the worker did not link the checkout's ./artifacts"
        (link / "report.txt").write_text("an artifact, not part of the work\n")
        (repo / ".gitignore").write_text("!/artifacts\n!artifacts\n!/artifacts/**\n")
        (repo / "work.txt").write_text("the work\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-unignore-artifacts", edit=edit,
        link_artifacts=True,
    )

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin, branch)
    assert "work.txt" in files and ".gitignore" in files
    assert not [f for f in files if f == "artifacts" or f.startswith("artifacts/")], files


# -- the harvest reads no agent configuration either (#259, owner item 3) ----


@pytest.mark.parametrize("publish", [False, True], ids=["park", "publish"])
def test_the_harvest_runs_no_filter_the_agent_configured(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path, publish
):
    """`summarize_work` took the patch with `git diff <base>` IN THE CLONE,
    before the reap. A diff of the working tree reads each file through the
    clean filter `.gitattributes` names, so the agent's `filter.x.clean` ran
    as the worker there, and the patch carried what the filter printed.

    The marker is cleared at the end of the agent's turn, so ANY run from the
    harvest onwards -- on a park, which never publishes, as much as on a
    publish -- is caught, and the patch must hold the working tree's bytes."""
    marker = tmp_path / "agent-filter.ran"
    program = tmp_path / "agent-filter.sh"
    program.write_text(f"#!/bin/sh\necho \"filter $*\" >> '{marker}'\ntr a-z A-Z\n")
    program.chmod(0o755)
    line = "left uncommitted, in lower case"

    def edit(repo: Path) -> None:
        _git(repo, "config", "filter.x.clean", str(program))
        _git(repo, "config", "filter.x.smudge", str(program))
        (repo / ".gitattributes").write_text("* filter=x\n")
        (repo / "README.md").write_text(f"{line} (edited)\n")
        (repo / "notes.txt").write_text(f"{line}\n")
        # The control: the filter is live in the clone.
        filtered = _git(repo, "hash-object", "--path", "notes.txt", "notes.txt")
        raw = _git(repo, "hash-object", "--no-filters", "notes.txt")
        assert filtered != raw and marker.exists(), "the agent's filter is not active"
        marker.unlink()

    worker, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-harvest-filter-{publish}", edit=edit,
        publish=publish,
    )

    assert not marker.exists(), (
        "the agent's clean filter ran during the harvest or publish: " + marker.read_text()
    )
    assert out.get("patch"), out
    patch = (worker.ws.artifacts / lifecycle.PATCH_NAME).read_text()
    assert f"+{line}" in patch, "the patch is not the working tree's bytes"
    assert line.upper() not in patch, "the patch holds what the agent's filter printed"
    assert "notes.txt" in out["dirty"], out
    if publish:
        assert out["published"] is True, out.get("publish_reason")
        branch = f"{config.git_branch_prefix}{config.task_id}"
        assert _show(origin, branch, "notes.txt") == f"{line}\n".encode()


# -- what the agent excluded stays unpublished (#259, owner item 2) ----------


@pytest.mark.parametrize(
    "how, published",
    [
        ("info-exclude", False),
        ("excludes-file-in-workspace", False),
        ("excludes-file-in-home", False),
        ("excludes-file-outside-workspace", True),
        ("excludes-file-through-a-link", True),
    ],
)
def test_a_file_the_agent_excluded_is_not_published(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path, how, published
):
    """The agent's `.git/info/exclude` and its `core.excludesFile` are read
    as DATA -- never by running git in the clone -- and their patterns hide
    files from the worker's commit, as they did when that commit was made in
    the clone. `core.excludesFile` counts only when it names a file inside the
    workspace and no link is on the way (`~/` is the agent's HOME, `work/`);
    one outside, or reached through a link, is not read, and the file it
    would have hidden is published."""
    hidden = "hidden.txt"

    def edit(repo: Path) -> None:
        work = repo.parent
        if how == "info-exclude":
            with (repo / ".git" / "info" / "exclude").open("a") as handle:
                handle.write(f"\n{hidden}\n")
        elif how == "excludes-file-in-workspace":
            (work / "agent-ignore").write_text(f"{hidden}\n")
            _git(repo, "config", "core.excludesFile", str(work / "agent-ignore"))
        elif how == "excludes-file-in-home":
            (work / "agent-ignore").write_text(f"{hidden}\n")
            _git(repo, "config", "core.excludesFile", "~/agent-ignore")
        elif how == "excludes-file-outside-workspace":
            outside = tmp_path / "outside-ignore"
            outside.write_text(f"{hidden}\n")
            _git(repo, "config", "core.excludesFile", str(outside))
        else:
            (work / "real-ignore").write_text(f"{hidden}\n")
            os.symlink(str(work / "real-ignore"), work / "link-ignore")
            _git(repo, "config", "core.excludesFile", str(work / "link-ignore"))
        (repo / hidden).write_text("the agent kept this out of git\n")
        (repo / "visible.txt").write_text("the work\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-exclude-{how}", edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin, branch)
    assert "visible.txt" in files
    assert (hidden in files) is published, (how, files)


# -- upload-pack in the clone runs nothing the agent configured (item 4) ----


def test_the_fetch_out_of_the_clone_runs_no_program_the_agent_configured(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The fetch into the worker's repository runs `git upload-pack` in the
    clone -- the one git command left there. The agent names a
    `uploadpack.packObjectsHook`, an alternate-refs command, an ssh command,
    a proxy and a hooks directory in its `.git/config`; none of them runs,
    and the work still arrives."""
    marker = tmp_path / "agent-upload.ran"
    recorder = tmp_path / "agent-upload.sh"
    recorder.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker}'\nexit 0\n")
    recorder.chmod(0o755)
    hooks = tmp_path / "agent-upload-hooks"
    hooks.mkdir()
    for hook in ("pre-upload-pack", "post-upload-pack", "reference-transaction",
                 "pre-commit", "post-commit", "post-checkout"):
        shutil.copy(recorder, hooks / hook)

    def edit(repo: Path) -> None:
        (repo / "committed.txt").write_text("the agent's commit\n")
        _commit(repo, "Add committed")
        _git(repo, "config", "uploadpack.packObjectsHook", str(recorder))
        _git(repo, "config", "core.alternateRefsCommand", str(recorder))
        _git(repo, "config", "core.sshCommand", str(recorder))
        _git(repo, "config", "core.gitProxy", str(recorder))
        _git(repo, "config", "core.hooksPath", str(hooks))
        (repo / "uncommitted.txt").write_text("left uncommitted\n")
        marker.unlink(missing_ok=True)

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-upload-pack", edit=edit,
    )

    assert not marker.exists(), "a program the agent configured ran: " + marker.read_text()
    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin, branch)
    assert {"committed.txt", "uncommitted.txt"} <= files, files


# -- HEAD is read as data, never by git in the clone (#259 review, B1) ------


def test_a_partial_clone_with_a_missing_head_runs_nothing_and_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """After the reap the worker resolved the commit to publish with
    `git rev-parse HEAD^{commit}` IN THE CLONE. The agent makes its clone a
    partial clone whose promisor remote's upload-pack is a program of its
    own, and points its branch at an object the clone does not have: git
    lazy-fetches the missing object through that remote, running the
    program as the worker -- and a process it starts would outlive the reap
    into the push. HEAD is now read from `.git/HEAD` as data: nothing runs,
    and the publish fails closed, saying why."""
    marker = tmp_path / "agent-promisor.ran"
    recorder = tmp_path / "agent-promisor.sh"
    recorder.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker}'\nexit 1\n")
    recorder.chmod(0o755)
    missing = "0123456789abcdef0123456789abcdef01234567"

    def edit(repo: Path) -> None:
        (repo / "work.txt").write_text("the agent's work\n")
        _commit(repo, "Add work")
        branch = _git(repo, "symbolic-ref", "--short", "HEAD")
        _git(repo, "config", "core.repositoryFormatVersion", "1")
        _git(repo, "config", "extensions.partialClone", "origin")
        _git(repo, "config", "remote.origin.promisor", "true")
        _git(repo, "config", "remote.origin.url", str(origin))
        _git(repo, "config", "remote.origin.uploadpack", str(recorder))
        _git(repo, "config", "protocol.allow", "always")
        _git(repo, "config", "protocol.file.allow", "always")
        (repo / ".git" / "refs" / "heads" / branch).write_text(f"{missing}\n")
        marker.unlink(missing_ok=True)

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-promisor-head", edit=edit,
    )

    assert not marker.exists(), (
        "git in the clone lazy-fetched through the agent's promisor remote: "
        + marker.read_text()
    )
    assert out.get("published") is not True, out
    reason = f"{out.get('publish_reason', '')} {out.get('error', '')}"
    assert "could not fetch the agent's work" in reason, out
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch not in tree_at_refs(origin), "a branch was pushed"


def tree_at_refs(bare: Path) -> set[str]:
    listing = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout
    return {line for line in listing.splitlines() if line}


@pytest.mark.parametrize("how", ["detached", "packed-ref", "symref-outside-heads"])
def test_head_is_resolved_from_the_files_the_way_git_would_or_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, how
):
    """A detached HEAD and a branch that lives only in `packed-refs` publish
    the agent's commit as before. A HEAD that is a symbolic ref to anything
    outside `refs/heads/` is refused, with a reason naming HEAD, and nothing
    is pushed: the worker reads the ref files itself now, and it follows no
    ref it would have to trust the agent about."""
    def edit(repo: Path) -> None:
        (repo / "committed.txt").write_text("the agent's commit\n")
        _commit(repo, "Add committed")
        head = _git(repo, "rev-parse", "HEAD")
        if how == "detached":
            (repo / ".git" / "HEAD").write_text(f"{head}\n")
        elif how == "packed-ref":
            _git(repo, "pack-refs", "--all", "--prune")
            branch = _git(repo, "symbolic-ref", "--short", "HEAD")
            assert not (repo / ".git" / "refs" / "heads" / branch).exists()
        else:
            _git(repo, "update-ref", "refs/agent/head", head)
            (repo / ".git" / "HEAD").write_text("ref: refs/agent/head\n")
        (repo / "uncommitted.txt").write_text("left uncommitted\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-head-{how}", edit=edit,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    if how == "symref-outside-heads":
        assert out.get("published") is not True, out
        reason = f"{out.get('publish_reason', '')} {out.get('error', '')}"
        assert "HEAD" in reason and "refs/heads/" in reason, out
        assert branch not in tree_at_refs(origin), "a branch was pushed"
    else:
        assert out["published"] is True, out.get("publish_reason")
        assert {"committed.txt", "uncommitted.txt"} <= tree_at(origin, branch)

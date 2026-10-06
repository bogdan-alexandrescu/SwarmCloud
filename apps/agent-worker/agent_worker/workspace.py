"""The isolated per-attempt workspace.

One attempt, one directory tree, created empty and destroyed at the end:

    <workspace_root>/<attempt_id>/
        work/          the runner's current directory, and HOME for the agent CLI;
                       this is what gets checkpointed
          artifacts    a link to the artifacts/ below, absolute; never checkpointed
          repo/        the repository checkout, when a task has one; the agent
                       CLI's working directory then (#226)
            artifacts  the same link again, hidden from git; never checkpointed
        artifacts/     files the runner wants kept; uploaded on exit
        logs/          stdout.log, stderr.log
        tmp/           TMPDIR for the child, so a stray temp file cannot escape
        restore/       staging for a downloaded checkpoint archive (never checkpointed)
        private/       the WORKER's own scratch: git credentials during a clone,
                       and the one-file-at-a-time copy a standalone task's
                       working-folder upload redacts before it sends it (#184).
                       Its path is never in the child's environment, it is never
                       checkpointed and never walked for upload. Same uid, so
                       this is not a permission boundary -- it is the difference
                       between a credential file the agent is handed the path to
                       and one it would have to go looking for, and the clone
                       deletes it.

The one rule worth stating out loud: **a resumed worker starts from an empty
tree.** Cloud Run's ephemeral disk is per-execution, but GKE Jobs, local runs and
retries on a warm sandbox are not guaranteed to be, and a checkpoint restored on
top of leftovers from a previous attempt would produce a workspace that exists in
no checkpoint -- irreproducible, and silently wrong. So `create()` removes the
tree first, every time, and refuses to continue if it cannot.

**`work/artifacts` is a link to `artifacts/` (#149), made by `link_artifacts`
once the lifecycle has restored, cloned and staged -- never by `create()`.**
The artifacts directory is the working directory's SIBLING, so `./artifacts` is
the natural wrong guess. On 2026-09-25 (`wf_06a3a949d2c242c3b0e9`, scan-02) an
agent echoed `$SWARM_ARTIFACTS_DIR` correctly and then wrote
`<work>/artifacts/scan-02.md`, which nothing uploads. The link makes that guess
land in the uploaded directory. `create()` cannot make it: a restore refuses a
non-empty `work/`, and a declared input or a restored checkpoint may already
own the name, in which case the link is skipped rather than put over it.

**With a repository attached, the agent starts in `work/repo` (#226, owner
decision of 2026-09-26),** so Claude Code loads the repository's own
`CLAUDE.md` by itself, as a local lane does. `./artifacts` is then the
checkout's, so the lifecycle makes the same link at `work/repo/artifacts` and
hides it from git (`gitops.hide_from_git`); `work/artifacts` stays as well, as
`../artifacts` from the checkout. HOME stays `work/`: the CLI keeps its own
state under HOME, and none of it belongs in the repository's diff.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import WorkspaceError

#: How many folders deep `walk_tree` descends below its root. Past this a
#: folder is listed but not entered. Every tree the worker walks is in the
#: agent's reach, and on Python 3.11 `os.walk` and `Path.rglob` RECURSE, one
#: frame per level: an agent that made a tree 1,000 folders deep raised
#: `RecursionError` out of the disk check, the output scan and the restore
#: count (#259 review). 2,048 is past where one-character names reach Linux's
#: 4,096-byte PATH_MAX, so no tree a path can name is cut short by it; it
#: bounds the walk's memory, not its reach.
MAX_WALK_DEPTH = 2048


def walk_tree(
    root: Path, *, max_depth: int = MAX_WALK_DEPTH
) -> Iterator[tuple[Path, list[str], list[str]]]:
    """`os.walk(root, followlinks=False)`, top-down, WITHOUT recursion.

    Yields `(dirpath, dirnames, filenames)` exactly as `os.walk` does -- a
    link to a folder is listed in `dirnames` and never entered, a caller
    prunes by editing `dirnames` in place -- from an explicit stack, so a deep
    tree costs a list entry per level, not a Python frame. No descriptor is
    held between yields: each folder is listed whole and closed. A folder that
    cannot be listed is skipped, as `os.walk` skips it; a folder at
    `max_depth` levels below `root` is yielded but not entered.
    """
    stack: list[tuple[Path, int]] = [(Path(root), 0)]
    while stack:
        here, depth = stack.pop()
        dirnames: list[str] = []
        filenames: list[str] = []
        links: set[str] = set()
        try:
            with os.scandir(here) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    if is_dir:
                        dirnames.append(entry.name)
                        try:
                            if entry.is_symlink():
                                links.add(entry.name)
                        except OSError:
                            links.add(entry.name)
                    else:
                        filenames.append(entry.name)
        except OSError:
            continue
        yield here, dirnames, filenames
        if depth >= max_depth:
            continue
        # Reversed onto the stack, so they come off in the order listed.
        for name in reversed(dirnames):
            if name not in links:
                stack.append((here / name, depth + 1))


#: The repository checkout's directory inside `work/`. Defined here, beside the
#: tree it names, because the checkpoint needs it as well as the lifecycle:
#: a link to the artifacts directory inside the checkout must never be
#: archived either (`checkpoint.CheckpointManager.create`).
REPO_DIR_NAME = "repo"


@dataclass(frozen=True)
class Workspace:
    root: Path
    work: Path
    artifacts: Path
    logs: Path
    tmp: Path
    restore: Path
    private: Path

    @property
    def stdout_path(self) -> Path:
        return self.logs / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.logs / "stderr.log"

    @property
    def input_path(self) -> Path:
        return self.work / "input.json"

    @property
    def result_path(self) -> Path:
        return self.work / "result.json"

    @property
    def quota_path(self) -> Path:
        """Written by a runner that hit a provider rate limit."""
        return self.work / "quota.json"

    @property
    def credential_path(self) -> Path:
        """Written by a runner whose credential was refused.

        Separate from `quota_path` because the two mean opposite things and
        have opposite remedies. A rate limit means "the same credential will
        work later, wait"; a refused credential means "this credential will
        never work again, get the current one". Parking on the second would
        wait out a reset that is not coming.
        """
        return self.work / "credential.json"

    @property
    def children_dir(self) -> Path:
        """The child-task spool (docs/design/child-tasks.md §6.4): the agent's
        requests and await, the worker's responses and staged results. Under
        `work/` so a checkpoint carries which requests were answered; a control
        name, so no declared input or working-folder upload can take it."""
        return self.work / ".swarm-children"

    def control_file_names(self) -> frozenset[str]:
        """The names of the control files this class places inside `work/`.

        Found by inspection rather than listed, and that is the whole point.
        `inputs.destination_for` refuses to stage a declared input over any
        name in this set, so a control file that is NOT in it is one an
        upstream artifact can quietly land on first -- a file staged over
        `input.json` is destroyed by the worker moments later, and one staged
        over `result.json` is read back as the agent's own result. A list
        written out by hand at the call site stays correct only until someone
        adds a fifth property above and does not know to go and edit it.

        A property that raises is deliberately NOT swallowed: dropping a name
        out of this set is exactly the silent hole the method exists to close,
        and every property here is a path join.
        """
        names: set[str] = set()
        for klass in type(self).__mro__:
            for attribute, descriptor in vars(klass).items():
                if not isinstance(descriptor, property):
                    continue
                value = getattr(self, attribute)
                # `work` only: `stdout_path` and `stderr_path` live under
                # `logs/`, which the agent never has a path into and no
                # declared input can reach.
                if isinstance(value, Path) and value.parent == self.work:
                    names.add(value.name)
        return frozenset(names)

    # A METHOD, NOT A PROPERTY, and that is load-bearing. `control_file_names`
    # collects every Path-valued property under `work/`, and staging refuses a
    # declared input over any name in that set. The owner's rule for this name
    # is the opposite: a declared input called `artifacts/...` is staged, and
    # the link is the thing that gives way.
    def artifacts_link(self, within: Path | None = None) -> Path:
        """Where `link_artifacts` puts the link: `work/artifacts` by default,
        or `<within>/artifacts` -- the checkout's, when the agent starts there.

        Named after the directory it points at, derived rather than spelled
        again, because the directory's own name is the one an agent guesses.
        """
        return (within if within is not None else self.work) / self.artifacts.name

    # A METHOD, NOT A PROPERTY, for the reason `artifacts_link` is one:
    # `control_file_names` collects every Path-valued property under `work/`,
    # and `repo` is not a control file. A task with no repository whose agent
    # makes a `repo/` folder has made its own work, which is uploaded.
    def checkout(self) -> Path:
        """`work/repo`: where the repository is cloned, when a task has one."""
        return self.work / REPO_DIR_NAME

    def is_artifacts_link(self, path: Path) -> bool:
        """True when `path` is `work/artifacts` or `work/repo/artifacts` AND a
        link resolving to `artifacts/`.

        Compared by where it resolves, not by the link's text, so an agent
        that re-created it as `../artifacts` is still recognised: that link
        leaves `work/` just the same, which is what `checkpoint` has to know.
        A real directory at the same path is the agent's own work and is
        never this.
        """
        if path not in (self.artifacts_link(), self.artifacts_link(self.checkout())):
            return False
        try:
            return path.is_symlink() and path.resolve() == self.artifacts.resolve()
        except OSError:
            return False

    def disk_bytes(self) -> int:
        """Bytes on disk under the workspace, symlinks not followed.

        `walk_tree`, not `os.walk`: the tree is the agent's, and a deep one
        must not raise `RecursionError` out of the disk check.
        """
        total = 0
        for dirpath, dirnames, filenames in walk_tree(self.root):
            for name in filenames:
                path = dirpath / name
                try:
                    stat = path.lstat()
                except OSError:
                    continue
                total += stat.st_size
        return total

    def child_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The environment a runner child sees, minus anything inherited by luck.

        Built from an explicit allowlist rather than `os.environ.copy()`. The
        worker's own environment carries control-plane identifiers and the
        service-account metadata it authenticates with; a runner needs none of
        it, so the child gets only what is listed here plus whatever the
        lifecycle deliberately passes in `base` (the tenant's provider key and
        the runner's own inputs).
        """
        env = dict(base or {})
        env.update(
            {
                "SWARM_WORKSPACE": str(self.root),
                "SWARM_WORK_DIR": str(self.work),
                "SWARM_ARTIFACTS_DIR": str(self.artifacts),
                "SWARM_INPUT": str(self.input_path),
                "SWARM_RESULT": str(self.result_path),
                "SWARM_QUOTA_SIGNAL": str(self.quota_path),
                "TMPDIR": str(self.tmp),
                "HOME": str(self.work),
                "PWD": str(self.work),
            }
        )
        return env


def create(root: Path, attempt_id: str) -> Workspace:
    """Create a guaranteed-empty workspace for this attempt."""
    base = Path(root) / attempt_id
    if base.exists():
        # Not an error: a retried execution of the same Cloud Run Job can land
        # on a sandbox that still holds the previous run's tree.
        shutil.rmtree(base, ignore_errors=True)
    if base.exists():
        raise WorkspaceError(f"could not clear existing workspace at {base}")

    ws = Workspace(
        root=base,
        work=base / "work",
        artifacts=base / "artifacts",
        logs=base / "logs",
        tmp=base / "tmp",
        restore=base / "restore",
        private=base / "private",
    )
    for path in (ws.root, ws.work, ws.artifacts, ws.logs, ws.tmp, ws.restore, ws.private):
        path.mkdir(parents=True, exist_ok=False)
        os.chmod(path, 0o700)
    return ws


def link_artifacts(ws: Workspace, within: Path | None = None) -> bool:
    """Make `work/artifacts` (or `<within>/artifacts`) a link to `artifacts/`.
    False when the name is taken.

    Taken means anything at all is there, a dangling link included: a declared
    input staged as `artifacts/...`, or a directory a restored checkpoint
    brought back -- an agent that made `work/artifacts` itself, before this
    link existed, is exactly the case #149 measured. That is the agent's or the
    caller's, so it is left as it is and the caller of this says so. Replacing
    it would delete work, or put a staged input out of the agent's reach.

    The target is ABSOLUTE. With the default `WORKSPACE_ROOT` (`/workspace`,
    `config.py`) that is the same string `child_env` exports as
    `SWARM_ARTIFACTS_DIR`, so `readlink artifacts` and
    `echo $SWARM_ARTIFACTS_DIR` give an agent the same answer. A relative
    target would resolve against `work/`, not against the directory the
    workspace was made in. The link is never checkpointed
    (`checkpoint.CheckpointManager.create`), because it names this attempt's
    directory and a resumed attempt makes its own.

    `within` is the checkout, `work/repo`, where the agent starts when a
    task has a repository (#226). There a name the repository tracks, an
    `artifacts/` folder of its own, is taken in exactly the same sense.

    Raises OSError if the link cannot be made; the caller decides what that
    costs.
    """
    link = ws.artifacts_link(within)
    if os.path.lexists(link):
        return False
    os.symlink(os.path.abspath(ws.artifacts), link, target_is_directory=True)
    return True


def destroy(ws: Workspace) -> None:
    """Best-effort teardown. The container usually dies first; this is for
    long-lived sandboxes and local runs, where leaving a tenant's working tree
    on disk would be a cross-tenant leak.

    WITHOUT RECURSION (#737). This was `shutil.rmtree(ws.root,
    ignore_errors=True)`, which on Python 3.11 recurses one frame per folder
    level, and `ignore_errors` swallows only `OSError`: a tree about 1,000
    folders deep raised `RecursionError` out of `_cleanup`, after the task
    had SUCCEEDED, and the container exited 1. `_remove_tree` is the same
    best-effort removal from an explicit stack. Never raises `OSError`."""
    _remove_tree(ws.root)


#: How `_remove_tree` opens a folder: never through a link (`O_NOFOLLOW` makes
#: a link at the name fail with ELOOP rather than open its target).
_REMOVE_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _clear_files(fd: int) -> list[str]:
    """Unlink every entry of the folder open at `fd` that is not a real folder
    -- a file, or a link of any kind, which is unlinked and never followed --
    and return the names of the real folders left in it."""
    folders: list[str] = []
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                try:
                    is_folder = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_folder = False
                if is_folder:
                    folders.append(entry.name)
                    continue
                try:
                    os.unlink(entry.name, dir_fd=fd)
                except OSError:
                    pass
    except OSError:
        pass
    return folders


def _remove_tree(root: Path) -> None:
    """Remove `root` and everything under it, bottom-up, never recursing and
    never following a link.

    ONE DESCRIPTOR AT A TIME. A stack of open descriptors, one per level,
    would run out of them on a tree as deep as the one this exists for (the
    usual limit is 1,024). The stack holds, per level, the folder's name in
    its parent, its identity, and the sub-folders not yet entered: a frame is
    a few strings, not a Python frame or a descriptor. Every name is opened
    relative to its parent's descriptor, so no path is longer than one name
    however deep the tree is (a full path past PATH_MAX cannot be opened).

    CLIMBING BACK UP is `..` of the folder just emptied, and it is checked to
    be the very folder (device and inode) that was descended from. An agent
    process that outlived its runner shares the worker's uid and could move a
    folder out of the tree mid-removal; a `..` that no longer leads back stops
    the removal there rather than deleting wherever it now leads.

    Best effort, as `rmtree(ignore_errors=True)` was: anything that cannot be
    listed, opened or removed is left, and no `OSError` escapes."""
    root = Path(root)
    try:
        if not os.path.isdir(root) or os.path.islink(root):
            if os.path.lexists(root):
                os.unlink(root)
            return
        fd = os.open(root, _REMOVE_OPEN_FLAGS)
    except OSError:
        return
    try:
        here = os.fstat(fd)
        # (name in the parent, (st_dev, st_ino), sub-folders still to enter)
        stack: list[tuple[str, tuple[int, int], list[str]]] = [
            ("", (here.st_dev, here.st_ino), _clear_files(fd))
        ]
        while stack:
            name, _, pending = stack[-1]
            if pending:
                child = pending.pop()
                try:
                    child_fd = os.open(child, _REMOVE_OPEN_FLAGS, dir_fd=fd)
                except OSError:
                    # Gone, unreadable, or swapped for a link since it was
                    # listed: a link is unlinked, a folder that cannot be
                    # opened is left.
                    try:
                        os.unlink(child, dir_fd=fd)
                    except OSError:
                        pass
                    continue
                os.close(fd)
                fd = child_fd
                here = os.fstat(fd)
                stack.append((child, (here.st_dev, here.st_ino), _clear_files(fd)))
                continue
            stack.pop()
            if not stack:
                break
            parent_fd = os.open("..", _REMOVE_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = parent_fd
            parent = os.fstat(fd)
            if (parent.st_dev, parent.st_ino) != stack[-1][1]:
                return
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError:
                pass
    except OSError:
        return
    finally:
        os.close(fd)
    try:
        os.rmdir(root)
    except OSError:
        pass


def is_empty(path: Path) -> bool:
    return not any(Path(path).iterdir())

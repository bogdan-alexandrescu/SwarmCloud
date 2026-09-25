"""The isolated per-attempt workspace.

One attempt, one directory tree, created empty and destroyed at the end:

    <workspace_root>/<attempt_id>/
        work/          the runner's current directory; this is what gets checkpointed
          artifacts    a link to the artifacts/ below, absolute; never checkpointed
        artifacts/     files the runner wants kept; uploaded on exit
        logs/          stdout.log, stderr.log
        tmp/           TMPDIR for the child, so a stray temp file cannot escape
        restore/       staging for a downloaded checkpoint archive (never checkpointed)
        private/       the WORKER's own scratch: git credentials during a clone.
                       Its path is never in the child's environment, it is never
                       checkpointed and never uploaded. Same uid, so this is not
                       a permission boundary -- it is the difference between a
                       credential file the agent is handed the path to and one it
                       would have to go looking for, and the clone deletes it.

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
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkspaceError


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
    def artifacts_link(self) -> Path:
        """Where `link_artifacts` puts the link: `work/artifacts`.

        Named after the directory it points at, derived rather than spelled
        again, because the directory's own name is the one an agent guesses.
        """
        return self.work / self.artifacts.name

    def is_artifacts_link(self, path: Path) -> bool:
        """True when `path` is `work/artifacts` AND a link resolving to `artifacts/`.

        Compared by where it resolves, not by the link's text, so an agent
        that re-created it as `../artifacts` is still recognised: that link
        leaves `work/` just the same, which is what `checkpoint` has to know.
        A real directory at the same path is the agent's own work and is
        never this.
        """
        if path != self.artifacts_link():
            return False
        try:
            return path.is_symlink() and path.resolve() == self.artifacts.resolve()
        except OSError:
            return False

    def disk_bytes(self) -> int:
        """Bytes on disk under the workspace, symlinks not followed."""
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            for name in filenames:
                path = Path(dirpath) / name
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


def link_artifacts(ws: Workspace) -> bool:
    """Make `work/artifacts` a link to `artifacts/`. False when the name is taken.

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

    Raises OSError if the link cannot be made; the caller decides what that
    costs.
    """
    link = ws.artifacts_link()
    if os.path.lexists(link):
        return False
    os.symlink(os.path.abspath(ws.artifacts), link, target_is_directory=True)
    return True


def destroy(ws: Workspace) -> None:
    """Best-effort teardown. The container usually dies first; this is for
    long-lived sandboxes and local runs, where leaving a tenant's working tree
    on disk would be a cross-tenant leak."""
    shutil.rmtree(ws.root, ignore_errors=True)


def is_empty(path: Path) -> bool:
    return not any(Path(path).iterdir())

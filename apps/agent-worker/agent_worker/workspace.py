"""The isolated per-attempt workspace.

One attempt, one directory tree, created empty and destroyed at the end:

    <workspace_root>/<attempt_id>/
        work/          the runner's current directory; this is what gets checkpointed
        artifacts/     files the runner wants kept; uploaded on exit
        logs/          stdout.log, stderr.log
        tmp/           TMPDIR for the child, so a stray temp file cannot escape
        restore/       staging for a downloaded checkpoint archive (never checkpointed)

The one rule worth stating out loud: **a resumed worker starts from an empty
tree.** Cloud Run's ephemeral disk is per-execution, but GKE Jobs, local runs and
retries on a warm sandbox are not guaranteed to be, and a checkpoint restored on
top of leftovers from a previous attempt would produce a workspace that exists in
no checkpoint -- irreproducible, and silently wrong. So `create()` removes the
tree first, every time, and refuses to continue if it cannot.
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
    )
    for path in (ws.root, ws.work, ws.artifacts, ws.logs, ws.tmp, ws.restore):
        path.mkdir(parents=True, exist_ok=False)
        os.chmod(path, 0o700)
    return ws


def destroy(ws: Workspace) -> None:
    """Best-effort teardown. The container usually dies first; this is for
    long-lived sandboxes and local runs, where leaving a tenant's working tree
    on disk would be a cross-tenant leak."""
    shutil.rmtree(ws.root, ignore_errors=True)


def is_empty(path: Path) -> bool:
    return not any(Path(path).iterdir())

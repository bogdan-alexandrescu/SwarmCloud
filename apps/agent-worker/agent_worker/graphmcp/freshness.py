"""The freshness block every answer begins with (docs/design/knowledge-graph.md §5.2).

A stale index says so, every time. Two sets of paths make a row stale:

  * CHANGED SINCE THE INDEX: the paths in `index_sha..base_sha`. The stager
    (lane KG5) records them in `freshness.json` with `index_sha`, `base_sha`
    and `behind_by`, from a local `git diff --name-only`. Without that record
    the server works them out itself from `--workdir` when the index commit
    is in the clone, and says `unknown` when it is not -- never zero.
  * DIRTIED IN THIS STEP: what the agent has changed against the base since
    the step began, committed or not, and the files it has added. Read again
    for every answer, because the agent edits between questions.

THE SERVER KEEPS ITS OWN COPY OF THE GIT INDEX. Git compares the working
tree through the index's stat cache; a fresh clone's cache is often racy, and
then every `git diff` re-reads every file -- measured here at 240 ms an
answer, most of the 300 ms budget, until something refreshes the index. A
read-only server must not write the agent's index (nor take its lock: git
runs with `--no-optional-locks`), so it refreshes a PRIVATE copy, taken once
at start, through `GIT_INDEX_FILE`. The copy is only a stat cache here: the
dirty set is the working tree against the base COMMIT, so a file the agent
has staged, committed or removed since reads the same either way, and a file
it has added is untracked to the copy and counted dirty all the same.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHANGED = "changed since the index"
DIRTY = "dirtied in this step"
#: A git call that takes longer than this is not waited for; the block then
#: says the dirty set is unknown rather than holding the answer.
GIT_TIMEOUT_SECONDS = 5.0


def _git(workdir: Path, *args: str, index: Path | None = None) -> str | None:
    env = None
    if index is not None:
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.quotepath=off", "-C", str(workdir),
             *args],
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def _git_status(workdir: Path, *args: str, index: Path | None = None) -> None:
    env = {**os.environ, "GIT_INDEX_FILE": str(index)} if index is not None else None
    try:
        subprocess.run(["git", "-C", str(workdir), *args], capture_output=True,
                       timeout=GIT_TIMEOUT_SECONDS, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return


def _paths(output: str | None) -> set[str] | None:
    if output is None:
        return None
    return {p for p in output.split("\0") if p}


@dataclass
class Observation:
    """One answer's view: the block, and which paths it must mark stale."""

    block: dict[str, Any]
    changed: frozenset[str]
    dirty: frozenset[str]
    dirty_dirs: tuple[str, ...] = ()

    def stale(self, path: str) -> str | None:
        """Why a row on `path` is stale, or None."""
        if path in self.dirty or any(path.startswith(d) for d in self.dirty_dirs):
            return DIRTY
        if path in self.changed:
            return CHANGED
        return None


@dataclass
class Freshness:
    index_sha: str
    workdir: Path | None = None
    base_sha: str | None = None
    behind_by: int | None = None
    changed: frozenset[str] | None = None
    basis: str = "unknown"
    _notes: list[str] = field(default_factory=list)
    _index: Path | None = None
    _scratch: tempfile.TemporaryDirectory | None = None

    @classmethod
    def establish(cls, index_sha: str, *, workdir: Path | str | None,
                  record: dict | None) -> "Freshness":
        """From the stager's record when there is one, else from the checkout."""
        fresh = cls(index_sha=index_sha, workdir=Path(workdir) if workdir else None)
        fresh._private_index()
        if record is not None and record.get("index_sha") not in (None, index_sha):
            fresh._notes.append("freshness.json names another index commit; ignored")
            record = None
        if record is not None:
            fresh.base_sha = record.get("base_sha")
            behind = record.get("behind_by")
            fresh.behind_by = behind if isinstance(behind, int) else None
            changed = record.get("changed")
            if isinstance(changed, list):
                fresh.changed = frozenset(str(p) for p in changed)
            fresh.basis = "staged"
            return fresh
        if fresh.workdir is None:
            fresh._notes.append("no freshness record and no checkout: staleness unknown")
            return fresh
        head = _git(fresh.workdir, "rev-parse", "--verify", "HEAD")
        fresh.base_sha = head.strip() if head else None
        if fresh.base_sha and _git(fresh.workdir, "cat-file", "-e",
                                   f"{index_sha}^{{commit}}") is not None:
            count = _git(fresh.workdir, "rev-list", "--count", f"{index_sha}..{fresh.base_sha}")
            fresh.behind_by = int(count.strip()) if count and count.strip().isdigit() else None
            changed = _paths(_git(fresh.workdir, "diff", "--name-only", "-z", "--no-renames",
                                  index_sha, fresh.base_sha))
            fresh.changed = frozenset(changed) if changed is not None else None
            fresh.basis = "git" if fresh.changed is not None else "unknown"
        else:
            fresh._notes.append("the index commit is not in this clone: changes since it "
                                "are unknown")
        return fresh

    def _private_index(self) -> None:
        """Copy the checkout's index once and refresh the copy's stat cache."""
        if self.workdir is None or self._index is not None:
            return
        where = _git(self.workdir, "rev-parse", "--git-path", "index")
        if not where:
            return
        source = Path(where.strip())
        if not source.is_absolute():
            source = self.workdir / source
        self._scratch = tempfile.TemporaryDirectory(prefix="swarm-graph-index-")
        private = Path(self._scratch.name) / "index"
        try:
            shutil.copyfile(source, private)
        except OSError:
            return
        self._index = private
        self._refresh()

    def _refresh(self) -> None:
        if self.workdir is not None and self._index is not None:
            # Exit 1 means "some files differ", which is what we are asking.
            _git_status(self.workdir, "update-index", "-q", "--refresh", "--unmerged",
                        index=self._index)

    def observe(self) -> Observation:
        """The block for one answer, the dirty set read now."""
        dirty: set[str] | None = None
        dirty_dirs: tuple[str, ...] = ()
        if self.workdir is not None:
            against = self.base_sha or "HEAD"
            self._refresh()
            tracked = _paths(_git(self.workdir, "diff", "--name-only", "-z", "--no-renames",
                                  against, index=self._index))
            untracked = _paths(_git(self.workdir, "ls-files", "-z", "--others",
                                    "--exclude-standard", "--directory", index=self._index))
            if tracked is not None and untracked is not None:
                dirty = tracked | {p for p in untracked if not p.endswith("/")}
                dirty_dirs = tuple(sorted(p for p in untracked if p.endswith("/")))
        block: dict[str, Any] = {
            "index_sha": self.index_sha,
            "base_sha": self.base_sha,
            "behind_by": self.behind_by,
            "changed_since_index": len(self.changed) if self.changed is not None else "unknown",
            "dirty": (len(dirty) + len(dirty_dirs)) if dirty is not None else "unknown",
        }
        if self._notes:
            block["note"] = "; ".join(self._notes)
        return Observation(block=block, changed=self.changed or frozenset(),
                           dirty=frozenset(dirty or ()), dirty_dirs=dirty_dirs)

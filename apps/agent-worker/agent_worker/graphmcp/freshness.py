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

ONE GIT CALL PER ANSWER (docs/design/graph-service.md §4.3). Once, at start,
the server builds a PRIVATE git index of the base commit
(`GIT_INDEX_FILE=<tmp>/index git read-tree <base>`, then a refresh of its stat
cache). For each answer it runs one `git status --porcelain=v2` against that
index. Because the index IS the base tree:

  * the "staged" column (HEAD against the index) is every path committed
    since the base;
  * the "unstaged" column (the index against the working tree) is every path
    changed in the tree against the base;
  * an untracked entry is a path absent from the base and not ignored; a
    directory comes back with a trailing `/` (`Observation.dirty_dirs`).

Their union is the dirty set against the base COMMIT, which the three calls
this replaced (`update-index --refresh`, `diff --name-only <base>`,
`ls-files --others`) computed. One case is marked where the three calls did
not: a file committed and then put back in the tree as it was in the base is
listed by the staged column. That errs safe -- a row marked stale that is not
is a hint, an unmarked stale row is a wrong answer.

The agent's own index and its lock are never touched. The private index and
its lock are the server's, so `git status` runs WITHOUT `--no-optional-locks`
and writes back its refreshed stat cache: a fresh clone's cache is often racy
and then every comparison re-reads every file (measured at 240 ms an answer on
this repository), but only on the first call. Without a private index -- no
checkout, or a base tree git cannot read -- the dirty set is `unknown`, never
zero.
"""

from __future__ import annotations

import os
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


def _git(workdir: Path, *args: str, index: Path | None = None,
         locks: bool = False) -> str | None:
    """stdout of one git call, or None when it failed or ran past the timeout.

    `locks` is only ever True against the server's own private index: without
    it git takes no optional lock, so it never writes the agent's index.
    """
    env = None
    if index is not None:
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    argv = ["git"] + ([] if locks else ["--no-optional-locks"])
    try:
        done = subprocess.run(
            [*argv, "-c", "core.quotepath=off", "-C", str(workdir), *args],
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


#: The one call each answer makes, against the private index of the base.
STATUS_ARGS = ("status", "--porcelain=v2", "-z", "--no-renames", "--untracked-files=normal",
               "--ignore-submodules=dirty")
#: Fields before the path in a porcelain v2 record, by its first character:
#: `1 XY sub mH mI mW hH hI path` and `u XY sub m1 m2 m3 mW h1 h2 h3 path`.
_STATUS_FIELDS = {"1": 8, "u": 10}


def parse_status(output: str) -> tuple[set[str], set[str]]:
    """(dirty files, untracked directories ending in `/`) of `git status
    --porcelain=v2 -z --no-renames`. Every record names a path that differs from
    the index in some column, so every one is dirty."""
    files: set[str] = set()
    dirs: set[str] = set()
    for record in output.split("\0"):
        if not record or record.startswith("#"):
            continue
        kind = record[0]
        if kind in ("?", "!"):
            path = record[2:]
        elif kind in _STATUS_FIELDS:
            parts = record.split(" ", _STATUS_FIELDS[kind])
            if len(parts) <= _STATUS_FIELDS[kind]:
                continue
            path = parts[-1]
        else:
            continue
        if kind == "!" or not path:
            continue
        (dirs if path.endswith("/") else files).add(path)
    return files, dirs


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

    @classmethod
    def empty(cls, index_sha: str) -> "Observation":
        """The observation the shared graph service computes every answer under
        (graph-service.md §4.4): nothing stale, a block naming only the index.
        Staleness belongs to one working tree, and only the pod's shim knows it."""
        return cls(block={"index_sha": index_sha}, changed=frozenset(), dirty=frozenset())


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
        fresh._from(record)
        fresh._private_index()
        return fresh

    def _from(self, record: dict | None) -> None:
        if record is not None and record.get("index_sha") not in (None, self.index_sha):
            self._notes.append("freshness.json names another index commit; ignored")
            record = None
        if record is not None:
            self.base_sha = record.get("base_sha")
            behind = record.get("behind_by")
            self.behind_by = behind if isinstance(behind, int) else None
            changed = record.get("changed")
            if isinstance(changed, list):
                self.changed = frozenset(str(p) for p in changed)
            self.basis = "staged"
            return
        if self.workdir is None:
            self._notes.append("no freshness record and no checkout: staleness unknown")
            return
        head = _git(self.workdir, "rev-parse", "--verify", "HEAD")
        self.base_sha = head.strip() if head else None
        if self.base_sha and _git(self.workdir, "cat-file", "-e",
                                  f"{self.index_sha}^{{commit}}") is not None:
            count = _git(self.workdir, "rev-list", "--count",
                         f"{self.index_sha}..{self.base_sha}")
            self.behind_by = int(count.strip()) if count and count.strip().isdigit() else None
            changed = _paths(_git(self.workdir, "diff", "--name-only", "-z", "--no-renames",
                                  self.index_sha, self.base_sha))
            self.changed = frozenset(changed) if changed is not None else None
            self.basis = "git" if self.changed is not None else "unknown"
        else:
            self._notes.append("the index commit is not in this clone: changes since it "
                               "are unknown")

    def _private_index(self) -> None:
        """Read the base commit's tree into an index of our own, once, and fill
        its stat cache (graph-service.md §4.3)."""
        if self.workdir is None or self._index is not None:
            return
        self._scratch = tempfile.TemporaryDirectory(prefix="swarm-graph-index-")
        private = Path(self._scratch.name) / "index"
        base = self.base_sha or "HEAD"
        if _git(self.workdir, "read-tree", f"{base}^{{tree}}", index=private,
                locks=True) is None:
            self._notes.append("the base commit's tree could not be read: dirtied paths "
                               "are unknown")
            return
        self._index = private
        # Exit 1 means "some files differ", which is what we are asking.
        _git_status(self.workdir, "update-index", "-q", "--refresh", "--unmerged",
                    index=self._index)

    def observe(self) -> Observation:
        """The block for one answer, the dirty set read now in one git call."""
        dirty: set[str] | None = None
        dirty_dirs: tuple[str, ...] = ()
        if self.workdir is not None and self._index is not None:
            output = _git(self.workdir, *STATUS_ARGS, index=self._index, locks=True)
            if output is not None:
                dirty, dirs = parse_status(output)
                dirty_dirs = tuple(sorted(dirs))
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

"""The worker's publish scan, runnable from a checkout (owner decision, 2026-10-02).

    python -m agent_worker.publish_scan [--base <rev>]

WHY. The worker refuses to publish a branch whose ADDED text holds a
credential (`lifecycle.final_tree_leak`), and it decides only after the agent
has finished. Three of six SwarmCloud lanes did all their work and lost it
there. This runs the same scan first, in the agent's own checkout, so the
lane can fix the line while it still can.

WHAT IS THE SAME. The diff is read by the worker's own `_DiffLeakScanner`,
from a `git diff` with the worker's flags (`-U0`, `--text`, `--no-renames`,
the `a/`/`b/` prefixes), and every file's added text is judged by the same
predicate the worker applies after its registered-secret check:
`lifecycle._credential_in`, tiered by the file's path. Nothing is restated
here, so a rule or a tier changed there is changed here.

WHAT DIFFERS, AND WHY.
* The worker stops at its first hit; this lists EVERY hit, so a lane fixes
  them all in one pass. Each window is asked again from the line after a
  hit until the predicate finds nothing more.
* The worker diffs the clone base against its commit of everything; here
  the base is `--base`, else `SWARM_CLONE_BASE` (the worker's own clone
  base, exported to the agent), else the shallow boundary of the worker's
  `--depth 1` clone, else the merge-base of HEAD with `origin/main` -- and
  never HEAD, which would leave every committed secret out of the diff (exit
  2 instead) -- diffed against the WORKING TREE; an untracked, unignored file
  is diffed against nothing, because the worker's `git add -A` will commit
  it.
* A task's REGISTERED secrets are known only to the worker, so they are not
  checked here; a registered value in the diff is still refused at publish.

OUTPUT. `path:line rule` on stdout for each hit, never any part of the
value; a one-line summary on stderr. Exit 0 when clean, 1 on any hit, 2
when the scan could not run -- an unknown base or a failed git -- which is
never reported as clean.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

from agent_worker.gitops import EMPTY_CLONE_BASE
from agent_worker.lifecycle import (
    CLONE_BASE_ENV,
    SCAN_OVERLAP_CHARS,
    SCAN_WINDOW_CHARS,
    CredentialHit,
    ScanHit,
    _credential_in,
    _DiffLeakScanner,
)

#: The worker's diff flags (`lifecycle.final_tree_leak`), so the scanner reads
#: the same stream shape it reads there.
_DIFF_FLAGS = [
    "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--no-renames",
    "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/", "-U0",
]
#: Repository config that must not run code or rewrite paths during a scan.
_GIT_CONFIG = [
    "-c", "core.quotePath=false", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
]


class ScanError(RuntimeError):
    """The scan could not run. Never read as a clean result."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_GIT_CONFIG, *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _empty_tree(repo: Path) -> str:
    made = _git(repo, "hash-object", "-t", "tree", "/dev/null")
    if made.returncode != 0 or not made.stdout.strip():
        raise ScanError("could not name the empty tree")
    return made.stdout.strip()


def _shallow_boundary(repo: Path) -> str | None:
    """The one commit a shallow clone's history stops at, or None.

    The worker's clone is `--depth 1 --single-branch` (`gitops.shallow_clone`),
    so its boundary IS the commit the clone landed on. More than one boundary
    (the agent fetched more) names no single base, so it is not guessed at."""
    shallow = _git(repo, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0 or shallow.stdout.strip() != "true":
        return None
    where = _git(repo, "rev-parse", "--git-path", "shallow")
    if where.returncode != 0 or not where.stdout.strip():
        return None
    path = Path(where.stdout.strip())
    try:
        lines = [line.strip() for line in (repo / path).read_text().splitlines() if line.strip()]
    except OSError:
        return None
    return lines[0] if len(lines) == 1 else None


def default_base(repo: Path, environ: Mapping[str, str] | None = None) -> str:
    """The base the worker's publish will diff from, or a ScanError.

    In order: `SWARM_CLONE_BASE`, which the worker exports to the agent
    (`Worker._build_child_env`; `empty` for a repository cloned with no
    commit, scanned against the empty tree); the shallow boundary of a worker
    clone; the merge-base of HEAD with `origin/main`, for a full checkout on
    a laptop. NEVER HEAD (#470's review): a worker clone of a pinned SHA or of
    a non-main ref has no origin/main, and HEAD as the base left every
    COMMITTED secret out of the diff, so the scan said clean over them.
    """
    env = os.environ if environ is None else environ
    exported = (env.get(CLONE_BASE_ENV) or "").strip()
    if exported == EMPTY_CLONE_BASE:
        return _empty_tree(repo)
    if exported:
        return exported  # `scan` verifies it; a bad one is an error, not a guess
    boundary = _shallow_boundary(repo)
    if boundary:
        return boundary
    merged = _git(repo, "merge-base", "HEAD", "origin/main")
    if merged.returncode == 0 and merged.stdout.strip():
        return merged.stdout.strip()
    raise ScanError(
        "could not determine the clone base; nothing was scanned "
        f"(no {CLONE_BASE_ENV}, no shallow boundary, no origin/main; pass --base)"
    )


class _AllHits:
    """A leak predicate that records EVERY hit and answers None, so the
    worker's scanner reads the whole diff instead of stopping at the first.

    The scanner names a hit's line from its own window state; this asks it
    the same way the scanner does when it records its own (`_scan`), so the
    lines here are the ones a refusal would name.

    Hits are listed ONE PER LINE: after a hit, the window is asked again from
    the next line, so a second credential on the same line is not listed. The
    line is still named, and fixing it is what the lane must do either way;
    restarting mid-line would cut a token's left context and could name a rule
    the worker never would.
    """

    def __init__(self) -> None:
        self.scanner: _DiffLeakScanner | None = None
        self.hits: list[ScanHit] = []
        self._seen: set[ScanHit] = set()

    def __call__(self, path: str, text: str) -> CredentialHit | None:
        scanner = self.scanner
        assert scanner is not None
        start = 0
        while start < len(text):
            found = _credential_in(path, text[start:])
            if found is None:
                break
            offset = start + found.offset
            line = scanner._file_line(scanner._window_line + text.count("\n", 0, offset))
            hit = ScanHit(path or "a file", found.rule, line)
            if hit not in self._seen:  # overlapping windows see a hit twice
                self._seen.add(hit)
                self.hits.append(hit)
            newline = text.find("\n", offset)
            if newline < 0:
                break
            start = newline + 1
        return None


def _stream(
    repo: Path, argv: list[str], feed: Callable[[bytes], None], ok_codes: tuple[int, ...]
) -> None:
    """Run git and feed its stdout to the scanner as it arrives. Its stderr is
    dropped rather than piped: an unread pipe that fills would stall git."""
    with subprocess.Popen(
        ["git", *_GIT_CONFIG, *argv], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as proc:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b""):
            feed(chunk)
        code = proc.wait()
    if code not in ok_codes:
        raise ScanError(f"git {argv[0]} exited {code}")


def scan(repo: Path, base: str) -> list[ScanHit]:
    """Every credential the working tree ADDS against `base`, in diff order."""
    # `^{tree}`: a commit, or the empty tree an empty repository's clone base names.
    verified = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{tree}}")
    if verified.returncode != 0:
        raise ScanError(f"the base {base!r} names no commit")
    leaks = _AllHits()
    scanner = _DiffLeakScanner(leaks, window=SCAN_WINDOW_CHARS, overlap=SCAN_OVERLAP_CHARS)
    leaks.scanner = scanner
    _stream(repo, ["diff", *_DIFF_FLAGS, verified.stdout.strip(), "--"], scanner.feed, (0,))
    untracked = _git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    if untracked.returncode != 0:
        raise ScanError("git ls-files failed")
    for name in (n for n in untracked.stdout.split("\0") if n):
        # `--no-index` exits 1 when the files differ, which they always do.
        _stream(
            repo, ["diff", "--no-index", *_DIFF_FLAGS, "/dev/null", name], scanner.feed, (0, 1)
        )
    scanner.close()
    return leaks.hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_worker.publish_scan",
        description="Run the worker's publish credential scan over what the working tree adds.",
    )
    parser.add_argument(
        "--base",
        help=(
            "the revision to diff against (default: $SWARM_CLONE_BASE, else the shallow "
            "boundary, else the merge-base with origin/main; never HEAD)"
        ),
    )
    args = parser.parse_args(argv)
    repo = Path.cwd()
    try:
        top = _git(repo, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise ScanError("not inside a git work tree")
        repo = Path(top.stdout.strip())
        base = args.base or default_base(repo)
        hits = scan(repo, base)
    except ScanError as exc:
        print(f"publish-scan: could not scan: {exc}", file=sys.stderr)
        return 2
    for hit in hits:
        print(f"{hit.path}:{hit.line} {hit.rule}")
    if hits:
        print(
            f"publish-scan: {len(hits)} credential-shaped line(s) added against {base[:12]}; "
            "the worker will refuse to publish this diff",
            file=sys.stderr,
        )
        return 1
    print(f"publish-scan: clean against {base[:12]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

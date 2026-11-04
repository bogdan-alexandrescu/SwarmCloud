#!/usr/bin/env python3
"""How many swarm-ui component-test shards a run gets, and which vitest files each one runs.

    ui_shards.py count   (the `changes` job: writes ui_shards=[1, ..., N] as `shards`)
    ui_shards.py pick    (each `swarm-ui component tests (k/N)` shard: writes its files)

Both read the environment the workflow sets: DURATIONS (tests/unit/scripts/
ui_test_durations.json) and GITHUB_OUTPUT; `pick` also SHARD, SHARDS
(`strategy.job-total`) and SHARD_FILES. They run from the repository's
checkout; the files `pick` writes are relative to apps/swarm-ui, one a line,
where vitest runs (`npm run test:components -- $(cat "${SHARD_FILES}")`). That
expansion splits on whitespace and expands globs, so a test file whose path
holds either is refused, and `pick` never writes an empty list: `vitest run`
with no file argument runs the whole suite.

WHY (owner request 2026-11-03, part of #2688). Measured on eight pull request
runs of application.yml: the job ran vitest's own `--shard=k/4`, which splits
by file COUNT, and its slowest shard took 15-25 minutes while the others took
10-14. Eight spacing.*.test.tsx slices weigh 259-485 s each against ~2.9 s for
a typical file, so whichever shard drew two or three of them set the pace, and
with main required up to date every merge waits for that shard. This does for
the component tests what unit_shards.py does for the Python unit tests.

THE WEIGHTS. ui_test_durations.json: vitest's own per-file seconds from main's
'swarm-ui component tests' job logs (scripts/refresh-ui-weights.sh,
scripts/lib/ui_weights.py), the file related-tests.sh already cuts its vitest
runs by. Only files over ui_weights.THRESHOLD_SECONDS are recorded; every
other file -- a new one included -- weighs related_tests.VITEST_DEFAULT_SECONDS.
One store, one default, read from where they are kept.

THE FILES. Exactly the ones vitest runs: the `include` globs of
apps/swarm-ui/vitest.config.ts, read from that file, under vitest's default
directory excludes.

THE SPLIT. Longest-first onto the lightest shard (LPT), deterministic, so every
shard computes the same split and takes its own part: every file runs exactly
once. vitest reads a file argument as a case-insensitive SUBSTRING filter on
the path, so `src/a.test.ts` would also run `src/a.test.tsx`; files where one
path contains another are placed together, as one unit, so no filter can reach
a file another shard runs.

THE COUNT. About SHARD_TARGET_SECONDS of measured time a shard, at most
MAX_SHARDS, and never more shards than units to give them (a shard with no
file would make vitest exit "No test files found").

tests/unit/scripts/test_ui_shards.py runs both commands.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from related_tests import UI, VITEST_DEFAULT_SECONDS  # noqa: E402

# About this much of vitest's reported time a shard (owner's brief,
# 2026-11-03). vitest runs a shard's files on availableParallelism() - 1 = 3
# workers on the 4-vCPU runner, so ~600 s is ~200 s of wall time when nothing
# in the shard is long; a shard holding one 485 s spacing slice takes about
# that slice's time, which is the floor any split has.
SHARD_TARGET_SECONDS = 600
# On 2026-11-03 the suite weighed ~5,000 s (857 files, eight slices of
# 259-485 s): ten at the target, so eight -- one slice a shard, ~626 s each.
# Each shard is a runner and an `npm ci` (~60 s); more would add a runner apiece
# to the queue when a dozen pull requests arrive together.
MAX_SHARDS = 8

CONFIG = "vitest.config.ts"
# vitest 3.2's default `exclude`, as directories: a file under one never runs.
EXCLUDED_DIRS = frozenset({"node_modules", "dist", "cypress", ".idea", ".git", ".cache", ".output", ".temp"})
# What the vitest step's unquoted `$(cat ...)` would split on or expand.
UNSAFE = re.compile(r"[\s*?\[\]{}$`'\"\\]")


def include_globs(ui: Path) -> list[str]:
    text = (ui / CONFIG).read_text()
    match = re.search(r"^\s*include:\s*\[([^\]]*)\]", text, re.M)
    globs = re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)) if match else []
    if not globs:
        sys.exit(f"::error::no `include: [...]` list of test globs in {ui / CONFIG}")
    return globs


def test_files(ui: Path) -> list[str]:
    """vitest's test files, relative to the swarm-ui directory."""
    found: set[str] = set()
    for glob in include_globs(ui):
        for path in ui.glob(glob):
            rel = path.relative_to(ui)
            if path.is_file() and not EXCLUDED_DIRS.intersection(rel.parts[:-1]):
                found.add(rel.as_posix())
    if not found:
        sys.exit(f"::error::no vitest test file under {ui}: the checkout is not the repository")
    unsafe = sorted(f for f in found if UNSAFE.search(f))
    if unsafe:
        sys.exit(f"::error::vitest test path(s) with whitespace or a shell glob or quote character, which the "
                 f"component-test step would split or expand: {unsafe}")
    return sorted(found)


def units(files: list[str]) -> list[list[str]]:
    """The files grouped so that no file's path is a substring of a file in
    another group (vitest's filter is `path.toLowerCase().includes(arg)`)."""
    parent = {f: f for f in files}

    def root(f: str) -> str:
        while parent[f] != f:
            f = parent[f]
        return f

    lower = [(f, f.lower()) for f in files]
    for a, la in lower:
        for b, lb in lower:
            if a != b and la in lb:
                ra, rb = root(a), root(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = {}
    for f in files:
        groups.setdefault(root(f), []).append(f)
    return sorted(groups.values())


def weights() -> dict[str, float]:
    recorded = json.loads(Path(os.environ["DURATIONS"]).read_text())
    if not isinstance(recorded, dict):
        sys.exit(f"::error::{os.environ['DURATIONS']} is not a JSON object of file: seconds")
    return {str(k): float(v) for k, v in recorded.items()}


def weight(unit: list[str], recorded: dict[str, float]) -> float:
    return sum(recorded.get(f, VITEST_DEFAULT_SECONDS) for f in unit)


def shard_count(total_seconds: float, unit_count: int) -> int:
    return max(1, min(MAX_SHARDS, unit_count, math.ceil(total_seconds / SHARD_TARGET_SECONDS)))


def split(groups: list[list[str]], shards: int, recorded: dict[str, float]) -> tuple[list[list[str]], list[float]]:
    load = [0.0] * shards
    parts: list[list[str]] = [[] for _ in range(shards)]
    for unit in sorted(groups, key=lambda u: (-weight(u, recorded), u)):
        k = min(range(shards), key=lambda i: (load[i], i))
        load[k] += weight(unit, recorded)
        parts[k].extend(unit)
    return [sorted(p) for p in parts], load


def _output(line: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"{line}\n")


def _plan() -> tuple[list[str], list[list[str]], dict[str, float]]:
    files = test_files(Path(UI))
    return files, units(files), weights()


def count() -> int:
    files, groups, recorded = _plan()
    total = sum(weight(u, recorded) for u in groups)
    n = shard_count(total, len(groups))
    _output(f"shards={json.dumps(list(range(1, n + 1)))}")
    weighed = sum(1 for f in files if f in recorded)
    print(f"{len(files)} vitest file(s), {weighed} with a recorded weight, the rest at "
          f"{VITEST_DEFAULT_SECONDS} s: ~{total:.0f} s of measured test time, {n} shard(s) "
          f"(~{SHARD_TARGET_SECONDS} s a shard, at most {MAX_SHARDS})")
    return 0


def pick() -> int:
    shard, shards = int(os.environ["SHARD"]), int(os.environ["SHARDS"])
    if not 1 <= shard <= shards:
        sys.exit(f"::error::shard {shard} of {shards} does not exist")
    files, groups, recorded = _plan()
    if shards > len(groups):
        sys.exit(f"::error::{shards} shards for {len(groups)} unit(s) of vitest files: a shard would run nothing")
    parts, load = split(groups, shards, recorded)
    mine = parts[shard - 1]
    if not mine:
        sys.exit(f"::error::shard {shard} of {shards} was handed no vitest file")
    Path(os.environ["SHARD_FILES"]).write_text("".join(f"{f}\n" for f in mine))
    print(f"{len(files)} vitest file(s) in {shards} shard(s); shard {shard} runs {len(mine)}, "
          f"~{load[shard - 1]:.0f} s of measured test time (shards: {', '.join(f'{x:.0f}' for x in load)})")
    for f in mine:
        print(f"  {f}")
    _output(f"count={len(mine)}")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["count"]:
        return count()
    if argv == ["pick"]:
        return pick()
    sys.exit("usage: ui_shards.py count | pick")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

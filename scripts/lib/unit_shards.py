#!/usr/bin/env python3
"""How many unit-test shards a run gets, and which test files each one runs.

    unit_shards.py count   (the `changes` job: writes shards=[1, ..., N])
    unit_shards.py pick    (each `unit tests (k/N)` shard: writes its files)

Both read the environment the workflow sets: PYTHON_MODE (all | selected |
subset), PYTHON_AREAS (subset: docs, swarm-ui), PYTHON_TESTS (selected: the
test files the change reaches), DURATIONS (tests/unit/scripts/
unit_test_durations.json) and GITHUB_OUTPUT; `pick` also SHARD, SHARDS
(`strategy.job-total`) and SHARD_FILES. Paths are relative to the working
directory, the repository's checkout.

THE SPLIT (owner request 2026-10-07). Longest-first onto the lightest shard
(LPT), weighted by the measured seconds per file, a file with no weight at the
median. Deterministic, so every shard computes the same split and takes its
own part: every file runs exactly once.

THE COUNT (LANE CI-EARLY, part of #1694, owner decision 2026-10-20). It was a
fixed four, sized on 2026-10-07 for ~2,950 s of measured test time. The
weights had grown to ~7,000 s by 2026-10-20, so a full run's shard carried
~1,750 s -- a shard p90 of 455-501 s (max 829) against a 152-188 s median,
the median being pull requests' small selections. Now the count follows the
weight of what this run actually runs: about SHARD_TARGET_SECONDS a shard, up
to MAX_SHARDS. A full run spreads wider; a small selection takes one runner
instead of four, and runners were the largest share of pull-request wall time
(docs/ci.md, "Where pull-request CI time goes").

THE SELECTION. python=subset (a change confined to docs/ and apps/swarm-ui/)
runs only the files that read those areas: a module whose source builds a
path through the directory -- `REPO / "docs"`, "docs/ci.md",
"apps/swarm-ui/src", `/ "swarm-ui"` -- then every module that imports one of
those (a helper, or a test module used as one: test_issue_forms imports
test_nav_headings_agree), and everything under a conftest that reads it, until
nothing is added. A mention in prose -- "(docs/ci.md)" in a docstring -- is not
a read, nor is a dict key named "docs": matching those pulled in 496 of 551
files. Over-inclusion is the safe direction; a test that builds the path some
other way is missed here and caught by main's push run, which runs
everything. python=selected (B2) runs the files the `changes` job chose, plus
-- when it touches docs/ or apps/swarm-ui/ -- the area readers, exactly as
subset picks them.

tests/unit/scripts/test_unit_shards.py runs both commands.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

# About this much measured test time a shard. ~600 s of junit time is ~150 s
# of pytest on a 4-vCPU runner under `-n auto --dist worksteal` (the ratio
# the 2026-10-20 sample shows: ~1,750 s a shard ran at a ~470 s p90 with
# setup), so a shard of a selection and a shard of a full run take about the
# same wall time.
SHARD_TARGET_SECONDS = 600
# A full run (~7,000 s on 2026-10-20) got eight: ~875 s a shard, at most
# ~1.5x the target, which is the tail the owner asked for. On 2026-10-23 the
# night's new test files pushed a full run's shard just past 1.5x (900.1-900.2 s
# against a 900 s bound), and test_unit_shards.py went red on every pull
# request. Nine keeps the owner's 1.5x tail (~800 s a shard) for one more
# runner a full run; relaxing the 1.5x instead would drop the owner's rule.
# On 2026-10-29 the weights reached ~8,100 s (1,514 files on a pull request's
# merge with main) and nine shards carried 900.1-900.2 s again, red on pull
# request #2752: ten puts a full run's shard back at ~810 s, under the bound
# with room for the next ~900 s of new tests. More would add a runner apiece to
# the queue when a dozen pull requests arrive together; fewer puts a full
# run's shard back over the bound.
MAX_SHARDS = 10

# Below this share of the run's test files with a stored weight, `count`
# warns that the weights are worth refreshing -- and only warns (#2364). It
# was a hard floor in test_unit_shards.py, and new test files landing between
# hand refreshes turned main and every pull request red at 1069 of 1337. An
# unweighted file runs at the median weight, so staleness only unbalances the
# split; it never drops a file (scripts/refresh-unit-weights.sh refreshes).
WEIGHT_COVERAGE_WARN = 0.8

MODES = ("all", "selected", "subset")


def _is_test(path: Path) -> bool:
    # pytest's default python_files; pyproject.toml does not change it.
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def selected_tests(mode: str, areas: list[str], listed: set[str]) -> list[Path]:
    modules = sorted(p for p in Path("tests/unit").rglob("*.py") if "__pycache__" not in p.parts)
    tests = [p for p in modules if _is_test(p)]
    if not tests:
        sys.exit("::error::no test files under tests/unit: the checkout is not the repository")
    if mode == "all":
        return tests
    if mode not in ("subset", "selected"):
        sys.exit(f"::error::python={mode!r}: this job runs only on all, selected or subset")
    chosen: set[Path] = set()
    if mode == "selected":
        if not listed:
            sys.exit("::error::python=selected with no test file listed")
        chosen |= {p for p in tests if str(p) in listed}
    if mode == "subset" or areas:
        def names_dir(d: str) -> str:
            # `REPO / "docs"`, "docs/ci.md", Path("docs"), joinpath(.., "docs") --
            # a path, not the word: `state["docs"]` is a Firestore fake.
            q = "[\"']"
            return rf"/\s*{q}{d}{q}|{q}(?:\./)?{d}/|(?:Path|joinpath)\(\s*{q}{d}{q}|,\s*{q}{d}{q}\s*[,)]"
        reads = {"docs": names_dir("docs"), "swarm-ui": names_dir("swarm-ui") + r"|[\"'](?:\./)?apps/swarm-ui/"}
        if not areas or any(a not in reads for a in areas):
            sys.exit(f"::error::python=subset with areas {areas!r}; known: {sorted(reads)}")
        pattern = re.compile("|".join(reads[a] for a in areas))
        text = {p: p.read_text(errors="replace") for p in modules}
        # The names each module imports: every word of its import
        # statements, so `from .fakes import X`, `from control_plane.
        # test_nav_headings_agree import Y` and `from . import fakes` all
        # name their module. A word in prose names nothing.
        imports = re.compile(r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)|import[ \t]+([^\n]+))", re.M)
        words = {p: {w for m in imports.finditer(text[p]) for g in m.groups() if g for w in re.findall(r"\w+", g)}
                 for p in modules}
        reach = {p for p in modules if pattern.search(text[p])}
        todo = sorted(reach)
        while todo:
            module = todo.pop()
            if module.name == "conftest.py":
                more = {p for p in modules if module.parent in p.parents}
            elif module.name == "__init__.py":
                more = set()
            else:
                more = {p for p in modules if module.stem in words[p]}
            todo.extend(sorted(more - reach))
            reach |= more
        chosen |= {p for p in tests if p in reach}
    return [p for p in tests if p in chosen]


def _weights() -> tuple[dict[str, float], float]:
    weights = json.loads(Path(os.environ["DURATIONS"]).read_text())
    known = sorted(weights.values())
    return weights, (known[len(known) // 2] if known else 1.0)


def _from_env() -> list[Path]:
    mode = os.environ["PYTHON_MODE"]
    areas = os.environ.get("PYTHON_AREAS", "").split()
    listed = set(os.environ.get("PYTHON_TESTS", "").split())
    return selected_tests(mode, areas, listed)


def shard_count(total_seconds: float) -> int:
    return max(1, min(MAX_SHARDS, math.ceil(total_seconds / SHARD_TARGET_SECONDS)))


def split(tests: list[Path], shards: int, weights: dict[str, float], default: float) -> tuple[list[list[str]], list[float]]:
    load = [0.0] * shards
    parts: list[list[str]] = [[] for _ in range(shards)]
    for path in sorted(tests, key=lambda p: (-weights.get(str(p), default), str(p))):
        k = min(range(shards), key=lambda i: (load[i], i))
        load[k] += weights.get(str(path), default)
        parts[k].append(str(path))
    return parts, load


def _output(line: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"{line}\n")


def count() -> int:
    mode = os.environ["PYTHON_MODE"]
    if mode == "none":
        # The unit job is skipped by its own `if:`; the matrix still has to parse.
        _output("shards=[1]")
        print("python=none: the unit shards do not run")
        return 0
    tests = _from_env()
    weights, default = _weights()
    total = sum(weights.get(str(p), default) for p in tests)
    n = shard_count(total)
    _output(f"shards={json.dumps(list(range(1, n + 1)))}")
    print(f"{mode}: {len(tests)} test file(s), ~{total:.0f} s of measured test time: {n} shard(s) "
          f"(~{SHARD_TARGET_SECONDS} s a shard, at most {MAX_SHARDS})")
    weighted = sum(1 for p in tests if str(p) in weights)
    if tests and weighted < WEIGHT_COVERAGE_WARN * len(tests):
        line = (f"the shard weights cover {weighted} of {len(tests)} test files "
                f"({weighted / len(tests):.0%}, under {WEIGHT_COVERAGE_WARN:.0%}): the rest run at the "
                f"median weight, so the split drifts out of balance; refresh them with "
                f"scripts/refresh-unit-weights.sh (docs/ci.md, \"Refreshing the shard weights\")")
        print(f"::warning::{line}")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
                out.write(f"**Stale unit shard weights:** {line}.\n\n")
    return 0


def pick() -> int:
    shard, shards = int(os.environ["SHARD"]), int(os.environ["SHARDS"])
    if not 1 <= shard <= shards:
        sys.exit(f"::error::shard {shard} of {shards} does not exist")
    mode = os.environ["PYTHON_MODE"]
    tests = _from_env()
    weights, default = _weights()
    parts, load = split(tests, shards, weights, default)
    mine = sorted(parts[shard - 1])
    Path(os.environ["SHARD_FILES"]).write_text("".join(f"{f}\n" for f in mine))
    print(f"{mode}: {len(tests)} test file(s) in {shards} shard(s); shard {shard} runs {len(mine)}, "
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
    sys.exit("usage: unit_shards.py count | pick")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

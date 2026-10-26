#!/usr/bin/env python3
"""Merge main's measured unit-test durations over the stored shard weights.

    merge_unit_weights.py --stored tests/unit/scripts/unit_test_durations.json \
        --measured DIR [--root .] [--out PATH]

DIR is what `gh run download <run> -p 'unit-durations-*' -D DIR` wrote: one
`unit-durations-<k>/unit-durations.json` per python-unit shard of a push run on
main (application.yml, "record this shard's measured durations"). --out
defaults to --stored, rewritten in place.

THE MERGE (#2364). It replaced docs/ci.md's `jq -S -s add`, which wrote what
one run measured and nothing else: a file a run did not reach lost its weight,
and a deleted file's weight was kept only because nothing removed it. Now:

  * a measured weight replaces the stored one (the newest measurement wins);
  * a stored file that no longer exists under tests/unit is dropped;
  * an existing file nobody measured keeps its stored weight;
  * a measured path outside tests/unit, or not in the tree, is ignored.

Values are rounded to 0.1 s and floored at 0.1, keys sorted, and the file is
written exactly as the shards write theirs (indent=1, sort_keys, trailing
newline), so a refresh that changes nothing changes no byte and
scripts/refresh-unit-weights.sh opens no pull request.

Prints `key=value` lines on stdout -- artifacts, measured, dropped, test_files,
weighted_before, weighted_after -- for the script's commit and pull-request
text. A download with no unit-durations.json, or one that is not a JSON object
of file -> seconds, is refused: an empty refresh is a failure, never a clean
one (CLAUDE.md, rule zero).

tests/unit/scripts/test_unit_weights_refresh.py holds it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path

# The same rule the shards' selection uses for what a test file is.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unit_shards import _is_test  # noqa: E402

PREFIX = "tests/unit/"
ARTIFACT_FILE = "unit-durations.json"


def _seconds(path: str, value: object) -> float:
    # bool is an int to Python; `true` is not a duration.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{path}: {value!r} is not a number of seconds")
    return round(max(float(value), 0.1), 1)


def merge(stored: dict[str, object], measured: Iterable[dict[str, object]],
          present: Iterable[str]) -> dict[str, float]:
    """The new weights: `measured` over `stored`, limited to `present` test files."""
    files = {p for p in present if p.startswith(PREFIX)}
    out = {path: _seconds(path, v) for path, v in stored.items() if path in files}
    fresh: dict[str, float] = {}
    for shard in measured:
        if not isinstance(shard, dict):
            raise ValueError(f"a measurement is {type(shard).__name__}, not an object of file -> seconds")
        for path, v in shard.items():
            seconds = _seconds(path, v)
            if path in files:
                # A file runs in exactly one shard; if two ever name it, the
                # larger is kept so the result does not depend on their order.
                fresh[path] = max(seconds, fresh.get(path, 0.0))
    out.update(fresh)
    return dict(sorted(out.items()))


def dumps(weights: dict[str, float]) -> str:
    return json.dumps(weights, indent=1, sort_keys=True) + "\n"


def present_tests(root: Path) -> list[str]:
    unit = root / "tests" / "unit"
    return sorted(
        p.relative_to(root).as_posix()
        for p in unit.rglob("*.py")
        if "__pycache__" not in p.parts and _is_test(p)
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stored", required=True, type=Path)
    parser.add_argument("--measured", required=True, type=Path)
    parser.add_argument("--root", default=Path("."), type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    artifacts = sorted(args.measured.rglob(ARTIFACT_FILE))
    if not artifacts:
        sys.exit(f"merge_unit_weights: no {ARTIFACT_FILE} under {args.measured}: "
                 f"the run's unit-durations-* artifacts were not downloaded")
    present = present_tests(args.root)
    if not present:
        sys.exit(f"merge_unit_weights: no test files under {args.root / 'tests' / 'unit'}")
    try:
        stored = json.loads(args.stored.read_text())
        measured = [json.loads(p.read_text()) for p in artifacts]
        if not isinstance(stored, dict):
            raise ValueError(f"{args.stored} is not an object of file -> seconds")
        merged = merge(stored, measured, present)
    except ValueError as exc:  # json.JSONDecodeError is one
        sys.exit(f"merge_unit_weights: {exc}")

    files = set(present)
    accepted = {path for shard in measured for path in shard if path in files}
    (args.out or args.stored).write_text(dumps(merged))
    print(f"artifacts={len(artifacts)}")
    print(f"measured={len(accepted)}")
    print(f"dropped={sum(path not in files for path in stored)}")
    print(f"test_files={len(present)}")
    print(f"weighted_before={sum(path in stored for path in present)}")
    print(f"weighted_after={sum(path in merged for path in present)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

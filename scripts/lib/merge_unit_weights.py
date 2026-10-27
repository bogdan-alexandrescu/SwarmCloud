#!/usr/bin/env python3
"""Merge the per-file seconds main's unit shards measured into the stored
shard weights (#2364).

    merge_unit_weights.py --stored FILE --measured DIR [--root DIR]

WHY. tests/unit/scripts/unit_test_durations.json was refreshed only by hand,
with `jq -S -s add` over the downloaded artifacts. Between refreshes new test
files went unweighted until a hard coverage floor turned every pull request
red; scripts/refresh-unit-weights.sh now downloads main's `unit-durations-<k>`
artifacts and hands them here.

THE MERGE, against the checkout at --root:

  * a measured weight replaces the stored one -- the run is newer;
  * a stored file that no longer exists is dropped;
  * a file that exists but this run did not measure keeps its stored weight: a
    run that selected a subset, or a shard that failed to upload, is not
    evidence the file got lighter;
  * a measured path that is not an existing tests/unit/**.py file is ignored.

The output is the shards' own artifact format -- sorted keys, indent 1, a
trailing newline -- so a refresh with nothing new changes no byte. DIR holding
no `unit-durations.json`, or one that is not an object of positive numbers, is
refused with exit 1 and nothing is written: an unread download is never
"nothing to refresh". tests/unit/scripts/test_unit_weights_refresh.py holds
each rule.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ARTIFACT_NAME = "unit-durations.json"
TEST_PATH = re.compile(r"tests/unit/[\w/.-]+\.py")


def merge(stored: dict[str, float], measured: dict[str, float], root: Path) -> dict[str, float]:
    def exists(rel: str) -> bool:
        return bool(TEST_PATH.fullmatch(rel)) and (root / rel).is_file()

    out = {k: v for k, v in stored.items() if exists(k)}
    out.update({k: v for k, v in measured.items() if exists(k)})
    return dict(sorted(out.items()))


def read_measured(directory: Path) -> dict[str, float]:
    """Every ARTIFACT_NAME under `directory`, one per shard. A file appears in
    one shard only; if two ever name it, the heavier figure is kept."""
    files = sorted(directory.rglob(ARTIFACT_NAME)) if directory.is_dir() else []
    if not files:
        raise ValueError(f"no {ARTIFACT_NAME} under {directory}")
    measured: dict[str, float] = {}
    for path in files:
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"{path} is not JSON: {exc}") from exc
        if not isinstance(data, dict) or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in data.values()):
            raise ValueError(f"{path} is not an object of positive per-file seconds")
        for k, v in data.items():
            measured[k] = max(float(v), measured.get(k, 0.0))
    return measured


def render(weights: dict[str, float]) -> str:
    return json.dumps(weights, indent=1, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stored", required=True, help="the weights file, rewritten in place")
    ap.add_argument("--measured", required=True, help="directory of downloaded unit-durations-<k> artifacts")
    ap.add_argument("--root", default=".", help="the checkout the test paths are relative to")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    stored_path = Path(args.stored)
    try:
        measured = read_measured(Path(args.measured))
    except ValueError as exc:
        print(f"merge_unit_weights: {exc}", file=sys.stderr)
        return 1
    stored = json.loads(stored_path.read_text()) if stored_path.is_file() else {}
    merged = merge(stored, measured, root)
    tests = [p for p in (root / "tests" / "unit").rglob("*.py")
             if "__pycache__" not in p.parts and (p.name.startswith("test_") or p.name.endswith("_test.py"))]
    covered = sum(1 for p in tests if str(p.relative_to(root)) in merged)
    before = sum(1 for p in tests if str(p.relative_to(root)) in stored)
    text = render(merged)
    changed = not stored_path.is_file() or stored_path.read_text() != text
    if changed:
        stored_path.write_text(text)
    used = sum(1 for k in measured if k in merged)
    print(f"{len(measured)} measured file(s), {used} used; {len(set(stored) - set(merged))} dropped; "
          f"the weights cover {covered} of {len(tests)} test files (before: {before}); "
          f"{'rewrote' if changed else 'unchanged:'} {stored_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

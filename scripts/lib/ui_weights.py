#!/usr/bin/env python3
"""Per-file vitest CI-seconds from a vitest log, for related-tests.sh's weighted
vitest runs (UI-WEIGHTS, part of #2362).

    ui_weights.py --log FILE|- --out tests/unit/scripts/ui_test_durations.json
                  [--threshold SECONDS] [--replace]

WHY. related_tests.py cuts the vitest selection into runs of at most
VITEST_RUN_WEIGHT CI-seconds, from ui_test_durations.json. Cut by file count
alone, the nine spacing.*.test.tsx slices (252-485 s each) shared one 50-file
run that could not finish inside the 540 s each run is held to, and 40+ agents
timed out (#2362). scripts/refresh-ui-weights.sh feeds this the logs of main's
'swarm-ui component tests' jobs, where vitest's default reporter prints one
line a file:

    ✓ src/__tests__/spacing.capacity.test.tsx (1 test) 259018ms

(`ms`, or `s` with a fraction; `×`/`❯` for a failed file; ANSI colour codes
around any part, and GitHub's job/step/timestamp prefix before it).

WHAT IT WRITES. Only files over --threshold (default THRESHOLD_SECONDS): every
other file weighs related_tests.py's VITEST_DEFAULT_SECONDS, so recording it
would only make the file long. A file seen twice keeps its longer time.

By default a figure already recorded is never LOWERED: the seeded figures are
the agent-container times the 540 s limit is measured against, and a CI shard
that ran a slice faster would put two slices back in one run there. --replace
writes the measured figures alone (a file no longer over the threshold drops).

Pure standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 20 s is about seven times VITEST_DEFAULT_SECONDS (2.9 s): a file under it
# moves a 540 s run's estimate by under 4%, so it is left to the default.
THRESHOLD_SECONDS = 20.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FILE_LINE = re.compile(
    r"[✓✔×✗❯↓]\s+(?P<path>\S+\.test\.[cm]?[jt]sx?)\s+\((?P<count>\d+) tests?[^)]*\)"
    r"\s+(?P<time>\d+(?:\.\d+)?)(?P<unit>ms|s)\b"
)


def parse_log(text: str) -> dict[str, float]:
    """Each test file's seconds, as vitest's default reporter printed them."""
    seconds: dict[str, float] = {}
    for raw in text.splitlines():
        line = _ANSI.sub("", raw)
        match = _FILE_LINE.search(line)
        if not match:
            continue
        value = float(match["time"])
        if match["unit"] == "ms":
            value /= 1000
        path = match["path"]
        seconds[path] = max(seconds.get(path, 0.0), round(value, 1))
    return seconds


def merge(recorded: dict[str, float], measured: dict[str, float], *,
          threshold: float, replace: bool) -> dict[str, float]:
    kept = {p: s for p, s in measured.items() if s > threshold}
    if not replace:
        for path, value in recorded.items():
            kept[path] = max(kept.get(path, 0.0), value)
    return dict(sorted(kept.items()))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", required=True, help="vitest log file, or - for stdin")
    ap.add_argument("--out", required=True, help="ui_test_durations.json to rewrite")
    ap.add_argument("--threshold", type=float, default=THRESHOLD_SECONDS)
    ap.add_argument("--replace", action="store_true",
                    help="write the measured figures alone, lowering or dropping recorded ones")
    args = ap.parse_args(argv)
    text = sys.stdin.read() if args.log == "-" else Path(args.log).read_text(errors="replace")
    measured = parse_log(text)
    if not measured:
        # An empty parse is a log in another shape, never "every file is light".
        print("ui_weights: no vitest per-file timing line in the log; nothing written", file=sys.stderr)
        return 1
    out = Path(args.out)
    try:
        recorded = json.loads(out.read_text())
    except (OSError, ValueError):
        recorded = {}
    if not isinstance(recorded, dict):
        recorded = {}
    weights = merge(recorded, measured, threshold=args.threshold, replace=args.replace)
    out.write_text(json.dumps(weights, indent=2) + "\n")
    print(f"ui_weights: {len(measured)} file timing(s) read, {len(weights)} recorded over "
          f"{args.threshold:g} s in {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

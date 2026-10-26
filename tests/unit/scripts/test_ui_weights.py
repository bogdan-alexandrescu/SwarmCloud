"""scripts/lib/ui_weights.py turns vitest's per-file timing lines into the
CI-seconds related-tests.sh cuts its vitest runs by (UI-WEIGHTS, part of
#2362), and scripts/refresh-ui-weights.sh feeds it main's CI logs.

The log is built here, in the shape `gh run view --job <id> --log` returns: a
job/step/timestamp prefix, then vitest's default reporter line with its ANSI
colour codes. Nothing reads GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PARSER = REPO / "scripts" / "lib" / "ui_weights.py"
SCRIPT = REPO / "scripts" / "refresh-ui-weights.sh"

ESC = "\x1b"
GREEN, RED, DIM, YELLOW, RESET = (f"{ESC}[32m", f"{ESC}[31m", f"{ESC}[2m", f"{ESC}[33m", f"{ESC}[39m")
PREFIX = "swarm-ui component tests (2/4)\tcomponent tests\t2026-10-26T05:12:44.1234567Z "


def _parser():
    spec = importlib.util.spec_from_file_location("ui_weights_under_test", PARSER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _log() -> str:
    return "\n".join([
        PREFIX + f" {GREEN}✓{RESET} src/__tests__/spacing.capacity.test.tsx "
        f"{DIM}(1 test){RESET} {YELLOW}259018ms{RESET}",
        PREFIX + f" {GREEN}✓{RESET} src/__tests__/spacing.accounts.test.tsx {DIM}(1 test){RESET} 436.97s",
        PREFIX + f" {RED}×{RESET} src/__tests__/keyboard.test.tsx {DIM}(12 tests | 1 failed){RESET} 31240ms",
        PREFIX + f" {GREEN}✓{RESET} src/__tests__/light.test.ts {DIM}(3 tests){RESET} 812ms",
        PREFIX + f" {GREEN}✓{RESET} src/__tests__/twice.test.tsx {DIM}(2 tests){RESET} 25000ms",
        PREFIX + f" {GREEN}✓{RESET} src/__tests__/twice.test.tsx {DIM}(2 tests){RESET} 41000ms",
        PREFIX + "   Test Files  463 passed (463)",
        PREFIX + "   Duration  1322.45s (transform 31.2s, setup 12.0s)",
    ]) + "\n"


def test_vitest_log_lines_with_ansi_codes_become_per_file_seconds():
    got = _parser().parse_log(_log())
    # MUTATION: match without stripping ANSI codes -> the coloured lines
    # (every line of a CI log) parse to nothing.
    # MUTATION: read `ms` as seconds, or `s` as milliseconds -> 259018 / 0.437.
    assert got["src/__tests__/spacing.capacity.test.tsx"] == 259.0
    assert got["src/__tests__/spacing.accounts.test.tsx"] == 437.0
    # MUTATION: read only passed (✓) files -> a failed file's time is lost.
    assert got["src/__tests__/keyboard.test.tsx"] == 31.2
    assert got["src/__tests__/light.test.ts"] == 0.8
    # MUTATION: keep the last figure for a file seen twice -> 25.0.
    assert got["src/__tests__/twice.test.tsx"] == 41.0
    # MUTATION: match the summary's `Duration` line as a file -> extra keys.
    assert len(got) == 5


def test_the_parser_keeps_files_over_the_threshold_and_never_lowers_a_recorded_one(tmp_path):
    log = tmp_path / "vitest.log"
    log.write_text(_log())
    out = tmp_path / "ui_test_durations.json"
    out.write_text(json.dumps({"src/__tests__/spacing.capacity.test.tsx": 300,
                               "src/__tests__/gone.test.tsx": 90}))
    result = subprocess.run([sys.executable, str(PARSER), "--log", str(log), "--out", str(out)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(out.read_text())
    # MUTATION: write every file -> light.test.ts (0.8 s) is recorded.
    assert "src/__tests__/light.test.ts" not in got
    # MUTATION: overwrite a recorded figure with a lower CI one -> 259.0.
    assert got["src/__tests__/spacing.capacity.test.tsx"] == 300
    assert got["src/__tests__/gone.test.tsx"] == 90
    assert got["src/__tests__/spacing.accounts.test.tsx"] == 437.0

    result = subprocess.run([sys.executable, str(PARSER), "--log", str(log), "--out", str(out),
                             "--replace"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(out.read_text())
    # MUTATION: ignore --replace -> the recorded 300 and the gone file stay.
    assert got["src/__tests__/spacing.capacity.test.tsx"] == 259.0
    assert "src/__tests__/gone.test.tsx" not in got


def test_a_log_with_no_timing_line_writes_nothing_and_fails(tmp_path):
    log = tmp_path / "vitest.log"
    log.write_text(PREFIX + "npm error code ERESOLVE\n")
    out = tmp_path / "ui_test_durations.json"
    out.write_text('{"src/__tests__/a.test.tsx": 99}\n')
    result = subprocess.run([sys.executable, str(PARSER), "--log", str(log), "--out", str(out)],
                            capture_output=True, text=True, timeout=60)
    # MUTATION: read an empty parse as "every file is light" -> exit 0 and the
    # recorded weights rewritten.
    assert result.returncode == 1
    assert out.read_text() == '{"src/__tests__/a.test.tsx": 99}\n'


def test_the_refresh_script_has_the_house_shape():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = next(line.strip() for line in text.splitlines()[1:]
                     if line.strip() and not line.strip().startswith("#"))
    # MUTATION: drop `set -euo pipefail` or move it below other code -> red.
    assert effective == "set -euo pipefail"
    assert "lib/common.sh" in text
    # MUTATION: write the weights from a workflow change instead -> the script
    # stops reading the jobs' logs with gh.
    assert "gh run view" in text and "--log" in text and "ui_weights.py" in text
    assert SCRIPT.stat().st_mode & 0o111

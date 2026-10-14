"""scripts/accept-live-browser-streak.sh: when may live-browser go back into the waves.

Owner decision 2026-10-14 (lane SPEED-C2): the live-browser acceptance group
failed 18 of 34 post-release runs of accept.yml, against 0-3 for every other
group, and each red run updated the one acceptance bug. It moved off the red
path, onto a schedule of its own (accept.yml's `live-browser` job, every three
hours). It moves back when the scheduled job has passed FIVE runs in a row;
this script is what says so, and docs/ci.md tells the operator to do it.

These RUN the script against a fake `gh` that answers `gh run list` from a
fixture and records every call, so the counting is exercised, not read:

* the streak counts completed scheduled runs, newest first, and stops at the
  first that did not succeed;
* a cancelled or skipped run (a post-release run's quiesce cancels a
  scheduled one in flight) neither counts nor breaks the streak;
* five in a row exits 0 and says to promote; fewer exits 3; an unreadable
  listing fails loudly rather than reporting a streak of zero as an answer;
* it asks for scheduled runs of accept.yml on main only.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "accept-live-browser-streak.sh"

FAKE_GH = r"""#!{python}
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    if os.environ.get("FAKE_GH_FAIL"):
        sys.stderr.write("HTTP 502\n"); sys.exit(1)
    sys.stdout.write(os.environ.get("FAKE_GH_RUNS", "[]"))
else:
    sys.stderr.write(f"fake gh: {args} is not modelled\n"); sys.exit(2)
"""


def _runs(*conclusions: str) -> str:
    """Newest first, as `gh run list` answers."""
    return json.dumps([
        {"databaseId": 1000 - i, "conclusion": c, "createdAt": f"2026-10-14T{23 - i:02d}:17:00Z",
         "url": f"https://github.com/o/r/actions/runs/{1000 - i}"}
        for i, c in enumerate(conclusions)
    ])


def _streak(tmp_path: Path, runs: str = "[]", *args: str, **env: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "gh.log"
    proc = subprocess.run(
        [str(SCRIPT), *args],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_"))},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "NO_COLOR": "1",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_RUNS": runs,
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return proc, calls


def _out(proc) -> dict:
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def test_five_green_scheduled_runs_in_a_row_say_promote(tmp_path):
    proc, calls = _streak(tmp_path, _runs(*["success"] * 5, "failure"))
    assert proc.returncode == 0, proc.stderr
    assert _out(proc) == {"streak": "5", "need": "5", "promote": "true"}, proc.stdout
    assert "move it back" in proc.stderr


def test_a_failure_breaks_the_streak(tmp_path):
    """MUTATION: count every success in the window instead of stopping at the
    first failure. Four greens around one red would then read as five."""
    proc, _ = _streak(tmp_path, _runs("success", "success", "failure", "success", "success", "success"))
    assert proc.returncode == 3, proc.stderr
    assert _out(proc) == {"streak": "2", "need": "5", "promote": "false"}, proc.stdout


@pytest.mark.parametrize("neutral", ["cancelled", "skipped"])
def test_a_cancelled_or_skipped_run_neither_counts_nor_breaks(tmp_path, neutral):
    proc, _ = _streak(tmp_path, _runs("success", neutral, "success", "success", neutral, "success", "success"))
    assert proc.returncode == 0, proc.stderr
    assert _out(proc)["streak"] == "5"


@pytest.mark.parametrize("bad", ["timed_out", "startup_failure", "action_required", ""])
def test_anything_else_that_is_not_success_breaks_it(tmp_path, bad):
    proc, _ = _streak(tmp_path, _runs("success", bad, *["success"] * 5))
    assert proc.returncode == 3
    assert _out(proc)["streak"] == "1"


def test_no_scheduled_run_yet_is_a_streak_of_zero(tmp_path):
    proc, _ = _streak(tmp_path, "[]")
    assert proc.returncode == 3
    assert _out(proc) == {"streak": "0", "need": "5", "promote": "false"}


def test_the_threshold_can_be_raised(tmp_path):
    proc, _ = _streak(tmp_path, _runs(*["success"] * 6), "--need", "7")
    assert proc.returncode == 3
    assert _out(proc) == {"streak": "6", "need": "7", "promote": "false"}


def test_it_asks_for_scheduled_completed_runs_of_accept_yml_on_main(tmp_path):
    _, calls = _streak(tmp_path, _runs("success"), "--repo", "o/r")
    (call,) = calls
    assert call[:2] == ["run", "list"]
    pairs = {call[i]: call[i + 1] for i in range(2, len(call) - 1) if call[i].startswith("--")}
    assert pairs["--workflow"] == "accept.yml"
    assert pairs["--branch"] == "main"
    assert pairs["--event"] == "schedule"
    assert pairs["--status"] == "completed"
    assert pairs["--repo"] == "o/r"
    assert "conclusion" in pairs["--json"]


def test_an_unreadable_listing_fails_loudly(tmp_path):
    proc, _ = _streak(tmp_path, FAKE_GH_FAIL="1")
    assert proc.returncode not in (0, 3), "an error must not read as an answer"
    assert "promote" not in proc.stdout


@pytest.mark.parametrize("args", [("--need", "0"), ("--need", "x"), ("--limit", "-1"), ("--bogus",)])
def test_bad_arguments_are_refused(tmp_path, args):
    proc, calls = _streak(tmp_path, _runs("success"), *args)
    assert proc.returncode not in (0, 3)
    assert calls == []


def test_the_script_is_a_script_this_repository_accepts():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    first = next(l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    assert first == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert "lib/common.sh" in text


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed here")
def test_the_script_is_shellcheck_clean():
    result = subprocess.run(["shellcheck", "-x", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_ci_says_why_it_moved_and_how_it_comes_back():
    text = (ROOT / "docs" / "ci.md").read_text()
    assert "accept-live-browser-streak.sh" in text
    assert "18 of 34" in text, "the doc must keep the numbers the move was decided on"

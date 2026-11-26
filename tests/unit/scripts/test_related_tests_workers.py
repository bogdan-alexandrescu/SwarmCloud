"""scripts/related-tests.sh passes pytest the container's worker budget (issue 422).

The script printed and ran `uv run pytest -q -n auto <files>`. `-n auto` reads
PYTEST_XDIST_AUTO_NUM_WORKERS, which the platform sets from the container's
memory, but an agent copies the PRINTED command and edits `-n` freely, so the
count it was given never showed. Six workers of tests/unit/worker OOM-killed
an 8 GiB container twice. Now the listing and the run both carry an explicit
`-n <budget>` when the variable holds a positive integer, and `-n auto`
otherwise (CI and a laptop, which set no budget, keep today's behaviour).

The run is held by a stubbed `uv` that only logs its argv, as in
test_related_tests.py.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "related-tests.sh"
PYTEST_CMD = "uv run pytest "
BUDGET = "PYTEST_XDIST_AUTO_NUM_WORKERS"
# A change whose related tests are pytest files only: no vitest or node run.
CHANGED = "scripts/related-tests.sh\n"


def _fake_uv(bin_dir: Path, log: Path) -> None:
    fake = bin_dir / "uv"
    fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "uv $*" >>"{log}"\n')
    fake.chmod(0o755)


def _run_with_budget(tmp_path: Path, budget: str | None) -> tuple[list[str], list[str]]:
    """The pytest commands the script lists, and the uv calls --run made."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _fake_uv(bin_dir, log)
    diff = tmp_path / "diff.txt"
    diff.write_text(CHANGED)
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_CLONE_BASE", BUDGET)}
    env["NO_COLOR"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    if budget is not None:
        env[BUDGET] = budget
    with tempfile.TemporaryDirectory(prefix="related-tests-artifacts.") as scratch:
        env["SWARM_ARTIFACTS_DIR"] = scratch
        result = subprocess.run(
            ["bash", str(SCRIPT), "--run", "--files", str(diff)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
        )
    assert result.returncode == 0, result.stderr
    listed = [line for line in result.stdout.splitlines() if line.startswith(PYTEST_CMD)]
    calls = log.read_text().splitlines()
    assert listed and calls, (result.stdout, calls)
    return listed, calls


def test_the_run_and_the_listing_pass_the_budgets_worker_count(tmp_path):
    listed, calls = _run_with_budget(tmp_path, "3")

    # MUTATION: keep `-n auto` in command_of or in the run -> red.
    for command in listed + calls:
        assert command.startswith("uv run pytest -q -n 3 "), command
        assert "auto" not in command.split(), command
    # The command an agent copies is the command that ran.
    assert listed == calls


@pytest.mark.parametrize("budget", [None, "", "0", "four", "-2", "3x", "03"])
def test_without_a_valid_budget_the_run_uses_n_auto(tmp_path, budget):
    listed, calls = _run_with_budget(tmp_path, budget)

    for command in listed + calls:
        assert command.startswith("uv run pytest -q -n auto "), (budget, command)
    assert listed == calls

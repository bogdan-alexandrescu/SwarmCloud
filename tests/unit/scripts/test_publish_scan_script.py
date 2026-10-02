"""`scripts/publish-scan.sh` runs the worker's publish scan from a checkout (2026-10-02).

The script is a thin wrapper: it sources `scripts/lib/common.sh`, as every
script must, and hands its arguments to `python -m agent_worker.publish_scan`,
which reuses the worker's own scanner, predicate and tiers. These tests hold
its shape, and run it once against a temporary repository.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import string
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "publish-scan.sh"


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def test_the_script_has_the_house_shape():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.MULTILINE)
    assert "-m agent_worker.publish_scan" in text


def _has_a_project_python() -> bool:
    return (REPO / ".venv" / "bin" / "python").exists() or shutil.which("uv") is not None


@pytest.mark.skipif(shutil.which("git") is None or not _has_a_project_python(),
                    reason="needs git and the project's python")
def test_the_script_reports_a_planted_secret_in_the_current_checkout(tmp_path: Path):
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "scan@example.invalid")
    git("config", "user.name", "scan")
    (tmp_path / "app.py").write_text("X = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    value = "".join(random.Random(5).sample(string.ascii_letters + string.digits, 24))
    (tmp_path / "app.py").write_text("X = 1\n" + "pass" + f'word = "{value}"\n')

    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    done = subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, timeout=300, env=env
    )
    assert done.returncode == 1, done.stderr
    assert done.stdout.splitlines() == ["app.py:2 key_value_assignment"]
    assert value not in done.stdout + done.stderr

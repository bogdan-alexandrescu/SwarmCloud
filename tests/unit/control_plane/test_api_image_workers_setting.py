"""swarm-api's uvicorn worker count is a setting, SWARM_API_WORKERS, default 1.

The owner's follow-up of 2026-11-06 (swarm-api on 2 vCPU instances): measure
whether two workers per instance serve more than one
(scripts/measure-api-workers.sh). Turning two on is the owner's decision, so
the image reads the count from the environment and an instance that sets
nothing still runs the one worker it always ran.

These run the image's own ENTRYPOINT script under `sh`, with a stand-in
`uvicorn` first on PATH that writes the arguments it was execed with: what is
tested is what the container would run. No Docker, no credentials, no
emulator.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "images/swarm-api/Dockerfile"


def _entrypoint_script() -> str:
    lines = [ln for ln in DOCKERFILE.read_text().splitlines() if ln.startswith("ENTRYPOINT ")]
    assert len(lines) == 1, f"expected one ENTRYPOINT, found {len(lines)}"
    argv = json.loads(lines[0][len("ENTRYPOINT "):])
    assert argv[:4] == ["/usr/bin/tini", "--", "sh", "-c"], argv
    return argv[4]


def _uvicorn_argv(tmp_path: Path, env: dict[str, str]) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv"
    stand_in = bin_dir / "uvicorn"
    stand_in.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "{record}"\n')
    stand_in.chmod(0o755)
    base = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"}
    subprocess.run(["sh", "-c", _entrypoint_script()], env={**base, **env}, check=True,
                   capture_output=True, timeout=30)
    return record.read_text().splitlines()


def _workers(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--workers"]


def test_an_instance_that_sets_nothing_runs_one_worker(tmp_path):
    argv = _uvicorn_argv(tmp_path, {})
    # MUTATION: drop --workers from the start command, or default it to 2, and this fails.
    assert _workers(argv) == ["1"], argv


def test_an_empty_setting_still_runs_one_worker(tmp_path):
    # Terraform renders an unset optional variable as an empty string.
    argv = _uvicorn_argv(tmp_path, {"SWARM_API_WORKERS": ""})
    # MUTATION: write ${SWARM_API_WORKERS-1} (no colon) and an empty value
    # reaches uvicorn as `--workers ''`, and this fails.
    assert _workers(argv) == ["1"], argv


@pytest.mark.parametrize("count", ["2", "3"])
def test_the_setting_is_the_worker_count(tmp_path, count):
    argv = _uvicorn_argv(tmp_path, {"SWARM_API_WORKERS": count})
    # MUTATION: hard-code --workers 1 and this fails.
    assert _workers(argv) == [count], argv


def test_the_rest_of_the_start_command_is_unchanged(tmp_path):
    argv = _uvicorn_argv(tmp_path, {"PORT": "9090", "SWARM_API_WORKERS": "2"})
    # MUTATION: lose --factory or stop reading $PORT and this fails.
    assert argv[:6] == ["--factory", "swarm_api.main:create_app", "--host", "0.0.0.0",
                        "--port", "9090"], argv

"""`scripts/verify-destroy-guard.sh --record` writes the placeholder project, never the one it ran in.

A recording is committed to the repository as a test fixture, and the
repository holds no deployment values (docs/design/generic-repository.md). So
the project the real plan was taken in becomes the placeholder project
(scripts/guards/placeholders.json) everywhere in the recording -- its
`_recording.project` and every resource value that embeds it -- and the
recording must still judge identically to the plan it came from, now for the
placeholder project.

The run is the real script from a throwaway copy of scripts/, fed the recorded
destroy plan rewritten into a project made up at run time, so the "real"
project is a value no file holds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "verify-destroy-guard.sh"
FIXTURE = REPO / "tests" / "integration" / "fixtures" / "destroy-plan-dev-real.json"
_SPEC = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_P = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_P)

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or not FIXTURE.exists(),
    reason="jq and the recorded plan are required",
)


def test_record_writes_the_placeholder_project_not_the_one_it_ran_in(tmp_path: Path) -> None:
    live = f"rec-{secrets.token_hex(4)}"
    text = FIXTURE.read_text()
    recorded = json.loads(text)["_recording"]["project"]
    plan = tmp_path / "plan.json"
    plan.write_text(text.replace(recorded, live))
    assert live in plan.read_text()

    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={live}\nREGION={_P.REGION}\nENVIRONMENT=dev\nFIRESTORE_DATABASE=swarm\n")
    env_file.chmod(0o600)
    env = dict(os.environ, SWARM_ENV_FILE=str(env_file), NO_COLOR="1")
    out = tmp_path / "recording.json"

    proc = subprocess.run(
        [str(root / "scripts" / "verify-destroy-guard.sh"), "--environment", "dev",
         "--plan", str(plan), "--record", str(out)],
        cwd=root, env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
    )
    transcript = proc.stdout + proc.stderr
    assert out.exists(), transcript[-3000:]
    written = out.read_text()
    assert live not in written, "the recording still names the project it was taken in"
    assert json.loads(written)["_recording"]["project"] == _P.PROJECT
    assert _P.PROJECT in json.dumps(json.loads(written)["resource_changes"])
    assert "the recording judges identically to the plan it came from" in transcript, transcript[-3000:]
    assert proc.returncode == 0, transcript[-3000:]

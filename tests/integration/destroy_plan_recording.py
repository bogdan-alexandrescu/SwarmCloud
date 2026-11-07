"""The recorded destroy plan, rendered for the shared project this checkout protects.

`fixtures/destroy-plan-dev-real.json` names its project by the placeholder
(`projects.shared` in scripts/guards/placeholders.json), so the repository
carries no deployment's project (docs/design/generic-repository.md). The guard,
though, applies the deny-list only where PROJECT_ID is `SHARED_PROJECT_ID`
(common.sh `key_shared_deny_list`): a recording judged under the placeholder
would be judged against an EMPTY list and prove nothing. So the suites that
replay it judge a rendered copy: every placeholder occurrence replaced by
`SHARED_PROJECT_ID`, ASKED of common.sh the way production reads it, never
restated here. The rendered copy is the recording as it was made; only the
project's spelling in the checked-in file differs.

Offline: sources common.sh and rewrites one JSON file; no credentials.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMMON_SH = REPO / "scripts" / "lib" / "common.sh"
RECORDING = REPO / "tests" / "integration" / "fixtures" / "destroy-plan-dev-real.json"

_spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
assert _spec is not None and _spec.loader is not None
_placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_placeholders)

#: How the checked-in recording spells the shared project.
PLACEHOLDER: str = _placeholders.SHARED_PROJECT


def shared_project_id() -> str:
    """`SHARED_PROJECT_ID` from common.sh -- the project the deny-list protects."""
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; printf %s "$SHARED_PROJECT_ID"', "_", str(COMMON_SH)],
        capture_output=True, text=True, timeout=60,
        # The constant is set whatever the project; SWARM_NO_PROJECT=1 lets
        # common.sh be sourced on a runner with no PROJECT_ID.
        env={**os.environ, "SWARM_NO_PROJECT": "1"},
    )
    assert proc.returncode == 0, f"{COMMON_SH} cannot be sourced: {proc.stderr}"
    assert proc.stdout, (
        f"{COMMON_SH} sets no SHARED_PROJECT_ID, so no project would carry the deny-list "
        "and every replay of the recording would be judged against nothing"
    )
    return proc.stdout


def render(project: str) -> Path:
    """The recording with its placeholder project rendered as `project`.

    Written once per process to a directory removed at exit, so every caller
    in a run judges the same bytes.
    """
    text = RECORDING.read_text()
    assert PLACEHOLDER in text, (
        f"{RECORDING} no longer names its project as {PLACEHOLDER!r}; a recording "
        "that names a real project must be redacted to the placeholder before it is committed"
    )
    rendered = text.replace(PLACEHOLDER, project)
    json.loads(rendered)
    out_dir = Path(tempfile.mkdtemp(prefix="destroy-plan-"))
    atexit.register(shutil.rmtree, out_dir, True)
    out = out_dir / RECORDING.name
    out.write_text(rendered)
    return out

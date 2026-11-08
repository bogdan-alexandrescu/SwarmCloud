"""The recorded destroy plan, rendered for the deployment the tests judge it in.

`fixtures/destroy-plan-dev-real.json` names its project by the placeholder
(`projects.shared` in scripts/guards/placeholders.json), so the repository
carries no deployment's project (docs/design/generic-repository.md). The guard,
though, judges the plan for PROJECT_ID with that project's never-touch list
(config/<env>/deny-list.json, read by `deny_list_load` in scripts/lib/common.sh),
and the placeholder deployment (tests/unit/scripts/placeholder_deployment.py)
serves scripts/lib/deny-list.example.json for its own PROJECT. So the suites
that replay the recording judge a rendered copy: every placeholder occurrence
replaced by that project, so the plan judged is the recording as it was made
for the deployment whose list is served; only the project's spelling in the
checked-in file differs.

Offline: rewrites one JSON file; no credentials.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RECORDING = REPO / "tests" / "integration" / "fixtures" / "destroy-plan-dev-real.json"

_spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
assert _spec is not None and _spec.loader is not None
_placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_placeholders)

#: How the checked-in recording spells the shared project.
PLACEHOLDER: str = _placeholders.SHARED_PROJECT


def deny_list_project() -> str:
    """The project the placeholder deployment serves the never-touch list for."""
    return _placeholders.deployment().PROJECT


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

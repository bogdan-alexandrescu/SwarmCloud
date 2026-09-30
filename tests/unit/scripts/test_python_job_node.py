"""The unit-test job installs the Node that drives the WHATWG URL table.

tests/unit/control_plane/test_url_refusal_whatwg.py runs Node's own `URL`
class as a subprocess, to measure which host a browser opens for each URL
`url_refusal` judges (contract request 32). It FAILS under CI when `node` is
not on PATH, rather than skipping, because a WHATWG table that silently stopped
running is the mirror test it replaced. Until the review of #345 the `python`
job never installed Node: it passed only because the GitHub-hosted runner image
happens to ship one, which is a fact about someone else's image, not about this
workflow.

So the job sets Node up itself, from the line the platform already pins in
ONE place -- `ARG NODE_IMAGE` in images/swarm-ui/Dockerfile, which the `ui`
job reads the same way (tests/unit/scripts/test_ui_node_line.py) -- and not
from a third literal in application.yml.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"


def _python_steps() -> list[dict]:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]["python"]["steps"]


def _index(steps: list[dict], predicate) -> list[int]:
    return [i for i, step in enumerate(steps) if predicate(step)]


def test_the_python_job_sets_up_node_before_the_unit_tests():
    steps = _python_steps()
    setup = _index(steps, lambda s: str(s.get("uses", "")).startswith("actions/setup-node@"))
    assert len(setup) == 1, f"expected one setup-node step in the python job, found {len(setup)}"
    unit = _index(steps, lambda s: "pytest tests/unit" in str(s.get("run", "")))
    assert unit, "the python job no longer runs the unit suite; this test reads nothing"
    assert setup[0] < min(unit), "setup-node runs after the unit tests it is meant to serve"


def test_the_python_job_reads_its_node_from_the_ui_image_not_a_literal():
    steps = _python_steps()
    (i,) = _index(steps, lambda s: str(s.get("uses", "")).startswith("actions/setup-node@"))
    version = str((steps[i].get("with") or {}).get("node-version", ""))
    assert version.startswith("${{ steps.") and version.endswith("}}"), (
        f"the python job pins node-version {version!r} itself: a third statement of "
        "the platform's Node line, beside images/swarm-ui/Dockerfile and the ui job"
    )
    step_id = version.split("steps.", 1)[1].split(".", 1)[0]
    (reader,) = [s for s in steps[:i] if s.get("id") == step_id]
    assert "images/swarm-ui/Dockerfile" in str(reader.get("run", "")), reader
    assert "ARG NODE_IMAGE" in str(reader.get("run", "")), reader

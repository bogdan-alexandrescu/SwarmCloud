"""The release's image-size record writes to the job summary and names no token
(archived issue 625).

The shared promote action (.github/actions/release-promote, run by release.yml
and hotfix.yml alike) runs `image-sizes.sh --manifest` on the record the
promotion just wrote, so a jump in an image's size is on the release's own page.
What is held here is the one property that is a CI security decision the owner
kept on 2026-11-25: the step writes to $GITHUB_STEP_SUMMARY and names no access
token, so nothing it prints can hold a credential. The step's ordering, its
`continue-on-error` and its timeout were deleted by issue 71.

WHAT THIS CANNOT PROVE: that the deployer can read the registry; only a release
run shows the summary.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
ACTION = REPO / ".github" / "actions" / "release-promote" / "action.yml"


def _steps() -> list[dict]:
    return yaml.safe_load(ACTION.read_text())["runs"]["steps"]


def _size_step() -> dict:
    found = [s for s in _steps() if "image-sizes.sh" in s.get("run", "")]
    assert len(found) == 1, "release-promote has no single step running scripts/image-sizes.sh"
    return found[0]


def test_it_writes_the_job_summary_and_names_no_token():
    step = _size_step()
    assert "GITHUB_STEP_SUMMARY" in step["run"]
    text = step["run"] + str(step.get("env", {}))
    for needle in ("print-access-token", "ACCESS_TOKEN", "Bearer"):
        assert needle not in text

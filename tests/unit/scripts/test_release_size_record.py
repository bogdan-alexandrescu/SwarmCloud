"""Every promote records each runner image's compressed size and its delta from
the release before it, in the job summary (#625).

WHY. Issue #625 asked that the worker image's size be measured for every
release. `scripts/image-sizes.sh` measured it only when an operator ran it, so
the 2026-10-05 bisect had to reconstruct three weeks of sizes after the fact.
The shared promote action (.github/actions/release-promote, run by release.yml
and hotfix.yml alike) now runs `image-sizes.sh --manifest` on the record the
promotion just wrote, so a jump is on the release's own page.

The properties held here, by parsing the action:

  * the size step runs after the channel is recorded -- it reads the
    deployed-images record the promotion wrote, which exists only then;
  * it runs only when the promotion succeeded, on dev and prod alike;
  * it is `continue-on-error: true` and bounded in time: a size read must
    never fail or hold a release, and the dev release lock is held around it;
  * it writes to $GITHUB_STEP_SUMMARY, and names no access token.

WHAT THIS CANNOT PROVE: that the deployer can read the registry. It holds
roles/artifactregistry.admin (terraform/bootstrap/variables.tf) and
push-images.sh makes the same `gcloud artifacts docker images list` call in the
step before; only a release run shows the summary.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
ACTION = REPO / ".github" / "actions" / "release-promote" / "action.yml"


def _steps() -> list[dict]:
    return yaml.safe_load(ACTION.read_text())["runs"]["steps"]


def _index(name_part: str) -> int:
    found = [i for i, step in enumerate(_steps()) if name_part in step.get("name", "")]
    assert len(found) == 1, (name_part, [s.get("name") for s in _steps()])
    return found[0]


def _size_step() -> dict:
    found = [s for s in _steps() if "image-sizes.sh" in s.get("run", "")]
    assert len(found) == 1, "release-promote has no single step running scripts/image-sizes.sh"
    return found[0]


def test_the_size_step_runs_after_the_channel_is_recorded():
    size = _steps().index(_size_step())
    assert size > _index("record what the channel now holds")
    assert size > _index("scan and promote the recorded digests")
    assert size < _index("release the release lock")


def test_it_reads_the_record_this_promotion_wrote_and_only_after_it_succeeded():
    step = _size_step()
    assert "--manifest" in step["run"]
    assert "build/deployed-images-${ENVIRONMENT}.json" in step["run"]
    # Prod too: the record step is dev-only, this one is not.
    condition = step.get("if", "")
    assert "steps.promote.outcome == 'success'" in condition
    assert "prod" not in condition


def test_it_can_never_fail_or_hold_the_release():
    step = _size_step()
    assert step.get("continue-on-error") is True
    # A composite step has no timeout-minutes, and the dev lock is held here.
    assert "timeout " in step["run"]


def test_it_writes_the_job_summary_and_names_no_token():
    step = _size_step()
    assert "GITHUB_STEP_SUMMARY" in step["run"]
    text = step["run"] + str(step.get("env", {}))
    for needle in ("print-access-token", "ACCESS_TOKEN", "Bearer"):
        assert needle not in text

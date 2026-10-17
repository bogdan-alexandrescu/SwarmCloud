"""CI falls back to GitHub-hosted runners on its own, and comes back.

MEASURED 2026-10-17 (part of #1483). runner-dispatcher (project
saga-ci-runners) registers SwarmCloud's runners as the owner's GitHub user.
When that user's quota ran out (generate-jitconfig 403 from 02:00 UTC) its
sweep read `per_target: {swarmcloud: 0}` and every pull request's checks
stayed queued for hours, until SELF_HOSTED_CI was set to `off` by hand.
.github/workflows/runner-fallback.yml does that by itself (docs/ci.md, "CI
falls back to GitHub-hosted runners on its own").

This file holds what the workflow promises:

  * its judge runs on GitHub-hosted ubuntu-24.04, never on the pool it judges;
  * it runs every 10 minutes and on dispatch;
  * the GITHUB_TOKEN gets `actions: write` and nothing else, and the workflow
    level grants nothing;
  * it flips SELF_HOSTED_CI, the variable every guarded runs-on reads;
  * the 10/15/30-minute thresholds are named constants in its env;
  * and its decision, executed here from the workflow's own script with fake
    GitHub answers: a stuck pool flips off, hosted saturation does not, a
    serving pool does not, the hysteresis holds a second flip, recovery needs
    15 minutes of probes, and each run is re-run once.

The workflow is pushed by the operator from a patch (an agent cannot push
.github/workflows/), so until it lands every test here fails on the missing
file, by design.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "runner-fallback.yml"
CI_DOC = REPO / "docs" / "ci.md"

NOW = datetime(2026, 10, 17, 5, 30, tzinfo=timezone.utc)
POOL = ["self-hosted", "linux", "x64", "bogdan-ci"]


def _load() -> dict:
    assert WORKFLOW.is_file(), (
        f"{WORKFLOW.relative_to(REPO)} is missing: the operator applies "
        "workflow-lines.patch from the CI-FALLBACK pull request"
    )
    data = yaml.safe_load(WORKFLOW.read_text())
    if True in data:  # YAML 1.1 reads a bare `on:` key as True
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load()


@pytest.fixture(scope="module")
def judge_script(workflow) -> dict:
    """The judge's own Python, executed as a module (main() does not run)."""
    steps = workflow["jobs"]["judge"]["steps"]
    [step] = [s for s in steps if s.get("shell") == "python"]
    namespace: dict = {"__name__": "runner_fallback"}
    exec(compile(step["run"], str(WORKFLOW), "exec"), namespace)
    return namespace


# --- What the workflow file pins ---------------------------------------------


def test_the_judge_runs_on_github_hosted_never_on_the_pool(workflow):
    judge = workflow["jobs"]["judge"]
    assert judge["runs-on"] == "ubuntu-24.04"
    assert "bogdan-ci" not in str(judge["runs-on"])
    assert "SELF_HOSTED_CI" not in str(judge["runs-on"]), "the judge must not follow the hatch it sets"


def test_only_the_probe_asks_for_the_pool_and_only_while_the_hatch_is_off(workflow):
    on_pool = sorted(n for n, j in workflow["jobs"].items() if "bogdan-ci" in str(j.get("runs-on")))
    assert on_pool == ["probe"]
    probe = workflow["jobs"]["probe"]
    assert probe["if"] == "vars.SELF_HOSTED_CI == 'off'"
    assert probe["permissions"] == {}
    assert "secrets." not in yaml.safe_dump(probe)
    assert not any("checkout" in str(s.get("uses", "")) for s in probe["steps"])


def test_it_runs_every_ten_minutes_and_on_dispatch_and_nothing_a_fork_can_start(workflow):
    assert workflow["on"]["schedule"] == [{"cron": "*/10 * * * *"}]
    assert "workflow_dispatch" in workflow["on"]
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}


def test_the_github_token_gets_actions_write_and_nothing_else(workflow):
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["judge"]["permissions"] == {"actions": "write"}


def test_the_variable_is_written_with_the_app_token_scoped_to_variables(workflow):
    """GITHUB_TOKEN has no `variables` scope; the App token asks for only that."""
    [mint] = [s for s in workflow["jobs"]["judge"]["steps"] if "create-github-app-token" in str(s.get("uses"))]
    perms = {k: v for k, v in mint["with"].items() if k.startswith("permission-")}
    assert perms == {"permission-variables": "write"}


def test_it_flips_the_documented_variable_and_label(workflow):
    assert workflow["env"]["VARIABLE"] == "SELF_HOSTED_CI"
    assert workflow["env"]["POOL_LABEL"] == "bogdan-ci"
    assert "vars.SELF_HOSTED_CI == 'off'" in CI_DOC.read_text()


def test_the_thresholds_are_named_constants(workflow):
    env = workflow["env"]
    assert (env["QUEUED_MINUTES"], env["RECOVER_MINUTES"], env["HYSTERESIS_MINUTES"]) == ("10", "15", "30")


def test_the_script_reads_the_thresholds_from_env_not_literals(workflow):
    run = next(s["run"] for s in workflow["jobs"]["judge"]["steps"] if s.get("shell") == "python")
    for name in ("QUEUED_MINUTES", "RECOVER_MINUTES", "HYSTERESIS_MINUTES"):
        assert f'cfg["{name}"]' in run
    assert "decide(state, now, os.environ)" in run


def test_the_docs_describe_the_fallback():
    text = CI_DOC.read_text()
    assert "### CI falls back to GitHub-hosted runners on its own" in text
    for needle in ("runner-fallback.yml", "10 minutes", "15 minutes", "30 minutes", "permission-variables"):
        assert needle in text, needle


# --- The decision, run from the workflow's own script ------------------------


CFG = {"POOL_LABEL": "bogdan-ci", "QUEUED_MINUTES": "10", "RECOVER_MINUTES": "15", "HYSTERESIS_MINUTES": "30"}


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(run_id, *, queued_for=0.0, labels=POOL, status="queued", runner="", started=None, attempt=1, actor="someone"):
    return {
        "run_id": run_id,
        "run_attempt": attempt,
        "triggering_actor": actor,
        "labels": labels,
        "status": status,
        "runner_name": runner,
        "created_at": _iso(queued_for),
        "started_at": _iso(started) if started is not None else None,
    }


def _probe(run_id, created_ago, *, served=True):
    if served:
        return _job(run_id, queued_for=created_ago, status="completed", runner="vm-1", started=created_ago - 1)
    return _job(run_id, queued_for=created_ago)


def _state(value="", updated_ago=None, jobs=(), probes=()):
    return {
        "value": value,
        "updated_at": NOW - timedelta(minutes=updated_ago) if updated_ago is not None else None,
        "jobs": list(jobs),
        "probes": list(probes),
    }


def test_a_pool_job_stuck_past_ten_minutes_with_no_runner_flips_off_and_reruns(judge_script):
    verdict = judge_script["decide"](_state(jobs=[_job(7, queued_for=11), _job(8, queued_for=2)]), NOW, CFG)
    assert verdict["flip"] == "off"
    assert verdict["rerun"] == [7, 8], "every run still queued on the pool is re-run once the hatch is off"


def test_ten_minutes_is_not_yet_stuck(judge_script):
    verdict = judge_script["decide"](_state(jobs=[_job(7, queued_for=9)]), NOW, CFG)
    assert verdict["flip"] is None and verdict["rerun"] == []


def test_hosted_saturation_never_flips_onto_hosted(judge_script):
    jobs = [_job(7, queued_for=40, labels=["ubuntu-24.04"])]
    assert judge_script["decide"](_state(jobs=jobs), NOW, CFG)["flip"] is None


def test_a_serving_pool_with_a_backlog_does_not_flip(judge_script):
    jobs = [_job(7, queued_for=20), _job(9, status="in_progress", runner="vm-3", started=30)]
    assert judge_script["decide"](_state(jobs=jobs), NOW, CFG)["flip"] is None
    jobs = [_job(7, queued_for=20), _job(9, status="completed", runner="vm-3", started=4)]
    assert judge_script["decide"](_state(jobs=jobs), NOW, CFG)["flip"] is None


def test_the_hysteresis_holds_a_second_flip_inside_thirty_minutes(judge_script):
    stuck = [_job(7, queued_for=20)]
    held = judge_script["decide"](_state(value="on", updated_ago=29, jobs=stuck), NOW, CFG)
    assert held["flip"] is None and held["held"] is True and held["rerun"] == []
    free = judge_script["decide"](_state(value="on", updated_ago=31, jobs=stuck), NOW, CFG)
    assert free["flip"] == "off"


def test_recovery_needs_fifteen_minutes_of_served_probes(judge_script):
    decide = judge_script["decide"]
    probes = [_probe(1, 21), _probe(2, 11), _probe(3, 1, served=False)]
    assert decide(_state("off", updated_ago=60, probes=probes), NOW, CFG)["flip"] == "on"
    # Served for only 9 minutes: not yet.
    probes = [_probe(2, 10), _probe(3, 1)]
    assert decide(_state("off", updated_ago=60, probes=probes), NOW, CFG)["flip"] is None
    # One served probe is not a recovery, however old.
    assert decide(_state("off", updated_ago=60, probes=[_probe(1, 40)]), NOW, CFG)["flip"] is None


def test_a_probe_nobody_picked_up_breaks_the_streak_and_is_cancelled(judge_script):
    probes = [_probe(1, 41), _probe(2, 31), _probe(4, 21, served=False), _probe(5, 11), _probe(6, 1)]
    verdict = judge_script["decide"](_state("off", updated_ago=60, probes=probes), NOW, CFG)
    assert verdict["flip"] is None
    assert verdict["cancel_probes"] == [4]


def test_probes_from_before_the_flip_are_not_evidence(judge_script):
    probes = [_probe(1, 50), _probe(2, 40), _probe(3, 5)]
    assert judge_script["decide"](_state("off", updated_ago=35, probes=probes), NOW, CFG)["flip"] is None


def test_recovery_inside_the_hysteresis_is_held(judge_script):
    probes = [_probe(1, 21), _probe(2, 11)]
    verdict = judge_script["decide"](_state("off", updated_ago=25, probes=probes), NOW, CFG)
    assert verdict["flip"] is None and verdict["held"] is True


def test_while_off_each_run_is_rerun_once_and_never_by_its_own_rerun(judge_script):
    jobs = [
        _job(7, queued_for=3),
        _job(7, queued_for=3),
        _job(8, queued_for=3, attempt=2, actor="github-actions[bot]"),
        _job(9, queued_for=3, attempt=2, actor="a-person"),
    ]
    verdict = judge_script["decide"](_state("off", updated_ago=5, jobs=jobs), NOW, CFG)
    assert verdict["rerun"] == [7, 9]


def test_going_back_on_reruns_nothing(judge_script):
    probes = [_probe(1, 21), _probe(2, 11)]
    verdict = judge_script["decide"](_state("off", updated_ago=60, jobs=[_job(7)], probes=probes), NOW, CFG)
    assert verdict["flip"] == "on" and verdict["rerun"] == []

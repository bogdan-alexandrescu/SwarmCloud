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
    [step] = [s for s in steps if s.get("shell") == "python3 {0}"]
    namespace: dict = {"__name__": "runner_fallback"}
    exec(compile(step["run"], str(WORKFLOW), "exec"), namespace)
    return namespace


# --- What the workflow file pins ---------------------------------------------


def test_the_judge_runs_on_github_hosted_never_on_the_pool(workflow):
    judge = workflow["jobs"]["judge"]
    assert judge["runs-on"] == "ubuntu-24.04"
    assert "SWARM_CI_RUNNER_LABEL" not in str(judge["runs-on"])
    assert "SELF_HOSTED_CI" not in str(judge["runs-on"]), "the judge must not follow the hatch it sets"


def test_only_the_probe_asks_for_the_pool_and_only_while_the_hatch_is_off(workflow):
    on_pool = sorted(n for n, j in workflow["jobs"].items() if "SWARM_CI_RUNNER_LABEL" in str(j.get("runs-on")))
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
    assert perms == {"permission-actions-variables": "write"}


def test_the_mint_never_asks_for_the_variables_key_github_does_not_have(workflow):
    """#2220, 2026-10-27: the action turns permission-<name> into the API key <name>
    (dashes as underscores). `permission-variables` asked for `variables`, which GitHub
    refuses as "not granted" whatever the installation holds; the key is `actions_variables`."""
    for step in workflow["jobs"]["judge"]["steps"]:
        assert "permission-variables" not in (step.get("with") or {}), step.get("name")


def test_it_flips_the_documented_variable_and_label(workflow):
    assert workflow["env"]["VARIABLE"] == "SELF_HOSTED_CI"
    assert workflow["env"]["POOL_LABEL"] == "${{ vars.SWARM_CI_RUNNER_LABEL || 'swarm-ci' }}"
    assert "vars.SELF_HOSTED_CI == 'off'" in CI_DOC.read_text()


def test_the_thresholds_are_named_constants(workflow):
    env = workflow["env"]
    assert (env["QUEUED_MINUTES"], env["RECOVER_MINUTES"], env["HYSTERESIS_MINUTES"]) == ("10", "15", "30")


def test_the_script_reads_the_thresholds_from_env_not_literals(workflow):
    run = next(s["run"] for s in workflow["jobs"]["judge"]["steps"] if s.get("shell") == "python3 {0}")
    for name in ("QUEUED_MINUTES", "RECOVER_MINUTES", "HYSTERESIS_MINUTES"):
        assert f'cfg["{name}"]' in run
    assert "decide(state, now, os.environ)" in run


def test_the_docs_describe_the_fallback():
    text = CI_DOC.read_text()
    assert "### CI falls back to GitHub-hosted runners on its own" in text
    for needle in ("runner-fallback.yml", "10 minutes", "15 minutes", "30 minutes", "permission-actions-variables"):
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


# --- Thresholds, pinned at their edges ----------------------------------------


def _at(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_judge_script_runs_under_python3(workflow):
    [step] = [s for s in workflow["jobs"]["judge"]["steps"] if "run" in s and "def decide" in s["run"]]
    assert step["shell"] == "python3 {0}"


def test_queued_exactly_ten_minutes_is_not_stuck_and_one_second_more_is(judge_script):
    decide = judge_script["decide"]
    edge = _job(7, queued_for=10)
    assert edge["created_at"] == _at(600)
    assert decide(_state(jobs=[edge]), NOW, CFG)["flip"] is None  # fails if `>` becomes `>=`
    over = _job(7)
    over["created_at"] = _at(601)
    assert decide(_state(jobs=[over]), NOW, CFG)["flip"] == "off"


def test_a_variable_updated_exactly_thirty_minutes_ago_is_settled(judge_script):
    stuck = [_job(7, queued_for=20)]
    verdict = judge_script["decide"](_state(value="on", updated_ago=30, jobs=stuck), NOW, CFG)
    assert verdict["flip"] == "off" and verdict["held"] is False  # fails if `>=` becomes `>`
    verdict = judge_script["decide"](_state(value="on", updated_ago=29.98, jobs=stuck), NOW, CFG)
    assert verdict["flip"] is None and verdict["held"] is True


def _served(run_id, created_ago, started_seconds_ago):
    probe = _probe(run_id, created_ago)
    probe["started_at"] = _at(started_seconds_ago)
    return probe


def test_probes_served_for_exactly_fifteen_minutes_flip_back_on(judge_script):
    decide = judge_script["decide"]
    probes = [_served(1, 16, 900), _served(2, 6, 300)]
    assert decide(_state("off", updated_ago=60, probes=probes), NOW, CFG)["flip"] == "on"  # fails on `>=` -> `>`
    probes = [_served(1, 16, 899), _served(2, 6, 300)]
    assert decide(_state("off", updated_ago=60, probes=probes), NOW, CFG)["flip"] is None


def test_a_pool_job_started_exactly_ten_minutes_ago_still_counts_as_serving(judge_script):
    decide = judge_script["decide"]
    stuck = _job(7, queued_for=20)
    edge = _job(9, status="completed", runner="vm-3", started=10)
    assert decide(_state(jobs=[stuck, edge]), NOW, CFG)["flip"] is None  # fails if `<=` becomes `<`
    over = _job(9, status="completed", runner="vm-3", started=10)
    over["started_at"] = _at(601)
    assert decide(_state(jobs=[stuck, over]), NOW, CFG)["flip"] == "off"


# --- A run with a job in progress is never cancelled --------------------------


def test_a_run_with_a_job_in_progress_is_held_not_rerun(judge_script):
    decide = judge_script["decide"]
    hosted_deploy = _job(7, labels=["ubuntu-24.04"], status="in_progress", runner="gh-1", started=4)
    waiting = _job(7, queued_for=3)
    held = decide(_state("off", updated_ago=5, jobs=[hosted_deploy, waiting]), NOW, CFG)
    assert held["rerun"] == [] and held["held_runs"] == [7]
    finished = _job(7, labels=["ubuntu-24.04"], status="completed", runner="gh-1", started=4)
    free = decide(_state("off", updated_ago=5, jobs=[finished, waiting]), NOW, CFG)
    assert free["rerun"] == [7] and free["held_runs"] == []


# --- One run failing does not end the loop; a time budget ends it -------------


def test_each_runs_failure_is_isolated_and_named(judge_script):
    ApiError = judge_script["ApiError"]

    def action(run_id):
        if run_id == 8:
            raise ApiError("POST", "/repos/o/r/actions/runs/8/cancel", 403, "fork")
        return "re-ran"

    lines = judge_script["reconcile"]([7, 8, 9], action, lambda: 0, 600, "T")
    assert [l.split(": ")[0] for l in lines] == ["- T run 7", "- T run 8", "- T run 9"]
    assert "403" in lines[1] and "skipped" in lines[1] and lines[2].endswith("re-ran")


def test_no_new_run_is_started_after_the_time_budget(judge_script):
    ticks = iter([0, 400, 800, 1200])
    done = []
    lines = judge_script["reconcile"]([1, 2, 3], lambda r: done.append(r) or "re-ran", lambda: next(ticks), 600, "T")
    assert done == [1]
    assert lines[-1] == "- T time budget spent: 2 run(s) left for the next tick"


def test_rerun_failed_jobs_403_409_422_mean_nothing_to_rerun(judge_script, monkeypatch):
    for answered in (403, 409, 422):
        calls = []

        def fake(method, path, token, body=None, ok=(200,), _answered=answered):
            calls.append(path)
            if path.endswith("/rerun-failed-jobs"):
                assert _answered in ok
                return _answered, None
            return 200, {"status": "completed"}

        monkeypatch.setitem(judge_script, "api", fake)
        outcome = judge_script["cancel_and_rerun"]("o/r", "t", 5)
        assert outcome == f"nothing to re-run (answered {answered})"


# --- Reads are ordered by cost ------------------------------------------------


def _fake_api(calls, variable, queued_runs, in_progress_runs=()):
    def fake(method, path, token, body=None, ok=(200,)):
        calls.append(path)
        if "/actions/variables/" in path:
            return 200, variable
        if "status=queued" in path:
            return 200, {"workflow_runs": list(queued_runs)}
        if "status=in_progress" in path:
            return 200, {"workflow_runs": list(in_progress_runs)}
        if path.endswith("/jobs") or "/jobs?" in path:
            return 200, {"jobs": []}
        return 200, {"workflow_runs": []}

    return fake


def test_the_steady_state_ends_after_the_run_listing(judge_script, monkeypatch):
    calls = []
    variable = {"value": "on", "updated_at": _iso(120)}
    young = {"id": 1, "path": ".github/workflows/x.yml@refs/heads/b", "created_at": _iso(3)}
    monkeypatch.setitem(judge_script, "api", _fake_api(calls, variable, [young]))
    state = judge_script["read_state"]("o/r", "t", "a", NOW, CFG | {"VARIABLE": "SELF_HOSTED_CI"})
    assert len(calls) == 2 and not any("/jobs" in c for c in calls)
    assert state["read_jobs"] is False


def test_an_old_queued_run_makes_the_judge_read_jobs(judge_script, monkeypatch):
    calls = []
    variable = {"value": "on", "updated_at": _iso(120)}
    old = {"id": 1, "path": ".github/workflows/x.yml@refs/heads/b", "created_at": _iso(12)}
    monkeypatch.setitem(judge_script, "api", _fake_api(calls, variable, [old]))
    state = judge_script["read_state"]("o/r", "t", "a", NOW, CFG | {"VARIABLE": "SELF_HOSTED_CI"})
    assert any("/runs/1/jobs" in c for c in calls) and state["read_jobs"] is True


# --- RUNNER_FALLBACK=manual: read and report, never act ------------------------


def test_manual_is_read_from_runner_fallback_and_defaults_to_automatic(judge_script):
    decide = judge_script["decide"]
    jobs = [_job(7, queued_for=20)]
    manual = decide(_state(jobs=jobs), NOW, CFG | {"RUNNER_FALLBACK": "manual"})
    assert manual["manual"] is True and manual["flip"] == "off"  # still computes what it would do
    for value in ("", "auto", "MANUAL"):
        assert decide(_state(jobs=jobs), NOW, CFG | {"RUNNER_FALLBACK": value})["manual"] is False
    assert decide(_state(jobs=jobs), NOW, CFG)["manual"] is False


def test_manual_never_flips_cancels_or_reruns(judge_script, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    env = CFG | {
        "RUNNER_FALLBACK": "manual", "REPOSITORY": "o/r", "GH_TOKEN": "t", "APP_TOKEN": "a",
        "VARIABLE": "SELF_HOSTED_CI", "RUN_ID": "99", "GITHUB_STEP_SUMMARY": str(summary),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    state = _state(value="off", updated_ago=5, jobs=[_job(7, queued_for=3)],
                   probes=[_probe(1, 41), _probe(4, 21, served=False)])
    state.update(read_jobs=True, queued_runs=1)
    writes = []
    monkeypatch.setitem(judge_script, "read_state", lambda *a, **k: state)
    monkeypatch.setitem(judge_script, "api", lambda *a, **k: writes.append(a) or (200, None))
    judge_script["main"](now=NOW)
    assert writes == [], "manual must not make any API call after the reads"
    text = summary.read_text()
    assert "RUNNER_FALLBACK=manual" in text and "would re-run run(s) 7" in text

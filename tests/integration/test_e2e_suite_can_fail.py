"""Does `scripts/e2e-test.sh` notice when the platform is broken?

A test that cannot fail is not a test, and a SUITE that cannot fail is a gate
that passes hardest when it is most needed -- which is the shape this repository
keeps producing. So every check in `scripts/e2e-test.sh` is exercised twice
here: once against a platform where the fact it covers is TRUE, and once against
a platform where that same fact is FALSE and nothing else has changed.

The suite is driven for real. `tests/integration/fake_platform.py` puts a `curl`
on PATH that answers from a scenario, so the script runs with its own argument
handling, its own jq, its own assertions and its own exit code. Nothing is
created, no credentials are used, and a `gcloud` stub on the same PATH fails
loudly rather than letting a run reach a real project.

WHY THIS IS NOT THE LIVE PROOF, stated rather than implied. Breaking the
deployed platform to see whether the suite notices is not acceptable -- the
project is shared with another team and the failure modes being simulated
include a task stuck holding capacity. So the mutations happen at the lowest
seam that can be injected, which is the wire, and this file says so. What the
mutations demonstrate is the suite's JUDGEMENT; what they cannot demonstrate is
that the live platform speaks the shapes below. The last two tests close as much
of that gap as can be closed offline, by checking the fake's shapes against the
production serialisers rather than against a memory of them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from fake_platform import install, scenario

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "scripts" / "e2e-test.sh"

pytestmark = pytest.mark.skipif(
    not SUITE.exists() or shutil.which("jq") is None,
    reason="scripts/e2e-test.sh and jq are both required",
)


@dataclass
class Run:
    returncode: int
    transcript: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def failed_checks(self) -> list[str]:
        """The FAIL lines, which is what the suite is judged on."""
        return [
            line.strip()
            for line in self.transcript.splitlines()
            if line.strip().startswith("FAIL")
        ]

    def skipped_checks(self) -> list[str]:
        return [
            line.strip()
            for line in self.transcript.splitlines()
            if line.strip().startswith("SKIP")
        ]

    def mentions(self, needle: str) -> bool:
        return needle.lower() in self.transcript.lower()


def run_suite(tmp_path: Path, **deviations) -> Run:
    env = install(tmp_path)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario(**deviations)))
    env["FAKE_SWARM_SCENARIO"] = str(scenario_path)

    proc = subprocess.run(
        [str(SUITE), "--timeout", "20"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return Run(proc.returncode, proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# the control: a healthy platform passes
# ---------------------------------------------------------------------------


def test_a_healthy_platform_passes_and_reaches_nothing_real(tmp_path):
    """Without this, every mutation below could be failing for the wrong reason.

    A suite that fails on everything catches every mutation and is worth
    nothing, so this is the assertion that makes the rest of the file
    meaningful. It carries three separate claims, in one run because a run costs
    eight seconds on the gate everybody waits for:

      * the suite passes against a healthy platform, with no FAIL lines;
      * it actually RAN its checks -- a suite that died in `require_platform`
        also produces no FAIL lines, and would make every mutation below pass
        for the same empty reason;
      * it reached nothing real. The `gcloud` stub exits 1 with a message, so
        any code path that fell back to a genuine credential shows up here.
    """
    run = run_suite(tmp_path, executions="live")
    assert run.passed, run.transcript
    assert run.failed_checks() == [], run.transcript
    assert "refuses to run gcloud" not in run.transcript, run.transcript
    # Every check the suite defines, by its own numbering. If a check is added
    # or removed this fails, which is the point: a mutation test file that does
    # not know how many checks exist cannot claim to cover them.
    headings = [line for line in run.transcript.splitlines() if line.startswith("[")]
    assert len(headings) == 11, (
        "scripts/e2e-test.sh no longer has 11 checks. Every one of them needs a "
        f"mutation in this file:\n" + "\n".join(headings)
    )


# ---------------------------------------------------------------------------
# check 1: the handoff, asserted on content
# ---------------------------------------------------------------------------


def test_a_downstream_step_that_staged_nothing_is_caught(tmp_path):
    """The outage, exactly: every state green, nothing handed over.

    Both steps SUCCEEDED and the producer's manifest is intact; only
    `staged_inputs` is absent. A suite that asserted on states would pass here,
    which is why the check is on the bytes.
    """
    run = run_suite(tmp_path, staged="missing", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("recorded no staged input"), run.transcript


def test_a_handoff_of_the_wrong_size_is_caught(tmp_path):
    """Right filename, right source, wrong bytes.

    This is the mutation a `staged_inputs != []` assertion would miss: the
    handoff happened, the file that arrived is not the file that was produced.
    """
    run = run_suite(tmp_path, staged="wrong_bytes", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("bytes that arrived"), run.transcript


def test_a_handoff_fetched_from_another_task_is_caught(tmp_path):
    """The staged file resolved against somebody else's prefix.

    `inputs.artifact_reference` refuses this, and it must stay refused: an
    artifact staged from outside the declared upstream is a workflow reading a
    file it was never promised, and across tenants it is a boundary crossing.
    """
    run = run_suite(tmp_path, staged="foreign_source", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("came from the producer") or run.mentions("unexpected location"), (
        run.transcript
    )


# ---------------------------------------------------------------------------
# check 2: provenance -- the workload's artifact, apart from the runner's own
# ---------------------------------------------------------------------------


def test_a_manifest_holding_only_the_runners_own_files_is_caught(tmp_path):
    """`artifacts != []` was TRUE throughout the outage.

    The producer's manifest here contains its stdout and stderr and nothing the
    workload was asked for -- which is exactly what every claude-code attempt
    produced while SWARM_ARTIFACTS_DIR was missing from the agent's
    environment. The check has to name the file, and this proves it does.
    """
    run = run_suite(tmp_path, produce_manifest="runner_only", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("has no entry named"), run.transcript


# ---------------------------------------------------------------------------
# check 3: the negative control, which is what gives check 1 teeth
# ---------------------------------------------------------------------------


def test_a_platform_that_succeeds_on_an_impossible_input_is_caught(tmp_path):
    """The consumer was promised a file nobody wrote and reported SUCCEEDED.

    Every handoff assertion in the suite is satisfied by a platform that stages
    nothing and succeeds anyway; this control is the reason they are not. If
    this mutation ever stops being caught, the handoff checks above have become
    decoration.
    """
    run = run_suite(tmp_path, negative_consume_state="SUCCEEDED", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("reported succeeded"), run.transcript


def test_a_refusal_that_does_not_name_the_file_is_caught(tmp_path):
    """"The attempt could not start" is not a diagnosis.

    The two strings a person needs in order to fix a broken workflow are the
    upstream task and the filename, which is why `inputs.py` puts both in every
    refusal.
    """
    run = run_suite(tmp_path, negative_error_names_file=False, executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("does not name"), run.transcript


# ---------------------------------------------------------------------------
# check 4: spend
# ---------------------------------------------------------------------------


def test_spend_the_api_drops_is_caught(tmp_path):
    """The `attempt_from_dict` defect, restaged over HTTP.

    Firestore holds all five numbers and the API serves null for every one of
    them. The test that missed this for months asserted they are None when
    ABSENT -- true whether or not the decoder reads them -- so the assertion
    here is the other half: equal to what the store holds, wherever it holds
    anything.
    """
    run = run_suite(tmp_path, api_spend="null", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("disagree"), run.transcript


def test_an_api_that_omits_the_spend_keys_entirely_is_caught(tmp_path):
    """A field removed from the response shape, rather than served as null."""
    run = run_suite(tmp_path, api_spend="omit_keys", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("omits input_tokens"), run.transcript


def test_a_deployment_with_no_spend_reports_not_measured_rather_than_passing(tmp_path):
    """The honest third answer.

    The mock profile has provider=None and reports no usage, so a deployment
    that has only ever run mock tasks has nothing to compare. Calling that a
    PASS is the `[[ "" -eq 0 ]]` failure in another costume; calling it a
    FAILURE blames the platform for a state it is entitled to be in. It is
    reported as NOT MEASURED, counted apart, and listed in the summary.
    """
    run = run_suite(tmp_path, spend_recorded=False, executions="live")
    assert run.passed, run.transcript
    assert any("Firestore and the API could not be compared" in line
               for line in run.skipped_checks()), run.transcript
    assert run.mentions("NOT MEASURED"), run.transcript


def test_not_measured_becomes_a_failure_when_the_caller_requires_it(tmp_path):
    """`--require-spend` is how CI makes the check mandatory once it can be."""
    env = install(tmp_path)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario(spend_recorded=False, executions="live")))
    env["FAKE_SWARM_SCENARIO"] = str(scenario_path)
    proc = subprocess.run(
        [str(SUITE), "--timeout", "20", "--require-spend"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    transcript = proc.stdout + proc.stderr
    assert proc.returncode != 0, transcript
    assert "and this run requires it" in transcript, transcript


# ---------------------------------------------------------------------------
# check 5: the browser sign-in
# ---------------------------------------------------------------------------


def test_a_broken_account_authorize_route_is_caught(tmp_path):
    """The BrokerClient._call keyword defect: a 500 on every registration.

    Every test faked the account pool ABOVE that layer, so the one signature
    that mattered was never exercised. Driving the route over HTTP is what
    notices.
    """
    run = run_suite(tmp_path, authorize="error", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("browser sign-in path is unusable"), run.transcript


def test_a_leaked_pkce_verifier_is_caught(tmp_path):
    """A verifier the client holds is an exchange the client can complete alone."""
    run = run_suite(tmp_path, authorize="leak_verifier", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("verifier"), run.transcript


@pytest.mark.parametrize(
    "mode,needle",
    [
        ("http_url", "not https"),
        ("short_state", "characters"),
        ("no_challenge", "no code_challenge"),
    ],
)
def test_each_sign_in_property_is_checked_independently(tmp_path, mode, needle):
    run = run_suite(tmp_path, authorize=mode, executions="live")
    assert not run.passed, run.transcript
    assert run.mentions(needle), run.transcript


# ---------------------------------------------------------------------------
# check 6: state agreement, and the twenty-minute silence
# ---------------------------------------------------------------------------


def test_the_api_and_firestore_disagreeing_about_state_is_caught(tmp_path):
    """Two stores, one truth. A UI reads the API; the platform runs on Firestore."""
    run = run_suite(tmp_path, executions="live",
                    api_state_override={"task_produce_main": "RUNNING"})
    assert not run.passed, run.transcript
    assert run.mentions("firestore vs api state"), run.transcript


def test_a_task_holding_capacity_against_a_dead_execution_is_caught(tmp_path):
    """The failure nobody was told about for twenty minutes.

    The control plane says RUNNING, the attempt names an execution, and that
    execution has completed. The slot stays reserved and no screen says so --
    which is the whole reason this check exists, because the reconciler REPAIRS
    this and nothing REPORTED it.
    """
    run = run_suite(tmp_path, executions="dead")
    assert not run.passed, run.transcript
    assert run.mentions("with no live execution behind them"), run.transcript


def test_no_in_flight_work_reports_not_measured(tmp_path):
    """Nothing was running, so nothing could be compared. Not a pass."""
    run = run_suite(tmp_path, executions="idle")
    assert run.passed, run.transcript
    assert any("could not be compared" in line for line in run.skipped_checks()), (
        run.transcript
    )


# ---------------------------------------------------------------------------
# check 7: the workflow state nothing advances
# ---------------------------------------------------------------------------


def test_a_workflow_stuck_in_queued_after_every_step_succeeded_is_caught(tmp_path):
    """This is the platform's CURRENT behaviour, not a hypothetical.

    `Store.create_workflow` writes `state` once and nothing in the repository
    ever advances it; `Store.cancel_workflow` sets `cancel_requested` and
    nothing else. So a workflow whose every step has SUCCEEDED serves QUEUED for
    ever, through the field a UI renders as progress. The suite asserts against
    it rather than describing it, because a check that agrees with a defect is
    how the defect survives.
    """
    run = run_suite(tmp_path, workflow_state="QUEUED", executions="live")
    assert not run.passed, run.transcript
    assert run.mentions("nothing in the platform advances a workflow"), run.transcript


def test_nothing_in_the_repository_advances_a_workflow_state(tmp_path):
    """The claim above, checked against the code rather than against the fake.

    If a component ever starts advancing workflow state, this test is what says
    so -- and the suite's check turns from a standing failure into a passing
    one. Written as a source-level search because there is no component to run:
    the absence is the finding.
    """
    store = (REPO / "apps" / "swarm-api" / "swarm_api" / "store.py").read_text()
    assert 'def create_workflow' in store
    writers = [
        line.strip()
        for line in store.splitlines()
        if "WORKFLOWS" in line and (".update(" in line or ".set(" in line)
    ]
    assert not any('"state"' in line for line in writers), (
        "something now writes a workflow's state; scripts/e2e-test.sh's "
        "workflow check should start passing, and this test should be updated "
        "to assert the new behaviour rather than its absence:\n  "
        + "\n  ".join(writers)
    )


# ---------------------------------------------------------------------------
# keeping the fake honest
# ---------------------------------------------------------------------------


def test_the_fakes_attempt_shape_matches_the_production_serialiser():
    """A scenario is only as honest as the shapes it serves.

    The suite's spend check reads `attempts[].input_tokens` and friends off the
    HTTP response. If the real serialiser spelled them differently, the suite
    would pass here and fail in production -- a test exercising a shape
    production never produces, which is the exact trap `_usage_summary`'s
    docstring records. So the field names are taken from `attempt_to_api`
    itself.
    """
    from datetime import datetime, timezone

    from swarm_api.codec import attempt_from_dict, attempt_to_api

    served = attempt_to_api(attempt_from_dict({
        "attempt_id": "att_1", "task_id": "task_1", "tenant_id": "eng",
        "generation": 1, "created_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
        "input_tokens": 1234, "output_tokens": 567,
        "cache_read_input_tokens": 89, "cache_creation_input_tokens": 10,
        "cost_usd": 0.4213,
    }))
    for field in (
        "attempt_id", "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd",
    ):
        assert field in served, (
            f"{field} is not in the real attempt shape, so the e2e suite is "
            "asserting on a field the API does not serve"
        )


def test_the_fakes_artifact_shape_matches_the_production_read_path():
    """Same, for `GET /v1/tasks/<id>/artifacts`.

    `complete` is the field that stops an empty list reading as an answer, and
    the suite compares it against the literal `true` rather than through jq's
    alternative operator. Both facts are checked against `Store.list_artifacts`
    by driving it over a stub task rather than by reading the file.
    """
    from swarm_api.store import Store

    class _Task:
        result_summary = {
            "artifacts": [{"name": "finding.md", "bytes": 12, "uri": "gs://b/k"}],
            "artifacts_skipped": [],
            "artifact_bytes": 12,
        }

    store = Store.__new__(Store)
    store.get_task = lambda tenant_id, task_id: _Task()  # type: ignore[method-assign]
    served = Store.list_artifacts(store, "eng", "task_1")

    assert set(served) == {"artifacts", "artifacts_skipped", "artifact_bytes", "complete"}
    assert served["complete"] is True
    assert served["artifacts"][0]["name"] == "finding.md"
    assert "signed_url" not in served["artifacts"][0]

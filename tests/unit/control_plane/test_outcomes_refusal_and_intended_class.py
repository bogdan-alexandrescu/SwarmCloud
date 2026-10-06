"""#631: a publish the worker refused, and a failure a caller asked for, are
each counted as what they are, so `runner_error` means a real error.

Measured on dev, 2026-10-05 (history report section 2.7 item 6): over the
whole history `publish_refused` was 0 although 9 tasks failed and 23 attempts
retried on the final-tree credential scan, and `runner_error` (213) also held
191 deliberate acceptance failures -- mock tasks submitted with `fail: true`.

The worker has typed both publish refusals PUBLISH_REFUSED since contract
request 29 (2026-10-02, `lifecycle._fail_for_final_tree_leak` and
`_fail_for_refused_title`). Every task that ended before that carries the
refusal's own text and either no cause or the one its writer used then, so the
ledger reads those two literals -- only on an end whose final attempt exited 0,
which a runner's own error text never does.

MUTATIONS these catch: drop `_PUBLISH_REFUSED_RE` (or its exit-0 guard) from
`classify_failure`; drop the `intended` refinement or let it override a
timeout; read `fail` from a profile that does not declare it; forget the class
in `FAILURE_CLASSES`; leave either version where it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_api import outcomes
from swarm_api.outcomes import classify_failure, tuple_from_docs

ROOT = Path(__file__).resolve().parents[3]
LIFECYCLE = ROOT / "apps/agent-worker/agent_worker/lifecycle.py"

LEAK = "the final tree adds a credential in .env (rule aws_access_key_id, line 1); remove it"
TITLE = "pr-title.txt refused: names a task id; write a fact-style title"


# --------------------------------------------------------------------------
# A publish refusal written before contract request 29
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [LEAK, TITLE, "  " + LEAK])
def test_a_refused_publish_with_no_cause_is_classed_publish_refused(text):
    # Before the field existed the text fallback read "an exit code, no rule"
    # as a runner error: the refusal's attempt records exit 0.
    assert classify_failure("FAILED", text, 0) == "publish_refused"


@pytest.mark.parametrize(
    ("text", "typed"), [(LEAK, "runner_error"), (TITLE, "outputs_missing"), (LEAK, "outputs_missing")]
)
def test_a_refused_publish_typed_with_the_old_cause_is_classed_publish_refused(text, typed):
    assert classify_failure("FAILED", text, 0, end_cause=typed) == "publish_refused"


def test_a_runner_error_that_quotes_the_refusal_is_still_a_runner_error():
    """The runner's own error text is the agent's to write; only an end whose
    final attempt exited 0 -- the worker's refusal, never a runner's failure --
    is read as a refusal."""
    assert classify_failure("FAILED", LEAK, 1) == "runner_error"
    assert classify_failure("FAILED", LEAK, 1, end_cause="runner_error") == "runner_error"
    assert classify_failure("FAILED", LEAK, None) == "other"


def test_another_typed_cause_is_never_reread_as_a_refusal():
    assert classify_failure("FAILED", LEAK, 0, end_cause="timeout") == "timeout"
    assert classify_failure("FAILED", TITLE, 0, end_cause="lost_worker") == "lost_worker"


def test_the_refusal_literals_are_the_workers_own():
    """Pinned to the writer, as test_outcomes_classifier_parity.py pins the
    others: a reworded refusal would drain back into runner_error."""
    source = LIFECYCLE.read_text()
    assert 'f"the final tree adds a credential in {hit.path} (rule ' in source
    assert 'return f"{PR_TITLE_FILE} refused: {why}; write a fact-style title"' in source
    assert 'PR_TITLE_FILE = "pr-title.txt"' in source


def test_a_task_that_failed_on_the_credential_scan_folds_into_publish_refused():
    task = {"id": "t1", "state": "FAILED", "runner_profile": "claude-code",
            "last_error": LEAK, "end_cause": "runner_error", "input": {"prompt": "x"}}
    attempts = [{"generation": 1, "exit_code": 0}, {"generation": 2, "exit_code": 0}]
    assert tuple_from_docs(task, attempts, {})["failure_class"] == "publish_refused"


# --------------------------------------------------------------------------
# A failure the caller asked for
# --------------------------------------------------------------------------

def _mock_task(**overrides):
    task = {
        "id": "t2", "state": "FAILED", "runner_profile": "mock",
        "last_error": "acceptance fail_message run_1", "end_cause": "runner_error",
        "input": {"prompt": "x", "fail": True, "fail_message": "acceptance fail_message run_1"},
    }
    task.update(overrides)
    return task


def test_a_mock_asked_to_fail_is_classed_intended():
    assert tuple_from_docs(_mock_task(), [{"generation": 1, "exit_code": 1}], {})[
        "failure_class"] == "intended"
    # Any code the mock may fail with, and a task from before end_cause.
    assert tuple_from_docs(_mock_task(), [{"generation": 1, "exit_code": 255}], {})[
        "failure_class"] == "intended"
    assert tuple_from_docs(_mock_task(end_cause=None), [{"generation": 1, "exit_code": 2}], {})[
        "failure_class"] == "intended"


def test_a_mock_not_asked_to_fail_is_a_runner_error():
    for given in ({"prompt": "x"}, {"prompt": "x", "fail": False}, {"prompt": "x", "fail": "true"}, None):
        task = _mock_task(input=given)
        assert tuple_from_docs(task, [{"generation": 1, "exit_code": 1}], {})[
            "failure_class"] == "runner_error", given


def test_a_mock_asked_to_fail_that_failed_some_other_way_keeps_that_class():
    """It fails on purpose only after its steps: a timeout or a lost worker
    before that is a real failure, and is counted as one."""
    for cause, cls in (("timeout", "timeout"), ("lost_worker", "lost_worker"),
                       ("cannot_start", "could_not_start"), ("outputs_missing", "outputs_missing")):
        task = _mock_task(end_cause=cause)
        assert tuple_from_docs(task, [{"generation": 1, "exit_code": 1}], {})[
            "failure_class"] == cls, cause


def test_fail_is_read_only_where_the_catalogue_declares_it():
    """A profile that declares no `fail` input does not fail on purpose,
    whatever its stored input holds."""
    assert "fail" not in outcomes.RUNNER_PROFILES["claude-code"].inputs
    task = _mock_task(runner_profile="claude-code")
    assert tuple_from_docs(task, [{"generation": 1, "exit_code": 1}], {})[
        "failure_class"] == "runner_error"
    task = _mock_task(runner_profile="a-profile-this-image-does-not-know")
    assert tuple_from_docs(task, [{"generation": 1, "exit_code": 1}], {})[
        "failure_class"] == "runner_error"


def test_classify_failure_reads_intended_only_over_a_runner_error():
    assert classify_failure("FAILED", "boom", 1, end_cause="runner_error", intended=True) == "intended"
    assert classify_failure("FAILED", "boom", 1, intended=True) == "intended"
    assert classify_failure("FAILED", "boom", 1, end_cause="timeout", intended=True) == "timeout"
    assert classify_failure("FAILED", None, None, intended=True) == "no_reason"
    assert classify_failure("SUCCEEDED", None, 0, intended=True) is None


# --------------------------------------------------------------------------
# The vocabulary and the versions
# --------------------------------------------------------------------------

def test_intended_is_the_last_failure_class_with_its_own_label():
    keys = [k for k, _ in outcomes.FAILURE_CLASSES]
    assert keys[-1] == "intended"
    assert keys[-3:-1] == ["other", "no_reason"]
    assert dict(outcomes.FAILURE_CLASSES)["intended"] == "failed on purpose"
    assert "intended" not in outcomes._FAILURE_OF_CAUSE.values()


def test_both_versions_moved_so_every_stored_day_is_derived_again():
    # A day stored under 6 counted these under runner_error and has no
    # `intended` key; the text rules changed, so the classifier did too.
    assert outcomes.DERIVE_VERSION == 7
    assert outcomes.CLASSIFIER_VERSION == 3
    assert outcomes.VOCAB["classifier_version"] == 3

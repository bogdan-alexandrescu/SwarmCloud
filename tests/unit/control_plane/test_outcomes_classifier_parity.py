"""The outcome classifier reads other components' words. Every one is pinned here.

`swarm_api.outcomes.classify_failure` and `cancel_cause` sort a task's end into a
fixed class by reading `last_error` -- text written by the worker, the
reconciler and the scheduler, none of which the swarm-api image carries. That is
a restatement across a package boundary, the shape docs/mirrored-values.md
exists to track: if a writer rewords its message, the class it fed would quietly
drain into "other" and the "Why tasks failed" card would lie with a straight
face.

So each rule is held to its writer here, in the two ways available:

  * where the writer's text comes from a function, the REAL function is called
    and its output classified (`cannot_start_error`, `missing_error`);
  * where it is an f-string inside a larger method, the literal is asserted
    present in the writer's source, and a string built the same way is
    classified.

The exit codes are held to the worker's `ExitCode` and the reconciler's
constant the same way.

THE TEXT IS NOW THE FALLBACK. Contract request 23 was accepted on 2026-09-25
(#185, decision 9): every terminal writer records a typed `Task.end_cause`, and
the classifier reads it first. These pins still matter, for every task that
ended before the field existed -- which is every task on dev today.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from agent_worker import inputs as inputs_mod
from agent_worker import startup
from agent_worker.errors import ExitCode
from agent_worker.expected_outputs import missing_error
from reconciler.detect import WORKER_EXIT_CANNOT_START, cannot_start_error
from scheduler.loop import FAIL_WORKFLOW

from swarm_api.outcomes import (
    DECLARED_COST_PROFILES,
    EXIT_CANNOT_START,
    EXIT_LABELS,
    cancel_cause,
    classify_failure,
)
from swarm_common.profiles import RUNNER_PROFILES

ROOT = Path(__file__).resolve().parents[3]


def _src(relative: str) -> str:
    path = ROOT / relative
    assert path.is_file(), f"{relative} is not where this pin expects its writer"
    return path.read_text()


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------

def _worker_exits() -> dict[str, int]:
    return {
        name: value
        for name, value in vars(ExitCode).items()
        if name.isupper() and isinstance(value, int)
    }


def test_the_cannot_start_exit_is_the_workers_and_the_reconcilers():
    assert EXIT_CANNOT_START == ExitCode.CONFIG == WORKER_EXIT_CANNOT_START == 78


def test_every_worker_exit_has_a_label_and_every_label_is_a_real_exit():
    exits = _worker_exits()
    assert len(exits) >= 8, f"read too few exit codes to compare: {exits}"
    real = set(exits.values()) | {startup.EXIT_INTERRUPTED}
    assert set(EXIT_LABELS) == real, (
        f"EXIT_LABELS names {sorted(set(EXIT_LABELS) - real)} that no worker exits "
        f"with, and misses {sorted(real - set(EXIT_LABELS))}"
    )
    assert EXIT_LABELS[ExitCode.PARKED] == "parked"
    assert EXIT_LABELS[ExitCode.CONFIG] == "could not start"
    assert EXIT_LABELS[ExitCode.GENERATION_FENCED] == "generation fenced"
    assert EXIT_LABELS[ExitCode.UNAVAILABLE] == "dependency unavailable"
    assert EXIT_LABELS[ExitCode.CANCELLED] == "cancelled"
    assert EXIT_LABELS[startup.EXIT_INTERRUPTED] == "interrupted"


def test_a_timeout_records_the_childs_status_so_76_is_never_trusted():
    """The worker's timeout branch passes `result.exit_code` -- the killed child's
    status -- and nothing raises ChildTimeout, so no attempt carries 76. The
    classifier therefore reads the text, not the number."""
    lifecycle = _src("apps/agent-worker/agent_worker/lifecycle.py")
    branch = lifecycle[lifecycle.index("if result.timed_out:"):]
    branch = branch[: branch.index("return Outcome(exit_code=ExitCode.TIMEOUT")]
    assert "exit_code=result.exit_code" in branch, branch


# --------------------------------------------------------------------------
# Failure classes
# --------------------------------------------------------------------------

def test_the_timeout_text_is_the_workers():
    lifecycle = _src("apps/agent-worker/agent_worker/lifecycle.py")
    assert (
        'f"runner exceeded its {self.cfg.timeout_seconds}s timeout and was killed"' in lifecycle
    )
    text = f"runner exceeded its {7200}s timeout and was killed"
    assert classify_failure("FAILED", text, -9) == "timeout"


def test_every_cannot_start_text_the_reconciler_writes_is_could_not_start():
    execution = SimpleNamespace(namespace="swarm-tenant-eng", name="exec-1")
    from_attempt, source = cannot_start_error(
        execution, None, SimpleNamespace(exit_code=78, error="config is missing TASK_ID")
    )
    assert source == "attempt"
    from_message, source = cannot_start_error(
        execution,
        SimpleNamespace(message='{"message": "startup failed", "cause": "dns never resolved"}'),
        None,
    )
    assert source == "termination_message"
    from_exit, source = cannot_start_error(execution, None, None)
    assert source == "exit_code"
    for text in (from_attempt, from_message, from_exit):
        assert classify_failure("FAILED", text, None) == "could_not_start", text


def test_the_reconcilers_requeue_text_is_lost_worker():
    repair = _src("apps/reconciler/reconciler/repair.py")
    assert 'to_state, error = TaskState.READY, f"reconciled: {finding.reason}"' in repair
    assert classify_failure("FAILED", "reconciled: lease silent for 300s", None) == "lost_worker"


def test_the_missing_outputs_text_is_the_workers():
    text = missing_error(["report.md"])
    assert classify_failure("FAILED", text, 0) == "outputs_missing", text


def test_a_dispatch_failure_is_the_schedulers_code_and_attempt():
    store = _src("apps/scheduler/scheduler/store.py")
    assert 'public = f"{error_code} (attempt {reference})"' in store
    dispatch = _src("apps/scheduler/scheduler/dispatch.py")
    codes = set(re.findall(r'code="([^"]+)"', dispatch))
    assert len(codes) >= 10, f"read too few dispatch codes to compare: {codes}"
    loop = _src("apps/scheduler/scheduler/loop.py")
    assert '"scheduler_internal_error"' in loop
    codes.add("scheduler_internal_error")
    for code in sorted(codes):
        text = f"{code} (attempt att_0123456789abcdef)"
        assert classify_failure("FAILED", text, None) == "dispatch_failed", (
            f"{code!r} would be counted as 'other': the classifier expects snake_case codes"
        )


def _rendered_opening(literal: str) -> str:
    """An f-string's first literal as it reads once rendered: `{METADATA_KEY}`
    is the worker's real key, every other placeholder a word."""
    text = literal.replace("{METADATA_KEY}", inputs_mod.METADATA_KEY)
    return re.sub(r"\{[^}]*\}", "x1", text)


def test_every_input_the_worker_refuses_to_stage_is_inputs_unavailable():
    """#185, decision 4. The worker ends the task with `str(InputUnavailable)` as
    the whole `last_error`, so each message's opening literal is what the
    fallback classifier sees. EVERY raise in inputs.py is rendered and held
    here, so a new refusal -- or a reworded one -- cannot drain into
    "runner error" unseen."""
    source = _src("apps/agent-worker/agent_worker/inputs.py")
    openings = re.findall(r'raise InputUnavailable\(\s*f?"((?:[^"\\]|\\.)*)"', source)
    assert len(openings) >= 20, f"read too few InputUnavailable messages to compare: {openings}"
    assert len(openings) == source.count("raise InputUnavailable("), (
        "a raise in inputs.py does not open with a string literal, so this pin cannot read it"
    )
    for literal in openings:
        text = _rendered_opening(literal)
        assert classify_failure("FAILED", text, 1) == "inputs_unavailable", (
            f"{text!r} (inputs.py) would be counted as a runner error"
        )


def test_the_two_refusals_dev_measured_are_the_workers_words():
    """dev, 2026-09-25: 10 of 13 "runner errors" were these two."""
    source = _src("apps/agent-worker/agent_worker/inputs.py")
    assert 'f"upstream task {upstream_task_id} did not produce an artifact named "' in source
    assert "f\"upstream tasks {', '.join(sorted(sources))} all stage {filename!r} \"" in source


def test_a_clean_exit_without_a_result_is_the_runners_error():
    lifecycle = _src("apps/agent-worker/agent_worker/lifecycle.py")
    assert 'error = "runner exited 0 without writing result.json"' in lifecycle
    text = "runner exited 0 without writing result.json"
    assert classify_failure("FAILED", text, 0) == "runner_error"


# --------------------------------------------------------------------------
# Cancel causes
# --------------------------------------------------------------------------

def test_every_writer_that_ends_a_requested_cancel_uses_the_prefix():
    for relative in (
        "apps/agent-worker/agent_worker/control.py",
        "apps/scheduler/scheduler/store.py",
        "apps/reconciler/reconciler/repair.py",
    ):
        assert 'f"cancelled on request; ' in _src(relative), relative
    assert cancel_cause(False, "cancelled on request; reconciled: lease expired") == "requested"


def test_the_failed_parent_text_is_the_schedulers_and_says_neither_which_parent_nor_why():
    """The scheduler writes the same words after a FAILED, a DEAD_LETTERED and a
    CANCELLED parent (`_FAILED_PARENT_STATES` holds all three), so the text is
    split by the parents' states (#185, decision 2)."""
    loop = _src("apps/scheduler/scheduler/loop.py")
    assert loop.count('"an upstream workflow step did not succeed"') >= 2
    assert "_FAILED_PARENT_STATES = frozenset(" in loop
    text = "an upstream workflow step did not succeed"
    assert cancel_cause(False, text, parent_states=["FAILED"]) == "after_failure"
    assert cancel_cause(False, text, parent_states=["CANCELLED"]) == "after_cancel"


def test_the_sweep_text_is_the_schedulers():
    loop = _src("apps/scheduler/scheduler/loop.py")
    assert 'f"workflow step {label} is {first[\'state\']} and on_step_failure is "' in loop
    assert 'f"{FAIL_WORKFLOW}, so steps that had not started were cancelled"' in loop
    for state in ("FAILED", "DEAD_LETTERED"):
        text = (
            f"workflow step synthesis is {state} and on_step_failure is "
            f"{FAIL_WORKFLOW}, so steps that had not started were cancelled"
        )
        assert cancel_cause(False, text) == "workflow_sweep", text


def test_a_sigterm_without_the_flag_is_other():
    lifecycle = _src("apps/agent-worker/agent_worker/lifecycle.py")
    assert 'error="runner stopped on SIGTERM"' in lifecycle
    assert cancel_cause(False, "runner stopped on SIGTERM") == "other"


def test_the_declared_cost_profiles_are_in_the_catalogue():
    assert DECLARED_COST_PROFILES <= set(RUNNER_PROFILES)

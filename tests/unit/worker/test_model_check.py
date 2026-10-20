"""The worker checks the MODEL it was given against the step's signed class
(MODEL-WORKER, docs/design/model-per-step.md §4.3, §4.5, §4.6; part of #1435).

A classed step (`claude-code`, `claude-code-browser`, `claude-code-review`,
`indexer`) names a model class by name, swarm-api signs it at format 5
(contract request 104), and the scheduler renders that class's pin as the
execution's MODEL. The worker holds what it was given to what was signed,
after the signature and before a credential is read or the CLI started:

* no MODEL on a classed profile ends `model_not_pinned`;
* a MODEL the expected class does not admit ends `model_class_mismatch`;
* an unsigned legacy task expects its profile's default, so a class written
  onto it fails whenever the scheduler dispatched that class's pin.

The attempt records the model it started with beside `started_at`, and a
stream that ran none of the pinned model is logged as `model_mismatch`.

Model IDs are built from the frozen vocabulary's family words, so a re-pin
in Terraform changes nothing here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

import spec_keys
from agent_worker import lifecycle
from agent_worker import specverify
from agent_worker.config import WorkerConfig
from agent_worker.errors import ExitCode
from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt
from swarm_common import specsign
from swarm_common.profiles import MODEL_CLASSES, RUNNER_PROFILES
from swarm_common.states import TaskState

TASK = "task_1"
#: Inside the legacy window, so an unsigned task can be admitted at all.
BEFORE_UNTIL = datetime(2026, 10, 1, tzinfo=timezone.utc)


def pin(model_class: str) -> str:
    """A model ID its class admits, as Terraform's `runner_models` pins one."""
    return f"claude-{MODEL_CLASSES[model_class].family}-5-5"


class ChildReached(Exception):
    """The runner child was about to start: the model check let it through."""


@pytest.fixture
def child(monkeypatch) -> list[str]:
    """Records whether the CLI would have started, and stops it there."""
    started: list[str] = []

    class NoChild:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started.append("runner child")
            raise ChildReached()

    monkeypatch.setattr(lifecycle, "ChildProcess", NoChild)
    return started


def _classed_task(db, *, model_class: str | None, runner_profile: str = "claude-code") -> dict:
    seed_attempt(db, task_id=TASK, runner_profile=runner_profile)
    # The tenant's provider credential, so the attempt that passes the check
    # gets as far as the CLI rather than parking for want of one.
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    doc = db.doc(f"tasks/{TASK}")
    if model_class is not None:
        doc["model_class"] = model_class
    return doc


def _assert_refused_before_the_agent(
    db, rc: int, cause: str, child: list[str], secrets: FakeSecretClient
) -> dict[str, Any]:
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.FAILED, (rc, task.get("last_error"))
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["end_cause"] == "cannot_start", task.get("end_cause")
    check = (task.get("result_summary") or {}).get("model_check")
    assert check is not None, task.get("result_summary")
    assert check["cause"] == cause, check
    assert cause in str(task.get("last_error")), task.get("last_error")
    assert child == [], "the CLI was started on a refused model"
    assert secrets.accessed == [], f"a credential was read before the refusal: {secrets.accessed}"
    return check


# ---------------------------------------------------------------------------
# Format 5: the signed class
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> WorkerConfig:
    base: dict[str, Any] = dict(
        task_id=TASK, attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=3, runner_profile="claude-code", project_id="p", region="us-central1",
        firestore_database="swarm", artifact_bucket="b",
        spec_signing_key=spec_keys.SIGNING_KEY, spec_verify_keys=dict(spec_keys.VERIFY_KEYS),
    )
    base.update(overrides)
    return WorkerConfig(**base)


def _signed_doc(model_class: str | None) -> dict[str, Any]:
    profile = RUNNER_PROFILES["claude-code"]
    doc: dict[str, Any] = {
        "id": TASK, "tenant_id": TENANT, "runner_profile": "claude-code",
        "resource_class": profile.resource_class, "timeout_seconds": profile.timeout_seconds,
        "max_attempts": 3, "provider": profile.provider, "input": {"prompt": "p"},
        "submitted_by": "alice@example.com", "depends_on": [], "metadata": {},
    }
    if model_class is not None:
        doc["model_class"] = model_class
    return spec_keys.sign_document(doc, TASK, spec_format=specsign.SPEC_FORMAT)


def test_a_task_signed_with_a_class_verifies_and_carries_its_signed_class():
    assert specsign.SPEC_FORMAT in specverify.KNOWN_FORMATS
    check = specverify.verify_step_spec(_signed_doc("sonnet"), create_time=None, cfg=_config())
    assert check.reason == "verified"
    assert check.model_class == "sonnet"


def test_a_class_written_onto_a_task_signed_without_one_is_refused():
    doc = _signed_doc(None)
    doc["model_class"] = "haiku"
    with pytest.raises(specverify.SpecSignatureInvalid) as exc:
        specverify.verify_step_spec(doc, create_time=None, cfg=_config())
    assert exc.value.reason in ("not_canonical", "signature_mismatch")


# ---------------------------------------------------------------------------
# The check, through the lifecycle
# ---------------------------------------------------------------------------


def test_a_sonnet_step_given_the_opus_pin_ends_model_class_mismatch_before_the_cli(
    db, worker_factory, child, log_stream
):
    _classed_task(db, model_class="sonnet")
    secrets = FakeSecretClient()
    worker, _, _ = worker_factory(
        task_id=TASK, runner_profile="claude-code", secret_client=secrets, model=pin("opus")
    )
    check = _assert_refused_before_the_agent(
        db, worker.run(), "model_class_mismatch", child, secrets
    )
    # Both sides, so an operator sees which one is wrong.
    assert check["model_class"] == "sonnet"
    assert check["model"] == pin("opus")
    logged = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    refusal = [r for r in logged if r.get("cause") == "model_class_mismatch"]
    assert refusal and refusal[0].get("model_class") == "sonnet"
    assert refusal[0].get("job_model") == pin("opus")


def test_a_classed_profile_with_no_model_ends_model_not_pinned(db, worker_factory, child):
    _classed_task(db, model_class=None)
    secrets = FakeSecretClient()
    worker, _, _ = worker_factory(
        task_id=TASK, runner_profile="claude-code", secret_client=secrets, model=None
    )
    check = _assert_refused_before_the_agent(db, worker.run(), "model_not_pinned", child, secrets)
    assert check["model_class"] == "opus"


def test_the_refusal_is_not_retried(db, worker_factory, child):
    """The next attempt is dispatched from the same signed class and meets
    the same MODEL: FAILED on attempt 1 of 3, never READY."""
    _classed_task(db, model_class="haiku")
    worker, _, _ = worker_factory(
        task_id=TASK, runner_profile="claude-code", model=pin("sonnet")
    )
    worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value
    assert (task.get("result_summary") or {}).get("model_check", {}).get("cause") == (
        "model_class_mismatch"
    ), task.get("result_summary")
    assert task["attempt_count"] == 1 and task["max_attempts"] == 3


@pytest.mark.parametrize("model_class", sorted(MODEL_CLASSES))
def test_a_step_given_its_own_classs_pin_reaches_the_cli(
    model_class, db, worker_factory, child
):
    """The control: every class, with its own pin, passes the check."""
    _classed_task(db, model_class=model_class)
    worker, _, _ = worker_factory(
        task_id=TASK, runner_profile="claude-code", model=pin(model_class)
    )
    worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert "model_check" not in (task.get("result_summary") or {}), task.get("result_summary")
    assert child == ["runner child"], task.get("last_error")


def test_a_step_naming_no_class_runs_its_profiles_default(db, worker_factory, child):
    _classed_task(db, model_class=None)
    worker, _, _ = worker_factory(task_id=TASK, runner_profile="claude-code", model=pin("opus"))
    worker.run()
    assert child == ["runner child"]


def test_a_class_the_profile_does_not_accept_is_refused(db, worker_factory, child):
    """`claude-code-review` accepts only `opus`: a signed `haiku` on it is a
    mismatch whatever MODEL it was given."""
    _classed_task(db, model_class="haiku", runner_profile="claude-code-review")
    secrets = FakeSecretClient()
    worker, _, _ = worker_factory(
        task_id=TASK, runner_profile="claude-code-review", secret_client=secrets,
        model=pin("haiku"),
    )
    _assert_refused_before_the_agent(db, worker.run(), "model_class_mismatch", child, secrets)


# ---------------------------------------------------------------------------
# Unsigned legacy tasks expect their profile's default
# ---------------------------------------------------------------------------


def _legacy_worker(worker_factory, **kwargs: Any):
    return worker_factory(
        task_id=TASK,
        runner_profile="claude-code",
        sign_spec=False,
        spec_signature_mode="legacy",
        spec_legacy_cutover=datetime(2026, 9, 29, tzinfo=timezone.utc),
        spec_clock=lambda: BEFORE_UNTIL,
        **kwargs,
    )


def test_an_unsigned_legacy_task_with_a_class_written_onto_it_is_refused(
    db, worker_factory, child
):
    """The scheduler resolved the written class to its pin; the worker
    ignores the unsigned class and expects `opus`, so the haiku pin fails."""
    _classed_task(db, model_class="haiku")
    db.create_times[f"tasks/{TASK}"] = datetime(2026, 9, 28, tzinfo=timezone.utc)
    secrets = FakeSecretClient()
    worker, _, _ = _legacy_worker(worker_factory, secret_client=secrets, model=pin("haiku"))
    check = _assert_refused_before_the_agent(
        db, worker.run(), "model_class_mismatch", child, secrets
    )
    assert check["model_class"] == "opus"


def test_an_unsigned_legacy_task_on_the_default_pin_still_runs(db, worker_factory, child):
    _classed_task(db, model_class=None)
    db.create_times[f"tasks/{TASK}"] = datetime(2026, 9, 28, tzinfo=timezone.utc)
    worker, _, _ = _legacy_worker(worker_factory, model=pin("opus"))
    worker.run()
    assert child == ["runner child"]


# ---------------------------------------------------------------------------
# What the attempt records (contract request 103)
# ---------------------------------------------------------------------------


def test_the_attempt_writes_its_model_with_started_at(db, worker_factory):
    seed_attempt(db, task_id=TASK)
    model = pin("haiku")
    worker, _, _ = worker_factory(task_id=TASK, model=model)
    assert worker.run() == ExitCode.OK
    attempt = db.doc("attempts/att_1")
    assert attempt["model"] == model
    assert attempt["started_at"] is not None


def test_an_attempt_with_no_model_writes_none(db, worker_factory):
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(task_id=TASK, model=None)
    assert worker.run() == ExitCode.OK
    assert "model" not in db.doc("attempts/att_1")


def test_a_stale_generation_writes_no_model(db, worker_factory):
    seed_attempt(db, task_id=TASK, generation=1, task_generation=2)
    worker, _, _ = worker_factory(task_id=TASK, generation=1, model=pin("opus"))
    assert worker.run() == ExitCode.GENERATION_FENCED
    # Fenced at STEP 1, before the attempt's first write: no record at all.
    assert "attempts/att_1" not in db.documents, db.documents.get("attempts/att_1")
    assert not any(
        path == "attempts/att_1" and "model" in data for _, path, data in db.writes
    )


def test_the_control_plane_records_the_model_beside_started_at(db, worker_factory):
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(task_id=TASK)
    worker.control.record_attempt_start(
        backend="cloud_run", execution_name=None, model=pin("fable")
    )
    attempt = db.doc("attempts/att_1")
    assert attempt["model"] == pin("fable")
    assert attempt["started_at"] is not None


# ---------------------------------------------------------------------------
# The stream's own record: `model_mismatch`
# ---------------------------------------------------------------------------


def _mismatch_lines(log_stream) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    return [r for r in lines if r.get("cause") == "model_mismatch"]


def test_a_stream_reporting_another_model_logs_model_mismatch(db, worker_factory, log_stream):
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(task_id=TASK, model=pin("opus"))
    worker._note_model_mismatch([pin("sonnet")])
    lines = _mismatch_lines(log_stream)
    assert len(lines) == 1, log_stream.getvalue()
    assert lines[0]["job_model"] == pin("opus")
    assert lines[0]["stream_models"] == [pin("sonnet")]


@pytest.mark.parametrize(
    "models",
    [
        [pin("opus")],
        # The CLI's own housekeeping model beside the pinned one is not a
        # substitution.
        [pin("haiku"), pin("opus")],
        [pin("opus") + "[1m]"],
        [],
        None,
    ],
)
def test_a_stream_that_ran_the_pin_logs_no_mismatch(models, db, worker_factory, log_stream):
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(task_id=TASK, model=pin("opus"))
    worker._note_model_mismatch(models)
    assert _mismatch_lines(log_stream) == []


def test_the_spend_collector_checks_the_stream(db, worker_factory, log_stream, tmp_path):
    """Through `_collect_spend`, the site that reads every runner's result."""
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(task_id=TASK, model=pin("opus"))
    seen: list[Any] = []
    worker._note_model_mismatch = seen.append  # type: ignore[method-assign]

    class Ws:
        result_path = tmp_path / "result.json"

    Ws.result_path.write_text(json.dumps(
        {"output": {"modelUsage": {pin("sonnet"): {}}, "total_cost_usd": 0.5}}
    ))
    worker.ws = Ws()  # type: ignore[assignment]
    worker._spend_pending = True
    worker._collect_spend()
    assert seen == [[pin("sonnet")]]

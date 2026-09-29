"""Unsigned tasks during the rollout (contract request 34, section 6).

Every task written before swarm-api signed carries no signature, and some are
still parked or queued when the verifying worker ships. `SPEC_SIGNATURE_MODE`
is `enforce` by default; in `legacy` an unsigned task runs only if Firestore's
own `create_time` -- which no client can write -- is before
`SPEC_LEGACY_CUTOVER`, and only until `SPEC_LEGACY_UNTIL`, which is in the
code, so a flag nobody turns off stops working anyway.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_worker.config import WorkerConfig
from agent_worker.errors import ConfigError, ExitCode
from conftest import seed_attempt
from swarm_common.states import TaskState

TASK = "task_1"
CUTOVER = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
BEFORE_CUTOVER = datetime(2026, 9, 28, tzinfo=timezone.utc)
AFTER_CUTOVER = datetime(2026, 9, 30, tzinfo=timezone.utc)
INSIDE_WINDOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
PAST_UNTIL = datetime(2026, 10, 20, 0, 0, 1, tzinfo=timezone.utc)


def _unsigned(db, created) -> None:
    seed_attempt(db, task_id=TASK)
    db.create_times[f"tasks/{TASK}"] = created
    assert "spec_signature" not in db.doc(f"tasks/{TASK}")


def _run(worker_factory, **config):
    worker, _, _ = worker_factory(task_id=TASK, sign_spec=False, **config)
    return worker.run()


def _lines(log_stream) -> list[dict]:
    out = []
    for line in log_stream.getvalue().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def test_unsigned_in_enforce_is_refused(db, worker_factory):
    _unsigned(db, BEFORE_CUTOVER)
    rc = _run(worker_factory, spec_clock=lambda: INSIDE_WINDOW)
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.FAILED
    assert task["end_cause"] == "spec_signature_invalid"
    assert task["result_summary"]["spec_check"]["reason"] == "unsigned"


def test_unsigned_in_legacy_before_the_cutover_runs_with_the_note(db, worker_factory, log_stream):
    _unsigned(db, BEFORE_CUTOVER)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW,
    )
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.OK, task.get("last_error")
    assert task["state"] == TaskState.SUCCEEDED.value
    warnings = [
        r for r in _lines(log_stream)
        if r.get("severity") == "WARNING" and r.get("spec_check", {}).get("reason") == "legacy_unsigned"
    ]
    assert warnings, "a legacy task ran with no WARNING naming it"
    events = [
        e for e in db.events(TASK)
        if (e.get("detail") or {}).get("spec_check", {}).get("reason") == "legacy_unsigned"
    ]
    assert events, "the legacy note is not on the task's event"


def test_unsigned_in_legacy_after_the_cutover_is_refused(db, worker_factory):
    """Stripping the signature off a task created after the cutover does not
    qualify it: `create_time` is Firestore's, not the document's."""
    _unsigned(db, AFTER_CUTOVER)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW,
    )
    assert rc == ExitCode.FAILED
    assert db.doc(f"tasks/{TASK}")["result_summary"]["spec_check"]["reason"] == "unsigned"


def test_unsigned_in_legacy_with_no_create_time_is_refused(db, worker_factory):
    seed_attempt(db, task_id=TASK)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW,
    )
    assert rc == ExitCode.FAILED
    assert db.doc(f"tasks/{TASK}")["result_summary"]["spec_check"]["reason"] == "unsigned"


def test_legacy_with_no_cutover_admits_nothing(db, worker_factory):
    _unsigned(db, BEFORE_CUTOVER)
    rc = _run(worker_factory, spec_signature_mode="legacy", spec_clock=lambda: INSIDE_WINDOW)
    assert rc == ExitCode.FAILED
    assert db.doc(f"tasks/{TASK}")["result_summary"]["spec_check"]["reason"] == "unsigned"


def test_after_spec_legacy_until_the_flag_is_ignored_and_says_so(db, worker_factory, log_stream):
    _unsigned(db, BEFORE_CUTOVER)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=CUTOVER,
        spec_clock=lambda: PAST_UNTIL,
    )
    assert rc == ExitCode.FAILED
    assert db.doc(f"tasks/{TASK}")["result_summary"]["spec_check"]["reason"] == "unsigned"
    ignored = [r for r in _lines(log_stream) if "SPEC_LEGACY_UNTIL" in str(r.get("message"))]
    assert ignored, "the worker enforced past SPEC_LEGACY_UNTIL without saying it ignored legacy"


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------

_IDENTITY = {
    "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
    "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
    "PROJECT_ID": "swarm-test",
}


def _env(monkeypatch, **extra: str) -> None:
    for name in ("SPEC_SIGNATURE_MODE", "SPEC_LEGACY_CUTOVER", "SPEC_VERIFY_KEYS",
                 "SPEC_SIGNING_KEY", "TASK_TIMEOUT_SECONDS", "CLOUD_RUN_JOB",
                 "RUNNER_JOB_NAME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**_IDENTITY, **extra}.items():
        monkeypatch.setenv(name, value)


def test_the_mode_defaults_to_enforce(monkeypatch):
    _env(monkeypatch)
    cfg = WorkerConfig.from_env()
    assert cfg.spec_signature_mode == "enforce"
    assert cfg.spec_legacy_cutover is None
    assert cfg.task_timeout_env is None


def test_the_environment_is_read(monkeypatch):
    import spec_keys

    _env(
        monkeypatch,
        SPEC_SIGNATURE_MODE="legacy",
        SPEC_LEGACY_CUTOVER="2026-09-29T12:00:00Z",
        SPEC_SIGNING_KEY=spec_keys.SIGNING_KEY,
        SPEC_VERIFY_KEYS=json.dumps(spec_keys.VERIFY_KEYS),
        TASK_TIMEOUT_SECONDS="600",
        CLOUD_RUN_JOB="swarm-job-eng-mock",
    )
    cfg = WorkerConfig.from_env()
    assert cfg.spec_signature_mode == "legacy"
    assert cfg.spec_legacy_cutover == CUTOVER
    assert cfg.spec_signing_key == spec_keys.SIGNING_KEY
    assert dict(cfg.spec_verify_keys) == spec_keys.VERIFY_KEYS
    assert cfg.task_timeout_env == 600 and cfg.timeout_seconds == 600
    assert cfg.cloud_run_job == "swarm-job-eng-mock"


def test_the_gke_mount_is_read_when_the_environment_has_no_keys(monkeypatch, tmp_path):
    import spec_keys
    from agent_worker import specverify

    mount = tmp_path / "spec-verify-keys"
    mount.mkdir()
    (mount / "SPEC_VERIFY_KEYS").write_text(json.dumps(spec_keys.VERIFY_KEYS))
    (mount / "SPEC_SIGNING_KEY").write_text(spec_keys.SIGNING_KEY)
    monkeypatch.setattr(specverify, "VERIFY_KEYS_MOUNT", mount)
    _env(monkeypatch, RUNNER_JOB_NAME="swarm-1-1")
    cfg = WorkerConfig.from_env()
    assert dict(cfg.spec_verify_keys) == spec_keys.VERIFY_KEYS
    assert cfg.spec_signing_key == spec_keys.SIGNING_KEY
    assert cfg.runner_job_name == "swarm-1-1"


@pytest.mark.parametrize(
    "name,value",
    [
        ("SPEC_SIGNATURE_MODE", "off"),
        ("SPEC_LEGACY_CUTOVER", "yesterday"),
        ("SPEC_LEGACY_CUTOVER", "2026-09-29T12:00:00"),  # no zone: ambiguous
        ("SPEC_VERIFY_KEYS", "{not json"),
        ("SPEC_VERIFY_KEYS", '["a list"]'),
    ],
)
def test_a_malformed_setting_is_the_workers_configuration(monkeypatch, name, value):
    _env(monkeypatch, **{name: value})
    with pytest.raises(ConfigError):
        WorkerConfig.from_env()

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
# The cutover is capped at the signing release (#355)
# ---------------------------------------------------------------------------
# swarm-api has signed every task it creates since swarm-api-00119-lcs took
# traffic, 2026-09-30T21:24:36Z. An unsigned task Firestore created after that
# moment was never written by an unsigning swarm-api: its signature was
# stripped. So no configured SPEC_LEGACY_CUTOVER, however late, admits one.

SIGNING_RELEASE = datetime(2026, 9, 30, 21, 24, 36, tzinfo=timezone.utc)
LATE_CUTOVER = datetime(2026, 10, 15, tzinfo=timezone.utc)
AFTER_RELEASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
INSIDE_WINDOW_LATE = datetime(2026, 10, 16, tzinfo=timezone.utc)


def test_the_signing_release_is_in_the_code():
    from agent_worker import specverify

    assert specverify.SPEC_SIGNING_RELEASED_AT == SIGNING_RELEASE
    assert specverify.SPEC_SIGNING_RELEASED_AT < specverify.SPEC_LEGACY_UNTIL


def test_a_legacy_spec_after_the_signing_release_is_refused_with_its_reason(db, worker_factory):
    """A cutover set later than the release does not reopen the window."""
    _unsigned(db, AFTER_RELEASE)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=LATE_CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW_LATE,
    )
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.FAILED
    assert task["end_cause"] == "spec_signature_invalid"
    assert task["result_summary"]["spec_check"]["reason"] == "unsigned"
    assert "after the legacy cutover" in str(task.get("last_error")), task.get("last_error")
    assert SIGNING_RELEASE.isoformat() in str(task.get("last_error"))


def test_a_legacy_spec_at_the_signing_release_is_refused(db, worker_factory):
    _unsigned(db, SIGNING_RELEASE)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=LATE_CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW_LATE,
    )
    assert rc == ExitCode.FAILED
    assert db.doc(f"tasks/{TASK}")["result_summary"]["spec_check"]["reason"] == "unsigned"


def test_a_task_parked_before_the_signing_release_still_runs_under_a_late_cutover(
    db, worker_factory
):
    """The constraint in #355: tightening must not refuse a task parked
    before the signing release."""
    _unsigned(db, BEFORE_CUTOVER)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=LATE_CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW_LATE,
    )
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.OK, task.get("last_error")
    assert task["state"] == TaskState.SUCCEEDED.value


def test_a_legacy_refusal_names_the_cutover_it_was_judged_against(db, worker_factory):
    _unsigned(db, AFTER_CUTOVER)
    rc = _run(
        worker_factory,
        spec_signature_mode="legacy",
        spec_legacy_cutover=CUTOVER,
        spec_clock=lambda: INSIDE_WINDOW,
    )
    assert rc == ExitCode.FAILED
    last_error = str(db.doc(f"tasks/{TASK}").get("last_error"))
    assert "after the legacy cutover" in last_error and CUTOVER.isoformat() in last_error


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


# ---------------------------------------------------------------------------
# GKE follows the legacy window exactly as Cloud Run does (owner decision,
# 2026-09-29). A GKE pod's environment carries none of the four settings:
# kubernetes/render.py writes every key of terraform's
# `spec_verify_keys_configmap` output -- SPEC_VERIFY_KEYS, SPEC_SIGNING_KEY,
# SPEC_SIGNATURE_MODE, SPEC_LEGACY_CUTOVER -- as a file of the
# `swarm-spec-verify-keys` ConfigMap, mounted at /etc/swarm/spec-verify-keys.
# A worker that read only the keys from the mount enforced on GKE from its
# first day while Cloud Run ran legacy.
# ---------------------------------------------------------------------------


def _gke_mount(tmp_path, **files: str):
    mount = tmp_path / "spec-verify-keys"
    mount.mkdir(parents=True)
    for name, value in files.items():
        (mount / name).write_text(value)
    return mount


def _gke_legacy_mount(tmp_path):
    import spec_keys

    return _gke_mount(
        tmp_path,
        SPEC_VERIFY_KEYS=json.dumps(spec_keys.VERIFY_KEYS),
        SPEC_SIGNING_KEY=spec_keys.SIGNING_KEY,
        SPEC_SIGNATURE_MODE="legacy",
        SPEC_LEGACY_CUTOVER="2026-09-29T12:00:00Z",
    )


def _gke_config(monkeypatch, mount) -> WorkerConfig:
    """A GKE pod's configuration: no SPEC_* in the environment, the mount at `mount`."""
    from agent_worker import specverify

    monkeypatch.setattr(specverify, "VERIFY_KEYS_MOUNT", mount)
    _env(monkeypatch, RUNNER_JOB_NAME="swarm-1-1")
    return WorkerConfig.from_env()


def _spec_settings(cfg: WorkerConfig) -> dict:
    """The four settings a GKE pod's `from_env` read, for `build_worker`."""
    return {
        "spec_signature_mode": cfg.spec_signature_mode,
        "spec_legacy_cutover": cfg.spec_legacy_cutover,
        "spec_signing_key": cfg.spec_signing_key,
        "spec_verify_keys": dict(cfg.spec_verify_keys),
    }


def test_the_gke_mount_carries_the_mode_and_the_cutover(monkeypatch, tmp_path):
    cfg = _gke_config(monkeypatch, _gke_legacy_mount(tmp_path))
    assert cfg.spec_signature_mode == "legacy"
    assert cfg.spec_legacy_cutover == CUTOVER


def test_the_environment_wins_over_the_gke_mount(monkeypatch, tmp_path):
    """The mount fills only what the environment leaves unset, as it already
    does for the keys; Cloud Run's Jobs carry all four in the environment."""
    from agent_worker import specverify

    monkeypatch.setattr(specverify, "VERIFY_KEYS_MOUNT", _gke_legacy_mount(tmp_path))
    _env(monkeypatch, SPEC_SIGNATURE_MODE="enforce")
    assert WorkerConfig.from_env().spec_signature_mode == "enforce"


@pytest.mark.parametrize(
    "name,value",
    [
        ("SPEC_SIGNATURE_MODE", "off"),
        ("SPEC_LEGACY_CUTOVER", "2026-09-29T12:00:00"),  # no zone
    ],
)
def test_a_malformed_setting_in_the_gke_mount_is_the_workers_configuration(
    monkeypatch, tmp_path, name, value
):
    import spec_keys

    files = {
        "SPEC_VERIFY_KEYS": json.dumps(spec_keys.VERIFY_KEYS),
        "SPEC_SIGNING_KEY": spec_keys.SIGNING_KEY,
        "SPEC_SIGNATURE_MODE": "legacy",
        "SPEC_LEGACY_CUTOVER": "2026-09-29T12:00:00Z",
        name: value,
    }
    with pytest.raises(ConfigError):
        _gke_config(monkeypatch, _gke_mount(tmp_path, **files))


def test_a_gke_worker_in_legacy_admits_an_unsigned_task_created_before_the_cutover(
    monkeypatch, tmp_path, db, worker_factory
):
    cfg = _gke_config(monkeypatch, _gke_legacy_mount(tmp_path / "gke"))
    _unsigned(db, BEFORE_CUTOVER)
    rc = _run(worker_factory, spec_clock=lambda: INSIDE_WINDOW, **_spec_settings(cfg))
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.OK, task.get("last_error")
    assert task["state"] == TaskState.SUCCEEDED.value


def test_a_gke_worker_in_legacy_refuses_an_unsigned_task_created_after_the_cutover(
    monkeypatch, tmp_path, db, worker_factory
):
    cfg = _gke_config(monkeypatch, _gke_legacy_mount(tmp_path / "gke"))
    _unsigned(db, AFTER_CUTOVER)
    rc = _run(worker_factory, spec_clock=lambda: INSIDE_WINDOW, **_spec_settings(cfg))
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.FAILED
    assert task["end_cause"] == "spec_signature_invalid"
    assert task["result_summary"]["spec_check"]["reason"] == "unsigned"


@pytest.mark.parametrize("created", [BEFORE_CUTOVER, AFTER_CUTOVER])
def test_a_gke_worker_with_no_configmap_is_cannot_start_for_an_unsigned_task(
    monkeypatch, tmp_path, db, worker_factory, created
):
    """The volume is `optional`, so a namespace with no ConfigMap still starts
    its pod, and that pod has no key, no mode and no cutover. It must neither
    admit an unsigned task nor refuse the tenant's task as a signature failure:
    it cannot verify anything, which is CANNOT_START -- its own configuration."""
    cfg = _gke_config(monkeypatch, tmp_path / "no-configmap-mounted")
    assert not cfg.spec_verify_keys
    _unsigned(db, created)
    rc = _run(worker_factory, spec_clock=lambda: INSIDE_WINDOW, **_spec_settings(cfg))
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.CONFIG
    assert task.get("end_cause") != "spec_signature_invalid"
    assert task["state"] != TaskState.SUCCEEDED.value


def test_a_gke_worker_with_no_configmap_is_cannot_start_for_a_signed_task(
    monkeypatch, tmp_path, db, worker_factory
):
    cfg = _gke_config(monkeypatch, tmp_path / "no-configmap-mounted")
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(
        task_id=TASK, spec_clock=lambda: INSIDE_WINDOW, **_spec_settings(cfg)
    )
    assert worker.run() == ExitCode.CONFIG
    assert db.doc(f"tasks/{TASK}")["state"] != TaskState.SUCCEEDED.value

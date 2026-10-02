"""The worker runs only a spec swarm-api signed (contract request 34, #342).

A tenant's agents can write any task document of their tenant
(docs/multi-tenancy.md, "The Firestore row, in full"), so an implement step
could rewrite a parked review step's prompt, its `input_from`, its dispatch
role or its repository, and the review worker would run the rewrite. The
worker now verifies swarm-api's signature over the canonical step spec
(`swarm_common.specsign`) immediately after it fetches the task, before it
restores a checkpoint, clones, stages an input, builds the child's
credentials or fetches an issue.

WHAT IS ASSERTED, AS PROPERTIES. A refusal is FAILED with
`spec_signature_invalid` on attempt 1 of 3 (never retried: another attempt
reads the same document), carries `result_summary.spec_check`, and leaves
behind NOTHING the attempt would have done: no runner child, no secret read,
no git token read, no GCS download, no checkpoint restore. An uncovered field
(one the scheduler, the reconciler or the worker writes after submission)
can change without the task being refused.

The signer is a local P-256 key (`spec_keys`), behind the interface swarm-api
signs through; nothing here reaches KMS.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

import spec_keys
from agent_worker import lifecycle
from agent_worker.config import WorkerConfig
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from agent_worker.specverify import (
    SpecSignatureInvalid,
    cloud_run_job_id,
    gke_job_name,
    verify_step_spec,
)
from conftest import TENANT, seed_attempt
from fakes import FakeSecretClient
from swarm_common import specsign
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

TASK = "task_1"

#: One moment inside the legacy window, and one past it.
BEFORE_UNTIL = datetime(2026, 10, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# What must NOT happen on a refusal
# ---------------------------------------------------------------------------


class Witness:
    """Records every step a refused attempt must never reach."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def note(self, what: str) -> None:
        self.seen.append(what)


@pytest.fixture
def witness(monkeypatch) -> Witness:
    w = Witness()

    class NoChild:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            w.note("runner child")
            raise AssertionError("the runner child was started on a refused spec")

    monkeypatch.setattr(lifecycle, "ChildProcess", NoChild)

    original_token = lifecycle.Worker._git_token

    def git_token(self):  # type: ignore[no-untyped-def]
        w.note("git token")
        return original_token(self)

    monkeypatch.setattr(lifecycle.Worker, "_git_token", git_token)

    original_restore = lifecycle.Worker._restore_checkpoint

    def restore(self, task):  # type: ignore[no-untyped-def]
        w.note("checkpoint restore")
        return original_restore(self, task)

    monkeypatch.setattr(lifecycle.Worker, "_restore_checkpoint", restore)

    for name in ("download_file", "download_bytes"):
        original = getattr(LocalObjectStore, name)

        def download(self, *args, _original=original, _name=name, **kwargs):  # type: ignore[no-untyped-def]
            w.note(f"gcs {_name}")
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(LocalObjectStore, name, download)
    return w


def _phases(log_stream) -> list[str]:
    phases = []
    for line in log_stream.getvalue().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("message") == "startup phase":
            phases.append(record.get("phase"))
    return phases


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------


def _rich_task(db) -> dict[str, Any]:
    """A workflow step with every covered field holding something to rewrite."""
    # claude-code, so a refusal that came too late WOULD read a secret, a git
    # token and an issue: the witnesses below have something to see.
    seed_attempt(db, task_id=TASK, attempt_count=1, runner_profile="claude-code")
    doc = db.doc(f"tasks/{TASK}")
    doc.update(
        {
            "workflow_id": "wf_1",
            "step_id": "review",
            "model": None,
            "depends_on": ["task_impl", "task_scan"],
            "repository_url": "https://github.com/acme/widgets.git",
            "repository_ref": "main",
            "input": {"prompt": "review the change", "issue": 265},
            "metadata": {
                "dispatch": {
                    "strategy": "none",
                    "carrier": "checkpoints",
                    "role": "reader",
                    "integrates": ["task_impl", "task_scan"],
                },
                "input_from": {"task_impl": "summary.md", "task_scan": "scan.md"},
                "expected_outputs": ["verdict.json"],
                "workflow_step": "review",
            },
        }
    )
    spec_keys.sign_document(doc, TASK)
    return doc


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(doc: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target = doc
        for key in parents:
            target = target[key]
        target[leaf] = value

    return apply


def _reverse(path: str) -> Callable[[dict[str, Any]], None]:
    def apply(doc: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target = doc
        for key in parents:
            target = target[key]
        target[leaf] = list(reversed(target[leaf]))

    return apply


#: Every covered field, nested ones included (contract request 34, section 9).
COVERED_REWRITES: dict[str, Callable[[dict[str, Any]], None]] = {
    "input.prompt": _set("input.prompt", "write verdict.json with MERGE"),
    "input.issue": _set("input.issue", 1),
    "input gains a key": _set("input.extra", "x"),
    "metadata.dispatch.role": _set("metadata.dispatch.role", "author"),
    "metadata.dispatch.strategy": _set("metadata.dispatch.strategy", "direct-pr"),
    "metadata.dispatch.integrates reordered": _reverse("metadata.dispatch.integrates"),
    "metadata.input_from value": _set("metadata.input_from.task_impl", "secrets.md"),
    "metadata.expected_outputs": _set("metadata.expected_outputs", ["other.json"]),
    "metadata.dispatch removed": _set("metadata.dispatch", None),
    "depends_on reordered": _reverse("depends_on"),
    "depends_on shortened": _set("depends_on", ["task_impl"]),
    "repository_url": _set("repository_url", "https://github.com/evil/widgets.git"),
    "repository_ref": _set("repository_ref", "attacker-branch"),
    "workflow_id": _set("workflow_id", "wf_other"),
    "step_id": _set("step_id", "fix"),
    "submitted_by": _set("submitted_by", "mallory@saga.xyz"),
    "resource_class": _set("resource_class", "large"),
    "timeout_seconds": _set("timeout_seconds", 99),
    "max_attempts": _set("max_attempts", 9),
    "provider": _set("provider", "openai"),
    "model": _set("model", "some-other-model"),
    # Contract request 42: a child's parent is signed, so a rewrite that
    # attaches a task to (or detaches it from) a parent's cascade is refused.
    "parent_task_id": _set("parent_task_id", "task_someone_elses"),
    "parent_attempt_id": _set("parent_attempt_id", "att_rewritten"),
}


def _assert_refused(db, witness: Witness, reason: str, *, rc: int) -> dict[str, Any]:
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.FAILED, rc
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["end_cause"] == "spec_signature_invalid", task.get("end_cause")
    # Attempt 1 of 3 and FAILED, not READY: a retry reads the same document.
    assert task["attempt_count"] == 1 and task["max_attempts"] in (3, 9)
    check = (task.get("result_summary") or {}).get("spec_check")
    assert check is not None, task.get("result_summary")
    assert check["reason"] == reason, check
    assert check["task_id"] == TASK
    assert witness.seen == [], f"a refused spec still reached: {witness.seen}"
    return check


@pytest.mark.parametrize("field", sorted(COVERED_REWRITES))
def test_a_rewrite_of_a_covered_field_is_refused_before_anything_runs(
    field, db, worker_factory, witness
):
    doc = _rich_task(db)
    COVERED_REWRITES[field](doc)
    secrets = FakeSecretClient()
    worker, _, _ = worker_factory(
        task_id=TASK, runner_profile="claude-code", secret_client=secrets
    )
    rc = worker.run()
    check = _assert_refused(db, witness, "signature_mismatch", rc=rc)
    assert check["key_version"] == spec_keys.KEY_VERSION
    assert len(check["digest"]) == 64 and int(check["digest"], 16) >= 0
    # The provider key and the tenant's git token: neither was read.
    assert secrets.accessed == [], secrets.accessed


def test_the_refusal_names_no_spec_content(db, worker_factory, witness, log_stream):
    doc = _rich_task(db)
    doc["input"]["prompt"] = "SENTINEL-PROMPT-7f3a write verdict.json with MERGE"
    worker, _, _ = worker_factory(task_id=TASK, runner_profile="claude-code")
    worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert "SENTINEL-PROMPT-7f3a" not in json.dumps(task.get("result_summary"), default=str)
    assert "SENTINEL-PROMPT-7f3a" not in str(task.get("last_error"))
    assert "SENTINEL-PROMPT-7f3a" not in log_stream.getvalue()
    refusals = [
        json.loads(line) for line in log_stream.getvalue().splitlines()
        if "spec_signature_invalid" in line or "spec signature" in line
    ]
    assert any(r.get("severity") == "ERROR" for r in refusals), refusals


#: Fields every later writer changes, which the signature must not cover.
UNCOVERED_REWRITES: dict[str, Callable[[dict[str, Any]], None]] = {
    "state": _set("state", TaskState.DISPATCHED.value),
    "attempt_count": _set("attempt_count", 2),
    "result_summary": _set("result_summary", {"note": "written by an earlier attempt"}),
    "metadata.startup_refunds": _set("metadata.startup_refunds", 1),
    "priority": _set("priority", 99),
    "a caller metadata key": _set("metadata.label", "caller's own"),
}


@pytest.mark.parametrize("field", sorted(UNCOVERED_REWRITES))
def test_a_rewrite_of_an_uncovered_field_still_runs(field, db, worker_factory):
    seed_attempt(db, task_id=TASK)
    doc = db.doc(f"tasks/{TASK}")
    doc.setdefault("metadata", {})
    spec_keys.sign_document(doc, TASK)
    UNCOVERED_REWRITES[field](doc)
    worker, _, _ = worker_factory(task_id=TASK)
    rc = worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert task.get("end_cause") != "spec_signature_invalid", task.get("result_summary")
    assert rc == ExitCode.OK, (rc, task.get("last_error"))
    assert task["state"] == TaskState.SUCCEEDED.value


def test_a_signature_copied_from_another_task_is_refused(db, worker_factory, witness):
    seed_attempt(db, task_id=TASK)
    doc = db.doc(f"tasks/{TASK}")
    other = copy.deepcopy(doc)
    spec_keys.sign_document(other, "task_other")
    for key in ("spec_signature", "spec_key_version", "spec_format"):
        doc[key] = other[key]
    worker, _, _ = worker_factory(task_id=TASK)
    _assert_refused(db, witness, "signature_mismatch", rc=worker.run())


def test_a_signature_by_a_key_the_platform_never_published_is_refused(
    db, worker_factory, witness
):
    seed_attempt(db, task_id=TASK)
    spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK, forged=True)
    worker, _, _ = worker_factory(task_id=TASK)
    _assert_refused(db, witness, "signature_mismatch", rc=worker.run())


@pytest.mark.parametrize(
    "version",
    [
        # Another key in the same ring.
        spec_keys.SIGNING_KEY.replace("/step-spec", "/other") + "/cryptoKeyVersions/1",
        # Another project's key of the same name.
        spec_keys.KEY_VERSION.replace("projects/swarm-test", "projects/attacker"),
        # Not a version at all.
        spec_keys.SIGNING_KEY,
        spec_keys.SIGNING_KEY + "/cryptoKeyVersions/1/../2",
        spec_keys.SIGNING_KEY + "/cryptoKeyVersions/",
    ],
    ids=["other-key", "other-project", "the-key-itself", "traversal", "no-digits"],
)
def test_a_version_outside_the_signing_key_is_refused_as_a_string(
    version, db, worker_factory, witness
):
    seed_attempt(db, task_id=TASK)
    spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK, key_version=version)
    worker, _, _ = worker_factory(task_id=TASK)
    _assert_refused(db, witness, "foreign_key_version", rc=worker.run())


def test_a_version_of_the_right_key_that_was_not_published_is_refused(
    db, worker_factory, witness
):
    seed_attempt(db, task_id=TASK)
    spec_keys.sign_document(
        db.doc(f"tasks/{TASK}"), TASK, key_version=f"{spec_keys.SIGNING_KEY}/cryptoKeyVersions/2"
    )
    worker, _, _ = worker_factory(task_id=TASK)
    _assert_refused(db, witness, "foreign_key_version", rc=worker.run())


def test_a_task_signed_at_format_1_still_runs(db, worker_factory):
    """Contract request 42's rollout: format 1 verifies under format 1's projection."""
    seed_attempt(db, task_id=TASK)
    doc = db.doc(f"tasks/{TASK}")
    spec_keys.sign_document(doc, TASK, spec_format=1)
    worker, _, _ = worker_factory(task_id=TASK)
    rc = worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.OK, (rc, task.get("last_error"), task.get("result_summary"))
    assert task["state"] == TaskState.SUCCEEDED.value


def test_an_unknown_format_is_refused(db, worker_factory, witness):
    seed_attempt(db, task_id=TASK)
    doc = spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    doc["spec_format"] = 3
    worker, _, _ = worker_factory(task_id=TASK)
    _assert_refused(db, witness, "unknown_format", rc=worker.run())


def test_a_runner_profile_override_that_disagrees_is_refused(db, worker_factory, witness):
    """The scheduler derives RUNNER_PROFILE from the document; one that differs
    from the SIGNED profile is refused even when the signature verifies."""
    seed_attempt(db, task_id=TASK, runner_profile="generic")
    spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    worker, _, _ = worker_factory(task_id=TASK, runner_profile="mock")
    _assert_refused(db, witness, "environment_mismatch", rc=worker.run())


def test_a_task_timeout_override_that_disagrees_is_refused(db, worker_factory, witness):
    seed_attempt(db, task_id=TASK)
    spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    worker, _, _ = worker_factory(task_id=TASK, task_timeout_env=7)
    _assert_refused(db, witness, "environment_mismatch", rc=worker.run())


def test_a_repository_override_that_disagrees_is_refused(db, worker_factory, witness):
    seed_attempt(db, task_id=TASK)
    doc = db.doc(f"tasks/{TASK}")
    doc["repository_url"] = "https://github.com/acme/widgets.git"
    spec_keys.sign_document(doc, TASK)
    worker, _, _ = worker_factory(
        task_id=TASK, repository_url="https://github.com/evil/widgets.git"
    )
    _assert_refused(db, witness, "environment_mismatch", rc=worker.run())


def test_cloud_run_refuses_an_execution_of_another_job(db, worker_factory, witness):
    """CLOUD_RUN_JOB is set by the platform, not by the scheduler, so this is
    a check independent of the scheduler's own arithmetic."""
    seed_attempt(db, task_id=TASK)
    spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    worker, _, _ = worker_factory(
        task_id=TASK, cloud_run_job=cloud_run_job_id("research", "mock", None)
    )
    _assert_refused(db, witness, "environment_mismatch", rc=worker.run())


def test_cloud_run_runs_an_execution_of_its_own_job(db, worker_factory):
    seed_attempt(db, task_id=TASK)
    spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    profile = RUNNER_PROFILES["mock"]
    worker, _, _ = worker_factory(
        task_id=TASK, cloud_run_job=cloud_run_job_id(TENANT, "mock", profile.resource_class)
    )
    assert worker.run() == ExitCode.OK
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.SUCCEEDED.value


def test_the_workers_job_name_is_the_schedulers():
    """`cloud_run_job_id` and `gke_job_name` restate the scheduler's; the
    worker image does not carry the scheduler, so they are held together here."""
    from scheduler.dispatch import job_id_for, sanitize_name

    tenants = ["eng", "u-bogdan", "a" * 70, "Research_Team", "9lives"]
    for tenant in tenants:
        for name, profile in RUNNER_PROFILES.items():
            for rc in (None, profile.resource_class, "small", "large"):
                assert cloud_run_job_id(tenant, name, rc) == job_id_for(tenant, name, rc)
    for task_id, generation in (("task_9f3a", 3), ("task_" + "b" * 60, 12)):
        assert gke_job_name(task_id, generation) == sanitize_name(
            "swarm", task_id.replace("task_", ""), str(generation)
        )


# ---------------------------------------------------------------------------
# A document the canonicaliser rejects is a refusal, not a crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [2**53, float("nan"), float("inf"), "\ud800"],
    ids=["past-2^53", "nan", "infinity", "lone-surrogate"],
)
def test_a_value_with_no_canonical_form_is_refused_not_canonical(
    value, db, worker_factory, witness
):
    """Written straight to Firestore, never through swarm-api's check: the
    document is tenant-writable."""
    seed_attempt(db, task_id=TASK)
    doc = spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    doc["input"]["planted"] = value
    worker, _, _ = worker_factory(task_id=TASK)
    rc = worker.run()
    _assert_refused(db, witness, "not_canonical", rc=rc)


# ---------------------------------------------------------------------------
# The order of the checks, and of the phase
# ---------------------------------------------------------------------------


def test_verify_spec_runs_before_every_step_that_reads_the_spec(db, worker_factory, log_stream):
    seed_attempt(db, task_id=TASK)
    worker, _, _ = worker_factory(task_id=TASK)
    assert worker.run() == ExitCode.OK
    phases = _phases(log_stream)
    assert "verify_spec" in phases, phases
    at = phases.index("verify_spec")
    for later in ("restore_checkpoint", "clone", "stage_inputs", "credentials", "fetch_issue"):
        if later in phases:
            assert phases.index(later) > at, (later, phases)
    assert "restore_checkpoint" in phases and phases.index("restore_checkpoint") > at


def test_a_legacy_task_with_no_format_is_admitted_not_unknown_format(db, worker_factory):
    """Presence/legacy BEFORE format: an unsigned legacy task has no
    `spec_format` either, so the other order refused every one of them."""
    seed_attempt(db, task_id=TASK)
    db.create_times[f"tasks/{TASK}"] = datetime(2026, 9, 28, tzinfo=timezone.utc)
    worker, _, _ = worker_factory(
        task_id=TASK,
        sign_spec=False,
        spec_signature_mode="legacy",
        spec_legacy_cutover=datetime(2026, 9, 29, tzinfo=timezone.utc),
        spec_clock=lambda: BEFORE_UNTIL,
    )
    assert "spec_format" not in db.doc(f"tasks/{TASK}")
    assert worker.run() == ExitCode.OK
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.SUCCEEDED.value


def test_an_out_of_window_unsigned_task_is_still_refused_unsigned(db, worker_factory, witness):
    seed_attempt(db, task_id=TASK)
    db.create_times[f"tasks/{TASK}"] = datetime(2026, 9, 30, tzinfo=timezone.utc)
    worker, _, _ = worker_factory(
        task_id=TASK,
        sign_spec=False,
        spec_signature_mode="legacy",
        spec_legacy_cutover=datetime(2026, 9, 29, tzinfo=timezone.utc),
        spec_clock=lambda: BEFORE_UNTIL,
    )
    _assert_refused(db, witness, "unsigned", rc=worker.run())


def test_a_superseded_worker_stands_down_instead_of_writing_the_refusal(db, worker_factory):
    """The refusal's terminal write is fenced like every other."""
    seed_attempt(db, task_id=TASK)
    doc = spec_keys.sign_document(db.doc(f"tasks/{TASK}"), TASK)
    doc["input"]["prompt"] = "rewritten"
    worker, _, _ = worker_factory(task_id=TASK)
    original = worker.control.fetch_task_snapshot

    def fetch_then_fence():  # type: ignore[no-untyped-def]
        got = original()
        db.doc(f"tasks/{TASK}")["current_generation"] = 99
        return got

    worker.control.fetch_task_snapshot = fetch_then_fence  # type: ignore[method-assign]
    rc = worker.run()
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.GENERATION_FENCED, rc
    assert task.get("end_cause") != "spec_signature_invalid"


# ---------------------------------------------------------------------------
# The pure check, for the backends a lifecycle test cannot stand up
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> WorkerConfig:
    base: dict[str, Any] = dict(
        task_id=TASK, attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=3, runner_profile="browser", project_id="p", region="us-central1",
        firestore_database="swarm", artifact_bucket="b",
        spec_signing_key=spec_keys.SIGNING_KEY, spec_verify_keys=dict(spec_keys.VERIFY_KEYS),
    )
    base.update(overrides)
    return WorkerConfig(**base)


def _browser_doc() -> dict[str, Any]:
    profile = RUNNER_PROFILES["browser"]
    doc = {
        "id": TASK, "tenant_id": TENANT, "runner_profile": "browser",
        "resource_class": profile.resource_class, "timeout_seconds": profile.timeout_seconds,
        "max_attempts": 3, "provider": profile.provider, "input": {"prompt": "p"},
        "submitted_by": "alice@saga.xyz", "depends_on": [], "metadata": {},
    }
    return spec_keys.sign_document(doc, TASK)


def test_gke_refuses_a_runner_job_name_that_disagrees_within_the_render():
    cfg = _config(runner_job_name="swarm-somethingelse-3")
    with pytest.raises(SpecSignatureInvalid) as exc:
        verify_step_spec(_browser_doc(), create_time=None, cfg=cfg, now=BEFORE_UNTIL)
    assert exc.value.reason == "environment_mismatch"


def test_gke_accepts_the_name_the_dispatcher_rendered():
    cfg = _config(runner_job_name=gke_job_name(TASK, 3))
    check = verify_step_spec(_browser_doc(), create_time=None, cfg=cfg, now=BEFORE_UNTIL)
    assert check.reason == "verified" and check.key_version == spec_keys.KEY_VERSION


def test_gke_cannot_catch_a_dispatcher_that_is_consistently_wrong():
    """THE WEAKER CLAIM, DOCUMENTED. On GKE there is no platform-injected Job
    name: RUNNER_JOB_NAME and the Job's real `metadata.name` come from one
    render. A dispatcher that rendered the pod into a Job named for something
    else, while writing an environment that agrees with itself, passes this
    check -- the worker never sees `metadata.name`. Cloud Run's CLOUD_RUN_JOB
    check does not share this gap. Closing it would need the Downward API."""
    manifest_env = {"TASK_ID": TASK, "GENERATION": "3", "RUNNER_JOB_NAME": gke_job_name(TASK, 3)}
    real_job_metadata_name = "swarm-a-job-for-someone-else-1"  # never read by the worker
    cfg = _config(runner_job_name=manifest_env["RUNNER_JOB_NAME"])
    check = verify_step_spec(_browser_doc(), create_time=None, cfg=cfg, now=BEFORE_UNTIL)
    assert check.reason == "verified"
    assert real_job_metadata_name != cfg.runner_job_name


def test_no_verify_keys_at_all_is_the_workers_configuration_not_a_refusal():
    """A worker rendered with no keys cannot tell a good spec from a bad one;
    that is CANNOT_START (its own configuration), never a tenant's attack."""
    from agent_worker.errors import ConfigError

    cfg = _config(spec_verify_keys={})
    with pytest.raises(ConfigError):
        verify_step_spec(_browser_doc(), create_time=None, cfg=cfg, now=BEFORE_UNTIL)


def test_the_digest_is_the_canonical_forms():
    doc = _browser_doc()
    check = verify_step_spec(doc, create_time=None, cfg=_config(), now=BEFORE_UNTIL)
    expected = specsign.spec_digest(specsign.canonical_step_spec(doc, task_id=TASK)).hex()
    assert check.digest == expected


def test_the_verifier_binds_the_id_it_was_pointed_at_not_the_documents_own():
    doc = _browser_doc()
    doc["id"] = "task_other"  # the document's own `id` is not what is signed
    assert verify_step_spec(doc, create_time=None, cfg=_config(), now=BEFORE_UNTIL).reason == (
        "verified"
    )
    with pytest.raises(SpecSignatureInvalid) as exc:
        verify_step_spec(doc, create_time=None, cfg=_config(task_id="task_other"), now=BEFORE_UNTIL)
    assert exc.value.reason == "signature_mismatch"


def test_the_legacy_end_date_is_in_the_code():
    from agent_worker import specverify

    assert specverify.SPEC_LEGACY_UNTIL == datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert WorkerConfig.__dataclass_fields__["spec_signature_mode"].default == "enforce"
    _ = timedelta  # imported for readers comparing windows

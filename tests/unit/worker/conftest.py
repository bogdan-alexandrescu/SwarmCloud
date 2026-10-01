"""Fixtures that assemble a real worker against in-memory infrastructure.

The worker under test is the production `Worker`, driving the production
`ControlPlane`, the production `CheckpointManager` and the production process
manager. Only three things are substituted, and each is a real implementation
rather than a mock: Firestore becomes an in-memory document store, GCS becomes a
directory, and Cloud Monitoring becomes a list. The runner child is a genuine
subprocess running the genuine mock runner.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_worker.config import WorkerConfig
from agent_worker.control import ControlPlane
from agent_worker.hardening import PROTECTED, MemoryProtection
from agent_worker.lifecycle import Worker, WorkerDeps
from agent_worker.logs import build_logger
from agent_worker.objectstore import LocalObjectStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import TaskState

import spec_keys
from fakes import (
    FakeFirestore,
    FakeSecretClient,
    FakeTransactionRunner,
    RecordingExporter,
    RecordingQuotaReporter,
)

TENANT = "eng"
PROJECT = "saga-agents-staging"
BUCKET = "saga-agents-staging-swarm-artifacts"

#: What the entrypoint hands a worker whose `prctl(PR_SET_DUMPABLE, 0)` worked
#: (`hardening.make_non_dumpable`). A worker holds the tenant git token only
#: with this. The workers here are built in the test process, where the
#: entrypoint never ran, so they are given its success explicitly, the way
#: `reap_before_publish` is given a scoped stand-in. The tests of the refusal
#: set their own, and `test_worker_memory_protection.py` runs the real call in
#: a child process.
ENTRYPOINT_MEMORY = MemoryProtection(PROTECTED, "stand-in for the entrypoint's prctl")


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "gcs", bucket=BUCKET)


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def runner_inputs(monkeypatch) -> list[dict[str, Any]]:
    """`work/input.json` as the runner saw it, read at every checkpoint.

    Tests used to read it back out of the final checkpoint's archive. The
    worker leaves it out of the archive since the PR #229 review (the task's
    whole input was served back out of every checkpoint), so this reads the
    file on disk at the moment each checkpoint is taken -- the same moment,
    and the same bytes, the archive held. Newest last.
    """
    import json

    from agent_worker.checkpoint import CheckpointManager

    seen: list[dict[str, Any]] = []
    original = CheckpointManager.create

    def create(self, ws, *args, **kwargs):
        if ws.input_path.exists():
            seen.append(json.loads(ws.input_path.read_text()))
        return original(self, ws, *args, **kwargs)

    monkeypatch.setattr(CheckpointManager, "create", create)
    return seen


#: What `seed_attempt` stores as a task's input when a test names none: an
#: input its profile's declaration accepts. The worker re-checks a stored
#: input against its profile before it runs one (contract request 32,
#: *Preconditions*), so a claude-code or browser task seeded with the mock's
#: knobs -- which their runners never read -- would be refused before it ran.
_DEFAULT_INPUT: dict[str, dict[str, Any]] = {
    "mock": {"prompt": "hello", "steps": 2, "sleep_seconds": 0.1},
    "generic": {"prompt": "hello", "command": "pytest"},
}

#: What the mock's simulated PROVIDER does, by task id: set by `seed_attempt`'s
#: `simulated`, handed to the runner by `_the_runner_sees_its_simulation`.
_SIMULATED: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _the_runner_sees_its_simulation(monkeypatch):
    """Hand the mock runner what its simulated provider does, beside its input.

    THESE ARE NOT CALLER INPUT, AND ARE NEVER STORED AS ONE. The mock reads
    `spend`, `provider`, `credential_revoked_times`, `credential_detail`,
    `quota_detail` and `reset_at` to act out what a real provider does to a
    real runner: report a spend, revoke a credential, name its rate limit. The
    owner withheld them from callers on #142, because each writes a platform
    record; the API refuses them, and since contract request 32 the worker
    refuses a stored input that carries one before anything runs. So a test
    that needs one does not write it into the task document, which no caller
    can do either. It passes it here, and this puts it into `input.json` after
    the worker has checked the stored input and written the file -- the moment
    in production when the runner, not the platform, meets its provider.

    The seam is `Worker._build_child_env`, which the worker calls right after
    writing `input.json` and again on a credential reload; the merge is the
    same both times. A test that seeds nothing here sees no difference.
    """
    import json

    from agent_worker.lifecycle import Worker

    _SIMULATED.clear()
    original = Worker._build_child_env

    def build_child_env(self, *args: Any, **kwargs: Any):
        simulated = _SIMULATED.get(self.cfg.task_id)
        ws = getattr(self, "ws", None)
        if simulated and ws is not None and ws.input_path.exists():
            payload = json.loads(ws.input_path.read_text())
            payload.update(simulated)
            ws.input_path.write_text(json.dumps(payload, indent=2, default=str))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Worker, "_build_child_env", build_child_env)
    yield
    _SIMULATED.clear()


@pytest.fixture
def recheck_bypassed(monkeypatch) -> None:
    """A stored input that reached the runner step past the worker's re-check.

    For the tests of the worker's SECOND line: `input.model`, `staged_inputs`,
    `expected_outputs` and `attempt_count` are each dropped or overwritten by
    the lifecycle as it writes `input.json`, whatever the stored input holds.
    Since contract request 32 the worker refuses a stored input carrying any
    of them before it gets that far (`lifecycle._recheck_runner_input`, held
    by test_stored_input_is_rechecked.py), so that layer is reached only when
    the first is not there. This removes the first, so each test still proves
    what the second does on its own. `raising=False`: on a tree without the
    re-check there is nothing to remove, and the test asserts the same thing.
    """
    from agent_worker import lifecycle

    monkeypatch.setattr(
        lifecycle, "_recheck_runner_input", lambda *_args, **_kwargs: None, raising=False
    )


def seed_tenant(db: FakeFirestore, *, credentials: list[str] | None = None) -> None:
    db.seed(
        f"tenants/{TENANT}",
        {
            "tenant_id": TENANT,
            "kind": "group",
            "principal": "eng@saga.xyz",
            "created_at": utcnow(),
            "enabled": True,
            "credentials": credentials or [],
            "service_account": f"swarm-tenant-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
            "gcs_prefix": f"tenants/{TENANT}",
            # `swarm-tenant-`, not `swarm-`. This field is not decoration: it is
            # the one `GkeJobDispatcher.namespace_for` PREFERS over its own
            # template, so a tenant document carrying the short spelling
            # overrides the correct one and sends every GKE dispatch into a
            # namespace nothing created -- reported as 403 `jobs.batch is
            # forbidden`, never as a missing namespace. That is exactly what
            # `scripts/register-tenant.sh` was writing until 2026-09-24, and a
            # fixture that agreed with it is a fixture that could never have
            # caught it. `scripts/lib/check-contract-parity.sh` section 6 now
            # holds every namespace literal in the repository to one spelling.
            "namespace": f"swarm-tenant-{TENANT}",
        },
    )


def seed_attempt(
    db: FakeFirestore,
    *,
    task_id: str = "task_1",
    attempt_id: str = "att_1",
    lease_id: str = "lease_1",
    generation: int = 1,
    task_generation: int | None = None,
    state: TaskState = TaskState.LEASED,
    runner_profile: str = "mock",
    task_input: dict[str, Any] | None = None,
    cancel_requested: bool = False,
    lease_released: bool = False,
    latest_checkpoint: str | None = None,
    pool_active: int = 1,
    attempt_count: int = 1,
    simulated: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Create the documents a dispatched attempt would find in Firestore.

    `simulated` is what the mock's simulated provider does (see
    `_the_runner_sees_its_simulation`): handed to the runner, never stored in
    the task's input.
    """
    if simulated:
        _SIMULATED[task_id] = dict(simulated)
    profile = RUNNER_PROFILES[runner_profile]
    backend = resolve_backend(profile).value
    units = RESOURCE_CLASSES[profile.resource_class].units
    pools = pool_names_for(
        tenant_id=TENANT,
        provider=profile.provider,
        resource_class=profile.resource_class,
        runner_profile=runner_profile,
        backend=backend,
    )
    now = utcnow()
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id,
            "tenant_id": TENANT,
            "state": state.value,
            "runner_profile": runner_profile,
            "resource_class": profile.resource_class,
            "provider": profile.provider,
            # By profile, and one its declaration accepts: see `_DEFAULT_INPUT`.
            "input": task_input or dict(_DEFAULT_INPUT.get(runner_profile, {"prompt": "hello"})),
            "submitted_by": "alice@saga.xyz",
            "created_at": now,
            "updated_at": now,
            # What admission writes in the lease's own transaction: one more
            # for every lease, a park's next attempt included.
            "attempt_count": attempt_count,
            "max_attempts": 3,
            "current_lease_id": lease_id,
            "current_generation": task_generation if task_generation is not None else generation,
            "cancel_requested": cancel_requested,
            "latest_checkpoint": latest_checkpoint,
            "timeout_seconds": profile.timeout_seconds,
        },
    )
    db.seed(
        f"leases/{lease_id}",
        {
            "lease_id": lease_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "tenant_id": TENANT,
            "generation": generation,
            "pools": pools,
            "units": units,
            "state": state.value,
            "created_at": now,
            "dispatch_deadline": now + timedelta(seconds=300),
            "expires_at": now + timedelta(seconds=120),
            "heartbeat_at": None,
            "released_at": now if lease_released else None,
        },
    )
    for name in pools:
        db.seed(
            name if name.startswith("pools/") else f"pools/{name}",
            {"name": name, "hard_limit": 10, "active": pool_active, "enabled": True,
             "updated_at": now},
        )
    seed_tenant(db)
    return {"task_id": task_id, "attempt_id": attempt_id, "lease_id": lease_id}


def record_as_earlier_attempt(
    db: FakeFirestore, record: Any, *, task_id: str | None = None, attempt_count: int = 2
) -> None:
    """What a real earlier attempt leaves behind for `record`, a checkpoint it wrote.

    A test that builds a checkpoint with `CheckpointManager.create` directly
    skips `ControlPlane.record_checkpoint`, and since #347 a worker restores
    only a checkpoint an earlier attempt of the task RECORDED: its attempt
    document listing the id and the archive digest, the task's pointer, and an
    `attempt_count` past the first. Call it after `seed_attempt`, which
    rewrites the task document.
    """
    from agent_worker.control import CHECKPOINT_DIGESTS_FIELD

    db.seed(
        f"attempts/{record.attempt_id}",
        {
            "attempt_id": record.attempt_id,
            "task_id": record.task_id,
            "tenant_id": record.tenant_id,
            "generation": record.generation,
            "checkpoints": [record.checkpoint_id],
            CHECKPOINT_DIGESTS_FIELD: {record.checkpoint_id: record.archive_sha256},
        },
    )
    task = db.documents.get(f"tasks/{task_id or record.task_id}")
    if task is not None:
        task["latest_checkpoint"] = record.uri
        task["attempt_count"] = max(int(task.get("attempt_count") or 0), attempt_count)


def build_worker(
    db: FakeFirestore,
    store: LocalObjectStore,
    tmp_path: Path,
    log_stream: io.StringIO,
    *,
    task_id: str = "task_1",
    attempt_id: str = "att_1",
    lease_id: str = "lease_1",
    generation: int = 1,
    runner_profile: str = "mock",
    timeout_seconds: int = 60,
    checkpoint_interval_seconds: int = 2,
    heartbeat_interval_seconds: int = 1,
    control_poll_seconds: int = 1,
    termination_grace_seconds: int = 2,
    max_in_worker_retry_delay_seconds: int = 45,
    secret_client: Any | None = None,
    txn_runner: Any | None = None,
    reap_before_publish: Any | None = None,
    sign_spec: bool = True,
    quota_reporter: Any | None = None,
    **overrides: Any,
) -> tuple[Worker, WorkerConfig, RecordingExporter]:
    profile = RUNNER_PROFILES[runner_profile]
    # The key the worker trusts, as terraform renders it onto every Job
    # (contract request 34). A test that is about the keys sets its own.
    overrides.setdefault("spec_signing_key", spec_keys.SIGNING_KEY)
    overrides.setdefault("spec_verify_keys", dict(spec_keys.VERIFY_KEYS))
    config = WorkerConfig(
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=TENANT,
        generation=generation,
        runner_profile=runner_profile,
        project_id=PROJECT,
        region="us-central1",
        firestore_database="swarm",
        artifact_bucket=BUCKET,
        workspace_root=tmp_path / "workspace",
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
        max_in_worker_retry_delay_seconds=max_in_worker_retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        control_poll_seconds=control_poll_seconds,
        provider=profile.provider,
        **overrides,
    )
    logger = build_logger(
        task_id=task_id,
        attempt_id=attempt_id,
        tenant_id=TENANT,
        generation=generation,
        runner_profile=runner_profile,
        stream=log_stream,
    )
    control = ControlPlane(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=TENANT,
        generation=generation,
        logger=logger,
        # Replaceable so a test can model what the default cannot: a
        # transaction whose commit is refused because a document it read was
        # changed underneath it (tests/unit/worker/test_fenced_sigterm.py).
        txn_runner=txn_runner or FakeTransactionRunner(db),
        # Never the environment's broker: a test's provider outcomes are
        # recorded, and read back through `control.quota_reporter`.
        quota_reporter=quota_reporter or RecordingQuotaReporter(),
    )
    exporter = RecordingExporter()
    deps = WorkerDeps(
        control=control,
        store=store,
        logger=logger,
        db=db,
        metrics_exporter=exporter,
        secret_client=secret_client or FakeSecretClient(),
        memory=ENTRYPOINT_MEMORY,
    )
    worker = Worker(config, deps)
    # The production reaper runs `os.kill(-1, SIGKILL)`, which would take this
    # test process (and its whole session) down. Every worker built here gets a
    # scoped stand-in by default: `() ` means "nothing the agent started is
    # alive", which is the clean case the publish tests assume. The tests that
    # exercise the reap itself (test_forge_token_isolation.py) set their own.
    worker.reap_before_publish = reap_before_publish or (lambda: ())
    if sign_spec:
        _sign_before_run(worker, db, config)
    return worker, config, exporter


def _sign_before_run(worker: Worker, db: FakeFirestore, config: WorkerConfig) -> None:
    """Sign the task document the way swarm-api would have, just before the run.

    Every task a worker runs was signed at submission (contract request 34),
    and the worker refuses one that was not. Most tests here seed a task and
    then shape it -- its metadata, its input, its class -- before they run it,
    so signing at seed time would sign a spec the test then rewrites. Signing
    at `run()` signs the spec the test actually built. A document that already
    carries a signature is left alone: that is how the spec-signature tests
    sign first and rewrite afterwards. `sign_spec=False` leaves it unsigned.

    A config that names a repository gets the same on the document, as
    submission would have written it: the worker refuses an environment whose
    REPOSITORY_URL disagrees with the signed spec.
    """
    original_run = worker.run

    def run() -> int:
        doc = db.documents.get(f"tasks/{config.task_id}")
        if doc is not None and not doc.get("spec_signature"):
            if config.repository_url and not doc.get("repository_url"):
                doc["repository_url"] = config.repository_url
            if config.repository_ref and not doc.get("repository_ref"):
                doc["repository_ref"] = config.repository_ref
            spec_keys.sign_document(doc, config.task_id)
        return original_run()

    worker.run = run  # type: ignore[method-assign]


@pytest.fixture
def worker_factory(db, store, tmp_path, log_stream):
    def _factory(**kwargs: Any):
        return build_worker(db, store, tmp_path, log_stream, **kwargs)

    return _factory

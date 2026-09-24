"""Checkpoint retention, decided by REFERENCE rather than by clock.

A checkpoint is safe to delete when nothing can still resume from it. That is a
question about references, not about age, and the difference is not academic: a
task PARKED on a provider quota window is durable, costs nothing, and may sit
for as long as the provider takes to give the tenant its tokens back. Its
checkpoint is the entire value of the attempt that produced it. Any clock short
enough to control storage is short enough to delete that.

Until this module existed, the ONLY thing that removed a checkpoint was the
bucket-wide GCS lifecycle rule in `terraform/modules/storage/main.tf`
(`age = artifact_retention_days`, 14 days in `dev.tfvars`). It cannot see a
task, a lease or an attempt, so it deleted a parked attempt's only checkpoint on
exactly the same schedule as a succeeded task's leftovers. The reconciler's
`roles/storage.objectAdmin` binding on the artifact bucket already existed, with
a comment saying it was for cleaning up abandoned artifacts -- a seam built at
both ends with nothing in the middle.

WHAT CAN RESUME FROM A CHECKPOINT
---------------------------------
Two things, and both are visible in the control plane:

* `task.latest_checkpoint`, the pointer `agent_worker.lifecycle` reads first
  (`_restore_checkpoint`); and
* `CheckpointManager.find_latest()`, the fallback, which scans EVERY manifest
  under `tenants/<t>/tasks/<task>/attempts/` and takes the newest.

The second is why a single live attempt protects every checkpoint of its task,
not just the one it wrote: a resumed worker whose pointer fails to resolve will
happily select an older one. So the reference question is asked per TASK, and
the answers are exactly the three the lane asked for -- the task is terminal,
the owning attempt is complete, and nothing later still points at it.

THE BACKSTOP IS THE FALLBACK, NOT THE RULE
------------------------------------------
A reference that can leak needs a floor, and this one leaks three ways:

* the task document is gone -- `scripts/purge-data.sh` removes Firestore
  documents and bucket objects in separate steps, and either can fail alone;
* the attempt document is gone, so "the attempt finished" cannot be proven;
* the attempt document exists but never recorded `completed_at` on a task that
  is already terminal. `ControlPlane.finish` writes the task's terminal state
  BEFORE calling `record_attempt_end`, so a worker killed between those two
  writes leaves that shape permanently. A read-only dry run of this collector
  against saga-agents-staging on 2026-09-22 found 19 such checkpoints across 6
  tasks -- checkpoints an unfloored reference rule would have kept for ever.

Age decides those three, and age decides NOTHING else. Every KEEP in `classify`
is permanent until the reference itself goes away.

Deletion order is the reverse of creation. `CheckpointManager.create` uploads
the archive and commits with the manifest, so a half-written checkpoint is an
undiscoverable orphan rather than a manifest pointing at a truncated archive.
Removing the manifest FIRST preserves that property through a half-completed
delete: the worst case is an orphan archive, never a resume that downloads a
file that is no longer there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Protocol, runtime_checkable

from swarm_common.states import TaskState, can_transition, is_terminal

from .config import ReconcilerConfig
from .detect import FindingKind
from .model import AttemptView, TaskView

#: Written last by `agent_worker.checkpoint.CheckpointManager.create`, and
#: therefore the commit marker: a checkpoint prefix without one was never
#: finished and no restore will ever select it.
MANIFEST_NAME = "manifest.json"

#: The path segment that separates a checkpoint from the artifacts and logs of
#: the same attempt. Kept as a constant because the layout it belongs to is
#: `agent_worker.checkpoint.checkpoint_prefix`'s, not this module's;
#: `tests/unit/worker/test_checkpoint_retention.py` asserts the two still agree.
CHECKPOINTS_SEGMENT = "checkpoints"

#: Every object this platform writes lives under this root.
TENANTS_ROOT = "tenants/"


@runtime_checkable
class CheckpointStore(Protocol):
    """The three operations retention needs.

    Deliberately not the worker's `ObjectStore`: this is the only code in the
    platform that DELETES a checkpoint, and importing the worker's store into
    the reconciler image to get three methods would drag its whole
    upload/restore surface along with it. `LocalObjectStore` satisfies this
    structurally, so the tests drive the real collector against a real store.
    """

    def list_keys(self, prefix: str) -> list[str]: ...
    def download_bytes(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class GcsCheckpointStore:
    """Google Cloud Storage. The client is imported lazily so the unit tests
    never pull in grpc or authenticate to anything."""

    def __init__(self, bucket: str, project_id: str | None = None, client: Any = None) -> None:
        if not bucket:
            raise ValueError("a bucket is required to collect checkpoints")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # lazy: keeps unit tests grpc-free

            self._client = storage.Client(project=self._project_id)
        return self._client

    def list_keys(self, prefix: str) -> list[str]:
        client = self._get_client()
        return sorted(b.name for b in client.list_blobs(self.bucket, prefix=prefix))

    def download_bytes(self, key: str) -> bytes:
        return self._get_client().bucket(self.bucket).blob(key).download_as_bytes()

    def delete(self, key: str) -> None:
        blob = self._get_client().bucket(self.bucket).blob(key)
        if blob.exists():
            blob.delete()


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckpointRef:
    """One checkpoint, identified entirely by where it sits in the bucket."""

    tenant_id: str
    task_id: str
    attempt_id: str
    checkpoint_id: str
    #: `tenants/<t>/tasks/<task>/attempts/<attempt>/checkpoints/<id>`, no slash.
    prefix: str

    @property
    def manifest_key(self) -> str:
        return f"{self.prefix}/{MANIFEST_NAME}"


def parse_checkpoint_key(key: str) -> CheckpointRef | None:
    """Recover the referent of a checkpoint object from its key.

    Returns None for anything that is not inside a checkpoint directory --
    artifacts, logs, and any object whose layout this function does not
    recognise. None means "not mine to judge", and the collector leaves such
    objects entirely alone; a parser that guessed would be a parser that deletes
    a tenant's artifacts the day the layout gains a segment.
    """
    parts = key.split("/")
    # tenants/<t>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/<object...>
    if len(parts) < 9:
        return None
    if (
        parts[0] != "tenants"
        or parts[2] != "tasks"
        or parts[4] != "attempts"
        or parts[6] != CHECKPOINTS_SEGMENT
    ):
        return None
    if any(not part for part in parts[1:8]):
        return None
    return CheckpointRef(
        tenant_id=parts[1],
        task_id=parts[3],
        attempt_id=parts[5],
        checkpoint_id=parts[7],
        prefix="/".join(parts[:8]),
    )


def pointer_to_prefix(pointer: Any) -> str | None:
    """Normalise `task.latest_checkpoint` to a bucket-relative checkpoint prefix.

    The pointer is written as a store URI (`gs://bucket/tenants/...` in
    production, `file:///tmp/.../tenants/...` under `LOCAL_ARTIFACT_ROOT`), so
    slicing from `tenants/` turns any flavour into the key without this module
    needing to know which store wrote it -- the same trick
    `CheckpointManager.find_by_uri` uses.

    The caller compares the result as a WHOLE prefix, never as a substring:
    `ckpt-00001` and `ckpt-000010` share a string prefix, and a pointer test
    that matched on that would protect the wrong object and collect the right
    one.
    """
    if not isinstance(pointer, str) or not pointer:
        return None
    marker = pointer.find(TENANTS_ROOT)
    if marker < 0:
        return None
    candidate = pointer[marker:].rstrip("/")
    if candidate.endswith(f"/{MANIFEST_NAME}"):
        candidate = candidate[: -len(MANIFEST_NAME) - 1]
    return candidate or None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

class Disposition(str, Enum):
    #: Something can still resume from it. Age is not consulted, ever.
    KEEP = "keep"
    #: Provably unreferenced: the task is terminal and unrevivable, the owning
    #: attempt finished, and nothing points at it.
    RECLAIM = "reclaim"
    #: Neither could be established. The clock decides this one, and only this
    #: one -- see `CheckpointCollector._backstop_hold`.
    BACKSTOP = "backstop"


@dataclass(frozen=True)
class Decision:
    disposition: Disposition
    reason: str


def classify(
    ref: CheckpointRef,
    *,
    task: TaskView | None,
    attempts: Iterable[AttemptView],
) -> Decision:
    """Pure. Given the control plane's view, may anything still resume from this?

    The conditions are a conjunction; their order affects only the quality of
    the logged reason. No I/O and no clock -- the caller applies the age
    backstop to an ORPHAN and to nothing else, which is what keeps the backstop
    a fallback rather than the rule.
    """
    if task is None:
        # The referent is gone. Either the task was purged and its objects were
        # not, or a control-plane read is lying to us; the caller's age window
        # is what tells those apart.
        return Decision(Disposition.BACKSTOP, "no task document references this checkpoint")

    if task.tenant_id and task.tenant_id != ref.tenant_id:
        # The object's own path says one tenant and the task document says
        # another. One of the two is wrong, and this module is not the place to
        # find out which: deleting on a tenancy mismatch is how one tenant's
        # cleanup takes another tenant's state with it.
        return Decision(
            Disposition.KEEP,
            f"tenant mismatch: object is under {ref.tenant_id}, task belongs to {task.tenant_id}",
        )

    if not is_terminal(task.state):
        # Includes PARKED -- the case this module exists for -- and LEASED,
        # DISPATCHED, STARTING and RUNNING, where a worker may be alive now.
        return Decision(Disposition.KEEP, f"task is {task.state.value}, not terminal")

    attempts = list(attempts)
    owning = next((a for a in attempts if a.attempt_id == ref.attempt_id), None)
    if owning is None:
        # We cannot prove the attempt finished, so we do not get to act on the
        # claim that it did. A missing attempt document is the same class of
        # leak as a missing task document, and gets the same age window.
        return Decision(
            Disposition.BACKSTOP,
            f"attempt {ref.attempt_id} has no document; cannot prove it finished",
        )

    live = [a for a in attempts if a.completed_at is None]
    if live:
        # `find_latest()` scans the whole task prefix, so ANY unfinished attempt
        # of this task can select ANY checkpoint of it -- including one written
        # by an earlier attempt that has already completed.
        #
        # But the TASK is terminal here, so no scheduler will admit it and no
        # worker will be started for it. The only thing this can be is an
        # attempt whose completion was never written: `ControlPlane.finish`
        # transitions the task BEFORE calling `record_attempt_end`, so a worker
        # killed between those two writes leaves exactly this shape, for ever.
        # A dry run against saga-agents-staging on 2026-09-22 found 19 such
        # checkpoints across 6 tasks.
        #
        # So the reference is honoured, but it is given a floor. No worker winds
        # down for the length of the backstop window; anything still holding
        # after that is the leak, not the reference.
        return Decision(
            Disposition.BACKSTOP,
            f"attempt {live[0].attempt_id} never recorded completion on a "
            f"{task.state.value} task; honoured until the backstop",
        )

    # `is_terminal` and "can never be admitted again" are not the same thing:
    # the frozen state machine allows FAILED -> READY, which is a retry that
    # resumes from exactly this pointer. Asking `can_transition` rather than
    # hard-coding the set means this rule cannot drift from the contract.
    if can_transition(task.state, TaskState.READY):
        if pointer_to_prefix(task.latest_checkpoint) == ref.prefix:
            return Decision(
                Disposition.KEEP,
                f"task.latest_checkpoint names it and {task.state.value} may still return to READY",
            )

    return Decision(
        Disposition.RECLAIM,
        f"task {task.state.value}, attempt {ref.attempt_id} complete, nothing references it",
    )


def manifest_age_seconds(raw: bytes, *, now: datetime) -> float | None:
    """Seconds since the manifest said the checkpoint was written.

    Returns None -- never 0, never infinity -- when the bytes cannot be read as
    a manifest. The caller keeps the object in that case: a guard whose failure
    is indistinguishable from "old enough to delete" is the exact defect this
    repository keeps producing.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
        stamp = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
    except Exception:
        return None
    if stamp.tzinfo is None:
        # A manifest without an offset was written by `datetime.now(timezone.utc)`
        # with the suffix lost somewhere; UTC is the only reading that is not a
        # guess, and guessing local time here would age objects by hours.
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

@dataclass
class CheckpointOutcome:
    """One checkpoint this sweep acted on, or declined to act on."""

    kind: str
    reason: str
    prefix: str
    tenant_id: str | None = None
    task_id: str | None = None
    deleted: bool = False
    objects_deleted: int = 0
    skipped: str | None = None


@dataclass
class SweepReport:
    ran: bool = False
    keys_listed: int = 0
    checkpoints_examined: int = 0
    kept: int = 0
    #: Decided collectable. Equal to `reclaimed` on a real pass and to the
    #: number of objects a dry run would have taken. Counted separately because
    #: a dry run reporting `reclaimed: 0, kept: 142` beside a list of 123
    #: reclaimable outcomes is a report that contradicts itself -- caught by
    #: running this collector against saga-agents-staging before shipping it.
    reclaimable: int = 0
    reclaimed: int = 0
    #: True when `checkpoint_scan_limit` cut the listing short. Never silent:
    #: a sweep that examined a tenth of the bucket and a sweep that examined all
    #: of it look identical in every other field.
    truncated: bool = False
    outcomes: list[CheckpointOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "keys_listed": self.keys_listed,
            "checkpoints_examined": self.checkpoints_examined,
            "kept": self.kept,
            "reclaimable": self.reclaimable,
            "reclaimed": self.reclaimed,
            "truncated": self.truncated,
        }


class TaskReader(Protocol):
    def task_and_attempts(self, task_id: str) -> tuple[TaskView | None, list[AttemptView]]: ...


class CheckpointCollector:
    """Lists checkpoints, asks the control plane who still needs them, deletes
    only those nobody does."""

    def __init__(
        self,
        *,
        reader: TaskReader,
        objects: CheckpointStore,
        config: ReconcilerConfig,
        logger: Any,
    ) -> None:
        self._reader = reader
        self._objects = objects
        self._config = config
        self._log = logger

    def sweep(self, *, now: datetime) -> SweepReport:
        report = SweepReport(ran=True)
        try:
            keys = self._objects.list_keys(TENANTS_ROOT)
        except Exception as exc:
            # A bucket we cannot list is a bucket we must not act on -- the same
            # rule the reconciliation pass applies to an unreadable backend.
            report.ran = False
            report.errors.append(f"listing {TENANTS_ROOT}: {exc}")
            self._log.error("checkpoint sweep skipped: the bucket could not be listed",
                            error=str(exc))
            return report

        keys = sorted(keys)
        report.keys_listed = len(keys)
        if len(keys) > self._config.checkpoint_scan_limit:
            keys = keys[: self._config.checkpoint_scan_limit]
            report.truncated = True

        by_prefix: dict[str, list[str]] = {}
        refs: dict[str, CheckpointRef] = {}
        for key in keys:
            ref = parse_checkpoint_key(key)
            if ref is None:
                continue          # an artifact, a log, or a layout we do not own
            by_prefix.setdefault(ref.prefix, []).append(key)
            refs[ref.prefix] = ref

        if report.truncated and by_prefix:
            # The listing is sorted, so a checkpoint's objects are contiguous and
            # only the LAST group can have been cut in half. Dropping it stops
            # this sweep from deleting a manifest whose archive it never saw --
            # harmless in itself, but it would report a checkpoint as fully
            # collected when part of it is still there. The next sweep gets it.
            last = max(by_prefix)
            by_prefix.pop(last, None)
            refs.pop(last, None)

        by_task: dict[tuple[str, str], list[str]] = {}
        for prefix, ref in refs.items():
            by_task.setdefault((ref.tenant_id, ref.task_id), []).append(prefix)

        # Grouped by (tenant, task) but read by task id alone: the tenant is in
        # the key only to keep two tasks that somehow shared an id apart, and
        # `classify` is what compares it against the document.
        for (_tenant, task_id), prefixes in sorted(by_task.items()):
            try:
                task, attempts = self._reader.task_and_attempts(task_id)
            except Exception as exc:
                # "I could not read the task" must never look like "the task is
                # gone": one keeps the checkpoints, the other deletes them.
                report.errors.append(f"task {task_id}: {exc}")
                self._log.error(
                    "not collecting: the task document could not be read",
                    task_id=task_id,
                    error=str(exc),
                )
                continue
            for prefix in sorted(prefixes):
                report.checkpoints_examined += 1
                outcome = self._act(
                    refs[prefix], by_prefix[prefix], task=task, attempts=attempts, now=now
                )
                if outcome is None:
                    report.kept += 1
                    continue
                report.outcomes.append(outcome)
                if outcome.skipped is not None and outcome.skipped != "dry_run":
                    report.kept += 1      # held by the backstop, not collectable yet
                    continue
                report.reclaimable += 1
                if outcome.deleted:
                    report.reclaimed += 1

        self._log.info("checkpoint sweep complete", **report.as_dict())
        return report

    def _act(
        self,
        ref: CheckpointRef,
        keys: list[str],
        *,
        task: TaskView | None,
        attempts: list[AttemptView],
        now: datetime,
    ) -> CheckpointOutcome | None:
        """Returns None for an ordinary keep -- the common case, and not worth a
        row in a report an operator has to read."""
        decision = classify(ref, task=task, attempts=attempts)

        if decision.disposition is Disposition.KEEP:
            return None

        kind = FindingKind.RECLAIMABLE_CHECKPOINT.value
        if decision.disposition is Disposition.BACKSTOP:
            kind = FindingKind.ORPHAN_CHECKPOINT.value
            held = self._backstop_hold(ref, keys, now=now)
            if held is not None:
                return CheckpointOutcome(
                    kind=kind,
                    reason=decision.reason,
                    prefix=ref.prefix,
                    tenant_id=ref.tenant_id,
                    task_id=ref.task_id,
                    skipped=held,
                )

        outcome = CheckpointOutcome(
            kind=kind,
            reason=decision.reason,
            prefix=ref.prefix,
            tenant_id=ref.tenant_id,
            task_id=ref.task_id,
        )
        if self._config.dry_run:
            outcome.skipped = "dry_run"
            return outcome

        outcome.objects_deleted = self._delete(ref, keys)
        outcome.deleted = True
        self._log.info(
            "checkpoint reclaimed",
            prefix=ref.prefix,
            tenant_id=ref.tenant_id,
            task_id=ref.task_id,
            attempt_id=ref.attempt_id,
            reason=decision.reason,
            objects=outcome.objects_deleted,
        )
        return outcome

    def _backstop_hold(self, ref: CheckpointRef, keys: list[str], *, now: datetime) -> str | None:
        """None when the backstop window has passed; otherwise why it has not.

        THE ONLY PLACE A CLOCK IS CONSULTED. Every failure here holds the
        object: a stale checkpoint costs storage, while one deleted because
        its manifest happened to be unreadable costs an attempt.
        """
        if ref.manifest_key not in keys:
            # No commit marker, so this was never a finished checkpoint and no
            # restore can select it -- but it also carries no timestamp this
            # collector can read, and it may be an upload that is in flight
            # right now. The bucket's own lifecycle rule is what removes these.
            return "no manifest; not datable by this collector"
        try:
            raw = self._objects.download_bytes(ref.manifest_key)
        except Exception as exc:
            self._log.warning(
                "keeping an unreferenced checkpoint whose manifest could not be read",
                prefix=ref.prefix,
                error=str(exc),
            )
            return f"manifest unreadable: {exc}"
        age = manifest_age_seconds(raw, now=now)
        if age is None:
            self._log.warning(
                "keeping an unreferenced checkpoint with no usable created_at",
                prefix=ref.prefix,
            )
            return "manifest has no usable created_at"
        window = self._config.checkpoint_orphan_backstop_seconds
        if age < window:
            return f"unreferenced, but only {int(age)}s old; backstop is {window}s"
        return None

    def _delete(self, ref: CheckpointRef, keys: list[str]) -> int:
        """Manifest first. See the module docstring: the manifest is the commit
        marker, so removing it first means a half-completed delete leaves an
        undiscoverable archive rather than a pointer to a missing one."""
        ordered = [k for k in (ref.manifest_key,) if k in keys]
        ordered += sorted(k for k in keys if k != ref.manifest_key)
        deleted = 0
        for key in ordered:
            self._objects.delete(key)
            deleted += 1
        return deleted

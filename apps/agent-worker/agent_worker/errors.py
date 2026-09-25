"""Worker error taxonomy.

Exit codes are part of the contract with the dispatcher and the reconciler: a
worker that exits 0 has persisted a terminal state and released its lease, and a
worker that exits with one of the codes below has done so deliberately.
"""

from __future__ import annotations


class ExitCode:
    OK = 0
    FAILED = 1
    #: A dependency was UNAVAILABLE before the runner existed (a spent startup
    #: budget, UNAVAILABLE, any 5xx or gRPC UNKNOWN; at the generation check,
    #: any API error that is not a named refusal). The task and the lease were
    #: not written, and the reconciler RETRIES it like any lost attempt.
    #: EX_UNAVAILABLE in sysexits. Split from CONFIG on 2026-09-25, when 78
    #: became non-retryable: an outage the next attempt may not meet must not
    #: end the task. See `__main__` for every exit and what the reconciler does
    #: with it.
    UNAVAILABLE = 69
    #: Fencing generation was stale. The worker wrote neither the task nor the
    #: lease. The agent did not run if the fence was found at startup. If it
    #: was found later (mid-run, on SIGTERM, or at the terminal write), the
    #: agent was stopped and its result was not written.
    GENERATION_FENCED = 70
    #: Task was cancelled while running.
    CANCELLED = 71
    #: Parked on a long provider wait. Checkpointed, lease released.
    PARKED = 75
    #: Child process exceeded its timeout and was killed.
    TIMEOUT = 76
    #: The worker CANNOT START, and another attempt would fail the same way:
    #: bad configuration, DNS that stayed unreachable through the preflight's
    #: retries, clients that could not be built, a generation check Firestore
    #: refused. NON-RETRYABLE (owner, 2026-09-25): the reconciler reads it
    #: from the finished execution and fails the task with the worker's cause.
    #: `reconciler.detect.WORKER_EXIT_CANNOT_START` restates this number, and
    #: tests/unit/worker/test_worker_cannot_start.py holds the two together.
    CONFIG = 78
    #: A control-plane document the worker was pointed at belongs to a DIFFERENT
    #: tenant. The worker exits immediately, writing nothing at all.
    TENANT_MISMATCH = 79


class WorkerError(Exception):
    """Base class for deliberate worker failures."""

    exit_code = ExitCode.FAILED


class ConfigError(WorkerError):
    exit_code = ExitCode.CONFIG


class FencedError(WorkerError):
    """The attempt's generation is stale; another generation owns this task."""

    exit_code = ExitCode.GENERATION_FENCED

    def __init__(self, expected: int, actual: int, detail: str = "") -> None:
        super().__init__(
            f"fencing generation {expected} is stale (task is at {actual})"
            + (f": {detail}" if detail else "")
        )
        self.expected = expected
        self.actual = actual


class FencedWriteRefused(FencedError):
    """A write that would end or record this attempt found it fenced, and was not made.

    Raised by `ControlPlane` from inside the transaction that would have made
    the write, so nothing is committed: no task state, no checkpoint pointer,
    no event, no lease release. It is distinct from a plain FencedError
    because the two need different exits. A plain FencedError comes from the
    startup gate, before the attempt has done anything. This one comes from a
    worker already on its way out, and the only document it may still write
    is its own attempt (invariant 5).

    `write` names what was refused, for the log line an operator reads.
    """

    def __init__(self, expected: int, actual: int, detail: str = "", *, write: str = "") -> None:
        super().__init__(expected, actual, detail)
        self.write = write


class CancelledError(WorkerError):
    exit_code = ExitCode.CANCELLED


class ChildTimeout(WorkerError):
    exit_code = ExitCode.TIMEOUT


class CheckpointError(WorkerError):
    """Checkpointing failed. Mandatory means mandatory: this is not swallowed."""


class ControlPlaneError(WorkerError):
    """The control plane is in a shape the worker cannot act on.

    Distinct from FencedError: fencing is an expected, safe outcome, while this
    means a document is missing or malformed in a way that needs a human.
    """


class TenantMismatchError(ControlPlaneError):
    """A control-plane document names a tenant that is not this worker's.

    Invariant 9 in code rather than only in IAM. Firestore has no document-level
    authorization, so the tenant field on every document the worker touches is
    compared against the tenant this attempt was admitted for. A mismatch means
    either a corrupted control plane or another tenant writing into this task's
    documents, and in both cases the only safe response is to touch nothing:
    no state transition, no lease release, no event, because every one of those
    writes would land on the OTHER tenant's data.
    """

    exit_code = ExitCode.TENANT_MISMATCH

    def __init__(self, *, kind: str, document_id: str, expected: str, actual: str | None) -> None:
        super().__init__(
            f"{kind} {document_id} belongs to tenant {actual!r}, not {expected!r}; "
            "refusing to read or write another tenant's control-plane data"
        )
        self.kind = kind
        self.document_id = document_id
        self.expected = expected
        self.actual = actual


class QuotaExhausted(WorkerError):
    """The provider is unavailable for longer than the worker may wait.

    Carries the wait so the lifecycle can compute `next_eligible_at` without
    re-deriving it from the provider's response a second time.
    """

    exit_code = ExitCode.PARKED

    def __init__(self, provider: str, wait_seconds: int, detail: str = "") -> None:
        super().__init__(
            f"provider {provider} unavailable for {wait_seconds}s"
            + (f": {detail}" if detail else "")
        )
        self.provider = provider
        self.wait_seconds = wait_seconds
        self.detail = detail


class InputUnavailable(WorkerError):
    """A declared `input_from` artifact could not be staged into the workspace.

    Fails the attempt on purpose, and the agent is never started. A step that
    declares an input has been PROMISED that file: its prompt is written as
    though the file is there, so running without it produces a confident, wrong
    answer that nothing downstream can distinguish from a right one. Every
    message raised with this names the upstream task and the filename, because
    those two strings are what a person needs in order to fix it.
    """


class ArtifactTooLarge(WorkerError):
    """An artifact or checkpoint exceeded its configured cap and was refused."""


class WorkspaceError(WorkerError):
    """The isolated workspace could not be created or is not clean."""

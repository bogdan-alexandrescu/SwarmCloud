"""Worker error taxonomy.

Exit codes are part of the contract with the dispatcher and the reconciler: a
worker that exits 0 has persisted a terminal state and released its lease, and a
worker that exits with one of the codes below has done so deliberately.
"""

from __future__ import annotations


class ExitCode:
    OK = 0
    FAILED = 1
    #: Fencing generation was stale. The agent was NOT run.
    GENERATION_FENCED = 70
    #: Task was cancelled while running.
    CANCELLED = 71
    #: Parked on a long provider wait. Checkpointed, lease released.
    PARKED = 75
    #: Child process exceeded its timeout and was killed.
    TIMEOUT = 76
    #: Worker could not even start (bad config, missing lease).
    CONFIG = 78


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


class ArtifactTooLarge(WorkerError):
    """An artifact or checkpoint exceeded its configured cap and was refused."""


class WorkspaceError(WorkerError):
    """The isolated workspace could not be created or is not clean."""

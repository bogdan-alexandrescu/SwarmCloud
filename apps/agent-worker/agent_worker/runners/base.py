"""The contract between the worker and a runner child process.

A runner is a separate process, started by the worker as
`python -m agent_worker.runners.<name>` -- the exact command recorded in the
frozen `RUNNER_PROFILES` catalogue, never anything assembled from a caller's
input. It learns everything it needs from its environment:

    SWARM_WORK_DIR        current directory; the tree that gets checkpointed
    SWARM_ARTIFACTS_DIR   files written here are uploaded when the attempt ends
    SWARM_INPUT           input.json, the task's `input` dict verbatim
    SWARM_RESULT          where to write result.json
    SWARM_QUOTA_SIGNAL    where to write quota.json on a provider rate limit

and it communicates back through three channels, in descending order of
authority:

    exit code     0 success, EXIT_QUOTA_EXHAUSTED (77) rate-limited, else failure
    result.json   the structured outcome that becomes `task.result_summary`
    quota.json    retry-after / reset-at, which decides park versus short retry

Writing result.json is not optional: a runner that exits 0 without one is
reported as a failure, because "succeeded with no result" is indistinguishable
from "was killed before it could write anything".
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EXIT_OK = 0
EXIT_FAILED = 1
#: Reserved: the provider rate-limited us. The worker reads quota.json for the
#: wait, then decides between a short in-worker retry and parking the task.
EXIT_QUOTA_EXHAUSTED = 77
#: The runner was asked to stop (SIGTERM) and stopped cleanly.
EXIT_TERMINATED = 143


class RunnerFailure(RuntimeError):
    """Deterministic failure with a message that belongs in `task.last_error`."""


class QuotaExhaustedSignal(RuntimeError):
    def __init__(
        self,
        provider: str,
        retry_after_seconds: int | None = None,
        reset_at: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(f"{provider} rate limited: {detail or 'no detail'}")
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class RunnerContext:
    work_dir: Path
    artifacts_dir: Path
    input_path: Path
    result_path: Path
    quota_path: Path
    payload: dict[str, Any] = field(default_factory=dict)
    stop_requested: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RunnerContext":
        env = dict(env or os.environ)
        work = Path(env.get("SWARM_WORK_DIR") or Path.cwd())
        ctx = cls(
            work_dir=work,
            artifacts_dir=Path(env.get("SWARM_ARTIFACTS_DIR") or (work.parent / "artifacts")),
            input_path=Path(env.get("SWARM_INPUT") or (work / "input.json")),
            result_path=Path(env.get("SWARM_RESULT") or (work / "result.json")),
            quota_path=Path(env.get("SWARM_QUOTA_SIGNAL") or (work / "quota.json")),
        )
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if ctx.input_path.exists():
            try:
                loaded = json.loads(ctx.input_path.read_text() or "{}")
                ctx.payload = loaded if isinstance(loaded, dict) else {"value": loaded}
            except json.JSONDecodeError as exc:
                raise RunnerFailure(f"input.json is not valid JSON: {exc}") from exc
        return ctx

    # -- helpers used by every runner --------------------------------------
    def artifact_path(self, name: str) -> Path:
        safe = Path(name).name
        if not safe or safe in (".", ".."):
            raise RunnerFailure(f"unsafe artifact name {name!r}")
        return self.artifacts_dir / safe

    def write_artifact(self, name: str, content: str | bytes) -> Path:
        path = self.artifact_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return path

    def write_result(
        self,
        *,
        status: str,
        summary: str = "",
        output: dict[str, Any] | None = None,
        error: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        artifacts = (
            sorted(p.name for p in self.artifacts_dir.iterdir() if p.is_file())
            if self.artifacts_dir.exists()
            else []
        )
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        self.result_path.write_text(
            json.dumps(
                {
                    "status": status,
                    "summary": summary,
                    "output": output or {},
                    "error": error,
                    "metrics": metrics or {},
                    "artifacts": artifacts,
                    "finished_at": _now(),
                },
                indent=2,
                default=str,
            )
        )

    def write_quota_signal(self, signal_: QuotaExhaustedSignal) -> None:
        self.quota_path.write_text(
            json.dumps(
                {
                    "provider": signal_.provider,
                    "state": "EXHAUSTED",
                    "retry_after_seconds": signal_.retry_after_seconds,
                    "reset_at": signal_.reset_at,
                    "detail": signal_.detail,
                    "observed_at": _now(),
                },
                indent=2,
            )
        )

    def install_signal_handlers(self) -> None:
        """Turn SIGTERM into a flag so a runner can stop at its next safe point.

        The worker sends SIGTERM and waits `termination_grace_seconds` before
        SIGKILL, so a runner that checks `stop_requested` in its loop gets to
        finish the file it is writing instead of leaving a half-written one in
        the next checkpoint.
        """

        def _handler(signum: int, _frame: Any) -> None:
            self.stop_requested = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not on the main thread; the worker will SIGKILL if needed


def run_runner(body: Callable[[RunnerContext], dict[str, Any]], *, name: str) -> int:
    """Shared entrypoint wrapper. Every runner's `__main__` calls this.

    It guarantees the two things the worker depends on: a result.json always
    exists after the child exits, and a quota signal always reaches disk before
    the exit code that announces it.
    """
    try:
        ctx = RunnerContext.from_env()
    except RunnerFailure as exc:
        print(f"[{name}] bad context: {exc}", file=sys.stderr)
        return EXIT_FAILED

    ctx.install_signal_handlers()
    try:
        output = body(ctx) or {}
    except QuotaExhaustedSignal as exc:
        ctx.write_quota_signal(exc)
        ctx.write_result(
            status="quota_exhausted",
            summary=str(exc),
            error=str(exc),
            output={"provider": exc.provider, "retry_after_seconds": exc.retry_after_seconds},
        )
        print(f"[{name}] provider quota exhausted: {exc}", file=sys.stderr)
        return EXIT_QUOTA_EXHAUSTED
    except RunnerFailure as exc:
        ctx.write_result(status="failed", summary=str(exc), error=str(exc))
        print(f"[{name}] failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # unexpected: still leave a readable result behind
        ctx.write_result(
            status="failed",
            summary=f"{type(exc).__name__}: {exc}",
            error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:],
        )
        print(f"[{name}] crashed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_FAILED

    if ctx.stop_requested:
        ctx.write_result(
            status="terminated",
            summary="runner stopped on SIGTERM",
            output=output,
        )
        return EXIT_TERMINATED

    status = str(output.pop("status", "succeeded"))
    summary = str(output.pop("summary", ""))
    metrics = output.pop("metrics", None)
    ctx.write_result(
        status=status,
        summary=summary,
        output=output,
        metrics=metrics if isinstance(metrics, dict) else None,
    )
    return EXIT_OK

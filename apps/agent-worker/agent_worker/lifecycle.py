"""The worker lifecycle, written as an ordered state machine.

The order below is the contract. It is not a suggestion, and two steps in it are
load-bearing in a way the rest are not.

    1.  VALIDATE THE FENCING GENERATION        <-- before anything else
    2.  STARTING -> RUNNING
    3.  create the isolated workspace
    4.  restore the latest checkpoint, if any
    5.  optional shallow git clone
    6.  resolve the tenant's provider credential
    7.  start the runner child
    8.  while it runs: heartbeat, MANDATORY periodic checkpoint, watch for
        cancellation / quota exhaustion / a generation change
    9.  capture stdout and stderr
    10. upload artifacts and a final checkpoint
    11. persist the terminal state
    12. release the lease
    13. exit

**Step 1 is the single most important safety property in the platform.** If the
generation in this worker's environment is not the generation on the task
document, another attempt owns this task -- a reconciler decided this one was
dead and the scheduler admitted a replacement. Running anyway would give the
tenant two concurrent agents editing the same repository with the same
credentials, and the second one to finish would silently overwrite the first.
So the check happens before the workspace exists, before a secret is read, and
before the runner is started; a fenced worker emits `generation_fenced` and
exits, and it does NOT touch the lease, because the live lease is not its own.

**Step 8's checkpoint is mandatory.** Cloud Run's ephemeral disk is a Preview
feature that disables live migration. Without a checkpoint, an infrastructure
event two hours into an agent run costs the whole attempt; with one, it costs
`checkpoint_interval_seconds`.

Between them sits the quota rule: a wait longer than
`max_in_worker_retry_delay_seconds` is never slept through. The worker
checkpoints, publishes what it learned about the provider, parks the task with
`next_eligible_at`, releases its lease and exits, so the slot and the memory go
back to the platform instead of idling.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from swarm_common.models import ProviderState, utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from . import workspace as workspace_mod
from .checkpoint import CheckpointManager, CheckpointRecord
from .config import WorkerConfig
from .control import ControlPlane, ControlSignals
from .errors import (
    CheckpointError,
    ExitCode,
    FencedError,
    WorkerError,
)
from .gitops import GitError, shallow_clone
from .metrics import ResourceSampler
from .objectstore import ObjectStore
from .procman import ChildProcess, ChildResult
from .quota import QuotaDecision, decide, read_runner_signal, signal_from_control
from .runners.base import EXIT_QUOTA_EXHAUSTED, EXIT_TERMINATED
from .secrets import CredentialMissing, SecretManagerClient, load_tenant, resolve_credentials

#: How many times the runner may be restarted in place after a SHORT provider
#: wait. A long wait parks instead, so this bound is only ever reached by a
#: provider that is flapping.
MAX_IN_WORKER_RETRIES = 3

#: One heartbeat event per this many lease heartbeats. The lease is refreshed
#: every interval; the event stream would be unreadable at that rate.
HEARTBEAT_EVENT_EVERY = 5

REPO_DIR_NAME = "repo"


@dataclass
class WorkerDeps:
    control: ControlPlane
    store: ObjectStore
    logger: Any
    db: Any
    metrics_exporter: Any
    secret_client: SecretManagerClient | None = None


@dataclass
class Outcome:
    exit_code: int
    state: TaskState | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class Worker:
    """One attempt, start to finish."""

    def __init__(self, config: WorkerConfig, deps: WorkerDeps) -> None:
        self.cfg = config
        self.control = deps.control
        self.store = deps.store
        self.log = deps.logger
        self.db = deps.db
        self.metrics = deps.metrics_exporter
        self.secret_client = deps.secret_client
        self.ws: workspace_mod.Workspace | None = None
        self.checkpoints = CheckpointManager(
            store=deps.store,
            tenant_id=config.tenant_id,
            task_id=config.task_id,
            attempt_id=config.attempt_id,
            generation=config.generation,
            logger=deps.logger,
            max_bytes=config.max_checkpoint_bytes,
        )
        self._interrupted = False
        self._child: ChildProcess | None = None
        self._sampler: ResourceSampler | None = None
        self._last_checkpoint: CheckpointRecord | None = None
        self._restored_from: CheckpointRecord | None = None
        self._heartbeats = 0
        self._deadline = time.monotonic() + config.timeout_seconds

    # ------------------------------------------------------------------
    # entrypoint
    # ------------------------------------------------------------------
    def run(self) -> int:
        self._install_signal_handlers()

        # ---- STEP 1: fencing, before anything else exists ---------------
        try:
            signals = self.control.validate_generation()
        except FencedError as exc:
            return self._exit_fenced(exc)

        if signals.cancel_requested:
            # Cancelled between admission and start: nothing ran, so there is
            # nothing to clean up beyond giving the slot back.
            self.log.warning("task cancelled before the runner started")
            self.control.finish(
                state=TaskState.CANCELLED,
                exit_code=None,
                error="cancelled before execution started",
            )
            return ExitCode.CANCELLED

        try:
            outcome = self._execute()
        except FencedError as exc:
            return self._exit_fenced(exc)
        except WorkerError as exc:
            self.log.exception("worker failed", exc)
            self._safe_finish(TaskState.FAILED, exit_code=exc.exit_code, error=str(exc))
            return exc.exit_code
        except Exception as exc:  # never exit without a durable terminal state
            self.log.exception("worker crashed", exc)
            self._safe_finish(TaskState.FAILED, exit_code=ExitCode.FAILED, error=str(exc))
            return ExitCode.FAILED
        finally:
            self._cleanup()
        return outcome.exit_code

    # ------------------------------------------------------------------
    # the main path
    # ------------------------------------------------------------------
    def _execute(self) -> Outcome:
        cfg = self.cfg

        # ---- STEP 2: STARTING -> RUNNING --------------------------------
        self.control.record_attempt_start(
            backend=cfg.backend, execution_name=_execution_name()
        )
        self.control.advance_to_running()

        # ---- STEP 3: isolated workspace ---------------------------------
        self.ws = workspace_mod.create(cfg.workspace_root, cfg.attempt_id)
        ws = self.ws
        self.log.info("workspace created", path=str(ws.root))

        # ---- STEP 4: restore the latest checkpoint ----------------------
        task = self.control.fetch_task()
        self._restore_checkpoint(task.get("latest_checkpoint"))

        # ---- STEP 5: optional shallow clone -----------------------------
        repo_info = self._maybe_clone(task)

        # ---- runner input -----------------------------------------------
        payload = dict(task.get("input") or {})
        if repo_info:
            payload.setdefault("repository", repo_info)
        if cfg.model:
            payload.setdefault("model", cfg.model)
        payload.setdefault("task_id", cfg.task_id)
        payload.setdefault("attempt_id", cfg.attempt_id)
        payload.setdefault("resumed_from_checkpoint", bool(self._restored_from))
        ws.input_path.write_text(json.dumps(payload, indent=2, default=str))

        # ---- STEP 6: tenant credentials ---------------------------------
        try:
            child_env = self._build_child_env()
        except CredentialMissing as exc:
            return self._park_credential_missing(exc)

        # Re-check fencing immediately before the agent starts. Cloning a large
        # repository can take minutes, and the whole point of step 1 is that
        # nothing runs under a stale generation -- including after a slow setup.
        self.control.validate_generation()

        # A provider that is already exhausted must not be hit again.
        preflight = self.control.poll(cfg.provider)
        quota_signal = signal_from_control(preflight, cfg.provider)
        if quota_signal is not None:
            decision = decide(
                quota_signal,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            if decision.park:
                return self._park_for_quota(decision, source="preflight")

        # ---- STEPS 7-9: run the child, supervised -----------------------
        attempt_number = 0
        while True:
            attempt_number += 1
            result = self._run_child_supervised(child_env)
            if isinstance(result, Outcome):
                return result                      # cancelled / fenced / parked
            quota = self._quota_from_child(result)
            if quota is None:
                break
            decision = decide(
                quota,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            self.control.update_quota_state(
                provider=quota.provider,
                state=quota.state,
                retry_after_seconds=quota.retry_after_seconds,
                reset_at=quota.reset_at,
            )
            if decision.park or attempt_number > MAX_IN_WORKER_RETRIES:
                return self._park_for_quota(decision, source="runner")
            # Short wait only: the slot stays held because reacquiring it would
            # cost more than the wait itself.
            self.log.warning(
                "short provider wait; retrying in place",
                wait_seconds=decision.wait_seconds,
                attempt=attempt_number,
            )
            self.control.emit(
                EventType.RETRYING,
                {"wait_seconds": decision.wait_seconds, "attempt": attempt_number},
            )
            self._sleep_with_heartbeat(decision.wait_seconds)
            ws.quota_path.unlink(missing_ok=True)

        # ---- STEPS 10-12: artifacts, checkpoint, terminal state, lease --
        return self._finalise(result)

    # ------------------------------------------------------------------
    # child supervision
    # ------------------------------------------------------------------
    def _run_child_supervised(self, child_env: dict[str, str]) -> ChildResult | Outcome:
        cfg = self.cfg
        ws = self.ws
        assert ws is not None

        argv = _runner_argv(cfg)
        child = ChildProcess(
            argv,
            cwd=ws.work,
            env=child_env,
            stdout_path=ws.stdout_path,
            stderr_path=ws.stderr_path,
            max_stdout_bytes=cfg.max_stdout_bytes,
            max_stderr_bytes=cfg.max_stderr_bytes,
            logger=self.log,
        )
        self._child = child
        child.start()
        self._start_sampler(child)

        now = time.monotonic()
        next_heartbeat = now + cfg.heartbeat_interval_seconds
        next_checkpoint = now + cfg.checkpoint_interval_seconds
        next_poll = now + cfg.control_poll_seconds

        while True:
            slice_ = max(
                0.05,
                min(
                    next_heartbeat - time.monotonic(),
                    next_checkpoint - time.monotonic(),
                    next_poll - time.monotonic(),
                    self._deadline - time.monotonic(),
                    5.0,
                ),
            )
            if child.wait(slice_) is not None:
                break
            now = time.monotonic()

            if self._interrupted:
                return self._handle_interruption(child)

            if now >= next_heartbeat:
                self._heartbeat()
                next_heartbeat = now + cfg.heartbeat_interval_seconds

            if now >= next_checkpoint:
                self._checkpoint("periodic")
                next_checkpoint = now + cfg.checkpoint_interval_seconds

            if now >= next_poll:
                next_poll = now + cfg.control_poll_seconds
                diverted = self._apply_control_signals(child)
                if diverted is not None:
                    return diverted

            if now >= self._deadline:
                self.log.error("task timeout reached", timeout_seconds=cfg.timeout_seconds)
                child.mark_timed_out()
                child.terminate(cfg.termination_grace_seconds, reason="task timeout")
                break

        result = child.finish()
        self._stop_sampler()
        self.log.info(
            "runner finished",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            killed=result.killed,
            seconds=round(result.duration_seconds, 2),
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
        )
        return result

    def _apply_control_signals(self, child: ChildProcess) -> Outcome | None:
        cfg = self.cfg
        signals: ControlSignals = self.control.poll(cfg.provider)

        if signals.is_fenced(cfg.generation):
            # Someone else owns this task now. Stop the agent immediately and
            # leave the lease alone -- it is not ours to release any more.
            self.log.error(
                "generation fenced mid-run; stopping the runner",
                observed_generation=signals.generation,
                expected_generation=cfg.generation,
            )
            child.terminate(cfg.termination_grace_seconds, reason="generation fenced")
            child.finish()
            self._stop_sampler()
            self.control.emit(
                EventType.GENERATION_FENCED,
                {
                    "expected_generation": cfg.generation,
                    "observed_generation": signals.generation,
                    "phase": "running",
                },
            )
            return Outcome(exit_code=ExitCode.GENERATION_FENCED, detail={"fenced": True})

        if signals.cancel_requested:
            self.log.warning("cancellation requested; stopping the runner")
            child.terminate(cfg.termination_grace_seconds, reason="cancelled")
            child.finish()
            self._stop_sampler()
            self._checkpoint("cancellation")
            summary = self._upload_outputs()
            self._export_metrics()
            self.control.finish(
                state=TaskState.CANCELLED,
                exit_code=None,
                error="cancelled by request",
                result_summary=summary,
            )
            return Outcome(exit_code=ExitCode.CANCELLED, state=TaskState.CANCELLED)

        quota = signal_from_control(signals, cfg.provider)
        if quota is not None:
            decision = decide(
                quota,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            if decision.park:
                self.log.warning(
                    "provider unavailable for longer than the worker may wait; parking",
                    wait_seconds=decision.wait_seconds,
                )
                child.terminate(cfg.termination_grace_seconds, reason="provider backpressure")
                child.finish()
                self._stop_sampler()
                return self._park_for_quota(decision, source="backpressure")
        return None

    def _handle_interruption(self, child: ChildProcess) -> Outcome:
        """SIGTERM to the WORKER: the platform is taking the sandbox away.

        Cloud Run gives a short notice before an instance goes; with ephemeral
        disk there is no live migration to save us. Checkpoint, hand the slot
        back and let the task be re-admitted immediately -- PARKED with an
        already-passed `next_eligible_at` costs nothing and the scheduler picks
        it up on its next pass.
        """
        cfg = self.cfg
        self.log.warning("worker received SIGTERM; checkpointing before exit")
        child.terminate(cfg.termination_grace_seconds, reason="worker interrupted")
        child.finish()
        self._stop_sampler()
        self._checkpoint("interrupted")
        summary = self._upload_outputs()
        self._export_metrics()
        self.control.park(
            reason=ParkReason.SCHEDULED_RETRY,
            next_eligible_at=utcnow(),
            detail={"cause": "worker_interrupted", **summary},
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    # ------------------------------------------------------------------
    # finalisation
    # ------------------------------------------------------------------
    def _finalise(self, result: ChildResult) -> Outcome:
        ws = self.ws
        assert ws is not None

        self._checkpoint("final")
        summary = self._upload_outputs()
        summary["exit_code"] = result.exit_code
        summary["duration_seconds"] = round(result.duration_seconds, 3)
        self._export_metrics()

        runner_result = _read_json(ws.result_path)
        if runner_result:
            summary["runner"] = {
                "status": runner_result.get("status"),
                "summary": str(runner_result.get("summary", ""))[:4000],
                "output": _truncate_json(runner_result.get("output"), 8000),
                "metrics": runner_result.get("metrics") or {},
            }

        if result.timed_out:
            error = f"runner exceeded its {self.cfg.timeout_seconds}s timeout and was killed"
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=result.exit_code,
                error=error,
                result_summary=summary,
            )
            return Outcome(exit_code=ExitCode.TIMEOUT, state=TaskState.FAILED)

        if result.exit_code == 0:
            if runner_result is None:
                error = "runner exited 0 without writing result.json"
                self.control.finish(
                    state=TaskState.FAILED, exit_code=0, error=error, result_summary=summary
                )
                return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)
            if self.cfg.provider:
                # A clean run is evidence the provider is healthy again.
                self.control.update_quota_state(
                    provider=self.cfg.provider, state=ProviderState.AVAILABLE
                )
            self.control.finish(
                state=TaskState.SUCCEEDED, exit_code=0, result_summary=summary
            )
            return Outcome(exit_code=ExitCode.OK, state=TaskState.SUCCEEDED)

        if result.exit_code == EXIT_TERMINATED:
            self.control.finish(
                state=TaskState.CANCELLED,
                exit_code=result.exit_code,
                error="runner stopped on SIGTERM",
                result_summary=summary,
            )
            return Outcome(exit_code=ExitCode.CANCELLED, state=TaskState.CANCELLED)

        error = (runner_result or {}).get("error") or _tail_text(ws.stderr_path)
        self.control.finish(
            state=TaskState.FAILED,
            exit_code=result.exit_code,
            error=str(error)[:4000] if error else f"runner exited {result.exit_code}",
            result_summary=summary,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    # ------------------------------------------------------------------
    # pieces
    # ------------------------------------------------------------------
    def _exit_fenced(self, exc: FencedError) -> int:
        """Emit the event and leave. No workspace, no agent, no lease change."""
        self.log.error(
            "FENCED: this attempt has been superseded; exiting without running the agent",
            expected_generation=exc.expected,
            observed_generation=exc.actual,
            reason=str(exc),
        )
        try:
            self.control.emit(
                EventType.GENERATION_FENCED,
                {
                    "expected_generation": exc.expected,
                    "observed_generation": exc.actual,
                    "reason": str(exc),
                    "phase": "startup",
                },
            )
        except Exception as emit_exc:  # the exit code is the real signal
            self.log.warning("could not record the fencing event", error=str(emit_exc))
        return ExitCode.GENERATION_FENCED

    def _restore_checkpoint(self, pointer: Any) -> None:
        ws = self.ws
        assert ws is not None
        record: CheckpointRecord | None = None
        if isinstance(pointer, str) and pointer:
            record = self.checkpoints.find_by_uri(pointer)
        if record is None:
            record = self.checkpoints.find_latest()
        if record is None:
            self.log.info("no checkpoint to restore; starting from an empty workspace")
            return
        files = self.checkpoints.restore(record, ws)
        self._restored_from = record
        self.control.emit(
            EventType.CHECKPOINT_RESTORED,
            {
                "checkpoint_id": record.checkpoint_id,
                "from_attempt": record.attempt_id,
                "files": files,
                "bytes": record.archive_bytes,
            },
        )

    def _maybe_clone(self, task: dict[str, Any]) -> dict[str, Any] | None:
        ws = self.ws
        assert ws is not None
        url = self.cfg.repository_url or task.get("repository_url")
        if not url:
            return None
        destination = ws.work / REPO_DIR_NAME
        if destination.exists() and any(destination.iterdir()):
            # Restored from a checkpoint that already contains the clone.
            self.log.info("repository already present from checkpoint; skipping clone")
            return {"path": REPO_DIR_NAME, "from_checkpoint": True}
        ref = self.cfg.repository_ref or task.get("repository_ref")
        try:
            clone = shallow_clone(
                url=url,
                ref=ref,
                destination=destination,
                workspace_tmp=ws.tmp,
                logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds,
                logger=self.log,
                token=os.environ.get("GIT_TOKEN") or None,
            )
        except GitError as exc:
            raise WorkerError(f"repository clone failed: {exc}") from exc
        return {
            "path": REPO_DIR_NAME,
            "url": clone.url,
            "ref": clone.ref,
            "commit": clone.commit,
        }

    def _build_child_env(self) -> dict[str, str]:
        ws = self.ws
        assert ws is not None
        profile = self.cfg.profile
        base: dict[str, str] = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"
            ),
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "SWARM_TASK_ID": self.cfg.task_id,
            "SWARM_ATTEMPT_ID": self.cfg.attempt_id,
            "SWARM_TENANT_ID": self.cfg.tenant_id,
            "SWARM_RUNNER_PROFILE": self.cfg.runner_profile,
        }
        for passthrough in ("PYTHONPATH", "VIRTUAL_ENV", "NODE_PATH", "NODE_EXTRA_CA_CERTS"):
            value = os.environ.get(passthrough)
            if value:
                base[passthrough] = value
        if self.cfg.model:
            base["MODEL"] = self.cfg.model
        for name in ("CLAUDE_CODE_BIN", "CLAUDE_CODE_ARGS", "CODEX_BIN", "CODEX_ARGS"):
            # Platform-set, never caller-set: they live in the Job definition.
            if os.environ.get(name):
                base[name] = os.environ[name]

        if profile.provider and profile.secrets:
            if self.secret_client is None:
                raise WorkerError(
                    f"runner {profile.name} needs the {profile.provider} credential but no "
                    "Secret Manager client is configured"
                )
            tenant = load_tenant(self.db, self.cfg.tenant_id)
            resolved = resolve_credentials(
                tenant=tenant,
                provider=profile.provider,
                secret_env_names=profile.secrets,
                client=self.secret_client,
                logger=self.log,
            )
            base.update(resolved.env)
        return ws.child_env(base)

    def _park_credential_missing(self, exc: CredentialMissing) -> Outcome:
        """No key for this provider. Park; it costs nothing while an admin fixes it."""
        self.log.error("tenant credential missing; parking", provider=exc.provider)
        self._checkpoint("credential-missing")
        summary = self._upload_outputs()
        self._export_metrics()
        self.control.park(
            reason=ParkReason.CREDENTIAL_MISSING,
            next_eligible_at=utcnow() + timedelta(hours=1),
            detail={"provider": exc.provider, **summary},
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_for_quota(self, decision: QuotaDecision, *, source: str) -> Outcome:
        """Checkpoint -> upload -> publish quota -> PARK -> release -> exit."""
        self.log.warning(
            "parking on provider quota",
            wait_seconds=decision.wait_seconds,
            reason=decision.reason.value,
            source=source,
        )
        self._checkpoint("quota-park")
        summary = self._upload_outputs()
        self._export_metrics()
        provider = str(decision.detail.get("provider") or self.cfg.provider or "")
        if provider:
            try:
                state = ProviderState(str(decision.detail.get("provider_state", "EXHAUSTED")))
            except ValueError:
                state = ProviderState.EXHAUSTED
            self.control.update_quota_state(
                provider=provider,
                state=state,
                retry_after_seconds=decision.wait_seconds,
                reset_at=decision.next_eligible_at,
            )
        # `source` is where the signal was OBSERVED (the runner's 429, the
        # control plane's aggregate, the pre-flight check); `decision.detail`
        # already carries where it came FROM. Both are kept, under distinct keys.
        self.control.emit(
            EventType.QUOTA_EXHAUSTED,
            {
                **decision.detail,
                "next_eligible_at": decision.next_eligible_at,
                "park_phase": source,
            },
        )
        self.control.park(
            reason=decision.reason,
            next_eligible_at=decision.next_eligible_at,
            detail={**decision.detail, "park_phase": source, **summary},
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _quota_from_child(self, result: ChildResult) -> Any:
        ws = self.ws
        assert ws is not None
        signal_ = read_runner_signal(ws.quota_path, self.cfg.provider)
        if signal_ is None and result.exit_code == EXIT_QUOTA_EXHAUSTED:
            # The runner announced a rate limit but could not describe it.
            from .quota import QuotaSignal

            signal_ = QuotaSignal(
                provider=self.cfg.provider or "unknown",
                state=ProviderState.EXHAUSTED,
                source="runner",
                detail=f"runner exited {EXIT_QUOTA_EXHAUSTED} without a quota signal file",
            )
        return signal_

    # -- periodic work -----------------------------------------------------
    def _heartbeat(self) -> None:
        self.control.heartbeat()
        self._heartbeats += 1
        if self._heartbeats % HEARTBEAT_EVENT_EVERY == 1:
            usage = self._sampler.usage if self._sampler else None
            self.control.emit(
                EventType.HEARTBEAT,
                {
                    "elapsed_seconds": round(self._child.elapsed_seconds, 1) if self._child else 0,
                    "peak_rss_bytes": usage.peak_rss_bytes if usage else None,
                    "checkpoints": self.checkpoints.seq,
                },
            )

    def _checkpoint(self, label: str) -> CheckpointRecord | None:
        """Mandatory checkpoint. A failure here is logged, never swallowed.

        The one thing a failed checkpoint must not do is end the attempt: the
        agent is still working, and the correct response to "this checkpoint did
        not upload" is to try again at the next interval, not to throw away the
        run that is currently succeeding.
        """
        if self.ws is None:
            return None
        try:
            self.control.emit(EventType.CHECKPOINT_STARTED, {"label": label})
            record = self.checkpoints.create(self.ws, label=label)
        except CheckpointError as exc:
            self.log.error("CHECKPOINT FAILED", label=label, error=str(exc))
            return None
        except Exception as exc:
            self.log.exception("checkpoint failed unexpectedly", exc, label=label)
            return None
        self._last_checkpoint = record
        self.control.record_checkpoint(
            checkpoint_id=record.checkpoint_id,
            uri=record.uri,
            size_bytes=record.archive_bytes,
            seq=record.seq,
        )
        return record

    def _sleep_with_heartbeat(self, seconds: float) -> None:
        """Short waits only -- long ones park. Keeps the lease alive meanwhile."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._interrupted:
            time.sleep(min(self.cfg.heartbeat_interval_seconds, max(0.1, end - time.monotonic())))
            self._heartbeat()

    # -- uploads -----------------------------------------------------------
    def _upload_outputs(self) -> dict[str, Any]:
        ws = self.ws
        if ws is None:
            return {}
        artifacts: list[dict[str, Any]] = []
        skipped: list[str] = []
        total = 0
        for path in sorted(ws.artifacts.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            rel = path.relative_to(ws.artifacts).as_posix()
            if total + size > self.cfg.max_artifact_bytes:
                skipped.append(rel)
                continue
            key = f"{self.cfg.artifact_prefix}/{rel}"
            try:
                self.store.upload_file(key, path)
            except Exception as exc:
                self.log.warning("artifact upload failed", artifact=rel, error=str(exc))
                skipped.append(rel)
                continue
            total += size
            artifacts.append({"name": rel, "bytes": size, "uri": self.store.uri(key)})

        logs: dict[str, str] = {}
        for label, path in (("stdout", ws.stdout_path), ("stderr", ws.stderr_path)):
            if path.exists():
                key = f"{self.cfg.log_prefix}/{path.name}"
                try:
                    self.store.upload_file(key, path, content_type="text/plain")
                    logs[label] = self.store.uri(key)
                except Exception as exc:
                    self.log.warning("log upload failed", stream=label, error=str(exc))

        if skipped:
            self.log.warning(
                "artifacts skipped", count=len(skipped), cap_bytes=self.cfg.max_artifact_bytes
            )
        summary: dict[str, Any] = {
            "artifacts": artifacts,
            "artifact_bytes": total,
            "logs": logs,
        }
        if skipped:
            summary["artifacts_skipped"] = skipped[:50]
        if self._last_checkpoint is not None:
            summary["checkpoint"] = {
                "checkpoint_id": self._last_checkpoint.checkpoint_id,
                "uri": self._last_checkpoint.uri,
                "bytes": self._last_checkpoint.archive_bytes,
            }
        if self._restored_from is not None:
            summary["restored_from"] = {
                "checkpoint_id": self._restored_from.checkpoint_id,
                "attempt_id": self._restored_from.attempt_id,
            }
        return summary

    # -- metrics -----------------------------------------------------------
    def _start_sampler(self, child: ChildProcess) -> None:
        ws = self.ws
        assert ws is not None
        self._sampler = ResourceSampler(
            pid_provider=lambda: child.pid,
            disk_provider=ws.disk_bytes,
            memory_limit_bytes=self.cfg.memory_limit_bytes,
        )
        self._sampler.start()

    def _stop_sampler(self) -> None:
        if self._sampler is None:
            return
        usage = self._sampler.stop()
        self.control.record_resource_usage(
            peak_rss_bytes=usage.peak_rss_bytes,
            peak_disk_bytes=usage.peak_disk_bytes,
            oom_near_miss=usage.oom_near_miss,
        )
        if usage.oom_near_miss:
            self.log.error(
                "OOM NEAR MISS: this attempt came within a hair of its memory limit",
                peak_rss_bytes=usage.peak_rss_bytes,
                limit_bytes=self.cfg.memory_limit_bytes,
                resource_class=self.cfg.resource_class,
            )

    def _export_metrics(self) -> None:
        if self._sampler is None:
            return
        self.metrics.export(
            self._sampler.usage,
            {
                "tenant_id": self.cfg.tenant_id,
                "runner_profile": self.cfg.runner_profile,
                "resource_class": self.cfg.resource_class,
                "backend": self.cfg.backend,
                "attempt_id": self.cfg.attempt_id,
                "task_id": self.cfg.task_id,
            },
        )
        self._sampler = None

    # -- misc --------------------------------------------------------------
    def _remaining_seconds(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            self._interrupted = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def _safe_finish(self, state: TaskState, *, exit_code: int, error: str) -> None:
        try:
            summary = self._upload_outputs()
            self._export_metrics()
            self.control.finish(
                state=state, exit_code=exit_code, error=error[:4000], result_summary=summary
            )
        except Exception as exc:
            # The reconciler is the backstop: a lease with no heartbeat gets
            # reclaimed, so a worker that cannot write its own epitaph still
            # cannot strand a slot.
            self.log.exception("could not persist the terminal state", exc)

    def _cleanup(self) -> None:
        if self._child is not None:
            try:
                if self._child.poll() is None:
                    self._child.terminate(self.cfg.termination_grace_seconds, reason="cleanup")
                    self._child.finish()
            except Exception:
                pass
        if self.ws is not None:
            workspace_mod.destroy(self.ws)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _runner_argv(cfg: WorkerConfig) -> list[str]:
    """The command comes from the FROZEN catalogue, keyed by profile name.

    Nothing from the environment and nothing from the caller contributes to it,
    which is invariant 10 enforced at the last possible moment.
    """
    command = list(cfg.profile.command)
    program = command[0]
    if program in ("python", "python3") and shutil.which(program) is None:
        command[0] = sys.executable
    return command


def _execution_name() -> str | None:
    for name in ("CLOUD_RUN_EXECUTION", "K_REVISION", "JOB_NAME", "HOSTNAME"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else {"value": data}


def _truncate_json(value: Any, limit: int) -> Any:
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(encoded) <= limit:
        return value
    return {"truncated": True, "preview": encoded[:limit]}


def _tail_text(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-limit:]

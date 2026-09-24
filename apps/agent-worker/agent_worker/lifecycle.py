"""The worker lifecycle, written as an ordered state machine.

The order below is the contract. It is not a suggestion, and two steps in it are
load-bearing in a way the rest are not.

    1.  VALIDATE THE FENCING GENERATION        <-- before anything else
    2.  STARTING -> RUNNING
    3.  create the isolated workspace
    4.  restore the latest checkpoint, if any
    5.  optional shallow git clone
    5b. stage every artifact this step declared in `input_from`
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from swarm_common.models import ProviderState, utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from . import inputs as inputs_mod
from . import redact as redact_mod
from . import workspace as workspace_mod
from .accountlease import (
    ACCOUNT_TOKEN_ENV,
    ACCOUNT_UNREADABLE,
    BROKER_REFUSED,
    NO_RECENT_READING,
    POOL_PAUSED,
    AccountBroker,
    AccountUnreadable,
    Assignment,
    BrokerRefused,
    BrokerUnavailable,
    NoAccount,
    NoAccountAvailable,
    credential_env_from_account,
)
from .checkpoint import CheckpointManager, CheckpointRecord
from .config import WorkerConfig
from .control import ControlPlane, ControlSignals
from .errors import (
    CheckpointError,
    ExitCode,
    FencedError,
    TenantMismatchError,
    WorkerError,
)
from .forge import ForgeError, probe_repository, open_pull_request
from .gitops import (
    GitError,
    MergeOutcome,
    commit_dirty,
    merge_branches,
    push_branch,
    shallow_clone,
    summarize_work,
)
from .metrics import ResourceSampler
from .objectstore import ObjectStore
from .procman import ChildProcess, ChildResult
from .quota import QuotaDecision, decide, read_runner_signal, signal_from_control
from .runners.base import EXIT_QUOTA_EXHAUSTED, EXIT_TERMINATED, SPEND_KEYS
from .runners.limits import GRACE_ENV, STDERR_ENV, STDOUT_ENV, TIMEOUT_ENV
from .secrets import (
    CredentialMissing,
    SecretError,
    SecretManagerClient,
    load_tenant,
    resolve_credentials,
    resolve_git_token,
)

#: How many times the runner may be restarted in place after a SHORT provider
#: wait. A long wait parks instead, so this bound is only ever reached by a
#: provider that is flapping.
MAX_IN_WORKER_RETRIES = 3

#: How many times an attempt may reload its credential and restart in place.
#:
#: Two, not three, and not unbounded. A credential that was ROTATED under a
#: running agent is fixed by exactly one reload; a second covers the unlucky
#: case of a rotation landing again during the restart. Beyond that the
#: credential is not rotating, it is broken -- and retrying a broken credential
#: forever would turn one bad account into an attempt that never fails and
#: never finishes, holding its lease the whole time.
MAX_CREDENTIAL_RELOADS = 2

#: How long to park when the account pool has nothing and cannot say when it
#: will. Used ONLY as the fallback: when the broker reports the instant the
#: binding window clears, the task waits until exactly that instant instead.
#:
#: Fifteen minutes rather than one: the pool is empty because other agents are
#: using it, and a task that wakes every minute to be told the same thing is a
#: dispatch, an image pull and a lease for nothing. Rather than five, because a
#: five-hour window releases capacity in bursts.
NO_ACCOUNT_RETRY_SECONDS = 900

#: How long to park when the pool's readings have all gone STALE.
#:
#: A different number from the one above because it is a different claim.
#: "Spent" is a fact about the accounts and clears when the provider's window
#: rolls over, hours away. "Not observed recently" says nothing about whether
#: there is room -- only that nobody has looked lately -- and the broker's own
#: usage poll looks on every sweep tick, nominally every five minutes. Parking
#: for fifteen would leave a pool with plenty of headroom idle for three sweeps
#: after it had already refreshed its readings.
STALE_READING_RETRY_SECONDS = 300

#: How many different accounts one attempt will try before it gives up and
#: parks. Three, not one: a freshly onboarded account whose secret has no
#: version yet, and a borrowed account this worker was never granted access
#: to, both look identical at `choose()` -- which is deterministic, so asking
#: again without excluding the one that failed returns the same answer.
#:
#: Bounded, and small, because every miss is a Secret Manager call and a
#: round trip to the broker while this worker holds a concurrency slot. If
#: three different accounts in a row cannot be read, the pool needs a person,
#: not a fourth try.
MAX_ACCOUNT_TRIES = 3

#: One heartbeat event per this many lease heartbeats. The lease is refreshed
#: every interval; the event stream would be unreadable at that rate.
HEARTBEAT_EVENT_EVERY = 5

REPO_DIR_NAME = "repo"

#: Worker-owned scratch INSIDE the checkpointed work directory. It has to be
#: inside `work/` so a resumed attempt still knows which commit its clone
#: landed on, and outside `work/repo/` so nothing it holds can turn up in the
#: agent's own diff.
WORKER_STATE_DIR = ".swarm"
CLONE_BASE_FILE = "clone-base"
PATCH_NAME = "swarm-work.patch"


@dataclass
class WorkerDeps:
    control: ControlPlane
    store: ObjectStore
    logger: Any
    db: Any
    metrics_exporter: Any
    secret_client: SecretManagerClient | None = None
    #: The account pool client. None means "build one from the config, if the
    #: config names a broker" -- which is what the entrypoint does. Injected in
    #: tests so no unit test needs a metadata server or a network.
    account_broker: Any | None = None


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
        self._repo_url: str | None = None
        self._task: dict[str, Any] | None = None
        self._clone_base: str | None = None
        # What `metadata.input_from` put in the workspace. Kept so the result
        # summary can report which upstream artifact this attempt actually ran
        # on -- the question every debugging of a wrong workflow output starts
        # with, and one the workspace cannot answer because it is destroyed.
        self._staged_inputs: list[inputs_mod.StagedInput] = []
        self._heartbeats = 0
        self._deadline = time.monotonic() + config.timeout_seconds
        # The account this attempt holds, if the pool gave it one. Set once and
        # kept: a credential reload must re-read the SAME account's secret, not
        # move the agent onto a different subscription mid-run.
        self._account: Assignment | None = None
        self._account_released = False
        # Once this attempt has decided it is NOT running on the pool, it stays
        # decided. `_build_child_env` is called again on the credential-reload
        # path, and asking the broker a second time there could move a running
        # agent from the tenant secret onto a pool account halfway through its
        # run -- or, if the pool had emptied in the meantime, raise
        # NoAccountAvailable from a call site that is not a parking one.
        self._account_declined: str | None = None
        # Accounts this attempt was given and could not read. Sent back to the
        # broker as `exclude` so the next ask does not return the same one.
        self._account_rejected: list[str] = []
        # WHAT THIS ATTEMPT SPENT, summed over every runner it started. An
        # attempt can start several -- a short rate limit and a reloaded
        # credential both restart in place -- and each one's result.json is
        # overwritten by the next, so the numbers are taken off each run as it
        # ends (`_collect_spend`) rather than read once at the end.
        self._spend: dict[str, Any] = {}
        # What `record_spend` last wrote, so the several exits that call
        # `_record_spend` write once per change rather than once per call.
        self._spend_recorded: dict[str, Any] | None = None
        # True from a runner's start until its result has been collected. It
        # is what stops `_cleanup` collecting the same run a second time.
        self._spend_pending = False
        # Set on the tenant-mismatch exit, the one path that must write NOTHING
        # -- not even spend onto what may be another tenant's attempt.
        self._writes_forbidden = False
        self._account_broker = deps.account_broker
        if self._account_broker is None and config.quota_broker_url:
            self._account_broker = AccountBroker(
                config.quota_broker_url,
                logger=deps.logger,
                audience=config.quota_broker_audience,
            )

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
        except TenantMismatchError as exc:
            return self._exit_tenant_mismatch(exc)

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
        except TenantMismatchError as exc:
            # BEFORE `except WorkerError`, and the order is the point. The
            # generic handler calls `_safe_finish`, which writes a terminal
            # state, an attempt record and an event -- and on this path every
            # one of those writes would land on ANOTHER TENANT's documents.
            return self._exit_tenant_mismatch(exc)
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

        # THE FIRST HEARTBEAT GOES HERE, BEFORE ANY SLOW WORK.
        #
        # It used to happen only once the agent child was running -- after the
        # workspace, the checkpoint restore and the clone. But the reconciler
        # measures silence from LEASE ACQUISITION, so everything before this
        # point counted against `heartbeat_grace_seconds` (90): container cold
        # start, image pull, workspace setup, restoring a checkpoint and a
        # shallow clone.
        #
        # On 2026-09-19 that reclaimed a task three times in a row. Each
        # replacement worker started, found its generation superseded, logged
        # "FENCED: this attempt has been superseded; exiting without running the
        # agent" and exited 70 -- invariant 5 doing exactly what it exists to
        # do, on workers that were never unhealthy. The task never ran at all.
        #
        # Raising the grace was the other option and is worse: it delays every
        # genuine reclaim to accommodate startup cost. Heartbeating here makes
        # the grace measure LIVENESS, which is what it is for.
        self._heartbeat()

        # ---- STEP 3: isolated workspace ---------------------------------
        self.ws = workspace_mod.create(cfg.workspace_root, cfg.attempt_id)
        ws = self.ws
        self.log.info("workspace created", path=str(ws.root))

        # ---- STEP 4: restore the latest checkpoint ----------------------
        task = self.control.fetch_task()
        # Kept because the publish gate, several steps later, needs the
        # caller's dispatch strategy and re-fetching it there would be a
        # second read of a document that cannot have changed.
        self._task = task
        self._restore_checkpoint(task.get("latest_checkpoint"))
        # Restoring a large checkpoint is unbounded; prove liveness after it.
        self._heartbeat()

        # ---- STEP 5: optional shallow clone -----------------------------
        repo_info = self._maybe_clone(task)
        # A clone is the single slowest step before the agent starts, and the
        # one most likely to vary with repository size.
        self._heartbeat()

        # ---- STEP 5b: stage the artifacts this step declared -------------
        # Order relative to the clone is not load-bearing: the two write to
        # different paths inside `work/`, and a declared name that would collide
        # with the clone directory is refused rather than resolved. Order
        # relative to the RUNNER INPUT is: `input.json` has to be able to tell
        # the agent what it was given, so staging happens first.
        staged_inputs = self._stage_declared_inputs(task)
        if staged_inputs:
            # Downloading an upstream artifact is unbounded in the same way a
            # clone is; prove liveness after it for the same reason.
            self._heartbeat()

        # ---- runner input -----------------------------------------------
        payload = dict(task.get("input") or {})
        if repo_info:
            payload.setdefault("repository", repo_info)
        if staged_inputs:
            # Assigned, not `setdefault`: this key describes what is actually on
            # disk right now, so a caller's own `staged_inputs` in the step input
            # must not shadow it and leave the agent reading a stale claim.
            payload["staged_inputs"] = [item.as_dict() for item in staged_inputs]
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
        except NoAccountAvailable as exc:
            # The pool is this tenant's way of running and it is momentarily
            # empty. A wait, not a failure -- see `_park_no_account`.
            return self._park_no_account(exc)

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
        credential_reloads = 0
        while True:
            attempt_number += 1
            result = self._run_child_supervised(child_env)
            if isinstance(result, Outcome):
                return result                      # cancelled / fenced / parked
            quota = self._quota_from_child(result)
            if quota is None:
                # A REFUSED credential, which is not a failed attempt. The
                # platform refreshes accounts on a timer whether or not an
                # agent is holding one, and refreshing an OAuth credential
                # revokes the previously issued token -- so a long attempt can
                # have its token pulled out from under it. The secret already
                # holds the replacement; re-reading it costs one API call and
                # saves the attempt.
                refusal = self._credential_refusal()
                if refusal is not None and credential_reloads < MAX_CREDENTIAL_RELOADS:
                    credential_reloads += 1
                    self.log.warning(
                        "credential refused; reloading it and restarting in place",
                        provider=refusal.get("provider"),
                        marker=refusal.get("marker"),
                        reload=credential_reloads,
                    )
                    self.control.emit(
                        EventType.RETRYING,
                        {
                            "cause": "credential_reloaded",
                            "provider": refusal.get("provider"),
                            "reload": credential_reloads,
                        },
                    )
                    ws.credential_path.unlink(missing_ok=True)
                    # Rebuilt, not patched: `_build_child_env` is the one place
                    # that knows which env names this profile's credential maps
                    # to, and `access()` always reads `latest`, so this picks up
                    # whatever the refresher wrote.
                    #
                    # GUARDED LIKE STEP 6, because it is the same call and it
                    # can raise the same two exceptions. Unguarded, a pool that
                    # had emptied or an account whose secret had become
                    # unreadable since the agent started would leave
                    # NoAccountAvailable -- a plain RuntimeError -- to reach
                    # run()'s generic handler and write TaskState.FAILED, which
                    # is the one outcome this whole path exists to avoid.
                    try:
                        child_env = self._build_child_env()
                    except CredentialMissing as exc:
                        return self._park_credential_missing(exc)
                    except NoAccountAvailable as exc:
                        return self._park_no_account(exc)
                    continue
                if refusal is not None:
                    self.log.error(
                        "credential still refused after reloading; this account "
                        "needs re-authentication rather than another attempt",
                        provider=refusal.get("provider"),
                    )
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

        # THE RUNNER'S REPLY FILES ARE CLEARED BEFORE IT STARTS. result.json,
        # quota.json and credential.json are how a runner answers the worker,
        # and all three live in `work/` -- which is exactly what a checkpoint
        # archives and the next attempt restores. So a resumed attempt used to
        # start with the PREVIOUS attempt's answers already on disk:
        #
        #   * quota.json from the run that parked it. `_quota_from_child` read
        #     it after the NEW runner exited -- whatever it exited with -- and
        #     parked the task again on a 429 that belonged to another attempt.
        #     Every resumed attempt did its work and re-parked, so a task that
        #     was rate-limited once could never finish;
        #   * result.json, which a runner killed before writing its own left in
        #     place to be read as this run's result and counted as its spend.
        #
        # Anything in these paths before the runner starts is, by definition,
        # not from this runner. The in-place retry and the credential reload
        # already removed one file each for the same reason; this is the one
        # place that covers all three for every start.
        for stale in (ws.result_path, ws.quota_path, ws.credential_path):
            stale.unlink(missing_ok=True)
        self._spend_pending = True

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
        next_live_log = now + cfg.live_log_interval_seconds

        while True:
            slice_ = max(
                0.05,
                min(
                    next_heartbeat - time.monotonic(),
                    next_checkpoint - time.monotonic(),
                    next_poll - time.monotonic(),
                    next_live_log - time.monotonic(),
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

            if now >= next_live_log:
                self._publish_live_logs()
                next_live_log = now + cfg.live_log_interval_seconds

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
        self._child_ended()
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
            self._child_ended()
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
            self._child_ended()
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
                self._child_ended()
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
        self._child_ended()
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
        # The ONLY call that passes publish=True. The agent exited on its own
        # here; the other five call sites are parks and crashes.
        summary = self._upload_outputs(publish=True)
        summary["exit_code"] = result.exit_code
        summary["duration_seconds"] = round(result.duration_seconds, 3)
        self._export_metrics()

        runner_result = _read_json(ws.result_path)
        if runner_result:
            # The SAME figure `_record_spend` wrote onto the attempt (it ran
            # inside `_upload_outputs` above): the attempt's total across every
            # runner it started, so the summary a human reads and the typed
            # fields a query reads cannot disagree after an in-place retry.
            usage_summary = dict(self._spend)
            summary["runner"] = self._scrub(
                {
                    "status": runner_result.get("status"),
                    "summary": str(runner_result.get("summary", ""))[:4000],
                    "output": _truncate_json(runner_result.get("output"), 8000),
                    # Extracted BEFORE the line above discards it. See
                    # _usage_summary: the truncation dropped token counts on
                    # precisely the most expensive runs.
                    "usage": usage_summary,
                    "metrics": runner_result.get("metrics") or {},
                }
            )

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
            # `last_error` is a Firestore field and a failing CLI is exactly the
            # thing that echoes its own configuration, so the tail is scrubbed.
            error=self._scrub(str(error)[:4000]) if error else f"runner exited {result.exit_code}",
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

    def _exit_tenant_mismatch(self, exc: TenantMismatchError) -> int:
        """Stop, having written nothing. Not even an event.

        A control-plane document names a tenant that is not this attempt's, which
        is either corruption or another tenant writing into these documents.
        Either way the worker must not touch them: `emit` would create an event
        under another tenant's task, `finish` would rewrite their state and
        `release_lease` would decrement pools their live attempt is holding. So
        this path logs to stdout -- the one channel that is this pod's own -- and
        exits. The reconciler is the backstop: the lease stops heartbeating and
        is reclaimed by the component that is allowed to act across tenants.

        `_cleanup` still runs after this, in `run()`'s `finally`, and it records
        spend on every other exit. The flag is what stops it here.
        """
        self._writes_forbidden = True
        self.log.error(
            "TENANT MISMATCH: a control-plane document belongs to another tenant; "
            "exiting without writing anything",
            kind=exc.kind,
            document_id=exc.document_id,
            expected_tenant=exc.expected,
            actual_tenant=exc.actual,
        )
        return ExitCode.TENANT_MISMATCH

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
        self._repo_url = url
        if destination.exists() and any(destination.iterdir()):
            # Restored from a checkpoint that already contains the clone. The
            # base commit is read back from the marker the FIRST attempt wrote,
            # because by now the agent's own commits sit on top of it and HEAD
            # no longer answers "what did this repository look like on arrival".
            self.log.info("repository already present from checkpoint; skipping clone")
            self._clone_base = self._read_clone_base()
            return {
                "path": REPO_DIR_NAME,
                "from_checkpoint": True,
                "commit": self._clone_base,
            }
        ref = self.cfg.repository_ref or task.get("repository_ref")
        try:
            clone = shallow_clone(
                url=url,
                ref=ref,
                destination=destination,
                # The worker's own scratch directory, NOT `ws.tmp`: `ws.tmp` is
                # what the agent is handed as TMPDIR, and a credential file left
                # there is one `cat $TMPDIR/.git-credentials` away from any
                # prompt injection in the repository being cloned.
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds,
                logger=self.log,
                token=self._git_token(),
            )
        except GitError as exc:
            raise WorkerError(f"repository clone failed: {exc}") from exc
        self._repo_url = clone.url
        self._clone_base = clone.commit
        self._write_clone_base(clone.commit)
        return {
            "path": REPO_DIR_NAME,
            "url": clone.url,
            "ref": clone.ref,
            "commit": clone.commit,
        }

    def _stage_declared_inputs(self, task: dict[str, Any]) -> list[inputs_mod.StagedInput]:
        """Honour `metadata.input_from`: {upstream_task_id: artifact_filename}.

        The API validates the declaration against the DAG and the service
        rewrites it onto the task keyed by upstream TASK id; this is where the
        file actually arrives. See `agent_worker.inputs` for why a declared
        input that cannot be staged fails the attempt instead of warning.

        A task with no declaration returns here without a single read, which is
        what makes this invisible to every deployment that does not use it.
        """
        ws = self.ws
        assert ws is not None
        declared = inputs_mod.declared_inputs(task.get("metadata"))
        if not declared:
            return []
        staged = inputs_mod.stage_inputs(
            declared,
            work=ws.work,
            store=self.store,
            db=self.db,
            tenant_id=self.cfg.tenant_id,
            logger=self.log,
            resumed=self._restored_from is not None,
            # The workspace is memory-backed; see `stage_inputs` for why the
            # artifact cap is the right bound on what one attempt may stage.
            max_total_bytes=self.cfg.max_artifact_bytes,
            # Derived from the workspace rather than spelled out, so a new
            # control file added to `Workspace` cannot be silently stageable
            # over -- `control_file_names` finds every property it puts in
            # `work/`. `repo` and `.swarm` are the worker's OWN directories
            # inside `work/`, not the workspace's, so they are named here.
            reserved=frozenset({REPO_DIR_NAME, WORKER_STATE_DIR})
            | ws.control_file_names(),
        )
        self._staged_inputs = staged
        self.log.info(
            "declared inputs staged",
            count=len(staged),
            files=[item.path for item in staged],
        )
        return staged

    def _clone_base_path(self) -> Path | None:
        ws = self.ws
        if ws is None:
            return None
        return ws.work / WORKER_STATE_DIR / CLONE_BASE_FILE

    def _write_clone_base(self, commit: str | None) -> None:
        path = self._clone_base_path()
        if path is None or not commit:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(commit.strip() + "\n")
        except OSError as exc:
            # Losing the marker costs the harvest its diff base on a RESUMED
            # attempt only; the first attempt still has `clone.commit` in
            # memory. Not worth failing a run that is otherwise fine.
            self.log.warning("could not record the clone base", error=str(exc))

    def _read_clone_base(self) -> str | None:
        path = self._clone_base_path()
        if path is None or not path.exists():
            return None
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            return None
        return text or None

    def _git_token(self) -> str | None:
        """The TENANT's own clone token, or None.

        Read from `swarm-tenant-<tenant>-git` through the same per-tenant Secret
        Manager path as a provider key, never from a platform-wide `GIT_TOKEN`
        in the worker's environment: one token able to clone every tenant's
        repositories would make a single malicious repository in one tenant a
        credential compromise for all of them (invariant 9).

        None is not an error. A public repository clones without a credential
        and a private one fails with git's own message, which is the correct
        diagnosis to surface.
        """
        if self.secret_client is None:
            return None
        try:
            tenant = load_tenant(self.db, self.cfg.tenant_id)
            return resolve_git_token(
                tenant=tenant, client=self.secret_client, logger=self.log
            )
        except SecretError as exc:
            self.log.warning(
                "no usable tenant git credential; cloning unauthenticated",
                error=str(exc),
            )
            return None

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
            # The ceilings a runner may apply to ITS OWN child (the agent CLI,
            # the catalogue command). `runners/limits.py` clamps whatever the
            # caller's `input` asks for against these, so `input` can lower a
            # limit and never raise one -- which is invariant 10 for the one
            # execution parameter a caller is allowed to influence at all.
            TIMEOUT_ENV: str(self.cfg.timeout_seconds),
            GRACE_ENV: str(self.cfg.termination_grace_seconds),
            STDOUT_ENV: str(self.cfg.max_stdout_bytes),
            STDERR_ENV: str(self.cfg.max_stderr_bytes),
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
            # THE POOL FIRST, THE TENANT SECRET AS THE FALLBACK, and never the
            # other way round. An assigned account is a subscription chosen for
            # its headroom; the tenant secret is the one credential every agent
            # of this tenant shares. Falling back to it is correct when there is
            # no pool and wrong whenever there is one, so the order is fixed.
            account_env = self._pool_credential_env(profile)
            if account_env is not None:
                base.update(account_env)
            else:
                tenant = load_tenant(self.db, self.cfg.tenant_id)
                resolved = resolve_credentials(
                    tenant=tenant,
                    provider=profile.provider,
                    secret_env_names=profile.secrets,
                    any_of=profile.secrets_any_of,
                    client=self.secret_client,
                    logger=self.log,
                )
                base.update(resolved.env)
        return ws.child_env(base)

    # -- the account pool --------------------------------------------------
    def _pool_credential_env(self, profile: Any) -> dict[str, str] | None:
        """The child env an assigned account supplies, or None for the tenant secret.

        THE LOOP IS THE POINT. `choose()` is deterministic, so an account this
        worker cannot read is one it would be handed again on every retry --
        and the three real ways that happens (a freshly onboarded account whose
        secret has no version yet, an account lent by a tenant that never
        granted this worker's service account access to it, an empty version)
        are all invisible to the broker, which knows about Firestore documents
        and not about IAM. So: hand the unusable one back, say WHY, ask again
        excluding it, and park if nothing works.

        Parking rather than failing, because none of this is the task's fault
        and an unusable pool must cost nothing. Failing would burn all three
        attempts on the same account in a row.
        """
        for _ in range(MAX_ACCOUNT_TRIES):
            account = self._lease_account(profile)
            if account is None:
                return None
            try:
                return self._account_credential_env(profile, account)
            except AccountUnreadable as exc:
                self._reject_account(account, exc)
        raise NoAccountAvailable(
            NoAccount(reason=ACCOUNT_UNREADABLE),
            profile.provider or "unknown",
        )

    def _lease_account(self, profile: Any) -> Assignment | None:
        """The account this attempt runs on, or None to use the tenant secret.

        None is returned -- rather than raised -- for every case that means
        "the pool is not how this runs":

          * no broker is configured, which is every deployment that has not
            adopted the pool;
          * the profile does not declare `CLAUDE_CODE_OAUTH_TOKEN`, so it wants
            a metered API key and an account has none to give;
          * the broker is unreachable or answers unusably, which must degrade
            to the behaviour that worked yesterday rather than fail a task;
          * the tenant has no account registered at all.

        `NoAccountAvailable` is raised for the cases that are genuinely a wait
        or genuinely broken: accounts exist and every one is spent, paused,
        draining or unobserved; or the broker REFUSED this worker. The caller
        parks in both cases -- an attempt is never failed over the pool.
        """
        if self._account is not None:
            # Already held. Re-reading it is what the credential-reload path
            # wants; re-ASSIGNING would double-count this agent on the pool and
            # could move it onto a different subscription halfway through a run.
            return self._account
        if self._account_declined is not None:
            # Decided once, at step 6, and not revisited. See the field.
            return None
        if self._account_broker is None:
            return self._decline_pool("no_broker_configured")
        if ACCOUNT_TOKEN_ENV not in (profile.secrets or ()):
            self.log.info(
                "runner profile does not accept a subscription token; using the "
                "tenant credential rather than the account pool",
                runner_profile=profile.name,
                declared=sorted(profile.secrets or ()),
            )
            return self._decline_pool("profile_takes_no_subscription")

        provider = profile.provider
        try:
            outcome = self._account_broker.assign(
                provider, exclude=tuple(self._account_rejected)
            )
        except BrokerRefused as exc:
            # REFUSE, DO NOT CARRY ON. A 401/403/404 means the pool is
            # configured and this worker may not use it -- almost always a
            # missing `run.invoker` grant for this tenant's worker service
            # account, or a URL that is not the broker. Falling back here would
            # put every agent back on the one shared tenant subscription while
            # the pool's own dashboards showed it healthy and idle, which is
            # the failure nobody would ever find. Parking costs nothing and
            # puts the cause in the task's own blocked_by.
            self.log.error(
                "the quota broker refused this worker; the account pool is "
                "configured and this tenant cannot use it",
                provider=provider,
                error=str(exc),
            )
            raise NoAccountAvailable(
                NoAccount(reason=BROKER_REFUSED), provider or "unknown"
            ) from None
        except BrokerUnavailable as exc:
            # DEGRADE, DO NOT FAIL. The broker being down is not the task's
            # fault and the tenant secret still works; turning an outage in a
            # control-plane service into failed attempts across every tenant is
            # a much larger incident than the one that started it.
            self.log.warning(
                "could not reach the quota broker for an account; falling back "
                "to the tenant credential",
                provider=provider,
                error=str(exc),
            )
            return self._decline_pool("broker_unreachable")
        except Exception as exc:  # pragma: no cover - defence in depth
            self.log.warning(
                "the account pool client raised; falling back to the tenant credential",
                provider=provider,
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._decline_pool("broker_client_error")

        if isinstance(outcome, NoAccount):
            if outcome.is_pool_absent:
                self.log.info(
                    "no account is registered for this tenant; using the tenant credential",
                    provider=provider,
                )
                return self._decline_pool("no_accounts_registered")
            raise NoAccountAvailable(outcome, provider or "unknown")

        self._account = outcome
        self._account_released = False
        self.log.info(
            "account assigned from the pool",
            account_id=outcome.account_id,
            provider=provider,
            # Whether this tenant borrowed it. Worth a field of its own: a
            # deployment leaning on borrowed capacity looks healthy right up to
            # the day the lender revokes the loan.
            borrowed=bool(outcome.owner_tenant and outcome.owner_tenant != self.cfg.tenant_id),
        )
        self.control.emit(
            EventType.RUNNING,
            {
                "cause": "account_assigned",
                "account_id": outcome.account_id,
                "provider": provider,
            },
        )
        return outcome

    def _decline_pool(self, cause: str) -> None:
        """Record that this attempt is NOT on the pool, once and for good.

        Returns None so call sites can `return self._decline_pool(...)`. The
        stickiness is the behaviour: `_build_child_env` runs again whenever a
        credential is reloaded mid-attempt, and a second ask there could move a
        running agent onto a pool account it did not start on, or raise from a
        call site whose job is to restart the child rather than to park.
        """
        self._account_declined = cause
        return None

    def _account_credential_env(self, profile: Any, account: Assignment) -> dict[str, str]:
        """Read the ASSIGNED account's secret and shape it for the child.

        `{base}` holds only the access token -- the `{base}-refresh` half that
        can mint successors is readable by the broker alone -- so what lands in
        the child's environment expires on its own. Read on EVERY call, which
        is what makes the credential-reload path work: `access()` always takes
        `latest`, so a token the refresher rotated under a running agent is
        picked up by re-reading the same name.

        EVERY WAY THIS CAN FAIL BECOMES `AccountUnreadable`, including the ones
        that arrive as a raw google-cloud exception. `access()` raises NotFound
        for a secret with no version -- which is exactly the state
        `scripts/account.sh` leaves a freshly added account in until the
        broker's next sweep publishes the access token -- and PermissionDenied
        for an account lent by a tenant that never granted this worker's
        service account `secretmanager.secretAccessor` on it. Both used to
        travel straight out of here into `run()`'s generic handler and fail the
        attempt; both are "this account, not this task", so the caller hands it
        back and asks for another.
        """
        assert self.secret_client is not None
        try:
            payload = self.secret_client.access(account.secret)
            env = credential_env_from_account(
                payload,
                secret_env_names=profile.secrets,
                secret_name=account.secret,
            )
        except Exception as exc:
            raise AccountUnreadable(
                account.account_id, f"{type(exc).__name__}: {exc}"
            ) from None
        for value in env.values():
            self.log.register_secret(value)
        self.log.info(
            "account credentials resolved",
            account_id=account.account_id,
            secret=account.secret,
            variables=sorted(env),
        )
        return env

    def _reject_account(self, account: Assignment, exc: AccountUnreadable) -> None:
        """Hand back an account this worker cannot read, and remember not to ask for it.

        Three things, and all three matter. The hold goes back, or the pool
        counts an agent that never started. The id joins `exclude`, or the next
        ask returns the same account, because `choose()` is deterministic. And
        the reason travels with the release, so the broker can stop offering
        this account to THIS tenant for a while and an operator can see the
        difference between an account nobody wants and an account nobody can
        read.
        """
        self.log.error(
            "the assigned account cannot be read; handing it back and asking "
            "for another",
            account_id=account.account_id,
            secret=account.secret,
            error=exc.detail,
        )
        self.control.emit(
            EventType.RETRYING,
            {
                "cause": "account_unreadable",
                "account_id": account.account_id,
                "provider": self.cfg.provider,
            },
        )
        self._account_rejected.append(account.account_id)
        self._account = None
        self._account_released = False
        self._give_back(account, unusable=exc.detail)

    def _give_back(self, account: Assignment, *, unusable: str = "") -> None:
        """One release call, and it never raises.

        A failed release costs one over-counted hold until it EXPIRES. It is an
        expiry and not a reconciler: `apps/reconciler/` has no account code at
        all, and a comment here used to claim it did -- which is worse than no
        comment, because the next person reads it as a reason not to build the
        backstop. The real one is in the broker: every hold carries a deadline
        and the quota sweep prunes the expired ones, so a worker that is
        SIGKILLed, OOM-killed or preempted costs a few stale minutes on one
        account's counter rather than a slot that never comes back.
        """
        if self._account_broker is None:
            return
        try:
            self._account_broker.release(
                account.account_id, account.assignment_id, unusable=unusable
            )
            self.log.info(
                "account released",
                account_id=account.account_id,
                unusable=bool(unusable),
            )
        except Exception as exc:
            self.log.warning(
                "could not release the account; its hold expires on its own and "
                "the broker's quota sweep prunes it",
                account_id=account.account_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _release_account(self) -> None:
        """Give the account back, on whatever path this attempt is leaving by.

        CALLED FROM `_cleanup`, which is the `finally` of `run()`, because that
        is the ONE place every exit runs through: success, failure, a park on
        quota or on a missing credential, a cancellation, a crash, and a
        mid-run fencing exit. Releasing at each of those sites instead would
        mean a new exit path is a leaked assignment, discovered weeks later as
        an account that `choose()` has quietly stopped picking.

        Never raises. An exception here would replace the attempt's real
        outcome with this one.
        """
        account, self._account = self._account, None
        if account is None or self._account_released or self._account_broker is None:
            return
        self._account_released = True
        self._give_back(account)

    def _park_no_account(self, exc: NoAccountAvailable) -> Outcome:
        """The pool cannot serve this attempt. Park -- it costs nothing.

        NOT A FAILURE, for any of the reasons that land here. The tenant did
        nothing wrong, the credential is fine, and one of the three attempts
        must not be spent on a queue -- nor on a missing IAM grant, which no
        number of retries will produce. Parking gives the slot and the memory
        back and the scheduler returns the task by itself.

        THE WAIT DEPENDS ON WHAT IS ACTUALLY BEING WAITED FOR, and the reasons
        are not interchangeable:

          * a reset instant from the broker's `Account.next_reset` -- wake
            exactly then, rather than polling;
          * `no_recent_reading` -- nothing says the accounts are spent, only
            that nobody has looked lately, and the broker's usage poll looks
            every sweep. Minutes, not a quarter of an hour;
          * `pool_paused` -- waiting on a PERSON. There is no instant to wake
            at, so the long fallback;
          * `broker_refused` / `account_unreadable` -- a configuration error.
            It parks as CREDENTIAL_MISSING rather than as quota, because the
            honest summary is "an admin has to fix something", not "come back
            when there is room".
        """
        reason = exc.decision.reason
        configuration_error = reason in (BROKER_REFUSED, ACCOUNT_UNREADABLE)
        fallback_seconds = (
            STALE_READING_RETRY_SECONDS
            if reason == NO_RECENT_READING
            else NO_ACCOUNT_RETRY_SECONDS
        )
        next_eligible = _parse_iso(exc.decision.next_reset_at) or (
            utcnow() + timedelta(seconds=fallback_seconds)
        )
        self.log.warning(
            "the account pool cannot serve this attempt; parking",
            provider=exc.provider,
            reason=reason,
            waiting_on=(
                "an administrator" if configuration_error
                else "a person" if reason == POOL_PAUSED
                else "the broker's next usage poll" if reason == NO_RECENT_READING
                else "a quota window"
            ),
            next_eligible_at=next_eligible.isoformat(),
        )
        self._checkpoint("no-account")
        summary = self._upload_outputs()
        self._export_metrics()
        detail = {
            "provider": exc.provider,
            "account_pool_reason": exc.decision.reason,
            **summary,
        }
        self.control.emit(
            EventType.QUOTA_EXHAUSTED,
            {**detail, "next_eligible_at": next_eligible, "park_phase": "account_assign"},
        )
        self.control.park(
            # PROVIDER_QUOTA_EXHAUSTED when the pool will recover on its own:
            # the tenant HAS credentials, they are simply all spent or all
            # unobserved, and sending an operator to look at that wastes their
            # time. CREDENTIAL_MISSING when it will NOT recover on its own --
            # a broker that refused this worker, or an account whose secret
            # nobody granted it access to, is a configuration error, and that
            # ParkReason is the one that means "an admin must act".
            #
            # A ParkReason naming the pool would be more honest than either;
            # ParkReason is in the frozen contract, so that is a request in the
            # report, not a change.
            reason=(
                ParkReason.CREDENTIAL_MISSING
                if configuration_error
                else ParkReason.PROVIDER_QUOTA_EXHAUSTED
            ),
            next_eligible_at=next_eligible,
            detail={**detail, "park_phase": "account_assign"},
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

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
    def _scrub(self, value: Any) -> Any:
        """Redact every registered secret from a value bound for Firestore."""
        return self.log.scrub_value(value)

    def _redact_before_upload(self) -> list[dict[str, Any]]:
        """Scrub the captured streams and artifacts before they leave the pod.

        A log line is only one of four ways a provider key gets out. The other
        three are `stdout.log` and `stderr.log`, the runner's artifacts, and the
        result summary -- and the first two are uploaded to GCS, where they
        outlive the pod. The logger holds the registered values, so it does the
        rewriting; binary and oversized files are left alone by the rewrite,
        because corrupting a tenant's artifact to protect a key that is probably
        not in it is the wrong trade.

        THAT TRADE IS UNCHANGED; SAYING NOTHING ABOUT IT IS WHAT CHANGED
        (docs/audits/2026-09-18/02-agent-worker-credentials.md section 4). The
        rewrite's answer used to be discarded, so an artifact the rewrite
        skipped went to GCS with no signal at all. Now a skipped file's raw
        bytes are scanned for the registered values -- which answers whether
        "probably not in it" held -- and it is returned for the result summary
        unless the scan found it clean:
        `{"file", "reason", "bytes", "secret_found"}`, where `secret_found` is
        True (it IS in there, and the file is uploaded as-is) or None (the file
        could not be read, so nobody knows).

        Returns only those entries. A skipped file whose bytes hold no
        registered value is as clean as a rewritten one, and listing every PNG
        an agent writes would bury the entry that matters.
        """
        ws = self.ws
        if ws is None or not self.log.has_secrets:
            return []
        targets = [ws.stdout_path, ws.stderr_path]
        targets += [
            path
            for path in sorted(ws.artifacts.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]
        targets.append(ws.result_path)
        unredacted: list[dict[str, Any]] = []
        for path in targets:
            try:
                outcome = self.log.scrub_file_outcome(path, max_bytes=redact_mod.MAX_SCRUB_BYTES)
            except OSError as exc:  # a read-only or vanished file must not fail the attempt
                self.log.warning(
                    "could not redact a file before upload", path=str(path), error=str(exc)
                )
                continue
            if not outcome.skipped:
                continue
            found = self.log.file_contains_secret(path)
            try:
                size: int | None = path.stat().st_size
            except OSError:
                size = None
            label = _workspace_label(ws, path)
            if found is False:
                self.log.info(
                    "file not rewritten, and its raw bytes hold no registered secret",
                    file=label,
                    reason=outcome.value,
                    bytes=size,
                )
                continue
            entry = {"file": label, "reason": outcome.value, "bytes": size, "secret_found": found}
            unredacted.append(entry)
            if found:
                self.log.error(
                    "A FILE THAT COULD NOT BE REDACTED CONTAINS A REGISTERED SECRET; "
                    "it is uploaded as-is",
                    **entry,
                )
            else:
                self.log.warning(
                    "a file could be neither redacted nor scanned; it is uploaded unexamined",
                    **entry,
                )
        return unredacted

    # -- live logs ----------------------------------------------------------
    def _publish_live_logs(self) -> None:
        """Publish a bounded, scrubbed TAIL of each stream while the agent runs.

        WHY A TAIL AND NOT THE FILE. GCS has no append. Publishing the whole
        stream would rewrite up to `max_stdout_bytes` every interval, so the
        cost of watching a run would grow with the length of the run -- the
        opposite of what a tail is for. A fixed window keeps it flat.

        WHY IT IS SCRUBBED HERE TOO. `_redact_before_upload` runs once, on the
        way out. Anything published DURING the run has not been through it, so
        a provider key echoed into stdout would reach GCS in the clear and stay
        there: the object is overwritten by the next flush, but "it is gone
        five seconds later" is not a property anyone should rely on for a
        credential. The tail is scrubbed on every flush instead.

        It NEVER raises. A failed flush costs the watcher five seconds of
        staleness; failing the attempt over it would trade a running agent for
        a cosmetic feature.
        """
        ws = self.ws
        cfg = self.cfg
        if ws is None or not cfg.live_logs_enabled:
            return
        for label, path in (("stdout", ws.stdout_path), ("stderr", ws.stderr_path)):
            try:
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size == 0:
                    continue
                with path.open("rb") as handle:
                    if size > cfg.live_log_tail_bytes:
                        handle.seek(size - cfg.live_log_tail_bytes)
                    chunk = handle.read()
                text = chunk.decode("utf-8", errors="replace")
                if self.log.has_secrets:
                    text = self.log.scrub_text(text)
                # The byte offset the window starts at, so a reader can tell a
                # gap (it polled too slowly and the window moved past what it
                # had) from a continuation. Without it a tailer silently
                # stitches two non-adjacent pieces of output together.
                header = f"#swarm-tail offset={max(0, size - len(chunk))} size={size}\n"
                self.store.upload_bytes(
                    f"{cfg.log_prefix}/live/{label}.tail.log",
                    (header + text).encode("utf-8"),
                    content_type="text/plain; charset=utf-8",
                )
            except Exception as exc:  # pragma: no cover - never fail a run for this
                self.log.debug(
                    "could not publish a live log tail", stream=label, error=str(exc)
                )

    def _dispatch_block(self) -> dict[str, Any]:
        """The `task.metadata["dispatch"]` block, or an empty one.

        swarm-api writes four fields here -- strategy, carrier, role and
        integrates (see DispatchOptions.to_metadata). For a long time this
        worker read only the first, so `integrate` took the `direct-pr` path
        and every step of a workflow opened its own pull request against an
        API that had promised, in three places, to open exactly one.
        """
        task = self._task or {}
        metadata = task.get("metadata")
        if not isinstance(metadata, dict):
            return {}
        dispatch = metadata.get("dispatch")
        return dispatch if isinstance(dispatch, dict) else {}

    def _dispatch_strategy(self) -> str:
        """How the caller asked for this work to be merged. Defaults to collect.

        UNKNOWN VALUES FALL BACK TO `collect`, deliberately. swarm-api validates
        the field, so an unrecognised one here means a newer control plane and
        an older worker -- and in that disagreement the safe reading is the one
        that pushes nothing. A worker that guessed "probably direct-pr" would
        open pull requests a caller never asked for.
        """
        value = self._dispatch_block().get("strategy")
        if not isinstance(value, str):
            return "collect"
        value = value.strip().lower()
        return value if value in ("collect", "direct-pr", "integrate") else "collect"

    def _dispatch_role(self) -> str:
        """This task's part in an `integrate` workflow.

        Only `integrate` assigns roles; every other strategy leaves the field
        absent and every step behaves identically. An UNRECOGNISED role reads as
        `contributor` rather than `integrator`, matching the same
        newer-control-plane-older-worker reasoning as the strategy above: a
        contributor pushes a branch and opens nothing, which is the outcome
        that cannot surprise a caller.
        """
        value = self._dispatch_block().get("role")
        if not isinstance(value, str):
            return ""
        value = value.strip().lower()
        return value if value in ("contributor", "integrator") else "contributor"

    def _dispatch_integrates(self) -> list[str]:
        """The upstream task ids this integrator must bring together.

        Order is preserved: swarm-api builds it from the workflow's topological
        prefix, so it is the order the patches have to be applied in.
        """
        raw = self._dispatch_block().get("integrates")
        if not isinstance(raw, list):
            return []
        return [t.strip() for t in raw if isinstance(t, str) and t.strip()]

    def _dispatch_carrier(self) -> str:
        """Where a step's work is kept for the next step. Defaults to checkpoints.

        THE VOCABULARY IS swarm-api's, and it is written out here rather than
        imported because the worker must not pull the control plane into the
        image every agent runs in. That copy had already drifted: this accepted
        `("patches", "branches")` and fell back to `"patches"`, a word
        `DISPATCH_CARRIERS` has never contained and the validator REFUSES at
        submission. The real default block says `carrier: "checkpoints"`, which
        this read as unrecognised -- so the two ends agreed on one word out of
        three, and never on the one almost every dispatch carries.

        It had broken nothing only because no decision in this worker branches
        on the carrier yet. The first one that did would have read "patches"
        for a dispatch that said "checkpoints".

        `tests/unit/worker/test_dispatch_contract_parity.py` imports swarm-api's
        own constants and asserts this set matches them, so the next divergence
        fails there instead of in a deployed worker.
        """
        value = self._dispatch_block().get("carrier")
        if not isinstance(value, str):
            return "checkpoints"
        value = value.strip().lower()
        return value if value in ("checkpoints", "branches") else "checkpoints"

    def _credential_refusal(self) -> dict[str, Any] | None:
        """The runner's `credential.json`, if it wrote one.

        Deliberately separate from the quota signal. The two arrive from the
        same provider over the same connection and mean opposite things: one
        says wait, the other says this will never work again.
        """
        ws = self.ws
        if ws is None or not ws.credential_path.exists():
            return None
        data = _read_json(ws.credential_path)
        return data if isinstance(data, dict) else {}

    # -- git: harvest, and publish when the forge allows it -----------------
    def _harvest_git(self, *, publish: bool) -> dict[str, Any] | None:
        """Describe the agent's changes, and publish them when permitted.

        Returns the dict that becomes `result_summary["git"]`, or None when
        there is no repository to describe. It NEVER raises: an attempt that
        ran is not a failed attempt because its epilogue could not talk to
        GitHub, and this runs on the teardown path where the lease is about to
        be released either way. Every failure becomes a readable string in the
        returned dict instead.

        `publish` is False on all three park paths. A parked attempt resumes
        from its checkpoint and will reach the terminal path later, so pushing
        a branch and opening a pull request for work that is still in progress
        would put a half-finished change in front of a reviewer -- and do it
        again on every quota bounce.
        """
        ws = self.ws
        cfg = self.cfg
        if ws is None or not cfg.git_harvest_enabled:
            return None
        repo = ws.work / REPO_DIR_NAME
        if not (repo / ".git").exists():
            return None

        base = self._clone_base or self._read_clone_base()
        out: dict[str, Any] = {"base": base}
        if base is None:
            out["note"] = (
                "the clone base is unknown, so no diff could be computed; "
                "uncommitted files are still listed"
            )

        try:
            work = summarize_work(
                repo=repo,
                base=base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                patch_path=ws.artifacts / PATCH_NAME,
                max_patch_bytes=cfg.max_patch_bytes,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )
        except GitError as exc:
            self.log.warning("could not harvest the agent's git changes", error=str(exc))
            out["error"] = self._scrub(str(exc)[:500])
            return out

        out.update(
            {
                "head": work.head,
                "commits": [
                    {
                        "sha": c.sha,
                        "subject": self._scrub(c.subject[:200]),
                        "author": c.author,
                        "committed_at": c.committed_at,
                        "files_changed": c.files_changed,
                        "insertions": c.insertions,
                        "deletions": c.deletions,
                        "binary_files": c.binary_files,
                    }
                    for c in work.commits[:100]
                ],
                "commit_count": len(work.commits),
                "insertions": work.insertions,
                "deletions": work.deletions,
                "dirty": [self._scrub(path) for path in work.dirty],
                "dirty_count": len(work.dirty),
                "dirty_truncated": work.dirty_truncated,
                "patch": work.patch_name,
                "patch_bytes": work.patch_bytes,
                "patch_omitted": work.patch_omitted,
            }
        )
        if work.patch_omitted:
            out["patch_note"] = (
                f"the diff was {work.patch_bytes} bytes, over the "
                f"{cfg.max_patch_bytes} cap, and was discarded rather than "
                "truncated -- a truncated patch applies cleanly and silently "
                "drops the rest of the change"
            )

        # AN INTEGRATOR IS THE ONE STEP WHOSE DELIVERABLE IS NOT ITS OWN WORK.
        #
        # For every other step "changed nothing" means there is nothing to
        # push and nothing to open, and returning here is right. For an
        # integrator it meant the opposite of what the caller was promised: a
        # step whose agent edited no files -- an entirely ordinary outcome for
        # one whose prompt is "bring these together" -- returned before
        # `_publish_git` ran, so no contributor branch was ever merged, no
        # branch was pushed, and the ONE pull request `integrate` promises was
        # never opened. Zero, for a workflow whose contributors had all already
        # run, been billed and pushed their branches.
        #
        # It was invisible because it depended on whether the integrator's
        # agent happened to touch a file. Found by running the strategy against
        # a real repository, not by reading it.
        if work.is_empty and not self._integration_is_pending():
            out["published"] = False
            out["publish_reason"] = "the agent changed nothing in the repository"
            return out

        out.update(self._publish_git(repo=repo, work_head=work.head, publish=publish))
        return out

    def _integration_is_pending(self) -> bool:
        """True when this step still owes a merge even having changed nothing.

        Narrow on purpose: `integrate` AND the integrator role AND at least one
        upstream task to merge. A single-step `integrate` workflow that changed
        nothing has genuinely nothing to publish and keeps the short circuit
        above, and no other strategy is affected at all.
        """
        return (
            self._dispatch_strategy() == "integrate"
            and self._dispatch_role() == "integrator"
            and bool(self._dispatch_integrates())
        )

    def _publish_git(
        self, *, repo: Path, work_head: str | None, publish: bool
    ) -> dict[str, Any]:
        """The push-and-pull-request half. Gated on the forge, not on hope."""
        cfg = self.cfg
        ws = self.ws
        assert ws is not None

        if not publish:
            return {
                "published": False,
                "publish_reason": "this attempt parked; publishing waits for the run to finish",
            }
        if not cfg.git_publish_enabled:
            return {"published": False, "publish_reason": "publishing is disabled for this worker"}

        # THE CALLER'S STRATEGY IS A PROMISE AND THIS IS WHERE IT IS KEPT.
        #
        # swarm-api accepts `strategy` on submit and returns it in the 201, and
        # `collect` -- the default every caller gets -- states that patches are
        # harvested and NOTHING IS PUSHED. Until this check existed that
        # guarantee held only because the tenant's token happened to lack write
        # scope: granting write scope would have made every `collect` dispatch
        # start opening pull requests while its own API response promised it
        # would not. A guarantee enforced by an unrelated accident is not a
        # guarantee.
        #
        # Read from metadata rather than a typed field because adding one to
        # Task would be a frozen-contract change; the request to type it is
        # recorded in docs/contract-change-requests.md.
        strategy = self._dispatch_strategy()
        if strategy == "collect":
            return {
                "strategy": strategy,
                "published": False,
                "publish_reason": (
                    "strategy is 'collect': the patch is harvested and nothing "
                    "is pushed. Submit with strategy 'direct-pr' to open a pull "
                    "request from this agent"
                ),
            }

        url = self._repo_url or cfg.repository_url
        if not url:
            return {"published": False, "publish_reason": "the repository URL is unknown"}

        token = self._git_token()
        try:
            access = probe_repository(url=url, token=token)
        except ForgeError as exc:
            return {
                "published": False,
                "publish_reason": f"could not reach the forge: {self._scrub(str(exc)[:300])}",
            }
        if access is None:
            return {
                "published": False,
                "publish_reason": "this repository is not on a forge this worker can publish to",
            }

        out: dict[str, Any] = {
            "repository": access.ref.full_name,
            "default_branch": access.default_branch or None,
            "can_push": access.can_push,
        }
        if not access.can_push:
            # THE EXPECTED PATH TODAY, and the reason it reads as a fact rather
            # than a failure. The tenant's secret holds a clone token; the forge
            # was asked and said no. The patch above is the deliverable.
            out["published"] = False
            out["publish_reason"] = access.reason
            self.log.info("not publishing: no write permission", reason=access.reason)
            return out

        if not token:  # pragma: no cover - can_push implies a token
            out["published"] = False
            out["publish_reason"] = "no credential"
            return out

        branch = f"{cfg.git_branch_prefix}{cfg.task_id}"
        protected = (access.default_branch,) if access.default_branch else ()

        role = self._dispatch_role() if strategy == "integrate" else ""
        out["role"] = role or None

        auto_committed = False
        merge: MergeOutcome | None = None
        try:
            new_sha = commit_dirty(
                repo=repo,
                message=(
                    f"swarm: uncommitted changes from {cfg.task_id}\n\n"
                    "Staged by the worker at the end of the attempt so that work "
                    "the agent edited but did not commit is not lost between the "
                    "workspace and this branch."
                ),
                author_name=cfg.git_author_name,
                author_email=cfg.git_author_email,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )
            if new_sha:
                auto_committed = True
                work_head = new_sha

            # THE INTEGRATOR MERGES BEFORE IT PUSHES.
            #
            # Its own commit has to be in the tree first (above), and every
            # contributor's branch has to be in it before the push, or the
            # single pull request this strategy promises would contain only
            # the integrator's own step.
            #
            # The branch names are DERIVED from the upstream task ids with the
            # same prefix the contributors pushed under -- never taken from
            # metadata as names -- so nothing in a task document can point this
            # at an arbitrary ref.
            if role == "integrator":
                upstream = [
                    f"{cfg.git_branch_prefix}{tid}" for tid in self._dispatch_integrates()
                ]
                if upstream:
                    merge = merge_branches(
                        repo=repo,
                        url=url,
                        branches=upstream,
                        token=token,
                        private_dir=ws.private,
                        logs_dir=ws.logs,
                        timeout_seconds=cfg.git_clone_timeout_seconds,
                        logger=self.log,
                        author_name=cfg.git_author_name,
                        author_email=cfg.git_author_email,
                        branch_prefix=cfg.git_branch_prefix,
                    )
                    out["integrated"] = {
                        "merged": list(merge.merged),
                        "conflicted": list(merge.conflicted),
                        "missing": list(merge.missing),
                        "complete": merge.complete,
                    }

            pushed = push_branch(
                repo=repo,
                url=url,
                branch=branch,
                token=token,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_clone_timeout_seconds,
                logger=self.log,
                branch_prefix=cfg.git_branch_prefix,
                protected=protected,
            )
        except GitError as exc:
            out["published"] = False
            out["auto_committed"] = auto_committed
            out["publish_reason"] = self._scrub(str(exc)[:500])
            self.log.warning("could not publish the branch", error=str(exc))
            return out

        out.update(
            {
                "branch": branch,
                "pushed_head": pushed or work_head,
                "auto_committed": auto_committed,
            }
        )

        # A CONTRIBUTOR PUSHES AND STOPS. This is the whole difference between
        # `integrate` and `direct-pr`, and its absence is what made them the
        # same run: every step used to reach the code below and open its own
        # pull request, so a six-step `integrate` workflow produced six of them
        # against an API whose schema, validator and docs all say it produces
        # ONE. The branch is the deliverable here; the integrator merges it.
        if role == "contributor":
            out["published"] = True
            out["publish_reason"] = (
                "strategy is 'integrate' and this step is a contributor: its "
                "branch was pushed and no pull request was opened. The "
                "integrator step merges this branch and opens the single pull "
                "request for the whole workflow."
            )
            return out

        if not access.default_branch:
            out["published"] = True
            out["publish_reason"] = (
                "the branch was pushed; no pull request was opened because the "
                "repository's default branch could not be read"
            )
            return out

        title = f"[swarm] {cfg.task_id}"
        body = self._pull_request_body(
            branch=branch, auto_committed=auto_committed, merge=merge
        )
        try:
            pr = open_pull_request(
                access=access,
                token=token,
                head=branch,
                base=access.default_branch,
                title=title,
                body=body,
            )
        except ForgeError as exc:
            out["published"] = True
            out["publish_reason"] = (
                f"the branch was pushed but no pull request was opened: "
                f"{self._scrub(str(exc)[:300])}"
            )
            return out

        out.update(
            {
                "published": True,
                "pull_request": {
                    "number": pr.number,
                    "url": pr.url,
                    "state": pr.state,
                    "created": pr.created,
                },
                "publish_reason": (
                    "opened" if pr.created else "an open pull request already existed and was reused"
                ),
            }
        )
        self.log.info("pull request ready", number=pr.number, url=pr.url, created=pr.created)
        return out

    def _pull_request_body(
        self,
        *,
        branch: str,
        auto_committed: bool,
        merge: "MergeOutcome | None" = None,
    ) -> str:
        """The body a reviewer reads. Provenance first, prompt second.

        The prompt is truncated hard and scrubbed. It is UNTRUSTED text that
        ends up rendered as markdown on a public-facing page, so the one thing
        it must not do is arrive at full length with whatever the submitter
        decided to put in it.
        """
        cfg = self.cfg
        lines = [
            "Opened by SwarmCloud. This branch is written only by this task.",
            "",
            f"- task: `{cfg.task_id}`",
            f"- attempt: `{cfg.attempt_id}`",
            f"- tenant: `{cfg.tenant_id}`",
            f"- runner profile: `{cfg.profile.name}`",
            f"- branch: `{branch}`",
        ]
        if self._clone_base:
            lines.append(f"- base commit: `{self._clone_base}`")
        if self._last_checkpoint is not None:
            lines.append(f"- checkpoint: `{self._last_checkpoint.uri}`")
        if auto_committed:
            lines += [
                "",
                "One commit on this branch was made by the worker, not the agent: "
                "the agent left changes uncommitted and they would otherwise not "
                "have reached this branch at all.",
            ]

        # WHAT THIS PULL REQUEST DOES NOT CONTAIN, stated on the pull request
        # rather than only in the run result. An integrator takes what merges
        # and names what it could not take; a reviewer who cannot see that
        # would read a partial integration as a complete one, which is the
        # single most misleading thing this page could do.
        if merge is not None:
            lines += ["", f"Integrates {len(merge.merged)} contributor branch(es):"]
            lines += [f"- merged: `{b}`" for b in merge.merged] or ["- none"]
            if merge.conflicted:
                lines += [
                    "",
                    "**NOT included -- these branches conflict and were left out. "
                    "This pull request is an INCOMPLETE integration of the "
                    "workflow:**",
                ]
                lines += [f"- conflicted: `{b}`" for b in merge.conflicted]
            if merge.missing:
                lines += [
                    "",
                    "**NOT included -- these branches were not found on the "
                    "remote. The step either failed before pushing or never "
                    "ran:**",
                ]
                lines += [f"- missing: `{b}`" for b in merge.missing]

        return "\n".join(lines)

    def _upload_outputs(self, *, publish: bool = False) -> dict[str, Any]:
        ws = self.ws
        if ws is None:
            return {}
        # SPEND FIRST. Every exit that writes a terminal or parked state passes
        # through here before it does -- the clean finish, the three parks, the
        # cancellation, the SIGTERM and the crash handler -- so this is where
        # the attempt's spend lands ahead of its state, whichever way it is
        # leaving. `_cleanup` records again for the two cases that cannot be
        # here yet: a runner still alive when the worker crashed, and a
        # mid-run fence, which uploads nothing.
        self._record_spend()
        # BEFORE the redaction pass, not after: the harvest writes a patch into
        # `artifacts/`, and `_redact_before_upload` is what scrubs everything
        # in there. A patch produced afterwards would be the one file in the
        # upload that never had a provider key taken out of it.
        try:
            git_summary = self._harvest_git(publish=publish)
        except Exception as exc:  # pragma: no cover - defensive
            self.log.exception("the git harvest raised; continuing without it", exc)
            git_summary = {"error": "the git harvest failed unexpectedly"}
        unredacted = self._redact_before_upload()
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
        if git_summary is not None:
            summary["git"] = git_summary
        if skipped:
            summary["artifacts_skipped"] = skipped[:50]
        if unredacted:
            # Files that left the pod without being rewritten AND without a
            # clean scan. Capped like the list above; the log carries them all.
            summary["redaction_skipped"] = unredacted[:50]
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
        if self._staged_inputs:
            summary["staged_inputs"] = [item.as_dict() for item in self._staged_inputs]
        # This dict becomes `task.result_summary`, a Firestore document that
        # every reader of the task can see. It is scrubbed on the way out for
        # the same reason the files above are.
        return self._scrub(summary)

    # -- spend -------------------------------------------------------------
    def _child_ended(self) -> None:
        """Everything that has to happen the moment a runner is gone.

        Called at every site that reaps a runner, so a new site gets both
        halves by calling one thing: the resource sample is stopped and
        written, and the run's spend is taken before a restart can overwrite
        the result.json it is in.
        """
        self._stop_sampler()
        self._collect_spend()

    def _collect_spend(self) -> None:
        """Add the runner that just ended to this attempt's spend. Never raises.

        ONCE PER RUNNER: `_spend_pending` is set when a runner starts and
        cleared here, so the `_cleanup` backstop cannot count a run twice.

        Reads result.json knowing it is THIS runner's or nothing: the reply
        files are cleared before every start (`_run_child_supervised`), so a
        runner killed before writing leaves no file here rather than a
        restored one from a previous attempt.
        """
        if not self._spend_pending or self.ws is None:
            return
        self._spend_pending = False
        try:
            result = _read_json(self.ws.result_path) or {}
            usage = _usage_summary(result.get("output"))
        except Exception as exc:  # pragma: no cover - defensive; teardown path
            self.log.warning("could not read the runner's spend", error=str(exc))
            return
        if usage:
            self._spend = _add_spend(self._spend, usage)

    def _record_spend(self) -> None:
        """Write what this attempt has spent onto its attempt document. Never raises.

        Called from every exit (see `_upload_outputs` and `_cleanup`), and
        writes only when there is something new: `record_spend` merge-sets
        absolute totals, so writing the same figure twice is harmless and
        writing a larger one later -- a crash that reaped its runner after the
        first write -- corrects it.

        Not fatal. An attempt that ran is not a failed attempt because its
        accounting write failed, and this runs on the teardown path where the
        lease is about to be released either way.
        """
        if self._writes_forbidden or not self._spend or self._spend == self._spend_recorded:
            return
        snapshot = dict(self._spend)
        try:
            self.control.record_spend(snapshot)
        except Exception as exc:
            self.log.warning("could not record spend", error=f"{type(exc).__name__}: {exc}")
            return
        self._spend_recorded = snapshot

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
                state=state,
                exit_code=exit_code,
                error=self._scrub(error[:4000]),
                result_summary=summary,
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
        # THE SPEND BACKSTOP, after the runner is gone and before the workspace
        # holding its result.json is destroyed. Every orderly exit has already
        # recorded in `_upload_outputs`; this catches the two that cannot have:
        # a crash while a runner was still alive (it has only just been reaped,
        # above) and a mid-run fence, which uploads nothing. Writing a fenced
        # attempt's spend touches only its OWN attempt document -- never the
        # lease, which is what invariant 5 forbids -- exactly as the resource
        # usage write on that same path already does.
        self._collect_spend()
        self._record_spend()
        # AFTER the child is gone and BEFORE the workspace is destroyed. Giving
        # the account back while an agent could still be making calls on it
        # would let the broker hand the same subscription to another agent and
        # count one where there are two. This is the single release point for
        # every exit path -- see `_release_account`.
        self._release_account()
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


def _parse_iso(value: Any) -> datetime | None:
    """An ISO instant from the broker, or None.

    None on anything unparseable rather than an exception: this decides only
    WHEN a parked task wakes up, and a broker that grows a new timestamp format
    must not turn a free park into a failed attempt. The caller has a default.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else {"value": data}


#: The keys _usage_summary reads. Used to decide WHICH level of a possibly
#: nested result actually carries the numbers, rather than assuming one.
#:
#: Imported, not restated: the runner lifts exactly these out of a FAILED run
#: into result.json (runners/base.py), and a second copy here would be a key the
#: runner carries and this never reads.
_USAGE_KEYS = SPEND_KEYS

#: The fields of a usage summary that ADD UP across the runs of one attempt.
#: Everything `_usage_summary` emits except `models`, which is a set.
_SUMMED_SPEND = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "thinking_tokens",
    "total_cost_usd",
    "num_turns",
    "duration_ms",
    "duration_api_ms",
)


def _add_spend(total: dict[str, Any], more: dict[str, Any]) -> dict[str, Any]:
    """Two usage summaries as one: numbers summed, model lists unioned.

    A key absent from BOTH stays absent. "Not reported" is not zero (see
    `control.record_spend`), and a sum must not turn the one into the other.
    """
    out = dict(total)
    for key in _SUMMED_SPEND:
        if key in more:
            out[key] = out.get(key, 0) + more[key]
    if "total_cost_usd" in out:
        # Summing binary floats accumulates noise (0.1 + 0.2); no cost figure
        # means anything past the tenth decimal place of a dollar.
        out["total_cost_usd"] = round(out["total_cost_usd"], 10)
    models = set(out.get("models") or ()) | set(more.get("models") or ())
    if models:
        out["models"] = sorted(models)
    return out


def _workspace_label(ws: workspace_mod.Workspace, path: Path) -> str:
    """`artifacts/x.png`, `logs/stdout.log` -- a path a reader can place.

    Relative to the attempt's workspace, so the attempt id and the pod's
    filesystem layout stay out of a document every reader of the task sees.
    """
    try:
        return path.relative_to(ws.root).as_posix()
    except ValueError:
        return path.name


def _has_usage_keys(candidate: dict[str, Any]) -> bool:
    return any(key in candidate for key in _USAGE_KEYS)


def _usage_summary(output: Any) -> dict[str, Any]:
    """Token and cost numbers, pulled out of a CLI agent's result BEFORE truncation.

    `_truncate_json` replaces the whole result with a preview STRING once it
    exceeds its limit, so the runs that consumed the most tokens were exactly the
    runs whose token counts were discarded. The raw file still reaches GCS, but
    nothing queryable kept the numbers, and a per-tenant spend figure assembled by
    reading one GCS object per attempt does not survive real volume.

    This is deliberately a small, flat dict of scalars: it stays far below any
    truncation limit, so it survives whatever the rest of the result does.

    It does NOT add a field to `Attempt` -- `apps/common/swarm_common/` is frozen.
    These land inside the free-form runner summary. A typed field is a contract
    change request; see docs/contract-change-requests.md.
    """
    if not isinstance(output, dict):
        return {}

    # WHERE THE NUMBERS ACTUALLY ARE.
    #
    # A CLI runner does not return the agent's JSON. It returns its own
    # envelope -- {summary, provider, model, exit_code, structured_output,
    # limits, metrics} (runners/cliagent.py:342-357) -- and the CLI's own JSON,
    # which is where `usage`, `total_cost_usd`, `num_turns` and `modelUsage`
    # live, is nested one level down under `structured_output`.
    #
    # This function read only the top level, so on the production path it found
    # none of those keys and returned {} for every attempt -- while its unit
    # test, which feeds the raw CLI shape, passed. A test exercising a shape
    # production never produces is worse than no test: it reports a working
    # extractor when there is none. tests/unit/worker/test_usage_summary.py now
    # also feeds the real envelope.
    #
    # Preferring whichever level actually carries the keys, rather than always
    # descending, keeps this correct for a runner that returns the CLI JSON
    # directly and for one that wraps it.
    source = output
    if not _has_usage_keys(source):
        nested = output.get("structured_output")
        if isinstance(nested, dict) and _has_usage_keys(nested):
            source = nested

    summary: dict[str, Any] = {}

    usage = source.get("usage")
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                summary[key] = value
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), int):
            summary["thinking_tokens"] = details["thinking_tokens"]

    # bool is a subclass of int, so it is excluded explicitly -- `is_error: true`
    # arriving as a cost of 1 would be a quietly wrong number, which is the whole
    # class of bug this file is being edited to avoid.
    for key in ("total_cost_usd", "num_turns", "duration_ms", "duration_api_ms"):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value

    models = source.get("modelUsage")
    if isinstance(models, dict) and models:
        summary["models"] = sorted(str(m) for m in models)

    return summary


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

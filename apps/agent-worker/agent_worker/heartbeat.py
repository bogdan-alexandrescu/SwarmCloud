"""The lease's heartbeat: the beat, the threads that keep beating through a
long block or a short wait, and the scratch and cache volume readings a beat
takes.

Moved out of `lifecycle.py` unchanged (issue 68, a slice of splitting that file
by concern): `Worker` inherits `HeartbeatMixin`, so the methods keep their
`self` access and every `worker._heartbeat()` call and test still resolves.
`lifecycle` re-exports `HEARTBEAT_EVENT_EVERY` by assignment, for a cite of that
file; a test that changes the cadence patches it here, where `_heartbeat` reads
it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from swarm_common.states import EventType

from . import budget as budget_mod
from .errors import FencedError, TenantMismatchError
from .runners.streams import cli_agent_spec


#: One heartbeat event per this many lease heartbeats. The lease is refreshed
#: every interval; the event stream would be unreadable at that rate.
HEARTBEAT_EVENT_EVERY = 5


class HeartbeatMixin:
    """The heartbeat methods of `lifecycle.Worker`, which inherits them."""

    def _heartbeat(self) -> None:
        # Read BEFORE the beat: the lease's new expiry is computed from the
        # time the beat started, and a beat that retried for most of its
        # budget must not be credited with that time again.
        started = self.heartbeat_clock()
        if self.control.heartbeat() is not False:
            # Landed: the lease is live for one more extension from when the
            # beat started (archived issue 70). A refused beat (False) extends nothing, and
            # the control poll meets the fence behind it.
            self._lease_live_until = started + self.control.heartbeat_extension_seconds
        self._last_beat_started = started
        self._heartbeats += 1
        self._log_memory_guard_kills()
        scratch = self._scratch_reading()
        fresh = scratch is not None and self._scratch_crossed(scratch)
        cache = self._cache_reading()
        if cache is not None and self._cache_crossed(cache):
            fresh = True
        if self._heartbeats % HEARTBEAT_EVENT_EVERY == 1 or fresh:
            # OUT OF CADENCE when a scratch volume crossed a mark (archived issue 2436): kubelet
            # evicts with no grace, so the reading the reconciler reports must
            # be the newest there was. A whole reading, not `scratch` alone:
            # `reconciler.memory` takes the limit from the newest HEARTBEAT.
            detail = self._usage_reading()
            if scratch:
                detail["scratch"] = scratch
            if cache:
                detail["scratch_cache"] = cache
            self.control.emit(EventType.HEARTBEAT, detail)
        if self._heartbeats % HEARTBEAT_EVENT_EVERY == 1:
            # THE LIVE READING DETAILS DRAWS, on the attempt itself (contract
            # request #15): the same cadence as the event, so a running
            # attempt's figures are never more than one reading behind it.
            # WRITTEN EVEN WHEN THE FIGURES DID NOT MOVE (request #26): the
            # reading's time is what says the worker is still reading, and an
            # idle agent's unchanged figures skipped would age as a stall.
            self._record_cpu(periodic=True)

    #: The pod's local scratch volumes (`scratchusage.parse_volumes`), read
    #: once; [] on Cloud Run, where nothing is sampled. And per volume, the
    #: bytes (`scratchusage.crossing_bytes`) at its last out-of-cadence reading.
    _scratch_volumes: list[tuple[str, Path, int]] | None = None
    _scratch_marks: dict[str, int] | None = None
    #: The thread that walks them (`scratchusage.ScratchSampler`), built and
    #: started by the first beat that has volumes; stopped by `run`.
    _scratch_sampler: Any = None
    #: Where a crossing is written for the agent (`_scratch_note_env`), or
    #: None while the runner was given no note: Cloud Run, or a runner that
    #: cannot be told mid-run.
    _scratch_note: Path | None = None

    def _scratch_volumes_parsed(self) -> list[tuple[str, Path, int]]:
        from . import scratchusage as scratch_mod

        if self._scratch_volumes is None:
            self._scratch_volumes = scratch_mod.parse_volumes(os.environ.get(scratch_mod.SCRATCH_VOLUMES_ENV))
            self._scratch_marks = {}
        return self._scratch_volumes

    def _scratch_note_env(self) -> dict[str, str]:
        """The runner's scratch note (issue #26), clearing a stale one on first use.

        Only when the pod has scratch volumes and the runner is claude-code,
        whose PostToolUse hook (`runners/budget_note_hook.py`) is the one way
        to tell an agent mid-run; `codex exec` takes nothing once started.
        """
        from . import scratchusage as scratch_mod

        if self._scratch_note is None:
            spec = cli_agent_spec(self.cfg.runner_profile)
            if spec is None or spec.name != "claude-code" or self.ws is None:
                return {}
            if not self._scratch_volumes_parsed():
                return {}
            note = self.ws.private / "scratch-note.json"
            for stale in (note, note.with_name(note.name + ".told")):
                stale.unlink(missing_ok=True)
            self._scratch_note = note
        return {scratch_mod.SCRATCH_NOTE_ENV: str(self._scratch_note)}

    def _scratch_reading(self) -> dict[str, Any] | None:
        """The newest walk of the scratch volumes (archived issue 2436), or None. Never raises.

        None without volumes, and before the sampler's first pass ends. The
        walk runs on the sampler's own thread (issue #26), never here: this
        runs on the loop that extends the lease, and reading the published
        reading costs no I/O.
        """
        from . import scratchusage as scratch_mod

        if not self._scratch_volumes_parsed():
            return None
        try:
            if self._scratch_sampler is None:
                self._scratch_sampler = scratch_mod.ScratchSampler(
                    self._scratch_volumes,
                    scrub=self.log.scrub_text,
                    warn=lambda error: self.log.warning(
                        "could not read the scratch volumes", error=error),
                )
                self._scratch_sampler.start()
            return self._scratch_sampler.latest()
        except Exception as exc:  # noqa: BLE001 -- never raises; logged
            self.log.warning("could not read the scratch volumes", error=f"{type(exc).__name__}: {exc}")
            return None

    def _stop_scratch_sampler(self) -> None:
        sampler = self._scratch_sampler
        if sampler is not None:
            sampler.stop()

    def _scratch_crossed(self, scratch: dict[str, Any]) -> bool:
        """Whether a volume passed two thirds of its limit, or grew another 10% of it past.

        The first crossing is warned once per volume per attempt, in the
        worker's log; every crossing asks for an out-of-cadence reading, and
        tells the agent through its scratch note (`_scratch_note_env`), so
        that the agent itself, not only the log, learns what fills the volume.
        """
        from . import scratchusage as scratch_mod

        marks = self._scratch_marks if self._scratch_marks is not None else {}
        self._scratch_marks = marks
        crossed = False
        told: list[str] = []
        for name, mount, limit in self._scratch_volumes or []:
            entry = scratch.get(name)
            # A `partial` reading's bytes are a floor: compared by the larger
            # of them and the last complete walk's total (issue #26).
            if not entry or scratch_mod.crossing_bytes(entry) < scratch_mod.warn_threshold(limit):
                continue
            used = scratch_mod.crossing_bytes(entry)
            if name in marks and used < marks[name] + limit // 10:
                continue
            words = (
                f"{mount} is at {scratch_mod.size_words(used)} of its "
                f"{scratch_mod.limit_words(limit)} limit; past it kubelet evicts this pod. "
                f"Largest: {scratch_mod.format_top(entry)}. "
                "Move large scratch data under $TMPDIR (the cache volume)."
            )
            if name not in marks:
                self.log.warning(words, volume=name, used_bytes=used, limit_bytes=limit)
            told.append(words)
            marks[name] = used
            crossed = True
        if told:
            self._tell_scratch(" ".join(told))
        return crossed

    def _tell_scratch(self, words: str) -> None:
        """Write the agent's scratch note, replacing one not yet told. Never raises.

        One note per beat, naming every volume that crossed in it; a note the
        hook has not handed on yet is replaced by the newer beat's, so the
        agent is told the latest figures, once.
        """
        note = self._scratch_note
        if note is None:
            return
        try:
            budget_mod._write_private(note, json.dumps({"message": self.log.scrub_text(words)}))
        except OSError as exc:
            self.log.warning("could not write the scratch note", error=f"{type(exc).__name__}: {exc}")

    #: What the cache volume was warned of this attempt: `inodes`, `space`.
    _cache_warned: set[str] | None = None

    def _cache_reading(self) -> dict[str, Any] | None:
        """This beat's bytes and inodes of the pod's cache volume (archived issue 4061), or None. Never raises.

        None on Cloud Run, where there is no cache volume (`cfg.cache_root`).
        """
        from . import scratchusage as scratch_mod

        root = getattr(self.cfg, "cache_root", None)
        if root is None:
            return None
        try:
            return scratch_mod.cache_reading(Path(root))
        except Exception as exc:  # noqa: BLE001 -- never raises; logged
            self.log.warning(
                "could not read the cache volume", error=f"{type(exc).__name__}: {exc}"
            )
            return None

    def _cache_crossed(self, cache: dict[str, Any]) -> bool:
        """Whether the cache volume newly passed two thirds of its inodes or of its space.

        Each is warned once per attempt, in the worker's log, worded by
        `scratchusage.cache_warnings`: inodes named as inodes while bytes are
        free, which is the out-of-inodes ENOSPC a checkpoint write reports
        (`checkpoint._local_room`). A first warning asks for an out-of-cadence
        reading, so the HEARTBEAT that carries it is the one the warning saw.
        """
        from . import scratchusage as scratch_mod

        warned = self._cache_warned if self._cache_warned is not None else set()
        self._cache_warned = warned
        root = Path(self.cfg.cache_root)
        crossed = False
        for kind, words in scratch_mod.cache_warnings(root, cache).items():
            if kind in warned:
                continue
            warned.add(kind)
            self.log.warning(
                words,
                volume="cache",
                running_out_of=kind,
                used_bytes=cache.get("used_bytes"),
                total_bytes=cache.get("total_bytes"),
                inodes_used=cache.get("inodes_used"),
                inodes_total=cache.get("inodes_total"),
            )
            crossed = True
        return crossed

    @contextmanager
    def _heartbeat_meanwhile(
        self, what: str, *, progress: Callable[[], tuple[int, int]] | None = None
    ) -> Iterator[None]:
        """Heartbeat the lease from a thread for as long as the block runs.

        THE CHECKPOINT RAN ON THE LOOP THAT HEARTBEATS THE LEASE (archived issue 286), so the
        lease aged for as long as the archive and the upload took: a large
        work tree -- tool caches, before `checkpoint.TOOL_CACHES` -- held it
        for however long gzip and GCS needed, and on 2026-09-29 the attempt was
        fenced 64 s after `checkpoint_started`. The block's own work touches
        only the workspace and the object store, so the heartbeat has the
        control plane to itself meanwhile.

        Beats every `heartbeat_interval_seconds`, the loop's own cadence, and
        through `_heartbeat`, the loop's own call. A heartbeat that raises is
        logged and ends this thread only: the loop's next heartbeat meets the
        same condition and handles it as it always has.

        COUNTED FROM THE LAST BEAT, NOT FROM THE BLOCK'S START (archived issue 426). The
        first beat is due one interval after the last beat started
        (`_last_beat_started`), at once if that is already past. Counted from
        the block's start, a checkpoint begun just before the loop's next beat
        added a whole interval of silence, and on 2026-10-01 a worker
        compiling on every core was reclaimed alive after 98 s of it.

        BOUNDED, AND IT ASKS BEFORE EVERY BEAT (the archived pull request 288 review). A thread
        that beats for as long as the block runs keeps the lease -- and the
        capacity reserved behind it -- alive for a checkpoint that never ends,
        a wedged upload, which the supervision loop is not running to notice.
        So it beats for at most `heartbeat_meanwhile_max_seconds` (three lease
        timeouts; `config.WorkerConfig` says why), and before each beat it
        reads the control plane: once the attempt is FENCED -- another
        generation owns the task, or its lease is released or it is terminal
        (`ControlSignals.is_fenced`) -- it stops at once, rather than
        prolonging a lease that is no longer this attempt's. The loop sees the
        same signal on its next poll and acts on it as it always has.

        PAST THE BOUND ONLY WHILE IT PROVABLY ADVANCES, AND NEVER PAST THE
        CEILING (archived issue 1808, owner decision 2026-10-21). A checkpoint of a growing
        tree (238,819 files, 291 MB, about 7.5 min) outlasted the 360 s bound
        while still archiving, and a live agent was reclaimed three times in a
        row. So a block that passes `progress` -- the checkpoint passes its
        writer's counter (`CheckpointManager.progress`), files and bytes,
        which only grow -- keeps beating past the bound for as long as the
        reading moves: it is read at every wake, and no advance within ONE
        LEASE TIMEOUT (the bound over
        `config.HEARTBEAT_MEANWHILE_BOUND_LEASE_TIMEOUTS`) is a wedge, which
        stops the beating exactly as the bound did. Advancing or not, it stops
        at `heartbeat_meanwhile_ceiling_seconds` (`config.WorkerConfig` says
        why). Every other rule holds past the bound unchanged: the fence is
        asked before every beat, a cancel does not stop it. A block that
        passes no counter (a clone, a push) stops at the bound as before. One
        `heartbeat_extended` event, once the block ends, records the bound,
        the time used, the progress seen and how it ended, so a reclaim -- or
        the absence of one -- is explainable from the log.

        A CANCEL DOES NOT STOP IT (owner decision 2026-09-29, archived pull request 288). A
        cancelled attempt still writes a checkpoint on its way out
        (`self._checkpoint("cancellation")`), and that checkpoint needs the lease alive as
        much as any other; stopping the beat on `cancel_requested` would let
        the lease lapse under the very checkpoint the cancel asked for. The
        bound still applies to it.

        Joined before the block's caller goes on, with a timeout: a beat stuck
        in a Firestore call does not hold the checkpoint's caller up with it.
        What such a beat can still write is `heartbeat_at` and `expires_at` on
        the lease (`ControlPlane.heartbeat` is an unfenced update of those two
        fields). It cannot clear `released_at`, and a lease with `released_at`
        set reads as released (`ControlSignals.is_fenced`) whatever its expiry.
        """
        from .config import HEARTBEAT_MEANWHILE_BOUND_LEASE_TIMEOUTS
        from .lifecycle import _control_plane_unreachable

        stop = threading.Event()
        clock, wait = self.heartbeat_clock, self.heartbeat_wait
        interval = self.cfg.heartbeat_interval_seconds
        bound = self.cfg.heartbeat_meanwhile_max_seconds
        ceiling_seconds = max(bound, self.cfg.heartbeat_meanwhile_ceiling_seconds)
        # One lease timeout: the longest the block may go without progress
        # past the bound before it is treated as wedged.
        stall = bound / HEARTBEAT_MEANWHILE_BOUND_LEASE_TIMEOUTS
        opened = clock()
        deadline = opened + bound
        ceiling = opened + ceiling_seconds
        # What the extension saw, for its one log event: the reading at the
        # block's start, the last reading that moved and when, how it ended.
        first = progress() if progress is not None else (0, 0)
        last_reading = [first]
        advanced_at = [opened]
        extension: dict[str, str] = {}

        # When this thread last asked for a beat: it never asks twice within
        # an interval, whatever `_last_beat_started` says.
        asked: list[float] = []

        def due() -> float:
            # A fixed time, never "now": with no beat on record the first is
            # due at the block's start, which is at once.
            starts = [t for t in (self._last_beat_started, *asked) if t is not None]
            return max(starts) + interval if starts else opened

        def limit(now: float) -> float | None:
            """When beating must stop as things stand; None once it must stop now."""
            if progress is not None:
                reading = progress()
                if reading != last_reading[0]:
                    last_reading[0], advanced_at[0] = reading, now
            if now < deadline:
                return min(deadline, ceiling)
            stalled_at = advanced_at[0] + stall
            if progress is None or now >= stalled_at:
                if extension:
                    extension["ended"] = "no progress"
                self.log.warning(
                    "stopped heartbeating the lease during a long operation: it "
                    "outlasted its bound"
                    + (" and made no progress for a lease timeout" if progress else "")
                    + ", and the lease is left to expire",
                    during=what,
                    bound_seconds=bound,
                )
                return None
            if now >= ceiling:
                if extension:
                    extension["ended"] = "ceiling"
                self.log.warning(
                    "stopped heartbeating the lease during a long operation: it "
                    "reached its hard ceiling while still making progress, and the "
                    "lease is left to expire",
                    during=what,
                    bound_seconds=bound,
                    ceiling_seconds=ceiling_seconds,
                )
                return None
            extension.setdefault("ended", "finished")
            return min(ceiling, stalled_at)

        def beat() -> None:
            while True:
                now = clock()
                until = limit(now)
                if until is None:
                    return
                if wait(stop, max(0.0, min(due() - now, until - now))):
                    return
                if clock() >= until:
                    continue  # the top of the loop re-reads and decides
                if clock() < due():
                    continue  # another beat landed meanwhile; wait for the next
                asked[:] = [clock()]
                try:
                    signals = self.control.poll()
                    if signals.is_fenced(self.cfg.generation):
                        if extension:
                            extension["ended"] = "fenced"
                        # The relay stops forwarding now, not at the next poll.
                        self.mcp.mark_fenced()
                        self.log.warning(
                            "stopped heartbeating the lease during a long operation: "
                            "the attempt is fenced",
                            during=what,
                            task_generation=signals.generation,
                            lease_released=signals.lease_released,
                            state=signals.state,
                        )
                        return
                    self._heartbeat()
                except Exception as exc:  # noqa: BLE001 -- logged; only a fence or a defect ends the thread
                    # A beat that could not reach the control plane is one of
                    # the missed beats the interval is sized for (archived issue 426): its
                    # budget is one interval now, not the whole grace, so the
                    # next is asked for on time rather than the thread ending
                    # with the block still running. A fence, a tenant mismatch
                    # or a defect ends the thread, as before.
                    unreachable = not isinstance(
                        exc, (FencedError, TenantMismatchError)
                    ) and _control_plane_unreachable(exc)
                    self.log.warning(
                        "a heartbeat during a long operation failed; "
                        + ("the next beat is asked for on time" if unreachable
                           else "the loop will retry it"),
                        during=what,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    if not unreachable:
                        if extension:
                            extension["ended"] = "heartbeat failed"
                        return

        thread = threading.Thread(target=beat, name="heartbeat-meanwhile", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=interval)
            if thread.is_alive():
                self.log.warning(
                    "a heartbeat during a long operation is still in flight; going on without it",
                    during=what,
                )
            if extension:
                reading = progress() if progress is not None else first
                self.log.info(
                    "kept heartbeating the lease past its bound while a long "
                    "operation made progress",
                    event="heartbeat_extended",
                    during=what,
                    bound_seconds=bound,
                    ceiling_seconds=ceiling_seconds,
                    # On the clock `opened` was read from, so a test's virtual
                    # clock reports the virtual time the block took.
                    seconds=round(clock() - opened, 1),
                    files=reading[0] - first[0],
                    bytes=reading[1] - first[1],
                    ended=extension["ended"],
                )

    def _sleep_with_heartbeat(self, seconds: float) -> None:
        """Short waits only -- long ones park. Keeps the lease alive meanwhile."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._interrupted:
            time.sleep(min(self.cfg.heartbeat_interval_seconds, max(0.1, end - time.monotonic())))
            try:
                self._heartbeat()
            except Exception as exc:  # noqa: BLE001 -- logged, never fatal here
                # Logged, never fatal here (archived issue 70): the supervision loop's next
                # beat decides, against the lease, whether it is an outage.
                self._control_plane_call_failed("heartbeat", exc)

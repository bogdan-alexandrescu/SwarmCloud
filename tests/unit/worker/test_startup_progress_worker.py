"""The browser's start-up steps, from the live gate to the task's events (STARTUP-API).

The owner, 2026-10-13: a progress bar while a browser agent starts. The pod's
four steps are the reconciler's; these are the browser's three, which only
the pod can see: `browser_launching` (the gate is about to start Chromium),
`browser_ready` (Chromium answered CDP) and `first_frame` (the gate's capture
published its first frame to the stream). The `display` sidecar launches
Chromium before the worker container starts and holds no credential, so it
DATES each step in its ctl `status`, and the worker writes each once as a
`startup_progress` event at the gate's time.

WHAT EACH TEST PINS:

  * the gate dates launching before ready, and the first frame only once a
    stream is open; a restarted gate, which launches nothing, dates nothing;
  * the worker writes each step once per attempt, in the shared shape, at the
    gate's time -- however many ticks read the same status -- and tries a
    failed write again on the next tick;
  * without a live path (no tunnel, so no stream) it settles the browser's two
    and stops reading;
  * a fenced worker writes nothing (invariant 5).

Every ticket is built at runtime.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from agent_worker import handoff as handoff_mod
from agent_worker import lifecycle
from agent_worker.handoff import GATE_STARTUP_STEPS
from agent_worker.livegate import tunnel
from swarm_common.states import EventType

from test_livegate_main import Rig, _started, _until, short_tmp  # noqa: F401  (fixture)

# ---------------------------------------------------------------------------
# The gate dates the steps
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_the_gate_dates_launching_then_ready_and_no_frame_until_a_stream_opens(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        rig.gate._wall = Clock()
        stop, task = await _started(rig)
        startup = rig.gate.status()["startup"]
        assert set(startup) == {"browser_launching", "browser_ready"}
        assert startup["browser_launching"] < startup["browser_ready"]

        await rig.gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        rig.relay.push(tunnel.OP_OPEN, "v1")
        await _until(lambda: "first_frame" in rig.gate.status()["startup"])
        startup = rig.gate.status()["startup"]
        assert startup["first_frame"] > startup["browser_ready"]
        first = startup["first_frame"]
        await _until(lambda: rig.framebuffer.grabs >= 3)
        assert rig.gate.status()["startup"]["first_frame"] == first, "the FIRST frame, not the latest"
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_restarted_gate_dates_nothing(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        open(rig.cfg.marker, "w").close()
        stop, task = await _started(rig)
        assert rig.gate.status()["startup"] == {}
        stop.set()
        await task

    asyncio.run(body())


# ---------------------------------------------------------------------------
# The worker writes them, once each
# ---------------------------------------------------------------------------


class StatusGate:
    """The gate's `status`, scripted; every call is counted."""

    def __init__(self, startup: dict[str, float]) -> None:
        self.startup = startup
        self.calls = 0

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        assert op == "status"
        self.calls += 1
        return {"browser": "running", "tunnel": "open", "last_episode": None, "startup": dict(self.startup)}


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    info = warning


def _path(*, offered: bool, progress: Any) -> handoff_mod.HandoffPath:
    cfg = SimpleNamespace(runner_profile="claude-code-browser", tenant_id="eng", task_id="task_x",
                          attempt_id="att_x", lease_id="lease_x", generation=3)
    path = handoff_mod.HandoffPath(cfg, logger=Log(), memory=None, children=None, live=True,
                                   environ={}, progress=progress)
    path.unavailable = None if offered else handoff_mod.GATE_UNAVAILABLE
    return path


LAUNCH, READY, FRAME = 1_760_000_100.0, 1_760_000_101.5, 1_760_000_130.25


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def test_each_step_is_written_once_at_the_gates_time():
    written: list[tuple[str, datetime]] = []
    path = _path(offered=True, progress=lambda step, at: written.append((step, at)) or True)

    for _ in range(3):
        path.note_startup({"startup": {"browser_launching": LAUNCH, "browser_ready": READY}})
    path.note_startup({"startup": {"browser_launching": LAUNCH, "browser_ready": READY, "first_frame": FRAME}})
    path.note_startup({"startup": {"browser_launching": LAUNCH, "browser_ready": READY, "first_frame": FRAME}})

    assert written == [
        ("browser_launching", _at(LAUNCH)),
        ("browser_ready", _at(READY)),
        ("first_frame", _at(FRAME)),
    ]
    assert tuple(step for step, _ in written) == GATE_STARTUP_STEPS


def test_a_failed_write_is_tried_again_on_the_next_status():
    attempts: list[str] = []

    def flaky(step: str, at: datetime) -> bool:
        attempts.append(step)
        if len(attempts) == 1:
            raise RuntimeError("deadline exceeded")
        return True

    path = _path(offered=True, progress=flaky)
    status = {"startup": {"browser_launching": LAUNCH, "browser_ready": READY}}
    path.note_startup(status)
    assert path.startup_noted == set()
    path.note_startup(status)
    assert path.startup_noted == {"browser_launching", "browser_ready"}
    assert attempts == ["browser_launching", "browser_launching", "browser_ready"]


def test_a_status_without_startup_or_with_junk_writes_nothing():
    written: list[str] = []
    path = _path(offered=True, progress=lambda step, at: written.append(step) or True)
    path.note_startup({})
    path.note_startup({"startup": "soon"})
    path.note_startup({"startup": {"browser_launching": "x", "browser_ready": True}})
    assert written == []


def test_without_a_live_path_the_two_browser_steps_are_read_then_reading_stops(tmp_path):
    written: list[str] = []
    path = _path(offered=False, progress=lambda step, at: written.append(step) or True)
    gate = StatusGate({"browser_launching": LAUNCH, "browser_ready": READY})
    path.gate = gate

    for _ in range(4):
        path.tick(tmp_path, lambda: {})

    assert written == ["browser_launching", "browser_ready"]
    assert gate.calls == 1, "a settled start-up costs no further status reads"


def test_the_live_tick_notes_the_steps_from_the_status_it_already_reads(tmp_path):
    written: list[str] = []
    path = _path(offered=True, progress=lambda step, at: written.append(step) or True)
    path.gate = StatusGate({"browser_launching": LAUNCH, "browser_ready": READY, "first_frame": FRAME})

    path.tick(tmp_path, lambda: {})
    path.tick(tmp_path, lambda: {})

    assert written == list(GATE_STARTUP_STEPS)


# ---------------------------------------------------------------------------
# The lifecycle's writer: the shared shape, and never when fenced
# ---------------------------------------------------------------------------


class Control:
    def __init__(self) -> None:
        self.emitted: list[tuple[EventType, dict[str, Any], datetime | None]] = []

    def emit(self, event_type: EventType, detail: dict[str, Any] | None = None, *, at: datetime | None = None) -> None:
        self.emitted.append((event_type, dict(detail or {}), at))


def _worker(**flags: bool) -> Any:
    return SimpleNamespace(
        cfg=SimpleNamespace(attempt_id="att_x", generation=3),
        control=Control(),
        _fenced_exit=flags.get("fenced", False),
        _writes_forbidden=flags.get("forbidden", False),
        _control_plane_down=flags.get("down", False),
    )


def test_the_worker_writes_the_shared_shape_at_the_gates_time():
    worker = _worker()
    settled = lifecycle.Worker._emit_startup_progress(worker, "browser_ready", _at(READY))
    assert settled is True
    assert worker.control.emitted == [
        (EventType.STARTUP_PROGRESS, {"step": "browser_ready", "attempt_id": "att_x", "generation": 3}, _at(READY))
    ]


def test_a_fenced_worker_writes_nothing():
    for flags in ({"fenced": True}, {"forbidden": True}):
        worker = _worker(**flags)
        assert lifecycle.Worker._emit_startup_progress(worker, "browser_ready", _at(READY)) is True
        assert worker.control.emitted == []


def test_a_control_plane_outage_leaves_the_step_for_later():
    worker = _worker(down=True)
    assert lifecycle.Worker._emit_startup_progress(worker, "first_frame", _at(FRAME)) is False
    assert worker.control.emitted == []


def test_the_lifecycle_hands_its_writer_to_the_hand_off_path():
    source = lifecycle.Worker.__init__.__code__.co_names
    assert "_emit_startup_progress" in source

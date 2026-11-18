"""A followed workflow costs one workflow read per poll, not one read per step
(the owner's pick, 2026-11-06, after the load analysis: single-task reads were 39% of
swarm-api's requests, nearly all from the bridge's follow rows).

Offline: a fake client that counts what is asked of it, and a fake clock.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_mcp import compact
from test_follow_cursor import swarm, world  # noqa: F401 - fixtures

WORKFLOW = "wf_fewer"
STEPS = ["t0", "t1", "t2", "t3", "t4"]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Api:
    """Five step tasks of one workflow; `script` changes them at a poll number."""

    def __init__(self, states: dict[str, str], script: dict[int, dict[str, str]] | None = None) -> None:
        self.states = dict(states)
        self.script = script or {}
        self.polls = 0
        self.task_reads: list[str] = []
        self.workflow_reads = 0

    def _doc(self, task_id: str) -> dict:
        return {"id": task_id, "task_id": task_id, "workflow_id": WORKFLOW,
                "step_id": f"step-{task_id}", "state": self.states[task_id]}

    def task(self, task_id: str) -> dict:
        self.task_reads.append(task_id)
        return self._doc(task_id)

    def request(self, method: str, path: str, **_kwargs) -> dict:
        assert (method, path) == ("GET", f"/v1/workflows/{WORKFLOW}")
        self.workflow_reads += 1
        self.polls += 1
        self.states.update(self.script.get(self.polls, {}))
        return {"workflow": {"workflow_id": WORKFLOW,
                             "steps": [{"step_id": f"step-{t}", "task_id": t} for t in STEPS]},
                "tasks": [self._doc(t) for t in STEPS]}

    def attempts(self, _task_id: str, **_kwargs) -> list:
        return []

    def events_page(self, _task_id: str, **_kwargs):
        return [], None, False


def _now() -> datetime:
    return datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)


def _summary(api: _Api, clock: _Clock, since=None, wait: float = 0) -> dict:
    return compact.watch_summary(
        api, list(STEPS), {"noun": "step", "total": 5}, since=since, wait_seconds=wait,
        sleep=clock.sleep, clock=clock.clock, now=_now,
    )


START = {"t0": "LEASED", "t1": "LEASED", "t2": "QUEUED", "t3": "QUEUED", "t4": "QUEUED"}


def test_ten_polls_of_a_five_step_workflow_read_the_workflow_ten_times_and_only_changed_steps():
    api = _Api(START)
    clock = _Clock()
    since = _summary(api, clock)["since"]
    api.task_reads.clear()
    api.workflow_reads = 0
    api.polls = 0
    # Inside one hold: t0 moves on at polls 3 and 6 (still holding capacity, so
    # the hold goes on), and t2 is leased at poll 10, which ends it.
    api.script = {3: {"t0": "DISPATCHED"}, 6: {"t0": "STARTING"}, 10: {"t2": "LEASED"}}
    reply = _summary(api, clock, since=since, wait=1800)
    assert api.workflow_reads == 10
    # The token names the workflow, so no read by id finds it: one per change.
    assert api.task_reads == ["t0", "t0", "t2"]
    assert reply["transitions"]


def test_the_first_call_finds_the_workflow_with_one_task_read():
    api = _Api(START)
    _summary(api, _Clock())
    # t0 by id names the workflow; its one read serves the four others.
    assert api.task_reads == ["t0"]
    assert api.workflow_reads == 1


def test_a_quiet_hold_backs_off_and_any_change_resets_it():
    api = _Api(START, {6: {"t1": "RUNNING"}})
    clock = _Clock()
    since = _summary(api, clock)["since"]
    api.polls = 0
    clock.sleeps.clear()
    _summary(api, clock, since=since, wait=330)
    assert clock.sleeps[:5] == [
        compact.POLL_SECONDS, compact.POLL_SECONDS * 2, compact.POLL_SECONDS * 4,
        compact.IDLE_POLL_CAP_SECONDS, compact.IDLE_POLL_CAP_SECONDS,
    ]
    # Poll 6 saw t1 change: the next wait is the shortest again.
    assert clock.sleeps[5] == compact.POLL_SECONDS
    assert max(clock.sleeps) <= compact.IDLE_POLL_CAP_SECONDS


def test_a_single_task_follow_backs_off_too():
    class _One(_Api):
        def task(self, task_id: str) -> dict:
            self.polls += 1
            self.states.update(self.script.get(self.polls, {}))
            return super().task(task_id)

    api = _One({"t0": "RUNNING"}, {})
    clock = _Clock()
    since = compact.watch_progress(api, ["t0"], sleep=clock.sleep, clock=clock.clock, now=_now)["since"]
    compact.watch_progress(api, ["t0"], since=since, wait_seconds=600,
                           sleep=clock.sleep, clock=clock.clock, now=_now)
    assert clock.sleeps[:4] == [10.0, 20.0, 40.0, 60.0]
    assert api.workflow_reads == 0


def test_a_quiet_running_task_in_the_lines_format_backs_off_to_a_minute(swarm, world):
    from swarm_mcp import progress

    world.task("task_a", state="RUNNING")
    clock = _Clock()
    first = progress.watch(swarm, ["task_a"], sleep=clock.sleep, clock=clock.clock)
    got = progress.watch(swarm, ["task_a"], since=first["since"], wait_seconds=255,
                         sleep=clock.sleep, clock=clock.clock)
    assert clock.sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0], clock.sleeps
    assert got["polls"] == 8

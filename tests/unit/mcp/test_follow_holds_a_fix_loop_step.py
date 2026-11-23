"""A workflow with a `loop` step (the fix-loop profile) is held like any other (issue 238).

What was seen, 2026-11-23: a summary row attached to an implement, review,
fix workflow on a deployment that appends `loop` and `merge` got every
`swarm_follow` call back at once -- `line_changed: false`, `transitions: []`,
`stop: false` -- about sixty calls and 120,000 tokens in four minutes, and the
row died without a result. Rows of workflows without the loop step held.

The cause: the hold's "has this task moved?" compares the task's wake key
with the one the `since` token carries, read back as the token key with its
LAST `|` segment cut off. A loop step's key has one segment more than any
other step's -- `<wake>|<waiting words>|<round k of N ...>` -- so the read-back
wake key never matched and every poll of every call looked like a change.

Everything here runs on a fake API whose tasks move on a timeline read from
the same fake clock the hold sleeps on, so no test waits. The fake refuses
past a read budget, so a hold that spins without sleeping fails rather than
hangs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_mcp import compact
from swarm_mcp.client import SwarmError
from swarm_mcp.progress import FIX_LOOP_PROFILE

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
WORKFLOW = "wf_fixture_loop"
DEP = "DEPENDENCY_INCOMPLETE"
PERSON = "HUMAN_REQUIRED"
#: More reads than any honest test here makes; a spinning hold makes thousands.
READ_BUDGET = 2_000

STEPS = ("build", "review", "fix", "loop", "merge")
IDS = {step: f"task_{step}" for step in STEPS}
SUMMARY_OF = {"noun": "step", "total": 5, "counted": {}, "prs": [], "console": None, "line": ""}


class Workflow:
    """Five steps whose documents are a function of simulated time.

    `phases[step]` is a list of `(start_second, state, park_reason)`. A parked
    step is served with the API's `waiting` sentence, as the API serves it,
    and the loop step with the fix-loop profile and its signed round cap.
    """

    def __init__(self, phases: dict[str, list[tuple[float, str, str | None]]]) -> None:
        self.phases = phases
        self.now = 0.0
        self.reads = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now)

    def _charge(self) -> None:
        self.reads += 1
        assert self.reads <= READ_BUDGET, "the hold read without sleeping: it spins"

    def _doc(self, step: str) -> dict:
        start, state, reason = self.phases[step][0]
        for phase in self.phases[step]:
            if phase[0] <= self.now:
                start, state, reason = phase
        doc = {
            "id": IDS[step],
            "task_id": IDS[step],
            "step_id": step,
            "workflow_id": WORKFLOW,
            "runner_profile": FIX_LOOP_PROFILE if step == "loop" else "claude-code",
            "state": state,
            "park_reason": reason,
            "attempt_count": 0 if state == "PARKED" else 1,
            "max_attempts": 3,
            "created_at": T0.isoformat(),
            "updated_at": (T0 + timedelta(seconds=start)).isoformat(),
        }
        if step == "loop":
            doc["dispatch"] = {"fix_loop": {"max_rounds": 3}}
            if reason == PERSON:
                doc["blocked_by"] = [{"park_phase": "fix_loop_exhausted"}]
                doc["metadata"] = {"fix_loop_state": {"rounds": [{"round": 1}, {"round": 2}, {"round": 3}]}}
        if state == "PARKED":
            summary = ("waits for a person: the fix loop ran out of rounds" if reason == PERSON
                       else "waits for its parent steps to finish")
            doc["waiting"] = {"reason": reason, "summary": summary, "since": doc["updated_at"]}
        if state == "RUNNING":
            doc["started_at"] = (T0 + timedelta(seconds=start)).isoformat()
        if state == "SUCCEEDED":
            doc["started_at"] = T0.isoformat()
            doc["completed_at"] = (T0 + timedelta(seconds=start)).isoformat()
            doc["result_summary"] = {"runner": {"summary": "done"}, "artifacts": []}
        return doc

    # The client surface `compact` reads.
    def task(self, task_id: str) -> dict:
        self._charge()
        (step,) = [s for s in STEPS if IDS[s] == task_id]
        return self._doc(step)

    def request(self, method: str, path: str, **_kwargs) -> dict:
        self._charge()
        assert (method, path) == ("GET", f"/v1/workflows/{WORKFLOW}"), (method, path)
        return {"workflow_id": WORKFLOW, "tasks": [self._doc(s) for s in STEPS]}

    def attempts(self, task_id: str) -> list[dict]:
        minutes = int(self.now // 60)
        return [{"attempt_id": "att_1", "cost_usd": 0.01 * minutes, "input_tokens": 1000 * minutes, "output_tokens": 0}]

    def events_page(self, task_id: str, *, limit: int, newest_first: bool):
        at = (T0 + timedelta(minutes=int(self.now // 60))).isoformat()
        return [{"type": "checkpoint_completed", "at": at}], None, True

    def __getattr__(self, name):
        def _nothing(*_args, **_kwargs):
            raise SwarmError(f"{name} is not part of this fake", status=404)
        return _nothing


def _build_running(run_for: float = 10_000) -> dict[str, list[tuple[float, str, str | None]]]:
    """The first step running, every other step -- the loop too -- parked on its dependency."""
    return {
        "build": [(0, "RUNNING", None), (run_for, "SUCCEEDED", None)],
        **{step: [(0, "PARKED", DEP)] for step in ("review", "fix", "loop", "merge")},
    }


def _loop_reaches_a_person(at: float) -> dict[str, list[tuple[float, str, str | None]]]:
    """Build and review done, fix running; the loop parked on its dependency,
    then -- its rounds spent -- parked for a person at `at`, the merge parked
    on its dependency throughout."""
    return {
        "build": [(0, "SUCCEEDED", None)],
        "review": [(0, "SUCCEEDED", None)],
        "fix": [(0, "RUNNING", None), (at - 300, "SUCCEEDED", None)],
        "loop": [(0, "PARKED", DEP), (at - 300, "PARKED", "FIX_ROUNDS_RUNNING"), (at, "PARKED", PERSON)],
        "merge": [(0, "PARKED", DEP)],
    }


def _summary(world: Workflow, since=None, wait=compact.MAX_WAIT_SECONDS) -> dict:
    return compact.watch_summary(world, [IDS[s] for s in STEPS], SUMMARY_OF, since=since, wait_seconds=wait,
                                 sleep=world.sleep, clock=world.clock, now=world.wall)


# --------------------------------------------------------------------------
# The summary row
# --------------------------------------------------------------------------


def test_a_summary_call_holds_its_whole_window_while_the_loop_waits_on_its_dependency():
    world = Workflow(_build_running())
    first = _summary(world, wait=0)
    assert first["stop"] is False
    for call in range(3):
        began = world.now
        reply = _summary(world, since=first["since"] if call == 0 else reply["since"])
        assert world.now - began >= compact.MAX_WAIT_SECONDS, (
            f"call {call + 2} returned after {world.now - began:.0f} s with nothing new: "
            f"line_changed={reply['line_changed']} transitions={reply['transitions']}")
        assert reply["line_changed"] is False and reply["transitions"] == [] and reply["stop"] is False


def test_the_summary_wakes_once_when_the_loop_parks_for_a_person_then_holds_again():
    at = 1_200.0
    world = Workflow(_loop_reaches_a_person(at))
    reply = _summary(world, wait=0)
    woke: list[tuple[float, dict]] = []
    for _ in range(5):
        began = world.now
        reply = _summary(world, since=reply["since"])
        woke.append((world.now - began, reply))
    person = [r for _, r in woke
              if any(row["task_id"] == IDS["loop"] and row.get("park_reason") == PERSON for row in r["tasks"])]
    assert person, "the loop's park for a person never came back"
    # Every call that returned early carried news: the fix finishing, or the
    # loop's park reason moving -- and the park for a person ends exactly one.
    early = [(held, r) for held, r in woke if held < compact.MAX_WAIT_SECONDS]
    assert len(early) <= 3, [round(held) for held, _ in woke]
    assert world.now >= at
    after = [held for held, r in woke if r in person][1:]
    assert after and all(held >= compact.MAX_WAIT_SECONDS for held in after), (
        f"the person-wait came back on every call: holds {[round(h) for h in after]}")


# --------------------------------------------------------------------------
# The loop step's own row, and the finish row's stage line
# --------------------------------------------------------------------------


def _step(world: Workflow, since=None, wait=compact.MAX_WAIT_SECONDS) -> dict:
    return compact.watch_progress(world, [IDS["loop"]], since=since, wait_seconds=wait, step_id="loop",
                                  sleep=world.sleep, clock=world.clock, now=world.wall)


def test_the_loop_steps_row_holds_on_its_dependency_and_says_the_person_wait_once():
    at = 4_000.0
    world = Workflow(_loop_reaches_a_person(at))
    reply = _step(world, wait=0)
    assert reply["stop"] is False
    began = world.now
    reply = _step(world, since=reply["since"])
    held = world.now - began
    assert held >= compact.MAX_WAIT_SECONDS, (
        f"the loop's row returned after {held:.0f} s while the loop waited on its dependency")
    said: list[str] = []
    for _ in range(4):
        said += [line for line in reply["progress"] if "WAITING ON A PERSON" in line]
        if reply["stop"]:
            break
        reply = _step(world, since=reply["since"])
    assert reply["stop"] is True and reply["stop_reason"] == "waiting_on_person", reply
    assert len(said) == 1, said
    assert said[0].startswith("[loop] WAITING ON A PERSON") and "fix loop" in said[0]


def test_the_stage_line_holds_rather_than_spinning_while_the_loop_waits():
    world = Workflow(_build_running())
    stage_of = {"stages": {IDS[s]: i + 1 for i, s in enumerate(STEPS)}, "states": {}, "key": ""}
    first = compact.watch_stage(world, [IDS[s] for s in STEPS], stage_of, wait_seconds=0,
                                sleep=world.sleep, clock=world.clock, now=world.wall)
    began = world.now
    reply = compact.watch_stage(world, [IDS[s] for s in STEPS], stage_of, since=first["since"],
                                wait_seconds=compact.MAX_WAIT_SECONDS,
                                sleep=world.sleep, clock=world.clock, now=world.wall)
    assert world.now - began >= compact.MAX_WAIT_SECONDS
    assert reply["stage_changed"] is False and reply["stop"] is False

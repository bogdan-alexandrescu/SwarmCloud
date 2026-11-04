"""The sc plugin shows a fix loop's rounds, and `/sc check` flags the loop a person must answer.

Lane FL-PLUGIN of docs/design/fix-loop.md §8 (§6.3, §6.4), part of #2509.
BGD's answers (2026-10-27) bind it: Q3 (c) the person-park expires after 7
days, Q4 (a) any member adds rounds up to the ceiling of 10, Q5 (a)
`max_rounds` 1-5 default 3, Q10 (a) the profile `fix-loop`.

* `/sc check` (rule c): a loop PARKED HUMAN_REQUIRED on any §4.4 phase is
  STUCK, with its phase, the round counter, the last verdict's finding count
  and the exact add-round command. A loop PARKED FIX_ROUNDS_RUNNING is the
  platform working -- a round is in flight -- and is never flagged, whatever
  its age: rule (c)'s 90-minute CI_PENDING clock is not its clock.
* The rows (`progress.state_line`, `compact.progress_line`, the band's agent
  note) read `round k of N` and the half that runs.
* A row that follows a loop parked for a person ends with the add-round
  command, and run.js narrates it.
* `swarm_fix_loop` calls FL-DRIVE's two routes, and says that it changes state.

No credentials, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_common.states import ParkReason
from test_plugin_agents_and_workflows import _ANSWERS, _SPEC, _run

from swarm_mcp import browse, check, compact, progress, server
from swarm_mcp.client import SwarmError

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
WID = "wf_0a1b2c3d4e5f60718293"
LOOP = "task_f00f00f00f00f00f00f0"
CONSOLE = f"https://console.invalid/workflows/{WID}"
#: Every §4.4 phase a loop parks for a person on (`fixloop.PERSON_PHASES`).
PHASES = ("fix_loop_exhausted", "fix_changed_nothing", "round_failed", "review_unreadable",
          "round_refused")
ADD_ROUND = f"swarm_fix_loop {{workflow_id: '{WID}', action: 'add_round'}}"


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _rounds(*halves: tuple[int, str]) -> list[dict[str, Any]]:
    return [{"round": k, "kind": kind, "task_id": f"task_round{k}{kind}"} for k, kind in halves]


def _loop(*, park_reason: str, phase: str | None = None, rounds: list[dict[str, Any]] | None = None,
          max_rounds: int = 3, added: list[dict[str, Any]] | None = None, findings: int = 4,
          parked_at: datetime = NOW - timedelta(hours=10)) -> dict[str, Any]:
    """A `fix-loop` step as the API serves it (FL-DRIVE's shapes)."""
    rounds = rounds if rounds is not None else _rounds((1, "review"), (2, "fix"), (2, "review"),
                                                        (3, "fix"), (3, "review"))
    state: dict[str, Any] = {"rounds": rounds}
    if added is not None:
        state["added"] = added
    task: dict[str, Any] = {
        "id": LOOP, "task_id": LOOP, "workflow_id": WID, "step_id": "loop",
        "runner_profile": "fix-loop", "state": "PARKED", "park_reason": park_reason,
        "created_at": _iso(parked_at - timedelta(minutes=5)),
        "links": {"console": CONSOLE},
        "dispatch": {"strategy": "integrate", "fix_loop": {"review": "review", "fix": "fix",
                                                           "max_rounds": max_rounds}},
        "metadata": {"fix_loop_state": state},
        "blocked_by": [],
    }
    if phase is not None:
        last = rounds[-1] if rounds else {}
        task["blocked_by"] = [{"park_phase": phase, "fix_loop": {
            "task_id": last.get("task_id"), "round": last.get("round"), "step": "re-review"}}]
        task["result_summary"] = {"fix_loop": {
            "rounds": rounds, "final_review": None, "final_head": None, "verdict": "NOT_YET",
            "findings_count": findings, "phase": phase}}
    return task


class _Reads:
    def __init__(self, parked_at: datetime) -> None:
        self._events = [{"type": "parked", "at": _iso(parked_at),
                         "detail": {"reason": "FIX_ROUNDS_RUNNING"}}]

    def events(self, task_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        return self._events

    def task(self, task_id: str) -> None:  # noqa: ARG002
        return None

    def capacity(self) -> tuple[None, str]:
        return None, "not read in this test"


# --------------------------------------------------------------------------
# 1. /sc check
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", PHASES)
def test_a_loop_parked_for_a_person_is_stuck_with_its_phase_and_the_add_round_command(phase):
    task = _loop(park_reason=ParkReason.HUMAN_REQUIRED.value, phase=phase)

    flag = check.classify_task(task, _Reads(NOW - timedelta(hours=2)), now=NOW, window=20)

    # MUTATION (check.py `_parked`): no fix-loop branch -> the generic
    # hand-off flag, with no phase and "/sc status" as the next command.
    assert flag is not None and flag["kind"] == check.STUCK and flag["rule"] == "parked"
    assert flag["evidence"]["park_phase"] == phase
    assert flag["evidence"]["fix_loop"] == {"round": 3, "rounds": 3, "findings_count": 4,
                                            "verdict": "NOT_YET"}
    assert phase in flag["reason"] and "round 3 of 3" in flag["reason"]
    assert flag["next"].startswith(ADD_ROUND)
    assert "action: 'stop'" in flag["next"]
    # Never the hand-off's sign-in: `human-ready` refuses a fix loop (§4.5).
    assert "sign in" not in flag["reason"] and "take control" not in flag["reason"]


def test_the_counter_adds_the_rounds_a_person_gave_never_past_the_ceiling():
    task = _loop(park_reason="HUMAN_REQUIRED", phase="fix_loop_exhausted", max_rounds=5,
                 added=[{"by": "a@example.test", "rounds": 4}, {"by": "b@example.test", "rounds": 9}])

    flag = check.classify_task(task, _Reads(NOW - timedelta(hours=2)), now=NOW, window=20)

    # MUTATION: no ceiling -> 18.
    assert flag["evidence"]["fix_loop"]["rounds"] == 10


@pytest.mark.parametrize("age", [timedelta(minutes=5), timedelta(minutes=91), timedelta(days=3)])
def test_a_loop_with_a_round_running_is_never_flagged_whatever_its_age(age):
    task = _loop(park_reason=ParkReason.FIX_ROUNDS_RUNNING.value, rounds=_rounds((1, "review"), (2, "fix")),
                 parked_at=NOW - age)

    # MUTATION: FIX_ROUNDS_RUNNING aged like CI_PENDING (rule c's 90 minutes)
    # -> flagged STUCK at 91 minutes and 3 days.
    assert check.classify_task(task, _Reads(NOW - age), now=NOW, window=20) is None


def test_a_person_park_that_is_not_a_loops_keeps_its_own_flag():
    task = _loop(park_reason="HUMAN_REQUIRED")
    task["runner_profile"] = "claude-code"
    task["dispatch"] = {"strategy": "direct-pr"}
    task["metadata"] = {}
    task["blocked_by"] = [{"park_phase": "human_wait", "cause": "captcha"}]

    flag = check.classify_task(task, _Reads(NOW - timedelta(hours=2)), now=NOW, window=20)

    assert flag is not None and "fix_loop" not in flag["evidence"] and "next" not in flag


# --------------------------------------------------------------------------
# 2. The rows: round k of N
# --------------------------------------------------------------------------


def test_the_words_name_the_round_the_half_running_and_the_outcome():
    running = _loop(park_reason="FIX_ROUNDS_RUNNING", rounds=_rounds((1, "review"), (2, "fix")))
    reviewing = _loop(park_reason="FIX_ROUNDS_RUNNING",
                      rounds=_rounds((1, "review"), (2, "fix"), (2, "review")))
    waiting = _loop(park_reason="HUMAN_REQUIRED", phase="round_failed")
    fresh = _loop(park_reason="FIX_ROUNDS_RUNNING", rounds=[], max_rounds=2)
    merged = _loop(park_reason="", rounds=_rounds((1, "review"), (2, "fix"), (2, "review")))
    merged.update(state="SUCCEEDED", park_reason=None)
    merged["result_summary"] = {"fix_loop": {"rounds": _rounds((1, "review"), (2, "fix"), (2, "review")),
                                             "final_review": "task_round2review", "verdict": "MERGE"}}

    assert progress.fix_loop_words(running) == "round 2 of 3 · fix running"
    assert progress.fix_loop_words(reviewing) == "round 2 of 3 · re-review running"
    assert progress.fix_loop_words(waiting) == "round 3 of 3 · waiting for you: round_failed"
    assert progress.fix_loop_words(fresh) == "round 0 of 2 · no round started yet"
    assert progress.fix_loop_words(merged) == "round 2 of 3 · MERGE after 2 rounds"
    assert progress.fix_loop_words({"task_id": "task_x", "state": "RUNNING", "runner_profile": "claude-code"}) is None


def test_a_status_line_and_a_progress_row_show_the_round():
    task = _loop(park_reason="FIX_ROUNDS_RUNNING", rounds=_rounds((1, "review"), (2, "fix")))
    task["read"] = "ok"

    # MUTATION: state_line without the loop's words -> "its review-fix rounds
    # are running" alone, no counter.
    assert "round 2 of 3 · fix running" in progress.state_line(task)
    line, _key = compact.progress_line(object(), task, NOW)
    assert "round 2 of 3 · fix running" in line


def test_the_bands_agent_note_shows_the_round():
    task = _loop(park_reason="FIX_ROUNDS_RUNNING", rounds=_rounds((1, "review"), (2, "fix"), (2, "review")))

    assert browse._task_note(task) == "round 2 of 3 · re-review running"


class _Fake:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wall(self) -> datetime:
        return NOW + timedelta(seconds=self.now)

    def task(self, task_id: str) -> dict:  # noqa: ARG002
        return json.loads(json.dumps(self.doc))

    def attempts(self, task_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def events_page(self, task_id: str, *, limit: int = 100, newest_first: bool = False):  # noqa: ARG002
        return [], None, True

    def artifact_content(self, task_id: str, name: str, *, limit_bytes: int = 0):  # noqa: ARG002
        return {"status": "missing"}


def test_a_row_following_a_loop_parked_for_a_person_ends_with_the_add_round_command():
    fake = _Fake(_loop(park_reason="HUMAN_REQUIRED", phase="fix_loop_exhausted"))

    got = compact.watch_progress(fake, [LOOP], step_id="loop", wait_seconds=1800, sleep=fake.sleep,
                                 clock=fake.clock, now=fake.wall)

    assert got["stop"] is True and got["stop_reason"] == "waiting_on_person"
    wait = got["result"]["waiting_on_person"]
    # MUTATION (compact.py `person_wait`): no fix-loop kind -> a hand-off
    # naming POST .../human-ready, which the API refuses for a loop (409).
    assert wait["kind"] == "fix_loop" and wait["park_phase"] == "fix_loop_exhausted"
    assert wait["fix_loop"] == "round 3 of 3 · waiting for you: fix_loop_exhausted"
    assert wait["add_round"] == ADD_ROUND
    assert "human_ready_route" not in wait
    assert ADD_ROUND in got["result"]["last_error"]
    assert "fix loop round 3 of 3" in got["progress"][-1]


def test_run_js_narrates_a_loop_parked_for_a_person_with_its_command(tmp_path):
    answer = {"state": "PARKED", "result": {
        "state": "PARKED", "answer_excerpt": "", "pr_url": "", "artifacts": [], "questions": [],
        "last_error": "PARKED on HUMAN_REQUIRED", "console": CONSOLE,
        "waiting_on_person": {
            "kind": "fix_loop", "since": _iso(NOW), "since_clock": "09:00Z", "workflow_id": WID,
            "step_id": "report", "console": CONSOLE, "questions": [], "park_phase": "fix_loop_exhausted",
            "fix_loop": "round 3 of 3 · waiting for you: fix_loop_exhausted", "add_round": ADD_ROUND,
        }}}
    answers = {**_ANSWERS, "step:report": answer,
               "STATUS": {"state": "RUNNING", "state_note": None, "steps": [{"step_id": "report", "state": "PARKED"}]}}

    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    report = {row["step_id"]: row for row in got["result"]["steps"]}["report"]
    # MUTATION (run.js `personWait`): every kind but owner_questions read as a
    # hand-off -> "sign in on its console page", and the command dropped.
    assert report["waiting_on_person"]["kind"] == "fix_loop"
    assert report["waiting_on_person"]["add_round"] == ADD_ROUND
    assert any("report PARKED · WAITING ON A PERSON since 09:00Z · fix loop round 3 of 3" in line
               and ADD_ROUND in line for line in got["logs"]), got["logs"]


# --------------------------------------------------------------------------
# 3. swarm_fix_loop
# --------------------------------------------------------------------------


class _Api:
    tenant = "eng"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        self.calls.append((method, path, payload))
        return {"fix_loop": {"rounds_added": 1, "max_rounds": 4}}


def _tool() -> dict[str, Any]:
    return next(t for t in server.TOOLS if t["name"] == "swarm_fix_loop")


def test_the_tool_says_it_changes_state_and_takes_only_its_arguments():
    tool = _tool()
    assert "changes state" in tool["description"].lower()
    assert set(tool["inputSchema"]["properties"]) == {"workflow_id", "action", "rounds", "note"}
    assert tool["inputSchema"]["properties"]["action"]["enum"] == ["add_round", "stop"]
    assert tool["inputSchema"]["required"] == ["workflow_id", "action"]
    assert server._holds("swarm_fix_loop", {"workflow_id": WID, "action": "stop"}) is False


def test_add_round_posts_the_rounds_route_with_the_note():
    api = _Api()
    note = "focus on the retry path " + "x" * 8

    got = json.loads(server._call(api, "swarm_fix_loop", {"workflow_id": WID, "action": "add_round", "note": note}))

    assert api.calls == [("POST", f"/v1/workflows/{WID}/fix-loop/rounds", {"rounds": 1, "note": note})]
    assert got["fix_loop"]["rounds_added"] == 1


def test_add_round_without_a_note_sends_none_and_may_ask_for_more_rounds():
    api = _Api()
    server._call(api, "swarm_fix_loop", {"workflow_id": WID, "action": "add_round", "rounds": 2})
    assert api.calls == [("POST", f"/v1/workflows/{WID}/fix-loop/rounds", {"rounds": 2})]


def test_stop_posts_the_end_route():
    api = _Api()
    server._call(api, "swarm_fix_loop", {"workflow_id": WID, "action": "stop"})
    assert api.calls == [("POST", f"/v1/workflows/{WID}/fix-loop/end", {"outcome": "stop"})]


@pytest.mark.parametrize("args", [
    {"workflow_id": WID, "action": "merge"},
    {"workflow_id": WID},
    {"workflow_id": "", "action": "stop"},
    {"workflow_id": "../tasks", "action": "stop"},
    {"workflow_id": WID, "action": "stop", "note": "no"},
    {"workflow_id": WID, "action": "add_round", "rounds": 0},
    {"workflow_id": WID, "action": "add_round", "rounds": True},
    {"workflow_id": WID, "action": "add_round", "note": 7},
    {"workflow_id": WID, "action": "stop", "verdict": "MERGE"},
])
def test_a_wrong_call_is_refused_and_sends_nothing(args):
    api = _Api()
    with pytest.raises(SwarmError):
        server._call(api, "swarm_fix_loop", args)
    assert api.calls == []


def test_the_skill_names_the_tool_and_the_rows():
    text = (Path(__file__).resolve().parents[3] / "plugin" / "skills" / "sc" / "SKILL.md").read_text()
    assert "swarm_fix_loop" in text and "round k of N" in text


# --------------------------------------------------------------------------
# 4. The band (plugin/hooks/pane/model.ts, under node; band.test.ts holds the engine)
# --------------------------------------------------------------------------


def test_the_band_counts_a_loop_parked_for_you_and_not_one_with_a_round_running(tmp_path):
    from test_sc_band import READ_AT, WORKFLOW, _band, _state

    def loop(**step: Any) -> dict[str, Any]:
        return {**WORKFLOW, "steps": [{**s, "state": "PARKED", **step} if s["step_id"] == "integrate" else s
                                      for s in WORKFLOW["steps"]]}

    running = _state(workflows=[loop(park_reason="FIX_ROUNDS_RUNNING")], listReadAt=READ_AT)
    waiting = _state(workflows=[loop(park_reason="HUMAN_REQUIRED", park_phase="fix_loop_exhausted")],
                     listReadAt=READ_AT)

    got = _band(tmp_path, "[m.summaryText(a), m.summaryText(b), m.noticesOf(a.workflows[0]), m.noticesOf(b.workflows[0])]",
                a=running, b=waiting)

    assert got[0] == "SwarmCloud · 1 running"
    assert got[1] == "SwarmCloud · 1 running · 1 waits on you"
    assert got[2] == []
    # MUTATION (model.ts `noticesOf`): the generic "integrate waits for a person".
    assert got[3] == ["integrate: fix loop waits for you (fix_loop_exhausted)"]

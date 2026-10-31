"""A row for a step a person must act on ends; a row never shows a frozen age or hammers the API.

MEASURED, 2026-10-26 (#2441; BGD decision 08:1xZ). Seven merge steps sat
PARKED HUMAN_REQUIRED on owner questions for 3 to 7 hours:

* each one's `sc:step` row called `swarm_follow` with `wait_seconds: 1800`
  again and again, because the bridge answered `stop: false` for that park --
  27 to 35 calls and 0.32-0.46M tokens a row;
* a row writes a line only on a change, so the panel showed "PARKED · waiting
  7s" for 4.5 hours;
* one row's 30-minute holds came back after a median of 3.3 s (119 calls in
  2h14m, 2.1M tokens);
* after that row died, run.js's Result phase attached a second row for the
  same step.

So: a follow of a task PARKED on HUMAN_REQUIRED stops, `stop_reason:
"waiting_on_person"`, with what the person must do; a wait line says when
the wait began, and is handed out again at most every 30 minutes; holds that
keep returning fast make the row wait, then end it; a step past its parents
holds like any other (the cause of the 3.3 s holds); `new_only` attaches a
step that resumed after its row ended waiting; and the Result phase never
re-attaches a task another live row of the run follows.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from test_plugin_agents_and_workflows import _ANSWERS, _RUN_JS, _SPEC, _node, _run

compact = importlib.import_module("swarm_mcp.compact")
launch = importlib.import_module("swarm_mcp.launch")
server = importlib.import_module("swarm_mcp.server")
from swarm_mcp.client import SwarmError  # noqa: E402

#: The wall clock of every test: 07:35Z, as the issue's last reading.
T0 = datetime(2026, 10, 26, 7, 35, tzinfo=timezone.utc)
#: When the merge parked: 03:05Z, 4h 30m before T0.
PARKED_AT = datetime(2026, 10, 26, 3, 5, tzinfo=timezone.utc)
CONSOLE = "https://console.invalid/tasks/task_m"


class Fake:
    """The client surface `compact` reads, over documents a test edits, on a
    simulated clock: `sleep` moves `clock` and `wall` together."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.now = 0.0
        self.reads: list[str] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now)

    def task(self, task_id: str) -> dict:
        self.reads.append(task_id)
        if task_id not in self.docs:
            raise SwarmError(f"HTTP 404: no task {task_id}", status=404)
        return json.loads(json.dumps(self.docs[task_id]))

    def attempts(self, task_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def events_page(self, task_id: str, *, limit: int = 100, newest_first: bool = False):  # noqa: ARG002
        return [], None, True

    def artifact_content(self, task_id: str, name: str, *, limit_bytes: int = 0):  # noqa: ARG002
        content = self.files.get(f"{task_id}/{name}")
        if content is None:
            return {"status": "missing"}
        return {"status": "ok", "content": content, "truncated": False}


def _merge_held_by_owner_questions(fake: Fake) -> None:
    """FAILURES-1026's merge: PARKED on HUMAN_REQUIRED by the owner-question
    hold, naming question 1 of the implement step (task_impl)."""
    fake.docs["task_impl"] = {
        "task_id": "task_impl", "step_id": "implement", "state": "SUCCEEDED",
        "result_summary": {"questions": 2},
    }
    fake.files["task_impl/questions.json"] = json.dumps([
        {"question": "Q0: which pool?", "options": [{"label": "a", "description": "pool a"}],
         "recommended": "a", "context": "c0"},
        {"question": "Q1: keep the fallback route?",
         "options": [{"label": "keep", "description": "as built"}, {"label": "drop", "description": "remove it"}],
         "recommended": "keep", "context": "the review names it"},
    ])
    fake.docs["task_m"] = {
        "task_id": "task_m", "step_id": "merge", "workflow_id": "wf_e3", "state": "PARKED",
        "park_reason": "HUMAN_REQUIRED", "updated_at": PARKED_AT.isoformat(),
        "created_at": (PARKED_AT - timedelta(hours=3)).isoformat(),
        "links": {"console": CONSOLE},
        "blocked_by": [{
            "reason": "HUMAN_REQUIRED", "park_phase": "owner_questions",
            "owner_questions": {"code": "OWNER_QUESTIONS", "unreadable": 0, "questions": [
                {"task_id": "task_impl", "step_id": "implement", "question_index": 1,
                 "question": "the hold's copy of Q1"},
            ]},
        }],
    }


def _follow(fake: Fake, task_ids: list[str], **kwargs) -> dict:
    return compact.watch_progress(fake, task_ids, sleep=fake.sleep, clock=fake.clock, now=fake.wall, **kwargs)


# --------------------------------------------------------------------------
# 1. A person-wait ends the row
# --------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", ["merge", None], ids=["step row", "task row"])
def test_a_follow_of_a_task_parked_for_the_owners_questions_stops_with_them(step_id):
    fake = Fake()
    _merge_held_by_owner_questions(fake)

    got = _follow(fake, ["task_m"], step_id=step_id, wait_seconds=1800)

    assert got["stop"] is True, "a row must not hold, hour after hour, for a person"
    assert got["stop_reason"] == "waiting_on_person"
    assert fake.now == 0, "the stop is answered at once, not after a hold"
    result = got["result"]
    assert result["state"] == "PARKED"
    wait = result["waiting_on_person"]
    assert wait["kind"] == "owner_questions"
    (question,) = wait["questions"]
    # The asking step and index, and the question's OWN text -- read from the
    # asking task's questions, not the hold's copy.
    assert question["task_id"] == "task_impl" and question["step_id"] == "implement"
    assert question["question_index"] == 1
    assert question["question"] == "Q1: keep the fallback route?"
    assert [o["label"] for o in question["options"]] == ["keep", "drop"]
    assert wait["console"] == CONSOLE and result["console"] == CONSOLE
    # The row's last line.
    last = got["progress"][-1]
    assert last.startswith("[merge"), last
    assert last.endswith("] WAITING ON A PERSON since 03:05Z · 1 owner question · console: " + CONSOLE), last


def test_the_same_follow_of_a_task_parked_on_ci_does_not_stop():
    fake = Fake()
    fake.docs["task_m"] = {
        "task_id": "task_m", "step_id": "merge", "state": "PARKED", "park_reason": "CI_PENDING",
        "updated_at": PARKED_AT.isoformat(), "links": {"console": CONSOLE},
    }
    first = _follow(fake, ["task_m"])
    got = _follow(fake, ["task_m"], since=first["since"], wait_seconds=1800)

    assert got["stop"] is False and "stop_reason" not in got and "result" not in got
    assert fake.now >= 1800, "a CI park is held, not ended, for a follow that names no step"


def test_a_hand_off_stops_with_the_console_and_the_human_ready_route():
    fake = Fake()
    fake.docs["task_b"] = {
        "task_id": "task_b", "step_id": "browse", "workflow_id": "wf_1", "state": "PARKED",
        "park_reason": "HUMAN_REQUIRED", "updated_at": PARKED_AT.isoformat(),
        "links": {"console": CONSOLE},
        "blocked_by": [{"reason": "HUMAN_REQUIRED", "park_phase": "human_wait", "cause": "captcha"}],
    }

    got = _follow(fake, ["task_b"], step_id="browse", wait_seconds=1800)

    assert got["stop"] is True and got["stop_reason"] == "waiting_on_person"
    wait = got["result"]["waiting_on_person"]
    assert wait["kind"] == "hand_off" and wait["questions"] == []
    assert wait["human_ready_route"] == "POST /v1/tasks/task_b/human-ready"
    assert wait["console"] == CONSOLE
    assert "] WAITING ON A PERSON since 03:05Z · hand-off" in got["progress"][-1]


def test_a_question_wait_stops_with_its_own_questions_not_a_sign_in():
    # #2859: the agent's own blocking question, parked HUMAN_REQUIRED `question_wait`.
    fake = Fake()
    fake.docs["task_q"] = {
        "task_id": "task_q", "step_id": "implement", "workflow_id": "wf_v", "state": "PARKED",
        "park_reason": "HUMAN_REQUIRED", "updated_at": PARKED_AT.isoformat(),
        "links": {"console": CONSOLE},
        "blocked_by": [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait", "question_wait": {
            "questions": [{"question": "Refuse or warn?"}, {"question": "Not blocking"},
                          {"question": "Keep the route?"}],
            "blocking": [0, 2], "attempt_id": "att_ask"}}],
    }

    got = _follow(fake, ["task_q"], step_id="implement", wait_seconds=1800)

    assert got["stop"] is True and got["stop_reason"] == "waiting_on_person"
    wait = got["result"]["waiting_on_person"]
    # MUTATION: reading a question wait as a hand-off turns this red.
    assert wait["kind"] == "owner_questions" and wait["park_phase"] == "question_wait"
    assert [(q["task_id"], q["question_index"]) for q in wait["questions"]] == [("task_q", 0), ("task_q", 2)]
    # MUTATION: offering human-ready, which resumes it unanswered, turns this red.
    assert "human_ready_route" not in wait
    assert "sign in" not in got["result"]["last_error"]
    assert "answer the 2 blocking questions its agent asked" in got["result"]["last_error"]
    assert "] WAITING ON A PERSON since 03:05Z · 2 owner questions" in got["progress"][-1]


def test_the_bridge_remembers_a_row_that_ended_waiting_on_a_person():
    fake = Fake()
    _merge_held_by_owner_questions(fake)
    launch.forget_attached()

    reply = json.loads(server._call(fake, "swarm_follow", {
        "task_ids": ["task_m"], "format": "progress", "step_id": "merge", "wait_seconds": 0,
    }))

    assert reply["stop_reason"] == "waiting_on_person"
    assert launch._PERSON_WAITS["task_m"] == ("wf_e3", "merge")
    launch.forget_attached()


# --------------------------------------------------------------------------
# 3. Honest ages
# --------------------------------------------------------------------------


def test_a_progress_line_for_a_wait_carries_when_the_wait_began():
    fake = Fake()
    fake.docs["task_q"] = {
        "task_id": "task_q", "step_id": "review", "state": "PARKED", "park_reason": "DEPENDENCY_INCOMPLETE",
        "updated_at": PARKED_AT.isoformat(),
    }

    (line,) = _follow(fake, ["task_q"])["progress"]

    assert "parked since 03:05Z (4h 30m)" in line, line
    assert "waiting 4h" not in line, "an elapsed count alone is frozen at the moment of the change"


def test_an_unchanged_line_is_handed_out_again_once_thirty_minutes_have_passed():
    fake = Fake()
    fake.docs["task_q"] = {
        "task_id": "task_q", "step_id": "review", "state": "QUEUED", "updated_at": PARKED_AT.isoformat(),
    }
    first = _follow(fake, ["task_q"])

    short = _follow(fake, ["task_q"], since=first["since"], wait_seconds=600)
    assert short["progress"] == [] and short["changed"] is False

    held = _follow(fake, ["task_q"], since=short["since"], wait_seconds=1800)

    assert held["transitions"] == [], "a refresh is not a transition"
    assert held["refreshed"] is True and held["changed"] is True
    (line,) = held["progress"]
    assert "waiting since 03:05Z (5h 10m)" in line, line


# --------------------------------------------------------------------------
# 4. No hammering
# --------------------------------------------------------------------------


class _CollapsingClock:
    """A hold that does not hold: each call's deadline reads as passed at once,
    and the call took `took` seconds."""

    def __init__(self, took: float) -> None:
        self.took = took
        self.reads = 0

    def __call__(self) -> float:
        # began, the deadline check (past it), the duration.
        self.reads += 1
        return {1: 0.0, 2: 1e6, 0: self.took}[self.reads % 3]


def _fast_call(fake: Fake, since: str, took: float) -> dict:
    return compact.watch_progress(fake, ["task_q"], step_id="review", since=since, wait_seconds=1800,
                                  sleep=fake.sleep, clock=_CollapsingClock(took), now=fake.wall)


def test_three_fast_unchanged_returns_make_the_row_wait_and_five_end_it():
    fake = Fake()
    fake.docs["task_q"] = {
        "task_id": "task_q", "step_id": "review", "state": "QUEUED", "updated_at": T0.isoformat(),
        "links": {"console": CONSOLE},
    }
    reply = _follow(fake, ["task_q"], step_id="review")
    took = [3.1, 2.9, 3.4, 3.3, 3.2]
    replies = []
    for seconds in took:
        reply = _fast_call(fake, reply["since"], seconds)
        replies.append(reply)

    assert [r["stop"] for r in replies[:2]] == [False, False]
    for third in replies[2:4]:
        assert third["stop"] is True and third["stop_reason"] == "hold_wait"
        assert third["result"]["state"] == "HOLD_WAIT"
    last = replies[4]
    assert last["stop"] is True and last["stop_reason"] == "hold_not_holding"
    assert last["result"]["state"] == "UNKNOWN"
    why = last["result"]["last_error"]
    assert "not holding" in why and all(f"{s:.1f} s" in why for s in took), why


def test_a_call_that_held_clears_the_streak():
    fake = Fake()
    fake.docs["task_q"] = {"task_id": "task_q", "step_id": "review", "state": "QUEUED", "updated_at": T0.isoformat()}
    reply = _follow(fake, ["task_q"], step_id="review")
    reply = _fast_call(fake, reply["since"], 3.0)
    reply = _fast_call(fake, reply["since"], 3.0)
    reply = _follow(fake, ["task_q"], step_id="review", since=reply["since"], wait_seconds=1800)
    assert reply["held_s"] >= 1800 and "fast_returns" not in reply
    reply = _fast_call(fake, reply["since"], 3.0)
    assert reply["stop"] is False, "a streak broken by a real hold starts again"


def test_a_step_past_its_parents_holds_instead_of_answering_after_one_read():
    """The cause of the 3.3 s holds: a row that kept passing `parents` after
    they had finished had each call answered after one read."""
    fake = Fake()
    fake.docs["task_parent"] = {"task_id": "task_parent", "step_id": "re-review", "state": "SUCCEEDED"}
    fake.docs["task_q"] = {"task_id": "task_q", "step_id": "merge", "state": "READY", "updated_at": T0.isoformat()}
    first = _follow(fake, ["task_q"], step_id="merge", parents=["task_parent"])

    got = _follow(fake, ["task_q"], step_id="merge", parents=["task_parent"], since=first["since"],
                  wait_seconds=1800)

    assert fake.now >= 1800, f"the hold returned after {fake.now} s"
    assert got["stop"] is False and got["parents"] == {"task_parent": "SUCCEEDED"}


# --------------------------------------------------------------------------
# 2. Re-attach on resume
# --------------------------------------------------------------------------


def test_new_only_attaches_a_step_that_resumed_and_not_one_still_parked(monkeypatch):
    fake = Fake()
    fake.docs["task_m"] = {"task_id": "task_m", "state": "READY"}
    fake.docs["task_k"] = {"task_id": "task_k", "state": "PARKED", "park_reason": "HUMAN_REQUIRED"}
    launch.forget_attached()
    launch.remember_attached(["wf_resumed", "wf_parked"])
    launch.remember_person_wait("task_m", "wf_resumed", "merge")
    launch.remember_person_wait("task_k", "wf_parked", "merge")

    workflows, tasks = launch.resumed_person_waits(fake)

    assert workflows == {"wf_resumed": {"merge": "task_m"}} and tasks == set()

    seen: dict = {}

    def scoped(entries, kind, key, skip, *rest, **kw):  # noqa: ARG001
        seen.setdefault(kind, skip)
        kept = [e for e in entries if str(e.get("workflow_id") or e.get("task_id")) not in skip]
        return kept, [str(e.get("workflow_id")) for e in entries if str(e.get("workflow_id")) in skip], []

    shaped: dict = {}

    def attach_launches(workflow_id, *args, only_steps=None, **kw):  # noqa: ARG001
        shaped[workflow_id] = only_steps
        launch.remember_attached([workflow_id])
        return [{"workflow_id": workflow_id}], [], 1

    sc = importlib.import_module("swarm_mcp.sc")
    session = importlib.import_module("swarm_mcp.session")
    listing = {"workflows": [{"workflow_id": "wf_resumed"}, {"workflow_id": "wf_parked"}]}
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"tasks": []})
    monkeypatch.setattr(sc, "running_workflows", lambda client, **kw: listing)
    monkeypatch.setattr(sc, "group_by_workspace", lambda entries: entries)
    monkeypatch.setattr(launch, "named_workspaces", lambda entries, names, **kw: entries)
    monkeypatch.setattr(launch, "_own", lambda entries, sid, left_out: entries)
    monkeypatch.setattr(launch, "_scoped", scoped)
    monkeypatch.setattr(launch, "_read_workflows", lambda client, ids: {})
    monkeypatch.setattr(launch, "_attach_launches", attach_launches)
    monkeypatch.setattr(launch.workflows, "workspace_names", lambda client: {})
    monkeypatch.setattr(launch.details, "account_labels", lambda client: {})
    monkeypatch.setattr(session, "current", lambda: "sid")
    monkeypatch.setattr(session, "project_record", lambda: None)

    out = launch.for_all(fake, repository="all", new_only=True, rows="per-step")

    assert shaped == {"wf_resumed": ["merge"]}, "the resumed step alone, and never the one still parked"
    assert "wf_parked" in seen["workflow"] and "wf_resumed" not in seen["workflow"]
    assert [c["workflow_id"] for c in out["launches"]] == ["wf_resumed"]
    # Never twice: the entry is spent, so the next check leaves it out again.
    assert "task_m" not in launch._PERSON_WAITS and "task_k" in launch._PERSON_WAITS
    launch.forget_attached()


def test_a_resumed_steps_copy_follows_that_step_alone():
    envelope = {
        "workflow": {"workflow_id": "wf_1", "steps": [
            {"step_id": "implement"}, {"step_id": "review"}, {"step_id": "merge", "depends_on": ["review"]}]},
        "tasks": [],
    }
    rows = [
        {"step_id": "implement", "state": "SUCCEEDED", "depends_on": []},
        {"step_id": "review", "state": "SUCCEEDED", "depends_on": ["implement"]},
        {"step_id": "merge", "state": "READY", "depends_on": ["review"]},
    ]
    copies: list = []

    def copy(title, description, share=None):
        copies.append(share)
        return {"title": title}

    original = launch.workflows.step_rows
    try:
        launch.workflows.step_rows = lambda env: rows
        _, left, used = launch._shaped("per-step", copy, "SC · lane · 3 steps", "wf_1", "lane", 3,
                                       envelope, None, ["merge"])
    finally:
        launch.workflows.step_rows = original

    (share,) = copies
    assert share["steps"] == ["merge"] and used == 1 and left == []
    assert sorted(share["others"]) == ["implement", "review"], "no finished step is reported again"


# --------------------------------------------------------------------------
# 5. One row per step: run.js
# --------------------------------------------------------------------------


def _reattachable(rows: list, live: dict) -> list:
    """run.js's reattachable(), with LIVE_ROWS as given, evaluated by node."""
    source = _RUN_JS.read_text()
    pieces = []
    for start, end, keep in (("const TERMINAL = ", "\n", True), ("const LIVE_ROWS = {}", "\nasync function liveAgent", False),
                             ("function statusSteps(final) {", "\n}\n", True),
                             ("function reattachable(wf, rows, final) {", "\n}\n", True)):
        at = source.index(start)
        pieces.append(source[at:source.index(end, at) + (len(end) if keep else 0)])
    wf = {"all": [{"step_id": "merge", "task_id": "task_m"}, {"step_id": "fix", "task_id": "task_f"}]}
    final = {"state": "RUNNING", "steps": [{"step_id": "merge", "state": "RUNNING"}, {"step_id": "fix", "state": "RUNNING"}]}
    script = "\n".join(pieces) + (
        f"\nObject.assign(LIVE_ROWS, {json.dumps(live)})\n"
        f"process.stdout.write(JSON.stringify(reattachable({json.dumps(wf)}, {json.dumps(rows)}, {json.dumps(final)})"
        ".map((s) => s.step_id)))\n"
    )
    done = subprocess.run([_node(), "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_result_phase_does_not_reattach_a_task_another_live_row_follows():
    lost = [{"step_id": "merge", "state": "UNKNOWN"}, {"step_id": "fix", "state": "UNKNOWN"}]

    assert _reattachable(lost, {}) == ["merge", "fix"]
    assert _reattachable(lost, {"task_m": 1}) == ["fix"]


def test_a_row_that_ended_waiting_on_a_person_is_not_reattached():
    rows = [{"step_id": "merge", "state": "PARKED", "waiting_on_person": {"kind": "owner_questions"}},
            {"step_id": "fix", "state": "UNKNOWN", "waiting_on_person": {"kind": "hand_off"}}]
    assert _reattachable(rows, {}) == []


def _parked_merge_answer() -> dict:
    return {"state": "PARKED", "result": {
        "state": "PARKED", "answer_excerpt": "", "pr_url": "", "artifacts": [], "questions": [],
        "last_error": "PARKED on HUMAN_REQUIRED: waiting on a person since 03:05Z", "console": CONSOLE,
        "waiting_on_person": {
            "kind": "owner_questions", "since": PARKED_AT.isoformat(), "since_clock": "03:05Z",
            "workflow_id": "wf_1", "step_id": "report", "console": CONSOLE,
            "questions": [{"task_id": "task_3", "step_id": "join", "question_index": 1,
                           "question": "Q1: keep it?", "options": [{"label": "keep", "description": ""}],
                           "recommended": "keep", "context": ""}],
        }}}


def test_a_step_waiting_on_a_person_ends_parked_with_its_questions_under_the_asking_task(tmp_path):
    answers = {**_ANSWERS, "step:report": _parked_merge_answer(),
               "STATUS": {"state": "RUNNING", "state_note": None, "steps": [{"step_id": "report", "state": "PARKED"}]}}

    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    report = {row["step_id"]: row for row in got["result"]["steps"]}["report"]
    assert report["state"] == "PARKED" and report["waiting_on_person"]["kind"] == "owner_questions"
    assert "reattached" not in got["result"]
    assert sum(1 for call in got["calls"] if call["label"].endswith("· report")) == 1
    asked = [e for e in got["result"]["owner_questions"] if e["task_id"] == "task_3"]
    assert asked and asked[0]["questions"][0]["index"] == 1, got["result"]["owner_questions"]
    assert any("report PARKED · WAITING ON A PERSON since 03:05Z · 1 owner question" in line
               for line in got["logs"]), got["logs"]


def test_a_row_whose_holds_came_back_fast_waits_then_ends(tmp_path):
    hold_wait = {"state": "HOLD_WAIT", "result": {
        "state": "HOLD_WAIT", "answer_excerpt": "", "pr_url": "", "artifacts": [], "questions": [],
        "last_error": "the hold is not holding: 3 swarm_follow calls in a row (3.1 s, 2.9 s, 3.4 s)",
        "console": ""}}
    answers = {**_ANSWERS, "step:report": hold_wait, "WAIT": {"waited": True, "error": None},
               "STATUS": {"state": "RUNNING", "state_note": None, "steps": [{"step_id": "report", "state": "QUEUED"}]}}

    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    order = [("wait" if c["agentType"] == "sc:wait" else "row")
             for c in got["calls"] if c["label"].endswith("· report") or c["agentType"] == "sc:wait"]
    assert order[:5] == ["row", "wait", "row", "wait", "row"], order
    waits = [c for c in got["calls"] if c["agentType"] == "sc:wait"]
    assert all(c["prompt"] == "WAIT\nseconds: 60" for c in waits)
    report = {row["step_id"]: row for row in got["result"]["steps"]}["report"]
    assert report["state"] != "HOLD_WAIT", "a wait answer is never a step's result"

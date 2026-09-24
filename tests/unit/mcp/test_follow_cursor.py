"""`swarm_follow` -- the resumable cursor, against the REAL swarm-api.

WHY NOT A FAKE CLIENT. `test_bridge.py` says it in its own header: a fake
answers whatever its consumer asks for, and this module's entire subject is
whether a cursor built here addresses the bytes the SERVER really serves. The
two coordinate systems it translates between -- an offset into a rolling 256KB
tail object, and a position in the stream that object is a window onto -- exist
only in `swarm_api.inspect`, so a stand-in for that route would be a stand-in
for the thing under test. The real FastAPI application is built here over an
in-memory Firestore and an in-memory object store, and `SwarmClient`'s own
transport is pointed at it. Offline: no credentials, no emulator, no network.

THE FOUR PROPERTIES PROVED HERE, because they are the four the tool promises:

  * the cursor ADVANCES, and the second call returns only what the first did
    not (`test_the_cursor_advances_*`, `test_two_calls_never_repeat_*`);
  * the cap is REPORTED (`test_the_log_cap_is_reported_and_never_silent`,
    `test_the_budget_stops_later_streams_and_names_each_one`) -- a silent
    truncation reads as "that was all the output", which is the defect this
    repository keeps finding;
  * absence is never collapsed: a task that cannot be read, a task with no
    events, an attempt that has not started and an object that could not be
    read are four different answers;
  * several tasks in one call keep independent cursors.

`tests/unit` is on sys.path via this directory's conftest, which says why.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_common.states import EventType

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from swarm_mcp import cli
from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmClient, SwarmError
from swarm_mcp.follow import follow

from control_plane.conftest import PROJECT, api_settings, seed_task, seed_tenant

AUTH = {"Authorization": "Bearer token-alice"}
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
TENANT = "eng"


# --------------------------------------------------------------------------
# The application, the object store, and the real client wired to both
# --------------------------------------------------------------------------


class World:
    """Everything one test needs, in one object.

    It differs from `test_against_the_real_api.py`'s `api` fixture in exactly
    one way that matters: it wires an `InMemoryObjectReader`, without which
    `GET /v1/tasks/{id}/logs` has nothing to serve and the half of this tool
    that pages bytes cannot be exercised at all.
    """

    def __init__(self) -> None:
        from control_plane.fakes import FakeFirestore

        self.db = FakeFirestore()
        self.objects = InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}")
        self.ctx = build_context(
            settings=api_settings(),
            db=self.db,
            verifier=StaticTokenVerifier(
                {
                    "token-alice": {
                        "email": "alice@saga.xyz",
                        "email_verified": True,
                        "sub": "sub-alice",
                        "hd": "saga.xyz",
                    }
                }
            ),
            groups=StaticGroups({"alice@saga.xyz": (f"{TENANT}@saga.xyz",)}),
            credentials=InMemoryCredentials(),
            waker=NullWaker(),
            metrics=ApiMetrics(),
            objects=self.objects,
        )
        seed_tenant(self.db, TENANT)
        self.api = TestClient(create_app(self.ctx), raise_server_exceptions=False)

    # -- seeding -----------------------------------------------------------
    def task(self, task_id: str, *, state: str = "RUNNING") -> str:
        seed_task(self.db, task_id=task_id, tenant_id=TENANT, state=state)
        return task_id

    def set_state(self, task_id: str, state: str) -> None:
        self.db.docs[f"tasks/{task_id}"]["state"] = state

    def attempt(self, task_id: str, attempt_id: str, *, minutes_ago: int = 5) -> None:
        created = NOW - timedelta(minutes=minutes_ago)
        self.db.collection("attempts").document(attempt_id).set(
            {
                "attempt_id": attempt_id,
                "task_id": task_id,
                "tenant_id": TENANT,
                "generation": 1,
                "lease_id": f"lease_{attempt_id}",
                "backend": "CLOUD_RUN_JOB",
                "execution_name": f"swarm-job-{TENANT}-mock-{attempt_id}",
                "created_at": created,
                "started_at": created + timedelta(seconds=5),
                "completed_at": None,
                "exit_code": None,
                "error": None,
                "peak_rss_bytes": None,
                "oom_near_miss": False,
                "checkpoints": [],
            }
        )

    def event(self, task_id: str, kind: EventType) -> None:
        self.ctx.store.append_event(task_id=task_id, tenant_id=TENANT, type=kind)

    # -- object keys -------------------------------------------------------
    @staticmethod
    def prefix(task_id: str, attempt_id: str) -> str:
        return f"tenants/{TENANT}/tasks/{task_id}/attempts/{attempt_id}"

    def live(self, task_id: str, attempt_id: str, text: str, *, window_start: int = 0,
             stream_size: int | None = None, stream: str = "stdout") -> None:
        """Publish a live tail exactly as `lifecycle._publish_live_logs` does.

        The header is the whole point of the object and is written here in the
        worker's own format -- `offset` is the stream byte the window starts
        at, `size` is how long the stream is -- because a fixture that omitted
        it would prove the tool works against an object the platform does not
        produce.
        """
        body = text.encode("utf-8")
        size = stream_size if stream_size is not None else window_start + len(body)
        header = f"#swarm-tail offset={window_start} size={size}\n".encode("utf-8")
        self.objects.put(
            f"{self.prefix(task_id, attempt_id)}/logs/live/{stream}.tail.log", header + body
        )

    def final(self, task_id: str, attempt_id: str, text: str, *, stream: str = "stdout") -> None:
        self.objects.put(f"{self.prefix(task_id, attempt_id)}/logs/{stream}.log", text)


@pytest.fixture()
def world() -> World:
    return World()


@pytest.fixture()
def swarm(world: World, monkeypatch) -> SwarmClient:
    """A real `SwarmClient` whose socket is the real application.

    Only the transport is replaced, for the reason the sibling file gives: the
    request building, the header handling and the error mapping are the shipped
    ones, and they are the half a fake never exercises.
    """

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        headers = {**dict(req.headers), **AUTH}
        response = world.api.request(
            req.get_method(), path, content=req.data, headers=headers
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                response.status_code,
                "error",
                response.headers,
                io.BytesIO(response.content),
            )
        holder = io.BytesIO(response.content)
        holder.status = response.status_code
        holder.code = response.status_code
        holder.__enter__ = lambda: holder
        holder.__exit__ = lambda *exc: holder.close()
        return holder

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    return SwarmClient(base_url="http://api.invalid")


def stdout_of(report: dict, index: int = 0) -> dict:
    return next(
        row for row in report["tasks"][index]["logs"]["streams"] if row["stream"] == "stdout"
    )


def stderr_of(report: dict, index: int = 0) -> dict:
    return next(
        row for row in report["tasks"][index]["logs"]["streams"] if row["stream"] == "stderr"
    )


# --------------------------------------------------------------------------
# The cursor advances, and nothing is delivered twice
# --------------------------------------------------------------------------


def test_the_cursor_advances_and_the_second_call_returns_only_what_is_new(swarm, world):
    """The property the whole tool exists for.

    Two polls over a live tail that grew between them must PARTITION the
    stream: the first call's text plus the second call's text is the stream,
    once, in order. An overlap would make a session narrate the same work
    twice; a hole would make it miss work and never know.
    """
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "line one\nline two\n")

    first = follow(swarm, ["task_a"])
    row = stdout_of(first)
    assert row["status"] == "ok", row
    assert row["text"] == "line one\nline two\n"
    assert row["to"] == 18
    assert first["cursor"]["task_a"]["streams"]["stdout"]["pos"] == 18

    world.live("task_a", "att_1", "line one\nline two\nline three\n")
    second = follow(swarm, ["task_a"], cursor=first["cursor"])
    later = stdout_of(second)

    assert later["text"] == "line three\n", "the second call repeated output already delivered"
    assert later["from"] == 18 and later["to"] == 29
    assert second["cursor"]["task_a"]["streams"]["stdout"]["pos"] == 29
    assert row["text"] + later["text"] == "line one\nline two\nline three\n"


def test_two_calls_never_deliver_the_same_event_twice(swarm, world):
    """Events page by a COUNT, because the route orders ascending and takes
    `limit` as a head. A cursor that re-sent row zero would make every poll
    re-announce the task being submitted."""
    world.task("task_a")
    for kind in (EventType.SUBMITTED, EventType.QUEUED, EventType.READY):
        world.event("task_a", kind)

    first = follow(swarm, ["task_a"], max_new_events=2)
    assert [e["type"] for e in first["tasks"][0]["events"]["new"]] == ["submitted", "queued"]
    assert first["tasks"][0]["events"]["page_was_full"] is True
    assert "NO page token" in first["tasks"][0]["events"]["page_detail"]

    second = follow(swarm, ["task_a"], cursor=first["cursor"], max_new_events=2)
    assert [e["type"] for e in second["tasks"][0]["events"]["new"]] == ["ready"]
    assert second["tasks"][0]["events"]["delivered_total"] == 3

    third = follow(swarm, ["task_a"], cursor=second["cursor"], max_new_events=2)
    assert third["tasks"][0]["events"]["new"] == []
    assert third["tasks"][0]["events"]["status"] == "up_to_date"


def test_the_handover_from_the_live_tail_to_the_completed_log_loses_nothing(swarm, world):
    """The cursor is a STREAM position, not an object offset, and this is the
    payoff: the completed record is the stream from byte zero, so a position
    built while watching the rolling window addresses it exactly."""
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "a\nb\n")

    first = follow(swarm, ["task_a"])
    assert stdout_of(first)["text"] == "a\nb\n"
    assert stdout_of(first)["source"] == "live"

    world.set_state("task_a", "SUCCEEDED")
    world.final("task_a", "att_1", "a\nb\nc\n")

    second = follow(swarm, ["task_a"], cursor=first["cursor"])
    row = stdout_of(second)
    assert row["source"] == "final"
    assert row["text"] == "c\n", "the completed log repeated what the live tail had shown"
    assert second["cursor"]["task_a"]["streams"]["stdout"]["complete"] is True
    assert second["all_finished"] is True

    third = follow(swarm, ["task_a"], cursor=second["cursor"])
    assert stdout_of(third)["status"] == "complete"


def test_a_window_that_slid_past_the_cursor_reports_the_gap(swarm, world):
    """The live window holds the last 256KB. A watcher that polled too slowly
    lost the middle, and `cmd_tail` prints a gap header for exactly this. A
    cursor that stitched the two pieces together would print a transcript that
    never happened."""
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "first\n")

    first = follow(swarm, ["task_a"])
    assert first["cursor"]["task_a"]["streams"]["stdout"]["pos"] == 6

    world.live("task_a", "att_1", "much later\n", window_start=40)
    second = follow(swarm, ["task_a"], cursor=first["cursor"])
    row = stdout_of(second)

    assert row["gap_bytes"] == 34
    assert "missed" in (row["detail"] or "")
    assert second["truncated"] is True
    assert any("moved past" in note for note in second["truncation"]), second["truncation"]


# --------------------------------------------------------------------------
# The cap, which must never be silent
# --------------------------------------------------------------------------


def test_the_log_cap_is_reported_and_never_silent(swarm, world):
    """THE MUTATION TARGET OF THIS ITEM.

    A task that printed a lot must not arrive in one answer, and the answer
    must SAY it was cut. A truncation a reader has to infer is worse than no
    truncation at all: "that was all the output" is the conclusion a session
    draws, and it is wrong in the one direction that matters.

    The second half is as important as the first -- the cursor must resume at
    exactly the byte the cap stopped at, so the cap costs a second call and
    never a lost line.
    """
    stream = "".join(f"line {n:04d}\n" for n in range(2000))
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", stream)

    first = follow(swarm, ["task_a"], max_log_bytes=2000)
    row = stdout_of(first)

    assert first["truncated"] is True, "the answer was cut and did not say so"
    assert first["truncation"], "truncation was reported as a bare boolean with no sentence"
    said = " ".join(first["truncation"])
    assert "stdout" in said and "cursor" in said, said
    assert row["more_bytes"] > 0
    assert row["text"] and stream.startswith(row["text"])

    second = follow(swarm, ["task_a"], cursor=first["cursor"], max_log_bytes=2000)
    later = stdout_of(second)
    assert later["text"]
    joined = row["text"] + later["text"]
    assert stream.startswith(joined), "the cap lost or repeated bytes across the two calls"
    assert len(joined) > len(row["text"])


def test_the_budget_stops_later_streams_and_names_each_one(swarm, world):
    """A budget spent on the first task must not make the second one look
    silent. `skipped_budget` is a distinct status for that reason, and every
    skip is named in `truncation` rather than being left to a count."""
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "".join(f"a{n:04d}\n" for n in range(2000)))
    world.live("task_a", "att_1", "some stderr\n", stream="stderr")
    world.task("task_b")
    world.attempt("task_b", "att_2")
    world.live("task_b", "att_2", "b output\n")

    report = follow(swarm, ["task_a", "task_b"], max_log_bytes=100)

    assert stderr_of(report, 0)["status"] == "skipped_budget"
    assert stdout_of(report, 1)["status"] == "skipped_budget"
    assert report["truncated"] is True
    said = " ".join(report["truncation"])
    assert "task_b" in said and "budget" in said, said
    # The cursor for the untouched task must still be at zero, so the next call
    # reads it from the beginning rather than from a position it never reached.
    assert report["cursor"]["task_b"]["streams"]["stdout"]["pos"] == 0

    later = follow(swarm, ["task_b"], cursor=report["cursor"])
    assert stdout_of(later)["text"] == "b output\n"


# --------------------------------------------------------------------------
# Absence, which is four different facts
# --------------------------------------------------------------------------


def test_a_task_that_cannot_be_read_is_not_a_task_with_no_output(swarm, world):
    """A 404 must not arrive as an empty events list. It is the difference
    between "your agent has printed nothing yet" and "you are following a task
    that does not exist", and a session cannot recover the second from the
    first."""
    report = follow(swarm, ["task_missing"])
    task = report["tasks"][0]

    assert task["read"] == "failed"
    assert task["http_status"] == 404
    assert task["error"]
    assert task["events"].get("new") is None, "a failed read was dressed up as an empty history"
    # Not terminal: a task whose state could not be read has not been shown to
    # have finished, and `all_finished` deciding otherwise would stop a poll.
    assert task["terminal"] is False
    assert report["all_finished"] is False


def test_no_events_yet_is_reported_as_an_empty_history_not_as_a_failure(swarm, world):
    world.task("task_a", state="QUEUED")
    report = follow(swarm, ["task_a"])
    events = report["tasks"][0]["events"]

    assert events["status"] == "none_yet"
    assert "not a failed read" in events["detail"]
    assert events["new"] == []


def test_a_failed_events_read_is_not_an_empty_history(swarm, world, monkeypatch):
    """The other half of the same distinction, from the other direction."""
    world.task("task_a")

    def _boom(task_id, *, limit=200):  # noqa: ARG001
        raise SwarmError("the events route answered 503")

    monkeypatch.setattr(swarm, "events", _boom)
    report = follow(swarm, ["task_a"])
    events = report["tasks"][0]["events"]

    assert events["status"] == "unreadable"
    assert "503" in events["detail"]
    assert events["new"] == []


def test_a_task_with_no_attempt_yet_says_so_rather_than_showing_nothing(swarm, world):
    world.task("task_a", state="QUEUED")
    report = follow(swarm, ["task_a"])
    logs = report["tasks"][0]["logs"]

    assert logs["status"] == "no_attempt_yet"
    assert "not a failed read" in logs["detail"]
    assert stdout_of(report)["text"] is None, "an unread stream was given empty text"


def test_an_object_that_could_not_be_read_is_not_an_absent_one(swarm, world):
    """`swarm_api.inspect` keeps `absent` and `unreadable` apart and refuses to
    substitute one object for another after a failure. A client that collapsed
    them would report a storage outage as an agent that printed nothing."""
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "hello\n")
    world.objects.fail_on(f"{World.prefix('task_a', 'att_1')}/logs/live/stdout")

    report = follow(swarm, ["task_a"])
    row = stdout_of(report)
    assert row["status"] == "unreadable"
    assert row["text"] is None
    assert row["detail"]
    assert stderr_of(report)["status"] == "absent"


# --------------------------------------------------------------------------
# Fan-out, heartbeats, retries
# --------------------------------------------------------------------------


def test_several_tasks_are_followed_in_one_call_with_independent_cursors(swarm, world):
    """The case this exists for is five or six agents at once. Each task's
    cursor has to move on its own; one shared position would make a quiet agent
    skip the noisy one's output."""
    for name, text in (("task_a", "from a\n"), ("task_b", "from b\n"), ("task_c", "from c\n")):
        world.task(name)
        world.attempt(name, f"att_{name}")
        world.live(name, f"att_{name}", text)

    first = follow(swarm, ["task_a", "task_b", "task_c"])
    assert [t["task_id"] for t in first["tasks"]] == ["task_a", "task_b", "task_c"]
    assert [stdout_of(first, i)["text"] for i in range(3)] == ["from a\n", "from b\n", "from c\n"]

    world.live("task_b", "att_task_b", "from b\nmore b\n")
    second = follow(swarm, ["task_a", "task_b", "task_c"], cursor=first["cursor"])
    assert stdout_of(second, 0)["status"] == "up_to_date"
    assert stdout_of(second, 1)["text"] == "more b\n"
    assert stdout_of(second, 2)["status"] == "up_to_date"


def test_heartbeats_are_hidden_by_default_and_the_number_hidden_is_reported(swarm, world):
    world.task("task_a")
    world.event("task_a", EventType.SUBMITTED)
    for _ in range(4):
        world.event("task_a", EventType.HEARTBEAT)

    quiet = follow(swarm, ["task_a"])
    assert [e["type"] for e in quiet["tasks"][0]["events"]["new"]] == ["submitted"]
    assert quiet["tasks"][0]["events"]["heartbeats_hidden"] == 4
    # The cursor counted every row it consumed, heartbeats included, so asking
    # for them afterwards must not re-deliver the ones already summarised.
    assert quiet["cursor"]["task_a"]["events"] == 5

    loud = follow(swarm, ["task_a"], include_heartbeats=True)
    assert [e["type"] for e in loud["tasks"][0]["events"]["new"]][0] == "submitted"
    assert len(loud["tasks"][0]["events"]["new"]) == 5


def test_a_retry_resets_the_cursor_and_says_that_it_did(swarm, world):
    """A retry is a new attempt with new objects, so every byte position the
    cursor holds belongs to a run that is over. Continuing from them would
    splice the second attempt's output onto the first at an arbitrary byte."""
    world.task("task_a")
    world.attempt("task_a", "att_1", minutes_ago=30)
    world.live("task_a", "att_1", "first attempt\n")

    first = follow(swarm, ["task_a"])
    assert stdout_of(first)["text"] == "first attempt\n"
    assert first["cursor"]["task_a"]["attempt_id"] == "att_1"

    world.attempt("task_a", "att_2", minutes_ago=1)
    world.live("task_a", "att_2", "second attempt\n")

    second = follow(swarm, ["task_a"], cursor=first["cursor"])
    assert second["tasks"][0]["logs"]["attempt_id"] == "att_2"
    assert stdout_of(second)["text"] == "second attempt\n"
    assert any("reset" in note for note in second["tasks"][0]["logs"]["notes"])
    assert second["cursor"]["task_a"]["attempt_id"] == "att_2"
    # The read that DISCOVERED the retry addressed the old attempt's byte
    # positions and its answer was thrown away. Charging it would make the
    # budget count bytes the caller never received.
    assert second["log_bytes_returned"] == len("second attempt\n")
    assert second["truncated"] is False, second["truncation"]


def test_a_cursor_a_model_mangled_re_reads_rather_than_skipping_ahead(swarm, world):
    """The cursor crosses a tool boundary, so it comes back as whatever the
    model sent. A position that silently became large loses output with nothing
    to show for it; one that became zero costs a repeat, which is visible. The
    coercion is deliberately biased towards the recoverable failure."""
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "hello\n")

    mangled = {"task_a": {"events": "not a number", "streams": {"stdout": {"pos": -5}}}}
    report = follow(swarm, ["task_a"], cursor=mangled)
    assert stdout_of(report)["text"] == "hello\n"
    assert report["cursor"]["task_a"]["streams"]["stdout"]["pos"] == 6


def test_the_tool_is_registered_and_its_schema_names_the_cursor():
    """G2 was not that the capability was missing -- `cmd_tail` has followed a
    run since the beginning -- but that it was a CLI subcommand a model cannot
    call, while every tool description pointed at it. A test that the tool is
    LISTED is therefore the test that the hole is closed."""
    from swarm_mcp import server

    tool = next(t for t in server.TOOLS if t["name"] == "swarm_follow")
    assert "cursor" in tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["required"] == ["task_ids"]

    described = " ".join(t["description"] for t in server.TOOLS)
    assert "swarm tail" not in described, (
        "a tool description still sends the model to a terminal command it cannot run"
    )


def test_the_tool_call_returns_json_a_session_can_hand_straight_back(swarm, world):
    """Registered is not the same as reachable. `_call` is the dispatcher, and
    a tool listed but not routed answers "unknown tool" -- which is what G2
    was, one layer up: a capability that existed and could not be called.

    The finished task carries its `result` here too, so a session that polled
    to the end does not need a second call to learn what the agent produced.
    """
    import json as _json

    from swarm_mcp import server

    world.task("task_a", state="SUCCEEDED")
    world.attempt("task_a", "att_1")
    world.final("task_a", "att_1", "done\n")

    # A string where an integer belongs, which is what a model sends about as
    # often as the integer.
    text = server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "max_log_bytes": "5000"})
    report = _json.loads(text)

    assert report["all_finished"] is True
    assert report["tasks"][0]["result"]["task_id"] == "task_a"
    assert stdout_of(report)["text"] == "done\n"
    # The cursor must survive a JSON round trip, because that is the only way
    # it ever reaches the next call.
    again = server._call(
        swarm, "swarm_follow", {"task_ids": ["task_a"], "cursor": report["cursor"]}
    )
    assert stdout_of(_json.loads(again))["status"] == "complete"


def test_a_cursor_the_window_cannot_reach_says_behind_rather_than_repeating(swarm, world):
    """The one case where the cursor is ahead of everything the call could read.

    Serving the head of the window instead would repeat output already shown
    AND hide the real bytes, which is the worst of both. `behind` names it and
    says which knob fixes it.
    """
    stream = "".join(f"line {n:04d}\n" for n in range(2000))
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", stream)

    # A window start that is wrong in the one direction that under-sizes the
    # read: the code asks for the overlap plus the budget, and here it is told
    # there is no overlap at all.
    cursor = {"task_a": {"streams": {"stdout": {"pos": 10_000, "window_start": 10_000}}}}
    report = follow(swarm, ["task_a"], cursor=cursor, max_log_bytes=10)
    row = stdout_of(report)

    assert row["status"] == "behind"
    assert row["text"] is None
    assert "max_log_bytes" in row["detail"]
    assert report["truncated"] is True
    assert report["cursor"]["task_a"]["streams"]["stdout"]["pos"] == 10_000


def test_a_completed_log_shorter_than_the_tail_already_shown_says_so(swarm, world):
    """The worker caps what it uploads, so the record can hold less than the
    live window did. Reporting that as "nothing new" would say the agent went
    quiet when what happened is that the evidence shrank."""
    world.task("task_a")
    world.attempt("task_a", "att_1")
    world.live("task_a", "att_1", "one\ntwo\nthree\n")

    first = follow(swarm, ["task_a"])
    assert first["cursor"]["task_a"]["streams"]["stdout"]["pos"] == 14

    world.set_state("task_a", "FAILED")
    world.final("task_a", "att_1", "one\n")

    second = follow(swarm, ["task_a"], cursor=first["cursor"])
    row = stdout_of(second)
    assert row["status"] == "ok"
    assert row["text"] == ""
    assert "shorter than the window" in row["detail"]
    assert second["cursor"]["task_a"]["streams"]["stdout"]["complete"] is True


def _follow_args(**overrides):
    values = {
        "task_ids": ["task_a"],
        "interval": 0.0,
        "once": False,
        "max_log_bytes": 20_000,
        "verbose": False,
    }
    values.update(overrides)
    return type("A", (), values)()


def test_the_terminal_command_prints_the_report_the_tool_returns(swarm, world, capsys):
    """`swarm follow` exists so a person can check a session's claim against
    the same bytes. It must therefore render the tool's own report rather than
    going back to the client for a second answer."""
    world.task("task_a", state="SUCCEEDED")
    world.attempt("task_a", "att_1")
    world.final("task_a", "att_1", "hello from the agent\n")
    world.event("task_a", EventType.SUCCEEDED)

    assert cli.cmd_follow(swarm, _follow_args()) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "hello from the agent" in printed
    assert "succeeded" in printed
    assert "SUCCEEDED" in printed


def test_the_terminal_command_fails_when_a_task_did(swarm, world, capsys):
    """Exit codes here are read by scripts. A failed run that exits 0 tells a
    pipeline the work was fine because nothing said otherwise."""
    world.task("task_a", state="FAILED")
    world.attempt("task_a", "att_1")
    world.final("task_a", "att_1", "it went wrong\n")

    assert cli.cmd_follow(swarm, _follow_args()) == cli.EXIT_FAIL
    assert "it went wrong" in capsys.readouterr().out


# --------------------------------------------------------------------------
# A cancel that is only requested is narrated as a request
# --------------------------------------------------------------------------
# Contract request 17, accepted by the owner 2026-09-24. Before it, the API's
# flag-only cancel wrote `type: cancelled` with `detail.phase:
# cancel_requested`, and `swarm_follow` narrated that row as "cancelled" while
# the task stayed DISPATCHED with its lease held -- the reading behind incident
# wf_ebb3ab2d65664707a559. History written then is still stored that way.


def _legacy_request(world: World, task_id: str) -> None:
    """The row exactly as the pre-2026-09-24 API stored it."""
    world.db.docs[f"tasks/{task_id}/events/ev_legacy_request"] = {
        "event_id": "ev_legacy_request",
        "task_id": task_id,
        "tenant_id": TENANT,
        "type": "cancelled",
        "at": NOW,
        "attempt_id": None,
        "lease_id": None,
        "generation": None,
        "detail": {"requested_by": "alice@saga.xyz", "from_state": "DISPATCHED",
                   "phase": "cancel_requested"},
    }


def test_a_stored_flag_only_cancel_is_followed_as_a_request(swarm, world):
    """Through the real API: the model reads `cancel_requested`, not `cancelled`."""
    world.task("task_a", state="DISPATCHED")
    _legacy_request(world, "task_a")

    report = follow(swarm, ["task_a"])

    kinds = [e["type"] for e in report["tasks"][0]["events"]["new"]]
    assert kinds == ["cancel_requested"], (
        f"a cancel that was only requested was narrated as {kinds} on a task that "
        "is still DISPATCHED"
    )
    assert report["tasks"][0]["terminal"] is False


def test_the_follow_rule_holds_against_an_api_that_still_serves_the_old_shape():
    """The plugin runs against whatever API is deployed, which can be older than it.

    So the reading is also applied on this side, to the raw row -- and only to
    the flag-only shape: an immediate cancel, and the scheduler's cascade
    cancel (which has never carried a phase), stay `cancelled`.
    """
    from swarm_mcp.follow import event_type

    legacy = {"type": "cancelled", "detail": {"phase": "cancel_requested"}}
    assert event_type(legacy) == "cancel_requested"
    assert event_type({"type": "cancel_requested", "detail": {"phase": "cancel_requested"}}) == (
        "cancel_requested"
    )
    assert event_type({"type": "cancelled", "detail": {"phase": "cancelled"}}) == "cancelled"
    assert event_type({"type": "cancelled", "detail": {"reason": "upstream"}}) == "cancelled"
    assert event_type({"type": "cancelled", "detail": None}) == "cancelled"
    assert event_type({"type": "heartbeat"}) == "heartbeat"
    assert event_type({}) == "?"


def test_swarm_tail_prints_a_stored_request_as_a_request(swarm, world, capsys):
    """`swarm tail` prints each event's type on its own line; the same reading applies."""
    world.task("task_a", state="CANCELLED")
    _legacy_request(world, "task_a")

    cli.cmd_tail(swarm, _follow_args())

    printed = capsys.readouterr().out
    assert "· cancel_requested" in printed, printed
    assert "· cancelled" not in printed, (
        f"`swarm tail` printed a cancel request as a cancel:\n{printed}"
    )

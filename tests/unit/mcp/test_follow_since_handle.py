"""A row's `since` is a short handle the bridge keeps, and its last reply is a ready-made `result`.

MEASURED, 2026-10-05 (lane review B1+B2). The Haiku `sc:step` rows copied the
~175-character `since` token by hand and corrupted it on 88 of 192 calls (46%).
After the checksum refusals a row dropped `since` altogether -- the refusal
text even suggested it -- and every call without one returned at once, so
three rows polled 56-60 times in minutes: 14.9M tokens, 51% of all row tokens.
Separately, all 14 refused StructuredOutput calls were bare nulls
(`"pr_url": ,`), and one review row lost a SUCCEEDED result.

Owner decision, 2026-10-05:

* B1 -- the bridge keeps each follower's cursor itself and hands back a short
  handle; the handle is accepted as `since`, an old full token still is, a
  call WITHOUT `since` for tasks this bridge process already reported to that
  follower HOLDS instead of returning at once, and no refusal ever tells a row
  to omit its cursor.
* B2 -- the reply that stops carries `result`, the step's answer with no null
  anywhere (an empty string, or the field left out), for the row to return
  as given.

Every hold here runs on a fake clock, so no test waits.
"""

from __future__ import annotations

import functools
import json
import re

import pytest

from swarm_mcp import compact, progress, server
from swarm_mcp.client import SwarmError

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures
from test_follow_progress_format import _running, _Time


@pytest.fixture()
def clock(monkeypatch) -> _Time:
    """A fake clock every hold reached through the TOOL sleeps on."""
    fake = _Time()
    monkeypatch.setattr(
        compact, "watch_progress",
        functools.partial(compact.watch_progress, sleep=fake.sleep, clock=fake.clock),
    )
    monkeypatch.setattr(
        progress, "watch",
        functools.partial(progress.watch, sleep=fake.sleep, clock=fake.clock),
    )
    return fake


def _follow(client, fmt: str = "progress", **arguments) -> dict:
    return json.loads(server._call(client, "swarm_follow", {"task_ids": ["task_a"], "format": fmt, **arguments}))


def _nulls(value, path: str = "result") -> list[str]:
    """Every path in `value` that holds null."""
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _nulls(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _nulls(v, f"{path}[{i}]")]
    return []


def _quiet(world: World, task_id: str = "task_a") -> None:
    """A running task with a few lines of agent output and nothing more coming."""
    _running(world, task_id)
    world.live(task_id, "att_1", '{"type":"system","subtype":"init","model":"m"}\n', stream="agent_stdout")
    world.live(task_id, "att_1", '{"event": "runner line"}\n')


# --------------------------------------------------------------------------
# B1: the handle
# --------------------------------------------------------------------------


def test_a_progress_follow_returns_a_short_handle_and_resumes_from_it(swarm, world, clock):
    _running(world)
    first = _follow(swarm, step_id="review")

    handle = first["since"]
    assert len(handle) <= 8 and re.fullmatch(r"r[0-9a-f]+", handle), (
        f"{handle!r}: a row copies `since` by hand on every call, so it must be a short handle"
    )

    again = _follow(swarm, step_id="review", since=handle, wait_seconds=120)

    assert again["progress"] == [] and again["changed"] is False, (
        "the handle must resume from the position it was issued for, so nothing is said twice"
    )
    assert sum(clock.slept) == 120, "a resumed call holds like any follow"
    assert again["since"] != handle, "each reply issues its own handle"


def test_a_lines_follow_returns_a_short_handle_too(swarm, world, clock):
    _quiet(world)
    first = _follow(swarm, "lines")
    assert len(first["since"]) <= 8, first["since"]

    again = _follow(swarm, "lines", since=first["since"], wait_seconds=60)

    assert not any("session started" in line for line in again["lines"]), (
        "the handle must resume past the lines the first call showed"
    )


def test_a_corrupted_handle_is_refused_and_the_refusal_never_says_omit(swarm, world, clock):
    _running(world)
    handle = _follow(swarm)["since"]
    # Every single-character slip of the handle's body is caught.
    for index in range(1, len(handle)):
        for ch in "0123456789abcdef":
            if ch == handle[index]:
                continue
            bad = handle[:index] + ch + handle[index + 1:]
            with pytest.raises(SwarmError) as caught:
                _follow(swarm, since=bad)
            text = str(caught.value).lower()
            assert "omit" not in text and "start again" not in text, text
            assert "copy" in text, f"the refusal must say to copy `since` again: {text}"
    assert clock.slept == [], "a refused call reads and holds nothing"


def test_a_changed_full_token_is_refused_without_suggesting_omitting_it(swarm, world):
    _running(world)
    token = compact.watch_progress(swarm, ["task_a"])["since"]
    flipped = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(SwarmError, match="checksum") as caught:
        compact.watch_progress(swarm, ["task_a"], since=flipped)
    assert "omit" not in str(caught.value).lower(), str(caught.value)


def test_a_handle_issued_for_other_tasks_is_refused(swarm, world, clock):
    _running(world)
    _running(world, "task_b")
    handle = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_b"], "format": "progress"}))["since"]
    with pytest.raises(SwarmError) as caught:
        _follow(swarm, since=handle)
    assert "omit" not in str(caught.value).lower()


def test_a_follow_without_since_for_a_task_already_reported_holds(swarm, world, clock):
    _running(world)
    _follow(swarm, step_id="review", wait_seconds=1800)
    assert clock.slept == [], "the FIRST call for a task still answers at once"

    dropped = _follow(swarm, step_id="review", wait_seconds=1800)

    assert sum(clock.slept) == compact.SILENT_MAX_WAIT_SECONDS, (
        "a row that dropped `since` must hold like a normal follow, not return at once "
        "-- that is how three rows made 56-60 calls in minutes"
    )
    assert dropped["progress"] == [], "it resumes from what this bridge last reported"


def test_a_lines_follow_without_since_for_a_task_already_reported_holds(swarm, world, clock):
    _quiet(world)
    _follow(swarm, "lines")
    assert clock.slept == []

    _follow(swarm, "lines", wait_seconds=60)

    assert sum(clock.slept) == 60


def test_a_follow_without_since_for_a_task_never_reported_still_answers_at_once(swarm, world, clock):
    _running(world)
    _running(world, "task_b")
    _follow(swarm)  # task_a only

    json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_b"], "format": "progress", "wait_seconds": 600}))

    assert clock.slept == [], "a task this bridge never reported is a first call"


def test_an_old_full_token_is_still_accepted(swarm, world, clock):
    _running(world)
    # A token as a bridge before the handle minted it.
    token = compact.watch_progress(swarm, ["task_a"])["since"]
    assert len(token) > 40

    got = _follow(swarm, since=token, wait_seconds=120)

    assert got["progress"] == [] and got["changed"] is False
    assert sum(clock.slept) == 120
    assert len(got["since"]) <= 8, "the reply to an old token hands back a handle"


def test_an_old_full_lines_token_is_still_accepted(swarm, world, clock):
    _quiet(world)
    token = progress.watch(swarm, ["task_a"])["since"]

    got = _follow(swarm, "lines", since=token, wait_seconds=30)

    assert not any("session started" in line for line in got["lines"]), got["lines"]
    assert len(got["since"]) <= 8


def test_the_tool_text_never_tells_a_row_to_omit_since_after_a_refusal():
    text = json.dumps(next(t for t in server.TOOLS if t["name"] == "swarm_follow"))
    assert "handle" in text, "the tool text must say `since` is a short handle"
    assert "or omit it" not in text


# --------------------------------------------------------------------------
# B2: the ready-made result
# --------------------------------------------------------------------------


def test_the_reply_that_stops_carries_a_result_with_no_null(swarm, world, clock):
    _running(world)
    doc = world.db.docs["tasks/task_a"]
    doc["state"] = "SUCCEEDED"
    doc.pop("started_at", None)
    doc["result_summary"] = {"runner": {"summary": "done"}}

    got = _follow(swarm, step_id="review")

    assert got["stop"] is True
    result = got["result"]
    assert _nulls(result) == [], f"null fields in the result: {_nulls(result)}"
    assert result["state"] == "SUCCEEDED"
    assert result["pr_url"] == "" and result["last_error"] == ""
    assert result["artifacts"] == []
    assert "cost_usd" not in result, "a cost that was not recorded is left out -- never 0, never null"
    assert "duration_s" not in result


def test_the_result_carries_every_recorded_figure(swarm, world, clock):
    _running(world)
    doc = world.db.docs["tasks/task_a"]
    doc["state"] = "SUCCEEDED"
    doc["completed_at"] = NOW.replace(minute=4, second=12)
    doc["result_summary"] = {
        "runner": {"summary": "done"},
        "artifacts": [{"name": "report.md", "uri": "gs://b/report.md", "bytes": 12}],
        "git": {"commit_count": 1, "pull_request": {"number": 7, "url": "https://github.com/acme/w/pull/7"}},
    }
    world.db.docs["attempts/att_1"]["cost_usd"] = 0.21

    result = _follow(swarm)["result"]

    assert _nulls(result) == []
    assert result["cost_usd"] == 0.21 and result["duration_s"] == 252.0
    assert result["pr_url"] == "https://github.com/acme/w/pull/7"
    assert result["artifacts"] == ["report.md"]


def test_an_abandoned_task_stops_with_an_unknown_result_and_no_null(swarm, world, clock):
    got = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_missing"], "format": "progress"}))

    assert got["stop"] is True
    result = got["result"]
    assert _nulls(result) == []
    assert result["state"] == "UNKNOWN" and "404" in result["last_error"]


def test_a_reply_that_does_not_stop_carries_no_result(swarm, world, clock):
    _running(world)
    assert "result" not in _follow(swarm)


# --------------------------------------------------------------------------
# The row and the workflow script
# --------------------------------------------------------------------------


def test_the_step_row_returns_state_and_the_bridges_result_as_given():
    from test_plugin_agents_and_workflows import _PLUGIN, _load

    _, body = _load(_PLUGIN / "agents" / "step.md")
    flat = " ".join(body.split())
    assert "`result`" in flat and "{state, result}" in flat.replace("`", ""), (
        "the row must return `{state, result}` copied from the bridge's ready-made result"
    )
    lowered = flat.lower()
    assert "never drop `since`" in lowered, "the row must be told never to drop its cursor"
    assert "omit `since`" not in lowered and "omit it" not in lowered
    assert "no null" in lowered, "the row must be told the result holds no null, and to add none"


def test_run_js_asks_the_row_for_state_and_result():
    from test_plugin_agents_and_workflows import _RUN_JS

    source = _RUN_JS.read_text()
    start = source.index("const STEP_RESULT = {")
    block = source[start:source.index("\n}\n", start)]
    assert "result: STEP_FIELDS" in block, block
    assert "required: ['state', 'result']" in block, block


def test_run_js_reads_a_rows_state_and_result_as_the_flat_step_result(tmp_path):
    from test_plugin_agents_and_workflows import _ANSWERS, _SPEC, _run

    answers = {
        **_ANSWERS,
        "step:scan-01": {"state": "SUCCEEDED", "result": {
            "state": "SUCCEEDED", "answer_excerpt": "", "cost_usd": 0.21, "duration_s": 252,
            "pr_url": "https://github.com/acme/widgets/pull/231", "artifacts": ["a.md"],
            "last_error": "", "console": ""}},
        "step:scan-02": {"state": "FAILED", "result": {
            "state": "FAILED", "answer_excerpt": "", "pr_url": "", "artifacts": [],
            "last_error": "claude-code exited 1: boom", "console": ""}},
    }
    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    rows = {row["step_id"]: row for row in got["result"]["steps"]}
    one, two = rows["scan-01"], rows["scan-02"]
    assert (one["state"], one["cost_usd"], one["duration_s"]) == ("SUCCEEDED", 0.21, 252)
    assert one["pr_url"] == "https://github.com/acme/widgets/pull/231" and one["artifacts"] == ["a.md"]
    assert one["last_error"] is None and one["answer_excerpt"] is None, "an empty text is read back as null"
    assert "result" not in one, "the row's nesting is not carried into the step result"
    assert two["state"] == "FAILED" and two["last_error"] == "claude-code exited 1: boom"
    assert two["cost_usd"] is None and two["duration_s"] is None, "a figure left out is not recorded, never 0"
    assert any(line.startswith("scan-01 SUCCEEDED") for line in got["logs"]), got["logs"]

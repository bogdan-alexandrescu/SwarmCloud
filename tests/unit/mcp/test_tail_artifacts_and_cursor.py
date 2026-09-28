"""Wave 2026-09-27, lane 6: `swarm tail` past the 50th event, what a task
produced, and the follow cursor a relay copies by hand.

* #164 (option 1, the owner's pick): `swarm tail` read `?limit=50` on every
  poll, which the route answers with the task's OLDEST 50 events, so from the
  51st on it printed no event at all. It now pages back from the newest event
  to the last one it printed, so every event is printed exactly once.
* #143: `swarm result`, `sc task` and the bridge's `swarm_result` say what a
  step produced -- each artifact by name and size, the runner's summary, the
  exit code, the duration and the staged inputs -- and `swarm artifact` /
  `swarm_artifact` fetch one through `/v1/tasks/{id}/artifacts/content`.
* Epic #227: the `since` token carries a checksum and a changed one is refused;
  `watch()` puts `lines` first; `/sc:run` takes a spec file's path, read by
  the bridge's `swarm_workflow_spec` (the run.js half is in
  `test_plugin_agents_and_workflows.py`).

AGAINST THE REAL swarm-api, over an in-memory Firestore and object store, as
`test_swarm_says_what_happened.py` builds it: the defect in #164 was a
disagreement with what the route serves, and a fake route would agree with
whoever wrote it. Offline: no credentials, no emulator, no network.
"""

from __future__ import annotations

import argparse
import io
import json
from datetime import timedelta

import pytest

from swarm_mcp import cli, progress, render, sc, server, workflows
from swarm_mcp.client import SwarmError

from control_plane.conftest import PROJECT
from test_swarm_says_what_happened import AT, TENANT, World, _args, _sleeps, swarm, world  # noqa: F401

BUCKET = f"swarm-artifacts-{PROJECT}"


def _events(world: World, task_id: str, first: int, count: int, kind: str = "checkpoint_completed") -> None:
    """`count` events, one second apart, numbered from `first`."""
    for n in range(first, first + count):
        world.event(task_id, f"ev_{n:04d}", kind, AT + timedelta(seconds=n))


def _printed_clocks(printed: str, task_id: str, kind: str = "checkpoint_completed") -> list[str]:
    """The `HH:MM:SS` of every event line `tail` printed for this task."""
    out = []
    for line in printed.splitlines():
        if task_id[-8:] in line and f"· {kind}" in line:
            out.append(line.split("·")[0].split()[-1])
    return out


def _clock(n: int) -> str:
    return render.clock(AT + timedelta(seconds=n))


# ==========================================================================
# #164: every event, exactly once
# ==========================================================================


def test_tail_prints_every_event_of_a_task_with_more_than_fifty(swarm, world, capsys):
    """120 events on a finished task: one poll, and every one of them printed,
    oldest first. Before the fix it printed the oldest 50 and stopped."""
    task_id = world.task("task_000000000000000many", state="SUCCEEDED")
    _events(world, task_id, 0, 120)

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    printed = _printed_clocks(capsys.readouterr().out, task_id)
    assert len(printed) == 120, f"tail printed {len(printed)} of 120 events"
    assert printed == [_clock(n) for n in range(120)], "not every event, or not in order"


def test_tail_prints_the_events_that_arrive_after_the_fiftieth_on_a_later_poll(swarm, world, monkeypatch, capsys):
    """60 events on the first poll, 70 more by the second: 130 lines, none twice.

    The second poll is the case option 1 is for -- the newest events are read
    first and the paging stops at the last one already printed."""
    task_id = world.task("task_00000000000000later", state="RUNNING")
    _events(world, task_id, 0, 60)

    def _more(poll: int) -> None:
        if poll == 1:
            _events(world, task_id, 60, 70)
            world.set(task_id, state="SUCCEEDED")

    _sleeps(monkeypatch, _more)
    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    printed = _printed_clocks(capsys.readouterr().out, task_id)
    assert len(printed) == len(set(printed)), "an event was printed twice"
    assert printed == [_clock(n) for n in range(130)], (
        f"tail printed {len(printed)} of 130 events; the 51st onward are the ones #164 lost"
    )


def test_tail_reads_the_newest_page_first_and_stops_at_what_it_has_printed(swarm, world, monkeypatch, capsys):
    """One request per poll while `tail` keeps up: the second poll of a task
    with nothing new reads one page, not the whole history again."""
    task_id = world.task("task_0000000000000steady", state="RUNNING")
    _events(world, task_id, 0, 130)
    sent: list[str] = []
    real = swarm.request

    def _counting(method, path, *a, **k):
        if "/events" in path:
            sent.append(path)
        return real(method, path, *a, **k)

    monkeypatch.setattr(swarm, "request", _counting)
    after_first: list[int] = []

    def _finish(poll: int) -> None:
        if poll == 1:
            after_first.append(len(sent))
            world.set(task_id, state="SUCCEEDED")

    _sleeps(monkeypatch, _finish)

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    assert all("order=desc" in path for path in sent), sent
    assert any("page_token" in p for p in sent[: after_first[0]]), (
        "130 events at 50 a page is more than one page; no page token was followed"
    )
    assert len(sent) - after_first[0] == 1, f"the second poll read {sent[after_first[0]:]}"
    assert len(_printed_clocks(capsys.readouterr().out, task_id)) == 130


# ==========================================================================
# #143: what a task produced
# ==========================================================================


def _finished_with_artifacts(world: World, task_id: str, files: dict[str, str]) -> None:
    """A terminal task whose manifest and bucket agree, as a real run leaves them."""
    world.task(task_id, state="SUCCEEDED")
    entries = []
    for name, body in files.items():
        key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_1/artifacts/{name}"
        world.objects.put(key, body)
        entries.append({"name": name, "bytes": len(body.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"})
    world.set(
        task_id,
        result_summary={
            "artifacts": entries,
            "artifact_bytes": sum(e["bytes"] for e in entries),
            "logs": {},
            "exit_code": 0,
            "duration_seconds": 60.6,
            "runner": {"status": "succeeded", "summary": "wrote output.txt with the answer"},
            "staged_inputs": [
                {"task_id": "task_00000000000upstream", "filename": "notes.md",
                 "path": "/work/notes.md", "bytes": 12},
            ],
        },
    )


_OUTPUT = "the answer is forty-two, and here is why.\nok\n"


def test_result_lists_each_artifact_and_what_the_runner_said(swarm, world, capsys):
    """#143's own case: output.txt, a runner summary and a 60.6s duration, and
    `swarm result` printed only that no repository was cloned."""
    task_id = "task_0000000000produced"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})

    assert cli.cmd_result(swarm, argparse.Namespace(task_id=task_id, json=False)) == cli.EXIT_OK

    printed = capsys.readouterr().out
    line = next((row for row in printed.splitlines() if "output.txt" in row), None)
    assert line is not None, printed
    assert f"{len(_OUTPUT)} bytes" in line, line
    assert "wrote output.txt with the answer" in printed, printed
    assert "exit code 0" in printed, printed
    assert "60.6s" in printed, printed
    assert "notes.md" in printed and "task_00000000000upstream" in printed, printed
    assert f"swarm artifact {task_id} output.txt" in printed, "it must say how to fetch one"


def test_result_says_an_unfinished_task_has_not_uploaded_its_artifacts_yet(swarm, world, capsys):
    """`complete: false` is "not yet", never "none"."""
    task_id = world.task("task_000000000000running", state="RUNNING")

    cli.cmd_result(swarm, argparse.Namespace(task_id=task_id, json=False))

    printed = capsys.readouterr().out
    assert "uploaded when the attempt ends" in printed, printed
    assert "no artifacts" not in printed, printed


def _artifact_args(task_id: str, name: str, output=None) -> argparse.Namespace:
    return argparse.Namespace(task_id=task_id, name=name, output=output)


def test_swarm_artifact_prints_the_artifacts_content(swarm, world, capsys):
    task_id = "task_0000000000fetchone"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})

    assert cli.cmd_artifact(swarm, _artifact_args(task_id, "output.txt")) == cli.EXIT_OK

    assert capsys.readouterr().out == _OUTPUT


def test_swarm_artifact_pages_a_file_larger_than_one_window(swarm, world, monkeypatch, capsys):
    """The route cuts a window and says `next_offset`; the whole file is the
    concatenation of every window, and nothing is printed twice."""
    body = "".join(f"line {n:05d}\n" for n in range(1500))  # 16,500 bytes
    task_id = "task_000000000000paging"
    _finished_with_artifacts(world, task_id, {"big.txt": body})
    monkeypatch.setattr(cli, "ARTIFACT_WINDOW_BYTES", 4096)

    assert cli.cmd_artifact(swarm, _artifact_args(task_id, "big.txt")) == cli.EXIT_OK

    assert capsys.readouterr().out == body


def test_swarm_artifact_writes_to_a_file_when_asked(swarm, world, tmp_path, capsys):
    task_id = "task_0000000000tofile00"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})
    target = tmp_path / "out.txt"

    assert cli.cmd_artifact(swarm, _artifact_args(task_id, "output.txt", output=str(target))) == cli.EXIT_OK

    assert target.read_text() == _OUTPUT
    assert str(target) in capsys.readouterr().err


def test_swarm_artifact_names_the_artifacts_a_task_does_list(swarm, world):
    task_id = "task_0000000000misnamed"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})

    with pytest.raises(SwarmError) as caught:
        cli.cmd_artifact(swarm, _artifact_args(task_id, "outptu.txt"))
    assert "output.txt" in str(caught.value), str(caught.value)


def test_swarm_artifact_refuses_a_binary_artifact_and_says_why(swarm, world, capsys):
    task_id = "task_00000000000binary0"
    _finished_with_artifacts(world, task_id, {"blob.bin": "ab\x00cd"})

    assert cli.cmd_artifact(swarm, _artifact_args(task_id, "blob.bin")) == cli.EXIT_FAIL

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not text" in captured.err, captured.err


def test_the_cli_has_an_artifact_command():
    args = cli.build_parser().parse_args(["artifact", "task_x", "output.txt", "-o", "here.txt"])
    assert (args.task_id, args.name, args.output) == ("task_x", "output.txt", "here.txt")
    assert args.func is cli.cmd_artifact


def test_sc_task_lists_each_artifact_and_what_the_runner_said(swarm, world):
    task_id = "task_00000000000sctask0"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})
    out = io.StringIO()

    args = argparse.Namespace(task_id=task_id, json=False, width=120, color=False, ascii=True)
    assert sc.cmd_task(swarm, args, out) == sc.EXIT_OK

    printed = out.getvalue()
    line = next((row for row in printed.splitlines() if "output.txt" in row), None)
    assert line is not None, printed
    assert f"{len(_OUTPUT)} bytes" in line, line
    assert "wrote output.txt with the answer" in printed, printed
    assert "exit code 0" in printed and "60.6s" in printed, printed
    assert "notes.md" in printed, printed


def test_the_swarm_result_tool_says_what_the_task_produced(swarm, world):
    task_id = "task_000000000000tool00"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})

    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))

    produced = reply["outputs"]
    assert produced["artifacts"] == [{"name": "output.txt", "bytes": len(_OUTPUT)}]
    assert produced["artifacts_complete"] is True
    assert produced["exit_code"] == 0
    assert produced["duration_s"] == 60.6
    assert produced["runner_summary"] == "wrote output.txt with the answer"
    assert produced["staged_inputs"] == [
        {"filename": "notes.md", "bytes": 12, "from_task": "task_00000000000upstream"}
    ]


def test_the_swarm_artifact_tool_fetches_one_artifact_redacted_at_read_time(swarm, world):
    task_id = "task_00000000000tool001"
    _finished_with_artifacts(world, task_id, {"output.txt": _OUTPUT})

    reply = json.loads(server._call(swarm, "swarm_artifact", {"task_id": task_id, "name": "output.txt"}))

    assert reply["status"] == "ok"
    assert reply["content"] == _OUTPUT
    assert reply["truncated"] is False
    assert "redacted" in reply


def test_the_artifact_tool_takes_a_name_and_never_a_path_or_an_execution_parameter():
    """Invariant 10, and the route's own rule: a NAME from the manifest."""
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_artifact")["inputSchema"]
    assert set(schema["required"]) == {"task_id", "name"}
    forbidden = {"image", "command", "uri", "path", "key", "bucket"}
    assert forbidden.isdisjoint(schema["properties"]), sorted(forbidden & set(schema["properties"]))


# ==========================================================================
# Epic #227: the `since` token's checksum
# ==========================================================================


def _token() -> str:
    return progress.encode_since(
        {"task_a": {"events": 3, "attempt_id": "att_1", "streams": {}}}, {"task_a": "RUNNING"}, {}
    )


def test_a_since_token_carries_a_checksum_it_is_checked_against():
    token = _token()
    body, _, checksum = token.rpartition(".")
    assert body and len(checksum) == 8, token
    # The control: the token as issued is read back.
    cursor, states, _said, _groups, note = progress.decode_since(token)
    assert cursor["task_a"]["events"] == 3 and states == {"task_a": "RUNNING"} and note is None


def _one_character_changed(token: str, at: int) -> str:
    swap = "A" if token[at] != "A" else "B"
    return token[:at] + swap + token[at + 1:]


@pytest.mark.parametrize("where", ["payload", "checksum"])
def test_a_since_token_changed_by_one_character_is_refused_with_a_clear_error(where):
    """A relay retypes this token on every call. Base64 JSON with no checksum
    can decode, after a one-character slip, to a DIFFERENT position -- and a
    position that silently moved forward loses output."""
    token = _token()
    at = 10 if where == "payload" else len(token) - 2
    changed = _one_character_changed(token, at)

    with pytest.raises(SwarmError) as caught:
        progress.decode_since(changed)
    message = str(caught.value)
    assert "checksum" in message and "unchanged" in message, message


def test_a_changed_since_is_refused_by_the_follow_tool_before_anything_is_read(swarm, world):
    world.task("task_a")
    changed = _one_character_changed(_token(), 10)
    for fmt in ("json", "lines"):
        with pytest.raises(SwarmError, match="checksum"):
            server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "since": changed, "format": fmt})


# ==========================================================================
# Epic #227: watch() puts `lines` first
# ==========================================================================


def test_watch_returns_lines_before_the_cursor_and_the_tasks(swarm, world):
    """The /workflows row detail shows the START of each result; with `since`
    first it showed a cursor instead of what the task is doing."""
    world.task("task_a", state="QUEUED")

    reply = progress.watch(swarm, ["task_a"])
    assert list(reply)[0] == "lines", list(reply)

    through_the_tool = json.loads(
        server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "format": "lines"})
    )
    assert list(through_the_tool)[0] == "lines", list(through_the_tool)


# ==========================================================================
# Epic #227: a spec file, read by the bridge for /sc:run
# ==========================================================================


_SPEC = {"label": "scan", "steps": [{"step_id": "a", "prompt": "x"}, {"step_id": "b", "prompt": "y", "depends_on": ["a"]}]}


class _Nothing:
    """A client that must not be used: reading a spec submits nothing."""

    def request(self, *a, **k):  # pragma: no cover - failing is the assertion
        raise AssertionError("swarm_workflow_spec made a request")


def test_the_bridge_reads_a_spec_file_and_hands_back_its_digest_submitting_nothing(tmp_path, monkeypatch):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "scan.json").write_text(json.dumps(_SPEC, indent=2))
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))

    reply = json.loads(server._call(_Nothing(), "swarm_workflow_spec", {"path": "specs/scan.json"}))

    assert reply["spec"] == _SPEC
    assert reply["spec_digest"] == workflows.spec_digest(_SPEC)
    assert reply["path"] == str((tmp_path / "specs" / "scan.json").resolve())


def test_a_file_that_is_not_a_spec_is_refused_without_echoing_what_it_holds(tmp_path, monkeypatch):
    """The path comes through a relay; a file that is not a spec may be
    anything, so its content is never repeated back."""
    (tmp_path / "notes.txt").write_text("SECRET-VALUE-0123 is not json")
    (tmp_path / "other.json").write_text(json.dumps({"image": "SECRET-IMAGE", "steps": []}))
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))

    for name in ("notes.txt", "missing.json"):
        with pytest.raises(SwarmError) as caught:
            server._call(_Nothing(), "swarm_workflow_spec", {"path": name})
        assert "SECRET" not in str(caught.value), str(caught.value)
    with pytest.raises(SwarmError) as caught:
        server._call(_Nothing(), "swarm_workflow_spec", {"path": "other.json"})
    assert "SECRET-IMAGE" not in str(caught.value), str(caught.value)

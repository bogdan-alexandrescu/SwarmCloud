"""BROWSER-USE-FLAG (part of #2322): whether a browser runtime was used.

The 2026-10-25 audit had to read agent logs by hand to find 49 browser tasks
that never touched their browser: the runner counted screenshots and nothing
else. Now the claude-code runner counts every browser call (the browser MCP
tools, and the Bash commands that drive the browser or the gate) and every
image under the artifacts, and the worker records `browser_used` -- true,
false, or null when the attempt had no browser or its counts could not be
read -- in the task's `result_summary`, with the counts and the seconds.

No Chromium, no network, no credentials: the runner's CLI is replaced, and
the full-lifecycle case runs the fake `claude` of test_standalone_outputs.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.runners import claude_code

from test_standalone_outputs import _run, _seed, _summary, agent_cli  # noqa: F401

ENDPOINT = "http://127.0.0.1:9222"


def _call(name: str, tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """One tool call and its result, as the CLI's stream-json prints them."""
    call = "toolu_" + secrets.token_hex(8)
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": call, "name": name, "input": tool_input}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call, "content": [{"type": "text", "text": "ok"}]}]}},
    ]


def _stream(*calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "system", "subtype": "init", "session_id": "s"}]
    for call in calls:
        events.extend(call)
    events.append({"type": "result", "subtype": "success", "result": "done"})
    return events


def _ctx(tmp_path: Path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Check the settings page."},
    )


def _run_body(monkeypatch, tmp_path: Path, *, stream: list[dict[str, Any]] | None,
              images: tuple[str, ...] = (), truncated: bool = False) -> dict[str, Any]:
    """The runner's body on a live-browser attempt, its CLI replaced by one
    that writes `stream` as its stdout and `images` under the artifacts."""
    from agent_worker.runners.cliagent import cli_stream_files

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if stream is not None:
            path = ctx.artifacts_dir / cli_stream_files(spec).stdout
            path.write_text("".join(json.dumps(e) + "\n" for e in stream))
        for name in images:
            target = ctx.artifacts_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG" + secrets.token_bytes(8))
        return {"summary": "", "stdout_truncated": truncated}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in (claude_code.GIT_SPOOL_ENV, claude_code.SEARCH_SPOOL_ENV, claude_code.HANDOFF_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, "/opt/playwright")
    monkeypatch.setenv(claude_code.CDP_ENDPOINT_ENV, ENDPOINT)
    return claude_code.body(_ctx(tmp_path))


# --------------------------------------------------------------------------
# The runner's counts
# --------------------------------------------------------------------------

def test_every_browser_tool_and_browser_command_is_counted_and_nothing_else():
    stream = _stream(
        _call("mcp__playwright__browser_navigate", {"url": "http://127.0.0.1:5173/"}),
        _call("mcp__playwright__browser_snapshot", {}),
        _call("mcp__jev__judge_page", {}),
        _call("Bash", {"command": "swarm-handoff serve ./mockups"}),
        _call("Bash", {"command": "swarm-handoff device iphone-15"}),
        _call("Bash", {"command": "swarm-handoff theme dark"}),
        _call("Bash", {"command": "dev-browser --help"}),
        _call("Bash", {"command": "python -c 'from swarm_browser import live; live()'"}),
        # Not the browser: the agent's ordinary work.
        _call("Bash", {"command": "uv run pytest tests/unit/worker -q"}),
        _call("Bash", {"command": "swarm-handoff wait"}),
        _call("Read", {"file_path": "apps/swarm-ui/src/App.tsx"}),
        _call("mcp__swarm-git__git_fetch", {}),
    )
    # MUTATION: count only screenshots (the old rule) and this is 0 and 0.
    assert claude_code.browser_calls(stream) == {"tool": 3, "command": 5}
    assert claude_code.browser_calls([]) == {"tool": 0, "command": 0}
    assert claude_code.browser_calls(None) == {"tool": 0, "command": 0}


def test_an_attempt_with_browser_calls_reports_them(monkeypatch, tmp_path):
    stream = _stream(
        _call("mcp__playwright__browser_navigate", {"url": "http://127.0.0.1:5173/"}),
        _call("Bash", {"command": "swarm-handoff theme dark"}),
    )
    result = _run_body(monkeypatch, tmp_path, stream=stream)
    # MUTATION: drop the report from the runner's result and this is a KeyError.
    assert result["browser_use"] == {
        "read": True, "complete": True, "tool_calls": 2, "mcp_calls": 1, "command_calls": 1, "images": 0,
    }
    # The screenshot count is kept beside it.
    assert result["browser_screenshots"] == {"taken": 0, "seen_as_image": 0, "complete": True}


def test_an_image_rendered_by_a_script_is_counted_with_no_tool_call(monkeypatch, tmp_path):
    result = _run_body(monkeypatch, tmp_path, stream=_stream(),
                       images=("render/settings-dark.png", "flows/login.gif", "notes.md"))
    assert result["browser_use"]["tool_calls"] == 0
    # MUTATION: count only screenshots under screenshots/ and this is 0.
    assert result["browser_use"]["images"] == 2


def test_an_unread_stream_is_no_count_rather_than_zero(monkeypatch, tmp_path):
    result = _run_body(monkeypatch, tmp_path, stream=None)
    # MUTATION: swallow the read error as an empty stream and this is 0.
    assert result["browser_use"]["read"] is False
    assert result["browser_use"]["tool_calls"] is None


def test_a_cut_stream_says_so(monkeypatch, tmp_path):
    result = _run_body(monkeypatch, tmp_path, stream=_stream(), truncated=True)
    assert result["browser_use"]["complete"] is False


# --------------------------------------------------------------------------
# The worker's verdict
# --------------------------------------------------------------------------

def _counts(**overrides: Any) -> dict[str, Any]:
    base = {"read": True, "complete": True, "tool_calls": 0, "mcp_calls": 0, "command_calls": 0, "images": 0}
    base.update(overrides)
    return base


def test_browser_calls_record_browser_used_true_with_the_counts():
    record = lifecycle.browser_use_record(True, _counts(tool_calls=7, mcp_calls=5, command_calls=2), 812.34)
    assert record["browser_used"] is True
    assert record["browser_use"] == {
        "tool_calls": 7, "mcp_calls": 5, "command_calls": 2, "images": 0, "complete": True, "seconds": 812.3,
    }


def test_no_browser_call_and_no_image_records_false():
    # MUTATION: call an attempt with no calls "unknown" and this is None.
    assert lifecycle.browser_use_record(True, _counts(), 600.0)["browser_used"] is False


def test_an_image_only_render_counts_as_used():
    # MUTATION: decide on tool calls alone and this is False.
    assert lifecycle.browser_use_record(True, _counts(images=3), 60.0)["browser_used"] is True


def test_a_non_browser_runtime_records_null_and_nothing_else():
    # MUTATION: decide on the counts whatever the runtime and this is False.
    assert lifecycle.browser_use_record(False, _counts(), 60.0) == {"browser_used": None}
    assert lifecycle.browser_use_record(False, None, None) == {"browser_used": None}


def test_counts_that_could_not_be_read_record_null_with_why():
    unread = lifecycle.browser_use_record(True, _counts(read=False, tool_calls=None), 60.0)
    assert unread["browser_used"] is None
    assert unread["browser_use"]["unknown_because"] == "stream_unread"
    missing = lifecycle.browser_use_record(True, None, 60.0)
    assert missing["browser_used"] is None
    assert missing["browser_use"]["unknown_because"] == "not_reported"
    # A cut stream showing nothing is a lower bound of 0, not a measurement...
    cut = lifecycle.browser_use_record(True, _counts(complete=False), 60.0)
    assert cut["browser_used"] is None and cut["browser_use"]["unknown_because"] == "stream_cut"
    # ...but a cut stream that shows a call is still a use.
    assert lifecycle.browser_use_record(True, _counts(complete=False, tool_calls=1), 60.0)["browser_used"] is True


def _bound_worker(tmp_path: Path, *, live: bool, output: dict[str, Any] | None) -> SimpleNamespace:
    """What `Worker._add_runner_block` reads of a worker, and no more."""
    result = tmp_path / "result.json"
    if output is not None:
        result.write_text(json.dumps({"status": "succeeded", "summary": "done", "output": output}))
    return SimpleNamespace(
        ws=SimpleNamespace(result_path=result),
        _spend={},
        _scrub=lambda value: value,
        log=SimpleNamespace(warning=lambda *a, **k: None),
        handoff=SimpleNamespace(live=live),
        _child=None,
    )


def test_the_worker_writes_the_verdict_into_the_result_summary(tmp_path):
    # Bulky output, so the runner block's own copy is truncated: the verdict
    # is read from the result file, not from that copy.
    output = {"browser_use": _counts(tool_calls=4, mcp_calls=4), "filler": "x" * 9000}
    summary: dict[str, Any] = {"duration_seconds": 1234.5}
    lifecycle.Worker._add_runner_block(_bound_worker(tmp_path, live=True, output=output), summary)
    assert summary["runner"]["output"]["truncated"] is True
    # MUTATION: read the verdict from `runner.output` and this is None.
    assert summary["browser_used"] is True
    assert summary["browser_use"]["tool_calls"] == 4
    assert summary["browser_use"]["seconds"] == 1234.5


def test_a_browser_attempt_with_no_call_writes_false(tmp_path):
    summary: dict[str, Any] = {"duration_seconds": 900.0}
    lifecycle.Worker._add_runner_block(
        _bound_worker(tmp_path, live=True, output={"browser_use": _counts()}), summary)
    assert summary["browser_used"] is False
    assert summary["browser_use"]["seconds"] == 900.0


def test_a_claude_code_task_records_null_end_to_end(db, worker_factory, agent_cli):  # noqa: F811
    _seed(db, {})
    assert _run(worker_factory) == ExitCode.OK
    summary = _summary(db)
    # MUTATION: skip the record off the browser runtime and the key is absent.
    assert "browser_used" in summary and summary["browser_used"] is None
    assert "browser_use" not in summary


# --------------------------------------------------------------------------
# The one `browser_unused` mark
# --------------------------------------------------------------------------

def _idle_worker(tmp_path: Path, *, stream: list[dict[str, Any]]) -> SimpleNamespace:
    from agent_worker.runners.cliagent import cli_stream_files

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / cli_stream_files(claude_code.SPEC).stdout).write_text(
        "".join(json.dumps(e) + "\n" for e in stream))
    marks: list[tuple[str, dict[str, Any]]] = []
    worker = SimpleNamespace(
        _browser_idle_checked=False,
        handoff=SimpleNamespace(live=True),
        ws=SimpleNamespace(artifacts=artifacts),
        cfg=SimpleNamespace(runner_profile="claude-code-browser"),
        log=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        marks=marks,
    )
    worker._emit_startup_mark = lambda cause, detail: marks.append((cause, dict(detail)))
    return worker


def test_ten_minutes_with_no_browser_call_is_marked_once(tmp_path):
    worker = _idle_worker(tmp_path, stream=_stream(_call("Bash", {"command": "ls"})))
    early = SimpleNamespace(elapsed_seconds=lifecycle.BROWSER_IDLE_SECONDS - 1)
    lifecycle.Worker._note_browser_idle(worker, early)
    assert worker.marks == []
    late = SimpleNamespace(elapsed_seconds=lifecycle.BROWSER_IDLE_SECONDS + 5)
    lifecycle.Worker._note_browser_idle(worker, late)
    lifecycle.Worker._note_browser_idle(worker, late)
    # MUTATION: forget that it looked and this is two marks.
    assert [cause for cause, _ in worker.marks] == ["browser_unused"]
    assert worker.marks[0][1]["seconds"] == lifecycle.BROWSER_IDLE_SECONDS + 5


def test_an_agent_that_used_its_browser_is_not_marked(tmp_path):
    worker = _idle_worker(tmp_path, stream=_stream(_call("mcp__playwright__browser_snapshot", {})))
    lifecycle.Worker._note_browser_idle(worker, SimpleNamespace(elapsed_seconds=lifecycle.BROWSER_IDLE_SECONDS))
    assert worker.marks == []


def test_a_non_browser_attempt_is_never_marked(tmp_path):
    worker = _idle_worker(tmp_path, stream=_stream())
    worker.handoff = SimpleNamespace(live=False)
    lifecycle.Worker._note_browser_idle(worker, SimpleNamespace(elapsed_seconds=10_000))
    assert worker.marks == []

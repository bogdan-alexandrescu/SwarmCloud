"""The agent's OWN streams: named once, streamed as JSON lines, uploaded beside the runner's.

#184 (owner decisions 2026-09-25). The task drawer showed the RUNNER process's
stdout and stderr -- its own JSON log lines, "child started" -- under the label
"Output, as the agent wrote it". The agent's own streams are the files the
runner captures the agent CLI into, under `artifacts/`. This file pins:

  * ONE function names those files per profile, and every reader uses it;
  * claude-code prints stream-json, so there is something to watch while it
    runs and a record of every turn, not only the last;
  * the final upload keeps a copy of both agent streams beside the runner's
    logs and says, in `result_summary.agent_streams`, which artifact is which
    -- or `null` for a runner that has no agent CLI at all;
  * the transcript artifact is whole JSON or absent, never a cut document;
  * a streamed run's conversation cannot park a failed attempt as rate-limited.

Imports of new names are inside the tests, so each fails on its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_worker import workspace as workspace_mod


def make_ctx(tmp_path: Path, payload: dict):
    from agent_worker.runners.base import RunnerContext

    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


def _fake_cli(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "fake-cli"
    binary.write_text("#!/usr/bin/env python3\n" + body)
    binary.chmod(0o755)
    return binary


def _spec(**overrides):
    from agent_worker.runners.cliagent import CliAgentSpec

    base = dict(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY", model_flag=None,
        transcript_name="fake-transcript.json",
    )
    base.update(overrides)
    return CliAgentSpec(**base)


# ---------------------------------------------------------------------------
# one function names the files
# ---------------------------------------------------------------------------


def test_the_agent_stream_files_come_from_one_function_per_profile():
    from agent_worker.runners.streams import agent_stream_files

    claude = agent_stream_files("claude-code")
    assert (claude.stdout, claude.stderr, claude.transcript) == (
        "claude-code.stdout.log", "claude-code.stderr.log", "claude-transcript.json",
    )
    codex = agent_stream_files("codex")
    assert (codex.stdout, codex.stderr, codex.transcript) == (
        "codex.stdout.log", "codex.stderr.log", "codex-transcript.json",
    )
    generic = agent_stream_files("generic")
    assert (generic.stdout, generic.stderr, generic.transcript) == (
        "command.stdout.log", "command.stderr.log", None,
    )
    assert agent_stream_files("mock") is None
    assert agent_stream_files("browser") is None
    assert agent_stream_files("no-such-profile") is None


def test_the_cli_runner_writes_exactly_the_files_that_function_names(tmp_path, monkeypatch):
    """The names the worker publishes and uploads are the names the runner writes to."""
    from agent_worker.runners.cliagent import cli_stream_files, run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv(
        "FAKE_BIN",
        str(_fake_cli(tmp_path, "import sys\nprint('{\"result\": \"ok\"}')\nprint('w', file=sys.stderr)\n")),
    )
    spec = _spec()
    ctx = make_ctx(tmp_path, {"prompt": "hi"})
    run_cli_agent(ctx, spec)

    files = cli_stream_files(spec)
    assert (ctx.artifacts_dir / files.stdout).read_text().strip() == '{"result": "ok"}'
    assert (ctx.artifacts_dir / files.stderr).read_text().strip() == "w"
    assert (ctx.artifacts_dir / files.transcript).exists()


def test_claude_code_prints_stream_json_by_default():
    """With `--output-format json` the CLI prints one object at the END: nothing
    to watch while it runs, and none of the turns recorded. stream-json prints
    every event as it happens; the CLI requires `--verbose` beside it."""
    from agent_worker.runners.claude_code import SPEC

    args = SPEC.args_default
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args
    assert "--print" in args
    assert "--dangerously-skip-permissions" in args


# ---------------------------------------------------------------------------
# the final upload
# ---------------------------------------------------------------------------


def _worker_with_workspace(worker_factory, profile):
    worker, config, _ = worker_factory(runner_profile=profile)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    return worker, config


def test_the_final_upload_keeps_the_agent_streams_beside_the_runners_logs(
    worker_factory, store
):
    worker, config = _worker_with_workspace(worker_factory, "claude-code")
    ws = worker.ws
    (ws.artifacts / "claude-code.stdout.log").write_text('{"type":"result","result":"done"}\n')
    (ws.artifacts / "claude-code.stderr.log").write_text("agent stderr\n")
    (ws.artifacts / "claude-transcript.json").write_text('{"type": "result"}')
    ws.stderr_path.write_text('{"message":"child started"}\n')

    summary = worker._upload_outputs()

    prefix = config.log_prefix
    assert store.download_bytes(f"{prefix}/agent_stdout.log") == (
        b'{"type":"result","result":"done"}\n'
    )
    assert store.download_bytes(f"{prefix}/agent_stderr.log") == b"agent stderr\n"
    assert summary["logs"]["agent_stdout"].endswith("/logs/agent_stdout.log")
    assert summary["logs"]["agent_stderr"].endswith("/logs/agent_stderr.log")
    assert summary["logs"]["stderr"].endswith("/logs/stderr.log")
    assert summary["agent_streams"] == {
        "stdout": "claude-code.stdout.log",
        "stderr": "claude-code.stderr.log",
        "transcript": "claude-transcript.json",
        "transcript_skipped": None,
        # No result.json here, so the runner reported nothing about its
        # capture: unknown, which is not "not cut".
        "stdout_truncated": None,
        "stderr_truncated": None,
    }
    # The artifacts stay where they are: a dependant's input_from stages them.
    names = {entry["name"] for entry in summary["artifacts"]}
    assert {"claude-code.stdout.log", "claude-code.stderr.log", "claude-transcript.json"} <= names


def test_a_runner_with_no_agent_cli_says_so_rather_than_saying_nothing(worker_factory, store):
    """`agent_streams: null` -- a present key holding null -- is what lets a
    reader answer "not applicable" instead of "absent"."""
    worker, config = _worker_with_workspace(worker_factory, "mock")
    worker.ws.stdout_path.write_text("runner output\n")

    summary = worker._upload_outputs()

    assert "agent_streams" in summary and summary["agent_streams"] is None
    assert "agent_stdout" not in summary["logs"]
    assert store.list_keys(f"{config.log_prefix}/agent_") == []


def test_a_transcript_omitted_as_too_large_is_named_in_the_summary(worker_factory):
    worker, _config = _worker_with_workspace(worker_factory, "claude-code")
    ws = worker.ws
    (ws.artifacts / "claude-code.stdout.log").write_text('{"type":"result"}\n')
    ws.result_path.write_text(
        json.dumps({"status": "succeeded", "output": {"transcript_skipped": "too_large"}})
    )

    summary = worker._upload_outputs()

    assert summary["agent_streams"]["transcript"] is None
    assert summary["agent_streams"]["transcript_skipped"] == "too_large"
    assert summary["agent_streams"]["stderr"] is None, "a stream never written is not named"


def test_the_summary_says_which_agent_stream_was_cut_at_its_cap(worker_factory):
    """The runner reports its capture on EVERY outcome -- a run that failed
    after passing its cap included -- and the reader of `result_summary`
    must be able to tell a capped stream from a whole one (#188 review)."""
    worker, _config = _worker_with_workspace(worker_factory, "claude-code")
    ws = worker.ws
    (ws.artifacts / "claude-code.stdout.log").write_text('{"type":"result"}\n')
    ws.result_path.write_text(json.dumps({"status": "failed", "output": {
        "stdout_truncated": True,
        "stderr_truncated": False,
        "transcript_skipped": "capture_truncated",
    }}))

    streams = worker._upload_outputs()["agent_streams"]

    assert streams["stdout_truncated"] is True
    assert streams["stderr_truncated"] is False
    assert streams["transcript_skipped"] == "capture_truncated"


def test_an_agent_stream_that_is_a_symlink_is_not_uploaded_as_a_log(worker_factory, store, tmp_path):
    worker, config = _worker_with_workspace(worker_factory, "claude-code")
    outside = tmp_path / "outside.txt"
    outside.write_text("not the agent's\n")
    (worker.ws.artifacts / "claude-code.stdout.log").symlink_to(outside)

    summary = worker._upload_outputs()

    assert "agent_stdout" not in summary["logs"]
    assert store.list_keys(f"{config.log_prefix}/agent_stdout") == []


# ---------------------------------------------------------------------------
# the transcript artifact: whole or absent
# ---------------------------------------------------------------------------


def test_the_transcript_artifact_is_whole_json(tmp_path, monkeypatch):
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "result": "done", "is_error": False},
    ]
    printer = "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events)
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path, printer)))
    ctx = make_ctx(tmp_path, {"prompt": "hi"})

    out = run_cli_agent(ctx, _spec())

    transcript = json.loads((ctx.artifacts_dir / "fake-transcript.json").read_text())
    assert transcript == events
    assert out["transcript_skipped"] is None
    assert out["summary"] == "done"


def test_a_transcript_over_the_cap_is_omitted_not_cut(tmp_path, monkeypatch):
    """It used to be `json.dumps(parsed, indent=2)[:4_000_000]`: a long run's
    document cut mid-token, under a name that promised JSON."""
    from agent_worker.runners import cliagent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setattr(cliagent, "TRANSCRIPT_MAX_CHARS", 200)
    monkeypatch.setenv(
        "FAKE_BIN",
        str(_fake_cli(tmp_path, "import json\nprint(json.dumps({'result': 'x' * 500}))\n")),
    )
    ctx = make_ctx(tmp_path, {"prompt": "hi"})

    out = cliagent.run_cli_agent(ctx, _spec())

    assert not (ctx.artifacts_dir / "fake-transcript.json").exists()
    assert out["transcript_skipped"] == "too_large"
    # The canonical record is whole either way.
    assert json.loads((ctx.artifacts_dir / "fake.stdout.log").read_text())["result"] == "x" * 500


# ---------------------------------------------------------------------------
# a streamed conversation is not rate-limit evidence
# ---------------------------------------------------------------------------


def _stream_cli(tmp_path: Path, events: list[dict], *, exit_code: int, stderr: str = "") -> Path:
    lines = "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events)
    tail = f"import sys\nsys.stderr.write({stderr!r})\nsys.exit({exit_code})\n"
    return _fake_cli(tmp_path, lines + tail)


def test_a_failed_stream_run_whose_tools_mention_a_rate_limit_fails_not_parks(
    tmp_path, monkeypatch
):
    """Under stream-json the stdout holds the whole conversation. An agent that
    read a file about HTTP 429s prints every marker the heuristic looks for,
    and a FAILED run used to be parked as rate-limited over it -- the failure
    a person needs to see replaced by a wait for a quota that was never short."""
    from agent_worker.runners.base import QuotaExhaustedSignal, RunnerFailure
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "The client retries on 429 Too Many Requests (rate limit)."}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
            "content": "retry-after: 30\nrate_limit_error handled here\ntoo many requests"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "the tests failed"},
    ]
    monkeypatch.setenv("FAKE_BIN", str(_stream_cli(tmp_path, events, exit_code=1)))

    with pytest.raises(RunnerFailure) as caught:
        run_cli_agent(make_ctx(tmp_path, {"prompt": "fix the tests"}), _spec())
    assert not isinstance(caught.value, QuotaExhaustedSignal)


def test_an_allowed_rate_limit_reading_does_not_park_a_failed_run(tmp_path, monkeypatch):
    """Every streamed run carries `rate_limit_event` readings. One that says the
    request was allowed is a reading, not a refusal."""
    from agent_worker.runners.base import RunnerFailure
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1758800000}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "compile error"},
    ]
    monkeypatch.setenv("FAKE_BIN", str(_stream_cli(tmp_path, events, exit_code=1)))

    with pytest.raises(RunnerFailure):
        run_cli_agent(make_ctx(tmp_path, {"prompt": "build"}), _spec())


def test_a_stream_run_whose_result_is_a_rate_limit_still_parks(tmp_path, monkeypatch):
    """The narrowing keeps the provider's own words: the result event and stderr."""
    from agent_worker.runners.base import QuotaExhaustedSignal
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": "API Error: 429 rate_limit_error. Retry-After: 1800"},
    ]
    monkeypatch.setenv("FAKE_BIN", str(_stream_cli(tmp_path, events, exit_code=1)))

    with pytest.raises(QuotaExhaustedSignal) as caught:
        run_cli_agent(make_ctx(tmp_path, {"prompt": "hi"}), _spec())
    assert caught.value.retry_after_seconds == 1800


def test_a_rejected_rate_limit_reading_is_still_evidence(tmp_path, monkeypatch):
    from agent_worker.runners.base import QuotaExhaustedSignal
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour"}},
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": "stopped"},
    ]
    monkeypatch.setenv("FAKE_BIN", str(_stream_cli(tmp_path, events, exit_code=1)))

    with pytest.raises(QuotaExhaustedSignal):
        run_cli_agent(make_ctx(tmp_path, {"prompt": "hi"}), _spec())


def test_the_single_object_format_is_judged_exactly_as_before(tmp_path, monkeypatch):
    """A json-format CLI (one object) keeps its whole stdout tail as evidence."""
    from agent_worker.runners.base import QuotaExhaustedSignal
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    body = (
        "import json, sys\n"
        "print(json.dumps({'type': 'result', 'is_error': True, "
        "'result': 'rate limit reached, try again in 5 minutes'}))\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path, body)))

    with pytest.raises(QuotaExhaustedSignal) as caught:
        run_cli_agent(make_ctx(tmp_path, {"prompt": "hi"}), _spec())
    assert caught.value.retry_after_seconds == 300

"""A capped stream keeps its END, and the cut is reported on every outcome (#188 review).

WHAT WENT WRONG. claude-code prints `--output-format stream-json` since #184,
so its stdout is the whole conversation, every tool result included, and it is
still captured under `max_stdout_bytes` (32 MiB). `procman.StreamCapture`
wrote the first 32 MiB and DISCARDED the rest -- and the `result` event is
always the LAST line. A long session therefore lost its answer, its spend and
the evidence the rate-limit decision reads, and nothing anywhere said so.

WHAT IS PINNED.
  * a capture that opts in keeps the last bytes of a stream past its cap and
    writes them, on whole lines, after a notice saying how much was dropped;
  * a stream that passes the head but fits the cap is written whole, and the
    live file said, while it ran, that its end was being held back;
  * the default capture -- git's, which judges a patch by its size -- keeps
    its old shape, and now marks the cut even when the cap falls exactly on a
    read boundary;
  * the CLI runner opts in: a stream-json run over its cap keeps its result
    event, its answer and its spend, and reports the cut on every outcome;
  * the capture's own notice is never rate-limit evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MARK = b"[swarm] output truncated"


class _Pipe:
    """A pipe that hands out at most `size` bytes per read, like `read1` does.

    `on_read` runs before each read, when the capture file holds exactly what
    the live-tail publisher would have read at that moment.
    """

    def __init__(self, data: bytes, size: int, on_read=None) -> None:
        self._data = data
        self._size = size
        self._at = 0
        self._on_read = on_read

    def read1(self, n: int) -> bytes:
        if self._on_read is not None:
            self._on_read()
        piece = self._data[self._at : self._at + min(n, self._size)]
        self._at += len(piece)
        return piece

    def close(self) -> None:
        pass


def _lines(count: int, width: int = 40) -> bytes:
    """`count` numbered lines, each exactly `width` bytes with its newline."""
    return b"".join(
        f"event {n:05d} ".encode().ljust(width - 1, b".") + b"\n" for n in range(count)
    )


# ---------------------------------------------------------------------------
# the capture
# ---------------------------------------------------------------------------


def test_the_default_capture_keeps_its_head_and_marks_the_cut_on_a_read_boundary(tmp_path):
    """Unchanged for its other users, with one fix: a cap reached exactly at
    the end of one read used to drop everything after it with no notice."""
    from agent_worker.procman import TRUNCATION_NOTICE, StreamCapture

    data = _lines(100)
    capture = StreamCapture(tmp_path / "out.log", 1000)
    capture.pump(_Pipe(data, 500))

    assert (tmp_path / "out.log").read_bytes() == data[:1000] + TRUNCATION_NOTICE
    assert capture.truncated is True
    assert capture.written == 1000


def test_a_capture_that_keeps_its_tail_writes_head_notice_and_end_on_whole_lines(tmp_path):
    from agent_worker.procman import StreamCapture

    result_line = b'{"type": "result", "result": "the end"}\n'
    data = _lines(200) + result_line
    path = tmp_path / "out.log"
    capture = StreamCapture(path, 2000, keep_tail=True)
    capture.pump(_Pipe(data, 300))
    written = path.read_bytes()

    head, found, rest = written.partition(b"\n" + MARK)
    assert found, written[:200]
    assert head == data[:1000], "the head is the cap less the room kept for the end"
    notice, _, kept = rest.partition(b"\n")
    assert b"bytes were dropped here" in notice
    assert data.endswith(kept), "the kept bytes are the stream's own last bytes"
    assert kept.endswith(result_line), "so the last line -- the result event -- survives"
    assert data[len(data) - len(kept) - 1 : len(data) - len(kept)] == b"\n", (
        "the kept end starts on a whole line"
    )
    assert capture.truncated is True
    assert capture.dropped == len(data) - 1000 - len(kept)
    assert capture.written == 1000 + len(kept)
    assert len(written) <= 2000 + len(notice) + 2, "the cap still bounds the file"


def test_a_stream_that_fits_its_cap_is_whole_and_the_live_file_said_its_end_was_held(tmp_path):
    from agent_worker.procman import StreamCapture

    data = _lines(40)  # 1600 bytes: past the 1000-byte head, inside the 2000-byte cap
    path = tmp_path / "out.log"
    seen: list[bytes] = []
    capture = StreamCapture(path, 2000, keep_tail=True)
    capture.pump(_Pipe(data, 200, on_read=lambda: seen.append(path.read_bytes())))

    assert path.read_bytes() == data, "nothing was dropped, so nothing is missing"
    assert capture.truncated is False
    assert capture.dropped == 0
    assert capture.written == len(data)
    assert any(MARK in snapshot for snapshot in seen), (
        "while it ran, the live file said its end was being held back"
    )


# ---------------------------------------------------------------------------
# the CLI runner
# ---------------------------------------------------------------------------


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


def _spec():
    from agent_worker.runners.cliagent import CliAgentSpec

    return CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY", model_flag=None,
        transcript_name="fake-transcript.json",
    )


RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "the final answer", "total_cost_usd": 1.25, "num_turns": 400,
    "usage": {"input_tokens": 10, "output_tokens": 20},
}


def _long_run(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """A fake CLI that prints about 110 KB of stream-json, then its result."""
    binary = tmp_path / "fake-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "for n in range(400):\n"
        "    print(json.dumps({'type': 'assistant', 'message': {'content': [\n"
        "        {'type': 'text', 'text': f'turn {n} ' + 'x' * 200}]}}))\n"
        f"print(json.dumps({RESULT!r}))\n"
        f"sys.exit({exit_code})\n"
    )
    binary.chmod(0o755)
    return binary


def test_a_stream_json_run_over_its_cap_keeps_its_result_its_answer_and_its_spend(
    tmp_path, monkeypatch
):
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(_long_run(tmp_path)))
    ctx = make_ctx(tmp_path, {"prompt": "refactor", "max_stdout_bytes": 32768})

    out = run_cli_agent(ctx, _spec())

    stdout = (ctx.artifacts_dir / "fake.stdout.log").read_bytes()
    assert MARK in stdout
    assert stdout.endswith(json.dumps(RESULT).encode() + b"\n"), stdout[-300:]
    assert out["summary"] == "the final answer"
    assert out["structured_output"]["total_cost_usd"] == 1.25
    assert out.get("stdout_truncated") is True
    assert out.get("stdout_dropped_bytes", 0) > 0
    # A transcript built from a stream with its middle gone would not say so;
    # the stdout capture, which does, is the record.
    assert out.get("transcript_skipped") == "capture_truncated"
    assert not (ctx.artifacts_dir / "fake-transcript.json").exists()


def test_a_run_that_fails_over_its_cap_still_reports_the_cut_and_its_spend(
    tmp_path, monkeypatch
):
    from agent_worker.runners.base import RunnerFailure
    from agent_worker.runners.cliagent import run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(_long_run(tmp_path, exit_code=1)))
    ctx = make_ctx(tmp_path, {"prompt": "refactor", "max_stdout_bytes": 32768})

    with pytest.raises(RunnerFailure) as caught:
        run_cli_agent(ctx, _spec())
    assert caught.value.spend.get("total_cost_usd") == 1.25, "the kept result carries the spend"

    # What `run_runner` writes for a failure: the capture report rides along.
    ctx.write_result(status="failed", summary=str(caught.value), output={})
    written = json.loads(ctx.result_path.read_text())["output"]
    assert written["stdout_truncated"] is True
    assert written["stdout_dropped_bytes"] > 0


def test_the_captures_own_notice_is_never_rate_limit_evidence():
    """The notice counts the bytes it dropped, and `429` is a marker the
    heuristic matches anywhere in the text: 4,290,117 dropped bytes would
    park a failed run as rate-limited. The line the cap cut in half, just
    above the notice, is not evidence either."""
    from agent_worker.runners.cliagent import _detection_text, detect_rate_limit

    notice = (
        "[swarm] output truncated: size cap reached; 4290117 bytes were dropped here, "
        "and the last 1048576 bytes of the stream follow"
    )
    init = json.dumps({"type": "system", "subtype": "init"})
    result = json.dumps({"type": "result", "is_error": True, "result": "the tests failed"})
    cut_line = '{"type": "assistant", "message": {"content": "HTTP 429 fro'
    streamed = "\n".join([init, cut_line, notice, result]) + "\n"
    parsed = [json.loads(init), json.loads(result)]
    assert detect_rate_limit(_detection_text(streamed, parsed))[0] is False

    prose = "working...\n" + notice + "\nstill working\n"
    assert detect_rate_limit(_detection_text(prose, None))[0] is False

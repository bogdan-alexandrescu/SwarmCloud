"""A child's output reaches its capture file WHILE the child runs, not when it exits.

THE ROOT CAUSE OF "NOTHING IS LIVE" (#184). `procman.StreamCapture.pump` read
the Popen pipe with `read(65536)`. The pipe is a `BufferedReader`, and
`BufferedReader.read(n)` blocks until `n` bytes have arrived or the pipe
closes -- so a child that printed and flushed one short line had that line
reach the capture file only when it EXITED. Measured: a 392-byte line written
at t=0 appeared at exit, 3.1 s later. Both captures use this class -- the
worker's of the runner, and the runner's of the agent CLI -- so no stream
under 64 KiB existed on disk during an attempt, and the live-tail publisher
published nothing: zero `logs/live/` objects across 118 staging attempts.

The test is the probe: a real child writes and flushes 392 bytes and then
sleeps; the bytes must be in the file while it is still sleeping.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

from agent_worker.logs import StructuredLogger
from agent_worker.procman import ChildProcess

LINE = b"x" * 391 + b"\n"


def _wait_for(path: Path, size: int, *, seconds: float) -> int:
    deadline = time.monotonic() + seconds
    seen = 0
    while time.monotonic() < deadline:
        seen = path.stat().st_size if path.exists() else 0
        if seen >= size:
            return seen
        time.sleep(0.05)
    return seen


def test_a_small_flushed_write_reaches_the_capture_file_while_the_child_runs(tmp_path):
    script = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x' * 391 + b'\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('a warning\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(4)\n"
    )
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    child = ChildProcess(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
        logger=StructuredLogger(stream=io.StringIO()),
    )
    child.start()
    try:
        seen = _wait_for(stdout_path, len(LINE), seconds=2.5)
        still_running = child.poll() is None
        err_seen = _wait_for(stderr_path, len(b"a warning\n"), seconds=0.5)
    finally:
        child.terminate(1.0, reason="test over")
        child.finish()

    assert still_running, "the child exited before the check; the test measured nothing"
    assert seen == len(LINE), (
        f"{seen} of {len(LINE)} flushed bytes were on disk while the child ran: the "
        "capture is waiting for 64 KiB or for the pipe to close"
    )
    assert err_seen == len(b"a warning\n"), "stderr is captured by the same class"
    assert stdout_path.read_bytes() == LINE


def test_the_whole_stream_is_still_captured_and_capped(tmp_path):
    """The change to `read1` must not lose or duplicate a byte, and the cap holds."""
    script = (
        "import sys\n"
        "for i in range(2000):\n"
        "    sys.stdout.write(f'line {i:05d}\\n')\n"
        "    sys.stdout.flush()\n"
    )
    stdout_path = tmp_path / "stdout.log"
    child = ChildProcess(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=stdout_path,
        stderr_path=tmp_path / "stderr.log",
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024,
        logger=StructuredLogger(stream=io.StringIO()),
    )
    child.start()
    assert child.wait(20) is not None
    result = child.finish()

    expected = "".join(f"line {i:05d}\n" for i in range(2000)).encode()
    assert stdout_path.read_bytes() == expected
    assert result.stdout_bytes == len(expected)
    assert result.stdout_truncated is False

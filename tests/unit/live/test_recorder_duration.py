"""A recorded segment carries its own duration (BT-012, part of #2423).

On 2026-10-26 every `rec-<seq>.live.webm` the Attempts tab played reported a
duration of Infinity: the scrubber showed 0:00 with no total and could not
seek. ffmpeg's WebM muxer writes the segment's Duration and Cues by seeking
back after the last frame, and `FfmpegVp8Encoder` muxed to `pipe:1`, which
cannot seek. These tests hold the encoder to a seekable output.

THE STAND-IN FFMPEG behaves as the real one does on that one point: it writes a
duration only when its output can seek. There is no ffmpeg in the unit-test
image; the last case runs the real binary where one is installed.

MUTATION: put `pipe:1` back as the output (reading the segment from stdout) and
the first case fails, because the bytes carry no duration.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import sys
from pathlib import Path

import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live import recorder as rec  # noqa: E402

_FAKE = """\
import os, sys
frames = sys.stdin.buffer.read()
out = sys.argv[-1]
with open(os.path.join(os.path.dirname(__file__), "argv.txt"), "w") as log:
    log.write("\\n".join(sys.argv[1:]))
f = sys.stdout.buffer if out == "pipe:1" else open(out, "wb")
f.write(b"WEBM:" + str(len(frames)).encode())
if f.seekable():
    f.seek(0)
    f.write(b"DURN:")
f.close()
"""


def _fake_ffmpeg(tmp_path: Path) -> Path:
    script = tmp_path / "ffmpeg"
    script.write_text(f"#!{sys.executable}\n" + _FAKE)
    script.chmod(0o755)
    return script


def _encode(binary: str, frames: list[bytes], width: int, height: int, fps: int) -> bytes:
    async def go() -> bytes:
        stream = await rec.FfmpegVp8Encoder(binary).open(width, height, fps)
        for frame in frames:
            await stream.write(frame)
        return await stream.finish()

    return asyncio.run(go())


def test_the_segment_is_muxed_to_a_seekable_file_so_it_carries_its_duration(tmp_path, monkeypatch):
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(work))
    monkeypatch.setattr("tempfile.tempdir", None)
    fake = _fake_ffmpeg(tmp_path)
    data = _encode(str(fake), [b"\x00" * 12, b"\x01" * 12], 2, 2, 1)
    assert data == b"DURN:24", data
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert "pipe:1" not in argv
    assert argv[argv.index("-f", argv.index("pipe:0")) + 1] == "webm"
    # The private directory is gone once the segment is read.
    assert list(work.iterdir()) == []


def test_a_failed_encode_raises_and_still_removes_its_file(tmp_path, monkeypatch):
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(work))
    monkeypatch.setattr("tempfile.tempdir", None)
    failing = tmp_path / "ffmpeg"
    failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read()\nsys.exit(3)\n")
    failing.chmod(0o755)
    with pytest.raises(rec.RecordingError, match="exited 3"):
        _encode(str(failing), [b"\x00" * 12], 2, 2, 1)
    assert list(work.iterdir()) == []


def _ebml_duration(data: bytes) -> float | None:
    """The Segment Info Duration (ID 0x4489, a float), or None."""
    at = data.find(b"\x44\x89")
    if at < 0:
        return None
    size = data[at + 2] & 0x7F if data[at + 2] & 0x80 else None
    if size == 4:
        return struct.unpack(">f", data[at + 3:at + 7])[0]
    if size == 8:
        return struct.unpack(">d", data[at + 3:at + 11])[0]
    return None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg on this machine")
def test_the_real_ffmpeg_writes_a_finite_duration():
    frames = [bytes([i * 40]) * (16 * 16 * 3) for i in range(3)]
    data = _encode("ffmpeg", frames, 16, 16, 1)
    assert data[:4] == b"\x1a\x45\xdf\xa3"  # EBML magic
    duration = _ebml_duration(data)
    assert duration is not None and duration > 0

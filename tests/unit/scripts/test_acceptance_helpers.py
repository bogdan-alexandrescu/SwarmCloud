"""The acceptance suite's pure helpers: PNG blank detection and output parsers.

THE DEFECT THESE EXIST FOR. Every browser task this platform reported as a
success before 2026-09-29 screenshotted `about:blank`, and nothing noticed,
because nothing looked at the picture. scripts/acceptance/ now asserts OUTPUTS,
and every assertion rests on a parse: the pixels of a PNG, pytest's summary
line, the lines a patch removes, the order checkpoints were written in, the
code of a refusal. A parse that answers "fine" for input it did not understand
turns a failure into a PASS, which is the one thing an acceptance suite must
never do. So each helper is run here against input that must pass AND input
that must not, with no platform, no network and no credentials.

The PNGs are built in this file with zlib and struct -- every row filter the
format has, so the decoder's unfiltering is checked against a known image
rather than against itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PNGCHECK = ROOT / "scripts" / "acceptance" / "pngcheck.py"
PARSERS = ROOT / "scripts" / "acceptance" / "parsers.sh"

_spec = importlib.util.spec_from_file_location("acceptance_pngcheck", PNGCHECK)
assert _spec is not None and _spec.loader is not None
pngcheck = importlib.util.module_from_spec(_spec)
sys.modules["acceptance_pngcheck"] = pngcheck
_spec.loader.exec_module(pngcheck)

GREEN = (0x12, 0xA1, 0x50)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


# ---------------------------------------------------------------------------
# A small PNG writer, with every filter type
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _filter_row(kind: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray(len(row))
    for i, x in enumerate(row):
        left = row[i - bpp] if i >= bpp else 0
        up = prev[i]
        upper_left = prev[i - bpp] if i >= bpp else 0
        if kind == 0:
            pred = 0
        elif kind == 1:
            pred = left
        elif kind == 2:
            pred = up
        elif kind == 3:
            pred = (left + up) >> 1
        else:
            pred = _paeth(left, up, upper_left)
        out[i] = (x - pred) & 0xFF
    return bytes([kind]) + bytes(out)


def make_png(
    rows: list[bytes],
    width: int,
    *,
    colour_type: int = 2,
    filters: tuple[int, ...] = (0,),
    depth: int = 8,
    interlace: int = 0,
    extra_chunks: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]
    prev = bytes(width * bpp)
    raw = bytearray()
    for y, row in enumerate(rows):
        raw += _filter_row(filters[y % len(filters)], row, prev, bpp)
        prev = row
    header = struct.pack(">IIBBBBB", width, len(rows), depth, colour_type, 0, 0, interlace)
    body = pngcheck.PNG_SIGNATURE + _chunk(b"IHDR", header)
    for kind, data in extra_chunks:
        body += _chunk(kind, data)
    return body + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b"")


def rgb_rows(width: int, height: int, paint) -> list[bytes]:
    return [b"".join(bytes(paint(x, y)) for x in range(width)) for y in range(height)]


def fixture_like(x: int, y: int) -> tuple[int, int, int]:
    """White ground, a green block, and a few black 'text' strokes -- the
    shape of the acceptance fixture page."""
    if 10 <= y < 30 and 5 <= x < 55:
        return GREEN
    if y in (3, 4) and x % 3 == 0:
        return BLACK
    return WHITE


# ---------------------------------------------------------------------------
# PNG decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filters", [(0,), (1,), (2,), (3,), (4,), (0, 1, 2, 3, 4)])
def test_every_row_filter_round_trips_to_the_pixels_written(filters):
    width, height = 13, 11
    rows = rgb_rows(width, height, lambda x, y: ((x * 19) % 256, (y * 23) % 256, (x * y) % 256))
    image = pngcheck.decode(make_png(rows, width, filters=filters))
    assert (image.width, image.height) == (width, height)
    expected = b"".join(bytes((*row[i : i + 3], 255)) for row in rows for i in range(0, len(row), 3))
    assert image.rgba == expected


def test_grey_palette_and_alpha_images_decode_to_rgba():
    grey = pngcheck.decode(make_png([bytes([0, 128, 255])], 3, colour_type=0))
    assert grey.rgba == bytes([0, 0, 0, 255, 128, 128, 128, 255, 255, 255, 255, 255])

    palette = bytes([*GREEN, *WHITE])
    indexed = pngcheck.decode(
        make_png([bytes([0, 1, 0])], 3, colour_type=3, extra_chunks=((b"PLTE", palette),))
    )
    assert indexed.rgba == bytes([*GREEN, 255, *WHITE, 255, *GREEN, 255])

    grey_alpha = pngcheck.decode(make_png([bytes([10, 20, 30, 40])], 2, colour_type=4))
    assert grey_alpha.rgba == bytes([10, 10, 10, 20, 30, 30, 30, 40])


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"GIF89a....", "signature"),
        (make_png([bytes(6)], 1, depth=16), "bit depth"),
        (make_png([bytes(3)], 1, interlace=1), "interlaced"),
        (make_png([bytes(1)], 1, colour_type=3), "PLTE"),
    ],
)
def test_what_the_decoder_cannot_read_is_refused_not_judged(data, reason):
    with pytest.raises(pngcheck.PngError, match=reason):
        pngcheck.decode(data)


def test_truncated_image_data_is_refused():
    good = make_png(rgb_rows(4, 4, fixture_like), 4)
    idat_at = good.index(b"IDAT")
    (length,) = struct.unpack(">I", good[idat_at - 4 : idat_at])
    short = zlib.compress(zlib.decompress(good[idat_at + 4 : idat_at + 4 + length])[:10])
    broken = good[: idat_at - 4] + _chunk(b"IDAT", short) + _chunk(b"IEND", b"")
    with pytest.raises(pngcheck.PngError, match="expected"):
        pngcheck.decode(broken)


# ---------------------------------------------------------------------------
# Blank detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fill", [WHITE, BLACK, GREEN], ids=["white", "black", "flat-colour"])
def test_a_solid_fill_is_blank_whatever_the_colour(fill):
    image = pngcheck.decode(make_png(rgb_rows(40, 30, lambda x, y: fill), 40))
    stats = pngcheck.analyse(image)
    assert pngcheck.is_blank(stats)
    ok, reason = pngcheck.verdict(stats)
    assert not ok and reason.startswith("blank")


def test_a_fully_transparent_screenshot_is_blank():
    """about:blank captured with an alpha channel: every pixel transparent is
    the page background, white, and nothing else."""
    rows = [bytes([0, 0, 0, 0] * 20) for _ in range(20)]
    stats = pngcheck.analyse(pngcheck.decode(make_png(rows, 20, colour_type=6)))
    assert pngcheck.is_blank(stats)


def test_a_page_with_content_is_not_blank_and_its_block_colour_is_measured():
    width, height = 60, 40
    image = pngcheck.decode(make_png(rgb_rows(width, height, fixture_like), width, filters=(4, 1)))
    stats = pngcheck.analyse(image, colour=GREEN)
    assert not pngcheck.is_blank(stats)
    assert stats["colour_fraction"] == pytest.approx((20 * 50) / (width * height), abs=1e-6)
    ok, _ = pngcheck.verdict(stats, min_colour_fraction=0.1)
    assert ok


def test_content_without_the_fixture_colour_fails_the_colour_check():
    """A screenshot of some OTHER page -- an error page, a login wall -- has
    content but not the fixture's block."""
    image = pngcheck.decode(make_png(rgb_rows(30, 30, lambda x, y: BLACK if (x + y) % 4 == 0 else WHITE), 30))
    stats = pngcheck.analyse(image, colour=GREEN)
    assert not pngcheck.is_blank(stats)
    ok, reason = pngcheck.verdict(stats, min_colour_fraction=0.01)
    assert not ok and "covers" in reason


def test_a_near_miss_of_the_colour_counts_within_tolerance_only():
    near = (GREEN[0] + 5, GREEN[1] - 5, GREEN[2] + 5)
    far = (GREEN[0] + 20, GREEN[1], GREEN[2])
    image = pngcheck.decode(make_png([bytes([*near, *far, *WHITE, *WHITE])], 4))
    stats = pngcheck.analyse(image, colour=GREEN, tolerance=8)
    assert stats["colour_fraction"] == pytest.approx(0.25)


def test_parse_colour_accepts_a_hash_and_refuses_a_short_value():
    assert pngcheck.parse_colour("#12a150") == GREEN
    with pytest.raises(ValueError):
        pngcheck.parse_colour("fff")


def _cli(tmp_path, data: bytes, *args: str) -> tuple[int, dict]:
    path = tmp_path / "shot.png"
    path.write_bytes(data)
    result = subprocess.run(
        [sys.executable, str(PNGCHECK), str(path), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_the_command_line_exit_codes_are_the_verdict(tmp_path):
    content = make_png(rgb_rows(60, 40, fixture_like), 60)
    blank = make_png(rgb_rows(60, 40, lambda x, y: WHITE), 60)

    code, out = _cli(tmp_path, content, "--colour", "12a150")
    assert code == 0 and out["ok"] is True, out
    code, out = _cli(tmp_path, blank, "--colour", "12a150")
    assert code == 1 and out["ok"] is False and out["reason"].startswith("blank"), out
    code, out = _cli(tmp_path, b"not a png")
    assert code == 2 and out["reason"].startswith("unreadable"), out


# ---------------------------------------------------------------------------
# Output parsers (bash, run the way the suite sources them)
# ---------------------------------------------------------------------------


def _bash(function: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    return subprocess.run(
        ["bash", "-c", f'source "{PARSERS}"; {function} "$@"', "parsers", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("..\n2 passed in 0.01s\n", "passed=2 failed=0 errors=0"),
        ("F.\n1 failed, 3 passed in 0.20s\n", "passed=3 failed=1 errors=0"),
        ("===== 1 error in 0.10s =====\n", "passed=0 failed=0 errors=1"),
        # A test NAME carrying the word must not be read as the summary.
        ("test_it_passed_ok PASSED\n5 passed, 2 skipped in 1.5s\n", "passed=5 failed=0 errors=0"),
    ],
)
def test_pytest_summary_reads_the_summary_line(text, expected):
    result = _bash("acc_pytest_summary", stdin=text)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_pytest_output_with_no_summary_is_not_zero_failures():
    result = _bash("acc_pytest_summary", stdin="ImportError: no module named x\n")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


PATCH = """\
diff --git a/tests/acceptance/fixtures/claude-code/calc.py b/tests/acceptance/fixtures/claude-code/calc.py
index 1111111..2222222 100644
--- a/tests/acceptance/fixtures/claude-code/calc.py
+++ b/tests/acceptance/fixtures/claude-code/calc.py
@@ -1,4 +1,4 @@
 def add(a, b):
-    return a - b
+    return a + b
diff --git a/other.py b/other.py
--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-    return a - c
+    return a + c
"""
CALC = "tests/acceptance/fixtures/claude-code/calc.py"


def test_diff_replaces_the_bug_line_in_the_named_file():
    assert _bash("acc_diff_replaces", CALC, "    return a - b", stdin=PATCH).returncode == 0


@pytest.mark.parametrize(
    "path, line, patch",
    [
        ("other.py", "    return a - b", PATCH),  # the line is in a different file
        (CALC, "    return a - c", PATCH),  # that line is removed elsewhere, not here
        (CALC, "    return a - b", PATCH.replace("+    return a + b\n", "")),  # removed, nothing added
        (CALC, "    return a - b", ""),  # an empty patch touches nothing
    ],
    ids=["wrong-file", "line-from-another-file", "deleted-only", "empty"],
)
def test_diff_replaces_is_false_for_anything_else(path, line, patch):
    assert _bash("acc_diff_replaces", path, line, stdin=patch).returncode != 0


def test_diff_files_lists_every_changed_path():
    result = _bash("acc_diff_files", stdin=PATCH)
    assert result.stdout.split() == [CALC, "other.py"]


def _event(at: str, seq: int, kind: str = "checkpoint_completed") -> str:
    return json.dumps({"type": kind, "at": at, "detail": {"checkpoint_id": f"ckpt-{seq:05d}", "seq": seq}})


def test_checkpoint_ids_come_out_in_the_order_they_were_written():
    events = "\n".join(
        [
            _event("2026-09-29T10:02:00.5Z", 3),
            _event("2026-09-29T10:00:00.1Z", 1),
            json.dumps({"type": "heartbeat", "at": "2026-09-29T10:00:30Z", "detail": {}}),
            _event("2026-09-29T10:01:00.9Z", 2),
        ]
    )
    result = _bash("acc_checkpoint_ids", stdin=events + "\n")
    assert result.stdout.split() == ["ckpt-00001", "ckpt-00002", "ckpt-00003"]
    assert _bash("acc_strictly_increasing", stdin=result.stdout).returncode == 0


@pytest.mark.parametrize(
    "ids",
    ["ckpt-00001\n", "ckpt-00002\nckpt-00001\n", "ckpt-00001\nckpt-00001\n", "", "ckpt-x\nckpt-y\n"],
    ids=["one-is-not-a-sequence", "decreasing", "repeated", "none", "no-number"],
)
def test_strictly_increasing_refuses_everything_that_is_not(ids):
    assert _bash("acc_strictly_increasing", stdin=ids).returncode != 0


def test_iso_epoch_reads_firestore_fractions_and_refuses_junk():
    result = _bash("acc_iso_epoch", "2026-09-29T12:00:00.123456Z")
    assert result.returncode == 0 and result.stdout.strip() == "1790683200"
    assert _bash("acc_iso_epoch", "yesterday").returncode != 0


def test_error_code_reads_the_envelope():
    assert _bash("acc_error_code", stdin='{"code":"invalid_input","message":"x"}').stdout.strip() == "invalid_input"
    assert _bash("acc_error_code", stdin="<html>502</html>").stdout.strip() == ""


@pytest.mark.parametrize(
    "title, ok",
    [
        ("add() in the calc fixture adds instead of subtracting", True),
        ("[swarm] task_d18d8d8b044d469cb43c", False),
        ("Fix task_d18d8d8b044d469cb43c", False),
        ("two\nlines", False),
        ("   ", False),
    ],
)
def test_title_is_fact(title, ok):
    result = _bash("acc_title_is_fact", "task_d18d8d8b044d469cb43c", stdin=title)
    assert (result.returncode == 0) is ok


def test_merged_branches_are_read_from_an_integration_body():
    body = "Integrates 2 contributor branch(es):\n- merged: `swarm/task_aaa1`\n- merged: `swarm/task_bbb2`\n- conflicted: `swarm/task_ccc3`\n"
    assert _bash("acc_merged_branches", stdin=body).stdout.split() == ["swarm/task_aaa1", "swarm/task_bbb2"]

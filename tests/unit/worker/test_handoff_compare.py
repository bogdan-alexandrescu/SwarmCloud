"""IMPLEMENT-RENDER (#2442, BGD 2026-10-26): `swarm-handoff compare SHOT FRAME`.

A UI lane's implement step renders its own branch in the live browser and
compares each render with the repository's frame PNG at the same preset and
theme. The command scores the two -- 0 for the same picture, more for more
of the page that differs noticeably -- and writes a diff image and a report
under $SWARM_ARTIFACTS_DIR/compare/, so the step's artifacts carry the
evidence for every frame it touched.

No cloud, no browser, no Pillow: the unit environment carries no Pillow, so
these run the standard-library decoder, and the scoring after decoding is
the same code on either path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMAND = REPO / "images" / "agent-runtime-browser" / "swarm-handoff"
GUIDE_DOC = REPO / "docs" / "browser-qa.md"


def _command():
    loader = importlib.machinery.SourceFileLoader("swarm_handoff_compare", str(COMMAND))
    spec = importlib.util.spec_from_loader("swarm_handoff_compare", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _png(width: int, height: int, pixel, *, alpha: bool = False) -> bytes:
    channels = 4 if alpha else 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixel(x, y)[:channels])

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


WHITE, BLACK, BLUE, MAGENTA = (255, 255, 255), (0, 0, 0), (40, 90, 220), (255, 0, 255)


def _page(x: int, y: int, *, scale: int = 1, button=BLUE):
    """A small 'page': white, a dark header band and a coloured button."""
    x, y = x // scale, y // scale
    if y < 12:
        return (30, 30, 30, 255)
    if 40 <= x < 120 and 40 <= y < 64:
        return button + (255,)
    return WHITE + (255,)


def _speck(x: int, y: int):
    """The page with a 2 x 2 black speck. One black pixel averages, over its
    4 x 4 cell, to a delta E of about 5.7: under COMPARE_DELTA_E on purpose,
    because that is the size of a glyph edge drawn differently. Two by two is
    a defect a person sees."""
    return BLACK + (255,) if 150 <= x < 152 and 108 <= y < 110 else _page(x, y)


def _run(qa, tmp_path, monkeypatch, shot: bytes, frame: bytes, *extra: str, capsys):
    (tmp_path / "shot.png").write_bytes(shot)
    (tmp_path / "frame.png").write_bytes(frame)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    monkeypatch.setenv("SWARM_ARTIFACTS_DIR", str(artifacts))
    code = qa.main(["compare", str(tmp_path / "shot.png"), str(tmp_path / "frame.png"), *extra])
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1]), artifacts


def _rgb_at(qa, path: Path, x: int, y: int) -> tuple[int, int, int]:
    width, _height, channels, pixels = qa.decode_png(path.read_bytes())
    at = (y * width + x) * channels
    return tuple(pixels[at:at + 3])


def test_identical_images_score_zero_and_still_write_the_diff_image_and_report(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    code, report, artifacts = _run(qa, tmp_path, monkeypatch, shot, shot, "--name", "home--desktop--light", capsys=capsys)

    assert code == qa.EXIT_RETURNED
    assert report["outcome"] == "compared"
    assert report["score"] == 0
    assert report["within_threshold"] is True
    assert report["differing_cells"] == 0 and report["cells"] > 0
    diff = artifacts / "compare" / "home--desktop--light.diff.png"
    assert report["diff"] == str(diff) and diff.is_file()
    written = json.loads((artifacts / "compare" / "home--desktop--light.json").read_text())
    assert written["score"] == 0 and written["frame"].endswith("frame.png")
    # The diff image is the shot, faded, at the shot's size, with nothing marked.
    width, height, _channels, _pixels = qa.decode_png(diff.read_bytes())
    assert (width, height) == (160, 120)
    assert _rgb_at(qa, diff, 100, 50) != MAGENTA


def test_a_different_image_scores_over_the_threshold_and_the_diff_marks_where(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    # The frame's button is another colour: what a wrong token looks like.
    frame = _png(160, 120, lambda x, y: _page(x, y, button=(220, 60, 40)))
    code, report, artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_DIFFERENT
    assert report["outcome"] == "compared"
    assert report["score"] > report["threshold"] == qa.COMPARE_THRESHOLD
    assert report["within_threshold"] is False
    # The button is 80 x 24 of 160 x 120: a tenth of the page.
    assert report["score"] == pytest.approx(80 * 24 / (160 * 120), abs=0.01)
    diff = Path(report["diff"])
    assert diff.is_file() and diff.parent == artifacts / "compare"
    # Default name: the shot's file name.
    assert diff.name == "shot.diff.png"
    assert _rgb_at(qa, diff, 80, 50) == MAGENTA, "inside the button"
    assert _rgb_at(qa, diff, 10, 100) != MAGENTA, "outside it"
    assert _rgb_at(qa, diff, 140, 5) != MAGENTA, "the header is the same"


def test_a_frame_exported_at_twice_the_size_is_compared_at_the_shots_scale(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(320, 240, lambda x, y: _page(x, y, scale=2))
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_RETURNED, report
    assert report["score"] == 0
    assert report["frame_scale"] == 2.0
    assert report["size_mismatch"] is False


def test_a_phone_width_that_is_not_a_multiple_of_the_cell_still_matches_its_2x_frame(tmp_path, monkeypatch, capsys):
    """390 and 380 are not multiples of COMPARE_CELL. MEASURED on the first
    draft, which cut each image into the same NUMBER of columns on its own:
    a 390 x 844 render against its own 2x export scored 0.34, because the
    frame's edges fell half a CSS pixel off the shot's. That cut scores these
    stripes 0.13 (2026-10-26), well over the threshold."""
    qa = _command()

    def stripes(x: int, y: int, scale: int = 1):
        x, y = x // scale, y // scale
        return (255, 255, 255, 255) if (x // 3 + y // 5) % 2 else (20, 20, 20, 255)

    shot = _png(390, 60, stripes)
    frame = _png(780, 120, lambda x, y: stripes(x, y, scale=2))
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_RETURNED, report
    assert report["score"] == 0


def test_a_frame_taller_than_the_render_counts_the_rows_only_it_has(tmp_path, monkeypatch, capsys):
    """A full-page frame against a viewport render: the part the render does
    not show is not a match, and the report says the heights differ."""
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 240, _page)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_DIFFERENT
    assert report["size_mismatch"] is True
    assert report["score"] == pytest.approx(0.5, abs=0.02)
    assert "full-page" in report["next"]
    diff = Path(report["diff"])
    width, height, _channels, _pixels = qa.decode_png(diff.read_bytes())
    assert (width, height) == (160, 240)
    assert _rgb_at(qa, diff, 50, 200) == MAGENTA


def test_top_only_compares_a_viewport_render_with_the_top_of_a_full_page_frame(tmp_path, monkeypatch, capsys):
    """On a phone preset a lane takes the viewport, not a full-page shot
    (swarm_qa.py `_shot`: through the gate the first full-document shot at
    phone-390 came out 980x2121), so --top-only scores the rows both have
    and says how many of the frame's it left out."""
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 240, _page)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, "--top-only", capsys=capsys)

    assert code == qa.EXIT_RETURNED, report
    assert report["score"] == 0
    assert report["size_mismatch"] is True and report["top_only"] is True
    assert report["rows_left_out"] == 30
    width, height, _channels, _pixels = qa.decode_png(Path(report["diff"]).read_bytes())
    assert (width, height) == (160, 120)


def test_a_speck_is_scored_but_stays_within_the_threshold(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 120, _speck)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_RETURNED
    assert 0 < report["score"] <= report["threshold"]


def test_a_transparent_frame_is_seen_over_white(tmp_path, monkeypatch, capsys):
    """A design tool exports an empty canvas as transparent; the browser paints white."""
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 120, lambda x, y: (0, 0, 0, 0) if _page(x, y)[:3] == WHITE else _page(x, y), alpha=True)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_RETURNED, report
    assert report["score"] == 0


def test_the_threshold_is_the_callers_to_tighten(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 120, _speck)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, "--threshold", "0", capsys=capsys)

    assert code == qa.EXIT_DIFFERENT
    assert report["threshold"] == 0


@pytest.mark.parametrize("extra", [("--name", "../escape"), ("--name", "Has Spaces"), ("--threshold", "1.5")])
def test_a_bad_name_or_threshold_is_refused_and_writes_nothing(tmp_path, monkeypatch, capsys, extra):
    qa = _command()
    shot = _png(16, 16, _page)
    code, report, artifacts = _run(qa, tmp_path, monkeypatch, shot, shot, *extra, capsys=capsys)

    assert code == qa.EXIT_REFUSED
    assert report["outcome"] == "refused"
    assert not (artifacts / "compare").exists()


def test_an_unreadable_image_is_refused_by_name(tmp_path, monkeypatch, capsys):
    qa = _command()
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, _png(16, 16, _page), b"not a png", capsys=capsys)

    assert code == qa.EXIT_REFUSED
    assert report["code"] == "unreadable_image"
    assert "frame.png" in report["message"]


def test_without_an_artifacts_directory_it_is_refused(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png(16, 16, _page))
    monkeypatch.delenv("SWARM_ARTIFACTS_DIR", raising=False)
    code = qa.main(["compare", str(shot), str(shot)])
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == qa.EXIT_REFUSED
    assert report["code"] == "no_artifacts_dir"


def test_compare_needs_no_live_browser(tmp_path, monkeypatch, capsys):
    """It reads two files: no hand-off spool, so a step can compare after the
    browser is gone, and a plain claude-code step can run it too."""
    qa = _command()
    monkeypatch.delenv("SWARM_HANDOFF", raising=False)
    shot = _png(16, 16, _page)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, shot, capsys=capsys)

    assert code == qa.EXIT_RETURNED and report["outcome"] == "compared"


def test_the_lane_authors_section_names_the_profile_the_presets_and_the_command():
    text = GUIDE_DOC.read_text(encoding="utf-8")
    start = text.index("## 6. Rendering your own branch")
    section = text[start:]
    for needed in ("claude-code-browser", "swarm-handoff compare", "swarm-handoff device desktop",
                   "swarm-handoff device phone-390", "phone-380", "swarm-handoff theme dark",
                   "swarm-handoff theme light", "127.0.0.1", "no-auth", "Keycloak"):
        assert needed in section, needed

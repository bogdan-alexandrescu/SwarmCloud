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


# -- the size cap (IMPLEMENT-RENDER-FINISH) ---------------------------------------
#
# The first continuation of this lane timed out at 7,200 s with four cores busy
# for most of two hours. A pure-Python decode and grid over a full-size capture
# is the one loop in this command whose cost grows with its input, so the
# command never decodes more than COMPARE_MAX_PIXELS with the standard library:
# it reads the size from the header and refuses before it decompresses.


def _header_only_png(width: int, height: int) -> bytes:
    """A PNG whose header claims `width` x `height` and whose data is one empty
    row: decoding it would fail, so a refusal by size proves the size was read
    first. Built in microseconds, whatever size it claims."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")


@pytest.mark.parametrize("which", ["shot", "frame"])
def test_an_image_over_the_size_cap_is_refused_before_it_is_decoded(tmp_path, monkeypatch, capsys, which):
    qa = _command()
    small = _png(16, 16, _page)
    # 4000 x 4000 is 16 M pixels: over the cap, and what a 2x full-page frame comes to.
    huge = _header_only_png(4000, 4000)
    shot, frame = (huge, small) if which == "shot" else (small, huge)
    code, report, artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)

    assert code == qa.EXIT_REFUSED
    assert report["code"] == "too_large", report
    assert "%s.png" % which in report["message"]
    assert "4000" in report["message"]
    assert not (artifacts / "compare").exists()


def test_the_cap_admits_a_2x_desktop_frame():
    """The headline case, a 1440 x 900 desktop shot against its 2x export, is
    never refused: the cap is over 2880 x 1800."""
    qa = _command()
    assert 2880 * 1800 <= qa.COMPARE_MAX_PIXELS < 4000 * 4000


@pytest.mark.parametrize(
    ("size", "factor"),
    [((1440, 900), 1), ((2880, 1800), 1), ((2880, 6000), 2), ((2880, 12000), 3), ((390 * 3, 844 * 3), 1)],
)
def test_a_decoder_that_can_shrink_shrinks_by_the_smallest_whole_factor_under_the_cap(size, factor):
    """With Pillow (the image's venv), an image over the cap is box-averaged by
    a whole factor in C instead of refused: the grid compares CSS-pixel cells,
    so a frame at 3x scores as one at 1x."""
    qa = _command()
    width, height = size
    assert qa.reduction(width, height) == factor
    assert (width // factor) * (height // factor) <= qa.COMPARE_MAX_PIXELS


def test_an_image_past_any_decoder_is_refused_even_with_pillow():
    qa = _command()
    with pytest.raises(qa.TooLarge):
        qa.reduction(16384, 16384)


def test_a_half_transparent_pixel_is_composited_over_white():
    qa = _command()
    pixels = bytes((0, 0, 0, 128, 255, 0, 0, 255, 10, 20, 30, 0))
    assert qa._opaque_rgb(3, 1, 4, pixels) == bytes((127, 127, 127, 255, 0, 0, 255, 255, 255))


# -- where the change is (TOKEN-RENDER, BGD 2026-10-27, part of #2524) ------------
#
# A diff image Read into a lane stays in its context for the rest of the lane.
# So compare says WHERE the frames differ, as boxes in the shot's CSS pixels,
# and writes a small crop of that region beside the diff image: the one image
# a parent reads, and only for a frame over the threshold.


def test_a_different_image_prints_the_changed_boxes_and_writes_a_crop(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 120, lambda x, y: _page(x, y, button=(220, 60, 40)))
    code, report, artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, "--name", "home--desktop--dark",
                                   capsys=capsys)

    assert code == qa.EXIT_DIFFERENT
    # The button is x 40-120, y 40-64: one box, exactly the differing cells.
    assert report["boxes"] == [{"x": 40, "y": 40, "width": 80, "height": 24, "cells": 20 * 6}]
    assert report["boxes_total"] == 1
    crop = artifacts / "compare" / "home--desktop--dark.crop.png"
    assert report["crop"] == str(crop) and crop.is_file()
    assert crop.parent == Path(report["diff"]).parent
    width, height, _channels, _pixels = qa.decode_png(crop.read_bytes())
    # The box with its margin, never the whole page.
    assert width < 160 and height < 120
    assert max(width, height) <= qa.COMPARE_CROP_MAX_SIDE
    assert report["crop_box"]["x"] <= 40 and report["crop_box"]["y"] <= 40
    assert _rgb_at(qa, crop, width // 2, height // 2) == MAGENTA, "the crop shows the marked cells"
    written = json.loads((artifacts / "compare" / "home--desktop--dark.json").read_text())
    assert written["boxes"] == report["boxes"] and written["crop"] == str(crop)
    # The next step reads the crop, never the whole diff image.
    assert "crop" in report["next"] and "Open the diff image" not in report["next"]


def test_identical_images_have_no_boxes_and_no_crop(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    code, report, artifacts = _run(qa, tmp_path, monkeypatch, shot, shot, "--name", "same", capsys=capsys)

    assert code == qa.EXIT_RETURNED
    assert report["boxes"] == [] and report["boxes_total"] == 0
    assert report["crop"] is None and report["crop_box"] is None
    assert not (artifacts / "compare" / "same.crop.png").exists()


def test_a_crop_left_by_an_earlier_compare_of_the_same_name_is_removed_once_it_matches(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 120, lambda x, y: _page(x, y, button=(220, 60, 40)))
    _run(qa, tmp_path, monkeypatch, shot, frame, "--name", "again", capsys=capsys)
    crop = tmp_path / "artifacts" / "compare" / "again.crop.png"
    assert crop.is_file()
    _run(qa, tmp_path, monkeypatch, shot, shot, "--name", "again", capsys=capsys)
    assert not crop.exists()


def test_two_separate_changes_are_two_boxes_largest_first(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)

    def frame_pixel(x, y):
        if 120 <= x < 128 and 100 <= y < 108:
            return (220, 60, 40, 255)
        return _page(x, y, button=(220, 60, 40))

    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, _png(160, 120, frame_pixel), capsys=capsys)
    assert code == qa.EXIT_DIFFERENT
    assert [(b["x"], b["y"], b["width"], b["height"]) for b in report["boxes"]] == [
        (40, 40, 80, 24), (120, 100, 8, 8)]
    assert report["boxes_total"] == 2


def test_a_large_changed_region_is_downscaled_in_the_crop(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(1200, 40, lambda x, y: WHITE)
    frame = _png(1200, 40, lambda x, y: BLUE)
    code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, "--name", "wide", capsys=capsys)

    assert code == qa.EXIT_DIFFERENT
    assert report["boxes"][0]["width"] == 1200
    assert report["crop_scale"] > 1
    width, height, _channels, _pixels = qa.decode_png(Path(report["crop"]).read_bytes())
    assert width <= qa.COMPARE_CROP_MAX_SIDE and height <= qa.COMPARE_CROP_MAX_SIDE


def test_rows_only_the_frame_has_are_boxed_below_the_render(tmp_path, monkeypatch, capsys):
    qa = _command()
    shot = _png(160, 120, _page)
    frame = _png(160, 160, _page)
    _code, report, _artifacts = _run(qa, tmp_path, monkeypatch, shot, frame, capsys=capsys)
    (box,) = report["boxes"]
    assert (box["x"], box["y"], box["width"], box["height"]) == (0, 120, 160, 40)
    assert Path(report["crop"]).is_file()


def test_the_guide_reads_only_the_crop_of_a_frame_over_the_threshold():
    text = GUIDE_DOC.read_text(encoding="utf-8")
    start = text.index("### 6.5 ")
    end = text.find("\n## ", start)
    section = text[start:end if end != -1 else None]
    assert "Open each diff image with `Read`" not in section
    assert "Read only the crop of a frame over threshold" in section
    assert "Sonnet child per screen" in section and "screen-review.md" in section
    assert "model_class" in section and "sonnet" in section
    # review.md names every frame and the child that read it.
    assert "review.md" in section and "every frame" in section

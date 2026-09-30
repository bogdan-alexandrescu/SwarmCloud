#!/usr/bin/env python3
"""Is this PNG a picture of something, or a blank page?

WHY THIS EXISTS. Every browser task this platform reported as successful
before 2026-09-29 screenshotted `about:blank`: the runner took the picture, the
artifact uploaded, the task SUCCEEDED, and nothing ever looked at what the
picture showed. A white rectangle is a valid PNG. So the acceptance suite
decodes the screenshot and asserts two things about its PIXELS:

  * it is not blank -- more than one colour, and a luminance spread above a
    floor that a solid fill (white, black, or any flat colour) cannot reach;
  * optionally, a known colour is present over a minimum fraction of it --
    the fixture page paints a block of one exact colour, so a screenshot of
    any OTHER page, an error page included, does not satisfy it.

STANDARD LIBRARY ONLY. It runs in the swarm-verify image and in CI's unit job,
neither of which may be assumed to carry Pillow. `zlib` inflates the IDAT
stream and the five PNG row filters are undone here. That is slower than a C
decoder (a full-page 1280-wide screenshot takes seconds, not milliseconds),
which is irrelevant next to the minutes a browser task takes to run.

Supported: 8-bit depth, colour types 0 (grey), 2 (RGB), 3 (palette), 4
(grey + alpha) and 6 (RGBA), not interlaced -- which is every PNG Chromium's
screenshot writes. Anything else is refused as unreadable (exit 2), never
judged: a decoder that guessed would turn "could not read it" into a verdict.

Exit codes, for the shell that calls it:
    0  the image shows something (and the requested colour, when asked)
    1  the image is blank, or the requested colour is absent
    2  the file is not a PNG this decoder reads

It prints one JSON object either way, so the reason is in the output.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from dataclasses import dataclass

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Channels per pixel, by PNG colour type.
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

#: Past this many pixels, statistics are taken over an even sample. Every row
#: is still unfiltered (a filter depends on the row above), but the per-pixel
#: arithmetic -- luminance, the colour match -- runs on at most this many.
MAX_SAMPLED_PIXELS = 2_000_000

#: Distinct colours are counted exactly up to this many, then reported as
#: "at least". A blank page has one; the fixture has thousands (antialiased
#: text); only the difference between one and "more than a handful" matters.
DISTINCT_CAP = 4096

#: The default floor on luminance standard deviation. A flat fill is 0. The
#: fixture page -- black text, a saturated block, white ground -- measures
#: well above 20. A page with a single line of small text on white sits near 5,
#: which is still "not blank"; 2.0 is below that and above JPEG-style noise.
DEFAULT_MIN_STDDEV = 2.0


class PngError(ValueError):
    """The bytes are not a PNG this decoder reads."""


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    #: Always RGBA after decoding, 4 bytes per pixel, row-major.
    rgba: bytes


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    """Undo the per-row filters. `bpp` is bytes per pixel (>= 1 at 8-bit)."""
    stride = width * bpp
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise PngError(f"image data is {len(raw)} bytes, expected {expected}")
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        kind = raw[pos]
        row = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += stride + 1
        if kind == 0:
            pass
        elif kind == 1:  # Sub
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif kind == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif kind == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                upper_left = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(left, prev[i], upper_left)) & 0xFF
        else:
            raise PngError(f"row {y} has unknown filter type {kind}")
        out[y * stride : (y + 1) * stride] = row
        prev = row
    return out


def decode(data: bytes) -> Image:
    """Decode an 8-bit, non-interlaced PNG into RGBA."""
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("not a PNG (bad signature)")
    pos = len(PNG_SIGNATURE)
    header: tuple[int, ...] | None = None
    palette: bytes | None = None
    transparency: bytes | None = None
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) != length:
            raise PngError(f"chunk {kind!r} is truncated")
        pos += 12 + length  # length, type, body, crc
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise PngError("no IHDR chunk")
    width, height, depth, colour, _compression, _filter, interlace = header
    if width == 0 or height == 0:
        raise PngError("the image has no pixels")
    if depth != 8:
        raise PngError(f"bit depth {depth} is not supported (8 only)")
    if colour not in _CHANNELS:
        raise PngError(f"colour type {colour} is not a PNG colour type")
    if interlace != 0:
        raise PngError("interlaced PNGs are not supported")
    if colour == 3 and palette is None:
        raise PngError("a palette image carries no PLTE chunk")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngError(f"image data does not inflate: {exc}") from exc

    channels = _CHANNELS[colour]
    pixels = _unfilter(raw, width, height, channels)
    if colour == 6:
        return Image(width, height, bytes(pixels))

    rgba = bytearray(width * height * 4)
    n = width * height
    if colour == 2:
        for i in range(n):
            rgba[i * 4 : i * 4 + 3] = pixels[i * 3 : i * 3 + 3]
            rgba[i * 4 + 3] = 255
    elif colour == 0:
        for i in range(n):
            g = pixels[i]
            rgba[i * 4 : i * 4 + 4] = bytes((g, g, g, 255))
    elif colour == 4:
        for i in range(n):
            g = pixels[i * 2]
            rgba[i * 4 : i * 4 + 4] = bytes((g, g, g, pixels[i * 2 + 1]))
    else:  # colour == 3
        assert palette is not None
        entries = len(palette) // 3
        for i in range(n):
            index = pixels[i]
            if index >= entries:
                raise PngError(f"pixel {i} names palette entry {index} of {entries}")
            alpha = transparency[index] if transparency and index < len(transparency) else 255
            rgba[i * 4 : i * 4 + 4] = palette[index * 3 : index * 3 + 3] + bytes((alpha,))
    return Image(width, height, bytes(rgba))


def parse_colour(text: str) -> tuple[int, int, int]:
    """`12a150` or `#12a150` -> (18, 161, 80)."""
    value = text.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"colour {text!r} is not RRGGBB")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def analyse(
    image: Image,
    *,
    colour: tuple[int, int, int] | None = None,
    tolerance: int = 8,
) -> dict:
    """Statistics over the image, composited onto white (a transparent pixel of
    a screenshot is page background, which is white)."""
    total = image.width * image.height
    step = max(1, math.ceil(total / MAX_SAMPLED_PIXELS))
    data = image.rgba
    distinct: set[int] = set()
    distinct_capped = False
    count = 0
    lum_sum = 0.0
    lum_sq = 0.0
    matches = 0
    for i in range(0, total, step):
        r, g, b, a = data[i * 4], data[i * 4 + 1], data[i * 4 + 2], data[i * 4 + 3]
        if a != 255:
            r = (r * a + 255 * (255 - a)) // 255
            g = (g * a + 255 * (255 - a)) // 255
            b = (b * a + 255 * (255 - a)) // 255
        if not distinct_capped:
            distinct.add((r << 16) | (g << 8) | b)
            if len(distinct) >= DISTINCT_CAP:
                distinct_capped = True
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        lum_sum += lum
        lum_sq += lum * lum
        if colour is not None and (
            abs(r - colour[0]) <= tolerance
            and abs(g - colour[1]) <= tolerance
            and abs(b - colour[2]) <= tolerance
        ):
            matches += 1
        count += 1
    mean = lum_sum / count
    variance = max(0.0, lum_sq / count - mean * mean)
    stats = {
        "width": image.width,
        "height": image.height,
        "sampled_pixels": count,
        "distinct_colours": len(distinct),
        "distinct_capped": distinct_capped,
        "luminance_mean": round(mean, 3),
        "luminance_stddev": round(math.sqrt(variance), 3),
    }
    if colour is not None:
        stats["colour"] = "%02x%02x%02x" % colour
        stats["colour_fraction"] = round(matches / count, 6)
    return stats


def is_blank(stats: dict, *, min_stddev: float = DEFAULT_MIN_STDDEV) -> bool:
    """Blank: one colour, or a luminance spread no page with content has."""
    return stats["distinct_colours"] < 2 or stats["luminance_stddev"] < min_stddev


def verdict(
    stats: dict, *, min_stddev: float = DEFAULT_MIN_STDDEV, min_colour_fraction: float | None = None
) -> tuple[bool, str]:
    """(ok, one-line reason)."""
    if is_blank(stats, min_stddev=min_stddev):
        return False, (
            f"blank: {stats['distinct_colours']} colour(s), luminance stddev "
            f"{stats['luminance_stddev']} < {min_stddev}"
        )
    if min_colour_fraction is not None:
        fraction = stats.get("colour_fraction", 0.0)
        if fraction < min_colour_fraction:
            return False, (
                f"colour #{stats.get('colour')} covers {fraction:.4f} of the image, "
                f"below {min_colour_fraction}"
            )
        return True, (
            f"not blank ({stats['distinct_colours']}{'+' if stats['distinct_capped'] else ''} colours, "
            f"stddev {stats['luminance_stddev']}); #{stats['colour']} covers {fraction:.4f}"
        )
    return True, (
        f"not blank ({stats['distinct_colours']}{'+' if stats['distinct_capped'] else ''} colours, "
        f"stddev {stats['luminance_stddev']})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file")
    parser.add_argument("--colour", help="RRGGBB that must be present")
    parser.add_argument("--min-colour-fraction", type=float, default=0.01)
    parser.add_argument("--tolerance", type=int, default=8)
    parser.add_argument("--min-stddev", type=float, default=DEFAULT_MIN_STDDEV)
    args = parser.parse_args(argv)

    try:
        with open(args.file, "rb") as handle:
            image = decode(handle.read())
    except (OSError, PngError) as exc:
        print(json.dumps({"ok": False, "reason": f"unreadable: {exc}"}))
        return 2
    colour = parse_colour(args.colour) if args.colour else None
    stats = analyse(image, colour=colour, tolerance=args.tolerance)
    ok, reason = verdict(
        stats,
        min_stddev=args.min_stddev,
        min_colour_fraction=args.min_colour_fraction if colour else None,
    )
    print(json.dumps({"ok": ok, "reason": reason, **stats}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

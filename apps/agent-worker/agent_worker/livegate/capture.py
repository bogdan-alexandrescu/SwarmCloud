"""Frames from the display sidecar's Xvfb, as a copy the mask can paint on.

docs/design/live-browser.md §3.1: the live gate captures the framebuffer,
masks it (§5.3, `livegate.mask`), then encodes it. This module is the first
step.

HOW: XVFB'S OWN FRAMEBUFFER FILE, NOT AN X CLIENT. Xvfb started with
`-fbdir <dir>` keeps its screen in `<dir>/Xvfb_screen0`, a memory-mapped file
in XWD format (header, colormap, then the pixels), and draws into it directly.
Reading that file needs no X connection, no XShm, no Xlib binding and no
third-party package: the gate opens it read-only and copies the pixel bytes.
The directory is the sidecar's own `/tmp` (§3.1), which the worker container
does not mount.

A COPY, ALWAYS. `grab()` returns the pixels copied out of the mapping, and the
mask paints on the copy. Painting the mapping itself would race Xvfb (the next
draw overwrites the box) and would put the mask into what the browser reads
back, so the unmasked screen and the masked frame are never the same memory.

ONLY WHAT IT CAN MASK. A frame the mask cannot paint correctly is a frame that
leaves unmasked, so a format other than 32-bit ZPixmap -- the only one Xvfb
writes at the depth 24 this design runs (`-screen 0 WxHx24`) -- is refused
with `CaptureError` rather than guessed at. The gate sends nothing for a frame
it could not capture.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: The XWD file header: 25 CARD32 fields, big-endian whatever the host
#: (Xvfb byte-swaps it on a little-endian machine; X11 `XWDFile.h`).
_HEADER = struct.Struct(">25I")
_COLOUR_SIZE = 12  # XWDColor: CARD32 pixel, 3x CARD16, CARD8 flags, CARD8 pad
_XWD_VERSION = 7
_ZPIXMAP = 2
_LSB_FIRST = 0
_MSB_FIRST = 1

BYTES_PER_PIXEL = 4

#: A screen larger than this is not one this design launches (§6.1's largest
#: preset is 1440x900 before its device scale factor), and a header claiming
#: it is a corrupt file, not a reason to allocate gigabytes.
MAX_DIMENSION = 8192


class CaptureError(Exception):
    """The framebuffer could not be read as a frame the mask can paint."""


@dataclass
class Frame:
    """One screen of 32-bit pixels, in the byte order and channel masks Xvfb used.

    `pixels` is `height` rows of `stride` bytes. The channel masks and byte
    order are kept so the encoder can declare the same pixel format to the
    viewer and send the bytes as they are.
    """

    width: int
    height: int
    stride: int
    pixels: bytearray
    red_mask: int = 0xFF0000
    green_mask: int = 0x00FF00
    blue_mask: int = 0x0000FF
    little_endian: bool = True
    _shifts: tuple[int, int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        masks = (self.red_mask, self.green_mask, self.blue_mask)
        if any(m == 0 or m >= 1 << 32 or not _contiguous_byte(m) for m in masks):
            raise CaptureError("pixel format: each channel must be one byte of a 32-bit pixel")
        self._shifts = tuple((m & -m).bit_length() - 1 for m in masks)  # type: ignore[assignment]
        if self.stride < self.width * BYTES_PER_PIXEL or len(self.pixels) < self.stride * self.height:
            raise CaptureError("pixel buffer shorter than its dimensions")

    @classmethod
    def blank(cls, width: int, height: int, rgb: tuple[int, int, int]) -> "Frame":
        frame = cls(width, height, width * BYTES_PER_PIXEL, bytearray(width * height * BYTES_PER_PIXEL))
        frame.fill(0, 0, width, height, rgb)
        return frame

    def encode(self, rgb: tuple[int, int, int]) -> bytes:
        """The four bytes one pixel of this colour is in this frame's format."""
        r, g, b = rgb
        rs, gs, bs = self._shifts
        value = (r << rs) | (g << gs) | (b << bs)
        return value.to_bytes(BYTES_PER_PIXEL, "little" if self.little_endian else "big")

    def rgb_at(self, x: int, y: int) -> tuple[int, int, int]:
        at = y * self.stride + x * BYTES_PER_PIXEL
        value = int.from_bytes(self.pixels[at:at + BYTES_PER_PIXEL],
                               "little" if self.little_endian else "big")
        rs, gs, bs = self._shifts
        return ((value >> rs) & 0xFF, (value >> gs) & 0xFF, (value >> bs) & 0xFF)

    def fill(self, x0: int, y0: int, x1: int, y1: int, rgb: tuple[int, int, int]) -> None:
        """Paint the half-open box [x0, x1) x [y0, y1), clipped to the frame."""
        x0, x1 = max(0, x0), min(self.width, x1)
        y0, y1 = max(0, y0), min(self.height, y1)
        if x0 >= x1 or y0 >= y1:
            return
        run = self.encode(rgb) * (x1 - x0)
        for y in range(y0, y1):
            at = y * self.stride + x0 * BYTES_PER_PIXEL
            self.pixels[at:at + len(run)] = run


def _contiguous_byte(mask: int) -> bool:
    low = mask & -mask
    return mask // low == 0xFF and (low.bit_length() - 1) % 8 == 0


def parse_xwd(data: bytes | bytearray | memoryview | mmap.mmap) -> Frame:
    """A copy of the pixels of an XWD image, or `CaptureError`."""
    if len(data) < _HEADER.size:
        raise CaptureError("framebuffer shorter than an XWD header")
    (header_size, version, pixmap_format, _depth, width, height, xoffset,
     byte_order, _unit, _bit_order, _pad, bits_per_pixel, bytes_per_line,
     _visual, red_mask, green_mask, blue_mask, _bits_per_rgb, _entries, ncolors,
     *_window) = _HEADER.unpack_from(data, 0)
    if version != _XWD_VERSION or pixmap_format != _ZPIXMAP or xoffset != 0:
        raise CaptureError("framebuffer is not an XWD version 7 ZPixmap")
    if bits_per_pixel != 32 or byte_order not in (_LSB_FIRST, _MSB_FIRST):
        raise CaptureError("framebuffer is not 32 bits per pixel")
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise CaptureError("framebuffer dimensions out of range")
    if header_size < _HEADER.size or bytes_per_line < width * BYTES_PER_PIXEL:
        raise CaptureError("framebuffer header is inconsistent")
    start = header_size + ncolors * _COLOUR_SIZE
    end = start + bytes_per_line * height
    if end > len(data):
        raise CaptureError("framebuffer shorter than its header says")
    return Frame(
        width=width,
        height=height,
        stride=bytes_per_line,
        pixels=bytearray(data[start:end]),
        red_mask=red_mask,
        green_mask=green_mask,
        blue_mask=blue_mask,
        little_endian=byte_order == _LSB_FIRST,
    )


class XvfbFramebuffer:
    """Xvfb's `-fbdir` screen file, mapped read-only.

    The mapping is reopened when the file's size changes, which is what a
    display resize (a device preset, §6.1) does to it.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None
        self._map: mmap.mmap | None = None

    def __enter__(self) -> "XvfbFramebuffer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _mapping(self) -> mmap.mmap:
        size = os.stat(self._path).st_size
        if self._map is not None and len(self._map) == size:
            return self._map
        self.close()
        if size == 0:
            raise CaptureError("framebuffer file is empty")
        fd = os.open(self._path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            self._map = mmap.mmap(fd, size, access=mmap.ACCESS_READ)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self._map

    def grab(self) -> Frame:
        """The screen as it is now, copied. `CaptureError` if it cannot be read."""
        try:
            return parse_xwd(self._mapping())
        except OSError as exc:
            raise CaptureError(f"framebuffer unreadable: {exc.strerror}") from None

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

"""Scripted visual-QA capture through the live browser (QA-CAPTURE, part of #1649).

    python -m swarm_qa capture \\
        --routes apps/swarm-ui/qa/routes.json --group work-a \\
        --base http://localhost:5173 \\
        --presets desktop,phone-390 --themes light,dark \\
        --out "$SWARM_ARTIFACTS_DIR/capture-work-a" \\
        --bundle "$SWARM_ARTIFACTS_DIR/capture-work-a.tar" [--axe]

    python -m swarm_qa diff A.tar B.tar --out DIR [--threshold 0.001]

docs/design/visual-qa.md §4.2 is the design; this is the tool an agent RUNS on
`claude-code-browser`. Nothing in the worker calls it. It is installed beside
`swarm_browser.py` in the image's venv site-packages
(images/agent-runtime-browser/Dockerfile), so `python -m swarm_qa` works from
the agent's `python`.

WHY A SCRIPT AND NOT THE MODEL. Two captures can only be compared if they were
taken the same way: same device, same scheme, same scroll positions, the same
clock. A model driving the browser screenshots whatever it thought to, at
whatever moment; this takes the same images every time, and a model reviews
them afterwards (§3.2, owner decision 2026-10-19, design B).

What `capture` does, and why each:

1. REFUSES WITHOUT THE LIVE BROWSER. `$SWARM_CDP_ENDPOINT` unset exits 2 and
   writes nothing. A headless fallback would capture at another size with
   other fonts, which is noise in every comparison.
2. Connects with `swarm_browser.live()` and opens ONE TAB OF ITS OWN, closed at
   the end; it disconnects (Playwright stops) and never closes the browser,
   which is the one the person watches.
3. THE DEVICE IS THE GATE'S, BY NAME: `swarm-handoff device <preset>` per
   preset, which blocks until the worker answers. It never calls
   `set_viewport_size`: the gate refuses the metrics override that sends.
4. THE THEME, TWICE, TO ONE VALUE. The design said
   `page.emulate_media(color_scheme=...)`; main has since grown
   `swarm-handoff theme`, the gate's own override on every page's session
   (QA-KIT; livegate/device.py says which of two sessions' overrides Chromium
   shows was NOT measured). So the capture sets both, to the same scheme:
   whichever session wins, the page sees the scheme asked for. emulate_media
   also sets `reduced_motion="reduce"`, which the gate's command does not
   carry. When the capture ends the gate's theme goes back to `system`.
5. Per route: navigates and waits for `load`, then up to NETWORK_IDLE_MS for
   the network to go quiet (the console polls, so it may never be quiet for
   long: not reaching it is recorded, not fatal), then `document.fonts.ready`,
   then the route's `ready` selector. The clock is frozen at the route list's
   instant (`page.clock.set_fixed_time`, before the first navigation; timers
   still run) so "3 minutes ago" reads the same every run. Viewport-sized
   SEGMENTS, scrolling the page's own scroller one viewport at a time up to the
   route's `segments` (default DEFAULT_SEGMENTS): a model is handed a tall
   image downscaled until its text is unreadable, and whether a full-page
   capture through the gate needs a metrics override is unverified.
   `animations="disabled"`, `caret="hide"`.
6. Records what the page said, beside the PNGs: console and page errors and
   failed requests (an address as origin and path only: a query can carry a
   token), the body text, and the ARIA snapshot (`locator.aria_snapshot()`;
   Playwright deprecated `page.accessibility` in its favour).
7. `--axe` runs axe-core from the image (AXE_SCRIPT, pinned in the
   Dockerfile) and writes `<route>--<preset>--<theme>.axe.json`.
8. `manifest.json` and then the tarball, ALWAYS. A route that failed is a
   finding, listed in `failed`, not a failed step: the workflow's next step
   names the tarball in `input_from`, and a missing one fails the attempt
   (§2.3). It exits 0 when at least one image was captured and 1 when none
   was -- an empty capture is not a clean one -- with the tarball either way.

File names: `<group>/<route>--<preset>--<theme>--s<N>.png`, so a review and a
report can name every image without opening the manifest.

`diff` compares two tarballs image by image: a changed-pixel ratio per image
present in both, a `<name>.diff.png` where the ratio is over the threshold
(the changed pixels in DIFF_MARK over a faded copy of A), and the names only
one side has. It decodes with Pillow when Pillow imports (the image pins it)
and otherwise with the standard-library decoder below, which reads what
Chromium writes (8-bit RGB or RGBA, not interlaced): the unit tests run with
no Pillow, and the comparison itself is the same code either way. Byte-equal
images are ratio 0 without decoding, which is the deterministic case.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: The live browser's CDP endpoint (swarm_browser.ENDPOINT_ENV): set only when it exists.
ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"
#: The agent's hand-off command, on PATH in the browser image.
HANDOFF = "swarm-handoff"
#: Where the Dockerfile puts axe-core's browser bundle.
AXE_SCRIPT = "/opt/swarm/axe-core/axe.min.js"

EXIT_OK = 0
EXIT_NOTHING_CAPTURED = 1
EXIT_NO_LIVE_BROWSER = 2
EXIT_USAGE = 64

ROUTES_FORMAT = 1
MANIFEST_FORMAT = 1
DIFF_FORMAT = 1
DEFAULT_SEGMENTS = 4
#: More than this many viewports of one page is a page to split, not to scroll.
MAX_SEGMENTS = 12
THEMES = ("light", "dark")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")

#: `swarm-handoff device` waits up to 30 s for the worker (DEVICE_WAIT_SECONDS
#: there); this is that plus room for the process itself.
HANDOFF_TIMEOUT_SECONDS = 60
NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 10_000
READY_TIMEOUT_MS = 30_000
SHOT_TIMEOUT_MS = 30_000
#: An error message kept in the manifest is one line, this long at most.
MAX_ERROR_CHARS = 300
#: Each console or page error kept beside a route's images.
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 500

DIFF_THRESHOLD = 0.001
#: The colour a changed pixel is painted in a diff image (magenta: a console
#: page never fades to it, so a mark is never mistaken for the page).
DIFF_MARK = (255, 0, 255)
#: A PNG member of a tarball is read only up to this size.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

FONTS_READY_JS = "() => document.fonts.ready.then(() => true)"
#: The element that scrolls: the document, or the console's `main` or open
#: agent pane when the page itself does not scroll. Kept on `window` for SCROLL_JS.
MEASURE_JS = """() => {
  const doc = document.scrollingElement || document.documentElement;
  let best = doc, room = doc.scrollHeight - doc.clientHeight;
  for (const el of document.querySelectorAll('main, [role="dialog"]')) {
    const overflow = getComputedStyle(el).overflowY;
    const r = el.scrollHeight - el.clientHeight;
    if ((overflow === 'auto' || overflow === 'scroll') && r > room) { best = el; room = r; }
  }
  window.__swarmQaScroller = best;
  return [best.scrollHeight, best === doc ? innerHeight : best.clientHeight];
}"""
SCROLL_JS = """(y) => {
  const el = window.__swarmQaScroller || document.scrollingElement || document.documentElement;
  el.scrollTop = y;
  return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(null))));
}"""
AXE_INJECT_JS = "(source) => { (0, eval)(source); return typeof axe !== 'undefined'; }"
AXE_RUN_JS = "() => axe.run(document, {resultTypes: ['violations']})"


class UsageError(ValueError):
    """A route list, group, preset or theme this tool will not run with."""


# -- the route list ------------------------------------------------------------


def load_routes(path: str | os.PathLike) -> dict:
    """apps/swarm-ui/qa/routes.json, checked: every route has a file-name-safe
    id unique across groups, a router path and a ready selector."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read the route list {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != ROUTES_FORMAT:
        raise UsageError(f"the route list is not format {ROUTES_FORMAT}")
    clock_at(document)
    groups = document.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise UsageError("the route list has no groups")
    seen: set[str] = set()
    for group, routes in groups.items():
        if not NAME.fullmatch(str(group)) or not isinstance(routes, list) or not routes:
            raise UsageError(f"group {group!r} is not a named, non-empty list")
        for route in routes:
            rid = route.get("id") if isinstance(route, dict) else None
            if not isinstance(rid, str) or not NAME.fullmatch(rid) or "--" in rid or rid in seen:
                raise UsageError(f"group {group}: route id {rid!r} is missing, unsafe or repeated")
            seen.add(rid)
            if not isinstance(route.get("path"), str) or not route["path"].startswith("/"):
                raise UsageError(f"route {rid}: path must start with /")
            if not isinstance(route.get("ready"), str) or not route["ready"].strip():
                raise UsageError(f"route {rid}: no ready selector")
            segments = route.get("segments", DEFAULT_SEGMENTS)
            if not isinstance(segments, int) or isinstance(segments, bool) or not 1 <= segments <= MAX_SEGMENTS:
                raise UsageError(f"route {rid}: segments must be 1..{MAX_SEGMENTS}")
    mockups = document.setdefault("mockups", [])
    if not isinstance(mockups, list) or not all(
        isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("path"), str) for m in mockups
    ):
        raise UsageError("mockups must be a list of {id, path, compare_with}")
    return document


def clock_at(document: dict) -> dt.datetime:
    try:
        when = dt.datetime.fromisoformat(str(document.get("clock")))
    except ValueError as exc:
        raise UsageError("the route list's clock is not an ISO 8601 instant") from exc
    if when.tzinfo is None:
        raise UsageError("the route list's clock carries no time zone")
    return when


def image_name(group: str, route: str, preset: str, theme: str, segment: int) -> str:
    return f"{group}/{route}--{preset}--{theme}--s{segment}.png"


def _stem(group: str, route: str, preset: str, theme: str) -> str:
    return f"{group}/{route}--{preset}--{theme}"


def _names(value: str, what: str) -> list[str]:
    names = [v.strip() for v in value.split(",") if v.strip()]
    if not names or len(set(names)) != len(names) or not all(NAME.fullmatch(n) for n in names):
        raise UsageError(f"--{what} must be a comma-separated list of distinct names, got {value!r}")
    return names


# -- the live browser ----------------------------------------------------------


def _connect():
    """`(playwright, browser, context)` on the live browser (swarm_browser.live)."""
    from swarm_browser import live

    return live()


def _handoff(*args: str) -> str | None:
    """Run `swarm-handoff ARGS`; None when it was applied, else why not."""
    command = shutil.which(HANDOFF)
    if command is None:
        return f"{HANDOFF} is not on PATH"
    try:
        done = subprocess.run([command, *args], capture_output=True, text=True,
                              timeout=HANDOFF_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{HANDOFF} {' '.join(args)}: {exc}"
    if done.returncode == 0:
        return None
    answer: Any = None
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    why = answer.get("message") or answer.get("code") if isinstance(answer, dict) else None
    return f"{HANDOFF} {' '.join(args)} exited {done.returncode}: {why or 'no answer'}"


def _one_line(text: Any, limit: int = MAX_ERROR_CHARS) -> str:
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line[:limit]


def _where(url: Any) -> str:
    """An address as origin and path: a query or fragment can carry a token."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "?"
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else "?"


class _Errors:
    """What the page reported while one route was on screen."""

    def __init__(self, page):
        self.reset()
        page.on("console", self._console)
        page.on("pageerror", self._page_error)
        page.on("requestfailed", self._request_failed)

    def reset(self):
        self.console: list[str] = []
        self.page: list[str] = []
        self.requests: list[str] = []

    def _console(self, message):
        try:
            if message.type == "error":
                self.console.append(str(message.text)[:MAX_MESSAGE_CHARS])
        except Exception:  # noqa: BLE001 -- a listener must never break the capture
            pass

    def _page_error(self, error):
        self.page.append(str(error)[:MAX_MESSAGE_CHARS])

    def _request_failed(self, request):
        try:
            self.requests.append(_where(request.url))
        except Exception:  # noqa: BLE001
            pass

    def document(self) -> dict:
        return {"console_errors": self.console[:MAX_MESSAGES], "page_errors": self.page[:MAX_MESSAGES],
                "failed_requests": self.requests[:MAX_MESSAGES]}


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _capture_route(page, errors: _Errors, *, base: str, group: str, route: dict, preset: str, theme: str,
                   out: Path, axe_source: str | None) -> dict:
    """One route at one preset and theme. Raises with the step that failed."""
    rid, ready = route["id"], route["ready"]
    stem = _stem(group, rid, preset, theme)
    errors.reset()
    step = "navigate"
    try:
        page.goto(base + route["path"], wait_until="load", timeout=NAV_TIMEOUT_MS)
        idle = True
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
        except Exception:  # noqa: BLE001 -- a console that polls may never be idle
            idle = False
        step = "fonts"
        page.evaluate(FONTS_READY_JS)
        step = "ready"
        page.wait_for_selector(ready, state="visible", timeout=READY_TIMEOUT_MS)
        step = "measure"
        total, view = page.evaluate(MEASURE_JS)
        count = max(1, min(int(route.get("segments", DEFAULT_SEGMENTS)), math.ceil(total / max(1, view))))
        images = []
        for segment in range(1, count + 1):
            step = f"segment {segment}"
            page.evaluate(SCROLL_JS, (segment - 1) * view)
            png = page.screenshot(type="png", animations="disabled", caret="hide", timeout=SHOT_TIMEOUT_MS)
            name = image_name(group, rid, preset, theme, segment)
            _write(out / name, png)
            images.append(name)
        step = "text"
        _write(out / f"{stem}.txt", page.inner_text("body"))
        step = "a11y"
        snapshot = page.locator("body").aria_snapshot()
        _write(out / f"{stem}.a11y.json", json.dumps({"format": "aria-snapshot", "snapshot": snapshot}, indent=1))
        entry = {
            "id": rid, "preset": preset, "theme": theme, "status": "captured", "path": route["path"],
            "images": images, "network_idle": idle,
            "text": f"{stem}.txt", "a11y": f"{stem}.a11y.json", "errors": f"{stem}.errors.json",
        }
        if axe_source is not None:
            step = "axe"
            if not page.evaluate(AXE_INJECT_JS, axe_source):
                raise RuntimeError("axe-core did not load in the page")
            _write(out / f"{stem}.axe.json", json.dumps(page.evaluate(AXE_RUN_JS), indent=1))
            entry["axe"] = f"{stem}.axe.json"
    except Exception as exc:  # noqa: BLE001 -- a failed route is a finding
        where = f"ready selector {ready!r} not found" if step == "ready" else step
        raise _RouteFailed(f"{where}: {type(exc).__name__}: {_one_line(exc)}") from exc
    _write(out / f"{stem}.errors.json", json.dumps(errors.document(), indent=1))
    entry.update({"console_errors": len(errors.console), "page_errors": len(errors.page),
                  "failed_requests": len(errors.requests)})
    return entry


class _RouteFailed(Exception):
    pass


def _git_head() -> str | None:
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None


def _playwright_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:  # noqa: BLE001
        return None


def _bundle(out: Path, bundle: Path) -> None:
    bundle.parent.mkdir(parents=True, exist_ok=True)
    tmp = bundle.with_name(bundle.name + ".tmp")
    with tarfile.open(tmp, "w") as tar:
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            tar.add(str(path), arcname=path.relative_to(out).as_posix())
    os.replace(tmp, bundle)


def capture(*, routes: dict, group: str, base: str, presets: list[str], themes: list[str],
            out: Path, bundle: Path, axe_source: str | None = None) -> dict:
    """Capture `group` at every preset and theme; write the manifest and the
    tarball, whatever failed. Returns the manifest."""
    playwright, browser, context = _connect()
    manifest: dict = {
        "format": MANIFEST_FORMAT, "group": group, "commit": _git_head(),
        "browser": getattr(browser, "version", None), "playwright": _playwright_version(),
        "clock": routes["clock"], "base": base, "presets": presets, "themes": themes,
        "routes": [], "failed": [],
    }
    group_routes = routes["groups"][group]

    def fail_all(preset: str, theme: str, why: str) -> None:
        manifest["failed"].extend({"id": r["id"], "preset": preset, "theme": theme, "error": why}
                                  for r in group_routes)

    page = None
    try:
        page = context.new_page()
        errors = _Errors(page)
        page.clock.set_fixed_time(clock_at(routes))
        for preset in presets:
            why = _handoff("device", preset)
            if why is not None:
                for theme in themes:
                    fail_all(preset, theme, f"device {preset}: {why}")
                continue
            for theme in themes:
                why = _handoff("theme", theme)
                if why is not None:
                    fail_all(preset, theme, f"theme {theme}: {why}")
                    continue
                page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                for route in group_routes:
                    try:
                        manifest["routes"].append(_capture_route(
                            page, errors, base=base, group=group, route=route, preset=preset,
                            theme=theme, out=out, axe_source=axe_source))
                    except _RouteFailed as exc:
                        manifest["failed"].append({"id": route["id"], "preset": preset, "theme": theme,
                                                   "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 -- the browser went away: record it, still bundle
        manifest["aborted"] = f"{type(exc).__name__}: {_one_line(exc)}"
    finally:
        _handoff("theme", "system")
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        playwright.stop()
        _write(out / "manifest.json", json.dumps(manifest, indent=1))
        _bundle(out, bundle)
    return manifest


# -- PNG, standard library -----------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    stride = width * bpp
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif kind != 0:
            raise ValueError(f"PNG row filter {kind} is not one of 0..4")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """`(width, height, RGBA bytes)` of an 8-bit RGB or RGBA PNG, not interlaced."""
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    pos, header, idat = len(_PNG_SIGNATURE), None, bytearray()
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG without IHDR")
    width, height, depth, colour, _compression, _filter, interlace = header
    if depth != 8 or colour not in (2, 6) or interlace:
        raise ValueError(f"PNG depth {depth} colour type {colour} interlace {interlace}: only 8-bit RGB/RGBA")
    bpp = 4 if colour == 6 else 3
    pixels = _unfilter(zlib.decompress(bytes(idat)), width, height, bpp)
    if bpp == 4:
        return width, height, bytes(pixels)
    rgba = bytearray(width * height * 4)
    rgba[0::4], rgba[1::4], rgba[2::4] = pixels[0::3], pixels[1::3], pixels[2::3]
    rgba[3::4] = b"\xff" * (width * height)
    return width, height, bytes(rgba)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (_PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _decode(data: bytes) -> tuple[int, int, bytes]:
    """Pillow when the image carries it (fast); the standard library otherwise."""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return decode_png(data)
    with Image.open(BytesIO(data)) as image:
        rgba = image.convert("RGBA")
        return rgba.width, rgba.height, rgba.tobytes()


# -- diff ----------------------------------------------------------------------

#: Every channel of A, faded three quarters of the way to white.
_FADE = bytes((v + 3 * 255) // 4 for v in range(256))


def _pngs(bundle: Path) -> dict[str, bytes]:
    """The PNG members of a capture tarball, by name; never extracted to disk."""
    found: dict[str, bytes] = {}
    with tarfile.open(bundle) as tar:
        for member in tar.getmembers():
            name = member.name
            if (not member.isfile() or not name.endswith(".png") or name.startswith("/")
                    or ".." in name.split("/") or member.size > MAX_MEMBER_BYTES):
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                found[name] = handle.read()
    return found


def compare(a: bytes, b: bytes) -> tuple[float, bytes | None]:
    """`(changed-pixel ratio, diff PNG or None)` of two PNGs. A size change is
    ratio 1 with no diff image: every pixel moved."""
    if a == b:
        return 0.0, None
    wa, ha, pa = _decode(a)
    wb, hb, pb = _decode(b)
    if (wa, ha) != (wb, hb):
        return 1.0, None
    stride = wa * 4
    marks = bytearray(pa.translate(_FADE))
    mark = bytes(DIFF_MARK) + b"\xff"
    changed = 0
    for y in range(ha):
        row = slice(y * stride, (y + 1) * stride)
        if pa[row] == pb[row]:
            continue
        ra = memoryview(pa)[row].cast("I")
        rb = memoryview(pb)[row].cast("I")
        for x in range(wa):
            if ra[x] != rb[x]:
                changed += 1
                at = y * stride + x * 4
                marks[at:at + 4] = mark
    return changed / (wa * ha), encode_png(wa, ha, bytes(marks))


def diff(a: Path, b: Path, out: Path, threshold: float = DIFF_THRESHOLD) -> dict:
    left, right = _pngs(a), _pngs(b)
    report: dict = {"format": DIFF_FORMAT, "a": str(a), "b": str(b), "threshold": threshold, "images": [],
                    "only_in_a": sorted(set(left) - set(right)), "only_in_b": sorted(set(right) - set(left))}
    for name in sorted(set(left) & set(right)):
        ratio, image = compare(left[name], right[name])
        entry: dict = {"name": name, "ratio": ratio, "diff": None}
        if image is not None and ratio > threshold:
            entry["diff"] = name[: -len(".png")] + ".diff.png"
            _write(out / entry["diff"], image)
        report["images"].append(entry)
    report["over_threshold"] = sum(1 for i in report["images"] if i["ratio"] > threshold)
    _write(out / "diff.json", json.dumps(report, indent=1))
    return report


# -- the command ---------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m swarm_qa", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="capture one route group through the live browser")
    cap.add_argument("--routes", required=True)
    cap.add_argument("--group", required=True)
    cap.add_argument("--base", required=True, help="the console's origin, e.g. http://localhost:5173")
    cap.add_argument("--presets", default="desktop,phone-390")
    cap.add_argument("--themes", default="light,dark")
    cap.add_argument("--out", required=True)
    cap.add_argument("--bundle", required=True)
    cap.add_argument("--axe", action="store_true", help=f"run axe-core ({AXE_SCRIPT}) on every route")
    dif = sub.add_parser("diff", help="compare two capture tarballs image by image")
    dif.add_argument("a")
    dif.add_argument("b")
    dif.add_argument("--out", required=True)
    dif.add_argument("--threshold", type=float, default=DIFF_THRESHOLD)
    return parser


def _capture_command(args) -> int:
    if not os.environ.get(ENDPOINT_ENV, "").strip():
        sys.stderr.write(f"swarm_qa: no live browser ({ENDPOINT_ENV} is not set): "
                         "this capture would not match its baseline, so it was not taken\n")
        return EXIT_NO_LIVE_BROWSER
    try:
        routes = load_routes(args.routes)
        if args.group not in routes["groups"]:
            raise UsageError(f"no group {args.group!r} in {args.routes}; groups: {', '.join(routes['groups'])}")
        presets = _names(args.presets, "presets")
        themes = _names(args.themes, "themes")
        if not set(themes) <= set(THEMES):
            raise UsageError(f"--themes takes {' and '.join(THEMES)}")
        if not args.base.startswith(("http://", "https://")):
            raise UsageError("--base must be an http(s) origin")
        axe_source = None
        if args.axe:
            try:
                axe_source = Path(AXE_SCRIPT).read_text(encoding="utf-8")
            except OSError as exc:
                raise UsageError(f"--axe: axe-core is not at {AXE_SCRIPT}: {exc}") from exc
    except UsageError as exc:
        sys.stderr.write(f"swarm_qa: {exc}\n")
        return EXIT_USAGE
    out, bundle = Path(args.out), Path(args.bundle)
    out.mkdir(parents=True, exist_ok=True)
    manifest = capture(routes=routes, group=args.group, base=args.base.rstrip("/"), presets=presets,
                       themes=themes, out=out, bundle=bundle, axe_source=axe_source)
    images = sum(len(r["images"]) for r in manifest["routes"])
    sys.stdout.write(json.dumps({"group": args.group, "images": images, "captured": len(manifest["routes"]),
                                 "failed": len(manifest["failed"]), "aborted": manifest.get("aborted"),
                                 "bundle": str(bundle)}) + "\n")
    return EXIT_OK if images else EXIT_NOTHING_CAPTURED


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        return _capture_command(args)
    out = Path(args.out)
    report = diff(Path(args.a), Path(args.b), out, args.threshold)
    sys.stdout.write(json.dumps({"compared": len(report["images"]), "over_threshold": report["over_threshold"],
                                 "only_in_a": len(report["only_in_a"]), "only_in_b": len(report["only_in_b"]),
                                 "report": str(out / "diff.json")}) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

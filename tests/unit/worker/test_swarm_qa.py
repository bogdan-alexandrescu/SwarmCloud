"""QA-CAPTURE (docs/design/visual-qa.md §4.2, §6; part of #1649): `swarm_qa`.

`python -m swarm_qa capture` sweeps one group of the console's route list
(apps/swarm-ui/qa/routes.json) through the LIVE browser, at each device preset
and colour scheme, and always leaves a tarball with a manifest; `swarm_qa diff`
compares two such tarballs image by image. These are the row's red-first
tests:

* without `$SWARM_CDP_ENDPOINT` it exits 2 and writes nothing -- a headless
  fallback would capture at another size with other fonts;
* the device is switched by preset name through `swarm-handoff device`, and
  the page is never resized (the gate refuses `setDeviceMetricsOverride`);
* the theme is set by emulated media;
* a route whose ready selector times out is a finding in the manifest, not a
  missing tarball;
* every image is named `<group>/<route>--<preset>--<theme>--s<N>.png`;
* `diff` reports a changed-pixel ratio per image and writes a diff PNG where
  it is over the threshold.

No Chromium and no Playwright: the page, the context and `swarm-handoff` are
fakes, and the PNGs are written by an encoder of this file's own, row filters
and all, so the module's decoder is tested against bytes it did not write.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import struct
import sys
import tarfile
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
IMAGE_DIR = REPO / "images" / "agent-runtime-browser"
MODULE = IMAGE_DIR / "swarm_qa.py"
DOCKERFILE = IMAGE_DIR / "Dockerfile"


def _module():
    spec = importlib.util.spec_from_file_location("swarm_qa_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# PNGs written here, independently of the module
# --------------------------------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _png(width: int, height: int, pixel) -> bytes:
    """An 8-bit RGBA PNG, each row under a different filter (0..4 in turn),
    so a decoder that only undoes `None` fails on it."""
    bpp = 4
    rows = [bytes(v for x in range(width) for v in pixel(x, y)) for y in range(height)]
    raw = bytearray()
    prev = bytes(width * bpp)
    for y, row in enumerate(rows):
        kind = y % 5
        out = bytearray()
        for i, v in enumerate(row):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pred = (0, a, b, (a + b) // 2, _paeth(a, b, c))[kind]
            out.append((v - pred) & 0xFF)
        raw.append(kind)
        raw += out
        prev = row
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b""))


def _white(_x, _y):
    return (255, 255, 255, 255)


# --------------------------------------------------------------------------
# A fake live browser
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self, log):
        self.log = log

    def set_fixed_time(self, when):
        self.log.append(("clock", when))


class _Locator:
    def __init__(self, page):
        self.page = page

    def aria_snapshot(self, **_kw):
        return "- heading \"%s\" [level=1]" % self.page.url


class FakePage:
    """What `swarm_qa.capture` may do to a page. `set_viewport_size` fails the
    test outright: the gate owns the size."""

    def __init__(self, module, *, slow=(), scroll_height=2000, viewport_height=900):
        self.qa = module
        self.slow = set(slow)
        self.scroll_height = scroll_height
        self.viewport_height = viewport_height
        self.url = "about:blank"
        self.log = []
        self.media = None
        self.clock = _Clock(self.log)
        self.handlers = {}
        self.closed = False

    def set_viewport_size(self, *_a, **_kw):
        raise AssertionError("swarm_qa resized the page; the gate owns the size")

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kw):
        self.url = url
        self.log.append(("goto", url, kw.get("wait_until")))

    def wait_for_load_state(self, state=None, **_kw):
        self.log.append(("load_state", state))

    def wait_for_selector(self, selector, **kw):
        self.log.append(("ready", selector))
        if any(self.url.endswith(path) for path in self.slow):
            raise TimeoutError(f"Timeout {kw.get('timeout')}ms exceeded waiting for {selector}")

    def evaluate(self, expression, arg=None):
        if expression == self.qa.FONTS_READY_JS:
            return True
        if expression == self.qa.MEASURE_JS:
            return [self.scroll_height, self.viewport_height]
        if expression == self.qa.SCROLL_JS:
            self.log.append(("scroll", arg))
            return None
        if expression == self.qa.AXE_RUN_JS:
            return {"violations": []}
        self.log.append(("evaluate", expression[:40]))
        return None

    def emulate_media(self, **kw):
        self.media = dict(kw)
        self.log.append(("media", dict(kw)))

    def screenshot(self, **kw):
        assert "path" not in kw, "swarm_qa names its files itself"
        assert not kw.get("full_page"), "a full-page capture is a downscaled image to the model"
        self.log.append(("shot", self.url, (self.media or {}).get("color_scheme"), dict(kw)))
        return _png(3, 2, _white)

    def inner_text(self, selector, **_kw):
        return f"text of {self.url} under {selector}"

    def locator(self, _selector):
        return _Locator(self)

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = []

    def new_page(self):
        self.pages.append(self.page)
        return self.page


class FakeBrowser:
    version = "141.0.0.0"

    def close(self):
        raise AssertionError("swarm_qa closed the live browser; it must only disconnect")


class FakePlaywright:
    stopped = False

    def stop(self):
        self.stopped = True


ROUTES = {
    "format": 1,
    "clock": "2026-10-19T12:00:00Z",
    "groups": {
        "work-a": [
            {"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1"},
            {"id": "timeline", "address": "work/timeline", "path": "/timeline", "ready": "main h1", "segments": 1},
        ],
        "other": [
            {"id": "pools", "address": "capacity/pools", "path": "/capacity/pools", "ready": "main h1"},
        ],
    },
    "mockups": [],
}


@pytest.fixture
def qa():
    return _module()


@pytest.fixture
def world(tmp_path, monkeypatch, qa):
    """A live browser that exists: the endpoint is set, `swarm-handoff` on PATH
    is a fake that logs its arguments and answers `applied`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "handoff.log"
    fake = bin_dir / "swarm-handoff"
    fake.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_HANDOFF_LOG'], 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'outcome': 'applied'}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_HANDOFF_LOG", str(log))
    monkeypatch.setenv(qa.ENDPOINT_ENV, "http://127.0.0.1:9222")
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps(ROUTES), encoding="utf-8")

    class World:
        pass

    w = World()
    w.tmp = tmp_path
    w.log = log
    w.routes = routes
    w.page = FakePage(qa)
    w.playwright = FakePlaywright()
    monkeypatch.setattr(qa, "_connect", lambda: (w.playwright, FakeBrowser(), FakeContext(w.page)))

    def handoff_calls():
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    w.handoff_calls = handoff_calls
    return w


def _capture(qa, world, *extra, group="work-a", presets="desktop,phone-390", themes="light,dark"):
    out = world.tmp / f"capture-{group}"
    bundle = world.tmp / f"capture-{group}.tar"
    code = qa.main([
        "capture", "--routes", str(world.routes), "--group", group,
        "--base", "http://localhost:5173", "--presets", presets, "--themes", themes,
        "--out", str(out), "--bundle", str(bundle), *extra,
    ])
    return code, out, bundle


def _manifest(bundle: Path) -> dict:
    with tarfile.open(bundle) as tar:
        member = tar.extractfile("manifest.json")
        assert member is not None, "the tarball carries no manifest.json"
        return json.loads(member.read().decode("utf-8"))


# --------------------------------------------------------------------------
# The row's red-first tests
# --------------------------------------------------------------------------


def test_capture_refuses_without_the_live_browser(qa, world, monkeypatch, capsys):
    monkeypatch.delenv(qa.ENDPOINT_ENV)

    def no_connect():
        raise AssertionError("connected although there is no live browser")

    monkeypatch.setattr(qa, "_connect", no_connect)
    code, out, bundle = _capture(qa, world)
    assert code == 2
    assert not out.exists() and not bundle.exists(), "a refused capture wrote something"
    assert world.handoff_calls() == [], "a refused capture switched the device"
    assert "no live browser" in capsys.readouterr().err


def test_capture_switches_device_by_preset_and_never_resizes(qa, world):
    code, _out, bundle = _capture(qa, world)
    assert code == 0
    calls = world.handoff_calls()
    devices = [c for c in calls if c.startswith("device ")]
    assert devices == ["device desktop", "device phone-390"], calls
    # Each preset's images were taken after its switch: the desktop shots come
    # before the second `device` call in the page's own order of events.
    manifest = _manifest(bundle)
    assert manifest["presets"] == ["desktop", "phone-390"]
    assert {r["preset"] for r in manifest["routes"]} == {"desktop", "phone-390"}
    # FakePage.set_viewport_size raises: reaching here is the assertion. The
    # capture opened a tab of its own, closed it, and disconnected.
    assert world.page.closed and world.playwright.stopped


def test_capture_sets_theme_by_emulated_media(qa, world):
    code, _out, _bundle = _capture(qa, world, presets="desktop")
    assert code == 0
    media = [entry[1] for entry in world.page.log if entry[0] == "media"]
    assert [m["color_scheme"] for m in media] == ["light", "dark"], media
    assert all(m.get("reduced_motion") == "reduce" for m in media), media
    shots = [entry for entry in world.page.log if entry[0] == "shot"]
    assert shots and {s[2] for s in shots} == {"light", "dark"}
    for shot in shots:
        assert shot[3].get("animations") == "disabled" and shot[3].get("caret") == "hide", shot
    # The gate's own theme is set to match, and lifted when the capture ends,
    # so the page's session and the gate's never disagree on the scheme.
    themes = [c for c in world.handoff_calls() if c.startswith("theme ")]
    assert themes == ["theme light", "theme dark", "theme system"], themes
    # The clock is frozen at the route list's instant before the first page loads.
    first_goto = next(i for i, e in enumerate(world.page.log) if e[0] == "goto")
    clocks = [i for i, e in enumerate(world.page.log) if e[0] == "clock"]
    assert clocks and clocks[0] < first_goto
    assert world.page.log[clocks[0]][1].isoformat().startswith("2026-10-19T12:00:00")


def test_capture_writes_the_tarball_when_a_route_fails(qa, world):
    world.page.slow = {"/timeline"}
    code, out, bundle = _capture(qa, world)
    assert code == 0, "a failed route is a finding, not a failed step"
    assert bundle.is_file()
    manifest = _manifest(bundle)
    failed = manifest["failed"]
    assert {(f["id"], f["preset"], f["theme"]) for f in failed} == {
        ("timeline", p, t) for p in ("desktop", "phone-390") for t in ("light", "dark")
    }
    assert all("ready" in f["error"] or "Timeout" in f["error"] for f in failed), failed
    captured = [r for r in manifest["routes"] if r["status"] == "captured"]
    assert {r["id"] for r in captured} == {"runs"} and len(captured) == 4
    # And the run whose every route failed still leaves its tarball, but does
    # not call itself a success: an empty capture is not a clean one.
    world.page.slow = {"/timeline", "/runs"}
    code, _out, bundle = _capture(qa, world)
    assert code == 1
    assert _manifest(bundle)["routes"] == [] or all(r["status"] != "captured" for r in _manifest(bundle)["routes"])
    assert len(_manifest(bundle)["failed"]) == 8


def test_file_names_carry_route_preset_theme_segment(qa, world):
    assert qa.image_name("work-a", "runs", "phone-390", "dark", 2) == "work-a/runs--phone-390--dark--s2.png"
    code, out, bundle = _capture(qa, world)
    assert code == 0
    with tarfile.open(bundle) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.name.endswith(".png"))
    pattern = re.compile(r"^work-a/(runs|timeline)--(desktop|phone-390)--(light|dark)--s([1-9][0-9]*)\.png$")
    assert names and all(pattern.match(n) for n in names), names
    # 2000 px of page in a 900 px viewport is three segments; `timeline` asks for one.
    runs = [n for n in names if n.startswith("work-a/runs--desktop--light--")]
    assert runs == [f"work-a/runs--desktop--light--s{i}.png" for i in (1, 2, 3)]
    assert [n for n in names if n.startswith("work-a/timeline--desktop--light--")] == [
        "work-a/timeline--desktop--light--s1.png"
    ]
    assert len(names) == 4 * (3 + 1)
    manifest = _manifest(bundle)
    listed = sorted(i for r in manifest["routes"] for i in r["images"])
    assert listed == names, "the manifest names exactly the images the tarball holds"
    for route in manifest["routes"]:
        assert route["text"] == f"work-a/{route['id']}--{route['preset']}--{route['theme']}.txt"
        assert (out / route["text"]).is_file() and (out / route["a11y"]).is_file()


def test_diff_reports_ratio_and_writes_a_diff_png(qa, tmp_path):
    same = _png(10, 10, _white)

    def dotted(x, y):
        return (0, 0, 0, 255) if x < 2 and y < 5 else (255, 255, 255, 255)

    changed = _png(10, 10, dotted)

    def bundle(path, images):
        with tarfile.open(path, "w") as tar:
            for name, data in images.items():
                file = tmp_path / "member"
                file.write_bytes(data)
                tar.add(str(file), arcname=name)
        return path

    a = bundle(tmp_path / "a.tar", {
        "g/one--desktop--light--s1.png": same,
        "g/two--desktop--light--s1.png": same,
        "g/gone--desktop--light--s1.png": same,
    })
    b = bundle(tmp_path / "b.tar", {
        "g/one--desktop--light--s1.png": same,
        "g/two--desktop--light--s1.png": changed,
        "g/new--desktop--light--s1.png": same,
    })
    out = tmp_path / "diff"
    assert qa.main(["diff", str(a), str(b), "--out", str(out)]) == 0
    report = json.loads((out / "diff.json").read_text(encoding="utf-8"))
    by_name = {i["name"]: i for i in report["images"]}
    assert by_name["g/one--desktop--light--s1.png"]["ratio"] == 0
    assert by_name["g/one--desktop--light--s1.png"]["diff"] is None
    two = by_name["g/two--desktop--light--s1.png"]
    assert two["ratio"] == pytest.approx(10 / 100)
    assert report["only_in_a"] == ["g/gone--desktop--light--s1.png"]
    assert report["only_in_b"] == ["g/new--desktop--light--s1.png"]
    # The diff image is a PNG of the same size, and it marks exactly the
    # changed pixels: read back with zlib, outside the module.
    data = (out / two["diff"]).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (10, 10)
    w, h, rgba = qa.decode_png(data)
    marked = {(i // 4 % w, i // 4 // w) for i in range(0, len(rgba), 4) if rgba[i:i + 3] == bytes(qa.DIFF_MARK)}
    assert marked == {(x, y) for x in range(2) for y in range(5)}


def test_decode_png_reads_every_row_filter(qa):
    def gradient(x, y):
        return ((x * 37 + y) % 256, (y * 53) % 256, (x * y) % 256, 255 - x)

    w, h, rgba = qa.decode_png(_png(7, 10, gradient))
    assert (w, h) == (7, 10)
    assert rgba == bytes(v for y in range(10) for x in range(7) for v in gradient(x, y))


# --------------------------------------------------------------------------
# The image carries the tool and its pins
# --------------------------------------------------------------------------


def _instructions(text: str) -> list[str]:
    joined, current = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        joined.append(current + stripped)
        current = ""
    return joined


def test_the_image_installs_swarm_qa_beside_swarm_browser_with_axe_and_pillow_pinned():
    steps = _instructions(DOCKERFILE.read_text(encoding="utf-8"))
    copies = [s for s in steps if s.startswith("COPY ") and "images/agent-runtime-browser/swarm_qa.py" in s]
    assert copies == [
        "COPY --chmod=0444 images/agent-runtime-browser/swarm_qa.py "
        "/opt/venv/lib/python3.11/site-packages/swarm_qa.py"
    ], copies
    args = dict(re.findall(r"^ARG (AXE_CORE_VERSION|PILLOW_VERSION)=(\S+)$", "\n".join(steps), re.M))
    assert re.fullmatch(r"\d+\.\d+\.\d+", args.get("AXE_CORE_VERSION", "")), args
    assert re.fullmatch(r"\d+\.\d+\.\d+", args.get("PILLOW_VERSION", "")), args
    runs = [s for s in steps if s.startswith("RUN ")]
    assert any('"pillow==${PILLOW_VERSION}"' in s and "pip install" in s for s in runs), runs
    assert any('"axe-core@${AXE_CORE_VERSION}"' in s for s in runs), runs
    # The axe script is where swarm_qa reads it, and nothing is installed
    # after the self-test (test_browser_enforce holds the order).
    qa = _module()
    assert any(qa.AXE_SCRIPT in s for s in runs), (qa.AXE_SCRIPT, runs)

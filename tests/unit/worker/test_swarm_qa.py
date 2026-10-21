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
import shutil
import struct
import subprocess
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
# A DOM under node, for the scripts the capture evaluates in the page
# --------------------------------------------------------------------------

#: The page's scripts run against a DOM of boxes this program builds from a
#: list of nodes: what each scrolls (`sh` / `ch` / `cw`, scrollHeight,
#: clientHeight, clientWidth), its overflow-y, its tag, classes and
#: attributes, its own text, whether it has no box (`hidden`), its parent,
#: its CSS `position` and `top` (`pos`, `top` in px) and where its box is on
#: the screen (`rt` its top, `rh` its height; `ch` when not given). Selectors are the shapes the scripts and the
#: route list use: `tag`, `.class`, `[attr="v"]`, `[attr*="v"]`, `*`,
#: descendant and comma.
_DOM_PROGRAM = r"""
const spec = JSON.parse(process.argv[1]);
const els = spec.nodes.map((n) => ({
  localName: n.tag, tagName: n.tag.toUpperCase(), id: n.id || '', className: n.cls || '',
  classList: (n.cls || '').split(' ').filter(Boolean), _attrs: n.attrs || {}, _oy: n.oy || 'visible',
  scrollHeight: n.sh || 0, clientHeight: n.ch || 0, clientWidth: n.cw || 0, scrollTop: 0, parentElement: null,
  _text: n.text || '', _hidden: n.hidden || false, _pos: n.pos || 'static',
  _top: n.top === undefined ? 'auto' : n.top + 'px', _rt: n.rt || 0, _rh: n.rh === undefined ? (n.ch || 0) : n.rh,
}));
spec.nodes.forEach((n, i) => { els[i].parentElement = n.parent === undefined ? null : els[n.parent]; });
const simple = (el, s) => {
  const m = s.match(/^([a-z0-9*]*)((?:\.[\w-]+)*)(?:\[([\w-]+)(\*?)="([^"]*)"\])?$/);
  if (!m) throw new SyntaxError('unsupported selector ' + s);
  if (m[1] && m[1] !== '*' && m[1] !== el.localName) return false;
  for (const c of m[2].split('.').filter(Boolean)) if (!el.classList.includes(c)) return false;
  if (!m[3]) return true;
  const value = m[3] === 'class' ? el.className : el._attrs[m[3]];
  return m[4] ? typeof value === 'string' && value.includes(m[5]) : value === m[5];
};
const one = (el, sel) => {
  const parts = sel.trim().split(/\s+/);
  if (!simple(el, parts.pop())) return false;
  for (let up = el.parentElement; parts.length && up; up = up.parentElement) {
    if (simple(up, parts[parts.length - 1])) parts.pop();
  }
  return parts.length === 0;
};
const all = (sel) => els.filter((el) => sel.split(',').some((s) => one(el, s)));
els.forEach((el) => {
  el.matches = (s) => all(s).includes(el);
  el.contains = (o) => { for (let x = o; x; x = x.parentElement) if (x === el) return true; return false; };
  el.getClientRects = () => (el._hidden ? [] : [{}]);
  el.getBoundingClientRect = () => ({ top: el._rt, bottom: el._rt + el._rh, height: el._rh });
});
globalThis.window = globalThis;
globalThis.innerHeight = spec.inner_height;
globalThis.document = {
  documentElement: els[0], scrollingElement: els[0], body: els[1],
  querySelectorAll: all, querySelector: (s) => all(s)[0] || null,
};
globalThis.getComputedStyle = (el) => ({ overflowY: el._oy, visibility: 'visible', position: el._pos, top: el._top });
globalThis.NodeFilter = { SHOW_TEXT: 4 };
document.createTreeWalker = (root) => {
  const texts = els.filter((el) => el._text && root.contains(el)).map((el) => ({ data: el._text, parentElement: el }));
  let i = 0;
  return { nextNode: () => texts[i++] || null };
};
const fn = (0, eval)('(' + process.argv[2] + ')');
process.stdout.write(JSON.stringify(fn(JSON.parse(process.argv[3]))));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the page's own scripts cannot run here; "
                        "these must not pass by skipping")
        pytest.skip("node is not installed here; CI runs the page's scripts")
    return node


def _run_js(expression: str, dom: dict, arg) -> object:
    done = subprocess.run([_node(), "-e", _DOM_PROGRAM, json.dumps(dom), expression, json.dumps(arg)],
                          capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _console_dom(*, ready_inside: bool = True) -> dict:
    """The console's shape (Spine.tsx): the document does not scroll; the
    route's content scrolls inside `div.ctl-scroll`, under `div.sk-main`,
    beside a rail that scrolls a little of its own. `ready_inside=False`
    puts the route's ready element in a pane beside the scroller instead."""
    nodes = [
        {"tag": "html", "sh": 900, "ch": 900, "cw": 1440},
        {"tag": "body", "parent": 0, "sh": 900, "ch": 900, "cw": 1440},
        {"tag": "nav", "cls": "sk-rail", "parent": 1, "oy": "auto", "sh": 1300, "ch": 900, "cw": 64},
        {"tag": "div", "cls": "sk-main", "parent": 1, "oy": "hidden", "sh": 900, "ch": 900, "cw": 1376},
        {"tag": "div", "cls": "ctl-scroll", "parent": 3, "oy": "auto", "sh": 3000, "ch": 840, "cw": 1376},
        {"tag": "main", "parent": 4, "sh": 3000, "ch": 3000, "cw": 1376},
        {"tag": "h1", "parent": 5, "sh": 40, "ch": 40, "cw": 600},
    ]
    if not ready_inside:
        nodes.append({"tag": "div", "cls": "ag-split", "parent": 3, "sh": 60, "ch": 60, "cw": 1376})
    return {"inner_height": 900, "nodes": nodes}


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
        # A sticky header's height: once the scroller has scrolled, the top
        # `sticky` px of what it shows are under the header (SCROLL_JS
        # answers with it). `at` is where the scroller is, clamped as a
        # browser clamps scrollTop.
        self.sticky = 0
        self.at = 0
        self.slow = set(slow)
        self.scroll_height = scroll_height
        self.viewport_height = viewport_height
        self.url = "about:blank"
        self.log = []
        self.media = None
        self.clock = _Clock(self.log)
        self.handlers = {}
        self.closed = False
        # A DOM for MEASURE_JS to run against under node (`_run_js`); None
        # answers with `scroll_height` and `viewport_height` as before.
        self.dom = None
        # path -> how long, in ms after the load, the page shows a "reading…"
        # marker; None for a page that never stops reading.
        self.pending = {}
        self.elapsed = 0
        # What the console keeps in the profile across loads (localStorage):
        # the fake app writes one key per page it loaded, as RecentWorkflows
        # fills from storage after /workflows was opened.
        self.storage = {}
        self.init_scripts = []
        self.loaded_with = {}

    def set_viewport_size(self, *_a, **_kw):
        raise AssertionError("swarm_qa resized the page; the gate owns the size")

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kw):
        self.url = url
        self.elapsed = 0
        self.log.append(("goto", url, kw.get("wait_until")))
        reset = getattr(self.qa, "reset_state_script", None)
        origin = "/".join(url.split("/")[:3])
        if reset is not None and reset(origin) in self.init_scripts:
            self.storage.clear()
        self.loaded_with[url] = sorted(self.storage)
        self.storage["visited:" + url[len(origin):]] = "1"

    def add_init_script(self, script=None, **_kw):
        self.init_scripts.append(script)

    def wait_for_timeout(self, ms):
        self.elapsed += ms
        self.log.append(("wait", ms))

    def _path(self):
        return "/" + "/".join(self.url.split("/")[3:])

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
            if self.dom is not None:
                return _run_js(expression, self.dom, arg)
            return [self.scroll_height, self.viewport_height]
        if expression == getattr(self.qa, "PENDING_JS", None):
            if self._path() not in self.pending:
                return []
            clears = self.pending[self._path()]
            return ['"reading…" in button.c-refresh'] if clears is None or self.elapsed < clears else []
        if expression == self.qa.SCROLL_JS:
            self.log.append(("scroll", arg))
            self.at = max(0, min(arg, self.scroll_height - self.viewport_height))
            return self.sticky if self.at > 0 else 0
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
        self.log.append(("shot", self.url, (self.media or {}).get("color_scheme"), dict(kw), self.elapsed, self.at))
        return _png(3, 2, _white)

    def inner_text(self, selector, **_kw):
        return f"text of {self.url} under {selector}; storage at load {self.loaded_with.get(self.url)}"

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
# QA-TOOLS-FIX (part of #1649): the first visual-QA run's capture defects
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ready, ready_inside", [("main h1", True), (".ag-split", False)])
def test_a_page_whose_content_scrolls_inside_a_nested_div_is_captured_in_segments(qa, world, ready, ready_inside):
    """QA-003: the console's document does not scroll, `div.ctl-scroll` does.
    The scroller is the route's content's nearest scrolling ancestor or, when
    the content has none, the largest element that scrolls -- never a class
    named in the tool, and never the 64 px rail that scrolls a little too."""
    # MUTATION: look for a scroller only in the document, `main` and
    # `[role=dialog]` (MEASURE_JS before this lane) and every route is one
    # segment: 900 of 900 px, no room.
    routes = json.loads(world.routes.read_text(encoding="utf-8"))
    routes["groups"]["work-a"] = [{"id": "runs", "address": "work/runs", "path": "/runs", "ready": ready}]
    world.routes.write_text(json.dumps(routes), encoding="utf-8")
    world.page.dom = _console_dom(ready_inside=ready_inside)
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    (entry,) = _manifest(bundle)["routes"]
    # 3000 px of content in an 840 px scroller is four segments (the default cap).
    assert entry["images"] == [f"work-a/runs--desktop--light--s{i}.png" for i in (1, 2, 3, 4)], entry
    assert entry["scroller"] == "div.ctl-scroll", entry
    scrolls = [e[1] for e in world.page.log if e[0] == "scroll"]
    assert scrolls == [0, 840, 1680, 2520], "one scroller-height at a time, by the scroller's own height"


def test_a_page_whose_document_scrolls_is_still_measured_by_the_document(qa):
    dom = {"inner_height": 900, "nodes": [
        {"tag": "html", "sh": 2000, "ch": 900, "cw": 1440},
        {"tag": "body", "parent": 0, "sh": 2000, "ch": 2000, "cw": 1440},
        {"tag": "main", "parent": 1, "sh": 2000, "ch": 2000, "cw": 1440},
        {"tag": "h1", "parent": 2, "sh": 40, "ch": 40, "cw": 600},
    ]}
    assert _run_js(qa.MEASURE_JS, dom, "main h1") == [2000, 900, "document"]


def test_a_sticky_header_hides_no_row_between_two_segments(qa, world):
    """task-021 (visual QA run 3, wf_d598fa7bffdb41fc9d76): at 390 the list's
    55 px tab bar is sticky at the scroller's top, so a segment scrolled by
    the scroller's whole height shows its first 55 px of new rows under the
    bar, and no segment showed them. Each segment now overlaps the last by
    the height of what is stuck over the scroller's top, measured on the
    page (SCROLL_JS answers with it), never a number in the tool."""
    routes = json.loads(world.routes.read_text(encoding="utf-8"))
    routes["groups"]["work-a"] = [{"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1"}]
    world.routes.write_text(json.dumps(routes), encoding="utf-8")
    page = world.page
    page.scroll_height, page.viewport_height, page.sticky = 2000, 844, 55
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    (entry,) = _manifest(bundle)["routes"]
    at = [e[5] for e in page.log if e[0] == "shot"]
    assert len(at) == len(entry["images"]) and at[0] == 0, at
    # What each image shows clear of the header: all of it at the top, below
    # the header once scrolled.
    shown = [(y + (page.sticky if y > 0 else 0), y + page.viewport_height) for y in at]
    # MUTATION: scroll each segment by the scroller's whole height again
    # (`(segment - 1) * view`) and this fails: rows 844..899 are under the
    # bar in segment 2 and below the fold of segment 1.
    for (_, end), (start, _) in zip(shown, shown[1:]):
        assert start <= end, f"rows {end}..{start} are in no segment: {shown}"
    assert shown[-1][1] >= page.scroll_height, f"the page's last rows are in no segment: {shown}"
    # MUTATION: hard-code the overlap (55) and a page whose header is 80 px
    # skips 25 px a segment; with no header the segments do not overlap.
    page.log.clear()
    page.sticky = 0
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert [e[5] for e in page.log if e[0] == "shot"] == [0, 844, 1156], "no header, no overlap"


def _sticky_dom(*nodes) -> dict:
    """`_console_dom` at 390 with boxes on the screen: `div.ctl-scroll` starts
    60 px down, under the page head, and shows 784 px."""
    dom = _console_dom()
    for node in dom["nodes"]:
        if node.get("cls") == "ctl-scroll":
            node.update({"ch": 784, "rt": 60, "cw": 390})
    dom["nodes"].extend(nodes)
    return dom


def test_what_is_stuck_over_the_scroller_is_measured_on_the_page(qa):
    """STUCK_JS, which SCROLL_JS answers with: how far down from the
    scroller's top edge an element stuck there (`position: sticky` in this
    scroller, or `fixed`) covers it. A sticky head of a table that scrolls on
    its own, a sticky side rail as tall as the page and an element with no
    box cover no row of the page."""
    # Node 5 is `main` inside `div.ctl-scroll` (node 4); the table, when
    # given, is first, node 7, so its head's parent is 7.
    tabs = {"tag": "div", "cls": "ag-list-tabs", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 55}
    table = {"tag": "div", "cls": "c-tbl", "parent": 5, "oy": "auto", "sh": 900, "ch": 300, "rt": 60, "rh": 300}
    thead = {"tag": "thead", "parent": 7, "pos": "sticky", "top": 0, "rt": 60, "rh": 32}
    rail = {"tag": "aside", "cls": "rn-side", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 700}
    gone = {"tag": "div", "cls": "toast", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 90, "hidden": True}
    probe = "(ready) => { (" + qa.MEASURE_JS + ")(ready); return (" + qa.STUCK_JS + ")(); }"
    # MUTATION: return 0 from STUCK_JS and this fails: the tab bar is 55 px.
    assert _run_js(probe, _sticky_dom(table, thead, tabs, rail, gone), "main h1") == 55
    # A sticky element with a negative inset (`.ag-tabs-edge`, top: -18px)
    # covers what shows of it.
    edge = dict(tabs, cls="ag-tabs-edge", top=-18, rt=42, rh=73)
    assert _run_js(probe, _sticky_dom(edge), "main h1") == 55
    # MUTATION: count a sticky element that is not at its inset (not stuck,
    # further down the page) and this fails.
    assert _run_js(probe, _sticky_dom(dict(tabs, rt=400)), "main h1") == 0
    assert _run_js(probe, _sticky_dom(table, thead, rail, gone), "main h1") == 0
    # MUTATION: let SCROLL_JS resolve to null again and the capture never
    # learns the header's height.
    assert qa.STUCK_JS in qa.SCROLL_JS


def test_a_route_is_shot_only_after_its_reading_marker_clears(qa, world):
    """QA-027 (with 018, 023, 033, 034, 090): the preview build reads on
    timers, with no network traffic, so network idle and the ready selector
    are both met while the pane still says "reading…"."""
    # MUTATION: shoot as soon as the ready selector is visible (the capture
    # before this lane) and the /runs shots are taken at 0 ms, mid-read.
    world.page.pending = {"/runs": 600}
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    shots = [e for e in world.page.log if e[0] == "shot" and e[1].endswith("/runs")]
    assert shots and all(s[4] >= 600 for s in shots), [s[4] for s in shots]
    manifest = _manifest(bundle)
    runs = next(r for r in manifest["routes"] if r["id"] == "runs")
    assert runs["status"] == "captured" and runs["settled"] is True, runs
    assert manifest["failed"] == []


def test_a_route_that_never_settles_is_reported_and_its_wait_is_bounded(qa, world):
    # MUTATION: no settle wait, and the never-ending read is shot as if it
    # were the page, with nothing in `failed` to say so.
    world.page.pending = {"/timeline": None}
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0, "the other route was captured"
    manifest = _manifest(bundle)
    (failed,) = manifest["failed"]
    assert failed["id"] == "timeline" and "settle" in failed["error"] and "reading…" in failed["error"], failed
    (timeline,) = [r for r in manifest["routes"] if r["id"] == "timeline"]
    assert timeline["status"] == "unsettled" and timeline["settled"] is False, timeline
    assert timeline["pending"] == ['"reading…" in button.c-refresh'], timeline
    waited = sum(e[1] for e in world.page.log if e[0] == "wait")
    assert qa.SETTLE_TIMEOUT_MS <= waited <= qa.SETTLE_TIMEOUT_MS + qa.SETTLE_POLL_MS, waited


def test_the_pending_markers_are_what_the_page_draws(qa):
    """PENDING_JS, run: a drawn skeleton, a busy region, the head's refresh
    while it reads and a drawn "reading…" are pending; the same with no box
    (display: none) is not on the image, so is not."""
    # MUTATION: drop the `shown` check and the hidden skeleton keeps a
    # settled page "reading" until the timeout.
    def page(*extra):
        return {"inner_height": 900, "nodes": [
            {"tag": "html", "sh": 900, "ch": 900}, {"tag": "body", "parent": 0, "sh": 900, "ch": 900},
            {"tag": "main", "parent": 1, "text": "Runs"}, *extra]}

    assert _run_js(qa.PENDING_JS, page(), None) == []
    assert _run_js(qa.PENDING_JS, page({"tag": "div", "cls": "skeleton ctl-skeleton-row", "parent": 2, "hidden": True},
                                       {"tag": "span", "cls": "ov-reading", "parent": 2, "text": "reading…",
                                        "hidden": True}), None) == []
    assert _run_js(qa.PENDING_JS, page(
        {"tag": "div", "cls": "skeleton ctl-skeleton-row", "parent": 2},
        {"tag": "span", "cls": "ol-skel-bar is-static", "parent": 2},
        {"tag": "div", "cls": "wfl is-loading", "attrs": {"aria-busy": "true"}, "parent": 2},
        {"tag": "button", "cls": "c-refresh", "attrs": {"aria-label": "Reading"}, "parent": 2},
        {"tag": "span", "cls": "ov-reading", "parent": 2, "text": "tenant reading…"},
    ), None) == ["div.skeleton.ctl-skeleton-row", "span.ol-skel-bar.is-static", "div.wfl.is-loading",
                 "button.c-refresh", '"reading…" in span.ov-reading']


def test_the_pending_markers_are_the_consoles_own():
    """PENDING_JS looks for what the console draws while a read is in flight;
    a marker renamed there and not here is a capture that stops waiting."""
    qa = _module()
    styles = (REPO / "apps" / "swarm-ui" / "src" / "styles.css").read_text(encoding="utf-8")
    shell = (REPO / "apps" / "swarm-ui" / "src" / "Shell.tsx").read_text(encoding="utf-8")
    for cls in (".skeleton", ".ctl-pending", ".c-refresh"):
        assert cls in qa.PENDING_JS and re.search(re.escape(cls) + r"\b", styles), cls
    assert "aria-label={reading ? 'Reading'" in shell and "'reading…'" in shell
    assert "reading…" in qa.PENDING_JS and "aria-busy" in qa.PENDING_JS


def test_two_captures_in_a_row_start_from_the_same_clean_state(qa, world):
    """QA-077: the capture's tab is the live browser's one profile, so what
    one route left in storage (RecentWorkflows' list) drew the next route's
    sidebar, and the sidebar depended on capture order."""
    # MUTATION: drop the storage reset and the second route of each run sees
    # the first's key, and the second run sees the first run's.
    texts = []
    for _ in range(2):
        code, out, _bundle = _capture(qa, world)
        assert code == 0
        texts.append({p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
                      for p in sorted(out.rglob("*.txt"))})
    assert len(texts[0]) == 8 and texts[0] == texts[1], "two captures in a row differ"
    assert all(t.endswith("storage at load []") for t in texts[0].values()), texts[0]


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

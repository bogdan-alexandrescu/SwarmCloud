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


def _black(_x, _y):
    return (0, 0, 0, 255)


# --------------------------------------------------------------------------
# A DOM under node, for the scripts the capture evaluates in the page
# --------------------------------------------------------------------------

#: The page's scripts run against a DOM of boxes this program builds from a
#: list of nodes: what each scrolls (`sh` / `ch` / `cw`, scrollHeight,
#: clientHeight, clientWidth), its overflow-y, its tag, classes and
#: attributes, its own text, whether it has no box (`hidden`), its parent,
#: its CSS `position` and `top` (`pos`, `top` in px) and where its box is on
#: the screen (`rt` its top, `rh` its height; `ch` when not given), its
#: top padding (`pt`, px) and its CSS `bottom` inset (`bottom`, px); its
#: box's width is `rw` (`cw` when not given). `scrollTop` clamps as a
#: browser's does, to scrollHeight less clientHeight, and a script that
#: answers with a promise is awaited (SCROLL_JS waits two frames).
#: `innerText` is the drawn text of the element and everything under it.
#: Selectors are the shapes the scripts and the route list use: `tag`,
#: `.class`, `[attr="v"]`, `[attr*="v"]`, `*`, descendant and comma.
_DOM_PROGRAM = r"""
const spec = JSON.parse(process.argv[1]);
const els = spec.nodes.map((n) => ({
  localName: n.tag, tagName: n.tag.toUpperCase(), id: n.id || '', className: n.cls || '',
  classList: (n.cls || '').split(' ').filter(Boolean), _attrs: n.attrs || {}, _oy: n.oy || 'visible',
  scrollHeight: n.sh || 0, clientHeight: n.ch || 0, clientWidth: n.cw || 0, scrollTop: 0, parentElement: null,
  _text: n.text || '', _hidden: n.hidden || false, _pos: n.pos || 'static',
  _top: n.top === undefined ? 'auto' : n.top + 'px', _rt: n.rt || 0, _rh: n.rh === undefined ? (n.ch || 0) : n.rh,
  _pt: (n.pt || 0) + 'px', _bottom: n.bottom === undefined ? 'auto' : n.bottom + 'px',
}));
spec.nodes.forEach((n, i) => {
  els[i].parentElement = n.parent === undefined ? null : els[n.parent];
  els[i]._rw = n.rw === undefined ? (n.cw || 0) : n.rw;
  let at = 0;
  Object.defineProperty(els[i], 'scrollTop', {
    get: () => at,
    set: (v) => { at = Math.max(0, Math.min(v, els[i].scrollHeight - els[i].clientHeight)); },
  });
});
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
  el.getBoundingClientRect = () => ({ top: el._rt, bottom: el._rt + el._rh, height: el._rh,
    left: 0, right: el._rw, width: el._rw });
  Object.defineProperty(el, 'innerText', { get: () => els.filter((o) => !o._hidden && el.contains(o) && o._text)
    .map((o) => o._text).join('\n') });
});
globalThis.window = globalThis;
globalThis.innerHeight = spec.inner_height;
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
if (spec.visual_scale) globalThis.visualViewport = { scale: spec.visual_scale };
globalThis.document = {
  documentElement: els[0], scrollingElement: els[0], body: els[1],
  querySelectorAll: all, querySelector: (s) => all(s)[0] || null,
};
globalThis.getComputedStyle = (el) => ({ overflowY: el._oy, visibility: 'visible', position: el._pos, top: el._top,
  bottom: el._bottom, paddingTop: el._pt, borderTopWidth: '0px' });
globalThis.NodeFilter = { SHOW_TEXT: 4 };
document.createTreeWalker = (root) => {
  const texts = els.filter((el) => el._text && root.contains(el)).map((el) => ({ data: el._text, parentElement: el }));
  let i = 0;
  return { nextNode: () => texts[i++] || null };
};
const fn = (0, eval)('(' + process.argv[2] + ')');
Promise.resolve(fn(JSON.parse(process.argv[3]))).then((v) => process.stdout.write(JSON.stringify(v)));
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


class _Response:
    """What `page.goto` answers for a full document load, and what a
    `response` listener is handed."""

    def __init__(self, url, status):
        self.url = url
        self.status = status


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
        # A bar fixed over the scroller's bottom edge (the Reads bar): the
        # bottom `bottom` px of what it shows are under it, at every offset.
        self.bottom = 0
        self.at = 0
        # The scroller stops here whatever it is asked (a page whose
        # scrollHeight runs past what it lets scroll: run 6's 6:capacity-005);
        # None scrolls to scroll_height less viewport_height, as a browser.
        self.max_top = None
        # The scroller's scrollHeight once it has scrolled (content read in
        # as it scrolls); None keeps scroll_height.
        self.grow_to = None
        # How much of the scroller the screen shows once it has scrolled
        # (STUCK_JS's third number: a scroller taller than innerHeight shows
        # less than its clientHeight); None is viewport_height.
        self.visible = None
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
        # path -> how long, in ms after the load, the body text goes on
        # changing (a read landing with no marker: an attempt count, a cost,
        # a lane's events); None for text that never stops changing.
        self.text_changes = {}
        self.elapsed = 0
        # What the console keeps in the profile across loads (localStorage):
        # the fake app writes one key per page it loaded, as RecentWorkflows
        # fills from storage after /workflows was opened.
        self.storage = {}
        self.init_scripts = []
        self.loaded_with = {}
        # Every script the capture evaluated, in order (VQ5-CAPTURE-GENERIC:
        # `--settle generic` must never ask the console's PENDING_JS).
        self.evaluated = []
        # path -> how long, in ms after the load, the DOM goes on changing
        # (DOM_CHANGES_JS answers a new count each poll until then); None
        # for a page that never stops changing.
        self.mutations = {}
        # path -> the hrefs LINKS_JS answers with on that page (discover).
        self.links = {}
        # True: a `document.cookie` write raises (QA-PERSONA-COOKIE: the
        # persona step's own failure is reported as that step).
        self.cookie_fails = False
        # path -> the HTTP status its main document answers (200 when
        # absent). A same-document navigation (only the fragment differs)
        # answers None, as Playwright's `goto` does.
        self.statuses = {}
        # path -> [(url, status)] the page's subresources answer, handed to
        # every `response` listener on a full load of it.
        self.subresources = {}
        # (width, height) of the first screenshot after a full document
        # load, when it differs from the viewport (QA-GENERIC-GAPS: at
        # phone-390 it came out 980x2121 with the page drawn small); None
        # for a page whose every shot is the viewport's size, 3x2.
        self.first_shot = None
        self.fresh = False
        # (width, height) of every screenshot (run 5b's admin phone shots,
        # 394x852 against the reading); None is first_shot's rule.
        self.every_shot = None
        # True: the app answers prefers-color-scheme: dark, so a dark shot is
        # drawn dark; False draws every shot the same whatever the scheme (an
        # app with no dark theme, #1981 comment 8).
        self.dark_theme = False

    def set_viewport_size(self, *_a, **_kw):
        raise AssertionError("swarm_qa resized the page; the gate owns the size")

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kw):
        same_document = "#" in url and url.split("#")[0] == self.url.split("#")[0]
        self.url = url
        self.elapsed = 0
        self.log.append(("goto", url, kw.get("wait_until")))
        origin = "/".join(url.split("/")[:3])
        # An init script runs on a new document only: a same-document
        # navigation (a hash route) keeps what the route before it left.
        if not same_document:
            self._run_init_scripts(origin)
        self.loaded_with[url] = sorted(self.storage)
        self.storage["visited:" + url[len(origin):]] = "1"
        if same_document:
            return None
        self.fresh = True
        for sub_url, status in self.subresources.get(self._path(), []):
            for handler in self.handlers.get("response", []):
                handler(_Response(sub_url, status))
        return _Response(url, self.statuses.get(self._path(), 200))

    def _run_init_scripts(self, origin):
        reset = getattr(self.qa, "reset_state_script", None)
        if reset is not None and reset(origin) in self.init_scripts:
            self.storage.clear()

    def reload(self, **kw):
        """A full load of the document the page shows: its init scripts run."""
        self.elapsed = 0
        self.log.append(("reload", self.url, kw.get("wait_until")))
        origin = "/".join(self.url.split("/")[:3])
        self._run_init_scripts(origin)
        self.loaded_with[self.url] = sorted(self.storage)
        self.storage["visited:" + self.url[len(origin):]] = "1"
        self.fresh = True
        return _Response(self.url, self.statuses.get(self._path(), 200))

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
        self.evaluated.append(expression)
        if "document.cookie" in expression:
            # The page's own write, on whatever document it shows now: the
            # gate allows it, as it allows every other page.evaluate. The
            # cookie text is the argument when there is one, else the script.
            self.log.append(("document.cookie", self.url, arg if isinstance(arg, str) else expression))
            if self.cookie_fails:
                raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
            return None
        if expression == getattr(self.qa, "DOM_CHANGES_JS", None):
            stops = self.mutations.get(self._path(), 0)
            if stops is None or self.elapsed < stops:
                return [self.elapsed // 10 + 1, "div.feed"]
            return [stops // 10 + 1 if stops else 0, "div.feed" if stops else None]
        if expression == getattr(self.qa, "LINKS_JS", None):
            return list(self.links.get(self._path(), []))
        if expression == self.qa.FONTS_READY_JS:
            return True
        # QA-SHOT-MASK: a page whose text the gate scrubbed of nothing, so
        # nothing is masked (test_swarm_qa_shot_mask drives the mask).
        if expression == getattr(self.qa, "MASK_FIND_JS", None):
            return []
        if expression == getattr(self.qa, "MASK_CLEAR_JS", None):
            return [True, 0]
        if expression == getattr(self.qa, "VIEWPORT_JS", None):
            return [3, 2, 1]
        if expression == self.qa.MEASURE_JS:
            if self.dom is not None:
                return _run_js(expression, self.dom, arg)
            return [self.scroll_height, self.viewport_height]
        if expression == getattr(self.qa, "PENDING_JS", None):
            if self._path() not in self.pending:
                return []
            clears = self.pending[self._path()]
            return ['"reading…" in button.c-refresh'] if clears is None or self.elapsed < clears else []
        if expression == getattr(self.qa, "TEXT_JS", None):
            path = self._path()
            if path in self.text_changes:
                stops = self.text_changes[path]
                if stops is None or self.elapsed < stops:
                    # A different count on every poll, the same length.
                    return [24, format(self.elapsed, "08x"), "span.tl-unread"]
            return [24, "0000ffff", None]
        if expression == self.qa.SCROLL_JS:
            self.log.append(("scroll", arg))
            if arg > 0 and self.grow_to is not None:
                self.scroll_height = self.grow_to
            room = self.scroll_height - self.viewport_height
            self.at = max(0, min(arg, room if self.max_top is None else min(room, self.max_top)))
            top = self.sticky if self.at > 0 else 0
            visible = self.visible if self.visible is not None and self.at > 0 else self.viewport_height
            # STUCK_JS's `[top, bottom, view]`, then where the scroller is,
            # its scrollHeight and what was measured around it.
            return [top, self.bottom, visible, self.at, self.scroll_height,
                    {"scroller": [0, visible], "inner": visible, "dock": None}]
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
        if self.every_shot is not None:
            return _png(*self.every_shot, _white)
        if self.fresh and self.first_shot is not None:
            self.fresh = False
            return _png(*self.first_shot, _white)
        self.fresh = False
        if self.dark_theme and (self.media or {}).get("color_scheme") == "dark":
            return _png(3, 2, _black)
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

    def add_cookies(self, cookies):
        # Refused, as the live gate refuses it: Playwright sends this over CDP
        # as Storage.setCookies and the gate never exposes the cookie APIs
        # (#1980: console QA run 5 failed every route here). Logged first, so
        # a test can also say no call was made.
        self.page.log.append(("cookies", [dict(c) for c in cookies]))
        raise RuntimeError("Protocol error (Storage.setCookies): the live gate refuses the cookie APIs")

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
    # The gate's refusal record (QA-GATE-REFUSALS): a gate holding no QA
    # target, read without a socket; test_swarm_qa_gate_refusals drives it.
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": False, "refusals": [], "dropped": 0})

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
    # At phone-390 `timeline` runs to its scroller's end all the same (owner
    # decision c49): three images there, one on the desktop.
    assert [n for n in names if n.startswith("work-a/timeline--phone-390--light--")] == [
        f"work-a/timeline--phone-390--light--s{i}.png" for i in (1, 2, 3)
    ]
    assert len(names) == 2 * (3 + 1) + 2 * (3 + 3)
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
    world.page.scroll_height, world.page.viewport_height = 3000, 840
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
    tabs = {"tag": "div", "cls": "ag-list-tabs", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 55, "rw": 390}
    table = {"tag": "div", "cls": "c-tbl", "parent": 5, "oy": "auto", "sh": 900, "ch": 300, "rt": 60, "rh": 300}
    thead = {"tag": "thead", "parent": 7, "pos": "sticky", "top": 0, "rt": 60, "rh": 32}
    rail = {"tag": "aside", "cls": "rn-side", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 700}
    gone = {"tag": "div", "cls": "toast", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 90, "hidden": True}
    probe = "(ready) => { (" + qa.MEASURE_JS + ")(ready); return (" + qa.STUCK_JS + ")(); }"
    # MUTATION: return 0 from STUCK_JS and this fails: the tab bar is 55 px.
    # It answers `[top, bottom]` (QAFIX4-QATOOL): nothing covers the bottom here.
    assert _run_js(probe, _sticky_dom(table, thead, tabs, rail, gone), "main h1") == [55, 0, 784]
    # A sticky element with a negative inset (`.ag-tabs-edge`, top: -18px)
    # covers what shows of it.
    edge = dict(tabs, cls="ag-tabs-edge", top=-18, rt=42, rh=73)
    assert _run_js(probe, _sticky_dom(edge), "main h1") == [55, 0, 784]
    # MUTATION: count a sticky element that is not at its inset (not stuck,
    # further down the page) and this fails.
    assert _run_js(probe, _sticky_dom(dict(tabs, rt=400)), "main h1") == [0, 0, 784]
    assert _run_js(probe, _sticky_dom(table, thead, rail, gone), "main h1") == [0, 0, 784]
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
    # `timeline` asks for one segment of a 2000 px page, so it is in `failed`
    # as cut (QAFIX4-QATOOL); /runs, which settled and ran to its end, is not.
    assert [f for f in manifest["failed"] if f["id"] == "runs"] == []


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
    # The timeline is the group's last route: its wait is every wait after its load.
    last_load = max(i for i, e in enumerate(world.page.log) if e[0] == "goto")
    waited = sum(e[1] for e in world.page.log[last_load:] if e[0] == "wait")
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
# QAFIX4-QATOOL (part of #1680): visual QA run 4's capture defects
# --------------------------------------------------------------------------


def _one_route(world, **extra):
    routes = json.loads(world.routes.read_text(encoding="utf-8"))
    routes["groups"]["work-a"] = [{"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1", **extra}]
    world.routes.write_text(json.dumps(routes), encoding="utf-8")


def test_a_phone_capture_runs_to_the_scrollers_end_whatever_the_routes_budget(qa, world):
    """The owner's decision c49 (2026-10-22, box comment 6283704756): run 4's
    phone captures stopped at their route's budget (4, or 3 for `now`) before
    the page ended, on six routes in both themes. At phone-390 the capture
    takes segments until the scroller's end, up to MAX_SEGMENTS."""
    _one_route(world, segments=4)
    page = world.page
    page.scroll_height, page.viewport_height = 9 * 844, 844
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    (entry,) = manifest["routes"]
    # MUTATION: keep the route's `segments` as the phone-390 budget and this
    # fails: four images of a nine-viewport page.
    assert len(entry["images"]) == 9, entry["images"]
    # MUTATION: write `truncated: True` for every entry and this fails: the
    # ninth segment reached the scroller's end.
    assert entry["truncated"] is False and entry["reached"] == entry["total"] == 9 * 844, entry
    assert manifest["failed"] == []


def test_a_phone_capture_cut_at_max_segments_says_so(qa, world, capsys):
    _one_route(world, segments=4)
    page = world.page
    page.scroll_height, page.viewport_height = 15 * 844, 844
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    (entry,) = manifest["routes"]
    # MUTATION: drop MAX_SEGMENTS from the phone budget (scroll until the end
    # with no bound) and this fails: fifteen images.
    assert len(entry["images"]) == qa.MAX_SEGMENTS == 12, entry["images"]
    # MUTATION: leave the entry as it was (no `truncated`) and this fails: a
    # cut capture recorded as complete.
    assert entry["truncated"] is True and entry["reached"] == 12 * 844 and entry["total"] == 15 * 844, entry
    # MUTATION: record the cut only in the entry and this fails: the summary
    # lists it, as it lists an unsettled route.
    (failed,) = manifest["failed"]
    assert failed["id"] == "runs" and "cut" in failed["error"] and "10128 of 12660 px" in failed["error"], failed
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["truncated"] == 1


def test_a_desktop_capture_keeps_its_routes_budget_and_says_when_it_cut(qa, world):
    _one_route(world, segments=4)
    page = world.page
    page.scroll_height, page.viewport_height = 9 * 900, 900
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    (entry,) = _manifest(bundle)["routes"]
    # MUTATION: give every preset MAX_SEGMENTS and this fails: nine images.
    assert len(entry["images"]) == 4, entry["images"]
    # MUTATION: set `truncated` only for phone-390 and this fails.
    assert entry["truncated"] is True and entry["reached"] == 4 * 900 and entry["total"] == 9 * 900, entry
    assert qa.segment_budget({"segments": 4}, "desktop") == 4
    assert qa.segment_budget({}, "desktop") == qa.DEFAULT_SEGMENTS
    assert qa.segment_budget({"segments": 3}, "phone-390") == qa.MAX_SEGMENTS


def test_the_380_phone_is_captured_to_the_end_as_the_390_one_is(qa):
    """PHONE-380 (BGD 2026-10-26, part of #2136): AI Studio's 380 x 800 phone
    frame is a phone at least as tall per page as phone-390, so owner decision
    c49's reason -- phone captures stopped at their budget before the page
    ended -- holds for it too."""
    # MUTATION: leave phone-380 out of TO_THE_END_PRESETS and this fails.
    assert qa.segment_budget({"segments": 3}, "phone-380") == qa.MAX_SEGMENTS
    from agent_worker.livegate.presets import PRESETS

    assert qa.TO_THE_END_PRESETS <= set(PRESETS), "a preset the gate does not have"


def test_a_page_whose_text_keeps_changing_is_shot_only_once_it_stops(qa, world):
    """Box comment 6283709068 and 6283730057 (run 4's task-007, work-a-001,
    work-a-002, work-a-003): the agent list's 'attempt … of 3', the Workflows
    Cost's '—' and the Timeline's 'events not read on N lanes' were shot
    while their reads landed, with no skeleton, `aria-busy` or "reading…" on
    the page, and recorded settled. The page now counts as settled only once
    its body text has stopped changing for TEXT_QUIET_MS."""
    _one_route(world)
    world.page.text_changes = {"/runs": 3000}
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    shots = [e[4] for e in world.page.log if e[0] == "shot"]
    # MUTATION: settle on PENDING_JS alone (no TEXT_JS) and this fails: the
    # shots are taken at 300 ms, mid-read.
    # MUTATION: set TEXT_QUIET_MS to 0 and this fails the same way.
    assert shots and all(t >= 3000 + qa.TEXT_QUIET_MS for t in shots), shots
    (entry,) = _manifest(bundle)["routes"]
    assert entry["settled"] is True and entry["status"] == "captured", entry
    # The window is longer than the fixture build's longest single timer
    # (620 ms, api.ts) and leaves the Timeline's ~4.5 s of lane reads room
    # under the timeout.
    assert 620 < qa.TEXT_QUIET_MS and 4500 + qa.TEXT_QUIET_MS < qa.SETTLE_TIMEOUT_MS


def test_a_page_whose_text_never_stops_changing_is_shot_unsettled_and_says_where(qa, world):
    _one_route(world)
    world.page.text_changes = {"/runs": None}
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    (entry,) = manifest["routes"]
    # MUTATION: return [] from `_settle` when only the text is still moving
    # at SETTLE_TIMEOUT_MS and this fails: the page is recorded settled.
    assert entry["settled"] is False and entry["status"] == "unsettled", entry
    (what,) = entry["pending"]
    assert "text still changing" in what and "span.tl-unread" in what, what
    assert "text of" not in what, "the page's own text is never written into the manifest"
    (failed,) = manifest["failed"]
    assert "settle" in failed["error"] and "text still changing" in failed["error"], failed
    waited = sum(e[1] for e in world.page.log if e[0] == "wait")
    assert qa.SETTLE_TIMEOUT_MS <= waited <= qa.SETTLE_TIMEOUT_MS + qa.SETTLE_POLL_MS, waited


def test_the_text_fingerprint_moves_with_the_drawn_text_and_carries_none_of_it(qa):
    """TEXT_JS, run: its answer is the body text's length and hash and where
    it last changed, so the settle loop compares two polls without the
    page's text ever leaving the page."""
    dom = {"inner_height": 900, "nodes": [
        {"tag": "html", "sh": 900, "ch": 900}, {"tag": "body", "parent": 0, "sh": 900, "ch": 900},
        {"tag": "main", "parent": 1, "text": "Agents"},
        {"tag": "span", "cls": "ag-attempt", "parent": 2, "text": "attempt … of 3"},
        {"tag": "span", "cls": "skeleton", "parent": 2, "text": "hidden", "hidden": True},
    ]}
    probe = ("() => { const f = (" + qa.TEXT_JS + "); const a = f();"
             " document.querySelector('span.ag-attempt')._text = 'attempt 3 of 3';"
             " const b = f(); const c = f(); return [a, b, c]; }")
    a, b, c = _run_js(probe, dom, None)
    # MUTATION: answer a constant and this fails: the landed read is not seen.
    assert a[:2] != b[:2], (a, b)
    # MUTATION: hash anything that moves between polls (a counter, the time) and this fails.
    assert b[:2] == c[:2], (b, c)
    assert a[0] == len("Agents\nattempt … of 3"), a
    assert "attempt" not in json.dumps([a, b, c]), "the fingerprint carries the page's text"


def _drawer_dom(*extra) -> dict:
    """Task detail at 390 (run 4's task-006): the phone header is 44 px, the
    open task `div.drawer.ctl-drawer.ag-split` is fixed under it and is the
    scroller, with the drawer's 18 px of top padding; its tab strip
    (`.ag-tabs-edge`, agents.css) is sticky at `top: -18px`, so stuck it sits
    at the scroller's top edge; the Reads bar is fixed at the screen's foot,
    over the scroller's bottom 45 px."""
    return {"inner_height": 844, "nodes": [
        {"tag": "html", "sh": 844, "ch": 844, "cw": 390},
        {"tag": "body", "parent": 0, "sh": 844, "ch": 844, "cw": 390},
        {"tag": "div", "cls": "drawer ctl-drawer ag-split", "parent": 1, "oy": "auto", "pos": "fixed",
         "sh": 3000, "ch": 772, "cw": 390, "rt": 44, "pt": 18},
        {"tag": "div", "cls": "ag-tabs-edge", "parent": 2, "pos": "sticky", "top": -18, "rt": 44, "rh": 45, "cw": 390},
        {"tag": "div", "cls": "ag-split-pane", "parent": 2, "sh": 2900, "ch": 2900, "rt": 89},
        {"tag": "main", "parent": 4, "sh": 2900, "ch": 2900, "rt": 89},
        {"tag": "h1", "parent": 5, "sh": 40, "ch": 40, "rt": 89},
        {"tag": "div", "cls": "ctl-reads", "parent": 1, "pos": "fixed", "bottom": 0, "rt": 771, "rh": 73},
        *extra,
    ]}


def test_a_strip_stuck_inside_a_drawer_and_a_fixed_bar_over_its_foot_are_measured(qa):
    """Box comment 6314118924 (run 4's task-006): the drawer's tab strip and
    the Reads bar hid rows from every segment and the manifest said
    overlaps [0, 0]. STUCK_JS measured the strip's inset from the scroller's
    border edge, 18 px above where a browser sticks it (from the padding
    edge), and counted nothing at the scroller's foot."""
    probe = "(ready) => { (" + qa.MEASURE_JS + ")(ready); return (" + qa.STUCK_JS + ")(); }"
    toast = {"tag": "div", "cls": "toast", "parent": 1, "pos": "fixed", "rt": 300, "rh": 40}
    # MUTATION: measure a sticky inset from the scroller's border edge again
    # (`top + inset`) and the first number is 0: the strip is not counted.
    # MUTATION: count nothing over the scroller's bottom edge and the second is 0.
    # A fixed box clear of the bottom edge (the toast mid-screen) covers none of it.
    # MUTATION: count every fixed box below the top edge as a bottom cover and
    # the second number is 516.
    assert _run_js(probe, _drawer_dom(toast), "main h1") == [45, 45, 772]
    no_bar = _drawer_dom()
    no_bar["nodes"] = [n for n in no_bar["nodes"] if n.get("cls") != "ctl-reads"]
    assert _run_js(probe, no_bar, "main h1") == [45, 0, 772]


def test_a_bar_fixed_over_the_scrollers_foot_shortens_each_step(qa, world):
    """Each step is the visible height less what is stuck at the top and
    what covers the bottom, so the rows under the Reads bar in one segment
    are clear in the next; `overlaps` is still one number per step (both
    together, the px the next segment repeats), and `covers` splits it."""
    _one_route(world)
    page = world.page
    page.scroll_height, page.viewport_height, page.sticky, page.bottom = 3000, 772, 45, 45
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    (entry,) = _manifest(bundle)["routes"]
    at = [e[5] for e in page.log if e[0] == "shot"]
    shown = [(y + (page.sticky if y > 0 else 0), y + page.viewport_height - page.bottom) for y in at]
    # MUTATION: step by the visible height less only the top (QAFIX3) and
    # rows 727..772 are under the bar in segment 1 and above segment 2.
    for (_, end), (start, _) in zip(shown, shown[1:]):
        assert start <= end, f"rows {end}..{start} are in no segment: {shown}"
    assert entry["overlaps"] == [90] * (len(at) - 1), entry
    assert entry["covers"] == [{"top": 45, "bottom": 45}] * (len(at) - 1), entry


# --------------------------------------------------------------------------
# QAFIX6-CAPTURE (part of #1680): visual QA run 6, wf_31aca00c977e4d37a935
# --------------------------------------------------------------------------


def test_a_scroller_that_stops_short_is_reported_truncated_with_no_second_image_of_its_frame(qa, world):
    """6:capacity-005 (and 6:admin-001, -002, -003, box 6343796637): Holders'
    dark phone capture ended at the 'mock' row while the manifest said
    reached 4527 of 4527, truncated false, beside byte-identical shots.
    `reached` was the offset the capture asked for; it is now the one the
    scroller took, read back after each scroll."""
    _one_route(world)
    page = world.page
    page.scroll_height, page.viewport_height = 9 * 844, 844
    page.max_top = 2 * 844 + 100
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    (entry,) = manifest["routes"]
    at = [e[5] for e in page.log if e[0] == "shot"]
    # MUTATION: set `y` from the requested offset again (`y = nxt`) and this
    # fails: nine shots, the last six of the same clamped frame.
    assert len(at) == len(set(at)) == len(entry["images"]) == 4, at
    # MUTATION: compute `reached` from the requested offset and it reads
    # 7596 of 7596, truncated false.
    assert entry["truncated"] is True and entry["reached"] == page.max_top + 844, entry
    assert entry["duplicate"] == {"segment": 5, "at": page.max_top}, entry
    (failed,) = manifest["failed"]
    assert "stopped" in failed["error"] and f"{page.max_top + 844} of {9 * 844} px" in failed["error"], failed


def test_a_scroller_that_grows_while_it_is_captured_is_followed_to_its_new_end(qa, world):
    """The scroller's height is read back with every scroll, so content read
    in as the page scrolls is not left out under a 'reached N of N'."""
    _one_route(world)
    page = world.page
    page.scroll_height, page.viewport_height = 2 * 844, 844
    page.grow_to = 4 * 844
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    (entry,) = _manifest(bundle)["routes"]
    # MUTATION: keep MEASURE_JS's total for the whole capture and this
    # fails: two images, reached 1688 of 1688.
    assert entry["total"] == entry["reached"] == 4 * 844 and entry["truncated"] is False, entry
    assert len(entry["images"]) == 4, entry


def test_the_scroll_answers_with_where_the_scroller_went(qa):
    """SCROLL_JS, under node: a scroller asked past its end answers with the
    offset it clamped to, its scrollHeight, and what the screen shows."""
    probe = "(y) => { (" + qa.MEASURE_JS + ")('main h1'); return (" + qa.SCROLL_JS + ")(y); }"
    answer = _run_js(probe, _console_dom(), 5000)
    # MUTATION: answer only STUCK_JS's numbers and the capture cannot tell a
    # clamped scroll from one that moved.
    assert answer[3] == 3000 - 840 and answer[4] == 3000, answer
    assert answer[5]["inner"] == 900 and answer[5]["dock"] is None, answer


def _tall_dom(*extra) -> dict:
    """At 390 the scroller `div.ctl-scroll` starts under the 44 px header and
    is 900 px tall, past the window's 844 px foot; the Reads bar is fixed
    over the window's last 73 px."""
    return {"inner_height": 844, "nodes": [
        {"tag": "html", "sh": 844, "ch": 844, "cw": 390},
        {"tag": "body", "parent": 0, "sh": 844, "ch": 844, "cw": 390},
        {"tag": "div", "cls": "ctl-scroll", "parent": 1, "oy": "auto", "sh": 3000, "ch": 900, "cw": 390, "rt": 44},
        {"tag": "main", "parent": 2, "sh": 3000, "ch": 3000, "rt": 44},
        {"tag": "h1", "parent": 3, "sh": 40, "ch": 40, "rt": 44},
        *extra,
    ]}


def test_a_bar_over_the_windows_foot_counts_when_the_scroller_runs_past_it(qa):
    """6:capacity-001 to -004, 6:admin-005 (box 6283704756, QA-003): the
    Reads bar is fixed at the window's foot, but the scroller's own foot is
    below the window, so the bar spanned no edge STUCK_JS looked at and the
    rows under it, and those below the window, were in no image."""
    probe = "(ready) => { (" + qa.MEASURE_JS + ")(ready); return (" + qa.STUCK_JS + ")(); }"
    bar = {"tag": "div", "cls": "ctl-dock", "parent": 1, "pos": "fixed", "bottom": 0, "rt": 771, "rh": 73}
    # MUTATION: take the foot as `top + clientHeight` again and this reads
    # [0, 0, 900]: no cover, and 56 px below the window counted as shown.
    assert _run_js(probe, _tall_dom(bar), "main h1") == [0, 73, 800]


def test_a_strip_stuck_to_the_scrollers_foot_counts_as_a_foot_cover(qa):
    """6:work-b-001 (boxes 6343797451, 6314118924): /submit's 'Not ready'
    strip is `position: sticky; bottom: 0` and rests on the scroller's foot;
    the bands under it were skipped while covers read 0."""
    probe = "(ready) => { (" + qa.MEASURE_JS + ")(ready); return (" + qa.STUCK_JS + ")(); }"
    dom = _sticky_dom({"tag": "div", "cls": "sf-strip", "parent": 5, "pos": "sticky", "bottom": 0,
                       "rt": 844 - 52, "rh": 52, "rw": 390})
    # MUTATION: count only `fixed` boxes over the foot and this reads 0.
    assert _run_js(probe, dom, "main h1") == [0, 52, 784]
    # Further up the page, not resting on the foot, it covers nothing.
    dom["nodes"][-1]["rt"] = 400
    assert _run_js(probe, dom, "main h1") == [0, 0, 784]


def test_each_step_shortens_by_what_the_screen_does_not_show(qa, world):
    """A scroller that shows less than its clientHeight once scrolled (the
    phone bar compacting, a scroller past the window's foot): each step is
    what the screen showed clear, re-measured after every scroll."""
    _one_route(world)
    page = world.page
    page.scroll_height, page.viewport_height, page.sticky, page.bottom = 4000, 900, 45, 73
    page.visible = 800
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    at = [e[5] for e in page.log if e[0] == "shot"]
    shown = [(y + (page.sticky if y > 0 else 0), y + (page.visible if y > 0 else 900) - page.bottom) for y in at]
    # MUTATION: keep MEASURE_JS's view for every step and each later step
    # runs 100 px past what the screen showed.
    for (_, end), (start, _) in zip(shown, shown[1:]):
        assert start <= end, f"rows {end}..{start} are in no segment: {shown}"


def test_a_side_column_stuck_at_the_top_covers_no_row(qa):
    """6:work-b-002: the Submit forms' 300 px sticky side panel at 1440 was
    counted as a cover over the scroller's top (overlaps of 371 and 335 px).
    Only what spans at least half the scroller's width covers its rows."""
    probe = "(ready) => { (" + qa.MEASURE_JS + ")(ready); return (" + qa.STUCK_JS + ")(); }"
    dom = _console_dom()
    for node in dom["nodes"]:
        if node.get("cls") == "ctl-scroll":
            node.update({"cw": 1100, "rt": 60})
    aside = {"tag": "aside", "cls": "sf-side", "parent": 5, "pos": "sticky", "top": 0, "rt": 60, "rh": 371, "rw": 300}
    dom["nodes"].append(aside)
    # MUTATION: drop the width check and this reads 371.
    assert _run_js(probe, dom, "main h1")[0] == 0
    # A bar across the scroller still counts (run 3's task-021 tab bar).
    dom["nodes"][-1].update({"rh": 55, "rw": 1100})
    assert _run_js(probe, dom, "main h1")[0] == 55


def test_a_shot_still_off_size_after_a_device_reset_leaves_the_route_unsettled(qa, world):
    """Box 6343795935 (run 5b's 5b:QA-001): every admin phone-390 segment
    came back 394x852 against its reading and was kept as it came, under a
    `captured` status. PR 1998 has the gate put the device back after a
    clipped capture, and the shots were still off; so after its retries a
    shot is taken once more after `swarm-handoff device <preset>`, and one
    still off leaves the route unsettled."""
    _one_route(world)
    world.page.every_shot = (394, 852)
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    (entry,) = manifest["routes"]
    # MUTATION: keep the status `captured` beside an off-size segment and
    # the report counts the route as clean.
    assert entry["off_viewport"] and entry["status"] != "captured", entry
    # MUTATION: never reset the device and only the preset's own handoff runs.
    assert world.handoff_calls().count("device phone-390") == 1 + len(entry["images"]), world.handoff_calls()
    (failed,) = manifest["failed"]
    assert "394x852" in failed["error"], failed


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


# --------------------------------------------------------------------------
# QAFIX4-FIXTURE-A (part of #1680): the capture persona (F4, F5)
# --------------------------------------------------------------------------


def _persona_routes(world, routes):
    document = json.loads(world.routes.read_text(encoding="utf-8"))
    document["groups"]["work-a"] = routes
    world.routes.write_text(json.dumps(document), encoding="utf-8")


def test_load_routes_accepts_the_two_personas_and_refuses_any_other(qa, world):
    _persona_routes(world, [
        {"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1", "persona": "platform-admin"},
        {"id": "timeline", "address": "work/timeline", "path": "/timeline", "ready": "main h1", "persona": "member"},
    ])
    document = qa.load_routes(world.routes)
    assert [r.get("persona") for r in document["groups"]["work-a"]] == ["platform-admin", "member"]
    # MUTATION: drop the persona check from load_routes and this does not raise.
    for bad in ("admin", "Platform-Admin", "", 1, None):
        _persona_routes(world, [{"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1", "persona": bad}])
        with pytest.raises(qa.UsageError, match="persona"):
            qa.load_routes(world.routes)
    # MUTATION: rename either value in PERSONAS and the console's cookie reader
    # (api.ts) no longer understands what the capture sets.
    assert qa.PERSONAS == ("member", "platform-admin")
    assert qa.PERSONA_COOKIE == "swarm_qa_persona"


CONSOLE = "http://localhost:5173"


def _origin(url):
    return "/".join(url.split("/")[:3])


def _persona_writes(world):
    """(log index, the page's URL, the cookie text) of every page write of
    PERSONA_COOKIE (QA-PERSONA-COOKIE, #1980)."""
    return [(i, e[1], e[2]) for i, e in enumerate(world.page.log)
            if e[0] == "document.cookie" and e[2].split("=", 1)[0].strip() == "swarm_qa_persona"]


def _route_loads(world):
    """The log index of every navigation to a route (not to the bare origin)."""
    return [i for i, e in enumerate(world.page.log) if e[0] == "goto" and e[1].rstrip("/") != CONSOLE]


def _personas_before_each_load(world):
    """[(route URL, persona)] for every route navigation: the persona the last
    page write between the navigation before it and this one set. Fails when a
    navigation has no write of its own, or the write was not on the console's
    origin or not for every path."""
    log, seen, previous = world.page.log, [], -1
    for i in _route_loads(world):
        writes = [w for w in _persona_writes(world) if previous < w[0] < i]
        # MUTATION: set the cookie only for a route that names a persona (or
        # only when it changes) and a route has no write of its own before
        # its navigation.
        assert writes, f"no persona cookie written before {log[i]}: {log}"
        _j, url, text = writes[-1]
        # MUTATION: skip the navigation to the console's origin before the
        # first write and the cookie is written on about:blank, which no
        # console load ever reads.
        assert _origin(url) == CONSOLE, (url, log)
        parts = [part.strip() for part in text.split(";")]
        # MUTATION: drop `path=/` and the cookie belongs to the path it was
        # written from, not to every route the console loads.
        assert "path=/" in parts, text
        seen.append((log[i][1], parts[0].split("=", 1)[1]))
        previous = i
    return seen


def test_each_route_is_loaded_under_its_own_persona_cookie_member_included(qa, world):
    """The cookie is set before EVERY navigation, `member` included, so a
    route with no persona never inherits the admin cookie the route before it
    set. A cookie and not storage: RESET_STATE_JS empties local and session
    storage on every load. Written by the page on the console's origin, never
    with context.add_cookies: that is Storage.setCookies, which the live gate
    refuses (#1980)."""
    _persona_routes(world, [
        {"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1", "persona": "platform-admin"},
        {"id": "timeline", "address": "work/timeline", "path": "/timeline", "ready": "main h1"},
    ])
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    # MUTATION: set the persona with context.add_cookies again and the fake
    # refuses it as the gate does: every route fails at "persona", code 1.
    assert code == 0, _manifest(bundle)["failed"]
    # MUTATION: the same, seen from the context: any add_cookies call is here.
    assert [e for e in world.page.log if e[0] == "cookies"] == [], world.page.log
    # MUTATION: skip the cookie for `member` and the second value stays platform-admin.
    assert _personas_before_each_load(world) == [
        ("http://localhost:5173/runs", "platform-admin"), ("http://localhost:5173/timeline", "member")]
    # MUTATION: leave the persona out of the manifest entry and this fails.
    assert [(r["id"], r["persona"]) for r in _manifest(bundle)["routes"]] == [("runs", "platform-admin"), ("timeline", "member")]


def test_a_failed_persona_write_fails_the_route_at_the_persona_step(qa, world):
    """The page write is the persona step: when it throws, the route is
    reported as failed at `persona`, and it is never loaded as someone else."""
    world.page.cookie_fails = True
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 1
    failed = _manifest(bundle)["failed"]
    assert [f["id"] for f in failed] == ["runs", "timeline"], failed
    # MUTATION: set step = "navigate" before the write and the failure names
    # the wrong step.
    assert all(f["error"].startswith("persona: RuntimeError") for f in failed), failed
    # MUTATION: swallow the write's failure and the route loads under
    # whichever persona the cookie last held.
    assert _route_loads(world) == [], world.page.log


# --------------------------------------------------------------------------
# VQ5-CAPTURE-GENERIC (docs/design/visual-qa-any-app.md §4.1, §4.2; part of
# #1933): the capture flags for any app, and `discover`
# --------------------------------------------------------------------------


def _no_clock(world):
    document = json.loads(world.routes.read_text(encoding="utf-8"))
    document.pop("clock")
    world.routes.write_text(json.dumps(document), encoding="utf-8")


def test_keep_storage_never_runs_reset_state_js(qa, world):
    """An own-login target's session lives in its storage: emptying it on
    every load signs the capture out after the first route."""
    code, out, _bundle = _capture(qa, world, "--keep-storage", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: add RESET_STATE_JS whatever --keep-storage says and this list
    # holds the reset script.
    assert not any("localStorage.clear" in (s or "") for s in world.page.init_scripts), world.page.init_scripts
    # MUTATION: the same, seen from the page: the second route loads with what
    # the first left in storage only when nothing emptied it.
    # (Not the whole list: a console capture loads the bare origin once
    # first, to write the persona cookie on it, and that load keeps its own.)
    timeline = (out / "work-a" / "timeline--desktop--light.txt").read_text(encoding="utf-8")
    assert "'visited:/runs'" in timeline.rsplit("storage at load ", 1)[1], timeline


def test_without_keep_storage_every_load_still_runs_reset_state_js(qa, world):
    code, _out, _bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    # MUTATION: make --keep-storage the default and the console's captures
    # lose QA-077's clean state.
    assert world.page.init_scripts == [qa.reset_state_script("http://localhost:5173")]


def test_settle_generic_never_evaluates_pending_js_and_waits_for_the_dom_to_go_quiet(qa, world):
    """Someone else's app draws none of the console's markers, and a word
    like "reading…" in its text is its content, not a pending read."""
    world.page.pending = {"/runs": None, "/timeline": None}
    world.page.mutations = {"/runs": 1200}
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: settle every page with the console's _settle and PENDING_JS is evaluated.
    assert qa.PENDING_JS not in world.page.evaluated
    # MUTATION: the console's settle also reads TEXT_JS; generic needs only the DOM's changes.
    assert qa.TEXT_JS not in world.page.evaluated
    shots = [e for e in world.page.log if e[0] == "shot" and e[1].endswith("/runs")]
    # MUTATION: return as soon as the DOM is quiet once (no 500 ms window) and
    # the first /runs shot is taken before 1200 + 500 ms.
    assert shots and all(s[4] >= 1200 + qa.GENERIC_QUIET_MS for s in shots), [s[4] for s in shots]
    manifest = _manifest(bundle)
    assert manifest["settle"] == "generic"
    runs = next(r for r in manifest["routes"] if r["id"] == "runs")
    # MUTATION: report a never-clearing PENDING_JS marker under generic and /runs is unsettled.
    assert runs["status"] == "captured" and runs["settled"] is True, runs


def test_settle_generic_on_a_dom_that_never_stops_is_bounded_and_says_where(qa, world):
    world.page.mutations = {"/timeline": None}
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    (timeline,) = [r for r in manifest["routes"] if r["id"] == "timeline"]
    # MUTATION: wait for quiet forever, or call a moving page settled, and this fails.
    assert timeline["status"] == "unsettled" and timeline["settled"] is False, timeline
    assert any("DOM still changing" in p and "div.feed" in p for p in timeline["pending"]), timeline
    last_load = max(i for i, e in enumerate(world.page.log) if e[0] == "goto")
    waited = sum(e[1] for e in world.page.log[last_load:] if e[0] == "wait")
    # MUTATION: drop the SETTLE_TIMEOUT_MS bound and the wait never ends.
    assert qa.SETTLE_TIMEOUT_MS <= waited <= qa.SETTLE_TIMEOUT_MS + qa.SETTLE_POLL_MS, waited
    assert any(f["id"] == "timeline" and "DOM still changing" in f["error"] for f in manifest["failed"])


def test_the_dom_change_counter_runs_in_a_page(qa):
    # MUTATION: a syntax error in DOM_CHANGES_JS and node refuses it; a page
    # with no observer yet answers no change and nowhere.
    dom = {"inner_height": 900, "nodes": [{"tag": "html"}, {"tag": "body", "parent": 0}]}
    assert _run_js(qa.DOM_CHANGES_JS, dom, None) == [0, None]


# VQ5-PERSONA (part of #1933; BGD, 2026-10-22 07:07Z: "Skip it when no route
# names a persona and --settle generic"): PERSONA_COOKIE is the console
# fixture's, so someone else's app is not handed it.


def test_a_generic_capture_with_no_persona_route_sets_no_persona_cookie(qa, world):
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: set the cookie whatever the settle and the route list say and
    # someone else's app is handed swarm_qa_persona on every route.
    assert [e for e in world.page.log if e[0] in ("document.cookie", "cookies")] == [], world.page.log
    # MUTATION: load the origin to write a cookie that is never written and
    # someone else's app is loaded once more than its routes ask.
    assert [e[1] for e in world.page.log if e[0] == "goto"] == [CONSOLE + "/runs", CONSOLE + "/timeline"]
    routes = _manifest(bundle)["routes"]
    assert [r["id"] for r in routes] == ["runs", "timeline"]
    # MUTATION: keep recording the member in the entry and the manifest claims
    # an identity no cookie carried.
    assert all(r["persona"] is None for r in routes), routes


def test_a_console_capture_still_sets_the_persona_cookie_on_every_route(qa, world):
    """No route here names a persona either: the console's settle alone keeps
    the cookie, `member` on each navigation."""
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0, _manifest(bundle)["failed"]
    assert [e for e in world.page.log if e[0] == "cookies"] == [], world.page.log
    # MUTATION: skip the cookie whenever no route names a persona (forget
    # the settle) and the console's routes load with no cookie of their own.
    assert _personas_before_each_load(world) == [
        ("http://localhost:5173/runs", "member"), ("http://localhost:5173/timeline", "member")]
    # MUTATION: skip the write on every route after the first and there is one.
    assert len(_persona_writes(world)) == 2
    assert [r["persona"] for r in _manifest(bundle)["routes"]] == ["member", "member"]


def test_a_generic_capture_whose_route_names_a_persona_keeps_the_cookie(qa, world):
    _persona_routes(world, [
        {"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1", "persona": "platform-admin"},
        {"id": "timeline", "address": "work/timeline", "path": "/timeline", "ready": "main h1"},
    ])
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0, _manifest(bundle)["failed"]
    assert [e for e in world.page.log if e[0] == "cookies"] == [], world.page.log
    # MUTATION: skip the cookie on every generic capture and a route list that
    # asks for a persona is captured as no one.
    assert [v for _url, v in _personas_before_each_load(world)] == ["platform-admin", "member"], world.page.log
    assert [r["persona"] for r in _manifest(bundle)["routes"]] == ["platform-admin", "member"]
    # MUTATION: skip the persona check under generic and an unknown one is
    # accepted.
    _persona_routes(world, [{"id": "runs", "address": "work/runs", "path": "/runs", "ready": "main h1", "persona": "admin"}])
    code, _out, _bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 64


def test_the_clock_is_not_frozen_unless_asked(qa, world):
    """Freezing `Date` can expire an app's own session token: a route list
    with no `clock` and no `--clock` runs on the real clock."""
    _no_clock(world)
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    # MUTATION: freeze the clock whatever the route list says and a clock entry is logged.
    assert [e for e in world.page.log if e[0] == "clock"] == []
    # MUTATION: keep requiring `clock` in load_routes and this route list is refused (exit 64).
    assert _manifest(bundle)["clock"] is None
    code, _out, bundle = _capture(qa, world, "--clock", "2026-10-21T08:30:00Z", presets="desktop", themes="light")
    assert code == 0
    clocks = [e for e in world.page.log if e[0] == "clock"]
    # MUTATION: ignore --clock and nothing is frozen.
    assert len(clocks) == 1 and clocks[0][1].isoformat().startswith("2026-10-21T08:30:00"), clocks
    assert _manifest(bundle)["clock"] == "2026-10-21T08:30:00Z"
    # MUTATION: accept a clock with no time zone and the frozen instant depends on the machine.
    code, _out, _bundle = _capture(qa, world, "--clock", "2026-10-21T08:30:00", presets="desktop", themes="light")
    assert code == qa.EXIT_USAGE


def test_the_console_templates_captures_are_unchanged():
    """The console's steps pass none of the generic flags, and its route list
    carries the clock it is frozen at, so every console capture takes the
    defaults: storage emptied, the console's settle, the frozen clock."""
    template = (REPO / "docs" / "qa" / "console-qa.workflow.json").read_text(encoding="utf-8")
    commands = re.findall(r"swarm_qa capture [^`]*", template)
    assert len(commands) >= 6, commands
    for command in commands:
        # MUTATION: a console step that passes a generic flag captures differently.
        assert "--keep-storage" not in command and "--settle" not in command and "--clock" not in command, command
    routes = json.loads((REPO / "apps" / "swarm-ui" / "qa" / "routes.json").read_text(encoding="utf-8"))
    # MUTATION: drop the console route list's clock and its captures stop being frozen.
    assert isinstance(routes.get("clock"), str)


def test_the_default_capture_is_the_consoles(qa, world):
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    # MUTATION: default --settle to generic and PENDING_JS is never asked.
    assert qa.PENDING_JS in world.page.evaluated and qa.TEXT_JS in world.page.evaluated
    # MUTATION: default to generic and the console's capture waits on DOM changes instead.
    assert qa.DOM_CHANGES_JS not in world.page.evaluated
    # MUTATION: stop freezing at the route list's clock and this is empty.
    assert [e[1].isoformat()[:19] for e in world.page.log if e[0] == "clock"] == ["2026-10-19T12:00:00"]
    manifest = _manifest(bundle)
    assert manifest["settle"] == "console" and manifest["clock"] == "2026-10-19T12:00:00Z"
    assert manifest["keep_storage"] is False


# -- discover ----------------------------------------------------------------

APP = "https://app.example.test"


@pytest.fixture
def site(world, qa, monkeypatch):
    """A site for `discover`: links per path, and its robots.txt."""
    world.robots = None
    world.robots_asked = []

    def fetch(url):
        world.robots_asked.append(url)
        return world.robots

    monkeypatch.setattr(qa, "_fetch_robots", fetch)
    # The walk spaces its navigations (QAENV-CRAWL, qa-environments.md §4):
    # on a clock of its own here, so these tests never sleep.
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(qa, "_now", lambda: clock[0], raising=False)
    monkeypatch.setattr(qa, "_sleep", sleep, raising=False)
    return world


def _discover(qa, world, *extra, max_pages="60", max_depth="3", base=APP):
    out = world.tmp / "screens.json"
    if out.exists():
        out.unlink()
    code = qa.main(["discover", "--base", base, "--max-pages", max_pages, "--max-depth", max_depth,
                    "--out", str(out), *extra])
    return code, out


def _paths(out: Path) -> list[str]:
    document = json.loads(out.read_text(encoding="utf-8"))
    return [r["path"] for routes in document["groups"].values() for r in routes]


def _gotos(world) -> list[str]:
    return [e[1][len(APP):] or "/" for e in world.page.log if e[0] == "goto"]


def test_discover_stays_same_origin_and_writes_a_valid_route_list(qa, site):
    site.page.links = {
        "/": [APP + "/", APP + "/pricing?ref=nav#top", APP + "/docs/", "https://other.example.test/x",
              "http://app.example.test/insecure", "https://app.example.test:8443/port", "mailto:a@example.test",
              "javascript:void(0)", APP + "/Docs/Intro"],
        "/pricing": [APP + "/docs/", APP + "/"],
    }
    code, out = _discover(qa, site)
    assert code == 0
    paths = _paths(out)
    # MUTATION: follow any http(s) link and the other host, the other scheme
    # and the other port are listed.
    assert sorted(paths) == ["/", "/Docs/Intro", "/docs/", "/pricing"], paths
    # MUTATION: keep the query or fragment and a token in one is in the route list.
    assert not any("?" in p or "#" in p for p in paths)
    # MUTATION: write anything load_routes refuses and this raises.
    document = qa.load_routes(out)
    ids = [r["id"] for routes in document["groups"].values() for r in routes]
    assert len(set(ids)) == len(ids) == 4
    # MUTATION: write a clock and every capture of a discovered list is frozen.
    assert "clock" not in document
    assert all(r["ready"] == "body" for routes in document["groups"].values() for r in routes)
    # MUTATION: never navigate off the origin: no visit outside APP.
    assert all(e[1].startswith(APP + "/") for e in site.page.log if e[0] == "goto")


def test_discover_skips_exclusions_and_robots_disallow(qa, site):
    site.robots = ("User-agent: somebot\nDisallow: /\n\n"
                   "User-agent: *\nDisallow: /private\nDisallow: /*.pdf$\nDisallow:\n")
    site.page.links = {"/": [APP + p for p in ("/logout", "/signout/now", "/account", "/admin/users",
                                               "/private/x", "/report.pdf", "/report.pdf.html", "/ok")]}
    code, out = _discover(qa, site, "--exclude", "/admin/*")
    assert code == 0
    paths = _paths(out)
    # MUTATION: drop the default exclusions and /logout and /signout/now are listed.
    assert "/logout" not in paths and "/signout/now" not in paths
    # MUTATION: ignore --exclude and /admin/users is listed.
    assert "/admin/users" not in paths
    # MUTATION: ignore robots.txt, or read somebot's group, and the result differs.
    assert sorted(paths) == ["/", "/account", "/ok", "/report.pdf.html"], paths
    # MUTATION: visit an excluded or disallowed page and it is in the navigations.
    assert set(_gotos(site)) <= {"/", "/account", "/ok", "/report.pdf.html"}, _gotos(site)
    assert site.robots_asked == [APP + "/robots.txt"]
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["discovered"]["skipped"]["robots"] == 2 and document["discovered"]["skipped"]["excluded"] == 3


def test_discover_stops_at_its_page_cap(qa, site):
    site.page.links = {"/": [APP + f"/p{i}" for i in range(30)]}
    for i in range(30):
        site.page.links[f"/p{i}"] = [APP + f"/p{i}/q{j}" for j in range(5)]
    code, out = _discover(qa, site, max_pages="5")
    assert code == 0
    # MUTATION: no page cap and all 181 paths are listed.
    assert len(_paths(out)) == 5
    # MUTATION: keep loading pages after the cap and there are more navigations than pages.
    assert len(_gotos(site)) <= 5


def test_discover_stops_at_its_depth_cap(qa, site):
    chain = ["/", "/a", "/a/b", "/a/b/c", "/a/b/c/d"]
    site.page.links = {p: [APP + n] for p, n in zip(chain, chain[1:])}
    code, out = _discover(qa, site, max_depth="2")
    assert code == 0
    # MUTATION: no depth cap and /a/b/c and /a/b/c/d are listed.
    assert _paths(out) == ["/", "/a", "/a/b"]
    # MUTATION: load a page at the depth cap (its links are past it) and /a/b is navigated.
    assert _gotos(site) == ["/", "/a"]
    code, out = _discover(qa, site, max_depth="0")
    assert code == 0 and _paths(out) == ["/"]


def test_discover_groups_a_long_list_into_groups_of_at_most_eight(qa, site):
    site.page.links = {"/": [APP + f"/p{i}" for i in range(20)]}
    code, out = _discover(qa, site, max_depth="1")
    assert code == 0
    document = qa.load_routes(out)
    sizes = [len(routes) for routes in document["groups"].values()]
    # MUTATION: one group of 21 and a capture step holds more screens than §4.1 allows.
    assert sizes == [8, 8, 5], sizes


def test_discover_refuses_caps_above_the_designs(qa, site, capsys):
    # MUTATION: accept any cap and 61 pages or depth 4 run.
    assert _discover(qa, site, max_pages="61")[0] == qa.EXIT_USAGE
    assert _discover(qa, site, max_depth="4")[0] == qa.EXIT_USAGE
    assert _discover(qa, site, max_pages="0")[0] == qa.EXIT_USAGE
    assert [e for e in site.page.log if e[0] == "goto"] == []


def test_discover_refuses_without_the_live_browser(qa, site, monkeypatch):
    monkeypatch.delenv(qa.ENDPOINT_ENV)
    # MUTATION: discover with no live browser and it connects (the fake raises here).
    monkeypatch.setattr(qa, "_connect", lambda: (_ for _ in ()).throw(AssertionError("connected")))
    code, out = _discover(qa, site)
    assert code == qa.EXIT_NO_LIVE_BROWSER and not out.exists()


def test_discover_with_its_start_page_disallowed_lists_nothing_and_says_so(qa, site, capsys):
    site.robots = "User-agent: *\nDisallow: /\n"
    code, out = _discover(qa, site)
    # MUTATION: write an empty route list and capture is handed a list load_routes refuses.
    assert code == qa.EXIT_NOTHING_CAPTURED and not out.exists()
    assert _gotos(site) == []
    assert "robots" in capsys.readouterr().err


def test_robots_rules_are_the_star_groups_disallow_lines(qa):
    text = ("# comment\nUser-agent: a\nUser-agent: *\nDisallow: /x # why\nAllow: /x/y\n"
            "\nUser-agent: b\nDisallow: /b\n")
    rules = qa.robots_disallow(text)
    # MUTATION: read every group's Disallow and /b is in the rules.
    assert rules == ["/x"], rules
    assert qa.disallowed("/x/1", rules) and qa.disallowed("/x", rules) and not qa.disallowed("/y", rules)
    assert qa.robots_disallow(None) == [] and qa.robots_disallow("User-agent: *\nDisallow:\n") == []
    # MUTATION: treat `*` and `$` literally and these miss.
    assert qa.disallowed("/a/file.pdf", ["/*.pdf$"]) and not qa.disallowed("/a/file.pdf.html", ["/*.pdf$"])


# -- QA-GENERIC-GAPS (part of #1981, #1933): the generic path on an external app

TODO_APP = APP + "/todomvc/"


def test_discover_starts_at_a_base_with_a_path_and_keeps_only_links_under_it(qa, site):
    """Epic #1981, the first gap: an app served under a sub-path
    (https://demo.playwright.dev/todomvc/) is the start, not the origin's /."""
    site.page.links = {
        "/todomvc/": [APP + "/todomvc/about", APP + "/todomvc", APP + "/", APP + "/other/x",
                      APP + "/todomvcx", "https://other.example.test/todomvc/y"],
        "/todomvc/about": [APP + "/todomvc/about/team"],
    }
    code, out = _discover(qa, site, base=TODO_APP)
    assert code == 0
    paths = _paths(out)
    # MUTATION: keep refusing a base with a path and this exits 64.
    # MUTATION: keep every same-origin link and /, /other/x and /todomvcx are listed.
    assert sorted(paths) == ["/todomvc", "/todomvc/", "/todomvc/about", "/todomvc/about/team"], paths
    # MUTATION: start at the origin's / and the first navigation is /.
    assert _gotos(site)[0] == "/todomvc/"
    # The paths stay relative to the origin, as `capture --base <origin>` expects.
    assert all(p.startswith("/todomvc") for p in paths)
    # MUTATION: fetch robots.txt under the base's path and the origin's own rules are never read.
    assert site.robots_asked == [APP + "/robots.txt"]
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["discovered"]["skipped"]["outside_base"] == 3, document["discovered"]["skipped"]


@pytest.mark.parametrize("base", [
    "https://user@app.example.test/todomvc/",
    "https://user:pw@app.example.test/",
    "https://app.example.test/todomvc/?ref=x",
    "https://app.example.test/todomvc/?",
    "https://app.example.test/todomvc/#/active",
    "ftp://app.example.test/todomvc/",
    "https://203.0.113.7/todomvc/",
    "https://[2001:db8::1]/",
    "https://localhost/todomvc/",
    "https://intranet/",
    "https://app.svc.cluster.local/",
    "https://app.corp.internal/x/",
])
def test_discover_still_refuses_a_base_that_is_no_public_http_page(qa, site, base):
    # MUTATION: drop the userinfo, query, fragment, scheme, IP-literal or
    # internal-name refusal and this base is crawled.
    code, out = _discover(qa, site, base=base)
    assert code == qa.EXIT_USAGE and not out.exists()
    assert [e for e in site.page.log if e[0] == "goto"] == []


def test_discovers_internal_suffixes_are_swarm_egresss():
    """The internal-name rule is swarm_egress.policy's, restated in the image
    (which cannot import it): the two lists must not drift."""
    import ast

    tree = ast.parse((REPO / "apps" / "swarm-egress" / "swarm_egress" / "policy.py").read_text(encoding="utf-8"))
    egress = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) == "INTERNAL_SUFFIXES" for t in n.targets))
    # MUTATION: drop a suffix from swarm_qa's list and a cluster name is crawled.
    assert tuple(_module().INTERNAL_SUFFIXES) == tuple(egress)


def test_discover_with_hash_routes_lists_route_shaped_fragments_as_routes(qa, site):
    """Epic #1981, the second gap: a hash-routed app's routes (#/active,
    #/completed) are found, but only with --hash-routes."""
    site.page.links = {
        "/todomvc/": [TODO_APP + "#/", TODO_APP + "#/active", TODO_APP + "#/completed?token=x",
                      TODO_APP + "#section", TODO_APP + "#", TODO_APP + "#access_token=abc",
                      APP + "/todomvc/about#/team"],
    }
    code, out = _discover(qa, site, base=TODO_APP)
    assert code == 0
    # MUTATION: keep fragments by default and a token-shaped one is a route.
    assert sorted(_paths(out)) == ["/todomvc/", "/todomvc/about"], _paths(out)
    code, out = _discover(qa, site, "--hash-routes", base=TODO_APP)
    assert code == 0
    paths = _paths(out)
    # MUTATION: ignore --hash-routes and #/active and #/completed are never listed.
    # MUTATION: keep any fragment and #section, # and #access_token=... are routes.
    # MUTATION: keep a hash route's own query and its token is in the route list.
    # MUTATION: keep a hash link on another document and /todomvc/about#/team is a route.
    assert sorted(paths) == ["/todomvc/", "/todomvc/#/", "/todomvc/#/active", "/todomvc/#/completed",
                             "/todomvc/about"], paths
    document = qa.load_routes(out)
    ids = [r["id"] for routes in document["groups"].values() for r in routes]
    assert len(set(ids)) == len(ids) == 5


def test_hash_routes_honour_the_exclusions(qa, site):
    site.page.links = {"/": [APP + "/#/logout", APP + "/#/admin/users", APP + "/#/ok"]}
    code, out = _discover(qa, site, "--hash-routes", "--exclude", "/admin/*")
    assert code == 0
    # MUTATION: test only the document path against the exclusions and #/logout is listed.
    assert sorted(_paths(out)) == ["/", "/#/ok"], _paths(out)


def test_discover_does_not_list_a_page_that_answered_an_http_error(qa, site, capsys):
    """Epic #1981, the third gap: the origin root was a GitHub Pages 404 and
    was listed."""
    site.page.links = {"/": [APP + "/gone", APP + "/broken", APP + "/ok"], "/gone": [APP + "/never"]}
    site.page.statuses = {"/gone": 404, "/broken": 503}
    code, out = _discover(qa, site)
    assert code == 0
    # MUTATION: ignore the main document's status and /gone and /broken are listed.
    assert sorted(_paths(out)) == ["/", "/ok"], _paths(out)
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["discovered"]["http_errors"] == {"/gone": 404, "/broken": 503}
    # MUTATION: read a 404 page's links and /never is listed.
    assert "/never" not in _paths(out)
    site.page.statuses = {"/": 404}
    code, out = _discover(qa, site)
    # MUTATION: list a start page that answered 404 and capture is handed a 404.
    assert code == qa.EXIT_NOTHING_CAPTURED and not out.exists()
    assert "404" in capsys.readouterr().err


def test_capture_records_a_page_that_answered_an_http_error_as_failed_at_navigate(qa, world):
    world.page.statuses = {"/timeline": 404}
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    # MUTATION: ignore the status and a 404 page is recorded captured.
    assert [r["id"] for r in manifest["routes"]] == ["runs"]
    failed = manifest["failed"]
    assert [f["id"] for f in failed] == ["timeline"]
    # MUTATION: record it at another step, or without its status, and this fails.
    assert failed[0]["error"].startswith("navigate: ") and "HTTP 404" in failed[0]["error"], failed


def test_a_same_document_navigation_with_no_response_is_captured(qa, world):
    """A hash route's `goto` on the same document answers no response:
    that is not an HTTP error."""
    world.page.url = CONSOLE + "/runs"
    routes = json.loads(world.routes.read_text(encoding="utf-8"))
    routes["groups"]["work-a"] = [{"id": "runs-active", "path": "/runs#/active", "ready": "main h1"}]
    world.routes.write_text(json.dumps(routes), encoding="utf-8")
    code, _out, bundle = _capture(qa, world, "--settle", "generic", "--keep-storage", presets="desktop",
                                  themes="light")
    assert code == 0
    # MUTATION: treat a None response as a failure and a hash route is never captured.
    assert [r["id"] for r in _manifest(bundle)["routes"]] == ["runs-active"]


def _hash_routes(world) -> None:
    routes = json.loads(world.routes.read_text(encoding="utf-8"))
    routes["groups"]["work-a"] = [{"id": "active", "path": "/todo#/active", "ready": "main h1"},
                                  {"id": "completed", "path": "/todo#/completed", "ready": "main h1"}]
    world.routes.write_text(json.dumps(routes), encoding="utf-8")


def test_a_hash_route_after_a_route_of_the_same_document_gets_a_full_load(qa, world):
    """Epic #1981, the fifth comment: `goto` between two hash routes of one
    document is a same-document navigation, so RESET_STATE_JS (an init
    script, run on a new document only) never ran and the second route drew
    what the first left in storage."""
    _hash_routes(world)
    code, out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    log = world.page.log
    second = log.index(("goto", CONSOLE + "/todo#/completed", "load"))
    # MUTATION: drop the forced load and the second hash route is never reloaded.
    assert ("reload", CONSOLE + "/todo#/completed", "load") in log[second:], log[second:]
    completed = (out / "work-a" / "completed--desktop--light.txt").read_text(encoding="utf-8")
    # MUTATION: the same, seen from the page: what the first route stored is
    # still there when the second is captured.
    assert "visited:/todo#/active" not in completed.rsplit("storage at load ", 1)[1], completed
    entries = {r["id"]: r for r in _manifest(bundle)["routes"]}
    # MUTATION: force a load and never say so, and the reviewer cannot tell.
    assert entries["active"]["reloaded"] is False and entries["completed"]["reloaded"] is True, entries


def test_keep_storage_never_reloads_a_same_document_hash_route(qa, world):
    """An own-login target's storage holds its session: a forced load would
    keep it (no reset script), but the capture keeps the navigation it was
    asked for."""
    _hash_routes(world)
    code, _out, bundle = _capture(qa, world, "--keep-storage", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: force the load whatever --keep-storage says and a reload is logged.
    assert not [e for e in world.page.log if e[0] == "reload"], world.page.log
    assert {r["id"]: r["reloaded"] for r in _manifest(bundle)["routes"]} == {"active": False, "completed": False}


def test_the_manifest_says_whether_the_dark_captures_differ_from_the_light(qa, world):
    """Epic #1981, the eighth comment: an app with no dark theme draws its
    dark capture the same as its light one, and "dark mode is ignored" was
    filed against it. The manifest records the fact for the reviewer."""
    code, _out, bundle = _capture(qa, world, presets="desktop")
    assert code == 0
    routes = _manifest(bundle)["routes"]
    # MUTATION: never record the comparison and this key is missing.
    assert routes and all(r["dark_differs"] is False for r in routes), routes
    world.page.dark_theme = True
    world.page.log.clear()
    code, _out, bundle = _capture(qa, world, presets="desktop", group="other")
    assert code == 0
    routes = _manifest(bundle)["routes"]
    # MUTATION: compare the light shots with themselves and a dark theme reads False.
    assert routes and all(r["dark_differs"] is True for r in routes), routes


def test_a_capture_in_one_theme_says_nothing_of_the_dark_one(qa, world):
    code, _out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    routes = _manifest(bundle)["routes"]
    # MUTATION: record False with no dark capture to compare and a light-only
    # run claims the app has no dark theme.
    assert routes and all(r["dark_differs"] is None for r in routes), routes


def test_an_http_error_subresource_is_recorded_with_its_url_and_status(qa, world):
    """Epic #1981, the fifth comment: "Failed to load resource ... 404" had
    no URL, and failed_requests stayed empty (an HTTP error is no failed
    request)."""
    world.page.subresources = {"/runs": [(CONSOLE + "/app.js?v=" + "9" * 8, 404), (CONSOLE + "/ok.css", 200),
                                         ("https://cdn.example.test/font.woff2", 503)]}
    code, out, bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    errors = json.loads((out / "work-a" / "runs--desktop--light.errors.json").read_text(encoding="utf-8"))
    # MUTATION: listen to requestfailed alone and this list is empty.
    # MUTATION: keep the query and a token in one is in errors.json.
    assert errors["bad_responses"] == [{"url": CONSOLE + "/app.js", "status": 404},
                                       {"url": "https://cdn.example.test/font.woff2", "status": 503}]
    assert errors["failed_requests"] == []
    entry = next(r for r in _manifest(bundle)["routes"] if r["id"] == "runs")
    assert entry["bad_responses"] == 2
    # MUTATION: never reset the list between routes and /timeline carries /runs's.
    timeline = json.loads((out / "work-a" / "timeline--desktop--light.errors.json").read_text(encoding="utf-8"))
    assert timeline["bad_responses"] == []


def test_the_first_shot_after_a_document_load_is_retaken_at_the_viewports_size(qa, world):
    """Epic #1981, the phone-390 comment: the first screenshot after a full
    document load came out 980x2121 with the page drawn small while the page
    reported innerWidth 390; later shots in the same document were 390x844.
    A shot whose size is not the page's viewport is taken again."""
    world.page.first_shot = (9, 21)
    code, out, bundle = _capture(qa, world, presets="phone-390", themes="light")
    assert code == 0
    for name in ("runs--phone-390--light--s1.png", "timeline--phone-390--light--s1.png"):
        # MUTATION: keep the first shot and segment 1 is the 9x21 image.
        assert qa.decode_png((out / "work-a" / name).read_bytes())[:2] == (3, 2), name
    shots = [e for e in world.page.log if e[0] == "shot"]
    # Three segments a route, and one shot again per route, no more.
    assert len(shots) == 2 * 3 + 2
    entry = next(r for r in _manifest(bundle)["routes"] if r["id"] == "runs")
    # MUTATION: retake silently and the manifest does not say the first shot was wrong.
    assert entry["reshot"] == [{"segment": 1, "size": [9, 21], "viewport": [3, 2]}]
    assert not any("viewport" in f["error"] for f in _manifest(bundle)["failed"])


def test_a_shot_that_stays_off_the_viewports_size_is_kept_and_listed_failed(qa, world, monkeypatch):
    world.page.first_shot = (9, 21)
    shoot = world.page.screenshot

    def always_wrong(**kw):
        shoot(**kw)
        return _png(9, 21, _white)

    monkeypatch.setattr(world.page, "screenshot", always_wrong)
    code, _out, bundle = _capture(qa, world, presets="phone-390", themes="light", group="other")
    assert code == 0
    failed = _manifest(bundle)["failed"]
    # MUTATION: retake forever, or drop the finding, and this differs.
    assert len(failed) == 1 and "9x21" in failed[0]["error"] and "3x2" in failed[0]["error"], failed
    shots = [e for e in world.page.log if e[0] == "shot"]
    # Each segment: the shot, SHOT_RETRIES more, and one after the device reset.
    assert len(shots) <= 3 * (2 + qa.SHOT_RETRIES)


def test_the_viewport_probe_runs_in_a_page(qa):
    _node()
    dom = _console_dom()
    answer = _run_js(qa.VIEWPORT_JS, dom, None)
    # MUTATION: read another height than the window's and the shot is
    # compared with something else.
    assert isinstance(answer, list) and len(answer) == 4 and answer[1] == dom["inner_height"], answer
    # No visual viewport to read: its scale is taken as 1.
    assert answer[3] == 1, answer
    dom["visual_scale"] = 0.398
    # MUTATION: read the device pixel ratio, or no scale, in its place and a
    # page laid out at 980 px on a phone is compared with 390.
    assert _run_js(qa.VIEWPORT_JS, dom, None)[3] == 0.398


class _ShotPage:
    """A page as `_shot` sees it: each screenshot the next size in `shots`
    (a PNG header is all `png_size` reads), VIEWPORT_JS answering `reading`."""

    def __init__(self, qa, shots, reading):
        self.qa = qa
        self.shots = list(shots)
        self.reading = reading
        self.taken = 0
        self.log = []

    def screenshot(self, **_kw):
        self.taken += 1
        width, height = self.shots.pop(0) if len(self.shots) > 1 else self.shots[0]
        return _PNG_HEADER + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + bytes(5)

    def evaluate(self, expression, arg=None):
        if expression == getattr(self.qa, "MASK_FIND_JS", None):
            return []
        if expression == getattr(self.qa, "MASK_CLEAR_JS", None):
            return [True, 0]
        assert expression == self.qa.VIEWPORT_JS, expression
        return list(self.reading)

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"

#: QA-DEVICE's reading at phone-390 on a page with no viewport meta tag
#: (task_b3f3786bde2144389073, Chrome 153, Playwright 1.63): innerWidth 390
#: and innerHeight 844 over a visual viewport scale of 0.398, a layout
#: viewport of 980 x 2121 -- the size Playwright's clip arithmetic gives the
#: shot (innerWidth / visualViewport.scale), and the correct phone layout.
_NO_META_PHONE = (390, 844, 3, 0.398)


def test_a_faithful_980_wide_first_shot_of_a_page_without_a_viewport_meta_is_kept(qa):
    """QA-REASSERT (part of #1981): the first shot at phone-390 of a page
    with no viewport meta tag is 980x2121 and faithful. The check before
    this lane compared it with innerWidth x innerHeight (390x844), retook
    it, and kept the unfaithful dpr-1 shot that followed."""
    page = _ShotPage(qa, [(980, 2121), (390, 844)], _NO_META_PHONE)
    png, reshot, off, _masked = qa._shot(page, 1)
    # MUTATION: compare with innerWidth x innerHeight (no scale) and the
    # 980x2121 shot is retaken and the 390x844 one kept.
    assert qa.png_size(png) == (980, 2121)
    assert reshot == [] and off is None
    assert page.taken == 1


def test_a_shot_that_disagrees_with_the_layout_viewport_reading_is_retaken(qa):
    # The unfaithful later shot QA-DEVICE measured before the gate
    # reasserted the device: 390x844 at dpr 1, while the page still reads
    # 980 x 2121 as its layout viewport.
    page = _ShotPage(qa, [(390, 844), (980, 2121)], _NO_META_PHONE)
    png, reshot, off, _masked = qa._shot(page, 2)
    # MUTATION: accept any shot (drop the check) and the 390x844 one is kept.
    assert qa.png_size(png) == (980, 2121)
    assert reshot == [{"segment": 2, "size": [390, 844], "viewport": [980, 2121]}]
    assert off is None and page.taken == 2


def test_a_shot_at_a_scale_of_one_is_still_checked_against_the_viewport(qa):
    # A page with a viewport meta tag: scale 1, the layout viewport is the
    # window's own size, at one screen pixel per CSS pixel or at the dpr.
    for size in ((390, 844), (1170, 2532)):
        page = _ShotPage(qa, [size], (390, 844, 3, 1))
        _, reshot, off, _masked = qa._shot(page, 1)
        assert reshot == [] and off is None, size
    page = _ShotPage(qa, [(980, 2121)], (390, 844, 3, 1))
    _, reshot, off, _masked = qa._shot(page, 1)
    # MUTATION: keep a shot that matches neither size of the reading and
    # this 980x2121 one at scale 1 is neither retaken nor listed.
    assert len(reshot) == qa.SHOT_RETRIES and off is not None and "980x2121" in off and "390x844" in off

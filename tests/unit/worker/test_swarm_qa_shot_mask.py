"""Every screenshot the capture takes masks the QA target's credential
(QA-SHOT-MASK; part of #2055).

On 2026-10-23 visual QA of a login demo found its home page printing the
demo's password beside a link. The capture's text and ARIA files carried the
gate's redaction marker in its place (the live gate scrubs everything bound
for the agent, livegate/qatarget.py), but every full screenshot showed it
readable. Held here:

* a page printing the credential in a text node gives a stored screenshot
  whose pixels over that text are the mask's colour, and `masked: 1`;
* the credential split across two inline elements is still masked;
* the credential in an input's value, and in a ::before content, is masked;
* a page without it gives `masked: 0` and the shot it would have given;
* a mask that cannot be drawn, or that the page removes before the shot,
  stores no shot, and the route says why;
* the mask is gone after the shot, and the credential is in no file written.

No Chromium: the page is a DOM under node that keeps its state between
evaluations (the mask is drawn by one, read by the "screenshot", taken down
by another), and the gate is a scrub with the gate's own `needles`. The
credential is built at runtime, never a literal.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest
from agent_worker.livegate import qatarget as gate_qt

from test_swarm_qa import FakePage, _capture, _manifest, _node, _png, qa, world  # noqa: F401  (fixtures)

WIDTH, HEIGHT = 200, 120
INK = (200, 0, 0, 255)
WHITE = (255, 255, 255, 255)

#: A DOM under node that keeps its state between commands, one JSON line
#: each on stdin: `eval` runs a page script, `tick` runs the page's own
#: scripts (`strip_mask`: a MutationObserver that removes what was added to
#: the document), and `render` answers what a screenshot would show: the
#: boxes of every text node and every valued control (the page's ink), then
#: every box under an open top-layer element's shadow root (the mask).
_LIVE_DOM = r"""
const readline = require('readline');
const spec = JSON.parse(process.argv[1]);
const box = (r) => ({left: r[0], top: r[1], width: r[2], height: r[3], right: r[0] + r[2],
  bottom: r[1] + r[3], x: r[0], y: r[1]});
let documentNode = null;
class FakeNode {
  constructor(type) { this.nodeType = type; this.childNodes = []; this.parentNode = null; }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n.nodeType === 11 ? n.host.isConnected : n === documentNode;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.childNodes.push(c); return c; }
  remove() {
    if (!this.parentNode) return;
    const s = this.parentNode.childNodes;
    s.splice(s.indexOf(this), 1);
    this.parentNode = null;
  }
}
class FakeText extends FakeNode {
  constructor(data, rect) { super(3); this.data = data; this._rect = rect || null; }
  get textContent() { return this.data; }
}
class FakeShadow extends FakeNode {
  constructor(host) { super(11); this.host = host; }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super(1);
    this.localName = tag; this.tagName = tag.toUpperCase(); this._attrs = {}; this.shadowRoot = null;
    this._shadow = null; this._open = false; this._rect = null; this._before = null; this._after = null;
    const props = {};
    this.style = {_p: props, setProperty: (k, v) => { props[k] = v; }};
  }
  get attributes() { return Object.entries(this._attrs).map(([name, value]) => ({name, value})); }
  getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; }
  setAttribute(n, v) { this._attrs[n] = String(v); }
  getClientRects() { return this.isConnected && this._rect ? [box(this._rect)] : []; }
  attachShadow(init) {
    if (spec.attach_throws) throw new Error('attachShadow is not available on this page');
    const root = new FakeShadow(this);
    this._shadow = root;
    if (init.mode === 'open') this.shadowRoot = root;
    return root;
  }
  showPopover() {
    if (this.getAttribute('popover') === null) throw new Error('not a popover');
    this._open = true;
  }
  hidePopover() { this._open = false; }
  matches(selector) {
    if (selector === ':popover-open') return this._open;
    throw new SyntaxError('unsupported selector ' + selector);
  }
}
class FakeRange {
  selectNodeContents(n) { this.n = n; }
  getClientRects() {
    const out = [];
    const visit = (n) => {
      if (n.nodeType === 3) { if (n._rect && n.isConnected) out.push(box(n._rect)); return; }
      n.childNodes.forEach(visit);
    };
    visit(this.n);
    return out;
  }
}
const build = (s) => {
  if (typeof s.text === 'string') return new FakeText(s.text, s.rect);
  const el = new FakeElement(s.tag);
  Object.entries(s.attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
  if (s.value !== undefined) el.value = s.value;
  if (s.rect) el._rect = s.rect;
  if (s.before) el._before = s.before;
  (s.children || []).forEach((c) => el.appendChild(build(c)));
  return el;
};
documentNode = new FakeNode(9);
const html = build(spec.root);
documentNode.appendChild(html);
globalThis.window = globalThis;
globalThis.innerWidth = spec.width;
globalThis.innerHeight = spec.height;
globalThis.devicePixelRatio = 1;
globalThis.document = {
  documentElement: html,
  createElement: (tag) => new FakeElement(tag),
  createRange: () => new FakeRange(),
};
globalThis.getComputedStyle = (el, pseudo) => ({
  content: (pseudo === '::before' ? el._before : pseudo === '::after' ? el._after : null) || 'none',
});
const px = (v) => parseFloat(v);
const render = () => {
  const ink = [];
  const masks = [];
  const visit = (n) => {
    if (n.nodeType === 3) { if (n._rect) ink.push(n._rect); return; }
    if (n.nodeType === 1 && typeof n.value === 'string' && n.value && n._rect) ink.push(n._rect);
    if (n.nodeType === 1 && n._open && n._shadow && n.isConnected) {
      for (const b of n._shadow.children) {
        const p = b.style._p;
        masks.push([px(p.left), px(p.top), px(p.width), px(p.height), p.background]);
      }
      return;
    }
    n.childNodes.forEach(visit);
  };
  visit(html);
  return {ink, masks};
};
const lines = readline.createInterface({input: process.stdin});
const answer = (v) => process.stdout.write(JSON.stringify(v) + '\n');
let queue = Promise.resolve();
lines.on('line', (line) => {
  queue = queue.then(async () => {
    const cmd = JSON.parse(line);
    if (cmd.op === 'render') return answer({ok: render()});
    if (cmd.op === 'tick') {
      if (spec.strip_mask) html.children.filter((c) => c.localName === 'swarm-qa-mask').forEach((c) => c.remove());
      return answer({ok: null});
    }
    try {
      const fn = (0, eval)('(' + cmd.expr + ')');
      const value = await fn(cmd.arg);
      answer({ok: value === undefined ? null : value});
    } catch (e) {
      answer({error: String(e && e.message)});
    }
  });
});
"""


def _credential() -> str:
    # Built at runtime: nothing in this file looks like a credential.
    return "demo-" + secrets.token_hex(6)


def _scrub(value: Any, forms: list[str]) -> Any:
    """What the live gate does to every string bound for the agent
    (`QaTarget._replace`, the gate's own needle forms, longest first)."""
    if isinstance(value, str):
        for form in forms:
            value = value.replace(form, gate_qt.PLACEHOLDER)
        return value
    if isinstance(value, list):
        return [_scrub(v, forms) for v in value]
    if isinstance(value, dict):
        return {_scrub(k, forms): _scrub(v, forms) for k, v in value.items()}
    return value


class MaskDom:
    """The page's DOM, alive between evaluations, behind the gate's scrub."""

    def __init__(self, dom: dict, credential: str | None) -> None:
        self.forms = gate_qt.needles(credential) if credential else []
        self.proc = subprocess.Popen([_node(), "-e", _LIVE_DOM, json.dumps(dom)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        self.scripts: list[str] = []

    def _send(self, command: dict) -> dict:
        self.proc.stdin.write(json.dumps(command) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "the page's DOM exited"
        return json.loads(line)

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.scripts.append(expression)
        reply = self._send({"op": "eval", "expr": expression, "arg": arg})
        if "error" in reply:
            raise RuntimeError(_scrub(reply["error"], self.forms))
        return _scrub(reply["ok"], self.forms)

    def picture(self) -> bytes:
        """What a screenshot shows now: the page's ink on white, the mask over it."""
        self._send({"op": "tick"})
        drawn = self._send({"op": "render"})["ok"]
        masks = []
        for left, top, width, height, colour in drawn["masks"]:
            rgb = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
            masks.append((left, top, left + width, top + height, rgb + (255,)))

        def pixel(x, y):
            for left, top, right, bottom, rgba in reversed(masks):
                if left <= x < right and top <= y < bottom:
                    return rgba
            for left, top, width, height in drawn["ink"]:
                if left <= x < left + width and top <= y < top + height:
                    return INK
            return WHITE

        return _png(WIDTH, HEIGHT, pixel)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


class MaskPage(FakePage):
    """test_swarm_qa's page, whose page scripts for the mask run in a
    `MaskDom` and whose screenshot is that DOM's picture."""

    def __init__(self, module, dom: MaskDom) -> None:
        super().__init__(module, scroll_height=HEIGHT, viewport_height=HEIGHT)
        self.live = dom
        self.pictures: list[bytes] = []

    def evaluate(self, expression, arg=None):
        if expression in (self.qa.VIEWPORT_JS, getattr(self.qa, "MASK_FIND_JS", None),
                          getattr(self.qa, "MASK_CHILDREN_JS", None), getattr(self.qa, "MASK_DRAW_JS", None),
                          getattr(self.qa, "MASK_CLEAR_JS", None)):
            self.evaluated.append(expression)
            return self.live.evaluate(expression, arg)
        return super().evaluate(expression, arg)

    def screenshot(self, **kw):
        super().screenshot(**kw)
        picture = self.live.picture()
        self.pictures.append(picture)
        return picture


def _page(*children, **flags) -> dict:
    return {"width": WIDTH, "height": HEIGHT, **flags,
            "root": {"tag": "html", "children": [{"tag": "body", "children": list(children)}]}}


#: Where the page draws its other text, and where it draws the credential.
OTHER = (10, 10, 60, 12)
SECRET_BOX = (10, 40, 80, 12)


def _shoot(qa, world, dom: dict, credential: str | None):
    """One route, desktop, light, through `capture`: the manifest's entry,
    the stored image's pixels, and the page."""
    live = MaskDom(dom, credential)
    world.page = MaskPage(qa, live)
    try:
        code, out, bundle = _capture(qa, world, group="other", presets="desktop", themes="light")
    finally:
        live.close()
    manifest = _manifest(bundle)
    entries = [r for r in manifest["routes"] if r["id"] == "pools"]
    entry = entries[0] if entries else None
    image = out / "other" / "pools--desktop--light--s1.png"
    pixels = qa.decode_png(image.read_bytes()) if image.exists() else None
    return manifest, entry, pixels, world.page, out


def _at(pixels, x: int, y: int) -> tuple:
    width, _height, rgba = pixels
    at = (y * width + x) * 4
    return tuple(rgba[at:at + 4])


def _covered(pixels, rect) -> bool:
    left, top, width, height = rect
    return all(_at(pixels, x, y)[:3] == (0, 0, 0) for x in range(left, left + width) for y in range(top, top + height))


def _black(qa) -> None:
    # The colour the pixels are checked against is the module's.
    assert qa.MASK_COLOUR.lower() == "#000000"


def test_the_capture_reads_the_gates_own_redaction_marker(qa):
    # MUTATION: restate a different marker and the capture finds no mark in
    # the text the gate scrubbed, and masks nothing.
    assert qa.CREDENTIAL_MARK == gate_qt.PLACEHOLDER


def test_a_credential_in_a_text_node_is_under_the_mask_in_the_stored_screenshot(qa, world):
    _black(qa)
    secret = _credential()
    dom = _page({"tag": "p", "children": [{"text": "Sign in", "rect": list(OTHER)}]},
                {"tag": "p", "children": [{"text": f"user demo password {secret}", "rect": list(SECRET_BOX)}]})
    _, entry, pixels, page, _out = _shoot(qa, world, dom, secret)
    assert entry is not None and entry["images"] == ["other/pools--desktop--light--s1.png"]
    # MUTATION: skip the mask before the screenshot and these pixels are the
    # page's ink, the credential readable in the stored image.
    assert _covered(pixels, SECRET_BOX)
    # MUTATION: mask the whole viewport and the page's other text is gone too.
    assert _at(pixels, OTHER[0] + 1, OTHER[1] + 1) == INK
    # MUTATION: drop `masked` from the segment's geometry, or count boxes
    # rather than places, and this is absent or wrong.
    assert entry["geometry"][0]["masked"] == 1 and entry["masked"] == 1
    # MUTATION: leave the mask up after the shot and the page the agent sees
    # next still carries it.
    assert page.live._send({"op": "render"})["ok"]["masks"] == []
    assert page.live.evaluate("() => window.__swarmQaMask === undefined") is True


def test_a_credential_split_across_two_inline_elements_is_still_masked(qa, world):
    secret = _credential()
    half = len(secret) // 2
    first, second = (10, 40, 40, 12), (50, 40, 40, 12)
    dom = _page({"tag": "p", "children": [{"text": "Sign in", "rect": list(OTHER)}]},
                {"tag": "p", "children": [
                    {"tag": "b", "children": [{"text": secret[:half], "rect": list(first)}]},
                    {"tag": "i", "children": [{"text": secret[half:], "rect": list(second)}]},
                ]})
    _, entry, pixels, _page_, _out = _shoot(qa, world, dom, secret)
    # MUTATION: look only at single text nodes (drop the descent in
    # `_mask_targets`) and neither half is masked, the credential readable.
    assert _covered(pixels, first) and _covered(pixels, second)
    assert entry["geometry"][0]["masked"] == 1
    assert _at(pixels, OTHER[0] + 1, OTHER[1] + 1) == INK


def test_a_credential_in_an_inputs_value_is_masked(qa, world):
    secret = _credential()
    field = (10, 60, 120, 20)
    dom = _page({"tag": "p", "children": [{"text": "Sign in", "rect": list(OTHER)}]},
                {"tag": "input", "attrs": {"type": "text"}, "value": secret, "rect": list(field)})
    _, entry, pixels, _page_, _out = _shoot(qa, world, dom, secret)
    # MUTATION: read text nodes only (drop the control's value from
    # MASK_FIND_JS) and the field shows the credential.
    assert _covered(pixels, field)
    assert entry["geometry"][0]["masked"] == 1


def test_a_credential_drawn_by_a_pseudo_elements_content_is_masked(qa, world):
    secret = _credential()
    label = (10, 80, 120, 14)
    dom = _page({"tag": "span", "before": f'"{secret}"', "rect": list(label)})
    _, entry, pixels, _page_, _out = _shoot(qa, world, dom, secret)
    # MUTATION: drop ::before from MASK_FIND_JS and the element's box is
    # left as the page drew it.
    assert entry["geometry"][0]["masked"] == 1
    assert _covered(pixels, label)


def test_a_page_without_the_credential_is_shot_unchanged_and_says_masked_0(qa, world):
    secret = _credential()
    dom = _page({"tag": "p", "children": [{"text": "Sign in", "rect": list(OTHER)}]},
                {"tag": "p", "children": [{"text": "user demo", "rect": list(SECRET_BOX)}]})
    _, entry, pixels, page, out = _shoot(qa, world, dom, secret)
    bare = MaskDom(dom, secret)
    try:
        unmasked = bare.picture()
    finally:
        bare.close()
    # MUTATION: draw the mask host whether or not anything matched (or mask
    # every text node) and the stored image differs from the page's own.
    assert (out / "other" / "pools--desktop--light--s1.png").read_bytes() == unmasked
    # MUTATION: report `masked` only when it is non-zero and this key is absent.
    assert entry["geometry"][0]["masked"] == 0 and entry["masked"] == 0
    assert qa.MASK_DRAW_JS not in page.evaluated
    assert pixels is not None


def test_a_mask_that_cannot_be_drawn_stores_no_shot_and_the_route_says_why(qa, world):
    secret = _credential()
    dom = _page({"tag": "p", "children": [{"text": f"password {secret}", "rect": list(SECRET_BOX)}]},
                attach_throws=True)
    manifest, entry, pixels, page, out = _shoot(qa, world, dom, secret)
    # MUTATION: catch the draw's error and shoot anyway, and the unmasked
    # image is stored.
    assert pixels is None and not list(out.rglob("*.png"))
    # MUTATION: take the shot before checking the draw, and a picture was made.
    assert page.pictures == []
    assert entry is None
    failed = [f for f in manifest["failed"] if f["id"] == "pools"]
    # MUTATION: swallow the mask's failure and the route is not failed, or
    # fails with no word of the mask.
    assert len(failed) == 1 and "segment 1" in failed[0]["error"] and "mask" in failed[0]["error"]
    assert secret not in json.dumps(manifest)


def test_a_mask_the_page_removes_before_the_shot_stores_no_shot(qa, world):
    secret = _credential()
    dom = _page({"tag": "p", "children": [{"text": f"password {secret}", "rect": list(SECRET_BOX)}]},
                strip_mask=True)
    manifest, entry, pixels, _page_, _out = _shoot(qa, world, dom, secret)
    # MUTATION: trust the draw and skip the intact check in MASK_CLEAR_JS,
    # and the unmasked shot is stored.
    assert pixels is None and entry is None
    failed = [f for f in manifest["failed"] if f["id"] == "pools"]
    assert len(failed) == 1 and "mask" in failed[0]["error"]


def test_the_credential_is_in_no_file_the_capture_writes(qa, world):
    secret = _credential()
    dom = _page({"tag": "p", "children": [{"text": f"password {secret}", "rect": list(SECRET_BOX)}]},
                {"tag": "input", "value": secret, "rect": [10, 60, 120, 20]})
    _, entry, _pixels, _page_, out = _shoot(qa, world, dom, secret)
    assert entry["geometry"][0]["masked"] == 2
    forms = [f.encode() for f in gate_qt.needles(secret)]
    written = [p for p in Path(out).rglob("*") if p.is_file()]
    assert written
    # MUTATION: write the sweep's rows, or the value they stood for, beside
    # the images and this finds it.
    for path in written:
        data = path.read_bytes()
        assert not any(form in data for form in forms), path.name


def test_a_malformed_sweep_refuses_the_shot(qa):
    class Page:
        def __init__(self):
            self.shots = 0
            self.cleared = 0

        def evaluate(self, expression, arg=None):
            if expression == qa.MASK_FIND_JS:
                return [["t", "not-a-node", 0, 0, "x"]]
            if expression == qa.MASK_CLEAR_JS:
                self.cleared += 1
                return [False, 0]
            raise AssertionError(expression)

        def screenshot(self, **_kw):
            self.shots += 1
            return b""

    page = Page()
    # MUTATION: treat a malformed answer as "nothing to mask" and the shot is taken.
    with pytest.raises(qa._MaskFailed):
        qa._masked_screenshot(page)
    assert page.shots == 0 and page.cleared == 1

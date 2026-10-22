"""Scripted visual-QA capture through the live browser (QA-CAPTURE, part of #1649).

    python -m swarm_qa capture \\
        --routes apps/swarm-ui/qa/routes.json --group work-a \\
        --base http://localhost:5173 \\
        --presets desktop,phone-390 --themes light,dark \\
        --out "$SWARM_ARTIFACTS_DIR/capture-work-a" \\
        --bundle "$SWARM_ARTIFACTS_DIR/capture-work-a.tar" [--axe] \\
        [--keep-storage] [--settle console|generic] [--clock INSTANT]

    python -m swarm_qa discover --base https://app.example \\
        --max-pages 60 --max-depth 3 [--exclude GLOB ...] --out screens.json

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
   which is the one the person watches. Every load of the console in that tab
   starts with the console's local and session storage emptied
   (RESET_STATE_JS), so a route never draws what the route before it left.
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
5. Per route: sets the route's PERSONA (`persona` in the route list:
   `member`, today's fixture identity, when absent, or `platform-admin`) as
   the cookie PERSONA_COOKIE on the console's origin, on EVERY route, so no
   route inherits the one before it's. The page writes it (`document.cookie`,
   PERSONA_COOKIE_JS), loading the console's origin first when it shows
   another; never `context.add_cookies`, which Playwright sends as
   `Storage.setCookies` and the live gate refuses with every cookie API, by
   design (#1980: run 5 failed every route there). The cookie is the console
   fixture's: a `--settle generic` capture whose route list names no
   persona sets it on no route, and records `persona: null` (VQ5-PERSONA; BGD, 2026-10-22:
   "Skip it when no route names a persona and --settle generic"). Then it
   navigates and waits for `load`, then up to NETWORK_IDLE_MS for
   the network to go quiet (the console polls, so it may never be quiet for
   long: not reaching it is recorded, not fatal), then `document.fonts.ready`,
   then the route's `ready` selector, then until the page shows no pending
   read for SETTLE_QUIET_POLLS polls in a row (PENDING_JS: the console's
   skeletons and its "reading…") AND its body text has not changed for
   TEXT_QUIET_MS (TEXT_JS: a read that lands with no marker changes only the
   text), for up to SETTLE_TIMEOUT_MS. A route still reading then is shot as
   it stands, `unsettled` in its entry and listed in `failed` with what it
   still showed and where its text was still changing (never the text). The
   clock is frozen at the route list's
   instant when it carries one, or at `--clock` (FOR ANY APP, below) (`page.clock.set_fixed_time`, before the first navigation; timers
   still run) so "3 minutes ago" reads the same every run. Viewport-sized
   SEGMENTS, scrolling the page's own scroller (MEASURE_JS: the nearest one
   holding the route's ready element, the document, or the largest on the
   page; its name is the entry's `scroller`) one of its heights at a time, less
   what is stuck over its top once scrolled and what is fixed over its foot
   (STUCK_JS: rows under a sticky header or the Reads bar are otherwise in no
   image; each step's overlap is in the entry's `overlaps`, split in
   `covers`), until the scroller's end or the budget: MAX_SEGMENTS at
   phone-390 (TO_THE_END_PRESETS, owner decision c49), the route's `segments`
   (default DEFAULT_SEGMENTS) at the desktop. A capture cut at its budget
   before the end says so (`truncated`, `reached`, `total`) and is listed in
   `failed`. Segments, because a model is handed a tall
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

FOR ANY APP (VQ5-CAPTURE-GENERIC, docs/design/visual-qa-any-app.md §4.2;
part of #1933). Three parts of the above are the console's, and each is a
default only the console's route list and template rely on:

* `--keep-storage` adds no RESET_STATE_JS: an own-login target keeps its
  session in its storage, and emptying it on every load signs the capture out
  after its first route.
* `--settle generic` never evaluates PENDING_JS (or TEXT_JS): someone else's
  app draws none of the console's markers, and its own "reading…" is content.
  It waits for network idle, as every capture does, and then until the DOM
  has not changed (DOM_CHANGES_JS, a MutationObserver's count) for
  GENERIC_QUIET_MS, bounded by SETTLE_TIMEOUT_MS as the console's settle is.
* The clock is frozen only when asked: by the route list's `clock` (the
  console's routes.json carries one, so its captures are unchanged) or by
  `--clock`. Freezing `Date` can expire an app's own session token, so a
  route list with neither runs on the page's real clock.

`discover` writes a route list for an app nobody listed (§4.1): from `--base`
it loads each page through the live browser, reads its links (LINKS_JS, the
rendered `a[href]`s), and keeps the same-origin ones (scheme, host and port;
the path only: a query or fragment can carry a token), breadth first. It
never lists or loads a path matching an exclusion (DEFAULT_EXCLUDE, always,
plus each `--exclude` glob) or a `robots.txt` `Disallow` line of the `*`
group; it lists at most `--max-pages` (DISCOVER_MAX_PAGES at most) paths at
most `--max-depth` (DISCOVER_MAX_DEPTH at most) links from the start, and
loads only the pages whose links could still be listed, so a page at the
depth cap is listed and never loaded. The route list it writes has no
`clock`, `ready` "body" on every route, and groups of at most
DISCOVER_GROUP_SIZE routes, and `load_routes` accepts it. It sets no persona
cookie and no storage reset, and switches no device or theme.

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
import fnmatch
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
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
#: The presets captured until the scroller's end, up to MAX_SEGMENTS, whatever
#: the route's `segments` (owner decision c49, 2026-10-22, QAFIX4-QATOOL): in
#: visual QA run 4 six phone captures stopped at their budget before the page
#: ended, and the rows below were in no image. A route's `segments` governs
#: every other preset (the desktop), where a page is a few viewports tall.
TO_THE_END_PRESETS = frozenset({"phone-390"})
THEMES = ("light", "dark")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
#: Who the fixture build answers as, per route (QAFIX4-FIXTURE-A, run-4
#: admin-004 and admin-005): `member` is alice@example.com in u-bogdan, not a
#: platform admin and administering no group workspace, and is every route
#: that names none; `platform-admin` is a platform admin who also administers
#: one group workspace. The console reads PERSONA_COOKIE once when api.ts
#: loads, in the fixture build only. A cookie and not storage, because
#: RESET_STATE_JS empties local and session storage on every load.
PERSONAS = ("member", "platform-admin")
DEFAULT_PERSONA = PERSONAS[0]
PERSONA_COOKIE = "swarm_qa_persona"
#: Writes the cookie text it is handed from the page, on the page's own origin:
#: the gate passes page.evaluate, and refuses Storage.setCookies (#1980). Write
#: only: nothing here ever reads a cookie back.
PERSONA_COOKIE_JS = "(cookie) => { document.cookie = cookie; }"

#: `swarm-handoff device` waits up to 30 s for the worker (DEVICE_WAIT_SECONDS
#: there); this is that plus room for the process itself.
HANDOFF_TIMEOUT_SECONDS = 60
NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 10_000
READY_TIMEOUT_MS = 30_000
SHOT_TIMEOUT_MS = 30_000
#: How long one route may go on showing a pending marker (PENDING_JS) after
#: its ready selector: three times the slowest read the preview build makes
#: (the Timeline, about 4.5 s). A route still reading then is shot as it
#: stands and listed in `failed`: a finding, never a silent image.
SETTLE_TIMEOUT_MS = 15_000
SETTLE_POLL_MS = 150
#: How long the page's body text must go unchanged (TEXT_JS) before it counts
#: as settled (QAFIX4-QATOOL, run 4's task-007, work-a-001..003): a read that
#: lands with no marker (the agent list's 'attempt … of 3', the Workflows
#: Cost's '—', the Timeline's per-lane event reads) changes only the text.
#: Longer than the fixture build's longest single timer (620 ms, api.ts; the
#: head's read is 350 ms), so a chain of reads whose next one starts without
#: changing the text in between is still inside the window, with room for a
#: slow machine; short enough that the Timeline's ~4.5 s of lane reads plus
#: this window stay well inside SETTLE_TIMEOUT_MS. It costs every route this
#: long at least. The page clock is frozen (`set_fixed_time`), so a relative
#: time ("3 minutes ago") does not keep the text moving.
TEXT_QUIET_MS = 1_500
#: `--settle generic` (VQ5-CAPTURE-GENERIC, visual-qa-any-app.md §4.2): how
#: long the DOM must go unchanged (DOM_CHANGES_JS) before the page counts as
#: settled. The design's figure: the bound is SETTLE_TIMEOUT_MS, as today.
GENERIC_QUIET_MS = 500
#: The settle each capture may use: the console's (PENDING_JS and TEXT_JS,
#: the default, which the console template relies on) or any app's.
SETTLES = ("console", "generic")
#: Polls in a row with nothing pending before the page counts as settled:
#: one read landing and the next starting can leave a frame with no marker.
SETTLE_QUIET_POLLS = 2
#: An error message kept in the manifest is one line, this long at most.
MAX_ERROR_CHARS = 300
#: Each console or page error kept beside a route's images.
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 500

#: `discover`'s caps (visual-qa-any-app.md §1 `screens.discover`): 60 pages
#: is a run's screen cap (8 groups of at most 8), 3 links deep is as far as a
#: bounded crawl of someone else's app goes.
DISCOVER_MAX_PAGES = 60
DISCOVER_MAX_DEPTH = 3
#: A capture step's screens (§4.1: groups of at most 8).
DISCOVER_GROUP_SIZE = 8
#: Every discovered route's ready selector: nothing about an unknown page is
#: known but that it has a body.
DISCOVER_READY = "body"
#: Never listed or loaded, whatever the target says (§1 `exclusions`): a
#: capture that signs itself out captures the login page everywhere after.
DEFAULT_EXCLUDE = ("/logout", "/logout/*", "/signout", "/signout/*")
ROBOTS_TIMEOUT_SECONDS = 10
ROBOTS_MAX_BYTES = 512 * 1024
#: What an unreachable robots.txt (a 5xx, or no answer) means: everything is
#: disallowed (RFC 9309 §2.3.1.4). A 4xx means there is none: nothing is.
ROBOTS_UNREACHABLE = "User-agent: *\nDisallow: /\n"
#: A page's links are read up to this many.
MAX_LINKS = 2000

DIFF_THRESHOLD = 0.001
#: The colour a changed pixel is painted in a diff image (magenta: a console
#: page never fades to it, so a mark is never mistaken for the page).
DIFF_MARK = (255, 0, 255)
#: A PNG member of a tarball is read only up to this size.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

FONTS_READY_JS = "() => document.fonts.ready.then(() => true)"
#: The element that scrolls (QA-TOOLS-FIX, QA-003). The console's document does
#: not scroll: the route's content scrolls inside `div.ctl-scroll` (Spine.tsx),
#: and the first run, which looked only at the document, `main` and
#: `[role=dialog]`, took one segment of every route. So: the nearest ancestor
#: of the route's ready element that scrolls and has room; else the document,
#: when it has room; else the largest element on the page that scrolls and has
#: room, by the area it shows (the 64 px rail scrolls too, and is not the
#: page). No class is named, so a console that moves its scroller still
#: segments. Kept on `window` for SCROLL_JS; returns
#: `[scrollHeight, the height it shows, what it is]`.
MEASURE_JS = """(ready) => {
  const doc = document.scrollingElement || document.documentElement;
  const room = (el) => el.scrollHeight - el.clientHeight > 1;
  const scrolls = (el) => /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY) && room(el);
  let best = null;
  let content = null;
  try { content = ready ? document.querySelector(ready) : null; } catch (e) { content = null; }
  for (let el = content; el && el !== doc && best === null; el = el.parentElement) {
    if (scrolls(el)) best = el;
  }
  if (best === null && room(doc)) best = doc;
  if (best === null) {
    let area = 0;
    for (const el of document.querySelectorAll('*')) {
      if (el !== doc && scrolls(el) && el.clientWidth * el.clientHeight > area) {
        best = el; area = el.clientWidth * el.clientHeight;
      }
    }
  }
  if (best === null) best = doc;
  window.__swarmQaScroller = best;
  const name = best === doc ? 'document'
    : best.localName + (best.id ? '#' + best.id : '') + Array.from(best.classList, (c) => '.' + c).join('');
  return [best.scrollHeight, best === doc ? innerHeight : best.clientHeight, name];
}"""
#: What the console draws while a read is in flight (QA-TOOLS-FIX, QA-027):
#: a skeleton (`.skeleton`, the `*-skel*` bars, `.ctl-pending`'s moving
#: surface), a region marked `aria-busy`, the page head's refresh while it
#: reads (Shell.tsx RefreshControl: `.c-refresh`, aria-label "Reading"), and
#: the word "reading…" that every other pending read writes. The preview
#: build reads on timers with no network traffic, so network idle and the
#: ready selector are both met mid-read (the head's read alone is 350 ms, the
#: Timeline's 26 tasks' attempts about 4.5 s). Only what is drawn counts: an
#: element with no box, or hidden, is not on the image. Returns up to
#: five descriptions; [] is a page that has settled.
PENDING_JS = """() => {
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const name = (el) => el.localName + Array.from(el.classList, (c) => '.' + c).join('');
  const found = [];
  const markers = '.skeleton, [class*="skel"], .ctl-pending, [aria-busy="true"], .c-refresh[aria-label="Reading"]';
  for (const el of document.querySelectorAll(markers)) {
    if (found.length >= 5) return found;
    if (shown(el)) found.push(name(el));
  }
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walk.nextNode(); node && found.length < 5; node = walk.nextNode()) {
    if (node.data.includes('reading…') && node.parentElement && shown(node.parentElement)) {
      found.push('"reading…" in ' + name(node.parentElement));
    }
  }
  return found;
}"""
#: What the body text is, without the text (QAFIX4-QATOOL): its length, a
#: 32-bit FNV-1a hash of `document.body.innerText` (what is drawn), and the
#: element the last DOM change landed in (a MutationObserver this installs on
#: the first call; null before any), so the settle loop can say where a page
#: was still changing. The page's text never leaves the page: a manifest is
#: read by people who may not see the fixture's data, and a live console's
#: text is a tenant's.
TEXT_JS = """() => {
  const name = (el) => el.localName + (el.id ? '#' + el.id : '') + Array.from(el.classList, (c) => '.' + c).join('');
  if (!window.__swarmQaText) {
    window.__swarmQaText = {where: null};
    if (typeof MutationObserver === 'function') {
      new MutationObserver((changes) => {
        const target = changes[changes.length - 1].target;
        const el = target.nodeType === 1 ? target : target.parentElement;
        if (el) window.__swarmQaText.where = name(el);
      }).observe(document.body, {childList: true, characterData: true, subtree: true});
    }
  }
  const text = document.body.innerText || '';
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return [text.length, (h >>> 0).toString(16).padStart(8, '0'), window.__swarmQaText.where];
}"""
#: How far down from the scroller's top edge what is stuck there covers it
#: (QAFIX3-QATOOL, run 3's task-021): at 390 the task list's tab bar
#: (`.ag-list-tabs`, sticky at top 0) is 55 px, and a segment scrolled by the
#: scroller's whole height showed its first 55 px of new rows under the bar,
#: in no image. Counted: an element with a box whose `position` is `fixed`
#: and which spans the scroller's top edge, or is `sticky` with a `top`
#: inset, sits at that inset (it is stuck, not merely further down) and
#: sticks to THIS scroller (no scrolling element between them: a table's own
#: sticky head covers the table, not the page); either under half the height
#: the scroller shows (a sticky side rail is as tall as the page and covers
#: no row across it). Measured after each scroll, because a header sticks
#: only once scrolled and only inside its own section.
#: A sticky inset is measured from the scroller's PADDING edge, inside its
#: border and top padding (QAFIX4-QATOOL, run 4's task-006): at 390 the open
#: task's tab strip (`.ag-tabs-edge`, agents.css) is `sticky; top: -18px`
#: inside `div.drawer.ctl-drawer.ag-split`, which has 18 px of top padding,
#: so stuck it sits at the scroller's top edge; measured from the border
#: edge it was expected 18 px higher and was never counted.
#: AND WHAT COVERS THE SCROLLER'S FOOT: a `fixed` element with a box that
#: spans the scroller's bottom edge (the Reads bar at 390 covered the
#: drawer's bottom ~45 px), under half its height, measured the same way.
#: Returns `[top, bottom]`, whole px.
STUCK_JS = """() => {
  const doc = document.scrollingElement || document.documentElement;
  const el = window.__swarmQaScroller || doc;
  const top = el === doc ? 0 : el.getBoundingClientRect().top;
  const view = el === doc ? innerHeight : el.clientHeight;
  const foot = top + view;
  const own = el === doc ? null : getComputedStyle(el);
  const pad = own ? (parseFloat(own.borderTopWidth) || 0) + (parseFloat(own.paddingTop) || 0) : 0;
  const sticksHere = (n) => {
    for (let up = n.parentElement; up; up = up.parentElement) {
      if (up === el) return true;
      if (up !== doc && /^(auto|scroll|overlay)$/.test(getComputedStyle(up).overflowY)) return false;
    }
    return el === doc;
  };
  let cover = 0;
  let under = 0;
  for (const n of document.querySelectorAll('*')) {
    if (n === el || n === doc) continue;
    const style = getComputedStyle(n);
    if (style.position !== 'sticky' && style.position !== 'fixed') continue;
    if (n.getClientRects().length === 0 || style.visibility === 'hidden') continue;
    const r = n.getBoundingClientRect();
    if (r.height <= 0 || r.height >= view / 2 || r.bottom <= top) continue;
    if (style.position === 'fixed' && r.top > top + 1) {
      if (r.top < foot && r.bottom >= foot - 1) under = Math.max(under, foot - r.top);
      continue;
    }
    if (style.position === 'sticky') {
      const inset = parseFloat(style.top);
      if (Number.isNaN(inset) || Math.abs(r.top - (top + pad + inset)) > 1 || !sticksHere(n)) continue;
    }
    cover = Math.max(cover, r.bottom - top);
  }
  return [Math.round(cover), Math.round(under)];
}"""
#: Scrolls the scroller MEASURE_JS chose to `y` and, two frames later,
#: answers with STUCK_JS: what is stuck over its top and fixed over its foot there.
SCROLL_JS = """(y) => {
  const el = window.__swarmQaScroller || document.scrollingElement || document.documentElement;
  el.scrollTop = y;
  return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done((%s)()))));
}""" % STUCK_JS
#: QA-TOOLS-FIX (QA-077). The capture's tab is in the live browser's one
#: profile, so what a route left in storage drew the next route: the sidebar's
#: Recent workflows (Spine.tsx RecentWorkflows, filled from localStorage once
#: /workflows had been opened) appeared on every route after that one and on
#: none before it. Each route is its own load (`goto`), so the page's memory
#: starts empty; this, run before the console's own scripts on every load of
#: the console's origin in the capture's tab, empties its local and session
#: storage too, so every route starts from the state a first visit has. A
#: fresh browser context per route would do it as well, but opens a window
#: the gate has never been measured fitting to a preset. Other origins are
#: left alone; the console's localStorage is shared with any other tab on its
#: origin in this profile, which is the agent's own dev server.
RESET_STATE_JS = """(() => {
  if (location.origin !== %s) return;
  try { localStorage.clear(); } catch (e) {}
  try { sessionStorage.clear(); } catch (e) {}
})();"""
#: `--settle generic`: how many DOM changes this page has made since the
#: first call installed its MutationObserver, and the element the last one
#: landed in (null before any), so a page still changing can say where. No
#: text leaves the page. A page without MutationObserver answers 0 forever.
DOM_CHANGES_JS = """() => {
  const name = (el) => el.localName + (el.id ? '#' + el.id : '') + Array.from(el.classList, (c) => '.' + c).join('');
  if (!window.__swarmQaDom) {
    window.__swarmQaDom = {count: 0, where: null};
    if (typeof MutationObserver === 'function') {
      new MutationObserver((changes) => {
        window.__swarmQaDom.count += changes.length;
        const target = changes[changes.length - 1].target;
        const el = target.nodeType === 1 ? target : target.parentElement;
        if (el) window.__swarmQaDom.where = name(el);
      }).observe(document.documentElement, {childList: true, characterData: true, attributes: true, subtree: true});
    }
  }
  return [window.__swarmQaDom.count, window.__swarmQaDom.where];
}"""
#: `discover`: the page's rendered links, resolved by the browser.
LINKS_JS = "() => Array.from(document.querySelectorAll('a[href]'), (a) => a.href).slice(0, %d)" % MAX_LINKS
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
    # Optional (VQ5-CAPTURE-GENERIC): a route list with a clock asks for it
    # frozen; one without (a discovered one) runs on the real clock.
    if document.get("clock") is not None:
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
            if "persona" in route and route["persona"] not in PERSONAS:
                raise UsageError(f"route {rid}: persona must be one of {', '.join(PERSONAS)} (absent is {DEFAULT_PERSONA})")
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


def reset_state_script(origin: str) -> str:
    """RESET_STATE_JS for the console at `origin` (scheme://host[:port])."""
    return RESET_STATE_JS % json.dumps(origin)


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


def _fingerprint(answer: Any) -> tuple | None:
    """TEXT_JS's answer as (length, hash), or None when it is not one."""
    if isinstance(answer, (list, tuple)) and len(answer) >= 2:
        return (answer[0], answer[1])
    return None


def _settle(page) -> list[str]:
    """Wait until the page has shown no pending marker (PENDING_JS) on
    SETTLE_QUIET_POLLS polls in a row AND its body text (TEXT_JS) has not
    changed for TEXT_QUIET_MS, for up to SETTLE_TIMEOUT_MS. Returns what it
    still showed when the time ran out, and a line saying where its text was
    still changing (an element's name and a count, never the text); [] when
    it settled. The time is counted in the waits it asks for, so the bound is
    the same whatever the page costs to ask."""
    waited, quiet = 0, 0
    last, since, changes, where = None, 0, 0, None
    while True:
        pending = list(page.evaluate(PENDING_JS) or [])
        answer = page.evaluate(TEXT_JS)
        text = _fingerprint(answer)
        if text != last:
            if last is not None:
                changes += 1
                if isinstance(answer, (list, tuple)) and len(answer) > 2 and isinstance(answer[2], str):
                    where = answer[2]
            last, since = text, waited
        quiet = 0 if pending else quiet + 1
        still = waited - since >= TEXT_QUIET_MS
        if still and (quiet >= SETTLE_QUIET_POLLS or (not pending and waited >= SETTLE_TIMEOUT_MS)):
            return []
        if waited >= SETTLE_TIMEOUT_MS:
            out = [str(p)[:MAX_ERROR_CHARS] for p in pending]
            if not still:
                out.append(_one_line(
                    f"text still changing: {changes} change{'' if changes == 1 else 's'} in {waited} ms, "
                    f"the last {waited - since} ms before the shot"
                    + (f", last in {where}" if where else "")))
            return out
        page.wait_for_timeout(SETTLE_POLL_MS)
        waited += SETTLE_POLL_MS


def _px(value: Any, view: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(int(value), (view - 1) // 2))


def _stuck(answer: Any, view: int) -> tuple[int, int]:
    """SCROLL_JS's answer as (what is stuck over the scroller's top, what is
    fixed over its foot), in px: a bare number is the top alone (STUCK_JS
    before QAFIX4-QATOOL), anything else is 0, and each is under half the
    view (STUCK_JS counts nothing taller), so the two together leave a step
    of at least 1 px and every segment still moves on."""
    if isinstance(answer, (list, tuple)):
        return (_px(answer[0], view) if answer else 0, _px(answer[1], view) if len(answer) > 1 else 0)
    return (_px(answer, view), 0)


def segment_budget(route: dict, preset: str) -> int:
    """How many segments a route may take at `preset`: MAX_SEGMENTS at a
    preset captured to the scroller's end (TO_THE_END_PRESETS, owner
    decision c49), else the route's `segments`."""
    if preset in TO_THE_END_PRESETS:
        return MAX_SEGMENTS
    return max(1, int(route.get("segments", DEFAULT_SEGMENTS)))


def persona_of(route: dict) -> str:
    """The persona a route is captured as: its `persona`, or the member."""
    return route.get("persona", DEFAULT_PERSONA)


def sets_persona(routes: dict, settle: str) -> bool:
    """Whether the capture sets PERSONA_COOKIE at all: always under the
    console's settle; under `--settle generic` only when some route in the
    route list names a persona. Someone else's app has no use for the
    console fixture's cookie (VQ5-PERSONA)."""
    return settle != "generic" or any(
        "persona" in route for group in routes["groups"].values() for route in group)


def _settle_generic(page) -> list[str]:
    """`--settle generic`: wait until the DOM has not changed
    (DOM_CHANGES_JS) for GENERIC_QUIET_MS, for up to SETTLE_TIMEOUT_MS.
    Never evaluates PENDING_JS or TEXT_JS. Returns [] when it settled, else
    one line saying how much and where the DOM was still changing."""
    waited, last, since, changes, where = 0, None, 0, 0, None
    while True:
        answer = page.evaluate(DOM_CHANGES_JS)
        count = answer[0] if isinstance(answer, (list, tuple)) and answer else None
        if isinstance(answer, (list, tuple)) and len(answer) > 1 and isinstance(answer[1], str):
            where = answer[1]
        if count != last:
            if last is not None:
                changes += 1
            last, since = count, waited
        if waited - since >= GENERIC_QUIET_MS:
            return []
        if waited >= SETTLE_TIMEOUT_MS:
            return [_one_line(
                f"DOM still changing: {changes} change{'' if changes == 1 else 's'} in {waited} ms, "
                f"the last {waited - since} ms before the shot" + (f", last in {where}" if where else ""))]
        page.wait_for_timeout(SETTLE_POLL_MS)
        waited += SETTLE_POLL_MS


def _capture_route(page, errors: _Errors, *, base: str, group: str, route: dict, preset: str, theme: str,
                   out: Path, axe_source: str | None, settle: str = "console",
                   persona_cookie: bool = True) -> dict:
    """One route at one preset and theme. Raises with the step that failed.
    `persona_cookie` False (sets_persona) sets no PERSONA_COOKIE."""
    rid, ready = route["id"], route["ready"]
    stem = _stem(group, rid, preset, theme)
    persona = persona_of(route) if persona_cookie else None
    step = "persona"
    try:
        # Set for the member too: a cookie is not storage, RESET_STATE_JS
        # leaves it, and the route before may have been the admin. Written by
        # the page, so the page must be on the console's origin: the first
        # route (about:blank) and one after a failed load go there first;
        # every other route is still on the route before it's.
        if persona is not None:
            here = _origin(page.url)
            if here is None or here != _origin(base):
                page.goto(base, wait_until="load", timeout=NAV_TIMEOUT_MS)
            page.evaluate(PERSONA_COOKIE_JS, f"{PERSONA_COOKIE}={persona}; path=/; SameSite=Lax")
        # After the persona step, so what the origin's own load logged is not
        # the route's.
        errors.reset()
        step = "navigate"
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
        step = "settle"
        pending = _settle_generic(page) if settle == "generic" else _settle(page)
        step = "measure"
        measured = page.evaluate(MEASURE_JS, ready)
        total, view = measured[0], max(1, measured[1])
        budget = segment_budget(route, preset)
        images, overlaps, covers = [], [], []
        step = "segment 1"
        _top, under = _stuck(page.evaluate(SCROLL_JS, 0), view)
        y = 0
        for segment in range(1, budget + 1):
            step = f"segment {segment}"
            png = page.screenshot(type="png", animations="disabled", caret="hide", timeout=SHOT_TIMEOUT_MS)
            name = image_name(group, rid, preset, theme, segment)
            _write(out / name, png)
            images.append(name)
            if segment == budget or y + view >= total:
                break
            # One scroller-height on, less what is fixed over its foot here
            # (the Reads bar) and what is stuck over its top there (a sticky
            # header, a drawer's tab strip), both from STUCK_JS, so the rows
            # under either are in this segment or the next.
            nxt = y + view - under
            stuck, under_next = _stuck(page.evaluate(SCROLL_JS, nxt), view)
            if stuck:
                nxt -= stuck
                _, under_next = _stuck(page.evaluate(SCROLL_JS, nxt), view)
            overlaps.append(stuck + under)
            covers.append({"top": stuck, "bottom": under})
            y, under = nxt, under_next
        # Cut before the end: at its budget, the route's at the desktop or
        # MAX_SEGMENTS at a phone. Never recorded as complete (c49).
        reached = min(y + view, total)
        truncated = reached < total
        step = "text"
        _write(out / f"{stem}.txt", page.inner_text("body"))
        step = "a11y"
        snapshot = page.locator("body").aria_snapshot()
        _write(out / f"{stem}.a11y.json", json.dumps({"format": "aria-snapshot", "snapshot": snapshot}, indent=1))
        entry = {
            "id": rid, "preset": preset, "theme": theme, "persona": persona,
            "status": "unsettled" if pending else "captured",
            "path": route["path"], "images": images, "network_idle": idle,
            "settled": not pending, "pending": pending, "scroller": measured[2] if len(measured) > 2 else None,
            "overlaps": overlaps, "covers": covers,
            "truncated": truncated, "reached": reached, "total": total,
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
            out: Path, bundle: Path, axe_source: str | None = None, keep_storage: bool = False,
            settle: str = "console", clock: str | None = None) -> dict:
    """Capture `group` at every preset and theme; write the manifest and the
    tarball, whatever failed. Returns the manifest. `clock` (else the route
    list's) freezes the page clock; neither leaves it running."""
    frozen = clock if clock is not None else routes.get("clock")
    playwright, browser, context = _connect()
    manifest: dict = {
        "format": MANIFEST_FORMAT, "group": group, "commit": _git_head(),
        "browser": getattr(browser, "version", None), "playwright": _playwright_version(),
        "clock": frozen, "settle": settle, "keep_storage": keep_storage,
        "base": base, "presets": presets, "themes": themes,
        "routes": [], "failed": [],
    }
    group_routes = routes["groups"][group]
    persona_cookie = sets_persona(routes, settle)

    def fail_all(preset: str, theme: str, why: str) -> None:
        manifest["failed"].extend({"id": r["id"], "preset": preset, "theme": theme, "error": why}
                                  for r in group_routes)

    page = None
    try:
        page = context.new_page()
        parts = urlsplit(base)
        if not keep_storage:
            page.add_init_script(script=reset_state_script(f"{parts.scheme}://{parts.netloc}".lower()))
        errors = _Errors(page)
        if frozen is not None:
            page.clock.set_fixed_time(clock_at({"clock": frozen}))
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
                        entry = _capture_route(page, errors, base=base, group=group, route=route,
                                               preset=preset, theme=theme, out=out, axe_source=axe_source,
                                               settle=settle, persona_cookie=persona_cookie)
                        manifest["routes"].append(entry)
                        why = []
                        if entry["pending"]:
                            why.append(f"did not settle in {SETTLE_TIMEOUT_MS} ms: still showing "
                                       + "; ".join(entry["pending"]) + " (shot as it stood)")
                        if entry["truncated"]:
                            why.append(f"cut at {len(entry['images'])} segments: reached {entry['reached']} "
                                       f"of {entry['total']} px (the rest is in no image)")
                        if why:
                            manifest["failed"].append({"id": route["id"], "preset": preset, "theme": theme,
                                                       "error": "; ".join(why)})
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


# -- discover ------------------------------------------------------------------


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) address, the port made explicit;
    None for anything else."""
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port or (443 if scheme == "https" else 80)


def same_origin_path(href: Any, origin: tuple[str, str, int]) -> str | None:
    """The path of `href` when it is on `origin`, else None. The path only:
    a query or a fragment can carry a token, and is no route."""
    if _origin(href) != origin:
        return None
    return urlsplit(str(href)).path or "/"


def excluded(path: str, globs: list[str] | tuple[str, ...]) -> bool:
    """Whether `path` matches an exclusion glob, case-insensitively."""
    return any(fnmatch.fnmatchcase(path.lower(), g.lower()) for g in globs)


def robots_disallow(text: str | None) -> list[str]:
    """The `Disallow` values of robots.txt's group(s) for user agent `*`
    (consecutive `User-agent` lines are one group). An empty `Disallow`
    disallows nothing; no robots.txt disallows nothing. `Allow` is not read:
    the design honours `Disallow` (visual-qa-any-app.md §3), so an `Allow`
    inside a disallowed prefix stays unvisited, the cautious side."""
    rules: list[str] = []
    agents: list[str] = []
    in_agents = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agents:
                agents = []
            agents.append(value)
            in_agents = True
            continue
        in_agents = False
        if key == "disallow" and value and "*" in agents:
            rules.append(value)
    return rules


def disallowed(path: str, rules: list[str]) -> bool:
    """Whether a `Disallow` rule matches `path`: a prefix, `*` any run of
    characters, a trailing `$` the end of the path (RFC 9309 §2.2.3)."""
    for rule in rules:
        anchored = rule.endswith("$")
        body = rule[:-1] if anchored else rule
        pattern = ".*".join(re.escape(piece) for piece in body.split("*")) + ("$" if anchored else "")
        if re.match(pattern, path):
            return True
    return False


def _fetch_robots(url: str) -> str | None:
    """robots.txt's text; None when there is none (a 4xx);
    ROBOTS_UNREACHABLE when the site did not answer (a 5xx, or no answer)."""
    request = urllib.request.Request(url, headers={"User-Agent": "swarm-qa"})
    try:
        with urllib.request.urlopen(request, timeout=ROBOTS_TIMEOUT_SECONDS) as answer:  # noqa: S310 -- http(s) only, checked
            return answer.read(ROBOTS_MAX_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None if 400 <= exc.code < 500 else ROBOTS_UNREACHABLE
    except (OSError, ValueError):
        return ROBOTS_UNREACHABLE


def route_id(path: str, taken: set[str]) -> str:
    """A file-name-safe route id for `path`, unique in `taken` (and added to it)."""
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:40].rstrip("-") or "home"
    rid, n = slug, 2
    while rid in taken:
        rid = f"{slug}-{n}"
        n += 1
    taken.add(rid)
    return rid


def discover(*, base: str, max_pages: int, max_depth: int, exclude: list[str]) -> dict:
    """The route list of what `base` links to, breadth first (module
    docstring). Returns `{"document": route list or None, "why": str}`:
    None when nothing could be listed (the start page excluded or
    disallowed), and why."""
    globs = list(DEFAULT_EXCLUDE) + [g for g in exclude if g not in DEFAULT_EXCLUDE]
    robots = _fetch_robots(base + "/robots.txt")
    rules = robots_disallow(robots)
    origin = _origin(base)
    if origin is None:
        raise UsageError("--base must be an http(s) origin")
    skipped = {"off_origin": 0, "excluded": 0, "robots": 0, "failed": 0}
    off: set[str] = set()
    seen: set[str] = set()
    listed: list[tuple[str, int]] = []
    failed: list[str] = []

    def admit(path: str, depth: int) -> None:
        if path in seen:
            return
        seen.add(path)
        if excluded(path, globs):
            skipped["excluded"] += 1
        elif disallowed(path, rules):
            skipped["robots"] += 1
        elif len(listed) < max_pages and depth <= max_depth:
            listed.append((path, depth))

    admit("/", 0)
    if not listed:
        why = "excluded" if skipped["excluded"] else "disallowed by robots.txt"
        return {"document": None, "why": f"the start page / is {why}: nothing to discover"}
    playwright, _browser, context = _connect()
    page = None
    try:
        page = context.new_page()
        at = 0
        # Load a page only while its links could still be listed: under the
        # depth cap, and before the page cap is reached.
        while at < len(listed) and len(listed) < max_pages:
            path, depth = listed[at]
            at += 1
            if depth >= max_depth:
                continue
            try:
                page.goto(base + path, wait_until="load", timeout=NAV_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                except Exception:  # noqa: BLE001 -- an app that polls may never be idle
                    pass
                hrefs = page.evaluate(LINKS_JS) or []
            except Exception:  # noqa: BLE001 -- a page that failed is listed; its capture says so
                skipped["failed"] += 1
                failed.append(path)
                continue
            for href in list(hrefs)[:MAX_LINKS]:
                found = same_origin_path(href, origin)
                if found is None:
                    if str(href) not in off:
                        off.add(str(href))
                        skipped["off_origin"] += 1
                    continue
                admit(found, depth + 1)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        playwright.stop()
    taken: set[str] = set()
    routes = [{"id": route_id(path, taken), "path": path, "ready": DISCOVER_READY} for path, _depth in listed]
    groups = {f"pages-{n + 1}": routes[i:i + DISCOVER_GROUP_SIZE]
              for n, i in enumerate(range(0, len(routes), DISCOVER_GROUP_SIZE))}
    document = {
        "format": ROUTES_FORMAT, "groups": groups, "mockups": [],
        "discovered": {
            "base": base, "max_pages": max_pages, "max_depth": max_depth, "exclude": globs,
            "robots": "absent" if robots is None else ("unreachable" if robots == ROBOTS_UNREACHABLE else "read"),
            "depths": {path: depth for path, depth in listed}, "skipped": skipped, "failed": failed,
        },
    }
    return {"document": document, "why": ""}


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
    cap.add_argument("--keep-storage", action="store_true",
                     help="never empty the app's local and session storage between loads (an own-login target)")
    cap.add_argument("--settle", choices=SETTLES, default="console",
                     help="console: the console's pending reads; generic: no DOM change for "
                          f"{GENERIC_QUIET_MS} ms")
    cap.add_argument("--clock", default=None,
                     help="freeze the page clock at this ISO 8601 instant (else the route list's clock, if any)")
    dis = sub.add_parser("discover", help="write a route list of the same-origin pages an app links to")
    dis.add_argument("--base", required=True, help="the app's origin, e.g. https://app.example")
    dis.add_argument("--max-pages", type=int, default=DISCOVER_MAX_PAGES)
    dis.add_argument("--max-depth", type=int, default=DISCOVER_MAX_DEPTH)
    dis.add_argument("--exclude", action="append", default=[],
                     help=f"a path glob never listed or loaded (repeatable; {', '.join(DEFAULT_EXCLUDE)} always)")
    dis.add_argument("--out", required=True, help="the route list to write, e.g. screens.json")
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
        if args.clock is not None:
            clock_at({"clock": args.clock})
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
                       themes=themes, out=out, bundle=bundle, axe_source=axe_source,
                       keep_storage=args.keep_storage, settle=args.settle, clock=args.clock)
    images = sum(len(r["images"]) for r in manifest["routes"])
    sys.stdout.write(json.dumps({"group": args.group, "images": images, "captured": len(manifest["routes"]),
                                 "failed": len(manifest["failed"]),
                                 "truncated": sum(1 for r in manifest["routes"] if r.get("truncated")),
                                 "aborted": manifest.get("aborted"), "bundle": str(bundle)}) + "\n")
    return EXIT_OK if images else EXIT_NOTHING_CAPTURED


def _discover_command(args) -> int:
    if not os.environ.get(ENDPOINT_ENV, "").strip():
        sys.stderr.write(f"swarm_qa: no live browser ({ENDPOINT_ENV} is not set): "
                         "discover reads the rendered pages, so it did not run\n")
        return EXIT_NO_LIVE_BROWSER
    base = args.base.rstrip("/")
    try:
        parts = urlsplit(base)
        if _origin(base) is None or parts.path or parts.query or parts.fragment:
            raise UsageError("--base must be an http(s) origin, with no path")
        if not 1 <= args.max_pages <= DISCOVER_MAX_PAGES:
            raise UsageError(f"--max-pages must be 1..{DISCOVER_MAX_PAGES}")
        if not 0 <= args.max_depth <= DISCOVER_MAX_DEPTH:
            raise UsageError(f"--max-depth must be 0..{DISCOVER_MAX_DEPTH}")
        if not all(isinstance(g, str) and g.startswith("/") for g in args.exclude):
            raise UsageError("--exclude takes path globs starting with /")
    except (UsageError, ValueError) as exc:
        sys.stderr.write(f"swarm_qa: {exc}\n")
        return EXIT_USAGE
    found = discover(base=base, max_pages=args.max_pages, max_depth=args.max_depth, exclude=args.exclude)
    document = found["document"]
    if document is None:
        sys.stderr.write(f"swarm_qa: {found['why']}\n")
        return EXIT_NOTHING_CAPTURED
    out = Path(args.out)
    tmp = out.with_name(out.name + ".tmp")
    _write(tmp, json.dumps(document, indent=1))
    os.replace(tmp, out)
    routes = sum(len(r) for r in document["groups"].values())
    sys.stdout.write(json.dumps({"routes": routes, "groups": len(document["groups"]),
                                 "skipped": document["discovered"]["skipped"], "out": str(out)}) + "\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        return _capture_command(args)
    if args.command == "discover":
        return _discover_command(args)
    out = Path(args.out)
    report = diff(Path(args.a), Path(args.b), out, args.threshold)
    sys.stdout.write(json.dumps({"compared": len(report["images"]), "over_threshold": report["over_threshold"],
                                 "only_in_a": len(report["only_in_a"]), "only_in_b": len(report["only_in_b"]),
                                 "report": str(out / "diff.json")}) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

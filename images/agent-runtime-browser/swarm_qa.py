"""Scripted visual-QA capture through the live browser (QA-CAPTURE, part of #1649).

    python -m swarm_qa capture \\
        --routes apps/swarm-ui/qa/routes.json --group work-a \\
        --base http://localhost:5173 \\
        --presets desktop,phone-390 --themes light,dark \\
        --out "$SWARM_ARTIFACTS_DIR/capture-work-a" \\
        --bundle "$SWARM_ARTIFACTS_DIR/capture-work-a.tar" [--axe] \\
        [--keep-storage] [--settle console|generic] [--clock INSTANT]

    python -m swarm_qa discover --base https://app.example[/path/] \\
        --max-pages 60 --max-depth 3 [--exclude GLOB ...] [--hash-routes] \\
        --out screens.json

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
   navigates and waits for `load` (a main document that answered 4xx or 5xx
   fails the route at `navigate`, with its status: #1981), then up to NETWORK_IDLE_MS for
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
   page; its name is the entry's `scroller`) by what the screen shows of it
   at a time, less what is stuck over its top once scrolled and what covers
   its foot (STUCK_JS: rows under a sticky header, the Reads bar or a strip
   stuck to the foot are otherwise in no image; each step's overlap is in the
   entry's `overlaps`, split in `covers`; what was measured at each segment,
   the scroller's box, `innerHeight` and the Reads bar's box, in `geometry`),
   until the scroller's end or the budget: MAX_SEGMENTS at
   phone-390 (TO_THE_END_PRESETS, owner decision c49), the route's `segments`
   (default DEFAULT_SEGMENTS) at the desktop. Where the scroller is and how
   tall it is are read back after every scroll (SCROLL_JS), never assumed
   from the offset asked for: one that did not move on is not shot again and
   its stop is the entry's `duplicate`. A capture that ends short of the end,
   at its budget or at such a stop, says so (`truncated`, `reached`, `total`)
   and is listed in `failed`. Segments, because a model is handed a tall
   image downscaled until its text is unreadable, and whether a full-page
   capture through the gate needs a metrics override is unverified.
   `animations="disabled"`, `caret="hide"`. A shot whose size is not the
   page's layout viewport (innerWidth and innerHeight over the visual
   viewport's scale, VIEWPORT_JS, read after it) is taken again, up to
   SHOT_RETRIES times, then once more after `swarm-handoff device <preset>`,
   each in the entry's `reshot`; one still off is kept, listed in `failed`
   and leaves the route `unsettled` (`_shot` says what was measured at
   phone-390, #1981, and in run 5b).
6. Records what the page said, beside the PNGs: console and page errors,
   failed requests, and the subresources that answered 4xx or 5xx with
   their status (`bad_responses`: an HTTP error is no failed request, so a
   "Failed to load resource" otherwise has no URL), each address as origin
   and path only (a query can carry a token), the body text, and the ARIA snapshot (`locator.aria_snapshot()`;
   Playwright deprecated `page.accessibility` in its favour). And what the
   LIVE GATE answered itself while the route loaded (`gate_refused`, below).
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

THE GATE'S REFUSALS ARE THE RUN'S, NOT THE APP'S (QA-GATE-REFUSALS, part of
#2067). The live gate answers some requests itself: a write, or anything but
GET and HEAD to another origin, gets its 403 (`qa_write_refused`), and a top
frame sent off the target gets its page (`qa_target_left`). A 403 to another
origin carries no CORS header, so Chrome logs a CORS block and a failed
request, and on 2026-10-23 a run filed exactly that (an app's OIDC token
exchange) as the app's blocker. So after each route the capture reads the
gate's refusal record (`_gate_document`: GET GATE_REFUSALS_PATH on
$SWARM_CDP_ENDPOINT, the endpoint it already connects to; method, origin,
path and code, never a query or a body) and writes the refusals made while
that route loaded into its `errors.json` as `gate_refused`, each refusal to
one route only; a failed request or a 4xx the gate made is marked as the
gate's (`GATE_MARK`, `bad_responses`' `gate`). The manifest's
`gate_refusals` counts the run's refusals by method and origin. A record
that cannot be read leaves `gate_refused` null, never `[]`, and is not read
again in that capture (`why` says what failed), so an unreadable gate costs
one timeout, not one a route.

EVERY SHOT MASKS THE QA TARGET'S CREDENTIAL (QA-SHOT-MASK, part of #2055).
The live gate replaces a QA target's configured credential (its password,
its challenge bypass value, and their URL and base64 forms) with
CREDENTIAL_MARK in everything it sends the agent, so the text and ARIA files
never carried it; a screenshot is pixels, which no scrub can match, and on
2026-10-23 a login demo that prints its own password on its home page had it
readable in every stored image. So before every screenshot (`_shot`, each
retake included; this tool takes no other) the capture finds where the page
draws it and covers it, using the gate's comparison and never the value:
this process never holds the credential, and nothing here asks for it.
MASK_FIND_JS returns the page's text as the gate scrubs it on the way back
(every text node; every form control's value, a select's options, a
placeholder, an alt, a ::before or ::after content, through open shadow
roots; and each tree scope's whole text), and wherever a mark came back,
that node is masked. A scope whose whole text holds more marks than its text
nodes do holds a credential split across elements, which `_mask_targets`
finds by descending, child by child (MASK_CHILDREN_JS), to the node whose
text holds the mark its children's do not. MASK_DRAW_JS then draws an opaque
MASK_COLOUR box over each such text box or element box, in the page, in a
closed shadow root in the top layer, for that shot only; MASK_CLEAR_JS takes
it down, so the next shot and the agent's own view are unchanged. Each
segment's `geometry` says how many places were masked (`masked`, a count,
never the value or its length), the entry sums them. IT FAILS CLOSED: a
sweep, a draw or a take-down that throws or answers malformed, or a mask the
page removed or changed before the take-down, drops that shot unread, and
the route fails at that segment saying why. KNOWN LIMITS: what the page draws
as pixels (a canvas, an image of text), and what lives in a closed shadow
root or another frame, is not text the sweep can read; nor is a mark the
gate's scrub would not make (its own limits, livegate/qatarget.py). A page
that shows the credential only between the sweep and the shot is not seen,
and the sweep runs in the page's own world (Playwright's `page.evaluate`),
so a page script that rewrites the DOM's methods could hide a box from it.

`discover` writes a route list for an app nobody listed (§4.1): from `--base`
(an http(s) address at a public host name, its path where the app starts:
`discover_base`) it loads each page through the live browser, reads its
links (LINKS_JS, the rendered `a[href]`s), and keeps the same-origin ones
(scheme, host and port) under the base's path, breadth first, as paths from
the origin (what `capture --base <origin>` takes). It keeps the path only: a
query or fragment can carry a token. `--hash-routes` also keeps a page's
links to itself whose fragment is a route (`#/active`, HASH_ROUTE), as
routes of their own, never `#section`. A page whose main document answered
4xx or 5xx is not listed (`http_errors`); one listed at a cap and never
loaded is `unchecked`, and its capture reads the status. It
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
import ipaddress
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
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
#: The live gate's refusal record on its CDP endpoint (livegate/qatarget.py
#: `REFUSALS_PATH`, restated: this image does not carry agent_worker; the
#: refusal tests hold them equal). Loopback only, as every path there is.
GATE_REFUSALS_PATH = "/json/swarm/qa-refusals"
GATE_TIMEOUT_SECONDS = 5
MAX_GATE_BYTES = 1 << 20
#: What a failed request the gate refused is marked with in `failed_requests`.
GATE_MARK = "refused by the live gate ({code}): the gate's answer, not the app's"
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
#: Host suffixes that only ever name something inside a cluster, a VPC or a
#: LAN: `swarm_egress.policy.INTERNAL_SUFFIXES`, restated because the image
#: cannot import it (a unit test holds the two equal). `discover --base`
#: refuses a host ending in one, as it refuses a single-label name and an IP
#: literal: discovery is for a deployed app, never an internal address.
INTERNAL_SUFFIXES = (
    ".local", ".localhost", ".internal", ".cluster.local", ".svc", ".localdomain",
    ".home.arpa", ".intranet", ".lan",
)
#: A hash route's path (`--hash-routes`): the fragment after `#`, up to its
#: own `?`, when it is `/` and path characters. `#section` is an anchor, not a
#: route, and `#access_token=...` is a credential; neither matches.
HASH_ROUTE = re.compile(r"/[A-Za-z0-9._~%/-]*")

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
#: THE FOOT IS WHERE THE SCREEN STOPS SHOWING THE SCROLLER (QAFIX6-CAPTURE,
#: run 6's 6:capacity-001 to -004 and 6:admin-005): `top + clientHeight`, or
#: the window's `innerHeight` when the scroller runs past it. At 390 the
#: scroller's edge was below the window, the Reads bar fixed at the window's
#: foot spanned no edge this looked at, and the rows under it were in no
#: image. So the third number is what the screen shows of the scroller,
#: `view`, re-measured with every scroll (the phone bar compacts from 44 to
#: 36 px once scrolled). A `sticky` element with a `bottom` inset resting on
#: the scroller's own foot covers it too (/submit's 'Not ready' strip, run
#: 6's 6:work-b-001), counted where the screen still shows it.
#: A TOP COVER SPANS THE SCROLLER (6:work-b-002): at 1440 the Submit forms'
#: 300 px sticky side panel was counted as a 371 px cover, over a scroller
#: 1100 px wide. Only a box at least half the scroller's width covers its
#: rows; the 390 tab bar spans all of it.
#: Returns `[top, bottom, view]`, whole px.
STUCK_JS = """() => {
  const doc = document.scrollingElement || document.documentElement;
  const el = window.__swarmQaScroller || doc;
  const top = el === doc ? 0 : el.getBoundingClientRect().top;
  const edge = top + (el === doc ? innerHeight : el.clientHeight);
  const foot = Math.min(edge, innerHeight);
  const view = foot - top;
  const wide = el.clientWidth;
  const own = el === doc ? null : getComputedStyle(el);
  const pad = own ? (parseFloat(own.borderTopWidth) || 0) + (parseFloat(own.paddingTop) || 0) : 0;
  const padFoot = own ? (parseFloat(own.borderBottomWidth) || 0) + (parseFloat(own.paddingBottom) || 0) : 0;
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
    if (r.height <= 0 || r.height >= view / 2 || r.bottom <= top || r.top >= foot) continue;
    if (style.position === 'fixed' && r.top > top + 1) {
      if (r.bottom >= foot - 1) under = Math.max(under, foot - r.top);
      continue;
    }
    if (style.position === 'sticky') {
      const inset = parseFloat(style.top);
      if (Number.isNaN(inset) || Math.abs(r.top - (top + pad + inset)) > 1) {
        const low = parseFloat(style.bottom);
        if (!Number.isNaN(low) && Math.abs(r.bottom - (edge - padFoot - low)) <= 1 && sticksHere(n)) {
          under = Math.max(under, foot - r.top);
        }
        continue;
      }
      if (!sticksHere(n)) continue;
    }
    if (r.width < wide / 2) continue;
    cover = Math.max(cover, r.bottom - top);
  }
  return [Math.round(cover), Math.round(under), Math.round(view)];
}"""
#: Scrolls the scroller MEASURE_JS chose to `y` and, two frames later,
#: answers with STUCK_JS's `[top, bottom, view]` there, then WHERE THE
#: SCROLLER WENT and how tall it is now (QAFIX6-CAPTURE, run 6's
#: 6:capacity-005: a scroller clamps below an offset past its end, and the
#: capture recorded the offset it asked for, 'reached 4527 of 4527' beside
#: two identical shots), then what was measured around it for the entry's
#: `geometry`: the scroller's box and `.ctl-dock`'s (the console's Reads
#: bar; null when there is none) as `[top, bottom]`, and `innerHeight`.
#: `[top, bottom, view, scrollTop, scrollHeight, {scroller, dock, inner}]`.
SCROLL_JS = """(y) => {
  const doc = document.scrollingElement || document.documentElement;
  const el = window.__swarmQaScroller || doc;
  el.scrollTop = y;
  const box = (n) => {
    if (!n || n.getClientRects().length === 0) return null;
    const r = n.getBoundingClientRect();
    return [Math.round(r.top), Math.round(r.bottom)];
  };
  return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done([
    ...(%s)(), Math.round(el.scrollTop), el.scrollHeight,
    {scroller: el === doc ? [0, innerHeight] : box(el), dock: box(document.querySelector('.ctl-dock')), inner: innerHeight},
  ]))));
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
#: The window's size, device pixel ratio and visual viewport scale, read
#: after each screenshot to check the shot is the layout viewport's size
#: (`_shot`). A browser with no `visualViewport` answers scale 1.
VIEWPORT_JS = ("() => [window.innerWidth, window.innerHeight, window.devicePixelRatio || 1, "
               "(window.visualViewport && window.visualViewport.scale) || 1]")
#: How many times `_shot` takes a screenshot again when it is not the size
#: of the viewport, before it keeps it and lists it in `failed`.
SHOT_RETRIES = 2
LINKS_JS = "() => Array.from(document.querySelectorAll('a[href]'), (a) => a.href).slice(0, %d)" % MAX_LINKS
AXE_INJECT_JS = "(source) => { (0, eval)(source); return typeof axe !== 'undefined'; }"
AXE_RUN_JS = "() => axe.run(document, {resultTypes: ['violations']})"

#: THE CREDENTIAL MASK (QA-SHOT-MASK, part of #2055; module docstring). What
#: the live gate writes in place of a QA target's credential in everything
#: bound for the agent (livegate/qatarget.py `PLACEHOLDER`, restated: this
#: image does not carry agent_worker; test_swarm_qa_shot_mask holds them
#: equal). The capture never holds the credential: it finds where the page
#: shows it by finding this in the page's own text, as the gate returned it.
CREDENTIAL_MARK = "[qa-credential]"
#: The mask's colour, opaque, and how far each box reaches past the text's
#: own box (anti-aliased glyph edges and underlines sit just outside it).
MASK_COLOUR = "#000000"
MASK_PAD_PX = 2
#: The window property one shot's sweep lives in, from MASK_FIND_JS to
#: MASK_CLEAR_JS: element references and the mask's host, never text.
MASK_KEY = "__swarmQaMask"
#: How many levels MASK_CHILDREN_JS descends looking for a credential split
#: across elements before the shot is refused: deeper than any page this
#: capture has met, and a bound, so a page cannot hold a shot open.
MASK_MAX_LEVELS = 512


def _mask_js(template: str) -> str:
    return (template.replace("__KEY__", MASK_KEY).replace("__COLOUR__", MASK_COLOUR)
            .replace("__PAD__", str(MASK_PAD_PX)))


#: Every place the page could draw text, as `[kind, node, scope, ...]` rows
#: whose strings the gate scrubs on the way back: `s` a tree scope's whole
#: text (the document's, each open shadow root's), `t` one text node (its
#: index among its parent's child nodes, then its text), `x` what an element
#: draws that is no text node (a form control's value, a select's options:
#: a list box draws them all, and an option's text has no box of its own;
#: its placeholder and alt, a ::before or ::after content, and every
#: attribute of an element whose content reads one). Whitespace is left out.
MASK_FIND_JS = _mask_js("""() => {
  const nodes = [];
  const index = new Map();
  const rows = [];
  const add = (node) => { index.set(node, nodes.length); nodes.push(node); return nodes.length - 1; };
  const text = (value) => typeof value === 'string' && /\\S/.test(value);
  const drawn = (el) => {
    const out = [];
    if ((el.localName === 'input' || el.localName === 'textarea') && text(el.value)) out.push(el.value);
    if (el.localName === 'select' && el.options) {
      for (const option of Array.from(el.options)) out.push(option.textContent || '');
    }
    for (const name of ['placeholder', 'alt', 'value', 'label']) out.push(el.getAttribute(name) || '');
    for (const pseudo of ['::before', '::after']) {
      const content = getComputedStyle(el, pseudo).content || '';
      if (content === 'none' || content === 'normal') continue;
      out.push(content);
      if (content.includes('attr(')) for (const a of Array.from(el.attributes)) out.push(a.value);
    }
    return out.filter(text);
  };
  const walk = (node, scope) => {
    const at = add(node);
    const own = scope === null ? at : scope;
    if (scope === null) rows.push(['s', at, own, node.textContent || '']);
    if (node.nodeType === 1) for (const value of drawn(node)) rows.push(['x', at, own, value]);
    Array.from(node.childNodes).forEach((child, k) => {
      if (child.nodeType === 3) { if (text(child.data)) rows.push(['t', at, own, k, child.data]); }
      else if (child.nodeType === 1) walk(child, own);
    });
    if (node.nodeType === 1 && node.shadowRoot) walk(node.shadowRoot, null);
  };
  const left = window.__KEY__;
  if (left && left.host) left.host.remove();
  walk(document.documentElement, null);
  Object.defineProperty(window, '__KEY__', {value: {nodes, index, host: null, root: null, boxes: 0},
    configurable: true, writable: true, enumerable: false});
  return rows;
}""")
#: The element children of each node named, as `[node, child, text]`: the
#: descent that finds a credential split across elements (`_mask_targets`).
MASK_CHILDREN_JS = _mask_js("""(ids) => {
  const state = window.__KEY__;
  if (!state) throw new Error('the mask sweep is gone: the page changed under it');
  const rows = [];
  for (const at of ids) {
    const node = state.nodes[at];
    if (!node) throw new Error('a node of the mask sweep is gone');
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType !== 1) continue;
      const text = child.textContent || '';
      if (!/\\S/.test(text)) continue;
      const idx = state.index.get(child);
      if (idx === undefined) throw new Error('the page changed under the mask sweep');
      rows.push([at, idx, text]);
    }
  }
  return rows;
}""")
#: An opaque box over each target's boxes on the screen (`t` a text node,
#: `n` all the text under a node, `x` an element's own border boxes), in a
#: closed shadow root no page style reaches, on a manual popover so it is in
#: the top layer over anything the page stacks, fixed to the viewport (the
#: boxes are the viewport's). Answers `[sites drawn, boxes]`.
MASK_DRAW_JS = _mask_js("""(targets) => {
  const state = window.__KEY__;
  if (!state) throw new Error('the mask sweep is gone: the page changed under it');
  const boxes = [];
  let sites = 0;
  for (const [kind, at, k] of targets) {
    const node = state.nodes[at];
    if (!node) throw new Error('a node of the mask sweep is gone');
    let rects;
    if (kind === 'x') {
      rects = node.getClientRects();
    } else {
      const range = document.createRange();
      if (kind === 't') {
        const child = node.childNodes[k];
        if (!child || child.nodeType !== 3) throw new Error('a text node of the mask sweep is gone');
        range.selectNodeContents(child);
      } else {
        range.selectNodeContents(node);
      }
      rects = range.getClientRects();
    }
    let drew = false;
    for (const r of Array.from(rects)) {
      if (r.width > 0 && r.height > 0) { boxes.push(r); drew = true; }
    }
    if (drew) sites += 1;
  }
  if (!boxes.length) return [0, 0];
  const css = (el, decls) => { for (const [k, v] of Object.entries(decls)) el.style.setProperty(k, v, 'important'); };
  const host = document.createElement('swarm-qa-mask');
  host.setAttribute('popover', 'manual');
  css(host, {display: 'block', position: 'fixed', left: '0px', top: '0px', right: 'auto', bottom: 'auto',
    width: '100%', height: '100%', margin: '0px', padding: '0px', border: '0px', background: 'transparent',
    overflow: 'visible', 'pointer-events': 'none', opacity: '1', visibility: 'visible', transform: 'none',
    filter: 'none', 'z-index': '2147483647'});
  const root = host.attachShadow({mode: 'closed'});
  const pad = __PAD__;
  for (const r of boxes) {
    const box = document.createElement('div');
    css(box, {position: 'absolute', display: 'block', margin: '0px', padding: '0px', border: '0px',
      left: (r.left - pad) + 'px', top: (r.top - pad) + 'px',
      width: (r.width + 2 * pad) + 'px', height: (r.height + 2 * pad) + 'px', background: '__COLOUR__'});
    root.appendChild(box);
  }
  state.host = host;
  state.root = root;
  state.boxes = boxes.length;
  document.documentElement.appendChild(host);
  host.showPopover();
  return [sites, boxes.length];
}""")
#: Takes the mask down and ends the sweep. Answers `[intact, boxes]`: false
#: when the sweep is gone (the page navigated) or the mask was not whole and
#: on top at the moment it was taken down, so the shot before it is refused.
MASK_CLEAR_JS = _mask_js("""() => {
  const state = window.__KEY__;
  if (!state) return [false, 0];
  delete window.__KEY__;
  if (!state.host) return [true, 0];
  const host = state.host;
  let open = false;
  try { open = host.matches(':popover-open'); } catch (e) { open = false; }
  const intact = open && host.isConnected && host.parentNode === document.documentElement
    && state.root.childElementCount === state.boxes;
  try { host.hidePopover(); } catch (e) { /* already closed */ }
  host.remove();
  return [intact, state.boxes];
}""")


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


def _gate_url(endpoint: str) -> str:
    """GATE_REFUSALS_PATH on the gate's CDP endpoint, its client tag kept."""
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}{GATE_REFUSALS_PATH}" + (
        f"?{parts.query}" if parts.query else "")


def _gate_document() -> dict:
    """The live gate's refusal record (module docstring). Raises when it cannot be read."""
    url = _gate_url(os.environ.get(ENDPOINT_ENV, "").strip())
    # No proxy: the gate is on loopback, and an egress proxy must not see this.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=GATE_TIMEOUT_SECONDS) as answer:
        document = json.loads(answer.read(MAX_GATE_BYTES))
    if not isinstance(document, dict) or not isinstance(document.get("refusals"), list):
        raise ValueError("the gate's answer is not a refusal record")
    return document


def _refusal(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    n, at = raw.get("n"), raw.get("at")
    fields = {k: raw.get(k) for k in ("method", "origin", "path", "code")}
    if not isinstance(n, int) or not isinstance(at, (int, float)) or not all(isinstance(v, str) for v in fields.values()):
        return None
    return {"n": n, "at": float(at), **{k: v[:MAX_ERROR_CHARS] for k, v in fields.items()}}


def _gate_key(origin: str, path: str) -> str:
    return f"{origin.lower()}{path}"


class _GateRefusals:
    """The gate's refusals in this capture, each given to the one route that
    was loading when the gate made it (module docstring)."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.why: str | None = None
        self.armed: bool | None = None
        self.seen: dict[int, dict] = {}
        self.given: set[int] = set()

    def _read(self) -> bool:
        if self.why is not None:
            return False
        try:
            document = _gate_document()
        except Exception as exc:  # noqa: BLE001 -- an unread record is said, never a failed capture
            self.why = f"{type(exc).__name__}: {_one_line(exc)}"
            return False
        self.armed = document.get("armed") is True
        for raw in document["refusals"]:
            record = _refusal(raw)
            if record is not None and record["at"] >= self.started:
                self.seen[record["n"]] = record
        return True

    def during(self, start: float, end: float) -> list[dict] | None:
        """`gate_refused` for a route loaded from `start` to `end`; None when unread."""
        if not self._read():
            return None
        out = []
        for n in sorted(self.seen):
            record = self.seen[n]
            if n not in self.given and start <= record["at"] <= end:
                self.given.add(n)
                out.append({k: record[k] for k in ("method", "origin", "path", "code")})
        return out

    def summary(self) -> dict:
        """The manifest's `gate_refusals`: the run's refusals by method and origin."""
        self._read()
        counts: dict[tuple[str, str], int] = {}
        for record in self.seen.values():
            key = (record["method"], record["origin"])
            counts[key] = counts.get(key, 0) + 1
        out: dict = {"read": self.why is None, "armed": self.armed, "count": len(self.seen),
                     "by": [{"method": m, "origin": o, "count": c} for (m, o), c in sorted(counts.items())]}
        if self.why is not None:
            out["why"] = self.why
        return out


class _Errors:
    """What the page reported while one route was on screen."""

    def __init__(self, page):
        self.reset()
        page.on("console", self._console)
        page.on("pageerror", self._page_error)
        page.on("requestfailed", self._request_failed)
        page.on("response", self._response)

    def reset(self):
        #: When this route began, for the gate's refusals (`_GateRefusals.during`).
        self.since = time.time()
        self.console: list[str] = []
        self.page: list[str] = []
        self.requests: list[str] = []
        self.responses: list[dict] = []

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

    def _response(self, response):
        # An HTTP error is no failed request (`requestfailed` is a network
        # failure), so without this a 404 left only the console's "Failed to
        # load resource", with no URL (#1981).
        try:
            status = response.status
            if isinstance(status, int) and status >= 400:
                self.responses.append({"url": _where(response.url), "status": status})
        except Exception:  # noqa: BLE001
            pass

    def document(self, gate_refused: list[dict] | None = None) -> dict:
        """The route's errors.json; a request the gate refused is marked as the gate's."""
        codes = {_gate_key(r["origin"], r["path"]): r["code"] for r in gate_refused or []}
        requests = []
        for where in self.requests[:MAX_MESSAGES]:
            code = codes.get(_gate_key(*_split_where(where)))
            requests.append(f"{where} ({GATE_MARK.format(code=code)})" if code else where)
        responses = []
        for response in self.responses[:MAX_MESSAGES]:
            code = codes.get(_gate_key(*_split_where(response["url"])))
            responses.append({**response, "gate": code} if code else response)
        return {"console_errors": self.console[:MAX_MESSAGES], "page_errors": self.page[:MAX_MESSAGES],
                "failed_requests": requests, "bad_responses": responses, "gate_refused": gate_refused}


def _split_where(where: str) -> tuple[str, str]:
    """`_where`'s origin-and-path back into (origin, path)."""
    try:
        parts = urlsplit(where)
    except ValueError:
        return where, ""
    return f"{parts.scheme}://{parts.netloc}", parts.path or "/"


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


def _whole(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _scrolled(answer: Any, asked: int, view: int, total: int) -> tuple[int, int, int, int, int, dict]:
    """SCROLL_JS's answer as `(at, top, bottom, view, total, geometry)`:
    where the scroller is (read back; `asked` when the answer does not
    say), what is stuck over its top and fixed over its foot (`_stuck`),
    what the screen shows of it, its scrollHeight now, and what was
    measured around it. `view` and `total` stand when it does not say."""
    parts = list(answer) if isinstance(answer, (list, tuple)) else [answer]
    if len(parts) > 2:
        view = max(1, _whole(parts[2], view))
    top, bottom = _stuck(parts[:2] if len(parts) > 1 else parts[0], view)
    at = _whole(parts[3], asked) if len(parts) > 3 else asked
    total = _whole(parts[4], total) if len(parts) > 4 else total
    geometry = parts[5] if len(parts) > 5 and isinstance(parts[5], dict) else {}
    return at, top, bottom, view, total, geometry


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


def png_size(data: bytes) -> tuple[int, int] | None:
    """A PNG's (width, height) from its IHDR, or None when it is no PNG."""
    if len(data) >= 24 and data.startswith(_PNG_SIGNATURE) and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    return None


def _viewport_sizes(answer: Any) -> list[tuple[float, float]]:
    """VIEWPORT_JS's answer as the sizes a faithful shot of the viewport may
    have: the page's layout viewport, innerWidth / scale by innerHeight /
    scale (what Playwright clips a viewport shot to), at one screen pixel per
    CSS pixel (the gate's headed Chromium) or times the device pixel ratio.
    [] when it is not one."""
    if not isinstance(answer, (list, tuple)) or len(answer) < 2:
        return []
    width, height = answer[0], answer[1]
    ratio = answer[2] if len(answer) > 2 else 1
    scale = answer[3] if len(answer) > 3 else 1
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
               for v in (width, height, ratio, scale)):
        return []
    width, height = width / scale, height / scale
    return [(width, height), (width * ratio, height * ratio)]


def _faithful(size: tuple[int, int], sizes: list[tuple[float, float]]) -> bool:
    """`size` is one of `sizes` to the pixel: Playwright rounds the clip out
    to whole CSS pixels (980 for 390 / 0.398 = 979.9)."""
    return any(abs(size[0] - w) < 1 and abs(size[1] - h) < 1 for w, h in sizes)


class _MaskFailed(RuntimeError):
    """The credential mask could not be drawn, or was not whole when the shot
    was taken: that shot is not stored (`_masked_screenshot`)."""


def _int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("the mask sweep answered a malformed row")
    return value


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("the mask sweep answered a malformed row")
    return value


def _mask_targets(page, rows: Any) -> list[list]:
    """What MASK_DRAW_JS covers: every text node and every element's drawn
    value in which the gate's scrub left CREDENTIAL_MARK, and every node whose
    whole text holds more marks than its children's and own text nodes' do,
    which is a credential split across elements (`<b>pa</b><i>ss</i>`), found
    by descending (MASK_CHILDREN_JS) from each tree scope whose whole text
    holds more marks than its text nodes. Counts, never text, are kept: the
    marks are the scrub's, so nothing here ever held the credential."""
    if not isinstance(rows, list):
        raise ValueError("the mask sweep answered no rows")
    targets: list[list] = []
    drawn: set[int] = set()
    own: dict[int, int] = {}
    parts: dict[int, int] = {}
    scopes: list[tuple[int, int]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            raise ValueError("the mask sweep answered a malformed row")
        kind, at, scope = row[0], _int(row[1]), _int(row[2])
        if kind == "s":
            scopes.append((at, _str(row[3]).count(CREDENTIAL_MARK)))
        elif kind == "t" and len(row) == 5:
            marks = _str(row[4]).count(CREDENTIAL_MARK)
            own[at] = own.get(at, 0) + marks
            parts[scope] = parts.get(scope, 0) + marks
            if marks:
                targets.append(["t", at, _int(row[3])])
        elif kind == "x":
            if _str(row[3]).count(CREDENTIAL_MARK) and at not in drawn:
                drawn.add(at)
                targets.append(["x", at, 0])
        else:
            raise ValueError("the mask sweep answered a malformed row")
    frontier = [(at, marks) for at, marks in scopes if marks > parts.get(at, 0)]
    levels = 0
    while frontier:
        levels += 1
        if levels > MASK_MAX_LEVELS:
            raise ValueError(f"a credential split across elements is more than {MASK_MAX_LEVELS} levels deep")
        children: dict[int, list[tuple[int, int]]] = {}
        answer = page.evaluate(MASK_CHILDREN_JS, [at for at, _ in frontier])
        if not isinstance(answer, list):
            raise ValueError("the mask descent answered no rows")
        for row in answer:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError("the mask descent answered a malformed row")
            children.setdefault(_int(row[0]), []).append((_int(row[1]), _str(row[2]).count(CREDENTIAL_MARK)))
        nxt = []
        for at, marks in frontier:
            kids = children.get(at, [])
            if marks > sum(m for _, m in kids) + own.get(at, 0):
                targets.append(["n", at, 0])
            nxt.extend((child, m) for child, m in kids if m > own.get(child, 0))
        frontier = nxt
    return targets


def _clear_mask(page) -> list:
    answer = page.evaluate(MASK_CLEAR_JS)
    if (not isinstance(answer, list) or len(answer) != 2 or not isinstance(answer[0], bool)
            or not isinstance(answer[1], int) or isinstance(answer[1], bool)):
        raise ValueError("the mask's clear answered malformed")
    return answer


def _masked_screenshot(page) -> tuple[bytes, int]:
    """One viewport screenshot with every on-screen occurrence of the QA
    target's credential under an opaque box (QA-SHOT-MASK, module
    docstring), and how many places were masked. The mask is drawn for this
    shot alone and taken down after it, whatever happened. Raises
    `_MaskFailed`, and the shot is dropped unread, when the mask could not be
    drawn, could not be taken down, or was not whole when it was."""
    try:
        targets = _mask_targets(page, page.evaluate(MASK_FIND_JS))
        drew = page.evaluate(MASK_DRAW_JS, targets) if targets else [0, 0]
        if (not isinstance(drew, list) or len(drew) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in drew)):
            raise ValueError("the mask's draw answered malformed")
    except Exception as exc:  # noqa: BLE001 -- any failure refuses the shot
        try:
            _clear_mask(page)
        except Exception:  # noqa: BLE001 -- the shot is refused either way
            pass
        raise _MaskFailed(f"the credential mask could not be drawn, so no shot was stored: "
                          f"{type(exc).__name__}: {_one_line(exc)}") from exc
    try:
        png = page.screenshot(type="png", animations="disabled", caret="hide", timeout=SHOT_TIMEOUT_MS)
    finally:
        try:
            intact, boxes = _clear_mask(page)
        except Exception as exc:  # noqa: BLE001 -- unknown is not intact
            raise _MaskFailed(f"the credential mask could not be checked after the shot, so it was not "
                              f"stored: {type(exc).__name__}: {_one_line(exc)}") from exc
    if not intact or boxes != drew[1]:
        raise _MaskFailed("the credential mask was not whole when the shot was taken (the page changed or "
                          "removed it), so the shot was not stored")
    return png, drew[0]


def _shot(page, segment: int, preset: str | None = None) -> tuple[bytes, list[dict], str | None, int]:
    """One screenshot of the viewport, taken again (up to SHOT_RETRIES times)
    while its size disagrees with the page's layout viewport read just after
    it, then, given the `preset`, once more after putting the device back
    (`swarm-handoff device <preset>`). Returns the PNG, a record of each shot
    taken again, a line for `failed` when the last one is still off, and how
    many places the credential mask covered in the PNG returned. Every shot,
    a retake included, is a `_masked_screenshot`, and raises as it does.

    MEASURED (box 6343795935, run 5b's 5b:QA-001, after PR 1998 had the gate
    put the device back after a clipped capture): every admin-persona
    phone-390 segment came back 394x852 against a layout viewport of 394x853
    or 398x862 and was kept, under a `captured` status. The retries alone did
    not bring it back; the reset is the one thing left to try, and a shot
    still off after it leaves the route unsettled (`_capture_route`).

    MEASURED (#1981, task_aa240c7a75894ff9852b): at phone-390 the first
    screenshot after every full document load came out 980x2121 with the
    page drawn small; every later shot in the same document was 390x844.
    Lane QA-DEVICE (task_b3f3786bde2144389073, Chrome 153, Playwright 1.63)
    found the FIRST one faithful: a page with no viewport meta tag is laid
    out at 980 px on a phone, Playwright clips a viewport shot to
    innerWidth / visualViewport.scale (390 / 0.398) by innerHeight / scale,
    and that is 980x2121. The later ones were the unfaithful shots: every
    clipped capture lifted the gate's device (dpr 1, a desktop scrollbar),
    which the gate now puts back after each (livegate/device.py, QA-REASSERT).
    So a shot is checked against that layout viewport, never the preset's
    nominal size or innerWidth alone, and one that disagrees is taken again."""
    reshot: list[dict] = []
    attempt = 0
    reset = False
    while True:
        png, masked = _masked_screenshot(page)
        size = png_size(png)
        sizes = _viewport_sizes(page.evaluate(VIEWPORT_JS))
        if size is None or not sizes or _faithful(size, sizes):
            return png, reshot, None, masked
        view = (round(sizes[0][0]), round(sizes[0][1]))
        if attempt >= SHOT_RETRIES and (preset is None or reset):
            after = f"{attempt + 1} shots" + (f" and a reset of the device to {preset}" if reset else "")
            return png, reshot, (f"segment {segment} was {size[0]}x{size[1]}, not the page's layout viewport "
                                 f"{view[0]}x{view[1]}, after {after} (kept as it came)"), masked
        record = {"segment": segment, "size": list(size), "viewport": list(view)}
        if attempt >= SHOT_RETRIES:
            reset = True
            why = _handoff("device", preset)
            record["device_reset"] = why or "applied"
        reshot.append(record)
        attempt += 1
        page.wait_for_timeout(SETTLE_POLL_MS)


def _capture_route(page, errors: _Errors, *, base: str, group: str, route: dict, preset: str, theme: str,
                   out: Path, axe_source: str | None, settle: str = "console",
                   persona_cookie: bool = True, gate: _GateRefusals | None = None) -> dict:
    """One route at one preset and theme. Raises with the step that failed.
    `persona_cookie` False (sets_persona) sets no PERSONA_COOKIE. `gate`
    gives the route the refusals the live gate made while it loaded."""
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
        response = page.goto(base + route["path"], wait_until="load", timeout=NAV_TIMEOUT_MS)
        # None for a same-document navigation (a hash route from the route
        # before it): no document answered, so there is no status to read.
        status = getattr(response, "status", None) if response is not None else None
        if isinstance(status, int) and status >= 400:
            raise _RouteFailed(f"navigate: the main document answered HTTP {status}")
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
        images, overlaps, covers, reshot, off_size, geometry = [], [], [], [], [], []
        masked_total = 0
        duplicate = None
        step = "segment 1"
        y, _top, under, view, total, around = _scrolled(page.evaluate(SCROLL_JS, 0), 0, view, total)
        for segment in range(1, budget + 1):
            step = f"segment {segment}"
            png, again, off, masked = _shot(page, segment, preset)
            reshot.extend(again)
            if off is not None:
                off_size.append(off)
            name = image_name(group, rid, preset, theme, segment)
            _write(out / name, png)
            images.append(name)
            # How many places the credential mask covered in this image: a
            # count, never the value or its length (QA-SHOT-MASK).
            geometry.append({"segment": segment, "at": y, "view": view, **around, "masked": masked})
            masked_total += masked
            if segment == budget or y + view >= total:
                break
            # What the screen showed of the scroller, less what is fixed over
            # its foot here (the Reads bar) and what is stuck over its top
            # there (a sticky header, a drawer's tab strip), all from
            # STUCK_JS, so the rows under either are in this segment or the
            # next.
            nxt = y + view - under
            at, stuck, under_next, view_next, total_next, around = _scrolled(
                page.evaluate(SCROLL_JS, nxt), nxt, view, total)
            if stuck:
                nxt -= stuck
                at, _, under_next, view_next, total_next, around = _scrolled(
                    page.evaluate(SCROLL_JS, nxt), nxt, view, total)
            # Where the scroller went, not where it was asked to go: one that
            # did not move on would be shot as the same frame again, and its
            # end recorded as reached (6:capacity-005).
            if at <= y:
                duplicate = {"segment": segment + 1, "at": at}
                break
            overlaps.append(stuck + under)
            covers.append({"top": stuck, "bottom": under})
            y, under, view, total = at, under_next, view_next, total_next
        # Short of the end: cut at its budget, the route's at the desktop or
        # MAX_SEGMENTS at a phone, or a scroller that stopped moving. Never
        # recorded as complete (c49).
        reached = min(y + view, total)
        truncated = reached < total
        step = "text"
        _write(out / f"{stem}.txt", page.inner_text("body"))
        step = "a11y"
        snapshot = page.locator("body").aria_snapshot()
        _write(out / f"{stem}.a11y.json", json.dumps({"format": "aria-snapshot", "snapshot": snapshot}, indent=1))
        entry = {
            "id": rid, "preset": preset, "theme": theme, "persona": persona,
            # A segment still off the viewport's size after the device reset
            # (`_shot`) is no capture of the route as it stands.
            "status": "unsettled" if pending or off_size else "captured",
            "path": route["path"], "images": images, "network_idle": idle,
            "settled": not pending, "pending": pending, "scroller": measured[2] if len(measured) > 2 else None,
            "overlaps": overlaps, "covers": covers,
            "truncated": truncated, "reached": reached, "total": total, "duplicate": duplicate,
            "geometry": geometry, "masked": masked_total, "reshot": reshot, "off_viewport": off_size,
            "text": f"{stem}.txt", "a11y": f"{stem}.a11y.json", "errors": f"{stem}.errors.json",
        }
        if axe_source is not None:
            step = "axe"
            if not page.evaluate(AXE_INJECT_JS, axe_source):
                raise RuntimeError("axe-core did not load in the page")
            _write(out / f"{stem}.axe.json", json.dumps(page.evaluate(AXE_RUN_JS), indent=1))
            entry["axe"] = f"{stem}.axe.json"
    except _RouteFailed:
        raise
    except Exception as exc:  # noqa: BLE001 -- a failed route is a finding
        where = f"ready selector {ready!r} not found" if step == "ready" else step
        raise _RouteFailed(f"{where}: {type(exc).__name__}: {_one_line(exc)}") from exc
    refused = gate.during(errors.since, time.time()) if gate is not None else None
    _write(out / f"{stem}.errors.json", json.dumps(errors.document(refused), indent=1))
    entry.update({"console_errors": len(errors.console), "page_errors": len(errors.page),
                  "failed_requests": len(errors.requests), "bad_responses": len(errors.responses),
                  "gate_refused": len(refused) if refused is not None else None})
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
    gate = _GateRefusals(time.time())
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
                                               settle=settle, persona_cookie=persona_cookie, gate=gate)
                        manifest["routes"].append(entry)
                        why = []
                        if entry["pending"]:
                            why.append(f"did not settle in {SETTLE_TIMEOUT_MS} ms: still showing "
                                       + "; ".join(entry["pending"]) + " (shot as it stood)")
                        if entry["off_viewport"]:
                            why.extend(entry["off_viewport"])
                        if entry["truncated"] and entry["duplicate"]:
                            why.append(f"the scroller stopped at {entry['duplicate']['at']} px after "
                                       f"{len(entry['images'])} segments: reached {entry['reached']} "
                                       f"of {entry['total']} px (the rest is in no image)")
                        elif entry["truncated"]:
                            why.append(f"cut at {len(entry['images'])} segments: reached {entry['reached']} "
                                       f"of {entry['total']} px (the rest is in no image)")
                        if why:
                            manifest["failed"].append({"id": route["id"], "preset": preset, "theme": theme,
                                                       "error": "; ".join(why)})
                    except _RouteFailed as exc:
                        manifest["failed"].append({"id": route["id"], "preset": preset, "theme": theme,
                                                   "error": str(exc),
                                                   "gate_refused": gate.during(errors.since, time.time())})
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
        manifest["gate_refusals"] = gate.summary()
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


def discover_base(base: str) -> tuple[tuple[str, str, int], str, str]:
    """`discover --base` checked: http(s), a host name that is neither an IP
    literal nor internal (a single label, `localhost`, INTERNAL_SUFFIXES:
    swarm_egress.policy's rule), and no userinfo, query or fragment. A path
    is allowed (#1981: an app served under https://demo.playwright.dev/todomvc/).
    Returns the origin, `scheme://host[:port]` as given, and the start path
    (`/` when there is none)."""
    if not isinstance(base, str) or "?" in base or "#" in base or "@" in base:
        raise UsageError("--base must be an http(s) address with no userinfo, query or fragment")
    origin = _origin(base)
    if origin is None:
        raise UsageError("--base must be an http(s) address")
    host = origin[1].rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UsageError("--base must name a host, not an IP address")
    if "." not in host or host == "localhost" or any(("." + host).endswith(s) for s in INTERNAL_SUFFIXES):
        raise UsageError("--base must name a public host, not an internal one")
    parts = urlsplit(base)
    return origin, f"{parts.scheme}://{parts.netloc}", parts.path or "/"


def under(path: str, start: str) -> bool:
    """Whether `path` is the start path or below it: `/todomvc/` keeps
    `/todomvc`, `/todomvc/` and `/todomvc/x`, never `/todomvcx`."""
    prefix = start.rstrip("/")
    return not prefix or path == prefix or path.startswith(prefix + "/")


def hash_route(href: Any, document: str) -> str | None:
    """`document#/route` when `href` is a link on `document` itself (same
    path, no query) whose fragment is route-shaped (HASH_ROUTE, cut at its
    own `?`); else None."""
    try:
        parts = urlsplit(str(href))
    except ValueError:
        return None
    if (parts.path or "/") != document or parts.query:
        return None
    route = parts.fragment.split("?", 1)[0]
    return f"{document}#{route}" if HASH_ROUTE.fullmatch(route) else None


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


def discover(*, base: str, max_pages: int, max_depth: int, exclude: list[str],
             hash_routes: bool = False) -> dict:
    """The route list of what `base` links to, breadth first (module
    docstring). Returns `{"document": route list or None, "why": str}`:
    None when nothing could be listed (the start page excluded, disallowed
    or answering an HTTP error), and why."""
    origin, root, start = discover_base(base)
    globs = list(DEFAULT_EXCLUDE) + [g for g in exclude if g not in DEFAULT_EXCLUDE]
    # robots.txt is the origin's, whatever path the app is served under.
    robots = _fetch_robots(root + "/robots.txt")
    rules = robots_disallow(robots)
    skipped = {"off_origin": 0, "outside_base": 0, "excluded": 0, "robots": 0, "failed": 0, "http_error": 0}
    off: set[str] = set()
    outside: set[str] = set()
    seen: set[str] = set()
    listed: list[tuple[str, int]] = []
    failed: list[str] = []
    http_errors: dict[str, int] = {}
    loaded: set[str] = set()

    def admit(path: str, depth: int) -> None:
        if path in seen:
            return
        seen.add(path)
        document, _, route = path.partition("#")
        if excluded(document, globs) or (route and excluded(route, globs)):
            skipped["excluded"] += 1
        elif disallowed(document, rules):
            skipped["robots"] += 1
        elif len(listed) < max_pages and depth <= max_depth:
            listed.append((path, depth))

    admit(start, 0)
    if not listed:
        why = "excluded" if skipped["excluded"] else "disallowed by robots.txt"
        return {"document": None, "why": f"the start page {start} is {why}: nothing to discover"}
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
                response = page.goto(root + path, wait_until="load", timeout=NAV_TIMEOUT_MS)
                loaded.add(path)
                # A 4xx or 5xx document is no screen (#1981: a GitHub Pages
                # 404 was listed); None is a same-document (hash) navigation.
                status = getattr(response, "status", None) if response is not None else None
                if isinstance(status, int) and status >= 400:
                    at -= 1
                    listed.pop(at)
                    http_errors[path] = status
                    skipped["http_error"] += 1
                    continue
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                except Exception:  # noqa: BLE001 -- an app that polls may never be idle
                    pass
                hrefs = page.evaluate(LINKS_JS) or []
            except Exception:  # noqa: BLE001 -- a page that failed is listed; its capture says so
                skipped["failed"] += 1
                failed.append(path)
                continue
            here = path.partition("#")[0]
            for href in list(hrefs)[:MAX_LINKS]:
                found = same_origin_path(href, origin)
                if found is None:
                    if str(href) not in off:
                        off.add(str(href))
                        skipped["off_origin"] += 1
                    continue
                if not under(found, start):
                    if found not in outside:
                        outside.add(found)
                        skipped["outside_base"] += 1
                    continue
                if hash_routes:
                    route = hash_route(href, here)
                    if route is not None:
                        admit(route, depth + 1)
                admit(found, depth + 1)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        playwright.stop()
    if not listed:
        return {"document": None,
                "why": f"the start page {start} answered HTTP {http_errors.get(start)}: nothing to discover"}
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
            "hash_routes": hash_routes, "http_errors": http_errors,
            # Listed at a cap and never loaded, so their status was not read:
            # their capture's navigate step reads it.
            "unchecked": [path for path, _depth in listed if path not in loaded and path not in failed],
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
    dis.add_argument("--base", required=True,
                     help="where the app starts, e.g. https://app.example or https://app.example/todomvc/; "
                          "only links under its path are kept")
    dis.add_argument("--max-pages", type=int, default=DISCOVER_MAX_PAGES)
    dis.add_argument("--max-depth", type=int, default=DISCOVER_MAX_DEPTH)
    dis.add_argument("--exclude", action="append", default=[],
                     help=f"a path glob never listed or loaded (repeatable; {', '.join(DEFAULT_EXCLUDE)} always)")
    dis.add_argument("--hash-routes", action="store_true",
                     help="also list a page's own links whose fragment is a route (#/active), as routes")
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
    base = args.base
    try:
        discover_base(base)
        if not 1 <= args.max_pages <= DISCOVER_MAX_PAGES:
            raise UsageError(f"--max-pages must be 1..{DISCOVER_MAX_PAGES}")
        if not 0 <= args.max_depth <= DISCOVER_MAX_DEPTH:
            raise UsageError(f"--max-depth must be 0..{DISCOVER_MAX_DEPTH}")
        if not all(isinstance(g, str) and g.startswith("/") for g in args.exclude):
            raise UsageError("--exclude takes path globs starting with /")
    except (UsageError, ValueError) as exc:
        sys.stderr.write(f"swarm_qa: {exc}\n")
        return EXIT_USAGE
    found = discover(base=base, max_pages=args.max_pages, max_depth=args.max_depth, exclude=args.exclude,
                     hash_routes=args.hash_routes)
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

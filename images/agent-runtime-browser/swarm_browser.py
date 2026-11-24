"""The live browser, from an agent's Python (BROWSER-ENFORCE, part of archived issue 1030).

    from swarm_browser import live

    playwright, browser, context = live()
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://example.com")
    ...
    playwright.stop()   # disconnects; the live browser keeps running

On `claude-code-browser` the browser is the one the platform launched and the
person watches on the console (docs/design/live-browser.md §3.2). The agent
does not start a Chromium of its own -- that launch fails by design, with a
message pointing here -- it connects to that one over CDP, at
`$SWARM_CDP_ENDPOINT`. `live()` is that connection: the browser's existing
context, so the pages it opens are the tabs the person sees.

Do not `browser.close()` it: stop Playwright instead. Installed into the
image's venv site-packages by images/agent-runtime-browser/Dockerfile, so it
imports from `python` on the agent's PATH (/opt/venv/bin is first there).

AFTER A HAND-OFF, READ THE PAGE THE PERSON LEFT (LB-RETURN, part of archived issue 1030):

    playwright, browser, context = live()          # a fresh connection
    page = handoff_page(context)                   # the tab the person left
    code = page.inner_text("#code")

MEASURED by the live-browser acceptance group on 4+ runs since BROWSER-ENFORCE
(PR 1248): the person typed, the page showed its code, and the agent's answer
held none of it. `context.pages[0]` is not "the page". The browser also
carries Chromium's first tab, Playwright MCP's, and every tab the agent opened
"in its own tab", and their order is whatever CDP reports. `handoff_page`
picks the tab whose address is the one `swarm-handoff wait` returned
(`$SWARM_HANDOFF/status.json`).

A CONNECTION THAT CLOSES OR IS REFUSED (part of issue 272): Chromium exited.
The gate starts it again (BROWSER-RELAUNCH) or, past its budget, leaves it
closed, and the worker writes which to `$SWARM_HANDOFF/live-browser.json` on its
control poll. `live()` reads that file when a connection fails: while it says
`restarting` (or still `running`, the file being some seconds behind) it tries
again, for at most `wait_seconds`; then it raises with the file's message.
Two review tasks lost the browser and saw only "connect_over_cdp: Connection
closed".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

#: Set by the dispatcher on the worker container, and by the claude-code
#: runner on the agent's environment, only when the live browser exists.
ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"
#: The hand-off spool (`swarm-handoff`); its `status.json` is the last
#: returned hand-off's summary, with the url of the page the person left.
HANDOFF_ENV = "SWARM_HANDOFF"
#: The worker's note of the live browser's own state in the spool
#: (agent_worker.handoff.BROWSER_STATE_NAME): `running`, `restarting` or `crashed`.
BROWSER_STATE_NAME = "live-browser.json"
#: How long `live()` waits, by default, for a browser that is being started
#: again. WHY 60 s: a relaunch waits 1, 3 then 10 s before each try
#: (livegate RELAUNCH_DELAYS) and the worker's poll writes the file up to a
#: control poll (10 s) late; a minute covers a relaunch or two with margin.
RECONNECT_WAIT_SECONDS = 60.0
#: Between two connection tries while waiting.
RECONNECT_STEP_SECONDS = 2.0
#: How long a failed connection is retried while the file still says
#: `running`: one control poll and a little, for the file to catch up.
STALE_RUNNING_SECONDS = 15.0


def browser_state() -> dict[str, Any] | None:
    """`$SWARM_HANDOFF/live-browser.json`, or None when there is none to read."""
    spool = os.environ.get(HANDOFF_ENV, "").strip()
    if not spool:
        return None
    try:
        document = json.loads((Path(spool) / BROWSER_STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def live(endpoint: str | None = None, *, timeout_ms: float = 30_000,
         wait_seconds: float = RECONNECT_WAIT_SECONDS,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> tuple[Any, Any, Any]:
    """Connect to the live browser; return `(playwright, browser, context)`.

    When the connection fails, `$SWARM_HANDOFF/live-browser.json` says why:
    while it says `restarting` the connection is tried again until
    `wait_seconds` is spent; then, or for a `crashed` browser, RuntimeError
    carries the file's state and message. With no file, the original error.
    """
    endpoint = endpoint or os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        raise RuntimeError(
            f"{ENDPOINT_ENV} is not set, so this attempt has no live browser to connect to "
            "(only claude-code-browser runs one)"
        )
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        started = clock()
        while True:
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
                break
            except Exception as exc:
                state = browser_state()
                if state is None:
                    raise
                said = str(state.get("state") or "unknown")
                waited = clock() - started
                bound = {"restarting": wait_seconds,
                         "running": min(wait_seconds, STALE_RUNNING_SECONDS)}.get(said, 0.0)
                if waited < bound:
                    sleep(min(RECONNECT_STEP_SECONDS, bound - waited))
                    continue
                message = str(state.get("message") or "no message")
                raise RuntimeError(
                    f"the live browser did not accept the connection: live-browser.json says {said!r} "
                    f"({message}); waited {waited:.0f} s; the connection's error: {exc}"
                ) from exc
        context = browser.contexts[0] if browser.contexts else browser.new_context()
    except BaseException:
        playwright.stop()
        raise
    return playwright, browser, context


def _where(url: Any) -> tuple[str, str] | None:
    """A page address as (origin, path): what `swarm-handoff wait` reports,
    which has no query or fragment (the worker strips them, §5.9)."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower(), parts.path or "/"


def returned_url() -> str | None:
    """The url of the page the person left, from the last returned hand-off, or None."""
    spool = os.environ.get(HANDOFF_ENV, "").strip()
    if not spool:
        return None
    try:
        status = json.loads((Path(spool) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = status.get("url") if isinstance(status, dict) else None
    return url if isinstance(url, str) and url else None


def handoff_page(context: Any, url: str | None = None) -> Any:
    """The tab the person left after a hand-off, from a FRESH `live()` context.

    The page at `url` -- by default the one `swarm-handoff wait` returned --
    matched on origin and path, the newest such tab when there are several.
    With no such tab, the newest page that is not blank; then the newest page.
    Read from it again: a value or a snapshot from before the hand-off is
    what the page held before the person changed it.
    """
    pages = list(getattr(context, "pages", []) or [])
    if not pages:
        return context.new_page()
    want = _where(url or returned_url())
    if want is not None:
        for page in reversed(pages):
            if _where(getattr(page, "url", None)) == want:
                return page
    for page in reversed(pages):
        if str(getattr(page, "url", "") or "") not in ("", "about:blank"):
            return page
    return pages[-1]

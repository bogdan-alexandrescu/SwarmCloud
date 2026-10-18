"""The live browser, from an agent's Python (BROWSER-ENFORCE, part of #1030).

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

AFTER A HAND-OFF, READ THE PAGE THE PERSON LEFT (LB-RETURN, part of #1030):

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
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: Set by the dispatcher on the worker container, and by the claude-code
#: runner on the agent's environment, only when the live browser exists.
ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"
#: The hand-off spool (`swarm-handoff`); its `status.json` is the last
#: returned hand-off's summary, with the url of the page the person left.
HANDOFF_ENV = "SWARM_HANDOFF"


def live(endpoint: str | None = None, *, timeout_ms: float = 30_000) -> tuple[Any, Any, Any]:
    """Connect to the live browser; return `(playwright, browser, context)`."""
    endpoint = endpoint or os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        raise RuntimeError(
            f"{ENDPOINT_ENV} is not set, so this attempt has no live browser to connect to "
            "(only claude-code-browser runs one)"
        )
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
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

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
"""

from __future__ import annotations

import os
from typing import Any

#: Set by the dispatcher on the worker container, and by the claude-code
#: runner on the agent's environment, only when the live browser exists.
ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"


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

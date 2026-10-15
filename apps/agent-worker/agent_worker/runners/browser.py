"""Runner: scripted browser work on Playwright + Chromium.

This is the one profile that does not run on Cloud Run Jobs. Chromium wants a
large `/dev/shm`, and Cloud Run does not let us size it; GKE Autopilot does, via
an in-memory emptyDir, so `RUNNER_PROFILES["browser"]` pins the backend to
Autopilot and the worker Job template mounts 2Gi at /dev/shm. That 2 GiB counts
against the container's memory limit, which is why the `browser` resource class
is 16 GiB rather than 8 -- the number is asserted by
`tests/unit/worker/test_kubernetes_manifests.py::test_the_browser_job_sizes_dev_shm`,
so this comment and the manifest cannot drift apart silently.

Chromium's own sandbox is disabled here, and that is deliberate rather than
lazy: it needs user namespaces and CAP_SYS_ADMIN, which the pod security policy
refuses on purpose. The isolation is the pod -- non-root, no privilege
escalation, seccomp RuntimeDefault, all capabilities dropped, per-tenant
namespace with default-deny egress -- not Chromium's internal one, and adding a
capability back to get a second sandbox would be a bad trade.

The task describes a sequence of actions. There is no "evaluate this JavaScript"
action: page text and screenshots cover the useful cases, and arbitrary script
execution in a browser holding a tenant's session is exactly the shape of an
exfiltration primitive.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any
from urllib.parse import urlparse

from swarm_common.profiles import url_refusal

from .base import RunnerContext, RunnerFailure, run_runner

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_TIMEOUT_MS = 30_000
MAX_ACTIONS = 200
#: What a goto waits for when the action names nothing. `load` waits for every
#: subresource, and an ad-heavy page or one full of iframes may never fire it:
#: QA batch full-1015 (2026-10-15) failed S1-adheavy and S1-iframes 3/3 on
#: "Page.goto: Timeout 30000ms exceeded ... waiting until load" with the text
#: already there. The DOM being parsed is what extraction needs (#1363).
DEFAULT_WAIT_UNTIL = "domcontentloaded"
#: A screenshot's own bound, below the 30 s default so a page whose web fonts
#: never settle costs seconds, not the action timeout. S1-wikipedia failed 3/3
#: in "waiting for fonts to load..." with 111,583 chars already extracted. A
#: screenshot is evidence, not the product: one that misses this bound is
#: recorded as missing and the task goes on (#1363).
SCREENSHOT_TIMEOUT_MS = 15_000

#: Title fragments that mean the page is a wall, not the content: a bot
#: challenge or a consent interstitial. Lower-case, matched as substrings. Only
#: ever used to SAY so in the result -- never to fail a task or retry it.
CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "are you a robot",
    "are you human",
    "human verification",
    "verify you are human",
    "captcha",
    "security check",
    "checking your browser",
    "request blocked",
)
CONSENT_TITLES = (
    "before you continue",
    "consent",
    "cookie",
    "privacy choices",
    "your privacy",
)


def _wall_from_title(title: str) -> str | None:
    """"challenge", "consent" or None, read from the page title alone."""
    lowered = (title or "").lower()
    if any(fragment in lowered for fragment in CHALLENGE_TITLES):
        return "challenge"
    if any(fragment in lowered for fragment in CONSENT_TITLES):
        return "consent"
    return None


def _event(events: list[dict[str, Any]], code: str, **detail: Any) -> None:
    """Record a degraded-but-not-failed step: in the result, and on stderr.

    By code, so a log query or a QA report can count them. Never the page's
    text, and never a URL beyond what `_check_url` already let through.
    """
    events.append({"code": code, **detail})
    extras = " ".join(f"{key}={value}" for key, value in detail.items())
    print(f"[browser] event {code} {extras}".rstrip(), file=sys.stderr)


def _goto(
    page: Any,
    url: str,
    wait_until: str,
    index: int,
    timeout_error: type[BaseException],
    events: list[dict[str, Any]],
    navigations: list[int],
) -> tuple[Any, bool]:
    """Navigate; (response, load_timed_out). RunnerFailure if it never committed.

    A wait that times out AFTER the navigation committed -- the document is
    the new one, its DOM is there -- is loaded enough: whatever the action
    waited for (`load`, `networkidle`) is a stricter condition than
    extraction needs. Committed is read two ways, both about THIS goto: the
    page's URL moved off the one it had before, or the main frame fired
    `framenavigated` during it (`navigations` is body()'s counter of those;
    a same-URL reload commits without moving the URL). Asking the current
    document whether its DOM is ready would not do: on a second goto (A ->
    B) that document is still A, long since ready, and a B that never
    reached the site would pass as loaded and extract A. A navigation that
    did neither never reached the site, and that is a failure, said plainly.
    """
    before = page.url
    navigations_before = navigations[0]
    try:
        return page.goto(url, wait_until=wait_until), False
    except timeout_error as exc:
        committed = page.url not in ("", "about:blank", before) or (
            navigations[0] != navigations_before
        )
        if not committed:
            raise RunnerFailure(
                f"action {index} (goto): the navigation never committed: no "
                f"response from the site within the timeout while waiting "
                f"until {wait_until!r}"
            ) from exc
        _event(events, "goto_load_timeout_after_commit", index=index, wait_until=wait_until)
        return None, True


def _screenshot(
    page: Any,
    path: Any,
    full_page: bool,
    error: type[BaseException],
    timeout_error: type[BaseException],
    events: list[dict[str, Any]],
    index: int | None,
) -> str | None:
    """Take a bounded screenshot to `path`; None, or why it failed (and an event)."""
    try:
        page.screenshot(
            path=str(path),
            full_page=full_page,
            timeout=SCREENSHOT_TIMEOUT_MS,
            animations="disabled",
        )
        return None
    except error as exc:
        reason = "timeout" if isinstance(exc, timeout_error) else type(exc).__name__
        _event(events, "screenshot_missing", index=index, reason=reason)
        return reason


def _check_url(url: Any) -> str:
    """The URL, when the browser may open it; RunnerFailure when not.

    The runner's own scheme and host checks stay, and the catalogue's
    `url_refusal` -- the one rule swarm-api and the plugin's bridge call at
    submission (contract request 32) -- runs after them. Checked here as well
    because a task written before request 32, or one that reached the store
    by any path that skipped the submission check, still arrives at this
    runner. Neither check is the SSRF control: the NetworkPolicy is, and
    `url_refusal`'s docstring says why a string check cannot be.

    A refusal names the reason, never the URL: `url_refusal` refuses a URL
    carrying `user:password@`, and the failure text is stored with the task.
    """
    if not isinstance(url, str) or not url:
        raise RunnerFailure("browser action requires a url")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise RunnerFailure(f"url scheme {parsed.scheme!r} is not allowed")
    if not parsed.netloc:
        raise RunnerFailure("the url has no host")
    reason = url_refusal(url)
    if reason:
        raise RunnerFailure(f"the url is refused: {reason}")
    return url


def _launch(playwright: Any, payload: dict[str, Any]) -> Any:
    return playwright.chromium.launch(
        headless=True,
        # The full Chromium in its new headless mode. The browser image no
        # longer installs chromium-headless-shell, which is what a headless
        # launch without a channel looks for (BROWSER-ENFORCE, owner decision
        # 2026-10-13: docs/browser-startup-speed.md §2.3, §9).
        channel="chromium",
        chromium_sandbox=False,  # see module docstring: the pod is the sandbox
        args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--disable-background-networking",
            "--disable-extensions",
            "--mute-audio",
        ],
        timeout=int(payload.get("launch_timeout_ms", 60_000)),
    )


def body(ctx: RunnerContext) -> dict[str, Any]:
    try:
        # lazy: only in the browser image
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise RunnerFailure(
            "playwright is not installed; the browser profile must run on "
            "images/agent-runtime-browser"
        ) from exc

    payload = ctx.payload
    actions = payload.get("actions") or []
    if not isinstance(actions, list):
        raise RunnerFailure("input.actions must be a list")
    if len(actions) > MAX_ACTIONS:
        raise RunnerFailure(f"input.actions is limited to {MAX_ACTIONS} entries")
    start_url = payload.get("url")
    if start_url:
        actions = [{"type": "goto", "url": start_url}, *actions]
    if not actions:
        raise RunnerFailure("browser runner needs input.url or at least one action")

    timeout_ms = int(payload.get("timeout_ms", DEFAULT_TIMEOUT_MS))
    performed: list[dict[str, Any]] = []
    console_errors: list[str] = []
    events: list[dict[str, Any]] = []
    notes: list[str] = []
    screenshot_report: dict[str, Any] | None = None
    started = time.monotonic()

    with sync_playwright() as playwright:
        browser = _launch(playwright, payload)
        try:
            context = browser.new_context(
                viewport={
                    "width": int(payload.get("viewport_width", 1280)),
                    "height": int(payload.get("viewport_height", 900)),
                },
                user_agent=payload.get("user_agent") or None,
                ignore_https_errors=False,
            )
            context.set_default_timeout(timeout_ms)
            page = context.new_page()
            page.on(
                "console",
                lambda msg: console_errors.append(f"{msg.type}: {msg.text}"[:500])
                if msg.type == "error"
                else None,
            )
            # Main-frame commits, counted for _goto: a frame with no parent is
            # the page's own. Fired when a navigation commits, before `load`.
            navigations = [0]

            def _on_navigated(frame: Any) -> None:
                if frame.parent_frame is None:
                    navigations[0] += 1

            page.on("framenavigated", _on_navigated)

            for index, action in enumerate(actions):
                if ctx.stop_requested:
                    performed.append({"index": index, "type": "aborted"})
                    break
                if not isinstance(action, dict):
                    raise RunnerFailure(f"action {index} must be an object")
                kind = str(action.get("type", "")).lower()
                record: dict[str, Any] = {"index": index, "type": kind}

                if kind == "goto":
                    url = _check_url(action.get("url"))
                    wait_until = str(action.get("wait_until") or DEFAULT_WAIT_UNTIL)
                    response, load_timed_out = _goto(
                        page, url, wait_until, index, PlaywrightTimeoutError, events, navigations
                    )
                    record["url"] = url
                    record["status"] = response.status if response else None
                    if load_timed_out:
                        record["load_timed_out"] = True
                        notes.append(
                            f"action {index} (goto): {wait_until!r} did not arrive within "
                            f"the timeout after the navigation committed; went on with "
                            f"the DOM as it was"
                        )
                elif kind == "click":
                    page.click(str(action["selector"]))
                    record["selector"] = action["selector"]
                elif kind == "fill":
                    page.fill(str(action["selector"]), str(action.get("text", "")))
                    record["selector"] = action["selector"]
                elif kind == "press":
                    page.press(str(action["selector"]), str(action.get("key", "Enter")))
                    record["selector"] = action["selector"]
                elif kind == "wait_for":
                    page.wait_for_selector(
                        str(action["selector"]),
                        timeout=int(action.get("timeout_ms", timeout_ms)),
                    )
                    record["selector"] = action["selector"]
                elif kind == "wait":
                    seconds = min(float(action.get("seconds", 1)), 60.0)
                    page.wait_for_timeout(seconds * 1000)
                    record["seconds"] = seconds
                elif kind == "screenshot":
                    name = str(action.get("name", f"screenshot-{index:03d}.png"))
                    path = ctx.artifact_path(name)
                    if (
                        _screenshot(
                            page,
                            path,
                            bool(action.get("full_page", True)),
                            PlaywrightError,
                            PlaywrightTimeoutError,
                            events,
                            index,
                        )
                        is None
                    ):
                        record["artifact"] = path.name
                    else:
                        record["missing"] = True
                        notes.append(f"action {index} (screenshot): {path.name} is missing")
                elif kind == "extract":
                    selector = str(action.get("selector", "body"))
                    text = page.inner_text(selector)
                    name = str(action.get("name", f"extract-{index:03d}.txt"))
                    ctx.write_artifact(name, text)
                    record["artifact"] = name
                    record["characters"] = len(text)
                else:
                    raise RunnerFailure(f"unsupported browser action type {kind!r}")
                performed.append(record)

            extract_text = bool(payload.get("extract_text", True))
            final_text = page.inner_text("body") if extract_text else ""
            if final_text.strip():
                ctx.write_artifact("page.txt", final_text)
            title = page.title()
            url_now = page.url
            if payload.get("screenshot", True):
                path = ctx.artifact_path("final.png")
                failure = _screenshot(
                    page, path, True, PlaywrightError, PlaywrightTimeoutError, events, None
                )
                if failure is None:
                    screenshot_report = {"artifact": path.name, "missing": False}
                elif not extract_text:
                    # Nothing was extracted: the screenshot was the product.
                    raise RunnerFailure(
                        "the final screenshot failed and text extraction is off; "
                        "the run produced nothing"
                    )
                else:
                    screenshot_report = {
                        "artifact": path.name,
                        "missing": True,
                        "error": failure,
                    }
                    notes.append("the final screenshot is missing; the page text was extracted")
        finally:
            browser.close()

    if console_errors:
        ctx.write_artifact("console-errors.json", json.dumps(console_errors, indent=2))

    # An empty page is a fact the result states, not a silent success: QA's
    # S1-cookie succeeded 3/3 with a 0-char page.txt and no word about why.
    chars = len(final_text.strip())
    wall = _wall_from_title(title)
    page_text: dict[str, Any] = {
        "chars": chars,
        "title": title,
        "final_url": url_now,
        "wall": wall,
    }
    if extract_text and chars == 0:
        notes.append(
            f"the text extraction is empty (0 chars) at {url_now}, title {title!r}"
        )
    if wall:
        notes.append(f"the page title {title!r} looks like a {wall} wall, not the content")

    summary = f"visited {url_now} ({title}) with {len(performed)} action(s)"
    if extract_text:
        summary += f"; {chars} chars of text"
        if wall:
            summary += f"; looks like a {wall} wall"

    result: dict[str, Any] = {
        "summary": summary,
        "final_url": url_now,
        "title": title,
        "actions": performed,
        "page_text": page_text,
        "events": events,
        "notes": notes,
        "console_error_count": len(console_errors),
        "metrics": {"duration_seconds": round(time.monotonic() - started, 3)},
    }
    if screenshot_report is not None:
        result["screenshot"] = screenshot_report
    return result


def main() -> int:
    return run_runner(body, name="browser")


if __name__ == "__main__":
    raise SystemExit(main())

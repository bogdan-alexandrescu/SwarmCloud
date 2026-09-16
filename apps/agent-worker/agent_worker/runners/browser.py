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
import time
from typing import Any
from urllib.parse import urlparse

from .base import RunnerContext, RunnerFailure, run_runner

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_TIMEOUT_MS = 30_000
MAX_ACTIONS = 200


def _check_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        raise RunnerFailure("browser action requires a url")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise RunnerFailure(f"url scheme {parsed.scheme!r} is not allowed")
    if not parsed.netloc:
        raise RunnerFailure(f"url {url!r} has no host")
    return url


def _launch(playwright: Any, payload: dict[str, Any]) -> Any:
    return playwright.chromium.launch(
        headless=True,
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
        from playwright.sync_api import sync_playwright  # lazy: only in the browser image
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
                    response = page.goto(url, wait_until=action.get("wait_until", "load"))
                    record["url"] = url
                    record["status"] = response.status if response else None
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
                    page.screenshot(path=str(path), full_page=bool(action.get("full_page", True)))
                    record["artifact"] = path.name
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

            final_text = page.inner_text("body") if payload.get("extract_text", True) else ""
            if final_text:
                ctx.write_artifact("page.txt", final_text)
            if payload.get("screenshot", True):
                ctx.write_artifact(
                    "final.png", page.screenshot(full_page=True)
                )
            title = page.title()
            url_now = page.url
        finally:
            browser.close()

    if console_errors:
        ctx.write_artifact("console-errors.json", json.dumps(console_errors, indent=2))

    return {
        "summary": f"visited {url_now} ({title}) with {len(performed)} action(s)",
        "final_url": url_now,
        "title": title,
        "actions": performed,
        "console_error_count": len(console_errors),
        "metrics": {"duration_seconds": round(time.monotonic() - started, 3)},
    }


def main() -> int:
    return run_runner(body, name="browser")


if __name__ == "__main__":
    raise SystemExit(main())

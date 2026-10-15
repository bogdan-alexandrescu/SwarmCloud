"""QA-SCRIPTED (part of #1363): the scripted browser runner extracts a page
whose DOM is ready instead of failing on a load-event or font-wait timeout.

Measured 2026-10-15 (QA batch full-1015, profile `browser`): S1-adheavy and
S1-iframes failed 3/3 on `Page.goto: Timeout 30000ms exceeded ... waiting
until load`; S1-wikipedia failed 3/3 in the final screenshot's font wait with
111,583 chars already in page.txt; S1-cookie succeeded with an empty page.txt
and nothing in the result saying so.

Playwright is replaced by a fake: these tests run with no browser, and they
drive `browser.body` end to end through the same lazy import the image uses.

MUTATION: put the goto default back to "load" and
`test_a_goto_defaults_to_domcontentloaded` fails; drop the TimeoutError
handling around `page.goto` and the commit test fails; drop the screenshot
try/except and the screenshot test fails.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agent_worker.runners import browser
from agent_worker.runners.base import RunnerContext, RunnerFailure


class FakeTimeoutError(Exception):
    """Stands in for playwright.sync_api.TimeoutError."""


class FakePage:
    def __init__(
        self,
        *,
        goto_times_out: bool = False,
        commits: bool = True,
        screenshot_times_out: bool = False,
        text: str = "hello world",
        title: str = "Example",
    ) -> None:
        self.url = "about:blank"
        self.goto_times_out = goto_times_out
        self.commits = commits
        self.screenshot_times_out = screenshot_times_out
        self.text = text
        self._title = title
        self.goto_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    def on(self, _event: str, _handler: Any) -> None:
        pass

    def goto(self, url: str, **kwargs: Any) -> Any:
        self.goto_calls.append({"url": url, **kwargs})
        if self.commits:
            self.url = url
        if self.goto_times_out:
            raise FakeTimeoutError(
                f'Page.goto: Timeout 30000ms exceeded.\nnavigating to "{url}", '
                f'waiting until "{kwargs.get("wait_until")}"'
            )
        return types.SimpleNamespace(status=200)

    def wait_for_load_state(self, state: str = "load", **_kw: Any) -> None:
        if not self.commits:
            raise FakeTimeoutError(f"waiting for {state}")

    def inner_text(self, _selector: str) -> str:
        return self.text

    def screenshot(self, **kwargs: Any) -> bytes:
        self.screenshot_calls.append(kwargs)
        if self.screenshot_times_out:
            raise FakeTimeoutError(
                "Page.screenshot: Timeout 30000ms exceeded.\n"
                "waiting for fonts to load...\nfonts loaded"
            )
        return b"\x89PNG fake"

    def title(self) -> str:
        return self._title


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def set_default_timeout(self, _ms: int) -> None:
        pass

    def new_page(self) -> FakePage:
        return self.page


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def new_context(self, **_kw: Any) -> FakeContext:
        return FakeContext(self.page)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_playwright(monkeypatch):
    """Install a fake `playwright.sync_api`; returns a setter for the page."""
    holder: dict[str, FakePage] = {}

    class _Manager:
        def __enter__(self) -> Any:
            return types.SimpleNamespace()

        def __exit__(self, *_exc: Any) -> None:
            return None

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _Manager()  # type: ignore[attr-defined]
    sync_api.TimeoutError = FakeTimeoutError  # type: ignore[attr-defined]
    sync_api.Error = Exception  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(browser, "_launch", lambda _pw, _payload: FakeBrowser(holder["page"]))

    def use(page: FakePage) -> FakePage:
        holder["page"] = page
        return page

    return use


def _ctx(tmp_path: Path, payload: dict[str, Any]) -> RunnerContext:
    work = tmp_path / "work"
    work.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


URL = "https://example.com/heavy"


def test_a_goto_defaults_to_domcontentloaded(fake_playwright, tmp_path):
    page = fake_playwright(FakePage())
    browser.body(_ctx(tmp_path, {"url": URL}))
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"


def test_a_goto_that_times_out_after_commit_still_extracts(fake_playwright, tmp_path):
    """S1-adheavy / S1-iframes: the load event never came, the DOM did."""
    page = fake_playwright(FakePage(goto_times_out=True, text="the article"))
    ctx = _ctx(tmp_path, {"actions": [{"type": "goto", "url": URL, "wait_until": "load"}]})
    out = browser.body(ctx)

    assert (ctx.artifacts_dir / "page.txt").read_text() == "the article"
    assert out["page_text"]["chars"] == len("the article")
    goto = out["actions"][0]
    assert goto["load_timed_out"] is True
    assert goto["status"] is None
    codes = [e["code"] for e in out["events"]]
    assert "goto_load_timeout_after_commit" in codes
    assert page.url == URL


def test_a_goto_that_never_commits_fails_with_a_clear_error(fake_playwright, tmp_path):
    fake_playwright(FakePage(goto_times_out=True, commits=False))
    ctx = _ctx(tmp_path, {"url": URL})
    with pytest.raises(RunnerFailure) as caught:
        browser.body(ctx)
    message = str(caught.value)
    assert "never committed" in message
    assert "action 0" in message


def test_a_screenshot_timeout_leaves_the_task_succeeded_with_the_screenshot_missing(
    fake_playwright, tmp_path
):
    """S1-wikipedia: 111,583 chars extracted, then the font wait timed out."""
    page = fake_playwright(FakePage(screenshot_times_out=True, text="x" * 1000))
    ctx = _ctx(tmp_path, {"url": URL})
    out = browser.body(ctx)

    assert out.get("status", "succeeded") == "succeeded"
    assert out["screenshot"] == {"artifact": "final.png", "missing": True, "error": "timeout"}
    assert not (ctx.artifacts_dir / "final.png").exists()
    assert any("screenshot" in note for note in out["notes"])
    assert "screenshot_missing" in [e["code"] for e in out["events"]]
    # bounded, and animations off
    call = page.screenshot_calls[0]
    assert call["timeout"] == browser.SCREENSHOT_TIMEOUT_MS
    assert call["animations"] == "disabled"
    assert (ctx.artifacts_dir / "page.txt").read_text() == "x" * 1000


def test_a_screenshot_action_that_times_out_is_marked_missing(fake_playwright, tmp_path):
    fake_playwright(FakePage(screenshot_times_out=True))
    ctx = _ctx(tmp_path, {"url": URL, "screenshot": False, "actions": [{"type": "screenshot"}]})
    out = browser.body(ctx)
    shot = out["actions"][1]
    assert shot["missing"] is True
    assert "artifact" not in shot


def test_a_successful_screenshot_is_written(fake_playwright, tmp_path):
    fake_playwright(FakePage())
    ctx = _ctx(tmp_path, {"url": URL})
    out = browser.body(ctx)
    assert (ctx.artifacts_dir / "final.png").read_bytes() == b"\x89PNG fake"
    assert out["screenshot"] == {"artifact": "final.png", "missing": False}


def test_an_empty_extraction_reports_chars_zero_and_the_title(fake_playwright, tmp_path):
    """S1-cookie: SUCCEEDED with an empty page.txt and no word about why."""
    fake_playwright(FakePage(text="   ", title="Just a moment..."))
    ctx = _ctx(tmp_path, {"url": URL})
    out = browser.body(ctx)
    assert out["page_text"]["chars"] == 0
    assert out["page_text"]["title"] == "Just a moment..."
    assert out["page_text"]["final_url"] == URL
    assert out["page_text"]["wall"] == "challenge"
    assert "0 chars" in out["summary"]
    assert any("empty" in note for note in out["notes"])
    json.dumps(out)  # the result must stay serialisable


@pytest.mark.parametrize(
    ("title", "wall"),
    [
        ("Just a moment...", "challenge"),
        ("Attention Required! | Cloudflare", "challenge"),
        ("Human verification", "challenge"),
        ("Before you continue to Google", "consent"),
        ("Cookie consent", "consent"),
        ("Example Domain", None),
        ("", None),
    ],
)
def test_a_wall_is_named_from_the_title(title, wall):
    assert browser._wall_from_title(title) == wall


def test_a_page_with_text_reports_no_wall_by_default(fake_playwright, tmp_path):
    fake_playwright(FakePage(text="content", title="Example Domain"))
    out = browser.body(_ctx(tmp_path, {"url": URL}))
    assert out["page_text"] == {
        "chars": 7,
        "title": "Example Domain",
        "final_url": URL,
        "wall": None,
    }
    assert out["notes"] == []

"""QA-KIT (part of #1649): what a live-browser agent needs for visual QA.

Owner decision, 2026-10-19 ("QA kit for the agent"):

* the gate sets `prefers-color-scheme` (`swarm-handoff theme dark|light|system`)
  with `Emulation.setEmulatedMedia` on its OWN session on every page, so the
  choice holds across navigation, reaches pages opened later, survives a
  device switch and a lost session, and is refused unless named off the list;
* the agent's instructions name the device presets and the theme command, and
  say that `browser_resize` is refused and why;
* Playwright MCP is registered with `--output-dir $SWARM_ARTIFACTS_DIR/screenshots`
  and nothing that turns its image responses off;
* a `browser_take_screenshot` whose tool_result carries an image block is
  counted as seen by the model, from the CLI's own stream.

No Chromium, no network: the gate's CDP is a scripted fake that answers the
way Chromium does for the calls made here, and the runner's CLI is replaced.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker.livegate import ctl
from agent_worker.livegate.__main__ import LiveGate
from agent_worker.livegate.device import DeviceController, DeviceRefused
from agent_worker.livegate import presets
from agent_worker.livegate.presets import PRESETS

ENDPOINT = "http://127.0.0.1:9222"
MEDIA = "Emulation.setEmulatedMedia"


class Chromium:
    """The gate's pipe, as far as the device controller uses it."""

    def __init__(self, pages: tuple[str, ...] = ("P1",)) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self._sessions = 0

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page"} for t in self.pages]}
        if method == "Target.attachToTarget":
            self._sessions += 1
            return {"sessionId": f"S{self._sessions}"}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1}
        if method == "Browser.getVersion":
            return {"product": "Chrome/130.0.6723.58", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        return {}

    def media(self, session: str | None = None) -> list[dict[str, Any]]:
        return [p for m, p, s in self.calls if m == MEDIA and (session is None or s == session)]


class Display:
    async def resize(self, width: int, height: int) -> None:
        pass


class Scale:
    def set_scale(self, scale: float) -> None:
        pass


def _controller(chromium: Chromium, records: list[tuple[str, dict[str, str]]]) -> DeviceController:
    return DeviceController(chromium, Display(), Scale(), record=lambda k, f: records.append((k, dict(f))))


def _scheme(value: str) -> dict[str, Any]:
    return {"features": [{"name": "prefers-color-scheme", "value": value}]}


# ------------------------------------------------------------------ the preset


def test_phone_390_is_a_preset_with_iphone_15s_mobile_flags():
    phone, iphone = PRESETS["phone-390"], PRESETS["iphone-15"]
    assert (phone.width, phone.height) == (390, 844)
    for flag in ("scale", "mobile", "touch", "user_agent", "platform", "hints_platform", "natural"):
        assert getattr(phone, flag) == getattr(iphone, flag), flag


# ------------------------------------------------------------- the theme, gate


def test_the_theme_is_set_on_every_page_from_the_gates_own_session_and_recorded():
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> tuple[Chromium, DeviceController, dict[str, Any]]:
        chromium = Chromium(("P1", "P2"))
        device = _controller(chromium, records)
        await device.start()
        assert chromium.media() == [], "no theme was asked for: Chromium's own is left alone"
        return chromium, device, await device.handle_theme({"theme": "dark"})

    chromium, device, answer = asyncio.run(main())
    assert answer == {"theme": "dark"} and device.theme == "dark"
    for page in ("P1", "P2"):
        session = device.session_for(page)
        assert session is not None
        assert chromium.media(session) == [_scheme("dark")]
    assert ("theme_set", {"theme": "dark"}) in records


def test_system_clears_the_override():
    async def main() -> Chromium:
        chromium = Chromium()
        device = _controller(chromium, [])
        await device.start()
        await device.handle_theme({"theme": "light"})
        await device.handle_theme({"theme": "system"})
        return chromium

    assert asyncio.run(main()).media() == [_scheme("light"), _scheme("")]


@pytest.mark.parametrize("args", [
    {"theme": "sepia"},
    {"theme": "Dark"},
    {"theme": None},
    {},
    {"theme": "dark", "media": "print"},
    {"theme": "dark", "features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
])
def test_a_theme_off_the_list_or_carrying_anything_else_is_refused_and_changes_nothing(args):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> tuple[Chromium, DeviceController]:
        chromium = Chromium()
        device = _controller(chromium, records)
        await device.start()
        with pytest.raises(DeviceRefused):
            await device.handle_theme(args)
        return chromium, device

    chromium, device = asyncio.run(main())
    assert chromium.media() == [] and device.theme == "system"
    assert records == [("theme_refused", {"cause": "not_a_theme"})]


def test_a_page_opened_after_the_theme_is_given_it():
    async def main() -> tuple[Chromium, DeviceController]:
        chromium = Chromium()
        device = _controller(chromium, [])
        await device.start()
        await device.handle_theme({"theme": "dark"})
        chromium.pages.append("P2")
        device.on_event({"method": "Target.targetCreated",
                         "params": {"targetInfo": {"targetId": "P2", "type": "page"}}})
        for _ in range(50):
            await asyncio.sleep(0)
            if device.session_for("P2") and chromium.media(device.session_for("P2")):
                break
        return chromium, device

    chromium, device = asyncio.run(main())
    assert chromium.media(device.session_for("P2")) == [_scheme("dark")]


def test_the_theme_is_put_back_when_the_pages_session_is_lost():
    """A renderer swap detaches the gate's session and takes its overrides
    with it (device.py): the page is attached again with the device AND the
    theme, so a cross-site navigation does not drop back to light."""

    async def main() -> tuple[Chromium, DeviceController, str]:
        chromium = Chromium()
        device = _controller(chromium, [])
        await device.start()
        await device.handle_theme({"theme": "dark"})
        lost = device.session_for("P1")
        assert lost is not None
        device.on_event({"method": "Target.detachedFromTarget", "params": {"sessionId": lost}})
        for _ in range(50):
            await asyncio.sleep(0)
            again = device.session_for("P1")
            if again and again != lost and chromium.media(again):
                break
        return chromium, device, lost

    chromium, device, lost = asyncio.run(main())
    again = device.session_for("P1")
    assert again is not None and again != lost
    assert chromium.media(again) == [_scheme("dark")]


def test_a_device_switch_keeps_the_theme():
    async def main() -> tuple[Chromium, DeviceController]:
        chromium = Chromium()
        device = _controller(chromium, [])
        await device.start()
        await device.handle_theme({"theme": "dark"})
        await device.select("phone-390")
        return chromium, device

    chromium, device = asyncio.run(main())
    assert device.device.name == "phone-390" and device.theme == "dark"
    assert chromium.media(device.session_for("P1"))[-1] == _scheme("dark")


def test_theme_is_an_operation_of_the_gate_and_reaches_the_device_controller():
    assert "theme" in ctl.OPS

    async def main() -> tuple[dict[str, Any], Chromium]:
        chromium = Chromium()
        device = _controller(chromium, [])
        await device.start()
        gate = SimpleNamespace(devices=device, _need=lambda component: component)
        return await LiveGate.handle_ctl(gate, "theme", {"theme": "dark"}), chromium  # type: ignore[arg-type]

    answer, chromium = asyncio.run(main())
    assert answer == {"theme": "dark"} and chromium.media() == [_scheme("dark")]


def test_the_theme_is_refused_by_the_gate_once_the_browser_is_gone():
    def gone(component: Any) -> Any:
        raise RuntimeError("the browser is not running")

    gate = SimpleNamespace(devices=None, _need=gone)
    with pytest.raises(RuntimeError):
        asyncio.run(LiveGate.handle_ctl(gate, "theme", {"theme": "dark"}))  # type: ignore[arg-type]


def test_the_theme_list_is_fixed():
    assert presets.THEMES == ("dark", "light", "system")


# --------------------------------------------------------------- the runner


def _ctx(tmp_path: Path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Sweep the settings page at 390 and 1440, light and dark."},
    )


def _run_body(monkeypatch, tmp_path: Path, *, stream: list[dict[str, Any]] | None = None,
              truncated: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    from agent_worker.runners import claude_code
    from agent_worker.runners.cliagent import cli_stream_files

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["args"] = list(extra_args)
        if stream is not None:
            ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = ctx.artifacts_dir / cli_stream_files(spec).stdout
            path.write_text("".join(json.dumps(e) + "\n" for e in stream))
        return {"summary": "", "stdout_truncated": truncated}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in (claude_code.GIT_SPOOL_ENV, claude_code.SEARCH_SPOOL_ENV, claude_code.HANDOFF_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, "/opt/playwright")
    monkeypatch.setenv(claude_code.CDP_ENDPOINT_ENV, ENDPOINT)
    result = claude_code.body(_ctx(tmp_path))
    args = seen["args"]
    seen["mcp"] = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]
    return seen, result


def test_playwright_mcp_saves_its_screenshots_in_the_tasks_artifacts(monkeypatch, tmp_path):
    seen, _ = _run_body(monkeypatch, tmp_path)
    args = seen["mcp"]["playwright"]["args"]
    assert args[args.index("--output-dir") + 1] == str(tmp_path / "artifacts" / "screenshots")


def test_nothing_in_playwright_mcps_registration_turns_image_responses_off(monkeypatch, tmp_path):
    """@playwright/mcp 0.0.83 sends a screenshot back as an image unless
    `--image-responses omit` (or its environment variable) says otherwise, and
    only for a screenshot taken WITHOUT a filename -- which the note asks for."""
    seen, _ = _run_body(monkeypatch, tmp_path)
    server = seen["mcp"]["playwright"]
    assert not any(a.startswith("--image-responses") for a in server["args"])
    assert "PLAYWRIGHT_MCP_IMAGE_RESPONSES" not in (server.get("env") or {})
    # `--caps` would widen the tool set (storage state into the artifacts).
    assert not any(a.startswith("--caps") for a in server["args"])


def test_the_instructions_name_the_presets_the_theme_command_and_why_resize_is_refused(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen, _ = _run_body(monkeypatch, tmp_path)
    prompt = seen["prompt"]
    note = claude_code.LIVE_BROWSER_NOTE
    assert note in prompt
    for name, preset in PRESETS.items():
        assert f"{name} ({preset.width}x{preset.height})" in note, name
    assert "swarm-handoff device <preset>" in note
    assert "swarm-handoff theme dark|light|system" in note
    assert "browser_resize is refused" in note
    assert "the gate owns the browser's size" in note
    assert "without a filename" in note and "$SWARM_ARTIFACTS_DIR/screenshots" in note


def _screenshot_stream(*, image: bool, filename: bool = False) -> list[dict[str, Any]]:
    """The CLI's stream-json for one screenshot call and its result, in the
    shape Claude Code prints: the tool_use in an assistant message, the
    tool_result in the next user message, its content a list of blocks."""
    call = "toolu_" + secrets.token_hex(8)
    tool_input: dict[str, Any] = {"type": "png"}
    if filename:
        tool_input["filename"] = "settings-dark.png"
    content: list[dict[str, Any]] = [{"type": "text", "text": "### Result\n- [Screenshot of viewport](page.png)"}]
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": secrets.token_hex(32)}})
    return [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": call, "name": "mcp__playwright__browser_take_screenshot",
             "input": tool_input}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call, "content": content}]}},
        {"type": "result", "subtype": "success", "result": "done"},
    ]


def test_a_screenshot_whose_result_carries_an_image_block_is_counted_as_seen():
    from agent_worker.runners import claude_code

    stream = _screenshot_stream(image=True) + _screenshot_stream(image=False, filename=True)
    assert claude_code.screenshot_images(stream) == {"taken": 2, "seen_as_image": 1}
    assert claude_code.screenshot_images([]) == {"taken": 0, "seen_as_image": 0}
    # Another tool's image is not a screenshot the model asked for.
    other = _screenshot_stream(image=True)
    other[1]["message"]["content"][0]["name"] = "mcp__playwright__browser_snapshot"
    assert claude_code.screenshot_images(other) == {"taken": 0, "seen_as_image": 0}


def test_the_runner_reports_what_the_model_saw_of_its_screenshots(monkeypatch, tmp_path):
    stream = _screenshot_stream(image=True) + _screenshot_stream(image=True)
    _, result = _run_body(monkeypatch, tmp_path, stream=stream)
    assert result["browser_screenshots"] == {"taken": 2, "seen_as_image": 2, "complete": True}
    _, result = _run_body(monkeypatch, tmp_path, stream=stream, truncated=True)
    assert result["browser_screenshots"]["complete"] is False

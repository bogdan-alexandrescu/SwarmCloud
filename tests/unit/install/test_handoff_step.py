"""The hand-off opens the console's Admin › Setup in the browser, once, after
the console answers behind IAP, and keeps polling (DAYONE-OPEN-PAGES).

Driven through the real engine loop on the fakes of test_handoff.py; the
browser is a fake `open_page`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_install.steps import handoff

from .test_handoff import URL, rig_for


def test_handoff_opens_admin_setup_once_after_console_answers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, world, engine, said = rig_for(tmp_path, monkeypatch)
    opened: list[str] = []

    def fake_open(url: str, *, why: str, **_: object) -> None:
        world.log.append("open")
        opened.append(url)

    monkeypatch.setattr(handoff, "open_page", fake_open)
    result = engine.run()
    assert result.exit_code == 0, result
    assert opened == [URL], "Admin › Setup is opened once"
    first_open = world.log.index("open")
    assert world.log[:first_open] == ["status"] * world.status_calls, "opened only once the console answers"
    assert rig.events.of_kind("wait_countdown"), "then the engine keeps polling the console's finish"


def test_handoff_page_honours_no_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With SWARM_NO_BROWSER=1 the URL is printed, once, and no browser starts."""
    import webbrowser

    started: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: started.append(url))
    monkeypatch.setenv("SWARM_NO_BROWSER", "1")
    monkeypatch.setenv("DISPLAY", ":0")
    rig, world, engine, said = rig_for(tmp_path, monkeypatch)
    assert engine.run().exit_code == 0
    assert started == []
    assert len(said) == 1 and URL in said[0]

"""After a hand-off the agent reads the page the person left, fresh (LB-RETURN; part of #1030).

MEASURED by the live-browser acceptance group on 4+ runs since BROWSER-ENFORCE
(PR 1248): the task SUCCEEDED, and neither its answer nor session-code.txt held
the session code the person's typing put in `#code`. The agent's second read
came back empty. On the agent's side, nothing pointed it at the page the person
had left. It "connected again the same way" and took `context.pages[0]`, but
since BROWSER-ENFORCE that browser also carries Playwright MCP's tab and
every page the agent opened "in its own tab" (#1252). Or it reused the
snapshot or page handle it took before the hand-off. The gate's own resync on
return (DEVB-GATE, livegate/cdpproxy.py) repairs a held connection's contexts.
It cannot choose the tab for a fresh one, and it cannot refresh a value the
agent read before.

So the helper names the page (`swarm_browser.handoff_page`, matched on the url
`swarm-handoff wait` returns), and every place the agent is told what to do
after `wait` exits 0 says: read again, from that tab.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from agent_worker import handoff as handoff_mod
from agent_worker.runners import claude_code

REPO = Path(__file__).resolve().parents[3]
IMAGE = REPO / "images" / "agent-runtime-browser"
FIXTURE = "http://127.0.0.1:8765/login.html"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _context(*urls: str) -> SimpleNamespace:
    return SimpleNamespace(pages=[SimpleNamespace(url=u, name=f"page{i}") for i, u in enumerate(urls)])


def _status(tmp_path: Path, monkeypatch, document: dict | None) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    if document is not None:
        (root / "status.json").write_text(json.dumps(document))
    monkeypatch.setenv("SWARM_HANDOFF", str(root))


def test_handoff_page_is_the_tab_the_person_left_not_the_first_tab(tmp_path, monkeypatch):
    sb = _load("swarm_browser_t1", IMAGE / "swarm_browser.py")
    _status(tmp_path, monkeypatch, {"outcome": "returned", "url": FIXTURE})
    # Chromium's first tab and Playwright MCP's come before the agent's own.
    context = _context("about:blank", "about:blank", FIXTURE + "?next=1", "https://example.com/")
    assert sb.handoff_page(context).url == FIXTURE + "?next=1"


def test_handoff_page_takes_the_url_it_is_given_over_the_spool(tmp_path, monkeypatch):
    sb = _load("swarm_browser_t2", IMAGE / "swarm_browser.py")
    _status(tmp_path, monkeypatch, {"outcome": "returned", "url": "https://example.com/"})
    context = _context("about:blank", FIXTURE, "https://example.com/")
    assert sb.handoff_page(context, url=FIXTURE).url == FIXTURE


def test_without_a_url_handoff_page_is_the_newest_page_that_is_not_blank(tmp_path, monkeypatch):
    sb = _load("swarm_browser_t3", IMAGE / "swarm_browser.py")
    _status(tmp_path, monkeypatch, None)
    context = _context("about:blank", FIXTURE, "about:blank")
    assert sb.handoff_page(context).url == FIXTURE


def test_wait_on_return_tells_the_agent_to_read_the_page_fresh_from_that_tab():
    cmd = _load("swarm_handoff_t", IMAGE / "swarm-handoff")
    said = cmd.NEXT[cmd.EXIT_RETURNED]
    assert "handoff_page" in said, said
    assert "browser_tabs" in said, said
    assert "pages[0]" in said, said
    assert "before the hand-off" in said, said


def test_the_agents_guide_and_the_runner_note_say_the_same():
    for text in (handoff_mod.AGENT_GUIDE, claude_code.LIVE_BROWSER_NOTE):
        assert "handoff_page" in text
        assert "browser_tabs" in text
        assert "before the hand-off" in text

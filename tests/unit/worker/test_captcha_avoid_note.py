"""The live-browser note steers an agent away from what provokes a challenge (CAPTCHA-AVOID; part of #2058).

BGD decided on 2026-10-23 to avoid triggers: the 2026-10-15 benchmark logged
11 to 21 Google 429s per batch, from agents typing searches into google.com in
the live browser. So the note an agent with the live browser is given says:
search with the WebSearch tool, never a search engine's page; prefer a site's
official API to scraping its pages; and keep to about one navigation a second
on any one site. The headless claude-code runner restricts no tool (no
`--allowedTools`, no deny rule of its own), so WebSearch is available to the
agent the note tells to use it; the last test holds that.
"""

from __future__ import annotations

import json

from agent_worker import browser_tools
from agent_worker.runners import claude_code


def test_the_note_says_search_with_the_websearch_tool_never_a_search_engine_page():
    note = claude_code.LIVE_BROWSER_NOTE
    # MUTATION: drop the WebSearch sentence from LIVE_BROWSER_NOTE and this fails.
    assert "For a web search, use the WebSearch tool" in note
    # MUTATION: drop "never a search engine's page in the browser" and this fails.
    assert "never a search engine's page in the browser" in note


def test_the_note_says_prefer_a_sites_official_api_to_scraping():
    note = claude_code.LIVE_BROWSER_NOTE
    # MUTATION: drop the API sentence from LIVE_BROWSER_NOTE and this fails.
    assert "official API" in note and "prefer it to scraping its pages" in note


def test_the_note_says_about_one_navigation_a_second_on_any_one_site():
    note = claude_code.LIVE_BROWSER_NOTE
    # MUTATION: drop the rate sentence from LIVE_BROWSER_NOTE and this fails.
    assert "on any one site, keep to about one navigation a second" in note


def test_every_selection_hands_the_agent_the_avoid_sentences():
    for selected in browser_tools.SELECTIONS:
        choice = browser_tools.choose(selected, jev_installed=False, jev_key=False)
        note = claude_code.live_browser_note(choice, search=False)
        # MUTATION: leave LIVE_BROWSER_NOTE out of live_browser_note() and this fails.
        assert "use the WebSearch tool" in note, selected


def test_the_runner_restricts_no_tool_so_websearch_is_available(tmp_path):
    args = claude_code.SPEC.args_default
    # MUTATION: add "--allowedTools" (or "--disallowedTools WebSearch") to args_default and this fails.
    assert not any(a in args for a in ("--allowedTools", "--allowed-tools", "--tools",
                                       "--disallowedTools", "--disallowed-tools"))
    settings = json.loads(claude_code.write_headless_settings(tmp_path).read_text())
    denied = (settings.get("permissions") or {}).get("deny") or []
    # MUTATION: add a permissions.deny of "WebSearch" to the generated settings and this fails.
    assert not any(str(rule).startswith("WebSearch") for rule in denied)

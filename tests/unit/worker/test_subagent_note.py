"""When a sub-agent is worth starting: one standing paragraph, said once.

Wave 3 of the owner's "Shorter sessions" plan: a read of 30 days of task
records on 2026-11-23 put sub-agent spend at about 15 percent of recorded
cost. The coding agent's standing text says when to start one and when not,
and how to brief it; no cap is enforced (a cap is decided on the scorecard's
measure, scripts/lib/scorecard.py `subagent_share_by_role`).

MUTATIONS: drop the paragraph from the standing list (the once test fails);
append it to the user prompt as well (the once test fails); let it grow past
one short paragraph (the size test fails).
"""

from __future__ import annotations

from agent_worker.runners import claude_code
from test_standing_paragraphs_system_prompt import _run


def test_the_subagent_paragraph_is_in_the_system_prompt_once(tmp_path, monkeypatch):
    system, user, _ = _run(tmp_path, monkeypatch)
    note = claude_code.SUBAGENT_NOTE
    assert (system + user).count(note) == 1
    assert note in system


def test_the_subagent_paragraph_says_when_and_how():
    note = claude_code.SUBAGENT_NOTE.lower()
    for words in ("broad search", "independent work", "known target", "handful of tool calls",
                  "check you could run yourself", "goal", "ruled out"):
        assert words in note, words
    # One short paragraph: no line break, and small beside the standing text.
    assert "\n" not in claude_code.SUBAGENT_NOTE
    assert len(claude_code.SUBAGENT_NOTE) < 600, len(claude_code.SUBAGENT_NOTE)

"""`swarm_follow`'s `format` is normalised at the bridge's boundary (#1865).

MEASURED, 2026-10-21 (wf_647d11a5f7d1492b9b5c, step review-226, plugin sc
0.5.45). A `/sc:swarmcloud` step row ended UNKNOWN although its task finished:
the bridge refused the row's `swarm_follow` with unknown format '"progress"'.
The value arrived JSON-quoted -- the quote characters were part of the string
-- because the row agent encoded the argument twice. The row agent
(`plugin/agents/step.md`) stops with UNKNOWN on any format error, which is
right for a bridge that truly lacks the format, so the fix is the bridge's:
trim whitespace and ONE layer of matching quotes before the guard, and use
that one value for every read after it. What is still not a format after
that is refused as before, with the value as received in the message.
"""

from __future__ import annotations

import json

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError

from test_follow_cursor import World, swarm, world  # noqa: F401 - fixtures
from test_follow_progress_format import _running


def _follow(client, fmt, **arguments) -> str:
    return server._call(client, "swarm_follow", {"task_ids": ["task_a"], "format": fmt, **arguments})


@pytest.mark.parametrize("fmt", ['"progress"', "'progress'", " progress "])
def test_a_quoted_or_padded_progress_follows_as_progress(swarm, world, fmt):
    _running(world)

    # `step_id` is read after the guard, against the format: it must see the
    # normalised value too, or the row is refused one check later.
    text = _follow(swarm, fmt, step_id="review")
    reply = json.loads(text)

    assert "progress" in reply, text  # MUTATION: no normalising -- refused as unknown format.
    assert "lines" not in reply, text  # MUTATION: normalise to lines -- the log stream comes back.
    assert reply["tasks"][0]["step_id"] == "review"  # MUTATION: guard sees the clean value, step check the raw one -- refused.
    assert "\n" not in text, "a progress reply is compact JSON"  # MUTATION: the reply branch reads the raw value -- indented.


def test_a_quoted_lines_follows_as_lines(swarm, world):
    _running(world)

    reply = json.loads(_follow(swarm, '"lines"'))

    assert any("LOGMARKER" in line for line in reply["lines"])  # MUTATION: no normalising -- refused as unknown format.
    assert "progress" not in reply  # MUTATION: every quoted value read as progress -- no log.


@pytest.mark.parametrize("fmt", ['"bogus"', '"progress', "'progress\""])
def test_a_value_that_is_still_not_a_format_is_refused_with_what_was_received(swarm, world, fmt):
    _running(world)

    with pytest.raises(SwarmError) as refused:
        _follow(swarm, fmt)

    message = str(refused.value)
    assert "unknown format" in message  # MUTATION: strip any quote, matched or not -- '"progress' followed.
    assert repr(fmt) in message, message  # MUTATION: show the normalised value -- the quotes vanish from the error.

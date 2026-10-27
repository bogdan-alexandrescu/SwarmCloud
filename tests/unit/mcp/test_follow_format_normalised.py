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


# --------------------------------------------------------------------------
# `step_id` and `parents`, normalised the same way (#2379 comment 20)
# --------------------------------------------------------------------------
#
# MEASURED, 2026-10-26: a `/sc:swarmcloud` step row handed its step id as
# '"review"' -- quote characters inside the string, the same double encoding
# as #1865's format -- and the bridge, which trimmed only whitespace, compared
# 'review' to '"review"', abandoned the task as another step's, and the row
# ended UNKNOWN on a step that succeeded. plugin/workflows/run.js already
# relays the id unquoted; the quotes come from the row agent's own call.


@pytest.mark.parametrize("step_id", ['"review"', "'review'", ' "review" '])
def test_a_quoted_step_id_follows_its_own_step(swarm, world, step_id):
    _running(world, step_id="review")

    for fmt in ("progress", "lines"):
        reply = json.loads(_follow(swarm, fmt, step_id=step_id))
        row = reply["tasks"][0]

        assert not row.get("abandoned"), (fmt, row)  # MUTATION: no normalising -- 'review' vs '"review"', abandoned.
        assert "another step" not in json.dumps(reply), (fmt, reply)


def test_a_different_step_id_is_still_refused_when_quoted(swarm, world):
    _running(world, step_id="implement")

    reply = json.loads(_follow(swarm, "progress", step_id='"review"'))
    row = reply["tasks"][0]

    assert row["abandoned"] is True, row  # MUTATION: drop the step check with the quotes -- another step followed.
    assert "another step" in row["abandoned_because"]
    assert "'review'" in row["abandoned_because"], "the refusal names the normalised step id"


@pytest.mark.parametrize("step_id", ['"review', "review'", "'review\""])
def test_a_lone_or_unmatched_quote_is_not_stripped_from_a_step_id(swarm, world, step_id):
    _running(world, step_id="review")

    reply = json.loads(_follow(swarm, "progress", step_id=step_id))

    assert reply["tasks"][0]["abandoned"] is True, reply  # MUTATION: strip any quote, matched or not -- followed.


@pytest.mark.parametrize("step_id", ['""', "' '", "  "])
def test_a_step_id_that_is_only_quotes_is_refused_as_empty(swarm, world, step_id):
    _running(world)

    with pytest.raises(SwarmError) as refused:
        _follow(swarm, "progress", step_id=step_id)

    assert "non-empty string" in str(refused.value)  # MUTATION: normalise after the check -- '' compared as a step id.


def test_a_quoted_parent_id_is_read_as_that_parent(swarm, world):
    _running(world, step_id="review")
    world.task("task_parent", state="SUCCEEDED")

    reply = json.loads(_follow(swarm, "progress", step_id="review", parents=['"task_parent"', " 'task_parent' "]))

    # MUTATION: parents not normalised -- '"task_parent"' is read, 404s, and maps to None.
    assert reply["parents"] == {"task_parent": "SUCCEEDED"}, reply
    assert not reply["tasks"][0].get("abandoned"), reply


@pytest.mark.parametrize("parent", ['""', '"task_parent'])
def test_a_parent_that_is_only_quotes_is_refused_and_a_lone_quote_kept(swarm, world, parent):
    _running(world, step_id="review")
    world.task("task_parent", state="SUCCEEDED")

    if parent == '""':
        with pytest.raises(SwarmError) as refused:
            _follow(swarm, "progress", step_id="review", parents=[parent])
        assert "non-empty string" in str(refused.value)  # MUTATION: normalise after the check -- '' read as a task id.
        return
    reply = json.loads(_follow(swarm, "progress", step_id="review", parents=[parent]))
    assert list(reply["parents"]) == ['"task_parent'], reply  # MUTATION: strip a lone quote -- read as task_parent.

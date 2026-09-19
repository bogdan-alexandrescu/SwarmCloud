"""The token numbers must survive the truncation that discards the result.

`_truncate_json` replaces an oversized runner result with a preview STRING. The
results that exceed the limit are the long, expensive runs -- so the attempts
that cost the most were exactly the ones whose token counts were lost. These
tests pin the numbers to a path truncation cannot reach.

The sample is the real shape, taken from an actual Claude Code result observed
in this platform's own artifacts on 2026-09-19, not an invented one.
"""

from __future__ import annotations

import json

from agent_worker.lifecycle import _truncate_json, _usage_summary

REAL_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "stop_reason": "end_turn",
    "num_turns": 4,
    "duration_ms": 6286,
    "duration_api_ms": 5102,
    "total_cost_usd": 0.0642028,
    "session_id": "412e6bae-2d76-46d7-9a66-1e38a2f5e711",
    "usage": {
        "input_tokens": 8,
        "cache_creation_input_tokens": 9706,
        "cache_read_input_tokens": 101779,
        "output_tokens": 402,
        "output_tokens_details": {"thinking_tokens": 75},
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {"inputTokens": 922, "costUSD": 0.000987},
        "claude-opus-5": {"inputTokens": 8784, "costUSD": 0.063216},
    },
    "result": "the agent's reply",
}


def test_the_numbers_survive_a_result_too_big_to_keep():
    """The regression this file exists for."""
    bloated = dict(REAL_RESULT, result="x" * 40_000)

    truncated = _truncate_json(bloated, 8000)
    assert truncated == {"truncated": True, "preview": truncated["preview"]}
    # Proof the old path really did lose them: nothing structured remains.
    assert not isinstance(truncated.get("preview"), dict)

    usage = _usage_summary(bloated)
    assert usage["input_tokens"] == 8
    assert usage["output_tokens"] == 402
    assert usage["cache_read_input_tokens"] == 101779
    assert usage["thinking_tokens"] == 75
    assert usage["total_cost_usd"] == 0.0642028
    assert usage["num_turns"] == 4
    assert usage["models"] == ["claude-haiku-4-5-20251001", "claude-opus-5"]


def test_it_stays_small_enough_to_never_be_truncated_itself():
    """A summary that could itself be truncated would solve nothing."""
    encoded = json.dumps(_usage_summary(REAL_RESULT))
    assert len(encoded) < 1000, encoded


def test_a_boolean_is_not_mistaken_for_a_cost():
    """bool is a subclass of int; `is_error: True` must not become a number.

    Asserted rather than assumed, because a silently wrong number is the exact
    class of bug this change was made to stop.
    """
    usage = _usage_summary({"total_cost_usd": True, "num_turns": False})
    assert "total_cost_usd" not in usage
    assert "num_turns" not in usage


def test_junk_in_does_not_raise():
    """Called on every attempt's teardown path; it must never be the thing that fails."""
    assert _usage_summary(None) == {}
    assert _usage_summary("a string") == {}
    assert _usage_summary({}) == {}
    assert _usage_summary({"usage": "not a dict", "total_cost_usd": "free"}) == {}
    assert _usage_summary({"usage": {"input_tokens": "eight"}}) == {}


def test_a_partial_result_yields_what_it_has():
    """An interrupted run still carries whatever the CLI managed to report."""
    usage = _usage_summary({"usage": {"output_tokens": 12}})
    assert usage == {"output_tokens": 12}

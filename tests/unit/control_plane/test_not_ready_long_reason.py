"""A planner's NOT_READY reason longer than 2000 characters is kept, not refused (#977).

The reason is the planner's answer. Refusing a long one made
`parse_planner_output` raise InvalidPlan, which `routes/runs.py` ends as a
FAILED run, and the answer was lost. The stored reason is now bounded by
truncation with a visible marker; the planner's plan.json artifact keeps the
full text.

No credentials, no network, no emulator: `parse_planner_output` is pure.
"""

from __future__ import annotations

import json

import pytest

from swarm_api import issueruns
from swarm_api.issueruns import InvalidPlan, parse_planner_output


def _verdict(reason: str) -> str:
    return json.dumps(
        {"ready": False, "kind": "blocked", "reason": reason, "needs": ["depends on #1"]}
    )


def test_a_5000_character_reason_ends_not_ready_truncated_with_a_marker():
    reason = "word " * 1000
    assert len(reason) == 5000

    kind, verdict = parse_planner_output(_verdict(reason))

    assert kind == "not_ready"
    assert set(verdict) == {"kind", "reason", "needs"}
    assert verdict["kind"] == "blocked"
    assert verdict["needs"] == ["depends on #1"]
    kept = verdict["reason"]
    assert len(kept) <= issueruns.MAX_NOT_READY_REASON_CHARS == 2_000
    assert kept.endswith(issueruns.NOT_READY_REASON_TRUNCATED)
    assert kept.startswith("word word word word")


def test_a_short_reason_is_kept_unchanged():
    reason = "The sort path is rewritten by #612, which is still open."

    kind, verdict = parse_planner_output(_verdict(reason))

    assert kind == "not_ready"
    assert verdict["reason"] == reason
    assert issueruns.NOT_READY_REASON_TRUNCATED not in verdict["reason"]


def test_an_empty_reason_is_still_refused():
    with pytest.raises(InvalidPlan, match="reason"):
        parse_planner_output(_verdict(""))


def test_a_secret_across_the_cut_is_masked_before_truncation():
    # Where the kept text ends: the limit, less the marker.
    cut = issueruns.MAX_NOT_READY_REASON_CHARS - len(issueruns.NOT_READY_REASON_TRUNCATED)
    secret = "ghp_" + "A" * 36
    start = cut - 10
    filler = ("word " * (start // 5 + 1))[:start - 1] + " "
    reason = filler + secret + " " + "word " * 600
    assert reason.index(secret) == start < cut < start + len(secret)

    kind, verdict = parse_planner_output(_verdict(reason))

    assert kind == "not_ready"
    kept = verdict["reason"]
    assert len(kept) <= issueruns.MAX_NOT_READY_REASON_CHARS
    assert kept.endswith(issueruns.NOT_READY_REASON_TRUNCATED)
    # Truncating first would keep "ghp_AAAAAA", too short for the scan to
    # recognise; masking the full text first leaves only the stand-in.
    assert "AAAA" not in kept
    assert "ghp_*" in kept

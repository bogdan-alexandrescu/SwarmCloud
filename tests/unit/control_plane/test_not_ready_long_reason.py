"""A planner's long NOT_READY reason or need is kept, not refused (#977, #2026).

The reason is the planner's answer. Refusing a long one made
`parse_planner_output` raise InvalidPlan, which `routes/runs.py` ends as a
FAILED run, and the answer was lost. The stored reason is now bounded by
truncation with a visible marker; the planner's plan.json artifact keeps the
full text.

A need over 500 characters was refused the same way (`needs.0: String should
have at most 500 characters`, run_7d2c328a57564a6e8c9d on #1999, #2026). Each
need is now bounded by truncation too, at MAX_NOT_READY_NEED_CHARS; an empty
need and more than MAX_NOT_READY_NEEDS of them are still refused.

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


def _needs_verdict(needs: list[str]) -> str:
    return json.dumps({
        "ready": False,
        "kind": "owner_decision",
        "reason": "The issue asks for a choice only the owner can make.",
        "needs": needs,
    })


def test_a_600_character_need_ends_not_ready_truncated_with_a_marker():
    need = "owner decision: " + "x" * 600

    kind, verdict = parse_planner_output(_needs_verdict([need, "depends on #1"]))

    assert kind == "not_ready"
    kept = verdict["needs"][0]
    assert len(kept) <= issueruns.MAX_NOT_READY_NEED_CHARS == 500
    assert kept.endswith(issueruns.NOT_READY_NEED_TRUNCATED)
    assert kept.startswith("owner decision: xxx")
    assert verdict["needs"][1] == "depends on #1"


def test_a_short_need_is_kept_unchanged():
    need = "owner decision: should the sweep skip epics?"

    kind, verdict = parse_planner_output(_needs_verdict([need]))

    assert kind == "not_ready"
    assert verdict["needs"] == [need]
    assert issueruns.NOT_READY_NEED_TRUNCATED not in verdict["needs"][0]


def test_an_empty_need_is_still_refused():
    with pytest.raises(InvalidPlan, match="needs"):
        parse_planner_output(_needs_verdict([""]))


def test_more_than_20_needs_is_still_refused():
    needs = [f"depends on #{n}" for n in range(issueruns.MAX_NOT_READY_NEEDS + 1)]

    with pytest.raises(InvalidPlan, match="needs"):
        parse_planner_output(_needs_verdict(needs))
    kind, _ = parse_planner_output(_needs_verdict(needs[:issueruns.MAX_NOT_READY_NEEDS]))
    assert kind == "not_ready"


def test_a_secret_across_the_need_cut_is_masked_before_truncation():
    # Where the kept text ends: the limit, less the marker.
    cut = issueruns.MAX_NOT_READY_NEED_CHARS - len(issueruns.NOT_READY_NEED_TRUNCATED)
    secret = "ghp_" + "A" * 36
    start = cut - 10
    filler = ("word " * (start // 5 + 1))[:start - 1] + " "
    need = filler + secret + " " + "word " * 200
    assert need.index(secret) == start < cut < start + len(secret)

    kind, verdict = parse_planner_output(_needs_verdict([need]))

    assert kind == "not_ready"
    kept = verdict["needs"][0]
    assert len(kept) <= issueruns.MAX_NOT_READY_NEED_CHARS
    assert kept.endswith(issueruns.NOT_READY_NEED_TRUNCATED)
    assert "AAAA" not in kept
    assert "ghp_*" in kept

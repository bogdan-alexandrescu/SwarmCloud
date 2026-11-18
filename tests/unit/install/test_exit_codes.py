"""The installer's exit codes (docs/design/day-one-install.md §2.1; DAYONE-ENGINE-1).

An `unknown` -- a check that could not run -- is never reported as 0, and
neither is an outcome nobody listed or a set of statuses with nothing in it.
"""

from __future__ import annotations

import itertools

from swarm_install.exitcodes import (
    MET,
    OUTCOMES,
    STATUSES,
    UNKNOWN,
    ExitCode,
    for_outcome,
    for_statuses,
)


def test_unknown_is_never_zero() -> None:
    assert for_outcome("unknown") != 0
    assert for_outcome("something nobody listed") == ExitCode.TROUBLE
    assert for_statuses([]) != 0, "nothing checked is not fine"
    for size in range(1, 4):
        for combo in itertools.product(STATUSES, repeat=size):
            code = for_statuses(combo)
            if UNKNOWN in combo:
                assert code != 0, combo
            assert (code == 0) == all(s == MET for s in combo), combo


def test_codes_match_the_design_table() -> None:
    assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4, 5]
    assert [o for o, c in OUTCOMES.items() if c == 0] == ["ok"]
    assert for_outcome("paused") == 4
    assert for_outcome("refused") == 5
    assert for_outcome("failed") == for_outcome("blocked") == 1

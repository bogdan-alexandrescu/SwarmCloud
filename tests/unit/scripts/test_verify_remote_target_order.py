"""verify-remote.sh runs concurrency-test last, where its backlog can mislead nothing.

THE DEFECT (#1754, item 2 and the lane's item 6). The default order was
smoke, concurrency, race, e2e. concurrency-test submits three times the global
limit of mock tasks and cancels them on exit, but cancelled leases come back
over the following seconds to minutes. race-test ran next, narrowed runner:mock
and read `active=40` -- concurrency-test's leftovers at the pool's normal
ceiling -- and e2e's platform-wide capacity check ran into the same drain.

race-test now refuses a pool held by other work, but the gate should not lean
on a refusal to sequence its own targets: the target that leaves the most work
behind runs last.
"""

from __future__ import annotations

import re

from .gate_script_functions import SCRIPTS

VERIFY_REMOTE = SCRIPTS / "verify-remote.sh"


def default_targets() -> list[str]:
    text = VERIFY_REMOTE.read_text()
    match = re.search(r'\|\| TARGETS=\(([^)]*)\)', text)
    assert match, "verify-remote.sh no longer has a default TARGETS list"
    return match.group(1).split()


def test_every_default_target_is_still_run() -> None:
    # MUTATION: drop a target from the default list and the gate measures less.
    assert sorted(default_targets()) == ["concurrency-test", "e2e-test", "race-test", "smoke-test"]


def test_the_backlog_target_runs_last() -> None:
    targets = default_targets()
    # MUTATION: put concurrency-test back in second place and race-test reads its leases.
    assert targets[-1] == "concurrency-test", targets
    assert targets.index("race-test") < targets.index("concurrency-test")
    assert targets.index("e2e-test") < targets.index("concurrency-test")

"""Contract request 29, applied: `EndCause.PUBLISH_REFUSED`.

ACCEPTED by the owner on 2026-09-29 (#259) and never applied until lane B46
(2026-10-02). The worker's two publish refusals -- a credential in the final
tree, an unusable `pr-title.txt` -- were written as RUNNER_ERROR and
OUTPUTS_MISSING, so the outcome ledger counted a platform refusal as the
runner's error or a missing output. The writers are held in
tests/unit/worker/test_published_nothing.py; the ledger's class in
tests/unit/control_plane/test_outcomes_end_cause.py.
"""

from __future__ import annotations

from swarm_common import models


def test_publish_refused_is_in_the_frozen_contract_with_the_requested_value():
    assert models.EndCause("publish_refused").name == "PUBLISH_REFUSED"
    assert models.EndCause.PUBLISH_REFUSED.value == "publish_refused"


def test_publish_refused_is_appended_so_no_existing_value_moved():
    # The property is "no existing value moved": PUBLISH_REFUSED sits straight
    # after VERDICT_FAILED, where request 29 appended it. Later requests (41,
    # CHILD_CASCADE) append after it, so it is not asserted to be the last.
    values = [c.value for c in models.EndCause]
    assert values.index("publish_refused") == values.index("verdict_failed") + 1, values
    assert "child_cascade" in values[values.index("publish_refused") + 1 :], values

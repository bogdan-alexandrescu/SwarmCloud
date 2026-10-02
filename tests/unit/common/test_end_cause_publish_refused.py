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
    values = [c.value for c in models.EndCause]
    assert values[-1] == "publish_refused", values
    assert values.index("verdict_failed") == len(values) - 2, values

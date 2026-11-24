"""A review's verdict.json that answers the plan's `done_when` statements
(swarm_api.donewhen) reads exactly as one without: the fix's gate, the
re-review and the fix loop take `verdict` and `findings` and ignore the rest.

No cloud, no emulator.
"""

from __future__ import annotations

import json

from agent_worker import verdict as verdict_mod

NOT_YET = {
    "verdict": "NOT_YET",
    "findings": [{"severity": "major", "file": "src/widgets/sort.py", "line": 3,
                  "message": "sort-key.2: an unknown key does not raise"}],
    "requirements": [{"index": 1, "met": False, "note": "the unknown key is accepted"}],
}


def test_a_verdict_with_done_when_reads_as_the_same_verdict_without():
    answered = {**NOT_YET, "done_when": [
        {"id": "sort-key.1", "met": True, "note": "ordered by name"},
        {"id": "sort-key.2", "met": False, "note": "no ValueError"},
    ]}
    with_it = verdict_mod.read_verdict_bytes(json.dumps(answered).encode(), task_id="task_r",
                                             filename="verdict.json")
    without = verdict_mod.read_verdict_bytes(json.dumps(NOT_YET).encode(), task_id="task_r",
                                             filename="verdict.json")
    assert with_it.verdict == without.verdict == "NOT_YET"
    assert with_it.findings == without.findings

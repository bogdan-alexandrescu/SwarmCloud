"""Two of the three tests the generic runner's `pytest` acceptance checks run.

scripts/acceptance/groups/generic.sh asserts a run of this directory reports
"3 passed" (these two and test_single.py's one), so a run that collected less
is caught. The `paths` variant names test_single.py alone and asserts
"1 passed". Standard library only: the runtime image runs this with its own
interpreter, and a clone is not installed.
"""

import json


def test_json_round_trips():
    assert json.loads(json.dumps({"ran": True})) == {"ran": True}


def test_arithmetic_is_arithmetic():
    assert sum(range(5)) == 10

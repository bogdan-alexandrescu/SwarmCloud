"""The one test the generic runner's smoke row runs, and that must pass.

`scripts/smoke-test.sh --profile generic` submits `command: pytest` with
`working_directory: repo/tests/smoke/generic` and this repository as the
task's clone (scripts/lib/testlib.sh, `profile_input` and `profile_extra`).
On an empty workspace pytest exits 5, "no tests collected", which proves
dispatch and nothing about the command; here it exits 0 or the row fails.

Standard library only, and nothing from this repository: the runtime image
runs it with its own interpreter, and a clone is not installed. Held by
tests/unit/scripts/test_profile_input.py, which runs it the way the runner does.
"""

import json


def test_the_generic_runner_ran_pytest_here():
    assert json.loads('{"ran": true}') == {"ran": True}

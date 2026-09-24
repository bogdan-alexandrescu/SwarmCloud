"""The UI fixture's runner input contract is the API's, not a second opinion.

`apps/swarm-ui/src/api.ts` serves a fixture `/v1/capacity` so the console can be
worked on with no deployment. Until this file existed it served no
`input_contract` for any runner profile, so the submit screens' required-keys
path -- the one that refuses `claude-code` a blank prompt before a slot is
spent -- was unreachable in every fixture session (docs/web-ui/ux-plan.md §1.2).

Adding it means stating, in TypeScript, which keys each runner demands. That is
a copy of `swarm_api.runnerinputs`, which is itself a permitted copy of a worker
rule held to the worker by test_workflow_step_input_surface.py. A copy with
nothing comparing it is correct until the day it is not, and a fixture that
disagrees with the API is worse than none: the screen would be verified against
a rule production does not apply.

So this compares the fixture's table with `input_contract()` over the WHOLE
frozen catalogue: a profile missing from the fixture fails, a profile the
catalogue does not hold fails, and a key list that differs fails.

The table is a strict-JSON literal and is read with the same `json_literal`
test_blocker_ui_surface.py uses for FIXTURE_ADMISSION -- one parser for the
fixture literals, not one per test file. No node, no build. The component side
of the same claim ("the fixture actually serves it") is
apps/swarm-ui/src/__tests__/submit.fixture.test.ts.
"""

from __future__ import annotations

import re

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.runnerinputs import input_contract

from .test_blocker_ui_surface import UI, body_of, json_literal, src

needs_ui = pytest.mark.skipif(not (UI / "api.ts").is_file(), reason="apps/swarm-ui/src is not present")

TABLE = "const FIXTURE_INPUT_CONTRACTS"


def _fixture_table() -> object:
    source = src("api.ts")
    assert TABLE in source, (
        "api.ts declares no FIXTURE_INPUT_CONTRACTS. Without it the fixture capacity "
        "serves no input_contract and the submit screens' required-keys path cannot "
        "be reached without a deployment."
    )
    return json_literal(source, TABLE)


@needs_ui
def test_the_fixture_table_is_the_apis_contract_for_every_profile():
    expected = {name: input_contract(profile) for name, profile in RUNNER_PROFILES.items()}
    assert _fixture_table() == expected, (
        "the UI fixture's input contract disagrees with swarm_api.runnerinputs over the "
        "frozen catalogue; a screen verified against it is verified against a rule "
        "production does not apply. Regenerate the literal from input_contract()."
    )


@needs_ui
def test_the_fixture_capacity_serves_the_table_for_every_profile_it_lists():
    """The table existing is not the fixture serving it.

    Every runner profile `fixtureCapacity` lists must take its contract from the
    table by name. Written inline, a profile's list is a third copy; left out,
    that profile reads as unread in every fixture session.
    """
    body = body_of(src("api.ts"), "async function fixtureCapacity(")
    listed = re.findall(r"^\s{8}'?([a-z][a-z0-9-]*)'?: \{\n\s+resource_class:", body, re.MULTILINE)
    assert listed, "could not find the runner profiles fixtureCapacity lists"
    for name in listed:
        spelled = re.escape(name)
        # Either spelling, as FIXTURE_ADMISSION is referenced beside it:
        # `.mock`, and `['claude-code']` where a dot cannot reach.
        assert re.search(
            rf"input_contract:\s*FIXTURE_INPUT_CONTRACTS(\[['\"]{spelled}['\"]\]|\.{spelled}\b(?!-))",
            body,
        ), f"fixtureCapacity lists {name!r} without taking its input_contract from the table"

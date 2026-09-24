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

The TypeScript is read as text, the technique test_workflow_step_input_surface.py
uses -- no node, no build. The table is a flat object literal on purpose, so
this parse has one shape to recognise; the component side of the same claim
("the fixture actually serves it") is apps/swarm-ui/src/__tests__/submit.fixture.test.ts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.runnerinputs import input_contract

ROOT = Path(__file__).resolve().parents[3]
API_TS = ROOT / "apps/swarm-ui/src/api.ts"

needs_ui = pytest.mark.skipif(not API_TS.is_file(), reason="apps/swarm-ui/src is not present")

TABLE = re.compile(
    r"^const FIXTURE_INPUT_CONTRACTS\b[^=]*=\s*\{\n(?P<body>.*?)^\}", re.MULTILINE | re.DOTALL
)
ENTRY = re.compile(
    r"^\s*'?(?P<name>[a-z][a-z0-9-]*)'?\s*:\s*\{\s*required_keys:\s*\[(?P<keys>[^\]]*)\]\s*\},?\s*$"
)


def _fixture_table() -> dict[str, list[str]]:
    source = API_TS.read_text()
    match = TABLE.search(source)
    assert match, (
        "api.ts has no top-level `const FIXTURE_INPUT_CONTRACTS = { ... }`. Without it "
        "the fixture capacity serves no input_contract and the submit screens' "
        "required-keys path cannot be reached without a deployment."
    )
    table: dict[str, list[str]] = {}
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        entry = ENTRY.match(line)
        assert entry, f"unrecognised line in FIXTURE_INPUT_CONTRACTS: {line!r}"
        table[entry.group("name")] = re.findall(r"'([^']*)'", entry.group("keys"))
    return table


@needs_ui
def test_the_fixture_table_is_the_apis_contract_for_every_profile():
    expected = {
        name: list(input_contract(profile)["required_keys"])
        for name, profile in RUNNER_PROFILES.items()
    }
    assert _fixture_table() == expected, (
        "the UI fixture's input contract disagrees with swarm_api.runnerinputs over the "
        "frozen catalogue; a screen verified against it is verified against a rule "
        "production does not apply"
    )


@needs_ui
def test_the_fixture_capacity_serves_the_table_for_every_profile_it_lists():
    """The table existing is not the fixture serving it.

    Every runner profile `fixtureCapacity` lists must take its contract from the
    table by name. Written inline, a profile's list is a third copy; left out,
    that profile reads as unread in every fixture session.
    """
    source = API_TS.read_text()
    start = source.index("async function fixtureCapacity(")
    end = source.index("\nasync function ", start + 1)
    body = source[start:end]
    listed = re.findall(r"^\s{8}'?([a-z][a-z0-9-]*)'?: \{\n\s+resource_class:", body, re.MULTILINE)
    assert listed, "could not find the runner profiles fixtureCapacity lists"
    for name in listed:
        spelled = re.escape(name)
        assert re.search(
            rf"input_contract:\s*FIXTURE_INPUT_CONTRACTS(\[['\"]{spelled}['\"]\]|\.{spelled}\b)",
            body,
        ), f"fixtureCapacity lists {name!r} without taking its input_contract from the table"

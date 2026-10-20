"""The bridge passes a step's `inputs.territory` through (TERRITORY-WARN, part of #1468).

`territory` is the platform's, like `prompt`: every profile takes it and the
frozen catalogue declares it for none, so the bridge's declaration check takes
it out, checks only that it is a list of strings -- swarm-api checks each
entry and answers 422 for a malformed one -- and sends it as given. A spec
that does not give one sends exactly what it sent before: no new required
field.

Offline: nothing is sent anywhere.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RUNNER_PROFILES
from swarm_mcp import profiles as catalogue
from swarm_mcp import workflows
from swarm_mcp.client import SwarmError

TERRITORY = ["apps/agent-worker/agent_worker/territory.py", "tests/unit/worker/"]


def test_a_workflow_step_carries_its_territory():
    steps = workflows.build_steps(
        [{"step_id": "lane", "runner_profile": "claude-code", "prompt": "fix it",
          "inputs": {"issue": 1468, "territory": TERRITORY}}]
    )
    assert steps[0]["input"] == {"prompt": "fix it", "issue": 1468, "territory": TERRITORY}


def test_a_step_without_a_territory_sends_what_it_sent_before():
    steps = workflows.build_steps(
        [{"step_id": "lane", "runner_profile": "claude-code", "prompt": "fix it",
          "inputs": {"issue": 1468}}]
    )
    assert steps[0]["input"] == {"prompt": "fix it", "issue": 1468}


def test_a_profile_that_declares_no_input_still_takes_a_territory():
    name = next(n for n, p in RUNNER_PROFILES.items() if not p.inputs)
    assert catalogue.check_inputs(name, {"territory": TERRITORY}) == {"territory": TERRITORY}


def test_the_dispatch_flag_reads_a_territory_as_json():
    raw = catalogue.parse_input_flags("claude-code", ['territory=["a.py", "docs/"]'])
    assert raw == {"territory": ["a.py", "docs/"]}


@pytest.mark.parametrize("value", ["a.py", [], [3], [""], {"a.py": 1}])
def test_a_territory_that_is_not_a_list_of_paths_is_refused_before_it_travels(value):
    with pytest.raises(SwarmError, match="territory"):
        catalogue.check_inputs("claude-code", {"territory": value})

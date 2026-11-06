"""Contract request 136, part C: `RunnerProfile.runtime_capabilities`.

Each profile names the runtime capabilities its runner provides
(`dom_automation`, `screenshots`, `write_isolation` ...), each a name from
`swarm_api.workcontract.RUNTIME_CAPABILITIES`, so swarm-api can refuse a
strict work contract whose capabilities its profile does not provide. The
request left every profile empty until the routing package fills them in,
and every catalogue entry states its value rather than inheriting it, so the
package's change is a visible edit to each profile, never a default.

Contract request 142 (managed plugins, docs/design/managed-plugins.md section
14 request P4, accepted by the owner 6 November 2026) fills in the five
claude-code profiles: `claude_plugins` (the runner loads Claude Code
plugins) and the plugin runtimes `agent-runtime-base` ships, `node`, `bun`,
`python3` and `bash`. Every other profile stays empty.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from swarm_common import profiles
from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

from swarm_api.workcontract import RUNTIME_CAPABILITIES

PROFILES_PY = Path(profiles.__file__)

#: Contract request 142: what a claude-code profile's runner provides to a
#: managed plugin. The runtimes are the design's closed list (section 5).
PLUGIN_CAPABILITIES = ("claude_plugins", "node", "bun", "python3", "bash")

#: The five profiles whose runner is the claude-code runner, as swarm-api's
#: plugin catalogue names them.
CLAUDE_CODE_PROFILES = (
    "claude-code",
    "claude-code-large",
    "claude-code-review",
    "claude-code-browser",
    "claude-code-lightpanda",
)


def _constructed_profiles() -> dict[str, set[str]]:
    """name -> the keyword arguments of every `RunnerProfile(...)` in the catalogue's source."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(PROFILES_PY.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "RunnerProfile":
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            name = kwargs.get("name")
            assert isinstance(name, ast.Constant), "every catalogue profile is built by name"
            out[name.value] = set(kwargs)
    return out


def test_every_catalogue_profile_states_its_runtime_capabilities():
    constructed = _constructed_profiles()
    assert set(RUNNER_PROFILES) <= set(constructed)
    missing = sorted(n for n, kw in constructed.items() if "runtime_capabilities" not in kw)
    assert missing == []


@pytest.mark.parametrize("name", sorted(RUNNER_PROFILES))
def test_each_profile_names_only_capabilities_the_contract_knows(name):
    caps = RUNNER_PROFILES[name].runtime_capabilities
    assert isinstance(caps, tuple)
    assert set(caps) <= set(RUNTIME_CAPABILITIES) | set(PLUGIN_CAPABILITIES)


def test_the_five_claude_code_profiles_provide_the_plugin_runtimes():
    assert {n: p.runtime_capabilities for n, p in RUNNER_PROFILES.items()
            if p.runtime_capabilities} == {n: PLUGIN_CAPABILITIES for n in CLAUDE_CODE_PROFILES}


def test_the_five_are_the_plugin_catalogues_profiles():
    from swarm_api import plugincatalogue

    assert plugincatalogue.CLAUDE_CODE_PROFILES == CLAUDE_CODE_PROFILES
    assert all(RUNNER_PROFILES[n].agent == "claude-code" for n in CLAUDE_CODE_PROFILES)


@pytest.mark.parametrize("name", sorted(set(RUNNER_PROFILES) - set(CLAUDE_CODE_PROFILES)))
def test_every_other_profile_is_still_empty(name):
    assert RUNNER_PROFILES[name].runtime_capabilities == ()


def test_the_default_is_no_capability():
    profile = RunnerProfile(name="x", image="agent-runtime-base", resource_class="standard",
                            backend=profiles.Backend.CLOUD_RUN_JOB, runner_argv=("true",))
    assert profile.runtime_capabilities == ()


@pytest.mark.parametrize("caps", [
    ["screenshots"],
    ("screenshots", "screenshots"),
    ("",),
    ("Screenshots",),
    ("screenshots", 7),
    ("/usr/bin/chromium",),
])
def test_a_malformed_capability_list_is_refused(caps):
    with pytest.raises(ValueError):
        RunnerProfile(name="x", image="agent-runtime-base", resource_class="standard",
                      backend=profiles.Backend.CLOUD_RUN_JOB, runner_argv=("true",),
                      runtime_capabilities=caps)

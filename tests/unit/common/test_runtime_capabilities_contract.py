"""Contract request 136, part C: `RunnerProfile.runtime_capabilities`.

Each profile names the runtime capabilities its runner provides
(`dom_automation`, `screenshots`, `write_isolation` ...), each a name from
`swarm_api.workcontract.RUNTIME_CAPABILITIES`, so swarm-api can refuse a
strict work contract whose capabilities its profile does not provide. The
request leaves every profile empty until the routing package fills them in,
and every catalogue entry states its value rather than inheriting it, so the
package's change is a visible edit to each profile, never a default.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from swarm_common import profiles
from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

from swarm_api.workcontract import RUNTIME_CAPABILITIES

PROFILES_PY = Path(profiles.__file__)


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
    assert set(caps) <= set(RUNTIME_CAPABILITIES)


def test_every_profile_is_empty_until_the_routing_package_fills_them_in():
    assert {n: p.runtime_capabilities for n, p in RUNNER_PROFILES.items()
            if p.runtime_capabilities} == {}


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

"""GET /v1/capacity serves each runner profile's DECLARED inputs (MCP-INPUTS, part of archived issue 1433).

THE GAP THIS CLOSES. The Submit form's MCP server picker (MCP-UI) and its
browser tool picker (BSTATUS-SUBMIT) are drawn from
`input_contract.inputs.<key>`: `declaredMcpServers` reads `mcp_servers`'s
`maximum`, `declaredBrowserTools` reads `browser_tool`'s `choices`. Read at
3e6df40 on 2026-10-18, `swarm_api.runnerinputs.input_contract` served only
`{"required_keys": [...]}` and no route served `RunnerProfile.inputs`, so both
pickers read "this profile declares nothing" for every profile and were never
drawn against a real API.

THE SHAPE IS THE ONE THE CONSOLE ALREADY READS: an object keyed by input name,
each value the declaration's own `kind`, `minimum`, `maximum`, `choices`,
`required` and `means`. An object rather than a list because both merged
pickers index it by key (`inputs[MCP_SERVERS_KEY]`) and refuse an array
(`isRecord`), so a list would leave them exactly as undrawn as an absent block.
A profile that declares nothing serves `{}`: measured-empty, never absent.

Read from the frozen catalogue (`RunnerProfile.inputs`), never restated: the
expected values below are taken from `RUNNER_PROFILES` itself.

Offline: FakeFirestore and the static verifiers from conftest. No credentials,
no emulator, no network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.runnerinputs import input_contract

from .conftest import auth_header

ROOT = Path(__file__).resolve().parents[3]

#: Every field a served declaration carries. Each names a bound or a meaning a
#: form can check locally; none names an image, a command, a resource or a
#: backend parameter, so nothing here gives a caller a way to choose one
#: (invariant 10). A field added to this set is a decision, reviewed here.
DECLARATION_FIELDS = {"kind", "minimum", "maximum", "choices", "required", "means"}


def _served(client) -> dict:
    response = client.get("/v1/capacity", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    profiles = response.json()["runner_profiles"]
    assert set(profiles) == set(RUNNER_PROFILES), "the route did not serve the whole catalogue"
    return profiles


def test_a_profile_declaring_mcp_servers_serves_it_with_its_kind_and_maximum(client):
    declared = RUNNER_PROFILES["claude-code"].inputs["mcp_servers"]
    served = _served(client)["claude-code"]["input_contract"]["inputs"]["mcp_servers"]
    assert served["kind"] == declared.kind == "list"
    assert served["maximum"] == declared.maximum
    assert served["minimum"] == declared.minimum
    assert served["means"] == declared.means


def test_every_profile_that_declares_mcp_servers_serves_it(client):
    profiles = _served(client)
    declaring = [n for n, p in RUNNER_PROFILES.items() if "mcp_servers" in p.inputs]
    assert declaring, "no profile declares mcp_servers; the picker test above is vacuous"
    for name in declaring:
        assert "mcp_servers" in profiles[name]["input_contract"]["inputs"], name


def test_the_browser_tool_input_serves_its_choices(client):
    declared = RUNNER_PROFILES["claude-code-browser"].inputs["browser_tool"]
    served = _served(client)["claude-code-browser"]["input_contract"]["inputs"]["browser_tool"]
    assert served["kind"] == "string"
    assert served["choices"] == list(declared.choices)
    assert "playwright" in served["choices"]


def test_a_profile_with_no_inputs_serves_an_empty_inputs_block(client):
    assert RUNNER_PROFILES["merge"].inputs == {}, "merge declares inputs now; pick another empty profile"
    contract = _served(client)["merge"]["input_contract"]
    assert contract["inputs"] == {}


def test_every_declared_key_is_served_and_nothing_else(client):
    profiles = _served(client)
    for name, profile in RUNNER_PROFILES.items():
        inputs = profiles[name]["input_contract"]["inputs"]
        assert list(inputs) == list(profile.inputs), name
        for key, decl in profile.inputs.items():
            entry = inputs[key]
            assert set(entry) == DECLARATION_FIELDS, f"{name}.{key}: {sorted(entry)}"
            assert entry["kind"] == decl.kind
            assert entry["minimum"] == decl.minimum
            assert entry["maximum"] == decl.maximum
            assert entry["choices"] == list(decl.choices)
            assert entry["required"] is decl.required
            assert entry["means"] == decl.means


def test_required_keys_is_unchanged(client):
    profiles = _served(client)
    assert profiles["claude-code"]["input_contract"]["required_keys"] == ["prompt"]
    assert profiles["mock"]["input_contract"]["required_keys"] == []
    for name, profile in RUNNER_PROFILES.items():
        assert set(input_contract(profile)) == {"required_keys", "inputs"}, name


def test_nothing_served_names_an_image_command_resource_or_backend():
    """Invariant 10: a caller picks a profile by name and nothing else.

    The served block is bounds and meanings. Its field names are checked
    above; this checks no field's NAME or value smuggles a runtime parameter
    in -- an `image`, `argv`, `env` or resource key on a declaration would be
    an offer to choose one.
    """
    forbidden = {"image", "runner_argv", "argv", "command_argv", "env", "resources",
                 "cpu", "memory", "backend", "resource_class", "service_account"}
    for name, profile in RUNNER_PROFILES.items():
        contract = input_contract(profile)
        assert not forbidden & set(contract), name
        for key, entry in contract["inputs"].items():
            assert not forbidden & set(entry), f"{name}.{key}"


# --------------------------------------------------------------------------
# What the console's pickers are held to (part of #72)
# --------------------------------------------------------------------------
# The MCP-server and browser-tool pickers were held here by reading
# McpServers.tsx and BrowserToolPicker.tsx as text. Now the `profiles`
# constants group (scripts/api_constants/profiles.py) serves the declared keys
# and the field names of a served declaration, `--check` keeps the generated
# file equal to it, and apps/swarm-ui/src/__tests__/contract.profiles.test.tsx
# calls `declaredMcpServers` and `declaredBrowserTools` on a declaration
# carrying only those fields. This half holds what the group serves to the
# route.


def _built() -> dict:
    """What the `profiles` constants group builds, from the live objects."""
    path = ROOT / "scripts/api_constants/profiles.py"
    spec = importlib.util.spec_from_file_location("api_constants_profiles_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def test_the_group_serves_the_fields_this_route_serves(client):
    built = _built()
    assert built["declaration_fields"] == sorted(DECLARATION_FIELDS)
    served = _served(client)
    for name, entry in built["profiles"].items():
        assert entry["declared_inputs"] == list(served[name]["input_contract"]["inputs"]), name


@pytest.mark.parametrize("key", ["mcp_servers", "browser_tool"])
def test_the_group_names_a_profile_serving_each_pickers_key(client, key):
    """A picker whose key no profile serves is never drawn."""
    built = _built()["profiles"]
    declaring = sorted(n for n, e in built.items() if key in e["declared_inputs"])
    assert declaring, f"no profile declares {key!r}; its picker would never be drawn"
    served = _served(client)
    for name in declaring:
        assert key in served[name]["input_contract"]["inputs"], name
    if key == "browser_tool":
        for name in declaring:
            assert built[name]["input_choices"][key] == served[name]["input_contract"]["inputs"][key]["choices"]

"""Registered MCP servers: the frozen contract's part (lane MCP-CONTRACT).

docs/design/registered-mcp-servers.md §5, requests M1-M4, accepted by the
owner 2026-10-17 and filed as contract requests 88-91:

  * 88 (M1): `claude-code`, `codex` and `claude-code-browser` declare the
    input `mcp_servers`, from a dict of its own, so `indexer` and
    `claude-code-review`, which share `_CLI_AGENT_INPUTS`, do not;
  * 89 (M2): `mcp` is a secret-consumer kind with one slot, `auth`;
  * 90 (M3): `MCP_SERVER_NAME` and the reserved names, one spelling;
  * 91 (M4): no new spec format -- `input` is already signed, so a task's
    `mcp_servers` is in its canonical step spec.

No cloud, no emulator: the contract is pure.
"""

from __future__ import annotations

import pytest

from swarm_common import models, profiles, specsign
from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs

TAKES_MCP = ("claude-code", "codex", "claude-code-browser")
REFUSES_MCP = ("browser", "merge", "indexer", "claude-code-review")


# --------------------------------------------------------------------------
# Request 88 (M1): the input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", TAKES_MCP)
def test_cli_agent_profiles_accept_mcp_servers(name):
    assert check_inputs(RUNNER_PROFILES[name], {"mcp_servers": ["kb"]}) == {"mcp_servers": ["kb"]}


@pytest.mark.parametrize("name", TAKES_MCP)
def test_four_names_are_accepted_alongside_issue(name):
    servers = ["kb", "docs", "tickets", "graph"]
    assert check_inputs(RUNNER_PROFILES[name], {"issue": 7, "mcp_servers": servers}) == {
        "issue": 7,
        "mcp_servers": servers,
    }


@pytest.mark.parametrize("name", REFUSES_MCP)
def test_other_profiles_refuse_mcp_servers(name):
    with pytest.raises(InputRefused) as caught:
        check_inputs(RUNNER_PROFILES[name], {"mcp_servers": ["kb"]})
    # Refused as undeclared (no bound broken), while the input exists: the
    # refusal is this profile's, not the contract's lack of the key.
    assert caught.value.key == "mcp_servers"
    assert caught.value.expected is None
    assert "mcp_servers" in RUNNER_PROFILES["claude-code"].inputs


@pytest.mark.parametrize("name", ("indexer", "claude-code-review"))
def test_indexer_and_review_inputs_are_unchanged(name):
    # They share _CLI_AGENT_INPUTS with claude-code; the input is a dict of
    # its own so that it does not reach them.
    assert check_inputs(RUNNER_PROFILES[name], {"issue": 12}) == {"issue": 12}
    assert set(RUNNER_PROFILES[name].inputs) == {"issue"}
    assert set(RUNNER_PROFILES["claude-code"].inputs) - set(RUNNER_PROFILES[name].inputs) == {
        "mcp_servers"
    }


@pytest.mark.parametrize("name", TAKES_MCP)
@pytest.mark.parametrize(
    "value",
    [
        ["a", "b", "c", "d", "e"],   # five names: the bound is four
        [],                          # at least one
        ["kb", 7],                   # a non-string
        "kb",                        # not a list
        ["k" * 32],                  # longer than MCP_SERVER_NAME allows
    ],
    ids=["five", "empty", "non-string", "bare-string", "too-long"],
)
def test_mcp_servers_out_of_bounds_is_refused(name, value):
    with pytest.raises(InputRefused) as caught:
        check_inputs(RUNNER_PROFILES[name], {"mcp_servers": value})
    # Refused for a declared bound, not as an undeclared key. An item's
    # refusal names its index: `mcp_servers[1]`.
    assert caught.value.key.partition("[")[0] == "mcp_servers"
    assert caught.value.expected is not None


def test_mcp_servers_item_bound_is_the_name_bound():
    # The input's item bound and MCP_SERVER_NAME's length are one number.
    spec = RUNNER_PROFILES["claude-code"].inputs["mcp_servers"]
    longest = "k" * int(spec.items.maximum)
    assert models.MCP_SERVER_NAME.fullmatch(longest)
    assert not models.MCP_SERVER_NAME.fullmatch(longest + "k")


# --------------------------------------------------------------------------
# Request 89 (M2): the consumer kind and its slot
# --------------------------------------------------------------------------


def test_mcp_is_a_consumer_kind():
    assert models.SECRET_CONSUMER_KINDS == ("profile", "tool", "component", "mcp")


def test_mcp_consumer_round_trips():
    spelt = models.consumer_name("mcp", "kb")
    assert spelt == "mcp:kb"
    assert models.parse_consumer(spelt) == ("mcp", "kb")


@pytest.mark.parametrize("consumer", ["mcp:kb", "mcp:judge", "mcp:indexer"])
def test_the_catalogue_does_not_vouch_for_a_registration(consumer):
    # Registry membership is swarm-api's to check; `mcp:judge` must not pass
    # because `judge` is a platform component.
    models.parse_consumer(consumer)
    assert profiles.is_secret_consumer(consumer) is False


def test_mcp_secret_slots():
    assert set(profiles.MCP_SECRET_SLOTS) == {"auth"}
    slot = profiles.MCP_SECRET_SLOTS["auth"]
    assert isinstance(slot, profiles.SecretSlot)
    assert slot.kinds == ("api_key", "bearer_token")
    assert slot.required is False
    with pytest.raises(TypeError):
        profiles.MCP_SECRET_SLOTS["other"] = slot  # type: ignore[index]


# --------------------------------------------------------------------------
# Request 90 (M3): one spelling for a server's name
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kb", "a", "knowledge-base-2", "k" * 31])
def test_mcp_server_name_accepts(name):
    assert models.MCP_SERVER_NAME.fullmatch(name)
    assert models.consumer_name("mcp", name) == f"mcp:{name}"


@pytest.mark.parametrize("name", ["", "Kb", "2kb", "-kb", "k" * 32, "kb.local", "kb/x", "kb_x"])
def test_mcp_server_name_refuses(name):
    assert not models.MCP_SERVER_NAME.fullmatch(name)
    with pytest.raises(ValueError):
        models.consumer_name("mcp", name)


@pytest.mark.parametrize("name", ["swarm-git", "swarm-graph", "playwright", "jev"])
def test_reserved_names_are_refused(name):
    # `swarm-git` is well spelt; only the reserved prefix refuses it.
    assert models.MCP_SERVER_NAME.fullmatch(name)
    assert (
        name.startswith(models.RESERVED_MCP_SERVER_PREFIX)
        or name in models.RESERVED_MCP_SERVER_NAMES
    )
    with pytest.raises(ValueError):
        models.consumer_name("mcp", name)
    with pytest.raises(ValueError):
        models.parse_consumer(f"mcp:{name}")


def test_reserved_list():
    assert models.RESERVED_MCP_SERVER_NAMES == ("playwright", "jev")
    assert models.RESERVED_MCP_SERVER_PREFIX == "swarm-"


# --------------------------------------------------------------------------
# Request 91 (M4): no new spec format; the list is signed
# --------------------------------------------------------------------------


def _task_doc(extra: dict) -> dict:
    return {
        "tenant_id": "acme",
        "runner_profile": "claude-code",
        "input": {"prompt": "look it up", **check_inputs(RUNNER_PROFILES["claude-code"], extra)},
        "repository_url": "https://github.com/acme/app",
        "metadata": {},
    }


def test_mcp_servers_is_inside_the_signed_spec():
    assert specsign.SPEC_FORMAT == 3
    without = specsign.canonical_step_spec(_task_doc({}), task_id="task_0123456789abcdef")
    with_mcp = specsign.canonical_step_spec(
        _task_doc({"mcp_servers": ["kb"]}), task_id="task_0123456789abcdef"
    )
    assert without != with_mcp
    assert b'"mcp_servers":["kb"]' in with_mcp

"""Contract requests 80-83 (workspace secrets, #1347), as the owner accepted
them on 2026-10-14 (PR #1354):

  * 80: `SECRET_KINDS`, `SecretSlot` and `RunnerProfile.secret_slots`;
  * 81: `RuntimeTool`, `RUNTIME_TOOLS` and `tools_launched`, and the import-time
    rule that every `browser_tool` choice but `auto` launches a tool;
  * 82: `SECRET_CONSUMER_KINDS`, `PLATFORM_COMPONENTS`, `consumer_name` and
    `parse_consumer` in `models.py`, and `is_secret_consumer` beside the
    catalogue in `profiles.py`;
  * 83: no new park reason: a required slot with no grant parks
    `CREDENTIAL_MISSING`.

WRITTEN TO FAIL BEFORE THE CHANGE, ON ITS ASSERTIONS: every new name is reached
through the module at test time, so on the old contract each case runs and
fails on what it asserts rather than the file failing to import.
"""

from __future__ import annotations

import dataclasses

import pytest

from swarm_common import models, profiles, states
from swarm_common.profiles import RUNNER_PROFILES, Backend, RunnerProfile, WorkerAction

#: Request 80: every profile that runs an agent declares `files`, none required.
AGENT_PROFILES = (
    "claude-code", "codex", "browser", "claude-code-review", "indexer", "claude-code-browser",
    "claude-code-large", "claude-code-lightpanda",
)
# await-input is a control_plane profile (contract request 93), which may not
# declare a slot at all.
NO_SLOT_PROFILES = ("mock", "generic", "merge", "post-verdict", "await-input", "fix-loop")


def _profile(**overrides):
    base = dict(
        name="probe",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.mock"),
    )
    base.update(overrides)
    return RunnerProfile(**base)


def _slot(**overrides):
    base = dict(kinds=("api_key",), means="a probe")
    base.update(overrides)
    return profiles.SecretSlot(**base)


# --------------------------------------------------------------------------
# Request 80
# --------------------------------------------------------------------------


def test_secret_kinds_are_the_designs_six():
    assert profiles.SECRET_KINDS == (
        "api_key", "bearer_token", "basic_auth", "oauth_client", "opaque", "forge_token",
    )


def test_a_slot_with_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown kind"):
        _slot(kinds=("api_key", "api-key"))


def test_a_slot_that_takes_a_forge_token_is_refused():
    # Owner rule 2026-09-25: a forge token is never readable by the agent.
    with pytest.raises(ValueError, match="forge token"):
        _slot(kinds=("forge_token",))
    with pytest.raises(ValueError, match="forge token"):
        _slot(kinds=("opaque", "forge_token"))


def test_a_slot_accepts_at_least_one_kind_and_says_what_it_is_for():
    with pytest.raises(ValueError, match="at least one kind"):
        _slot(kinds=())
    with pytest.raises(ValueError, match="means"):
        _slot(means="   ")


def test_a_slot_is_optional_unless_declared_required():
    assert _slot().required is False
    assert _slot(required=True).required is True


def test_a_worker_action_profile_with_a_slot_is_refused():
    with pytest.raises(ValueError, match="worker_action"):
        _profile(runner_argv=(), worker_action=WorkerAction.MERGE, secret_slots={"files": _slot()})


def test_a_worker_action_profile_with_no_slot_still_constructs():
    assert _profile(runner_argv=(), worker_action=WorkerAction.MERGE).secret_slots == {}


@pytest.mark.parametrize("name", ["Files", "1files", "fi_les", "", "f" * 33, "files/x", "../x"])
def test_a_slot_name_that_is_not_a_short_lower_case_word_is_refused(name):
    with pytest.raises(ValueError, match="not a slot name"):
        _profile(secret_slots={name: _slot()})


def test_a_slot_that_is_not_a_secret_slot_is_refused():
    with pytest.raises(ValueError, match="not a SecretSlot"):
        _profile(secret_slots={"files": {"kinds": ("api_key",), "means": "x"}})


def test_secret_slots_default_empty_and_keep_profiles_hashable():
    probe = _profile(secret_slots={"files": _slot()})
    assert _profile().secret_slots == {}
    field = {f.name: f for f in dataclasses.fields(RunnerProfile)}["secret_slots"]
    assert field.hash is False
    hash(probe)


#: Request 117: the one profile that declares a slot beside `files`.
EXTRA_SLOTS = {"claude-code-browser": {"cf-access"}}


@pytest.mark.parametrize("name", AGENT_PROFILES)
def test_every_agent_profile_declares_files_and_only_the_slots_its_requests_add(name):
    slots = RUNNER_PROFILES[name].secret_slots
    assert set(slots) == {"files"} | EXTRA_SLOTS.get(name, set())
    files = slots["files"]
    assert files.required is False
    assert set(files.kinds) == set(profiles.SECRET_KINDS) - {"forge_token"}


def test_request_117_cf_access_is_an_optional_oauth_client_slot_on_the_browser_profile():
    # MUTATION: widen its kinds, make it required, or declare it on a second
    # profile and this fails.
    cf = RUNNER_PROFILES["claude-code-browser"].secret_slots["cf-access"]
    assert cf.kinds == ("oauth_client",)
    assert cf.required is False
    assert "CF-Access-Client-Id" in cf.means and "CF-Access-Client-Secret" in cf.means
    assert [n for n, p in RUNNER_PROFILES.items() if "cf-access" in p.secret_slots] \
        == ["claude-code-browser"]


@pytest.mark.parametrize("name", NO_SLOT_PROFILES)
def test_mock_generic_and_the_worker_actions_declare_no_slot(name):
    assert RUNNER_PROFILES[name].secret_slots == {}


def test_every_profile_is_classified_by_this_test():
    assert set(AGENT_PROFILES) | set(NO_SLOT_PROFILES) == set(RUNNER_PROFILES)


# --------------------------------------------------------------------------
# Request 81
# --------------------------------------------------------------------------


def test_runtime_tools_are_the_three_browser_tools():
    assert set(profiles.RUNTIME_TOOLS) == {"jev-browser", "dev-browser", "playwright"}
    for key, tool in profiles.RUNTIME_TOOLS.items():
        assert tool.name == key


def test_the_runtime_tool_catalogue_is_read_only():
    with pytest.raises(TypeError):
        profiles.RUNTIME_TOOLS["extra"] = profiles.RUNTIME_TOOLS["playwright"]


def test_every_browser_tool_choice_but_auto_launches_a_runtime_tool():
    # The red test design §8 names for request B.
    choices = RUNNER_PROFILES["claude-code-browser"].inputs["browser_tool"].choices
    launched = {v for t in profiles.RUNTIME_TOOLS.values() for v in t.launched_by}
    missing = [c for c in choices if c != "auto" and c not in launched]
    assert missing == []


def test_every_tool_names_profiles_that_declare_browser_tool_and_only_its_choices():
    for tool in profiles.RUNTIME_TOOLS.values():
        assert tool.profiles
        for name in tool.profiles:
            declared = RUNNER_PROFILES[name].inputs["browser_tool"]
            assert set(tool.launched_by) <= set(declared.choices)


def test_only_jev_browser_has_a_slot_and_it_is_an_optional_api_key():
    tools = profiles.RUNTIME_TOOLS
    assert tools["dev-browser"].secret_slots == {}
    assert tools["playwright"].secret_slots == {}
    assert set(tools["jev-browser"].secret_slots) == {"typesafe"}
    typesafe = tools["jev-browser"].secret_slots["typesafe"]
    assert typesafe.kinds == ("api_key",)
    assert typesafe.required is False


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        ({}, {"jev-browser", "dev-browser", "playwright"}),
        ({"browser_tool": "auto"}, {"jev-browser", "dev-browser", "playwright"}),
        ({"browser_tool": "playwright"}, {"playwright"}),
        ({"browser_tool": "dev-browser"}, {"dev-browser", "playwright"}),
        ({"browser_tool": "jev"}, {"jev-browser", "playwright"}),
    ],
)
def test_tools_launched_follows_the_signed_browser_tool(inputs, expected):
    got = profiles.tools_launched(RUNNER_PROFILES["claude-code-browser"], inputs)
    assert set(got) == expected
    assert len(got) == len(set(got))


def test_a_playwright_attempt_is_never_released_the_jev_key():
    # Option B (design §3.6): the tool checkbox decides which attempts.
    got = profiles.tools_launched(RUNNER_PROFILES["claude-code-browser"], {"browser_tool": "playwright"})
    assert "jev-browser" not in got


@pytest.mark.parametrize("name", sorted(set(RUNNER_PROFILES) - {"claude-code-browser"}))
def test_no_other_profile_launches_a_tool(name):
    # `browser` is a scripted runner that launches none of the three (request 80's correction).
    assert profiles.tools_launched(RUNNER_PROFILES[name], {}) == ()
    assert profiles.tools_launched(RUNNER_PROFILES[name], {"browser_tool": "jev"}) == ()


def test_the_import_time_check_refuses_a_fifth_choice_with_no_tool():
    widened = dict(RUNNER_PROFILES)
    browser = RUNNER_PROFILES["claude-code-browser"]
    tool_input = browser.inputs["browser_tool"]
    widened["claude-code-browser"] = dataclasses.replace(
        browser,
        inputs={
            **browser.inputs,
            "browser_tool": dataclasses.replace(tool_input, choices=(*tool_input.choices, "lynx")),
        },
    )
    with pytest.raises(ValueError, match="lynx"):
        profiles._check_runtime_tools(widened, profiles.RUNTIME_TOOLS)


def test_the_import_time_check_refuses_a_tool_on_a_profile_without_browser_tool():
    bad = dict(profiles.RUNTIME_TOOLS)
    bad["probe"] = profiles.RuntimeTool(name="probe", profiles=("claude-code",), launched_by=("auto",))
    with pytest.raises(ValueError, match="browser_tool"):
        profiles._check_runtime_tools(RUNNER_PROFILES, bad)


def test_the_import_time_check_refuses_a_launched_by_value_that_is_not_a_choice():
    bad = dict(profiles.RUNTIME_TOOLS)
    bad["probe"] = profiles.RuntimeTool(
        name="probe", profiles=("claude-code-browser",), launched_by=("chrome",)
    )
    with pytest.raises(ValueError, match="chrome"):
        profiles._check_runtime_tools(RUNNER_PROFILES, bad)


def test_the_import_time_check_passes_on_the_catalogue():
    profiles._check_runtime_tools(RUNNER_PROFILES, profiles.RUNTIME_TOOLS)


# --------------------------------------------------------------------------
# Request 82
# --------------------------------------------------------------------------


def test_consumer_kinds_and_platform_components():
    # Contract request 89 added "mcp", a registered MCP server's consumer kind.
    assert models.SECRET_CONSUMER_KINDS == ("profile", "tool", "component", "mcp")
    assert models.PLATFORM_COMPONENTS == ("judge", "merge", "indexer", "git")


def test_every_catalogue_consumer_round_trips():
    # Built at test time, not collection time, so the old contract fails here.
    consumers = [
        *(("profile", name) for name in RUNNER_PROFILES),
        *(("tool", name) for name in profiles.RUNTIME_TOOLS),
        *(("component", name) for name in models.PLATFORM_COMPONENTS),
    ]
    assert len(consumers) == len(RUNNER_PROFILES) + 3 + 4
    for kind, name in consumers:
        spelt = models.consumer_name(kind, name)
        assert spelt == f"{kind}:{name}"
        assert models.parse_consumer(spelt) == (kind, name)
        assert profiles.is_secret_consumer(spelt) is True


def test_profile_and_component_with_the_same_name_are_different_consumers():
    assert models.consumer_name("profile", "indexer") != models.consumer_name("component", "indexer")


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("component", "anything-else"),
        ("profile", "Claude-Code"),
        ("tool", "jev:browser"),
        ("", "claude-code"),
        ("image", "agent-runtime-base"),
        ("profile", ""),
        ("profile", "-x"),
        ("profile", "x" * 64),
        ("profile", "a/b"),
    ],
)
def test_consumer_name_refuses(kind, name):
    with pytest.raises(ValueError):
        models.consumer_name(kind, name)


@pytest.mark.parametrize(
    "consumer",
    [
        "component:anything-else",
        "profile:Claude-Code",
        "tool:jev:browser",
        ":claude-code",
        "claude-code",
        "profile:",
        "tool/jev-browser",
        " profile:claude-code",
        "profile:claude-code\n",
        "",
    ],
)
def test_parse_consumer_refuses_anything_consumer_name_would_not_produce(consumer):
    with pytest.raises(ValueError) as caught:
        models.parse_consumer(consumer)
    # It is fed stored ids, so its message echoes nothing.
    assert str(caught.value) == "not a secret consumer name"
    assert profiles.is_secret_consumer(consumer) is False


@pytest.mark.parametrize(
    "consumer",
    ["profile:no-such-profile", "tool:jev", "tool:chromium", "profile:jev-browser", "tool:claude-code"],
)
def test_is_secret_consumer_checks_catalogue_membership(consumer):
    # Well spelt, but not in the catalogue: `jev` is the input value, the tool
    # is `jev-browser` (request 81).
    models.parse_consumer(consumer)
    assert profiles.is_secret_consumer(consumer) is False


# --------------------------------------------------------------------------
# Request 83
# --------------------------------------------------------------------------


def test_a_missing_required_secret_reuses_credential_missing():
    assert states.ParkReason.CREDENTIAL_MISSING.value == "CREDENTIAL_MISSING"
    names = {member.name for member in states.ParkReason}
    assert not {n for n in names if "SECRET" in n or "GRANT" in n}

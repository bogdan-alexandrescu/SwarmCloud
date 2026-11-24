"""What the Submit form may offer, served from the catalogue; what each runner reads (archived pull request 213 review, archived issue 218).

`SUGGESTED` in the console's Submit form is what it offers per profile. The
review of archived pull request 213 found one offer nothing read: the form
offered the browser runner `timeout_seconds`, which only the runners that start
a child through `runners/limits.py` (`resolve_limits`) read -- generic and the
CLI runners. Since contract request 32 (archived issue 218) every runner
profile declares its inputs, so the chain is held in two halves:

  * THE CONSOLE HALF (part of #72): `apps/swarm-ui/src/__tests__/
    contract.profiles.test.tsx` holds `SUGGESTED` and `ACTION_SHAPE` to the
    generated `profiles.json` -- an offer the contract does not declare, a
    declared key the form never offers, generic's `command` choices and the
    browser action shapes. `scripts/gen_api_types.py --check` fails while that
    file disagrees with what `scripts/api_constants/profiles.py` builds.
  * THIS FILE, the Python half: the `profiles` group builds exactly what the
    catalogue declares (so the console test compares with the catalogue), and
    every key a profile whose runner reads its own payload declares is one that
    runner reads (so an offer the console test allows is one a runner reads).
    The runners' source is read as text; no console file is.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.runnerinputs import input_contract
from swarm_api.validation import PLATFORM_INPUT_KEYS

ROOT = Path(__file__).resolve().parents[3]
GROUP = ROOT / "scripts/api_constants/profiles.py"
RUNNERS = ROOT / "apps/agent-worker"
LIMITS = RUNNERS / "agent_worker/runners/limits.py"

_READ = re.compile(r"""payload(?:\.get\(|\[)\s*["']([A-Za-z_]\w*)["']""")

#: Contract request 67's profile, named because the console's parity script
#: and its fixtures name it too.
BROWSER_PROFILE = "claude-code-browser"


def _built() -> dict[str, Any]:
    """What the `profiles` constants group builds, from the live objects."""
    spec = importlib.util.spec_from_file_location("api_constants_profiles_under_test", GROUP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def _limits_keys() -> set[str]:
    """The caller-lowerable limits `resolve_limits` reads from a payload."""
    body = re.search(r"def resolve_limits\(.*?\n    return ", LIMITS.read_text(), re.S)
    assert body, "limits.py has no resolve_limits; this test reads nothing"
    keys = set(re.findall(r'"([a-z_]+)":\s*(?:float\()?ceilings\.', body.group(0)))
    assert "timeout_seconds" in keys, keys
    return keys


def _reads(name: str) -> set[str]:
    """Every key of `input` the runner behind `name` reads."""
    module = RUNNER_PROFILES[name].runner_argv[-1]
    source = (RUNNERS / (module.replace(".", "/") + ".py")).read_text()
    keys = set(_READ.findall(source))
    if "resolve_limits(" in source:
        keys |= _limits_keys()
    return keys


#: Every profile that starts a runner. A `worker_action` profile (contract
#: requests 33 and 35) runs none, and a caller sends it `input: {}`; nor does a
#: `control_plane` profile (contract request 93), which nothing runs at all.
RUNNER_STARTING = sorted(
    n for n, p in RUNNER_PROFILES.items() if p.worker_action is None and not p.control_plane
)

#: The runner-starting profiles whose own module reads its payload: the CLI
#: runners take their prompt through `run_cli_agent` and their `issue`,
#: `pull_request` and `mcp_servers` through modules of their own
#: (tests/unit/mcp/test_runner_inputs.py holds those reads).
PAYLOAD_READING = sorted(n for n in RUNNER_STARTING if "prompt" not in input_contract(RUNNER_PROFILES[n])["required_keys"])


def test_the_group_serves_every_profile_and_only_names_and_keys():
    profiles = _built()["profiles"]
    assert set(profiles) == set(RUNNER_PROFILES)
    assert BROWSER_PROFILE in profiles, "contract request 67's profile is missing from the group"
    for name, entry in profiles.items():
        assert set(entry) == {"declared_inputs", "required_keys", "input_choices", "worker_action",
                              "control_plane"}, name


def test_the_group_marks_every_profile_that_runs_nothing():
    """A worker action or a control-plane profile is offered nothing on the
    console; the group is what tells the console which profiles those are."""
    profiles = _built()["profiles"]
    for name, profile in RUNNER_PROFILES.items():
        entry = profiles[name]
        expected = None if profile.worker_action is None else profile.worker_action.value
        assert entry["worker_action"] == expected, name
        assert entry["control_plane"] is bool(profile.control_plane), name
        if profile.worker_action is not None or profile.control_plane:
            assert entry["declared_inputs"] == [] and entry["required_keys"] == [], name
    assert sorted(n for n, e in profiles.items() if e["worker_action"] is None and not e["control_plane"]) == RUNNER_STARTING


@pytest.mark.parametrize("name", sorted(RUNNER_PROFILES))
def test_the_group_serves_each_profiles_declared_keys_and_choices(name):
    profile = RUNNER_PROFILES[name]
    entry = _built()["profiles"][name]
    assert entry["declared_inputs"] == list(profile.inputs)
    assert entry["required_keys"] == input_contract(profile)["required_keys"]
    assert entry["input_choices"] == {k: list(d.choices) for k, d in profile.inputs.items() if d.choices}


def test_the_group_serves_the_generic_command_names_and_the_platform_keys():
    """generic's `command` NAMES an entry in the frozen argv catalogue; the
    console's picker must be exactly these names."""
    built = _built()
    assert built["profiles"]["generic"]["input_choices"]["command"] == list(
        RUNNER_PROFILES["generic"].inputs["command"].choices
    )
    assert built["platform_input_keys"] == sorted(PLATFORM_INPUT_KEYS)
    assert "prompt" in built["platform_input_keys"]


def test_the_group_serves_the_declared_browser_action_shapes():
    variants = RUNNER_PROFILES["browser"].inputs["actions"].items.variants
    shapes = _built()["browser_actions"]
    assert set(shapes) == set(variants)
    for kind, fields in variants.items():
        assert shapes[kind] == {f: bool(spec.required) for f, spec in fields.items()}, kind
    # The case the review found: `press` takes `key`, never `text`.
    assert "key" in shapes["press"] and "text" not in shapes["press"]


def test_the_group_carries_no_image_command_argv_resource_or_backend():
    """Invariant 10: nothing a caller could use to choose a runtime. The only
    `command` is generic's input KEY, whose values are catalogue names."""
    text = repr(_built())
    for name, profile in RUNNER_PROFILES.items():
        assert repr(profile.image) not in text, name
        assert repr(" ".join(profile.runner_argv)) not in text, name
    for word in ("'image'", "'runner_argv'", "'resource_class'", "'backend'", "'cpu'", "'memory'"):
        assert word not in text, word


@pytest.mark.parametrize("name", PAYLOAD_READING)
def test_every_key_a_payload_reading_runner_declares_is_read(name):
    """The console offers only declared keys; this makes each one a key the
    runner reads, so the form cannot offer one nothing reads."""
    declared = set(RUNNER_PROFILES[name].inputs)
    assert declared, f"{name} declares nothing; this test reads nothing there"
    unread = sorted(declared - _reads(name))
    assert not unread, f"{name} declares {unread}, which its runner never reads"


def test_the_payload_reading_runners_are_the_ones_without_a_prompt():
    assert PAYLOAD_READING == ["browser", "generic", "mock"]


def test_the_limits_are_read_only_by_runners_that_resolve_them():
    """The control for the runner-read check: generic reads `timeout_seconds`
    through `resolve_limits`, and the browser runner does not call it at all."""
    assert "timeout_seconds" in _reads("generic")
    assert "timeout_seconds" not in _reads("browser")
    assert "timeout_seconds" not in RUNNER_PROFILES["browser"].inputs

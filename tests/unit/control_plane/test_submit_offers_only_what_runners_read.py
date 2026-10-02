"""The Submit form offers a profile only what it declares, and its runner reads (#213 review, #218).

`SUGGESTED` in apps/swarm-ui/src/Submit.tsx is what the form offers per
profile. Section 13 of scripts/lib/check-contract-parity.sh holds every offer
to the profile's declaration, and tests/unit/mcp/test_runner_inputs.py holds
every declared key to a read in the runner.

Until contract request 32 (#218) `browser` and `generic` declared nothing, so
neither check applied to them and this file held their offers to the runner's
source instead. The review of #213 had found one offer nothing read: the form
offered the browser runner `timeout_seconds`, described as "lowers the child
wall clock", which only the runners that start a child through
`runners/limits.py` (`resolve_limits`) read -- generic and the CLI runners.

Both declare now. So every profile's offers are held here to BOTH: the
declaration (the same comparison section 13 makes, so the form cannot invite
a 422) and the runner's source (a `payload.get("key")` or `payload["key"]` in
the module its `runner_argv` names, or one of the limits `resolve_limits`
reads when that module calls it). And the browser actions the form BUILDS are
held to the eight declared shapes, field by field. Read from the source,
never imported.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES

ROOT = Path(__file__).resolve().parents[3]
SUBMIT = ROOT / "apps/swarm-ui/src/Submit.tsx"
RUNNERS = ROOT / "apps/agent-worker"
LIMITS = RUNNERS / "agent_worker/runners/limits.py"

_READ = re.compile(r"""payload(?:\.get\(|\[)\s*["']([A-Za-z_]\w*)["']""")


def _offers() -> dict[str, list[str]]:
    """`SUGGESTED`, as {profile: [offered key, ...]}."""
    text = SUBMIT.read_text()
    table = re.search(r"const SUGGESTED\b[^=]*=\s*\{\n(.*?)\n\}\n", text, re.S)
    assert table, "Submit.tsx has no SUGGESTED table; this test reads nothing"
    offers: dict[str, list[str]] = {}
    for block in re.finditer(
        r"^  ('?)([a-z][a-z0-9-]*)\1:\s*\[(.*?)^  \],?$", table.group(1), re.M | re.S
    ):
        offers[block.group(2)] = re.findall(r"name:\s*'([A-Za-z_]+)'", block.group(3))
    return offers


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
#: requests 33 and 35) runs none, and a caller sends it `input: {}`.
RUNNER_STARTING = sorted(n for n, p in RUNNER_PROFILES.items() if p.worker_action is None)


def test_the_table_is_read():
    offers = _offers()
    assert set(offers) >= set(RUNNER_STARTING), (
        f"read offers for {sorted(offers)}, not every profile in the catalogue"
    )


def test_a_worker_action_is_offered_nothing():
    offers = _offers()
    for name, profile in RUNNER_PROFILES.items():
        if profile.worker_action is not None:
            assert not offers.get(name), f"the form offers the worker action {name} {offers[name]}"


@pytest.mark.parametrize("name", RUNNER_STARTING)
def test_every_offer_is_declared_and_read(name):
    offered = _offers()[name]
    assert offered, f"the form offers {name} nothing; this test reads nothing there"
    declared = {"prompt"} | set(RUNNER_PROFILES[name].inputs)
    undeclared = sorted(set(offered) - declared)
    assert not undeclared, (
        f"the Submit form offers {name} {undeclared}, which it does not declare: "
        "the form invites a submission the API refuses with 422 invalid_input"
    )
    unread = sorted(set(offered) - _reads(name) - {"prompt"})
    assert not unread, f"the Submit form offers {name} {unread}, which its runner never reads"


def test_the_generic_command_choices_are_the_declarations():
    """The form's `command` picker is the catalogue's closed list, not a copy
    that can gain a name the API refuses."""
    text = SUBMIT.read_text()
    offer = re.search(r"\{ name: 'command'[^}]*choices: \[([^\]]*)\]", text)
    assert offer, "the form no longer offers generic a command picker"
    choices = set(re.findall(r"'([a-z-]+)'", offer.group(1)))
    assert choices == set(RUNNER_PROFILES["generic"].inputs["command"].choices), choices


def _action_shape() -> dict[str, list[str]]:
    """`ACTION_SHAPE` in Submit.tsx, as {type: [the key each field is SENT as]}."""
    text = SUBMIT.read_text()
    table = re.search(r"const ACTION_SHAPE\b[^=]*=\s*\{\n(.*?)\n\}\n", text, re.S)
    assert table, "Submit.tsx has no ACTION_SHAPE table; this test reads nothing"
    shapes: dict[str, list[str]] = {}
    for line in re.finditer(r"^  ([a-z_]+):\s*\[(.*)\],?$", table.group(1), re.M):
        sent = []
        for part in re.finditer(r"\{([^}]*)\}", line.group(2)):
            prop = re.search(r"prop:\s*'([a-z_]+)'", part.group(1))
            send = re.search(r"send:\s*'([a-z_]+)'", part.group(1))
            assert prop, part.group(0)
            sent.append(send.group(1) if send else prop.group(1))
        shapes[line.group(1)] = sent
    return shapes


def test_every_action_the_form_builds_is_a_declared_shape():
    """The form built `press` as `{selector, text}` while the runner reads
    `key` -- the text typed as the key was sent under a name nothing read, and
    since request 32 the declaration refuses it outright. Every field the form
    sends must be one the declared shape takes."""
    variants = RUNNER_PROFILES["browser"].inputs["actions"].items.variants
    shapes = _action_shape()
    assert set(shapes) == set(variants), (sorted(shapes), sorted(variants))
    for kind, sent in shapes.items():
        extra = sorted(set(sent) - set(variants[kind]))
        assert not extra, f"the form sends a {kind!r} action {extra}, which that shape does not take"
        required = sorted(f for f, spec in variants[kind].items() if spec.required)
        assert set(required) <= set(sent), f"the form's {kind!r} action never sends {required}"


def test_the_limits_are_read_only_by_runners_that_resolve_them():
    """The control for the runner-read check: generic reads `timeout_seconds`
    through `resolve_limits`, and the browser runner does not call it at all."""
    assert "timeout_seconds" in _reads("generic")
    assert "timeout_seconds" not in _reads("browser")

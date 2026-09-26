"""The Submit form offers a profile only the keys its runner reads (#213 review).

`SUGGESTED` in apps/swarm-ui/src/Submit.tsx is what the form offers per
profile. For a profile that DECLARES its inputs, section 13 of
scripts/lib/check-contract-parity.sh holds every offer to the declaration, and
tests/unit/mcp/test_runner_inputs.py holds every declared key to a read in the
runner. For `browser` and `generic`, whose inputs are NOT DECLARED YET (#218),
neither applies: the API bounds what is sent to them by size alone, so a key
their runner never reads is accepted, stored and ignored.

The review found one: the form offered the browser runner `timeout_seconds`,
described as "lowers the child wall clock". Only the runners that start a
child through `runners/limits.py` (`resolve_limits`) read it -- generic and the
CLI runners -- and the browser runner does not, so a user who set it was told
their run was shorter while the platform's ceiling applied unchanged.

So for every profile not declared yet, each key the form offers must be one
the profile's runner reads: a `payload.get("key")` or `payload["key"]` in the
module its `runner_argv` names, or one of the limits `resolve_limits` reads
when that module calls it. Read from the source, never imported.
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

#: The profiles whose inputs the catalogue has not declared yet.
UNDECLARED = sorted(name for name, profile in RUNNER_PROFILES.items() if profile.inputs is None)

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


def test_the_table_is_read():
    offers = _offers()
    assert set(offers) >= set(RUNNER_PROFILES), (
        f"read offers for {sorted(offers)}, not every profile in the catalogue"
    )


@pytest.mark.parametrize("name", UNDECLARED or ["<none>"])
def test_a_profile_not_declared_yet_is_offered_only_keys_its_runner_reads(name):
    if name == "<none>":
        pytest.skip("every profile declares its inputs now; section 13 holds the form")
    offered = _offers()[name]
    assert offered, f"the form offers {name} nothing; this test reads nothing there"
    reads = _reads(name)
    assert reads, f"read no payload key from {name}'s runner; this test reads nothing there"
    unread = sorted(set(offered) - reads)
    assert not unread, (
        f"the Submit form offers {name} {unread}, which its runner never reads: "
        f"the API accepts it (size alone bounds {name}), stores it and nothing acts on it"
    )


def test_the_limits_are_read_only_by_runners_that_resolve_them():
    """The control for the test above: generic reads `timeout_seconds` through
    `resolve_limits`, and the browser runner does not call it at all."""
    assert "timeout_seconds" in _reads("generic")
    assert "timeout_seconds" not in _reads("browser")

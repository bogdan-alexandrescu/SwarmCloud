"""The `profiles` group: what the Submit screens may offer, by profile name.

Read from the live catalogue and from swarm-api's validation, never restated:

  * `profiles`: per runner profile name, the input keys its contract declares
    (`swarm_api.runnerinputs.input_contract(profile)["inputs"]`, in declaration
    order), the keys it requires (`required_keys`), the closed `choices` of
    each input that has them, its `worker_action` (null when it starts a
    runner) and whether it is a `control_plane` profile, which runs nothing;
  * `platform_input_keys`: the keys of `input` every profile takes beside its
    declaration (`validation.PLATFORM_INPUT_KEYS`: the prompt, the territory);
  * `declaration_fields`: the field names every served input declaration
    carries (the keys of each `input_contract.inputs.<key>` object), which the
    console's pickers read -- names, not the values;
  * `browser_actions`: the shapes the `browser` profile's `actions` input
    takes, as {action type: {field: required}};
  * `dispatch`: the strategies and carriers `swarm_api.validation` accepts,
    the strategies it can run today (`dispatchable_strategies()`), the two
    defaults, the keys `codec.dispatch_of` serves for a task with no stored
    dispatch, and the `detail` of each refusal the Submit form attributes to
    a field (an unknown strategy, an unknown carrier, a strategy that pushes
    with no repository), produced by calling `resolve_dispatch_options`.

NAMES AND KEYS ONLY. Invariant 10 (CONTRACT.md) is about what a caller may
send: a profile by name, never an image, command, resource spec or backend
parameter. This file is kept to what a signed-in user is already shown --
GET /v1/runtimes names the profiles and GET /v1/capacity serves each one's
`input_contract`, the declared keys and their `choices` included -- so it
holds no image, no argv, no resource class or size and no backend. The
`generic` profile's `command` choices are NAMES of entries in the platform's
argv catalogue (the caller picks a name, the platform owns the argv), as
`/v1/capacity` already serves them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

INPUTS = (
    "scripts/api_constants/profiles.py",
    "apps/common/swarm_common/",
    "apps/swarm-api/swarm_api/runnerinputs.py",
    "apps/swarm-api/swarm_api/validation.py",
    "apps/swarm-api/swarm_api/codec.py",
)

def _refusal(**kwargs: Any) -> dict[str, Any]:
    """The `detail` `resolve_dispatch_options` refuses these options with."""
    from swarm_api.validation import DispatchOptionError, resolve_dispatch_options

    try:
        resolve_dispatch_options(scale="task", repository_url=None, **kwargs)
    except DispatchOptionError as refused:
        return dict(refused.detail)
    raise AssertionError(f"resolve_dispatch_options accepted {kwargs}; the refusal is gone")


def build() -> dict[str, Any]:
    from swarm_common.profiles import RUNNER_PROFILES

    from swarm_api.codec import dispatch_of
    from swarm_api.runnerinputs import input_contract
    from swarm_api.validation import (
        DEFAULT_CARRIER,
        DEFAULT_STRATEGY,
        DISPATCH_CARRIERS,
        DISPATCH_STRATEGIES,
        PLATFORM_INPUT_KEYS,
        dispatchable_strategies,
    )

    profiles: dict[str, Any] = {}
    fields: set[str] = set()
    for name, profile in RUNNER_PROFILES.items():
        contract = input_contract(profile)
        inputs = contract["inputs"]
        for decl in inputs.values():
            fields.update(decl)
        profiles[name] = {
            "declared_inputs": list(inputs),
            "required_keys": list(contract["required_keys"]),
            "input_choices": {key: list(decl["choices"]) for key, decl in inputs.items() if decl["choices"]},
            "worker_action": None if profile.worker_action is None else profile.worker_action.value,
            "control_plane": bool(profile.control_plane),
        }

    variants = RUNNER_PROFILES["browser"].inputs["actions"].items.variants
    browser_actions = {
        kind: {field: bool(spec.required) for field, spec in fields.items()}
        for kind, fields in variants.items()
    }

    # A task that stored no dispatch block: what every task's block holds at least.
    served = dispatch_of(SimpleNamespace(metadata={}, budget=None))

    return {
        "profiles": profiles,
        "platform_input_keys": sorted(PLATFORM_INPUT_KEYS),
        "declaration_fields": sorted(fields),
        "browser_actions": browser_actions,
        "dispatch": {
            "strategies": list(DISPATCH_STRATEGIES),
            "dispatchable_strategies": list(dispatchable_strategies()),
            "carriers": list(DISPATCH_CARRIERS),
            "default_strategy": DEFAULT_STRATEGY,
            "default_carrier": DEFAULT_CARRIER,
            "task_dispatch_keys": list(served),
            "refusals": {
                "strategy": _refusal(strategy="no-such-strategy", carrier=DEFAULT_CARRIER),
                "carrier": _refusal(strategy=DEFAULT_STRATEGY, carrier="no-such-carrier"),
                "repository_url": _refusal(strategy="direct-pr", carrier=DEFAULT_CARRIER),
            },
        },
    }

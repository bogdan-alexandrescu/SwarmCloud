"""The runner catalogue, as a session is allowed to see it.

WHY THIS EXISTS AT ALL. Invariant 10 says an API caller picks a `runner_profile`
BY NAME and never supplies an image, a command, a resource spec or a backend.
Every write path in this bridge already honours that -- `client.dispatch` has no
image parameter and `workflows.build_steps` refuses an unknown step key by name.
What was missing was the other half of "by name": nowhere in the plugin could a
session find out what the names ARE. The `delegate` skill listed them in a
sentence of prose, which is a restatement of the frozen contract in a file no
test read, and which was already one edit away from being wrong -- `codex` was
disabled on 2026-09-23 and the sentence had to be changed by hand to say so.

So the names come from `swarm_common.profiles.RUNNER_PROFILES` and from nowhere
else. `render.py` already imports `swarm_common.states` for exactly this reason;
this is the same move for the same reason.

WHAT THIS DELIBERATELY DOES NOT SHOW: the image and the command. They are on
every `RunnerProfile` and it would be one dictionary key to serve them. They are
withheld because the invariant is not only "a caller may not SUPPLY an image" --
it is that the image is not part of the caller's vocabulary. A session that can
read `agent-runtime-browser` off a tool response is a session that will sooner
or later offer to change it, and the first thing anyone does with a field they
can see is ask for a parameter that sets it. Nothing downstream needs them: the
name is what dispatch takes, and the size, the backend and the credential
requirement are what a chooser actually weighs.

The `available` flag is served WITH its reason, and that pairing is the point.
`RunnerProfile.__post_init__` already refuses a disabled profile that does not
say why, on the grounds that "a caller told only that a known profile was
refused has nothing to act on". Serving the flag without the reason here would
reintroduce exactly that, one layer up.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_common.profiles import (
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    InputRefused,
    RunnerInput,
    resolve_backend,
)
from swarm_common.profiles import check_inputs as check_inputs_by_declaration

from .client import SwarmError


# --------------------------------------------------------------------------
# The inputs a caller may send a runner (#142)
# --------------------------------------------------------------------------
#
# THE DECLARATION IS THE FROZEN CATALOGUE'S, `RunnerProfile.inputs`, and this
# module keeps no copy of it. Until contract request 25 was accepted (#142,
# 2026-09-25) the bridge held the one table, `DECLARED_INPUTS`, keyed by
# profile NAME; swarm-api now refuses an undeclared key from every caller with
# 422 `invalid_input`, reading the same field, so a table here would be a
# second answer to one question -- the plugin refusing what the API accepts,
# or sending what it refuses. What is left here is the bridge's own framing:
# where a refusal happened (`where`), what to call next (`swarm_profiles`),
# and `swarm dispatch --input KEY=VALUE` typed by the declaration.
#
# WHY PER PROFILE, AND WHY THIS IS NOT INVARIANT 10 LOOSENED. A caller still
# names a profile and supplies DATA; nothing declared is an image, a command,
# a resource spec or a backend parameter. But an input means something only to
# the runner that reads it: `input.model` is read by the CLI runners and would
# select the model a `claude-code` agent runs, which is the contract change
# test_model_flag_is_attribution_only.py exists to stop. So a profile that
# declares nothing takes nothing, and a key a profile does not declare is
# refused by name -- never dropped, never passed through.
#
# A PROFILE WHOSE INPUTS ARE NOT DECLARED YET (`inputs is None`: `browser`,
# `generic`) is bounded by size alone at the API. The bridge sends it none:
# it has no declaration to type a value by or to check one against.
#
# tests/unit/mcp/test_runner_inputs.py holds the bridge to the catalogue, every
# declared key to a `payload` read in the runner's source, and every exit code
# the worker reads as something other than a failure to a refusal.


def declared_inputs(name: str) -> dict[str, RunnerInput]:
    """The inputs this profile declares; `{}` for one that declares none."""
    profile = RUNNER_PROFILES.get(name)
    return dict(profile.inputs or {}) if profile is not None else {}


def declaring() -> list[str]:
    """The profiles that declare at least one input, read from the catalogue."""
    return sorted(name for name in RUNNER_PROFILES if declared_inputs(name))


def _declaring() -> str:
    return ", ".join(declaring()) or "none"


def check_inputs(name: str, raw: Any, *, where: str = "") -> dict[str, Any]:
    """The declared inputs `raw` asks for, checked; refuses anything else. Returns them.

    `raw` is None (nothing asked for) or an object. The profile NAME has already
    been checked by `check`; this is only about what may travel with it. The
    rule is `swarm_common.profiles.check_inputs`, the one swarm-api applies.
    """
    prefix = f"{where}: " if where else ""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SwarmError(
            f"{prefix}`inputs` must be an object of the inputs {name} declares, not "
            f"{type(raw).__name__}"
        )
    profile = RUNNER_PROFILES[name]
    declared = declared_inputs(name)
    if raw and not declared:
        not_yet = (
            " yet -- the catalogue has not decided which keys it takes"
            if profile.inputs is None
            else ""
        )
        raise SwarmError(
            f"{prefix}runner profile {name!r} declares no inputs{not_yet}, so none can "
            f"be sent ({sorted(raw)} were given). Only these profiles declare any: "
            f"{_declaring()} -- see `swarm_profiles`. A step's instructions go in `prompt`."
        )
    try:
        return check_inputs_by_declaration(profile, raw)
    except InputRefused as refused:
        if refused.expected is not None:
            raise SwarmError(f"{prefix}{refused}") from None
        offered = ", ".join(f"{key} ({spec.describe()})" for key, spec in sorted(declared.items()))
        raise SwarmError(
            f"{prefix}{name} does not declare {list(refused.keys)} as an input, so it is not "
            f"sent. It declares: {offered}. The instructions go in `prompt`; an image, a "
            "command, a resource spec, a backend or a model are never sent "
            "(invariant 10)."
        ) from None


def parse_input_flags(name: str, flags: list[str] | None, *, where: str = "") -> dict[str, Any]:
    """`swarm dispatch --input KEY=VALUE ...`, typed by what `name` declares.

    A string input keeps its text as typed -- `fail_message=123` is the message
    "123" -- and every other kind is read as JSON (`120`, `2.5`, `true`), so
    the value is typed by the DECLARATION rather than by whatever JSON happens
    to make of the text. The result goes through `check_inputs`.
    """
    prefix = f"{where}: " if where else ""
    declared = declared_inputs(name)
    raw: dict[str, Any] = {}
    for flag in flags or []:
        key, sep, text = str(flag).partition("=")
        key = key.strip()
        if not sep or not key:
            raise SwarmError(f"{prefix}--input takes KEY=VALUE, not {flag!r}")
        spec = declared.get(key)
        if spec is None or spec.kind in ("string", "filename"):
            raw[key] = text
            continue
        try:
            raw[key] = json.loads(text)
        except json.JSONDecodeError:
            raw[key] = text
    return check_inputs(name, raw or None, where=where)


def names() -> list[str]:
    """Every profile name in the catalogue, including disabled ones.

    Disabled profiles are INCLUDED because they are still names the platform
    knows. A caller who typed `codex` needs to be told it is refused and why,
    not that it does not exist -- which is the distinction the frozen catalogue
    goes out of its way to preserve by disabling rather than deleting.
    """
    return sorted(RUNNER_PROFILES)


def dispatchable() -> list[str]:
    """The names a dispatch can actually name today."""
    return sorted(name for name, p in RUNNER_PROFILES.items() if p.available)


def public_profile(name: str) -> dict[str, Any]:
    """One profile, in the fields a chooser needs and no others.

    An ALLOW-LIST, not a filtered copy of the dataclass. `sc`'s account table
    uses the same shape for the same reason: a projection built by removing
    fields serves every field somebody adds later, and here the field somebody
    adds later could be the image.
    """
    profile = RUNNER_PROFILES[name]
    resource = RESOURCE_CLASSES[profile.resource_class]
    out: dict[str, Any] = {
        "name": profile.name,
        "available": profile.available,
        # Resolved, not raw. A profile may be declared AUTO and the caller
        # cares which backend it will actually land on -- that is the answer to
        # "why did this one queue behind GKE capacity when the others did not".
        "backend": resolve_backend(profile).value,
        "resource_class": profile.resource_class,
        "cpu": resource.cpu,
        "memory_gib": resource.memory_gib,
        # A slice OF memory, not extra capacity: the workspace is a tmpfs. Named
        # `workspace_gib` rather than `disk_gib` because "disk" is what made
        # people read it as additional storage in the first place.
        "workspace_gib": resource.disk_gib,
        "timeout_seconds": profile.timeout_seconds,
        # WHETHER a credential is needed, never WHICH VALUE. `secrets` holds
        # variable NAMES and no material, but serving the names invites a
        # session to go looking for the values, and this bridge has no business
        # anywhere near them.
        "needs_provider_credential": profile.provider is not None,
        "provider": profile.provider,
    }
    if not profile.available:
        out["disabled_reason"] = profile.disabled_reason
    declared = declared_inputs(name)
    if declared:
        # What a caller may send this profile besides its prompt (#142), so the
        # names are learned here rather than from prose that goes stale.
        out["inputs"] = {
            key: {"kind": spec.describe(), "means": spec.means}
            for key, spec in sorted(declared.items())
        }
    return out


def catalogue() -> list[dict[str, Any]]:
    """The whole catalogue, dispatchable first, then refused.

    Ordered rather than alphabetical because the list is read to make a choice,
    and a disabled profile sorted into the middle of the usable ones is a name
    a reader picks before reaching the reason it is there.
    """
    usable = [public_profile(n) for n in dispatchable()]
    refused = [public_profile(n) for n in names() if n not in set(dispatchable())]
    return usable + refused


def check(name: str, *, where: str = "") -> str:
    """Refuse a profile name here, before it costs a dispatch. Returns the name.

    THE COST OF NOT DOING THIS IS MEASURED, not hypothetical. On 2026-09-23 a
    twenty-step run dispatched four `codex` steps and all four failed at the
    provider -- the incident recorded in `swarm_common.profiles` next to the
    `available=False` flag it produced. Every one of those four was admitted,
    leased and dispatched before anything said no. `build_steps` already makes
    this argument about a missing prompt: "a refusal at the keyboard costs
    nothing; the same refusal four minutes later costs a dispatch, a lease and
    a pod." A name the catalogue refuses is the same shape of mistake.

    IT IS NOT A SECOND OPINION ABOUT WHO MAY RUN WHAT. The API refuses these
    too, and it remains the authority -- this cannot admit anything the API
    would refuse, only refuse sooner. It is safe to be this confident locally
    for one specific reason: `apps/swarm-mcp` depends on `swarm-common`, which
    is the SAME frozen module the API imports, so the two cannot hold different
    catalogues. If that dependency ever goes away this check must go with it,
    because then it would be a copy rather than the original.

    The disabled reason is quoted from the catalogue VERBATIM rather than
    reworded. It is written to be read by the person who has to act on it, and
    a paraphrase here would be a second wording of the platform's own answer.
    """
    prefix = f"{where}: " if where else ""
    if not isinstance(name, str) or not name.strip():
        raise SwarmError(
            f"{prefix}no runner profile was named. Pick one by name: "
            f"{', '.join(dispatchable())}"
        )
    profile = RUNNER_PROFILES.get(name)
    if profile is None:
        raise SwarmError(
            f"{prefix}there is no runner profile called {name!r}. "
            f"The catalogue holds: {', '.join(names())}. "
            "A profile is chosen BY NAME -- an image, a command or a resource "
            "spec cannot be supplied instead."
        )
    if not profile.available:
        raise SwarmError(f"{prefix}{name} is refused: {profile.disabled_reason}")
    return name


def check_resource_class(name: str, *, where: str = "") -> str:
    """Refuse a resource-class name the catalogue does not hold. Returns it.

    ONLY THE NAME. Whether the class is within what the step's profile allows
    is `swarm_api.validation`'s decision and stays there; this refuses
    `standrad` for `standard`, which is a typo the API would answer with a 4xx
    after the submission had travelled.
    """
    prefix = f"{where}: " if where else ""
    if name not in RESOURCE_CLASSES:
        raise SwarmError(
            f"{prefix}there is no resource class called {name!r}. "
            f"The catalogue holds: {', '.join(sorted(RESOURCE_CLASSES))}. "
            "A class is chosen BY NAME -- cpu and memory numbers cannot be "
            "supplied instead."
        )
    return name


def backend_of(name: Any) -> str | None:
    """The backend a task's profile resolves to, or None if it is unknowable.

    NEVER RAISES, because its caller is `describe_task`, which reads a task
    document that may name a profile the catalogue no longer holds -- a run
    from before a profile was renamed, say. A result read that threw on an old
    task would take away the one thing that read is for. None here means "not
    known from the catalogue", and the caller must render it as unknown rather
    than as a default backend, which is the same three-marks rule the rest of
    this plugin is held to.
    """
    if not isinstance(name, str):
        return None
    profile = RUNNER_PROFILES.get(name)
    if profile is None:
        return None
    return resolve_backend(profile).value

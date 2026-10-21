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
    MODEL_CLASSES,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    InputRefused,
    RunnerInput,
    effective_size,
    resolve_backend,
    step_model_class,
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
# 422 `invalid_input` wherever a profile declares, reading the same field and
# calling the same rule (`swarm_common.profiles.check_inputs`), so a table
# here would be a second answer to one question -- the plugin refusing what
# the API accepts, or sending what it refuses. What is left here is the
# bridge's own framing:
# where a refusal happened (`where`), what to call next (`swarm_profiles`),
# and `swarm dispatch --input KEY=VALUE` typed by the declaration.
#
# WHY PER PROFILE, AND WHY THIS IS NOT INVARIANT 10 LOOSENED. A caller still
# names a profile and supplies DATA; nothing declared is an image, a command,
# a resource spec or a backend parameter. But an input means something only to
# the runner that reads it: `input.model` was read by the CLI runners and
# selected the model a `claude-code` agent ran, which is the contract change
# test_model_flag_is_attribution_only.py exists to stop (since #226 the runner
# reads only the Job's MODEL, and the worker drops a stored `input.model`). So
# a profile that
# declares nothing takes nothing, and a key a profile does not declare is
# refused by name -- never dropped, never passed through.
#
# EVERY PROFILE DECLARES. Until contract request 32 (#218, accepted by the
# owner on 2026-09-29) `browser` and `generic` were `inputs=None`, bounded by
# size alone at the API, and this bridge sent them nothing -- so every browser
# task dispatched through the plugin failed after it started, for want of a
# `url` (#220). They declare every key their runners read now, and the bridge
# keeps its send policy unchanged: it sends a key only when a declaration
# names it, which gives `--input` a kind to type the value by and lets a bad
# value be refused here, before it travels. A browser task's `url` and
# `actions`, and a generic task's `command`, travel because they are declared.
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


#: Declared inputs whose refused value is NEVER repeated in a refusal: the
#: registered MCP servers a step names (contract request 88). A name is
#: harmless, but the per-run credential that travels beside it
#: (`mcp_credentials`, docs/design/registered-mcp-servers.md §4.3) is the
#: value most likely to be pasted here by mistake, and a refusal is a tool
#: reply in the session's transcript.
_UNREPEATED_INPUTS = frozenset({"mcp_servers"})

#: The key a step declares its lane's territory under: every profile takes it
#: and no runner reads it (`swarm_api.laneterritory`, TERRITORY-WARN).
TERRITORY_INPUT_KEY = "territory"


def check_inputs(name: str, raw: Any, *, where: str = "") -> dict[str, Any]:
    """The declared inputs `raw` asks for, checked; refuses anything else. Returns them.

    `raw` is None (nothing asked for) or an object. The profile NAME has already
    been checked by `check`; this is only about what may travel with it. The
    rule is `swarm_common.profiles.check_inputs`, the one swarm-api applies.
    """
    prefix = f"{where}: " if where else ""
    if raw is None:
        # Nothing asked for is still asked of the rule: `generic` REQUIRES
        # `command`, and a dispatch with no `inputs` must not slip past that.
        raw = {}
    if not isinstance(raw, dict):
        raise SwarmError(
            f"{prefix}`inputs` must be an object of the inputs {name} declares, not "
            f"{type(raw).__name__}"
        )
    # THE LANE'S TERRITORY travels whatever the profile declares
    # (TERRITORY-WARN, part of #1468): it is the platform's, like `prompt`,
    # and the API checks each entry's shape and answers 422 for a malformed
    # one. Refused here only when it is not a list of strings at all.
    territory = raw.get(TERRITORY_INPUT_KEY)
    if TERRITORY_INPUT_KEY in raw and not (
        isinstance(territory, list) and territory
        and all(isinstance(entry, str) and entry.strip() for entry in territory)
    ):
        raise SwarmError(
            f"{prefix}input {TERRITORY_INPUT_KEY!r} is a non-empty list of repo-relative "
            "paths and globs, the only files this step may edit"
        )
    raw = {key: value for key, value in raw.items() if key != TERRITORY_INPUT_KEY}
    kept = {TERRITORY_INPUT_KEY: list(territory)} if territory is not None else {}
    profile = RUNNER_PROFILES[name]
    declared = declared_inputs(name)
    if raw and not declared:
        # The bridge's send policy (see the header): only what a declaration
        # names travels, and this profile's declaration is empty.
        raise SwarmError(
            f"{prefix}runner profile {name!r} declares no inputs, so none can "
            f"be sent ({sorted(raw)} were given). Only these profiles declare any: "
            f"{_declaring()} -- see `swarm_profiles`. A step's instructions go in `prompt`."
        )
    try:
        return {**check_inputs_by_declaration(profile, raw), **kept}
    except InputRefused as refused:
        # A declared key out of its bounds, or a required key not sent: the
        # rule's own message names the key and says which.
        if refused.key.split("[", 1)[0] in _UNREPEATED_INPUTS:
            # The rule's message repeats the start of the refused value; for
            # this input that may be a credential pasted where a name goes.
            raise SwarmError(
                f"{prefix}input {refused.key!r} must be {refused.expected or 'as declared'}; "
                "the value given is not repeated, because a per-run credential belongs in "
                "`mcp_credentials` and a value here could be one"
            ) from None
        if refused.expected is not None or set(refused.keys) <= set(declared):
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

    A text input keeps its text as typed -- `fail_message=123` is the message
    "123", and `target=123` the make target "123" (a `url` and an `argument`
    are text too, contract request 32, Question 3) -- and every other kind is
    read as JSON (`120`, `2.5`, `true`, `[{"type": "screenshot"}]`), so the
    value is typed by the DECLARATION rather than by whatever JSON happens to
    make of the text. The result goes through `check_inputs`.
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
        if key == TERRITORY_INPUT_KEY:
            # `--input 'territory=["apps/x/", "docs/x.md"]'`: a list, as JSON.
            try:
                raw[key] = json.loads(text)
            except json.JSONDecodeError:
                raw[key] = text
            continue
        if spec is None or spec.kind in ("string", "filename", "url", "argument", "header"):
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
    backend = resolve_backend(profile)
    # THE CODE DEFAULTS, version 0. Runner sizes are an admin setting
    # (docs/design/runner-sizes.md §4), which this copy cannot read: it is what
    # a Cloud Run profile always runs at and what a GKE one runs at until an
    # admin resizes its class. `served_catalogue` labels it so.
    size = effective_size(profile.resource_class, None, backend)
    out: dict[str, Any] = {
        "name": profile.name,
        "available": profile.available,
        # Resolved, not raw. A profile may be declared AUTO and the caller
        # cares which backend it will actually land on -- that is the answer to
        # "why did this one queue behind GKE capacity when the others did not".
        "backend": backend.value,
        "resource_class": profile.resource_class,
        # The REQUEST under its old name, beside both CPU numbers: the request
        # is guaranteed and billed, the limit is what a GKE pod may burst to.
        "cpu": size.cpu_request,
        "cpu_request": size.cpu_request,
        "cpu_limit": size.cpu_limit,
        # Request AND limit: memory never bursts (invariant 7).
        "memory_gib": size.memory_gib,
        # A slice OF memory, not extra capacity: the workspace is a tmpfs. Named
        # `workspace_gib` rather than `disk_gib` because "disk" is what made
        # people read it as additional storage in the first place.
        "workspace_gib": size.disk_gib,
        # What admission charges against every pool, in capacity units.
        "units": size.units,
        "size_version": 0,
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
    out.update(_bundled_model_classes(profile.model_classes, profile.default_model_class))
    declared = declared_inputs(name)
    if declared:
        # What a caller may send this profile besides its prompt (#142), so the
        # names are learned here rather than from prose that goes stale.
        out["inputs"] = {
            key: {"kind": spec.describe(), "means": spec.means}
            for key, spec in sorted(declared.items())
        }
    return out


# --------------------------------------------------------------------------
# Model classes (#1435, docs/design/model-per-step.md §4.8)
# --------------------------------------------------------------------------
#
# THE CLASS IS THE CALLER'S WORD, THE ID IS TERRAFORM'S. A step names
# `opus`, `fable`, `sonnet` or `haiku` (the frozen `MODEL_CLASSES`) and the
# platform runs that class's pin, `runner_models` in terraform/infra/locals.tf,
# which swarm-api serves on `GET /v1/runtimes` as each profile's `models`.
# This bridge holds no copy of the pins: one written here would be a second
# answer to "which model does `haiku` run", wrong the day the pin moves. So
# the bundled copy serves the classes and the default, which ARE frozen, and
# says which family each class is and where the ID is read.


def _models_note(classes: tuple[str, ...] | list[str], why: str) -> str:
    families = "; ".join(
        f"{name}: claude-{MODEL_CLASSES[name].family}-<{MODEL_CLASSES[name].min_major} or later>"
        for name in classes
        if name in MODEL_CLASSES
    )
    return (
        f"{why}. The model each class runs is pinned by the platform and served by "
        f"GET /v1/runtimes; each class is a family: {families}"
    )


def _bundled_model_classes(classes: tuple[str, ...], default: str | None) -> dict[str, Any]:
    """The copy's `model_classes`, `default_model_class` and `models` (always `{}`)."""
    out: dict[str, Any] = {
        "model_classes": list(classes),
        "default_model_class": default,
        "models": {},
    }
    if classes:
        out["models_note"] = _models_note(classes, "This bridge's copy holds no model IDs")
    return out


def check_model_class(profile_name: str, name: Any, *, where: str = "") -> str | None:
    """Refuse a model class `profile_name` cannot run, before anything travels. Returns it.

    None (nothing named) is returned as None and NOT resolved to the default:
    a task naming no class is sent without the key, exactly as before the
    classes existed, and runs its profile's default (R3). The rule is
    `swarm_common.profiles.step_model_class`, the one swarm-api, the scheduler
    and the worker apply, so this can only refuse sooner, never admit what the
    API refuses -- and the API still answers `model_class_not_yet_available`
    while its gate is closed, which this cannot see and passes through.

    A MODEL ID IS NOT A CLASS. `claude-haiku-5-5` here is refused naming the
    four classes: the caller picks a class by name and never a model
    (invariant 10).
    """
    if name is None:
        return None
    prefix = f"{where}: " if where else ""
    if not isinstance(name, str) or name not in MODEL_CLASSES:
        raise SwarmError(
            f"{prefix}there is no model class called {name!r}. A step names one of "
            f"{', '.join(MODEL_CLASSES)} -- a class, never a model ID; the platform runs "
            "the class's pinned model (`swarm_profiles` lists them). Nothing was sent"
        )
    profile = RUNNER_PROFILES[profile_name]
    if not profile.model_classes:
        raise SwarmError(
            f"{prefix}{profile_name} runs no Claude model, so a step on it names no "
            "model class. Nothing was sent"
        )
    try:
        return step_model_class(profile, name)
    except ValueError as exc:
        raise SwarmError(f"{prefix}{exc}. Nothing was sent") from None


# A WORKFLOW STEP'S CLASS. `workflows.build_steps` refuses a step key it does
# not list, and `model_class` is not one of them (its `_STEP_KEYS`), so the
# key is taken off each step before the step is built and put back on the
# built step afterwards, checked against its RESOLVED profile -- `claude-code`
# when the step names none. One pair of functions for `swarm_workflow`, the
# launch and `swarm workflow`, so the three read a spec's classes alike.


def spec_without_model_classes(document: Any) -> Any:
    """`document` (a spec) with each step's `model_class` taken off; else as given."""
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
        return document
    return {**document, "steps": steps_without_model_classes(document["steps"])}


def steps_without_model_classes(steps: Any) -> Any:
    """Workflow steps with `model_class` taken off each; anything else as given."""
    if not isinstance(steps, list):
        return steps
    return [
        {k: v for k, v in step.items() if k != "model_class"} if isinstance(step, dict) else step
        for step in steps
    ]


def with_model_classes(built: list[dict[str, Any]], given: Any) -> list[dict[str, Any]]:
    """`built` with each step's `model_class` as the step `given` named it.

    BY STEP ID, and only where one was named: a step naming none is sent
    without the key and runs its profile's default (R3). A refusal names the
    step, and the caller submits nothing.
    """
    named: dict[str, Any] = {}
    for raw in given if isinstance(given, list) else []:
        if isinstance(raw, dict) and raw.get("model_class") is not None:
            named[str(raw.get("step_id") or "").strip()] = raw["model_class"]
    for step in built:
        if step["step_id"] in named:
            step["model_class"] = check_model_class(
                step["runner_profile"], named[step["step_id"]], where=f"step {step['step_id']!r}"
            )
    return built


def check_spec_model_classes(document: Any) -> None:
    """Refuse a spec any of whose steps names a class its profile cannot run."""
    from . import workflows

    if not isinstance(document, dict):
        return
    steps = workflows.read_spec(spec_without_model_classes(document))["steps"]
    with_model_classes(steps, document.get("steps"))


def catalogue() -> list[dict[str, Any]]:
    """The whole catalogue, dispatchable first, then refused.

    Ordered rather than alphabetical because the list is read to make a choice,
    and a disabled profile sorted into the middle of the usable ones is a name
    a reader picks before reaching the reason it is there.
    """
    usable = [public_profile(n) for n in dispatchable()]
    refused = [public_profile(n) for n in names() if n not in set(dispatchable())]
    return usable + refused


#: Where a `swarm_profiles` answer came from.
CATALOGUE_FROM_PLATFORM = "platform (GET /v1/runtimes)"


def bridge_copy_source() -> str:
    """This bridge's own catalogue, named by the package version that carries it.

    THE COPY IS AS OLD AS THE INSTALLED PLUGIN. The bridge imports
    `swarm_common` from the tag the plugin was installed at, so a profile the
    platform has since moved -- claude-code to GKE Autopilot in PR 866, deployed
    by release 37757901721 on 2026-10-08 -- still reads at its old backend
    here until a plugin tag catches up.
    """
    from importlib import metadata

    try:
        version = metadata.version("swarm-mcp")
    except metadata.PackageNotFoundError:
        return "bridge copy (swarm-mcp not installed as a package; version unknown)"
    return f"bridge copy (swarm-mcp {version})"


def _served_entry(name: str, served: dict[str, Any]) -> dict[str, Any] | None:
    """One `/v1/runtimes` entry in `public_profile`'s shape, or None if malformed.

    THE SAME ALLOW-LIST. `/v1/runtimes` serves `image` and `secrets` too; they
    are dropped here for the reason this module's header gives, and a field
    the route adds later is dropped with them.
    """
    resources = served.get("resources")
    if not isinstance(resources, dict) or not isinstance(served.get("available"), bool):
        return None
    backend = served.get("resolved_backend") or served.get("backend")
    # The request by its own name first: `cpu` is its alias, served "for one
    # release" (docs/design/runner-sizes.md §4.5), and a deployment from before
    # size versions serves the alias alone.
    cpu_request = resources.get("cpu_request", resources.get("cpu"))
    out: dict[str, Any] = {
        "name": served.get("name") or name,
        "available": served["available"],
        "backend": backend if isinstance(backend, str) and backend else None,
        "resource_class": served.get("resource_class"),
        # THE PLATFORM'S SIZE, NEVER THE COPY'S. The route serves the current
        # runner size version for a GKE profile and the code defaults for a
        # Cloud Run one (design §4.7), and an admin can resize a class with no
        # deploy, so the bridge's RESOURCE_CLASSES is wrong after the first
        # resize. A field the platform did not serve is None, never filled in
        # from the copy: that would be a different deployment's answer.
        "cpu": cpu_request,
        "cpu_request": cpu_request,
        "cpu_limit": resources.get("cpu_limit"),
        "memory_gib": resources.get("memory_gib"),
        "workspace_gib": resources.get("disk_gib"),
        "units": resources.get("units"),
        # The entry's own, not the platform's current version: a Cloud Run
        # profile reads 0 whatever an admin set.
        "size_version": resources.get("size_version"),
        "timeout_seconds": served.get("timeout_seconds"),
        "needs_provider_credential": served.get("provider") is not None,
        "provider": served.get("provider"),
    }
    if not out["available"]:
        out["disabled_reason"] = served.get("disabled_reason")
    out.update(_served_model_classes(name, served))
    if name in RUNNER_PROFILES:
        # The inputs are the bridge's to check before a dispatch travels
        # (`check_inputs`), so they are the copy's -- the API refuses an
        # undeclared key from every caller regardless.
        local = public_profile(name)
        if local.get("inputs"):
            out["inputs"] = local["inputs"]
        if local["backend"] != out["backend"]:
            # Said, not hidden: the copy this bridge checks names against
            # disagrees with the platform, which means the plugin is behind.
            out["bridge_copy_backend"] = local["backend"]
    return out


def _served_model_classes(name: str, served: dict[str, Any]) -> dict[str, Any]:
    """The platform's `model_classes`, `default_model_class` and `models`, as served.

    The platform's, not the copy's: the route reads the catalogue swarm-api
    was deployed with and the pins it dispatches with. A deployment older than
    the classes serves none of the three, and then the copy's classes are
    given with a note saying the IDs could not be read -- never an ID guessed.
    """
    classes = served.get("model_classes")
    models = served.get("models")
    if isinstance(classes, list) and all(isinstance(c, str) for c in classes):
        default = served.get("default_model_class")
        return {
            "model_classes": list(classes),
            "default_model_class": default if isinstance(default, str) else None,
            "models": {
                str(k): v for k, v in models.items() if isinstance(v, str)
            } if isinstance(models, dict) else {},
        }
    profile = RUNNER_PROFILES.get(name)
    copy = tuple(profile.model_classes) if profile is not None else ()
    out: dict[str, Any] = {
        "model_classes": list(copy),
        "default_model_class": profile.default_model_class if profile is not None else None,
        "models": {},
    }
    if copy:
        out["models_note"] = _models_note(
            copy,
            "This platform's GET /v1/runtimes does not serve model classes yet, so these "
            "are this bridge's copy and no model IDs could be read",
        )
    return out


def served_catalogue(client: Any) -> dict[str, Any]:
    """`swarm_profiles`' answer: the platform's catalogue, else the labelled copy.

    THE PLATFORM FIRST. `GET /v1/runtimes` is swarm-api reading the frozen
    catalogue it was DEPLOYED with, so its backend and `available` flag are
    the ones admission uses. This bridge's copy is the one the plugin was
    installed with, and on 2026-10-08 the two disagreed about claude-code.

    THE COPY WHEN THE PLATFORM CANNOT BE READ, and still an answer: this was
    the one tool that answered with the cluster unreachable, and a session
    guessing at a profile name is exactly the one that needs it then. It is
    labelled `catalogue_source` with the bridge's version and a note saying
    plainly that the platform may differ.
    """
    try:
        data = client.request("GET", "/v1/runtimes")
    except SwarmError as exc:
        data, why = None, f"GET /v1/runtimes could not be read: {exc}"
    else:
        why = "GET /v1/runtimes answered without a `runtimes` object"
    runtimes = data.get("runtimes") if isinstance(data, dict) else None
    if isinstance(runtimes, dict) and runtimes:
        entries, unread = [], []
        for name, served in sorted(runtimes.items()):
            entry = _served_entry(name, served) if isinstance(served, dict) else None
            if entry is None:
                unread.append(name)
            else:
                entries.append(entry)
        if entries:
            # Dispatchable first, as `catalogue()` orders them.
            entries.sort(key=lambda e: (not e["available"], e["name"]))
            out: dict[str, Any] = {"catalogue_source": CATALOGUE_FROM_PLATFORM, "profiles": entries}
            if unread:
                # Named, never dropped silently: a profile missing from this
                # list reads as one the platform does not hold.
                out["profiles_not_read"] = unread
            return out
    return {
        "catalogue_source": bridge_copy_source(),
        "catalogue_note": (
            f"{why}. These are this bridge's own copy of the catalogue, as old "
            "as the installed plugin; the platform may differ -- a backend or an "
            "availability here may not be what the platform uses now. Their sizes "
            "are the code defaults (size version 0); an admin may have resized a "
            "GKE profile's class since"
        ),
        "profiles": catalogue(),
    }


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

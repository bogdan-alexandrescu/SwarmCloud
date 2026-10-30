"""Submission guards.

Two separate jobs live here.

The first is the catalogue boundary (invariant 10): a caller names a
`runner_profile` and nothing else. Image, command, resource class, backend and
provider are all read from the FROZEN catalogue, never from the request. The
request schemas forbid unknown fields, so a caller who tries to smuggle
`image` or `backend` in gets a 422 rather than silently having it ignored.

The second is workflow DAG validation. A workflow whose `depends_on` graph has
a cycle would sit in DEPENDENCY_INCOMPLETE forever, holding no capacity but also
never completing and never erroring -- the worst kind of failure, because
nothing alerts. So the cycle is rejected at submission with the exact cycle
named. An `input_from` the worker would refuse gets the same treatment: caught
at run time, it is caught only after every upstream step has spent its compute.
"""

from __future__ import annotations

import json
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

from swarm_common.profiles import (
    INT64_MAX,
    INT64_MIN,
    MODEL_CLASSES,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    InputRefused,
    ResourceSize,
    RunnerProfile,
    WorkerAction,
    check_inputs,
    default_sizes,
    effective_size,
    resolve_backend,
    step_model_class,
)

from . import laneterritory, refusals
from .errors import Forbidden, ValidationFailed

#: Fields a caller may never set, no matter how they spell it. The request
#: models already forbid extras; this list is the explicit, greppable statement
#: of invariant 10 and is used to produce a precise error message.
FORBIDDEN_CALLER_FIELDS = (
    "image",
    "images",
    "command",
    "args",
    "entrypoint",
    "backend",
    "cpu",
    "memory",
    "memory_gib",
    "disk_gib",
    "resources",
    "resource_spec",
    "service_account",
    "secrets",
    "env",
    "node_selector",
    "spot",
)


def known_providers() -> tuple[str, ...]:
    """Providers the catalogue actually references. Nothing else is registrable.

    DELIBERATELY THE WHOLE CATALOGUE, not `available_runner_profiles()`. Codex
    is disabled and openai is now referenced only by a disabled profile -- but
    a tenant already holds an openai credential, and narrowing this would make
    that secret unmanageable through the API: unregistrable, unrotatable, and
    undeletable by the route that owns it. Disabling a runner should not strand
    a credential someone has to be able to clean up.

    It also keeps re-enabling cheap: the key can be replaced before the profile
    is switched back on, rather than after.

    EXCEPT THE RETIRED #295 APP KEYS (`APP_CREDENTIAL_PROVIDERS`). The
    credential route (`routes/tenants.py`) and the admin limit, drain and
    enable routes (`routes/admin.py` `_check_provider`) both accept exactly
    this set, and a credential registered through the credential route is
    granted to the tenant's ORDINARY worker account
    (`credentials._grant_accessor`), whose token any agent of the tenant can
    mint. `git-merge` and `git-review` were GitHub App keys meant for the
    merge and post-verdict Jobs' own accounts (contract request 33's #364
    amendment). Those accounts are retired (owner decision MS0-Q4,
    2026-10-06): nothing reads either key any more, so neither is registrable,
    listed (`service.providers`) or accepted by the quota broker
    (`quota_broker.main._known_provider`) anywhere. The frozen catalogue still
    names `git-review` on its disabled post-verdict entry until contract
    request 50 is decided, which is why the exclusion is still needed.

    AND THE FORGE TOKEN, `git`, which the `merge` profile names since contract
    request 47 (2026-10-04). It is the tenant's own forge token, registered
    only by `scripts/register-tenant.sh --add-provider git` and stored only
    with `scripts/create-secrets.sh --stdin` (owner rule, 2026-09-25: a forge
    token never passes through anything else). It was never in this set, and
    naming it in the catalogue does not put it there.
    """
    return tuple(
        sorted(
            {p.provider for p in RUNNER_PROFILES.values() if p.provider}
            - APP_CREDENTIAL_PROVIDERS
            - {FORGE_PROVIDER}
        )
    )


#: The tenant's forge token's provider, `swarm-tenant-<tenant>-git` (the
#: worker's `secrets.GIT_PROVIDER`, swarm-api's `forge.GIT_PROVIDER`). The
#: `merge` profile reads it since contract request 47.
FORGE_PROVIDER = "git"

#: The providers of the `worker_action` profiles that are GitHub App keys:
#: `git-review` (contract request 35), and `git-merge` until contract request
#: 47 moved the merge onto the tenant's `-git` token. Derived from the
#: catalogue rather than named, so a third worker action is left out of
#: `known_providers()` the day it is added rather than the day someone
#: remembers this line. The forge token is the one worker-action provider the
#: worker DOES read, so it is not in it.
#:
#: BOTH ARE RETIRED (owner decision MS0-Q4, 2026-10-06): the merge,
#: post-verdict and review accounts that alone were to read them are gone from
#: terraform, so no account reads either key. The set stays, as the refusal:
#: `scripts/register-tenant.sh` reads it to refuse either provider on every
#: path, and a `-git-merge` or `-git-review` secret that exists still holds an
#: App key no worker may be granted. `git-merge` is named
#: (`RETIRED_APP_CREDENTIAL_PROVIDERS`) because the catalogue no longer is.
RETIRED_APP_CREDENTIAL_PROVIDERS: frozenset[str] = frozenset({"git-merge"})
APP_CREDENTIAL_PROVIDERS: frozenset[str] = frozenset(
    p.provider for p in RUNNER_PROFILES.values()
    if p.worker_action is not None and p.provider and p.provider != FORGE_PROVIDER
) | RETIRED_APP_CREDENTIAL_PROVIDERS


#: TypeSafe (docs.typesafe.ai), which serves the Jev judgment models. Its key
#: is stored by `swarm_api.provider_keys` (KEYS-API, docs/provider-keys.md).
TYPESAFE_PROVIDER = "typesafe"

#: Providers whose key is set through `/v1/provider-keys/{provider}`: a
#: person's own key in their personal tenant, or a tenant key an ADMIN sets.
#: DELIBERATELY NOT IN `known_providers()`. That set is what
#: `POST /v1/tenants/me/credentials` accepts from ANY tenant member, and what
#: the admin limit, drain and enable routes accept as a quota pool. A
#: TypeSafe tenant key is an admin's to set and has no pool, so either use
#: would be a second, unguarded way in. It is not a forge or App key
#: either: the tenant's worker account IS the reader it is bound to.
PROVIDER_KEY_PROVIDERS: frozenset[str] = frozenset({TYPESAFE_PROVIDER})

#: The live browser's tool, a `claude-code-browser` input (contract request
#: 77, BSTATUS-API). Its names are the catalogue's `choices`; nothing here
#: restates them.
BROWSER_TOOL_INPUT_KEY = "browser_tool"

#: A review step's runtime, chosen from its diff (#2322, BGD 2026-10-25: "if a
#: review only does code review and a UI review or UI QA is not needed, it
#: should not dispatch a review task with a browser runtime"). A WORKFLOW STEP
#: may carry `metadata.runtime = {"policy": "browser_if_ui", "browser":
#: "claude-code-browser", "code": "claude-code"}`: the scheduler runs it on
#: the browser when any path its parents changed is UI, test files excluded,
#: and on `claude-code` otherwise, deciding as it readies the step
#: (`scheduler.runtime`). The two names are the ONLY pair: they are catalogue
#: profile names, nothing else, so the policy names no image, command,
#: resource or backend (invariant 10). Not a typed field: `swarm_common`'s
#: step model is frozen, and step metadata already travels to the task.
RUNTIME_METADATA_KEY = "runtime"
RUNTIME_POLICY_BROWSER_IF_UI = "browser_if_ui"
RUNTIME_POLICY_PROFILES: Mapping[str, str] = MappingProxyType(
    {"browser": "claude-code-browser", "code": "claude-code"}
)
#: The task fields a profile decides. swarm-api fills them for the `code`
#: profile into `metadata.runtime.alternate` at submission and signs that
#: spec too (`specsigning.sign_task_specs`); the scheduler swaps them, with
#: the signature, only to a spec swarm-api signed. Restated by
#: `scheduler.runtime.RUNTIME_SWAP_FIELDS`, held equal by
#: tests/unit/control_plane/test_runtime_by_diff.py.
RUNTIME_SWAP_FIELDS = (
    "runner_profile", "resource_class", "timeout_seconds", "provider", "model", "model_class",
)


class RuntimePolicyInvalid(ValidationFailed):
    """422 `invalid_runtime`: a `metadata.runtime` this platform does not run."""

    code = "invalid_runtime"


def resolve_runtime_policy(
    metadata: Mapping[str, Any] | None, *, runner_profile: str, step_id: str | None,
    workflow_step: bool,
) -> dict[str, str] | None:
    """A step's `browser_if_ui` block, checked and normalised; None when it has none.

    Refused: on anything but a workflow step (a task has no parent whose
    diff could choose for it); any shape but the three keys; any policy but
    `browser_if_ui`; any pair but `RUNTIME_POLICY_PROFILES`; and a step not
    submitted on the pair's browser profile -- the profile a step runs when
    nothing resolves it, so a failed resolution never loses the browser.
    """
    if not isinstance(metadata, Mapping) or RUNTIME_METADATA_KEY not in metadata:
        return None
    where = {"step_id": step_id} if step_id else {}
    shape = {"policy": RUNTIME_POLICY_BROWSER_IF_UI, **RUNTIME_POLICY_PROFILES}
    if not workflow_step:
        raise RuntimePolicyInvalid(
            "metadata.runtime is a workflow step's: a task has no upstream step whose "
            "change could choose its runtime",
            detail={**where, "field": f"metadata.{RUNTIME_METADATA_KEY}"},
        )
    raw = metadata[RUNTIME_METADATA_KEY]
    if not isinstance(raw, Mapping) or set(raw) != set(shape):
        raise RuntimePolicyInvalid(
            f"metadata.runtime must be exactly {shape}: the policy and both profiles "
            "it may resolve to",
            detail={**where, "field": f"metadata.{RUNTIME_METADATA_KEY}", "accepted": shape},
        )
    if raw.get("policy") != RUNTIME_POLICY_BROWSER_IF_UI:
        raise RuntimePolicyInvalid(
            f"metadata.runtime.policy {raw.get('policy')!r} is not one this platform runs; "
            f"the one policy is {RUNTIME_POLICY_BROWSER_IF_UI!r}",
            detail={**where, "field": f"metadata.{RUNTIME_METADATA_KEY}.policy",
                    "accepted": [RUNTIME_POLICY_BROWSER_IF_UI]},
        )
    pair = {key: raw.get(key) for key in RUNTIME_POLICY_PROFILES}
    if pair != dict(RUNTIME_POLICY_PROFILES):
        raise RuntimePolicyInvalid(
            f"metadata.runtime names {pair}; a {RUNTIME_POLICY_BROWSER_IF_UI} step resolves "
            f"only between {dict(RUNTIME_POLICY_PROFILES)}",
            detail={**where, "field": f"metadata.{RUNTIME_METADATA_KEY}",
                    "accepted": dict(RUNTIME_POLICY_PROFILES)},
        )
    if runner_profile != RUNTIME_POLICY_PROFILES["browser"]:
        raise RuntimePolicyInvalid(
            f"a {RUNTIME_POLICY_BROWSER_IF_UI} step is submitted on "
            f"{RUNTIME_POLICY_PROFILES['browser']!r}, the profile it runs when nothing "
            f"resolves it; this one names {runner_profile!r}",
            detail={**where, "field": "runner_profile",
                    "accepted": [RUNTIME_POLICY_PROFILES["browser"]]},
        )
    return dict(shape)


#: A review step's gate (#2524, BGD 2026-10-27: "Risk paths + over 300 lines;
#: reuse renders"). A WORKFLOW STEP may carry `metadata.review_gate =
#: {"min_lines": 300, "risk_paths": [...]}`: the scheduler holds it PARKED
#: when its parents end, swarm-api reads what they changed (`reviewgate`) and
#: the review runs only when that touches a risk path or changes more than
#: `min_lines` lines; otherwise swarm-api ends the step SUCCEEDED without an
#: agent and its verdict.json reads MERGE, with the reason. Either key may be
#: left out (`min_lines` 300, `risk_paths` `reviewgate.DEFAULT_RISK_PATHS`). Not a typed
#: field, for the reason `metadata.runtime` is not: `swarm_common` is frozen.
REVIEW_GATE_METADATA_KEY = "review_gate"
REVIEW_GATE_KEYS = ("min_lines", "risk_paths", "requirements_check")
#: EVERY REVIEW STEP IS GATED BY DEFAULT (BGD 2026-10-28, page M8: "every
#: review step"). A step another step's `when` reads a verdict from is a
#: review; one whose metadata names no `review_gate` is stored with this
#: block (`default_review_gate_steps`, stamped by `submit_workflow`), and
#: swarm-api resolves it when it decides: the workspace's
#: `workspace_settings/{tenant}.review_gate` when one is set, else the
#: platform's (`min_lines` 300, `reviewgate.DEFAULT_RISK_PATHS`). Why a
#: default and not opt-in: the #2524 week reviewed every change however
#: small, and a review a step has to ask to have gated is one nobody gates.
#: A step that names its own block keeps it; `"review_gate": false` switches
#: the gate off for that step, and its review always runs. Never accepted
#: from a caller (it is not in `REVIEW_GATE_KEYS`).
REVIEW_GATE_DEFAULT_BLOCK: Mapping[str, Any] = MappingProxyType({"default": True})
#: An issue run's requirements check (#2524, BGD 2026-10-28 ~18:25Z): a step
#: carrying `review_gate.requirements_check` is held like a gated review and
#: runs ONLY when the review it stands beside is skipped
#: (`reviewgate.is_requirements_check`). It follows the default policy, as
#: that review does, unless it names its own.
REVIEW_GATE_REQUIREMENTS_CHECK = "requirements_check"
#: BGD's value, read by `reviewgate.DEFAULT_MIN_LINES`.
REVIEW_GATE_DEFAULT_MIN_LINES = 300
#: Bounds on what a caller may write: a gate is a short list of globs, and
#: the whole block sits in a task document read on every promotion.
REVIEW_GATE_MAX_MIN_LINES = 1_000_000
REVIEW_GATE_MAX_RISK_PATHS = 100
REVIEW_GATE_MAX_PATH_CHARS = 200


class ReviewGateInvalid(ValidationFailed):
    """422 `invalid_review_gate`: a `metadata.review_gate` this platform does not run."""

    code = "invalid_review_gate"


def resolve_review_gate(
    metadata: Mapping[str, Any] | None, *, step_id: str | None, workflow_step: bool,
) -> dict[str, Any] | None:
    """A step's `review_gate` block, checked and normalised; None when it has none.

    A switch (`refusals.SWITCHES["invalid_review_gate"]`), on by BGD's
    decision of 2026-10-27. Turned off, a malformed block is not refused and
    is not stored either (None): the step's review then always runs, as it
    did before gates existed.
    """
    try:
        return _checked_review_gate(metadata, step_id=step_id, workflow_step=workflow_step)
    except ReviewGateInvalid as error:
        refusals.refuse(error)
        return None


def _checked_review_gate(
    metadata: Mapping[str, Any] | None, *, step_id: str | None, workflow_step: bool,
) -> dict[str, Any] | None:
    """`resolve_review_gate`, raising `ReviewGateInvalid` for every refusal.

    Refused: on anything but a workflow step (a task has no parent whose diff
    could decide for it); any key but `REVIEW_GATE_KEYS`; a `min_lines` that
    is not a whole number in 0..`REVIEW_GATE_MAX_MIN_LINES` (a bool is not);
    and a `risk_paths` that is not a list of 1..`REVIEW_GATE_MAX_RISK_PATHS`
    non-empty strings of at most `REVIEW_GATE_MAX_PATH_CHARS`, with no control
    character and none absolute. A left-out `risk_paths` is left out, so the
    scheduler's default list applies.
    """
    if not isinstance(metadata, Mapping) or REVIEW_GATE_METADATA_KEY not in metadata:
        return None
    where = {"step_id": step_id} if step_id else {}
    field = f"metadata.{REVIEW_GATE_METADATA_KEY}"
    if not workflow_step:
        raise ReviewGateInvalid(
            "metadata.review_gate is a workflow step's: a task has no upstream step whose "
            "change could decide whether it runs",
            detail={**where, "field": field},
        )
    raw = metadata[REVIEW_GATE_METADATA_KEY]
    if raw is False:
        # The gate switched off for this step: nothing is stored, and the
        # default is not stamped either (`default_review_gate_steps`).
        return None
    if not isinstance(raw, Mapping) or not set(raw) <= set(REVIEW_GATE_KEYS):
        unknown = sorted(set(raw) - set(REVIEW_GATE_KEYS)) if isinstance(raw, Mapping) else None
        raise ReviewGateInvalid(
            f"metadata.review_gate must be an object with only {list(REVIEW_GATE_KEYS)}",
            detail={**where, "field": field, "accepted": list(REVIEW_GATE_KEYS),
                    **({"unknown": unknown} if unknown else {})},
        )
    min_lines = raw.get("min_lines", REVIEW_GATE_DEFAULT_MIN_LINES)
    if (isinstance(min_lines, bool) or not isinstance(min_lines, int)
            or not 0 <= min_lines <= REVIEW_GATE_MAX_MIN_LINES):
        raise ReviewGateInvalid(
            f"metadata.review_gate.min_lines must be a whole number from 0 to "
            f"{REVIEW_GATE_MAX_MIN_LINES}",
            detail={**where, "field": f"{field}.min_lines"},
        )
    check = raw.get(REVIEW_GATE_REQUIREMENTS_CHECK, False)
    if not isinstance(check, bool):
        raise ReviewGateInvalid(
            "metadata.review_gate.requirements_check must be true or false",
            detail={**where, "field": f"{field}.{REVIEW_GATE_REQUIREMENTS_CHECK}"},
        )
    if check and not set(raw) - {REVIEW_GATE_REQUIREMENTS_CHECK}:
        # A check naming no policy follows the default, as its review does.
        return {**REVIEW_GATE_DEFAULT_BLOCK, REVIEW_GATE_REQUIREMENTS_CHECK: True}
    out: dict[str, Any] = {"min_lines": min_lines}
    if check:
        out[REVIEW_GATE_REQUIREMENTS_CHECK] = True
    if "risk_paths" in raw:
        paths = raw["risk_paths"]
        if (not isinstance(paths, list) or not paths
                or len(paths) > REVIEW_GATE_MAX_RISK_PATHS
                or not all(isinstance(p, str) and p.strip() and len(p) <= REVIEW_GATE_MAX_PATH_CHARS
                           and not p.startswith("/") and p.isprintable() for p in paths)):
            raise ReviewGateInvalid(
                f"metadata.review_gate.risk_paths must be a list of 1 to "
                f"{REVIEW_GATE_MAX_RISK_PATHS} repository-relative globs of at most "
                f"{REVIEW_GATE_MAX_PATH_CHARS} characters",
                detail={**where, "field": f"{field}.risk_paths"},
            )
        out["risk_paths"] = [p.strip() for p in paths]
    return out


def default_review_gate_steps(
    steps: Sequence[Any], workflow_metadata: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """The steps stored with `REVIEW_GATE_DEFAULT_BLOCK`: every review step that names no gate.

    A review is a step another step's `when` reads a verdict from (#264):
    the fix, the gated integrator, the re-review. Left out: a step whose own
    metadata, or the workflow's, names `review_gate` at all -- its own block
    wins, and `false` is the gate switched off. `steps` are
    `WorkflowStepCreate`s (`step_id`, `when`, `metadata`).
    """
    targets = {
        step.when.step for step in steps if getattr(step, "when", None) is not None
    }
    shared = REVIEW_GATE_METADATA_KEY in (workflow_metadata or {})
    return frozenset(
        step.step_id for step in steps
        if step.step_id in targets and not shared
        and REVIEW_GATE_METADATA_KEY not in (getattr(step, "metadata", None) or {})
    )


def requirements_check_steps(steps: Sequence[Any]) -> frozenset[str]:
    """The steps that are an issue run's requirements check (`REVIEW_GATE_REQUIREMENTS_CHECK`).

    Not merged by an integrator: the check is read-only, its deliverable is
    `requirements.json`, and when the review runs it runs no agent and
    pushes no branch at all.
    """
    return frozenset(
        step.step_id for step in steps
        if _mapping_or_empty(_mapping_or_empty(getattr(step, "metadata", None))
                             .get(REVIEW_GATE_METADATA_KEY)).get(REVIEW_GATE_REQUIREMENTS_CHECK)
        is True
    )


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def validate_runner_profile(name: str) -> RunnerProfile:
    profile = RUNNER_PROFILES.get(name)
    if profile is None:
        raise ValidationFailed(
            f"unknown runner_profile {name!r}",
            detail={"known_runner_profiles": sorted(available_runner_profiles())},
        )
    # KNOWN BUT REFUSED IS NOT THE SAME AS UNKNOWN, and collapsing them sends a
    # caller hunting for a typo that is not there. The profile exists, it is
    # spelled correctly, and the platform will not run it -- so say that, and
    # say why, because the reason is the only part they can act on.
    if not profile.available:
        raise ValidationFailed(
            f"runner_profile {name!r} is disabled: {profile.disabled_reason}",
            detail={
                "runner_profile": name,
                "disabled": True,
                "reason": profile.disabled_reason,
                "known_runner_profiles": sorted(available_runner_profiles()),
            },
        )
    return profile


def available_runner_profiles() -> dict[str, RunnerProfile]:
    """The profiles a caller may actually dispatch.

    Every list OFFERED to a caller comes from here rather than from
    RUNNER_PROFILES, so a disabled profile cannot be advertised on one screen
    and refused on submit from another.
    """
    return {n: p for n, p in RUNNER_PROFILES.items() if p.available}


def _class_size(name: str, sizes: Mapping[str, ResourceSize] | None, backend: Any) -> ResourceSize:
    """The size `name` runs at: `effective_size` for the frozen classes, and
    the catalogue entry itself for one `RESOURCE_CLASSES` holds and the size
    defaults do not (the guard tests add one to prove every dimension is
    compared)."""
    if name in default_sizes():
        return effective_size(name, sizes, backend)
    rc = RESOURCE_CLASSES[name]
    return ResourceSize(cpu_request=rc.cpu, cpu_limit=rc.cpu_limit_cores,
                        memory_gib=rc.memory_gib, disk_gib=rc.disk_gib)


def validate_resource_class_override(
    profile: RunnerProfile,
    requested: str | None,
    *,
    sizes: Mapping[str, ResourceSize] | None = None,
) -> str:
    """A step may not pick a bigger box than its profile was built for.

    The only override permitted is to a resource class that exists AND whose
    sizing is no larger than the profile's own class in every dimension. This is
    still not a resource spec from the caller -- it is a choice among named,
    admin-defined classes -- but it cannot be used to grow a task.

    `sizes` is the current runner size version (`runnersizes.current_sizes`,
    design §4.8): once an admin resizes a class, "larger" is judged at the
    sizes the step would RUN at, which is what admission charges and the pod
    is rendered from. Each side is the size its backend runs (`effective_size`:
    the code default on Cloud Run). None compares the code defaults.
    """
    if requested is None:
        return profile.resource_class
    if requested not in RESOURCE_CLASSES:
        raise ValidationFailed(
            f"unknown resource_class {requested!r}",
            detail={"known_resource_classes": sorted(RESOURCE_CLASSES)},
        )
    backend = resolve_backend(profile)
    target = _class_size(requested, sizes, backend)
    base = _class_size(profile.resource_class, sizes, backend)
    # Every dimension, which is what the guarantee above says. `disk_gib` was
    # missing: it is not exploitable with the current three-class catalogue,
    # where disk rises with cpu and memory, but the check is the stated promise
    # and a fourth class with a big disk and a small CPU would walk straight
    # through the gap. `cpu` is the REQUEST, what admission charges; the limit
    # rises with it (at most `CPU_BURST_MAX_RATIO` x), and is compared too so
    # a step can never burst further than its profile could.
    larger = [
        dimension
        for dimension, requested_value, base_value in (
            ("cpu", target.cpu_request, base.cpu_request),
            ("cpu_limit", target.cpu_limit, base.cpu_limit),
            ("memory_gib", target.memory_gib, base.memory_gib),
            ("disk_gib", target.disk_gib, base.disk_gib),
            ("units", target.units, base.units),
        )
        if requested_value > base_value
    ]
    if larger:
        raise ValidationFailed(
            f"resource_class {requested!r} is larger than runner_profile "
            f"{profile.name!r} permits ({profile.resource_class!r})",
            detail={"larger_in": larger},
        )
    return requested


class UnknownModelClass(ValidationFailed):
    """422 `unknown_model_class`: the name is not one of `MODEL_CLASSES`.

    A model ID sent as a class (`"claude-opus-5-5"`) lands here too: a class
    is a name from the frozen vocabulary, never an ID (invariant 10)."""

    code = "unknown_model_class"


class ModelClassNotAccepted(ValidationFailed):
    """422 `model_class_not_accepted`: a class the profile does not run."""

    code = "model_class_not_accepted"


class ModelClassNotYetAvailable(ValidationFailed):
    """422 `model_class_not_yet_available`: this deployment pins no model for
    the class (WORKER_CLASS_MODELS is unset), so it cannot be named yet."""

    code = "model_class_not_yet_available"


class ModelIsChosenByModelClass(ValidationFailed):
    """422 `model_is_chosen_by_model_class`: `model` is not the pin of the
    step's class. The model is the class's, never the caller's (invariant 10,
    owner's Q4 of #1435); the message names the `model_class` to send."""

    code = "model_is_chosen_by_model_class"


def validate_model_class(
    profile: RunnerProfile, requested: str | None, *, step_id: str | None = None,
) -> str | None:
    """The class a step on `profile` runs, by the frozen
    `profiles.step_model_class` (docs/design/model-per-step.md §4.4): the
    requested one if the profile accepts it, its default when none is named,
    None for a profile that runs no Claude model. Refused with a stable code
    otherwise, and the refusal names what may be sent."""
    try:
        return step_model_class(profile, requested)
    except ValueError:
        pass
    where = {"step_id": step_id} if step_id is not None else {}
    if requested not in MODEL_CLASSES:
        raise UnknownModelClass(
            f"unknown model_class {requested!r}: a model class is one of "
            f"{', '.join(MODEL_CLASSES)}, never a model ID",
            detail={"model_class": requested, "known_model_classes": list(MODEL_CLASSES),
                    **where},
        )
    if not profile.model_classes:
        message = (f"runner_profile {profile.name!r} runs no Claude model, so a step on "
                   "it names no model_class")
    else:
        message = (f"runner_profile {profile.name!r} does not accept model_class "
                   f"{requested!r}; it accepts {', '.join(profile.model_classes)}")
    raise ModelClassNotAccepted(
        message,
        detail={"runner_profile": profile.name, "model_class": requested,
                "accepted_model_classes": list(profile.model_classes), **where},
    )


def stored_model_class(profile: RunnerProfile, resolved: str | None) -> str | None:
    """What `Task.model_class` stores: the class only when it differs from the
    profile's default (§4.3). Naming the default stores nothing, so the task
    keeps the bytes, and the signing format, it had before classes existed;
    only a step that asks for another class is signed at the format that
    covers `model_class`."""
    if resolved is None or resolved == profile.default_model_class:
        return None
    return resolved


def check_model_class_available(
    profile: RunnerProfile, resolved: str | None, class_models: Mapping[str, str], *,
    step_id: str | None = None,
) -> None:
    """A class other than the profile's default only once this deployment pins
    it (WORKER_CLASS_MODELS). No flag gates the classes (owner decision R4,
    2026-10-17); what is refused here is a class with no pin to record or
    serve, which is the local emulator loop's case and a deployment's whose
    terraform predates `runner_models` by class. The scheduler would refuse
    such a step at dispatch, after admission took its lease; this refuses it
    before anything is stored."""
    if stored_model_class(profile, resolved) is None or resolved in class_models:
        return
    raise ModelClassNotYetAvailable(
        f"model_class {resolved!r} is not available on this deployment yet: no model is "
        f"pinned for it; a step on {profile.name!r} that names none runs "
        f"{profile.default_model_class!r}",
        detail={"runner_profile": profile.name, "model_class": resolved,
                "default_model_class": profile.default_model_class,
                **({"step_id": step_id} if step_id is not None else {})},
    )


def resolve_task_model(
    profile: RunnerProfile, resolved: str | None, requested_model: str | None,
    class_models: Mapping[str, str], *, step_id: str | None = None,
) -> str | None:
    """`Task.model`: the pin of the step's class, at submission (owner's Q4).

    A `model` equal to that pin is accepted; any other value is refused
    `model_is_chosen_by_model_class` once its refusal switch is on, and the
    refusal names the `model_class` that would run it, or lists the profile's
    classes and their models. `Task.model` is a record, not a
    selector: the scheduler resolves the class's pin again at dispatch and
    `Attempt.model` records what ran (§4.5, R6).

    A profile with no classes, or a deployment with no class map, has no pin to
    fill or compare against, so `model` is recorded as sent, as before: it
    selects nothing on any path (the scheduler never reads it into `MODEL`).
    """
    if resolved is None or resolved not in class_models:
        return requested_model
    pin = class_models[resolved]
    if requested_model is None or requested_model == pin:
        return pin
    where = {"step_id": step_id} if step_id is not None else {}
    models = {name: class_models[name] for name in profile.model_classes if name in class_models}
    named = next((name for name, model_id in models.items() if model_id == requested_model), None)
    if named is not None:
        error = ModelIsChosenByModelClass(
            f"model is chosen by model_class: to run {requested_model}, send "
            f'model_class: "{named}"',
            detail={"model": requested_model, "model_class": named,
                    "runner_profile": profile.name, **where},
        )
    else:
        error = ModelIsChosenByModelClass(
            f"model is chosen by model_class, and {requested_model!r} is the pin of no class "
            f"{profile.name!r} runs: send model_class, one of "
            + ", ".join(f"{name} ({model_id})" for name, model_id in models.items()),
            detail={"model": requested_model, "runner_profile": profile.name,
                    "models": models, **where},
        )
    # A switch, report-only until turned on (`refusals.SWITCHES`): it can
    # refuse a caller that sends `model` today. Past it, the caller's string
    # is recorded as sent, as before this check.
    refusals.refuse(error)
    return requested_model


def firestore_size(payload: Any) -> int:
    """The bytes `payload` costs as a Firestore value, by Firestore's own rule.

    A string is its UTF-8 bytes + 1, an integer or a float 8, a boolean or null
    1, an array the sum of its values, a map the sum of each key (sized as a
    string) and its value. A value of any other type is sized as its `str()`,
    which is what `json.dumps(default=str)` made of it here before.

    Walked with a stack, not recursion, for the reason `validate_storable` is.
    """
    total = 0
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if value is None or isinstance(value, bool):
            total += 1
        elif isinstance(value, (int, float)):
            total += 8
        elif isinstance(value, str):
            total += len(value.encode("utf-8")) + 1
        elif isinstance(value, Mapping):
            for key, item in value.items():
                total += len(str(key).encode("utf-8")) + 1
                stack.append(item)
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
        else:
            total += len(str(value).encode("utf-8")) + 1
    return total


def validate_input_size(payload: Any, max_bytes: int, *, label: str = "input") -> int:
    """Refuse `payload` if it costs more than `max_bytes` AS FIRESTORE STORES IT.

    WHY NOT THE JSON TEXT (#232 review, epic #227). The limit is there to keep
    a task document under Firestore's 1 MiB, and the worker's manifest budget
    (`agent_worker.artifact_manifest`) counts the input at `max_input_bytes`
    Firestore bytes. This measured `json.dumps` bytes, and Firestore stores an
    integer in 8 bytes whatever its digits: `0, ` is 3 bytes of JSON, so a list
    of small integers measured at 256 KiB took about 680 KiB once stored. Text
    costs about the same either way, so an ordinary prompt measures as before.

    Still serialised first, so a value JSON cannot carry (a reference cycle) is
    refused as such rather than walked.

    A STRING HOLDING A LONE SURROGATE HAS NO FIRESTORE SIZE EITHER. `json.dumps`
    above does not catch it -- its default `ensure_ascii=True` escapes a lone
    surrogate like any other codepoint -- so `firestore_size`'s own
    `str.encode("utf-8")` raised `UnicodeEncodeError` UNCAUGHT here: a 500 at
    submission instead of the 422 `invalid_input` every other non-canonical
    value gets at signing (`specsigning.sign_task_specs`), for a case
    `specsigning` never got the chance to see because this call always runs
    first (contract request 34, PR #353 review). Caught here, at the same
    layer as the JSON-serialisability check above, so the message never
    echoes the string itself -- only that one held no canonical form.
    """
    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{label} is not JSON-serialisable: {exc}") from None
    try:
        size = firestore_size(payload)
    except UnicodeEncodeError:
        raise InvalidInput(
            f"{label} holds a string with no canonical form (a lone surrogate) and "
            "cannot be signed or stored"
        ) from None
    if size > max_bytes:
        raise ValidationFailed(
            f"{label} is {size} bytes, over the {max_bytes} byte limit",
            detail={"bytes": size, "max_bytes": max_bytes},
        )
    return size


#: The label a task's runner `input` is walked under, and so the one whose
#: non-finite refusal is `InvalidInput`.
PROMPT_INPUT_LABEL = "input"


def _non_finite_token(value: float) -> str:
    """The token the caller sent: Python's JSON reader is how the value got here."""
    if math.isnan(value):
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def _non_finite_refusal(
    path: str,
    value: float,
    step_id: str | None,
    *,
    key: Any = None,
    error: type[ValidationFailed] = ValidationFailed,
) -> ValidationFailed:
    token = _non_finite_token(value)
    message = (
        f"{path} is {token}, which is not a finite number. JSON has no such value "
        "(Python's reader accepts the token, the standard does not), and every "
        "reader of a stored task that counts with it would fail; send a finite "
        "number, or a string if the text is what you mean"
    )
    detail: dict[str, Any] = {"path": path, "value": token}
    if key is not None:
        detail["key"] = key
    if step_id is not None:
        detail["step_id"] = step_id
        message = f"step {step_id!r}: {message}"
    return error(message, detail=detail)


def reject_non_finite(
    payload: Any,
    *,
    label: str = "input",
    step_id: str | None = None,
    error: type[ValidationFailed] | None = None,
) -> None:
    """Refuse NaN, Infinity or -Infinity anywhere in `payload`, naming the path (#294, S0).

    THE DEFECT. `json.loads` -- what reads every request body here -- accepts
    the bare tokens `NaN`, `Infinity` and `-Infinity`, and a task's `input` and
    `metadata` are `dict[str, Any]`, so nothing refused them and Firestore
    stored them. Any control-plane reader that later counts with such a value
    raises (`int(float("inf"))` is an `OverflowError`, `int(float("nan"))` a
    `ValueError`), and a reader inside a platform-wide loop stops that loop
    for every tenant: one poisoned task, every tenant's pass. The readers are
    made total too (`reconciler.store.ControlStore.snapshot`); this is the
    door, so a new one does not have to be.

    Run on a task's input and metadata and on every workflow step's input and
    metadata, BEFORE the runner-input declaration is checked, so the refusal
    names the path and the value rather than "undeclared key" or a bound a NaN
    compares false against. `validate_storable` checks the same, so a caller
    of that alone is covered as well.

    THE CODE A CALLER BRANCHES ON IS KEPT. A runner `input` was already
    refused for a NaN at a DECLARED key, as 422 `invalid_input` naming the key
    (test_runner_inputs_by_declaration.py), so a refusal in `input` is still
    `InvalidInput` and its detail still carries `key` -- the top-level key the
    value sits under -- beside the new `path`. Metadata answers
    `validation_failed`, as its other storability refusals do.

    Walked with a stack, not recursion, for the reason `validate_storable` is.
    """
    if error is None:
        error = InvalidInput if label == PROMPT_INPUT_LABEL else ValidationFailed
    # (path, the top-level key it sits under, value)
    stack: list[tuple[str, Any, Any]] = [(label, None, payload)]
    while stack:
        path, top, value = stack.pop()
        if isinstance(value, float):
            if not math.isfinite(value):
                raise _non_finite_refusal(path, value, step_id, key=top, error=error)
        elif isinstance(value, Mapping):
            stack.extend(
                (f"{path}.{key}", key if top is None else top, item)
                for key, item in value.items()
            )
        elif isinstance(value, (list, tuple)):
            stack.extend((f"{path}[{index}]", top, item) for index, item in enumerate(value))


def validate_storable(payload: Any, *, label: str = "input", step_id: str | None = None) -> None:
    """Refuse an integer Firestore cannot store, or a non-finite float, anywhere in `payload`.

    Python reads a JSON integer of any length, and Firestore stores a signed
    64-bit one, so `{"n": 10**30}` passed every check this module made and
    raised when the task was written: a 500 at the store, where the caller
    learns nothing, instead of a 422 here that names the path (the review of
    #213). A declared runner input cannot get this far out of range --
    `RunnerInput` requires both bounds, inside the same range, and since
    contract request 32 (#218) every profile declares -- but a task's metadata
    is bounded by size and its reserved keys alone, and this check runs on the
    input too, so it holds whatever order the two checks run in. This is the
    store's own limit, not a declaration, so it applies to every profile.

    Walked with a stack, not recursion: a payload nested a thousand deep is
    small enough to pass the size limit and deep enough to overflow Python's.
    """
    stack: list[tuple[str, Any]] = [(label, payload)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, bool):
            continue
        if isinstance(value, float):
            # Firestore stores NaN and Infinity; every reader that counts with
            # them does not survive them (#294). See `reject_non_finite`.
            if not math.isfinite(value):
                raise _non_finite_refusal(path, value, step_id)
        elif isinstance(value, int):
            if not INT64_MIN <= value <= INT64_MAX:
                message = (
                    f"{path} is a {len(str(abs(value)))}-digit integer, outside the "
                    f"signed 64-bit range Firestore stores ({INT64_MIN}..{INT64_MAX})"
                )
                detail: dict[str, Any] = {"path": path, "range": "signed 64-bit"}
                if step_id is not None:
                    detail["step_id"] = step_id
                    message = f"step {step_id!r}: {message}"
                raise ValidationFailed(message, detail=detail)
        elif isinstance(value, Mapping):
            stack.extend((f"{path}.{key}", item) for key, item in value.items())
        elif isinstance(value, (list, tuple)):
            stack.extend((f"{path}[{index}]", item) for index, item in enumerate(value))


class InvalidInput(ValidationFailed):
    """A task's or a step's `input` sets a key its runner profile does not
    declare, or a declared key outside its bounds (contract request 25).

    Its own code, like `invalid_dispatch` and `invalid_dag`, so a caller can
    branch on it: this refusal is about what was sent to the runner, and its
    detail names the key (`key`, and every refused key in `keys`), the bound
    it broke (`expected`, for a declared key), what the profile does declare
    (`declared`) and, on a workflow, the step (`step_id`).
    """

    code = "invalid_input"


#: The one key of `input` every profile takes: the instructions. Everything
#: else is what `RunnerProfile.inputs` declares.
PROMPT_INPUT_KEY = "prompt"


#: The lane's territory (TERRITORY-WARN, part of #1468): every profile takes
#: it, as it takes `prompt`, and no runner reads it -- the worker compares the
#: published diff with it (`laneterritory`). Its shape is checked here.
TERRITORY_INPUT_KEY = laneterritory.TERRITORY_INPUT_KEY

#: The keys of `input` that are the platform's rather than a runner's, and so
#: are never asked of the catalogue's declaration.
PLATFORM_INPUT_KEYS = frozenset({PROMPT_INPUT_KEY, TERRITORY_INPUT_KEY})


#: The input that names an issue in the task's own repository (contract
#: request 28, #265). Its one rule beyond its declared bounds is here.
ISSUE_INPUT_KEY = "issue"


def validate_runner_input(
    profile: RunnerProfile,
    payload: Mapping[str, Any],
    *,
    step_id: str | None = None,
    repository_url: str | None,
) -> None:
    """Refuse an `input` key the profile does not declare, or a value out of its bounds.

    THE CATALOGUE IS THE RULE, and this keeps no copy of it: the owner accepted
    contract request 25 on #142 (2026-09-25) as "`RunnerProfile.inputs` goes in
    the frozen catalogue and the API enforces it for every caller, not only the
    bridge". Until then the API bounded an input's SIZE and nothing else, so a
    script or the Submit form could send the mock `quota_exhausted` -- which
    parked on every attempt, and a park does not spend one -- or send
    `claude-code` a `model`, which its runner passed as `--model` (until #226,
    when the model became the Job's `MODEL` and the runner stopped reading it).

    Every caller reaches this: `_build_task` calls it for a task and for each
    task of a batch, and `submit_workflow` for each step before any task is
    built. Values are checked, never rewritten: the input is stored as sent.

    EVERY PROFILE IS ASKED, and the answer is the shared rule's. Every profile
    DECLARES now. The mock declared first (#142); `claude-code` and `codex`
    declare `issue` (contract request 28, #265); `browser` and `generic`, which
    were `inputs=None` and bounded by size alone until contract request 32
    (#218, accepted by the owner on 2026-09-29), declare every key their
    runners read, and `generic`'s `command` is required -- a task without it is
    refused here, naming it, rather than admitted and failed in the pod. There
    is no branch here for any profile; the review of #213 found this function
    once deciding a case for itself ahead of the shared rule, which gave two
    answers to one question. The size is `validate_input_size`'s, which every
    caller runs first.

    `issue` NEEDS A REPOSITORY (#265). It names an issue in the task's own
    `repository_url` -- a workflow's, for a step -- and the worker fetches it
    from there, so without one it names nothing and would fail the attempt
    after admission. Required as a keyword, so no caller can forget to pass it.
    """
    rest = {key: value for key, value in payload.items() if key not in PLATFORM_INPUT_KEYS}
    try:
        if TERRITORY_INPUT_KEY in payload:
            try:
                laneterritory.parse(payload[TERRITORY_INPUT_KEY])
            except laneterritory.TerritoryError as malformed:
                raise InputRefused(
                    str(malformed),
                    key=TERRITORY_INPUT_KEY,
                    expected=(
                        f"a list of 1..{laneterritory.MAX_ENTRIES} repo-relative paths or "
                        "globs (`*`, `?`, `[...]`, `**`)"
                    ),
                ) from None
        check_inputs(profile, rest)
        if (
            ISSUE_INPUT_KEY in rest
            and ISSUE_INPUT_KEY in (profile.inputs or {})
            and not (repository_url or "").strip()
        ):
            raise InputRefused(
                f"input {ISSUE_INPUT_KEY!r} names an issue in the task's repository, "
                "and this submission has no repository_url",
                key=ISSUE_INPUT_KEY,
                expected="an issue in the task's repository_url",
            )
    except InputRefused as refused:
        declared = profile.inputs
        detail: dict[str, Any] = {
            "runner_profile": profile.name,
            "key": refused.key,
            "keys": list(refused.keys),
            "declared": {key: spec.describe() for key, spec in sorted(declared.items())},
        }
        if refused.expected is not None:
            detail["expected"] = refused.expected
        message = str(refused)
        if step_id is not None:
            detail["step_id"] = step_id
            message = f"step {step_id!r}: {message}"
        raise InvalidInput(message, detail=detail) from None


def validate_batch_size(count: int, max_batch_size: int) -> None:
    if count <= 0:
        raise ValidationFailed("batch must contain at least one task")
    if count > max_batch_size:
        raise ValidationFailed(
            f"batch of {count} exceeds max_batch_size {max_batch_size}",
            detail={"count": count, "max_batch_size": max_batch_size},
        )


# --------------------------------------------------------------------------
# Dispatch options: how the work gets merged, and what carries it between steps
# --------------------------------------------------------------------------
#
# Decided by the owner and recorded in docs/design/dispatch-and-integration.md,
# sections 4.2 and 4.3. Two knobs, chosen per dispatch at submit time.
#
# THEY ARE NOT EXECUTION PARAMETERS, so invariant 10 is untouched: neither one
# selects an image, a command, a resource class, a backend or a provider. They
# say what happens to the work AFTER the agent has produced it.
#
# They live in `task.metadata` rather than on the Task dataclass because
# `apps/common/swarm_common/` is frozen and a new field on Task would be a
# frozen change. `metadata` is already the free-form per-dispatch dict and is
# already used this way (`metadata.unit` from swarm-mcp, `metadata.input_from`
# written by workflow submission below).

#: Accepted `strategy` values, in the order every refusal lists them.
#:
#:   collect     patches are harvested into the task's GCS prefix and nothing is
#:               pushed. The only strategy that works with a read-only token.
#:   direct-pr   every agent pushes `swarm/<task>` and opens its own PR.
#:   integrate   one final step receives the others' patches and opens ONE PR.
#:   single-pr   one `author` step opens ONE PR, later steps read or amend that
#:               same branch, and the chain ends in a `merge` worker action that
#:               merges it (#295, docs/merge-step.md §3). Workflow scale only.
DISPATCH_STRATEGIES = ("collect", "direct-pr", "integrate", "single-pr")

#: The strategy whose chain merges its own pull request (#295).
SINGLE_PR = "single-pr"

#: The worker actions a `single-pr` chain must contain exactly one step of
#: each: the step that posts the review's verdict to GitHub and the step that
#: merges. Neither runs an agent (contract requests 33 and 35).
SINGLE_PR_WORKER_ACTIONS = (WorkerAction.POST_VERDICT, WorkerAction.MERGE)

# --- the `merge` step outside `single-pr` (contract request 47) -----------
#
# Owner decisions of 2026-10-04, recorded on #295: merging moves off the
# GitHub-side App into a workflow `merge` step, on the tenant's existing
# `-git` token, in any repository a workflow runs on, as an OPT-IN final step
# chosen by a platform default or per job (docs/merge-step.md, "Revised
# 2026-10-04 (owner)"; docs/workflows.md).

#: The strategies whose workflow opens a pull request a `merge` step can merge:
#: `integrate`'s one, and a `direct-pr` workflow's when it has one agent step.
MERGE_STRATEGIES = ("direct-pr", "integrate")

#: The key inside a workflow's `metadata` that chooses the merge step per job.
#: NOT reserved: the caller writes it, and it is stored as written.
#:
#:   on    append a `merge` step (when the spec states none); refused for a
#:         workflow that opens no single pull request, rather than ignored.
#:   on_merge_verdict
#:         append a `merge` step that merges ONLY on a review's MERGE verdict:
#:         an `integrate` workflow whose integrator is gated on a review. A
#:         re-review step is appended too, on the integrator's pushed head, and
#:         the merge reads ITS verdict -- the latest one (`REREVIEW_ROUNDS`).
#:         It is gated like the integrator, so its agent runs only after a
#:         fix; on MERGE it passes the first review's verdict on unchanged.
#:         Refused for a workflow with no such review, rather than merging
#:         unreviewed work, and beside a spec's own merge step.
#:   off   append none, and refuse a spec that states one: the two disagree.
#:   absent  the repository's registered `merge_policy`, else the platform's
#:         `merge_by_default` ("on"), decides whether one is appended; a
#:         spec's own merge step is honoured whatever they say.
MERGE_METADATA_KEY = "merge"
MERGE_ON_VERDICT = "on_merge_verdict"
MERGE_CHOICES = ("on", "off", MERGE_ON_VERDICT)

#: What a registered repository's `merge_policy` may be (WF-MERGE-API,
#: 2026-10-10): the default a workflow on it takes when its `metadata.merge`
#: says nothing, and the default `auto_merge` of an issue run on it. Absent
#: (never set) defers to the platform's `merge_by_default`. Only these two,
#: not "on": a default that merges with no review verdict is a decision a job
#: makes for itself, never one a repository makes for every job on it.
MERGE_POLICY_OFF = "off"
MERGE_POLICIES = (MERGE_POLICY_OFF, MERGE_ON_VERDICT)

#: The step id the appended re-review takes, suffixed when a step already has it.
REREVIEW_STEP_ID = "re-review"

#: Review rounds after the fix under `on_merge_verdict`: ONE. The first review
#: judges the implementer; the fix runs on NOT_YET; one re-review judges the
#: fix's pushed head, and the merge reads that verdict. A second NOT_YET stops
#: the workflow at its merge step (`verdict_not_merge`) with the pull request
#: open for a person, rather than paying for fix after fix: a review that
#: still says NOT_YET after one fix is a disagreement a person settles, and an
#: unbounded loop is a cost nobody chose. The workflow is a DAG fixed at
#: submission, so a round is a step that exists or not; raising this means
#: appending fix/re-review pairs, which `rereview_step_for` does not do.
REREVIEW_ROUNDS = 1

#: What the re-review agent is told before the review's own instructions. The
#: earlier verdict and the review's inputs are staged by parent, so each lands
#: at `<step id>/<file>` and the earlier `verdict.json` can never be mistaken
#: for the one this step writes into `$SWARM_ARTIFACTS_DIR`.
REREVIEW_PREAMBLE = (
    "You are the RE-REVIEW of this workflow (one round, after the fix step). "
    "Your checkout is the head the fix step pushed: the implementer's change "
    "plus any fix. Review that whole change against the default branch, not "
    "only the fix. The earlier review's verdict is staged at {verdict_path}; "
    "check every finding in it is resolved. Write your OWN verdict to "
    "$SWARM_ARTIFACTS_DIR/{verdict_file} in the same shape: "
    '{{"verdict": "MERGE" or "NOT_YET", "commit": "<the full SHA of HEAD you '
    'reviewed, from git rev-parse HEAD>", "findings": [...]}}. '
    "The merge step merges only on your MERGE. Do not edit files.\n\n"
    "The original review instructions follow, for reference; files they name "
    "are staged under the directory of the step that produced them ({staged}).\n\n"
)

#: The signed dispatch block on a merge step naming what it merges. The
#: worker spells it `agent_worker.merge.MERGE_TARGET_FIELD`;
#: tests/unit/worker/test_merge_action.py holds the two equal.
MERGE_TARGET_FIELD = "merge_target"
#: `merge_target.merge_mode` (TRAIN-WS, docs/merge-trains.md §5.1): the
#: two modes a workspace merges in. Defined here, where the signed block is
#: built, and imported by `mergesettings`; only TRAIN is ever written.
MERGE_MODE_DIRECT = "direct"
MERGE_MODE_TRAIN = "train"

#: The forge hosts a merge step can act on: github.com, the only host the
#: tenant's token is ever sent to (`agent_worker.forge.may_receive_forge_token`,
#: #307), where `agent_worker.merge.GitHubMerger` is the one `ForgeMerger`.
#: Another host is refused at SUBMISSION, never at merge time. The worker's
#: `merge.MERGEABLE_HOSTS` is held equal to this by test_merge_action.py.
MERGE_FORGE_HOSTS = frozenset({"github.com", "www.github.com"})

#: Attempts a merge step gets. A required check still running no longer
#: spends one: the step parks CI_PENDING and its attempt is refunded, up to the
#: worker's `MERGE_CI_MAX_WAKES` (lane MS2, docs/merge-step.md "Revised
#: 2026-10-06" §1). Past that bound each wake counts, so ten is how many wakes
#: a pull request whose CI never settles gets after its refunds, and how many
#: outages a merge survives, against the three a task gets by default. The
#: API's own ceiling on `max_attempts` is 10.
MERGE_STEP_MAX_ATTEMPTS = 10

#: The step id an appended merge step takes, suffixed when a step already has it.
MERGE_STEP_ID = "merge"

# --- the merge step's knobs (lane MS1, docs/merge-step.md "Revised 2026-10-06") ---

#: The key inside a workflow's `metadata` that asks for CI-fix rounds: how
#: many times a red required check hands the pull request to a fix
#: continuation before the merge step refuses `checks_failed` (MS7 spends
#: them). NOT reserved: the caller writes it, and it is stored as written.
MERGE_FIX_ROUNDS_KEY = "merge_fix_rounds"
#: Absent ON A STORED MERGE TASK means 0: a red check refuses at once, which
#: is what the built step did before SPEED-D2. Submission now stamps every
#: merge task with `resolve_fix_rounds` (2 for a lane by default), so 0 is
#: what an older task, or one whose key was deleted, reads as. 5 bounds what one pull request can spend on fix agents before a
#: person looks at it; each round is a full agent attempt.
MERGE_FIX_ROUNDS_DEFAULT = 0
MERGE_FIX_ROUNDS_MAX = 5


def resolve_merge_fix_rounds(
    metadata: Mapping[str, Any], *, merge_step: bool
) -> int:
    """A workflow's `metadata.merge_fix_rounds`, or the default when it is absent.

    Refused when it is not an integer in 0-`MERGE_FIX_ROUNDS_MAX` (a bool is
    not one), and refused whenever it is present on a workflow with no merge
    step: rounds that nothing would spend are a request that would silently
    not happen, as `metadata.merge` "on" on such a workflow is refused.
    """
    if MERGE_FIX_ROUNDS_KEY not in metadata:
        return MERGE_FIX_ROUNDS_DEFAULT
    value = metadata[MERGE_FIX_ROUNDS_KEY]
    field = f"metadata.{MERGE_FIX_ROUNDS_KEY}"
    bounds = {"min": 0, "max": MERGE_FIX_ROUNDS_MAX}
    if isinstance(value, bool) or not isinstance(value, int) or not (
        0 <= value <= MERGE_FIX_ROUNDS_MAX
    ):
        raise DispatchOptionError(
            f"{field} is {str(value)[:40]!r}; it is a whole number of CI-fix rounds "
            f"from 0 to {MERGE_FIX_ROUNDS_MAX}, or absent for "
            f"{MERGE_FIX_ROUNDS_DEFAULT}.",
            detail={"field": field, "accepted": bounds},
        )
    if not merge_step:
        raise DispatchOptionError(
            f"{field} asks for CI-fix rounds before a merge, but this workflow has no "
            f"merge step to spend them. Set metadata.{MERGE_METADATA_KEY} to 'on', "
            f"or remove {field}.",
            detail={"field": field, "accepted": bounds, "merge_step": False},
        )
    return value


#: The fix rounds a workflow with a merge step gets when neither its spec's
#: `fix_rounds` nor its `metadata.merge_fix_rounds` names a number (lane
#: SPEED-D2, owner decision 2026-10-14 "Auto fix rounds"). Two, measured: of
#: the last 150 merged pull requests 0.52 runs per PR came back red and the
#: operator dispatched a fixer by hand five times in one day, and the PR-side
#: fixer's own bound is two (`cifix.DEFAULT_MAX_ATTEMPTS`). A second red at
#: the head a round pushed is a question for a person, not a third agent.
#: Not 0, which is `MERGE_FIX_ROUNDS_DEFAULT`: that is what an ABSENT key on
#: a stored merge task reads as, and every merge task this resolution stamps
#: carries the number, so the two never meet.
LANE_FIX_ROUNDS_DEFAULT = 2
#: The workflow metadata key an issue run's workflows carry
#: (`issueci.merge_workflow`, `issueruns.compile_plan`). An issue run fixes
#: red CI with its own loop (`--fix-rounds`, `issueci._start_round`) before it
#: submits its merge, so its merge step keeps no rounds of its own unless
#: asked: two loops on one pull request would race each other's pushes.
ISSUE_RUN_METADATA_KEY = "issue_run"


def resolve_fix_rounds(
    fix_rounds: int | None, metadata: Mapping[str, Any], *, merge_step: bool,
    named_pull: bool = False,
) -> int | None:
    """The fix rounds a workflow's merge step may spend, or None with no merge step.

    `fix_rounds` is the spec's field (`WorkflowCreate.fix_rounds`, 0-5,
    typed there); `metadata` the workflow's, whose `merge_fix_rounds`
    (`resolve_merge_fix_rounds`, checked per step) says the same thing the
    older way. Both given and different is refused, rather than one quietly
    winning. Neither given: `LANE_FIX_ROUNDS_DEFAULT`, or 0 for an issue
    run's workflow. `fix_rounds` with no merge step is refused, as the
    metadata key is. A `merge_pr` workflow (`named_pull`, #352) gets 0 and
    refuses any other number, as `continuation.resolve_merge_pr` refuses the
    metadata key: its branch is no SwarmCloud task's, so no round could push
    to it, and rounds asked for and never possible would park the worker on
    `ci_fix_pending` until its six-hour bound.

    The number lives on the merge TASK as `metadata.merge_fix_rounds`, which
    the wake tick and the worker already read: the frozen `Workflow` has no
    field for it (docs/contract-change-requests.md entry 75).
    """
    stated = resolve_merge_fix_rounds(metadata, merge_step=merge_step)
    explicit = MERGE_FIX_ROUNDS_KEY in metadata
    if fix_rounds is not None and not merge_step:
        raise DispatchOptionError(
            f"fix_rounds is {fix_rounds}, but this workflow has no merge step to spend "
            f"fix rounds before. Set metadata.{MERGE_METADATA_KEY} to 'on', or remove "
            "fix_rounds.",
            detail={"field": "fix_rounds", "merge_step": False},
        )
    if fix_rounds is not None and explicit and fix_rounds != stated:
        raise DispatchOptionError(
            f"fix_rounds is {fix_rounds} and metadata.{MERGE_FIX_ROUNDS_KEY} is {stated}: "
            "they name the same budget. Give one of them.",
            detail={"field": "fix_rounds", "metadata": f"metadata.{MERGE_FIX_ROUNDS_KEY}"},
        )
    if not merge_step:
        return None
    if named_pull:
        if fix_rounds:
            raise DispatchOptionError(
                f"merge_pr takes no fix_rounds (it is {fix_rounds}): a fix round continues "
                "the task whose branch the pull request is on, and a named pull request's "
                "branch is no task's. Remove fix_rounds, or set it to 0.",
                detail={"field": "fix_rounds", "merge_pr": True},
            )
        return MERGE_FIX_ROUNDS_DEFAULT
    if fix_rounds is not None:
        return fix_rounds
    if explicit:
        return stated
    if ISSUE_RUN_METADATA_KEY in metadata:
        return MERGE_FIX_ROUNDS_DEFAULT
    return LANE_FIX_ROUNDS_DEFAULT


def merge_repository(repository_url: str | None) -> tuple[str, str] | None:
    """`(owner, repo)` of a repository a merge step can act on, else None.

    Only for a host `is_mergeable_forge` admits, so the pair is a GitHub
    name. Used to find the tenant's registration of the repository
    (`swarm_api.repositories.repo_id_for`), never to reach the forge.
    """
    if not is_mergeable_forge(repository_url):
        return None
    from urllib.parse import urlsplit

    text = (repository_url or "").strip()
    if text.startswith("git@"):
        text = "ssh://" + text.replace(":", "/", 1)
    try:
        path = urlsplit(text).path
    except ValueError:
        return None
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return (owner, repo) if owner and repo else None


def dispatchable_strategies() -> tuple[str, ...]:
    """`DISPATCH_STRATEGIES` less any strategy no workflow could complete today.

    `single-pr` is ACCEPTED as a word -- a caller who names it is told what
    its chain lacks, not that the word is unknown -- but it cannot run while
    the catalogue disables the `merge` or `post-verdict` profile, which the
    owner decided on 2026-10-01 it does for every tenant until #342 is
    enforced and the review and merge Apps exist. A surface that OFFERS
    strategies (the UI, the CLI) offers these, so it never offers a control
    that refuses every time; the day both profiles are enabled this equals
    `DISPATCH_STRATEGIES` and the parity tests demand the surfaces follow.
    """
    enabled = {
        p.worker_action for p in RUNNER_PROFILES.values()
        if p.available and p.worker_action is not None
    }
    return tuple(
        s for s in DISPATCH_STRATEGIES
        if s != SINGLE_PR or all(a in enabled for a in SINGLE_PR_WORKER_ACTIONS)
    )

#: Accepted `carrier` values -- where a step's work is kept for the next step.
#:
#:   checkpoints the tarballs the worker already writes every few minutes.
#:   branches    pushed feature branches; durable, and they outlive the platform.
DISPATCH_CARRIERS = ("checkpoints", "branches")

#: THE DEFAULTS ARE TODAY'S BEHAVIOUR, and that is the whole reason they are
#: these two values. A caller who sends neither field gets a task that behaves
#: exactly as it did before this feature existed, and a deployment whose tenant
#: token is read-only keeps working. Widening a token's scope stays a decision
#: somebody makes rather than one that happens to them because a default moved.
DEFAULT_STRATEGY = "collect"
DEFAULT_CARRIER = "checkpoints"

#: The single key inside `task.metadata` this feature owns. One nested key
#: rather than two flat ones because `metadata` is caller-supplied and free
#: form: a caller who already sends `metadata.strategy` for their own purposes
#: must not have it silently overwritten by the platform.
DISPATCH_METADATA_KEY = "dispatch"
#: The record a merge step's conflict fix round is submitted with
#: (`mergewake._on_conflict`, #1076): round, merge task, head, pull request.
#: A caller's metadata as far as signing goes, so the worker never reads it.
CONFLICT_ROUND_METADATA_KEY = "merge_conflict_round"
#: The dispatch block's mark of a conflict fix round (#1321), copied from
#: the record above at submission, on a continuation only. Inside the signed
#: block, so the worker may act on it: it checks, before the round pushes,
#: that the branch now stands on the default branch's head. The worker spells
#: it `agent_worker.lifecycle.CONFLICT_ROUND_FIELD`.
CONFLICT_ROUND_FIELD = "conflict_round"
#: A CI fix's red head (#2276): `{"pull_request": int, "head_sha": 40-hex}`,
#: copied from `metadata.ci_fix` at submission, on a continuation only, and
#: only those two fields. Inside the signed block, so the worker may act on
#: it: at attempt start it ends a fix as superseded when the pull request's
#: branch has moved past that head or the pull request is merged or closed.
#: The worker spells it `agent_worker.cifixstart.FIELD`.
CI_FIX_HEAD_FIELD = "ci_fix_head"

#: The key inside `task.metadata` the worker stages declared inputs from:
#: `{upstream TASK id: filename}`. Written by workflow expansion only
#: (`SubmissionService.submit_workflow`), which rewrites a step's
#: `{upstream_step: filename}` to the task ids it has just minted. The worker
#: spells it `agent_worker.inputs.METADATA_KEY`, and
#: tests/unit/control_plane/test_input_from_is_reserved.py and
#: test_input_from_submission.py hold the two equal: reserving a key the worker
#: does not read would refuse nothing that matters. Defined once, here, beside
#: the other reserved keys, and `submit_workflow` writes under it.
INPUT_FROM_METADATA_KEY = "input_from"

#: The key inside a workflow's or a step's `metadata` choosing where that
#: step's `input_from` files land (#75, owner decision 2026-10-01, option (b)).
#: NOT reserved: the caller writes it, and it is stored on the step's task as
#: written. What the worker acts on is `DispatchOptions.input_parents`, which
#: this service derives from it into the signed dispatch block.
#:
#:   by_name    the filename is the path, exactly as before #75. The default,
#:              so no existing prompt's paths change.
#:   by_parent  each file lands at `<parent_step_id>/<filename>`, so two parents
#:              that both write `notes.md` can feed one step.
#:
#: Carried in metadata rather than as a field on the step because
#: `WorkflowStep.input_from` is the frozen contract's `dict[str, str]` and a
#: new field there would be a frozen change.
INPUT_LAYOUT_METADATA_KEY = "input_layout"
INPUT_LAYOUT_BY_NAME = "by_name"
INPUT_LAYOUT_BY_PARENT = "by_parent"
INPUT_LAYOUTS = (INPUT_LAYOUT_BY_NAME, INPUT_LAYOUT_BY_PARENT)
DEFAULT_INPUT_LAYOUT = INPUT_LAYOUT_BY_NAME

#: The key inside `task.metadata` naming the files a workflow step's dependants
#: stage from it (#149). Written by workflow expansion only, on each UPSTREAM
#: step's task (`swarm_api.expected_outputs.record_expected_outputs`). Defined
#: here, beside the other reserved keys, because `expected_outputs` imports this
#: module and the reservation below needs the key: the other way round is an
#: import cycle. `swarm_api.expected_outputs` re-exports it under the same name,
#: and tests/unit/control_plane/test_expected_outputs_seam.py holds it equal to
#: the worker's `agent_worker.expected_outputs.METADATA_KEY`.
EXPECTED_OUTPUTS_METADATA_KEY = "expected_outputs"

#: The key inside `task.metadata` recording attempts the reconciler took back
#: because they ended before their runner started (#67). Written only by the
#: reconciler, after it repairs a task (`reconciler.model.STARTUP_REFUNDS_KEY`,
#: `reconciler.store.ControlStore.repair_task_state`), never by this service. Before
#: this reservation, a caller could set it directly: `TaskView.from_doc` read
#: it with no bound, and `int(float("inf"))` raised `OverflowError` out of the
#: unguarded per-task loop in `ControlStore.snapshot`, crashing a reconciler
#: pass for every task of every tenant (security review, PR #290 -- the read
#: path itself was also made total, see `reconciler.model._startup_refunds_int`,
#: so a document written before this reservation existed cannot do it either).
#: Defined here rather than imported from `reconciler.model`, which `swarm-api`
#: does not depend on; tests/unit/control_plane/test_startup_refunds_reserved.py
#: holds the two strings equal, the same seam `test_input_from_is_reserved.py`
#: holds for `INPUT_FROM_METADATA_KEY`.
STARTUP_REFUNDS_METADATA_KEY = "startup_refunds"

#: The reconciler's count of a task's attempts lost to its pod passing its own
#: disk limit (#893, `reconciler.model.DISK_EVICTIONS_KEY`). A caller who set it
#: would choose how often its task is retried after an eviction. Restated for
#: the reason STARTUP_REFUNDS_METADATA_KEY is;
#: tests/unit/worker/test_worker_evicted.py holds them equal.
DISK_EVICTIONS_METADATA_KEY = "disk_evictions"

#: Why the current attempt's GKE pod is not starting (F1-POD,
#: `reconciler.startproblem.START_PROBLEM_KEY`): the container, kubelet's
#: reason, its restarts and message. Written and cleared only by the
#: reconciler; a caller who set it would put a false "Pod not starting" on its
#: task's page. Restated for the reason STARTUP_REFUNDS_METADATA_KEY is;
#: tests/unit/reconciler/test_pod_start_problem.py holds them equal.
START_PROBLEM_METADATA_KEY = "start_problem"

#: The owner's answers to the questions the task's agent asked (lane
#: TASK-ANSWERS, owner decision 2026-10-13, part of #1206): one entry per
#: answered question, `{question_index, question, choice, text, by, at,
#: history}`. Written only by POST /v1/tasks/{task_id}/answers
#: (`agent_questions.answer_question`); a caller who set it at submission
#: would put words in the owner's mouth that the launcher then acts on.
#: In metadata, as `start_problem` is, because the frozen `Task` has no field
#: for it and an additive key changes no reader that does not want it.
OWNER_ANSWERS_METADATA_KEY = "owner_answers"

#: Whose slot a service submission's task runs with (INDEX-CRED, part of
#: #1041): `tenant` (the tenant's `-git` token) or `owner` (a personal
#: tenant's owner's GitHub connection, `forge.service_slot`). Written only by
#: `SubmissionService._resolve_forge`, before the spec is signed, and read by
#: `codec.forge_credential_source`: the slot name alone (`git-u-<hex>`) cannot
#: tell the owner's connection run on the tenant's behalf from a person's
#: own task, and a caller who set it would mislabel whose credential ran.
#: Not signed (`swarm_common.specsign.SIGNED_METADATA_KEYS` is frozen): it is
#: a label no worker acts on, and it says `owner` only beside a `git-u-`
#: `forge_credential`, which IS signed.
FORGE_SOURCE_METADATA_KEY = "forge_credential_source"

#: Child tasks (contract request 14, docs/design/child-tasks.md §6.4). The
#: agent's `request_id` on a child, written once by the children route and read
#: by its dedupe; the awaits refunded so far on a parent, written by the
#: worker's await park; and the cascade marker on a child the API or the
#: scheduler cancelled because of its parent, which tells whichever writer
#: finishes a flagged child to end it CHILD_CASCADE. The worker and the
#: scheduler restate the last two; tests/unit/control_plane/test_child_tasks*.py
#: holds the strings equal.
CHILD_REQUEST_ID_METADATA_KEY = "child_request_id"
CHILD_AWAIT_RESUMES_METADATA_KEY = "child_await_resumes"
CHILD_CASCADE_METADATA_KEY = "child_cascade"

#: A merge step's CI wait (lane MS2, docs/merge-step.md "Revised 2026-10-06"
#: §1): what its park waits on, written by the worker's CI_PENDING park, and
#: the wake marker in it, written by the wake tick (`mergewake`). Restated by
#: the worker and the scheduler; tests/unit/worker/test_merge_action.py holds
#: the strings equal.
MERGE_WAIT_METADATA_KEY = "merge_wait"

#: The mark a schedule's firing puts on the work it creates (docs/schedules.md
#: §1.5, §2.7, lane S2): `{schedule_id, firing_id, slot, type}`. A caller who
#: could set it would forge "made by schedule X" on any task, and could plant
#: a task a schedule's finisher would ADOPT as its own firing's work (§2.2),
#: so the firing would create nothing and report someone else's outcome.
#: Written only by `schedulefire`, through `schedule_mark_allowed`. Unsigned:
#: no worker acts on it, only swarm-api reads it.
SCHEDULE_METADATA_KEY = "schedule"

#: The live-browser hand-off (docs/design/live-browser.md §4.3, lane LB-L10):
#: the parks refunded so far, written by the worker's HUMAN_REQUIRED park, as
#: `child_await_resumes` is by the await's; the mark that a person pressed
#: "I'm ready to sign in" (`human-ready`), which tells the next attempt's worker
#: to open the hand-off at once; and the hand-offs one attempt has asked for,
#: the cap swarm-api enforces from the task document. A caller who set the first
#: would buy a free attempt; the second, a hand-off nobody asked for; the third,
#: a cap of its choosing. `swarm_api.handoff` imports these; the worker restates
#: the first two (lane L4).
HUMAN_WAIT_RESUMES_METADATA_KEY = "human_wait_resumes"
HUMAN_READY_METADATA_KEY = "human_ready"
HUMAN_HANDOFFS_METADATA_KEY = "human_handoffs"
#: LB-RETURN: a person returned control while an attempt ran; the worker's park
#: of exactly that attempt reads it and re-queues READY instead. A caller who
#: set it would turn a park into a free re-queue.
HUMAN_RETURNED_METADATA_KEY = "human_returned"

#: An agent's release wait (lane AU7-REFUND, docs/design/agent-autonomy.md
#: §3.8): the waits whose attempt the worker's release-wait park refunded,
#: counted against `attemptstate.RELEASE_WAIT_MAX_RESUMES`. A caller who set
#: it would choose how many free attempts the task has left: a negative or
#: zero count buys waits beyond the bound. The worker restates it
#: (`control.RELEASE_WAIT_RESUMES_METADATA_KEY`).
RELEASE_WAIT_RESUMES_METADATA_KEY = "release_wait_resumes"

#: The provider-quota parks whose attempt was refunded (#2350), counted
#: against `attemptstate.QUOTA_MAX_RESUMES`. A caller who set it would choose
#: how many free attempts the task has left. The worker restates it
#: (`control.QUOTA_PARK_RESUMES_METADATA_KEY`).
QUOTA_PARK_RESUMES_METADATA_KEY = "quota_park_resumes"

#: The pull requests a workflow's root step waits for before it may start
#: (`after`, lane SPEED-D3, `swarm_api.prgate`): written by `submit_workflow`
#: from the spec's `after` and then only by the wake tick. A caller who set
#: it would start a held workflow early or hold one nobody asked to.
AFTER_PRS_METADATA_KEY = "after_prs"

#: An `await-input` step's wait and its answer (docs/design/human-steps.md
#: §4.7): `opened_at`, `expires_at` and the answer's record, written only by
#: the platform (the scheduler's park, swarm-api's answer route). A caller who
#: set it would hand the step an answer nobody gave, or an expiry nobody chose.
#: The DECLARATION is not here: it is `metadata.dispatch.await_input`, signed.
AWAIT_INPUT_METADATA_KEY = "await_input"

#: A QA run's mark on each of its browser steps (docs/design/visual-qa-any-app.md
#: §1.1): `{id, revision}` of the target the step drives, which the worker's
#: route (`POST /v1/attempts/{id}/qa-target`) resolves the target from. Written
#: only by `:run`, through `qa_target_mark_allowed`. VQ-2 already made a
#: caller's mark inert (the route serves a target only to a task `:run`'s
#: `qa_target_runs` record names); reserving it is the defence in depth decided
#: 2026-10-22 (VQ2-TARGETS question 3), so no caller's mark reaches the store.
#: Unsigned: no worker acts on it, only swarm-api reads it.
QA_TARGET_METADATA_KEY = "qa_target"

#: Every key inside `task.metadata` this service writes and a caller may not,
#: in the order a refusal names them. One tuple, checked by one function, so a
#: caller who sent several is told about all of them in one 422 rather than one
#: per round trip -- which is what #153's separate expected_outputs check did
#: until it was folded in here.
RESERVED_METADATA_KEYS = (
    DISPATCH_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    EXPECTED_OUTPUTS_METADATA_KEY,
    STARTUP_REFUNDS_METADATA_KEY,
    CHILD_REQUEST_ID_METADATA_KEY,
    CHILD_AWAIT_RESUMES_METADATA_KEY,
    CHILD_CASCADE_METADATA_KEY,
    MERGE_WAIT_METADATA_KEY,
    DISK_EVICTIONS_METADATA_KEY,
    SCHEDULE_METADATA_KEY,
    HUMAN_WAIT_RESUMES_METADATA_KEY,
    HUMAN_READY_METADATA_KEY,
    HUMAN_HANDOFFS_METADATA_KEY,
    HUMAN_RETURNED_METADATA_KEY,
    START_PROBLEM_METADATA_KEY,
    OWNER_ANSWERS_METADATA_KEY,
    FORGE_SOURCE_METADATA_KEY,
    AFTER_PRS_METADATA_KEY,
    QA_TARGET_METADATA_KEY,
    RELEASE_WAIT_RESUMES_METADATA_KEY,
    QUOTA_PARK_RESUMES_METADATA_KEY,
)

#: Reserved like every key above, and refused by the same function in the
#: same 422, but NOT YET IN `RESERVED_METADATA_KEYS`. That tuple is two more
#: things besides a refusal list: `task_input.PLATFORM_METADATA_KEYS` (served
#: as stored, unmasked) and, less the counters, the spec signature's key set
#: (tests/unit/common/test_specsign_covers.py). `metadata.await_input` is
#: written by no one yet: the lane that writes it (HW-API-ANSWER) decides how
#: it is served, and moves it into that tuple with the parity test's entry.
#: Until then nothing a caller sends can reach the store under it.
REFUSED_UNWRITTEN_METADATA_KEYS = (AWAIT_INPUT_METADATA_KEY,)

#: An agent's blocking question (lane AU-7, docs/design/agent-autonomy.md
#: §3.9): the wait swarm-api adopts from the worker's park into
#: `metadata.question_wait`, and the earlier waits with the answers each was
#: given, written only by `agent_questions.adopt_wait` while the task is
#: parked. The worker hands the first to the resumed agent, and neither is in
#: the step-spec signature (written after submission by design), so refusing
#: them here is what proves swarm-api wrote them: a caller who set them would
#: hand the resumed agent a wait, and answers, nobody asked or gave.
#: Refused by the same function in the same 422 as `RESERVED_METADATA_KEYS`,
#: but NOT IN that tuple, which `task_input.PLATFORM_METADATA_KEYS` serves as
#: stored: the wait is the agent's text, stored unredacted (swarm-api redacts
#: it when `/questions` serves it), so it must keep going through the masker
#: as caller metadata does. Moving them into the tuple also needs them in
#: `task_input.COLLECTED_METADATA_KEYS` and in the spec-signature parity
#: test's `UNSIGNED_PLATFORM_KEYS` (tests/unit/common/test_specsign_covers.py).
QUESTION_WAIT_METADATA_KEY = "question_wait"
QUESTION_WAITS_EARLIER_METADATA_KEY = "question_waits_earlier"
REFUSED_AGENT_TEXT_METADATA_KEYS = (
    QUESTION_WAIT_METADATA_KEY,
    QUESTION_WAITS_EARLIER_METADATA_KEY,
)

#: Strategies and carriers that cannot work without somewhere to push to.
_NEEDS_REPOSITORY_STRATEGIES = ("direct-pr", "integrate", "single-pr")

#: Strategies under which EVERY step pushes `swarm/<its task id>`: `direct-pr`
#: and an `integrate` contributor both push, and so does the integrator. A step
#: can start from an upstream step's branch (`builds_on`) only under these,
#: because under `collect` no branch is ever pushed to start from.
_EVERY_STEP_PUSHES_STRATEGIES = ("direct-pr", "integrate")

#: The verdicts a review step writes into its verdict file, in the order every
#: refusal lists them (#264). `{"verdict": "MERGE" | "NOT_YET", "findings":
#: [...]}` is the convention; a step's `when.verdict_in` names which of them
#: run its agent. The worker restates this as
#: `agent_worker.verdict.REVIEW_VERDICTS`, because it must not import the
#: control plane, and tests/unit/worker/test_verdict_gate.py holds the two
#: equal.
REVIEW_VERDICTS = ("MERGE", "NOT_YET")

#: A `single-pr` step's part in the one pull request (docs/merge-step.md §3),
#: in the order every refusal lists them.
#:
#:   author   clones `repository_ref`, pushes `swarm/<own task id>` and opens
#:            the pull request. Exactly one per chain.
#:   reader   clones the author's branch and pushes nothing (review, proof).
#:   amender  clones the author's branch and fast-forward pushes to it,
#:            opening nothing (fix). At most one per chain.
#:   none     clones nothing: a worker-action step (post-verdict, merge). The
#:            only role a worker action may hold, and the one it is given
#:            when it declares none.
PR_ROLES = ("author", "reader", "amender", "none")
PR_ROLE_NONE = "none"

#: The roles an AGENT step of a `single-pr` chain may declare.
_AGENT_PR_ROLES = ("author", "reader", "amender")

#: The keys of the merge step's `merges` block, in the order it is written.
#: Corrected in the joint review with CR 34 (docs/merge-step.md §4.1) to
#: include `post-verdict`, whose signed spec the merge verifies with the rest.
#: `fix` is present only when the chain has an amender.
MERGES_KEYS = ("author", "review", "post-verdict", "fix", "proof")

#: The one file the merge stages, from the proof (docs/merge-step.md §4.1).
PROOF_FILENAME = "proof.json"


# --------------------------------------------------------------------------
# A repository URL carries no credential
# --------------------------------------------------------------------------
#
# WHY (the PR #229 review). `repository_url` is caller-supplied, and the usual
# way to hand a tool a private repository is to put the token in it:
# `https://x-access-token:ghp_...@github.com/o/r`. `_repo_scheme` checked the
# scheme only, so the token was stored on the task, served on every task route
# beside the masked input, drawn in Details' `repo` fact directly above the
# masked prompt, written into the clone's argv (which the worker logs), and
# echoed into stderr -- and so into `last_error` -- by a failed clone.
#
# The platform has its own path for a private repository, and it is the only
# one: the tenant's forge token lives in Secret Manager as
# `swarm-tenant-<tenant>-git` (`scripts/create-secrets.sh --stdin`), and the
# worker writes it into a 0600 credential file for git at clone time
# (`agent_worker.gitops`), never into argv. So a URL carrying userinfo is
# refused at submission, and one stored before this is masked on the way out
# (`task_input.TaskMasking.repository_url`). ONE definition of "userinfo that
# can carry a credential", used by both, below.
#
#   * `https://` -- ANY userinfo. A forge token is as often the user name
#     (`https://ghp_...@github.com/o/r`) as the password.
#   * `ssh://` and the scp form `git@host:path` -- any userinfo but exactly
#     `SSH_LOGIN`. The first version of this rule let any BARE ssh user name
#     through as "the login", so `ssh://<token>@host/o/r` and
#     `git@<token>@host:o/r` were accepted and stored whole (wave 2026-09-27,
#     epic #227). A token goes in the user position as easily as in the
#     password's, and a forge's ssh endpoint logs in as `git` and nothing else,
#     so no other user name is something a caller needs to send.
#
# The refusal names no part of the value: the part that would identify the
# problem is the credential.

#: The one ssh user that is a login and not a credential.
SSH_LOGIN = "git"

#: What the refusal tells a caller to do instead.
REPOSITORY_CREDENTIAL_PATH = (
    "a private repository is cloned with the tenant's forge token, which an "
    "operator stores in Secret Manager as swarm-tenant-<tenant>-git "
    "(scripts/create-secrets.sh --stdin); the worker hands it to git at clone "
    "time, never in the URL"
)


def repository_userinfo(url: str) -> tuple[int, int] | None:
    """Where the credential-bearing userinfo of `url` is, as `(start, end)`; else None.

    `end` is the index of the `@` that closes it. None for a URL with no
    userinfo, and for an ssh or scp-form URL whose user is exactly `SSH_LOGIN`.

    SCP VERSUS URL IS DECIDED BY PREFIX, NOT BY WHETHER `://` APPEARS
    ANYWHERE. An scp-form path can itself contain `://`
    (`git@tok@host:o/r://x`), and a repository path never does otherwise, so
    scanning the whole string for `://` sent that URL to the URL branch below
    and skipped its userinfo entirely. Only a literal `git@` prefix (an scp
    URL always starts with `{SSH_LOGIN}@`, the one prefix `check_repository_url`
    allows besides `https://` and `ssh://`) or a total absence of `://` is
    scp form; everything else is `scheme://...`.

    THE SCP FORM HAS NO AUTHORITY DELIMITER a token cannot also contain: its
    host ends at the first `:`, and `git@tok:pw@host:path` puts a `:` inside
    the userinfo. So its userinfo runs to the LAST `@` in the whole URL. That
    refuses an scp-form path containing `@`, which no forge's repository path
    does, and masks every credential that shape can hold.

    For `ssh://`, the authority ends at the first `/` ONLY -- git's own
    `parse_connect_url` cuts an ssh host the same way, so a `?` or `#` inside
    the userinfo (`ssh://tok#@host/o/r`, `ssh://tok?@host/o/r`) does not end
    it early the way it does for https.
    """
    if url.startswith(f"{SSH_LOGIN}@") or "://" not in url:
        at = url.rfind("@")
        if at < 0 or url[:at] == SSH_LOGIN:
            return None
        return 0, at
    scheme, sep, rest = url.partition("://")
    start = len(scheme) + len(sep)
    if scheme.lower() == "ssh":
        slash = rest.find("/")
        authority = rest[:slash] if slash >= 0 else rest
    else:
        ends = [at for at in (rest.find("/"), rest.find("?"), rest.find("#")) if at >= 0]
        authority = rest[: min(ends)] if ends else rest
    at = authority.rfind("@")
    if at < 0:
        return None
    if scheme.lower() == "ssh" and authority[:at] == SSH_LOGIN:
        return None
    return start, start + at


def check_repository_url(value: str | None) -> str | None:
    """The one `repository_url` rule, for `TaskCreate` and `WorkflowCreate` alike.

    Raises ValueError, which pydantic turns into the 422 naming the field.
    """
    if value is None:
        return None
    if not value.startswith(("https://", "git@", "ssh://")):
        raise ValueError("repository_url must be an https://, ssh:// or git@ URL")
    if repository_userinfo(value) is not None:
        # Constants only: see the section header.
        raise ValueError(
            "repository_url must not carry a credential (a user name or token before "
            f"'@'); an ssh URL may name only the {SSH_LOGIN!r} user; "
            + REPOSITORY_CREDENTIAL_PATH
        )
    return value


#: What the worker reads as a commit sha rather than a branch or tag name:
#: `agent_worker.gitops._SHA_RE`, restated because this image cannot import the
#: worker. Every ref this matches is fetched BY SHA, and a forge serves a fetch
#: by sha only for the full 40 characters.
_WORKER_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_FULL_SHA_LENGTH = 40


def check_repository_ref(value: str | None) -> str | None:
    """Refuse a `repository_ref` the worker would fetch as an abbreviated sha.

    task_531f0eeb was submitted with `b7bda42e` and failed in the clone, after
    admission, a lease and a container start (history analysis 2026-10-05,
    F4). The worker treats 7-40 lowercase hex as a sha and only a full one can
    be fetched, so 7-39 is refused here, before anything holds capacity. A
    branch or tag name -- anything the worker passes to `--branch` -- and a
    full sha are accepted unchanged here. Whether a branch or tag name exists
    is asked of the forge once per submission afterwards
    (`SubmissionService._check_ref_exists`, #2726), which refuses it with
    `RepositoryRefNotFound` and fails open.
    """
    if not value:
        return value
    if _WORKER_SHA_RE.match(value) and len(value) != _FULL_SHA_LENGTH:
        raise ValidationFailed(
            f"repository_ref {value!r} is an abbreviated commit sha, which the clone "
            f"cannot fetch; send the full {_FULL_SHA_LENGTH}-character sha "
            f"(`git rev-parse {value}`) or a branch or tag name instead",
            detail={"field": "repository_ref", "repository_ref": value},
        )
    return value


class RepositoryRefNotFound(ValidationFailed):
    """422 `repository_ref_not_found`: the forge has neither a branch nor a tag
    of that name in a repository it can read (#2726)."""

    code = "repository_ref_not_found"


#: `swarm/<task id>`: the branch a `direct-pr` task or an integrator pushed
#: (`continuation.TASK_ID_RE`, restated: continuation imports this module).
_SWARM_TASK_BRANCH_RE = re.compile(r"^swarm/(?P<task_id>task_[0-9a-f]{20})$")


def ref_needs_lookup(value: str | None) -> bool:
    """Whether `repository_ref` is a branch or tag name the forge is asked about.

    Not when absent (the default branch is cloned), and not a full sha, which
    the worker fetches by sha. An abbreviated sha never gets here:
    `check_repository_ref` refused it first.
    """
    if not value:
        return False
    return not (_WORKER_SHA_RE.match(value) and len(value) == _FULL_SHA_LENGTH)


def swarm_task_of_ref(value: str) -> str | None:
    """The task id a `swarm/<task id>` branch names, else None."""
    match = _SWARM_TASK_BRANCH_RE.match(value)
    return match.group("task_id") if match else None


def repository_ref_not_found(
    value: str,
    repository: str,
    *,
    task_id: str | None = None,
    pull_request: int | None = None,
    pull_state: str | None = None,
    default_branch: str | None = None,
) -> RepositoryRefNotFound:
    """The refusal of a `repository_ref` the forge does not have.

    `task_id` only for a task of the CALLER's tenant (the caller checks):
    another tenant's task is never named, so a guess at its id learns only the
    generic sentence. `pull_state` is "merged", "closed" or "open" when the
    pull request was read, None when it was not.
    """
    base = default_branch or "the default branch"
    detail: dict[str, Any] = {"field": "repository_ref", "repository_ref": value,
                              "repository": repository}
    if task_id is None:
        return RepositoryRefNotFound(
            f"repository_ref {value!r} is neither a branch nor a tag of {repository}; "
            f"it may have been deleted. Send an existing branch or tag, a full "
            f"{_FULL_SHA_LENGTH}-character sha, or no repository_ref to start from {base}.",
            detail=detail,
        )
    detail["task_id"] = task_id
    if pull_request is None:
        what = f"the branch task {task_id!r} published"
    else:
        detail["pull_request"] = pull_request
        state = {"merged": "was merged", "closed": "was closed"}.get(pull_state or "")
        what = (f"the branch of task {task_id!r}'s pull request #{pull_request}"
                + (f", which {state}" if state else ""))
    return RepositoryRefNotFound(
        f"repository_ref {value!r} is {what}, and it is no longer a branch of "
        f"{repository} (GitHub deletes a merged pull request's branch when the "
        f"repository is set to). To add to that work, send continues_task: "
        f"{task_id!r} instead of repository_ref; to build on what was merged, "
        f"start from {base} (no repository_ref).",
        detail=detail,
    )


# --------------------------------------------------------------------------
# An issue reference: `owner/repo#N` or the issue's URL (#454)
# --------------------------------------------------------------------------
#
# An issue run (`swarm_api.issueruns`) and the console's issue preview both
# start from one string a person typed or pasted. Two spellings are accepted
# because both are what people have: the short form GitHub itself renders, and
# the URL in the browser's address bar.
#
# THE REPOSITORY GOES THROUGH `check_repository_url`, the one rule every other
# repository on this platform passes: the planner task and the compiled
# workflow clone it, so it must be a URL a task would have accepted. A pasted
# URL is checked AS PASTED first, so one carrying a token before `@` is refused
# by that rule's own words (which name no part of the value) instead of being
# taken apart.
#
# A PULL REQUEST IS NOT AN ISSUE. GitHub numbers both from one sequence, so
# `/pull/12` parsed as "issue 12" would plan work against a pull request's
# description. The URL says which it is, so the refusal says so; `owner/repo#N`
# cannot, and the preview's forge read refuses it there (`forge.py`).

#: The one forge an issue reference may name. The worker's issue fetch and the
#: preview's client both read GitHub's API and nothing else.
ISSUE_FORGE_HOSTS = ("github.com", "www.github.com")

#: The catalogue's own ceiling on the `issue` runner input (contract request
#: 28): a reference past it names an issue no planner task could be given.
MAX_ISSUE_NUMBER = 999_999

_ISSUE_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_ISSUE_REPO = r"[A-Za-z0-9._-]{1,100}"
_SHORT_ISSUE_REF = re.compile(rf"^({_ISSUE_OWNER})/({_ISSUE_REPO})#([0-9]{{1,7}})$")
_ISSUE_URL_PATH = re.compile(
    rf"^/({_ISSUE_OWNER})/({_ISSUE_REPO})/(issues|pull|pulls)/([0-9]{{1,7}})/?$"
)


class PullRequestReference(ValueError):
    """The reference names a pull request. Its own type so the preview can
    answer `is_pull_request` rather than a generic refusal."""


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repository_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"{self.repository_url}/issues/{self.number}"

    @property
    def short(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.short,
            "owner": self.owner,
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "repository_url": self.repository_url,
        }


def _issue_repo_name(repo: str) -> str:
    name = repo[:-4] if repo.lower().endswith(".git") else repo
    if name in ("", ".", "..") or name.startswith("."):
        raise ValueError("issue must name a repository: owner/repo#N")
    return name


def _issue_number(raw: str) -> int:
    number = int(raw)
    if number < 1 or number > MAX_ISSUE_NUMBER:
        raise ValueError(f"issue number must be between 1 and {MAX_ISSUE_NUMBER}")
    return number


def parse_issue_ref(value: str) -> IssueRef:
    """`owner/repo#N` or `https://github.com/owner/repo/issues/N` -> IssueRef.

    Raises ValueError (pydantic's 422 naming the field), and
    `PullRequestReference` for a `/pull/N` URL. The value is never echoed: a
    URL can carry a credential, and the rule that refuses it names none.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("issue is required: owner/repo#N or the issue's URL")
    short = _SHORT_ISSUE_REF.match(text)
    if short is not None:
        owner, repo, number = short.group(1), _issue_repo_name(short.group(2)), short.group(3)
    else:
        # As pasted, before anything is taken apart: the scheme rule and the
        # credential rule, in that rule's own words.
        check_repository_url(text)
        if not text.startswith("https://"):
            raise ValueError("an issue URL must be https://github.com/<owner>/<repo>/issues/<N>")
        rest = text[len("https://"):]
        cut = min([at for at in (rest.find("?"), rest.find("#")) if at >= 0], default=len(rest))
        authority, _, path = rest[:cut].partition("/")
        if authority.lower() not in ISSUE_FORGE_HOSTS:
            raise ValueError(
                "an issue URL must be on github.com: the planner and the preview "
                "read GitHub's API and no other forge"
            )
        matched = _ISSUE_URL_PATH.match("/" + path)
        if matched is None:
            raise ValueError(
                "issue must be owner/repo#N or https://github.com/<owner>/<repo>/issues/<N>"
            )
        if matched.group(3) != "issues":
            raise PullRequestReference(
                f"#{int(matched.group(4))} is a pull request, not an issue; "
                "an issue run plans work from an issue"
            )
        owner, repo, number = matched.group(1), _issue_repo_name(matched.group(2)), matched.group(4)
    ref = IssueRef(owner=owner, repo=repo, number=_issue_number(number))
    # The repository every task of the run clones passes the rule every other
    # repository does.
    check_repository_url(ref.repository_url)
    return ref


class DispatchOptionError(ValidationFailed):
    """422 `invalid_dispatch`: a refused dispatch option, or a reserved metadata key.

    The `metadata.input_from` (#151) and `metadata.expected_outputs` (#149)
    reservations answer with this code too, rather than new ones. The owner's
    instruction was "reserved, like dispatch", and a caller branching on the
    code should read every reservation the same way.
    `detail.reserved_metadata_keys` says which keys it was.
    """

    code = "invalid_dispatch"


@dataclass(frozen=True)
class DispatchOptions:
    """The resolved per-dispatch integration contract for ONE task."""

    strategy: str = DEFAULT_STRATEGY
    carrier: str = DEFAULT_CARRIER
    #: "integrator" or "contributor" on an `integrate` workflow, None otherwise.
    #: A standalone task and every task of a `collect`/`direct-pr` dispatch has
    #: no role, because there is only one kind of participant.
    role: str | None = None
    #: The task ids whose work the integrator must apply, in topological order.
    #: Set on the integrator only. It is stored rather than re-derived because
    #: `task.depends_on` holds DIRECT parents only -- in a chain a -> b -> c the
    #: integrator c never names a -- and walking the DAG in the worker would be
    #: a second implementation of the ordering this module already computed.
    integrates: tuple[str, ...] = ()
    #: The task whose `swarm/<task-id>` branch this dispatch clones and pushes
    #: to, instead of a branch of its own (#263). Set only on a one-step
    #: `direct-pr` workflow, by `continuation.resolve_continuation`, which has
    #: checked it is the caller's own task and resolved it to the ROOT of any
    #: chain of continuations. A task id, never a branch name: the worker
    #: derives the branch with the prefix it pushed under.
    continues: str | None = None
    #: The upstream TASK id whose pushed branch this step clones instead of the
    #: workflow's `repository_ref` (#264). The worker derives the branch from
    #: the id with its own prefix, as it does for `integrates`, so nothing here
    #: is a ref name.
    builds_on: str | None = None
    #: The verdict gate (#264): the upstream TASK id whose staged verdict file
    #: decides whether this step's agent runs, and the verdicts that run it.
    gate_task_id: str | None = None
    gate_verdicts: tuple[str, ...] = ()
    #: The step's `allow_empty_diff` (owner decision, 2026-10-05): an empty
    #: diff ends it SUCCEEDED with `result_summary.no_change` instead of
    #: failing on `empty_diff`. Written only when true, inside the signed block.
    allow_empty_diff: bool = False
    #: The workflow's label (`metadata.unit`, else `metadata.title`), on a
    #: GATED step only: what a MERGE verdict, which runs no fix agent, titles
    #: the pull request with when the implementer wrote no `pr-title.txt`.
    #: Here and not read from `metadata` by the worker, because the worker
    #: reads no key the spec signature does not cover.
    pr_label: str | None = None
    #: The tenant's wave epic (#638), on a GATED step only: the issue the
    #: worker files the review verdict's MINOR findings on, read from the
    #: tenant's `findings_epic` setting at submission. Here, inside the signed
    #: block, so no agent of the tenant can point the worker at another issue.
    findings_epic: int | None = None
    #: A merge step's conflict fix round (#1321): set by `submit_workflow` on
    #: a continuation whose metadata carries `CONFLICT_ROUND_METADATA_KEY`.
    #: Written only when true, inside the signed block.
    conflict_round: bool = False
    #: A CI fix's red head (#2276), `{"pull_request": int, "head_sha": str}`:
    #: set by `submit_workflow` on a continuation whose `metadata.ci_fix`
    #: names a head. Written only when set, inside the signed block.
    ci_fix_head: dict[str, Any] | None = None
    #: `(upstream TASK id, upstream STEP id)` for every `input_from` entry of a
    #: step that opted in to `input_layout: "by_parent"` (#75), else empty. The
    #: worker sees only task ids in `metadata.input_from`; this is how it learns
    #: the step id to stage each file under. In the dispatch block because the
    #: spec signature covers it (`swarm_common.specsign.SIGNED_METADATA_KEYS`)
    #: and because `input_from`'s `{task id: filename}` shape is read as such by
    #: the UI and the masking.
    input_parents: tuple[tuple[str, str], ...] = ()
    #: This step's part in a `single-pr` chain (`PR_ROLES`), None under every
    #: other strategy (#295).
    pr_role: str | None = None
    #: The author's TASK id, on a `single-pr` reader or amender: the worker
    #: derives the branch it clones (and an amender pushes to) as
    #: `swarm/<pr_author>`, never reading a branch name.
    pr_author: str | None = None
    #: The review's TASK id, on the `post-verdict` step only. It derives the
    #: verdicts-prefix path of review.json from this and its own workflow id,
    #: which the spec signature makes facts (docs/merge-step.md §4.3).
    verdict_source: str | None = None
    #: `(MERGES_KEYS key, upstream TASK id)`, on the `merge` step only: the
    #: tasks whose `result_summary.git` and signed specs it checks (§4.1, §4.2).
    merges: tuple[tuple[str, str], ...] = ()
    #: `(key, value)` of the `merge_target` block, on a `merge` step outside
    #: `single-pr` only (contract request 47): `pull_request`, the TASK id
    #: whose pull request it merges, and -- when the workflow has a review --
    #: `review`, that review's task id, and `verdict_file`, the file it stages
    #: from it. Inside the dispatch block, so the spec signature covers it.
    #: For a `merge_pr` workflow (#352) it names no task: `number` and
    #: `head_sha`, the pull request and the head the caller named, instead.
    merge_target: tuple[tuple[str, Any], ...] = ()
    #: An `await-input` step's declaration (docs/design/human-steps.md §4.1),
    #: as `resolve_await_input` normalised it, on that step only. Inside the
    #: dispatch block, so the spec signature covers what the person is asked,
    #: how long the step waits and what happens at expiry. Not hashed: a map.
    await_input: Mapping[str, Any] | None = field(default=None, hash=False)
    #: `(number, task id)` of the existing pull request this step's lane
    #: publishes onto instead of opening one (#2183), as
    #: `publishtopr.resolve` read it from GitHub at submission; empty on every
    #: other dispatch. The worker derives the branch from the task id.
    publish_to_pr: tuple[tuple[str, Any], ...] = ()

    @property
    def needs_repository(self) -> bool:
        """True when this dispatch has to push somewhere to mean anything.

        `direct-pr` and `integrate` both end in a pull request, and
        `carrier: branches` pushes intermediate work. All three need a
        repository, and a dispatch without one would run to completion and
        quietly produce nothing -- which is the failure this platform keeps
        hitting, so it is refused at submission instead.
        """
        return self.strategy in _NEEDS_REPOSITORY_STRATEGIES or self.carrier == "branches"

    def with_role(self, role: str, integrates: Sequence[str] = ()) -> "DispatchOptions":
        return replace(self, role=role, integrates=tuple(integrates))

    def with_routing(
        self,
        *,
        builds_on: str | None,
        gate_task_id: str | None,
        gate_verdicts: Sequence[str] = (),
        allow_empty_diff: bool = False,
        pr_label: str | None = None,
        findings_epic: int | None = None,
    ) -> "DispatchOptions":
        """This step's `builds_on`, verdict gate and empty-diff permission,
        already resolved to task ids. `pr_label` and `findings_epic` are kept
        only on a gated step."""
        return replace(
            self,
            builds_on=builds_on,
            gate_task_id=gate_task_id,
            gate_verdicts=tuple(gate_verdicts) if gate_task_id else (),
            allow_empty_diff=bool(allow_empty_diff),
            pr_label=pr_label if gate_task_id and pr_label else None,
            findings_epic=findings_epic if gate_task_id and findings_epic else None,
        )

    def with_await_input(self, block: Mapping[str, Any] | None) -> "DispatchOptions":
        """This `await-input` step's normalised declaration; None on every other step."""
        return replace(self, await_input=block)

    def with_input_parents(self, parents: Mapping[str, str]) -> "DispatchOptions":
        """This step's `{upstream task id: upstream step id}`, for a `by_parent` step."""
        return replace(self, input_parents=tuple(sorted(parents.items())))

    def with_pr_role(
        self,
        pr_role: str,
        *,
        pr_author: str | None = None,
        verdict_source: str | None = None,
        merges: Mapping[str, str] | None = None,
    ) -> "DispatchOptions":
        """This `single-pr` step's role and the upstream task ids it needs."""
        return replace(
            self,
            pr_role=pr_role,
            pr_author=pr_author,
            verdict_source=verdict_source,
            merges=tuple((k, merges[k]) for k in MERGES_KEYS if k in (merges or {})),
        )

    def with_merge_target(
        self,
        *,
        pull_request: str | None = None,
        review: str | None = None,
        verdict_file: str | None = None,
        base: str | None = None,
        number: int | None = None,
        head_sha: str | None = None,
        pull_request_workflow: str | None = None,
        merge_mode: str | None = None,
    ) -> "DispatchOptions":
        """The `merge` step's target, already resolved to task ids. No role:
        it runs no agent, clones nothing and is integrated by nobody.

        `base` is the default branch the tenant registered the repository
        with, absent when it registered none (lane MS1): the worker refuses a
        pull request on any other base `base_not_default` from this, inside
        the signed block, without reading the registry itself.

        A `merge_pr` workflow (#352) names the pull request by `number` and
        the `head_sha` the caller named instead of by the task that opened
        it, because no task did; it has no review.

        A merge-only continuation names `pull_request_workflow` too: the
        continued task's own workflow, which the worker verifies that task's
        signed spec against instead of the merge's (#900). It has no review.

        `merge_mode` is the workspace's effective merge mode for this
        repository (TRAIN-WS, `mergesettings.effective_mode`), read at
        submission. Only `train` is written: direct is the absence of the
        key, so a workspace that never chose trains stores exactly the block
        it stored before, and the worker merges as it always has."""
        if merge_mode not in (None, MERGE_MODE_DIRECT, MERGE_MODE_TRAIN):
            raise ValueError(f"unknown merge mode {merge_mode!r}")
        train = [("merge_mode", MERGE_MODE_TRAIN)] if merge_mode == MERGE_MODE_TRAIN else []
        target: list[tuple[str, Any]]
        if pull_request is None:
            if number is None or head_sha is None:
                raise ValueError("a merge target names a task, or a number and a head sha")
            target = [("number", number), ("head_sha", head_sha)]
            if base:
                target.append(("base", base))
            return replace(self, role=None, integrates=(),
                           merge_target=tuple(target + train))
        target = [("pull_request", pull_request)]
        if pull_request_workflow is not None:
            if review is not None or verdict_file is not None:
                raise ValueError("a continued pull request's merge target names no review")
            target.append(("pull_request_workflow", pull_request_workflow))
        if review is not None and verdict_file is not None:
            target += [("review", review), ("verdict_file", verdict_file)]
        if base:
            target.append(("base", base))
        return replace(self, role=None, integrates=(), merge_target=tuple(target + train))

    def to_metadata(self) -> dict[str, Any]:
        """The `task.metadata["dispatch"]` block, exactly as the worker reads it."""
        block: dict[str, Any] = {"strategy": self.strategy, "carrier": self.carrier}
        if self.role is not None:
            block["role"] = self.role
        if self.integrates:
            block["integrates"] = list(self.integrates)
        if self.continues:
            block["continues"] = self.continues
        # Absent unless the step asked, so a workflow that uses neither stores
        # exactly the block it stored before #264.
        if self.builds_on:
            block["builds_on"] = self.builds_on
        if self.gate_task_id:
            block["verdict_gate"] = {
                "task_id": self.gate_task_id,
                "verdict_in": list(self.gate_verdicts),
            }
        # Absent unless asked for (2026-10-05), so every other step stores
        # exactly the block it stored before. The worker spells both keys in
        # `agent_worker.expected_outputs`.
        if self.allow_empty_diff:
            block["allow_empty_diff"] = True
        if self.pr_label:
            block["pr_label"] = self.pr_label
        # Absent unless the tenant set an epic (#638). The worker spells the
        # key `agent_worker.findings_epic.EPIC_FIELD`.
        if self.findings_epic:
            block["findings_epic"] = self.findings_epic
        # Absent unless the step opted in (#75), so a step that did not stores
        # exactly the block it stored before. The worker spells the key
        # `agent_worker.inputs.PARENTS_KEY`.
        if self.input_parents:
            block["input_parents"] = dict(self.input_parents)
        # Absent outside `single-pr` (#295), so every other strategy stores
        # exactly the block it stored before. Inside `dispatch`, so the spec
        # signature covers all four (`swarm_common.specsign.SIGNED_METADATA_KEYS`).
        if self.pr_role is not None:
            block["pr_role"] = self.pr_role
        if self.pr_author:
            block["pr_author"] = self.pr_author
        if self.verdict_source:
            block["verdict_source"] = {"review": self.verdict_source}
        if self.merges:
            block["merges"] = dict(self.merges)
        # Absent on every step but a merge step (contract request 47), so
        # every other step stores exactly the block it stored before.
        if self.merge_target:
            block[MERGE_TARGET_FIELD] = dict(self.merge_target)
        # Absent on every step but a conflict fix round (#1321), so every
        # other step stores exactly the block it stored before.
        if self.conflict_round:
            block[CONFLICT_ROUND_FIELD] = True
        # Absent on every step but a CI fix that names its red head (#2276),
        # so every other step stores exactly the block it stored before.
        if self.ci_fix_head:
            block[CI_FIX_HEAD_FIELD] = {
                "pull_request": self.ci_fix_head["pull_request"],
                "head_sha": self.ci_fix_head["head_sha"],
            }
        # Absent on every step but an `await-input` step (HW-API-SUBMIT), so
        # every other step stores exactly the block it stored before.
        if self.await_input is not None:
            block[AWAIT_INPUT_FIELD] = json.loads(json.dumps(self.await_input))
        # Absent unless the spec named a pull request (#2183), so every other
        # dispatch stores exactly the block it stored before. The worker
        # spells the key `agent_worker.publishpr.FIELD`.
        if self.publish_to_pr:
            block["publish_to_pr"] = dict(self.publish_to_pr)
        return block


#: The metadata keys a workflow's label is read from, in order: `unit` is
#: what the MCP bridge sends a spec's `label` as, `title` its `title`
#: (`swarm_mcp.workflows.stored_names`).
WORKFLOW_LABEL_KEYS = ("unit", "title")
#: GitHub's own title field holds 256 characters; the worker cuts there too.
WORKFLOW_LABEL_MAX_CHARS = 256


def workflow_label(metadata: Mapping[str, Any] | None) -> str | None:
    """The workflow's label as one line of text, or None when it has none.

    The first of `WORKFLOW_LABEL_KEYS` that holds a non-blank string, with its
    whitespace runs folded to one space and cut to
    `WORKFLOW_LABEL_MAX_CHARS`. A caller's text: the worker scrubs it, refuses
    it if it carries a task id or attribution, and neutralises every mention
    before it titles anything, as it does for an agent's `pr-title.txt`.

    A label of `ready` is a title like any other, merge step or not (owner
    decision 2026-10-06, #352): it never becomes a GitHub label, so it
    cannot reach `auto-merge.yml`.
    """
    for key in WORKFLOW_LABEL_KEYS:
        value = (metadata or {}).get(key)
        if isinstance(value, str):
            text = " ".join(value.split())
            if text:
                return text[:WORKFLOW_LABEL_MAX_CHARS]
    return None


def _accepted_value(name: str, value: Any, accepted: tuple[str, ...], detail_key: str) -> str:
    text = (value or "").strip() if isinstance(value, str) else value
    if text not in accepted:
        raise DispatchOptionError(
            f"unknown {name} {value!r}; accepted values are " + ", ".join(accepted),
            detail={detail_key: list(accepted)},
        )
    return text


#: Why each reserved key is refused, and what the caller should send instead.
#: A refusal that says only "reserved" leaves the caller guessing at the one
#: part they can act on.
_RESERVED_BECAUSE = {
    DISPATCH_METADATA_KEY: (
        f"metadata.{DISPATCH_METADATA_KEY} is reserved: it records the strategy "
        "and carrier this service resolved for the dispatch. Use the top-level "
        "`strategy` and `carrier` fields instead."
    ),
    INPUT_FROM_METADATA_KEY: (
        f"metadata.{INPUT_FROM_METADATA_KEY} is reserved: it is set by workflow "
        "expansion, which rewrites a step's `input_from` to the ids of the "
        "upstream tasks it creates. To stage an upstream step's artifact, submit "
        "a workflow (POST /v1/workflows) and declare `input_from` on the step "
        "that needs it, with the upstream step in its `depends_on`."
    ),
    EXPECTED_OUTPUTS_METADATA_KEY: (
        f"metadata.{EXPECTED_OUTPUTS_METADATA_KEY} is reserved: it is set by "
        "workflow expansion, which records on each upstream step the files its "
        "dependants' `input_from` stage from it. To have an agent told which "
        "files a later step needs, submit a workflow (POST /v1/workflows) and "
        "declare `input_from` on the step that needs them."
    ),
    STARTUP_REFUNDS_METADATA_KEY: (
        f"metadata.{STARTUP_REFUNDS_METADATA_KEY} is reserved: it is set only by "
        "the reconciler, to count attempts refunded because they ended before "
        "their runner started (#67). There is no caller-facing equivalent to "
        "set; drop the key from metadata."
    ),
    CHILD_REQUEST_ID_METADATA_KEY: (
        f"metadata.{CHILD_REQUEST_ID_METADATA_KEY} is reserved: it is set only on a "
        "child task, from the request id its parent's agent chose, by the route "
        "its parent's worker calls. Drop the key from metadata."
    ),
    CHILD_AWAIT_RESUMES_METADATA_KEY: (
        f"metadata.{CHILD_AWAIT_RESUMES_METADATA_KEY} is reserved: it is set only by "
        "the worker, to count the awaits of a parent's children whose attempt "
        "was refunded. Drop the key from metadata."
    ),
    CHILD_CASCADE_METADATA_KEY: (
        f"metadata.{CHILD_CASCADE_METADATA_KEY} is reserved: it is set only on a "
        "child task cancelled because of its parent. Drop the key from metadata."
    ),
    MERGE_WAIT_METADATA_KEY: (
        f"metadata.{MERGE_WAIT_METADATA_KEY} is reserved: it is set only on a merge "
        "step waiting for its pull request's checks, by the worker that parked it "
        "and the tick that wakes it. Drop the key from metadata."
    ),
    SCHEDULE_METADATA_KEY: (
        f"metadata.{SCHEDULE_METADATA_KEY} is reserved: it is set only by a "
        "schedule's firing, on the work that firing created (docs/schedules.md "
        "§2.7). Drop the key from metadata."
    ),
    DISK_EVICTIONS_METADATA_KEY: (
        f"metadata.{DISK_EVICTIONS_METADATA_KEY} is reserved: it is set only by "
        "the reconciler, to count attempts lost to the pod passing its own disk "
        "limit (#893). Drop the key from metadata."
    ),
    START_PROBLEM_METADATA_KEY: (
        f"metadata.{START_PROBLEM_METADATA_KEY} is reserved: it is set only by "
        "the reconciler, to say why the task's pod is not starting. Drop the key "
        "from metadata."
    ),
    HUMAN_WAIT_RESUMES_METADATA_KEY: (
        f"metadata.{HUMAN_WAIT_RESUMES_METADATA_KEY} is reserved: it is set only by "
        "the worker, to count the live-browser hand-off parks whose attempt was "
        "refunded. Drop the key from metadata."
    ),
    HUMAN_READY_METADATA_KEY: (
        f"metadata.{HUMAN_READY_METADATA_KEY} is reserved: it is set only by "
        "POST /v1/tasks/{task_id}/human-ready, when a person says they are ready to "
        "sign in. Drop the key from metadata."
    ),
    HUMAN_HANDOFFS_METADATA_KEY: (
        f"metadata.{HUMAN_HANDOFFS_METADATA_KEY} is reserved: it counts the "
        "live-browser hand-offs an attempt asked for, written only by the worker's "
        "hand-off route. Drop the key from metadata."
    ),
    HUMAN_RETURNED_METADATA_KEY: (
        f"metadata.{HUMAN_RETURNED_METADATA_KEY} is reserved: it records a person's "
        "return of the live browser during an attempt, written only by "
        "POST /v1/tasks/{task_id}/live/control/return. Drop the key from metadata."
    ),
    RELEASE_WAIT_RESUMES_METADATA_KEY: (
        f"metadata.{RELEASE_WAIT_RESUMES_METADATA_KEY} is reserved: it is set only "
        "by the worker, to count the release waits whose attempt was refunded. "
        "Drop the key from metadata."
    ),
    QUOTA_PARK_RESUMES_METADATA_KEY: (
        f"metadata.{QUOTA_PARK_RESUMES_METADATA_KEY} is reserved: it is set only "
        "by the worker's quota park, to count the provider-quota parks whose "
        "attempt was refunded. Drop the key from metadata."
    ),
    OWNER_ANSWERS_METADATA_KEY: (
        f"metadata.{OWNER_ANSWERS_METADATA_KEY} is reserved: it holds the owner's "
        "answers to the agent's questions, written only by "
        "POST /v1/tasks/{task_id}/answers. Drop the key from metadata."
    ),
    QUESTION_WAIT_METADATA_KEY: (
        f"metadata.{QUESTION_WAIT_METADATA_KEY} is reserved: it records the "
        "blocking questions an agent parked on, written only by the platform "
        "while the task waits for its owner. Drop the key from metadata."
    ),
    QUESTION_WAITS_EARLIER_METADATA_KEY: (
        f"metadata.{QUESTION_WAITS_EARLIER_METADATA_KEY} is reserved: it keeps "
        "an agent's earlier question waits with the answers each was given, "
        "written only by the platform. Drop the key from metadata."
    ),
    FORGE_SOURCE_METADATA_KEY: (
        f"metadata.{FORGE_SOURCE_METADATA_KEY} is reserved: it says whose GitHub "
        "credential a service submission runs with, written only by submission. "
        "Drop the key from metadata."
    ),
    AWAIT_INPUT_METADATA_KEY: (
        f"metadata.{AWAIT_INPUT_METADATA_KEY} is reserved: it records an "
        "`await-input` step's wait and the answer it was given, written only by "
        "the platform. Declare the wait as the step's own `await_input` field "
        "(POST /v1/workflows) instead."
    ),
    AFTER_PRS_METADATA_KEY: (
        f"metadata.{AFTER_PRS_METADATA_KEY} is reserved: it records the pull "
        "requests a workflow waits for, written from the spec's `after` and then "
        "only by the platform's wake tick. Name them in `after` instead, e.g. "
        '"after": {"pr": 41}.'
    ),
    QA_TARGET_METADATA_KEY: (
        f"metadata.{QA_TARGET_METADATA_KEY} is reserved: it names the QA target a "
        "browser step drives, written only by the platform when a target is run. "
        "Start the run with POST /v1/qa-targets/{id}:run instead."
    ),
}


#: The one `metadata.schedule` value the CURRENT submission may carry, set
#: only by `schedule_mark_allowed`. None everywhere else, which is every
#: request a caller makes.
_SCHEDULE_MARK: ContextVar[Mapping[str, Any] | None] = ContextVar("schedule_mark", default=None)


@contextmanager
def schedule_mark_allowed(mark: Mapping[str, Any]) -> Iterator[None]:
    """Let THIS submission carry `metadata.schedule == mark`, and nothing else.

    WHY A SCOPED VALUE AND NOT ORDER. Every other reserved key gets past
    `reject_reserved_metadata` by order: `SubmissionService` adds it after
    the caller's metadata was checked. The schedule's firing submits through
    the SAME service methods a person uses (docs/schedules.md §2.7) and
    service.py is not lane S2's file, so the mark has to travel in the
    spec's metadata. It is inside the stored task from the one write that
    creates it -- which is what makes the finisher's
    lookup by `firing_id` (§2.2) sound: work created and not yet recorded
    is found, never created twice.

    THE EXEMPTION IS THE EXACT VALUE. Only metadata whose `schedule` equals
    `mark` passes, and only inside this block, which `schedulefire` alone
    enters around its own submission call. A request never runs inside it:
    a ContextVar set in one call is not visible to another request's thread.
    Anything else under the key is refused as before.
    """
    token = _SCHEDULE_MARK.set(dict(mark))
    try:
        yield
    finally:
        _SCHEDULE_MARK.reset(token)


#: The one `metadata.qa_target` value the CURRENT submission may carry, set
#: only by `qa_target_mark_allowed`. A separate variable from the schedule's,
#: so neither exemption admits the other's key.
_QA_TARGET_MARK: ContextVar[Mapping[str, Any] | None] = ContextVar("qa_target_mark", default=None)


@contextmanager
def qa_target_mark_allowed(mark: Mapping[str, Any]) -> Iterator[None]:
    """Let THIS submission carry `metadata.qa_target == mark`, and nothing else.

    The same scoped exemption as `schedule_mark_allowed`, for the same reason:
    `:run` (routes/qatargets.py `run_target`) submits through the SAME
    `submit_workflow` a person uses, with the mark on each browser step's own
    metadata, so the mark travels in the spec and is in each task from the one
    write that creates it. Only the exact value passes, only inside this
    block, which two callers enter, each around its own submission call:
    routes/qatargets.py `run_target` and a visual-qa schedule firing's
    schedtypes/visual_qa.py `create`. A ContextVar set in one request is not
    visible to another's.
    """
    token = _QA_TARGET_MARK.set(dict(mark))
    try:
        yield
    finally:
        _QA_TARGET_MARK.reset(token)


def reject_reserved_metadata(metadata: dict[str, Any]) -> None:
    """Refuse a caller's metadata that names a key this service writes.

    `metadata.dispatch`: accepting it would let a caller write a role, an
    integrates list, or a strategy that never passed the checks below, straight
    into the document the worker acts on.

    `metadata.input_from` (owner decision on #151, 2026-09-25): the worker
    stages whatever it names. Accepted from a caller, it arrived with no
    dependency edge, so nothing guaranteed the upstream had run, and a bad
    declaration was refused only at run time, after the task had been admitted
    and had held capacity. Workflow expansion is the only writer, because only
    it has checked the declaration against the DAG. ANY value is refused, `{}`
    and None included: the key is reserved, not validated.

    `metadata.expected_outputs` (owner decision on #149, point (d)): the worker
    tells the agent, in the platform's voice, that later steps of its workflow
    need these files. Accepted from a caller, that would be said about a task
    no step stages from. Reserved, not validated, like `input_from`.

    HOW THE SERVICE'S OWN WRITES GET PAST THIS: ORDER, NOT A FLAG. This runs on
    the caller's metadata only, before any of the keys is added. `_build_task`
    calls it, then adds `dispatch`. `submit_workflow` calls it on the workflow's
    own metadata; it adds `input_from` to a step's task after `_build_task` has
    returned, and `expected_outputs` to every built task just before the one
    store write. Nothing a caller sends can reach the store under any of them.

    Every reserved key present is named in the detail, in RESERVED_METADATA_KEYS
    order, so a caller who sent several learns about all of them from one
    refusal.
    """
    present = [
        key for key in (
            RESERVED_METADATA_KEYS
            + REFUSED_UNWRITTEN_METADATA_KEYS
            + REFUSED_AGENT_TEXT_METADATA_KEYS
        )
        if key in metadata
    ]
    for key, exempt in ((SCHEDULE_METADATA_KEY, _SCHEDULE_MARK),
                        (QA_TARGET_METADATA_KEY, _QA_TARGET_MARK)):
        allowed = exempt.get()
        if allowed is not None and key in metadata and metadata[key] == allowed:
            present.remove(key)
    if not present:
        return
    raise DispatchOptionError(
        " ".join(_RESERVED_BECAUSE[key] for key in present),
        detail={"reserved_metadata_keys": present},
    )


def resolve_dispatch_options(
    *,
    strategy: Any,
    carrier: Any,
    scale: str,
    repository_url: str | None,
) -> DispatchOptions:
    """Validate the pair and return it, or refuse with the accepted values named.

    `scale` is "task" or "workflow" and decides one rule only: `integrate`
    names a FINAL STEP that receives the other steps' patches, so a standalone
    task -- and every task in a batch, which is N independent tasks with no
    dependencies between them -- has nothing to integrate. That is a refusal
    rather than a quiet downgrade to `collect`, because a caller who asked for
    one pull request and silently got three has been lied to.

    Whether the tenant's token can push for `carrier: branches` is not asked
    here, nor anywhere in swarm-api: answering it reads the tenant's git
    secret, and exactly one identity -- the tenant's worker GSA -- may read
    that secret (terraform/modules/secret_manager). The worker asks the forge
    before the agent runs and fails the attempt `forge_read_only` (D13).
    """
    options = DispatchOptions(
        strategy=_accepted_value("strategy", strategy, DISPATCH_STRATEGIES,
                                 "accepted_strategies"),
        carrier=_accepted_value("carrier", carrier, DISPATCH_CARRIERS,
                                "accepted_carriers"),
    )
    if options.strategy == SINGLE_PR and scale != "workflow":
        raise DispatchOptionError(
            "strategy 'single-pr' names a chain of steps -- an author, the steps "
            "that read or amend its pull request, and the step that merges it -- "
            "and a single task (or a batch, whose tasks are independent) is not "
            "one. Submit a workflow, or choose 'collect' or 'direct-pr'.",
            detail={"scale": scale, "accepted_strategies": list(DISPATCH_STRATEGIES)},
        )
    if options.strategy == "integrate" and scale != "workflow":
        raise DispatchOptionError(
            "strategy 'integrate' has nothing to integrate here: it names a final "
            "step that receives the other steps' patches, and a single task (or a "
            "batch, whose tasks are independent) has no other steps. Submit a "
            "workflow whose last step depends on the rest, or choose "
            "'collect' or 'direct-pr'.",
            detail={"scale": scale, "accepted_strategies": list(DISPATCH_STRATEGIES)},
        )
    if options.needs_repository and not (repository_url or "").strip():
        raise DispatchOptionError(
            f"strategy {options.strategy!r} with carrier {options.carrier!r} has to "
            "push, so it needs a repository_url on the "
            + ("workflow" if scale == "workflow" else "task")
            + ". Without one the agent would run to completion and publish nothing.",
            detail={
                "strategy": options.strategy,
                "carrier": options.carrier,
                "missing": "repository_url",
            },
        )
    return options


def resolve_integrator_step(steps: Sequence[StepSpec], rereview: str | None = None) -> str:
    """The step that integrates the others: the workflow's single sink.

    Call this only AFTER `validate_dag`, which has already rejected cycles and
    dangling dependencies.

    Exactly one step may be final -- one that nothing else depends on -- because
    `integrate` promises ONE pull request and a second final step would open a
    second one. That single check is also sufficient to prove every other step
    feeds the integrator: from any step, follow its dependents; the graph is
    finite and acyclic so the walk ends at a step nothing depends on, and there
    is only one of those. Transitive feeding is what matters, so a chain
    a -> b -> c is a legal `integrate` workflow with c as the integrator.

    `rereview` is the re-review step swarm-api appended under
    `metadata.merge` "on_merge_verdict" (`rereview_step_for`), by id: it
    reviews the integrator's pushed head, so it comes after the integrator
    and is not one of the steps the integrator integrates.
    """
    # A `merge` step (contract request 47) comes after the integrator and
    # integrates nothing: the integrator is the sink of the OTHER steps.
    # `plan_merge` holds the merge step to depending on it.
    steps = [
        step for step in steps
        if _worker_action(step) is not WorkerAction.MERGE and step.step_id != rereview
    ]
    if len(steps) < 2:
        raise DispatchOptionError(
            "strategy 'integrate' needs something to integrate: it names a final "
            f"step that receives the OTHER steps' patches, and this workflow has "
            f"{len(steps)} step(s). Add the steps whose work should be merged, or "
            "choose 'collect' or 'direct-pr'.",
            detail={"steps": len(steps)},
        )
    depended_on = {dep for step in steps for dep in step.depends_on}
    terminals = [step.step_id for step in steps if step.step_id not in depended_on]
    if len(terminals) != 1:
        raise DispatchOptionError(
            "strategy 'integrate' opens ONE pull request, so exactly one step must be "
            f"final -- the one every other step feeds. This workflow has "
            f"{len(terminals)} steps that nothing depends on: " + ", ".join(terminals)
            + ". Make the integrating step depend on them.",
            detail={"terminal_steps": terminals},
        )
    return terminals[0]


def _ancestors(steps: Sequence[StepSpec]) -> dict[str, set[str]]:
    """Every step's transitive upstreams. Call only after `validate_dag`."""
    parents = {step.step_id: set(step.depends_on) for step in steps}
    found: dict[str, set[str]] = {}

    def walk(step_id: str) -> set[str]:
        if step_id not in found:
            found[step_id] = set()
            seen: set[str] = set()
            for parent in parents.get(step_id, ()):
                seen |= {parent} | walk(parent)
            found[step_id] = seen
        return found[step_id]

    for step in steps:
        walk(step.step_id)
    return found


def refuse_worker_action_outside_single_pr(
    profile: RunnerProfile, strategy: str, *, step_id: str | None = None
) -> None:
    """Where a worker-action profile may run. Refuses everywhere else.

    `post-verdict` runs only inside `single-pr`: it acts on the one pull
    request that chain opens, after the steps that order it.

    `merge` (contract request 47, owner decisions 2026-10-04) runs as the
    final step of a WORKFLOW that opens a pull request -- `direct-pr`,
    `integrate`, or `single-pr`'s own chain -- and nowhere else: a standalone
    task, or a `collect` workflow, has no pull request to merge.
    `plan_merge` then checks where in the workflow it sits. Called for a task
    by `_build_task` and for a workflow step by `validate_step_routing`.
    """
    if profile.worker_action is None or strategy == SINGLE_PR:
        return
    where = f"step {step_id!r}" if step_id is not None else "this task"
    if profile.worker_action is WorkerAction.MERGE:
        if step_id is not None and strategy in MERGE_STRATEGIES:
            return
        raise DispatchOptionError(
            f"{where} runs the 'merge' worker action, which merges the pull request "
            "a workflow opens: it is the final step of a workflow whose strategy is "
            + " or ".join(repr(s) for s in MERGE_STRATEGIES)
            + (f", and strategy {strategy!r} opens no pull request." if step_id is not None
               else ", never a task on its own.")
            + f" Or set the workflow's metadata.{MERGE_METADATA_KEY} to 'on' and the "
            "step is appended for you.",
            detail={"step_id": step_id, "runner_profile": profile.name,
                    "strategy": strategy, "merge_strategies": list(MERGE_STRATEGIES)},
        )
    raise DispatchOptionError(
        f"{where} runs the {profile.name!r} worker action, which only a "
        "'single-pr' workflow may contain: it acts on the one pull request that "
        f"chain opens, after the steps that order it. Strategy {strategy!r} has "
        "no such chain.",
        detail={"step_id": step_id, "runner_profile": profile.name,
                "strategy": strategy},
    )


def _worker_action(step: StepSpec) -> WorkerAction | None:
    """The worker action `step`'s profile performs instead of an agent, if any."""
    profile = RUNNER_PROFILES.get(step.runner_profile or "")
    return profile.worker_action if profile is not None else None


@dataclass(frozen=True)
class SinglePrPlan:
    """A `single-pr` chain's steps by their part in it, all STEP ids (#295).

    Built by `resolve_single_pr`, after every refusal in docs/merge-step.md §3
    has passed, and read by `SubmissionService._step_dispatch` to write each
    step's role and the task ids it needs into its signed dispatch block.
    """

    #: Every step's `PR_ROLES` value, worker actions as `none`.
    roles: Mapping[str, str] = field(hash=False)
    author: str
    review: str
    post_verdict: str
    proof: str
    merge: str
    fix: str | None = None

    def merges_steps(self) -> dict[str, str]:
        """The merge step's `merges` block, by STEP id, in `MERGES_KEYS` order."""
        found = {
            "author": self.author,
            "review": self.review,
            "post-verdict": self.post_verdict,
            "fix": self.fix,
            "proof": self.proof,
        }
        return {k: found[k] for k in MERGES_KEYS if found[k] is not None}


def resolve_single_pr(steps: Sequence[StepSpec]) -> SinglePrPlan:
    """Refuse a `single-pr` workflow that is not the chain §3 allows, else plan it.

    Call only AFTER `validate_dag`. The chain is
    implement -> review -> post-verdict -> fix -> proof -> merge, and each rule
    below is a way it would otherwise run and merge something other than what
    was reviewed (docs/merge-step.md §3, §4.3, §7):

    * every agent step declares an author, reader or amender role, and a
      worker action declares none (or `none`): the worker clones and pushes
      by role, so a step with no role would do neither predictably;
    * exactly one `post-verdict` step and one `merge` step: the merge checks
      ONE posted verdict on ONE pull request;
    * exactly one author, an ancestor of every other step: every other step
      clones its branch, which must exist before they start; at most one
      amender, because two would race fast-forward pushes to that branch;
    * `merge` is the only sink, so nothing runs after the merge;
    * `post-verdict` stages nothing and depends on exactly one reader, the
      review: it reads review.json from the path its signed `verdict_source`
      derives, never through `input_from`'s tenant-writable staging (§4.3);
    * nothing stages from `post-verdict`, which writes no artifact;
    * every step downstream of the review, other than `post-verdict`, is
      downstream of `post-verdict` too, and the amender and the merge depend
      on it directly: an ORDERING-only edge, which is what keeps every later
      agent from acting before the verdict is an immutable GitHub review (T3a);
    * the merge stages exactly the proof's proof.json, from a reader;
    * a `merges` the caller stated names exactly the steps the graph does.

    Graph-shape refusals are `invalid_dag`; a role or `merges` that is not one
    the API accepts is `invalid_dispatch`, the split `validate_step_routing`
    already makes.
    """
    by_id = {step.step_id: step for step in steps}
    order = [step.step_id for step in steps]
    roles: dict[str, str] = {}
    actions: dict[WorkerAction, list[str]] = {a: [] for a in SINGLE_PR_WORKER_ACTIONS}

    for step in steps:
        action = _worker_action(step)
        if action is not None:
            if step.pr_role not in (None, PR_ROLE_NONE):
                raise DispatchOptionError(
                    f"step {step.step_id!r} runs the {step.runner_profile!r} worker "
                    "action, which clones nothing and runs no agent, so its pr_role "
                    f"can only be {PR_ROLE_NONE!r} (or absent), not {step.pr_role!r}.",
                    detail={"step_id": step.step_id, "pr_role": step.pr_role,
                            "accepted_pr_roles": [PR_ROLE_NONE]},
                )
            if step.when_step is not None:
                raise DispatchOptionError(
                    f"step {step.step_id!r} runs the {step.runner_profile!r} worker "
                    "action, which runs no agent, so a verdict gate on it would gate "
                    "nothing. Remove its `when`.",
                    detail={"step_id": step.step_id, "when": step.when_step},
                )
            roles[step.step_id] = PR_ROLE_NONE
            actions.setdefault(action, []).append(step.step_id)
        else:
            if step.pr_role not in _AGENT_PR_ROLES:
                raise DispatchOptionError(
                    f"step {step.step_id!r}: under strategy 'single-pr' every agent "
                    "step declares its part in the one pull request as pr_role, one "
                    "of " + ", ".join(_AGENT_PR_ROLES)
                    + (f"; {step.pr_role!r} is not one" if step.pr_role is not None
                       else "; it declares none") + ".",
                    detail={"step_id": step.step_id, "pr_role": step.pr_role,
                            "accepted_pr_roles": list(_AGENT_PR_ROLES)},
                )
            roles[step.step_id] = step.pr_role
        if step.merges is not None and action is not WorkerAction.MERGE:
            raise DispatchOptionError(
                f"step {step.step_id!r} declares `merges`, which only the step on "
                "the 'merge' profile may: it names the upstream steps that merge "
                "checks.",
                detail={"step_id": step.step_id},
            )

    for action in SINGLE_PR_WORKER_ACTIONS:
        found = actions[action]
        if len(found) != 1:
            profile = next(
                n for n, p in RUNNER_PROFILES.items() if p.worker_action is action
            )
            raise DagError(
                f"a 'single-pr' workflow has exactly one step on the {profile!r} "
                f"profile; this one has {len(found)}"
                + (": " + ", ".join(found) if found else "") + ".",
                detail={"runner_profile": profile, "steps": found},
            )
    post_verdict = actions[WorkerAction.POST_VERDICT][0]
    merge = actions[WorkerAction.MERGE][0]

    authors = [sid for sid in order if roles[sid] == "author"]
    if len(authors) != 1:
        raise DagError(
            "a 'single-pr' workflow has exactly one step with pr_role 'author' -- "
            f"the one that opens the pull request; this one has {len(authors)}"
            + (": " + ", ".join(authors) if authors else "") + ".",
            detail={"authors": authors},
        )
    author = authors[0]
    amenders = [sid for sid in order if roles[sid] == "amender"]
    if len(amenders) > 1:
        raise DagError(
            "a 'single-pr' workflow has at most one step with pr_role 'amender': "
            "two would both fast-forward push to the author's branch. This one has "
            + ", ".join(amenders) + ".",
            detail={"amenders": amenders},
        )

    ancestors = _ancestors(steps)
    stray = [sid for sid in order if sid != author and author not in ancestors[sid]]
    if stray:
        raise DagError(
            f"every step of a 'single-pr' workflow clones the branch its author "
            f"{author!r} pushes, so every step must be downstream of it; "
            + ", ".join(stray) + " is not.",
            detail={"author": author, "not_downstream_of_author": stray},
        )

    depended_on = {dep for step in steps for dep in step.depends_on}
    terminals = [sid for sid in order if sid not in depended_on]
    if terminals != [merge]:
        raise DagError(
            f"the merge step {merge!r} is the last thing a 'single-pr' workflow "
            "does, so it must be the only step nothing depends on. This workflow's "
            "final steps are " + ", ".join(terminals) + ".",
            detail={"merge_step_id": merge, "terminal_steps": terminals},
        )

    pv = by_id[post_verdict]
    if pv.input_from:
        raise DagError(
            f"step {post_verdict!r} posts the review's verdict, and reads review.json "
            "only from the path its own signed spec derives (docs/merge-step.md "
            "§4.3), so it may stage nothing through input_from. Remove its "
            "input_from; its depends_on on the review is what orders it.",
            detail={"step_id": post_verdict, "input_from": dict(pv.input_from)},
        )
    if len(pv.depends_on) != 1 or roles[pv.depends_on[0]] != "reader":
        raise DagError(
            f"step {post_verdict!r} posts ONE review's verdict, so it depends on "
            "exactly one step -- the review, a step with pr_role 'reader' -- and "
            "nothing else. It depends on " + (", ".join(pv.depends_on) or "nothing")
            + ".",
            detail={"step_id": post_verdict, "depends_on": list(pv.depends_on)},
        )
    review = pv.depends_on[0]

    for step in steps:
        if post_verdict in step.input_from:
            raise DagError(
                f"step {step.step_id!r} stages a file from {post_verdict!r}, which "
                "runs no agent and writes no artifact. Keep the depends_on edge, "
                "which orders it, and drop the input_from entry.",
                detail={"step_id": step.step_id, "stages_from": post_verdict},
            )
    for sid in order:
        if sid != post_verdict and review in ancestors[sid] and post_verdict not in ancestors[sid]:
            raise DagError(
                f"step {sid!r} comes after the review {review!r} but not after "
                f"{post_verdict!r}, so its agent could act before the verdict is "
                f"posted to GitHub. Add {post_verdict!r} to its depends_on (an "
                "ordering-only edge, with no input_from entry).",
                detail={"step_id": sid, "post_verdict_step_id": post_verdict},
            )
    for sid in [*amenders, merge]:
        if post_verdict not in by_id[sid].depends_on:
            raise DagError(
                f"step {sid!r} depends on {post_verdict!r} directly under "
                "'single-pr' (docs/merge-step.md §3): an ordering-only edge, with "
                "no input_from entry, so it never starts before the verdict is "
                "posted.",
                detail={"step_id": sid, "post_verdict_step_id": post_verdict},
            )

    merge_inputs = dict(by_id[merge].input_from)
    proof = next(iter(merge_inputs), None)
    if (
        len(merge_inputs) != 1
        or merge_inputs[proof] != PROOF_FILENAME
        or roles[proof] != "reader"
    ):
        raise DagError(
            f"the merge step {merge!r} stages exactly one file, the proof's "
            f"{PROOF_FILENAME}, from a step with pr_role 'reader': "
            f'`"input_from": {{"<proof step>": "{PROOF_FILENAME}"}}`.',
            detail={"step_id": merge, "input_from": merge_inputs},
        )

    plan = SinglePrPlan(
        roles=roles,
        author=author,
        review=review,
        post_verdict=post_verdict,
        proof=proof,
        merge=merge,
        fix=amenders[0] if amenders else None,
    )
    stated = by_id[merge].merges
    if stated is not None and dict(stated) != plan.merges_steps():
        raise DispatchOptionError(
            f"step {merge!r} states `merges` as {dict(stated)!r}, but this "
            f"workflow's roles make it {plan.merges_steps()!r}. `merges` names "
            "steps by their part in the chain (" + ", ".join(MERGES_KEYS)
            + "; `fix` only when there is an amender); omit it to have it derived.",
            detail={"step_id": merge, "merges": dict(stated),
                    "derived": plan.merges_steps()},
        )
    return plan


def validate_step_routing(
    steps: Sequence[StepSpec],
    *,
    strategy: str,
    integrator_step_id: str | None,
    rereview: str | None = None,
) -> SinglePrPlan | None:
    """Refuse a verdict gate or a `builds_on` that could not work (#264).

    Call only AFTER `validate_dag` and, under `integrate`,
    `resolve_integrator_step`: this reasons about a graph already known to be
    acyclic and closed.

    The shape these make expressible is implement -> review -> fix, where the
    fix's agent runs only when the review's verdict names it and the fix is the
    one step that publishes. Each refusal below is a way that shape would
    otherwise run to completion and do something other than what was asked:

    * a gate on a step that stages nothing from the gating step has no file
      to read, and would fail only after every upstream step had run;
    * a verdict outside REVIEW_VERDICTS can never be written by a review that
      follows the convention, so the gate would never open (or never shut);
    * under `direct-pr` every step opens its own pull request, the
      implementer's included, so the review would come after the publish it
      exists to prevent;
    * under `integrate` the integrator's pull request is the one place the
      verdict is shown, so a gate on any other step would route work that PR
      says nothing about -- except `rereview`, the re-review swarm-api
      appended under `metadata.merge` "on_merge_verdict" (by id), which is
      gated on the same review as the integrator so it runs only after a fix
      (owner decision 2026-10-10, part of #295);
    * a gated step whose agent does not run writes no artifact but the
      verdict file it staged, which it re-publishes unchanged
      (`agent_worker.lifecycle.Worker._republish_staged_verdict`), so a step
      that stages anything else from it would fail every time the gate
      stayed shut; staging that verdict file, by the same name, is allowed;
    * `builds_on` names a step whose branch this step clones, so it must be
      upstream (or its branch may not exist yet), and the strategy must push
      every step's branch (`collect` pushes none).

    Graph-shape refusals are `invalid_dag`; strategy refusals are
    `invalid_dispatch`, the same split `resolve_integrator_step` makes.

    Under `single-pr` (#295) the chain's own rules are `resolve_single_pr`'s,
    and the plan it returns is returned from here. Under every other strategy
    a `pr_role`, a `merges` or a worker-action profile is refused: each means
    something only to a chain that ends in its own merge, and a worker action
    anywhere else would run outside the ordering that makes it safe.
    """
    plan: SinglePrPlan | None = None
    if strategy == SINGLE_PR:
        plan = resolve_single_pr(steps)
    else:
        for step in steps:
            profile = RUNNER_PROFILES.get(step.runner_profile or "")
            if profile is not None:
                refuse_worker_action_outside_single_pr(
                    profile, strategy, step_id=step.step_id
                )
            if step.pr_role is not None or step.merges is not None:
                raise DispatchOptionError(
                    f"step {step.step_id!r} declares "
                    + ("pr_role" if step.pr_role is not None else "merges")
                    + f", which means something only under strategy 'single-pr'; "
                    f"under {strategy!r} it would be ignored. Remove it, or choose "
                    "'single-pr'.",
                    detail={"step_id": step.step_id, "strategy": strategy},
                )

    ancestors = _ancestors(steps)

    for step in steps:
        if step.when_step is not None:
            if step.when_step not in step.input_from:
                raise DagError(
                    f"step {step.step_id!r} runs its agent on the verdict of "
                    f"{step.when_step!r} but stages no file from it. Add "
                    f'`"input_from": {{"{step.when_step}": "verdict.json"}}` '
                    f"(and {step.when_step!r} to depends_on): the verdict is read "
                    "from the file this step stages from that step.",
                    detail={"step_id": step.step_id, "when": step.when_step},
                )
            verdicts = list(step.when_verdicts)
            unknown = [v for v in verdicts if v not in REVIEW_VERDICTS]
            if not verdicts or unknown or len(set(verdicts)) != len(verdicts):
                raise DagError(
                    f"step {step.step_id!r}: when.verdict_in must name one or more "
                    "distinct verdicts from " + ", ".join(REVIEW_VERDICTS)
                    + (f"; {', '.join(repr(v) for v in unknown)} is not one" if unknown else "")
                    + ". A review writes one of these as `verdict` in its verdict file.",
                    detail={
                        "step_id": step.step_id,
                        "verdict_in": verdicts,
                        "accepted_verdicts": list(REVIEW_VERDICTS),
                    },
                )
            if strategy == "direct-pr":
                raise DispatchOptionError(
                    f"step {step.step_id!r} is gated on a review verdict, and under "
                    "strategy 'direct-pr' every step opens its own pull request -- "
                    "the implementer's included -- so the review would come after "
                    "the publish it is meant to precede. Use 'integrate', where "
                    "only the final step opens a pull request, or 'collect'.",
                    detail={"step_id": step.step_id, "strategy": strategy},
                )
            if strategy == "integrate" and step.step_id not in (integrator_step_id, rereview):
                raise DispatchOptionError(
                    f"step {step.step_id!r} is gated on a review verdict, but under "
                    f"strategy 'integrate' the step that publishes is "
                    f"{integrator_step_id!r}, and its pull request is where the "
                    "verdict is shown. Gate the integrating step, or make the "
                    "gated step the one every other step feeds.",
                    detail={
                        "step_id": step.step_id,
                        "integrator_step_id": integrator_step_id,
                    },
                )
            # The one file a shut gate still publishes is the verdict it read,
            # passed on byte for byte under the name it staged it by.
            passed_on = step.input_from.get(step.when_step)
            other = [
                s.step_id for s in steps
                if step.step_id in s.input_from and s.input_from[step.step_id] != passed_on
            ]
            if other:
                raise DagError(
                    f"step {step.step_id!r} is gated on a review verdict, so when the "
                    "verdict does not name it its agent does not run and the only "
                    f"artifact it writes is the {passed_on!r} it staged, passed on -- but "
                    + ", ".join(repr(s) for s in other)
                    + " stage another file from it. A gated step is an input_from "
                    f"source only for {passed_on!r}.",
                    detail={"step_id": step.step_id, "staged_by": other,
                            "passed_on": passed_on},
                )

        if step.builds_on is not None:
            if step.builds_on not in ancestors.get(step.step_id, set()):
                raise DagError(
                    f"step {step.step_id!r} builds on {step.builds_on!r}, which is not "
                    "upstream of it. A step starts from the branch its base pushed, "
                    "so the base must be one of its dependencies, directly or "
                    "through another step.",
                    detail={"step_id": step.step_id, "builds_on": step.builds_on},
                )
            if strategy == SINGLE_PR:
                raise DispatchOptionError(
                    f"step {step.step_id!r} builds on {step.builds_on!r}'s branch, and "
                    "under strategy 'single-pr' the branch every step clones is "
                    "decided by its pr_role: a reader or amender starts from the "
                    "author's. Remove builds_on.",
                    detail={"step_id": step.step_id, "strategy": strategy,
                            "builds_on": step.builds_on},
                )
            if strategy not in _EVERY_STEP_PUSHES_STRATEGIES:
                raise DispatchOptionError(
                    f"step {step.step_id!r} builds on {step.builds_on!r}'s branch, and "
                    f"under strategy {strategy!r} no step pushes a branch. Use "
                    "'integrate' (only the final step opens a pull request) or "
                    "'direct-pr'.",
                    detail={"step_id": step.step_id, "strategy": strategy,
                            "builds_on": step.builds_on},
                )
    return plan


def _is_agent_step(step: StepSpec) -> bool:
    """A step whose profile runs an agent: not a worker action (post-verdict,
    merge) and not a control-plane step (`await-input`). An unknown profile
    counts as an agent; `validate_runner_profile` has refused it already."""
    profile = RUNNER_PROFILES.get(step.runner_profile or "")
    return profile is None or (profile.worker_action is None and not profile.control_plane)


def reader_step_ids(steps: Sequence[StepSpec], *, strategy: str) -> frozenset[str]:
    """The `direct-pr` agent steps that are readers: stored with
    `allow_empty_diff` so an empty diff ends them SUCCEEDED `no_change` (#2662).

    Under `direct-pr` the worker holds every step to opening a pull request,
    and only `allow_empty_diff` (or `verification.md`) lets one that committed
    nothing succeed. A step that another agent step `depends_on` (or stages a
    file from through `input_from`) and that no step `builds_on` is a reader:
    its dependants consume its artifacts, never its branch. A read-only review
    that writes `review.md` and changes nothing did its job; failing it
    `published_nothing:` swept the fix behind it (wf_ea38b900). If it does
    commit, it still opens its pull request: the flag covers an empty diff
    and nothing else.

    Left out, keeping today's obligation:

    * a step another step `builds_on` -- its branch is the deliverable the
      builder clones (#1225);
    * a step nothing depends on -- its pull request is the workflow's result;
    * a step only a worker action or a control-plane step depends on -- the
      merge step `plan_merge` appends under `direct-pr` merges the pull
      request this step opens, so that step must open one;
    * every step under any other strategy, which stores the block it did.
    """
    if strategy != "direct-pr":
        return frozenset()
    agents = [step for step in steps if _is_agent_step(step)]
    built_on = {step.builds_on for step in steps if step.builds_on}
    read = {
        upstream
        for step in agents
        for upstream in (*step.depends_on, *step.input_from)
    }
    return frozenset(
        step.step_id for step in agents
        if step.step_id in read and step.step_id not in built_on
    )


# --------------------------------------------------------------------------
# The `merge` step (contract request 47)
# --------------------------------------------------------------------------


def resolve_merge_choice(metadata: Mapping[str, Any]) -> str | None:
    """A workflow's `metadata.merge`: "on", "off", or None when it says nothing."""
    if MERGE_METADATA_KEY not in metadata:
        return None
    value = metadata[MERGE_METADATA_KEY]
    if value not in MERGE_CHOICES:
        raise DispatchOptionError(
            f"metadata.{MERGE_METADATA_KEY} is {str(value)[:40]!r}; it is one of "
            + ", ".join(repr(c) for c in MERGE_CHOICES)
            + ", or absent for the repository's merge_policy, else the platform's "
            "default.",
            detail={"field": f"metadata.{MERGE_METADATA_KEY}",
                    "accepted": list(MERGE_CHOICES)},
        )
    return value


def is_merge_step(runner_profile: str | None) -> bool:
    profile = RUNNER_PROFILES.get(runner_profile or "")
    return profile is not None and profile.worker_action is WorkerAction.MERGE


@dataclass(frozen=True)
class MergeSources:
    """What a workflow's merge step acts on, all STEP ids."""

    #: The step that opens the pull request: the integrator, or a `direct-pr`
    #: workflow's one agent step. A TASK id instead when `continued` is set.
    pull_request: str
    #: The review whose verdict must be MERGE -- the step the publishing
    #: step's verdict gate reads -- and the file that step stages from it.
    review: str | None = None
    verdict_file: str | None = None
    #: True for a merge-only continuation (`continues_task` and one `merge`
    #: step): `pull_request` is then the continued TASK, which pushed the
    #: head to pin in an earlier workflow, and the step depends on nothing.
    continued: bool = False
    #: True for a `merge_pr` workflow (#352): no task opened the pull
    #: request, `pull_request` is "", and the step depends on nothing.
    named: bool = False

    def depends_on(self) -> list[str]:
        if self.continued or self.named:
            return []
        return [self.pull_request] + ([self.review] if self.review else [])

    def input_from(self) -> dict[str, str]:
        return {self.review: self.verdict_file} if self.review and self.verdict_file else {}


@dataclass(frozen=True)
class MergePlan:
    merge_step: str
    sources: MergeSources


def merge_sources(
    steps: Sequence[StepSpec], strategy: str, continued_task: str | None = None,
    named_pull: bool = False, rereview: str | None = None,
) -> MergeSources | None:
    """The pull request a merge step would merge, or None when there is not ONE.

    `steps` are the AGENT steps (no merge step), after `validate_dag`. Under
    `integrate` the pull request is the integrator's -- the agent steps' one
    sink -- and the review is the step its verdict gate reads (#264), the
    only gate `integrate` allows. Under `direct-pr` every step opens its own
    pull request, so there is ONE only when there is one agent step, and no
    review (`direct-pr` refuses a gate). `collect` opens none, and
    `single-pr` ends in its own merge.

    `continued_task` is the task a `continues_task` workflow names, as
    `continuation.resolve_continuation` checked it (the caller's tenant, a
    branch with its own pull request, the same repository). A continuation
    with NO agent step merges that task's pull request at the head that task
    pushed: how an issue run merges once its CI is green and its keyword
    block is written (`issueci`), after the workflow that opened it ended.

    `named_pull` is True for a workflow with `merge_pr` (#352), as
    `continuation.resolve_merge_pr` checked it: with NO agent step it merges
    the pull request the caller named, which no task opened.

    `rereview` is the re-review `metadata.merge` "on_merge_verdict" appended
    (`rereview_step_for`), by id. It is not the integrator -- it comes after
    it -- and it IS the review the merge reads: the latest verdict, written
    under the same file name the gate's review wrote.
    """
    agents = [step for step in steps if not is_merge_step(step.runner_profile)]
    if rereview is not None:
        if strategy != "integrate":
            return None
        rest = merge_sources(
            [step for step in agents if step.step_id != rereview], strategy, continued_task,
        )
        if rest is None or rest.review is None or not rest.verdict_file:
            return None
        return MergeSources(rest.pull_request, rereview, rest.verdict_file)
    if named_pull:
        return MergeSources("", named=True) if strategy == "direct-pr" and not agents else None
    if strategy == "direct-pr" and continued_task is not None and not agents:
        return MergeSources(continued_task, continued=True)
    if strategy == "direct-pr":
        return MergeSources(agents[0].step_id) if len(agents) == 1 else None
    if strategy != "integrate" or len(agents) < 2:
        return None
    depended_on = {dep for step in agents for dep in step.depends_on}
    sinks = [step for step in agents if step.step_id not in depended_on]
    if len(sinks) != 1:
        return None
    integrator = sinks[0]
    review = integrator.when_step
    if review is None:
        return MergeSources(integrator.step_id)
    return MergeSources(integrator.step_id, review, integrator.input_from.get(review))


def merge_step_for(
    steps: Sequence[StepSpec], strategy: str, continued_task: str | None = None,
    named_pull: bool = False, rereview: str | None = None,
) -> dict[str, Any] | None:
    """The `merge` step swarm-api appends to a workflow, or None if it opens no one PR.

    `depends_on` names every step that must finish first -- the step that
    opens the pull request, and the review when there is one -- and the
    review's verdict file is staged, so the merge reads the verdict the
    publishing step's gate read.
    """
    sources = merge_sources(steps, strategy, continued_task, named_pull, rereview)
    if sources is None:
        return None
    return {
        "step_id": _free_step_id(MERGE_STEP_ID, {step.step_id for step in steps}),
        "runner_profile": MERGE_STEP_ID,
        "depends_on": sources.depends_on(),
        "input_from": sources.input_from(),
    }


def _free_step_id(base: str, taken: set[str]) -> str:
    """`base`, or `base-2`, `base-3`... -- the first id no step has."""
    step_id, n = base, 1
    while step_id in taken:
        n += 1
        step_id = f"{base}-{n}"
    return step_id


def rereview_step_for(
    steps: Sequence[StepSpec], review: Mapping[str, Any], strategy: str,
) -> dict[str, Any] | None:
    """The re-review step `metadata.merge` "on_merge_verdict" appends, or None.

    None unless the workflow is `integrate` and its integrator is gated on a
    review (`merge_sources` finds a review and the file the gate stages from
    it): that is the implement -> review -> fix shape whose fix may change
    the head after the review judged it. `review` is that review step as
    submitted (`WorkflowStepCreate.model_dump()`).

    The re-review runs the REVIEW's runner profile, by name, on the review's
    own input with `REREVIEW_PREAMBLE` before its prompt, so nothing the
    caller did not already choose for its review is chosen here (invariant
    10). It builds on the integrator -- its checkout is the head the merge
    will pin -- and stages, by parent, the earlier verdict and whatever the
    review staged. It is GATED on the same review and verdicts as the
    integrator (owner decision 2026-10-10, part of #295): its agent runs
    only when the fix's did, which in implement -> review -> fix is NOT_YET.
    On MERGE no fix ran, the head is the one the review already judged, and
    the re-review runs no agent: the worker re-publishes the verdict it
    staged, the first review's MERGE, byte for byte as its own artifact
    (`agent_worker.lifecycle.Worker._republish_staged_verdict`), so the
    merge step reads the same file name from the same step on both paths.

    A review that asked for `browser_if_ui` (#2322) passes the block on with
    the rest of its metadata, and the service adds `inherit`, naming the
    review's task (`SubmissionService.submit_workflow`): the scheduler runs
    the re-review on the profile the review RESOLVED to, never re-deciding
    from the integrator's diff (`scheduler.runtime.resolve`).

    A review PINNED to `claude-code`, with no block, gets one on its
    re-review (#2275, BGD 2026-10-27, Q-2275): a `claude-code` review of a
    UI lane can never say MERGE, and a re-review copying its profile could
    not either, so no fix round reached the merge. The re-review is
    submitted on the pair's browser profile with `browser_if_ui`, catalogue
    names only (invariant 10), and the scheduler decides from the lane's own
    files -- the browser only when they are UI, which is the #2322 cost
    decision. It carries no `inherit`: the review it would inherit from was
    pinned, not resolved (`SubmissionService.submit_workflow`).
    """
    sources = merge_sources(steps, strategy)
    if (
        strategy != "integrate" or sources is None or sources.review is None
        or not sources.verdict_file or review.get("step_id") != sources.review
    ):
        return None
    integrator = next((s for s in steps if s.step_id == sources.pull_request), None)
    if integrator is None:
        return None
    staged = {**dict(review.get("input_from") or {}), sources.review: sources.verdict_file}
    paths = ", ".join(f"{src}/{name}" for src, name in staged.items())
    original = dict(review.get("input") or {})
    prompt = original.get("prompt")
    preamble = REREVIEW_PREAMBLE.format(
        verdict_path=f"{sources.review}/{sources.verdict_file}",
        verdict_file=sources.verdict_file,
        staged=paths,
    )
    metadata = dict(review.get("metadata") or {})
    profile = review.get("runner_profile")
    if profile == RUNTIME_POLICY_PROFILES["code"] and RUNTIME_METADATA_KEY not in metadata:
        profile = RUNTIME_POLICY_PROFILES["browser"]
        metadata[RUNTIME_METADATA_KEY] = {
            "policy": RUNTIME_POLICY_BROWSER_IF_UI, **RUNTIME_POLICY_PROFILES,
        }
    step: dict[str, Any] = {
        "step_id": _free_step_id(REREVIEW_STEP_ID, {s.step_id for s in steps}),
        "runner_profile": profile,
        "input": {**original, "prompt": preamble + (prompt if isinstance(prompt, str) else "")},
        "depends_on": list(dict.fromkeys([sources.pull_request, *staged])),
        "builds_on": sources.pull_request,
        "input_from": staged,
        # The integrator's own gate: the re-review runs after a fix, and
        # passes the review's verdict on when there was none.
        "when": {"step": sources.review, "verdict_in": list(integrator.when_verdicts)},
        "metadata": {**metadata, INPUT_LAYOUT_METADATA_KEY: INPUT_LAYOUT_BY_PARENT},
    }
    for name in ("resource_class", "timeout_seconds"):
        if review.get(name) is not None:
            step[name] = review[name]
    return step


def plan_merge(
    steps: Sequence[StepSpec], strategy: str, continued_task: str | None = None,
    named_pull: bool = False, rereview: str | None = None,
) -> MergePlan | None:
    """Refuse a merge step that could not merge what the workflow opened, else plan it.

    Call AFTER `validate_dag` and `validate_step_routing`. None when the
    workflow has no merge step, or is `single-pr` (`resolve_single_pr`
    plans that chain's own). The rules, each a way the step would otherwise
    merge something other than what the workflow produced and its review
    judged:

    * at most one merge step, and nothing depends on it: it is the last
      thing the workflow does;
    * the workflow opens ONE pull request (`merge_sources`);
    * no `when`, `builds_on`, `pr_role` or `merges` on it: it runs no agent
      and clones nothing;
    * it depends directly on the step that opens the pull request and on the
      review, so neither can still be running when it reads them;
    * it stages exactly the review's verdict file and nothing else.
    """
    if strategy == SINGLE_PR:
        return None
    found = [step for step in steps if is_merge_step(step.runner_profile)]
    if not found:
        return None
    if len(found) > 1:
        raise DagError(
            "a workflow has at most one 'merge' step: it merges the one pull request "
            f"the workflow opens; this one has {len(found)}: "
            + ", ".join(step.step_id for step in found) + ".",
            detail={"merge_steps": [step.step_id for step in found]},
        )
    merge = found[0]
    dependants = [step.step_id for step in steps if merge.step_id in step.depends_on]
    if dependants:
        raise DagError(
            f"the merge step {merge.step_id!r} is the last thing a workflow does, but "
            + ", ".join(repr(d) for d in dependants) + " depend on it.",
            detail={"merge_step_id": merge.step_id, "dependants": dependants},
        )
    for name, value in (("when", merge.when_step), ("builds_on", merge.builds_on),
                        ("pr_role", merge.pr_role), ("merges", merge.merges)):
        if value is not None:
            raise DispatchOptionError(
                f"step {merge.step_id!r} runs the 'merge' worker action, which runs no "
                f"agent and clones nothing, so it takes no `{name}`. Remove it.",
                detail={"step_id": merge.step_id, "field": name},
            )
    sources = merge_sources(steps, strategy, continued_task, named_pull, rereview)
    if sources is None:
        raise DispatchOptionError(
            f"step {merge.step_id!r} merges the pull request this workflow opens, and "
            + ("under 'direct-pr' every agent step opens its own, so it needs exactly "
               "one agent step; use 'integrate' to open one pull request from several."
               if strategy == "direct-pr" else
               f"strategy {strategy!r} opens no single pull request."),
            detail={"step_id": merge.step_id, "strategy": strategy},
        )
    if sources.review is not None and not sources.verdict_file:
        raise DagError(
            f"the merge step reads the verdict of {sources.review!r} from the file the "
            "gated step stages from it, and that step stages none.",
            detail={"merge_step_id": merge.step_id, "review": sources.review},
        )
    missing = [sid for sid in sources.depends_on() if sid not in merge.depends_on]
    if missing:
        raise DagError(
            f"the merge step {merge.step_id!r} must depend directly on "
            + ", ".join(repr(m) for m in missing)
            + ": the step that opens the pull request"
            + (" and the review whose verdict it requires" if sources.review else "")
            + f". Add them to its depends_on, or omit the step and set metadata."
            f"{MERGE_METADATA_KEY} 'on' to have it derived.",
            detail={"merge_step_id": merge.step_id, "missing": missing,
                    "derived_depends_on": sources.depends_on()},
        )
    if dict(merge.input_from) != sources.input_from():
        raise DagError(
            f"the merge step {merge.step_id!r} stages "
            + (f"exactly {sources.input_from()!r}, the review's verdict file"
               if sources.review else "nothing")
            + f", not {dict(merge.input_from)!r}.",
            detail={"merge_step_id": merge.step_id, "input_from": dict(merge.input_from),
                    "derived_input_from": sources.input_from()},
        )
    return MergePlan(merge.step_id, sources)


def _forge_host(repository_url: str | None) -> tuple[str, int | None]:
    """`(host, port)` of a repository URL, https or scp-style ssh."""
    from urllib.parse import urlsplit

    text = (repository_url or "").strip()
    if text.startswith("git@"):
        head, _, tail = text.partition(":")
        text = f"ssh://{head}/{tail}"
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        host, port = "", None
    return host, port


def is_mergeable_forge(repository_url: str | None) -> bool:
    """Whether a `ForgeMerger` serves this repository's host."""
    host, port = _forge_host(repository_url)
    return host in MERGE_FORGE_HOSTS and port is None


def refuse_unmergeable_forge(repository_url: str | None) -> None:
    """A merge step's repository must be on a host a `ForgeMerger` serves.

    Refused HERE, at submission, so a workflow never runs every agent step to
    completion and then fails at its merge for a host nothing can merge on.
    """
    if is_mergeable_forge(repository_url):
        return
    host, _ = _forge_host(repository_url)
    raise DispatchOptionError(
        "the merge step merges on github.com only, and this workflow's repository "
        f"{'is on ' + repr(host) if host else 'names no forge host'}. Set the "
        f"workflow's metadata.{MERGE_METADATA_KEY} to 'off', or remove the merge step.",
        detail={"repository_host": host or None,
                "supported_forge_hosts": sorted(MERGE_FORGE_HOSTS)},
    )


def validate_timeout(profile: RunnerProfile, requested: int | None) -> int:
    """A caller may shorten a timeout, never lengthen it past the profile's."""
    if requested is None:
        return profile.timeout_seconds
    if requested <= 0:
        raise ValidationFailed("timeout_seconds must be positive")
    return min(int(requested), profile.timeout_seconds)



# --------------------------------------------------------------------------
# Whose GitHub credential a task runs with (#780 lane OB7, decision D4)
# --------------------------------------------------------------------------
#
# Contract request 54: `Task.forge_credential` names the slot the worker reads
# its forge token from and `Task.forge_access` whether it may push. Only
# swarm-api writes them, at submission, from the grants the person chose
# under Access (`access.py`, `forge_grants`) -- never from the request body,
# which `extra="forbid"` refuses them in (invariant 10) -- and BEFORE the spec
# is signed, so a rewritten value fails the worker's check.
#
# D4 as the owner decided it on 2026-10-07: a PERSON's task on a GitHub
# repository they hold no grant for is refused, 403 REPOSITORY_NOT_GRANTED,
# until they choose it under Access; a SERVICE ACCOUNT's submission (tenant
# automation, the release's acceptance suite) and a repository index run
# use the tenant token, `git`, with write, as before #780. A task with no
# repository, or one on a host that is not GitHub (grants exist only for
# GitHub), carries neither field and reads the tenant token as it always has.

#: The code a client branches on. Upper case, like the access API's own
#: refusals (`access.AccessRefused`), because the console's Access page reads
#: them side by side.
REPOSITORY_NOT_GRANTED = "REPOSITORY_NOT_GRANTED"

#: `Task.forge_access` for a service submission: the tenant token's reach, as
#: before #780. A person's task carries their grant's mode instead.
SERVICE_FORGE_ACCESS = "write"

#: The domain every Google service account's email ends in, user-managed
#: (`SERVICE_ACCOUNT_EMAIL`) and Google-managed alike. No person's address
#: can: no Workspace domain ends in it.
_SERVICE_ACCOUNT_DOMAIN = ".gserviceaccount.com"


class RepositoryNotGranted(Forbidden):
    """A person submitted work on a GitHub repository they hold no grant for."""

    code = REPOSITORY_NOT_GRANTED


def is_service_submitter(email: str | None) -> bool:
    """Whether the submitter is a service account rather than a person (D4)."""
    return (email or "").strip().lower().endswith(_SERVICE_ACCOUNT_DOMAIN)


def github_repository(repository_url: str | None) -> tuple[str, str] | None:
    """`(owner, repo)` of a repository on GitHub, else None.

    Wider than `merge_repository` on purpose: that one answers "can a merge
    step act here" and says None for a URL with a port or a trailing path.
    This one answers "would the worker clone from GitHub", so a GitHub host
    is GitHub whatever port or trailing dot it is written with, and a GitHub
    URL that names no `owner/repo` is REFUSED rather than read as "not
    GitHub", which would hand it the tenant token D4 withholds.
    """
    from urllib.parse import urlsplit

    text = (repository_url or "").strip()
    if not text:
        return None
    if text.startswith("git@"):
        head, _, tail = text.partition(":")
        text = f"ssh://{head}/{tail}"
    try:
        parts = urlsplit(text)
        # `.hostname` only: `.port` raises on a malformed port, and a host
        # read as "" there would be "not GitHub".
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
        parts = None
    if host not in MERGE_FORGE_HOSTS:
        return None
    segments = [seg for seg in parts.path.strip("/").split("/") if seg] if parts else []
    if len(segments) == 2 and segments[1].lower().endswith(".git"):
        segments[1] = segments[1][:-4]
    if len(segments) != 2 or not all(segments):
        raise ValidationFailed(
            "repository_url on GitHub must name one repository as "
            "https://github.com/<owner>/<repo>",
            detail={"field": "repository_url"},
        )
    return segments[0], segments[1]


def repository_not_granted(refused: Sequence[tuple[str | None, str]]) -> RepositoryNotGranted:
    """The refusal for every `(step_id, "owner/repo")` the person holds no grant on.

    One refusal for the whole submission, naming each repository and, in a
    workflow, each step: nothing is stored, so a person fixes all of it at
    once under Access rather than one resubmission per step.
    """
    repositories = sorted({name for _, name in refused}, key=str.lower)
    if len(repositories) == 1:
        message = (f"repository {repositories[0]} is not granted to you: "
                   "choose it under Access")
    else:
        message = (f"repositories {', '.join(repositories)} are not granted to you: "
                   "choose them under Access")
    detail: dict[str, Any] = {"repository": repositories[0], "repositories": repositories}
    steps = [{"step_id": step, "repository": name} for step, name in refused if step]
    if steps:
        detail["steps"] = steps
    return RepositoryNotGranted(message, detail=detail)


# --------------------------------------------------------------------------
# A step's additional repositories (#1434, docs/design/multi-repository-steps.md §4.1)
# --------------------------------------------------------------------------
#
# Every one is refused at submission, before anything is stored and before
# any capacity is asked for (invariant 1), by `extrarepos`. Each is new with
# the field and raised only for a submission that names it, behind
# `ApiSettings.additional_repositories_enabled` (off by default), so no
# existing caller can be refused by any of them. A message names the
# caller's own `owner/repo` and never a URL, a slot or a token.


class AdditionalRepositoriesDisabled(ValidationFailed):
    """The submission names additional repositories and this deployment has
    not turned them on (ADDITIONAL_REPOSITORIES_ENABLED)."""

    code = "additional_repositories_disabled"


class RepositoryNotRegistered(ValidationFailed):
    """No registration of this repository in the caller's tenant: one answer
    whether or not another tenant registered it."""

    code = "repository_not_registered"


class RepositoryNotReadableByTasks(Forbidden):
    """Registered, but an admin has not let its tenant's tasks read it
    (`readable_by_tasks`, the gate `read_private` uses; owner's Q1), or it is
    archived."""

    code = "repository_not_readable_by_tasks"


class AdditionalRepositoryUnreadable(ValidationFailed):
    """The slot the worker would clone it with cannot read it, or its ref
    names no commit. One sentence for both, as `read_private`'s
    `repo_not_allowlisted`: a repository readable only through its own
    `git-r-` token is refused here too (owner's Q2)."""

    code = "additional_repository_unreadable"


class AdditionalRepositoryDuplicate(ValidationFailed):
    """Two entries name one repository or one directory, or an entry names
    the task's own repository."""

    code = "additional_repository_duplicate"


class TooManyAdditionalRepositories(ValidationFailed):
    """More entries than the platform setting `max_additional_repositories`."""

    code = "too_many_additional_repositories"


class AdditionalRepositoriesTooLarge(ValidationFailed):
    """The forge's reported sizes total more than the platform setting
    `additional_repositories_budget_gib`."""

    code = "additional_repositories_too_large"


class AdditionalRepositoriesUnsupportedProfile(ValidationFailed):
    """The step's profile does not take additional repositories: only GKE
    agent profiles do (owner's Q4; design §4.3)."""

    code = "additional_repositories_unsupported_profile"


# --------------------------------------------------------------------------
# Workflow DAG
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StepSpec:
    """The DAG-relevant slice of a submitted step."""

    step_id: str
    depends_on: tuple[str, ...]
    #: upstream step_id -> artifact filename, exactly as submitted.
    #:
    #: THE FILENAMES ARE HERE, NOT ONLY THE IDS. This used to be a tuple of
    #: parent ids, which is all the dependency rule needs. That is why two
    #: parents staging the same `notes.md` into one step went unchecked until
    #: the worker refused the step, after both parents had run (#64). Excluded
    #: from the hash because a mapping has none.
    input_from: Mapping[str, str] = field(default_factory=dict, hash=False)
    #: `when.step` and `when.verdict_in` as submitted (#264), or None and ().
    when_step: str | None = None
    when_verdicts: tuple[str, ...] = ()
    #: `builds_on` as submitted (#264): an upstream step id, or None.
    builds_on: str | None = None
    #: Where this step's `input_from` files land (#75): `INPUT_LAYOUTS`,
    #: already resolved by `resolve_input_layout`.
    input_layout: str = DEFAULT_INPUT_LAYOUT
    #: The step's runner profile BY NAME, as submitted. Read here only to tell
    #: a worker-action step (post-verdict, merge) from an agent step (#295);
    #: whether the profile exists and is enabled is `validate_runner_profile`'s.
    runner_profile: str | None = None
    #: `pr_role` and `merges` as submitted (#295), checked by
    #: `validate_step_routing`. `merges` names STEP ids; the service resolves
    #: them to task ids.
    pr_role: str | None = None
    merges: Mapping[str, str] | None = field(default=None, hash=False)


class DagError(ValidationFailed):
    """Every workflow refusal `validate_dag` and its helpers make: 422 `invalid_dag`.

    ONE CODE, ONE STATUS. The `input_from` refusals added for #64 (a filename
    two parents stage into one step, an absolute or traversing filename) raise
    this class too, not a subclass with a status of its own. The owner decided
    so on #64 on 2026-09-25: the New Workflow screen (`SubmitWorkflow.tsx`
    `KIND_BY_STATUS`) heads a 422 "That request was not valid", and a caller
    branching on the status or on the code must read every refusal in the
    family the same way. `test_every_invalid_dag_refusal_answers_one_status`
    holds it.

    A workflow-level `metadata.input_from` is NOT in this family. The key is
    reserved (owner decision on #151), so `reject_reserved_metadata` refuses it
    with 422 `invalid_dispatch` before `validate_dag` runs, whatever its value.
    The same test pins that ordering and the one status both codes share.
    """

    code = "invalid_dag"


def validate_dag(steps: Sequence[StepSpec], *, max_steps: int) -> list[str]:
    """Validate and topologically order a submitted workflow.

    Checks, in the order a caller most wants to hear about them:
      1. at least one step, and no more than `max_steps`
      2. step ids are non-empty and unique
      3. no step depends on itself
      4. every `depends_on` names a step IN THIS WORKFLOW
      5. every `input_from` source is also an upstream dependency
      6. every `input_from` filename is a relative path inside the workspace,
         within the worker's name bound, and -- unless the step stages by
         parent (#75) -- no two parents of one step stage the same one, or one
         a directory of the other
      7. the graph is acyclic

    Returns a topological order, which the caller uses to create tasks parent
    before child so a child never observes a missing parent task document.
    """
    if not steps:
        raise DagError("a workflow needs at least one step")
    if len(steps) > max_steps:
        raise DagError(
            f"workflow has {len(steps)} steps, over the {max_steps} step limit",
            detail={"steps": len(steps), "max_workflow_steps": max_steps},
        )

    ids: list[str] = []
    seen: set[str] = set()
    for step in steps:
        sid = step.step_id.strip()
        if not sid:
            raise DagError("every step needs a non-empty step_id")
        if sid in seen:
            raise DagError(f"duplicate step_id {sid!r}", detail={"step_id": sid})
        seen.add(sid)
        ids.append(sid)

    for step in steps:
        for dep in step.depends_on:
            if dep == step.step_id:
                raise DagError(
                    f"step {step.step_id!r} depends on itself",
                    detail={"cycle": [step.step_id, step.step_id]},
                )
            if dep not in seen:
                raise DagError(
                    f"step {step.step_id!r} depends on {dep!r}, which is not a step "
                    "in this workflow",
                    detail={"step_id": step.step_id, "missing_dependency": dep,
                            "known_steps": ids},
                )
        for source in step.input_from:
            if source not in seen:
                raise DagError(
                    f"step {step.step_id!r} stages input from {source!r}, which is not a "
                    "step in this workflow",
                    detail={"step_id": step.step_id, "missing_dependency": source},
                )
            if source not in step.depends_on:
                raise DagError(
                    f"step {step.step_id!r} stages input from {source!r} but does not "
                    "depend on it; an artifact cannot be staged from a step that may "
                    "not have run yet",
                    detail={"step_id": step.step_id, "input_from": source},
                )
        validate_staged_filenames(step)

    cycle = find_cycle(steps)
    if cycle:
        raise DagError(
            "workflow dependency graph contains a cycle: " + " -> ".join(cycle),
            detail={"cycle": cycle},
        )
    return topological_order(steps)


# --------------------------------------------------------------------------
# input_from filenames: what the worker would refuse, refused here first
# --------------------------------------------------------------------------
#
# The worker ALREADY enforces every rule below at run time
# (`agent_worker/inputs.py`: `declared_inputs`, `_assert_distinct_destinations`,
# `destination_for`), and it keeps doing so. That is defence in depth: the
# worker reads a free-form dict, and it alone knows the reserved names.
# Two rules have no single check there, only a failure: a filename that is a
# directory of another (#71) fails the step while staging, and one over the
# name bound was, before #232's exemption for declared names, never uploaded.
#
# A step's `input_from` is the ONLY door a declaration has. A caller cannot
# send `metadata.input_from` at all -- on a plain task, a batch or a workflow's
# own `metadata` it is refused whole by `reject_reserved_metadata` (422
# `invalid_dispatch`, #151), before `validate_dag` runs -- so every
# `metadata.input_from` the worker reads was written by `submit_workflow` from
# a step declaration that passed the checks here. There is no workflow-level
# declaration to check separately.
#
# What checking here changes is WHEN the answer arrives. At run time it
# arrives after every upstream step has run. Measured 2026-09-25 on wf_1e547922a981411991e2, that
# was ~20 minutes of claude-code across nine steps, then four failed merges and
# seventeen cancellations, for a mistake visible in the request body (#64).
#
# THE RULE IS RESTATED BECAUSE IT HAS TO BE, AND A TEST HOLDS THE COPY. The
# swarm-api image ships apps/common and apps/swarm-api and nothing else, so
# `agent_worker` cannot be imported here.
# tests/unit/control_plane/test_input_from_submission.py runs the worker's own
# functions against these rules: every filename accepted here must pass the
# worker's `destination_for`, and the collision refused here must be one the
# worker refuses.
#
# THE WORKER'S RESERVED NAMES ARE NOT RESTATED. The worker derives `repo`,
# `.swarm` and the workspace control files from `Workspace` at run time, and a
# copy here would be the mirrored-value shape behind three outages in this
# repository (docs/mirrored-values.md). A reserved name is still refused: by
# the worker, at staging.

#: Path segments that make a filename name something other than a file inside
#: the step's workspace. "" comes from a leading, doubled or trailing "/".
#:
#: STRICTER THAN THE WORKER on "" and ".". `destination_for` normalises
#: `./notes.md` and `reports//notes.md` through `PurePosixPath` and would place
#: them. Refusing them costs nothing, because such a name can never MATCH an
#: artifact. The uploader names each artifact by its path relative to the
#: artifacts directory (`relative_to(...).as_posix()` in
#: `lifecycle._upload_outputs`), and that path never has an empty or "."
#: segment, so the worker would refuse the name one step later with "did not
#: produce an artifact named ...".
_UNSAFE_SEGMENTS = ("", ".", "..")

#: The longest `input_from` filename, in UTF-8 bytes. It is the worker's
#: `agent_worker.artifact_manifest.MAX_NAME_BYTES`, restated because swarm-api
#: cannot import `agent_worker`; tests/unit/control_plane/
#: test_input_from_submission.py holds the two equal.
#:
#: WHY THE MANIFEST'S BOUND (wave 2026-09-27, epic #227). An `input_from`
#: filename is also the name the upstream step's artifact is uploaded under, and
#: the manifest's worst-case arithmetic -- what keeps the upstream task's
#: document under 1 MiB -- is done for names of at most this many bytes. The
#: worker exempts a DECLARED name from the bound (#232 review) so that a name
#: nothing refused at submission does not fail every attempt; that exemption is
#: the backstop, and this is the refusal, where the caller can still fix it.
MAX_INPUT_FROM_NAME_BYTES = 256

#: The longest single path segment, in UTF-8 bytes: Linux's NAME_MAX. A longer
#: segment is a file neither the upstream agent can write nor the worker stage.
MAX_SEGMENT_BYTES = 255


def _filename_problem(filename: str) -> str | None:
    """Why `filename` cannot be staged into a workspace, or None if it can.

    `filename` has already been stripped, as the worker strips it.
    """
    if not filename:
        return "is empty"
    if filename.startswith("/"):
        return "is an absolute path"
    if "\\" in filename or "\x00" in filename:
        return "contains a backslash or a NUL character"
    for segment in filename.split("/"):
        if segment in _UNSAFE_SEGMENTS:
            return f"contains the path segment {segment!r}"
    size = len(filename.encode("utf-8"))
    if size > MAX_INPUT_FROM_NAME_BYTES:
        return (
            f"is {size} bytes of UTF-8, over the {MAX_INPUT_FROM_NAME_BYTES}-byte "
            "bound the worker holds an artifact's name to"
        )
    for segment in filename.split("/"):
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            return (
                f"has a path segment over the {MAX_SEGMENT_BYTES} bytes Linux allows "
                "one file name"
            )
    return None


#: How much of an over-bound or otherwise malformed `input_from` filename to
#: echo back verbatim. `raw` is caller-controlled and unbounded -- that is
#: exactly the shape `_filename_problem` refuses -- so the refusal shows a
#: short prefix plus the true byte length rather than the whole string.
_ECHO_PREFIX_BYTES = 80


def _short_echo(raw: object) -> str:
    """A safe-to-echo stand-in for a filename too big (or wrong-shaped) to quote whole."""
    quoted = repr(raw)
    text = raw if isinstance(raw, str) else quoted
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _ECHO_PREFIX_BYTES:
        return quoted
    prefix = encoded[:_ECHO_PREFIX_BYTES].decode("utf-8", errors="ignore")
    return f"{prefix!r}... ({len(encoded)} bytes)"


def _distinct_name(source: str, filename: str) -> str:
    """The filename the refusal suggests: the same name, prefixed with the parent's step id."""
    path = PurePosixPath(filename)
    return str(path.with_name(f"{source}-{path.name}"))


def validate_staged_filenames(step: StepSpec) -> None:
    """Refuse an `input_from` whose files cannot all land in this step's workspace.

    There are two refusals. Each names the step, the parent and the filename,
    because those three strings are what a person needs to fix the request:

    * a filename that is empty, absolute, or has an empty, "." or ".." segment;
    * two or more parents staging the SAME filename. `input_from` names the
      artifact in the upstream's prefix AND the path it lands at here, so there
      is no way to say "take both": one would overwrite the other. The fix is
      on the upstream side: each parent writes its own filename;
    * one parent's filename a DIRECTORY of another's, `out` and `out/notes.md`
      (#71): `out` would have to be a file and a directory at once, so one of
      the two can never be staged, and the worker failed the step as a crash
      after both parents had run. Compared by whole segments, so `out` and
      `outline.md` are two siblings. The worker's `_assert_distinct_destinations`
      compares names only; it stays the backstop for the same-name case.

    A step that opted in to `input_layout: "by_parent"` (#75) is checked for
    the first refusal only: its files land under their parents' step ids, so
    the clashes cannot happen.

    Filenames are compared after `.strip()`, which is what the worker compares.
    The problems with one filename are reported first, then a same-name clash,
    then a directory clash.
    """
    landing: dict[str, list[str]] = {}
    for source, raw in step.input_from.items():
        filename = raw.strip() if isinstance(raw, str) else ""
        problem = _filename_problem(filename)
        if problem is not None:
            echoed = _short_echo(raw)
            raise DagError(
                f"step {step.step_id!r} stages input from {source!r} as {echoed}, which "
                f"{problem}. An input_from filename is where the artifact lands in "
                "this step's workspace, so it must be a relative path inside it, "
                "such as 'notes.md' or 'reports/notes.md', with no empty, '.' or "
                "'..' segment. The worker would refuse it at run time, after "
                f"{source!r} had already run.",
                detail={
                    "step_id": step.step_id,
                    "input_from": source,
                    "filename": echoed,
                    "problem": problem,
                },
            )
        landing.setdefault(filename, []).append(source)

    if step.input_layout == INPUT_LAYOUT_BY_PARENT:
        # Every file lands at `<parent step id>/<filename>` (#75). A parent
        # appears once in `input_from`, step ids are unique and are one path
        # segment each (`WorkflowStepCreate.step_id`'s pattern), so no two
        # destinations can be equal or one a directory of another: the two
        # clash rules below have nothing left to find. The worker still checks
        # destinations (`_assert_distinct_destinations`) as defence in depth.
        return

    for filename, sources in landing.items():
        if len(sources) < 2:
            continue
        parents = sorted(sources)
        suggestion = " and ".join(repr(_distinct_name(p, filename)) for p in parents[:2])
        raise DagError(
            f"step {step.step_id!r} stages {filename!r} from {len(parents)} upstream "
            f"steps ({', '.join(repr(p) for p in parents)}). An input_from filename "
            "is both the artifact's name in the upstream step and the path it lands "
            "at in this step's workspace, so these files would overwrite one another. "
            "The worker refuses such a step at run time, after every one of those "
            "upstream steps has already run. Give each parent a distinct artifact "
            f"filename (have each upstream step write its own, e.g. {suggestion}) "
            "and stage those instead.",
            detail={
                "step_id": step.step_id,
                "filename": filename,
                "colliding_upstream_steps": parents,
            },
        )

    # Every name is distinct by now, so each has exactly one parent. Sorted, so
    # the same request is always refused over the same pair.
    for inner in sorted(landing):
        segments = inner.split("/")
        for depth in range(1, len(segments)):
            outer = "/".join(segments[:depth])
            if outer not in landing:
                continue
            outer_source, inner_source = landing[outer][0], landing[inner][0]
            suggestion = " and ".join(
                repr(_distinct_name(source, name))
                for source, name in ((outer_source, outer), (inner_source, inner))
            )
            raise DagError(
                f"step {step.step_id!r} stages {outer!r} from {outer_source!r} and "
                f"{inner!r} from {inner_source!r}, so {outer!r} would have to be a "
                "file and a directory at once, and one of the two can never be "
                "staged. An input_from filename is both the artifact's name in the "
                "upstream step and the path it lands at in this step's workspace. "
                "The worker fails such a step at run time, after both upstream "
                "steps have already run. Give each parent a distinct artifact "
                "filename that is not a directory of the other (have each upstream "
                f"step write its own, e.g. {suggestion}) and stage those instead.",
                detail={
                    "step_id": step.step_id,
                    "filename": outer,
                    "nested_filename": inner,
                    "colliding_upstream_steps": sorted({outer_source, inner_source}),
                },
            )


def input_layout_in(
    metadata: Mapping[str, Any] | None, *, path: str, step_id: str | None = None
) -> str | None:
    """The `input_layout` a metadata block sets, None when it sets none; refused if invalid.

    PRESENT IS A CHOICE, whatever the value, so `null` is refused like a typo
    rather than read as "unset": a caller who wrote the key meant something,
    and the only safe reading of a value that is not one of `INPUT_LAYOUTS` is
    none. The refusal names the path and every accepted value.
    """
    if not metadata or INPUT_LAYOUT_METADATA_KEY not in metadata:
        return None
    value = metadata[INPUT_LAYOUT_METADATA_KEY]
    if isinstance(value, str) and value in INPUT_LAYOUTS:
        return value
    detail: dict[str, Any] = {"path": path, "accepted": list(INPUT_LAYOUTS)}
    message = (
        f"{path} is {_short_echo(value)}; accepted values are "
        + ", ".join(repr(v) for v in INPUT_LAYOUTS)
        + f". {INPUT_LAYOUT_BY_PARENT!r} stages each input_from file at "
        "<parent step id>/<filename>; "
        f"{INPUT_LAYOUT_BY_NAME!r}, the default, at the filename"
    )
    if step_id is not None:
        detail["step_id"] = step_id
        message = f"step {step_id!r}: {message}"
    raise DagError(message, detail=detail)


def resolve_input_layout(
    workflow_metadata: Mapping[str, Any] | None,
    step_metadata: Mapping[str, Any] | None,
    *,
    step_id: str,
) -> str:
    """A step's `input_layout`: its own, else the workflow's, else `DEFAULT_INPUT_LAYOUT`.

    The step's own wins because the workflow's value is copied onto every
    step's task and the step's is merged over it -- this is the value the
    step's task stores.
    """
    workflow = input_layout_in(workflow_metadata, path=f"metadata.{INPUT_LAYOUT_METADATA_KEY}")
    own = input_layout_in(
        step_metadata,
        path=f"steps[{step_id}].metadata.{INPUT_LAYOUT_METADATA_KEY}",
        step_id=step_id,
    )
    return own or workflow or DEFAULT_INPUT_LAYOUT


def find_cycle(steps: Iterable[StepSpec]) -> list[str] | None:
    """Return one concrete cycle as a path, or None.

    Iterative three-colour DFS: recursion would blow the stack on a pathological
    submission long before `max_workflow_steps` made it impossible, and a stack
    overflow in the request path is a denial of service.
    """
    edges: dict[str, tuple[str, ...]] = {s.step_id: tuple(s.depends_on) for s in steps}
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {node: WHITE for node in edges}

    for root in edges:
        if colour[root] != WHITE:
            continue
        # (node, index-into-its-edges); the path mirrors the GREY nodes.
        stack: list[list[Any]] = [[root, 0]]
        path: list[str] = [root]
        colour[root] = GREY
        while stack:
            node, index = stack[-1]
            neighbours = edges.get(node, ())
            if index >= len(neighbours):
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            stack[-1][1] = index + 1
            nxt = neighbours[index]
            if nxt not in colour:
                continue                      # validated separately as a missing dep
            if colour[nxt] == GREY:
                start = path.index(nxt)
                return path[start:] + [nxt]
            if colour[nxt] == WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append([nxt, 0])
    return None


def topological_order(steps: Sequence[StepSpec]) -> list[str]:
    """Kahn's algorithm, ties broken by submission order for determinism."""
    order_index = {s.step_id: i for i, s in enumerate(steps)}
    remaining = {s.step_id: set(s.depends_on) for s in steps}
    dependents: dict[str, list[str]] = {s.step_id: [] for s in steps}
    for step in steps:
        for dep in step.depends_on:
            dependents[dep].append(step.step_id)

    ready = sorted((sid for sid, deps in remaining.items() if not deps), key=order_index.get)
    out: list[str] = []
    while ready:
        node = ready.pop(0)
        out.append(node)
        for child in dependents[node]:
            remaining[child].discard(node)
            if not remaining[child]:
                ready.append(child)
                ready.sort(key=order_index.get)
    if len(out) != len(steps):
        # validate_dag calls find_cycle first, so this is unreachable from the
        # request path; it is a guard against a future caller skipping that.
        raise DagError("workflow dependency graph contains a cycle")
    return out


# --------------------------------------------------------------------------
# `await-input` steps (docs/design/human-steps.md §4.1, lane HW-API-SUBMIT)
# --------------------------------------------------------------------------

#: The catalogue profile of a step that waits for a tenant member's answer
#: (contract request 94). The only profile `await_input` is valid on, and it
#: requires one.
AWAIT_INPUT_PROFILE = "await-input"

#: The key of the step's declaration inside `metadata.dispatch` (§4.7).
AWAIT_INPUT_FIELD = "await_input"

#: The longest a step may wait: 24 hours, the owner's Q4, which is the bound
#: the scheduler holds a `HUMAN_REQUIRED` park to
#: (`apps/scheduler/scheduler/loop.py`, `HUMAN_PARK_MAX_SECONDS`). swarm-api
#: does not carry the scheduler, so the number is restated here, and
#: tests/unit/control_plane/test_await_input_submission.py imports both and
#: fails if they differ.
AWAIT_INPUT_MAX_SECONDS = 86_400
#: The shortest: five minutes. A shorter wait is a race with the person
#: opening the page, not a question.
AWAIT_INPUT_MIN_SECONDS = 300
#: A reminder at most hourly (§4.5): the bell is derived on read, and an
#: item that advanced every few minutes would be noise, not a reminder.
AWAIT_INPUT_REMIND_MIN_SECONDS = 3_600
#: Files one answer may carry (Q6, §4.1): ten keeps the manifest entry and
#: the upload form small.
AWAIT_INPUT_MAX_FILES = 10
#: Addresses whose bell a wait rings (§4.5). Not who may answer: any member
#: may (Q2).
AWAIT_INPUT_MAX_REMIND = 10
#: A declared default is part of the signed spec, so it is held to a size
#: the dispatch block can carry (§4.1).
AWAIT_INPUT_DEFAULT_MAX_BYTES = 16 * 1024

#: The answer's shapes, and the artifact each is written as when the author
#: names none (§4.1).
AWAIT_INPUT_ARTIFACTS: Mapping[str, str] = {
    "verdict": "verdict.json",
    "answers": "answers.json",
    "text": "input.json",
}
AWAIT_INPUT_FORMS = tuple(AWAIT_INPUT_ARTIFACTS)
AWAIT_INPUT_ON_EXPIRE = ("fail", "default")

#: The upstream file a `form: "answers"` step must stage: the agent's
#: questions are the form only when the step stages them explicitly (Q11).
QUESTIONS_FILENAME = "questions.json"

#: The worker's bounds on a verdict (`agent_worker/verdict.py`,
#: `MAX_FINDINGS`, `MAX_FINDING_CHARS`), which a verdict default is held to
#: so a downstream `when` reads it as it reads an agent's (§4.6).
AWAIT_VERDICT_MAX_FINDINGS = 50
AWAIT_VERDICT_MAX_FINDING_CHARS = 1_000
#: TASK-ANSWERS' bound on one answer's free text (§4.6).
AWAIT_ANSWER_MAX_TEXT_CHARS = 2_000

#: §4.1: a filename dependants stage by, and whose stem names the
#: attachments' directory, so no separator, no leading dot.
_AWAIT_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_AWAIT_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

#: Every key the block may carry, in the order a refusal checks them.
AWAIT_INPUT_KEYS = (
    "form", "artifact", "max_files", "expires_after_seconds", "on_expire",
    "default", "remind_after_seconds", "remind",
)

#: The first draft's who-may-answer list. Q2 replaced it with tenant
#: membership, so it is refused BY NAME, pointing at `remind`: a caller who
#: meant it as a restriction learns it is not one rather than believing it
#: holds (§4.1).
AWAIT_INPUT_RETIRED_KEYS = ("assignees",)

#: A step's fields that mean something only to a container, and so nothing
#: on an `await-input` step, which never runs one (§4.1). Each is refused
#: naming itself when set to anything but its default.
AWAIT_STEP_REFUSED_FIELDS = (
    "resource_class", "timeout_seconds", "builds_on", "when",
    "allow_empty_diff", "pr_role", "merges",
)


class AwaitInputRefused(ValidationFailed):
    """422 `validation_failed`: an `await-input` step, or its block, refused.

    `detail.field` names what was refused (`await_input.<key>` inside the
    block, the step's own field otherwise), and `detail.step_id` the step.
    The established code, not a new one: a new 4xx code ships behind a
    switch (`swarm_api.refusals`, test_new_refusals_ship_off.py), and the
    field is what a caller branches on here.
    """


def _await_refused(step_id: str | None, field_name: str, message: str) -> AwaitInputRefused:
    detail: dict[str, Any] = {"field": field_name}
    prefix = ""
    if step_id is not None:
        detail["step_id"] = step_id
        prefix = f"step {step_id!r}: "
    return AwaitInputRefused(prefix + message, detail=detail)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def refuse_control_plane_outside_workflow(
    profile: RunnerProfile, *, workflow_id: str | None
) -> None:
    """A `control_plane` profile is a workflow step or nothing (§4.1).

    Its task is ended by the platform on a person's answer, and what it
    hands on is staged by the steps after it. As a task, a batch entry or a
    child it would have no step to answer and nobody to hand anything to.
    """
    if profile.control_plane and workflow_id is None:
        raise _await_refused(
            None, "runner_profile",
            f"runner_profile {profile.name!r} is a workflow step that waits for a "
            "person's answer; it is valid only inside a workflow (POST /v1/workflows)",
        )


def refuse_control_plane_integrator(
    profile: RunnerProfile, *, step_id: str
) -> None:
    """Under `integrate`, the final step applies the others' patches and opens
    the pull request. A `control_plane` step runs nothing, so it cannot."""
    if profile.control_plane:
        raise _await_refused(
            step_id, "runner_profile",
            f"runner_profile {profile.name!r} runs no container, so it cannot be "
            "the step that integrates the others under strategy 'integrate'. Add "
            "the step that acts on the answer after it.",
        )


def _await_default(
    form: str, default: Any, *, step_id: str
) -> dict[str, Any]:
    """`default`, checked against `form` (§4.1, §4.6), normalised."""
    field_name = "await_input.default"

    def refused(message: str) -> AwaitInputRefused:
        return _await_refused(step_id, field_name, message)

    if not isinstance(default, dict):
        raise refused("await_input.default must be a JSON object shaped like the form")
    allowed = {"verdict": {"verdict", "findings"}, "answers": {"answers"}, "text": {"text"}}[form]
    extra = sorted(set(default) - allowed)
    if extra:
        raise refused(
            f"await_input.default for form {form!r} takes only {sorted(allowed)}, "
            f"not {extra}; a default carries no files"
        )
    try:
        encoded = json.dumps(default, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise refused("await_input.default is not plain JSON") from None
    if len(encoded) > AWAIT_INPUT_DEFAULT_MAX_BYTES:
        raise refused(
            f"await_input.default is {len(encoded)} bytes; at most "
            f"{AWAIT_INPUT_DEFAULT_MAX_BYTES} are signed into the step"
        )
    if form == "verdict":
        verdict = default.get("verdict")
        if verdict not in REVIEW_VERDICTS:
            raise refused(f"await_input.default.verdict must be one of {list(REVIEW_VERDICTS)}")
        findings = default.get("findings", [])
        if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
            raise refused("await_input.default.findings must be a list of strings")
        if len(findings) > AWAIT_VERDICT_MAX_FINDINGS:
            raise refused(
                f"await_input.default.findings has {len(findings)} entries; at most "
                f"{AWAIT_VERDICT_MAX_FINDINGS}"
            )
        if any(len(f) > AWAIT_VERDICT_MAX_FINDING_CHARS for f in findings):
            raise refused(
                f"each of await_input.default.findings is at most "
                f"{AWAIT_VERDICT_MAX_FINDING_CHARS} characters"
            )
        return {"verdict": verdict, "findings": list(findings)}
    if form == "answers":
        answers = default.get("answers")
        if not isinstance(answers, list) or not answers:
            raise refused("await_input.default.answers must be a non-empty list")
        normalised = []
        for position, answer in enumerate(answers):
            where = f"await_input.default.answers[{position}]"
            if not isinstance(answer, dict):
                raise refused(f"{where} must be an object")
            unknown = sorted(set(answer) - {"question_index", "choice", "text"})
            if unknown:
                raise refused(f"{where} takes question_index, choice and text, not {unknown}")
            index = answer.get("question_index")
            choice = answer.get("choice")
            text = answer.get("text")
            if not _is_int(index) or index < 0:
                raise refused(f"{where}.question_index must be a non-negative integer")
            if choice is not None and not isinstance(choice, str):
                raise refused(f"{where}.choice must be a string or null")
            if text is not None and not isinstance(text, str):
                raise refused(f"{where}.text must be a string or null")
            if not (choice or (text or "").strip()):
                raise refused(f"{where} needs a choice or a text")
            if text is not None and len(text) > AWAIT_ANSWER_MAX_TEXT_CHARS:
                raise refused(
                    f"{where}.text is at most {AWAIT_ANSWER_MAX_TEXT_CHARS} characters"
                )
            normalised.append({"question_index": index, "choice": choice, "text": text})
        return {"answers": normalised}
    text = default.get("text")
    if not isinstance(text, str):
        raise refused("await_input.default.text must be a string")
    return {"text": text}


def resolve_await_input(
    profile: RunnerProfile,
    raw: Any,
    *,
    step_id: str,
    container_fields: Mapping[str, Any],
    input_from: Mapping[str, str],
    submitted_by: str,
    allowed_domains: Iterable[str],
    allowed_users: Iterable[str] = (),
) -> dict[str, Any] | None:
    """One workflow step's `await_input`, checked and normalised (§4.1).

    Returns the block as `metadata.dispatch.await_input` stores it --
    every key present, defaults filled in, `remind` lower-cased -- or None
    for a step of any other profile, which may not carry one. Every refusal
    is a 422 `validation_failed` whose `detail.field` names the field.

    `container_fields` is the step's own `AWAIT_STEP_REFUSED_FIELDS`, by
    name; `allowed_domains` and `allowed_users` are who may sign in at all,
    which is what "an address in the platform's hosted domain" means for
    `remind` in a deployment with no domain of its own.
    """
    if profile.name != AWAIT_INPUT_PROFILE:
        if raw is not None:
            raise _await_refused(
                step_id, "await_input",
                f"await_input is valid only on a step whose runner_profile is "
                f"{AWAIT_INPUT_PROFILE!r}, not {profile.name!r}",
            )
        return None
    for name in AWAIT_STEP_REFUSED_FIELDS:
        value = container_fields.get(name)
        if value is not None and value is not False:
            raise _await_refused(
                step_id, name,
                f"{name} means nothing on an {AWAIT_INPUT_PROFILE!r} step, which runs "
                "no container; drop it",
            )
    if raw is None:
        raise _await_refused(
            step_id, "await_input",
            f"a step whose runner_profile is {AWAIT_INPUT_PROFILE!r} needs an await_input "
            f"block: form, expires_after_seconds and on_expire at least",
        )
    if not isinstance(raw, dict):
        raise _await_refused(step_id, "await_input", "await_input must be a JSON object")
    for key in AWAIT_INPUT_RETIRED_KEYS:
        if key in raw:
            raise _await_refused(
                step_id, f"await_input.{key}",
                "await_input.assignees is not a field: any member of the step's tenant "
                "may answer. To choose whose bell is rung, name them in "
                "await_input.remind; that rings them and restricts nobody.",
            )
    for key in raw:
        if key not in AWAIT_INPUT_KEYS:
            raise _await_refused(
                step_id, f"await_input.{key}",
                f"await_input does not take {key!r}; it takes {list(AWAIT_INPUT_KEYS)}",
            )

    form = raw.get("form")
    if form not in AWAIT_INPUT_FORMS:
        raise _await_refused(
            step_id, "await_input.form",
            f"await_input.form is required and must be one of {list(AWAIT_INPUT_FORMS)}",
        )

    artifact = raw.get("artifact", AWAIT_INPUT_ARTIFACTS[form])
    if not isinstance(artifact, str) or not _AWAIT_ARTIFACT_RE.match(artifact):
        raise _await_refused(
            step_id, "await_input.artifact",
            "await_input.artifact must be a filename of letters, digits, '_', '.' "
            "and '-', at most 128 characters, starting with a letter or a digit",
        )

    max_files = raw.get("max_files", AWAIT_INPUT_MAX_FILES)
    if not _is_int(max_files) or not 0 <= max_files <= AWAIT_INPUT_MAX_FILES:
        raise _await_refused(
            step_id, "await_input.max_files",
            f"await_input.max_files must be an integer from 0 to {AWAIT_INPUT_MAX_FILES}",
        )

    expires = raw.get("expires_after_seconds")
    if not _is_int(expires) or not (
        AWAIT_INPUT_MIN_SECONDS <= expires <= AWAIT_INPUT_MAX_SECONDS
    ):
        raise _await_refused(
            step_id, "await_input.expires_after_seconds",
            "await_input.expires_after_seconds is required: an integer from "
            f"{AWAIT_INPUT_MIN_SECONDS} to {AWAIT_INPUT_MAX_SECONDS} (24 hours)",
        )

    on_expire = raw.get("on_expire")
    if on_expire not in AWAIT_INPUT_ON_EXPIRE:
        raise _await_refused(
            step_id, "await_input.on_expire",
            f"await_input.on_expire is required and must be one of {list(AWAIT_INPUT_ON_EXPIRE)}",
        )

    default: dict[str, Any] | None = None
    if on_expire == "default":
        if raw.get("default") is None:
            raise _await_refused(
                step_id, "await_input.default",
                "on_expire 'default' needs await_input.default, the answer the step "
                "ends with when nobody answers",
            )
        default = _await_default(form, raw["default"], step_id=step_id)
    elif raw.get("default") is not None:
        raise _await_refused(
            step_id, "await_input.default",
            "await_input.default is used only with on_expire 'default'; with 'fail' "
            "drop it",
        )

    remind_after = raw.get("remind_after_seconds")
    if remind_after is not None and (
        not _is_int(remind_after)
        or not AWAIT_INPUT_REMIND_MIN_SECONDS <= remind_after <= expires
    ):
        raise _await_refused(
            step_id, "await_input.remind_after_seconds",
            "await_input.remind_after_seconds is null or an integer from "
            f"{AWAIT_INPUT_REMIND_MIN_SECONDS} to expires_after_seconds ({expires})",
        )

    remind_raw = raw.get("remind", [submitted_by])
    if not isinstance(remind_raw, list) or len(remind_raw) > AWAIT_INPUT_MAX_REMIND:
        raise _await_refused(
            step_id, "await_input.remind",
            f"await_input.remind must be a list of at most {AWAIT_INPUT_MAX_REMIND} addresses",
        )
    domains = {d.strip().lower() for d in allowed_domains if d.strip()}
    users = {u.strip().lower() for u in allowed_users if u.strip()}
    remind: list[str] = []
    for entry in remind_raw:
        address = entry.strip().lower() if isinstance(entry, str) else ""
        if not _AWAIT_ADDRESS_RE.match(address) or (
            address.rsplit("@", 1)[1] not in domains and address not in users
        ):
            raise _await_refused(
                step_id, "await_input.remind",
                f"await_input.remind entry {_short_echo(entry)} is not an address in "
                "this platform's allowed domains",
            )
        if address not in remind:
            remind.append(address)

    if form == "answers":
        sources = [src for src, name in input_from.items() if name == QUESTIONS_FILENAME]
        if len(sources) != 1:
            raise _await_refused(
                step_id, "await_input.form",
                f"form 'answers' asks an upstream agent's questions, so the step must "
                f"stage exactly one {QUESTIONS_FILENAME!r} in input_from; it stages "
                f"{len(sources)}",
            )

    return {
        "form": form,
        "artifact": artifact,
        "max_files": max_files,
        "expires_after_seconds": expires,
        "on_expire": on_expire,
        "default": default,
        "remind_after_seconds": remind_after,
        "remind": remind,
    }


# --------------------------------------------------------------------------
# Registered MCP servers (docs/design/registered-mcp-servers.md §4.3,
# lane MCP-SUBMIT, part of #1433)
# --------------------------------------------------------------------------
#
# A step names servers BY NAME in `input.mcp_servers` (contract request 88),
# never a URL, a header or a command (invariant 10); a per-run credential for
# a `dispatch` server travels beside the task in `mcp_credentials`, never in
# it. The registry reads are `SubmissionService`'s; what is here is pure, so
# `schedules.py` can hold the same rules with no Firestore client.
#
# Every refusal names the field, the rule and the server's NAME, never a
# credential's value, its length or any part of it.

#: The profile-declared input that names a step's servers.
MCP_SERVERS_INPUT_KEY = "mcp_servers"

#: `mcp_servers.MODE_DISPATCH`, the one auth mode whose key the caller sends
#: per run. Restated so this module imports no Firestore client;
#: tests/unit/control_plane/test_mcp_submit.py holds the two equal.
MCP_DISPATCH_MODE = "dispatch"


class McpServerUnregistered(ValidationFailed):
    """Not registered for this tenant, or registered and disabled: one code
    for both, so the answer does not say whether another tenant has the name."""

    code = "mcp_server_unregistered"


class McpServerProfileRefused(Forbidden):
    """Registered, but its `allowed_profiles` does not include this profile."""

    code = "mcp_server_profile_refused"


class McpServerNotInherited(ValidationFailed):
    """A child named a server its parent's signed input does not name (Q7):
    the agent writing a child's spec is the party a credential is kept from,
    so it may not widen what its tree reaches."""

    code = "mcp_server_not_inherited"


class McpCredentialMissing(ValidationFailed):
    """A `dispatch` server with no per-run credential to use."""

    code = "mcp_credential_missing"


class McpCredentialUnexpected(ValidationFailed):
    """A credential for a server the submission does not name, or for one
    whose key is not the caller's to send (`none`, `workspace_secret`)."""

    code = "mcp_credential_unexpected"


def mcp_server_names(payload: Mapping[str, Any] | None) -> list[str]:
    """The servers an input names, in order, once each. Its type and bounds
    are the catalogue's (`validate_runner_input`), checked before this runs."""
    raw = (payload or {}).get(MCP_SERVERS_INPUT_KEY)
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(n for n in raw if isinstance(n, str)))


def _mcp_detail(step_id: str | None, **detail: Any) -> dict[str, Any]:
    if step_id is not None:
        detail["step_id"] = step_id
    return detail


def _mcp_where(step_id: str | None) -> str:
    return f"step {step_id!r}: " if step_id is not None else ""


def refuse_unexpected_mcp_credentials(
    credentials: Mapping[str, Any] | None, modes: Mapping[str, Any],
) -> None:
    """Every key of `credentials` is a server the submission names whose
    auth mode is `dispatch`; `modes` maps every named server to its mode."""
    unexpected = sorted(k for k in (credentials or {}) if modes.get(k) != MCP_DISPATCH_MODE)
    if unexpected:
        raise McpCredentialUnexpected(
            "mcp_credentials carries a credential for a server the submission does not "
            "name in input.mcp_servers, or for one whose auth mode is not `dispatch` "
            "(its key is the workspace's, or it takes none); nothing was stored",
            detail={"field": "mcp_credentials", "servers": unexpected})


def refuse_missing_mcp_credentials(
    names: Sequence[str], modes: Mapping[str, Any], available: Iterable[str], *,
    step_id: str | None = None,
) -> None:
    """Every `dispatch` server in `names` has a credential in `available`."""
    have = set(available)
    missing = [n for n in names if modes.get(n) == MCP_DISPATCH_MODE and n not in have]
    if missing:
        raise McpCredentialMissing(
            f"{_mcp_where(step_id)}input.mcp_servers names a `dispatch` server whose "
            "per-run credential is the caller's to send in mcp_credentials, and none was "
            "sent; nothing was stored",
            detail=_mcp_detail(step_id, field="mcp_credentials", servers=missing))


def refuse_uninherited_mcp_servers(names: Sequence[str], parent_names: Sequence[str]) -> None:
    """A child's servers are a subset of its parent's (Q7)."""
    added = [n for n in names if n not in set(parent_names)]
    if added:
        raise McpServerNotInherited(
            "a child task may name only MCP servers its parent named; it cannot add one",
            detail={"field": "input.mcp_servers", "servers": added})

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
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.profiles import (
    INT64_MAX,
    INT64_MIN,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    InputRefused,
    RunnerProfile,
    check_inputs,
)

from .errors import ValidationFailed

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
    """
    return tuple(sorted({p.provider for p in RUNNER_PROFILES.values() if p.provider}))


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


def validate_resource_class_override(profile: RunnerProfile, requested: str | None) -> str:
    """A step may not pick a bigger box than its profile was built for.

    The only override permitted is to a resource class that exists AND whose
    sizing is no larger than the profile's own class in every dimension. This is
    still not a resource spec from the caller -- it is a choice among named,
    admin-defined classes -- but it cannot be used to grow a task.
    """
    if requested is None:
        return profile.resource_class
    target = RESOURCE_CLASSES.get(requested)
    if target is None:
        raise ValidationFailed(
            f"unknown resource_class {requested!r}",
            detail={"known_resource_classes": sorted(RESOURCE_CLASSES)},
        )
    base = RESOURCE_CLASSES[profile.resource_class]
    # Every dimension, which is what the guarantee above says. `disk_gib` was
    # missing: it is not exploitable with the current three-class catalogue,
    # where disk rises with cpu and memory, but the check is the stated promise
    # and a fourth class with a big disk and a small CPU would walk straight
    # through the gap.
    larger = [
        dimension
        for dimension, requested_value, base_value in (
            ("cpu", target.cpu, base.cpu),
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


def validate_input_size(payload: Any, max_bytes: int, *, label: str = "input") -> int:
    try:
        encoded = json.dumps(payload, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{label} is not JSON-serialisable: {exc}") from None
    size = len(encoded)
    if size > max_bytes:
        raise ValidationFailed(
            f"{label} is {size} bytes, over the {max_bytes} byte limit",
            detail={"bytes": size, "max_bytes": max_bytes},
        )
    return size


def validate_storable(payload: Any, *, label: str = "input", step_id: str | None = None) -> None:
    """Refuse an integer, anywhere in `payload`, that Firestore cannot store.

    Python reads a JSON integer of any length, and Firestore stores a signed
    64-bit one, so `{"n": 10**30}` passed every check this module made and
    raised when the task was written: a 500 at the store, where the caller
    learns nothing, instead of a 422 here that names the path (the review of
    #213). A declared runner input cannot get this far out of range --
    `RunnerInput` requires both bounds, inside the same range -- but a profile
    whose inputs are not declared yet (`browser`, `generic`, #218) is bounded
    by size alone, and a task's metadata by size and its reserved keys. This is
    the store's own limit, not a declaration, so it applies to every profile.

    Walked with a stack, not recursion: a payload nested a thousand deep is
    small enough to pass the size limit and deep enough to overflow Python's.
    """
    stack: list[tuple[str, Any]] = [(label, payload)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
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


def validate_runner_input(
    profile: RunnerProfile, payload: Mapping[str, Any], *, step_id: str | None = None
) -> None:
    """Refuse an `input` key the profile does not declare, or a value out of its bounds.

    THE CATALOGUE IS THE RULE, and this keeps no copy of it: the owner accepted
    contract request 25 on #142 (2026-09-25) as "`RunnerProfile.inputs` goes in
    the frozen catalogue and the API enforces it for every caller, not only the
    bridge". Until then the API bounded an input's SIZE and nothing else, so a
    script or the Submit form could send the mock `quota_exhausted` -- which
    parked on every attempt, and a park does not spend one -- or send
    `claude-code` a `model`, which its runner passes as `--model`.

    Every caller reaches this: `_build_task` calls it for a task and for each
    task of a batch, and `submit_workflow` for each step before any task is
    built. Values are checked, never rewritten: the input is stored as sent.

    EVERY PROFILE IS ASKED, and the answer is the shared rule's. A profile
    whose inputs are NOT DECLARED YET (`inputs is None`: `browser`, `generic`,
    open with the owner on #218) is bounded by size alone, as every profile was
    before, because its runner cannot start without keys nobody has decided on
    yet -- and that is `check_inputs`'s answer, not a branch here. The review
    of #213 found this function returning before it asked, while the shared
    rule refused every key for the same profiles: two answers to one question.
    The size is `validate_input_size`'s, which every caller runs first.
    """
    rest = {key: value for key, value in payload.items() if key != PROMPT_INPUT_KEY}
    try:
        check_inputs(profile, rest)
    except InputRefused as refused:
        # Only a profile that declares can refuse, so `inputs` is a mapping here.
        declared = profile.inputs or {}
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
DISPATCH_STRATEGIES = ("collect", "direct-pr", "integrate")

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

#: The key inside `task.metadata` naming the files a workflow step's dependants
#: stage from it (#149). Written by workflow expansion only, on each UPSTREAM
#: step's task (`swarm_api.expected_outputs.record_expected_outputs`). Defined
#: here, beside the other reserved keys, because `expected_outputs` imports this
#: module and the reservation below needs the key: the other way round is an
#: import cycle. `swarm_api.expected_outputs` re-exports it under the same name,
#: and tests/unit/control_plane/test_expected_outputs_seam.py holds it equal to
#: the worker's `agent_worker.expected_outputs.METADATA_KEY`.
EXPECTED_OUTPUTS_METADATA_KEY = "expected_outputs"

#: Every key inside `task.metadata` this service writes and a caller may not,
#: in the order a refusal names them. One tuple, checked by one function, so a
#: caller who sent several is told about all of them in one 422 rather than one
#: per round trip -- which is what #153's separate expected_outputs check did
#: until it was folded in here.
RESERVED_METADATA_KEYS = (
    DISPATCH_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    EXPECTED_OUTPUTS_METADATA_KEY,
)

#: Strategies and carriers that cannot work without somewhere to push to.
_NEEDS_REPOSITORY_STRATEGIES = ("direct-pr", "integrate")


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
#   * `ssh://`  -- a password (`user:pass@`). A bare user name is the ssh
#     login (`ssh://git@github.com/o/r`) and is not a credential.
#   * `git@host:path` -- the scp form. Its user is the fixed `git` login.

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
    userinfo, an ssh login name, and the scp form.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return None
    start = len(scheme) + len(sep)
    ends = [at for at in (rest.find("/"), rest.find("?"), rest.find("#")) if at >= 0]
    authority = rest[: min(ends)] if ends else rest
    at = authority.rfind("@")
    if at < 0:
        return None
    userinfo = authority[:at]
    if scheme.lower() == "ssh" and ":" not in userinfo:
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
        raise ValueError(
            "repository_url must not carry a credential (a user name or token before "
            "'@'); " + REPOSITORY_CREDENTIAL_PATH
        )
    return value


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
        return DispatchOptions(
            strategy=self.strategy,
            carrier=self.carrier,
            role=role,
            integrates=tuple(integrates),
        )

    def to_metadata(self) -> dict[str, Any]:
        """The `task.metadata["dispatch"]` block, exactly as the worker reads it."""
        block: dict[str, Any] = {"strategy": self.strategy, "carrier": self.carrier}
        if self.role is not None:
            block["role"] = self.role
        if self.integrates:
            block["integrates"] = list(self.integrates)
        return block


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
}


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
    present = [key for key in RESERVED_METADATA_KEYS if key in metadata]
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
    """
    options = DispatchOptions(
        strategy=_accepted_value("strategy", strategy, DISPATCH_STRATEGIES,
                                 "accepted_strategies"),
        carrier=_accepted_value("carrier", carrier, DISPATCH_CARRIERS,
                                "accepted_carriers"),
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


def resolve_integrator_step(steps: Sequence[StepSpec]) -> str:
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
    """
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


def validate_timeout(profile: RunnerProfile, requested: int | None) -> int:
    """A caller may shorten a timeout, never lengthen it past the profile's."""
    if requested is None:
        return profile.timeout_seconds
    if requested <= 0:
        raise ValidationFailed("timeout_seconds must be positive")
    return min(int(requested), profile.timeout_seconds)


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
         and no two parents of one step stage the same one
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
    return None


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
      on the upstream side: each parent writes its own filename.

    Filenames are compared after `.strip()`, which is what the worker compares.
    """
    landing: dict[str, list[str]] = {}
    for source, raw in step.input_from.items():
        filename = raw.strip() if isinstance(raw, str) else ""
        problem = _filename_problem(filename)
        if problem is not None:
            raise DagError(
                f"step {step.step_id!r} stages input from {source!r} as {raw!r}, which "
                f"{problem}. An input_from filename is where the artifact lands in "
                "this step's workspace, so it must be a relative path inside it, "
                "such as 'notes.md' or 'reports/notes.md', with no empty, '.' or "
                "'..' segment. The worker would refuse it at run time, after "
                f"{source!r} had already run.",
                detail={
                    "step_id": step.step_id,
                    "input_from": source,
                    "filename": raw,
                    "problem": problem,
                },
            )
        landing.setdefault(filename, []).append(source)

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

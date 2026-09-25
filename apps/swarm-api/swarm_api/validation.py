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
named.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, RunnerProfile

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
#: tests/unit/control_plane/test_input_from_is_reserved.py holds the two equal:
#: reserving a key the worker does not read would refuse nothing that matters.
INPUT_FROM_METADATA_KEY = "input_from"

#: Every key inside `task.metadata` this service writes and a caller may not,
#: in the order a refusal names them.
RESERVED_METADATA_KEYS = (DISPATCH_METADATA_KEY, INPUT_FROM_METADATA_KEY)

#: Strategies and carriers that cannot work without somewhere to push to.
_NEEDS_REPOSITORY_STRATEGIES = ("direct-pr", "integrate")


class DispatchOptionError(ValidationFailed):
    """422 `invalid_dispatch`: a refused dispatch option, or a reserved metadata key.

    The `metadata.input_from` reservation (#151) answers with this code too,
    rather than a new one. The owner's instruction was "reserved, like
    dispatch", and a caller branching on the code should read both reservations
    the same way. `detail.reserved_metadata_keys` says which key it was.
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

    HOW THE SERVICE'S OWN WRITES GET PAST THIS: ORDER, NOT A FLAG. This runs on
    the caller's metadata only, before either key is added. `_build_task` calls
    it, then adds `dispatch`. `submit_workflow` calls it on the workflow's own
    metadata, and adds `input_from` to a step's task after `_build_task` has
    returned. Nothing a caller sends can reach the store under either key.

    Every reserved key present is named in the detail, in RESERVED_METADATA_KEYS
    order, so a caller who sent two learns about both from one refusal.
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
    input_from: tuple[str, ...] = ()


class DagError(ValidationFailed):
    code = "invalid_dag"


def validate_dag(steps: Sequence[StepSpec], *, max_steps: int) -> list[str]:
    """Validate and topologically order a submitted workflow.

    Checks, in the order a caller most wants to hear about them:
      1. at least one step, and no more than `max_steps`
      2. step ids are non-empty and unique
      3. no step depends on itself
      4. every `depends_on` names a step IN THIS WORKFLOW
      5. every `input_from` source is also an upstream dependency
      6. the graph is acyclic

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

    cycle = find_cycle(steps)
    if cycle:
        raise DagError(
            "workflow dependency graph contains a cycle: " + " -> ".join(cycle),
            detail={"cycle": cycle},
        )
    return topological_order(steps)


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

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
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.profiles import (
    INT64_MAX,
    INT64_MIN,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    InputRefused,
    RunnerProfile,
    WorkerAction,
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

    EXCEPT A WORKER ACTION'S PROVIDER (`APP_CREDENTIAL_PROVIDERS`). Both
    credential routes accept exactly this set (`routes/tenants.py`,
    `routes/admin.py`), and a credential registered through them is granted to
    the tenant's ORDINARY worker account (`credentials._grant_accessor`), whose
    token any agent of the tenant can mint. `git-merge` and `git-review` are
    GitHub App keys that only the merge and post-verdict Jobs' own service
    accounts may read (contract request 33's #364 amendment, accepted
    2026-10-01), so neither may ever enter through that path.

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
#: remembers this line. An App key is read by its own Job's service account at
#: action time and is never registered against the worker account.
#: `scripts/register-tenant.sh` reads this set to refuse binding one to the
#: worker; the forge token is the one worker-action provider the worker DOES
#: read, so it is not in it.
#:
#: `git-merge` STAYS IN THE SET after contract request 47 retired it from the
#: catalogue (`RETIRED_APP_CREDENTIAL_PROVIDERS`). A `-git-merge` secret that
#: exists holds a GitHub App key; dropping the name here would let
#: `register-tenant.sh` grant the tenant's worker account read on it, which is
#: the one thing that secret's design forbids. It goes when the merge account
#: leaves `terraform/modules/service_account_ids`.
RETIRED_APP_CREDENTIAL_PROVIDERS: frozenset[str] = frozenset({"git-merge"})
APP_CREDENTIAL_PROVIDERS: frozenset[str] = frozenset(
    p.provider for p in RUNNER_PROFILES.values()
    if p.worker_action is not None and p.provider and p.provider != FORGE_PROVIDER
) | RETIRED_APP_CREDENTIAL_PROVIDERS


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
    rest = {key: value for key, value in payload.items() if key != PROMPT_INPUT_KEY}
    try:
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
#:   off   append none, and refuse a spec that states one: the two disagree.
#:   absent  the platform's `merge_by_default` decides whether one is
#:         appended; a spec's own merge step is honoured whatever it says.
MERGE_METADATA_KEY = "merge"
MERGE_CHOICES = ("on", "off")

#: The signed dispatch block on a merge step naming what it merges. The
#: worker spells it `agent_worker.merge.MERGE_TARGET_FIELD`;
#: tests/unit/worker/test_merge_action.py holds the two equal.
MERGE_TARGET_FIELD = "merge_target"

#: The forge hosts a merge step can act on: github.com, the only host the
#: tenant's token is ever sent to (`agent_worker.forge.may_receive_forge_token`,
#: #307), where `agent_worker.merge.GitHubMerger` is the one `ForgeMerger`.
#: Another host is refused at SUBMISSION, never at merge time. The worker's
#: `merge.MERGEABLE_HOSTS` is held equal to this by test_merge_action.py.
MERGE_FORGE_HOSTS = frozenset({"github.com", "www.github.com"})

#: Attempts a merge step gets. A required check still running fails the
#: attempt retryably and the step waits READY, holding nothing, for the
#: worker's `CHECKS_PENDING_RETRY_SECONDS` (300 s) before reading again; ten
#: attempts is about 45 minutes of CI, against the three a task gets by
#: default. The API's own ceiling on `max_attempts` is 10.
MERGE_STEP_MAX_ATTEMPTS = 10

#: The step id an appended merge step takes, suffixed when a step already has it.
MERGE_STEP_ID = "merge"


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
    merge_target: tuple[tuple[str, str], ...] = ()

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
    ) -> "DispatchOptions":
        """This step's `builds_on`, verdict gate and empty-diff permission,
        already resolved to task ids. `pr_label` is kept only on a gated step."""
        return replace(
            self,
            builds_on=builds_on,
            gate_task_id=gate_task_id,
            gate_verdicts=tuple(gate_verdicts) if gate_task_id else (),
            allow_empty_diff=bool(allow_empty_diff),
            pr_label=pr_label if gate_task_id and pr_label else None,
        )

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
        pull_request: str,
        review: str | None = None,
        verdict_file: str | None = None,
    ) -> "DispatchOptions":
        """The `merge` step's target, already resolved to task ids. No role:
        it runs no agent, clones nothing and is integrated by nobody."""
        target = [("pull_request", pull_request)]
        if review is not None and verdict_file is not None:
            target += [("review", review), ("verdict_file", verdict_file)]
        return replace(self, role=None, integrates=(), merge_target=tuple(target))

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
    # A `merge` step (contract request 47) comes after the integrator and
    # integrates nothing: the integrator is the sink of the OTHER steps.
    # `plan_merge` holds the merge step to depending on it.
    steps = [step for step in steps if _worker_action(step) is not WorkerAction.MERGE]
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
      says nothing about;
    * a gated step whose agent does not run writes no artifact, so a step that
      stages from it would fail every time the gate stayed shut;
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
    staged_by: dict[str, list[str]] = {}
    for step in steps:
        for source in step.input_from:
            staged_by.setdefault(source, []).append(step.step_id)

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
            if strategy == "integrate" and step.step_id != integrator_step_id:
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
            if staged_by.get(step.step_id):
                raise DagError(
                    f"step {step.step_id!r} is gated on a review verdict, so when the "
                    "verdict does not name it its agent does not run and it writes "
                    "no artifact -- but " + ", ".join(repr(s) for s in staged_by[step.step_id])
                    + " stage a file from it. A gated step cannot be an input_from "
                    "source.",
                    detail={"step_id": step.step_id, "staged_by": staged_by[step.step_id]},
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
            + ", or absent for the platform's default.",
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

    def depends_on(self) -> list[str]:
        if self.continued:
            return []
        return [self.pull_request] + ([self.review] if self.review else [])

    def input_from(self) -> dict[str, str]:
        return {self.review: self.verdict_file} if self.review and self.verdict_file else {}


@dataclass(frozen=True)
class MergePlan:
    merge_step: str
    sources: MergeSources


def merge_sources(
    steps: Sequence[StepSpec], strategy: str, continued_task: str | None = None
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
    """
    agents = [step for step in steps if not is_merge_step(step.runner_profile)]
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
    steps: Sequence[StepSpec], strategy: str, continued_task: str | None = None
) -> dict[str, Any] | None:
    """The `merge` step swarm-api appends to a workflow, or None if it opens no one PR.

    `depends_on` names every step that must finish first -- the step that
    opens the pull request, and the review when there is one -- and the
    review's verdict file is staged, so the merge reads the verdict the
    publishing step's gate read.
    """
    sources = merge_sources(steps, strategy, continued_task)
    if sources is None:
        return None
    taken = {step.step_id for step in steps}
    step_id, n = MERGE_STEP_ID, 1
    while step_id in taken:
        n += 1
        step_id = f"{MERGE_STEP_ID}-{n}"
    return {
        "step_id": step_id,
        "runner_profile": MERGE_STEP_ID,
        "depends_on": sources.depends_on(),
        "input_from": sources.input_from(),
    }


def plan_merge(
    steps: Sequence[StepSpec], strategy: str, continued_task: str | None = None
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
    sources = merge_sources(steps, strategy, continued_task)
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

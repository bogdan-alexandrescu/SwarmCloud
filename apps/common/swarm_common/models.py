"""Firestore document shapes for the control plane.

Firestore has no joins, so every document carries the denormalised fields the
scheduler needs to make an admission decision without a second read. The
scheduler's hot path reads exactly one query (READY tasks, ordered) plus the
slot-pool documents; nothing else.

Collection layout (`swarm` database, not `(default)`, so the project's default
database stays free for other teams):

    tenants/{tenant_id}
    tasks/{task_id}
    tasks/{task_id}/events/{event_id}
    attempts/{attempt_id}
    leases/{lease_id}
    pools/{pool_name}
    quota/{provider}:{tenant_id}
    workflows/{workflow_id}
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timezone
from typing import Any

from .states import TaskState, ParkReason, BlockedReason, EventType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# --------------------------------------------------------------------------
# Slot pools
# --------------------------------------------------------------------------

@dataclass
class SlotPool:
    """A named concurrency budget.

    `active` is the authoritative count of leases currently holding this pool.
    It is mutated ONLY inside the admission transaction and the release
    transaction, never by a background job, so it cannot drift under contention.

    `effective_limit` is the real ceiling and is always the minimum of the
    configured hard limit, the adaptive target, and any quota-derived cap.
    Adaptive logic may lower it; nothing may raise it above `hard_limit`.

    `hard_limit` is None when the pool's document carries none: nobody set the
    ceiling (contract request 38, #374). That is UNKNOWN -- not 0, which says
    an operator chose zero, and not unlimited. `effective_limit` and
    `available` are then None too, and `has_capacity` is False, so admission
    refuses through the pool (`BlockedReason.POOL_LIMIT_UNSET`).
    """

    name: str
    hard_limit: int | None
    adaptive_target: int | None = None
    quota_derived_limit: int | None = None
    active: int = 0
    enabled: bool = True
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def effective_limit(self) -> int | None:
        if self.hard_limit is None:
            return None
        candidates = [self.hard_limit]
        if self.adaptive_target is not None:
            candidates.append(self.adaptive_target)
        if self.quota_derived_limit is not None:
            candidates.append(self.quota_derived_limit)
        return max(0, min(candidates))

    @property
    def available(self) -> int | None:
        limit = self.effective_limit
        if limit is None:
            return None
        return max(0, limit - self.active)

    def has_capacity(self, units: int = 1) -> bool:
        limit = self.effective_limit
        return self.enabled and limit is not None and self.active + units <= limit


def pool_names_for(
    *,
    tenant_id: str,
    provider: str | None,
    resource_class: str,
    runner_profile: str,
    backend: str,
) -> list[str]:
    """Every pool a task must simultaneously acquire.

    Admission is all-or-nothing across this list. Reserving some and failing on
    the rest would leak capacity that nothing would ever release.
    """
    pools = [
        "global",
        f"tenant:{tenant_id}",
        f"resource:{resource_class}",
        f"runner:{runner_profile}",
        f"backend:{backend}",
    ]
    if provider:
        pools.append(f"provider:{provider}")
        # Provider quota is tracked per tenant because tenants bring their own
        # keys -- one tenant's 429 must not throttle another's.
        pools.append(f"provider:{provider}:tenant:{tenant_id}")
    return pools


#: What a lease takes in each `provider:*` pool, whatever its size. A provider
#: pool bounds concurrent LLM sessions against that provider's accounts, and an
#: agent is one session however large its runner is (contract request 129,
#: #2950). Every other pool bounds compute and takes the resource units.
PROVIDER_POOL_UNITS = 1


def pool_units_for(pool: str, units: int) -> int:
    """The units a lease of `units` resource units takes in `pool`.

    THE one rule. Reserve, release and every recount of a pool's `active` go
    through it (or through `held_pool_units`, which applies it), so what is
    taken and what is given back cannot drift apart.

      * `provider:<p>` and `provider:<p>:tenant:<t>`: 1. One agent is one
        session against the provider; a large runner is not four sessions.
      * `global`, `tenant:<t>`, `resource:<c>`, `runner:<p>`, `backend:<b>`:
        the resource units. Each bounds compute -- the platform's, a tenant's
        share of it, a size class, a profile, a backend's capacity -- and a
        large runner is four standard runners' worth of it.
    """
    return PROVIDER_POOL_UNITS if pool.startswith("provider:") else units


def reserved_pool_units(pools: list[str], units: int) -> dict[str, int]:
    """Per pool, what a lease reserved under the current rule takes."""
    return {name: pool_units_for(name, units) for name in pools}


def held_pool_units(
    pools: list[str], units: int, pool_units: dict[str, int] | None
) -> dict[str, int]:
    """Per pool, what a STORED lease holds: what release gives back.

    A lease reserved since request 129 records `pool_units`, and holds exactly
    that. A lease reserved before it has none and took its `units` in every
    pool, the provider pools included, so it holds `units` in each: giving
    back the new rule's 1 would leave the difference held for ever.
    """
    if pool_units is None:
        return {name: units for name in pools}
    return {name: int(pool_units.get(name, units)) for name in pools}


# --------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------

@dataclass
class Lease:
    """Authoritative record that a task holds capacity.

    The lease -- not the pod, not the Cloud Run execution -- is what concurrency
    accounting counts. Infrastructure is downstream of the lease, never upstream.
    """

    lease_id: str
    task_id: str
    attempt_id: str
    tenant_id: str
    generation: int
    pools: list[str]
    units: int
    state: TaskState
    created_at: datetime
    dispatch_deadline: datetime
    expires_at: datetime
    heartbeat_at: datetime | None = None
    released_at: datetime | None = None
    release_reason: str | None = None
    #: What the lease took in each pool (`reserved_pool_units`), written at
    #: reserve. None on a lease reserved before contract request 129, which
    #: took `units` in every pool.
    pool_units: dict[str, int] | None = None

    def held_units(self) -> dict[str, int]:
        """Per pool, what this lease holds and release gives back."""
        return held_pool_units(self.pools, self.units, self.pool_units)

    @property
    def is_released(self) -> bool:
        return self.released_at is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) > self.expires_at

    def dispatch_overdue(self, now: datetime | None = None) -> bool:
        """Past the dispatch deadline with no worker to show for it.

        NOT guarded on `state is LEASED`. `mark_dispatched`
        (scheduler/store.py) writes DISPATCHED to the lease the moment the
        backend ACCEPTS the create call -- long before a container runs -- so
        that guard excluded essentially every lease this method names. Measured
        on task_b5dc2568713a40158851 in saga-agents-staging on 2026-09-22:
        `dispatch_overdue` was still False 301 seconds after admission, and the
        `overdue_only=1` operator query returned an empty list at exactly the
        moment it was asked.

        A `heartbeat_at is None` conjunct is deliberately NOT added. The
        reconciler needs that distinction because it decides whether to fence a
        generation; this method answers the narrower question -- has the
        deadline passed -- and a lease that heartbeated and then went quiet past
        the deadline is still, factually, overdue.

        Changed 2026-09-22 by the owner's decision on contract change request 9.
        """
        return (now or utcnow()) > self.dispatch_deadline


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

class EndCause(str, Enum):
    """Why a task reached a FAILED or CANCELLED state, written by the writer that ended it.

    Contract request 23, ACCEPTED by the owner on 2026-09-25 (#185, the
    decisions comment, item 9). Until this existed the outcome ledger
    (`swarm_api.outcomes`) read `last_error` -- free text from four writers the
    API image does not carry -- and sorted it into classes by prefix. One case
    was not recoverable from the text at all: "an upstream workflow step did
    not succeed" is written after a FAILED parent and after a CANCELLED one,
    so a cancel somebody pressed read the same as a failure.

    Each value is written by exactly one kind of writer, beside `completed_at`:

      * the worker (`agent_worker.control`): TIMEOUT, OUTPUTS_MISSING,
        INPUTS_UNAVAILABLE, CANNOT_START, RUNNER_ERROR, CANCEL_REQUESTED,
        SPEC_SIGNATURE_INVALID, MERGE_REFUSED, MERGE_FAILED, VERDICT_REFUSED,
        VERDICT_FAILED, PUBLISH_REFUSED, and BUDGET_EXCEEDED beside TIMEOUT;
      * the reconciler (`repair_task_state`): LOST_WORKER, CANNOT_START,
        CANCEL_REQUESTED;
      * the scheduler: DISPATCH_FAILED, FAILED_PARENT, CANCELLED_PARENT,
        WORKFLOW_SWEEP, CANCEL_REQUESTED;
      * the API's cancel: CANCEL_REQUESTED, and CHILD_CASCADE for the
        children of the task it cancelled;
      * CHILD_CASCADE (request 41) is written by the API's cancel cascade and
        the scheduler's sweep, and by the worker or reconciler ending a child
        flagged by either -- every writer above that ends a task CANCELLED on
        its `cancel_requested` writes CHILD_CASCADE instead of
        CANCEL_REQUESTED when the task's `metadata.child_cascade` is set.

    SUCCEEDED carries None: nothing about a success needs a cause. So does a
    task that ended before this field existed, and one a writer ended for a
    reason with no value here (the worker's "runner stopped on SIGTERM"
    without a requested cancel); the ledger falls back to its text classifier
    for exactly those, never for a task that carries a cause.

    FAILED_PARENT and CANCELLED_PARENT are decided by each parent's OWN END,
    not its state: a CANCELLED parent that is itself a failure's cascade (or
    was taken by the workflow sweep) passes a failure down, so a failure's
    cascade stays FAILED_PARENT however many steps down it reaches (the review
    of PR #217). A cascade below a parent cancelled before this field existed,
    with no cancel flag, is written with None: the scheduler cannot name that
    parent's end, and the ledger splits the step by its chain of parents.

    INPUTS_UNAVAILABLE was not in the request as filed. It is the owner's
    decision item 4 on the same comment: the worker refusing to stage a
    declared `input_from` artifact (`agent_worker.errors.InputUnavailable`) is
    its own class, not a runner error -- 10 of dev's 13 "runner errors" on
    2026-09-25 were exactly that. CANCELLED_PARENT is the request's own, and
    what decision item 2 (split "after a cancel" from "after a failure") needs.

    SPEC_SIGNATURE_INVALID is contract request 34 (#342): the worker refused
    to run a spec swarm-api did not sign -- a signature that does not verify
    over the fetched document, a missing one outside the rollout window, or a
    key version that is not the platform's. A worker-action step's OWN spec
    failing this check is this cause too; an UPSTREAM step's spec failing it
    is contract request 33's own end cause instead (that entry names it; this
    one does not), with reason "upstream:<task id>:<why>" -- the same reason
    shape this cause uses for its own failures. Never retried: another
    attempt reads the same document. Every occurrence is either a tenant's
    agent rewriting a step or a platform bug, and both causes are alerted on.

    MERGE_REFUSED/MERGE_FAILED (contract request 33) and VERDICT_REFUSED/
    VERDICT_FAILED (contract request 35), ACCEPTED by the owner on 2026-10-01
    for #295, are written only by the worker, for a `worker_action` profile's
    own end: no runner ran, so RUNNER_ERROR would be false. REFUSED means a
    condition for acting was not met and nothing changed on the forge; FAILED
    means the action was allowed and the forge did not do it. The specific
    reason is a worker vocabulary, not a frozen one: `result_summary.merge.
    refusal` and `result_summary.verdict.refusal` (docs/merge-step.md §6, §6a).

    PUBLISH_REFUSED is contract request 29, ACCEPTED by the owner on
    2026-09-29 (#259) and applied 2026-10-02: the worker refused to publish --
    the branch it would push adds a credential in its final tree, or the
    agent's `pr-title.txt` is present and unusable. Both fail the attempt
    retryably, so the cause is the task's only once its attempts are spent.
    It names the refusal, never the value: the attempt's error names the
    file, never its content.
    CHILD_CASCADE is contract request 41, applied with request 14 (owner
    decision OD-B15-4, 2026-10-02): a CHILD task (`Task.parent_task_id` set)
    ended because of its parent -- the parent was cancelled, ended FAILED or
    DEAD_LETTERED, or its await outlived `child_await_max_seconds`. It is not
    CANCELLED_PARENT, which names an UPSTREAM workflow step, and not
    CANCEL_REQUESTED, which the outcome ledger reads as a cancel somebody
    pressed. The specific reason (`parent_cancelled`, `parent_ended`,
    `await_expired`) is event detail, not a frozen vocabulary.

    BUDGET_EXCEEDED is contract request 118 (BUDGET-CAP, #2459), accepted by
    the owner 2026-10-26 and applied 2026-10-27: the worker stopped the agent
    because it spent its per-agent budget, at a turn boundary after a
    checkpoint, and published what it had. Written only by the worker, beside
    TIMEOUT. Never retried by the worker: another attempt of the same step
    starts from the same budget, so a resume is a person's choice.

    FIX_LOOP_EXHAUSTED is contract request 126 (FL-CONTRACT, #2509), accepted
    by the owner 2026-10-27 and applied 2026-10-28: a workflow's `fix-loop`
    step whose review-fix rounds ended without a MERGE -- a person answered
    "stop", or the park for a person passed 7 days. Written only by swarm-api
    and the scheduler's `_expire_human_waits`; a failure of the work, not of
    the platform.
    """

    TIMEOUT = "timeout"
    CANNOT_START = "cannot_start"
    LOST_WORKER = "lost_worker"
    OUTPUTS_MISSING = "outputs_missing"
    INPUTS_UNAVAILABLE = "inputs_unavailable"
    DISPATCH_FAILED = "dispatch_failed"
    RUNNER_ERROR = "runner_error"
    CANCEL_REQUESTED = "cancel_requested"
    FAILED_PARENT = "failed_parent"
    CANCELLED_PARENT = "cancelled_parent"
    WORKFLOW_SWEEP = "workflow_sweep"
    SPEC_SIGNATURE_INVALID = "spec_signature_invalid"
    MERGE_REFUSED = "merge_refused"   # a condition for merging was not met; nothing changed on the forge
    MERGE_FAILED = "merge_failed"     # the merge was allowed, and the forge did not do it
    VERDICT_REFUSED = "verdict_refused"   # a condition for posting the review was not met; nothing changed on the forge
    VERDICT_FAILED = "verdict_failed"     # the post was allowed, and the forge did not do it
    PUBLISH_REFUSED = "publish_refused"   # the worker refused to publish: a credential in the final tree, or an unusable pr-title.txt
    CHILD_CASCADE = "child_cascade"   # the parent task was cancelled or ended, or its await expired
    BUDGET_EXCEEDED = "budget_exceeded"   # the worker stopped the agent: it spent its budget
    FIX_LOOP_EXHAUSTED = "fix_loop_exhausted"   # the fix loop's rounds ended without a MERGE


#: The shape of `Task.forge_credential`: a provider suffix for
#: `Tenant.secret_name`, never a secret name or a value. Anchored by
#: `fullmatch`, so it can name no other tenant's secret and no `-refresh` twin.
#: Contract request 54.
FORGE_CREDENTIAL = re.compile(r"git(-[ru]-[0-9a-f]{16})?")

#: The values of `Task.forge_access`. None on a task means "write".
FORGE_ACCESS = ("write", "read")


#: Contract request 95 (MULTI-REPO request A, docs/design/multi-repository-steps.md
#: §4.7), accepted by the owner 2026-10-17 on PR 1461. The directory an
#: additional repository is checked out under, `work/repos/<name>`: lower-case,
#: no separator, never "." or ".." (the first character cannot be a dot).
#: Anchor with `fullmatch`.
ADDITIONAL_REPOSITORY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")

#: The most entries `Task.additional_repositories` can HOLD: a guard on the
#: document's shape, NEVER the operative limit. What swarm-api enforces at
#: submission is the platform setting `max_additional_repositories` (default 5,
#: editable by an admin at run time, accepted range 1 to this ceiling), and a
#: step over it is refused `too_many_additional_repositories` naming the
#: current setting. The owner set this ceiling at 10 on 2026-10-17 so the
#: setting can rise without a contract change; a code constant here would
#: have been the limit nobody can edit live.
ADDITIONAL_REPOSITORIES_CEILING = 10

#: The forge slot an additional repository is cloned with: the tenant slot
#: ("git") or a user slot ("git-u-<16 hex>"). Never a repository slot
#: (`git-r-`), which only swarm-api reads (#1225); such a repository is
#: refused at submission (`additional_repository_unreadable`, the owner's Q2).
ADDITIONAL_FORGE_CREDENTIAL = re.compile(r"git(-u-[0-9a-f]{16})?")

#: Whose slot it is, as swarm-api's `ForgeSlot.source` spells it: the tenant's,
#: a personal tenant's owner's, or the submitting person's own. A label, never
#: a value.
ADDITIONAL_REPOSITORY_SOURCES = ("tenant", "owner", "person")

#: The keys of one entry, all required, no others.
ADDITIONAL_REPOSITORY_KEYS = (
    "name", "repo_id", "repository", "repository_url", "ref", "commit",
    "forge_credential", "forge_credential_source", "size_kib",
)

_REPO_ID = re.compile(r"repo_[0-9a-f]{16}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
#: GitHub's own spelling of owner/repo, as swarm-api's `parse_repository` reads it.
_OWNER_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}")
#: A ref as asked, kept for display: a branch, a tag or a 40-hex sha. Never an
#: option (a leading "-") and never whitespace or a control character.
_REF = re.compile(r"[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]{0,254}")
_ENTRY_SHAPES = {
    "name": ADDITIONAL_REPOSITORY_NAME,
    "repo_id": _REPO_ID,
    "repository": _OWNER_REPO,
    "ref": _REF,
    "commit": _COMMIT,
    "forge_credential": ADDITIONAL_FORGE_CREDENTIAL,
}


def check_additional_repositories(value: Any) -> None:
    """Raise ValueError unless `value` is a well-formed `Task.additional_repositories`.

    None (today's task) or a list of at most ADDITIONAL_REPOSITORIES_CEILING
    maps, each of exactly ADDITIONAL_REPOSITORY_KEYS: `name` matching
    ADDITIONAL_REPOSITORY_NAME and `repo_id` matching `repo_<16 hex>`, both
    unique in the list; `repository` an owner/repo and `repository_url`
    exactly "https://github.com/" + `repository`; `ref` a non-empty ref that
    is not an option; `commit` a 40-hex lower-case sha; `forge_credential`
    matching ADDITIONAL_FORGE_CREDENTIAL; `forge_credential_source` one of
    ADDITIONAL_REPOSITORY_SOURCES; `size_kib` a non-negative int, not a bool.

    The message names the key and the entry's position, never a value: the
    values are a tenant's repository names, and a document a tenant's agent
    can write is not trusted to be free of anything else.
    """
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError("additional_repositories is a list or None")
    if len(value) > ADDITIONAL_REPOSITORIES_CEILING:
        raise ValueError(
            f"additional_repositories holds at most {ADDITIONAL_REPOSITORIES_CEILING} entries"
        )
    names: set[str] = set()
    repo_ids: set[str] = set()
    for i, entry in enumerate(value):
        where = f"additional_repositories[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is a map")
        if set(entry) != set(ADDITIONAL_REPOSITORY_KEYS):
            raise ValueError(f"{where} has exactly the keys {ADDITIONAL_REPOSITORY_KEYS}")
        for key, pattern in _ENTRY_SHAPES.items():
            if not (isinstance(entry[key], str) and pattern.fullmatch(entry[key])):
                raise ValueError(f"{where}.{key} is not well formed")
        if entry["repository_url"] != "https://github.com/" + entry["repository"]:
            raise ValueError(f"{where}.repository_url is not https://github.com/<repository>")
        if entry["forge_credential_source"] not in ADDITIONAL_REPOSITORY_SOURCES:
            raise ValueError(
                f"{where}.forge_credential_source is one of {ADDITIONAL_REPOSITORY_SOURCES}"
            )
        size = entry["size_kib"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{where}.size_kib is a non-negative integer")
        if entry["name"] in names:
            raise ValueError(f"{where}.name repeats an earlier entry's")
        if entry["repo_id"] in repo_ids:
            raise ValueError(f"{where}.repo_id repeats an earlier entry's")
        names.add(entry["name"])
        repo_ids.add(entry["repo_id"])


#: Contract request 82 (workspace secrets, design §2.3). A workspace secret
#: is granted to a CONSUMER, spelt "<kind>:<name>". `profile:` names a key of
#: RUNNER_PROFILES, `tool:` a key of RUNTIME_TOOLS (request 81), `component:`
#: one of PLATFORM_COMPONENTS. Never an image, a command or a path
#: (invariant 10).
#:
#: Contract request 89 (registered MCP servers, design §5 M2): a fourth
#: kind, `mcp:`, names a server the tenant registered. Its name is checked
#: against MCP_SERVER_NAME and the reserved names here; membership in the
#: tenant's registry is checked by its reader, as for `profile:`.
SECRET_CONSUMER_KINDS: tuple[str, ...] = ("profile", "tool", "component", "mcp")

#: Platform steps that take a workspace secret themselves, outside any
#: agent. `judge` reads private/jev-key for TypeSafe judging; `merge` and
#: `indexer` are the worker's own steps; `git` takes forge tokens only and is
#: never offered a non-forge secret (design §6.2).
PLATFORM_COMPONENTS: tuple[str, ...] = ("judge", "merge", "indexer", "git")

_CONSUMER_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")

#: Contract request 90 (registered MCP servers, design §5 M3): the one
#: spelling of a registered MCP server's name. swarm-api writes it at
#: registration; the worker makes it a server key in the CLI's MCP config and
#: a path segment of its relay; the plugin reads it. Anchor with `fullmatch`.
#: 31 characters is the `mcp_servers` input's item bound (profiles.py).
MCP_SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
#: Names the worker registers itself; a registration may not take them, nor
#: any name starting with RESERVED_MCP_SERVER_PREFIX (`swarm-git`,
#: `swarm-graph`), so a tenant's server never shadows one of the platform's.
RESERVED_MCP_SERVER_NAMES: tuple[str, ...] = ("playwright", "jev")
RESERVED_MCP_SERVER_PREFIX = "swarm-"


def consumer_name(kind: str, name: str) -> str:
    """The one spelling of a secret consumer: "<kind>:<name>".

    Checks the kind, the name's characters and, for `component`, the closed
    list, and, for `mcp`, MCP_SERVER_NAME and the reserved names (request
    90). Membership of `profile` and `tool` names is checked against the
    catalogue by its reader (`profiles.py` imports this module, not the
    reverse, so this function cannot see RUNNER_PROFILES), and of `mcp`
    names against the tenant's registry by swarm-api. A consumer
    name holds only `[a-z0-9-]` and one `:`, so it is safe in a Firestore
    id and a URL segment without escaping."""
    if kind not in SECRET_CONSUMER_KINDS:
        raise ValueError(f"unknown secret consumer kind {kind!r}")
    if not _CONSUMER_NAME.fullmatch(name):
        raise ValueError(f"secret consumer name {name!r} is not [a-z0-9-]")
    if kind == "component" and name not in PLATFORM_COMPONENTS:
        raise ValueError(f"unknown platform component {name!r}")
    if kind == "mcp" and (
        not MCP_SERVER_NAME.fullmatch(name)
        or name in RESERVED_MCP_SERVER_NAMES
        or name.startswith(RESERVED_MCP_SERVER_PREFIX)
    ):
        raise ValueError(f"{name!r} is not a registrable MCP server name")
    return f"{kind}:{name}"


def parse_consumer(consumer: str) -> tuple[str, str]:
    """The inverse of consumer_name; raises on anything consumer_name would
    not have produced, so `parse_consumer(consumer_name(k, n)) == (k, n)`
    and no other string parses.

    Its message echoes nothing, because it is fed stored ids, so
    consumer_name's own refusal (which names the kind or name) is replaced
    rather than chained."""
    kind, sep, name = consumer.partition(":")
    try:
        spelt = consumer_name(kind, name) if sep else None
    except ValueError:
        spelt = None
    if spelt != consumer:
        raise ValueError("not a secret consumer name")
    return kind, name


@dataclass
class Task:
    id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime
    state: TaskState
    runner_profile: str
    resource_class: str
    input: dict[str, Any]
    submitted_by: str                       # verified email from the ID token
    provider: str | None = None
    model: str | None = None
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    repository_url: str | None = None
    repository_ref: str | None = None
    timeout_seconds: int = 3600
    max_attempts: int = 3
    attempt_count: int = 0
    next_eligible_at: datetime | None = None
    park_reason: ParkReason | None = None
    blocked_by: list[dict[str, Any]] = field(default_factory=list)
    current_lease_id: str | None = None
    current_generation: int = 0
    # Workflow membership. Null for a standalone task.
    workflow_id: str | None = None
    step_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    result_summary: dict[str, Any] | None = None
    latest_checkpoint: str | None = None
    #: Why the task ended, typed. See `EndCause` (contract request 23). None on
    #: every task that has not ended, on a success, and on anything written
    #: before 2026-09-25 -- an old document decodes exactly as it did.
    end_cause: EndCause | None = None
    #: swarm-api's signature over this task's canonical step spec (contract
    #: request 34, #342): standard base64 of the DER ECDSA signature that Cloud
    #: KMS returns for `specsign.spec_digest(specsign.canonical_step_spec(...))`.
    #: Written once, in the write that creates the document, and never after.
    #: None on every task written before swarm-api signed; the worker refuses
    #: such a task outside the rollout window.
    spec_signature: str | None = None
    #: The FULL resource name of the key version that made `spec_signature`:
    #: projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<n>.
    #: Written by the same write. A tenant can rewrite it, so the worker trusts
    #: it only as a lookup key among the versions the platform published.
    spec_key_version: str | None = None
    #: `specsign.SPEC_FORMAT` at signing. Read to choose the projection; it is
    #: also inside the signed bytes, so a rewrite to another format fails.
    spec_format: int | None = None
    #: The task whose agent submitted this one from inside a running attempt.
    #: None for work a person, a client or a workflow submitted. Contract
    #: request 14, applied 2026-10-02 (docs/design/child-tasks.md): SET BY
    #: swarm-api from the submitting attempt's attested registration, never
    #: supplied by a caller.
    parent_task_id: str | None = None
    #: The parent's ATTEMPT. A parent can be retried, and the children of attempt 1
    #: and attempt 2 are different work -- the second may re-create the first's.
    parent_attempt_id: str | None = None
    #: The forge secret this task's worker reads, as the provider suffix
    #: `Tenant.secret_name` places under the task's OWN tenant: "git" (the
    #: tenant token), "git-r-<16 hex>" (a repository token) or "git-u-<16 hex>"
    #: (a user's slot). Shape: `FORGE_CREDENTIAL`. Contract request 54 (request
    #: E, docs/onboarding.md §3.3), accepted by the owner 2026-10-07: WRITTEN BY
    #: swarm-api ONLY, at submission, from its resolution of the task's
    #: repository and `submitted_by` against the grants; never accepted from a
    #: caller. Covered by the spec signature (specsign format 3). None is
    #: today's behaviour, "git", and every document written before it decodes
    #: exactly as it did.
    forge_credential: str | None = None
    #: What the worker may do on the forge with it, one of `FORGE_ACCESS`
    #: (write or read). A "read" task is never given a push credential.
    #: Written by swarm-api only, in the same write, and signed with it. None is
    #: "write", today's behaviour, for backward compatibility.
    forge_access: str | None = None
    #: The live-browser hand-off this task's agent is waiting on, while its pod
    #: HOLDS (docs/design/live-browser.md section 4.3): `reason`, `expect`,
    #: `requested_at`, `deadline`, `controller` (None, or the person in control).
    #: None when nothing is waiting. The task stays RUNNING and keeps its lease
    #: while this is set, so it is counted as any RUNNING task is (invariants
    #: 1 and 3) and no reader of the state had to change. WRITTEN BY swarm-api
    #: ONLY, from a worker route signed with the attempt key; never accepted
    #: from a caller, and not in the spec signature: it is attempt state, not
    #: the task's spec. Contract request 70 (LB-C, the owner's Q2: the field,
    #: not a new state), accepted by the owner 2026-10-11 (#1030).
    human_wait: dict[str, Any] | None = None
    #: The registered repositories this task reads beside its own, each checked
    #: out read-only at `commit` in `work/repos/<name>` and never harvested
    #: (`collect`, `direct-pr` and `integrate` carry the primary only). Entry
    #: shape: `check_additional_repositories`. Contract request 95 (MULTI-REPO
    #: request A), accepted by the owner 2026-10-17 on PR 1461: WRITTEN BY
    #: swarm-api ONLY, in the creating write, from its resolution of the
    #: caller's `owner/repo` names against the tenant's registrations (gate:
    #: registered and `readable_by_tasks`) with each ref pinned to a commit;
    #: never accepted from a caller (invariant 10). Covered by the spec
    #: signature at format 4 (request 96). None is today's behaviour, and every
    #: document written before it decodes exactly as it did.
    additional_repositories: list[dict[str, Any]] | None = None
    #: Contract request 102 (#1435): the MODEL_CLASSES name this step asked
    #: for. None: the profile's default_model_class. Written by swarm-api at
    #: submission, only when it differs from that default; signed at spec
    #: format 5 (request 104). Resolved by profiles.step_model_class. A class
    #: is a name, never a model ID: `model` above is the ID, a record of the
    #: class's pin at submission. Last in the class, so no positional
    #: construction of a task changes meaning; every document written before
    #: it decodes exactly as it did.
    model_class: str | None = None
    #: Contract request 120 (BUDGET-CAP, #2459), accepted by the owner
    #: 2026-10-26: the attempt's RESOLVED per-agent budget, `usd` and/or
    #: `tokens` -- min(step, workspace default, platform ceiling), each unit on
    #: its own. Written by swarm-api at submission, only when the step or its
    #: workspace sets a budget; signed at spec format 6. None: no budget,
    #: today's behaviour. It only lowers a limit and chooses no resource
    #: (invariant 10). Before this field the same figures were pinned in
    #: `metadata.dispatch.budget`, which readers fall back to for one release.
    budget: dict[str, float | int] | None = None
    #: Consecutive dispatches the platform refused (PLATFORM_REFUSED parks).
    #: Reset to 0 by a dispatch that succeeds. Contract request 123, accepted
    #: by BGD 2026-10-28. Last in the class, after `budget`, so no positional
    #: construction of a task changes meaning; absent on every older
    #: document, which decodes as 0.
    platform_refusals: int = 0
    #: Contract request 131 (TIMEOUT-ASKED-CONTRACT, part of #2688), the
    #: owner's "API records the ask" (2026-11-01): the `timeout_seconds` the
    #: spec asked for, written by swarm-api at submission ONLY when it stored
    #: a lower `timeout_seconds` (the profile's cap, or a child's parent's).
    #: A RECORD ONLY: the limit is `timeout_seconds` alone, and nothing reads
    #: this as one (invariant 10); the worker reports it in the runner's
    #: `limits.clamped`. Unsigned: written at submission, it chooses nothing.
    #: None on a task that was not lowered and on every older document. Last
    #: in the class, after `platform_refusals`, so no positional construction
    #: of a task changes meaning.
    timeout_seconds_asked: int | None = None

    def __post_init__(self) -> None:
        # Only the fields of requests 54 and 95: a document decoded before them
        # is never refused here, and a writer cannot construct a malformed one.
        if self.forge_credential is not None and not (
            isinstance(self.forge_credential, str)
            and FORGE_CREDENTIAL.fullmatch(self.forge_credential)
        ):
            raise ValueError(
                "forge_credential is 'git', 'git-r-<16 hex>' or 'git-u-<16 hex>'"
            )
        if self.forge_access is not None and self.forge_access not in FORGE_ACCESS:
            raise ValueError(f"forge_access is one of {FORGE_ACCESS}")
        # Request 95: None (every older document) passes; a malformed list,
        # which no writer produces, is refused.
        check_additional_repositories(self.additional_repositories)

    def retries_exhausted(self) -> bool:
        """This task has used its last attempt. See `retries_exhausted`."""
        return retries_exhausted(self.attempt_count, self.max_attempts)

    def to_firestore(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["park_reason"] = self.park_reason.value if self.park_reason else None
        d["end_cause"] = (
            self.end_cause.value if isinstance(self.end_cause, EndCause) else self.end_cause
        )
        return d



def retries_exhausted(attempt_count: int, max_attempts: int) -> bool:
    """Whether a task has used its last attempt.

    A FREE FUNCTION as well as a method because the two enforcement points do
    not both hold a `Task`: the reconciler decides inside a Firestore
    transaction, from the raw document, where constructing one would mean
    decoding a task to read two integers.

    THIS EXISTS BECAUSE THE RULE WAS RESTATED AND THEN OMITTED. It lived only
    in `reconciler/store.py`, on the path that repairs a task to READY. The
    scheduler's `return_to_ready_after_failed_dispatch` -- the OTHER path that
    returns a task to READY -- wrote the state unconditionally, so a task whose
    dispatch kept failing retried for ever. Observed 2026-09-23:
    `task_d18d8d8b044d469cb43c` reached 83 attempts against a cap of 3,
    re-dispatching every 30 seconds for hours on `gke_create_job_failed`.

    The scheduler and the reconciler are separate images and cannot import each
    other, so a rule they both need has exactly one home that is not a
    restatement: here.
    """
    return attempt_count >= max_attempts


#: Consecutive PLATFORM_REFUSED parks after which a task is reported as
#: trouble (contract request 123). Beside `retries_exhausted` because it is
#: its counterpart for a refusal that spends no attempt: past it the task
#: STAYS PARKED and keeps being retried at the longest backoff -- BGD's
#: standing rule (2026-09-14) is that a quota pauses work and never fails
#: it, and BGD's 2026-10-28 brief applies that to the platform's refusals.
PLATFORM_REFUSED_MAX_PARKS = 5


@dataclass
class Attempt:
    """One execution of a task. A retry is a new attempt with a new generation."""

    attempt_id: str
    task_id: str
    tenant_id: str
    generation: int
    lease_id: str
    backend: str
    created_at: datetime
    execution_name: str | None = None       # Cloud Run execution or k8s Job name
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    peak_rss_bytes: int | None = None       # feeds the sizing tuning report
    peak_disk_bytes: int | None = None
    oom_near_miss: bool = False
    checkpoints: list[str] = field(default_factory=list)
    #: Checkpoint id -> lowercase hex SHA-256 of that checkpoint's archive.
    #: Written ONLY by the worker's `ControlPlane.record_checkpoint`, in the
    #: same merge as `checkpoints` and before `task.latest_checkpoint` moves;
    #: read ONLY by a later attempt of the same task, which restores a
    #: checkpoint only when its archive matches the digest recorded here (#347).
    #: Empty means "no digest recorded": a document written before #348, or a
    #: checkpoint recorded without one. Its retry starts from an empty
    #: workspace; an empty map is never read as "anything goes".
    checkpoint_sha256: dict[str, str] = field(default_factory=dict)

    # What the attempt SPENT. Added 2026-09-19 as change request #2 in
    # docs/contract-change-requests.md, approved by the platform owner.
    #
    # Until this existed an attempt recorded precisely how much MEMORY and DISK
    # it used and nothing at all about tokens -- on a platform whose entire cost
    # is tokens. The numbers were already being captured
    # (agent_worker.lifecycle._usage_summary) and landed in an untyped runner
    # summary, where no index can reach them: "spend per tenant last week" meant
    # scanning and parsing rather than querying.
    #
    # All optional, defaulting to None, so every existing document stays valid
    # and no migration runs. None means NOT REPORTED, which is genuinely
    # different from zero: a mock task costs nothing on purpose, and a run whose
    # result could not be parsed costs an unknown amount. A UI that renders
    # those two the same way is lying about one of them.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: float | None = None

    # What the attempt's CPU did. Added 2026-09-25 as change request #15 in
    # docs/contract-change-requests.md, accepted by the platform owner on #184.
    #
    # Every figure is the ATTEMPT's -- every runner it started, combined by
    # `agent_worker.metrics.combine_usage` -- in seconds and in cores, where
    # 1.0 is one whole vCPU:
    #
    #   cpu_seconds      CPU time consumed while the attempt's runners ran;
    #   peak_cpu_cores   the busiest sampling interval of any of them;
    #   mean_cpu_cores   cpu_seconds over RUNNER wall time, so setup, the clone
    #                    and retry waits are not counted as idle;
    #   cpu_limit_cores  what the figures are a fraction of: the container's
    #                    cgroup `cpu.max`, else the catalogue cpu of the class
    #                    it was sized with.
    #
    # These replace the interim home #188 gave them, flat keys on HEARTBEAT
    # events read back by an opt-in events query: that was not queryable across
    # attempts and cost a read per request. They sit at the end of the class,
    # not beside `peak_rss_bytes` as the request drew them, so no positional
    # construction of an `Attempt` changes meaning.
    #
    # None means NOT MEASURED, as for the spend fields above: an attempt fenced
    # before its runner started measured nothing, which is a different fact
    # from an idle agent's 0.0 cores. Optional, so every existing document
    # stays valid and nothing migrates.
    cpu_seconds: float | None = None
    peak_cpu_cores: float | None = None
    mean_cpu_cores: float | None = None
    cpu_limit_cores: float | None = None

    # WHEN the four were measured, and WHERE the limit came from. Added
    # 2026-09-26 as change request #26 in docs/contract-change-requests.md,
    # accepted by the platform owner on #184.
    #
    #   cpu_measured_at   the worker's clock at the reading it last wrote. On a
    #                     running attempt the four are a live reading, and
    #                     without a time a worker that stopped writing an hour
    #                     ago looked exactly like one that wrote a second ago;
    #   cpu_limit_source  "cgroup" (the container's cgroup v2 `cpu.max`) or
    #                     "resource_class" (the catalogue cpu of the class the
    #                     container was sized with), which the interim
    #                     HEARTBEAT path recorded and request #15 did not.
    #
    # Written with the four, never alone. None means not recorded: every
    # attempt from before this change, which the API and the UI keep reading.
    # At the end of the class for the reason given above.
    cpu_measured_at: datetime | None = None
    cpu_limit_source: str | None = None

    #: Contract request 103 (#1435): the model ID this attempt's agent was
    #: started with (MODEL), written by the worker at attempt start after its
    #: generation check. None: no model (mock, generic, browser, worker
    #: actions) or written before this field existed. At the end of the class
    #: for the reason given above.
    model: str | None = None

    # The size the scheduler RENDERED this attempt at. Contract request 111
    # (RS-4, accepted by the owner 2026-10-20 for #1689;
    # docs/design/runner-sizes.md §4.4). `cpu_limit_cores` above is what the
    # worker MEASURED; once an admin can resize a class the catalogue no
    # longer says what an older attempt ran at, so the scheduler's
    # `create_attempt` pins it here, from the same size the lease's units
    # were charged from. Never written by the worker or a caller.
    #
    #   size_version      the runner-size version admitted at; 0 is the code
    #                     defaults (no version written, or a Cloud Run profile);
    #   sized_*           that version's numbers for the attempt's class;
    #   sized_live_split  a live-browser attempt's two containers,
    #                     {"worker": {...}, "display": {...}}, each with
    #                     cpu_request, cpu_limit and memory_gib. A mapping, not
    #                     RS-3's LiveShare pair, because a named key survives a
    #                     reader that does not know the pair's order. None on
    #                     every attempt that is not a live pod.
    #
    # None means NOT RECORDED: every attempt from before the change, which
    # readers keep sizing from the class's code default. At the end of the
    # class for the reason given above.
    size_version: int | None = None
    sized_cpu_request: float | None = None
    sized_cpu_limit: float | None = None
    sized_memory_gib: int | None = None
    sized_disk_gib: int | None = None
    sized_live_split: dict[str, dict[str, float]] | None = None

    def __post_init__(self) -> None:
        check_sized_live_split(self.sized_live_split)


#: The containers and numbers of `Attempt.sized_live_split` (request 111).
SIZED_LIVE_SPLIT_ROLES = ("worker", "display")
SIZED_LIVE_SPLIT_FIELDS = ("cpu_request", "cpu_limit", "memory_gib")


def check_sized_live_split(value: Any) -> None:
    """Refuse a `sized_live_split` that is not None or exactly
    {"worker": {...}, "display": {...}}, each holding exactly the three
    numbers. A reader of the record (the console, the preview's per-version
    split) relies on both keys being there."""
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != set(SIZED_LIVE_SPLIT_ROLES):
        raise ValueError(f"sized_live_split is None or a map of exactly {SIZED_LIVE_SPLIT_ROLES}")
    for role in SIZED_LIVE_SPLIT_ROLES:
        share = value[role]
        if not isinstance(share, dict) or set(share) != set(SIZED_LIVE_SPLIT_FIELDS):
            raise ValueError(f"sized_live_split[{role!r}] holds exactly {SIZED_LIVE_SPLIT_FIELDS}")
        for name in SIZED_LIVE_SPLIT_FIELDS:
            number = share[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValueError(f"sized_live_split[{role!r}][{name!r}] is a number")


@dataclass
class TaskEvent:
    event_id: str
    task_id: str
    tenant_id: str
    type: EventType
    at: datetime
    attempt_id: str | None = None
    lease_id: str | None = None
    generation: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Tenants, quota, workflows
# --------------------------------------------------------------------------

@dataclass
class Tenant:
    """A Google group, or a single user as a personal fallback tenant."""

    tenant_id: str
    kind: str                               # "group" | "user"
    principal: str                          # eng@saga.xyz | alice@saga.xyz
    created_at: datetime
    display_name: str | None = None
    max_active: int = 20
    capacity_units: int = 40
    monthly_budget_usd: float | None = None
    enabled: bool = True
    #: Providers this tenant has registered a key for. A runner whose provider
    #: is absent parks as CREDENTIAL_MISSING rather than failing at runtime.
    credentials: list[str] = field(default_factory=list)
    #: The three below are RECORDS, not authority (contract request 87,
    #: docs/design/tenant-settings-integrity.md §5 request C, #1457). A tenant
    #: identity can rewrite its own `tenants/<id>` document, so a reader that
    #: dispatched as the stored account or into the stored namespace would run
    #: one tenant's attempt as another's. Derive them from `tenant_id`.
    service_account: str | None = None  # RECORD ONLY: dispatch derives it with
                                        # identity.worker_service_account_id. A
                                        # tenant identity can rewrite this field.
    gcs_prefix: str | None = None       # RECORD ONLY: derived from tenant_id.
    namespace: str | None = None        # RECORD ONLY: derived from the template.

    def secret_name(self, provider: str) -> str:
        return f"swarm-tenant-{self.tenant_id}-{provider}"


class ProviderState(str, Enum):
    AVAILABLE = "AVAILABLE"
    THROTTLED = "THROTTLED"
    EXHAUSTED = "EXHAUSTED"
    COOLDOWN = "COOLDOWN"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


@dataclass
class QuotaState:
    """Per (provider, tenant) health. Keyed that way because keys are per tenant."""

    provider: str
    tenant_id: str
    state: ProviderState
    updated_at: datetime
    configured_hard_max: int = 50
    adaptive_target: int | None = None
    quota_derived_limit: int | None = None
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    reset_at: datetime | None = None
    cooldown_until: datetime | None = None
    last_429_at: datetime | None = None
    retry_after_seconds: int | None = None
    success_count: int = 0
    rate_limit_count: int = 0

    @property
    def effective_limit(self) -> int:
        candidates = [self.configured_hard_max]
        if self.adaptive_target is not None:
            candidates.append(self.adaptive_target)
        if self.quota_derived_limit is not None:
            candidates.append(self.quota_derived_limit)
        limit = min(candidates)
        if self.state in (ProviderState.EXHAUSTED, ProviderState.DISABLED):
            return 0
        if self.state is ProviderState.COOLDOWN:
            return 0
        return max(0, limit)


@dataclass
class WorkflowStep:
    step_id: str
    runner_profile: str
    input: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    resource_class: str | None = None
    #: Map of upstream step_id -> artifact filename to stage into this step's
    #: workspace. Artifacts pass by GCS reference, never inline through Firestore.
    input_from: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int | None = None
    task_id: str | None = None
    #: Contract request 102 (#1435): the MODEL_CLASSES name this step asks
    #: for, carried to its task's `model_class`. None: its profile's default.
    model_class: str | None = None
    #: Contract request 120 (#2459): the budget this step ASKED for, `usd`
    #: and/or `tokens`, as submitted. Its task's `budget` is what it resolved
    #: to. None: the step set none.
    budget: dict[str, float | int] | None = None


@dataclass
class Workflow:
    workflow_id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime
    state: TaskState
    submitted_by: str
    steps: list[WorkflowStep] = field(default_factory=list)
    on_step_failure: str = "fail_workflow"   # or "continue"
    priority: int = 0
    cancel_requested: bool = False

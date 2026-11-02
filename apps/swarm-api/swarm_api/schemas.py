"""Request and response schemas.

Every request model sets `extra="forbid"`. That is the schema-level half of
invariant 10: a caller who sends `image`, `command`, `backend`, `cpu` or
`service_account` gets a 422 naming the field, rather than having it silently
dropped and believing it took effect. The other half is that no model below has
a field for any of those things in the first place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from swarm_common.models import ADDITIONAL_REPOSITORIES_CEILING, ADDITIONAL_REPOSITORY_NAME

from . import modelprices
from .sessiontag import PATTERN as SESSION_TAG_PATTERN
from .validation import (
    DEFAULT_CARRIER,
    DEFAULT_STRATEGY,
    check_repository_url,
    parse_issue_ref,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

#: At most this many per-run MCP credentials in one map: the `mcp_servers`
#: input names one to four servers (contract request 88), so a fifth entry
#: could only ever be refused as unexpected.
MAX_MCP_CREDENTIALS = 4


class McpCredentialCreate(BaseModel):
    """One per-run MCP credential (docs/design/registered-mcp-servers.md §4.3).

    NOT a `StrictModel`: that strips whitespace from every string, and a
    credential is not a string to tidy. Both fields are `Any` so that their
    shape is refused by `mcp_credentials.check_credential` with its own code,
    `mcp_credential_shape`, whose message names the rule and never the value,
    rather than by a pydantic type error. The value is a local of the
    submission and goes to exactly one Secret Manager `add_version`
    (`SubmissionService`); it is in no document, no response and no log.
    """

    model_config = ConfigDict(extra="forbid")

    value: Any
    expires_in: Any


def _mcp_credentials_field() -> Any:
    """The request field a per-run credential map arrives in. `exclude=True`:
    no `model_dump` of a request ever carries a credential, so a spec stored
    as JSON (a custom-prompt schedule's `params.spec`) or logged whole cannot
    hold one. `repr=False` keeps it out of a model's repr in a traceback."""
    return Field(default=None, max_length=MAX_MCP_CREDENTIALS, exclude=True, repr=False)


#: The most entries a request's `additional_repositories` list may carry
#: before it is parsed at all: a bound on the work one body costs, NOT the
#: limit. The limit is the platform setting `max_additional_repositories`
#: (default 5, at most ADDITIONAL_REPOSITORIES_CEILING), which swarm-api
#: refuses `too_many_additional_repositories` against, naming its current
#: value -- so this sits well above the ceiling, and a list between the two
#: gets that refusal rather than pydantic's generic shape.
MAX_ADDITIONAL_REPOSITORIES_IN_REQUEST = 4 * ADDITIONAL_REPOSITORIES_CEILING


class AdditionalRepositoryCreate(StrictModel):
    """One repository a step reads beside its own (docs/design/
    multi-repository-steps.md §4.1, #1434): a NAME the tenant registered,
    never a URL, a slot or a commit. swarm-api resolves the rest -- the
    registration, whose slot clones it, the commit its ref names now, its
    size -- and stores that, signed (`extrarepos`); nothing here reaches the
    task as sent (invariant 10)."""

    #: `owner/repo`, as GitHub spells it (`repositories.parse_repository`).
    #: A URL is refused by shape: it is derived from the registration.
    repository: str = Field(min_length=3, max_length=140)
    #: A branch, a tag or a full 40-hex sha; absent, the registration's
    #: default branch. Pinned to a commit at submission.
    ref: str | None = Field(default=None, min_length=1, max_length=255)
    #: The directory under `work/repos/`; absent, the repository part,
    #: lower-cased. `ADDITIONAL_REPOSITORY_NAME` (contract request 95).
    name: str | None = Field(default=None, min_length=1, max_length=63)

    @field_validator("repository")
    @classmethod
    def _owner_repo(cls, value: str) -> str:
        # Lazily: `repositories` imports the forge clients, which this module
        # must not pull in at import. ValueError, so pydantic names the field.
        from .repositories import parse_repository

        owner, repo = parse_repository(value)
        return f"{owner}/{repo}"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is not None and not ADDITIONAL_REPOSITORY_NAME.fullmatch(value):
            raise ValueError("name is lower-case letters, digits, '.', '_' and '-', "
                             "starting with a letter or digit, at most 63 characters")
        return value


def _additional_repositories_field() -> Any:
    """None is "not said": a workflow step then reads the workflow's list. `[]`
    is "none", which on a step replaces the workflow's list (§7 Q6)."""
    return Field(default=None, max_length=MAX_ADDITIONAL_REPOSITORIES_IN_REQUEST)


#: The largest budget a step, a workspace default or the platform ceiling may
#: name (`budget`, #2459). Not a policy: a shape guard, so a typo of many
#: zeros is refused rather than stored as "no limit" in all but name.
#: $100,000 is far past any one agent's spend on a subscription pool; 10
#: billion tokens is hundreds of times the 15M the owner gave as an example.
BUDGET_MAX_USD = 100_000.0
BUDGET_MAX_TOKENS = 10_000_000_000


class StepBudget(StrictModel):
    """`budget` on a task or a workflow step: `{usd: N}` or `{tokens: N}` (#2459).

    It may only LOWER the attempt's limit: `budget.refuse_step_above` refuses
    one above the workspace default or the platform ceiling, naming both. A
    limit, never a resource (invariant 10), as `timeout_seconds` is."""

    usd: StrictFloat | StrictInt | None = Field(default=None, gt=0, le=BUDGET_MAX_USD)
    tokens: StrictInt | None = Field(default=None, ge=1, le=BUDGET_MAX_TOKENS)

    @model_validator(mode="after")
    def _one_unit(self) -> "StepBudget":
        if (self.usd is None) == (self.tokens is None):
            raise ValueError("a budget names exactly one of usd or tokens")
        return self


class TaskCreate(StrictModel):
    #: The ONLY execution knob a caller has. Image, command, resource spec and
    #: backend all come from the frozen catalogue keyed by this name.
    runner_profile: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    repository_url: str | None = Field(default=None, max_length=1024)
    repository_ref: str | None = Field(default=None, max_length=256)
    #: May only SHORTEN the profile's timeout; a longer value is clamped.
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    #: May only LOWER the attempt's spend limit (#2459): see `StepBudget`.
    budget: StepBudget | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    #: The model ID, a RECORD: swarm-api fills it with the pin of the task's
    #: `model_class` (WORKER_CLASS_MODELS), and a value that is not that pin is
    #: refused `model_is_chosen_by_model_class` behind its switch
    #: (`validation.resolve_task_model`, #1435 Q4). It selects nothing: the
    #: scheduler resolves the class's pin again at dispatch.
    model: str | None = Field(default=None, max_length=128)
    #: The model class this task runs, BY NAME from the frozen
    #: `profiles.MODEL_CLASSES` (`opus`, `fable`, `sonnet`, `haiku`), never a
    #: model ID (invariant 10). None: the profile's `default_model_class`.
    #: Checked by `validation.validate_model_class`, which names the accepted
    #: classes in its refusal, rather than declared as a `Literal` here, for
    #: the same reason as `strategy` below.
    model_class: str | None = Field(default=None, max_length=32)
    #: How this dispatch's work gets merged, and what carries it between steps.
    #: Validated in `validation.resolve_dispatch_options`, which names the
    #: accepted values in its refusal, rather than declared as a `Literal` here:
    #: a Literal produces pydantic's generic 422 shape, and every other refusal
    #: this service makes carries a stable `code` a caller can branch on.
    #:
    #: The defaults are today's behaviour, so a caller who sends neither field
    #: gets exactly the dispatch they got before this feature existed.
    strategy: str = Field(default=DEFAULT_STRATEGY, max_length=32)
    carrier: str = Field(default=DEFAULT_CARRIER, max_length=32)
    #: The workflow switches (2026-11-01): `{code_intelligence, quality_gates,
    #: jev_judgments}`, each `on` or `off`, stored inside the signed dispatch
    #: block. Checked in `validation.resolve_dispatch_switches`, which names
    #: the accepted switches and values in its refusal, for the reason
    #: `strategy` is. Absent: today's dispatch exactly.
    dispatch: dict[str, Any] | None = None
    #: Per-run credentials for the `dispatch` servers `input.mcp_servers`
    #: names, by server name (docs/design/registered-mcp-servers.md §4.3):
    #: beside the task, never in it. Checked and stored by `SubmissionService`.
    mcp_credentials: dict[str, McpCredentialCreate] | None = _mcp_credentials_field()
    #: The registered repositories this task reads beside its own, checked out
    #: read-only and never carried back (#1434). Behind
    #: `ApiSettings.additional_repositories_enabled` (off by default).
    additional_repositories: list[AdditionalRepositoryCreate] | None = (
        _additional_repositories_field())

    #: An existing pull request of `repository_url` this task's commits are
    #: pushed onto, as a fast-forward, instead of opening one (#2183): a
    #: positive integer, or `owner/repo#N` of that same repository. The rules
    #: -- `direct-pr`, open, not from a fork, on a `swarm/task_*` branch a
    #: lane of this tenant published -- are in `publishtopr`.
    publish_to_pr: StrictInt | StrictStr | None = None

    @field_validator("repository_url")
    @classmethod
    def _repo_scheme(cls, value: str | None) -> str | None:
        # The scheme, and no credential in the URL (the PR #229 review): see
        # `validation.check_repository_url`, which WorkflowCreate calls too.
        return check_repository_url(value)


class TaskBatchCreate(StrictModel):
    tasks: list[TaskCreate] = Field(min_length=1)


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------

class StepVerdictGate(StrictModel):
    """`when` on a workflow step: run this step's agent only on these verdicts (#264).

    The verdict is read from the file this step stages from `step` through
    `input_from`, a JSON object whose `verdict` is one of
    `validation.REVIEW_VERDICTS`. When the verdict is not in `verdict_in` the
    step still runs, without an agent, and still publishes: it is the step
    that opens the pull request. The values are checked by
    `validation.validate_step_routing`, which names the accepted ones.
    """

    step: str = Field(min_length=1, max_length=64)
    verdict_in: list[str] = Field(min_length=1, max_length=8)


class WorkflowStepCreate(StrictModel):
    step_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")
    runner_profile: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    #: A NAMED class from the frozen catalogue, and only one no larger than the
    #: profile's own. Not a resource spec.
    resource_class: str | None = Field(default=None, max_length=64)
    #: The model class this step runs, by name, as `TaskCreate.model_class`
    #: (#1435). Stored on `WorkflowStep.model_class` and on its task only when
    #: it differs from the profile's default.
    model_class: str | None = Field(default=None, max_length=32)
    #: upstream step_id -> artifact filename staged into this step's workspace.
    input_from: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    #: This step's spend limit, which may only LOWER the workspace's (#2459):
    #: see `StepBudget`. Pinned, resolved, in the step task's signed
    #: `metadata.dispatch.budget`.
    budget: StepBudget | None = None
    #: Run this step's agent only when an upstream review's verdict says so
    #: (#264). See `StepVerdictGate`.
    when: StepVerdictGate | None = None
    #: An upstream step whose pushed branch this step's checkout starts from,
    #: instead of the workflow's `repository_ref` (#264). A step id, never a
    #: ref: the worker derives the branch from the upstream's task id.
    builds_on: str | None = Field(default=None, min_length=1, max_length=64)
    #: An empty diff is this step's result, not its failure (owner decision,
    #: 2026-10-05): the step ends SUCCEEDED with `result_summary.no_change`,
    #: and a later step that needs its change (its `swarm-work.patch`, its
    #: branch) is skipped with "nothing to change" instead of failing. Strict:
    #: `"yes"` is refused, not read as true. Stored in `metadata.dispatch`,
    #: which the spec signature covers; see docs/workflows.md.
    allow_empty_diff: StrictBool = False
    #: Free-form, like a task's: merged over the workflow's `metadata` onto this
    #: step's task, with the same reserved keys, size limit and storability
    #: checks. One key in it means something to the platform:
    #: `input_layout` (#75), `"by_name"` (the default) or `"by_parent"`, which
    #: stages each `input_from` file at `<parent step id>/<filename>`. Checked
    #: by `validation.resolve_input_layout`, which names the accepted values.
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: This step's part in a `single-pr` workflow's one pull request (#295):
    #: `author`, `reader` or `amender` on an agent step; absent or `none` on a
    #: worker-action step (post-verdict, merge). Refused under every other
    #: strategy. Checked by `validation.resolve_single_pr`, which names the
    #: accepted values, rather than declared as a `Literal` here, for the same
    #: reason as `strategy`.
    pr_role: str | None = Field(default=None, max_length=16)
    #: On the `merge` step of a `single-pr` workflow only, and optional: the
    #: upstream STEP ids it checks, keyed `author`, `review`, `post-verdict`,
    #: `fix`, `proof` (`validation.MERGES_KEYS`). The API derives the block
    #: from the roles either way; a caller who states it is refused if the
    #: graph says otherwise, so a chain wired differently from what was meant
    #: is caught at submission rather than at merge time.
    merges: dict[str, str] | None = Field(default=None, max_length=5)
    #: On an `await-input` step only, and required there: what the person is
    #: asked to answer, its shape, and when and how the wait ends
    #: (docs/design/human-steps.md §4.1). A free-form object here, checked by
    #: `validation.resolve_await_input`, for the same reason as `strategy`:
    #: every refusal then carries `code` and names its field, `assignees`
    #: included, which a nested model would answer with pydantic's generic
    #: shape. Stored normalised in `metadata.dispatch.await_input`, signed.
    await_input: Any = None
    #: This step's own list of additional repositories (#1434). It REPLACES
    #: the workflow's `additional_repositories`, never merges with it, and
    #: `[]` means none (owner's Q6, 2026-10-17): what a step reads is answered
    #: from one place. Absent, the step reads the workflow's list.
    additional_repositories: list[AdditionalRepositoryCreate] | None = (
        _additional_repositories_field())


class MergePullRequest(StrictModel):
    """The pull request a merge-only workflow merges, when no workflow opened it.

    Owner decision 2026-10-07 (#352, MS0 question 2). A NUMBER and the HEAD
    SHA the caller means, never a branch or a repository: the repository is
    the tenant's registered one (`continuation.resolve_merge_pr`), and the
    sha is what the merge step pins -- it merges only at that head, with the
    required checks green there.
    """

    number: StrictInt = Field(ge=1, le=10**9)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


#: The most pull requests one workflow's `after` may name (SPEED-D3). Ten: a
#: lane waits for one or two; a longer list is a plan that should be split,
#: and each costs one GitHub read per tick (`prgate.wake_tenant`).
MAX_AFTER_PRS = 10


class AfterPullRequest(StrictModel):
    """One pull request a workflow waits for before its steps start (SPEED-D3).

    A NUMBER, never a repository: the pull request is the workflow's own
    repository's (`prgate.resolve_after`), so a spec cannot point the
    tenant's token at another one.
    """

    pr: StrictInt = Field(ge=1, le=10**9)


class WorkflowCreate(StrictModel):
    steps: list[WorkflowStepCreate] = Field(min_length=1)
    priority: int = Field(default=0, ge=-100, le=100)
    on_step_failure: Literal["fail_workflow", "continue"] = "fail_workflow"
    #: Free-form, copied onto every step's task. Two keys are the merge
    #: step's, checked at submission by `validation` rather than typed here, so
    #: a refusal names the key and its accepted values in the API's 422 shape:
    #: `merge` ("on" | "off", `resolve_merge_choice`) and `merge_fix_rounds`
    #: (0-5, only beside a merge step, `resolve_merge_fix_rounds`).
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: How many fix rounds the merge step may spend on a pull request whose
    #: required checks are red at its head (lane SPEED-D2): each a claude-code
    #: continuation of the pull request's branch, handed only the failing
    #: jobs and their log tails. 0 disables them; absent is 2 beside a merge
    #: step (`validation.LANE_FIX_ROUNDS_DEFAULT`), and refused without one.
    #: The same budget as `metadata.merge_fix_rounds`, which it is stored as
    #: on the merge task (`validation.resolve_fix_rounds`). A conflicting pull
    #: request's rounds keep their own cap of 2 (`mergewake`).
    fix_rounds: StrictInt | None = Field(default=None, ge=0, le=5)
    #: Chosen once for the whole workflow, not per step: `integrate` produces
    #: ONE pull request, so "which repository" cannot be a per-step answer.
    #: Same accepted values and same defaults as `TaskCreate`.
    strategy: str = Field(default=DEFAULT_STRATEGY, max_length=32)
    carrier: str = Field(default=DEFAULT_CARRIER, max_length=32)
    #: The workflow switches (2026-11-01): `{code_intelligence, quality_gates,
    #: jev_judgments}`, each `on` or `off`, stored inside the signed dispatch
    #: block. Checked in `validation.resolve_dispatch_switches`, which names
    #: the accepted switches and values in its refusal, for the reason
    #: `strategy` is. Absent: today's dispatch exactly.
    dispatch: dict[str, Any] | None = None
    #: The repository every step of this workflow clones, and the one an
    #: `integrate` step opens its pull request against.
    #:
    #: WHY IT IS HERE AND NOT ON THE STEP. A workflow step had no way to name a
    #: repository at all before this, so a workflow's tasks were always created
    #: with `repository_url=None` and no step could ever clone anything -- which
    #: made every strategy but `collect` unreachable for a workflow. It is
    #: workflow-level because the steps of one workflow integrate into one
    #: branch; a step that needed a different repository is a different dispatch.
    repository_url: str | None = Field(default=None, max_length=1024)
    repository_ref: str | None = Field(default=None, max_length=256)
    #: A TASK ID, never a branch: the task whose `swarm/<task-id>` branch this
    #: workflow's one step clones and pushes to instead of a branch of its own,
    #: so a fix lands on the pull request that needs it (#263). The rules --
    #: same tenant, `direct-pr`, one step, no ref -- are in
    #: `continuation.resolve_continuation`.
    continues_task: str | None = Field(default=None, max_length=64)
    #: A pull request NO workflow opened, merged by this workflow's one
    #: `merge` step at the head sha named here (#352). The rules -- one merge
    #: step, `direct-pr`, the tenant's registered repository, open, not from
    #: a fork, at that head now -- are in `continuation.resolve_merge_pr`.
    merge_pr: MergePullRequest | None = None
    #: An existing pull request of this workflow's repository that the lane's
    #: commits are pushed onto, as a fast-forward, instead of opening a
    #: replacement (#2183, owner approval 2026-10-24): a positive integer, or
    #: `owner/repo#N` of that same repository. The rules -- `direct-pr` of one
    #: agent step or `integrate`, open, not from a fork, on a `swarm/task_*`
    #: branch a lane of this tenant published, no merge step -- are in
    #: `publishtopr`.
    publish_to_pr: StrictInt | StrictStr | None = None
    #: The pull requests of this workflow's own repository that must MERGE
    #: before any of its steps starts (lane SPEED-D3, `prgate`): one
    #: `{"pr": N}` or a list of them, stored as the list. Until then every
    #: root step is PARKED, holding nothing (invariant 1); one closed without
    #: merging fails the workflow. Absent: the workflow starts as it always did.
    after: list[AfterPullRequest] | None = Field(default=None, min_length=1,
                                                 max_length=MAX_AFTER_PRS)
    #: One per-run credential map for the whole submission (§4.3): the
    #: credential is the caller's, so every step that names a `dispatch`
    #: server uses the one stored credential, owned by the workflow.
    mcp_credentials: dict[str, McpCredentialCreate] | None = _mcp_credentials_field()
    #: The additional repositories every agent step reads, unless the step
    #: names its own list (which replaces this one) (#1434). A worker-action
    #: or `await-input` step runs no agent and reads none of them. Each
    #: (repository, ref) is pinned ONCE per submission, so every step reads
    #: the same commit even if the branch moves while it is submitted.
    additional_repositories: list[AdditionalRepositoryCreate] | None = (
        _additional_repositories_field())

    @field_validator("after", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        # One object is the common case ("after PR 41"), so it is accepted
        # as itself rather than making every spec write a one-item list.
        return [value] if isinstance(value, dict) else value

    @field_validator("repository_url")
    @classmethod
    def _repo_scheme(cls, value: str | None) -> str | None:
        # The same rule TaskCreate applies -- the same function, not a second
        # statement of it -- because a workflow's repository reaches
        # Task.repository_url without passing through TaskCreate.
        return check_repository_url(value)


# --------------------------------------------------------------------------
# Issue runs (#454)
# --------------------------------------------------------------------------

#: Review-then-fix rounds a run may spend. 3 by default, 1-5: the owner's
#: decision on #454 (2026-10-01/02). One round is the floor because a run
#: whose work is never reviewed before it publishes is what the review shape
#: (#264) exists to prevent; five is the ceiling because a sixth review of the
#: same change is a plan that was wrong, which a person should see.
DEFAULT_FIX_ROUNDS = 3
MIN_FIX_ROUNDS = 1
MAX_FIX_ROUNDS = 5


class RunCreate(StrictModel):
    """Plan an issue, and run the plan once it is approved.

    No profile, image, command or resource field, as everywhere else
    (invariant 10): the planner and every step are `claude-code`, chosen by
    `issueruns`, never by the caller.
    """

    #: `owner/repo#N` or the issue's URL, stored in its short form. A pull
    #: request is refused, saying it is one (`validation.parse_issue_ref`).
    issue: str = Field(min_length=1, max_length=1024)
    #: `required`: the plan waits for a person to approve the digest they were
    #: shown. `auto`: the plan is approved the moment it is read. Required by
    #: default, because the plan is an agent's and its steps run with the
    #: tenant's forge token.
    plan_approval: Literal["required", "auto"] = "required"
    #: Merge the run's pull request with a merge step once CI is green and the
    #: keyword block is written (#295, contract request 47; `issueci._merge`). Absent takes
    #: the platform's `merge_by_default`; the run records what was resolved.
    auto_merge: bool | None = None
    fix_rounds: int = Field(default=DEFAULT_FIX_ROUNDS, ge=MIN_FIX_ROUNDS, le=MAX_FIX_ROUNDS)
    #: The Claude Code session that asked (SC-SESSION-SCOPE, `sessiontag`):
    #: stored on the run and copied onto its planner task and every workflow
    #: it starts, so the sc plugin counts the run as that session's own. A
    #: run's caller writes no metadata, so it is a field of its own. Optional:
    #: any other caller sends none.
    claude_session: str | None = Field(default=None, pattern=SESSION_TAG_PATTERN)

    @field_validator("issue")
    @classmethod
    def _issue_ref(cls, value: str) -> str:
        return parse_issue_ref(value).short


class PlanApprove(StrictModel):
    #: The digest of the plan the caller was SHOWN (`plan_digest` on the run).
    #: Refused with 409 `plan_changed` when the plan is no longer that one (D3).
    plan_digest: str = Field(min_length=1, max_length=128)


class PlanEdit(StrictModel):
    #: The digest of the plan being edited, so two editors cannot silently
    #: overwrite each other: the second is refused with `plan_changed`.
    plan_digest: str = Field(min_length=1, max_length=128)
    #: The whole replacement plan, checked against `issueruns.PlanSpec`.
    plan: dict[str, Any]


class PlanReject(StrictModel):
    #: Optional: when sent, the rejection is refused if the plan changed since.
    plan_digest: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=1024)


# --------------------------------------------------------------------------
# Tenant credentials
# --------------------------------------------------------------------------

class CredentialCreate(StrictModel):
    provider: str = Field(min_length=1, max_length=64)
    #: Write-only. No response model in this service contains this field, and
    #: `credentials.py` has no function that returns a payload.
    api_key: str = Field(min_length=8, max_length=8192)


#: What a feature whose state could not be read is served as. Never `false`:
#: a caller that reads `false` stops offering the feature, which is the wrong
#: answer Product Oracle's New run form gave its users on 2026-10-20 (#1748).
FEATURE_UNKNOWN = "unknown"

#: One feature's state on `GET /v1/tenants/me`.
FeatureState = bool | Literal["unknown"]


class TenantFeatures(StrictModel):
    """`features` on `GET /v1/tenants/me` (FEATURES-READ, part of #1748).

    Each is the state the serving process ACTS on, read where it acts on it
    (`routes/tenants.py` `_features`), never a value kept for display.
    """

    #: Registered (caller) MCP servers: `mcp_servers.mcp_servers_enabled()`.
    caller_mcp: FeatureState
    #: The `await-input` runner profile's `available`, from the frozen catalogue.
    await_input: FeatureState
    #: This workspace is a service workspace whose own principal may answer
    #: its waits: `awaitinput.service_principal`.
    service_answer: FeatureState


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

class PauseRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=512)


#: A list price, USD per million tokens. STRICT: a string ("0.20"), a bool or a
#: null is refused, never coerced -- an admin's figure is stored as they typed
#: it or not at all. Negative, infinite and NaN are refused too.
PriceRate = Annotated[float, Field(strict=True, ge=0, le=modelprices.MAX_RATE,
                                   allow_inf_nan=False)]


class ModelPriceTierRequest(StrictModel):
    """One tier of a model priced by prompt length (`modelprices.tiers_problem`)."""

    #: The largest prompt, in tokens, this tier prices; null on the last tier.
    #: Required, so a tier that forgot its ceiling is refused, not unbounded.
    up_to_prompt_tokens: StrictInt | None = Field(ge=1, le=modelprices.MAX_TIER_CEILING)
    input: PriceRate
    output: PriceRate
    cache_read: PriceRate
    cache_write: PriceRate


class ModelPriceRequest(StrictModel):
    """One model's entry in `model_prices` (#1481). `updated_by` is not the
    caller's to say: swarm-api stamps the admin who made the change."""

    input: PriceRate
    output: PriceRate
    cache_read: PriceRate
    cache_write: PriceRate
    currency: Literal["USD"] = modelprices.CURRENCY
    #: Where the figure was read: `console` for an admin's edit, the weekly
    #: refresh writes `pricing-page`.
    source: str = Field(default="console", min_length=1, max_length=64)
    #: The day the figure was read from its source, YYYY-MM-DD.
    as_of: str = Field(pattern=modelprices.AS_OF.pattern)
    #: A model priced by prompt length (Haiku 5.5): its tiers in rising order,
    #: the first at the four rates above, the last with no ceiling. Omitted
    #: for a model priced at one set of rates, which is every other model.
    tiers: list[ModelPriceTierRequest] | None = Field(
        default=None, min_length=2, max_length=modelprices.MAX_TIERS)

    @model_validator(mode="after")
    def _tiers_cover_every_prompt(self) -> "ModelPriceRequest":
        if self.tiers is not None:
            rates = {name: getattr(self, name) for name in modelprices.RATES}
            _tiers, why = modelprices.tiers_problem([t.model_dump() for t in self.tiers], rates)
            if why is not None:
                raise ValueError(why)
        return self


class BudgetCeilingRequest(StrictModel):
    """`agent_budget_ceiling` (#2459): the most any workspace default or step
    budget may name, per unit. A unit left out has no ceiling, so `{}` clears it."""

    usd: StrictFloat | StrictInt | None = Field(default=None, gt=0, le=BUDGET_MAX_USD)
    tokens: StrictInt | None = Field(default=None, ge=1, le=BUDGET_MAX_TOKENS)


class PlatformSettingsRequest(StrictModel):
    """`PUT /v1/admin/settings`: the settings to change; one omitted is left as it is."""

    #: Append a `merge` step to every workflow that opens one pull request and
    #: does not say `metadata.merge` (contract request 47). Off by default.
    merge_by_default: bool | None = None
    #: The most additional repositories one step may read (#1434), read by
    #: swarm-api at every submission, so a change applies to the next one.
    #: 1 to the frozen ceiling; `store.PLATFORM_SETTINGS_DEFAULTS` says why 5.
    max_additional_repositories: StrictInt | None = Field(
        default=None, ge=1, le=ADDITIONAL_REPOSITORIES_CEILING)
    #: The GiB one step's additional repositories may total (#1434), enforced
    #: by swarm-api on the forge's estimate and by the worker on disk.
    #: 1 to 20, the cache volume's size; default 6.
    additional_repositories_budget_gib: StrictInt | None = Field(default=None, ge=1, le=20)
    #: List prices by model (#1481), the ones named replacing theirs; a model
    #: not named keeps its figure. Read at every estimate, so a change prices
    #: the next stopped attempt. `modelprices` says why a model with no entry
    #: prices nothing.
    model_prices: dict[
        Annotated[str, Field(pattern=modelprices.MODEL_NAME.pattern)], ModelPriceRequest
    ] | None = Field(default=None, min_length=1, max_length=modelprices.MAX_MODELS)
    #: The platform's per-agent budget ceiling (#2459), replaced whole: a
    #: workspace default above it in its unit is refused on save, and every
    #: budgeted attempt's limit is at most it. Off by default.
    agent_budget_ceiling: BudgetCeilingRequest | None = None


#: A runner-size change, by class then field (docs/design/runner-sizes.md
#: §4.5). Deliberately loose here: `runnersizes.apply_changes` judges every
#: field against the frozen `check_size`, so a refused body is ONE 422 naming
#: every refusal -- `units` (derived), `disk_gib` (shown, not edited), an
#: unknown field or class and a bound alike -- not the first pydantic stops at.
RunnerSizeClasses = dict[str, dict[str, Any]]


class RunnerSizesPreviewRequest(StrictModel):
    """`POST /v1/admin/runner-sizes:preview`: the PUT's classes; nothing written."""

    classes: RunnerSizeClasses = Field(default_factory=dict)
    base_version: StrictInt | None = Field(default=None, ge=0)


class RunnerSizesRequest(StrictModel):
    """`PUT /v1/admin/runner-sizes`. A class or field the body does not name
    keeps its value. `base_version` is the version the admin edited: another
    admin's save in between is a 409, never a silent overwrite."""

    base_version: StrictInt = Field(ge=0)
    #: Required (owner Q4): every version says why.
    reason: str = Field(min_length=1, max_length=500)
    classes: RunnerSizeClasses = Field(min_length=1)
    #: A memory cut below the class's 7-day max peak is refused without it
    #: (owner Q6); the console sends it after the class name is typed.
    confirm_below_recent_peak: StrictBool = False


class RunnerSizesRestoreRequest(StrictModel):
    """`POST /v1/admin/runner-sizes/restore`: a NEW version with `version`'s
    classes (0: the code defaults). The pointer is never rewound."""

    base_version: StrictInt = Field(ge=0)
    version: StrictInt = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    confirm_below_recent_peak: StrictBool = False


class LimitRequest(StrictModel):
    #: `hard_limit` is accepted as well as `limit`, because that is the spelling
    #: the operator runbooks in docs/ tell you to send and every one of those
    #: copy-pasteable curls returned 422 against `extra="forbid"`. A limit an
    #: operator cannot set during an incident is not a limit.
    limit: int = Field(
        ge=0,
        le=100_000,
        validation_alias=AliasChoices("limit", "hard_limit"),
    )


class AdaptiveTargetRequest(StrictModel):
    """`PUT /v1/admin/quota/{provider}/{tenant_id}/adaptive-target` (QUOTA-RESET).

    The broker's own body, minus `requested_by`: that is the admin's verified
    email, taken from the token, never from the request.
    """

    target: int = Field(ge=0, le=100_000)
    reason: str = Field(min_length=1, max_length=500)
    clear_cooldown: bool = False


class HardMaxRequest(StrictModel):
    """`PUT /v1/admin/quota/{provider}/{tenant_id}/hard-max` (POOL-LIMITS-QUOTA).

    `reason` is the point of the change's audit entries: swarm-api's
    `admin_audit` one, and the broker's `quota_audit` one, which it is
    forwarded to. No `requested_by`: who asked is the admin's verified email,
    taken from the token, and a body that names one is refused
    (`extra="forbid"`).
    """

    hard_max: int = Field(ge=0, le=100_000)
    reason: str = Field(min_length=1, max_length=500)


class DrainRequest(StrictModel):
    #: Draining disables admission into the pool and lets in-flight work finish.
    #: Undraining is the same call with `drain=false`.
    drain: bool = True
    reason: str | None = Field(default=None, max_length=512)


class HistoryPurgeRequest(StrictModel):
    """`POST /v1/admin/history:purge` (purge.py). A dry run unless `dry_run`
    is false AND `confirm` is the word `purge`; `states` is checked against
    `purge.PURGEABLE_STATES`, so nothing live can be asked for."""

    states: list[str] = Field(min_length=1, max_length=3)
    #: Only what last changed before this instant. Naive means UTC.
    before: datetime | None = None
    #: Narrows the selection. Never where the bucket prefix comes from: that
    #: is the tenant stored on each task (invariant 9).
    tenant_id: str | None = Field(default=None, max_length=128)
    #: Workflow or task ids to keep. A step named keeps its whole workflow.
    exclude_ids: list[str] = Field(default_factory=list, max_length=1000)
    dry_run: bool = True
    confirm: str | None = Field(default=None, max_length=32)
    limit: int | None = None
    page_token: str | None = Field(default=None, max_length=256)


class ProviderEnableRequest(StrictModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=512)


class TenantLimitsRequest(StrictModel):
    max_active: int | None = Field(default=None, ge=0, le=10_000)
    capacity_units: int | None = Field(default=None, ge=0, le=100_000)
    enabled: bool | None = None
    #: Accepted so the refusal can explain itself rather than arriving as a bare
    #: "extra_forbidden". This control plane has no cost attribution source -- no
    #: billing export, no per-attempt spend -- so a dollar budget could be stored
    #: and echoed back but never enforced, and ParkReason.BUDGET_EXHAUSTED would
    #: never be reached. The route rejects it; see routes/admin.py.
    monthly_budget_usd: float | None = Field(default=None, ge=0)


class TenantFindingsEpicRequest(StrictModel):
    """`PUT /v1/admin/tenants/{id}/findings-epic` (#638).

    The issue number, in the repository each run works on, that a review's
    minor findings are filed on as comments; null stops the filing. Required,
    so an empty body is a 422 rather than a silent clear. Strict, so `"638"`
    and `true` are refused rather than coerced to an issue nobody chose.
    """

    findings_epic: StrictInt | None = Field(..., ge=1, le=2_147_483_647)


# --------------------------------------------------------------------------
# The account pool (proxied to the quota broker)
# --------------------------------------------------------------------------

#: The kinds of account the pool holds (#2688).
#:
#: Every account in this pool is a ROTATING OAUTH PAIR the quota broker
#: refreshes on a timer, where refreshing REVOKES the token it replaces: a
#: Claude subscription (`anthropic`) or, since the owner's choice of
#: 2026-11-01, a ChatGPT plan (`openai`, which codex runs on). There is no
#: API-key account and no credential-kind selector -- nothing in the pool's
#: write path, the broker's refresher or the worker's lease would know what to
#: do with one. The broker keeps the same pair as `ACCOUNT_PROVIDERS` in
#: `quota_broker.main` and refuses anything else; this service does not import
#: the broker, so the pair is restated here and checked against it by test.
#: A tenant's own provider API key is a different thing entirely and has a
#: different route (`POST /v1/tenants/me/credentials`), which this does not
#: touch.
SUBSCRIPTION_PROVIDER = "anthropic"
PLAN_PROVIDER = "openai"
ACCOUNT_PROVIDERS = (SUBSCRIPTION_PROVIDER, PLAN_PROVIDER)
AccountProvider = Literal["anthropic", "openai"]


class AccountSignInStart(StrictModel):
    """Begin adding an account by signing in to its provider in a browser.

    No credential here, which is the whole point: the person signs in with
    Anthropic or OpenAI and we never see anything but the code the provider
    hands back afterwards.

    `provider` defaults to `anthropic`, so a Claude sign-in sends nothing new
    and behaves exactly as it did before ChatGPT plans existed. It is forwarded
    to the broker unchanged; the broker picks the authorize page and the
    redirect from it and echoes both in its answer.
    """

    label: str = Field(min_length=1, max_length=64)
    provider: AccountProvider = SUBSCRIPTION_PROVIDER
    lend_to: list[str] = Field(default_factory=list, max_length=50)


class AccountSignInFinish(StrictModel):
    """Finish it, with what the callback page showed.

    No `provider` field: the broker's exchange takes none. The PENDING sign-in
    the broker keyed by `state` records which provider started it, so the
    finish cannot disagree with the start.
    """

    state: str = Field(min_length=8, max_length=256)
    #: Taken as pasted. The callback renders the code followed by a hash and
    #: the state, and people paste what is on screen; the broker splits it.
    code: str = Field(min_length=4, max_length=2048)


class AccountCreate(StrictModel):
    """Register a Claude subscription or a ChatGPT plan into the CALLER's pool.

    A rotating OAuth pair, and nothing else: see `ACCOUNT_PROVIDERS` above.
    There is no `api_key` field here and no kind selector beyond `provider`,
    because every member of the pool is a pair the broker refreshes.

    THE OWNER IS THE TENANT ON THE VERIFIED TOKEN, always. `owner_tenant` below
    is read for one purpose only -- to be compared against it -- and its value
    is never what the account is filed under, so there is no ordering of
    validation in which a body field could put a live credential into somebody
    else's pool.

    It is accepted at all because the broker's own route takes that field (this
    API mirrors the broker's contract) and because the refusal can then explain
    itself: a caller naming another tenant is told the rule, rather than getting
    a bare `extra_forbidden` from `extra="forbid"` and guessing.
    """

    #: Optional, and only ever equal to the caller's own tenant. See above.
    owner_tenant: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    #: One of `ACCOUNT_PROVIDERS`, defaulting to a Claude subscription. A plain
    #: string rather than a Literal so it is compared case-insensitively and a
    #: caller naming anything else gets a 422 that names it and lists the known
    #: providers. `routes/accounts.py` does the comparison.
    provider: str = Field(default=SUBSCRIPTION_PROVIDER, max_length=64)
    #: Tenants this account will lend spare capacity to. Isolation is the
    #: default (CONTRACT.md invariant 9); this is the narrowing, with a name on
    #: it, and only the owner can set it.
    lend_to: list[str] = Field(default_factory=list, max_length=50)
    #: Write-only, and taken AS PASTED -- Claude Code's keychain item, including
    #: its `claudeAiOauth` wrapper. `quota_broker.oauth.parse_credential` is the
    #: single reader of that shape; parsing it here would be a second definition
    #: of it, and the two would disagree the first time it changed.
    #:
    #: No response model in this service contains this field and no route in
    #: this API can return key material -- the same rule the tenant credential
    #: route states.
    credential: str = Field(min_length=8, max_length=16384)


#: An account's health (#2302), as `GET /v1/accounts/{id}/health` and each row
#: of `GET /v1/accounts` serve it. Allow-lists, not types: the broker
#: (`quota_broker.accountstore`) owns the record, and `routes/accounts.py`
#: serves a value outside these as nothing rather than passing it through.
#: The four statuses, by the broker's rule (`accountstore.health_status`):
#: re-auth first, then limited, then erroring (3+ errors in the last hour).
ACCOUNT_HEALTH_STATUSES = ("healthy", "limited", "erroring", "reauth_needed")
#: What an event may be: a rate limit or an exhausted report; a provider error
#: by kind (auth, overloaded 529, 5xx, network); refused at assignment; a
#: re-auth mark; a credential refresh that failed.
ACCOUNT_HEALTH_KINDS = (
    "rate_limit",
    "exhausted",
    "auth",
    "overloaded",
    "server_error",
    "network",
    "assign_refused",
    "reauth",
    "refresh_failed",
)
#: The window a rate-limit event names.
ACCOUNT_HEALTH_WINDOWS = ("five_hour", "seven_day", "other")
#: The count windows the record serves.
ACCOUNT_HEALTH_SPANS = ("last_1h", "last_24h", "last_7d")
#: The listing row's latest hold deadline (#2222, the Timeline's B3/B4): the
#: latest `expires_at` among the account's live holds, as the broker
#: (`quota_broker.main.account_to_api`) serves it. Allowed only as an ISO
#: instant with its zone; `routes/accounts.py` serves anything else as null,
#: and keeps a row from an older broker without the key.
ACCOUNT_HOLD_DEADLINE = "latest_hold_deadline"


class AccountLending(StrictModel):
    lend_to: list[str] = Field(default_factory=list, max_length=50)


class AccountStateChange(StrictModel):
    #: AVAILABLE | PAUSED | DRAINING | REAUTH_REQUIRED. Deliberately not an enum
    #: here: the broker owns the state machine and answers an unknown value with
    #: a 422 listing what it accepts, and a second copy of that list in this
    #: service is one more thing that can drift out of step with it.
    state: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=512)

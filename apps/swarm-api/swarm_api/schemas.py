"""Request and response schemas.

Every request model sets `extra="forbid"`. That is the schema-level half of
invariant 10: a caller who sends `image`, `command`, `backend`, `cpu` or
`service_account` gets a 422 naming the field, rather than having it silently
dropped and believing it took effect. The other half is that no model below has
a field for any of those things in the first place.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

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
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    #: Provider model name, recorded for attribution and cost reporting. It
    #: selects nothing about the container.
    model: str | None = Field(default=None, max_length=128)

    @field_validator("repository_url")
    @classmethod
    def _repo_scheme(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("https://", "git@", "ssh://")):
            raise ValueError("repository_url must be an https://, ssh:// or git@ URL")
        return value


class TaskBatchCreate(StrictModel):
    tasks: list[TaskCreate] = Field(min_length=1)


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------

class WorkflowStepCreate(StrictModel):
    step_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")
    runner_profile: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    #: A NAMED class from the frozen catalogue, and only one no larger than the
    #: profile's own. Not a resource spec.
    resource_class: str | None = Field(default=None, max_length=64)
    #: upstream step_id -> artifact filename staged into this step's workspace.
    input_from: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)


class WorkflowCreate(StrictModel):
    steps: list[WorkflowStepCreate] = Field(min_length=1)
    priority: int = Field(default=0, ge=-100, le=100)
    on_step_failure: Literal["fail_workflow", "continue"] = "fail_workflow"
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Tenant credentials
# --------------------------------------------------------------------------

class CredentialCreate(StrictModel):
    provider: str = Field(min_length=1, max_length=64)
    #: Write-only. No response model in this service contains this field, and
    #: `credentials.py` has no function that returns a payload.
    api_key: str = Field(min_length=8, max_length=8192)


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

class PauseRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=512)


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


class DrainRequest(StrictModel):
    #: Draining disables admission into the pool and lets in-flight work finish.
    #: Undraining is the same call with `drain=false`.
    drain: bool = True
    reason: str | None = Field(default=None, max_length=512)


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


# --------------------------------------------------------------------------
# The account pool (proxied to the quota broker)
# --------------------------------------------------------------------------

#: The one kind of credential the account pool handles.
#:
#: An account in this pool is a CLAUDE SUBSCRIPTION: a rotating OAuth pair the
#: quota broker refreshes on a timer, where refreshing REVOKES the token it
#: replaces. There is no API-key account and no credential-kind selector --
#: nothing in the pool's write path, the broker's refresher or the worker's
#: lease would know what to do with one. A tenant's own provider API key is a
#: different thing entirely and has a different route
#: (`POST /v1/tenants/me/credentials`), which this constant does not touch.
SUBSCRIPTION_PROVIDER = "anthropic"


class AccountCreate(StrictModel):
    """Register a Claude subscription into the CALLER's pool.

    A SUBSCRIPTION, and nothing else: see `SUBSCRIPTION_PROVIDER` above. There
    is no `api_key` field here and no kind selector, because the pool has one
    kind of member.

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
    #: Not a choice: `SUBSCRIPTION_PROVIDER` is the only accepted value. The
    #: field survives so a caller naming anything else gets a 422 that says why
    #: rather than a bare `extra_forbidden`, and so the body still mirrors the
    #: broker's own contract. `routes/accounts.py` does the comparison.
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


class AccountLending(StrictModel):
    lend_to: list[str] = Field(default_factory=list, max_length=50)


class AccountStateChange(StrictModel):
    #: AVAILABLE | PAUSED | DRAINING | REAUTH_REQUIRED. Deliberately not an enum
    #: here: the broker owns the state machine and answers an unknown value with
    #: a 422 listing what it accepts, and a second copy of that list in this
    #: service is one more thing that can drift out of step with it.
    state: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=512)

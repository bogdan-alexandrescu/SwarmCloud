"""Request and response schemas.

Every request model sets `extra="forbid"`. That is the schema-level half of
invariant 10: a caller who sends `image`, `command`, `backend`, `cpu` or
`service_account` gets a 422 naming the field, rather than having it silently
dropped and believing it took effect. The other half is that no model below has
a field for any of those things in the first place.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    limit: int = Field(ge=0, le=100_000)


class NamedLimitRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    limit: int = Field(ge=0, le=100_000)


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
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    enabled: bool | None = None

"""The schedule job-type catalogue: twelve types, picked BY NAME (docs/schedules.md §3).

A schedule names a type from this catalogue and nothing else: no image, no
command, no profile, no resource class, no backend parameter (invariant 10).
A type that runs an agent names its profile here, in code. The catalogue has
the shape of the frozen `RUNNER_PROFILES` (`available`, `disabled_reason`) but
is not part of the frozen contract, and must not leak into it (§1.5).

Each entry declares what §3 says it must: the executor, the parameter model
(extra keys refused by name), the minimum interval, the default and floor
gates, the default budget and catch-up, the risk tier, whether it pushes, and
who may create it.

AVAILABILITY IS THE PRESENCE OF THE EXECUTOR MODULE. Type `issue-sweep` is
available when `swarm_api/schedtypes/issue_sweep.py` exists, and so on. That
is what lets the later type lanes (S6, S10a-h, §9) each ADD ONE FILE and edit
nothing shared: two lanes adding two files cannot overwrite each other, where
two lanes flipping two flags in this file would. Presence is a file check, not
an import, so listing the catalogue imports nothing that might not exist.

BUDGET CAPS (SD4). The owner set per-run, per-day and concurrency caps for the
first four types (§4.3); "the other types' figures are set in their lanes". So
an entry without caps here takes them from its executor module's `BUDGET_CAPS`
(a `BudgetCaps`), read only once the module is present; a present module that
declares none leaves the type unavailable, because a schedule whose budget
cannot be bounded must not be creatable. No measurement backs any figure: they
are the owner's caps.

GITHUB PERMISSIONS (lane S0, §0.1 check 1, read 2026-10-08 at ce68220). The
onboarding GitHub App holds `checks: read` and NO `actions` permission, so
`release-health` (which reads workflow runs) cannot work for an
onboarding-connected tenant until the owner adds `Actions: Read` to the App,
and `ci-flake-hunter` finds flakes from check runs alone. `forge_permissions`
records what each type reads, so its lane and the route can say which a
tenant's token lacks; it does not make a type available or unavailable here.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from .schemas import WorkflowCreate

# --------------------------------------------------------------------------
# Gate points and their order of strictness (§4.2)
# --------------------------------------------------------------------------

#: Each point's values, LOOSEST FIRST. A value's index is its strictness, so
#: "at or above the floor" is an index comparison. `merge: off` (never merge)
#: is stricter than `approve` (merge after a person says so).
GATE_POINTS: dict[str, tuple[str, ...]] = {
    "run": ("auto", "approve"),
    "plan": ("auto", "approve"),
    "merge": ("auto", "approve", "off"),
}


def strictness(point: str, value: str) -> int:
    return GATE_POINTS[point].index(value)


@dataclass(frozen=True)
class GateSpec:
    """The three points of a gate (§4.2). `approvers` is not floored: every floor is `members`."""

    run: str = "auto"
    plan: str = "auto"
    merge: str = "off"

    def __post_init__(self) -> None:
        for point, allowed in GATE_POINTS.items():
            if getattr(self, point) not in allowed:
                raise ValueError(f"gate {point}={getattr(self, point)!r} is not one of {allowed}")

    def as_dict(self) -> dict[str, str]:
        return {"run": self.run, "plan": self.plan, "merge": self.merge}


@dataclass(frozen=True)
class BudgetCaps:
    """The platform caps for one type, which are also its defaults (§4.3, SD4)."""

    per_run_usd: float
    per_day_usd: float
    max_concurrent: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "per_run_usd": self.per_run_usd,
            "per_day_usd": self.per_day_usd,
            "max_concurrent": self.max_concurrent,
        }


# --------------------------------------------------------------------------
# Parameter models, one per type (§3.1-§3.12)
# --------------------------------------------------------------------------

_LABEL = Field(min_length=1, max_length=50)
_REPO_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/*-]{1,200}$")


class Params(BaseModel):
    """A type's parameters. Extra keys are refused by name, as `PlanSpec` refuses them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _unique(values: list[str], name: str) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} lists a value twice")
    return values


def _repo_paths(values: list[str]) -> list[str]:
    for value in values:
        if not _REPO_PATH.match(value):
            raise ValueError(f"{value!r} is not a relative path inside the repository")
    return _unique(values, "paths")


class _Selection(Params):
    """The issue selection §3.1 and §3.2 share."""

    labels_include: list[str] = Field(default_factory=list, max_length=20)
    #: `security` is here by default and is ALSO a hard stop (§4.4): removing
    #: it from this list does not let a security-class plan through unheld.
    labels_exclude: list[str] = Field(
        default_factory=lambda: ["security", "needs-owner", "wontfix", "question"], max_length=20
    )
    max_new_per_firing: int = Field(default=3, ge=1, le=8)
    cooldown_hours: int = Field(default=72, ge=1, le=720)

    @field_validator("labels_include", "labels_exclude")
    @classmethod
    def _labels(cls, values: list[str]) -> list[str]:
        for value in values:
            if not 1 <= len(value) <= 50:
                raise ValueError("a label is 1-50 characters")
        return _unique(values, "labels")


class IssueSweepParams(_Selection):
    #: Lane SWEEP's cap of 8 is the upper bound: one firing holds at most 8
    #: tenant slots of work at once.
    max_live_runs: int = Field(default=8, ge=1, le=8)
    #: These two restate the schedule's gate for the runs it creates; the gate
    #: is the authority and `schedules.resolve_params` keeps them equal to it.
    plan_approval: Literal["auto", "required"] = "auto"
    merge: Literal["off", "approve", "auto"] = "approve"
    fix_rounds: int = Field(default=3, ge=1, le=5)
    #: Not settable to false by a member (§3.1); `schedules.resolve_params`
    #: refuses it unless the caller is a platform admin.
    territory_guard: StrictBool = True


class IssuePlanOnlyParams(_Selection):
    #: A new plan is not made while this many are waiting for a person.
    max_pending_plans: int = Field(default=5, ge=1, le=20)
    fix_rounds: int = Field(default=3, ge=1, le=5)


class RepoIndexRefreshParams(Params):
    kind: Literal["incremental", "full"] = "full"
    only_if_behind: StrictBool = False


OBSERVER_FOCUS = ("cost", "latency", "ci", "idle_fixes", "titles", "parks", "refusals")


class ObserverParams(Params):
    window_hours: int = Field(default=24, ge=1, le=168)
    focus: list[Literal["cost", "latency", "ci", "idle_fixes", "titles", "parks", "refusals"]] = Field(
        default_factory=lambda: list(OBSERVER_FOCUS), min_length=1
    )
    file_issues: StrictBool = False
    #: The repository the report's epic is filed in (owner decision
    #: 2026-10-11: the schedule names it, because the report's task has no
    #: repository). Required when `file_issues` is true, and then one of the
    #: tenant's registrations the owner can write -- checked at create, at
    #: edit and again at fire time by `schedtypes/observer.py`, not here: a
    #: model refusal would also refuse a stored schedule at fire time, where
    #: the answer is to file nothing, not to fail the report.
    file_issues_repo_id: str | None = Field(default=None, pattern=r"^repo_[0-9a-f]{16}$")

    @field_validator("focus")
    @classmethod
    def _focus(cls, values: list[str]) -> list[str]:
        return _unique(values, "focus")


class EpicTriageParams(Params):
    epic_label: str = Field(default="epic", min_length=1, max_length=50)
    max_epics: int = Field(default=5, ge=1, le=20)


class LabelAction(Params):
    add: list[str] = Field(default_factory=list, max_length=5)
    remove: list[str] = Field(default_factory=list, max_length=5)


class PrShepherdParams(Params):
    stale_hours: int = Field(default=48, ge=1, le=720)
    #: Conditions at the pull request's head, each to labels added or removed.
    labels: dict[Literal["stale", "ci_red", "behind", "conflict"], LabelAction] = Field(
        default_factory=lambda: {
            "stale": LabelAction(add=["stale"]),
            "ci_red": LabelAction(remove=["ready"]),
        }
    )
    update_branch: StrictBool = True
    fix_conflicts: StrictBool = False
    max_prs_per_firing: int = Field(default=10, ge=1, le=50)


class CiFlakeHunterParams(Params):
    window_days: int = Field(default=7, ge=1, le=30)
    min_occurrences: int = Field(default=2, ge=1, le=50)
    max_flakes: int = Field(default=3, ge=1, le=10)
    diagnose: StrictBool = False


class DependencyCveRefreshParams(Params):
    paths: list[str] = Field(default_factory=lambda: ["images/"], min_length=1, max_length=20)
    severity_at_least: Literal["low", "medium", "high", "critical"] = "high"
    max_packages: int = Field(default=10, ge=1, le=50)

    @field_validator("paths")
    @classmethod
    def _paths(cls, values: list[str]) -> list[str]:
        return _repo_paths(values)


class ReleaseHealthParams(Params):
    workflow: str = Field(default="release.yml", pattern=r"^[A-Za-z0-9._-]{1,100}\.ya?ml$")
    #: None is the repository's default branch, read at firing time.
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    issue_label: str = Field(default="bug", min_length=1, max_length=50)


class DocsDriftParams(Params):
    paths: list[str] = Field(default_factory=lambda: ["docs/"], min_length=1, max_length=20)
    fix: StrictBool = False

    @field_validator("paths")
    @classmethod
    def _paths(cls, values: list[str]) -> list[str]:
        return _repo_paths(values)


class CostReportParams(Params):
    window: Literal["day", "week"] = "day"
    post_to_issue: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]{0,9}$")


class CustomPromptParams(Params):
    #: The same model `POST /v1/workflows` validates, so any profile it names
    #: is named, never an image.
    spec: WorkflowCreate
    max_steps: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _steps_within_max(self) -> "CustomPromptParams":
        if len(self.spec.steps) > self.max_steps:
            raise ValueError(f"spec has {len(self.spec.steps)} steps; max_steps is {self.max_steps}")
        return self


# --------------------------------------------------------------------------
# The entries
# --------------------------------------------------------------------------

Executor = Literal["issue_runs", "task", "workflow", "api"]
Role = Literal["member", "admin", "owner"]
ROLE_ORDER: tuple[Role, ...] = ("member", "admin", "owner")

#: Where executor modules live. A type is available when its file is here.
EXECUTOR_DIR = Path(__file__).resolve().parent / "schedtypes"
EXECUTOR_PACKAGE = "swarm_api.schedtypes"


def _fixed(tier: str) -> Callable[[GateSpec, BaseModel], str]:
    return lambda gate, params: tier


def _never(params: BaseModel) -> bool:
    return False


def _always(params: BaseModel) -> bool:
    return True


@dataclass(frozen=True)
class ScheduleType:
    name: str
    description: str
    executor: Executor
    params_model: type[Params]
    min_interval: timedelta
    default_gate: GateSpec
    floor_gate: GateSpec
    #: None: the executor module declares `BUDGET_CAPS` (see the module docstring).
    budget: BudgetCaps | None
    catch_up: Literal["skip", "run_once"]
    #: The risk tier (§4.1) from the resolved gate and parameters.
    risk: Callable[[GateSpec, BaseModel], str]
    #: Whether the work pushes to a repository (§5.4: the owner needs `write`).
    pushes: Callable[[BaseModel], bool]
    #: Who may create it, per scope mode (§3 "creates", §3.13). A mode not
    #: listed is refused for the type.
    creators: Mapping[str, Role]
    #: The profile its agent work runs, named in code (invariant 10); None for
    #: an `api` type that runs no agent (SD8).
    profile: str | None
    #: The build lane that adds its executor (§9).
    lane: str
    phase: int
    #: Who must create it in a repository registered `platform: true` (§3).
    platform_repository_creator: Role | None = None
    forge_permissions: tuple[str, ...] = ()
    gate_notes: str = ""

    @property
    def module_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def module(self) -> str:
        return f"{EXECUTOR_PACKAGE}.{self.module_name}"


_PR_PUSH = ("contents:write", "pull_requests:write")

TYPES: tuple[ScheduleType, ...] = (
    ScheduleType(
        name="issue-sweep",
        description="Start an issue run for each ready open issue: plan, execute, review, and merge behind the merge gate.",
        executor="issue_runs",
        params_model=IssueSweepParams,
        min_interval=timedelta(minutes=15),
        default_gate=GateSpec(run="auto", plan="auto", merge="approve"),
        # `merge: auto` is reachable only through the audited switch (SD3).
        floor_gate=GateSpec(run="auto", plan="auto", merge="auto"),
        budget=BudgetCaps(per_run_usd=15, per_day_usd=120, max_concurrent=8),
        catch_up="skip",
        risk=lambda gate, params: "R3" if gate.merge == "auto" else "R2",
        pushes=_always,
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S6",
        phase=1,
        forge_permissions=("issues:write", *_PR_PUSH, "checks:read"),
        gate_notes="merge: auto only through the audited switch; in a platform repository only a platform admin may switch it",
    ),
    ScheduleType(
        name="issue-plan-only",
        description="Plan the ready open issues and wait: nothing executes until a person approves each plan.",
        executor="issue_runs",
        params_model=IssuePlanOnlyParams,
        min_interval=timedelta(minutes=15),
        default_gate=GateSpec(run="auto", plan="approve", merge="approve"),
        floor_gate=GateSpec(run="auto", plan="approve", merge="approve"),
        budget=BudgetCaps(per_run_usd=3, per_day_usd=30, max_concurrent=5),
        catch_up="skip",
        risk=_fixed("R1"),
        # An approved plan executes and pushes, so the owner needs `write`.
        pushes=_always,
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S6",
        phase=1,
        forge_permissions=("issues:write", *_PR_PUSH),
    ),
    ScheduleType(
        name="repo-index-refresh",
        description="Queue a repository index run for each repository in scope, never duplicating one in flight.",
        executor="task",
        params_model=RepoIndexRefreshParams,
        min_interval=timedelta(hours=1),
        default_gate=GateSpec(run="auto", plan="auto", merge="off"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="off"),
        budget=BudgetCaps(per_run_usd=5, per_day_usd=40, max_concurrent=4),
        catch_up="run_once",
        risk=_fixed("R0"),
        pushes=_never,
        creators={"repos": "member", "all": "member"},
        profile="indexer",
        lane="S6",
        phase=1,
        forge_permissions=("contents:read",),
    ),
    ScheduleType(
        name="observer",
        description="A read-only report over the tenant's recent work, with proposals for the inbox.",
        executor="task",
        params_model=ObserverParams,
        min_interval=timedelta(hours=1),
        default_gate=GateSpec(run="auto", plan="auto", merge="off"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="off"),
        budget=BudgetCaps(per_run_usd=5, per_day_usd=10, max_concurrent=1),
        catch_up="run_once",
        risk=_fixed("R0"),
        pushes=_never,
        # The platform variant reads every tenant's aggregates: owner only.
        creators={"repos": "member", "all": "member", "platform": "owner"},
        profile="claude-code",
        lane="S6",
        phase=1,
    ),
    ScheduleType(
        name="epic-triage",
        description="Propose ticks for open epics' boxes, each with its evidence; swarm-api edits the comments once approved.",
        executor="task",
        params_model=EpicTriageParams,
        min_interval=timedelta(hours=6),
        default_gate=GateSpec(run="auto", plan="approve", merge="off"),
        floor_gate=GateSpec(run="auto", plan="approve", merge="off"),
        budget=None,
        catch_up="skip",
        risk=_fixed("R1"),
        pushes=_never,
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S10",
        phase=2,
        forge_permissions=("issues:write",),
    ),
    ScheduleType(
        name="pr-shepherd",
        description="Label stale pull requests, update branches that are behind, and fix conflicts on the platform's own.",
        executor="api",
        params_model=PrShepherdParams,
        min_interval=timedelta(minutes=30),
        default_gate=GateSpec(run="approve", plan="auto", merge="off"),
        # Fixes included at auto: below the default, so a platform admin only.
        floor_gate=GateSpec(run="auto", plan="auto", merge="off"),
        budget=None,
        catch_up="skip",
        risk=lambda gate, params: "R2" if params.fix_conflicts else "R1",
        # Update-branch writes a merge commit to the pull request's branch.
        pushes=lambda params: bool(params.update_branch or params.fix_conflicts),
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S10",
        phase=2,
        forge_permissions=("pull_requests:write", "checks:read"),
        gate_notes="the run point governs conflict-fix continuations only; the API actions (labels, update-branch, comments) are R1 and run at auto",
    ),
    ScheduleType(
        name="ci-flake-hunter",
        description="Find checks that failed then passed at the same sha, and file or update one issue per flaky test.",
        executor="api",
        params_model=CiFlakeHunterParams,
        min_interval=timedelta(hours=6),
        default_gate=GateSpec(run="auto", plan="auto", merge="off"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="off"),
        budget=None,
        catch_up="skip",
        risk=_fixed("R1"),
        pushes=_never,
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S10",
        phase=2,
        # §0.1 check 1: `checks: read` is held; workflow-run reads would need
        # `actions: read`, which the onboarding App does not hold.
        forge_permissions=("checks:read", "issues:write"),
    ),
    ScheduleType(
        name="dependency-cve-refresh",
        description="Bump fixable vulnerable package pins from the latest image scan, as one reviewed pull request.",
        executor="workflow",
        params_model=DependencyCveRefreshParams,
        min_interval=timedelta(hours=24),
        default_gate=GateSpec(run="auto", plan="auto", merge="approve"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="approve"),
        budget=None,
        catch_up="skip",
        risk=_fixed("R2"),
        pushes=_always,
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S10",
        phase=2,
        forge_permissions=("checks:read", *_PR_PUSH),
    ),
    ScheduleType(
        name="release-health",
        description="Open or update one issue per failing streak of a named workflow; comment when it is green again.",
        executor="api",
        params_model=ReleaseHealthParams,
        min_interval=timedelta(minutes=15),
        default_gate=GateSpec(run="auto", plan="auto", merge="off"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="off"),
        budget=None,
        catch_up="run_once",
        risk=_fixed("R1"),
        pushes=_never,
        creators={"repos": "member", "all": "member"},
        profile=None,
        lane="S10",
        phase=2,
        platform_repository_creator="owner",
        # §0.1 check 1: the onboarding App holds no `actions` permission, so
        # this type cannot read workflow runs for an onboarding-connected
        # tenant until the owner adds `Actions: Read` to the App.
        forge_permissions=("actions:read", "issues:write"),
    ),
    ScheduleType(
        name="docs-drift",
        description="Check docs' citations and stated defaults against the code; with fix, open a docs-only pull request.",
        executor="task",
        params_model=DocsDriftParams,
        min_interval=timedelta(hours=24),
        default_gate=GateSpec(run="auto", plan="auto", merge="approve"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="approve"),
        budget=None,
        catch_up="run_once",
        risk=lambda gate, params: "R2" if params.fix else "R1",
        pushes=lambda params: bool(params.fix),
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S10",
        phase=2,
        forge_permissions=("contents:read",),
    ),
    ScheduleType(
        name="cost-report",
        description="Sum reported cost over the window by schedule, repository, profile and person, with coverage.",
        executor="api",
        params_model=CostReportParams,
        min_interval=timedelta(hours=24),
        default_gate=GateSpec(run="auto", plan="auto", merge="off"),
        floor_gate=GateSpec(run="auto", plan="auto", merge="off"),
        budget=None,
        catch_up="run_once",
        risk=_fixed("R0"),
        pushes=_never,
        creators={"repos": "member", "all": "member", "platform": "admin"},
        profile=None,
        lane="S10",
        phase=2,
    ),
    ScheduleType(
        name="custom-prompt",
        description="Run a saved, signed workflow spec; a platform admin approves each new spec digest before it fires.",
        executor="workflow",
        params_model=CustomPromptParams,
        min_interval=timedelta(hours=6),
        default_gate=GateSpec(run="approve", plan="auto", merge="approve"),
        # The floor equals the default: members cannot lower it, and
        # `merge: auto` is never allowed, an admin included.
        floor_gate=GateSpec(run="approve", plan="auto", merge="approve"),
        budget=None,
        catch_up="skip",
        risk=_fixed("R3"),
        pushes=_always,
        creators={"repos": "member", "all": "member"},
        profile="claude-code",
        lane="S10",
        phase=3,
        forge_permissions=_PR_PUSH,
        gate_notes="each new spec digest needs a one-time platform-admin approval",
    ),
)

BY_NAME: dict[str, ScheduleType] = {entry.name: entry for entry in TYPES}


def get(name: str) -> ScheduleType | None:
    return BY_NAME.get(name)


# --------------------------------------------------------------------------
# Availability, caps and who may create
# --------------------------------------------------------------------------


def executor_present(entry: ScheduleType, root: Path = EXECUTOR_DIR) -> bool:
    """Whether the executor module's FILE exists. Nothing is imported."""
    return (root / f"{entry.module_name}.py").is_file()


def budget_caps(entry: ScheduleType, root: Path = EXECUTOR_DIR) -> BudgetCaps | None:
    """The type's caps: the catalogue's, else its present executor module's `BUDGET_CAPS`."""
    if entry.budget is not None:
        return entry.budget
    if not executor_present(entry, root):
        return None
    caps = getattr(importlib.import_module(entry.module), "BUDGET_CAPS", None)
    return caps if isinstance(caps, BudgetCaps) else None


def availability(entry: ScheduleType, root: Path = EXECUTOR_DIR) -> tuple[bool, str]:
    """`(available, disabled_reason)`, as profiles serve them."""
    if not executor_present(entry, root):
        reason = (
            f"not built yet: its executor, swarm_api/schedtypes/{entry.module_name}.py, "
            f"is lane {entry.lane} of docs/schedules.md §9"
        )
        if "actions:read" in entry.forge_permissions:
            reason += "; it also needs actions: read, which the onboarding GitHub App does not hold"
        return False, reason
    if budget_caps(entry, root) is None:
        return False, "its executor declares no BUDGET_CAPS, so a schedule of it could not be bounded (SD4)"
    return True, ""


def available_names(root: Path = EXECUTOR_DIR) -> list[str]:
    return [entry.name for entry in TYPES if availability(entry, root)[0]]


def creator_role(
    entry: ScheduleType,
    scope_mode: str,
    *,
    platform_repository: bool = False,
    merge_auto: bool = False,
) -> Role | None:
    """The least role that may create `entry` in `scope_mode`; None when the mode is refused.

    In a repository registered `platform: true`, a type's own platform rule
    applies, and any gate allowing an unattended merge is owner-only (§3):
    both put the platform's own code within one approval's reach.
    """
    role = entry.creators.get(scope_mode)
    if role is None:
        return None
    needed = [role]
    if platform_repository and entry.platform_repository_creator:
        needed.append(entry.platform_repository_creator)
    if platform_repository and merge_auto:
        needed.append("owner")
    return max(needed, key=ROLE_ORDER.index)


def describe(entry: ScheduleType, root: Path = EXECUTOR_DIR) -> dict[str, Any]:
    """One row of `GET /v1/schedule-types` (§7.1)."""
    available, reason = availability(entry, root)
    caps = budget_caps(entry, root) if available else entry.budget
    return {
        "name": entry.name,
        "description": entry.description,
        "executor": entry.executor,
        "params_schema": entry.params_model.model_json_schema(),
        "default_gate": entry.default_gate.as_dict(),
        "floor_gate": entry.floor_gate.as_dict(),
        "min_interval_minutes": int(entry.min_interval.total_seconds() // 60),
        "budget_caps": caps.as_dict() if caps else None,
        "catch_up": entry.catch_up,
        "scopes": sorted(entry.creators),
        "forge_permissions": list(entry.forge_permissions),
        "gate_notes": entry.gate_notes,
        "phase": entry.phase,
        "available": available,
        "disabled_reason": reason,
    }


def catalogue(root: Path = EXECUTOR_DIR) -> list[dict[str, Any]]:
    return [describe(entry, root) for entry in TYPES]

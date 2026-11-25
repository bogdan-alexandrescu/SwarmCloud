"""The model class a step runs, by its ROLE, when its caller named none.

The owner decided on 2026-11-23, from 30 days of run data: review, re-review,
fix, CI-fix and conflict-fix steps on the default class (`opus`) were about a
third of all recorded cost, and 373 of 413 planner tasks ran on `opus`
because nothing named a planning class. `ROLE_CLASSES` is that decision, as
data: every site that submits a step of one of these roles calls `choose`,
so a later change is one edit to the table (docs/design/model-per-step.md
§4.12).

THE RULES, in order (`choose`):

  * a class the caller named always wins (`CALLER`);
  * a profile that runs no Claude model (`codex`, `mock`) gets no class,
    whatever its role;
  * the role's class when the profile accepts it and this deployment pins it
    (`Settings.worker_class_models`); a role class equal to the profile's
    default is sent as nothing, so the step keeps the bytes and the signing
    format it had before classes existed (§4.3);
  * otherwise the profile's default, exactly as the requirements check falls
    back (`issueruns._check_class`): a class with no pin would be refused at
    submission and fail the whole run.

THE FIRST REVIEW is decided where its diff is read: the review gate
(`verdictpublish._consider_review`), which already reads the change's paths
and size to decide whether the review runs at all. Submission stores the
review on the profile's default (`opus`) and marks it `REVIEW_AT_GATE`; when
the gate lets it run, `review_role` picks its role from what the gate read,
and swarm-api re-signs the step on the new class in the write that hands it
back to the scheduler. A review whose gate never decides it (a lost wake, a
gate switched off for the step) runs the `opus` it was signed with: the
fallback spends the dearer class, never the cheaper one on a risk path.

WHAT IS SIGNED, AND WHAT IS ONLY A RECORD. A tenant's agents can write any
task document of their tenant, so nothing here decides from a field outside
the signed spec:

  * the gate's licence to pick a first review's class is
    `metadata.dispatch.class_at_gate` (`CLASS_AT_GATE_KEY`), written by
    submission alone inside the dispatch block the spec signature covers at
    every format, and which no caller can write (`reject_reserved_metadata`);
    the gate acts on it only once the stored spec verifies (`at_gate`,
    `verdictpublish._review_class`);
  * a class a role chose for a step whose submission NAMED one (a fix
    loop's round, a CI-fix round) is recorded in the same signed block as
    `metadata.dispatch.class_chosen_by`, so `explicit_class` and
    `gate_decided` tell a caller's class from a role's by signed data only.

`metadata.class_chosen_by` is the unsigned DISPLAY record of every choice:
the console shows it and a scorecard counts verdicts by it. It is never read
to pick a model, to let the gate re-sign a step, or to decide whether a
stored class was the caller's. A model class is a name from
`profiles.MODEL_CLASSES`, so none of this accepts an image, a command, a
resource spec or a backend parameter from anyone (invariant 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

#: The date of the owner's decision the table records.
DECIDED = "2026-11-23"

ROLE_PLANNER = "planner"
ROLE_BUILD = "build"
ROLE_REVIEW = "review, no risk path"
ROLE_REVIEW_RISK = "review, risk path"
ROLE_REVIEW_UNREAD = "review, diff not read"
ROLE_REVIEW_LARGE = "review, over the size ceiling"
ROLE_SECURITY_REVIEW = "security review"
ROLE_FIX = "fix"
ROLE_REREVIEW = "re-review"
ROLE_CI_FIX = "CI fix"
ROLE_CONFLICT_FIX = "conflict fix"
#: The issue run's readiness pre-pass (owner, 2026-11-24): a short task before
#: the full planner, on the cheaper class (`issueruns.readiness_task`).
ROLE_READINESS = "readiness pre-pass"

#: Role -> class, applied only when the caller named no class. A fix loop's
#: re-review rounds are `ROLE_REREVIEW`, its fix rounds `ROLE_FIX`.
ROLE_CLASSES: Mapping[str, str] = MappingProxyType({
    ROLE_PLANNER: "fable",
    ROLE_BUILD: "opus",
    ROLE_REVIEW: "sonnet",
    ROLE_REVIEW_RISK: "opus",
    ROLE_REVIEW_UNREAD: "opus",
    ROLE_REVIEW_LARGE: "opus",
    ROLE_SECURITY_REVIEW: "opus",
    ROLE_FIX: "opus",
    ROLE_REREVIEW: "sonnet",
    ROLE_CI_FIX: "sonnet",
    ROLE_CONFLICT_FIX: "sonnet",
    ROLE_READINESS: "sonnet",
})

#: The size ceiling (the owner's decision, 2026-11-23): a first review moves
#: to `ROLE_CLASSES[ROLE_REVIEW]` only when its change touches no risk path
#: AND is at most this many changed lines -- insertions plus deletions over
#: every parent the gate read, `reviewgate.Decision.changed_lines`. Above it
#: the review is `ROLE_REVIEW_LARGE` and keeps the default class.
REVIEW_CHEAP_MAX_LINES = 1000

#: The task metadata key that records which rule chose the class.
CLASS_CHOSEN_BY_KEY = "class_chosen_by"
#: What it says for a class the caller named.
CALLER = "caller"
#: What a first review's submission records: the class is the gate's to pick.
REVIEW_AT_GATE = "role default: review, decided at the review gate"
#: The task's signed `metadata.dispatch` block (`specsign.SIGNED_METADATA_KEYS`).
DISPATCH_KEY = "dispatch"
#: The key, inside the signed dispatch block, that submission sets to True on
#: a first review whose class the gate picks (the module doc).
CLASS_AT_GATE_KEY = "class_at_gate"


@dataclass(frozen=True)
class Choice:
    """A step's class: `model_class` to send (None is the profile's default)
    and the rule that chose it."""

    model_class: str | None
    chosen_by: str

    def fields(self) -> dict[str, Any]:
        """The step's `model_class` key: only when there is a class to send."""
        return {"model_class": self.model_class} if self.model_class is not None else {}

    def metadata(self) -> dict[str, Any]:
        return {CLASS_CHOSEN_BY_KEY: self.chosen_by}


def default_class(role: str) -> str:
    """The class `role` runs by default. KeyError for a role not in the table."""
    return ROLE_CLASSES[role]


def _profile(profile: str | RunnerProfile | None) -> RunnerProfile | None:
    if profile is None or isinstance(profile, RunnerProfile):
        return profile
    return RUNNER_PROFILES.get(str(profile))


def choose(role: str, profile: str | RunnerProfile | None,
           class_models: Mapping[str, str] | None, *, explicit: str | None = None) -> Choice:
    """The class a step of `role` on `profile` runs (the rules in the module doc).

    `class_models` is the deployment's pins (`Settings.worker_class_models`);
    None pins nothing, so every role falls back to the profile's default.
    """
    if explicit is not None:
        return Choice(explicit, CALLER)
    resolved = _profile(profile)
    if resolved is None or not resolved.model_classes:
        return Choice(None, "profile runs no Claude model")
    wanted = ROLE_CLASSES[role]
    by_role = f"role default: {role}"
    if wanted == resolved.default_model_class:
        return Choice(None, by_role)
    if wanted not in resolved.model_classes:
        return Choice(None, f"{by_role}; {resolved.name} does not run {wanted}, profile default")
    if wanted not in (class_models or {}):
        return Choice(None, f"{by_role}; {wanted} not pinned, profile default")
    return Choice(wanted, by_role)


def _signed_block(metadata: Any) -> Mapping[str, Any]:
    """The signed dispatch block of a task's `metadata`, or an empty map."""
    block = metadata.get(DISPATCH_KEY) if isinstance(metadata, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def signed_record(record: str | None, named: str | None) -> str | None:
    """What submission copies into the signed dispatch block for a step
    stored with the record `record` (`submitted_record`) and the class
    `named`: the role's record when a role chose a NAMED class, else None
    (nothing: a named class without one is the caller's)."""
    if named is None or record is None or record == CALLER:
        return None
    return record


def explicit_class(doc: Mapping[str, Any]) -> str | None:
    """A stored task's class when its CALLER named it, else None.

    Decided from signed fields only (the module doc): the class is a role's
    when the signed dispatch block carries a role record for it
    (`signed_record`) or the gate's mark (`CLASS_AT_GATE_KEY`); otherwise
    it is the caller's -- a task from before this module, when only a caller
    ever set a class, included. The unsigned `class_chosen_by` is not read.
    """
    stored = doc.get("model_class")
    if not isinstance(stored, str) or not stored:
        return None
    block = _signed_block(doc.get("metadata"))
    if block.get(CLASS_AT_GATE_KEY) is True or block.get(CLASS_CHOSEN_BY_KEY) is not None:
        return None
    return stored


#: The roles only the review gate picks, after submission (`review_role`).
GATE_ROLES = (ROLE_REVIEW, ROLE_REVIEW_RISK, ROLE_REVIEW_UNREAD, ROLE_REVIEW_LARGE)


def submitted_record(record: Any, named: str | None) -> str | None:
    """The `class_chosen_by` a submission stores for a step that carried
    `record` in its metadata and named the class `named` (None for none).

    The key is unsigned and a caller writes its own metadata, so a record is
    not taken on trust. `REVIEW_AT_GATE` never (None, no record):
    `submit_workflow` marks a first review itself. For a named class, only the record a role rule
    writes for exactly that class (`role default: <role>`, `ROLE_CLASSES[role]`
    equal to it, and not one of the `GATE_ROLES`, which no submission
    writes); any other is `CALLER`, so a caller cannot make its own class
    look role-chosen to `explicit_class` (which reads the copy submission
    signs, `signed_record`) -- short of naming the very class a non-gate
    role would have picked, which only gives up its own fix-loop carry. With no class named the record selects nothing and is
    kept.
    """
    if not isinstance(record, str) or record == REVIEW_AT_GATE:
        return None  # no record: a stored class is the caller's (`explicit_class`)
    if named is None:
        return record
    prefix = "role default: "
    role = record[len(prefix):] if record.startswith(prefix) else None
    if role in ROLE_CLASSES and role not in GATE_ROLES and ROLE_CLASSES[role] == named:
        return record
    return CALLER


def at_gate(metadata: Any) -> bool:
    """Whether a review's class is the review gate's to pick: its signed
    dispatch block carries `CLASS_AT_GATE_KEY` True. Only submission writes
    it, so the unsigned `REVIEW_AT_GATE` record is not read; a reader that
    acts on it verifies the stored spec first (`verdictpublish._review_class`)."""
    return _signed_block(metadata).get(CLASS_AT_GATE_KEY) is True


def gate_decided(metadata: Any) -> bool:
    """Whether a review's class is, or was, the review gate's. The gate
    re-signs the step with its dispatch block unchanged, so the signed mark
    stays after it picks: the same test as `at_gate`."""
    return at_gate(metadata)


def review_role(decision: Any) -> str:
    """A first review's role from what the review gate read (`reviewgate.Decision`).

    A risk path touched (the platform's `DEFAULT_RISK_PATHS` plus the step's
    and the workspace's, `reviewgate._gate_from`) is `ROLE_REVIEW_RISK`,
    looked for over the UNION of each parent's `own_files` and `files` (a
    risk path merged in from a side branch is in `files` alone,
    `Decision.class_risk_hits`); no decision, a parent that could not be
    read, or a file list cut short, absent or unreadable is
    `ROLE_REVIEW_UNREAD`; a diff read whole that touches none but is over
    `REVIEW_CHEAP_MAX_LINES` is `ROLE_REVIEW_LARGE`; only the rest is
    `ROLE_REVIEW`. What the gate runs or skips is not read from here.
    """
    if decision is None:
        return ROLE_REVIEW_UNREAD
    if getattr(decision, "risk_hits", ()) or getattr(decision, "class_risk_hits", ()):
        return ROLE_REVIEW_RISK
    if (getattr(decision, "unreadable", ()) or getattr(decision, "truncated", False)
            or getattr(decision, "files_unread", False)):
        return ROLE_REVIEW_UNREAD
    if not getattr(decision, "read", ()):
        return ROLE_REVIEW_UNREAD
    lines = getattr(decision, "changed_lines", None)
    if not isinstance(lines, int) or lines > REVIEW_CHEAP_MAX_LINES:
        return ROLE_REVIEW_LARGE
    return ROLE_REVIEW


def class_models_of(ctx: Any) -> Mapping[str, str] | None:
    """The deployment's pinned classes from an `AppContext`, None without settings."""
    return getattr(getattr(ctx, "settings", None), "worker_class_models", None)

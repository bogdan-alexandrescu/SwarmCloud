"""`custom-prompt`: run a saved workflow spec, once its digest is approved (docs/schedules.md §3.12, lane S10h).

THE SPEC IS THE SCHEDULE'S `params.spec`, validated by `WorkflowCreate`, the
model `POST /v1/workflows` validates (`scheduletypes.CustomPromptParams`). So
every step names a runner profile BY NAME and nothing else: no image, no
command, no resource spec (invariant 10). Each firing submits it as ONE
workflow through `firing.submit_workflow` -- the owner's `SubmissionService`
path, which signs every step's spec as any workflow's is signed -- marked
with the firing, so a firing that stops half-way is finished by adopting its
tasks (§2.2), never by submitting again.

THE SPEC DIGEST. `spec_digest` is sha256 over the VALIDATED spec as JSON,
keys sorted: what is approved is what runs, and two stored spellings of one
spec (key order, a default written out or left out) are one digest. Editing
the spec makes a new digest.

THE ADMIN'S APPROVAL OF A DIGEST (§3.12 "Activation", §4.5 kind `spec`). A
firing of a digest no platform admin has approved FOR THIS SCHEDULE creates
nothing. It asks for the approval -- one `approvals` record of kind `spec`,
keyed by schedule, digest and round, carrying the spec it approves -- and
records one `api_action` that ended `failed` and says why
(`SPEC_PENDING`, `SPEC_REJECTED`). The decision is the inbox's
(`POST /v1/approvals/{id}:approve` with the digest), where only a platform
admin or the owner may approve a `spec` (routes/approvals.py). Approved once,
every later firing of that digest runs; a rejection stands for that digest
until the spec is edited; an approval that EXPIRED unanswered is asked again
as the next round. This gate is not a refusal switch: it is a document a
person decides, like an issue run's PLANNED wait, and it is enforced from
the first firing. A refusal switch's off position lets the request through,
and an unapproved standing instruction to an agent with push rights is the
one thing this type must never run.

THE GATE (§3.12). `run: approve` is the type's floor (`scheduletypes`), so a
firing waits as a `run` approval in the inbox before this module is asked to
create anything (`schedulefire.Ticker._finish`). `merge` is never `auto`:
every workflow is submitted with `metadata.merge: "off"`, whatever the spec
says, so no merge step is appended and a spec that states its own is
refused by the submission (422, `service.submit_workflow`). The pull request
the workflow opens waits, open, for a person to merge it. That is stricter
than `approve`: a workflow has no merge approval in the inbox yet, only an
issue run does, so "approve merge" here is a person merging on GitHub.

THE REPOSITORY. A spec that names a `repository_url` runs only against a
repository in the schedule's scope, as registered now (`firing.repo_ids`). A
repository outside it is refused with `custom_prompt_repository_out_of_scope`,
a new refusal that ships report-only (`refusals.SWITCHES`); off, the
submission's own registration and grant checks still apply. A spec with no
repository runs with none, as `POST /v1/workflows` would.

GITHUB PERMISSIONS (lane S0, §0.1, read from the repository 2026-10-08). The
type needs `contents: write` and `pull_requests: write`, which the onboarding
App's runbook lists. The App holds `Workflows: No access`, so a spec whose
change touches `.github/workflows/` is refused by GitHub at push for every
onboarding-connected tenant. Nothing here can see that before the agent has
run, so the dry run says it, plainly, every time.

DRY RUN (§2.8): the digest, the approval's state, the steps and their
profiles, the repository and whether it is in scope, and what the
submission would be refused for. It requests no approval and submits
nothing.

INVARIANTS. One workflow, its steps ordinary QUEUED tasks admitted like any
other; an approval request is a document and holds no capacity (1-3). Every
profile is the spec's NAMED one (10). Every read and the approval record are
the schedule's tenant's own (9).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping

from swarm_common.profiles import RUNNER_PROFILES

from .. import approvals, refusals, scheduletypes
from ..errors import Forbidden
from ..repositories import repo_id_for
from ..schemas import WorkflowCreate
from ..validation import MERGE_METADATA_KEY, is_merge_step, merge_repository
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "custom-prompt"

#: SD4 leaves "the other types' figures" to their lanes; no measurement backs
#: these. $15 a run is `issue-sweep`'s per-run cap: one issue run is itself a
#: plan, implement, review and fix workflow, the nearest measured shape to a
#: spec of up to `max_steps` steps. $60 a day is four runs, the most the 6 h
#: minimum interval fires. One at a time: a firing makes one workflow, and a
#: second live one would be the same standing instruction twice.
BUDGET_CAPS = scheduletypes.BudgetCaps(per_run_usd=15, per_day_usd=60, max_concurrent=1)

#: The `api_action` a firing records when its spec digest is not approved.
SPEC_ACTION = "custom_prompt.spec_approval"
SPEC_PENDING = "spec_approval_pending"
SPEC_REJECTED = "spec_approval_rejected"
SPEC_ROUNDS_EXHAUSTED = "spec_approval_rounds_exhausted"
#: Who decides a `spec` record. The route checks the role itself; this is
#: what the inbox shows.
SPEC_APPROVERS = "platform_admin"
#: Expired requests of one digest asked again, at most. Past it the spec is
#: edited (a new digest) rather than asked a fifty-first time.
MAX_ROUNDS = 50
#: The only merge choice a firing submits with (§3.12: `merge: auto` is
#: never allowed).
MERGE_CHOICE = "off"

WORKFLOWS_NOT_HELD = (
    "the onboarding GitHub App holds Workflows: No access (docs/schedules.md §0.1), so a "
    "change to .github/workflows/ is refused by GitHub at push for an onboarding-connected tenant"
)


class CustomPromptRepositoryOutOfScope(Forbidden):
    code = "custom_prompt_repository_out_of_scope"


# --------------------------------------------------------------------------
# The digest and its approval
# --------------------------------------------------------------------------


def spec_json(spec: WorkflowCreate) -> dict[str, Any]:
    """The validated spec as JSON. A step's `model_class` (#1435) is left out
    when it names none, so a spec saved before steps had a class keeps the
    digest an admin approved; a step that names one has it in the digest, so
    changing a step's class is a new spec to approve."""
    data = spec.model_dump(mode="json")
    for step in data.get("steps") or []:
        if step.get("model_class") is None:
            step.pop("model_class", None)
    return data


def spec_digest(spec: WorkflowCreate) -> str:
    """sha256 of the validated spec, keys sorted: what is approved is what runs."""
    canonical = json.dumps(spec_json(spec), sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def spec_key(schedule_id: str, digest: str, round_: int) -> str:
    return f"{schedule_id}\n{digest}\n{round_}"


def _read(db: Any, tenant_id: str, item_id: str) -> dict[str, Any] | None:
    snap = db.collection(approvals.COLLECTION).document(item_id).get()
    doc = snap.to_dict() if snap.exists else None
    if doc is None:
        return None
    if doc.get("tenant_id") != tenant_id or doc.get("kind") != approvals.SPEC:
        # A record under this id that is not this tenant's spec approval is
        # never read as one (invariant 9).
        raise LookupError(f"approval {item_id} is not tenant {tenant_id}'s spec approval")
    return doc


def find_spec_approval(db: Any, schedule: Mapping[str, Any], digest: str) -> tuple[dict[str, Any] | None, int]:
    """(the live record of this schedule's digest, the round to ask next).

    Live: pending, approved or rejected. An expired or superseded round is
    passed over. (None, n): no live record, and round n is the one to ask.
    """
    tenant_id = str(schedule["tenant_id"])
    schedule_id = str(schedule["schedule_id"])
    for round_ in range(MAX_ROUNDS):
        doc = _read(db, tenant_id, approvals.approval_id(approvals.SPEC, spec_key(schedule_id, digest, round_)))
        if doc is None:
            return None, round_
        if doc.get("state") in (approvals.EXPIRED, approvals.SUPERSEDED):
            continue
        return doc, round_
    return None, MAX_ROUNDS


def _summary(schedule: Mapping[str, Any], spec: WorkflowCreate, digest: str) -> str:
    profiles = sorted({step.runner_profile for step in spec.steps})
    return (f"{schedule.get('name')}: custom-prompt spec {digest[:19]} ({len(spec.steps)} steps, "
            f"profiles {', '.join(profiles)}) waits for a platform admin")


def request_spec_approval(ctx: Any, schedule: Mapping[str, Any], spec: WorkflowCreate,
                          digest: str) -> dict[str, Any] | None:
    """The live record for the digest, asked for when there is none. Idempotent.

    None only when `MAX_ROUNDS` requests of this digest all expired.
    """
    doc, round_ = find_spec_approval(ctx.db, schedule, digest)
    if doc is not None:
        return doc
    if round_ >= MAX_ROUNDS:
        return None
    schedule_id = str(schedule["schedule_id"])
    return approvals.request(
        ctx.db, ctx.now(), tenant_id=str(schedule["tenant_id"]), kind=approvals.SPEC,
        key=spec_key(schedule_id, digest, round_),
        subject={"schedule_id": schedule_id, "spec_digest": digest},
        digest=digest,
        summary=_summary(schedule, spec, digest),
        approvers=SPEC_APPROVERS,
        ttl=approvals.ttl_hours(schedule),
        # What the admin approves is shown with the record, and a standing
        # instruction is merge-tier (R3, §4.1).
        extra={"merge_tier": True, "spec": spec_json(spec), "round": round_},
    )


def _unapproved_code(doc: Mapping[str, Any] | None) -> str | None:
    if doc is None:
        return SPEC_ROUNDS_EXHAUSTED
    state = doc.get("state")
    if state == approvals.APPROVED:
        return None
    if state == approvals.REJECTED:
        return SPEC_REJECTED
    return SPEC_PENDING


# --------------------------------------------------------------------------
# The repository and the submitted spec
# --------------------------------------------------------------------------


def repository_in_scope(firing: Any, spec: WorkflowCreate) -> tuple[str | None, bool]:
    """(the spec repository's registration id, whether it is in the firing's scope).

    (None, True) for a spec with no repository.
    """
    if not spec.repository_url:
        return None, True
    named = merge_repository(spec.repository_url)
    if named is None:
        return None, False
    repo_id = repo_id_for(str(firing.schedule["tenant_id"]), *named)
    return repo_id, repo_id in firing.repo_ids


def submitted_spec(spec: WorkflowCreate) -> WorkflowCreate:
    """The spec as submitted: `metadata.merge` forced off (§3.12)."""
    return spec.model_copy(update={"metadata": {**spec.metadata, MERGE_METADATA_KEY: MERGE_CHOICE}})


def _merge_steps(spec: WorkflowCreate) -> list[str]:
    return [step.step_id for step in spec.steps if is_merge_step(step.runner_profile)]


def _unknown_profiles(spec: WorkflowCreate) -> list[str]:
    return sorted({step.runner_profile for step in spec.steps if step.runner_profile not in RUNNER_PROFILES})


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    spec: WorkflowCreate = params.spec
    schedule = firing.schedule
    digest = spec_digest(spec)
    if firing.room <= 0:
        log.info("schedule %s firing %s: no room for a workflow", schedule["schedule_id"],
                 firing.firing["firing_id"])
        return []
    doc = request_spec_approval(firing.ctx, schedule, spec, digest)
    code = _unapproved_code(doc)
    if code is not None:
        log.warning("schedule %s firing %s: spec %s not approved (%s); nothing submitted",
                    schedule["schedule_id"], firing.firing["firing_id"], digest[:19], code)
        return [firing.api_action(SPEC_ACTION, outcome="failed", detail={
            "code": code, "spec_digest": digest,
            "approval_id": (doc or {}).get("approval_id"),
            "state": (doc or {}).get("state"),
        })]
    repo_id, in_scope = repository_in_scope(firing, spec)
    if not in_scope:
        # A new refusal ships report-only (refusals.py): off, the workflow is
        # submitted and the submission's own grant checks decide.
        refusals.refuse(CustomPromptRepositoryOutOfScope(
            "the spec's repository is not in this schedule's scope",
            detail={"repo_id": repo_id, "scope": list(firing.repo_ids)},
        ))
    item = firing.submit_workflow(submitted_spec(spec), repo_id=repo_id)
    log.info("schedule %s firing %s: workflow %s from spec %s", schedule["schedule_id"],
             firing.firing["firing_id"], item["id"], digest[:19])
    return [item]


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    spec: WorkflowCreate = params.spec
    digest = spec_digest(spec)
    doc, _round = find_spec_approval(firing.ctx.db, firing.schedule, digest)
    repo_id, in_scope = repository_in_scope(firing, spec)
    merge_steps = _merge_steps(spec)
    code = _unapproved_code(doc)
    if doc is None:
        would = "request_spec_approval"
    elif code is not None:
        would = code
    elif not in_scope:
        would = "refuse_repository_out_of_scope"
    elif merge_steps:
        would = "refused_at_submission_merge_step"
    elif firing.room <= 0:
        would = "room"
    else:
        would = "submit_workflow"
    return {
        "would": would,
        "spec_digest": digest,
        "spec_approval": {
            "approval_id": (doc or {}).get("approval_id"),
            "state": (doc or {}).get("state"),
        },
        "steps": [{"step_id": step.step_id, "runner_profile": step.runner_profile} for step in spec.steps],
        "unknown_profiles": _unknown_profiles(spec),
        "max_steps": params.max_steps,
        "repository": {"repo_id": repo_id, "in_scope": in_scope},
        # `metadata.merge` is submitted "off": no merge step is appended, and
        # a spec's own merge step is refused by the submission.
        "merge": MERGE_CHOICE,
        "merge_steps": merge_steps,
        "room": firing.room,
        "forge": {"needs": list(scheduletypes.get(TYPE).forge_permissions), "not_held": WORKFLOWS_NOT_HELD},
    }

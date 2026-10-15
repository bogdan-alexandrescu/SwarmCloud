"""Does a task need a workspace secret nobody has granted? Asked before the lease.

docs/design/workspace-secrets.md §4.2: "A required slot with no enabled grant
parks the task CREDENTIAL_MISSING **before** it leases, at no cost, and it
starts by itself once the grant is ticked, which is today's rule for a missing
provider key." `credentials.credential_for` asks this module as the last half
of its one question, so admission (`loop.Scheduler._admit_one`) parks on it
before the capacity transaction (invariants 1, 2 and 3), and the credential
sweep (`loop.Scheduler._promote_credentials`) returns the park to READY once
every required slot has an enabled grant (lane SEC-ADMIT-PARK, #1347).

THE WORKER'S PARK IS THE BACKSTOP, AND STAYS. SEC-RELEASE built the worker's
own park (`agent_worker.secretrelease`, `lifecycle.py` step 6a): after the
lease, after a pod started, when the release left a required slot empty. It
still catches what admission cannot see: swarm-api unreachable, a grant whose
secret was deleted or is of a kind its slot refuses, a personal secret whose
owner has left the group since this pass read it.

WHICH CONSUMERS A TASK IS, read the release's way (`swarm_api.workspace_
secrets.attempt_consumers`): `profile:<runner_profile>` when the profile
declares a slot, and `tool:<t>` for each RUNTIME_TOOLS entry its signed
`browser_tool` launches (`swarm_common.profiles.tools_launched`, which the
dispatcher's `consumes_workspace_secrets` also reads). The release also derives
`component:merge` and `component:indexer`, which declare no slot, so they can
require nothing and are not derived here. The spelling is `consumer_name`'s.
The scheduler cannot import swarm-api, and the frozen module has no home for
the release's derivation, so it is composed here from the same frozen pieces
and held to swarm-api's function by tests/unit/control_plane/
test_secret_slot_admission.py over every profile and every `browser_tool`.

A GRANT IS PRESENT WHEN IT IS ENABLED (§2.2). Admission reads no secret
record of the workspace's own, and no `attestation` (lane SEC-GRANT-ATTEST).
The release is where a grant is proven: a forged grant can at worst admit a
task whose worker then parks it. A grant that names no slot fills its
consumer's only slot, as swarm-api's `Consumer.slot(None)` does.

A PERSON'S OWN SECRET, ON A GROUP TASK (owner decision Q3). It counts when the
release would match it, except for the one thing the scheduler cannot ask: the
release re-checks the submitter's membership of the group NOW, against the
directory, and admission does not. The record must be the submitter's personal
tenant's own, `scope: personal`, `active`, opted in to this workspace
(`use_in_workspaces`), created by the submitter, and the personal tenant's
document must name them as its principal. A person who has since left the
group is admitted and parked by the worker.

READ ONCE PER PASS. One query of every enabled grant, made lazily: only a task
whose profile or launched tool declares a REQUIRED slot reads anything, and no
catalogue slot is required yet (contract request 80), so today a pass reads
nothing extra. `forget()` is called at the top of each run and each admission
pass, so a grant ticked between two passes is seen by the second.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable, Mapping

from swarm_common.identity import tenant_id_for_user
from swarm_common.models import Task, Tenant, consumer_name
from swarm_common.profiles import RUNTIME_TOOLS, RunnerProfile, SecretSlot, tools_launched

#: swarm-api's collections and spellings (swarm_api/workspace_secrets.py:
#: GRANTS_COLLECTION, SECRETS_COLLECTION, SCOPE_PERSONAL, ACTIVE). The
#: scheduler cannot import swarm-api; a test holds each to its source.
GRANTS_COLLECTION = "workspace_secret_grants"
SECRETS_COLLECTION = "workspace_secrets"
SCOPE_PERSONAL = "personal"
ACTIVE = "active"


@dataclass(frozen=True)
class MissingSlot:
    """A required slot no enabled grant fills. Names only, never a secret id."""

    consumer: str
    slot: str

    def to_detail(self) -> dict[str, str]:
        return {"consumer": self.consumer, "slot": self.slot}


#: A grant as admission counts it: (consumer, slot), the slot None when the
#: grant names none.
Granted = tuple[str, str | None]


def slot_consumers(
    profile: RunnerProfile, inputs: Mapping[str, Any] | None
) -> tuple[tuple[str, Mapping[str, SecretSlot]], ...]:
    """Each consumer an attempt of `profile` with `inputs` is that declares a
    slot, with its declared slots. The release's derivation (module
    docstring), less the components, which declare none."""
    out: list[tuple[str, Mapping[str, SecretSlot]]] = []
    if profile.secret_slots:
        out.append((consumer_name("profile", profile.name), profile.secret_slots))
    for name in tools_launched(profile, inputs if isinstance(inputs, Mapping) else {}):
        slots = RUNTIME_TOOLS[name].secret_slots
        if slots:
            out.append((consumer_name("tool", name), slots))
    return tuple(out)


def required_slots(profile: RunnerProfile, inputs: Mapping[str, Any] | None) -> tuple[MissingSlot, ...]:
    """Every slot `profile`, or a tool it launches with `inputs`, declares required."""
    return tuple(
        MissingSlot(consumer, slot)
        for consumer, slots in slot_consumers(profile, inputs)
        for slot, declared in sorted(slots.items())
        if declared.required
    )


def missing_required_slots(
    profile: RunnerProfile,
    inputs: Mapping[str, Any] | None,
    granted: Collection[Granted],
) -> tuple[MissingSlot, ...]:
    """THE RULE: the required slots of this task that no enabled grant fills.

    `granted` is the (consumer, slot) of every enabled grant the task may
    use. A grant fills the slot it names, or, naming none, its consumer's
    only slot. Pure: no read, no clock."""
    missing: list[MissingSlot] = []
    for consumer, slots in slot_consumers(profile, inputs):
        only = next(iter(slots)) if len(slots) == 1 else None
        filled = {slot if slot is not None else only for c, slot in granted if c == consumer}
        missing.extend(
            MissingSlot(consumer, slot)
            for slot, declared in sorted(slots.items())
            if declared.required and slot not in filled
        )
    return tuple(missing)


class WorkspaceGrants:
    """Every enabled grant, read in one query at most once per pass.

    Kept by tenant, and a grant counts only for the tenant its own document
    names (invariant 9): the release reads a workspace's grants the same way.
    Personal records (Q3) are read by id, once per pass each, and only for a
    group task whose own grants leave a required slot empty."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._by_tenant: dict[str, list[tuple[str, Granted]]] | None = None
        self._records: dict[str, dict[str, Any] | None] = {}

    def forget(self) -> None:
        self._by_tenant = None
        self._records = {}

    def _grants(self, tenant_id: str) -> list[tuple[str, Granted]]:
        """(secret_id, (consumer, slot)) of each enabled grant of `tenant_id`."""
        if self._by_tenant is None:
            # Imported here, not at module top: kubernetes/render.py imports
            # dispatch -> credentials -> this module, and the CI jobs that
            # render manifests (checkov, kubernetes manifests) install no
            # google-cloud-firestore.
            from google.cloud.firestore_v1.base_query import FieldFilter

            by_tenant: dict[str, list[tuple[str, Granted]]] = {}
            query = self._db.collection(GRANTS_COLLECTION).where(
                filter=FieldFilter("enabled", "==", True)
            )
            for snap in query.stream():
                doc = snap.to_dict() or {}
                tenant, consumer = doc.get("tenant_id"), doc.get("consumer")
                if doc.get("enabled") is not True or not isinstance(tenant, str) \
                        or not isinstance(consumer, str):
                    continue
                slot = doc.get("slot") if isinstance(doc.get("slot"), str) else None
                by_tenant.setdefault(tenant, []).append(
                    (str(doc.get("secret_id") or ""), (consumer, slot))
                )
            self._by_tenant = by_tenant
        return self._by_tenant.get(tenant_id, [])

    def _record(self, secret_id: str) -> dict[str, Any] | None:
        if secret_id not in self._records:
            snap = self._db.collection(SECRETS_COLLECTION).document(secret_id).get()
            self._records[secret_id] = (snap.to_dict() or None) if snap.exists else None
        return self._records[secret_id]

    def _personal(
        self, task: Task, tenant: Tenant, tenant_of: Callable[[str], Tenant | None]
    ) -> Iterable[Granted]:
        """Q3: the submitter's own opted-in grants, on a group task."""
        email = str(task.submitted_by or "").strip().lower()
        if tenant.kind != "group" or "@" not in email:
            return ()
        personal = tenant_id_for_user(email)
        if personal == task.tenant_id:
            return ()
        owner = tenant_of(personal)
        if owner is None or owner.kind != "user" or owner.principal.strip().lower() != email:
            return ()
        out: list[Granted] = []
        for secret_id, granted in self._grants(personal):
            record = self._record(secret_id) if secret_id else None
            if (
                record is not None
                and record.get("secret_id") == secret_id
                and record.get("tenant_id") == personal
                and record.get("state") == ACTIVE
                and record.get("scope") == SCOPE_PERSONAL
                and task.tenant_id in (record.get("use_in_workspaces") or [])
                and str(record.get("created_by") or "").strip().lower() == email
            ):
                out.append(granted)
        return out

    def missing(
        self,
        task: Task,
        profile: RunnerProfile,
        tenant: Tenant,
        tenant_of: Callable[[str], Tenant | None],
    ) -> tuple[MissingSlot, ...] | None:
        """The task's required slots that no grant it may use fills, empty
        when every one is filled. None, reading nothing, when the task
        requires no slot."""
        if not required_slots(profile, task.input):
            return None
        own = [granted for _, granted in self._grants(task.tenant_id)]
        missing = missing_required_slots(profile, task.input, own)
        if missing:
            mine = list(self._personal(task, tenant, tenant_of))
            if mine:
                missing = missing_required_slots(profile, task.input, own + mine)
        return missing


__all__ = [
    "MissingSlot",
    "WorkspaceGrants",
    "missing_required_slots",
    "required_slots",
    "slot_consumers",
]
